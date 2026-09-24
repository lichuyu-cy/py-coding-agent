# 阶段 23：SWE-bench 最终评测（Final Evaluation）

## 1. 本节目标

交付正式评测所需的完整工具链与如实记录（模块 27 + swebench-plan）：

- 真实模型接入：`providers/openai_compat.py`（OpenAI 兼容 Chat Completions，
  响应归一后与 Fake 走同一校验）；
- 评测驱动：`benchmark/eval_runner.py` + `scripts/run_swebench_eval.py`
  （实例清单解析、冻结信息、预测与清单落盘、dry-run）；
- `docs/05-benchmark/final-results.md`：**计划状态（未实际运行）**——
  当前环境缺少真实模型 API key、Docker 与预算，按设计「未运行不生成虚假结果」
  保持模板与前置条件；本地 Fake 验收不代表任何 resolved 数值。

执行正式评测的前置条件与步骤见 `docs/05-benchmark/final-results.md`。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/providers/openai_compat.py` | `OpenAICompatibleProvider` | 真实模型接入（非流式；错误映射） |
| `src/coding_agent/benchmark/eval_runner.py` | `EvalConfig`、`EvalReport`、`load_instances`、`freeze_info`、`detect_git_sha`、`run_evaluation`、`main` | 评测驱动与产物落盘 |
| `scripts/run_swebench_eval.py` | CLI 薄壳 | 一键入口 |
| `tests/unit/test_openai_compat.py` | 17 个用例 | 请求编码/响应归一/错误映射/取消/不支持流式/真形态驱动 Runtime |
| `tests/integration/test_swebench_eval_driver.py` | 5 个用例 | 清单解析/冻结/产物/dry-run/CLI 入口 |
| `docs/05-benchmark/final-results.md` | — | 最终结果（计划状态） |
| `docs/03-implementation/23-final-evaluation.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `pyproject.toml` | `httpx>=0.27` 移入主依赖 | Provider 运行时需要；此前仅 dev 使用（服务端测试） |

## 4. 每个文件的作用

- `openai_compat.py`：messages/tools → JSON 编码（assistant tool_calls 的
  arguments 稳定序列化；tool 消息带 `tool_call_id`）；响应解析 → `ModelResponse`，
  非法结构/未知 finish_reason/非法参数 JSON 一律 `INVALID_RESPONSE`；
  HTTP 429→RATE_LIMIT、408→TIMEOUT、5xx→UNAVAILABLE、其余 4xx→INVALID_REQUEST；
  `stream()` 显式不支持（不把未实现伪装成能力）；不重试（重试策略属 Runtime）。
- `eval_runner.py`：`load_instances` 只取四个运行字段（`FAIL_TO_PASS`/`test_patch`
  等字段一律不读取）；`freeze_info` 记录 Python/平台/关键依赖/harness SHA/时间；
  `run_evaluation` 支持 Provider 工厂注入（真实评测用 OpenAI 兼容实现，
  离线演练注 Fake）与 dry-run（只落 freeze.json）；产物 `freeze.json`、
  `manifest.json`、`predictions.jsonl`（+ `workspaces/`）。
- `final-results.md`：未运行 → 保持计划状态；列出前置条件、绑定命令、待填模板
  与分母定义；已完成的准备项附可复核测试。
- 测试：Provider 用 `httpx.MockTransport` 全程离线；驱动用本地上游仓库镜像
  （`--repo-root`）与 Fake，不触网。

## 5. 核心实现逻辑

