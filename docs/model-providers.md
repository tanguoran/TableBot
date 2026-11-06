# 切换本地 Qwen 与 DeepSeek Flash

聊天、自主规划和模型验收脚本共用 `JARVIS_CHAT_PROVIDER`。默认值为 `ollama`，已有本地 Qwen 配置保留。修改 `.env` 后退出旧 CLI，再运行 `./wsl.ps1`。

## 使用 DeepSeek

在 `.env` 中设置：

```ini
JARVIS_CHAT_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_API_KEY=在这里填写自己的DeepSeek密钥
DEEPSEEK_THINK=false
DEEPSEEK_MAX_TOKENS=8192
DEEPSEEK_MAX_RETRY_TOKENS=16384
DEEPSEEK_TIMEOUT=120
```

模型名与 Chat Completions 格式依据 [DeepSeek 官方文档](https://api-docs.deepseek.com/api/create-chat-completion/)。直接使用 Python 标准库调用，无需安装 SDK。Key 只放在本机 `.env`；检查配置和正常请求日志不会输出 Key。

运行：

```powershell
./wsl.ps1 -Action check
./wsl.ps1
```

`check` 在 DeepSeek 模式下通过 `/models` 验证密钥和模型可用性，并检查本地知识库向量模型；不生成聊天内容，也不要求安装本地聊天模型。

聊天默认关闭思考，适合较快的日常响应。需要时可设置 `DEEPSEEK_THINK=true`；适配器会按 [官方思考与工具调用要求](https://api-docs.deepseek.com/guides/thinking_mode/) 保留聊天协议所需的 `reasoning_content`，不会把它加入共享角色经历或自主规划视图。监控仍显示接口实际返回的思考、正文与工具调用。

自主规划跟随同一个模型供应商，但始终关闭思考，单次上限 768 token、响应等待 20 秒，截断不重试。聊天截断时会丢弃整轮提案，以 `DEEPSEEK_MAX_RETRY_TOKENS` 重试一次；被截断、参数无效或未完成流中的工具不会执行。鉴权失败、余额不足和网络错误不会自动改用本地模型。

DeepSeek 模式下，聊天消息、角色上下文、桌面快照及实际检索到的知识片段会随请求发送至配置的接口；自主规划也会调用该接口。可通过 `JARVIS_AUTO_ENABLED=false` 关闭默认自主规划。

## 脑力口粮

DeepSeek 模式使用共享角色状态 `hunger` 表示脑力口粮。按币种计算余额占“吃饱”参考值的比例，多币种取覆盖比例最高的钱包，最高按 100% 计算。饥饿度等于 1 减去该比例。默认参考值为 20 元或 3 美元，可用 `DEEPSEEK_HUNGER_FULL_CNY`、`DEEPSEEK_HUNGER_FULL_USD` 调整。成功查询最多缓存五分钟，失败标记为未知并在一分钟后允许重试。`/hunger` 查看当前状态，充值后可用 `/hunger refresh` 立即更新。

饥饿度达到 75% 时，自主决策临时在本地等待，首次进入低余额区间可表现一次难过，用户主动聊天仍可调用 DeepSeek。接口报告余额不足或生成请求返回 HTTP 402 时，聊天改用本地口粮提示。恢复到低余额区间以上时，自主角色可高兴一次，随后恢复模型规划。原始金额不进入角色上下文。Ollama 模式不会查询 DeepSeek 余额。


## 切回本地 Qwen

只需改一项：

```ini
JARVIS_CHAT_PROVIDER=ollama
```

原来的 `OLLAMA_MODEL=qwen3.5:9b`、`OLLAMA_URL`、上下文与思考配置继续生效。DeepSeek 的 Key 和参数可以保留，选择本地模式时不使用它们。

## 知识库配置

两种聊天模型使用同一个 `knowledge/` 目录、SQLite 向量索引与 `search_knowledge` 工具，引用仍为 Markdown 路径和行号。切换聊天模型不需要重建知识库。

向量计算继续使用本地 Ollama 的 Qwen Embedding，与聊天供应商独立：

```ini
JARVIS_EMBED_MODEL=qwen3-embedding:0.6b
# 默认沿用 OLLAMA_URL；只有向量服务地址不同时才需要显式设置：
# JARVIS_EMBED_URL=http://127.0.0.1:11434
JARVIS_KNOWLEDGE_DIR=knowledge
JARVIS_KNOWLEDGE_TOP_K=4
JARVIS_KNOWLEDGE_MIN_SCORE=0.5
```

因此 DeepSeek 模式使用知识库时，仍需运行本地 Ollama 并保留向量模型，但无需加载本地 Qwen 聊天模型。索引计算在本地完成，查询检索出的片段交给所选聊天模型回答。资料增删改的自动同步规则保持一致，详见 [知识库说明](knowledge-base.md)。

```powershell
# 首次建立本地向量索引（不调用 DeepSeek，无需其 Key）
./wsl.ps1 -Action kb-index
# 用当前选中的聊天模型验证检索、引用、增删改；DeepSeek 模式会消耗 API 额度
./wsl.ps1 -Action kb-smoke
# 验证基础对话和工具调用；DeepSeek 模式同样会消耗 API 额度
./wsl.ps1 -Action smoke
```

启动栏显示实际供应商和模型；`./monitor.ps1` 的模型请求事件也记录供应商、模型和生成参数。设置变更都需要重启聊天程序。
