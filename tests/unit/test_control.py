"""阶段 09 单测：取消令牌与 steering / follow-up 队列。"""

from __future__ import annotations

import pytest

from coding_agent.agent.control import (
    AbortReason,
    CancelToken,
    ControlError,
    FollowUpQueue,
    QueueLimitExceededError,
    SteeringQueue,
)


class TestCancelToken:
    def test_cancel_is_idempotent(self) -> None:
        token = CancelToken()
        assert token.is_cancelled is False
        assert token.cancel(AbortReason.USER_REQUEST) is True
        assert token.is_cancelled is True
        assert token.reason == "user_request"
        assert token.cancel("again") is False  # 幂等
        assert token.reason == "user_request"  # 首次原因保留


class TestSteeringQueue:
    def test_enqueue_order_and_sequence(self) -> None:
        queue = SteeringQueue()
        first = queue.enqueue("one")
        second = queue.enqueue("two")
        assert (first.sequence, second.sequence) == (1, 2)
        assert queue.pending_count() == 2
        assert queue.has_pending() is True

    def test_drain_is_one_shot_in_order(self) -> None:
        queue = SteeringQueue()
        queue.enqueue("one")
        queue.enqueue("two")
        drained = queue.drain_before_next_request()
        assert [m.text for m in drained] == ["one", "two"]
        assert queue.drain_before_next_request() == ()
        assert queue.has_pending() is False

    def test_idempotent_by_client_event_id(self) -> None:
        queue = SteeringQueue()
        first = queue.enqueue("one", client_event_id="evt_1")
        second = queue.enqueue("one", client_event_id="evt_1")
        assert first is second
        assert queue.pending_count() == 1

    def test_empty_text_rejected(self) -> None:
        queue = SteeringQueue()
        with pytest.raises(ControlError):
            queue.enqueue("   ")

    def test_queue_limit(self) -> None:
        queue = SteeringQueue(max_pending=1)
        queue.enqueue("one")
        with pytest.raises(QueueLimitExceededError):
            queue.enqueue("two")


class TestFollowUpQueue:
    def test_dequeue_one_per_turn_end(self) -> None:
        queue = FollowUpQueue()
        queue.enqueue("first")
        queue.enqueue("second")
        assert queue.dequeue_after_turn_end().text == "first"
        assert queue.dequeue_after_turn_end().text == "second"
        assert queue.dequeue_after_turn_end() is None

    def test_idempotent_by_client_event_id(self) -> None:
        queue = FollowUpQueue()
        first = queue.enqueue("go", client_event_id="evt_9")
        second = queue.enqueue("go", client_event_id="evt_9")
        assert first is second
        assert queue.pending_count() == 1

    def test_limit(self) -> None:
        queue = FollowUpQueue(max_pending=1)
        queue.enqueue("one")
        with pytest.raises(QueueLimitExceededError):
            queue.enqueue("two")

    def test_empty_text_rejected(self) -> None:
        queue = FollowUpQueue()
        with pytest.raises(ControlError):
            queue.enqueue("")
