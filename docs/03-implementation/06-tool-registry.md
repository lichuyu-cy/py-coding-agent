# 阶段 06：Tool Registry

## 1. 本节目标

交付工具注册表：注册、查找、声明导出与不可变快照。

- `ToolRegistry.register/unregister/get/definitions/snapshot`；
- `RegisteredTool`（名称+实现+声明）、`RegistrySnapshot(version, tools)`；
- 名称规范化（strip + `[a-z][a-z0-9_-]{0,63}`）与唯一性、schema 合法性注册时一次性校验。

明确未实现：模型的工具选择（由 Provider/模型侧决定）；调用管线与拒绝结果（阶段 07/08）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/tools/registry.py` | `ToolRegistry`、`RegisteredTool`、`RegistrySnapshot`、`normalize_tool_name`、`ToolRegistryError`、`UnknownToolError` | 注册、查找、快照与声明导出 |
| `tests/unit/test_registry.py` | 23 个用例 | 注册规则、顺序、快照版本、动态删除 |
| `docs/03-implementation/06-tool-registry.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `docs/03-implementation/README.md` | 完成索引新增阶段 06 行 | 记录进度；无代码影响 |

## 4. 每个文件的作用

- `registry.py`：输入为工具实现（`Tool` 协议）与可选 allowlist；输出为工具实例、
  稳定顺序的 `ToolDefinition` 列表与快照。调用方：Bootstrap 装配（阶段 07 起）、
  后续 Context 用 `definitions()` 生成模型声明、Pipeline 用快照做执行查找。
- `tests/unit/test_registry.py`：以 `StubTool` 覆盖注册/拒绝/快照语义，不涉及真实工具。

## 5. 核心实现逻辑

```text
register(tool):
  ① tool.spec() → 名称规范化（strip、命名规则）
  ② schema 校验：Mapping、type=="object"、JSON 可序列化；description 非空
  ③ 唯一性检查（规范化后同名视为重复）→ 注册 → version+1
snapshot(): 冻结为 (version, tuple[RegisteredTool])；definitions() 返回声明浅拷贝
definitions(allowed_names): 规范化 + 未注册名报错，按注册顺序过滤
```

版本语义：version = 成功变更次数；旧快照永不变化（新增注册/删除都不影响已取出快照），
同一 turn 的声明与执行查找都基于同一快照（阶段 07 的 Pipeline 将持有快照引用）。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| （阶段 07）Pipeline | `RegistrySnapshot.get` | 四个编码工具 | `RegisteredTool.tool` |
| （阶段 07）Context/Loop | `definitions()` | `ports.provider.ToolDefinition` | 模型可见声明 |
| 注册表 | `Tool.spec()` | `ports.tool.ToolSpec` | 声明结构 |

不执行工具；不判断权限（阶段 08）。

## 7. 数据流变化

- 之前：工具各自独立存在，无可枚举声明。
- 之后：注册表可导出稳定顺序的声明（供 Provider 请求）并支持版本化查找；
  Loop 的 `tools=` 参数可由 `snapshot.definitions()` 填充（阶段 07 接线）。

## 8. 设计原因与备选方案

- **快照而非直接迭代注册表**：冻结版本，保证"声明与执行查找同一版本"，避免运行中
  schema 变化改变执行解析（单测明确验证）。
- **注册时一次性校验 schema**：把配置错误挡在启动时；`type=="object"` 是工具参数契约。
- **allowlist 中未知名直接报错**：配置错误尽早暴露（备选：静默忽略，放弃）。
- **unregister 保留在快照中的旧引用**：动态删除不打断活动调用（结构化拒绝在 Pipeline 层）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_registry.py` | 注册/获取、strip 规范化、重复名拒绝、7 类非法名称、4 类非法 schema、空 description、未知名报错、unregister、声明顺序、allowlist 过滤与未知名拒绝、声明 schema 浅拷贝、快照版本与查找、旧快照不受新注册影响、动态删除后活动快照可用、同 turn 声明与查找同版本（23 例） | 23 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–05） | 158 passed in 3.80s | 同上 |

## 10. 当前限制

- 无热重载（动态加载只在运行间生效，接口已可支撑）；无工具“选择策略”（模型决定）。
- 快照持工具实例引用，工具实现本身的可变状态不在注册表管理范围。
- 尚无 Pipeline 接线（阶段 07）；`definitions()` 暂未被 Loop 使用。

## 11. 后续依赖

- 阶段 07：Pipeline 每个 turn 取一次快照；`invoke(call, context)` 用快照解析工具，
  未知工具与不可用工具产生结构化拒绝结果。
- 阶段 10：Context 用 `definitions()` 生成 Provider 请求中的工具声明。
- 阶段 08：SafetyPolicy 在 Pipeline 内对解析到的工具调用做授权。

## 12. 面试解释

注册表的核心是"版本化快照"：一次模型请求里声明的工具集合，与这次请求内工具回执被解析时
依赖的注册表状态，必须完全一致——否则热更新或配置变化会让模型看到 A、实际执行 B。
我把注册表做成"当前表 + 不可变快照"两段式：任何变更只影响后续快照，已取出的快照永远
稳定，包括对已删除工具的解析（活动调用不被打断）。名称和 schema 的校验全部前置到注册时，
让配置错误在启动期爆掉，而不是在模型第一次调用时。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 重名拒绝、未知名、声明次序、schema 变化不影响运行中快照：对应单测全覆盖 ✅
- 同一 turn 的 schema 声明和执行查找对应同一个注册表版本：`test_same_turn_declaration_and_lookup_share_version` ✅

设计偏差：无契约变更。实现注记：snapshot 内 `tools` 更名为 RegisteredTool 列表
（语义等价于设计中的 `RegistrySnapshot(version, specs)`，并提供 `specs()` 访问器）。
