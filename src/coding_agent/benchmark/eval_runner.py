"""SWE-bench 评测驱动（阶段 23）：实例清单加载、冻结信息、运行与产物落盘。

- `load_instances`：读官方实例 JSONL 的运行所需字段；其余字段（含测试期答案）
  一律不读取、不进入 `BenchInstance`；
- `freeze_info`：运行前冻结记录（Python/平台/关键依赖版本/harness SHA/时间）；
- `run_evaluation`：Provider 工厂注入（真实评测传 OpenAI 兼容实现；演练传 Fake），
  产物：`freeze.json`、`manifest.json`、`predictions.jsonl`（+ workspaces/）；
- CLI 入口 `main` 由 `scripts/run_swebench_eval.py` 调用。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_agent.agent.runtime import AgentRuntime
from coding_agent.benchmark.swebench import BenchInstance, BenchRunManifest, SWEBenchAdapter
from coding_agent.bootstrap import build_runtime
from coding_agent.ports.provider import Provider

__all__ = [
    "EvalConfig",
    "EvalReport",
    "freeze_info",
    "load_instances",
    "main",
    "run_evaluation",
]

_REQUIRED_FIELDS = ("instance_id", "problem_statement", "repo", "base_commit")
_TRACKED_PACKAGES = ("httpx", "jsonschema", "pydantic", "starlette", "pytest")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class EvalConfig:
    instances_path: Path
    output_dir: Path
    model_name: str
    dataset_name: str = ""
    split: str = ""
    limit: int | None = None
    timeout_seconds: float = 1800.0
    harness_git_sha: str = ""
    repo_root: Path | None = None  # 离线演练/镜像：repo 标识映射到本地目录
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class EvalReport:
    freeze_path: Path
    manifest_path: Path | None
    predictions_path: Path | None
    summary: Mapping[str, Any]


def load_instances(path: Path, *, limit: int | None = None) -> tuple[BenchInstance, ...]:
    """逐行读取实例清单；缺字段显式失败，其余字段被忽略（不读取测试期答案）。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"instances file not found: {path}")
    instances: list[BenchInstance] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_number} is not valid JSON: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError(f"line {line_number} must be a JSON object")
        missing = [field for field in _REQUIRED_FIELDS if not raw.get(field)]
        if missing:
            raise ValueError(f"line {line_number} is missing required fields: {missing}")
        instances.append(
            BenchInstance(
                instance_id=str(raw["instance_id"]),
                problem_statement=str(raw["problem_statement"]),
                repo=str(raw["repo"]),
                base_commit=str(raw["base_commit"]),
            )
        )
        if limit is not None and len(instances) >= limit:
            break
    if not instances:
        raise ValueError(f"no instances loaded from {path}")
    return tuple(instances)


def freeze_info(*, harness_git_sha: str) -> dict[str, Any]:
    """运行前冻结信息；正式评测的配置与依赖版本必须可复核。"""
    packages: dict[str, str | None] = {}
    for name in _TRACKED_PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - 环境缺包
            packages[name] = None
    return {
        "created_at": _utc_now(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "harness_git_sha": harness_git_sha or "unknown",
        "packages": packages,
    }


def detect_git_sha(start: Path | None = None) -> str:
    """探测 harness 仓库 HEAD（失败返回 unknown，不阻塞演练）。"""
    root = start or Path(__file__).resolve().parents[3]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 无 git 环境
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _source_for_factory(config: EvalConfig) -> Callable[[BenchInstance], str]:
    if config.repo_root is None:
        return lambda instance: f"https://github.com/{instance.repo}.git"
    root = Path(config.repo_root)
    return lambda instance: str(root / instance.repo.split("/")[-1])


def _result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "instance_id": result.instance_id,
        "status": result.status.value,
        "run_id": result.run_id,
        "workspace": result.workspace,
        "duration_seconds": round(result.duration_seconds, 3),
        "error": result.error,
        "patch_chars": len(result.patch),
    }


