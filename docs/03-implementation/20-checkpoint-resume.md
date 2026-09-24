# 阶段 20：Checkpoint / Resume

## 1. 本节目标

交付崩溃/中断后的确定性恢复（模块 24 Checkpoint + 25 Recovery）：

- `ports/checkpoint.py`：`Checkpoint` 恢复边界索引、`ToolIntent` 工具意图 journal、
  `CheckpointStore` 协议、`RecoveryDecision`/`RecoveryPlan` 判定契约；
- `storage/checkpoint_store.py`：`SQLiteCheckpointStore`（checkpoints + tool_intents 两表）；
- `agent/recovery.py`：`RecoveryEngine.inspect/resume/resolve_uncertain` 与
  `workspace_fingerprint`；
- Loop/Runtime 接入：提交边界保存检查点；工具执行前标记 `planned → started`；
  assistant 提交登记意图、结果提交闭合意图；压缩完成保存检查点。

关键语义：`planned`（确定未执行）→ 自动补记「未执行」结果并 RESUME；
`started`（副作用未知）→ RECONCILE，绝不自动重放；记录与 journal 不一致 → STOP。

明确未实现：自动副作用判定器（设计扩展项）；跨机热迁移。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/ports/checkpoint.py` | `BoundaryKind`、`IntentStatus`、`Checkpoint`、`ToolIntent`、`BoundaryEvent`、`ToolExecutionStart`、`RecoveryDecision`、`RecoveryPlan`、`CheckpointStore` | 恢复端口契约 |
| `src/coding_agent/storage/checkpoint_store.py` | `SQLiteCheckpointStore` | 边界索引与意图 journal 持久化 |
| `src/coding_agent/agent/recovery.py` | `RecoveryEngine`、`RecoveryOutcome`、`RecoveryError`、`UnknownIntentError`、`workspace_fingerprint` | 恢复判定与安全补记 |
| `tests/unit/test_checkpoint.py` | 7 个用例 | 检查点往返/意图状态机/指纹/不一致即 STOP |
| `tests/integration/test_crash_recovery.py` | 6 个用例 | 崩溃注入矩阵与恢复全流程 |
| `docs/03-implementation/20-checkpoint-resume.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/agent/loop.py` | `boundary_sink`/`tool_start_sink` 可选回调；提交/注入边界与执行前标记 | 无 sink 时 no-op，阶段 04–19 行为不变 |
| `src/coding_agent/agent/runtime.py` | `checkpoint_store` 参数；`_record_intents`/`_on_tool_start`/`_on_boundary`/`_save_checkpoint`；`_digest_arguments` | 仅当 session_store 与 checkpoint_store 同时存在时启用；否则完全旁路 |
| `src/coding_agent/bootstrap.py` | `checkpoint_store` 透传 | 组装层 |

## 4. 每个文件的作用

- `ports/checkpoint.py`：`Checkpoint` 只索引会话版本（不复制历史）；
  `ToolIntent` 是工具意图的持久日志（参数只存摘要）；
  `RecoveryPlan` 输出「会话版本 + 上一个安全边界 + 未确定副作用」。
- `storage/checkpoint_store.py`：与 SessionStore 同一数据库文件（不同连接，WAL）；
  状态转换均为受约束 UPDATE（`mark_started` 仅允许从 `planned`）。
- `agent/recovery.py`：`inspect` 以「已提交历史中的未解决调用」为准、
  journal 只作证据；判定顺序：指纹 → 悬空意图 → 缺证据调用 → started → planned。
  `resume` 为 planned 调用补记 `cancelled + recovery_not_executed` 结果（模型可重试），
  并保存 `recovery` 边界检查点；`resolve_uncertain` 处理 started：
  `not_executed`（确认未执行）或 `executed_unknown`（确认已执行、结果丢失 →
  补记 `UNCERTAIN` 结果，历史显式标记）。
- `agent/loop.py`：`boundary()` 在用户任务/模型响应/工具结果/运行结束处通知 Runtime；
  执行工具前调用 `tool_start_sink`。
- `agent/runtime.py`：意图 journal 是消息事实的投影（与消息提交同序）；
  检查点携带 `session_version`、`state_seq`、`pending_intent_ids` 与工作区指纹。

## 5. 核心实现逻辑

```text
运行期（每个提交边界）：
  log.append 成功 → store.append（消息）→ 记录/闭合意图（planned / completed）
  Loop boundary(MODEL_RESPONSE | TOOL_RESULT | USER_TASK | RUN_END)
      → Runtime._save_checkpoint（版本 + state_seq + open intents + 指纹）
  工具执行前 → tool_start_sink → planned → started
  压缩提交 → summary 落库后保存 COMPACTION 检查点

崩溃恢复判定（inspect）：
  指纹不匹配 → STOP
  journal 有意图但历史无调用 → STOP（悬空）
  历史有未解决调用但 journal 无证据 → STOP（拒绝猜测）
  存在 started 未闭合 → RECONCILE（列出 uncertain_intents）
  planned 未执行 → RESUME（resume() 补记未执行结果）
  全部闭合 → RESUME（安全边界 = 最新检查点；旧检查点只作索引）

