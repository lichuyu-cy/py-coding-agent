"""阶段 07 单测：Tool Pipeline 的步骤顺序、各检查点注入与至多一次执行。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from coding_agent.domain.messages import ToolCall, ToolCallId, ToolResultStatus
from coding_agent.ports.tool import ToolContext, ToolExecution, ToolExecutionError, ToolOutcome, ToolSpec
from coding_agent.tools.pipeline import (
    BeforeHookResult,
    PermissionDecision,
    PermissionResult,
    PipelineEventKind,
    ToolPipeline,
)
from coding_agent.tools.registry import RegistrySnapshot, ToolRegistry


def make_call(name: str = "echo", args: Mapping[str, Any] | None = None) -> ToolCall:
    return ToolCall(
        id=ToolCallId("call_1"),
        name=name,
        arguments=dict(args if args is not None else {"text": "hi"}),
        ordinal=0,
    )


class SpyTool:
    def __init__(self, events: list[str], *, schema: Mapping[str, Any] | None = None, behavior: str = "ok") -> None:
        self.events = events
        self._schema = dict(
            schema
            if schema is not None
            else {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
        )
        self._behavior = behavior
        self.calls: list[Mapping[str, Any]] = []

    def spec(self) -> ToolSpec:
        return ToolSpec(name="echo", description="spy tool", json_schema=self._schema)

    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution:
        self.events.append("tool")
        self.calls.append(dict(args))
        if self._behavior == "ok":
            return ToolExecution(output="ok-output")
        if self._behavior == "nonzero":
            return ToolExecution(output="failed-output", exit_code=2)
        if self._behavior == "tool_error":
            raise ToolExecutionError("file_not_found", "file not found: x")
        if self._behavior == "timeout_error":
            raise ToolExecutionError("timeout", "command timed out", output="partial-output")
        if self._behavior == "cancelled_error":
            raise ToolExecutionError("cancelled", "command was cancelled")
        if self._behavior == "internal":
            raise ValueError("boom")
        if self._behavior == "slow":
            await asyncio.sleep(1.0)
            return ToolExecution(output="slow done")
        raise AssertionError(self._behavior)


class SpyHooks:
    def __init__(
        self,
        events: list[str],
        *,
        before_result: BeforeHookResult | None = None,
        before_raises: bool = False,
        after_raises: bool = False,
    ) -> None:
        self.events = events
        self.before_result = before_result
        self.before_raises = before_raises
        self.after_raises = after_raises
        self.after_outcomes: list[ToolOutcome] = []

    def before(self, invocation: Any) -> BeforeHookResult | None:
        self.events.append("before")
        if self.before_raises:
            raise RuntimeError("before exploded")
        return self.before_result

    def after(self, invocation: Any, outcome: ToolOutcome) -> None:
        self.events.append("after")
        self.after_outcomes.append(outcome)
        if self.after_raises:
            raise RuntimeError("after exploded")


class SpyPolicy:
    def __init__(
        self,
        events: list[str],
        stage: str,
        result: PermissionResult | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self.events = events
        self.stage = stage
        self.result = result or PermissionResult(PermissionDecision.ALLOW)
        self.raises = raises

    def authorize(self, invocation: Any) -> PermissionResult:
        self.events.append(self.stage)
        if self.raises:
            raise RuntimeError(f"{self.stage} policy exploded")
        return self.result


class SpyProcessor:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def process(self, outcome: ToolOutcome) -> ToolOutcome:
        self.events.append("process")
        return ToolOutcome(
            status=outcome.status,
            content=f"[processed] {outcome.content}",
            artifact_ref=outcome.artifact_ref,
            error_kind=outcome.error_kind,
        )


class Harness:
    """统一装配共享同一事件列表的各间谍对象。"""

    def __init__(
        self,
        *,
        behavior: str = "ok",
        schema: Mapping[str, Any] | None = None,
        before_result: BeforeHookResult | None = None,
        before_raises: bool = False,
        after_raises: bool = False,
        permission: PermissionResult | None = None,
        permission_raises: bool = False,
        safety: PermissionResult | None = None,
        safety_raises: bool = False,
        processor: bool = False,
        execute_timeout_seconds: float | None = None,
    ) -> None:
        self.events: list[str] = []
        self.tool = SpyTool(self.events, schema=schema, behavior=behavior)
        self.registry = ToolRegistry()
        self.registry.register(self.tool)
        self.hooks = SpyHooks(
            self.events,
            before_result=before_result,
            before_raises=before_raises,
            after_raises=after_raises,
        )
        self.permission = SpyPolicy(self.events, "permission", permission, raises=permission_raises)
        self.safety = SpyPolicy(self.events, "safety", safety, raises=safety_raises)
        self.processor = SpyProcessor(self.events) if processor else None
        self.pipeline_events: list = []
        self.pipeline = ToolPipeline(
            self.registry,
            permission_policy=self.permission,
            safety_policy=self.safety,
            hooks=self.hooks,
            output_processor=self.processor,
            observer=self.pipeline_events.append,
            execute_timeout_seconds=execute_timeout_seconds,
        )


def ctx(tmp_path: Path, *, deadline: float | None = None) -> ToolContext:
    return ToolContext(workspace=tmp_path, deadline=deadline)


class TestStageOrder:
    async def test_full_pipeline_order(self, tmp_path: Path) -> None:
        harness = Harness(processor=True)
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert harness.events == ["before", "permission", "safety", "tool", "process", "after"]
        assert outcome.status is ToolResultStatus.COMPLETED
        assert outcome.content == "[processed] ok-output"
        kinds = [e.kind for e in harness.pipeline_events]
        assert kinds == [PipelineEventKind.TOOL_CALL_START, PipelineEventKind.TOOL_CALL_END]

    async def test_defaults_are_filled_before_tool(self, tmp_path: Path) -> None:
        schema = {"type": "object", "properties": {"text": {"type": "string", "default": "defaulted"}}}
        harness = Harness(schema=schema)
        await harness.pipeline.invoke(make_call(args={}), ctx(tmp_path))
        assert harness.tool.calls[0] == {"text": "defaulted"}


class TestRejections:
    async def test_unknown_tool(self, tmp_path: Path) -> None:
        harness = Harness()
        outcome = await harness.pipeline.invoke(make_call(name="missing"), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.ERROR
        assert outcome.error_kind == "unknown_tool"
        assert harness.events == []  # 未进入任何钩子与执行
        assert harness.pipeline_events[-1].kind is PipelineEventKind.TOOL_CALL_ERROR
        assert harness.tool.calls == []

    async def test_invalid_arguments(self, tmp_path: Path) -> None:
        harness = Harness()
        outcome = await harness.pipeline.invoke(make_call(args={"wrong": 1}), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.ERROR
        assert outcome.error_kind == "invalid_arguments"
        assert "text" in outcome.content  # 报错提及缺失字段
        assert harness.tool.calls == []

    async def test_before_hook_denies_without_permission_stage(self, tmp_path: Path) -> None:
        harness = Harness(before_result=BeforeHookResult(PermissionDecision.DENY, "not now"))
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.DENIED
        assert outcome.error_kind == "hook_denied"
        assert harness.events == ["before", "after"]  # 未进入权限/安全/执行
        assert harness.hooks.after_outcomes[-1] is outcome
        assert harness.tool.calls == []

    async def test_before_hook_exception_denied_and_recorded(self, tmp_path: Path) -> None:
        harness = Harness(before_raises=True)
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.error_kind == "hook_denied"
        assert "before exploded" in outcome.content
        assert any(e.kind is PipelineEventKind.HOOK_ERROR for e in harness.pipeline_events)
        assert harness.tool.calls == []

    async def test_permission_deny(self, tmp_path: Path) -> None:
        harness = Harness(permission=PermissionResult(PermissionDecision.DENY, "no"))
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.DENIED
        assert outcome.error_kind == "permission_denied"
        assert harness.events == ["before", "permission", "after"]
        assert harness.tool.calls == []

    async def test_require_approval_denied_without_approver(self, tmp_path: Path) -> None:
        harness = Harness(permission=PermissionResult(PermissionDecision.REQUIRE_APPROVAL, "review"))
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.DENIED
        assert outcome.error_kind == "permission_approval_required"
        assert "approval" in outcome.content
        assert harness.tool.calls == []

    async def test_safety_deny_after_permission(self, tmp_path: Path) -> None:
        harness = Harness(safety=PermissionResult(PermissionDecision.DENY, "dangerous"))
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.error_kind == "safety_denied"
        assert harness.events == ["before", "permission", "safety", "after"]
        assert harness.tool.calls == []

    async def test_policy_exception_becomes_denial(self, tmp_path: Path) -> None:
        harness = Harness(permission_raises=True)
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.DENIED
        assert outcome.error_kind == "permission_denied"
        assert "exploded" in outcome.content
        assert harness.tool.calls == []


class TestNormalization:
    async def test_tool_error_kind_preserved(self, tmp_path: Path) -> None:
        harness = Harness(behavior="tool_error")
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.ERROR
        assert outcome.error_kind == "file_not_found"
        assert harness.events == ["before", "permission", "safety", "tool", "after"]
        assert harness.pipeline_events[-1].kind is PipelineEventKind.TOOL_CALL_ERROR

    async def test_timeout_error_keeps_partial_output(self, tmp_path: Path) -> None:
        harness = Harness(behavior="timeout_error")
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.TIMEOUT
        assert outcome.error_kind == "timeout"
        assert "partial-output" in outcome.content

    async def test_cancelled_error(self, tmp_path: Path) -> None:
        harness = Harness(behavior="cancelled_error")
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.CANCELLED
        assert outcome.error_kind == "cancelled"

    async def test_nonzero_exit_is_error_result(self, tmp_path: Path) -> None:
        harness = Harness(behavior="nonzero")
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.ERROR
        assert outcome.error_kind == "nonzero_exit"
        assert outcome.content == "failed-output"

    async def test_internal_exception_normalized_once(self, tmp_path: Path) -> None:
        harness = Harness(behavior="internal")
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.ERROR
        assert outcome.error_kind == "internal_error"
        assert "ValueError" in outcome.content
        assert len(harness.tool.calls) == 1  # 至多一次

    async def test_execute_timeout_wrap(self, tmp_path: Path) -> None:
        harness = Harness(behavior="slow", execute_timeout_seconds=0.2)
        started = time.monotonic()
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        elapsed = time.monotonic() - started
        assert outcome.status is ToolResultStatus.TIMEOUT
        assert elapsed < 0.9  # 未等待 1s 的 sleep
        assert len(harness.tool.calls) == 1

    async def test_deadline_from_context_bounds_execution(self, tmp_path: Path) -> None:
        harness = Harness(behavior="slow")
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path, deadline=0.2))
        assert outcome.status is ToolResultStatus.TIMEOUT


class TestHooksAndProcessor:
    async def test_after_hook_failure_recorded_without_rerun(self, tmp_path: Path) -> None:
        harness = Harness(after_raises=True)
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert outcome.status is ToolResultStatus.COMPLETED  # after 失败不改写结果
        assert len(harness.tool.calls) == 1  # 不重跑
        assert any(
            e.kind is PipelineEventKind.HOOK_ERROR and "after exploded" in (e.detail or "")
            for e in harness.pipeline_events
        )

    async def test_output_processor_runs_before_after(self, tmp_path: Path) -> None:
        harness = Harness(processor=True)
        await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert harness.events.index("process") < harness.events.index("after")
        assert harness.hooks.after_outcomes[-1].content == "[processed] ok-output"

    async def test_after_receives_outcome_on_execution_failure(self, tmp_path: Path) -> None:
        harness = Harness(behavior="tool_error")
        outcome = await harness.pipeline.invoke(make_call(), ctx(tmp_path))
        assert harness.hooks.after_outcomes[-1] is outcome


class TestSnapshotSemantics:
    async def test_explicit_snapshot_resolves_removed_tool(self, tmp_path: Path) -> None:
        events: list[str] = []
        tool = SpyTool(events)
        registry = ToolRegistry()
        registry.register(tool)
        snapshot: RegistrySnapshot = registry.snapshot()
        registry.unregister("echo")
        pipeline = ToolPipeline(registry)

        without_snapshot = await pipeline.invoke(make_call(), ctx(tmp_path))
        assert without_snapshot.error_kind == "unknown_tool"

        with_snapshot = await pipeline.invoke(make_call(), ctx(tmp_path), snapshot=snapshot)
        assert with_snapshot.status is ToolResultStatus.COMPLETED
        assert len(tool.calls) == 1

    async def test_loop_compatible_execute_entry(self, tmp_path: Path) -> None:
        harness = Harness()
        outcome = await harness.pipeline.execute(make_call(), workspace=tmp_path)
        assert outcome.status is ToolResultStatus.COMPLETED
        assert len(harness.tool.calls) == 1
