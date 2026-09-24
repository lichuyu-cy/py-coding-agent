"""阶段 22 集成测试：SWE-bench 适配器（模块 27）。

仅使用虚构实例（本地上游仓库 + Fake Provider）：验证解析、隔离 checkout、
diff 提取与 JSONL 格式；不下载/执行真实 benchmark、不触网。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent.benchmark.swebench import (
    BenchInstance,
    InstanceStatus,
    SWEBenchAdapter,
)
from coding_agent.bootstrap import build_runtime
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall

GIT = shutil.which("git")

BUGGY_CALC = '''"""Simple calculator module."""


def add(a, b):
    return a - b
'''

FIXED_CALC = '''"""Simple calculator module."""


def add(a, b):
    return a + b
'''


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


@pytest.fixture()
def upstream(tmp_path: Path) -> Path:
    """本地「上游」仓库：模拟 SWE-bench 的 repo + base_commit（虚构实例）。"""
    if GIT is None:
        pytest.skip("git is required for the SWE-bench adapter tests")
    repo = tmp_path / "upstream"
    repo.mkdir()
    (repo / "calc.py").write_text(BUGGY_CALC, encoding="utf-8")
    run_git(repo, "init", "-q")
    run_git(repo, "add", ".")
    run_git(
        repo,
        "-c",
        "user.name=harness-bench",
        "-c",
        "user.email=bench@example.invalid",
        "commit",
        "-q",
        "-m",
        "base",
    )
    return repo


def make_instance(upstream: Path, instance_id: str = "fake__fake-0001") -> BenchInstance:
    base_commit = run_git(upstream, "rev-parse", "HEAD").strip()
    return BenchInstance(
        instance_id=instance_id,
        problem_statement="The calculator add() returns the difference; make it return the sum.",
        repo="fake/fake",
        base_commit=base_commit,
    )


def make_adapter(upstream: Path, tmp_path: Path, provider: FakeProvider, **kwargs) -> SWEBenchAdapter:
    runtime = build_runtime(provider=provider)
    return SWEBenchAdapter(
        runtime=runtime,
        model_name="fake-model",
        workspace_root=tmp_path / "bench-workspaces",
        source_for=lambda instance: str(upstream),
        dataset_name="fake-dataset",
        split="test",
        harness_git_sha="deadbeef",
        **kwargs,
    )


class TestPrepare:
    def test_prepare_isolates_to_base_commit(self, upstream: Path, tmp_path: Path) -> None:
        adapter = make_adapter(upstream, tmp_path, FakeProvider([]))
        instance = make_instance(upstream)

        workspace = adapter.prepare(instance)
        assert workspace.parent == tmp_path / "bench-workspaces"
        assert run_git(workspace, "rev-parse", "HEAD").strip() == instance.base_commit
        assert (workspace / "calc.py").read_text(encoding="utf-8") == BUGGY_CALC

        # 每次 prepare 使用新的隔离目录（幂等重建，不删除既有工作区）
        second = adapter.prepare(instance)
        assert second != workspace

    def test_runtime_artifacts_excluded_from_patch(self, upstream: Path, tmp_path: Path) -> None:
        adapter = make_adapter(upstream, tmp_path, FakeProvider([]))
        instance = make_instance(upstream)
        workspace = adapter.prepare(instance)

        artifacts = workspace / ".coding-agent" / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "note.txt").write_text("runtime trace", encoding="utf-8")
        (workspace / "calc.py").write_text(FIXED_CALC, encoding="utf-8")

        patch = adapter.extract_patch(workspace, instance.base_commit)
        assert "+    return a + b" in patch
        assert ".coding-agent" not in patch


class TestRunInstance:
    async def test_completed_instance_extracts_patch(self, upstream: Path, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="edit",
                            arguments={
                                "path": "calc.py",
                                "expected_old": "return a - b",
                                "replacement": "return a + b",
                            },
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(content="fixed", stop_reason=StopReason.END_TURN),
            ]
        )
        adapter = make_adapter(upstream, tmp_path, provider)
        instance = make_instance(upstream)

        result = await adapter.run_instance(instance)
        assert result.status is InstanceStatus.COMPLETED
        assert result.run_id
        assert "-    return a - b" in result.patch
        assert "+    return a + b" in result.patch
        assert result.duration_seconds >= 0
        assert result.error is None

    async def test_empty_patch_flagged(self, upstream: Path, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="nothing to do", stop_reason=StopReason.END_TURN)])
        adapter = make_adapter(upstream, tmp_path, provider)
        result = await adapter.run_instance(make_instance(upstream))
        assert result.status is InstanceStatus.EMPTY_PATCH
        assert result.patch == ""
        assert result.error is None

    async def test_timeout_recorded(self, upstream: Path, tmp_path: Path) -> None:
        provider = FakeProvider(
            [FakeResponse(content="slow", stop_reason=StopReason.END_TURN, delay_seconds=0.5)]
        )
        adapter = make_adapter(upstream, tmp_path, provider, instance_timeout_seconds=0.05)
        result = await adapter.run_instance(make_instance(upstream))
        assert result.status is InstanceStatus.TIMEOUT
        assert "budget" in (result.error or "")

    async def test_prepare_failure_recorded(self, tmp_path: Path) -> None:
        runtime = build_runtime(provider=FakeProvider([]))
        adapter = SWEBenchAdapter(
            runtime=runtime,
            model_name="fake-model",
            workspace_root=tmp_path / "bench-workspaces",
            source_for=lambda instance: str(tmp_path / "does-not-exist"),
        )
        instance = BenchInstance(
            instance_id="fake__missing",
            problem_statement="irrelevant",
            repo="fake/missing",
            base_commit="0" * 40,
        )
        result = await adapter.run_instance(instance)
        assert result.status is InstanceStatus.PREPARE_FAILED
        assert result.run_id is None
        assert "clone" in (result.error or "")


class TestRunAndExport:
    async def test_run_manifest_and_jsonl_export(self, upstream: Path, tmp_path: Path) -> None:
        provider = FakeProvider(
            [
                # 实例 1：完成修复
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="edit",
                            arguments={
                                "path": "calc.py",
                                "expected_old": "return a - b",
                                "replacement": "return a + b",
                            },
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(content="fixed", stop_reason=StopReason.END_TURN),
                # 实例 2：无修改
                FakeResponse(content="no change", stop_reason=StopReason.END_TURN),
            ]
        )
        adapter = make_adapter(upstream, tmp_path, provider)
        one = make_instance(upstream, "fake__fake-0001")
        two = make_instance(upstream, "fake__fake-0002")

        manifest = await adapter.run([one, two])
        summary = manifest.summary()
        assert summary["total_instances"] == 2
        assert summary["submitted_predictions"] == 2
        assert summary["empty_patch"] == 1
        assert summary["results_by_status"]["completed"] == 1
        assert manifest.dataset_name == "fake-dataset"
        assert manifest.harness_git_sha == "deadbeef"
        assert all(result.workspace for result in manifest.results)
        assert manifest.results[0].workspace != manifest.results[1].workspace

        path = adapter.export_predictions(manifest.results, tmp_path / "preds" / "predictions.jsonl")
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == 2
        records = [json.loads(line) for line in lines]
        assert [record["instance_id"] for record in records] == ["fake__fake-0001", "fake__fake-0002"]
        for record in records:
            assert set(record.keys()) == {"instance_id", "model_name_or_path", "model_patch"}
            assert record["model_name_or_path"] == "fake-model"
        assert "+    return a + b" in records[0]["model_patch"]
        assert records[1]["model_patch"] == ""

    def test_failed_instances_not_submitted(self, upstream: Path, tmp_path: Path) -> None:
        from coding_agent.benchmark.swebench import InstanceResult

        adapter = make_adapter(upstream, tmp_path, FakeProvider([]))
        results = [
            InstanceResult(
                instance_id="fake__ok",
                status=InstanceStatus.COMPLETED,
                patch="diff --git a/x b/x\n",
            ),
            InstanceResult(
                instance_id="fake__empty",
                status=InstanceStatus.EMPTY_PATCH,
                patch="",
            ),
            InstanceResult(
                instance_id="fake__prep",
                status=InstanceStatus.PREPARE_FAILED,
                error="clone failed",
            ),
            InstanceResult(
                instance_id="fake__timeout",
                status=InstanceStatus.TIMEOUT,
                error="budget",
            ),
        ]
        path = adapter.export_predictions(results, tmp_path / "predictions.jsonl")
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        ids = [record["instance_id"] for record in records]
        assert ids == ["fake__ok", "fake__empty"]
        assert "fake__prep" not in ids
        assert "fake__timeout" not in ids
