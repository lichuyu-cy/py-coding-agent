"""阶段 11 单测：输出裁剪策略、artifact 托管与预算约束。"""

from __future__ import annotations

from pathlib import Path

from coding_agent.context.truncation import (
    ArtifactStore,
    ToolOutputTruncator,
    TruncationPolicy,
    TruncationStrategy,
    choose_strategy,
)
from coding_agent.domain.messages import ToolResultStatus
from coding_agent.ports.tool import ToolOutcome


def truncator(max_chars: int = 1000, **kwargs) -> ToolOutputTruncator:
    return ToolOutputTruncator(policy=TruncationPolicy(max_chars=max_chars, **kwargs))


class TestStrategySelection:
    def test_read_keeps_head(self) -> None:
        assert choose_strategy("read") is TruncationStrategy.HEAD

    def test_errors_keep_tail(self) -> None:
        assert choose_strategy("bash", error_kind="nonzero_exit") is TruncationStrategy.TAIL
        assert choose_strategy("read", error_kind="timeout") is TruncationStrategy.TAIL

    def test_default_is_head_tail(self) -> None:
        assert choose_strategy("bash") is TruncationStrategy.HEAD_TAIL


class TestTruncate:
    def test_under_limit_is_untouched(self) -> None:
        displayed = truncator().truncate("read", "short output")
        assert displayed.text == "short output"
        assert displayed.omitted_count == 0
        assert displayed.artifact_ref is None

    def test_empty_output(self) -> None:
        displayed = truncator().truncate("bash", "")
        assert displayed.text == ""
        assert displayed.omitted_count == 0

    def test_head_strategy_keeps_prefix_and_marks(self) -> None:
        output = "H" + "x" * 5000
        displayed = truncator(1000).truncate("read", output)
        assert displayed.strategy is TruncationStrategy.HEAD
        assert displayed.text.startswith("H")
        assert "characters omitted" in displayed.text
        assert displayed.omitted_count == len(output) - 800  # 预算 1000 - 标记预留 200
        assert displayed.text.endswith("]")

    def test_tail_strategy_keeps_stack_at_end(self) -> None:
        output = "x" * 5000 + "\nTraceback: final line"
        displayed = truncator(1000).truncate("bash", output, error_kind="nonzero_exit")
        assert displayed.strategy is TruncationStrategy.TAIL
        assert displayed.text.endswith("Traceback: final line")
        assert displayed.text.startswith("[... ")

    def test_head_tail_strategy_keeps_both_ends(self) -> None:
        output = "START" + "m" * 5000 + "END"
        displayed = truncator(1000).truncate("bash", output)
        assert displayed.strategy is TruncationStrategy.HEAD_TAIL
        assert displayed.text.startswith("START")
        assert displayed.text.endswith("END")
        assert "characters omitted" in displayed.text

    def test_displayed_text_within_budget(self) -> None:
        output = "y" * 50_000
        for kind, error_kind in (("read", None), ("bash", None), ("bash", "timeout")):
            displayed = truncator(1000).truncate(kind, output, error_kind=error_kind)
            assert len(displayed.text) <= 1000, (kind, error_kind)

    def test_unicode_boundary_is_safe(self) -> None:
        output = "你好世界" * 2000 + "🙂" * 100
        displayed = truncator(1000).truncate("bash", output)
        assert displayed.omitted_count > 0
        assert len(displayed.text) <= 1000
        assert _marker_of(displayed)  # 省略标记存在
        # 尾部保留的 emoji 未被截断（字符串切片按码点）
        assert displayed.text.rstrip().endswith("🙂")

    def test_policy_requires_room_for_marker(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            TruncationPolicy(max_chars=50)


class TestArtifacts:
    def test_full_output_stored_and_referenced(self, tmp_path: Path) -> None:
        output = "z" * 5000
        displayed = truncator(1000).truncate("bash", output, workspace=tmp_path)
        assert displayed.artifact_ref is not None
        assert displayed.artifact_ref.startswith(".coding-agent/artifacts/")
        artifact_path = tmp_path / displayed.artifact_ref
        assert artifact_path.exists()
        assert artifact_path.read_text(encoding="utf-8") == output
        assert displayed.artifact_ref in displayed.text

    def test_artifact_copy_capped_for_huge_outputs(self, tmp_path: Path) -> None:
        store = ArtifactStore(max_content_chars=100)
        tool = ToolOutputTruncator(policy=TruncationPolicy(max_chars=1000), artifact_store=store)
        displayed = tool.truncate("bash", "q" * 5000, workspace=tmp_path)
        assert displayed.artifact_ref is not None
        stored = (tmp_path / displayed.artifact_ref).read_text(encoding="utf-8")
        assert stored.startswith("q" * 100)
        assert "artifact copy truncated" in stored

    def test_storage_failure_still_returns_finite_text(self, tmp_path: Path) -> None:
        # 用同名文件占位，使 artifacts 目录无法创建
        blocker = tmp_path / ".coding-agent"
        blocker.write_text("block", encoding="utf-8")
        displayed = truncator(1000).truncate("bash", "w" * 5000, workspace=tmp_path)
        assert displayed.artifact_ref is None
        assert "not stored" in displayed.text
        assert len(displayed.text) <= 1000

    def test_no_workspace_means_no_store(self) -> None:
        displayed = truncator(1000).truncate("bash", "v" * 5000)
        assert displayed.artifact_ref is None
        assert "not stored" in displayed.text


class TestPipelineAdapter:
    class Record:
        name = "bash"

    class Context:
        def __init__(self, workspace: Path) -> None:
            self.workspace = workspace

    class Invocation:
        def __init__(self, workspace: Path) -> None:
            self.record = TestPipelineAdapter.Record()
            self.context = TestPipelineAdapter.Context(workspace)

    def test_process_truncates_and_attaches_ref(self, tmp_path: Path) -> None:
        tool = truncator(500)
        outcome = ToolOutcome(status=ToolResultStatus.COMPLETED, content="c" * 4000)
        processed = tool.process(outcome, self.Invocation(tmp_path))
        assert processed.status is ToolResultStatus.COMPLETED
        assert len(processed.content) <= 500
        assert processed.artifact_ref is not None
        assert (tmp_path / processed.artifact_ref).exists()

    def test_process_keeps_existing_ref_when_not_truncated(self, tmp_path: Path) -> None:
        tool = truncator(5000)
        outcome = ToolOutcome(
            status=ToolResultStatus.COMPLETED, content="small", artifact_ref="existing-ref"
        )
        processed = tool.process(outcome, self.Invocation(tmp_path))
        assert processed.content == "small"
        assert processed.artifact_ref == "existing-ref"


def _marker_of(displayed) -> str:
    """从显示文本中提取省略标记行（用于计数自洽断言）。"""
    for line in displayed.text.splitlines():
        if line.startswith("[... ") and line.endswith("]"):
            return line
    raise AssertionError("marker not found")
