# 阶段 08：Tool Safety / Error Recovery

## 1. 本节目标

交付工具治理的最后一块（模块 09 + 模块 10）：

- `tools/safety.py`：`SafetyPolicy`（`resolve_path`/`classify`/`authorize`）、`ResolvedPath`、
  `RiskLevel`、`SafetyDecision`（含策略版本与风险，作为授权证据）；
- `tools/recovery.py`：`ToolErrorKind` 权威目录（落在 ports/tool）、`RecoveryHint`、
  `normalize_tool_error`/`normalize_internal_error`、`to_model_observation`；
- Pipeline 接入：safety 缝注入真实策略、每次判断发 `POLICY_DECISION` 证据事件、
  归一结果携带 `retryable`/`exit_code`；Runtime/Loop 把观察格式用于下一轮模型请求。

明确未实现：交互式审批（审批项本地默认拒绝）；容器/OS 沙盒；崩溃恢复对 UNCERTAIN 的
生产者（阶段 09/20）；输出裁剪（阶段 11）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/tools/safety.py` | `SafetyPolicy`、`SafetyDecision`、`ResolvedPath`、`RiskLevel` | 路径解析、风险分类、授权与证据 |
| `src/coding_agent/tools/recovery.py` | `RecoveryHint`、`hint_for_kind`、`normalize_tool_error`、`normalize_internal_error`、`to_model_observation` | 错误归类与模型观察格式 |
| `tests/unit/test_safety.py` | 20 个用例 | 解析/分类/授权/可重现 |
| `tests/unit/test_recovery.py` | 14 个用例 | 提示表、归一、观察格式、持久化 |
| `tests/integration/test_governance.py` | 6 个用例 | 拒绝不落盘、错误可修复、完整轨迹 |
| `docs/03-implementation/08-tool-governance.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/ports/tool.py` | 新增 `ToolErrorKind` 枚举；`ToolOutcome` 增加 `retryable`/`exit_code` | 错误分类权威目录；字段向后兼容（默认 None） |
| `src/coding_agent/domain/messages.py` | `ToolResult` 增加可选 `retryable`/`exit_code`；序列化与校验同步 | 事实自包含，观察格式可用；旧数据仍可读（缺省 null），`schema_version` 保持 1 |
| `src/coding_agent/tools/pipeline.py` | 接入 recovery 归一；新增 `POLICY_DECISION` 事件；`_normalize_success` 填充 retryable/exit_code；删除旧映射表 | 归一逻辑单点化；策略证据可追溯 |
| `src/coding_agent/agent/loop.py` | 新增 `observation_formatter` 注入并用于 ToolResult 投影；提交结果透传 retryable/exit_code | 模型可见观察与应用事实分离 |
| `src/coding_agent/agent/runtime.py` | `AgentRuntime` 增加 `observation_formatter` 透传 | 组装层参数 |
| `src/coding_agent/bootstrap.py` | 默认注入 `SafetyPolicy()` 与 `to_model_observation`；新增 `safety_policy` 参数 | 生产路径默认带治理 |
| `tests/unit/test_pipeline.py` | 全链事件断言加入两条 POLICY_DECISION | 与新增证据事件对齐 |

## 4. 每个文件的作用

- `safety.py`：输入为 `ToolInvocation`（已通过 schema 校验的参数 + workspace）；
  输出为 `SafetyDecision`。调用方：Pipeline 的 safety 缝。`resolve_path` 供策略内部与
  后续模块复用；`classify` 可独立用于风险报告。
- `recovery.py`：输入为 `ToolExecutionError`/未知异常/已提交 `ToolResult`；
  输出为归一 `ToolOutcome` 或模型观察文本。调用方：Pipeline（归一）、Loop（观察）。
- `bootstrap.py`：默认装配改为"带安全策略 + 观察格式"的生产配置。

## 5. 核心实现逻辑

安全判断（Pipeline: permission → **safety** → execute 前）：

```text
read/write/edit：resolve_path 真实路径
  ├─ 越界/不可解析 → DENY（HIGH，证据含路径）
  ├─ write/edit 落在保护路径（默认 .git） → REQUIRE_APPROVAL（本地默认拒绝执行）
  └─ 其余 → ALLOW（LOW）
bash：命令文本词法扫描
  ├─ 破坏性模式（rm -r/-f、del /s/f、rmdir /s、format、mkfs、shutdown、reboot）→ HIGH
  ├─ 网络模式（curl/wget/ssh/scp/nc/ncat/telnet/ftp）→ MEDIUM
  └─ 默认 → LOW；HIGH/MEDIUM 默认 REQUIRE_APPROVAL，可用配置改 ALLOW（风险仍如实上报）
```

错误归一与观察：

```text
ToolExecutionError → status：timeout→TIMEOUT / cancelled→CANCELLED / 其余→ERROR
                    retryable、exit_code、内容（含部分输出）由 hint_for_kind 决定
未知异常 → internal_error（不猜测类型，不自动重试）
ToolResult → 观察文本：
  [tool result: <status> kind=... exit_code=... retryable=...]
  hint: <建议>            # 仅失败且存在分类
  <content>
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Pipeline | `SafetyPolicy.authorize` | — | `SafetyDecision`（decision/reason/risk/policy_version） |
| Pipeline | `normalize_*` | `ToolErrorKind` | 状态与可重试映射 |
| Loop | `to_model_observation`（经 bootstrap 注入） | `ToolResult` | 模型可见观察文本 |
| Runtime | 透传 retryable/exit_code | `ToolResult` | 持久事实扩展字段 |

