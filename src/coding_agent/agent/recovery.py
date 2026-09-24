"""Recovery：崩溃/中断后的确定性恢复判定（模块 25）。

原则：
- 仅恢复已证实的持久事实，不从内存对象猜测；
- 工具「确定未开始」（intent=planned）→ 可安全补记「未执行」结果（RESUME）；
- 工具「执行状态未知」（intent=started）→ 必须人工核对（RECONCILE），
  绝不自动重放写/删/命令；
- 指纹不匹配、悬空意图、历史与 journal 不一致 → STOP（需人工检查）。

本模块只读/写 SessionStore 与 CheckpointStore；执行恢复后的新请求由 Runtime 负责。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import (
    MessageMeta,
    ToolCallId,
    ToolResult,
    ToolResultStatus,
    message_to_dict,
    new_message_id,
    utc_now_rfc3339,
)
from coding_agent.ports.checkpoint import (
    BoundaryKind,
    Checkpoint,
    CheckpointStore,
    IntentStatus,
    RecoveryDecision,
    RecoveryPlan,
    ToolIntent,
)
from coding_agent.ports.store import SessionAppend, SessionRecord, SessionStore

__all__ = [
    "RecoveryEngine",
    "RecoveryError",
    "RecoveryOutcome",
    "UnknownIntentError",
    "workspace_fingerprint",
]

_NOT_EXECUTED_CONTENT = "recovery: tool call was not executed before the process stopped"
_EXECUTED_UNKNOWN_CONTENT = "recovery: tool executed, outcome unknown; resolved by operator"


class RecoveryError(HarnessError):
    """恢复流程错误（非法决策顺序、未知意图等）。"""


class UnknownIntentError(RecoveryError):
    def __init__(self, intent_id: str) -> None:
        super().__init__(f"unknown tool intent {intent_id!r}")
        self.intent_id = intent_id


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """resume/resolve_uncertain 的结果：本次补写的消息与解决的意图。"""

    session_id: str
    decision: RecoveryDecision
    appended_message_ids: tuple[str, ...]
    resolved_intent_ids: tuple[str, ...]
    session_version: int


def workspace_fingerprint(workspace: str | Path) -> str:
    """工作区指纹：规范化路径的 SHA-256（与内容无关，工具修改文件不影响）。"""
    path = Path(workspace)
    try:
        canonical = str(path.resolve())
    except OSError:  # pragma: no cover - 无法规范化时退回原样
        canonical = str(path)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"ws1:{digest[:32]}"


class RecoveryEngine:
    """恢复判定与安全补记；所有决策基于已提交事实与意图 journal。"""

    def __init__(self, sessions: SessionStore, checkpoints: CheckpointStore) -> None:
        self._sessions = sessions
        self._checkpoints = checkpoints

    # ---- 判定 ----

    def inspect(self, session_id: str, *, workspace: str | Path | None = None) -> RecoveryPlan:
        """检查会话状态并给出确定决策；无法判定时说明需人工检查的原因。"""
        record = self._sessions.load(session_id)
        checkpoint = self._checkpoints.latest(session_id)
        intents = self._checkpoints.all_intents(session_id)
        intent_by_call = {intent.call_id: intent for intent in intents}

        if workspace is not None and checkpoint is not None:
            current = workspace_fingerprint(workspace)
            if checkpoint.workspace_fingerprint and checkpoint.workspace_fingerprint != current:
                return self._plan(
                    record,
                    checkpoint,
                    RecoveryDecision.STOP,
                    "workspace fingerprint mismatch: checkpoint was taken in a different workspace",
                )

        # 会话尾部的未解决调用（历史事实为准）
        open_calls = _open_call_ids(record)
        # 悬空意图：journal 有记录但历史里找不到对应调用 → 数据不一致
        all_calls = _all_call_ids(record)
        for intent in intents:
            if intent.call_id not in all_calls:
                return self._plan(
                    record,
                    checkpoint,
                    RecoveryDecision.STOP,
                    f"dangling intent {intent.intent_id!r}: no matching tool call in committed history",
                )

        uncertain: list[ToolIntent] = []
        pending: list[ToolIntent] = []
        for call_id in open_calls:
            intent = intent_by_call.get(call_id)
            if intent is None:
                # 历史上存在未解决调用但没有意图证据：无法断定是否执行过
                return self._plan(
                    record,
                    checkpoint,
                    RecoveryDecision.STOP,
                    f"tool call {call_id!r} has no intent journal entry; refusing to guess",
                )
            if intent.status is IntentStatus.STARTED:
                uncertain.append(intent)
            elif intent.status is IntentStatus.PLANNED:
                pending.append(intent)
            else:  # completed/uncertain/resolved/abandoned 但历史仍无结果 → 不一致
                return self._plan(
                    record,
                    checkpoint,
                    RecoveryDecision.STOP,
                    f"tool call {call_id!r} is unresolved but its intent is {intent.status};"
                    " history and journal disagree",
                )

        if uncertain:
            return self._plan(
                record,
                checkpoint,
                RecoveryDecision.RECONCILE,
                f"{len(uncertain)} tool call(s) started but their outcome is unknown;"
                " manual reconciliation required",
                uncertain=tuple(uncertain),
                pending=tuple(pending),
            )
        return self._plan(
            record,
            checkpoint,
            RecoveryDecision.RESUME,
            "no uncertain side effects; safe to resume from committed history",
            pending=tuple(pending),
        )

    def _plan(
        self,
        record: SessionRecord,
        checkpoint: Checkpoint | None,
        decision: RecoveryDecision,
        reason: str,
        *,
        pending: tuple[ToolIntent, ...] = (),
        uncertain: tuple[ToolIntent, ...] = (),
    ) -> RecoveryPlan:
        return RecoveryPlan(
            session_id=record.session_id,
            decision=decision,
            reason=reason,
            session_version=record.version,
            safe_boundary=checkpoint,
            pending_intents=pending,
            uncertain_intents=uncertain,
            workspace_fingerprint=checkpoint.workspace_fingerprint if checkpoint else None,
        )

    # ---- 执行恢复 ----

    def resume(self, plan: RecoveryPlan) -> RecoveryOutcome:
        """对确定未执行的调用补记「未执行」结果；RECONCILE/STOP 计划显式拒绝。"""
        if plan.decision is not RecoveryDecision.RESUME:
            raise RecoveryError(
                f"cannot auto-resume a {plan.decision} plan: {plan.reason}"
            )
        if not plan.pending_intents:
            return RecoveryOutcome(
                session_id=plan.session_id,
                decision=RecoveryDecision.RESUME,
                appended_message_ids=(),
                resolved_intent_ids=(),
                session_version=plan.session_version,
            )
        return self._record_results(
            plan.session_id,
            plan.pending_intents,
            status=ToolResultStatus.CANCELLED,
            content=_NOT_EXECUTED_CONTENT,
            error_kind="recovery_not_executed",
            retryable=True,
            new_status=IntentStatus.ABANDONED,
        )

    def resolve_uncertain(
        self, session_id: str, intent_id: str, decision: str
    ) -> RecoveryOutcome:
        """人工核对「执行状态未知」的调用：

        - `not_executed`：确认未执行 → 补记「未执行」结果（可继续运行）；
        - `executed_unknown`：确认已执行但结果丢失 → 补记 UNCERTAIN 结果（历史显式标记）。
        """
        if decision not in ("not_executed", "executed_unknown"):
            raise RecoveryError(
                f"unknown reconciliation decision {decision!r};"
                " expected 'not_executed' or 'executed_unknown'"
            )
        intent = next(
            (item for item in self._checkpoints.all_intents(session_id) if item.intent_id == intent_id),
            None,
        )
        if intent is None:
            raise UnknownIntentError(intent_id)
        if intent.status is not IntentStatus.STARTED:
            raise RecoveryError(
                f"intent {intent_id!r} is {intent.status}, not awaiting reconciliation"
            )
        if decision == "not_executed":
            return self._record_results(
                session_id,
                (intent,),
                status=ToolResultStatus.CANCELLED,
                content=_NOT_EXECUTED_CONTENT,
                error_kind="recovery_not_executed",
                retryable=True,
                new_status=IntentStatus.ABANDONED,
            )
        return self._record_results(
            session_id,
            (intent,),
            status=ToolResultStatus.UNCERTAIN,
            content=_EXECUTED_UNKNOWN_CONTENT,
            error_kind="recovery_executed_unknown",
            retryable=None,
            new_status=IntentStatus.RESOLVED,
        )

    # ---- 内部 ----

    def _record_results(
        self,
        session_id: str,
        intents: tuple[ToolIntent, ...],
        *,
        status: ToolResultStatus,
        content: str,
        error_kind: str,
        retryable: bool | None,
        new_status: IntentStatus,
    ) -> RecoveryOutcome:
        record = self._sessions.load(session_id)
        payloads: list[Mapping[str, Any]] = []
        for intent in intents:
            assistant_meta = _assistant_meta_for(record, intent.call_id)
            turn_id = assistant_meta.turn_id if assistant_meta is not None else 0
            result = ToolResult(
                meta=MessageMeta(
                    id=new_message_id(),
                    session_id=session_id,
                    run_id=intent.run_id,
                    turn_id=turn_id,
                    created_at=utc_now_rfc3339(),
                ),
                tool_call_id=ToolCallId(intent.call_id),
                status=status,
                content=content,
                error_kind=error_kind,
                retryable=retryable,
            )
            payloads.append(message_to_dict(result))
        new_version = self._sessions.append(
            session_id, record.version, SessionAppend(messages=tuple(payloads))
        )
        for intent in intents:
            self._checkpoints.set_intent_status(session_id, intent.call_id, new_status)
        checkpoint = self._checkpoints.latest(session_id)
        self._checkpoints.save(
            Checkpoint(
                checkpoint_id=f"ckpt_recovery_{new_version}",
                session_id=session_id,
                session_version=new_version,
                state_seq=checkpoint.state_seq if checkpoint is not None else 0,
                boundary=BoundaryKind.RECOVERY,
                pending_intent_ids=tuple(
                    item.intent_id for item in self._checkpoints.open_intents(session_id)
                ),
                workspace_fingerprint=(
                    checkpoint.workspace_fingerprint if checkpoint is not None else ""
                ),
                created_at=utc_now_rfc3339(),
            )
        )
        return RecoveryOutcome(
            session_id=session_id,
            decision=RecoveryDecision.RESUME,
            appended_message_ids=tuple(str(payload["meta"]["id"]) for payload in payloads),
            resolved_intent_ids=tuple(intent.intent_id for intent in intents),
            session_version=new_version,
        )


def _open_call_ids(record: SessionRecord) -> tuple[str, ...]:
    """已提交历史中「有调用但无结果」的 tool call（按提交顺序）。"""
    resolved = {
        str(message["tool_call_id"])
        for message in record.messages
        if message.get("type") == "tool_result"
    }
    open_calls: list[str] = []
    for message in record.messages:
        if message.get("type") != "assistant":
            continue
        for call in message.get("tool_calls", ()):
            call_id = str(call["id"])
            if call_id not in resolved:
                open_calls.append(call_id)
    return tuple(open_calls)


def _all_call_ids(record: SessionRecord) -> frozenset[str]:
    calls: set[str] = set()
    for message in record.messages:
        if message.get("type") == "assistant":
            for call in message.get("tool_calls", ()):
                calls.add(str(call["id"]))
    return frozenset(calls)


def _assistant_meta_for(record: SessionRecord, call_id: str) -> MessageMeta | None:
    for message in record.messages:
        if message.get("type") != "assistant":
            continue
        for call in message.get("tool_calls", ()):
            if str(call["id"]) == call_id:
                return MessageMeta.from_dict(message["meta"])
    return None
