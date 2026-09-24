# 阶段 13：Token Management

## 1. 本节目标

交付统一的 token 估算与预算决策（模块 17）：

- `context/budget.py`：`TokenBudget`、`TokenEstimate`、`BudgetDecision`（FIT/COMPACT/REJECT）、
  `TokenManager`（唯一估算入口：`estimate`/`plan`/`decide`）；
- 接线：ContextManager 预算检查改用 TokenManager（含工具声明固定成本）；Loop 将
  "上下文超出预算"落为可解释终止（`limit_hit="context_overflow"`，BUDGET_EXHAUSTED）；
- Bootstrap 默认注入 128k 窗口预算（可配置）。

明确未实现：压缩执行（阶段 14；COMPACT 决策当前显式失败）；模型专用 tokenizer（接口已留）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/context/budget.py` | `TokenBudget`、`TokenEstimate`、`BudgetDecision`、`TokenManager` | 预算模型、估算与决策 |
| `tests/unit/test_token_budget.py` | 18 个用例 | 模型/边界/估算/决策/接线 |
| `docs/03-implementation/13-token-management.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/context/builder.py` | `ContextManager` 增加 `token_manager`；`build(..., tool_definitions=())`；预算检查优先走 TokenManager（否则保留 `policy.max_tokens` 旧路径） | 统一估算；旧配置路径兼容 |
| `src/coding_agent/agent/loop.py` | 请求构建捕获 `ContextError` → `limit_hit="context_overflow"`、FINISH/BUDGET_EXHAUSTED；构建时传入工具声明 | 溢出成为可解释终止而非崩溃 |
| `src/coding_agent/bootstrap.py` | 默认构建带预算的 ContextManager；新增 `context_limit_tokens`（默认 128_000） | 生产路径默认受预算约束 |

## 4. 每个文件的作用

- `budget.py`：输入为上下文快照与工具声明；输出为估算 + 决策 + 解释文本。
  调用方：ContextManager（唯一运行时调用者）；测试可直接使用。
- `builder.py`：预算检查从"单一 max_tokens"升级为"阈值/窗口两段式"。
- `loop.py`：把预算失败转换为"哪一个限额耗尽"的可报告结果（承接阶段 04 的 limit_hit 语义）。

## 5. 核心实现逻辑

```text
预算模型：usable = limit × (1 - 输出预留 0.2) × (1 - 安全余量 0.05)
          threshold = usable × 压缩阈值比例 0.75
决策：total ≤ threshold → FIT；≤ usable → COMPACT；否则 REJECT
估算：fixed = system + 全部工具声明（name/description/schema JSON, sort_keys 确定性）
      history = 历史消息 content + 助手调用的 name/参数 JSON
接入：ContextManager.build 构造 provisional 快照 → TokenManager.estimate →
      FIT → 返回（estimated_tokens = total）；COMPACT → ContextError（阶段 14 将接压缩）；
      REJECT → ContextError；Loop 捕获 → BUDGET_EXHAUSTED(context_overflow)
```

估算误差说明：占位计数器为字符启发式；安全余量默认 5% 即为估算不可靠时的保守缓冲。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| ContextManager | `TokenManager.estimate` | `ports.tokenizer.TokenCounter` | 估算接口 |
| Loop | `ContextError` 捕获 | `RunStatus.BUDGET_EXHAUSTED` | `limit_hit` 报告 |
| Bootstrap | `TokenBudget/TokenManager` | — | `context_limit_tokens` 配置 |

## 7. 数据流变化

- 之前：超长历史只在 `policy.max_tokens`（若配置）下显式失败；默认无上限。
- 之后：每次请求构建都经统一预算评估（含工具声明固定成本），三档决策；
  生产默认 128k 窗口（可用输入 ≈ 97.3k，压缩阈值 ≈ 73k）。

## 8. 设计原因与备选方案

- **两段式决策（阈值/窗口）**：为阶段 14 的"先压缩、再判断"预留中间态；
- **工具声明计入固定成本**：真实请求的 schema 常达数百 token，不计入会低估；
- **JSON 序列化 sort_keys**：估算对同内容不同 dict 顺序保持确定性；
- **COMPACT 当前显式失败而非静默截断**：压缩未实现前绝不丢弃历史（阶段 14 替换该分支）；
- 备选：在 Provider 返回 usage 后反馈校正估算（本期不做，预留）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_token_budget.py` | 窗口/阈值计算、契约比例默认值、4 类非法参数、plan、边界（750/751/1000/1001）、决策幂等、估算确定性、固定/历史拆分、工具声明计费、固定成本超窗 REJECT、ContextManager FIT/COMPACT/REJECT、循环终止（tiny 预算 → BUDGET_EXHAUSTED 且 0 次模型请求）、默认预算不干扰（18 例） | 18 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–12） | 316 passed in 8.87s | 同上 |

## 10. 当前限制

- COMPACT 决策暂以 ContextError 终止（阶段 14 实现压缩后改为自动压缩再构造）。
- 估算为字符启发式（≈4 字符/token）；未用 Provider 实返 usage 校正。
- 单次请求预算；run 级 token 总配额（跨请求累计）未实现（契约建议项，后续可加）。

## 11. 后续依赖

- 阶段 14：ContextManager 的 COMPACT 分支接入 Compactor（cut→摘要→重建）。
- 阶段 16：Metrics 收 Provider usage 与估算误差对比。
- 阶段 17：流式终结 usage 也走同一 Metrics 路径。

## 12. 面试解释

预算的核心是"在发请求之前就知道它装不装得下"。我把窗口拆成三层：给模型输出的预留
（20%）、给估算误差的安全余量（5%），剩下的才是可用输入窗口；窗口再乘 75% 作为压缩阈值——
超过阈值不一定要拒绝，而是进入"应该压缩"的状态，这正是为下一步的原子压缩留的中间态。
估算包含工具声明这种容易漏掉的固定成本，并且用 sort_keys 保证同内容不同字典顺序得到同样的
数字；整个决策是纯函数，可以精确断言边界（750/751/1000/1001）。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-24。

验收条件逐项核对：

- 输出预留、无法适配时显式失败：`output_reserve_ratio` + REJECT 用例 + Loop 终止用例 ✅
- 固定成本超过窗口、边界值、估算误差模拟、输出预留：对应 4 类用例 ✅
- 同一输入预算决策一致：`test_estimates_are_deterministic`、`test_decision_matches_estimate` ✅
- 禁止各模块自行各算一套 token：估算入口唯一（TokenManager）；ContextManager 旧路径保留
  仅为无预算配置时的兼容开关 ✅

设计偏差：无契约变更。实现注记：`TokenBudget` 增加 `safety_margin_ratio`（估算误差缓冲，
设计只给出预留与阈值两个比例）；`build` 增加 `tool_definitions` 参数（固定成本可见性）。