状态机：planned → started → completed
                      ↘ (crash) → 人工 resolve_uncertain → abandoned | resolved
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Loop | `boundary_sink` | Runtime | `BoundaryEvent` |
| Loop | `tool_start_sink` | Runtime | `ToolExecutionStart` |
| Runtime | `CheckpointStore` | `SQLiteCheckpointStore` | `Checkpoint`、`ToolIntent` |
| RecoveryEngine | SessionStore 只读/补记 | 阶段 19 存储 | `SessionAppend` |
| （阶段 21）故障注入矩阵 | RecoveryEngine | — | 判定断言 |
| （CLI/入口） | `inspect/resume/resolve_uncertain` | — | 人工核对流程 |

## 7. 数据流变化

- 之前：崩溃后只有「已提交消息」可知，无法区分「工具没跑」与「工具跑了但结果丢了」。
- 之后：意图 journal 提供第三种事实——每个工具调用的执行阶段；
  检查点把「会话版本 + 状态机 seq + 未闭合意图 + 工作区指纹」索引到具体边界。
- 不变：消息 append-only；恢复补记只新增消息（不修改/删除历史）。

## 8. 设计原因与备选方案

- **意图 journal 由消息事实投影**：不新增独立写入路径，避免「历史与 journal 双写
  不一致」的常见缺陷；`started` 是唯一执行前写入，因此它就是副作用窗口的唯一证据。
- **保守优先**：任何不一致（悬空、缺证据、状态矛盾）→ STOP 而不是猜测；
  `started` 绝不自动重放（不承诺任意 shell 命令 exactly-once）。
- **planned 自动补记而不是自动重试**：补记「未执行」结果让模型在下一回合自行决定
  是否重复调用，决策权留在计划层，恢复器只做事实登记。
- **指纹不含文件内容**：工作区在 run 中本就会被工具修改，指纹=规范化路径摘要。
- 备选：保存完整状态快照（放弃：与 Session 重复且引入双重事实源）；
  自动重试 started 工具（放弃：违反副作用安全不变量）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_checkpoint.py` | 检查点往返与最新读取；意图状态机（含 mark_started 不回退）；指纹稳定性；缺证据调用/悬空意图→STOP；非 RESUME 计划拒绝 resume（7 例） | 7 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/integration/test_crash_recovery.py` | 提交后崩溃→RESUME；工具启动前崩溃→补记未执行→continue_run 成功；执行中崩溃→RECONCILE→人工核对→RESUME；executed_unknown→UNCERTAIN 历史标记；旧检查点→RESUME；指纹不匹配→STOP（6 例） | 6 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01–19） | 429 passed | 同上 |

## 10. 当前限制

- `resolve_uncertain` 的 `executed_unknown` 只登记不确定结果，不尝试重建真实输出。
- 检查点每提交边界保存一行：长会话行数线性增长（本地 SQLite 可接受，无清理策略）。
- 崩溃注入覆盖进程级中断；未覆盖操作系统层面断电（SQLite WAL 由存储层保证）。
- 自动恢复入口（CLI 一键 inspect→resume）属阶段 21 验收流程的一部分。

## 11. 后续依赖

- 阶段 21：把崩溃注入矩阵与恢复流程写入完整本地验收（Mini Repo 场景含
  「中途杀死进程 → restart → resume」）。
- 阶段 22：SWE-bench 适配器在实例准备失败时复用 STOP 语义（不静默重试）。
- 阶段 23：评测报告中记录恢复相关指标（重放拒绝次数=0）。

## 12. 面试解释

崩溃恢复的关键不是「尽力重试」，而是「区分可确定与不可确定」。我们用意图 journal
把每个工具调用拆成 planned/started/completed 三个阶段：assistant 提交后调用是
planned（确定没跑），执行前翻成 started（跑了没跑未知），结果提交后 completed。
恢复器只对 planned 自动补记「未执行」，对 started 一律要求人工核对——因为任何
自动重放都可能把写文件或 shell 命令执行两次。检查点不复制历史，只用版本号索引，
避免双事实源；一旦记录和 journal 不一致就 STOP，绝不猜测。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本提交（代码+测试+本文档同提交）；
完成日期：2026-09-24。

验收条件逐项核对（dependency-graph.md 阶段 20 + 模块 25 测试清单）：

- 提交后崩溃：`test_crash_after_commit_resumes` ✅
- 工具启动前崩溃：`test_crash_before_tool_start_records_not_executed` ✅
- 工具执行后结果提交前崩溃：`test_crash_during_tool_execution_requires_reconcile` ✅
- 旧检查点：`test_stale_checkpoint_still_resumes` ✅
- 不盲重试写/删/命令：started → RECONCILE；resume 对非 RESUME 计划显式拒绝 ✅
- 无法判定时说明需人工检查：STOP 判定 + 原因文本（悬空/缺证据/指纹）✅
- 「恢复过程能说清会话版本、上一个安全边界及尚未确定的副作用」：
  `RecoveryPlan(session_version, safe_boundary, pending_intents, uncertain_intents)` ✅

设计偏差：无。实现注记：意图 journal 由 Runtime 从消息事实投影（不新增独立写入
路径），仅 `started` 标记在执行前写入；指纹为规范化路径摘要（不含内容哈希）。
