"""Tool Calling Pipeline：所有工具调用一致的检查、执行与结果归一。

生命周期（顺序固定，单测用 spy 断言）::

    解析(name/快照查找) → schema 校验 → 参数预处理(补默认值)
    → before hooks → permission → safety → execute(受 timeout 包裹)
    → 异常归一 → 输出处理(output_processor) → after hooks → ToolOutcome

约束：
- 不能让模型直接获得原始 Tool 实例；一切调用经本管线；
- before 可以补充日志/否决，但不可绕开后续权限/安全判断；
- after 在失败后也会收到 outcome，且不得改写真实执行事实（返回值被忽略，
  其异常只记录 HookError，绝不重跑工具）；
- 每个检查点失败都产生恰好一个可审计的 ToolOutcome（结构化拒绝）；
- 执行至多一次：任何归一/钩子失败都不会重放工具。

阶段边界：permission/safety 的默认实现为全放行，阶段 08 注入真实 SafetyPolicy；
输出裁剪在阶段 11 由 output_processor 接管；事件为最小通知，阶段 15 并入 typed EventBus。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import jsonschema

from coding_agent.domain.messages import ToolCall, ToolResultStatus
from coding_agent.ports.provider import CancelSignal
from coding_agent.ports.tool import ToolContext, ToolExecution, ToolExecutionError, ToolOutcome
from coding_agent.tools.recovery import hint_for_kind, normalize_internal_error, normalize_tool_error
from coding_agent.tools.registry import (
    RegisteredTool,
    RegistrySnapshot,
    ToolRegistry,
    ToolRegistryError,
    normalize_tool_name,
)

__all__ = [
    "AllowAllPolicy",
    "BeforeHookResult",
    "OutputProcessor",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionResult",
    "PipelineEvent",
    "PipelineEventKind",
    "PipelineObserver",
    "SafetyPolicy",
    "ToolInvocation",
    "ToolPipeline",
    "ToolPipelineHooks",
    "ValidationOutcome",
]


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str = ""


class PermissionPolicy(Protocol):
    """权限判断（在 Safety 之前）。阶段 08 由 SafetyPolicy 之外的策略注入。"""

    def authorize(self, invocation: "ToolInvocation") -> PermissionResult: ...


class SafetyPolicy(Protocol):
    """安全判断（在 Permission 之后）。阶段 08 提供真实实现，本阶段默认全放行。"""

    def authorize(self, invocation: "ToolInvocation") -> PermissionResult: ...


class AllowAllPolicy:
    """默认放行策略；仅用于权限/安全的默认占位（阶段 08 替换）。"""

    def authorize(self, invocation: "ToolInvocation") -> PermissionResult:  # noqa: ARG002
        return PermissionResult(PermissionDecision.ALLOW, "default allow-all")


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """一次已解析、已验证的工具调用。"""

    call: ToolCall
    record: RegisteredTool
    args: Mapping[str, Any]
    context: ToolContext


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    ok: bool
    args: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BeforeHookResult:
    """before 钩子的可选否决结果；None 表示不干预。"""

    decision: PermissionDecision = PermissionDecision.ALLOW
    reason: str = ""


class ToolPipelineHooks(Protocol):
    """调用前后钩子（结构化协议；提供 before/after 两个方法即可）。"""

    def before(self, invocation: ToolInvocation) -> BeforeHookResult | None: ...

    def after(self, invocation: ToolInvocation, outcome: ToolOutcome) -> None: ...


class OutputProcessor(Protocol):
    """输出处理（阶段 11 的裁剪/artifact 将实现本协议）。"""

    def process(self, outcome: ToolOutcome) -> ToolOutcome: ...


class PipelineEventKind(StrEnum):
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_ERROR = "tool_call_error"
    HOOK_ERROR = "hook_error"
    POLICY_DECISION = "policy_decision"


@dataclass(frozen=True, slots=True)
class PipelineEvent:
    kind: PipelineEventKind
    tool_call_id: str
    tool_name: str
    detail: str | None = None


PipelineObserver = Callable[[PipelineEvent], None]


class ToolPipeline:
    """工具调用管线；Runtime 的唯一工具执行入口。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        permission_policy: PermissionPolicy | None = None,
        safety_policy: SafetyPolicy | None = None,
        hooks: ToolPipelineHooks | None = None,
        output_processor: OutputProcessor | None = None,
        observer: PipelineObserver | None = None,
        execute_timeout_seconds: float | None = None,
    ) -> None:
        self._registry = registry
        self._permission = permission_policy or AllowAllPolicy()
        self._safety = safety_policy or AllowAllPolicy()
        self._hooks = hooks
        self._output_processor = output_processor
        self._observer = observer
        self._execute_timeout = execute_timeout_seconds

    async def invoke(
        self,
        call: ToolCall,
        context: ToolContext,
        *,
        snapshot: RegistrySnapshot | None = None,
    ) -> ToolOutcome:
        """执行一次完整调用，返回恰好一个 ToolOutcome。

        snapshot 为本次调用使用的注册表快照（默认取当前快照）；
        调用方应传入与本 turn 工具声明相同的快照版本。
        """
        active = snapshot if snapshot is not None else self._registry.snapshot()

        # 1. 解析：名称规范化 + 快照查找
        try:
            name = normalize_tool_name(call.name)
        except ToolRegistryError as exc:
            return self._reject(call, "unknown_tool", f"invalid tool name: {exc}")
        record = active.get(name)
        if record is None:
            return self._reject(call, "unknown_tool", f"unknown tool {call.name!r}")

        # 2. schema 校验 + 3. 参数预处理（补默认值）
        validation = self._validate_and_preprocess(record, call.arguments)
        if not validation.ok:
            detail = "; ".join(validation.errors)
            return self._reject(call, "invalid_arguments", f"invalid arguments for {name!r}: {detail}")
        invocation = ToolInvocation(call=call, record=record, args=validation.args, context=context)

        # 4. before hooks（可否决；不能绕开权限/安全）
        hook_result = self._call_before(invocation)
        if hook_result is not None and hook_result.decision is PermissionDecision.DENY:
            outcome = ToolOutcome(
                status=ToolResultStatus.DENIED,
                content=f"rejected by before hook: {hook_result.reason or 'no reason given'}",
                error_kind="hook_denied",
            )
            self._emit(PipelineEventKind.TOOL_CALL_ERROR, call, detail="hook_denied")
            self._call_after(invocation, outcome)
            return outcome

        # 5. permission
        permission = self._authorize(self._permission, invocation, "permission")
        if permission.decision is not PermissionDecision.ALLOW:
            outcome = self._denial_outcome(call, "permission", permission)
            self._call_after(invocation, outcome)
            return outcome

        # 6. safety
        safety = self._authorize(self._safety, invocation, "safety")
        if safety.decision is not PermissionDecision.ALLOW:
            outcome = self._denial_outcome(call, "safety", safety)
            self._call_after(invocation, outcome)
            return outcome

        # 7. execute（受 timeout 包裹；至多一次）
        self._emit(PipelineEventKind.TOOL_CALL_START, call)
        try:
            execution = await self._run_tool(record, invocation)
            outcome = self._normalize_success(execution)
        except ToolExecutionError as err:
            outcome = normalize_tool_error(err)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - 归一为内部错误，不猜测类型
            outcome = normalize_internal_error(err)

        # 8. 输出处理（裁剪/artifact，阶段 11）
        if self._output_processor is not None:
            processed = self._output_processor.process(outcome)
            if processed is not None:
                outcome = processed

        # 9. after hooks（失败后也调用；不可改写事实）
        self._call_after(invocation, outcome)

        if outcome.status is ToolResultStatus.COMPLETED:
            self._emit(PipelineEventKind.TOOL_CALL_END, call)
        else:
            self._emit(PipelineEventKind.TOOL_CALL_ERROR, call, detail=outcome.error_kind)
        return outcome

    async def execute(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        cancel: CancelSignal | None = None,
        deadline: float | None = None,
    ) -> ToolOutcome:
        """AgentLoop 兼容入口（工具执行口协议）：组装 ToolContext 后调用 invoke。"""
        context = ToolContext(workspace=workspace, cancel=cancel, deadline=deadline)
        return await self.invoke(call, context)

    # ---- 内部步骤 ----

    def _validate_and_preprocess(
        self, record: RegisteredTool, raw_args: object
    ) -> ValidationOutcome:
        if not isinstance(raw_args, Mapping):
            return ValidationOutcome(ok=False, errors=("arguments must be a JSON object",))
        schema = dict(record.spec.json_schema)
        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(
            (
                f"{'/'.join(str(part) for part in error.absolute_path) or '(root)'}: {error.message}"
                for error in validator.iter_errors(dict(raw_args))
            ),
        )
        if errors:
            return ValidationOutcome(ok=False, errors=tuple(errors))
        args = dict(raw_args)
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, prop in properties.items():
                if isinstance(prop, Mapping) and "default" in prop and key not in args:
                    args[key] = prop["default"]
        return ValidationOutcome(ok=True, args=args)

    async def _run_tool(self, record: RegisteredTool, invocation: ToolInvocation) -> ToolExecution:
        timeout = self._execute_timeout
        if invocation.context.deadline is not None:
            timeout = (
                invocation.context.deadline
                if timeout is None
                else min(timeout, invocation.context.deadline)
            )
        pending = record.tool.execute(invocation.args, invocation.context)
        if timeout is None:
            return await pending
        try:
            return await asyncio.wait_for(pending, timeout=max(timeout, 0.001))
        except TimeoutError:
            raise ToolExecutionError(
                "timeout",
                f"tool {record.name!r} exceeded its {max(timeout, 0.001):g}s execution budget",
            ) from None

    def _authorize(
        self, policy: PermissionPolicy | SafetyPolicy, invocation: ToolInvocation, stage: str
    ) -> PermissionResult:
        """执行策略判断；策略自身异常归一为拒绝（不静默放行），并记录 HookError 事件。

        每次正常判断都发 POLICY_DECISION 证据事件（decision/reason，可选 risk/policy_version）。
        """
        try:
            result = policy.authorize(invocation)
        except Exception as err:  # noqa: BLE001 - 策略故障必须可审计且不得放行
            self._emit(
                PipelineEventKind.HOOK_ERROR,
                invocation.call,
                detail=f"{stage} policy failed: {type(err).__name__}: {err}",
            )
            return PermissionResult(
                PermissionDecision.DENY,
                f"{stage} policy raised {type(err).__name__}: {err}",
            )
        detail = f"stage={stage} decision={result.decision.value} reason={result.reason}"
        risk = getattr(result, "risk", None)
        version = getattr(result, "policy_version", None)
        if risk is not None:
            detail += f" risk={risk}"
        if version:
            detail += f" policy={version}"
        self._emit(PipelineEventKind.POLICY_DECISION, invocation.call, detail=detail)
        return result

    @staticmethod
    def _normalize_success(execution: ToolExecution) -> ToolOutcome:
        artifact = execution.artifacts[0] if execution.artifacts else None
        if execution.exit_code is not None and execution.exit_code != 0:
            return ToolOutcome(
                status=ToolResultStatus.ERROR,
                content=execution.output,
                artifact_ref=artifact,
                error_kind="nonzero_exit",
                retryable=hint_for_kind("nonzero_exit").retryable,
                exit_code=execution.exit_code,
            )
        return ToolOutcome(
            status=ToolResultStatus.COMPLETED,
            content=execution.output,
            artifact_ref=artifact,
            exit_code=execution.exit_code,
        )

    def _denial_outcome(
        self, call: ToolCall, stage: str, result: PermissionResult
    ) -> ToolOutcome:
        reason = result.reason or "no reason given"
        if result.decision is PermissionDecision.REQUIRE_APPROVAL:
            outcome = ToolOutcome(
                status=ToolResultStatus.DENIED,
                content=(
                    f"denied at {stage}: approval required ({reason}); "
                    "no interactive approver is available, so the call was not executed"
                ),
                error_kind=f"{stage}_approval_required",
            )
        else:
            outcome = ToolOutcome(
                status=ToolResultStatus.DENIED,
                content=f"denied at {stage}: {reason}",
                error_kind=f"{stage}_denied",
            )
        self._emit(PipelineEventKind.TOOL_CALL_ERROR, call, detail=outcome.error_kind)
        return outcome

    def _reject(self, call: ToolCall, kind: str, message: str) -> ToolOutcome:
        """执行前拒绝（未知工具/参数非法/钩子否决）：不启动执行，不调用 after（无 invocation）。"""
        outcome = ToolOutcome(status=ToolResultStatus.ERROR, content=message, error_kind=kind)
        self._emit(PipelineEventKind.TOOL_CALL_ERROR, call, detail=kind)
        return outcome

    def _call_before(self, invocation: ToolInvocation) -> BeforeHookResult | None:
        if self._hooks is None:
            return None
        try:
            return self._hooks.before(invocation)
        except Exception as err:  # noqa: BLE001 - 钩子失败必须记录且不执行工具
            self._emit(
                PipelineEventKind.HOOK_ERROR,
                invocation.call,
                detail=f"before hook failed: {type(err).__name__}: {err}",
            )
            return BeforeHookResult(
                decision=PermissionDecision.DENY,
                reason=f"before hook raised {type(err).__name__}: {err}",
            )

    def _call_after(self, invocation: ToolInvocation, outcome: ToolOutcome) -> None:
        if self._hooks is None:
            return
        try:
            self._hooks.after(invocation, outcome)
        except Exception as err:  # noqa: BLE001 - 仅记录，不得重跑工具或改写结果
            self._emit(
                PipelineEventKind.HOOK_ERROR,
                invocation.call,
                detail=f"after hook failed: {type(err).__name__}: {err}",
            )

    def _emit(self, kind: PipelineEventKind, call: ToolCall, detail: str | None = None) -> None:
        if self._observer is not None:
            self._observer(
                PipelineEvent(kind=kind, tool_call_id=str(call.id), tool_name=call.name, detail=detail)
            )
