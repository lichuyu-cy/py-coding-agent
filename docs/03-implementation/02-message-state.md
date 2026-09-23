# 阶段 02：Message / State

## 1. 本节目标

交付消息系统与运行态类型：

- 消息事实记录：`SystemMessage`、`UserMessage`、`AssistantMessage`、`ToolCall`、`ToolResult`、
  `MessageMeta`、`ToolResultStatus`，以及追加验证与序列化（`MessageLog`）。
- 运行态：`AgentState`、`StopReason`、`RunStatus`、`CancellationState`、`RuntimeState`
  及完整转换表（`transition` / `can_start_tool` / `request_abort`）。
- 定义 `schema_version = 1` 与消息/会话 ID 生成规则，为阶段 19–20 持久化做前置。

明确未实现：存储与恢复、事件发布、Provider 投影、Context 组装、会话独占运行锁
（锁随 Runtime 落地，见第 10/13 节）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/domain/errors.py` | `HarnessError`、`MessageValidationError(code)`、`SchemaVersionError`、`StateTransitionError(current_state, trigger)` | 错误层次，供上层按类统一处理 |
| `src/coding_agent/domain/messages.py` | `MESSAGE_SCHEMA_VERSION`、`MessageId`、`ToolCallId`、`MessageMeta`、`ToolCall`、`ToolResultStatus`、`SystemMessage`、`UserMessage`、`AssistantMessage`、`ToolResult`、`Message`、`message_to_dict/from_dict`、`MessageLog`、`new_id/new_message_id/new_tool_call_id`、`utc_now_rfc3339/format_utc` | 消息事实记录、配对校验、序列化往返 |
| `src/coding_agent/domain/state.py` | `StopReason`、`RunStatus`、`AgentState`、`CancellationState`、`StateTrigger`、`ACTIVE_STATES`、`TERMINAL_STATES`、`RuntimeState` | run 的唯一控制位置与退出原因 |
| `tests/unit/test_messages.py` | 19 个用例 | 序列化往返、ID 唯一、多调用配对、各拒绝路径 |
| `tests/unit/test_state.py` | 43 个用例（含参数化 29 条转换/非法组合） | 转换表覆盖、竞态守卫、终止语义、stop reason 独立性 |
| `docs/03-implementation/02-message-state.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `docs/03-implementation/README.md` | 完成索引新增阶段 02 行 | 记录进度；无代码影响 |

## 4. 每个文件的作用

- `errors.py`：输入为错误场景字符串；输出为带结构化字段的异常（如 `code`）。
  调用方为 domain 自身与后续所有层。`code` 是稳定契约，测试与结构化拒绝结果引用它。
- `messages.py`：输入为外部用户文本、完整模型响应、工具管线结果；输出为按会话追加的记录与
  序列化快照。调用方：Runtime 写入；Context/Session/Compaction 读取；Fake Provider 集成（后续）。
  `MessageLog.append` 是唯一写入口，集中执行全部一致性校验。
- `state.py`：输入为 Runtime 已确认的触发事件与 expected_seq；输出为新的 `RuntimeState` 快照。
  只有 Runtime 调用；EventBus 等只能观察快照值，不得驱动转换。
- `tests/unit/test_messages.py`：直接构造/往返消息数据，断言校验与拒绝路径。
- `tests/unit/test_state.py`：以参数化表覆盖转换矩阵与非法组合；断言 seq 守卫与终止语义。

## 5. 核心实现逻辑

消息链路（`MessageLog.append`）：

1. 类型检查（四种消息之一）→ meta 类型检查 → `session_id` 一致；
2. 全局消息 ID 唯一；
3. `AssistantMessage`：ordinal 必须从 0 连续、消息内 call ID 不重复、call ID 全局唯一、
   name 非空、arguments 可 JSON 序列化；
4. `ToolResult`：`tool_call_id` 必须指向已存在的 call（孤立结果拒绝）、每个 call 至多一个最终结果；
5. 通过后追加；任何失败都不修改已有记录、不改写输入（不静默修复）。

序列化：`{"schema_version": 1, "session_id", "messages": [...]}`；反序列化按追加顺序重建并
重放全部校验；`schema_version != 1` 一律 `SchemaVersionError`（含每条 meta 内的版本）。

状态链路（`RuntimeState.transition`）：

1. `expected_seq` 与 `state_seq` 不符 → `StateTransitionError`（并发冲突，旧快照提交被拒）；
2. 终结态（`FINISHED/ABORTED/ERROR`）拒绝一切转换；
3. 查转移表（`architecture.md` 状态机，逐行编码；见第 13 节补充行）；非法组合拒绝；
4. `replace` 生成新快照：seq+1、turn 计数（START→1，TURN_START 递增）、
   `current_tool_call_id` 生命周期（进入 `WAITING_TOOL` 设置，仅在
   `WAITING_TOOL→PROCESSING_TOOL_RESULT` 保留，其余转换清除）、
   取消生命周期（`ABORT_REQUESTED→REQUESTED`、`CLEANUP_DONE→COMPLETED`）、
   终结状态映射（`FINISH→FINISHED`、`FAIL→ERROR`、`COMPACTION_FAILED→ERROR`、`CLEANUP_DONE→ABORTED`，
   可显式覆盖，如 `FINISH` 时记 `BUDGET_EXHAUSTED`）。

