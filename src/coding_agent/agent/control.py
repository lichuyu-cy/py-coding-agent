"""运行时控制：取消令牌、Steering / Follow-up 队列。

阶段 09 为内存队列（阶段 19 持久化队列位置，阶段 20 支持崩溃恢复）：
- `CancelToken`：协作式取消；实现 ports.provider.CancelSignal；幂等 cancel；
- `SteeringQueue`：当前 run 的高优先级控制消息，按序一次性 drain 注入下一次模型请求；
- `FollowUpQueue`：会话级的下一个用户任务队列，只在 turn 结束边界取出一个；
- `RunControl`：一次 run 的控制通道（run_id + cancel + steering）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import new_id, utc_now_rfc3339

__all__ = [
    "AbortReason",
    "CancelToken",
    "ControlError",
    "FollowUpItem",
    "FollowUpQueue",
    "QueueLimitExceededError",
    "RunControl",
    "SteeringMessage",
    "SteeringQueue",
    "SteeringRejectedError",
    "UnknownRunError",
]

DEFAULT_QUEUE_LIMIT = 20


class AbortReason(StrEnum):
    USER_REQUEST = "user_request"
    DEADLINE = "deadline"
    SHUTDOWN = "shutdown"


class ControlError(HarnessError):
    """控制通道错误基类。"""


class SteeringRejectedError(ControlError):
    """run 不存在或已结束时的 steering 提交（协议要求显式拒绝，不得静默丢失）。"""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"cannot submit steering: run {run_id!r} is not active; "
            "use submit_follow_up for a new task"
        )
        self.run_id = run_id


class UnknownRunError(ControlError):
    """引用了非活动 run。"""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"unknown or finished run {run_id!r}")
        self.run_id = run_id


class QueueLimitExceededError(ControlError):
    """队列达到上限（按最大任务数限制）。"""

    def __init__(self, kind: str, limit: int) -> None:
        super().__init__(f"{kind} queue is full (limit {limit})")
        self.kind = kind
        self.limit = limit


class CancelToken:
    """协作式取消信号（幂等）；对象只表达“已请求取消”，清理由 Runtime/Loop 完成。"""

    def __init__(self) -> None:
        self._cancelled = False
        self._reason: str | None = None

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: AbortReason | str) -> bool:
        """请求取消；返回本次调用是否“首次”触发（重复调用为幂等 False）。"""
        if self._cancelled:
            return False
        self._cancelled = True
        self._reason = str(reason)
        return True


@dataclass(frozen=True, slots=True)
class SteeringMessage:
    id: str
    text: str
    created_at: str
    sequence: int


@dataclass(frozen=True, slots=True)
class FollowUpItem:
    id: str
    text: str
    created_at: str
    sequence: int


class SteeringQueue:
    """当前 run 的 steering 队列：先到先注入，drain 为一次性取空。"""

    def __init__(self, *, max_pending: int = DEFAULT_QUEUE_LIMIT) -> None:
        self._max_pending = max_pending
        self._pending: list[SteeringMessage] = []
        self._by_client_event: dict[str, SteeringMessage] = {}
        self._sequence = 0

    def enqueue(self, text: str, *, client_event_id: str | None = None) -> SteeringMessage:
        """入队（幂等：相同 client_event_id 返回既有消息，不重复入队）。"""
        if client_event_id is not None and client_event_id in self._by_client_event:
            return self._by_client_event[client_event_id]
        self._validate_text(text)
        if len(self._pending) >= self._max_pending:
            raise QueueLimitExceededError("steering", self._max_pending)
        self._sequence += 1
        message = SteeringMessage(
            id=new_id("steer"), text=text, created_at=utc_now_rfc3339(), sequence=self._sequence
        )
        self._pending.append(message)
        if client_event_id is not None:
            self._by_client_event[client_event_id] = message
        return message

    def drain_before_next_request(self) -> tuple[SteeringMessage, ...]:
        """一次性取出全部待注入消息（按入队顺序）；重复调用返回空。"""
        drained = tuple(self._pending)
        self._pending.clear()
        return drained

    def has_pending(self) -> bool:
        return bool(self._pending)

    def pending_count(self) -> int:
        return len(self._pending)

    @staticmethod
    def _validate_text(text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ControlError("steering text must be a non-empty string")


class FollowUpQueue:
    """会话级的 follow-up 队列：一个 turn 结束只取出一个。"""

    def __init__(self, *, max_pending: int = DEFAULT_QUEUE_LIMIT) -> None:
        self._max_pending = max_pending
        self._pending: list[FollowUpItem] = []
        self._by_client_event: dict[str, FollowUpItem] = {}
        self._sequence = 0

    def enqueue(self, text: str, *, client_event_id: str | None = None) -> FollowUpItem:
        if client_event_id is not None and client_event_id in self._by_client_event:
            return self._by_client_event[client_event_id]
        if not isinstance(text, str) or not text.strip():
            raise ControlError("follow-up text must be a non-empty string")
        if len(self._pending) >= self._max_pending:
            raise QueueLimitExceededError("follow-up", self._max_pending)
        self._sequence += 1
        item = FollowUpItem(id=new_id("follow"), text=text, created_at=utc_now_rfc3339(), sequence=self._sequence)
        self._pending.append(item)
        if client_event_id is not None:
            self._by_client_event[client_event_id] = item
        return item

    def dequeue_after_turn_end(self) -> FollowUpItem | None:
        """turn 结束边界取出下一个任务；空队列返回 None。"""
        if not self._pending:
            return None
        return self._pending.pop(0)

    def pending_count(self) -> int:
        return len(self._pending)


class RunControl:
    """一次 run 的控制通道；由 Runtime 创建并在 run 结束后关闭（从活动表移除）。"""

    def __init__(self, run_id: str, session_id: str = "") -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.cancel = CancelToken()
        self.steering = SteeringQueue()
