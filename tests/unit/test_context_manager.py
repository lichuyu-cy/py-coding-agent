"""阶段 10 单测：ContextManager 的段落顺序、工具组完整性、确定性与预算。"""

from __future__ import annotations

import pytest

from coding_agent.context.builder import (
    ContextError,
    ContextManager,
    ContextPolicy,
    PromptSection,
    convert_to_provider,
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
from coding_agent.domain.state import StopReason
from coding_agent.ports.provider import ProviderMessageRole
from coding_agent.ports.tokenizer import SimpleTokenCounter

SESSION = "sess_ctx"


def meta(msg_id: str, turn: int = 1) -> MessageMeta:
    return MessageMeta(id=msg_id, session_id=SESSION, run_id="run_1", turn_id=turn, created_at="2026-09-23T00:00:00.000000Z")


def build_log(*, with_result: bool = True, tail: str = "tool_result") -> MessageLog:
    log = MessageLog(SESSION)
    log.append(UserMessage(meta=meta("msg_u1"), content="fix the bug"))
    log.append(
        AssistantMessage(
            meta=meta("msg_a1"),
            content="working",
            stop_reason=StopReason.TOOL_CALLS,
            tool_calls=(
                ToolCall(id=ToolCallId("call_1"), name="read", arguments={"path": "a.py"}, ordinal=0),
            ),
        )
    )
    if with_result:
        log.append(
            ToolResult(
                meta=meta("msg_r1"),
                tool_call_id=ToolCallId("call_1"),
                status=ToolResultStatus.COMPLETED,
                content="file contents",
            )
        )
    if tail == "user":
        log.append(UserMessage(meta=meta("msg_u2"), content="continue"))
    return log


def manager(**policy_kwargs) -> ContextManager:
    defaults = {"system_prompt": "system prompt text"}
    defaults.update(policy_kwargs)
    return ContextManager(ContextPolicy(**defaults))


class TestSections:
    def test_system_first_and_sections_in_order(self) -> None:
        ctx = ContextManager(
            ContextPolicy(system_prompt="base prompt", project_context="project facts")
        )
        snapshot = ctx.build(build_log())
        first = snapshot.messages[0]
        assert first.role is ProviderMessageRole.SYSTEM
        assert first.content.startswith("base prompt")
        assert first.content.index("base prompt") < first.content.index("project facts")
        assert [s.name for s in snapshot.sections] == ["system", "project"]

    def test_extra_sections_append_after_project(self) -> None:
        ctx = ContextManager(
            ContextPolicy(system_prompt="base", project_context="project")
        )
        snapshot = ctx.build(
            build_log(), extra_sections=(PromptSection(name="skill", content="skill body"),)
        )
        assert [s.name for s in snapshot.sections] == ["system", "project", "skill"]
        assert snapshot.messages[0].content.index("project") < snapshot.messages[0].content.index("skill body")

    def test_source_ids_include_sections_and_message_ids(self) -> None:
        snapshot = manager().build(build_log())
        assert snapshot.source_ids == (
            "section:system",
            "msg_u1",
            "msg_a1",
            "msg_r1",
        )


class TestConversion:
    def test_tool_group_converted_completely(self) -> None:
        snapshot = manager().build(build_log())
        roles = [m.role for m in snapshot.messages]
        assert roles == [
            ProviderMessageRole.SYSTEM,
            ProviderMessageRole.USER,
            ProviderMessageRole.ASSISTANT,
            ProviderMessageRole.TOOL,
        ]
        assistant = snapshot.messages[2]
        assert assistant.tool_calls[0].id == "call_1"
        tool = snapshot.messages[3]
        assert tool.tool_call_id == "call_1"

    def test_observation_formatter_applied(self) -> None:
        ctx = ContextManager(
            ContextPolicy(system_prompt="base"),
            observation_formatter=lambda result: f"[formatted {result.status.value}] {result.content}",
        )
        snapshot = ctx.build(build_log())
        assert snapshot.messages[-1].content.startswith("[formatted completed]")

    def test_convert_to_provider_is_pure(self) -> None:
        log = build_log()
        first = convert_to_provider(log.messages)
        second = convert_to_provider(log.messages)
        assert first == second


class TestValidation:
    def test_empty_history_rejected(self) -> None:
        with pytest.raises(ContextError, match="empty history"):
            manager().build(MessageLog(SESSION))

    def test_assistant_tail_rejected(self) -> None:
        log = build_log(with_result=True, tail="tool_result")
        log.append(
            AssistantMessage(meta=meta("msg_a2", turn=2), content="done", stop_reason=StopReason.END_TURN)
        )
        with pytest.raises(ContextError, match="assistant message"):
            manager().build(log)

    def test_incomplete_tool_group_rejected(self) -> None:
        log = MessageLog(SESSION)
        log.append(UserMessage(meta=meta("msg_u1"), content="fix"))
        log.append(
            AssistantMessage(
                meta=meta("msg_a1"),
                content="",
                stop_reason=StopReason.TOOL_CALLS,
                tool_calls=(
                    ToolCall(id=ToolCallId("call_1"), name="read", arguments={"path": "a.py"}, ordinal=0),
                    ToolCall(id=ToolCallId("call_2"), name="read", arguments={"path": "b.py"}, ordinal=1),
                ),
            )
        )
        # 尾部类型合法（工具结果），但 call_2 没有最终结果
        log.append(
            ToolResult(
                meta=meta("msg_r1"),
                tool_call_id=ToolCallId("call_1"),
                status=ToolResultStatus.COMPLETED,
                content="a contents",
            )
        )
        with pytest.raises(ContextError, match="incomplete tool group"):
            manager().build(log)

    def test_budget_exceeded_rejected(self) -> None:
        ctx = ContextManager(ContextPolicy(system_prompt="base", max_tokens=5))
        with pytest.raises(ContextError, match="budget"):
            ctx.build(build_log())

    def test_budget_within_limit_passes(self) -> None:
        ctx = ContextManager(ContextPolicy(system_prompt="base", max_tokens=10_000))
        snapshot = ctx.build(build_log())
        assert snapshot.estimated_tokens < 10_000


class TestDeterminism:
    def test_same_input_same_snapshot(self) -> None:
        log = build_log()
        ctx = manager()
        first = ctx.build(log)
        second = ctx.build(log)
        assert first == second

    def test_estimated_tokens_uses_injected_counter(self) -> None:
        class CountingTokenCounter:
            def __init__(self) -> None:
                self.calls = 0

            def count_text(self, text: str) -> int:
                self.calls += 1
                return 1

        counter = CountingTokenCounter()
        ctx = ContextManager(ContextPolicy(system_prompt="base"), counter=counter)
        snapshot = ctx.build(build_log())
        assert counter.calls > 0
        assert snapshot.estimated_tokens >= 4  # system + 3 条消息（含有调用的助手）

    def test_simple_counter_heuristic(self) -> None:
        counter = SimpleTokenCounter(chars_per_token=4.0)
        assert counter.count_text("") == 0
        assert counter.count_text("abcdefgh") == 2
        assert counter.count_text("abc") == 1

    def test_summary_version_reserved_as_none(self) -> None:
        snapshot = manager().build(build_log())
        assert snapshot.summary_version is None
