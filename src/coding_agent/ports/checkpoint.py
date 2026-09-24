"""Checkpoint/Recovery 端口：恢复边界的持久索引与未完成意图。

- `Checkpoint` 是 Session 的恢复索引（不复制历史，只用 session_version 链接会话事实）；
- `ToolIntent` 追踪每次工具调用的执行意图（planned → started → completed）：
  崩溃后依据它区分「确定未执行」（可安全补记）与「副作用未知」（必须人工核对）；
- `CheckpointStore` 提供保存/读取与意图状态机；
- `RecoveryDecision`/`RecoveryPlan` 是恢复判定的输出契约（模块 25）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "OPEN_INTENT_STATUSES",
    "BoundaryEvent",
    "BoundaryKind",
    "Checkpoint",
    "CheckpointStore",
    "IntentStatus",
    "RecoveryDecision",
    "RecoveryPlan",
    "ToolExecutionStart",
    "ToolIntent",
]


class BoundaryKind(StrEnum):
    """安全恢复边界（模块 24：模型完整响应/工具结果/压缩完成后建立检查点）。"""

    USER_TASK = "user_task"
    MODEL_RESPONSE = "model_response"
    TOOL_RESULT = "tool_result"
    COMPACTION = "compaction"
    RUN_END = "run_end"
    RECOVERY = "recovery"


class IntentStatus(StrEnum):
    """工具意图状态机：planned → started → completed；人工核对后 resolved/abandoned。"""

    PLANNED = "planned"
    STARTED = "started"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"
    RESOLVED = "resolved"
    ABANDONED = "abandoned"


OPEN_INTENT_STATUSES = frozenset({IntentStatus.PLANNED, IntentStatus.STARTED})


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """一次 run 的安全恢复边界索引。"""

    checkpoint_id: str
    session_id: str
    session_version: int
    state_seq: int
    boundary: BoundaryKind
    pending_intent_ids: tuple[str, ...]
    workspace_fingerprint: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ToolIntent:
    """单个工具调用的执行意图记录（journal 行）。"""

    intent_id: str
    session_id: str
    run_id: str
    call_id: str
    tool_name: str
    arguments_digest: str
    status: IntentStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class BoundaryEvent:
    """Loop 在提交/注入边界产生的检查点事件（由 Runtime 落库）。"""

    boundary: BoundaryKind
    state_seq: int
    run_id: str
    session_id: str
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolExecutionStart:
    """Loop 在实际执行工具前的意图标记（planned → started）。"""

    run_id: str
    session_id: str
    call_id: str


class RecoveryDecision(StrEnum):
    """恢复判定：安全续跑 / 人工核对 / 停止。"""

    RESUME = "resume"
    RECONCILE = "reconcile"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    """`inspect` 的输出：说清会话版本、上一个安全边界与尚未确定的副作用。"""

    session_id: str
    decision: RecoveryDecision
    reason: str
    session_version: int
    safe_boundary: Checkpoint | None
    pending_intents: tuple[ToolIntent, ...] = ()
    uncertain_intents: tuple[ToolIntent, ...] = ()
    workspace_fingerprint: str | None = None


class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...

    def latest(self, session_id: str) -> Checkpoint | None: ...

    def record_intent(self, intent: ToolIntent) -> None: ...

    def mark_started(self, session_id: str, call_id: str) -> None: ...

    def complete_intent(self, session_id: str, call_id: str) -> None: ...

    def set_intent_status(self, session_id: str, call_id: str, status: IntentStatus) -> None: ...

    def open_intents(self, session_id: str) -> tuple[ToolIntent, ...]: ...

    def all_intents(self, session_id: str) -> tuple[ToolIntent, ...]: ...

    def close(self) -> None: ...
