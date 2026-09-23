# 阶段 10：Context Manager

## 1. 本节目标

交付上下文管理器（模块 14），替换阶段 04 的最小请求组装：

- `context/builder.py`：`ContextManager.build(log, extra_sections?)` → `ContextSnapshot`
  （messages/source_ids/estimated_tokens/sections/summary_version）；`ContextPolicy`、
  `PromptSection`、`convert_to_provider`、`ContextError`；
- `ports/tokenizer.py`：`TokenCounter` 预留预算接口 + `SimpleTokenCounter` 启发式占位；
- Loop/Runtime/Bootstrap 接线：请求构造改经 ContextManager。

明确未实现：真实 token 预算决策与输出预留（阶段 13 的 TokenManager）；压缩（阶段 14，
`summary_version` 字段已预留）；技能正文注入（阶段 12，`extra_sections` 已就位）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/ports/tokenizer.py` | `TokenCounter`(Protocol)、`SimpleTokenCounter` | token 估算的预留接口与占位实现 |
| `src/coding_agent/context/builder.py` | `ContextManager`、`ContextPolicy`、`ContextSnapshot`、`PromptSection`、`convert_to_provider`、`ContextError` | 确定性上下文构建与校验 |
| `tests/unit/test_context_manager.py` | 15 个用例 | 段落顺序、转换、校验、确定性、预算 |
| `docs/03-implementation/10-context-manager.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/agent/loop.py` | 删除内联消息投影，改由 `ContextManager` 构建请求；新增 `context_manager` 参数（缺省自建） | 行为等价迁移；旧参数保留 |
| `src/coding_agent/agent/runtime.py` | 透传 `context_manager` | 组装层参数 |
| `src/coding_agent/bootstrap.py` | 透传 `context_manager` | 组装层参数 |

## 4. 每个文件的作用

- `builder.py`：输入为 `MessageLog`（与可选 `extra_sections`）；输出为一次性
  `ContextSnapshot`（Provider 请求用）。调用方：Loop（唯一调用者）；测试可直接构建。
  不编辑原始历史、不决定模型答案。
- `tokenizer.py`：输入文本，输出估算 token 数。被 ContextManager（本阶段）与
  TokenManager（阶段 13）共用，避免各模块各算一套。
- 测试：覆盖段落顺序、工具组完整性、旧工具组转换、确定性、预算边界与来源 ID。

## 5. 核心实现逻辑

```text
build(log, extra_sections?):
 ① 校验历史：非空 / 尾部不是 assistant / 每个 call 有最终结果（否则 ContextError）
 ② 段落组装：system → project(可选) → extra_sections（顺序固定）
 ③ system 内容 = 段落以空行拼接；历史经 convert_to_provider 投影
    （ToolResult 内容经注入的 observation_formatter 渲染）
 ④ source_ids = ("section:<name>"…) + 全部消息 ID（可解释、可复现）
 ⑤ estimated_tokens = 计数器对 system+各消息（含有调用的 name/参数）求和
 ⑥ 预算：max_tokens 非空且估算超限 → ContextError（真实压缩在阶段 14）
```

确定性：不依赖时间/随机；相同历史与配置输出完全相同的快照（含 source_ids 与估算值）。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Loop | `ContextManager.build` | `MessageLog` | 只读快照 |
| Loop | 同 | `ports.tokenizer.TokenCounter` | 估算接口 |
| Loop | 同 | `tools.recovery.to_model_observation`（经 bootstrap 注入） | 工具观察渲染 |
| （阶段 12/14） | `extra_sections` | — | 技能正文 / 摘要段落 |

## 7. 数据流变化

- 之前：Loop 内联把消息逐条投影为 `ProviderMessage`（无校验、无来源、无估算）。
- 之后：请求 = ContextSnapshot（含来源 ID 与估算）；孤立/未完成工具组在构建期显式失败。
- 持久事实不变：转换结果不落盘；历史仍只在 MessageLog。

## 8. 设计原因与备选方案

- **每次 build 一次性投影**：避免把 API 转换结果当作事实缓存；保证"同一版本历史 →
  同一请求"。
- **校验前置**：不完整工具组/助手尾部在构建期拒绝，而不是交给 Provider 报错。
- **计数器注入而非硬编码**：阶段 13 可直接替换为真实 TokenManager（同一 Protocol）；
  阶段 10 的占位明确标注"仅预估"。
- **source_ids 包含 section 名与消息 ID**：请求可解释性（审计/调试/后续压缩对齐）。
- 备选：把 observation_formatter 放在 Loop（放弃：属于"呈现转换"，归 Context 更合理）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_context_manager.py` | system 首要与段落顺序、extra_sections 追加、source_ids 内容、工具组完整转换、观察格式化、纯函数性、空历史/助手尾部/未完成组/预算超限拒绝、预算内通过、两次构建相等、计数器注入生效、启发式计数（15 例） | 15 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–09；Loop 现经 ContextManager 构造请求） | 260 passed in 8.47s | 同上 |

集成回归：阶段 07/08 的"第二请求携带观察"断言在新路径下全部继续通过（格式化经 Context）。

## 10. 当前限制

- 预算只有"上限拒绝"：无输出预留、无阈值压缩触发（阶段 13/14）。
- 估算为启发式字符计数，非模型专用 tokenizer（阶段 13 预留扩展）。
- 无检索型项目上下文（设计中的扩展项，不做）。
- `full history` 始终整段注入：长历史尚未压缩（阶段 14 前明确不承诺）。

## 11. 后续依赖

- 阶段 12：技能正文以 `extra_sections=[PromptSection("skill:<name>", body)]` 注入。
- 阶段 13：TokenManager 取代 `SimpleTokenCounter`；`build` 接入 plan/decide。
- 阶段 14：压缩后以 `PromptSection("summary", …)` + 未覆盖尾部构建；填充 `summary_version`。
- 阶段 15：ContextBuild 事件（含 source_ids/估算）并入 EventBus。

## 12. 面试解释

上下文构建的核心诉求是"可解释且确定"：我让每次模型请求都从同一版本的历史生成一次性快照，
快照里既有最终消息，也有完整来源（段落名 + 每条消息 ID）和估算 token 数；不完整工具组或
助手尾部这类非法状态在构建期直接显式失败，而不是让 Provider 报晦涩错误。token 估算通过
注入的计数器接口完成，阶段 13 可以直接换成真实 TokenManager 而不动调用方——这也是"禁止各
模块各算一套 token"的落地方式。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- system 与摘要顺序：`test_system_first_and_sections_in_order`、`test_extra_sections_append_after_project` ✅
- 工具组完整性：`test_tool_group_converted_completely`、`test_incomplete_tool_group_rejected` ✅
- 消息转换与版本快照：`test_convert_to_provider_is_pure`、`test_same_input_same_snapshot`、
  `test_summary_version_reserved_as_none` ✅
- 给定相同历史与配置输出确定性请求和来源 ID：`test_source_ids_include_sections_and_message_ids`
  + 确定性用例 ✅

设计偏差：无契约变更。实现注记：`ContextSnapshot` 增加 `sections` 字段（可解释性）；
`ContextPolicy` 为轻量配置对象；`build` 的 `extra_sections` 为阶段 12/14 预留的注入点。
