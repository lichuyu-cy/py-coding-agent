"""SWE-bench 评测驱动 CLI（阶段 23）。

用法（真实评测；需要 API key、Docker 与预算，见 docs/05-benchmark/final-results.md）：
    python scripts/run_swebench_eval.py \\
        --instances benchmark_inputs/lite_subset.jsonl \\
        --output-dir benchmark_runs/run-001 \\
        --model-name <frozen-model-id> \\
        --dataset-name princeton-nlp/SWE-bench_Lite --split test \\
        --git-sha <frozen-harness-sha>

离线演练（本地仓库镜像 + dry-run 或注入的 Provider，不触网）：
    python scripts/run_swebench_eval.py --instances fake.jsonl --output-dir out \\
        --model-name fake --repo-root /path/to/local/repos --dry-run

本脚本只生成预测与清单；官方评测（Docker + Harness）命令见计划文档，
不在此脚本中执行。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许在未安装包的源码检出中直接运行
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from coding_agent.benchmark.eval_runner import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