## 7. 数据流变化

- 之前：permission/safety 为全放行占位；错误结果只含 status/kind/content。
- 之后：默认生产装配带 SafetyPolicy；授权证据以 POLICY_DECISION 事件落痕；
  条目化失败（kind/retryable/exit_code）随 ToolResult 持久化，并以稳定格式进入下一轮请求。
- 不变式保持：拒绝发生在执行前（受保护文件未被修改）；每个调用仍恰好一个配对结果。

## 8. 设计原因与备选方案

- **保护路径"写拒绝、读允许"**：读 `.git/HEAD` 是常见诊断操作，写 `.git/*` 则可能破坏仓库
  （单测覆盖两侧）。
- **审批项默认拒绝**：本地无交互审批者，REQUIRE_APPROVAL 在 Pipeline 映射为 DENIED 结果，
  证据链完整（decision/risk/policy_version）。
- **策略异常归一为拒绝**：安全代码自身缺陷绝不等于放行；同时发 HookError 事件。
- **retryable 语义**：仅表达"同参数直接重试可能安全有效"，不触发自动重放；
  timeout/cancelled/nonzero_exit/拒绝类一律 false（与"不确定副作用不得自动重放"一致）。
- **命令词法扫描如实声明**：只降低误用风险；shell 间接执行、TOCTOU、软链替换无法靠解析消除。
- 备选：UNCERTAIN 在超时时立即使用（放弃：超时已知进程被终止，不确定的是副作用是否落盘，
  该场景属于恢复协同时的判定，分类与提示已就位，生产者在阶段 09/20 接入）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_safety.py` | 解析（内/`..`/绝对/软链/非法）、分类（LOW/MEDIUM/HIGH、保护路径读写差异）、授权（允许+证据字段、逃逸 DENY、保护写审批、破坏性/网络默认审批与配置放行、可重现）（20 例） | 20 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/unit/test_recovery.py` | 提示表全覆盖、四类归一、内部异常、观察格式（完成/错误/拒绝/回退）、retryable/exit_code 序列化往返（14 例） | 14 passed | 同上 |
| `python -m pytest tests/integration/test_governance.py` | 破坏性命令拒绝且文件未改、保护路径写拒绝未改、非零退出观察→修复、路径逃逸拒绝、读-改-测完整轨迹、策略配置放行（6 例） | 6 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01–07） | 227 passed in 6.40s | 同上 |

## 10. 当前限制

- 命令文本匹配不是沙盒；不能拦截 shell 间接执行与 workspace 外访问（如实声明）。
- 无交互式审批；无 OS/容器级隔离；保护路径仅按首层目录名匹配（不递归语义复杂化）。
- UNCERTAIN_EFFECT 分类与提示已定义，生产者随阶段 09（取消核对）/20（崩溃恢复）接入。
- 观察格式未附工具名（ToolResult 不含 name；阶段 10 的 Context 可用 `tool_call_id` 补全）。
- `retryable` 逐 kind 配置为启发式，非评测结论。

## 11. 后续依赖

- 阶段 09：abort/steering 与 Pipeline 的 cancel 协作；取消时的 UNCERTAIN 判定。
- 阶段 10：Context 用 `to_model_observation` 生成工具消息（含补充工具名）。
- 阶段 11：`OutputProcessor` 落实裁剪与 artifact；`truncated` 标记随 artifact_ref 成型。
- 阶段 16：Metrics 消费 `retryable`/error_kind 计数。

## 12. 面试解释

治理阶段的原则是"默认保守、证据先行"：写保护路径和危险命令默认拿到的是带原因、风险等级和
策略版本的拒绝，而不是静默放行；策略自身崩溃也会被归为拒绝并留下 HookError 事件——安全
组件永不以失败方式放行。错误恢复上，我把"能不能重试"从系统行为降级为给模型的建议：
retryable 只是提示，真正的重试由模型决定，因为超时或非零退出的副作用状态无法自动判定。
所有错误观察都有稳定的头部（status/kind/exit_code/retryable）和恢复建议，让 Fake 轨迹可以
断言"模型看到错误后换用安全工具完成任务"。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- `../`、绝对路径、软链逃逸、删除与危险命令、默认拒绝：`test_safety.py` 全覆盖 ✅
- 受保护路径在测试中保持未修改：`test_destructive_command_denied_and_files_untouched`、
  `test_protected_path_write_denied_without_modification` ✅
- 风险决策可重现且可追溯：`test_decision_is_reproducible` + POLICY_DECISION 事件
  （decision/risk/policy=safety-1）✅
- 错误变结果（文件不存在/退出码/超时/拒绝/内部异常、无盲目重放）：`test_recovery.py` +
  `test_governance.py` ✅
- Fake 收到错误 ToolResult 后选择其他工具，run 保持一致：`test_nonzero_exit_observed_then_repaired`、
  `test_destructive_command_denied_and_files_untouched` ✅

设计偏差：无契约变更。实现注记：`ToolResult` 增加可选字段 `retryable`/`exit_code`
（`schema_version` 仍为 1，缺省 null，向后兼容）；`ToolErrorKind` 收敛落位 ports/tool
（原 docstring 声明的字符串目录）；Pipeline 新增 `POLICY_DECISION` 事件。
