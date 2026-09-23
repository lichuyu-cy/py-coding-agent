# 阶段 09：Steering / Follow-up / Abort

## 1. 本节目标

交付运行时控制通道（模块 11/12/13）：

- `agent/control.py`：`CancelToken`、`SteeringQueue`、`FollowUpQueue`、`RunControl`、
  `AbortReason` 与控制错误族（幂等、上限、显式拒绝）；
- Loop 接入：取消优先级最高的边界检查、steering 只在模型请求边界注入（一次性 drain）、
  follow-up 在 turn 结束边界开新回合、取消时为未执行调用补 CANCELLED 结果；
- Runtime 公开 API：`submit_steering(run_id)`、`abort(run_id)`、`submit_follow_up(session_id)`、
  `pending_follow_ups(session_id)`。

明确未实现：队列位置持久化与崩溃恢复（阶段 19/20）；SSE 传输层（阶段 18）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/agent/control.py` | `CancelToken`、`SteeringQueue`、`FollowUpQueue`、`RunControl`、`SteeringMessage`、`FollowUpItem`、`AbortReason`、`ControlError`/`SteeringRejectedError`/`UnknownRunError`/`QueueLimitExceededError` | 控制通道与幂等语义 |
| `tests/unit/test_control.py` | 10 个用例 | 令牌幂等、队列顺序/drain/去重/上限 |
| `tests/integration/test_runtime_controls.py` | 8 个用例 | 注入边界、新回合、中止协作 |
| `docs/03-implementation/09-runtime-controls.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/agent/loop.py` | 新增 `control`/`follow_ups` 参数；取消检查（优先级最高）；STEERING 状态转换与注入；follow-up 新回合；工具批内取消补齐 CANCELLED；provider 错误路径区分取消；新增 `STEERING_INJECTED`/`FOLLOW_UP_STARTED` 事件 | 行为扩展；无控制对象时自建空 `RunControl`（兼容旧调用） |
| `src/coding_agent/agent/runtime.py` | 活动 run 表（run_id→RunControl）、会话级 follow-up 队列、控制 API、run/continue 注入控制 | 新增 API；既有接口不变 |

## 4. 每个文件的作用

- `control.py`：输入为文本/原因/run 标识；输出为控制对象与队列项。
  调用方：Runtime（创建/转发）、Loop（消费）。`CancelToken` 实现 `ports.provider.CancelSignal`，
  Provider/Tool 直接消费。
- `loop.py`：新增的控制流——每个边界处的取消检查、请求前的 steering 注入、
  turn 结束的 follow-up 取出；全部保持"恰好一个终结事件"。
- `runtime.py`：控制 API 的生命周期管理（run 结束即从活动表移除；重复操作按幂等/显式拒绝处理）。

## 5. 核心实现逻辑

```text
run 控制流（每回合）：
 ① 取消检查（最高优先级）→ abort_run(reason)：ABORTING → CLEANUP_DONE(ABORTED)
 ② deadline / max_turns 预算检查（既有）
 ③ STEERING → RUNNING（若处于该状态）→ steering 一次性 drain → 作为 UserMessage 注入
 ④ 模型请求（携带 cancel token；CANCELLED 错误 → 中止路径而非 FAIL）
 ⑤ 工具批：每个调用执行前查取消 → 未执行的剩余调用补 CANCELLED 结果（配对完整）→ 中止
       执行中的工具经 cancel token 协作终止（Pipeline/Bash 已对接）
       批尾：若有 steering 待注入 → PROCESSING_TOOL_RESULT → STEERING（下次请求前注入）
 ⑥ turn 结束（无工具调用）：steering 优先注入 → 否则 follow-up 取一个开新 turn（FOLLOW_UP→TURN_START，
   turn+1）→ 否则 FINISH
