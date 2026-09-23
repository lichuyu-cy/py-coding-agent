# 阶段 04：Minimal Agent Loop

## 1. 本节目标

交付受限额约束的确定性 Agent Loop 与 Runtime 骨架：

- `agent/loop.py`：Think→Act→Observe 循环；状态经 `domain.state` 转移表驱动；
  限额（turn / 工具调用 / deadline）与有限 Provider 重试；最小组件通知（供测试断言）。
- `agent/runtime.py`：`run(task, workspace, session_id?)` 与 `continue_run(session_id)` 入口；
  内存会话注册表与每会话独占运行锁（补足阶段 02 的"无两个活动运行写同一 Session"）；
  `RunResult` 结果包装。

明确未实现（按依赖顺序）：Tool Pipeline（阶段 07，当前用极简工具执行口）、ContextManager
与 Token 预算（阶段 10/13）、steering/follow-up/abort 队列（阶段 09）、流式输出（阶段 17）、
typed EventBus（阶段 15，当前为最小组件通知）、持久会话与检查点（阶段 19/20）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/agent/loop.py` | `AgentLoop`、`RunLimits`、`LoopOutcome`、`ToolOutcome`、`MinimalToolExecutor`(Protocol)、`LoopEvent`/`LoopEventKind`/`LoopObserver` | 有界循环、限额、极简执行口、最小通知 |
| `src/coding_agent/agent/runtime.py` | `AgentRuntime`、`InMemorySessionRegistry`、`RunResult`、`UnknownSessionError`、`RunConflictError`、`ResumeValidationError`、`DEFAULT_SYSTEM_PROMPT` | run 生命周期、会话锁、结果与入口 |
| `tests/integration/test_minimal_loop.py` | 17 个用例 | final / tool→result→final / 多工具 / 修复 / 限额 / 锁 / continue |
| `docs/03-implementation/04-minimal-agent-loop.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `docs/03-implementation/README.md` | 完成索引新增阶段 04 行 | 记录进度；无代码影响 |

## 4. 每个文件的作用

- `loop.py`：输入为 `MessageLog`、`RuntimeState`、workspace；输出为 `LoopOutcome`
  （终态快照、turns/tool_calls/provider_calls、final_text、limit_hit）。
  调用方：`AgentRuntime`（唯一）。失败路径不修改历史：Provider 错误不写 assistant；
  预算耗尽先补齐 CANCELLED 结果再终止。
- `runtime.py`：输入为用户任务与 workspace；输出为 `RunResult` 与已追加的持久历史。
  调用方：CLI / Server / Benchmark（阶段 18/22 复用同一入口）。
- `tests/integration/test_minimal_loop.py`：FakeProvider + FakeExecutor + 真实
  MessageLog/RuntimeState，断言顺序、计数、配对、锁与 continue 校验。

## 5. 核心实现逻辑

单 run 主循环（`AgentLoop.run`）：

```text
START(idle→running) → [每回合]
  ① 限额检查（deadline / max_turns）→ 超限：FINISH(status=BUDGET_EXHAUSTED)
  ② 组装请求（system + 全部已提交消息投影）
  ③ provider.complete（deadline 用 wait_for 包裹；瞬时错误有限重试，仅重发模型请求）
  ④ 提交 AssistantMessage（turn 递增）
     ├─ stop=PROVIDER_ERROR → FAIL
     ├─ stop=CANCELLED → ABORTING → CLEANUP_DONE（记录 stop reason）
     ├─ 有 tool_calls → WAITING_TOOL（每个调用逐一：执行前查工具预算，
     │    执行 → PROCESSING_TOOL_RESULT → 提交 ToolResult → CONTINUE →
     │    下一个调用再次 TOOL_CALL_COMPLETE；批内预算不足：为剩余调用记录
     │    CANCELLED 结果后 FINISH/BUDGET_EXHAUSTED）→ 回到 ①
     └─ 无 tool_calls → FINISH（END_TURN / MAX_TOKENS）
⑤ 每次提交事实后发最小通知（run_start/turn_start/assistant/tool_result/run_end 各一次）
```

- **重试边界**：只对 `RATE_LIMIT/TIMEOUT/UNAVAILABLE` 重试（默认 2 次，退避基数默认 0），
  且只重发当前模型请求；工具绝不因模型请求失败而重放。
- **运行锁**：`try_acquire(session_id)` 失败即 `RunConflictError`；`finally` 释放。
- **continue_run 尾部校验**：会话非空、尾部不是 AssistantMessage、所有历史工具调用均有
  最终结果；workspace 取显式参数或上次 run 记录值。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Runtime | `AgentLoop.run` | `domain.state.RuntimeState.transition` | 转移表与 `StateTrigger` |
| Runtime | `AgentLoop.run` | `domain.messages.MessageLog.append` | 消息配对校验 |
| Runtime | `AgentLoop.run` | `Provider.complete` | `ModelRequest/ModelResponse` |
| Runtime | `AgentLoop.run` | `MinimalToolExecutor.execute` | `ToolOutcome`（阶段 07 替换） |
| Runtime | `AgentRuntime` | `InMemorySessionRegistry` | 会话与运行锁 |

