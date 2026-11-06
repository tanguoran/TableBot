# 知识资料与 Git 归属

`knowledge/` 是 TableBot Agent 的 RAG 输入目录，不是单独的 Git 仓库。

- `dnd-expansions/` 是独立知识库子模块，保留其原始仓库与提交历史：<https://github.com/CharlietheACE/dnd-expansions>。
- `dnd-expansions/ErdaBook/` 是该知识库内的普通目录，不是另一层子模块。
- 本说明和 `示例-机器人使用指南.md` 由 TableBot 根仓库管理。
- 其余顶层笔记、文件或子目录默认视为本地资料，由根 `.gitignore` 排除；现有的 `个人偏好.md` 保持在本地，仍可正常参与 RAG。
- 如果确实要共享某份本地笔记，用 `git add -f knowledge/文件名.md` 显式纳入版本控制。
- 索引和数据库在 `data/` 中，不提交到任何仓库。

Git 忽略规则不影响 RAG 扫描；程序仍递归读取该目录内符合条件的 Markdown，跳过隐藏目录和 Git 元数据。

请勿在桌面子模块中再挂载一份知识库。知识库由模型侧直接读取，完整仓库关系与更新方式见 [仓库说明](../docs/repository-layout.md)。
