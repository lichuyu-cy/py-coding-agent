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
| 04 | [04-minimal-agent-loop.md](04-minimal-agent-loop.md) | `feat: add minimal loop` |
| 05 | [05-coding-tools.md](05-coding-tools.md) | `feat: add coding tools` |
| 06 | [06-tool-registry.md](06-tool-registry.md) | `feat: add tool registry` |
| 07 | [07-tool-pipeline.md](07-tool-pipeline.md) | `feat: add tool pipeline` |
| 08 | [08-tool-governance.md](08-tool-governance.md) | `feat: govern tool execution` |
| 09 | [09-runtime-controls.md](09-runtime-controls.md) | `feat: add runtime controls` |
| 10 | [10-context-manager.md](10-context-manager.md) | `feat: build provider context` |
| 11 | [11-tool-output-truncation.md](11-tool-output-truncation.md) | `feat: truncate tool outputs` |
| 12 | [12-skill-loading.md](12-skill-loading.md) | `feat: load skills on demand` |
| 13 | [13-token-management.md](13-token-management.md) | `feat: enforce token budgets` |
| 14 | [14-compaction.md](14-compaction.md) | `feat: compact context safely` |
| 15 | [15-event-bus.md](15-event-bus.md) | `feat: publish agent events` |
| 16 | [16-metrics-logging.md](16-metrics-logging.md) | `feat: record metrics and logs` |
| 17 | [17-streaming.md](17-streaming.md) | `feat: normalize agent stream` |
| 18 | [18-sse-server.md](18-sse-server.md) | `feat: expose runtime over sse` |
| 19 | [19-session.md](19-session.md) | `feat: persist sessions` |
| 20 | [20-checkpoint-resume.md](20-checkpoint-resume.md) | `feat: resume committed state` |
| 21 | [21-local-certification.md](21-local-certification.md) | `test: certify local harness` |
| 22 | [22-swebench-adapter.md](22-swebench-adapter.md) | `feat: add swebench adapter` |
| 23 | [23-final-evaluation.md](23-final-evaluation.md) | `docs: record final swebench run` |
