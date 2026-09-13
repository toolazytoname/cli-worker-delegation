#!/usr/bin/env python3
"""Delegate a bounded task to local Cursor CLI or Grok CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_CURSOR_MODEL = "cursor-grok-4.6-xhigh"
DEFAULT_GROK_MODEL = "grok-4.6"
DEFAULT_EFFORT = "xhigh"
DEFAULT_TIMEOUT = 600
DEFAULT_MAX_TURNS = 30
RESULT_CHAR_LIMIT = 2000
KILL_GRACE_SECONDS = 3.0
CURSOR_HELP_TIMEOUT = 5
CURSOR_HELP_REQUIRED_FLAGS = ("--print", "--workspace", "--output-format")
OUTPUT_DIR_MODE = 0o700

CURSOR_AGENT_BINARY = "cursor-agent"
CURSOR_FALLBACK_BINARY = "cursor"
GROK_BINARY = "grok"

WORKER_CONSTRAINTS = """\
你是被委派的 CLI worker，不是独立核验者。
- 遵守工作区适用指令（如 AGENTS.md、规则文件）与原始/只读路径不可改写约束。
- 只使用任务明确授权的路径。
- 未在任务中明确授权时，禁止读取密钥或凭据、commit、publish、push、删除、联网。
- 禁止再启动嵌套 agent/subagent。
- 权限旗标不是沙箱，不能当作隔离或安全边界。
- 最终回复须简洁：变更路径、测试、证据、阻塞；不得声称本回复是独立核验。
"""

READ_MODE_CONSTRAINTS = (
    "当前为只读模式：禁止任何编辑、写入或删除。这不是 OS 级沙箱。"
)
EDIT_MODE_CONSTRAINTS = "当前为编辑模式：仅修改任务授权的路径。"


class DelegateError(Exception):
    """Validation or launch error before/without treating a worker as successful."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="delegate.py",
        description=(
            "Delegate a bounded task to a local Cursor CLI (preferred) or Grok CLI. "
            "Stdout is a small JSON summary; full logs go to --output-dir."
        ),
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "cursor", "grok"),
        default="auto",
        help="CLI family. auto uses cursor-agent, then cursor, else grok (default: auto).",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="Existing workspace directory (child cwd).",
    )
    parser.add_argument(
        "--task-file",
        required=True,
        dest="task_file",
        help="Existing UTF-8 task file.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        dest="output_dir",
        help="New directory that must not already exist and must be outside --workspace.",
    )
    parser.add_argument(
        "--mode",
        choices=("read", "edit"),
        default="read",
        help="read forbids edits in the worker prompt (default: read).",
    )
    parser.add_argument(
        "--allow-unattended-writes",
        action="store_true",
        help="Required for --mode edit. Adds Cursor --force or Grok bypassPermissions.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=DEFAULT_TIMEOUT,
        help=f"Positive seconds before killing the worker process group (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model id. Defaults: Cursor cursor-grok-4.6-xhigh, Grok grok-4.6. Never silently changed.",
    )
    parser.add_argument(
        "--effort",
        default=DEFAULT_EFFORT,
        help="Grok --reasoning-effort (default: xhigh). Ignored for Cursor argv.",
    )
    parser.add_argument(
        "--max-turns",
        type=positive_int,
        default=DEFAULT_MAX_TURNS,
        dest="max_turns",
        help=f"Grok --max-turns (default: {DEFAULT_MAX_TURNS}).",
    )
    return parser.parse_args(argv)


