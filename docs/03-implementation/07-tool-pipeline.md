# 阶段 07：Tool Calling Pipeline

## 1. 本节目标

交付所有工具调用统一的检查、执行与结果归一管线（模块 08）：

- 固定生命周期：解析 → schema 校验 → 参数预处理 → before → permission → safety →
  execute（timeout 包裹）→ 异常归一 → 输出处理 → after → 恰好一个 `ToolOutcome`；
- `ToolInvocation` / `ValidationOutcome` / `PermissionDecision` / hooks / 最小事件通知；
- 生产接线：Bootstrap 组装（注册表 → Pipeline → Runtime），替换阶段 04 的极简执行口。

明确未实现：真实权限/安全规则（阶段 08 注入 SafetyPolicy）；输出裁剪与 artifact（阶段 11，
`OutputProcessor` 接口已就位）；typed EventBus（阶段 15）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/tools/pipeline.py` | `ToolPipeline`、`ToolInvocation`、`ValidationOutcome`、`BeforeHookResult`、`PermissionDecision`、`PermissionResult`、`PermissionPolicy`/`SafetyPolicy`/`ToolPipelineHooks`/`OutputProcessor`(Protocol)、`AllowAllPolicy`、`PipelineEvent`/`PipelineEventKind`/`PipelineObserver` | 调用生命周期、检查点、归一与通知 |
| `src/coding_agent/bootstrap.py` | `build_registry`、`build_tool_pipeline`、`build_runtime`、`CODING_TOOL_NAMES` | 单一组装入口 |
| `tests/unit/test_pipeline.py` | 22 个用例 | spy 顺序、各检查点注入、归一、至多一次 |
| `tests/integration/test_pipeline_with_tools.py` | 7 个用例 | 真实工具经管线驱动完整 run |
| `docs/03-implementation/07-tool-pipeline.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/ports/tool.py` | 新增 `ToolOutcome`（归一结果载荷） | Pipeline 与 Runtime 的共享结构；无破坏性变更 |
| `src/coding_agent/agent/loop.py` | `ToolOutcome` 改为从 ports 重导出；执行口增加 `deadline` 参数并传当前剩余时间 | 执行口协议演进；阶段 04/07 测试同步更新 |
| `src/coding_agent/tools/bash.py` | `_watch_and_collect` 增加 `asyncio.CancelledError` 处理（先终止进程树再传播） | Pipeline deadline 包裹不再有悬挂进程风险 |
| `tests/integration/test_minimal_loop.py` | FakeExecutor 增加 `deadline=None` 形参 | 与执行口协议同步 |
| `pyproject.toml` | dependencies 增加 `jsonschema>=4.18` | 运行时 schema 校验（设计允许的基础设施角色） |
| `docs/03-implementation/README.md` | 完成索引新增阶段 07 行 | 记录进度 |

## 4. 每个文件的作用

- `pipeline.py`：输入为完整 `ToolCall`、`ToolContext` 与注册表快照；输出为恰好一个
  `ToolOutcome`。调用方：Runtime（经 `execute` 兼容入口）与需要显式快照的调用方（`invoke`）。
  所有检查点失败都构造可审计结果；执行至多一次。
- `bootstrap.py`：输入为 Provider 与运行配置；输出为组装好的 `AgentRuntime`。
  调用方：CLI / Server / Benchmark（阶段 18/22）。
- 测试：单元 spy 覆盖顺序与注入；集成用真实 Read/Write/Bash 走完整 run。

## 5. 核心实现逻辑

```text
invoke(call, ctx, snapshot?):
 ① 名称规范化 + 快照查找 ├ 失败 → ERROR(unknown_tool)（无 invocation、不调 after）
 ② schema 校验(jsonschema) + 补默认值 ├ 失败 → ERROR(invalid_arguments)
 ③ before hook ├ DENY → DENIED(hook_denied)；异常 → HookError 事件 + DENIED
 ④ permission.authorize ├ 非 ALLOW → DENIED(permission_denied / permission_approval_required)
        └ 策略异常 → HookError 事件 + DENIED（永不静默放行）
 ⑤ safety.authorize（同 ④，缺省 AllowAllPolicy）
 ⑥ emit TOOL_CALL_START → execute（timeout=min(配置, ctx.deadline)，wait_for 包裹）
        ├ ToolExecutionError → 按 kind 归一（timeout→TIMEOUT，cancelled→CANCELLED，其余 ERROR）
        ├ 普通异常 → ERROR(internal_error)；CancelledError 向上传播
        └ exit_code≠0 → ERROR(nonzero_exit)
 ⑦ output_processor（阶段 11 裁剪）→ ⑧ after hook（失败仅记 HookError，不重跑）
 ⑨ emit TOOL_CALL_END（COMPLETED）或 TOOL_CALL_ERROR（其余）
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Runtime/Loop | `ToolPipeline.execute` | `RegistrySnapshot.get` | `RegisteredTool.tool` |
| Pipeline | `invoke` | 四个编码工具 | `ToolExecution`/`ToolExecutionError` |
| Pipeline | `invoke` | 快照声明 | `ToolSpec.json_schema`（校验） |
| Bootstrap | `build_runtime` | Registry/Pipeline/Runtime | `ToolDefinition` 声明与执行口 |

