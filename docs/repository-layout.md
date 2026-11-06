# 仓库与子模块关系

当前采用「模型与集成主仓库 + 独立桌面子模块 + 独立知识库子模块」，所有现有运行路径保持不变。

```text
TableBot/                         Git 主仓库：模型调用、Agent、系统集成
├── jarvis/                       Python Agent、模型提供方、RAG、MCP 与 HTTP 桥接
├── scripts/、tests/、docs/       启动、模型侧测试和集成文档
├── wsl.ps1、monitor.ps1         现有运行入口
├── persona.md、.env.example     可版本化的人设和配置模板
├── desktop/                     Git 子模块：独立 Unity 桌面仓库
│   └── ds_desktop/
│       ├── Assets/              场景、C#、模型、纹理与动画
│       ├── Packages/            包清单、锁文件和嵌入式包
│       └── ProjectSettings/     Unity 项目设置
├── knowledge/                   主仓库管理的资料入口目录
│   ├── README.md                主仓库文件
│   ├── 示例-机器人使用指南.md   主仓库文件
│   ├── 个人偏好.md              本地文件，Git 忽略
│   └── dnd-expansions/          Git 子模块：已有外部知识库
│       └── ErdaBook/            知识库内普通目录
├── .env                         本地密钥配置，Git 忽略
└── data/                        本地数据库、日志和验证结果，Git 忽略
```

`desktop` 和 `knowledge/dnd-expansions` 是根仓库下的两个并列子模块。根仓库记录各自的提交 SHA，不把其内容重复提交。桌面仓库不依赖知识库；Python Agent 通过现有 HTTP/MCP 边界与桌面交互，并直接读取知识资料。

## 当前远程状态

主仓库和桌面仓库目前仅在本机创建，未创建 GitHub 仓库，也未上传任何内容。

- 桌面的 `.gitmodules` URL 为 `./desktop`，用于从当前本地工作区克隆。它指向源工作区中的桌面 Git 仓库，不是 GitHub 地址。
- 知识库 URL 保持为 `https://github.com/CharlietheACE/dnd-expansions.git`，固定在初始化时已有的提交，不自动升级，也不改写知识库历史。
- Git LFS 对象目前在本地桌面仓库中。Git 子模块的提交指针不包含这些二进制对象，备份时需要保留完整的 Git/LFS 数据，不能只复制主仓库的提交。

## 从本机克隆

先确保 Windows Git 和 Git LFS 已安装。在 PowerShell 中设置源路径与一个尚不存在的目标路径：

```powershell
$source = 'C:\你的路径\TableBot'
$target = 'C:\你的路径\TableBot-copy'
git -c protocol.file.allow=always clone --recurse-submodules $source $target
```

`protocol.file.allow=always` 仅对此命令开放本地子模块来源，不修改全局 Git 安全配置。知识库默认从原远程获取，需要网络。

克隆后复制 `.env.example` 为 `.env` 并配置密钥；本地数据库和个人笔记不会随克隆传递。不要直接把包含 LFS 指针的工作树作为可运行 Unity 项目；确认 `git -C desktop lfs pull` 完成。

## 修改、提交与更新

桌面更新先提交到桌面仓库，再更新主仓库的指针：

```powershell
git -C desktop switch main
git -C desktop add .
git -C desktop commit -m "Describe the desktop change"
git add desktop
git commit -m "Update desktop revision"
```

子模块克隆后通常处于 detached HEAD；提交前应切到已有分支或创建工作分支。`git submodule update` 负责恢复主仓库记录的版本，不会自动选择远程最新版本。

知识库如需更新，应显式选择上游提交，再由主仓库记录新指针：

```powershell
git -C knowledge/dnd-expansions fetch origin
git -C knowledge/dnd-expansions checkout <选定的提交SHA>
git add knowledge/dnd-expansions
git commit -m "Update knowledge revision"
```

在更新或切换子模块版本前，先提交或保存该子模块中的本地修改。

## 以后连接远程仓库

已有主仓库和桌面仓库的真实远程地址后，从根目录配置：

```powershell
git remote add origin <主仓库URL>
git -C desktop remote add origin <桌面仓库URL>
git submodule set-url desktop <桌面仓库URL>
git submodule sync --recursive
git add .gitmodules
git commit -m "Configure desktop remote"
```

先推送桌面仓库及其 LFS 对象，确认子模块提交可获取，再推送主仓库：

```powershell
git -C desktop push -u origin main
git push -u origin main
```

之后使用 `git clone --recurse-submodules <主仓库URL>` 获取完整工作区。不要将本地 `./desktop` URL 原样用于远程发布；也不要将第三方知识库的 origin 改成主仓库地址。

## 日常检查

```powershell
git status
git submodule status --recursive
git -C desktop status
git -C desktop lfs status
git -C knowledge/dnd-expansions status
```

根仓库看到子模块修改时，先进入相应仓库查看并提交；在根仓库执行 `git add desktop` 只暂存桌面提交指针，不会替你提交桌面里的文件。
