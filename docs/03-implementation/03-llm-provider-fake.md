# 阶段 03：LLM Provider + Fake

## 1. 本节目标

交付 Provider 端口与脚本化 Fake：

- `ports/provider.py`：`Provider` 协议（`complete`/`stream`）、请求/响应投影类型
  （`ProviderMessage`、`ModelRequest`、`ModelResponse`、`ToolDefinition`、`TokenUsage`）、
  增量类型（`ModelDelta` 各分支）、错误分类（`ProviderError`/`ProviderErrorKind`）、
  请求/响应校验与 stop reason 归一。
- `providers/fake.py`：`FakeProvider` 支持固定脚本轨迹、故障（速率限制/超时/无效响应/损坏 JSON）、
  延时、取消、usage 可缺失、脚本耗尽即失败。

明确未实现：真实 Provider（后续按需接入，不改变 Loop 调用方式）；流式聚合与分片工具参数的
运行时处理（阶段 17）；重试与退避策略（Runtime 侧，阶段 04 起）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/ports/provider.py` | `Provider`(Protocol)、`CancelSignal`(Protocol)、`ProviderMessageRole`、`ProviderToolCallPart`、`ProviderMessage`、`ToolDefinition`、`TokenUsage`、`ModelRequest`、`ModelResponse`、`TextDelta`/`ToolCallDelta`/`UsageDelta`/`StopDelta`、`ProviderErrorKind`、`ProviderError`、`normalize_stop_reason`、`validate_model_request`、`validate_model_response` | Provider 协议与共享结构；归一与兼容性校验 |
| `src/coding_agent/providers/fake.py` | `FakeProvider`、`FakeResponse`、`FakeFault`、`ScriptedToolCall`、`FakeScriptExhaustedError` | 确定性本地轨迹；故障/延时/取消 |
| `tests/unit/test_fake_provider.py` | 17 个用例 | 脚本轨迹、归一、拒绝路径、延时、取消、stream 骨架 |
| `docs/03-implementation/03-llm-provider-fake.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `docs/03-implementation/README.md` | 完成索引新增阶段 03 行 | 记录进度；无代码影响 |

## 4. 每个文件的作用

- `ports/provider.py`：输入为 Context 投影后的消息与工具声明；输出为归一后的 `ModelResponse`
  或 `ModelDelta` 流。调用方：Runtime（唯一调用者）；Metrics 从事件读 usage（阶段 16）。
  校验函数把"stop reason 与 tool calls 不兼容、ordinal 不连续、参数非 JSON、重名工具声明"
  归类为 `ProviderError`，不进入持久历史。
- `providers/fake.py`：输入为构造时给定的步骤序列；输出与真实 Provider 相同的响应/异常形态。
  调用方：测试与本地轨迹验证。`requests` 记录全部已接收请求供断言；
  `remaining_steps` 便于脚本完整性检查。
- `tests/unit/test_fake_provider.py`：直接驱动 Fake，断言响应归一、拒绝与取消语义。

## 5. 核心实现逻辑

`FakeProvider.complete` 顺序：

1. 记录请求 → `validate_model_request`（工具名非空、不重名、schema 为对象）；
2. 取下一步（耗尽 → `FakeScriptExhaustedError`，立即失败）；
3. 延时等待：每 10ms 检查一次取消信号；开始前与等待中都检查，
   取消 → `ProviderError(CANCELLED)`；
4. 故障步骤 → 按分类抛 `ProviderError(kind)`；
5. 成功步骤 → 组装 `ToolCall`（ordinal 按位置 0 起、ID 保留或生成、`arguments_json`
   解析失败/非对象 → `INVALID_RESPONSE`）→ `validate_model_response`
   （stop reason 归一、tool calls 与 stop reason 兼容、ordinal 连续、ID 唯一、参数可 JSON 化）。

`FakeProvider.stream`：与 complete 相同的脚本消费与验证，产出
`TextDelta*（content_chunks 或整段）→ ToolCallDelta* → UsageDelta? → StopDelta`。
本阶段仅提供协议骨架与切分能力；增量聚合、乱序/重复分片语义在阶段 17。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| （后续）Runtime | `Provider.complete` | `FakeProvider` | `ModelRequest`/`ModelResponse` |
| Fake 实现 | `providers.fake` | `ports.provider` 校验函数 | `validate_model_*` |
| Fake 实现 | `providers.fake` | `domain.messages` | `ToolCall`、`new_tool_call_id` |
| Fake 实现 | `providers.fake` | `domain.state` | `StopReason` |

