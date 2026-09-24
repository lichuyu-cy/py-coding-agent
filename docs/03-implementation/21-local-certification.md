# 阶段 21：完整本地测试（Local Certification）

## 1. 本节目标

完成「干净环境一次全量通过」的本地验收（模块 26）：

- Mini Coding Repo 端到端：临时 Git 仓库、可修复的失败测试、Fake Provider 脚本驱动
  真实 Runtime/Pipeline/工具完成「读取 → 编辑 → 运行测试 → 报告」；
- 断言 `git diff`、仓库内测试通过、失败观察进入下一请求（tool→result→repair）；
- 汇总测试策略表的全部关键路径到「验收轨迹」映射（本文档第 9 节），
  命令、环境、结果为本次真实运行记录。

明确未实现：真实模型冒烟测试（可选、需额外预算，不替代本地验收）；
真实 SWE-bench 数据（阶段 22 用虚构样例）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `tests/e2e/test_mini_repo_e2e.py` | `mini_repo` fixture、`run_git`、2 个端到端场景 | Mini Repo 修复全流程与失败恢复 |
| `docs/03-implementation/21-local-certification.md` | — | 本文档（含验收轨迹汇总） |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| （无生产代码修改） | — | 阶段 21 只新增测试与验收记录 |

## 4. 每个文件的作用

- `tests/e2e/test_mini_repo_e2e.py`：
  - `mini_repo` fixture：在 `tmp_path` 建独立仓库（`calc.py` 含 bug、`test_calc.py`
    断言 `add(2, 3) == 5`），`git init/add/commit` 用 `-c user.*` 局部身份参数
    （不改动任何 git 配置）；无 git 时显式 skip。
  - `test_read_edit_run_tests_then_report`：read×2 → edit → bash `pytest -q` → final；
    断言文件内容、`git diff` 恰一处修改、工具观察含 `1 passed`。
  - `test_failed_edit_observed_then_repaired`：edit 冲突（`replace_not_found`）→
    失败观察含结构化 kind 进入下一请求 → 二次 edit → pytest 通过。
- 测试不接触真实用户仓库；所有工作发生在临时目录。

## 5. 核心实现逻辑

```text
fixture：tmp_path/mini-repo → 写入 bug 版 calc.py + 失败测试 → git init/add/commit
场景 A：FakeProvider 脚本 5 步（read test → read impl → edit 修复 → bash pytest → final）
       断言：FINISHED / turns=5 / tool_calls=4 / 文件=FIXED_CALC /
             git diff 含 -a-b +a+b 且 1 file changed / 观察含 "1 passed"
场景 B：脚本 4 步（edit 冲突 → edit 修复 → bash pytest → final）
       断言：call_1 error_kind=replace_not_found / requests[1] 含失败观察 /
             最终文件修复 / 观察含 "1 passed"
```

最终验收命令（干净环境，无网络、无真实模型）：

