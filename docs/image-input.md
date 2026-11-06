# 本地图片输入

当前 `qwen3.5:9b` 已具备视觉能力，CLI 使用 `/image` 把本地图片和问题一起发给模型。
图片直接走 Ollama 视觉接口，不需要额外 OCR 服务或 Python 第三方包。

## 使用方法

退出旧程序后重新启动：

```powershell
.\wsl.ps1
```

在 `You>` 后输入，例如：

```text
/image "C:\Users\user\Pictures\截图.png" 请解释这张图。
/image "C:\My Photos\照片.jpg" 图中有哪些物品？
/image "/mnt/c/My Photos/照片.jpg" 请读出清晰可见的文字。
/image ./截图.png
```

以上路径为格式示例，需替换为实际文件。路径带空格时用英文双引号或单引号括住。
Windows 盘符路径会在 WSL 中转换到 `/mnt/盘符/`，也可以直接使用 WSL 路径。
相对路径以项目根目录为准；省略问题时默认描述图片主要内容。
截图或剪贴板图片需先保存为文件。

发送后可以不重复附图，继续问“右边是什么？”“那一行写的是什么？”等问题。
输入 `/clear` 清除当前文字和图片上下文。

## 支持范围

- CLI 每条 `/image` 命令发送一张图；Python 的 `Agent.reply(text, image_paths=[...])` 每轮最多两张。
- 支持 PNG 和 JPEG，按文件内容识别格式；WebP、HEIC、SVG 等需先转成 PNG/JPEG。
- 单张上限 10 MiB、1600 万像素；超过限制时先缩小图片。
- 图片读取、格式头、文件大小和尺寸在发起模型请求前检查；文件无法完整解码时，Ollama 仍可能报错。
- 原文件只读。图片字节作为会话快照保存在进程内存，不自动写入 SQLite 或知识库。
- 每次请求和历史最多保留最近两张图片；同时仍受文字对话历史窗口限制。较早图片移出后应重新发送。
- 当前只接受本地文件，不下载图片网址。模型服务位置由 `.env` 的 `OLLAMA_URL` 决定，默认是本机 WSL。

图片细节可能识别错误，模糊文字、数值、地图位置等需要核对原图。
知识库仍只索引 Markdown 文本，不会自动扫描图片或把此次识图结果加入索引。

## 代码流程

```text
/image 路径 问题
  → parse_image_command() 提取路径和文字
  → Agent.reply(question, image_paths=[path])
  → load_images() 映射路径、检查文件并编码为 base64
  → 用户消息：{"role": "user", "content": "问题", "images": ["base64图片数据"]}
  → OllamaClient.chat() 调用 /api/chat
  → Qwen 视觉模型回答；需要外部资料时仍可调用现有工具
```

[images.py](../jarvis/images.py) 处理文件和命令；[agent.py](../jarvis/agent.py) 组织图文消息与历史。
[llm.py](../jarvis/llm.py) 已能透传消息字段，遵循 [Ollama 视觉接口](https://docs.ollama.com/capabilities/vision) 的 base64 `images` 数组约定。
现有的空回复恢复、生成预算重试和工具循环都会保留本轮图片。

历史预算计算排除 base64 字符，避免一张图把文字历史挤掉；图片数量单独限制为两张。
这个数量限制不等于精确的视觉 token 预算，实际推理开销仍与图像和模型有关。
图片内文字是参考数据，系统提示词要求模型不能把它当作工具操作授权。

## 验证

```powershell
.\wsl.ps1 -Action test
.\wsl.ps1 -Action vision-smoke
```

`vision-smoke` 用标准库生成临时的红色矩形与蓝色圆形 PNG，调用实际模型观察图片并继续追问。
测试结束自动删除临时文件，不调用联网或数据库工具。
自动测试另外覆盖含中文和空格的路径、JPEG 尺寸、错误输入、多轮图片保留、图片数量限制、
清除会话和实际发送给 Ollama 的图片字段。
