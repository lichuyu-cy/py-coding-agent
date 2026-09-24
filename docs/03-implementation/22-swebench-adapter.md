# 阶段 22：SWE-bench Adapter

## 1. 本节目标

把独立 Runtime 接到官方评测输入输出（模块 27）：实例准备、隔离 checkout、
任务注入、diff 采集与 JSONL 导出。

- `benchmark/swebench.py`：`BenchInstance`、`PredictionRecord`、`InstanceResult`、
  `BenchRunManifest`、`SWEBenchAdapter`（prepare / run_instance / extract_patch /
  export_predictions / run）；
- 失败独立记分：`prepare_failed` / `timeout` / `run_failed` / `empty_patch` 互不混淆；
- 隔离性：每次 prepare 使用唯一工作区；agent 运行痕迹（`.coding-agent/`）排除出 patch；
- 不暴露测试期答案：`BenchInstance` 仅含 `instance_id/problem_statement/repo/base_commit`。

明确未实现（阶段 23 才做）：真实数据集下载、真实模型评测、官方 Harness 运行。
本阶段只用虚构实例（本地上游仓库 + Fake Provider）验证解析、diff 与 JSONL 格式。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/benchmark/swebench.py` | `BenchInstance`、`PredictionRecord`、`InstanceResult`、`InstanceStatus`、`BenchRunManifest`、`SWEBenchAdapter`、`SWEBenchAdapterError` | 适配器全流程 |
| `tests/integration/test_swebench_adapter.py` | 8 个用例 | 隔离 checkout / patch 提取 / 失败分类 / JSONL 格式 |
| `docs/03-implementation/22-swebench-adapter.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/benchmark/__init__.py` | （无改动，说明该层定位） | 适配器不属于核心 Runtime 依赖 |

核心 Runtime / Loop / 工具均未修改：适配器只调用公共 `run(task, workspace)`。

## 4. 每个文件的作用

- `swebench.py`：
  - `BenchInstance`：官方数据集运行所需最小字段；不含 `test_patch`/参考 patch。
  - `SWEBenchAdapter.prepare`：`git clone`（`source_for` 解析源，默认 GitHub URL）→
    `checkout base_commit` → 写入 `.git/info/exclude` 排除 `.coding-agent/`；
    目录名含 uuid，天然幂等且不删除任何既有目录。
  - `run_instance`：`asyncio.wait_for(runtime.run(problem_statement, workspace))`；
    超时/运行异常各自归类，不向上爆炸；patch 提取失败单独归类。
  - `extract_patch`：`git add -A` + `git diff --cached <base_commit>`
    （即使 agent 自行 commit，patch 仍相对基准提交）。
  - `export_predictions`：仅导出 COMPLETED 与 EMPTY_PATCH；每行恰好
    `instance_id/model_name_or_path/model_patch` 三键。
  - `BenchRunManifest.summary`：按状态计数 + 预测分母（总实例、提交数、空 patch、
    准备失败、超时、运行失败）+ 冻结配置（数据集/split/模型/harness SHA）。
- 测试：虚构实例用本地上游 Git 仓库；`source_for` 注入本地路径，不触网。

## 5. 核心实现逻辑

```text
run(instances)：
  started_at 记录 → 逐实例 run_instance（串行，并发数固定为 1）
  run_instance：
    prepare（失败 → PREPARE_FAILED，不中断其余实例）
      clone → checkout base_commit → exclude .coding-agent/
    wait_for(runtime.run(problem_statement, workspace), timeout)
      TimeoutError → TIMEOUT（run 被取消，锁/会话由 Runtime finally 释放）
      其他异常 → RUN_FAILED（记录类型与消息）
    extract_patch（add -A + diff --cached base_commit）
      patch 非空 → COMPLETED；空 → EMPTY_PATCH
  → BenchRunManifest（含全部结果与冻结配置）