不 import AgentLoop；不执行工具；不修改历史（阶段 03 无历史可改）。

## 7. 数据流变化

- 之前：无 Provider 抽象。
- 之后：消息（阶段 10 后由 Context 投影）→ `ModelRequest` → Provider →
  `ModelResponse`（完整、可验证）或 `ModelDelta` 增量（暂不消费）。
- 持久事实仍只在 `MessageLog`；Provider 投影是临时结构，不进入存储。

## 8. 设计原因与备选方案

- **校验函数集中在端口模块**：`validate_model_request/response` 是 Provider 边界契约，
  Fake 与未来真实实现共用，避免"Fake 宽松、真实严格"的行为漂移。
- **`usage=None` 而非 0**：未知用量必须显式缺失（Metrics 阶段据此记 unknown）。
- **脚本耗尽立即失败**：违反"Fake 只复现声明的轨迹"的测试必须硬失败，防止假绿。
- **`arguments_json` 脚本通道**：真实 Provider 以 JSON 分片交付参数，损坏场景只能在
  解析前模拟；提前建立该能力供阶段 17 复用。
- **新增 `INVALID_REQUEST` 分类**：请求侧（如重名工具声明）与响应侧错误分开，
  便于 Runtime 区分"我方构造错误"与"Provider 输出错误"。
- 备选：直接用 `asyncio.Event` 做取消（放弃：接口过窄，阶段 09 需要可组合的 CancelToken）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_fake_provider.py -q` | 脚本响应/请求记录、工具调用归一（ordinal/ID）、ID 生成、空响应、usage 缺失为 None、stop reason 冲突、无调用报 tool_calls、损坏 JSON、非对象 JSON、重名工具声明、故障分类传播与恢复、脚本耗尽、延时真实等待、取消（开始前/延时中）、stream 增量与 StopDelta（17 例） | 17 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–02） | 82 passed in 0.36s | 同上 |

## 10. 当前限制

- `stream()` 只是协议骨架与 Fake 的简单切分；无增量累积、无乱序/重复分片处理、无
  半截工具参数防执行（阶段 17 的验收项）。
- 无重试/退避、无 deadline 强制（`ModelRequest.timeout_seconds` 字段已预留，Runtime 尚未实现）。
- 无真实 Provider；无 API 密钥管理。
- Fake 的延时等待以 10ms 轮询取消，精度足够测试但非生产精度。

## 11. 后续依赖

- 阶段 04 Loop：以 `complete()` 获取完整响应 → 构造 `AssistantMessage` 提交 `MessageLog`
  （stop reason 兼容性已在 Provider 边界校验）；用 `FakeProvider.requests/remaining_steps`
  断言调用次数与脚本消耗。
- 阶段 09 取消：`CancelSignal` 协议由 `CancelToken` 实现；Fake 的取消检查代码不变。
- 阶段 17 流式：`ModelDelta` 类型与 Fake 的 `content_chunks/arguments_json` 直接复用。

## 12. 面试解释

Provider 层只做一件事：把"通信"与"事实"隔开。响应必须先通过端口层的归一与兼容性校验
（tool calls 与 stop reason 不兼容、参数非 JSON 等都是 ProviderError），才可能变成持久消息；
这样 Runtime 永远不需要对半可信的数据做猜测。Fake 用脚本轨迹模拟真实 API 的成功、故障、
延时与取消，让 Loop 的确定性与恢复路径可以在没有网络、没有密钥的情况下反复验证。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 替换真实 Provider 与 Fake 不改变 Loop 的调用方式：`Provider` 协议 + Fake 同签名实现 ✅
- 固定脚本轨迹、空响应、损坏 JSON、取消及 usage 缺失：对应 16 例单测 ✅

设计偏差：无契约变更。实现注记（扩展非变更）：`ProviderErrorKind` 增加 `INVALID_REQUEST`
（请求侧错误与响应侧错误分离）；Fake 的 `stream()` 为骨架实现（聚合语义属阶段 17）。