## 7. 数据流变化

- 之前：组件齐备（消息/状态/Provider + Fake）但没有循环。
- 之后：`run(task)` → user 消息入历史 → 模型请求 → assistant/工具结果入历史 →
  再请求，直至 FINISH/ABORTED/ERROR/BUDGET_EXHAUSTED；`RunResult` 携带计数与限额。
- 历史始终 append-only；流式增量与事件系统仍未建立（后续阶段）。

## 8. 设计原因与备选方案

- **状态机驱动而非布尔标志**：每次提交事实伴随一次受 seq 守卫的转换，任何终态后不能再启动
  工具（`can_start_tool` 仅 `WAITING_TOOL` 为真）。
- **极简执行口而非提前实现 Pipeline**：阶段 05 交付工具自身、阶段 07 交付统一管线；
  本阶段用注入式 `MinimalToolExecutor` 保持"工具必经统一口"的形状（生产路径不暴露原始工具）。
- **预算终止先补齐 CANCELLED 结果**：维持"每个提出的调用都有最终结果"这一全局不变量，
  避免历史出现未解决调用（与 critical-contracts 的取消语义一致）。
- **最小通知而非提前实现 EventBus**：`LoopObserver` 仅用于验证"终结事件一次"与顺序；
  阶段 15 将替换为带 seq/订阅/重放的 typed EventBus。
- 备选：deadline 只在回合顶部检查（放弃：长请求会越过 deadline，故用 `wait_for` 强制）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/integration/test_minimal_loop.py -q` | 无工具 final（历史顺序/元数据）、tool→result→final（二次请求携带观察）、多工具 ordinal 顺序、错误结果入上下文、事件顺序与唯一 run_end、max_turns、max_tool_calls（CANCELLED 补齐）、deadline（不等慢响应）、瞬时错误重试、重试耗尽 ERROR、不可重试立即 ERROR、取消响应 ABORTED、会话冲突、continue 尾部校验/未解决调用/正常恢复（17 例） | 17 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1；~0.3s |
| `python -m pytest` | 全量（含阶段 01–03） | 99 passed in 1.03s | 同上 |

## 10. 当前限制

- 无真实工具（阶段 05）与统一 Pipeline（阶段 07）；`ToolOutcome` 是临时结构。
- 无 context 压缩/token 预算：超长历史不受限（阶段 13/14 前明确不承诺）。
- 无 steering/follow-up/abort 的公开 API（阶段 09）；仅处理 Provider 侧 CANCELLED 响应。
- "重复无进展"检测未实现（当前由 max_turns 兜底）；上下文溢出未定义（阶段 13 起）。
- 会话与运行锁只在内存中；进程重启即丢失（阶段 19/20）。
- `LoopObserver` 回调异常会向上传播（阶段 15 的订阅隔离机制尚未建立）。

## 11. 后续依赖

- 阶段 05/07：`MinimalToolExecutor` 位置将由 `ToolPipeline.invoke` 取代，`RunLimits.max_tool_calls`
  的检查点保持在同一位置（执行前查预算）。
- 阶段 06：工具声明经 Registry 导出的 `ToolDefinition` 注入 `AgentLoop.tools`。
- 阶段 09：steering/follow-up/abort 将挂到 `PROCESSING_TOOL_RESULT→CONTINUE` 与回合边界上。
- 阶段 15：`LoopEvent` 将被 typed 事件取代，语义与顺序保持。

## 12. 面试解释

这个 Loop 的关键词是"有界"和"可审计"：每一步都先检查预算，再用状态机记录事实
（assistant 提交、工具结果提交），任何路径退出时历史都是成对的完整调用组——包括预算中途
触发时，也会为未执行的调用补记 CANCELLED 结果。重试只发生在模型请求边界，工具永远不会因为
重试被重放；会话被独占锁保护，`continue_run` 只允许从合法尾部恢复。Fake Provider 让整条
轨迹可以逐条断言，包括"终结通知恰好一次"。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 无工具与虚构工具的确定性循环：`test_final_answer_without_tools`、`test_tool_then_final*` ✅
- 多工具（ordinal 顺序执行、配对）：`test_multiple_tools_executed_in_ordinal_order` ✅
- 工具失败后修复：`test_tool_error_is_observed_by_model` ✅
- 循环上限与中断边界：`test_max_turns_budget`、`test_max_tool_calls_budget_records_cancelled_results`、`test_deadline_budget_stops_before_completion`、`test_provider_cancelled_response_aborts_run` ✅
- 最终历史顺序与调用次数严格一致：各用例断言 + `provider.call_count/remaining_steps` ✅
- 终结事件一次：`test_events_ordered_and_run_end_once`（最小通知机制）✅

设计偏差：无契约变更。实现注记：`RunLimits` 增加 `max_provider_retries`/
`provider_retry_base_delay_seconds`（契约第 6 节的"有限退避由 Runtime 决定"）；
`RunResult` 增加 `provider_calls` 与 `limit_hit` 以报告"哪一个限额耗尽"；
`continue_run` 增加可选 workspace 参数（无持久会话前的必要输入）。
