"""SWE-bench 适配器（模块 27）：实例准备、隔离 checkout、patch 提取与 JSONL 导出。

边界（严格）：
- 只调用公共 `Runtime.run(task, workspace)`，不改变 Agent Loop；
- 不向 Agent 暴露测试期答案、参考 patch 或隐藏信息：`BenchInstance` 只有
  `instance_id/problem_statement/repo/base_commit`，适配器不读取其他字段；
- repo 准备失败 / 空 patch / 超时 / 运行失败分别记分，不把未提交题目算已 resolved；
- 实例源通过 `source_for` 解析（默认 GitHub URL）；测试用本地仓库注入，不触网。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from coding_agent.agent.runtime import AgentRuntime
from coding_agent.domain.errors import HarnessError

__all__ = [
    "BenchInstance",
    "BenchRunManifest",
    "InstanceResult",
    "InstanceStatus",
    "PredictionRecord",
    "SWEBenchAdapter",
    "SWEBenchAdapterError",
]


class SWEBenchAdapterError(HarnessError):
    """适配器基础设施错误（repo 准备/checkout/patch 提取）。"""


class InstanceStatus(StrEnum):
    """单实例结果分类（分别记分，互不混淆）。"""

    COMPLETED = "completed"  # 运行完成且 patch 非空
    EMPTY_PATCH = "empty_patch"  # 运行完成但未产生修改
    PREPARE_FAILED = "prepare_failed"  # repo 准备/checkout 失败
    TIMEOUT = "timeout"  # 实例超时
    RUN_FAILED = "run_failed"  # 运行或 patch 提取失败


@dataclass(frozen=True, slots=True)
class BenchInstance:
    """官方数据集中的一条实例（仅运行所需字段；不含测试期答案）。"""

    instance_id: str
    problem_statement: str
    repo: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """官方评测输入的一条预测（JSONL 行）。"""

    instance_id: str
    model_name_or_path: str
    model_patch: str

    def to_dict(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": self.model_name_or_path,
            "model_patch": self.model_patch,
        }


@dataclass(frozen=True, slots=True)
class InstanceResult:
    """单实例运行结果（含失败分类与原因）。"""

    instance_id: str
    status: InstanceStatus
    patch: str = ""
    run_id: str | None = None
    workspace: str | None = None
    duration_seconds: float = 0.0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BenchRunManifest:
    """一次批量运行的冻结配置与结果汇总。"""

    dataset_name: str
    split: str
    instance_ids: tuple[str, ...]
    model_name: str
    harness_git_sha: str
    started_at: str
    finished_at: str
    results: tuple[InstanceResult, ...] = ()

    def summary(self) -> dict[str, Any]:
        """按状态计数 + 预测分母（供报告使用）。"""
        counts = {status.value: 0 for status in InstanceStatus}
        for result in self.results:
            counts[result.status.value] += 1
        submitted = counts[InstanceStatus.COMPLETED.value] + counts[InstanceStatus.EMPTY_PATCH.value]
        return {
            "dataset_name": self.dataset_name,
            "split": self.split,
            "model_name": self.model_name,
            "harness_git_sha": self.harness_git_sha,
            "total_instances": len(self.instance_ids),
            "results_by_status": counts,
            "submitted_predictions": submitted,
            "empty_patch": counts[InstanceStatus.EMPTY_PATCH.value],
            "prepare_failed": counts[InstanceStatus.PREPARE_FAILED.value],
            "timeout": counts[InstanceStatus.TIMEOUT.value],
            "run_failed": counts[InstanceStatus.RUN_FAILED.value],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_name(instance_id: str) -> str:
    return instance_id.replace("/", "__").replace(":", "_")


class SWEBenchAdapter:
    """实例准备 → 隔离运行 → patch 提取 → JSONL 导出；串行执行（并发数固定为 1）。"""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        model_name: str,
        workspace_root: Path,
        source_for: Callable[[BenchInstance], str] | None = None,
        instance_timeout_seconds: float = 1800.0,
        dataset_name: str = "",
        split: str = "",
        harness_git_sha: str = "",
    ) -> None:
        self._runtime = runtime
        self._model_name = model_name
        self._workspace_root = Path(workspace_root)
        self._source_for = source_for or (lambda instance: f"https://github.com/{instance.repo}.git")
        self._instance_timeout_seconds = instance_timeout_seconds
        self._dataset_name = dataset_name
        self._split = split
        self._harness_git_sha = harness_git_sha

    # ---- 基础设施 ----

    def _git(self, *args: str) -> str:
        git = shutil.which("git")
        if git is None:  # pragma: no cover - 无 git 环境的显式失败
            raise SWEBenchAdapterError("git executable is required for SWE-bench preparation")
        completed = subprocess.run(
            [git, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise SWEBenchAdapterError(
                f"git {' '.join(args)} failed with exit {completed.returncode}:"
                f" {completed.stderr.strip()}"
            )
        return completed.stdout

    def prepare(self, instance: BenchInstance) -> Path:
        """隔离 checkout 到独立工作区（幂等重建：每次使用新的唯一目录）。"""
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        destination = self._workspace_root / f"{_safe_name(instance.instance_id)}_{uuid.uuid4().hex[:8]}"
        source = self._source_for(instance)
        self._git("clone", "--quiet", source, str(destination))
        self._git("-C", str(destination), "checkout", "--quiet", instance.base_commit)
        # agent 运行痕迹（artifacts/会话数据）不进入 patch
        exclude = destination / ".git" / "info" / "exclude"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\n.coding-agent/\n")
        return destination

    def extract_patch(self, workspace: Path, base_commit: str) -> str:
        """提取 base_commit → 当前工作区的完整 patch（含新增文件；不含运行痕迹）。"""
        self._git("-C", str(workspace), "add", "-A")
        return self._git("-C", str(workspace), "diff", "--cached", base_commit)

    # ---- 单实例 ----

    async def run_instance(self, instance: BenchInstance) -> InstanceResult:
        started = time.monotonic()
        try:
            workspace = self.prepare(instance)
        except SWEBenchAdapterError as exc:
            return InstanceResult(
                instance_id=instance.instance_id,
                status=InstanceStatus.PREPARE_FAILED,
                duration_seconds=time.monotonic() - started,
                error=str(exc),
            )
        run_id: str | None = None
        try:
            outcome = await asyncio.wait_for(
                self._runtime.run(instance.problem_statement, workspace),
                timeout=self._instance_timeout_seconds,
            )
            run_id = outcome.run_id
        except TimeoutError:
            return InstanceResult(
                instance_id=instance.instance_id,
                status=InstanceStatus.TIMEOUT,
                run_id=run_id,
                workspace=str(workspace),
                duration_seconds=time.monotonic() - started,
                error=f"instance exceeded {self._instance_timeout_seconds}s budget",
            )
        except Exception as exc:  # noqa: BLE001 - 运行失败单独记分，不向上爆炸
            return InstanceResult(
                instance_id=instance.instance_id,
                status=InstanceStatus.RUN_FAILED,
                run_id=run_id,
                workspace=str(workspace),
                duration_seconds=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )
        try:
            patch = self.extract_patch(workspace, instance.base_commit)
        except SWEBenchAdapterError as exc:
            return InstanceResult(
                instance_id=instance.instance_id,
                status=InstanceStatus.RUN_FAILED,
                run_id=run_id,
                workspace=str(workspace),
                duration_seconds=time.monotonic() - started,
                error=f"patch extraction failed: {exc}",
            )
        status = InstanceStatus.COMPLETED if patch.strip() else InstanceStatus.EMPTY_PATCH
        return InstanceResult(
            instance_id=instance.instance_id,
            status=status,
            patch=patch,
            run_id=run_id,
            workspace=str(workspace),
            duration_seconds=time.monotonic() - started,
        )

    # ---- 批量与导出 ----

    async def run(self, instances: Sequence[BenchInstance]) -> BenchRunManifest:
        """串行运行固定子集；失败实例保留原因，不中断其余实例。"""
        started_at = _utc_now()
        results: list[InstanceResult] = []
        for instance in instances:
            results.append(await self.run_instance(instance))
        return BenchRunManifest(
            dataset_name=self._dataset_name,
            split=self._split,
            instance_ids=tuple(instance.instance_id for instance in instances),
            model_name=self._model_name,
            harness_git_sha=self._harness_git_sha,
            started_at=started_at,
            finished_at=_utc_now(),
            results=tuple(results),
        )

    def export_predictions(self, results: Sequence[InstanceResult], path: Path) -> Path:
        """导出官方评测 JSONL：只包含运行完成（含空 patch）的实例。

        准备失败/超时/运行失败不提交预测——它们分别记分，不冒充已解决或已提交。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for result in results:
            if result.status not in (InstanceStatus.COMPLETED, InstanceStatus.EMPTY_PATCH):
                continue
            record = PredictionRecord(
                instance_id=result.instance_id,
                model_name_or_path=self._model_name,
                model_patch=result.patch,
            )
            lines.append(json.dumps(record.to_dict(), ensure_ascii=False))
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path
