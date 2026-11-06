# JARVIS-M1 · Phase 1

本仓库管理模型调用、Python Agent 与系统集成；`desktop/` 为独立 Unity 桌面子模块，`knowledge/dnd-expansions/` 为已有外部知识库子模块，两者并列。仓库关系、克隆、Git LFS 与提交顺序见 [仓库与子模块说明](docs/repository-layout.md)。密钥、数据库、Unity 缓存和个人知识笔记不纳入版本控制。

主仓库：<https://github.com/tanguoran/TableBot>；桌面仓库：<https://github.com/tanguoran/TableBot-Desktop>。两个仓库均为私有仓库，需使用有权限的 GitHub 账号并安装 Git LFS。获取完整工作区：

```powershell
git clone --recurse-submodules https://github.com/tanguoran/TableBot.git
cd TableBot
```

本地优先的纯软件 AI 管家：**Python + Ollama + Qwen3.5 + Tool Calling + SQLite**。
可选 DeepSeek Flash API：聊天与自主规划可通过 `.env` 一项切换，知识库共用本地向量索引，见 [模型切换说明](docs/model-providers.md)。
对应聊天中的第一阶段：先跑通中文对话、Markdown 知识库 RAG、搜索和提醒，再接语音与机器人。
范围和验收标准见 [Phase 1 规划](docs/phase1.md)。
初次阅读代码可从 [代码讲解](docs/phase1.md#代码讲解) 开始，包含启动流程、Agent 工具调用、纯向量 RAG、数据库结构和调试方法。
目录式 Markdown 知识库见 [使用说明](docs/knowledge-base.md)。
本地图片输入见 [图片使用说明](docs/image-input.md)。
Unity 桌面感知、角色行动与本地模型 MCP 接入见 [桌面 MCP 使用说明](docs/desktop-mcp.md)。
当前采用“大肥鱼／鲸鱼娘”聊天人设，可编辑根目录 [persona.md](persona.md)，调整方式见 [人设说明](docs/persona.md)。

## 在当前 Windows 电脑通过 WSL 使用

源码继续放在当前 TableBot 目录，程序用 **WSL Ubuntu 的 Python** 执行。
Ollama 和模型位于 WSL 文件系统中。运行代码仅依赖 Python 3.11+ 标准库，无需 pip 安装。

在当前目录的 PowerShell 中：

```powershell
# 检查 Python、时区、Ollama 和所选模型
.\wsl.ps1 -Action check

# 开始聊天
.\wsl.ps1

# 不使用模型的单元测试
.\wsl.ps1 -Action test

# 真实模型测试：聊天、提醒创建/查询/取消
.\wsl.ps1 -Action smoke

# 填入豆包搜索 Key 后，发起一次真实搜索（消耗搜索额度）
.\wsl.ps1 -Action search-test

# 索引 knowledge/ 下的 Markdown
.\wsl.ps1 -Action kb-index

# 真实模型验收：文档检索、引用与增删改同步
.\wsl.ps1 -Action kb-smoke

# 真实模型验收：图片识别与后续追问
.\wsl.ps1 -Action vision-smoke
```

在 WSL 终端中：

```bash
cd '/mnt/c/Users/user/Documents/个人资料/持续学习/代码/TableBot'
bash scripts/wsl.sh check
bash scripts/wsl.sh run
```

脚本首次运行时会将 `.env.example` 复制成 `.env`，已有配置会保留。
数据库默认位于 `data/jarvis.db`；测试模型的 `smoke` 模式使用独立的内存数据库。

## 新环境安装

1. 安装 WSL2 Ubuntu，确保 `python3 --version` 为 3.11 或更新版本。
2. 用 `nvidia-smi` 检查 WSL GPU 透传。
3. 按 [Ollama Linux 官方说明](https://docs.ollama.com/linux) 安装 Ollama。
4. 启动服务并下载模型：

```bash
sudo systemctl start ollama
ollama pull qwen3.5:9b
ollama pull qwen3-embedding:0.6b
bash scripts/wsl.sh check
```

本机已使用官方安装程序配置 `ollama` systemd 服务。该服务默认只监听本机
`127.0.0.1:11434`，随 WSL 启动；若主动关闭 WSL，服务也会停止。
没有 systemd 的其他环境可在独立终端运行 `ollama serve`。

聊天模型已从原规划的 4B 升级为 [qwen3.5:9b](https://ollama.com/library/qwen3.5:9b)，
本机采用 Q4_K_M 量化版，下载约 6.6GB。保留思考模式及 32768 token 的上下文设置。
修改 `.env` 中的 `OLLAMA_MODEL` 后，可运行 `.\wsl.ps1 -Action pull` 下载所选模型。
更换聊天模型后需退出旧 CLI 并重新启动；Markdown 知识库使用独立的向量模型，无需因此重建索引。
旧的 `qwen3.5:4b` 保留在本机，需要时可在 `.env` 中切回。

## 配置

编辑 `.env`：

```ini
OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:9b
OLLAMA_NUM_CTX=32768
OLLAMA_NUM_PREDICT=8192
OLLAMA_MAX_NUM_PREDICT=16384
OLLAMA_CHAT_TIMEOUT=300
OLLAMA_THINK=true
JARVIS_DB=data/jarvis.db
JARVIS_TIMEZONE=Asia/Tokyo
JARVIS_EMBED_MODEL=qwen3-embedding:0.6b
JARVIS_KNOWLEDGE_DIR=knowledge
JARVIS_KNOWLEDGE_TOP_K=4
JARVIS_KNOWLEDGE_MIN_SCORE=0.5
DOUBAO_SEARCH_API_KEY=
```

联网搜索使用 **豆包搜索 Custom 独立 API**；聊天与文档向量计算继续使用本地 Ollama。
填写 `DOUBAO_SEARCH_API_KEY` 后重启 CLI。旧的 `TAVILY_API_KEY` 不再参与配置。
未配置时仍可聊天、查询知识库和管理提醒，搜索工具会明确返回不可用。
`check` 只检查 Key 是否存在，不验证 Key；自动化测试使用模拟搜索。

## 启用豆包联网搜索

1. 在 [火山引擎搜索控制台](https://console.volcengine.com/search-infinity/web-search) 开通网页搜索。
2. 在 [搜索 API Key 管理](https://console.volcengine.com/search-infinity/api-key) 创建搜索专用 Key，选择与你开通方式一致的按量或订阅类别。
3. 将 Key 填入当前项目的 `.env`：`DOUBAO_SEARCH_API_KEY=你的实际Key`。这里使用搜索控制台的 Key。
4. 执行 `.\wsl.ps1 -Action search-test`。它直接发起一次真实查询，不依赖聊天模型或用户数据库；缺少 Key 时不会发送请求。
5. 执行 `.\wsl.ps1`，输入“请搜索最近一天的机器人新闻，附上来源网址”。

适配实现位于 `jarvis/search.py`，遵循 [豆包搜索 Custom 官方接口](https://docs.volcengine.com/docs/networked-search/networked-search?lang=zh)：

- 请求固定使用 `SearchType=web`，每次最多取 5 条；模型传入的搜索词需为 1–100 个字符，超长时返回参数错误。
- 可选 `time_range` 支持 `OneDay`、`OneWeek`、`OneMonth`、`OneYear`，不传时不限制发布时间。
- 优先采用相关摘要 `Summary`，其次正文 `Content`，最后使用短片段 `Snippet`，每条最多 1000 字符。
- 保留来源网址、站点、发布时间和接口返回的权威性描述；丢弃无效链接并按网址去重。相关描述不是独立事实核验。
- 请求超时 20 秒，不自动重试；Key 缺失、权限不足、额度用尽、限流、网络故障和响应格式异常分别返回明确错误。
- `ResponseMetadata.Error` 即使出现在 HTTP 200 响应中，也作为失败处理，避免把额度错误误报成“没有结果”。

`search-test` 会消耗一次搜索调用额度；免费额度与超额规则以你的账号控制台为准。
已通过模拟响应的协议与错误处理测试。2026-11-01 在 WSL 完成真实验收：独立接口返回 5 条结果，本地 Qwen 实际调用搜索工具并引用了返回的官方来源网址。
这验证了当前 Key、服务权限和基本调用链；不同问题的检索质量与长期稳定性仍需继续测试。

## Markdown 文档 RAG

资料统一由你编辑 `knowledge/` 下的 Markdown 文件。查询前自动同步文件变化，
使用 `qwen3-embedding:0.6b` 做纯向量语义检索，再将相关片段交给 `qwen3.5:9b` 回答并引用原文行号。
流程与配置见 [知识库使用说明](docs/knowledge-base.md)。

2026-11-02 按用户要求移除个人长期记忆：不再提供保存、搜索和删除个人记忆的工具，
`/memories`、`-Action index` 和 `rag-smoke` 已停用。旧数据库在启动时删除个人记忆及其向量表，
清理数据库空闲页；保留文档索引和提醒。当前项目数据库已完成清理。
不会把删除的个人记忆自动转换成 Markdown。资料需由你自行编辑。

普通对话和图片仅保留在当前进程的短期上下文，`/clear` 或退出会清除这些上下文。

## 手动验收

目录知识库可直接编辑 `knowledge/` 中的 `.md` 文件，下一次知识库查询前会自动同步。
启动 CLI 后，输入 `/kb` 查看索引状态，或输入 `/kb-sync` 提前同步；然后提问“请根据知识库里的使用指南，告诉我怎样添加新的资料，并标出来源”。

在 CLI 中依次输入：

```text
你好，你是谁？
请根据知识库里的使用指南，告诉我怎样添加资料，并标出来源。
/kb
帮我搜索今天的机器人新闻。
明天日本时间晚上八点提醒我检查机器人电池。
/reminders
请取消刚才的检查机器人电池提醒。
/quit
```

还支持 `/help`、`/image "图片路径" 你的问题` 和 `/clear`。
`/image` 支持本地 PNG/JPEG、Windows 或 WSL 路径；路径带空格时加引号，发送后可继续追问。
`/clear` 清除当前文字及图片对话上下文，保留数据库。
重启 CLI 后可通过 `/kb` 查看文档索引，通过 `/reminders` 查看持久化的待提醒事项。

## 当前边界

桌面自主行为默认开启：Unity Play 后启动 `./wsl.ps1` 即可。聊天和自主规划共用 `persona.md` 与角色上下文，回复后安静 30 秒恢复活动；明确说“停下”、`/auto pause` 或 `/auto off` 会保持暂停，用 `/auto resume` 恢复。支持指定到达朝向、坐下/起立、观察和原地跳跃。详见 [共享上下文与自动恢复](docs/character-context.md) 和 [桌面 MCP 文档](docs/desktop-mcp.md)。

模型监控：另开 PowerShell 运行 `./monitor.ps1`，实时查看本地模型思考、正文、规划、工具调用与实际结果。旧 CLI 需重启以启用记录；使用与日志说明见 [模型监控](docs/model-monitor.md)。

真实桌面文件已映射为3D实体：Unity Play 时自动同步用户/公共桌面的文件与文件夹，忽略快捷方式；支持整齐排列、分页、实时增删改名、PNG/JPEG预览和双击外部打开。使用与范围见 [桌面文件同步](docs/desktop-files.md)。

- 提醒持久化到 SQLite，只在每次 CLI 等待输入前检查。停留在输入框、CLI 关闭或 WSL 停止时，没有后台通知；过期提醒会在下次检查时显示。
- `--user` 是数据配置名，不是身份认证；本阶段用于本地单人原型。
- 搜索结果属于不可信内容；尚无确定性的引用核验器。
- 资料检索只使用 Markdown RAG，没有个人长期记忆或自动偏好保存。
- 工具调用由模型决定，成功率仍需用实际模型验证；`smoke` 是一次回归检查，不代表所有问法都可靠。
- 语音、ROS 2、摄像头、机械臂、底盘和远程接口留待后续阶段。

## 新闻查询出现空回复

此前可能在“帮我找一下今天头条新闻”等问题中显示“模型未返回文本。”。
在长对话中已复现：搜索成功，但生成空间不足，响应为 `done_reason=length`，只有 `thinking` 而没有 `content`；旧 Agent 把它直接当作最终回答处理。思考模式保持开启，通过预算和结束状态处理解决这个问题。

当前修复包括：

- 默认 `OLLAMA_THINK=true`，上下文为 32768 token，首次生成预算为 8192 token（包括思考和正文）。
- 遇到 `done_reason=length` 时，丢弃不完整响应，以相同的已完成输入和 16384 token 预算重新生成一次，整个过程保持思考开启。不执行截断响应中的工具调用，也不把半截正文当作完整答案。
- 若第二次仍截断，明确报告生成上限，并展示本轮已有搜索来源；不会无限重试或自动关闭思考。单次 HTTP 请求超时为 300 秒，可通过 `OLLAMA_CHAT_TIMEOUT` 调整。
- 按完整用户对话轮次整理历史，最多 30 条消息，按约 3000 字符目标预算保留最近轮次；最新轮次过大时，保留问题与最终答复，成组移除旧工具请求和结果。
- 历史只保留正文和完整工具交互，移除 `thinking`，避免把历史推理重复送入模型。
- 空正文且没有工具调用时，最多重试一次；恢复提示不写入长期对话历史。
- 恢复中重复请求本轮已成功执行的同名、同参数工具时，复用结果，避免再次搜索或重复写入。
- 如果模型仍无法总结，直接展示本次搜索返回的标题、发布时间和来源；搜索失败时展示实际错误。内部思考不会作为用户答复输出。

更新代码后，在旧 CLI 中输入 `/quit`，再运行 `.\wsl.ps1`。进程需要重启才能加载新配置和处理逻辑。
较早的关闭思考方案通过过 55 项测试；当前保留思考方案的配置、实现与验证记录见 [Phase 1 文档](docs/phase1.md#保留思考模式的截断处理2026-11-01)。
