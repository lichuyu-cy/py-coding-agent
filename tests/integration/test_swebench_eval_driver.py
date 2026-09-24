"""阶段 23 集成测试：评测驱动（eval_runner）的离线演练。

用虚构实例（本地仓库镜像 + Fake Provider）验证：实例清单解析、冻结信息、
产物落盘（freeze.json / manifest.json / predictions.jsonl）与 dry-run 行为；
不触网、不下载真实数据集。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent.benchmark.eval_runner import (
    EvalConfig,
    load_instances,
    main,
    run_evaluation,
)
from coding_agent.domain.state import StopReason
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
def repo_root(tmp_path: Path) -> Path:
    """本地镜像目录：repo 标识 `fake/calculator` → <repo_root>/calculator。"""
    if GIT is None:
        pytest.skip("git is required for the evaluation driver tests")
    root = tmp_path / "repos"
    repo = root / "calculator"
    repo.mkdir(parents=True)
    (repo / "calc.py").write_text(BUGGY_CALC, encoding="utf-8")
    run_git(repo, "init", "-q")
    run_git(repo, "add", ".")
    run_git(
        repo,
        "-c",
        "user.name=harness-eval",
        "-c",
        "user.email=eval@example.invalid",
        "commit",
        "-q",
        "-m",
        "base",
    )
    return root


def write_instances(path: Path, base_commit: str) -> Path:
    raw_lines = [
        {
            "instance_id": "fake__calc-0001",
            "problem_statement": "Make calculator add() return the sum.",
            "repo": "fake/calculator",
            "base_commit": base_commit,
            # 以下为评测侧字段：驱动必须忽略且不进入 BenchInstance
            "FAIL_TO_PASS": '["test_add"]',
            "PASS_TO_PASS": "[]",
            "test_patch": "SECRET-EVALUATION-PATCH",
        },
        {
            "instance_id": "fake__calc-0002",
            "problem_statement": "Should produce no change.",
            "repo": "fake/calculator",
            "base_commit": base_commit,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(line) for line in raw_lines) + "\n", encoding="utf-8"
    )
    return path


class TestLoadInstances:
    def test_ignores_evaluation_only_fields(self, repo_root: Path, tmp_path: Path) -> None:
        base_commit = run_git(repo_root / "calculator", "rev-parse", "HEAD").strip()
        instances_path = write_instances(tmp_path / "instances.jsonl", base_commit)

        instances = load_instances(instances_path)
        assert [instance.instance_id for instance in instances] == [
            "fake__calc-0001",
            "fake__calc-0002",
        ]
        first = instances[0]
        assert set(first.__slots__) == {"instance_id", "problem_statement", "repo", "base_commit"}
        assert "SECRET" not in repr(first)

        limited = load_instances(instances_path, limit=1)
        assert len(limited) == 1

    def test_missing_field_fails_explicitly(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text(json.dumps({"instance_id": "x"}) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required fields"):
            load_instances(path)


class TestRunEvaluation:
    async def test_full_offline_drill_writes_artifacts(self, repo_root: Path, tmp_path: Path) -> None:
        base_commit = run_git(repo_root / "calculator", "rev-parse", "HEAD").strip()
        instances_path = write_instances(tmp_path / "instances.jsonl", base_commit)

        provider = FakeProvider(
            [
                # 实例 1：修复
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
                FakeResponse(content="no change needed", stop_reason=StopReason.END_TURN),
            ]
        )
        config = EvalConfig(
            instances_path=instances_path,
            output_dir=tmp_path / "run-001",
            model_name="fake-model",
            dataset_name="fake-dataset",
            split="test",
            harness_git_sha="cafebabe",
            repo_root=repo_root,
            timeout_seconds=30.0,
        )
        report = await run_evaluation(config, provider_factory=lambda cfg: provider)

        # 产物：freeze.json / manifest.json / predictions.jsonl
        freeze = json.loads(report.freeze_path.read_text(encoding="utf-8"))
        assert freeze["model_name"] == "fake-model"
        assert freeze["harness_git_sha"] == "cafebabe"
        assert freeze["python_version"]
        assert freeze["instance_ids"] == ["fake__calc-0001", "fake__calc-0002"]
        assert freeze["repo_root"] == str(repo_root)

        assert report.manifest_path is not None
        manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
        summary = manifest["summary"]
        assert summary["total_instances"] == 2
        assert summary["results_by_status"]["completed"] == 1
        assert summary["results_by_status"]["empty_patch"] == 1
        assert summary["submitted_predictions"] == 2
        results = {item["instance_id"]: item for item in manifest["results"]}
        assert results["fake__calc-0001"]["patch_chars"] > 0
        assert results["fake__calc-0002"]["patch_chars"] == 0

        assert report.predictions_path is not None
        records = [
            json.loads(line)
            for line in report.predictions_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert [record["instance_id"] for record in records] == [
            "fake__calc-0001",
            "fake__calc-0002",
        ]
        assert "+    return a + b" in records[0]["model_patch"]
        assert records[1]["model_patch"] == ""

        # 工作区隔离在 output_dir/workspaces 下
        workspaces = list((config.output_dir / "workspaces").iterdir())
        assert len(workspaces) == 2
        assert all(workspace.is_dir() for workspace in workspaces)

    async def test_dry_run_writes_freeze_only(self, repo_root: Path, tmp_path: Path) -> None:
        base_commit = run_git(repo_root / "calculator", "rev-parse", "HEAD").strip()
        instances_path = write_instances(tmp_path / "instances.jsonl", base_commit)

        def exploding_factory(config):  # noqa: ANN001, ARG001
            raise AssertionError("provider must not be constructed during dry-run")

        config = EvalConfig(
            instances_path=instances_path,
            output_dir=tmp_path / "dry-run",
            model_name="fake-model",
            repo_root=repo_root,
            dry_run=True,
        )
        report = await run_evaluation(config, provider_factory=exploding_factory)

        assert report.freeze_path.exists()
        assert report.manifest_path is None
        assert report.predictions_path is None
        assert not (config.output_dir / "workspaces").exists()

    def test_cli_dry_run_entrypoint(self, repo_root: Path, tmp_path: Path) -> None:
        base_commit = run_git(repo_root / "calculator", "rev-parse", "HEAD").strip()
        instances_path = write_instances(tmp_path / "instances.jsonl", base_commit)
        output_dir = tmp_path / "cli-dry-run"

        exit_code = main(
            [
                "--instances",
                str(instances_path),
                "--output-dir",
                str(output_dir),
                "--model-name",
                "fake-model",
                "--repo-root",
                str(repo_root),
                "--git-sha",
                "deadbeef",
                "--dry-run",
            ]
        )
        assert exit_code == 0
        freeze = json.loads((output_dir / "freeze.json").read_text(encoding="utf-8"))
        assert freeze["harness_git_sha"] == "deadbeef"
        assert freeze["model_name"] == "fake-model"
