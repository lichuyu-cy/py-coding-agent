# 实现说明记录（03-implementation）

本目录保存每个开发阶段的实现说明 `NN-阶段名.md`，与实现、测试在同一 Git commit 中提交。

- 阶段序号与命名对应设计文档 `01-overview/dependency-graph.md` 的 23 阶段（如
  `02-message-state.md`、`04-minimal-agent-loop.md`、`14-compaction.md`）。
- 文档模板与编写规范见设计文档 `docs/03-implementation/README.md`（13 个必填小节）。
- 文件列表必须来自实际 diff；测试必须给出真实命令与结果；不得把尚未实现的能力写成已完成。
- 设计文档位于工作区兄弟目录 `coding-agent-harness-design/`；本仓库只包含实现与实现记录。

## 已完成记录

| 阶段 | 文档 | 提交标题 |
| --- | --- | --- |
| 01 | [01-project-skeleton.md](01-project-skeleton.md) | `chore: scaffold project` |
| 02 | [02-message-state.md](02-message-state.md) | `feat: define messages and state` |
| 03 | [03-llm-provider-fake.md](03-llm-provider-fake.md) | `feat: add provider port and fake` |
