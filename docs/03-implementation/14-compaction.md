# 阶段 14：Compaction

## 1. 本节目标

交付原子压缩（模块 18）：合法切割点 + 结构化摘要 + 校验后提交。

- `context/compaction.py`：`MessageGroup`/`group_messages`、`CutPlan`/`find_cut`、
  `StructuredSummary`/`extract_summary`/`render_summary`、`CompactionRecord`、`Compactor`；
- ContextManager：非 FIT 决策下先压缩再重建（summary 段 + 未覆盖尾部），成功才提交记录；
- Bootstrap：默认注入 `Compactor(preserve_recent=4)`。

明确未实现：模型辅助的高质量摘要（设计扩展项，本地为确定性抽取）；压缩记录的持久化（阶段 19）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/context/compaction.py` | `Compactor`、`CompactionRecord`、`StructuredSummary`、`CutPlan`、`MessageGroup`、`find_cut`、`group_messages`、`extract_summary`、`render_summary`、`CompactionError` | 切点/摘要/校验 |
| `tests/unit/test_compaction.py` | 19 个用例 | 分组、切点、摘要、版本、原子性、集成 |
| `docs/03-implementation/14-compaction.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/context/builder.py` | 非 FIT 决策（COMPACT 或 REJECT）→ 有压缩器则压缩重建、无则显式失败；新增 `compactor` 参数与会话级记录表 | REJECT 也允许压缩救援；无压缩器行为与阶段 13 一致 |
| `src/coding_agent/bootstrap.py` | `compactor` 参数（默认 `Compactor()`） | 生产默认启用压缩 |

## 4. 每个文件的作用

- `compaction.py`：输入为历史消息（只读）与可选的旧记录；输出为 `CompactionRecord` 或
  `None`（无合法切点）。`Compactor` 负责校验后才返回记录；历史本身永不修改。
- `builder.py`：`_compact_and_rebuild` 组装 `summary` 段 + 尾部消息，重建后再次评估；
  仅当重建通过（非 REJECT）才提交记录（原子性）；相同覆盖边界不重复递增版本。

## 5. 核心实现逻辑

```text
分组：user/system 单独成组；assistant + 其全部 tool results 成一组（可标记 incomplete）
切点：候选只能是完整组的末尾；第一个 incomplete 组及之后不可覆盖；
      max_coverable = min(组数 - preserve_recent, incomplete 前)
摘要（确定性抽取）：task_goal(首个用户消息, ≤200) / completed_work(完成调用数) /
      files_read(read 调用的 path) / files_modified(write/edit) / commands_executed(bash, ≤120 截断) /
      errors(非 completed 结果) / test_results(pytest 类命令的最后状态)；
      无法观测字段写 unknown 并列入 unknown_fields；source_ids 记录全部覆盖消息
重建：system = 既有段落 + "[summary of earlier history]…"；messages = system + 覆盖边界之后的尾部
提交：重建评估非 REJECT → 保存记录（版本单调；覆盖边界未变不递增）
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| ContextManager | `Compactor.compact` | `ports.tokenizer.TokenCounter` | token_before/after |
| ContextManager | `render_summary` | PromptSection(name="summary") | snapshot.summary_version |
| Loop/Context | 不变 | — | 覆盖边界的尾部消息照常转换 |

## 7. 数据流变化

- 之前：非 FIT → ContextError 终止（阶段 13）。
- 之后：阈值/超窗且存在合法切点时自动压缩；请求 = summary 段 + 最近完整组；
  历史仍为 append-only（阶段 19 将持久化压缩记录）。

## 8. 设计原因与备选方案

- **确定性抽取摘要**：本地无模型调用也能验证"切割安全"这一核心不变量；未知字段显式标记；
- **REJECT 也先尝试压缩**：覆盖部分移除后可能重新适配（设计流程：过阈值压缩→再评估→仍超限拒绝）；
- **覆盖边界未变不递增版本**：避免每次构建都自增（阻止版本膨胀）；
- **增量语义**：记录携带版本与覆盖边界；摘要按"全量覆盖组"确定性重算（本地为内存历史，
  重算成本可忽略；旧摘要用于版本演进与对照），这是对"增量压缩输入旧摘要+新增组"的可验证简化；
- 备选：摘要字符串拼接增量（放弃：易导致计数漂移与不可复现）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_compaction.py` | 多调用组不拆/未完成组标记、设计示例切点、最近组保护、无切点、incomplete 阻断、摘要字段与 unknown、渲染、版本递增、历史不变、no-cut→None、篡改记录校验、集成（summary 段/尾部保留/source_ids/版本递增/无切点报错/压缩后仍超限报错）、Loop 端到端压缩且历史保留（19 例） | 19 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–13） | 335 passed in 8.76s | 同上 |

## 10. 当前限制

- 摘要为抽取式（无模型润色）；字段语义较粗（如 completed_work 为计数）。
- 压缩记录在内存（随会话存于 ContextManager）；进程重启丢失（阶段 19/20）。
- `preserve_recent` 为固定值；未按 token 动态调整。
- token_before/after 用同一占位计数器估算（趋势可用，绝对值近似）。

## 11. 后续依赖

- 阶段 19：CompactionRecord 随 Session 持久化；阶段 20：恢复时验证覆盖边界。
- 阶段 15：CompactionStart/End 事件（当前无事件，记录已具备字段）。
- 阶段 16：Metrics 统计压缩次数与 token 前后。

## 12. 面试解释

压缩最怕两件事：拆散"调用-结果"配对，和把摘要当事实。我的实现把切割点限制在完整组末尾、
未完成组之后一律不可覆盖，最近 N 组受保护；摘要是确定性抽取，无法观测的字段明确写 unknown
而不是编造。重建只在评估通过后才提交记录，相同覆盖边界不重复递增版本——这三条保证压缩
"要么完整发生、要么什么都没变"。端到端测试里，大输出驱动的多轮对话触发了压缩，最终回答
正常完成且历史一条不少。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-24。

验收条件逐项核对：

- 一 assistant 多 tool calls 一组不拆：`test_groups_never_split_multi_call_assistant`、
  `test_cut_never_inside_group` ✅
- 缺结果（未完成组）不可覆盖：`test_incomplete_group_flagged`、`test_incomplete_group_blocks_cut_past_it` ✅
- 连续摘要（版本单调）：`test_compact_generates_record_and_versions_grow`、
  `test_second_build_extends_summary_version` ✅
- 最新用户请求保留：`test_recent_groups_preserved`（切点不越过受保护组）✅
- 无法压缩：`test_no_cut_when_too_few_groups`、`test_no_legal_cut_reports_overflow`、
  `test_still_over_budget_after_compaction_rejected` ✅
- 摘要失败/异常不改历史：`test_compact_does_not_modify_history` + 提交仅在重建成功后发生 ✅

设计偏差：无契约变更。实现注记：非 FIT（含 REJECT）统一走"先压缩、再评估"流程（阶段 13
文档中的 COMPACT-失败路径由此取代）；摘要字段 `commands_executed` 对长命令截断（120 字符）；
压缩记录的 `token_before/after` 使用同一占位计数器估算。
