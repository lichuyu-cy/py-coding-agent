# 阶段 15：Event Bus

## 1. 本节目标

交付 typed 事件总线（模块 19）：有序发布、订阅隔离、背压与重放。

- `domain/events.py`：`EventType`（16 类事件目录）、`AgentEvent`（event_id/seq/utc_time/payload_version）；
- `observability/event_bus.py`：`EventBus`（emit/subscribe/drain/unsubscribe/events/subscription_info）；
- 生产者接线：Loop 发布 Agent/LLM/Tool/Steering/FollowUp/Abort/Error/End；
  ContextManager 经 report 回调发布 ContextBuild/CompactionStart/End。

明确未实现：SessionSave/CheckpointSave 生产者（阶段 19/20）；SSE 传输（阶段 18）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/domain/events.py` | `EventType`、`AgentEvent`、`PAYLOAD_VERSION` | 事件契约 |
| `src/coding_agent/observability/event_bus.py` | `EventBus`、`SubscriptionInfo` | 发布/订阅/背压/审计 |
| `tests/unit/test_event_bus.py` | 14 个用例 | 序号/过滤/隔离/背压/重放/审计 |
| `tests/integration/test_event_bus_flow.py` | 6 个用例 | 完整 run 事件序列、终结唯一、订阅隔离 |
| `docs/03-implementation/15-event-bus.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/context/builder.py` | `build/_compact_and_rebuild/_rebuild_with_compaction` 增加 `report` 回调（CONTEXT_BUILD/COMPACTION_START/END/ERROR） | 事件生产；默认 None 无行为变化 |
| `src/coding_agent/agent/loop.py` | 新增 `event_bus` 参数与发布点（AGENT_START/LLM_*/TOOL_*/STEERING/FOLLOW_UP/ABORT/ERROR/AGENT_END） | typed 事件取代"仅最小通知"的愿景；`LoopObserver` 保留为诊断回调 |
| `src/coding_agent/agent/runtime.py` | `event_bus` 参数；`runtime.bus` 公开 | 总线默认自动创建 |
| `src/coding_agent/bootstrap.py` | `event_bus` 透传 | 组装层 |

## 4. 每个文件的作用

- `event_bus.py`：输入为事件类型/run/session/payload；输出为 `AgentEvent` 与订阅投递。
  订阅两种模式：内联 handler（同步、异常隔离并记录）与有界邮箱（`drain` 消费）。
- `builder/loop`：生产者——只发布"已确认事实"（如 ToolCallEnd 在结果入库后；
  LLMRequestEnd 在完整响应提交后）。

## 5. 核心实现逻辑

```text
emit：分配 run 内 seq（1 起）→ 写有界审计（deque maxlen）→ 扇出：
  内联 handler：try/except 记录到 subscriber_errors（不中断）
  邮箱订阅：队满时——droppable 类型丢弃并计数；否则断开该订阅（计数器记录）
订阅过滤：run_id?、types?；cursor>seq 时从审计预填充邮箱（重放）
审计读取：events(run_id?, after_seq?, types?) 供重放/审计（有界保留）
```

顺序约束：Loop 的发布点都在状态提交之后（assistant 入库 → LLMRequestEnd；
结果入库 → ToolCallEnd/Error；终态确定 → AGENT_END，恰好一次）。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Loop / ContextManager | `EventBus.emit` | — | `EventType`、`AgentEvent` |
| （阶段 16）Metrics | `subscribe(handler)` | — | on_event 消费 |
| （阶段 18）SSE | `subscribe(cursor)` + `events` | — | 断线重连重放 |

## 7. 数据流变化

- 之前：只有 Loop/Pipeline 的最小通知回调（诊断用途）。
- 之后：所有控制与提交事实以 typed 事件进入总线；审计可重放；
  订阅者故障与背压被隔离，不影响控制路径。

## 8. 设计原因与备选方案

- **邮箱 + 内联双模式**：内联用于 Metrics 这类即时聚合；邮箱用于网络消费者（SSE）带游标消费；
- **droppable 集合可配置**：当前无流式 delta（阶段 17 接入），先提供机制与测试；
- **审计有界**：内存约束下的重放窗口；超出窗口由消费者重新订阅当前状态（阶段 18 文档化）；
- **保留 LoopObserver**：历史测试与诊断路径不变；typed 总线为对外契约；
- 备选：全局单例总线（放弃：违反"组装发生于单一 bootstrap"与可测试性）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_event_bus.py tests/integration/test_event_bus_flow.py` | 序号单调/跨 run 独立、字段格式、过滤、drain 顺序、handler 内联与异常隔离、退订、背压（断开/丢弃）、游标重放、审计切片与有界、完整 run 事件序列与端一唯一、工具/Provider 失败事件、中止、steering/follow-up、压缩事件、订阅隔离（20 例） | 20 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–14） | 355 passed in 12.19s | 同上 |

## 10. 当前限制

- 审计有界（默认 10,000）；超窗重放不可用（阶段 18 规划 cursor 错误码）。
- SessionSave/CheckpointSave 尚无生产者（阶段 19/20）。
- 断开的订阅者不会自动重连（消费方重订阅即可，语义简单）。

## 11. 后续依赖

- 阶段 16：Metrics 以内联 handler 订阅；Logger 记录脱敏诊断。
- 阶段 17：流式 delta 事件可配置为 droppable。
- 阶段 18：SSE 用邮箱订阅 + cursor 重放；断连仅退订。
- 阶段 19/20：SessionSave/CheckpointSave 事件。

## 12. 面试解释

事件总线的关键不变量是"事件是已发生事实、消费者不能反过来影响控制"。我把发布点全部放在
状态提交之后（如 ToolCallEnd 在工具结果入库后、AGENT_END 在终态确定后且恰好一次），订阅端
分内联与邮箱两种：内联用于 Metrics 聚合，异常被隔离并记录；邮箱用于网络消费者，队满时要么
丢可恢复事件要么断开慢订阅者——两种情况都不会阻塞控制路径。审计队列提供 seq 游标重放，
为后续 SSE 断线重连铺路。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-24。

验收条件逐项核对：

- 同一 run 的已确认状态事件单调：`test_seq_monotonic_per_run_and_independent_between_runs`、
  集成序列断言 ✅
- 订阅端失败不污染控制状态：`test_handler_exception_isolated_and_recorded`、
  `test_subscriber_isolation_during_run` ✅
- 顺序、订阅抛错、背压、重放 cursor、终结事件一次：对应 5 类用例 ✅

设计偏差：无契约变更。实现注记：`droppable` 默认空集（阶段 17 接入流式 delta 后启用）；
`events()` 读取受审计上限约束；Pipeline 的最小通知保留为内部诊断（typed ToolCall 事件由
Loop 在结果入库后发布，符合"ToolCallEnd 只在结果被确认后"的契约）。