export_predictions：COMPLETED/EMPTY_PATCH → JSONL；其余显式不提交
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Adapter | `runtime.run` | 阶段 04–20 全链路 | task/workspace 公共入口 |
| Adapter | `git`（subprocess） | 临时仓库 | clone/checkout/diff |
| （阶段 23）评测脚本 | `BenchRunManifest`/`export_predictions` | 官方评测输入 | JSONL |
| （阶段 21）E2E | 同构「临时仓库 + git diff」验证模式 | — | — |

## 7. 数据流变化

- 之前：Runtime 只面向 CLI/Server 场景。
- 之后：同一 Runtime 可由适配器驱动：problem_statement 作为任务直传；
  patch 从基准提交的 diff 提取；结果分为可提交预测与失败台账两类事实。

## 8. 设计原因与备选方案

- **每次 prepare 新建唯一目录**：评测不可重复使用脏工作区；不删除旧目录避免
  误删风险（磁盘换安全，评测前统一清理）。
- **`git diff --cached <base_commit>`**：对「agent 自己 commit」也稳健；
  新文件经 `add -A` 进入索引后被包含。
- **排除 `.coding-agent/`**：运行痕迹不是解题 patch，避免污染评测输入。
- **失败分类不可合并**：设计明确「repo 准备失败/空 patch/运行超时/基础设施失败
  分别记分」；准备失败与超时不进入预测文件（不算已提交）。
- 备选：直接调用官方 harness 的 docker 代码（放弃：违反「外部依赖不进核心」且
  阶段 23 才做评测）；在 prepare 中删除旧目录（放弃：误删风险）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/integration/test_swebench_adapter.py` | 隔离到 base_commit + 唯一目录；运行痕迹不进 patch；完成修复→patch 含 diff；空 patch 标记；超时归类；prepare 失败归类；批量 manifest 计数 + JSONL 三键；失败实例不提交（8 例） | 8 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–21） | 439 passed | 同上 |

注：全部用例使用虚构实例与本地上游仓库，无网络、无真实 benchmark 数据。

## 10. 当前限制

- 串行执行（并发数 1）；真实评测的并发与 Docker 资源准备属阶段 23。
- 未实现实例清单筛选（子集选择）与费用统计脚本；阶段 23 在评测脚本中完成。
- `source_for` 默认 GitHub URL；离线环境需注入本地镜像路径。

## 11. 后续依赖

- 阶段 23：冻结 Git SHA/依赖/模型；选定固定子集；运行本适配器生成预测 →
  官方 Harness 评测一次 → `final-results.md` 记录完整分母与失败样例。

## 12. 面试解释

适配器的价值在于「把评测的每个环节都变成可审计的事实」。准备失败、超时、
空 patch、真实提交是四种不同事实，混在一起统计就会把基础设施问题算成模型失败，
把空 patch 算成已解决。所以适配器为每个实例记录独立状态、保留 workspace 与错误
原因，只在运行完成时提交预测；patch 提取用相对 base_commit 的索引 diff，
即使 agent 自己 commit 也不会丢改动；运行痕迹统一排除，保证 patch 只反映解题行为。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本提交（代码+测试+本文档同提交）；
完成日期：2026-09-24。

验收条件逐项核对（dependency-graph.md 阶段 22 + 模块 27）：

- 虚构实例验证解析：`test_prepare_isolates_to_base_commit`（repo/base_commit 语义）✅
- diff 提取：`test_completed_instance_extracts_patch`、`test_runtime_artifacts_excluded_from_patch` ✅
- JSONL 格式：`test_run_manifest_and_jsonl_export`（三键、每实例一行）✅
- 失败分类：`test_empty_patch_flagged`、`test_timeout_recorded`、
  `test_prepare_failure_recorded`、`test_failed_instances_not_submitted` ✅
- 无正式运行：全部用例为虚构实例、无网络 ✅

设计偏差：无。实现注记：`extract_patch` 使用 `diff --cached <base_commit>`
（对 agent 自行 commit 稳健）；工作区目录含 uuid 后缀，避免删除既有目录。
