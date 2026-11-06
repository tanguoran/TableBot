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

主仓库和桌面仓库分别使用以下 GitHub 私有仓库。克隆、拉取和推送需要有访问权限的 GitHub 账号：

- 主仓库：`https://github.com/tanguoran/TableBot.git`。
- 桌面仓库：`https://github.com/tanguoran/TableBot-Desktop.git`；`.gitmodules` 使用这个 HTTPS 地址，不依赖本机路径。
- 知识库 URL 保持为 `https://github.com/CharlietheACE/dnd-expansions.git`，固定在初始化时已有的提交，不自动升级，也不改写知识库历史。
- 桌面二进制资产由桌面仓库的 Git LFS 管理。主仓库中的子模块指针只记录桌面的提交 SHA；推送顺序应为桌面及其 LFS 对象在前，主仓库在后。

## 从 GitHub 克隆

先确保 Git、Git LFS 已安装，且 Git 能使用有访问权限的 GitHub 账号认证。在 PowerShell 中执行：

```powershell
git -c core.longpaths=true clone --recurse-submodules https://github.com/tanguoran/TableBot.git
cd TableBot
git config core.longpaths true
git submodule foreach --recursive 'git config core.longpaths true'
git -C desktop lfs pull
```

主仓库会还原它记录的桌面和知识库提交。知识库从原上游获取；两个私有仓库使用当前账号的认证权限。克隆会获取桌面 LFS 资产，初次下载量约为 3.1 GiB。

知识库和 Unity 项目包含较深的目录。`core.longpaths=true` 用于避免 Windows 长路径导致的 checkout 失败；克隆时的 `-c` 同时传给本次操作中的子模块，随后各仓库的本地配置用于后续更新。这些命令不修改全局 Git 配置。

克隆后复制 `.env.example` 为 `.env` 并配置密钥；本地数据库和个人笔记不会随克隆传递。不要直接把包含 LFS 指针的工作树作为可运行 Unity 项目；确认 `git -C desktop lfs pull` 完成。

已有工作区可先拉取主仓库，再同步子模块地址并还原固定版本：

```powershell
git pull --ff-only
git submodule sync --recursive
git -c core.longpaths=true submodule update --init --recursive
git -C desktop lfs pull
```

这些更新操作前应先保存或提交本地改动。如果只需要模型侧源码，可不使用 `--recurse-submodules`，然后按需初始化知识库；没有桌面子模块时，桌面相关功能不可用。

## 修改、提交与更新

桌面更新先提交到桌面仓库，再更新主仓库的指针：

```powershell
git -C desktop switch main
git -C desktop add .
git -C desktop commit -m "Describe the desktop change"
git -C desktop push origin main
git add desktop
git commit -m "Update desktop revision"
git push origin main
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

## 远程地址与边界

根仓库的 `origin` 指向 `tanguoran/TableBot`，桌面子模块的 `origin` 指向 `tanguoran/TableBot-Desktop`。两者分别提交和推送，不使用强制推送，也不把第三方知识库推送到这两个仓库。

如将来迁移桌面仓库，需要使用 `git submodule set-url desktop <新URL>`，再提交 `.gitmodules` 的变更。不要只修改某台机器的 `.git/config`，否则其他克隆仍会使用旧地址。

## 日常检查

```powershell
git status
git submodule status --recursive
git -C desktop status
git -C desktop lfs status
git -C knowledge/dnd-expansions status
```

根仓库看到子模块修改时，先进入相应仓库查看并提交；在根仓库执行 `git add desktop` 只暂存桌面提交指针，不会替你提交桌面里的文件。