def resolve_existing_dir(path: str, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise DelegateError(f"{label} must be an existing directory: {path}")
    return resolved


def resolve_existing_file(path: str, *, label: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise DelegateError(f"{label} must be an existing file: {path}")
    return resolved


def read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise DelegateError(f"task file is not valid UTF-8: {path}") from exc


def is_inside(path: Path, parent: Path) -> bool:
    path = path.resolve()
    parent = parent.resolve()
    return path == parent or parent in path.parents


def resolve_new_output_dir(path: str, workspace: Path) -> Path:
    output = Path(path).expanduser()
    if output.exists():
        raise DelegateError(f"output-dir must not already exist: {path}")
    resolved = output.resolve()
    if is_inside(resolved, workspace):
        raise DelegateError("output-dir must be outside the workspace")
    return resolved


def which_binary(name: str) -> str | None:
    return shutil.which(name)


def probe_cursor_help(binary_path: str) -> bool:
    """Probe fallback `cursor --help` only. Not task execution or failover."""
    try:
        completed = subprocess.run(
            [binary_path, "--help"],
            capture_output=True,
            timeout=CURSOR_HELP_TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    blob = f"{completed.stdout}\n{completed.stderr}"
    return all(flag in blob for flag in CURSOR_HELP_REQUIRED_FLAGS)


def discover_cursor() -> tuple[str, str] | None:
    agent = which_binary(CURSOR_AGENT_BINARY)
    if agent:
        return CURSOR_AGENT_BINARY, agent
    fallback = which_binary(CURSOR_FALLBACK_BINARY)
    if fallback and probe_cursor_help(fallback):
        return CURSOR_FALLBACK_BINARY, fallback
    return None


def discover_grok() -> tuple[str, str] | None:
    found = which_binary(GROK_BINARY)
    if found:
        return GROK_BINARY, found
    return None


def resolve_provider(provider: str) -> tuple[str, str, str]:
    """Return (provider, binary_name, binary_path). Never uses a binary named agent."""
    cursor = discover_cursor()
    grok = discover_grok()
    if provider == "cursor":
        if cursor is None:
            if (
                which_binary(CURSOR_AGENT_BINARY) is None
                and which_binary(CURSOR_FALLBACK_BINARY) is not None
            ):
                raise DelegateError(
                    "cursor CLI incompatible (fallback cursor --help missing "
                    "--print, --workspace, or --output-format)"
                )
            raise DelegateError(
                "cursor CLI not found (tried cursor-agent, then cursor)"
            )
        return "cursor", cursor[0], cursor[1]
    if provider == "grok":
        if grok is None:
            raise DelegateError("grok CLI not found")
        return "grok", grok[0], grok[1]
    if cursor is not None:
        return "cursor", cursor[0], cursor[1]
    if grok is not None:
        return "grok", grok[0], grok[1]
    raise DelegateError(
        "no CLI found (tried cursor-agent, then cursor, then grok)"
    )


def default_model(provider: str) -> str:
    if provider == "cursor":
        return DEFAULT_CURSOR_MODEL
    return DEFAULT_GROK_MODEL


def build_request_markdown(*, task: str, mode: str) -> str:
    mode_block = READ_MODE_CONSTRAINTS if mode == "read" else EDIT_MODE_CONSTRAINTS
    return (
        "# Worker constraints\n\n"
        f"{WORKER_CONSTRAINTS.rstrip()}\n\n"
        f"{mode_block}\n\n"
        "# Task\n\n"
        f"{task.rstrip()}\n"
    )


def build_cursor_argv(
    *,
    binary_path: str,
    workspace: Path,
    model: str,
    mode: str,
    allow_unattended_writes: bool,
    prompt: str,
) -> list[str]:
    argv = [
        binary_path,
        "--print",
        "--trust",
        "--workspace",
        str(workspace),
        "--model",
        model,
        "--output-format",
        "json",
    ]
    if mode == "read":
        argv.extend(["--mode", "ask"])
    elif allow_unattended_writes:
        argv.append("--force")
    argv.append(prompt)
    return argv


def build_grok_argv(
    *,
    binary_path: str,
    workspace: Path,
    model: str,
    effort: str,
    max_turns: int,
    mode: str,
    allow_unattended_writes: bool,
    prompt: str,
) -> list[str]:
    permission = "plan"
    if mode == "edit" and allow_unattended_writes:
        permission = "bypassPermissions"
    return [
        binary_path,
        "--cwd",
        str(workspace),
        "--model",
        model,
        "--reasoning-effort",
        effort,
        "--no-subagents",
        "--disable-web-search",
        "--max-turns",
        str(max_turns),
        "--permission-mode",
        permission,
        "-p",
        prompt,
    ]


def parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def flag_true(value: Any) -> bool:
    if value is True or value == 1:
        return True
    if isinstance(value, str) and value.lower() in {"true", "1", "yes"}:
        return True
    return False


def is_cursor_result_payload(data: dict[str, Any]) -> bool:
    return data.get("type") == "result"


def extract_result_text(
    *,
    provider: str,
    stdout: str,
    parsed: dict[str, Any] | None,
) -> str:
    if provider == "cursor" and parsed is not None:
        value = parsed.get("result")
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
    return stdout


def truncate_chars(text: str, limit: int = RESULT_CHAR_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def classify_status(
    *,
    provider: str,
    child_exit_code: int | None,
    timed_out: bool,
    stdout: str,
) -> tuple[str, dict[str, Any] | None]:
    if timed_out:
        return "timeout", parse_json_object(stdout) if provider == "cursor" else None
    parsed = parse_json_object(stdout) if provider == "cursor" else None
    if provider == "cursor":
        if child_exit_code != 0:
            return "failed", parsed
        if parsed is None:
            return "failed", None
        if not is_cursor_result_payload(parsed):
            return "failed", parsed
        if flag_true(parsed.get("is_error")):
            return "failed", parsed
        if "result" not in parsed:
            return "failed", parsed
        subtype = parsed.get("subtype")
        if subtype is not None and str(subtype).strip() != "":
            lowered = str(subtype).strip().lower()
            if lowered.startswith("error") or lowered != "success":
                return "failed", parsed
        return "completed_unverified", parsed
    if child_exit_code == 0:
        return "completed_unverified", None
    return "failed", None


def _kill_process_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, OSError):
        pass


def terminate_process_group(proc: subprocess.Popen[bytes], grace: float = KILL_GRACE_SECONDS) -> None:
    pgid = proc.pid
    _kill_process_group(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:
        _kill_process_group(pgid, signal.SIGKILL)
        raise
    # Always SIGKILL the original group after grace, even if the leader already exited.
    _kill_process_group(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    except KeyboardInterrupt:
        _kill_process_group(pgid, signal.SIGKILL)
        raise


def run_worker(
    argv: list[str],
    *,
    workspace: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
) -> tuple[int | None, bool]:
    timed_out = False
    with stdout_path.open("wb") as out_f, stderr_path.open("wb") as err_f:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(workspace),
                stdout=out_f,
                stderr=err_f,
                start_new_session=True,
            )
        except OSError as exc:
            raise DelegateError(f"failed to launch worker: {exc}") from exc
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group(proc)
        except KeyboardInterrupt:
            terminate_process_group(proc)
            raise
    return proc.returncode, timed_out


def emit_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def fail_summary(
    message: str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "failed",
        "timed_out": False,
        "exit_code": None,
        "error": message,
        "result": "",
    }
    if extra:
        payload.update(extra)
    return payload


def write_result_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def delegate(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.mode == "edit" and not args.allow_unattended_writes:
        raise DelegateError(
            "edit mode requires --allow-unattended-writes (refusing to launch)"
        )

    workspace = resolve_existing_dir(args.workspace, label="workspace")
    task_file = resolve_existing_file(args.task_file, label="task-file")
    output_dir = resolve_new_output_dir(args.output_dir, workspace)
    task = read_utf8(task_file)

    provider, binary_name, binary_path = resolve_provider(args.provider)
    model = args.model if args.model else default_model(provider)
    prompt = build_request_markdown(task=task, mode=args.mode)

    if provider == "cursor":
        argv = build_cursor_argv(
            binary_path=binary_path,
            workspace=workspace,
            model=model,
            mode=args.mode,
            allow_unattended_writes=args.allow_unattended_writes,
            prompt=prompt,
        )
    else:
        argv = build_grok_argv(
            binary_path=binary_path,
            workspace=workspace,
            model=model,
            effort=args.effort,
            max_turns=args.max_turns,
            mode=args.mode,
            allow_unattended_writes=args.allow_unattended_writes,
            prompt=prompt,
        )

    try:
        output_dir.mkdir(parents=False, exist_ok=False, mode=OUTPUT_DIR_MODE)
        os.chmod(output_dir, OUTPUT_DIR_MODE)
    except FileExistsError as exc:
        raise DelegateError(f"output-dir must not already exist: {output_dir}") from exc
    except OSError as exc:
        raise DelegateError(f"cannot create output-dir: {exc}") from exc

    request_path = output_dir / "request.md"
    stdout_path = output_dir / "stdout.log"
    stderr_path = output_dir / "stderr.log"
    result_path = output_dir / "result.json"
    request_path.write_text(prompt, encoding="utf-8")

    try:
        child_exit, timed_out = run_worker(
            argv,
            workspace=workspace,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout=args.timeout,
        )
    except DelegateError as exc:
        error = str(exc)
        full_fail: dict[str, Any] = {
            "provider": provider,
            "binary": binary_name,
            "binary_path": binary_path,
            "model": model,
            "mode": args.mode,
            "allow_unattended_writes": bool(args.allow_unattended_writes),
            "workspace": str(workspace),
            "task_file": str(task_file),
            "output_dir": str(output_dir),
            "exit_code": None,
            "status": "failed",
            "timed_out": False,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "request_path": str(request_path),
            "result_json": str(result_path),
            "result": "",
            "error": error,
        }
        if provider == "grok":
            full_fail["effort"] = args.effort
            full_fail["max_turns"] = args.max_turns
        write_result_json(result_path, full_fail)
        summary_fail: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "exit_code": None,
            "status": "failed",
            "timed_out": False,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "request_path": str(request_path),
            "result_json": str(result_path),
            "result": "",
            "error": error,
        }
        if provider == "grok":
            summary_fail["effort"] = args.effort
        return summary_fail, 1
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
    status, parsed = classify_status(
        provider=provider,
        child_exit_code=child_exit,
        timed_out=timed_out,
        stdout=stdout_text,
    )
    result_text = extract_result_text(
        provider=provider,
        stdout=stdout_text,
        parsed=parsed,
    )

    full: dict[str, Any] = {
        "provider": provider,
        "binary": binary_name,
        "binary_path": binary_path,
        "model": model,
        "mode": args.mode,
        "allow_unattended_writes": bool(args.allow_unattended_writes),
        "workspace": str(workspace),
        "task_file": str(task_file),
        "output_dir": str(output_dir),
        "argv": argv,
        "exit_code": child_exit,
        "status": status,
        "timed_out": timed_out,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "request_path": str(request_path),
        "result_json": str(result_path),
        "result": result_text,
        "stderr_preview": truncate_chars(stderr_text),
    }
    if provider == "grok":
        full["effort"] = args.effort
        full["max_turns"] = args.max_turns
    if parsed is not None:
        full["parsed_cursor_json"] = parsed

    write_result_json(result_path, full)

    summary: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "exit_code": child_exit,
        "status": status,
        "timed_out": timed_out,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "request_path": str(request_path),
        "result_json": str(result_path),
        "result": truncate_chars(result_text),
    }
    if provider == "grok":
        summary["effort"] = args.effort
    script_exit = 0 if status == "completed_unverified" else 1
    return summary, script_exit


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary, code = delegate(args)
    except DelegateError as exc:
        extra = dict(exc.details)
        emit_json(fail_summary(str(exc), extra=extra))
        return 1
    emit_json(summary)
    return code


def _entry() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    _entry()