def write_outputs(
    config: EvalConfig,
    *,
    freeze: Mapping[str, Any],
    manifest: BenchRunManifest | None = None,
    predictions_path: Path | None = None,
) -> EvalReport:
    """落盘冻结信息与运行清单；predictions.jsonl 由适配器写出后此处只做引用。"""
    config.output_dir.mkdir(parents=True, exist_ok=True)
    freeze_path = config.output_dir / "freeze.json"
    freeze_path.write_text(
        json.dumps(dict(freeze), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path: Path | None = None
    summary: dict[str, Any] = {}
    if manifest is not None:
        summary = dict(manifest.summary())
        payload = {
            "summary": summary,
            "results": [_result_to_dict(result) for result in manifest.results],
            "predictions_file": str(predictions_path) if predictions_path else None,
        }
        manifest_path = config.output_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return EvalReport(
        freeze_path=freeze_path,
        manifest_path=manifest_path,
        predictions_path=predictions_path,
        summary=summary,
    )


async def run_evaluation(
    config: EvalConfig,
    *,
    provider_factory: Callable[[EvalConfig], Provider],
    runtime: AgentRuntime | None = None,
) -> EvalReport:
    """加载实例 → 冻结 → （dry_run 则停）→ 适配器运行 → 导出预测与清单。"""
    instances = load_instances(config.instances_path, limit=config.limit)
    freeze = freeze_info(harness_git_sha=config.harness_git_sha or detect_git_sha())
    freeze = {
        **freeze,
        "instances_file": str(config.instances_path),
        "instance_ids": [instance.instance_id for instance in instances],
        "model_name": config.model_name,
        "dataset_name": config.dataset_name,
        "split": config.split,
        "timeout_seconds": config.timeout_seconds,
        "repo_root": str(config.repo_root) if config.repo_root else None,
    }
    if config.dry_run:
        return write_outputs(config, freeze=freeze)

    active_runtime = runtime or build_runtime(provider=provider_factory(config))
    adapter = SWEBenchAdapter(
        runtime=active_runtime,
        model_name=config.model_name,
        workspace_root=config.output_dir / "workspaces",
        source_for=_source_for_factory(config),
        instance_timeout_seconds=config.timeout_seconds,
        dataset_name=config.dataset_name,
        split=config.split,
        harness_git_sha=config.harness_git_sha,
    )
    manifest = await adapter.run(instances)
    predictions_path = adapter.export_predictions(
        manifest.results, config.output_dir / "predictions.jsonl"
    )
    return write_outputs(
        config, freeze=freeze, manifest=manifest, predictions_path=predictions_path
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SWE-bench evaluation driver: instances -> predictions + manifest"
    )
    parser.add_argument("--instances", required=True, type=Path, help="instance JSONL file")
    parser.add_argument("--output-dir", required=True, type=Path, help="artifacts directory")
    parser.add_argument("--model-name", required=True, help="frozen model identifier")
    parser.add_argument("--base-url", default="https://api.openai.com/v1")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--dataset-name", default="")
    parser.add_argument("--split", default="")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--git-sha", default="")
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _real_provider_factory(config: EvalConfig) -> Provider:
    import os

    from coding_agent.providers.openai_compat import OpenAICompatibleProvider

    api_key = os.environ.get(config.api_key_env, "")
    if not api_key:
        raise SystemExit(
            f"environment variable {config.api_key_env} is required for real evaluation"
        )
    return OpenAICompatibleProvider(
        base_url=config.base_url, api_key=api_key, model=config.model_name
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = EvalConfig(
        instances_path=args.instances,
        output_dir=args.output_dir,
        model_name=args.model_name,
        dataset_name=args.dataset_name,
        split=args.split,
        limit=args.limit,
        timeout_seconds=args.timeout_seconds,
        harness_git_sha=args.git_sha,
        repo_root=args.repo_root,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        dry_run=args.dry_run,
    )
    report = asyncio.run(run_evaluation(config, provider_factory=_real_provider_factory))
    print(f"freeze: {report.freeze_path}")
    if report.predictions_path is not None:
        print(f"predictions: {report.predictions_path}")
    if report.manifest_path is not None:
        print(f"manifest: {report.manifest_path}")
    if report.summary:
        print(json.dumps(report.summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - 由 scripts/ 入口调用
    sys.exit(main())