```text
真实评测链路（冻结后一次执行）：
  instances.jsonl（固定子集）
    → load_instances（忽略评测侧字段）
    → freeze.json（SHA/依赖/模型/时间）
    → SWEBenchAdapter.run（隔离 checkout → Runtime.run → patch）
    → predictions.jsonl（仅 COMPLETED/EMPTY_PATCH）
    → manifest.json（分母：总数/提交/空 patch/准备失败/超时/运行失败）
    → 官方 Harness（Docker，容器内 apply + test）

Provider 归一契约：
  HTTP JSON → ModelResponse → validate_model_response（与 Fake 相同规则）
  未知结构/未知 finish_reason/非法参数 → INVALID_RESPONSE（不猜测）
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| CLI 脚本 | `run_evaluation` | `SWEBenchAdapter`（阶段 22） | `BenchInstance`/`BenchRunManifest` |
| Runtime | `OpenAICompatibleProvider` | `Provider` 协议（阶段 03） | `ModelRequest`/`ModelResponse` |
| 评测驱动 | `build_runtime` | 阶段 04–20 全链路 | `run(task, workspace)` |
| 报告 | `final-results.md` | 官方 Harness 输出 | 分母与失败样例 |

## 7. 数据流变化

- 之前：只有 Fake Provider；真实模型无法接入，评测链路缺一环。
- 之后：真实 Provider 与 Fake 产出同一种可验证响应；评测驱动把「实例清单 →
  冻结 → 预测 → 清单」变成可重复流程；`final-results.md` 的数值只来自真实运行。

## 8. 设计原因与备选方案

- **Provider 不重试**：重试是 Runtime 的策略（阶段 04 的有限退避），Provider
  只做「一次请求 + 错误分类」，避免双层重试放大费用与副作用。
- **响应走同一校验**：`validate_model_response` 保证真实模型不会引入 Fake 没有
  的响应形态（工具调用序号/兼容性）。
- **评测侧字段不读取**：`load_instances` 只取四字段，从结构上保证不把测试期
  答案带进运行（`test_patch` 等即使存在也不进入对象）。
- **dry-run 与冻结先行**：正式评测前可先验证清单与冻结信息，避免跑到一半发现
  配置缺失。
- 备选：在 Provider 内实现流式（放弃：本期评测用非流式，流式另行实现并单独验证）；
  直接把预测发给官方 Harness（放弃：环境（Docker）不可用时不伪造执行）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_openai_compat.py` | 请求编码（消息/tool_calls/tools/max_tokens/认证头）；响应归一（tool_calls 序号、usage、length→MAX_TOKENS）；错误映射（429/5xx/4xx/超时/结构不符/未知 finish_reason/非法参数 JSON）；取消；流式显式不支持；真形态响应驱动完整 Runtime（17 例） | 17 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/integration/test_swebench_eval_driver.py` | 清单解析忽略评测侧字段/缺字段失败；离线演练产物（freeze/manifest/predictions + workspaces 隔离）；dry-run 不构造 Provider；CLI 入口 argparse 映射（5 例） | 5 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01–22） | 461 passed | 同上 |

## 10. 当前限制

- **正式评测未运行**：无真实 API key/Docker/预算；`final-results.md` 保持计划状态。
- Provider 不支持流式（`stream()` 显式报错）；多模态/推理模型特殊字段不在本期范围。
- 评测并发固定为 1；官方 Harness 的参数需在运行前用锁定版本 `--help` 核对。

## 11. 后续依赖

- 满足 `final-results.md` 的前置条件后：冻结 SHA → 生成预测 → 官方 Harness 一次
  评测 → 填写结果表（分母/resolved rate/费用/失败样例）。
- 若重评：保留原因与新 run_id（设计约束）。

## 12. 面试解释

把「评测工具链」和「评测结果」分开，是这一阶段最重要的诚实性设计。工具链可以
完全离线验证：Provider 用 MockTransport 证明请求/响应/错误映射正确；驱动用虚构
实例证明清单解析、冻结与产物落盘正确。但 resolved 率只能来自真实模型在真实
数据集上的官方 Harness 运行——没有 API key 和 Docker 就是没有，报告保持空模板，
绝不用 Fake 数字填空。任何一次正式运行都必须先冻结代码 SHA、依赖与模型版本，
保证「报告里的数字对应一个可复现的代码状态」。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本提交（工具链提交
`feat: add evaluation toolchain` 之后，记录 `docs: record final swebench run`）；
完成日期：2026-09-24。

验收条件逐项核对（dependency-graph.md 阶段 23 + swebench-plan.md）：

- 冻结版本：`freeze_info`/`detect_git_sha` → `freeze.json`（SHA/依赖/模型/时间）✅
- 固定子集：`load_instances(limit)` + `benchmark_inputs/<subset>.jsonl` 约定
  （运行前置，未运行故无实际清单）✅（机制就绪）
- 保存配置、预测、评测输出及完整分母：`EvalConfig`/`manifest.json` 分母字段
  （总数/提交/空 patch/准备失败/超时/运行失败）与 `final-results.md` 模板 ✅（机制就绪）
- 未实际运行不生成虚假结果：`final-results.md` 为计划状态 ✅
- 「不得在评测后修改冻结内容并宣称同一次评测」：本阶段提交只增加工具链与计划文档，
  正式评测时的冻结 SHA 由 `--git-sha` 固定 ✅

设计偏差：无。实现注记：阶段 23 的提交拆为两个（`feat: add evaluation toolchain`
工具链 + `docs: record final swebench run` 结果记录），原因是设计规定「正式评测后
的提交只记录评测产物」，工具链必须先行可复核；`final-results.md` 如实标注未运行。
