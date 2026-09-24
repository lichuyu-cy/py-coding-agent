"""HTTP 入参/出参模型（Pydantic 仅承担 HTTP 边界校验，不进入核心 Runtime）。"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "AbortRequest",
    "ErrorBody",
    "ErrorEnvelope",
    "RunAccepted",
    "RunInfo",
    "RunRequest",
    "SessionCreated",
    "SessionInfo",
]


class RunRequest(BaseModel):
    task: str = Field(min_length=1)
    skills: list[str] | None = None


class AbortRequest(BaseModel):
    reason: str | None = None


class SessionCreated(BaseModel):
    session_id: str


class SessionInfo(BaseModel):
    session_id: str
    workspace: str | None
    message_count: int
    tail_type: str | None
    pending_follow_ups: int
    active_run_id: str | None


class RunAccepted(BaseModel):
    run_id: str
    session_id: str


class RunInfo(BaseModel):
    run_id: str
    session_id: str | None
    active: bool
    completed: bool
    final_status: str | None
    limit_hit: str | None
    turns: int
    tool_calls: int
    duration_seconds: float | None


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorBody
