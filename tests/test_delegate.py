#!/usr/bin/env python3
"""Practical tests for scripts/delegate.py. Fake CLIs only; no paid calls."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
DELEGATE = SKILL_ROOT / "scripts" / "delegate.py"

CURSOR_OK = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "worker-ok",
    },
    ensure_ascii=False,
)


def load_delegate():
    import importlib.util

    spec = importlib.util.spec_from_file_location("cli_worker_delegate", DELEGATE)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load delegate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


FAKE_CLI_BODY = r"""
import json
import os
import signal
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
if "--help" in sys.argv:
    help_text = os.environ.get(
        "FAKE_HELP",
        "Usage:\n  --print\n  --workspace DIR\n  --output-format json\n",
    )
    sys.stdout.write(help_text)
    sys.stdout.flush()
    raise SystemExit(0)

logdir = Path(os.environ["ARGV_DIR"])
record = {
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "pid": os.getpid(),
}
(logdir / f"{name}.argv.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
with (logdir / f"{name}.invocations").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv, ensure_ascii=False) + "\n")
(logdir / f"{name}.ran").write_text("1", encoding="utf-8")

if os.environ.get("FAKE_IGNORE_SIGTERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

sleep_s = float(os.environ.get("FAKE_SLEEP", "0") or "0")
fork_child = os.environ.get("FAKE_FORK") == "1"
child_pid = None
if fork_child:
    child_pid = os.fork()
    if child_pid == 0:
        if os.environ.get("FAKE_CHILD_IGNORE_SIGTERM") == "1":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (logdir / f"{name}.child_pid").write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(float(os.environ.get("FAKE_CHILD_SLEEP", "60") or "60"))
        os._exit(0)
    (logdir / f"{name}.child_pid").write_text(str(child_pid), encoding="utf-8")

if sleep_s:
    time.sleep(sleep_s)

n = int(os.environ.get("FAKE_STDOUT_BYTES", "0") or "0")
chunk = b"x" * 65536
left = n
while left:
    piece = chunk if left >= len(chunk) else chunk[:left]
    sys.stdout.buffer.write(piece)
    left -= len(piece)

err_n = int(os.environ.get("FAKE_STDERR_BYTES", "0") or "0")
left = err_n
while left:
    piece = chunk if left >= len(chunk) else chunk[:left]
    sys.stderr.buffer.write(piece)
    left -= len(piece)

stdout = os.environ.get("FAKE_STDOUT", "")
stderr = os.environ.get("FAKE_STDERR", "")
if stdout:
    sys.stdout.buffer.write(stdout.encode("utf-8"))
if stderr:
    sys.stderr.buffer.write(stderr.encode("utf-8"))
sys.stdout.buffer.flush()
sys.stderr.buffer.flush()

if child_pid:
    os.waitpid(child_pid, 0)

raise SystemExit(int(os.environ.get("FAKE_EXIT", "0") or "0"))
"""


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class DelegateHarness:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="delegate-test-"))
        self.workspace = self.workspace_dir()
        self.workspace.mkdir()
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.argv_dir = self.root / "argv"
        self.argv_dir.mkdir()
        self.task = self.workspace / "task.md"
        self.task.write_text("do the bounded task\n", encoding="utf-8")
        self._run_i = 0
        self.env = os.environ.copy()
        # Hermetic PATH: fake CLIs have an absolute Python shebang.
        self.env["PATH"] = str(self.bindir)
        self.env["ARGV_DIR"] = str(self.argv_dir)
        self.env.pop("FAKE_STDOUT", None)
        self.env.pop("FAKE_STDERR", None)
        self.env.pop("FAKE_EXIT", None)
        self.env.pop("FAKE_SLEEP", None)
        self.env.pop("FAKE_FORK", None)
        self.env.pop("FAKE_IGNORE_SIGTERM", None)
        self.env.pop("FAKE_CHILD_IGNORE_SIGTERM", None)
        self.env.pop("FAKE_HELP", None)
        self.env.pop("FAKE_STDOUT_BYTES", None)
        self.env.pop("FAKE_STDERR_BYTES", None)

    def workspace_dir(self) -> Path:
        return self.root / "ws"

    def add_fake(self, name: str) -> Path:
        path = self.bindir / name
        write_executable(path, FAKE_CLI_BODY)
        return path

    def next_output(self, name: str | None = None) -> Path:
        self._run_i += 1
        label = name or f"run{self._run_i}"
        return self.outside / label

    def run(self, extra: list[str], *, output: Path | None = None) -> subprocess.CompletedProcess[str]:
        out = output or self.next_output()
        cmd = [
            sys.executable,
            str(DELEGATE),
            "--workspace",
            str(self.workspace),
            "--task-file",
            str(self.task),
            "--output-dir",
            str(out),
            *extra,
        ]
        return subprocess.run(
            cmd,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def summary(self, proc: subprocess.CompletedProcess[str]) -> dict:
        text = proc.stdout.strip()
        self.assert_json_stdout(proc)
        return json.loads(text)

    def assert_json_stdout(self, proc: subprocess.CompletedProcess[str]) -> None:
        if not proc.stdout.strip():
            raise AssertionError(f"empty stdout, stderr={proc.stderr!r}")
        json.loads(proc.stdout.strip().splitlines()[-1])

    def argv_record(self, name: str) -> dict:
        path = self.argv_dir / f"{name}.argv.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def ran(self, name: str) -> bool:
        return (self.argv_dir / f"{name}.ran").exists()

    def invocation_count(self, name: str) -> int:
        path = self.argv_dir / f"{name}.invocations"
        if not path.exists():
            return 0
        return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class DelegateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = DelegateHarness()

    def tearDown(self) -> None:
        self.h.cleanup()

    def _cursor_ok_env(self) -> None:
        self.h.env["FAKE_STDOUT"] = CURSOR_OK
        self.h.env["FAKE_EXIT"] = "0"

    def _grok_ok_env(self) -> None:
        self.h.env["FAKE_STDOUT"] = "grok-plain-ok\n"
        self.h.env["FAKE_EXIT"] = "0"

    def test_help_exits_zero(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(DELEGATE), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("--workspace", proc.stdout)
        self.assertIn("--allow-unattended-writes", proc.stdout)
        self.assertIn("--provider", proc.stdout)

    def test_auto_prefers_cursor_agent_over_cursor_and_grok(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.add_fake("cursor")
        self.h.add_fake("grok")
        self.h.add_fake("agent")
        self._cursor_ok_env()
        out = self.h.next_output()
        proc = self.h.run(["--provider", "auto"], output=out)
        summary = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(summary["provider"], "cursor")
        self.assertTrue(self.h.ran("cursor-agent"))
        self.assertFalse(self.h.ran("cursor"))
        self.assertFalse(self.h.ran("grok"))
        self.assertFalse(self.h.ran("agent"))
        self.assertEqual(self.h.invocation_count("cursor-agent"), 1)

    def test_auto_falls_back_to_cursor_binary(self) -> None:
        self.h.add_fake("cursor")
        self.h.add_fake("grok")
        self._cursor_ok_env()
        proc = self.h.run([])
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["provider"], "cursor")
        self.assertTrue(self.h.ran("cursor"))
        self.assertFalse(self.h.ran("grok"))
        argv = self.h.argv_record("cursor")["argv"]
        self.assertIn("--print", argv)
        self.assertNotIn("--help", argv)

    def test_auto_skips_incompatible_cursor_fallback_and_uses_grok(self) -> None:
        self.h.add_fake("cursor")
        self.h.add_fake("grok")
        self.h.env["FAKE_HELP"] = "Usage: cursor GUI editor\n"
        self._grok_ok_env()
        proc = self.h.run(["--provider", "auto"])
        summary = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(summary["provider"], "grok")
        self.assertTrue(self.h.ran("grok"))
        self.assertFalse(self.h.ran("cursor"))

    def test_explicit_cursor_fails_on_incompatible_fallback(self) -> None:
        self.h.add_fake("cursor")
        self.h.add_fake("grok")
        self.h.env["FAKE_HELP"] = "Usage: cursor GUI editor\n"
        proc = self.h.run(["--provider", "cursor"])
        self.assertNotEqual(proc.returncode, 0)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("incompatible", summary["error"])
        self.assertFalse(self.h.ran("cursor"))
        self.assertFalse(self.h.ran("grok"))

    def test_auto_uses_grok_when_no_cursor(self) -> None:
        self.h.add_fake("grok")
        self.h.add_fake("agent")
        self._grok_ok_env()
        proc = self.h.run(["--provider", "auto"])
        summary = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(summary["provider"], "grok")
        self.assertEqual(summary["effort"], "xhigh")
        self.assertTrue(self.h.ran("grok"))
        self.assertFalse(self.h.ran("agent"))

    def test_bare_agent_not_used_as_cursor(self) -> None:
        self.h.add_fake("agent")
        proc = self.h.run(["--provider", "cursor"])
        self.assertNotEqual(proc.returncode, 0)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(self.h.ran("agent"))

    def test_provider_cursor_does_not_fall_back_to_grok(self) -> None:
        self.h.add_fake("grok")
        proc = self.h.run(["--provider", "cursor"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.h.ran("grok"))
        self.assertIn("cursor CLI not found", json.loads(proc.stdout)["error"])

    def test_no_automatic_failover_after_cursor_failure(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.add_fake("grok")
        self.h.env["FAKE_STDOUT"] = "boom\n"
        self.h.env["FAKE_STDERR"] = "cursor-failed\n"
        self.h.env["FAKE_EXIT"] = "7"
        proc = self.h.run(["--provider", "auto"])
        summary = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["exit_code"], 7)
        self.assertTrue(self.h.ran("cursor-agent"))
        self.assertFalse(self.h.ran("grok"))

    def test_explicit_model_not_silently_changed(self) -> None:
        self.h.add_fake("cursor-agent")
        self._cursor_ok_env()
        proc = self.h.run(["--model", "custom-model-xyz"])
        self.assertEqual(proc.returncode, 0)
        argv = self.h.argv_record("cursor-agent")["argv"]
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "custom-model-xyz")
        self.assertEqual(json.loads(proc.stdout)["model"], "custom-model-xyz")

    def test_cursor_read_flags(self) -> None:
        self.h.add_fake("cursor-agent")
        self._cursor_ok_env()
        proc = self.h.run(["--mode", "read"])
        self.assertEqual(proc.returncode, 0)
        argv = self.h.argv_record("cursor-agent")["argv"]
        self.assertIn("--print", argv)
        self.assertIn("--trust", argv)
        self.assertEqual(argv[argv.index("--workspace") + 1], str(self.h.workspace.resolve()))
        self.assertEqual(argv[argv.index("--model") + 1], "cursor-grok-4.6-xhigh")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--mode") + 1], "ask")
        self.assertNotIn("--force", argv)
        self.assertEqual(argv[-1], Path(json.loads(proc.stdout)["request_path"]).read_text(encoding="utf-8"))
        self.assertEqual(self.h.argv_record("cursor-agent")["cwd"], str(self.h.workspace.resolve()))

    def test_cursor_edit_requires_opt_in_and_adds_force(self) -> None:
        self.h.add_fake("cursor-agent")
        proc = self.h.run(["--mode", "edit"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.h.ran("cursor-agent"))
        self.assertIn("allow-unattended-writes", json.loads(proc.stdout)["error"])

        self._cursor_ok_env()
        proc = self.h.run(["--mode", "edit", "--allow-unattended-writes"])
        self.assertEqual(proc.returncode, 0)
        argv = self.h.argv_record("cursor-agent")["argv"]
        self.assertIn("--force", argv)
        self.assertNotIn("ask", argv)

    def test_grok_read_and_edit_flags(self) -> None:
        self.h.add_fake("grok")
        self._grok_ok_env()
        proc = self.h.run(["--provider", "grok", "--mode", "read", "--max-turns", "12"])
        self.assertEqual(proc.returncode, 0)
        argv = self.h.argv_record("grok")["argv"]
        self.assertEqual(argv[argv.index("--cwd") + 1], str(self.h.workspace.resolve()))
        self.assertEqual(argv[argv.index("--model") + 1], "grok-4.6")
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "xhigh")
        self.assertIn("--no-subagents", argv)
        self.assertIn("--disable-web-search", argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "12")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertIn("-p", argv)
        self.assertNotIn("bypassPermissions", argv)

        (self.h.argv_dir / "grok.argv.json").unlink()
        (self.h.argv_dir / "grok.ran").unlink()
        proc = self.h.run(
            ["--provider", "grok", "--mode", "edit", "--allow-unattended-writes", "--effort", "high"]
        )
        self.assertEqual(proc.returncode, 0)
        argv = self.h.argv_record("grok")["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(argv[argv.index("--reasoning-effort") + 1], "high")
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["effort"], "high")

    def test_edit_gate_fails_before_launch_even_if_output_would_be_valid(self) -> None:
        self.h.add_fake("grok")
        out = self.h.next_output("edit-gate")
        proc = self.h.run(["--provider", "grok", "--mode", "edit"], output=out)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(out.exists())
        self.assertFalse(self.h.ran("grok"))

    def test_shell_metacharacters_are_not_executed(self) -> None:
        self.h.add_fake("cursor-agent")
        self._cursor_ok_env()
        nasty = 'hello; touch HACKED; $(touch HACKED2) `touch HACKED3` && echo pwned\n'
        self.h.task.write_text(nasty, encoding="utf-8")
        proc = self.h.run([])
        self.assertEqual(proc.returncode, 0)
        argv = self.h.argv_record("cursor-agent")["argv"]
        prompt = argv[-1]
        self.assertIn("touch HACKED", prompt)
        self.assertFalse((self.h.workspace / "HACKED").exists())
        self.assertFalse((self.h.workspace / "HACKED2").exists())
        self.assertFalse((self.h.workspace / "HACKED3").exists())
        self.assertFalse((self.h.root / "HACKED").exists())

    def test_metacharacters_in_workspace_path(self) -> None:
        weird = self.h.root / "ws; echo pwned"
        weird.mkdir()
        self.h.workspace = weird
        self.h.task = weird / "task.md"
        self.h.task.write_text("ok\n", encoding="utf-8")
        self.h.add_fake("cursor-agent")
        self._cursor_ok_env()
        proc = self.h.run([])
        self.assertEqual(proc.returncode, 0)
        argv = self.h.argv_record("cursor-agent")["argv"]
        self.assertEqual(argv[argv.index("--workspace") + 1], str(weird.resolve()))
        self.assertFalse((self.h.root / "pwned").exists())

    def test_cursor_error_json_exit0_is_failure(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = json.dumps(
            {"type": "result", "is_error": True, "result": "nope"}
        )
        proc = self.h.run([])
        summary = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["exit_code"], 0)
        self.assertFalse(summary["timed_out"])
        self.assertIn("nope", Path(summary["stdout_log"]).read_text(encoding="utf-8"))

    def test_cursor_subtype_error_is_failure(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = json.dumps(
            {"type": "result", "subtype": "error", "is_error": False, "result": "x"}
        )
        proc = self.h.run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "failed")

    def test_cursor_subtype_error_prefix_is_failure(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": False,
                "result": "hit limit",
            }
        )
        proc = self.h.run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "failed")

    def test_cursor_unknown_subtype_is_failure(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = json.dumps(
            {"type": "result", "subtype": "partial", "is_error": False, "result": "x"}
        )
        proc = self.h.run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "failed")

    def test_cursor_missing_result_field_is_failure(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False}
        )
        proc = self.h.run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "failed")

    def test_cursor_non_result_json_is_not_success(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = json.dumps(
            {"type": "assistant", "content": "hi"}
        )
        proc = self.h.run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "failed")

    def test_cursor_malformed_and_missing_json_not_success(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = "{not json"
        proc = self.h.run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "failed")

        (self.h.argv_dir / "cursor-agent.ran").unlink()
        self.h.env["FAKE_STDOUT"] = ""
        proc = self.h.run([])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["status"], "failed")

    def test_grok_plain_exit0_is_completed_unverified(self) -> None:
        self.h.add_fake("grok")
        self._grok_ok_env()
        proc = self.h.run(["--provider", "grok"])
        summary = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(summary["status"], "completed_unverified")
        self.assertEqual(summary["result"].strip(), "grok-plain-ok")

    def test_output_bound_large_stdout_no_pipe_deadlock(self) -> None:
        self.h.add_fake("grok")
        self.h.env["FAKE_STDOUT_BYTES"] = str(5_000_000)
        self.h.env["FAKE_STDERR_BYTES"] = str(1_000_000)
        self.h.env["FAKE_EXIT"] = "0"
        t0 = time.monotonic()
        proc = self.h.run(["--provider", "grok", "--timeout", "30"])
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 20)
        summary = json.loads(proc.stdout)
        self.assertEqual(summary["status"], "completed_unverified")
        self.assertLessEqual(len(summary["result"]), 2000)
        self.assertEqual(len(summary["result"]), 2000)
        stdout_size = Path(summary["stdout_log"]).stat().st_size
        stderr_size = Path(summary["stderr_log"]).stat().st_size
        self.assertGreaterEqual(stdout_size, 5_000_000)
        self.assertGreaterEqual(stderr_size, 1_000_000)

    def test_cursor_large_result_truncated_in_summary_full_in_result_json(self) -> None:
        self.h.add_fake("cursor-agent")
        big = "Z" * 5000
        self.h.env["FAKE_EXIT"] = "0"
        self.h.env["FAKE_STDOUT"] = json.dumps(
            {"type": "result", "is_error": False, "result": big}
        )
        proc = self.h.run([])
        summary = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(summary["status"], "completed_unverified")
        self.assertEqual(len(summary["result"]), 2000)
        self.assertEqual(summary["result"], "Z" * 2000)
        full = json.loads(Path(summary["result_json"]).read_text(encoding="utf-8"))
        self.assertEqual(full["result"], big)

    def test_task_and_output_path_validation(self) -> None:
        self.h.add_fake("cursor-agent")
        self._cursor_ok_env()

        missing_task = self.h.workspace / "nope.md"
        proc = subprocess.run(
            [
                sys.executable,
                str(DELEGATE),
                "--workspace",
                str(self.h.workspace),
                "--task-file",
                str(missing_task),
                "--output-dir",
                str(self.h.next_output()),
            ],
            env=self.h.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.h.ran("cursor-agent"))

        existing = self.h.next_output("exists")
        existing.mkdir()
        proc = self.h.run([], output=existing)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must not already exist", json.loads(proc.stdout)["error"])
        self.assertFalse(self.h.ran("cursor-agent"))

        inside = self.h.workspace / "nested-out"
        proc = self.h.run([], output=inside)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(inside.exists())
        self.assertIn("outside", json.loads(proc.stdout)["error"])

        not_dir = self.h.root / "ws-file"
        not_dir.write_text("x", encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(DELEGATE),
                "--workspace",
                str(not_dir),
                "--task-file",
                str(self.h.task),
                "--output-dir",
                str(self.h.next_output()),
            ],
            env=self.h.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("existing directory", json.loads(proc.stdout)["error"])

        bad_utf = self.h.workspace / "bad.bin"
        bad_utf.write_bytes(b"\xff\xfe\x00not-utf8")
        proc = subprocess.run(
            [
                sys.executable,
                str(DELEGATE),
                "--workspace",
                str(self.h.workspace),
                "--task-file",
                str(bad_utf),
                "--output-dir",
                str(self.h.next_output()),
            ],
            env=self.h.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("UTF-8", json.loads(proc.stdout)["error"])
        self.assertFalse(self.h.ran("cursor-agent"))

        proc = subprocess.run(
            [
                sys.executable,
                str(DELEGATE),
                "--workspace",
                str(self.h.workspace),
                "--task-file",
                str(self.h.task),
                "--output-dir",
                str(self.h.next_output()),
                "--timeout",
                "0",
            ],
            env=self.h.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_timeout_stops_process_group(self) -> None:
        self.h.add_fake("grok")
        self.h.env["FAKE_SLEEP"] = "60"
        self.h.env["FAKE_FORK"] = "1"
        self.h.env["FAKE_IGNORE_SIGTERM"] = "1"
        self.h.env["FAKE_EXIT"] = "0"
        t0 = time.monotonic()
        proc = self.h.run(["--provider", "grok", "--timeout", "1"])
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 15)
        summary = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(summary["status"], "timeout")
        self.assertTrue(summary["timed_out"])
        parent_pid = self.h.argv_record("grok")["pid"]
        child_path = self.h.argv_dir / "grok.child_pid"
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and pid_alive(parent_pid):
            time.sleep(0.05)
        self.assertFalse(pid_alive(parent_pid))
        if child_path.exists():
            child_pid = int(child_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and pid_alive(child_pid):
                time.sleep(0.05)
            self.assertFalse(pid_alive(child_pid))

    def test_timeout_kills_child_when_parent_exits_on_term(self) -> None:
        self.h.add_fake("grok")
        self.h.env["FAKE_SLEEP"] = "60"
        self.h.env["FAKE_FORK"] = "1"
        self.h.env["FAKE_CHILD_IGNORE_SIGTERM"] = "1"
        self.h.env["FAKE_EXIT"] = "0"
        t0 = time.monotonic()
        proc = self.h.run(["--provider", "grok", "--timeout", "1"])
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 15)
        summary = json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(summary["status"], "timeout")
        self.assertTrue(summary["timed_out"])
        parent_pid = self.h.argv_record("grok")["pid"]
        child_path = self.h.argv_dir / "grok.child_pid"
        self.assertTrue(child_path.exists())
        child_pid = int(child_path.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
            pid_alive(parent_pid) or pid_alive(child_pid)
        ):
            time.sleep(0.05)
        self.assertFalse(pid_alive(parent_pid))
        self.assertFalse(pid_alive(child_pid))

    def test_raw_logs_and_request_saved(self) -> None:
        self.h.add_fake("cursor-agent")
        self.h.task.write_text("unique-task-marker-42\n", encoding="utf-8")
        self.h.env["FAKE_STDOUT"] = CURSOR_OK
        self.h.env["FAKE_STDERR"] = "raw-stderr-line\n"
        self.h.env["FAKE_EXIT"] = "0"
        proc = self.h.run([])
        summary = json.loads(proc.stdout)
        request = Path(summary["request_path"]).read_text(encoding="utf-8")
        self.assertIn("unique-task-marker-42", request)
        self.assertIn("禁止", request)
        stdout = Path(summary["stdout_log"]).read_text(encoding="utf-8")
        stderr = Path(summary["stderr_log"]).read_text(encoding="utf-8")
        self.assertIn("worker-ok", stdout)
        self.assertIn("raw-stderr-line", stderr)
        result = json.loads(Path(summary["result_json"]).read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "completed_unverified")
        self.assertEqual(result["provider"], "cursor")
        self.assertIn("--print", result["argv"])
        self.assertNotIn("CURSOR_API_KEY", json.dumps(result))
        self.assertNotIn("env", result)

    def test_classify_helpers_match_cursor_contract(self) -> None:
        mod = load_delegate()
        status, parsed = mod.classify_status(
            provider="cursor",
            child_exit_code=0,
            timed_out=False,
            stdout=CURSOR_OK,
        )
        self.assertEqual(status, "completed_unverified")
        self.assertEqual(parsed["type"], "result")
        status, _ = mod.classify_status(
            provider="grok",
            child_exit_code=0,
            timed_out=False,
            stdout="not json",
        )
        self.assertEqual(status, "completed_unverified")
        status, _ = mod.classify_status(
            provider="grok",
            child_exit_code=2,
            timed_out=False,
            stdout="x",
        )
        self.assertEqual(status, "failed")
        status, _ = mod.classify_status(
            provider="cursor",
            child_exit_code=0,
            timed_out=False,
            stdout=json.dumps(
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": False,
                    "result": "x",
                }
            ),
        )
        self.assertEqual(status, "failed")
        status, parsed = mod.classify_status(
            provider="cursor",
            child_exit_code=0,
            timed_out=False,
            stdout=json.dumps({"type": "result", "is_error": False, "result": "ok"}),
        )
        self.assertEqual(status, "completed_unverified")
        self.assertEqual(parsed["result"], "ok")
        status, _ = mod.classify_status(
            provider="cursor",
            child_exit_code=0,
            timed_out=False,
            stdout=json.dumps({"type": "result", "is_error": False}),
        )
        self.assertEqual(status, "failed")
        status, _ = mod.classify_status(
            provider="cursor",
            child_exit_code=0,
            timed_out=False,
            stdout=json.dumps(
                {"type": "result", "subtype": "unknown", "is_error": False, "result": "x"}
            ),
        )
        self.assertEqual(status, "failed")

    def test_does_not_use_shell_true_against_metacharacters_in_output_dir_name(self) -> None:
        self.h.add_fake("cursor-agent")
        self._cursor_ok_env()
        out = self.h.outside / 'run; touch PWNED'
        proc = self.h.run([], output=out)
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(out.is_dir())
        self.assertFalse((self.h.outside / "PWNED").exists())
        self.assertFalse((self.h.workspace / "PWNED").exists())

    def test_output_dir_mode_0700_does_not_chmod_parent(self) -> None:
        self.h.add_fake("cursor-agent")
        self._cursor_ok_env()
        parent_mode_before = stat.S_IMODE(self.h.outside.stat().st_mode)
        out = self.h.next_output()
        proc = self.h.run([], output=out)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.h.outside.stat().st_mode), parent_mode_before)

    def test_launch_oserror_writes_result_json_without_task_in_summary(self) -> None:
        self.h.add_fake("cursor-agent")
        mod = load_delegate()
        out = self.h.next_output()
        args = mod.parse_args(
            [
                "--workspace",
                str(self.h.workspace),
                "--task-file",
                str(self.h.task),
                "--output-dir",
                str(out),
            ]
        )
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(self.h.bindir)
        try:
            with patch.object(
                mod.subprocess, "Popen", side_effect=OSError("stub-launch-failure")
            ):
                summary, code = mod.delegate(args)
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("stub-launch-failure", summary["error"])
        dumped = json.dumps(summary)
        self.assertNotIn("do the bounded task", dumped)
        self.assertNotIn(self.h.task.read_text(encoding="utf-8").strip(), dumped)
        self.assertTrue(out.is_dir())
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o700)
        result_path = Path(summary["result_json"])
        self.assertTrue(result_path.is_file())
        full = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(full["status"], "failed")
        self.assertIn("stub-launch-failure", full["error"])
        self.assertTrue(Path(summary["stdout_log"]).exists())
        self.assertTrue(Path(summary["stderr_log"]).exists())
        self.assertTrue(Path(summary["request_path"]).exists())


if __name__ == "__main__":
    unittest.main()
