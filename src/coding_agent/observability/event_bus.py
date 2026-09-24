"""Event Bus：run 内有序事件的发布、订阅与重放（单机内存实现）。

- `emit`：为 run 分配单调唯一 seq，写入有界审计日志，再向订阅者扇出；
- 订阅：`subscribe(run_id?, types?, cursor?)` 返回订阅 ID；两种投递方式：
  内联 handler（同步调用、异常隔离并记录）与有界邮箱（`drain` 消费）；
- 背压：邮箱满时，可丢弃事件（`droppable`）被丢弃并计数；不可丢弃事件
  导致该订阅者断开（计数器记录）——慢订阅者不影响控制状态与其它订阅者；
- 重放：`events(run_id, after_seq)` 从审计日志读取；`cursor` 订阅从日志预填充邮箱；
- 订阅者失败绝不向上抛出（"订阅端失败不污染控制状态"）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from coding_agent.domain.events import AgentEvent, EventType
from coding_agent.domain.messages import new_id, utc_now_rfc3339

EventPayload = Mapping[str, Any]
EventHandler = Callable[[AgentEvent], None]

DEFAULT_AUDIT_LIMIT = 10_000
DEFAULT_QUEUE_LIMIT = 1_000

__all__ = ["EventBus", "SubscriptionInfo"]


@dataclass
class _Subscription:
    id: int
    run_id: str | None
    types: frozenset[EventType] | None
    handler: EventHandler | None
    queue_limit: int
    mailbox: deque[AgentEvent] = field(default_factory=deque)
    dropped: int = 0
    disconnected: bool = False

    def matches(self, event: AgentEvent) -> bool:
        if self.run_id is not None and event.run_id != self.run_id:
            return False
        if self.types is not None and event.type not in self.types:
            return False
        return True


@dataclass(frozen=True, slots=True)
class SubscriptionInfo:
    """订阅状态快照（供测试/诊断查询）。"""

    id: int
    run_id: str | None
    pending: int
    dropped: int
    disconnected: bool


class EventBus:
    """发布/订阅与审计；只承载已确认事实的通知。"""

    def __init__(
        self,
        *,
        audit_limit: int = DEFAULT_AUDIT_LIMIT,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        droppable: Sequence[EventType] | None = None,
    ) -> None:
        self._audit: deque[AgentEvent] = deque(maxlen=audit_limit)
        self._seq_by_run: dict[str, int] = {}
        self._subscriptions: dict[int, _Subscription] = {}
        self._next_subscription_id = 1
        self._queue_limit = queue_limit
        self._droppable = frozenset(droppable) if droppable is not None else frozenset()
        self._subscriber_errors: list[tuple[int, str]] = []

    # ---- 发布 ----

    def emit(
        self,
        event_type: EventType,
        *,
        run_id: str,
        session_id: str,
        payload: EventPayload | None = None,
    ) -> AgentEvent:
        """发布一条事件：分配 seq、写审计、扇出（订阅者失败被隔离）。"""
        seq = self._seq_by_run.get(run_id, 0) + 1
        self._seq_by_run[run_id] = seq
        event = AgentEvent(
            type=event_type,
            event_id=new_id("evt"),
            session_id=session_id,
            run_id=run_id,
            seq=seq,
            utc_time=utc_now_rfc3339(),
            payload=dict(payload or {}),
        )
        self._audit.append(event)
        for subscription in tuple(self._subscriptions.values()):
            if subscription.disconnected or not subscription.matches(event):
                continue
            if subscription.handler is not None:
                self._invoke_handler(subscription, event)
                continue
            if len(subscription.mailbox) >= subscription.queue_limit:
                if event.type in self._droppable:
                    subscription.dropped += 1
                    continue
                subscription.disconnected = True
                continue
            subscription.mailbox.append(event)
        return event

    def _invoke_handler(self, subscription: _Subscription, event: AgentEvent) -> None:
        assert subscription.handler is not None
        try:
            subscription.handler(event)
        except Exception as err:  # noqa: BLE001 - 订阅者失败隔离并记录
            self._subscriber_errors.append((subscription.id, f"{type(err).__name__}: {err}"))

    # ---- 订阅 ----

    def subscribe(
        self,
        *,
        run_id: str | None = None,
        types: Sequence[EventType] | None = None,
        cursor: int | None = None,
        handler: EventHandler | None = None,
        queue_limit: int | None = None,
    ) -> int:
        """订阅事件；cursor 为 run 内 seq（从该序号之后重放）。返回订阅 ID。"""
        subscription = _Subscription(
            id=self._next_subscription_id,
            run_id=run_id,
            types=frozenset(types) if types is not None else None,
            handler=handler,
            queue_limit=queue_limit or self._queue_limit,
        )
        self._next_subscription_id += 1
        if cursor is not None and run_id is not None:
            for event in self._audit:
                if event.run_id == run_id and event.seq > cursor and subscription.matches(event):
                    subscription.mailbox.append(event)
        self._subscriptions[subscription.id] = subscription
        return subscription.id

    def unsubscribe(self, subscription_id: int) -> None:
        self._subscriptions.pop(subscription_id, None)

    def drain(self, subscription_id: int, *, max_items: int | None = None) -> tuple[AgentEvent, ...]:
        """取出邮箱中的待处理事件（按序）；无订阅返回空。"""
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None:
            return ()
        taken: list[AgentEvent] = []
        while subscription.mailbox and (max_items is None or len(taken) < max_items):
            taken.append(subscription.mailbox.popleft())
        return tuple(taken)

    def subscription_info(self, subscription_id: int) -> SubscriptionInfo | None:
        subscription = self._subscriptions.get(subscription_id)
        if subscription is None:
            return None
        return SubscriptionInfo(
            id=subscription.id,
            run_id=subscription.run_id,
            pending=len(subscription.mailbox),
            dropped=subscription.dropped,
            disconnected=subscription.disconnected,
        )

    # ---- 审计与重放 ----

    def events(
        self,
        *,
        run_id: str | None = None,
        after_seq: int | None = None,
        types: Sequence[EventType] | None = None,
    ) -> tuple[AgentEvent, ...]:
        """从审计日志读取（可重放）；审计为有界保留。"""
        allowed = frozenset(types) if types is not None else None
        selected: list[AgentEvent] = []
        for event in self._audit:
            if run_id is not None and event.run_id != run_id:
                continue
            if after_seq is not None and event.seq <= after_seq:
                continue
            if allowed is not None and event.type not in allowed:
                continue
            selected.append(event)
        return tuple(selected)

    def subscriber_errors(self) -> tuple[tuple[int, str], ...]:
        """被隔离的订阅者异常记录（订阅 ID, 描述）。"""
        return tuple(self._subscriber_errors)

    @property
    def audit_size(self) -> int:
        return len(self._audit)
