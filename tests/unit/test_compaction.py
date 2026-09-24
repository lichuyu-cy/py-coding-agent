"""阶段 14 单测：合法切割点、结构化摘要、原子提交与 ContextManager 集成。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.bootstrap import build_runtime
from coding_agent.context.budget import TokenBudget, TokenManager
from coding_agent.context.builder import ContextError, ContextManager, ContextPolicy
from coding_agent.context.compaction import (
    CompactionError,
    Compactor,
    extract_summary,
    find_cut,
    group_messages,
    render_summary,
)
from coding_agent.domain.messages import (
    AssistantMessage,
    MessageLog,
    MessageMeta,
    ToolCall,
    ToolCallId,
    ToolResult,
    ToolResultStatus,
    UserMessage,
)
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.ports.tokenizer import SimpleTokenCounter
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall

SESSION = "sess_compact"


def meta(msg_id: str, turn: int = 1) -> MessageMeta:
    return MessageMeta(
        id=msg_id, session_id=SESSION, run_id="run_1", turn_id=turn, created_at="2026-09-24T00:00:00.000000Z"
    )


def user(msg_id: str, content: str = "do the task") -> UserMessage:
    return UserMessage(meta=meta(msg_id), content=content)


def assistant_with_calls(msg_id: str, *calls: tuple[str, str, dict]) -> AssistantMessage:
    return AssistantMessage(
        meta=meta(msg_id),
        content="",
        stop_reason=StopReason.TOOL_CALLS,
        tool_calls=tuple(
            ToolCall(id=ToolCallId(call_id), name=name, arguments=args, ordinal=index)
            for index, (call_id, name, args) in enumerate(calls)
        ),
    )


def tool_result(msg_id: str, call_id: str, content: str = "ok", status: ToolResultStatus = ToolResultStatus.COMPLETED) -> ToolResult:
    return ToolResult(
        meta=meta(msg_id),
        tool_call_id=ToolCallId(call_id),
        status=status,
        content=content,
        error_kind="nonzero_exit" if status is ToolResultStatus.ERROR else None,
    )


def design_example_log() -> MessageLog:
    """contracts 示例：U1 → A1(c1,c2) → R1 → R2 → U2 → A2。"""
    log = MessageLog(SESSION)
    log.append(user("msg_u1", "fix the bug"))
    log.append(
        assistant_with_calls(
            "msg_a1",
            ("c1", "read", {"path": "a.py"}),
            ("c2", "bash", {"command": "pytest -q"}),
        )
    )
    log.append(tool_result("msg_r1", "c1", "file contents"))
    log.append(tool_result("msg_r2", "c2", "1 failed", ToolResultStatus.ERROR))
    log.append(user("msg_u2", "continue"))
    log.append(
        AssistantMessage(meta=meta("msg_a2", turn=2), content="done", stop_reason=StopReason.END_TURN)
    )
    return log


class TestGrouping:
    def test_groups_never_split_multi_call_assistant(self) -> None:
        groups = group_messages(design_example_log().messages)
        assert [len(g.messages) for g in groups] == [1, 3, 1, 1]
        assert groups[1].end_index == 3  # A1 + R1 + R2
        assert all(not g.incomplete for g in groups)

    def test_incomplete_group_flagged(self) -> None:
        log = MessageLog(SESSION)
        log.append(user("msg_u1"))
        log.append(
            assistant_with_calls(
                "msg_a1",
                ("c1", "read", {"path": "a.py"}),
                ("c2", "bash", {"command": "x"}),
            )
        )
        log.append(tool_result("msg_r1", "c1"))
        groups = group_messages(log.messages)
        assert groups[1].incomplete is True


class TestFindCut:
    def test_cut_never_inside_group(self) -> None:
        plan = find_cut(design_example_log().messages, preserve_recent=2)
        assert plan is not None
        assert plan.cut_index == 4  # U1, A1+R1+R2 之后
        assert plan.covered_ids == ("msg_u1", "msg_a1", "msg_r1", "msg_r2")
        assert plan.preserved_ids == ("msg_u2", "msg_a2")

    def test_recent_groups_preserved(self) -> None:
        plan = find_cut(design_example_log().messages, preserve_recent=1)
        assert plan is not None
        assert plan.cut_index == 5  # 保留最后一个组（A2）
        assert plan.preserved_ids == ("msg_a2",)

    def test_no_cut_when_too_few_groups(self) -> None:
        assert find_cut(design_example_log().messages, preserve_recent=4) is None

    def test_incomplete_group_blocks_cut_past_it(self) -> None:
        log = MessageLog(SESSION)
        log.append(user("msg_u1"))
        log.append(assistant_with_calls("msg_a1", ("c1", "read", {"path": "a.py"})))
        log.append(user("msg_u2"))
        # A1 未完成：任何覆盖都必须停在 U1
        plan = find_cut(log.messages, preserve_recent=1)
        assert plan is not None
        assert plan.covered_ids == ("msg_u1",)

    def test_preserve_recent_validation(self) -> None:
        with pytest.raises(ValueError):
            find_cut(design_example_log().messages, preserve_recent=0)


class TestSummary:
    def test_extraction_fields(self) -> None:
        log = design_example_log()
        cut = find_cut(log.messages, preserve_recent=2)
        assert cut is not None
        covered = [item for group in cut.covered for item in group.messages]
        summary = extract_summary(covered)
        assert summary.task_goal == "fix the bug"
        assert summary.files_read == ("a.py",)
        assert summary.commands_executed == ("pytest -q",)
        assert summary.completed_work == "1 tool calls completed"
        assert summary.errors and "bash" in summary.errors[0]
        assert "current_progress" in summary.unknown_fields
        assert summary.source_ids == ("msg_u1", "msg_a1", "msg_r1", "msg_r2")

    def test_unknown_not_fabricated(self) -> None:
        log = MessageLog(SESSION)
        log.append(user("msg_u1", "task"))
        summary = extract_summary(tuple(log.messages))
        assert summary.current_progress == "unknown"
        assert summary.next_step == "unknown"

    def test_render_contains_fields_and_unknowns(self) -> None:
        log = design_example_log()
        cut = find_cut(log.messages, preserve_recent=2)
        assert cut is not None
        covered = [item for group in cut.covered for item in group.messages]
        rendered = render_summary(extract_summary(covered))
        assert "[summary of earlier history]" in rendered
        assert "task_goal: fix the bug" in rendered
        assert "files_modified: none" in rendered
        assert "unknown_fields:" in rendered


class TestCompactAndValidate:
    def test_compact_generates_record_and_versions_grow(self) -> None:
        log = design_example_log()
        compactor = Compactor(preserve_recent=2)
        first = compactor.compact(log.messages)
        assert first is not None
        assert first.summary_version == 1
        assert first.covered_through_id == "msg_r2"
        log.append(user("msg_u3", "more"))
        log.append(AssistantMessage(meta=meta("msg_a3", turn=3), content="ok", stop_reason=StopReason.END_TURN))
        second = compactor.compact(log.messages, old_record=first)
        assert second is not None
        assert second.summary_version == 2
        assert second.covered_through_id == "msg_a2"  # 新增覆盖 [U2],[A2]，保留最近 2 组

    def test_compact_does_not_modify_history(self) -> None:
        log = design_example_log()
        before = log.serialize()
        Compactor(preserve_recent=2).compact(log.messages)
        assert log.serialize() == before

    def test_no_cut_returns_none(self) -> None:
        log = design_example_log()
        assert Compactor(preserve_recent=10).compact(log.messages) is None

    def test_validate_rejects_tampered_record(self) -> None:
        from dataclasses import replace

        log = design_example_log()
        record = Compactor(preserve_recent=2).compact(log.messages)
        assert record is not None
        tampered = replace(record, covered_through_id="msg_missing")
        with pytest.raises(CompactionError, match="not in the history"):
            Compactor.validate(tampered, log.messages)


class TestContextManagerIntegration:
    def _manager(self, limit: int, *, preserve_recent: int = 2) -> ContextManager:
        return ContextManager(
            ContextPolicy(system_prompt="base"),
            token_manager=TokenManager(
                SimpleTokenCounter(chars_per_token=1.0),
                budget=TokenBudget(context_limit=limit, output_reserve_ratio=0.0, safety_margin_ratio=0.0),
            ),
            compactor=Compactor(preserve_recent=preserve_recent),
        )

    def _big_log(self) -> MessageLog:
        """尾部为用户消息（可响应的历史）；U1 可被覆盖，U2 附近保留。"""
        log = MessageLog(SESSION)
        log.append(user("msg_u1", "u" * 600))
        log.append(assistant_with_calls("msg_a1", ("c1", "read", {"path": "a.py"})))
        log.append(tool_result("msg_r1", "c1", "r" * 500))
        log.append(user("msg_u2", "u" * 700))
        return log

    def test_compaction_rebuilds_with_summary_section(self) -> None:
        # usable=2000, threshold=1500；全量估算 ≈ 1825 → 触发压缩
        ctx = self._manager(2000)
        snapshot = ctx.build(self._big_log())
        assert snapshot.summary_version == 1
        assert any(section.name == "summary" for section in snapshot.sections)
        assert snapshot.messages[0].content.startswith("base")
        assert "[summary of earlier history]" in snapshot.messages[0].content
        # 覆盖范围之后的消息仍在（保留尾部）
        tail_user = [m for m in snapshot.messages[1:] if m.content == "u" * 700]
        assert len(tail_user) == 1
        # source_ids 含 summary 段与尾部消息
        assert "section:summary" in snapshot.source_ids

    def test_second_build_extends_summary_version(self) -> None:
        ctx = self._manager(2000)
        log = self._big_log()
        first = ctx.build(log)
        assert first.summary_version == 1
        log.append(user("msg_u3", "u" * 300))
        second = ctx.build(log)
        assert second.summary_version == 2

    def test_no_legal_cut_reports_overflow(self) -> None:
        ctx = self._manager(2000, preserve_recent=10)
        with pytest.raises(ContextError, match="no legal cut point"):
            ctx.build(self._big_log())

    def test_still_over_budget_after_compaction_rejected(self) -> None:
        # usable=1000, threshold=750；保留尾部很大（U2=900）→ 压缩后仍超窗 → 明确报错
        ctx = self._manager(1000, preserve_recent=1)
        log = MessageLog(SESSION)
        log.append(user("msg_u1", "u" * 400))
        log.append(assistant_with_calls("msg_a1", ("c1", "read", {"path": "a.py"})))
        log.append(tool_result("msg_r1", "c1", "r" * 400))
        log.append(user("msg_u2", "u" * 900))
        with pytest.raises(ContextError, match="still does not fit"):
            ctx.build(log)


class TestLoopCompactionFlow:
    async def test_run_compacts_and_finishes(self, tmp_path: Path) -> None:
        big = "x" * 800
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": f"echo {big}"}, id="call_1"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": f"echo {big}"}, id="call_2"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": f"echo {big}"}, id="call_3"),),
                ),
                FakeResponse(content="finished", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(
            provider=provider,
            context_limit_tokens=2600,
            compactor=Compactor(preserve_recent=1),
        )
        result = await runtime.run("print stuff", tmp_path)

        assert result.status is RunStatus.FINISHED
        assert result.final_text == "finished"
        # 至少一次请求的 system 中出现摘要段（压缩已发生）
        assert any("[summary of earlier history]" in request.messages[0].content for request in provider.requests)
        # 历史不被删除：user + 3×(assistant+result) + final assistant
        log = runtime.registry.get(result.session_id)
        assert len(log.messages) == 1 + 3 * 2 + 1