```

幂等与拒绝：`CancelToken.cancel` 重复调用返回 False（原因保留首次）；steering/follow-up 以
`client_event_id` 去重；run 结束后的 steering 显式 `SteeringRejectedError`（改用 follow-up）；
未知/已结束 run 的 abort 抛 `UnknownRunError`。队列上限 `QueueLimitExceededError`。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Runtime | `RunControl` | Loop | `control`/`follow_ups` 参数 |
| Loop | `CancelToken` | Provider/Tool Pipeline | `CancelSignal` 协议 |
| Loop | `SteeringQueue/FollowUpQueue` | MessageLog | 注入为 UserMessage |
| SSE（阶段 18） | 控制 API | Runtime | `submit_steering/abort/submit_follow_up` |

## 7. 数据流变化

- 之前：run 只能一次性执行到终态；无外部控制入口。
- 之后：活动 run 可接收 steering（注入下一次请求）、follow-up（事后排队成新回合）、
  abort（协作终止）；所有控制消息最终以 UserMessage 进入同一 append-only 历史。
- 取消路径的持久数据保持一致：未执行调用均有明确 CANCELLED 结果，不存在孤立调用。

## 8. 设计原因与备选方案

- **取消优先于一切边界检查**：先于 deadline/turn 预算判断，保证"尽快停止"。
- **steering 只在请求边界注入**：模型请求发出后不可篡改；工具执行中间不做注入，
  保证同一请求的快照语义（对应 contracts 第 1 节的"下一请求边界"）。
- **批尾 STEERING 状态转换**：使用状态机的 `PROCESSING_TOOL_RESULT→STEERING→RUNNING` 行，
  与设计表一致；steering 在 RUNNING 期间到达则仅排队（表中无对应转换）。
- **取消的调用补 CANCELLED 结果**：维持"每个提出的调用恰好一个结果"的不变量
  （与阶段 04 预算路径相同的处理模式）。
- **follow-up 按会话持有**：run 中止/失败时"保留但不自动启动"；下一次 run 的 turn 结束
  边界按正常语义取出。
- 备选：abort 直接抛异常中断协程（放弃：会跳过持久化与配对补齐，破坏不变量）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_control.py` | 令牌幂等/原因保留、steering 顺序与序列、drain 一次性、client_event_id 去重、空文本拒绝、上限（Follow-up 同）（10 例） | 10 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/integration/test_runtime_controls.py` | 注入边界（首请求无、次请求仅一次）、结束后拒绝、去重、follow-up 新回合与首请求隔离、abort 保留队列、provider 延迟中中止（碎片不入历史、RUN_END 一次）、工具批中止（已开始取消/未执行补齐、无新请求）、未知/已结束 run 拒绝（8 例） | 8 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01–08） | 245 passed in 8.52s | 同上 |

## 10. 当前限制

- 队列只在内存中：进程重启后 steering/follow-up 丢失（阶段 19 持久化游标、阶段 20 恢复）。
- 取消对"不检查取消信号"的工具无强制力（read/write/edit 为快速操作，未接入轮询）。
- steering 语义保持最小：仅文本、单优先级（设计中的扩展项不做）。
- `abort` 的竞态以"先提交的状态序号为准"（阶段 02 已保证）；cancel 与完成同时到达时
  以 Loop 实际观察到的边界为准。

## 11. 后续依赖

- 阶段 18：SSE 端点调用控制 API（Start/Abort/Steering/Follow-up），断连默认只解除订阅。
- 阶段 19：持久化队列位置与 run 元数据；阶段 20：恢复时校验 pending 意图。
- 阶段 20（UNCERTAIN）：中止发生在工具执行中时的副作用不确定判定。

## 12. 面试解释

控制通道的关键是"控制不破坏事实"：steering 永远只在模型请求边界注入（请求发出后不可篡改），
follow-up 只在 turn 结束取出，abort 只是设置协作式取消标志，由 Loop 在安全边界把它落成
ABORTING→ABORTED，并为尚未执行的调用补齐 CANCELLED 结果——所以无论怎么中断，历史里都不会
出现没有结果的调用。幂等性也做了细化：重复 abort 返回"非首次"，重复提交的 steering 以
client_event_id 去重，run 结束后的 steering 显式拒绝而不是静默丢弃。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 指定边界注入：`test_injected_at_next_request_boundary`（首请求无、次请求有且一次）✅
- 中断不启动新工具：`test_abort_mid_tool_batch_does_not_start_next_tool`（call_2 未执行，
  补 CANCELLED；`provider.call_count == 1`）✅
- 重复 Abort、结束后到达的 steering 处理：`test_abort_during_provider_delay`、
  `test_steering_rejected_after_run_ends` ✅
- follow-up 不在尚未完成的当前模型调用上下文：`test_follow_up_starts_new_turn_after_task_end` ✅

设计偏差：无契约变更。实现注记：`RunControl` 在未显式注入时由 Loop 自建空实例（兼容路径）；
`STEERING_INJECTED`/`FOLLOW_UP_STARTED` 为最小通知事件（阶段 15 并入 typed EventBus）。
