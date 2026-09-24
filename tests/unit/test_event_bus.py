"""阶段 15 单测：EventBus 的序号、过滤、隔离、背压、重放与审计。"""

from __future__ import annotations

import pytest

from coding_agent.domain.events import EventType
from coding_agent.observability.event_bus import EventBus


def bus(**kwargs) -> EventBus:
    return EventBus(**kwargs)


class TestEmitBasics:
    def test_seq_monotonic_per_run_and_independent_between_runs(self) -> None:
        b = bus()
        first = b.emit(EventType.AGENT_START, run_id="r1", session_id="s1")
        second = b.emit(EventType.AGENT_END, run_id="r1", session_id="s1")
        other = b.emit(EventType.AGENT_START, run_id="r2", session_id="s2")
        assert (first.seq, second.seq) == (1, 2)
        assert other.seq == 1

    def test_event_fields(self) -> None:
        b = bus()
        event = b.emit(EventType.CONTEXT_BUILD, run_id="r1", session_id="s1", payload={"a": 1})
        assert event.event_id.startswith("evt_")
        assert event.utc_time.endswith("Z")
        assert event.payload_version == 1
        assert dict(event.payload) == {"a": 1}
        assert event.to_dict()["type"] == "context_build"


class TestSubscriptions:
    def test_type_and_run_filters(self) -> None:
        b = bus()
        only_errors = b.subscribe(run_id="r1", types=[EventType.ERROR])
        b.emit(EventType.AGENT_START, run_id="r1", session_id="s1")
        b.emit(EventType.ERROR, run_id="r1", session_id="s1")
        b.emit(EventType.ERROR, run_id="r2", session_id="s2")
        drained = b.drain(only_errors)
        assert [e.type for e in drained] == [EventType.ERROR]
        assert drained[0].run_id == "r1"

    def test_drain_in_order_and_one_shot(self) -> None:
        b = bus()
        sub = b.subscribe()
        for _ in range(3):
            b.emit(EventType.CONTEXT_BUILD, run_id="r1", session_id="s1")
        events = b.drain(sub)
        assert [e.seq for e in events] == [1, 2, 3]
        assert b.drain(sub) == ()

    def test_handler_receives_inline(self) -> None:
        b = bus()
        seen: list = []
        b.subscribe(handler=seen.append)
        b.emit(EventType.AGENT_START, run_id="r1", session_id="s1")
        assert len(seen) == 1

    def test_handler_exception_isolated_and_recorded(self) -> None:
        b = bus()
        seen: list = []

        def bad(event) -> None:
            raise RuntimeError("subscriber boom")

        b.subscribe(handler=bad)
        b.subscribe(handler=seen.append)
        b.emit(EventType.AGENT_START, run_id="r1", session_id="s1")  # 不抛出
        assert len(seen) == 1  # 其它订阅者不受影响
        errors = b.subscriber_errors()
        assert len(errors) == 1
        assert "subscriber boom" in errors[0][1]

    def test_unsubscribe_stops_delivery(self) -> None:
        b = bus()
        sub = b.subscribe()
        b.emit(EventType.AGENT_START, run_id="r1", session_id="s1")
        b.unsubscribe(sub)
        b.emit(EventType.AGENT_END, run_id="r1", session_id="s1")
        assert b.drain(sub) == ()
        assert b.subscription_info(sub) is None


class TestBackpressure:
    def test_overflow_disconnects_non_droppable_subscriber(self) -> None:
        b = bus(queue_limit=2)
        sub = b.subscribe()
        for _ in range(3):
            b.emit(EventType.CONTEXT_BUILD, run_id="r1", session_id="s1")
        info = b.subscription_info(sub)
        assert info is not None
        assert info.disconnected is True
        assert info.pending == 2  # 断开前已入队的仍可取出
        b.emit(EventType.AGENT_END, run_id="r1", session_id="s1")
        assert len(b.drain(sub)) == 2

    def test_droppable_events_dropped_without_disconnect(self) -> None:
        b = bus(queue_limit=2, droppable=[EventType.LLM_REQUEST_START])
        sub = b.subscribe()
        for _ in range(5):
            b.emit(EventType.LLM_REQUEST_START, run_id="r1", session_id="s1")
        info = b.subscription_info(sub)
        assert info is not None
        assert info.disconnected is False
        assert info.dropped == 3
        assert len(b.drain(sub)) == 2

    def test_slow_subscriber_does_not_block_control_flow(self) -> None:
        b = bus(queue_limit=1)
        b.subscribe()
        # 控制路径继续发布不受阻
        b.emit(EventType.CONTEXT_BUILD, run_id="r1", session_id="s1")
        b.emit(EventType.CONTEXT_BUILD, run_id="r1", session_id="s1")
        b.emit(EventType.AGENT_END, run_id="r1", session_id="s1")


class TestReplayAndAudit:
    def test_cursor_prefills_mailbox(self) -> None:
        b = bus()
        for _ in range(3):
            b.emit(EventType.CONTEXT_BUILD, run_id="r1", session_id="s1")
        sub = b.subscribe(run_id="r1", cursor=1)
        replayed = b.drain(sub)
        assert [e.seq for e in replayed] == [2, 3]

    def test_events_slice_by_seq_and_type(self) -> None:
        b = bus()
        b.emit(EventType.AGENT_START, run_id="r1", session_id="s1")
        b.emit(EventType.ERROR, run_id="r1", session_id="s1")
        b.emit(EventType.AGENT_END, run_id="r1", session_id="s1")
        after = b.events(run_id="r1", after_seq=1)
        assert [e.seq for e in after] == [2, 3]
        errors = b.events(types=[EventType.ERROR])
        assert len(errors) == 1

    def test_audit_is_bounded(self) -> None:
        b = bus(audit_limit=2)
        for _ in range(4):
            b.emit(EventType.CONTEXT_BUILD, run_id="r1", session_id="s1")
        assert b.audit_size == 2
        assert [e.seq for e in b.events(run_id="r1")] == [3, 4]
