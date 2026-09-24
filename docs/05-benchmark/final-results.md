# SWE-bench 最终评测结果（计划状态：尚未实际运行）

状态：**NO RUN RECORDED（未实际运行）**。本文件按设计文档 `swebench-plan.md` 的要求，
在真实评测执行后填写；当前环境缺少执行正式评测的前置条件（真实模型 API key、
Docker 与预算），因此如实保持计划状态，不生成任何虚假数字。

本地验收（阶段 21）的 431/461 passed 是 Fake Provider 的编排正确性证据，
**不代表**本表任何数值（设计明确区分：单测/Fake E2E 不能推出 resolved 率）。

## 1. 执行前置条件（全部满足后才能运行正式评测）

- [ ] 固定实例子集清单（`benchmark_inputs/<subset>.jsonl`，字段：
      `instance_id/problem_statement/repo/base_commit`）
- [ ] 冻结 harness Git SHA（`python scripts/run_swebench_eval.py --git-sha <sha>`）
- [ ] 冻结模型版本与解码配置（provider/model id 写入 `freeze.json`）
- [ ] 冻结依赖锁与环境（`freeze.json` 的 `packages` 字段 + Python 版本）
- [ ] `base_url` 与 API key（环境变量，不写入仓库）
- [ ] Docker 与官方 Harness（`swebench`）就绪，镜像拉取在预算内
- [ ] 预算审批（每实例调用次数/token/时长上限已固定）

## 2. 运行步骤（冻结后一次执行）

```bash
# 步骤 1：生成预测（本仓库脚本；只调用公共 run(task, workspace)）
python scripts/run_swebench_eval.py \
    --instances benchmark_inputs/<subset>.jsonl \
    --output-dir benchmark_runs/<run-id> \
    --model-name <frozen-model-id> \
    --dataset-name princeton-nlp/SWE-bench_Lite --split test \
    --git-sha <frozen-harness-sha>

# 产物：freeze.json / manifest.json / predictions.jsonl / workspaces/

# 步骤 2：官方 Harness 评测（容器执行；命令为规划示例，
# 阶段 23 实际运行时以锁定版本的 --help 核对参数）
python -m swebench.harness.run_evaluation \
    --dataset_name princeton-nlp/SWE-bench_Lite \
    --predictions_path benchmark_runs/<run-id>/predictions.jsonl \
    --max_workers <fixed> --run_id <run-id>
```

约束（swebench-plan.md）：

- 固定实例清单与并发数在评测前选定；正式评测只跑一次；
- 如因基础设施故障重评，保留原因与新 run_id，不隐瞒额外尝试；
- 预测文件与 patch 可供回看；不含密钥的 trace 才可公布。

## 3. 结果（待填模板）

| 字段 | 值 |
| --- | --- |
| 日期 | —（未运行） |
| 数据集版本与 split | —（未运行） |
| 固定实例清单（subset） | —（未运行） |
| 总实例数 | — |
| 提交预测数 | — |
| 空 patch 数 | — |
| 成功完成评测数 | — |
| resolved 数 | — |
| 未解决数 | — |
| 基础设施失败数 | — |
| 分母定义 | — |
| resolved rate | — |
| 总费用 / 模型 token | — |
| 运行配置（并发/超时/上限） | — |
| Git SHA（harness 冻结） | — |
| 评测命令 | — |
| 输出目录 | — |
| 失败样例（instance_id + 原因） | — |

分母定义（填写时必须明示）：resolved rate = resolved 数 ÷（总实例数 − 基础设施失败数），
并按「提交预测数」给出另一组比值；空 patch 计入未解决，不隐去。

## 4. 已完成的可复核准备（本仓库）

| 准备项 | 位置 | 验证 |
| --- | --- | --- |
| 适配器（准备/运行/patch/JSONL） | `src/coding_agent/benchmark/swebench.py` | `tests/integration/test_swebench_adapter.py`（8 passed，虚构实例） |
| 评测驱动（冻结/清单/产物） | `src/coding_agent/benchmark/eval_runner.py`、`scripts/run_swebench_eval.py` | `tests/integration/test_swebench_eval_driver.py`（5 passed，离线演练） |
| 真实模型 Provider（OpenAI 兼容） | `src/coding_agent/providers/openai_compat.py` | `tests/unit/test_openai_compat.py`（17 passed，MockTransport） |
| 冻结信息生成 | `eval_runner.freeze_info` / `detect_git_sha` | 演练产出 `freeze.json` |

来源（规划参考）：

- 官方评测指南：https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md
- 官方 Harness 参考：https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/harness.md
