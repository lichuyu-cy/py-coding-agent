# 阶段 16：Metrics / Logging

## 1. 本节目标

交付指标聚合与脱敏日志（模块 20）：

- `observability/metrics.py`：`MetricsAccumulator`（事件流 → 幂等聚合）、`RunMetrics`、`MetricSnapshot`；
- `observability/logging.py`：`StructuredLogger`、`LogRecord`、`sanitize_fields`（敏感键遮蔽/长值截断）；
- 接线：Runtime 自动 `connect(bus)`；AGENT_END 时自动输出并 persist 快照（阶段 19 改写 Session）。

明确未实现：OTel/成本仪表盘（扩展项）；Session 持久化（阶段 19）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/observability/metrics.py` | `MetricsAccumulator`、`RunMetrics`、`MetricSnapshot` | 事件聚合与快照 |
| `src/coding_agent/observability/logging.py` | `StructuredLogger`、`LogRecord`、`sanitize_fields`、`MASK`、`MAX_VALUE_CHARS` | 脱敏结构化日志 |
| `tests/unit/test_metrics.py` | 14 个用例 | 计数/去重/未知 usage/负时长/脱敏 |
| `tests/integration/test_metrics_flow.py` | 4 个用例 | 真实 run 快照与手算一致 |
| `docs/03-implementation/16-metrics-logging.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/agent/runtime.py` | `metrics` 参数；默认 `MetricsAccumulator.connect(self.bus)`；公开 `runtime.metrics` | 生产默认开启观测 |
| `src/coding_agent/bootstrap.py` | `metrics` 透传 | 组装层 |

## 4. 每个文件的作用

- `metrics.py`：输入为 `AgentEvent`；输出为 `MetricSnapshot`（只读）。订阅采用内联 handler，
  只做计数不阻塞；对工具决策零影响。
- `logging.py`：输入为 level/message/fields/correlation；输出为 `LogRecord`（脱敏后）。
  sink 失败仅累计计数（观测缺口），绝不抛出。

## 5. 核心实现逻辑

```text
on_event：event_id 去重 → 按类型更新计数：
  LLM_REQUEST_START/END → 请求数；END 的 usage：缺失记 unknown（不记 0），已知值求和（混用时只算已知）
  TOOL_CALL_START/END/ERROR（status=cancelled 单独计数）
  COMPACTION_END → 次数与 token 前后和；STEERING/FOLLOW_UP/ABORT/ERROR(stage) 计数
  AGENT_START/END → 起止时间；duration = end - start（为负 → None + negative_duration anomaly）
  AGENT_END → 生成快照并 persist（终结算唯一）
脱敏：敏感键（api_key/secret/token/password/authorization/credential/cookie…）递归 → "***"；
      长值 >2000 字符截断并标注；bytes → "<N bytes>"
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| EventBus | `subscribe(handler=on_event)` | — | `AgentEvent` |
| Runtime | connect/persist | — | `runtime.metrics` |
| （阶段 18）SSE | `snapshot(run_id)` | — | 只读摘要查询 |
| （阶段 19）Session | `persist(snapshot)` | — | 指标随会话保存 |

## 7. 数据流变化

- 之前：事件进入总线后无消费者。
- 之后：每个 run 的聚合快照在终结时自动产生；可随时查询；usage 缺口显式记录。

## 8. 设计原因与备选方案

- **去重键为 event_id**：重复投递/重放天然幂等；乱序计数可交换（测试覆盖）；
- **负数防护**：时钟异常不得产生"看似有效"的时长；anomaly 显式列出；
- **usage 语义**：unknown 单独计数，混用时只累加已知值（不拿 0 冒充）；
- **内联 handler 而非邮箱**：聚合无背压需求；若未来昂贵可切换邮箱 + drain；
- 备选：日志直接写文件（放弃：I/O 与权限复杂度；sink 由入口注入）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_metrics.py tests/integration/test_metrics_flow.py` | 全轨迹计数与持久化、重复投递幂等、未知 usage、混合 usage、取消/失败工具分计、负时长、未知 run、乱序可交换、递归脱敏、长值截断、bytes、关联 ID、sink 失败不抛、有界记录；集成：正常/中止/失败 run 快照与手算一致（18 例） | 18 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–15） | 373 passed in 10.45s | 同上 |

## 10. 当前限制

- 快照为进程内存（persist 列表）；跨重启需阶段 19 的 Session。
- `turns` 取自 AGENT_END 载荷；异常终止（如 build 抛错外）也由 AGENT_END 覆盖。
- 日志无级别过滤/格式化输出（sink 由调用方决定）。

## 11. 后续依赖

- 阶段 17：流式 delta 事件（droppable）不影响计数（只统计确认事件）。
- 阶段 18：`GET /runs/{id}` 返回 `MetricSnapshot`。
- 阶段 19：persist 改写入 Session。

## 12. 面试解释

指标系统把"手算得出来的数字"作为验收标准：每个 LLM 请求、工具调用、压缩、异常都来自事件
流，重复投递通过 event_id 去重后仍然精确；缺 usage 的请求单独计 unknown，而不是拿 0 冒充；
时钟异常导致的负时长被显式标记为 anomaly。日志侧的原则是"能记关联、不能泄密"：所有嵌套
字段递归脱敏，长文本截断，sink 挂掉只留下失败计数而不影响运行。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-24。

验收条件逐项核对：

- usage/工具计数正确：`test_full_trajectory_counts`、集成快照 vs 手算 ✅
- 不泄露敏感值：脱敏用例（api_key/Authorization/token/password 递归遮蔽）✅
- 假事件流的计数、重复投递、负时长防护：对应单元用例 ✅
- Fake 轨迹可独立手算并与快照一致：`test_snapshot_matches_hand_counted_trajectory` ✅

设计偏差：无契约变更。实现注记：`MetricSnapshot` 包裹 `RunMetrics`（设计与实现命名对齐）；
TOOL_CALL_START 语义为"进入管线前发布"（含被拒绝的调用；未执行调用同时产生
TOOL_CALL_ERROR(status=cancelled/…)，计数语义在测试中锁定）。
