# 阶段 17：Streaming

## 1. 本节目标

交付流式增量的语义化聚合（模块 21）：

- `observability/stream.py`：`ToolCallAccumulator`（分片参数拼装）、`AgentStreamAggregator`
  （文本/usage/stop 聚合与同契约校验）、`InterruptedStreamError`/`StreamAssemblyError`；
- Provider 端口扩展 `ToolCallFragmentDelta`；Fake 支持分片脚本与流截断；
- Loop 增加 `streaming` 路径：增量聚合为完整响应，易失增量以 `LLM_REQUEST_STREAM` 事件广播。

明确未实现：SSE 网络编码（阶段 18）；多模态分片（扩展项）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/observability/stream.py` | `ToolCallAccumulator`、`AgentStreamAggregator`、`InterruptedStreamError`、`StreamAssemblyError` | 增量聚合与中断防护 |
| `src/coding_agent/ports/provider.py`（扩展） | `ToolCallFragmentDelta` | 分片参数增量 |
| `tests/unit/test_streaming.py` | 13 个用例 | 拼装/去重/损坏/中断/一致性 |
| `docs/03-implementation/17-streaming.md` | — | 本文档（补齐记录；代码提交在先，见提交历史） |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/domain/events.py` | 新增 `LLM_REQUEST_STREAM`（易失可丢弃） | 流式增量事件 |
| `src/coding_agent/agent/loop.py` | `streaming` 参数；`call_model()` 双路径；逐增量发布易失事件 | 默认 `False`，行为不变 |
| `src/coding_agent/agent/runtime.py` | `streaming` 透传；默认总线 droppable 含 `LLM_REQUEST_STREAM` | 慢订阅者丢弃而不阻塞 |
| `src/coding_agent/bootstrap.py` | `streaming` 透传 | 组装层 |
| `src/coding_agent/providers/fake.py` | `arguments_json_chunks`、`truncate_stream` | 分片与中断脚本 |

## 4. 每个文件的作用

- `stream.py`：输入为 `ModelDelta` 序列；输出为完整 `ModelResponse`（与 complete 同契约）。
  半段调用（无 name/无参数/损坏 JSON/无 StopDelta）全部显式失败，绝不进入工具管线。
- `loop.py`：`call_model()` 统一两条路径；增量事件只广播"可丢"摘要（文本/分片长度）。

## 5. 核心实现逻辑

```text
aggregate：TextDelta 按抵达顺序拼接；ToolCallDelta → 完整形态登记（幂等去重）；
           ToolCallFragmentDelta → 按 fragment_seq 排序拼 JSON；Usage/Stop 记录最新值
finalize_response：无 StopDelta → InterruptedStreamError；拼装失败 → ProviderError(INVALID_RESPONSE)；
           validate_model_response 同队列校验（stop/calls 兼容性）
Loop：streaming=True 时消费流并逐增量发布 LLM_REQUEST_STREAM（droppable）
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Loop | `AgentStreamAggregator` | Provider.stream | `ModelDelta` |
| EventBus | `LLM_REQUEST_STREAM` | — | 易失事件 |
| （阶段 18）SSE | 事件流 | — | 编码转发 |

## 7. 数据流变化

- 之前：只有 complete() 完整响应路径。
- 之后：同一脚本可用两条路径；最终持久消息结构一致；增量仅作暂存与广播，不入历史。

## 8. 设计原因与备选方案

- **聚合产物走同一校验**：保证"流式不会引入 complete 没有的响应形态"；
- **fragment_seq 排序 + 去重**：乱序/重复片段幂等；不同 seq 冲突先到先得；
- **中断显式异常**：InterruptedStreamError 继承 ProviderError(INVALID_RESPONSE)，
  复用 Loop 的失败路径，无需特殊分支；
- 备选：容忍缺 StopDelta 直接收尾（放弃：可能把半截调用送入工具）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_streaming.py` | 乱序拼装、重复 seq 去重、完整+分片混用、损坏 JSON、缺 name、空参数、usage 缺失、中断、stop 冲突、complete/stream 产物一致、分片经完整 run 执行、中断不落历史不执行（13 例） | 13 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–16） | 386 passed | 同上 |

## 10. 当前限制

- 增量事件的文本载荷未做长度治理（易失且可丢，SSE 层按需截断）。
- 无增量级背压重放（审计仍按完整事件记录；流式事件可被订阅端丢弃）。

## 11. 后续依赖

- 阶段 18：SSE 把事件（含流式）编码为帧；心跳与 last-event-id 重连。
- 阶段 21：流式中断纳入故障注入矩阵。

## 12. 面试解释

流式聚合的核心不变量是"流式与一次性调用产物一致"，因此两条路径共用同一响应校验；
分片参数按 seq 排序拼装、重复去重，只有完整 JSON 且全部分片到位才产出调用；缺 StopDelta
直接判定中断并复用 Provider 失败路径——系统里不存在"半截工具调用被送进执行"的路径。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：代码提交在本记录之前的提交中
（阶段 17 提交未含本文档，按设计逐阶段提交的要求存在一次偏差）；完成日期：2026-09-24。

验收条件逐项核对：

- 同脚本 complete 与 stream 的最终持久消息一致：`test_complete_and_stream_produce_identical_persisted_messages` ✅
- 分片 JSON、乱序/重复片段、流中断、final usage 缺失：对应单元用例 ✅

设计偏差：本记录晚于代码提交（文档与代码未同提交）——已在提交信息中标注待补，此处如实记录。
实现注记：`ToolCallFragmentDelta` 新增于 ports；Fake 的 `arguments_json_chunks` 同时服务
complete 与 stream（同源保证一致性）。
