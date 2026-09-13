# CLI Worker Delegation

将有界任务委派给本机 Cursor CLI 或 Grok CLI 的独立 Agent Skill。

## 使用

完整任务卡、命令与权限说明见 [SKILL.md](SKILL.md)。需要 Python 3.10+、macOS/Linux POSIX，以及所选 provider 的 CLI 与有效登录状态。

```sh
python3 scripts/delegate.py --help
python3 -m unittest discover -s tests -v
```

测试使用模拟 CLI，不代表真实 provider、模型或账号已通过端到端验证。模型和命令行选项以实际安装版本为准。

## 安全边界

- 默认只读；无人值守编辑必须显式启用。
- 权限旗标和提示词不是 OS 沙箱，worker 可能共享本机凭据。
- 委派结果必须由主 agent 独立核验。
- 运行产物可能含任务上下文；保存在仓库外，不提交到 Git。

本仓库根目录即 Skill 根目录，包含 `SKILL.md`、`scripts/` 和 `tests/`。