```text
python -m pytest        # 全量：unit + integration + e2e
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| E2E 场景 | `build_runtime` | Provider/Fake、Pipeline、编码工具 | 阶段 03–10 |
| E2E 场景 | Runtime | bash/read/edit 工具 | 阶段 05–08 |
| E2E 场景 | 仓库断言 | git（subprocess，临时目录） | — |
| 验收轨迹 | 全部测试 | 阶段 01–20 模块 | 见第 9 节映射 |

## 7. 数据流变化

- 之前：各阶段测试独立覆盖自身契约；没有跨「真实工具 + 真实磁盘 + 真实子进程」的
  端到端证据。
- 之后：E2E 在真实 Git 仓库上验证「模型脚本 → 工具 → 磁盘事实 → 测试进程」闭环，
  `git diff` 是可审计的最终事实。

## 8. 设计原因与备选方案

- **Git 仓库而非普通目录**：`git diff` 提供修改的第三方可审计视图（谁改了哪些行），
  也是阶段 22 diff 提取的同构基础。
- **bash 里运行 pytest 而非 Python API 调用**：走完整工具 → 子进程路径，
  同时验证超时/输出治理/观察格式的真实行为。
- **两个场景（成功 + 失败恢复）**：覆盖策略表「tool→result→repair」端到端版本。
- 备选：mock 文件系统/进程（放弃：失去磁盘与子进程真实性）；
  在真实仓库上跑（放弃：不可重复且违反设计约束）。

## 9. 测试与结果（本次真实运行记录）

环境：Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1；离线（无网络调用）。

| 命令 | 场景/断言 | 结果 |
| --- | --- | --- |
| `python -m pytest tests/unit` | 单元：消息/状态/存储/管线/预算/裁剪/事件等 | 351 passed（5.46s） |
| `python -m pytest tests/integration` | 集成：Loop→Pipeline→Context→Store、SSE、崩溃恢复 | 78 passed（7.76s） |
| `python -m pytest tests/e2e` | Mini Repo 修复 + 失败恢复 | 2 passed（3.65s） |
| `python -m pytest` | 一次性全量（干净进程） | 431 passed（17.41s） |

### 验收轨迹汇总（test-strategy.md 场景 → 对应测试）

| 策略场景 | 关键断言 | 对应测试 |
| --- | --- | --- |
| final answer | 1 次 LLM、0 tool、FINISHED、消息顺序 | `tests/integration/test_minimal_loop.py` |
| tool→result→repair | 失败结果完整进入下一请求，修复后结束 | `tests/e2e/test_mini_repo_e2e.py::test_failed_edit_observed_then_repaired` |
| assistant 同时调用两个工具 | ordinal 顺序、结果配对、压缩不拆组 | `tests/unit/test_pipeline.py`、`test_compaction.py::test_groups_never_split_multi_call_assistant` |
| schema/权限/路径失败 | 工具未执行、结构化拒绝结果 | `tests/integration/test_governance.py`、`tests/unit/test_safety.py` |
| Bash 超时 | 进程终止、结果 TIMEOUT、无悬挂 | `tests/unit/test_coding_tools.py` |
| 超大输出 | 裁剪标记 + artifact 引用有效 | `tests/integration/test_output_truncation.py` |
| steering/follow-up | 前者注入下一请求、后者新 turn | `tests/integration/test_runtime_controls.py` |
| abort 与 SSE 断连 | 显式 abort 停止；断连仅取消订阅 | `test_runtime_controls.py`、`tests/integration/test_sse_server.py` |
| Provider stream 中断 | 半截 ToolCall 未进入 Pipeline | `tests/unit/test_streaming.py` |
| token 超限 | 完整组压缩；不可压缩显式终止 | `tests/unit/test_token_budget.py`、`test_compaction.py` |
| 崩溃时工具副作用未知 | RECONCILE、调用计数不增加 | `tests/integration/test_crash_recovery.py` |
| Session 与 Checkpoint 恢复 | 重启仅加载已提交事实；版本冲突停止 | `test_session_persistence.py`、`test_crash_recovery.py`、`tests/unit/test_session_store.py` |
| Event/Metrics | 事件序号递增、去重计数准确 | `tests/integration/test_event_bus_flow.py`、`test_metrics_flow.py` |

## 10. 当前限制

- E2E 使用 Fake Provider：证明编排正确性，不代表真实模型解题能力（设计明确区分）。
- Mini Repo 为单文件 Python 项目；多语言/大型仓库场景不在本期验收范围。
- 未包含真实网络/真实模型冒烟（可选扩展，须单独计预算）。

## 11. 后续依赖

- 阶段 22：SWE-bench 适配器复用本阶段的「临时仓库 + git diff 提取」模式，
  用虚构实例验证解析与 patch 格式。
- 阶段 23：最终评测在冻结版本上进行，本阶段的全量结果作为冻结前基线。

## 12. 面试解释

本地验收的价值在于「用真实磁盘、真实子进程、真实 Git 仓库证明编排闭环」，
而不是再写一遍单元测试。Mini Repo 场景把模型脚本换成 Fake，但工具、文件系统、
pytest 子进程、git diff 全部真实：修好了就是修好了，`git diff` 与「1 passed」
是第三方可复核的硬事实。第二个场景专门验证失败恢复——编辑冲突作为结构化观察
进入下一请求，模型据此第二次修复成功，这正是真实 agent 最常走的路径。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本提交（测试+本文档同提交）；
完成日期：2026-09-24。

验收条件逐项核对（dependency-graph.md 阶段 21）：

- 全部关键路径通过：`python -m pytest` → 431 passed（第 9 节）✅
- 生成验收记录：本文档第 9 节（命令/环境/结果）+ 验收轨迹汇总 ✅
- 干净环境 Mini Repo E2E：`tests/e2e/test_mini_repo_e2e.py`（临时目录，git skip 保护）✅

设计偏差：无（未修改生产代码）。实现注记：E2E 的 git 身份通过 `-c user.*`
命令行参数提供，不写入任何 git 配置文件。