```text
IDLE ─start→ RUNNING ─complete_tool_call→ WAITING_TOOL ─result_resolved→ PROCESSING_TOOL_RESULT
  │             │  ↑                                                                  │
  │             │  │← steering_injected ← STEERING ←──────────── steering ────────────┤
  │             │  │← compaction_success ← COMPACTING ← context_insufficient ──────────┤
  │             │  │← new_turn ← FOLLOW_UP ← task_done_with_followup ──────────────────┤
  │             │  └─ no_more_messages → FINISHED / unrecoverable_error → ERROR        │
  │             └────── abort（任意活动态）→ ABORTING ─ cleanup_done → ABORTED ────────┘
  └─ IDLE 不可 abort；终结态不可再转换
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| （后续）Runtime | `MessageLog.append` | — | `Message`、`MessageMeta` |
| （后续）Runtime | `RuntimeState.transition` | — | `StateTrigger`、`RuntimeState` |
| 领域内部 | `messages` | `state.StopReason` | 助手消息的 stop_reason 类型 |
| 领域内部 | 两者 | `errors` | 统一异常层次 |

## 7. 数据流变化

- 之前：无消息与运行态数据结构。
- 之后：外部输入 →（校验）→ 追加进 `MessageLog` → 可序列化为稳定 JSON（可重建调用顺序、
  可查询任一 call 的最终结果）；运行态以不可变快照在转换间传递，序号单调。
- 仍未建立：事件发布、Provider 投影、持久化（后续阶段）。

## 8. 设计原因与备选方案

- **不可变 dataclass + 集中校验**：消息是审计事实，禁止就地修改；所有生成路径（构造、反序列化）
  经过同一 `append` 校验。备选：构造时即校验（放弃：无法校验跨消息关联，如孤立结果）。
- **NewType 而非独立类**：`MessageId`/`ToolCallId` 运行时即 str，序列化零成本；备选：值对象类
  （放弃：增加噪音，收益低）。
- **转换表集中编码 + expected_seq 守卫**：把并发冲突变成显式错误而不是竞态隐患；
  终结状态映射与 stop reason 解耦，保证“Provider stop reason ≠ RunStatus”。
- **`STEERING + steering_injected → RUNNING` 补充行**：设计表的 steering 行只说明“下次请求前
  插入消息”，未给出退出边；按“注入后回到运行态”补全（记录于第 13 节，属补全而非契约变更）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_messages.py -q` | 序列化往返、调用顺序重建、版本/类型/时间戳拒绝、重复 ID、重复 call、孤立结果、重复结果、ordinal 连续、非 JSON 参数、会话不一致、消息不可变、ID 唯一、时间格式（19 例） | 19 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/unit/test_state.py -q` | 20 条合法转换参数化、9 条非法组合、stale seq、竞态重复提交、仅 WAITING_TOOL 可启动工具、终结态封禁、abort 幂等/活性、取消与状态映射、stop reason 与 RunStatus 独立、回合与工具跟踪（43 例） | 43 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01 冒烟） | 65 passed in 0.13s | 同上 |

## 10. 当前限制

- 会话独占运行锁（“无两个活动运行写同一 Session”）未实现：目前以 `state_seq` 守卫并发提交；
  运行锁随 Runtime 在阶段 04 以内存形态落地、阶段 19 持久化。
- 无事件发布（MessageCommitted 等属阶段 15）；无持久存储（阶段 19）；无 Context/Provider 投影。
- `Summary` 类型未定义（阶段 14）；`ToolResult` 的错误细分字段（retryable 等）在阶段 08 定稿。
- 消息内 `arguments` 为浅拷贝的普通 dict（未做深层不可变），调用方不得就地修改。

## 11. 后续依赖

- 阶段 03 的 Fake Provider 输出将直接构造 `ToolCall`/`AssistantMessage`（含 stop reason 兼容性检查）。
- 阶段 04 的 Loop 将以 `RuntimeState.transition` 驱动状态、以 `MessageLog` 记录消息，
  并新增内存运行锁。
- 阶段 14 压缩将引用 `MessageLog` 的分组与 `covered_through_id` 语义；阶段 19 直接复用本阶段
  序列化格式（`schema_version=1`）。

## 12. 面试解释

消息层我做成“事实表 + 唯一写入口”：所有写入都过 `MessageLog.append` 的跨消息校验
（孤立工具结果、重复结果、ordinal 连续性），保证任何时刻的历史都能重建每次调用的完整配对。
运行态则用不可变快照 + expected_seq 守卫生成新状态，把并发冲突显式化；并刻意让模型 stop reason
与 run 最终状态是两个独立枚举，避免把 `MAX_TOKENS` 这种模型侧原因误当成业务结果。
这套结构让后续 Loop、压缩、持久化都建立在可验证的确定性数据上。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 可从序列化历史重建调用顺序：`test_call_order_reconstructable` ✅
- 任何 call 结果关联可查询：`MessageLog.find_tool_call/find_result` + 往返测试 ✅
- 终结状态不能再次执行工具：`test_can_start_tool_only_in_waiting_tool`、`test_terminal_states_cannot_transition` ✅
- 无两个活动运行写同一 Session：部分覆盖（seq 守卫、竞态拒绝）；会话锁随 Runtime 落地（第 10 节）⏳

设计偏差：无契约变更。补充未列出的转移行 `STEERING + steering_injected → RUNNING`
（依据：steering 只在下次模型请求边界注入后回到运行态；对应测试参数化中包含且注释标注）。
