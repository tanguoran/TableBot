# 模型监控

在项目根目录开两个 PowerShell 窗口。

监控同时支持本地 Qwen 与 DeepSeek Flash；模型请求事件标出供应商和实际模型。DeepSeek 默认不输出思考，可在 `.env` 中设置 `DEEPSEEK_THINK=true` 后重启，监控会显示接口实际返回的 `reasoning_content`。切换步骤见 [模型配置](model-providers.md)。

第一个窗口启动聊天（旧 CLI 必须退出并重启一次）：

```powershell
./wsl.ps1
```

第二个窗口运行监控：

```powershell
./monitor.ps1
```

监控会先回放最近 50 个事件，然后每约 0.15 秒读取新增日志。显示时区跟随 `JARVIS_TIMEZONE`。关闭监控窗口或 Ctrl+C 只停止查看，聊天与自主系统继续运行。

可查看：

- 用户输入、聊天决策轮次与最终答复。
- 本地模型实际返回的完整 `thinking`、正文和工具提案。聊天思考/正文逐段显示，不在终端省略内容。
- 工具是否真正开始执行、完整参数、耗时、实际返回值和异常。模型提出工具调用不等于执行成功。
- 自主规划的输入、原始提案、采用后的队列、拒绝原因及旧计划失效原因。
- 用户暂停/接管、等待动作、超时、截断重试与工具循环上限。达到上限时会留下本轮各次工具结果，便于排查“干点别的事”一类指令。

聊天事件附带 `turn_id`、决策轮次、`model_call_id`；规划使用 `plan_id`；工具执行使用 `tool_call_id`。诊断事件标识按线程隔离，能够区分聊天和自主动作；角色上下文由两者共享。

“共享角色上下文”事件显示聊天实际使用的近期经历、意图与控制状态；规划输入也包含同一份 `character_context`。“等待对话空闲后恢复”与“自主模式”事件可用于检查回复后 30 秒恢复活动的过程，详见 [共享上下文说明](character-context.md)。

自主规划原本使用 `think=false`，保持这个配置；此时通常没有思考字段，监控只显示模型实际返回的正文和结构化计划。为了保留原有 20 秒响应超时，自主规划在一轮返回后记录输出。聊天启用流式输出；工具始终等整轮正常结束才执行。被截断的工具提案会标记为丢弃，不会执行。参考 [Ollama 流式输出文档](https://docs.ollama.com/capabilities/streaming)。

常用选项：

```powershell
./monitor.ps1 -Tail 200       # 回放最近200个事件后继续跟随
./monitor.ps1 -Tail 0         # 只看启动监控之后的新事件
./monitor.ps1 -All            # 同时展示完整场景快照与规划输入
./monitor.ps1 -Raw            # 完整原始JSONL，包括所有事件字段
./monitor.ps1 -Once -Tail 50  # 只回放，不持续跟随
```

也支持 `./wsl.ps1 -Action monitor`，或 WSL 内 `python3 -m jarvis.monitor`。监控进程只读本地日志，不连接模型、不执行工具。

日志放在项目的 `data/traces/`，按进程会话分别保存 UTF-8 JSONL。聊天的完整思考文本、正文、规划、工具参数与结果都会保留；默认界面隐藏的自动场景快照也在原始文件中。日志代码不读取 API Key、环境变量清单或图片的 base64，也不会把诊断日志作为聊天历史、RAG 或个人记忆读回模型。该目录已被 `.gitignore` 的 `data/` 规则排除；需要保留多长时间可自行管理这些日志。

可选 `.env` 设置（已有 `.env` 不改也默认开启 CLI 监控）：

```ini
JARVIS_TRACE_ENABLED=true
JARVIS_TRACE_STREAM=true
# 可选，默认data/traces：
# JARVIS_TRACE_DIR=data/traces
```

设置 `JARVIS_TRACE_STREAM=false` 后，聊天仍记录完整返回，但需等每轮结束才显示。`JARVIS_TRACE_ENABLED=false` 可关闭记录；也可用 `python3 -m jarvis --no-trace` 单次关闭。配置变更需要重启 CLI。

独立脚本或其他代码直接调用 `OllamaClient` 时，可在入口调用 `trace.configure(True, directory)`，或先设置 `JARVIS_TRACE_ENABLED=true`。监控覆盖本项目的 Ollama、聊天 Agent 和自主调度链路；旧进程在更新之前产生的输出无法补录。

若遇到 `utf-8 ... surrogates not allowed`，表示文本里混入了不完整的 Unicode 编码。旧版本可能把坏输入留在共享上下文中，导致后续正常聊天和自主规划也报错。更新后先退出并重启 `./wsl.ps1`：启动脚本统一使用 UTF-8，完整的编码序列会自动恢复；无法恢复的输入会提示重新粘贴，并记录为“输入编码异常（未写入上下文）”。模型请求还有额外的编码保护，日志保留异常字符的转义表示以便排查。原始桌面文件不受此处理影响。