## 7. 数据流变化

- 之前：Runtime 经极简执行口直接调用注入的执行器（生产路径未接真实工具）。
- 之后：模型调用 → Pipeline（校验/策略/超时/归一）→ 工具 → `ToolOutcome` → Runtime 附着
  MessageMeta 提交为 `ToolResult`；拒绝与失败都以结构化结果进入下一轮模型请求。
- 归属澄清：`ToolOutcome` 移入 ports（Runtime 与 Pipeline 共享），Loop 重导出保持兼容。

## 8. 设计原因与备选方案

- **快照参数显式化**：`invoke(..., snapshot=)` 允许调用方锁定与本 turn 声明一致的版本；
  默认取当前快照（无热更新场景足够）。
- **拒绝也走 after 钩子（有 invocation 时）**：审计闭环；执行前拒绝（未知工具/参数非法）
  没有 invocation，仅发错误事件。
- **策略异常归一为拒绝**：安全判断的代码缺陷绝不能变成"默认放行"，且必须出现 HookError 事件。
- **timeout 用 wait_for 包裹 + 工具协同**：Bash 等自管超时的工具在 `CancelledError` 时
  先回收进程树（本轮修改），因此外层包裹不会引入悬挂进程。
- **输出处理器先于 after**：after 观察的是"最终呈现给模型的结果"，与契约
  "after 收到 outcome，但不得改写事实"一致（返回值被忽略）。
- 备选：拒绝用独立状态（放弃：`ToolResultStatus.DENIED` 语义完备）；钩子返回可改写结果
  （放弃：违反"不得改写真实执行事实"）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_pipeline.py` | 全链顺序 spy（before→permission→safety→tool→process→after）、默认值注入、未知工具、非法参数、before 否决/异常、permission 否决/审批拒绝/策略异常、safety 否决、四类错误归一（tool_error/timeout+部分输出/cancelled/nonzero）、内部异常至多一次、timeout 包裹、deadline 约束、after 异常不重跑、processor 顺序、显式快照语义、loop 兼容入口（22 例） | 22 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/integration/test_pipeline_with_tools.py` | 真实 read/write 经管线、未知工具拒绝→修复、非法参数不落盘、allowlist 声明、deadline 截断慢命令并耗尽预算、多调用配对（7 例） | 7 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01–06） | 187 passed in 6.01s | 同上 |

## 10. 当前限制

- permission/safety 默认全放行（阶段 08 注入真实策略）；无审批交互。
- 输出处理为空（阶段 11 裁剪 + artifact）；`artifact_ref` 仅取 `ToolExecution.artifacts[0]`
  （多 artifact 的完整语义在阶段 11）。
- 事件为最小通知（无 seq/订阅隔离/重放，阶段 15 并入）；`TOOL_CALL_END` 尚未与"存储确认"绑定。
- `internal_error` 未分类"永久系统损坏升级 Runtime Error"（阶段 08 完善）。
- Pipeline 每次调用重建 jsonschema validator（未做缓存），性能优化留待后续。

## 11. 后续依赖

- 阶段 08：`SafetyPolicy` 实现注入 pipeline 的 safety 缝；`ToolErrorKind` 收敛错误分类；
  `to_model_observation` 决定错误结果进入模型的呈现格式。
- 阶段 11：`OutputProcessor` 由真实裁剪实现。
- 阶段 15：`PipelineEvent` 并入 typed EventBus（START/END/ERROR/HOOK_ERROR 语义保留）。
- 阶段 18/22：入口仅使用 `bootstrap.build_runtime`。

## 12. 面试解释

管线的价值在于"任何模型提出的调用，无论对错，都收敛成恰好一个可审计结果"。我用固定顺序的
检查点实现这一点：schema 先于一切（参数不可信），before 钩子能否决但不能跳过权限/安全，
策略自身出错会变成拒绝而不是放行，执行被 timeout 包裹且只发生一次，after 即使失败也只记录
不重放。与 Bash 的配合尤其关键：外层 deadline 取消工具协程时，Bash 会先杀进程树再传播取消，
所以"超时"在系统里不会以悬挂进程的形式留下痕迹。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 用 spy 断言步骤顺序：`test_full_pipeline_order` ✅
- 分别注入每一检查点错误：未知工具/参数/钩子/权限/安全/执行/超时各有用例 ✅
- 确保执行至多一次：internal/after-异常/超时用例断言工具调用计数为 1 ✅
- 未知工具、恶意参数、拒绝、超时都不会绕过统一结果关联：集成测试中全部进入 MessageLog
  且与 `tool_call_id` 配对 ✅

设计偏差：无契约变更。实现注记：`invoke` 增加可选 `snapshot` 参数（锁定注册表版本）；
`PermissionResult` 暂含 decision/reason（策略版本字段阶段 08 追加）；新增
`bootstrap.py`（设计结构中的最终组装入口，提前到本阶段落地是因为生产接线需要它）。
