---
name: cli-worker-delegation
description: >-
  Delegates bounded local work to Cursor CLI (cursor-agent/cursor) or Grok CLI
  so an orchestrator can keep its own context small. Use when farming out a
  self-contained coding or investigation task to a local CLI worker. Not for
  Codex/Paseo-specific runners, tiny one-liners that cost more than doing the
  work inline, or as a guarantee of token or cost savings.
---

# CLI Worker 委派

将有界任务委派给本机 Cursor CLI（优先 `cursor-agent`，否则 `cursor`）或 Grok CLI。不绑定 Codex/Paseo，不保证更省 token/费用。用户授权委派不等于本 skill 能突破宿主限制；无授权则不要启动。

平台：Python 3.10+，macOS/Linux POSIX。任意编排方只要能阅读本 `SKILL.md` 并调用 `scripts/delegate.py` 即可使用，不依赖特定宿主的自动发现。

## 何时委派

- 任务自包含：目标、输入文件、写入范围、验收命令、最终报告要求。
- 避免比主 agent 直接做更贵的微小委派。
- 只给最小相关上下文：不要整段历史、不要让 worker 重复已完成的调查；给产物与日志路径，不要回灌日志全文。

## 任务卡（写入 task.md）

```text
目标：一个可独立验收的结果
输入：必要文件路径及已确认事实，不附完整聊天历史
允许修改：精确路径；禁止修改：raw/ 或其他受保护路径
验收：实际运行的命令、预期输出或断言
回报：变更路径、验证结果、阻塞；长日志只给路径
```

## 编排

主 agent 负责编排与独立核验。Worker 最终回复不是独立核验。失败即停，先检查部分编辑再显式重试；定向修复最多一次，随后重新评估。并行须使用互不重叠的写入集或 git worktree。优先让 worker 直接打补丁，少写长文。尊重调用方显式指定的 provider/model，禁止静默降级。主进程只读 stdout 上的短摘要 JSON；仅在失败或需要细节时再读冗长的 `result.json`。

Worker 与主进程共享 home 凭证；`--force` / `bypassPermissions` 不是隔离。Grok 的 `--disable-web-search` 只关闭其内置 web search/fetch 工具，不是 OS、shell 或 MCP 级断网；那些通道仅受任务授权约束。权限旗标不是沙箱；`--mode read` 会把 Cursor 设为 `--mode ask`、Grok 设为 `--permission-mode plan`，并在提示中禁止编辑，不是 OS 沙箱。选定 CLI 后执行失败不会自动换另一个 provider。不要调用名为 `agent` 的二进制（本机上属于 Grok）。

未授权时 worker 禁止：读密钥、commit、publish、push、删除、联网、嵌套 agent。须遵守工作区适用指令与原始路径不可改写约束，只动授权路径。最终回复简洁列出变更路径、测试、证据、阻塞，且不得声称自己是独立核验。

## 运行

`SKILL_DIR` 为本 skill 根目录（本 `SKILL.md` 所在目录）。用它解析脚本路径，不要写死用户家目录。

只读：

```bash
python3 "$SKILL_DIR/scripts/delegate.py" \
  --provider auto \
  --workspace "$PWD" \
  --task-file /path/to/task.md \
  --output-dir /tmp/cli-worker-out-$$ \
  --mode read
```

无人值守编辑（必须带 `--allow-unattended-writes`，否则脚本拒绝启动）：

```bash
python3 "$SKILL_DIR/scripts/delegate.py" \
  --provider auto \
  --workspace "$PWD" \
  --task-file /path/to/task.md \
  --output-dir /tmp/cli-worker-out-$$ \
  --mode edit \
  --allow-unattended-writes
```

`--output-dir` 必须尚不存在，且在 workspace 外。stdout 只有摘要 JSON（`status` 为 `completed_unverified` / `failed` / `timeout`，结果截断 ≤2000 字）；完整日志在该目录。默认 `--timeout 600`、`--max-turns 30`。Cursor 默认模型 `cursor-grok-4.6-xhigh`：`--print --trust --workspace --model --output-format json`，只读加 `--mode ask`，显式 opt-in 编辑才加 `--force`。Grok 默认 `grok-4.6`：`--cwd --model --reasoning-effort xhigh --no-subagents --disable-web-search --max-turns --permission-mode plan -p`，显式 opt-in 编辑才用 `bypassPermissions`。CLI 界面与可用模型会变；失败时再查已安装的 `--help`/models，不必每次跑前验证。

## 失败后

阅读 `result.json` 与 stdout/stderr 日志，并独立核验工作区 diff。不要把 worker 自述当成通过。
