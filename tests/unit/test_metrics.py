"""阶段 16 单测：指标聚合（去重/未知 usage/负时长）与脱敏日志。"""

from __future__ import annotations

from coding_agent.domain.events import AgentEvent, EventType
from coding_agent.observability.logging import MASK, MAX_VALUE_CHARS, StructuredLogger, sanitize_fields
from coding_agent.observability.metrics import MetricsAccumulator

RUN = "run_1"
SESSION = "sess_1"


def make_event(
    event_type: EventType,
    *,
    seq: int,
    payload: dict | None = None,
    event_id: str | None = None,
    utc_time: str = "2026-09-24T00:00:00.000000Z",
) -> AgentEvent:
    return AgentEvent(
        type=event_type,
        event_id=event_id or f"evt_{seq}",
        session_id=SESSION,
        run_id=RUN,
        seq=seq,
        utc_time=utc_time,
        payload=payload or {},
    )


class TestMetricsCounting:
    def test_full_trajectory_counts(self) -> None:
        acc = MetricsAccumulator()
        acc.on_event(make_event(EventType.AGENT_START, seq=1, utc_time="2026-09-24T00:00:00.000000Z"))
        acc.on_event(make_event(EventType.LLM_REQUEST_START, seq=2))
        acc.on_event(
            make_event(
                EventType.LLM_REQUEST_END,
                seq=3,
                payload={"usage_input_tokens": 10, "usage_output_tokens": 4},
            )
        )
        acc.on_event(make_event(EventType.TOOL_CALL_START, seq=4))
        acc.on_event(make_event(EventType.TOOL_CALL_END, seq=5))
        acc.on_event(make_event(EventType.STEERING, seq=6))
        acc.on_event(make_event(EventType.FOLLOW_UP, seq=7))
        acc.on_event(make_event(EventType.COMPACTION_END, seq=8, payload={"token_before": 100, "token_after": 40}))
        acc.on_event(make_event(EventType.ERROR, seq=9, payload={"stage": "provider"}))
        acc.on_event(
            make_event(
                EventType.AGENT_END,
                seq=10,
                payload={"status": "finished", "turns": 2, "limit_hit": None},
                utc_time="2026-09-24T00:00:05.000000Z",
            )
        )
        snapshot = acc.snapshot(RUN)
        assert snapshot is not None
        metrics = snapshot.metrics
        assert metrics.llm_requests_started == 1 and metrics.llm_requests_completed == 1
        assert metrics.input_tokens == 10 and metrics.output_tokens == 4
        assert metrics.tool_calls_started == 1 and metrics.tool_calls_completed == 1
        assert metrics.steering_count == 1 and metrics.follow_up_count == 1
        assert metrics.compactions == 1
        assert metrics.compaction_tokens_before == 100 and metrics.compaction_tokens_after == 40
        assert dict(metrics.errors_by_stage) == {"provider": 1}
        assert metrics.turns == 2 and metrics.final_status == "finished"
        assert metrics.completed is True
        assert metrics.duration_seconds == 5.0
        assert metrics.events_seen == 10
        assert acc.persisted() and acc.persisted()[0].run_id == RUN  # 终结时自动输出

    def test_duplicate_delivery_is_idempotent(self) -> None:
        acc = MetricsAccumulator()
        first = make_event(EventType.TOOL_CALL_START, seq=1, event_id="evt_fixed")
        acc.on_event(first)
        acc.on_event(first)  # 重复投递（同 event_id）
        acc.on_event(make_event(EventType.TOOL_CALL_START, seq=1, event_id="evt_fixed"))
        snapshot = acc.snapshot(RUN)
        assert snapshot is not None
        assert snapshot.metrics.tool_calls_started == 1

    def test_unknown_usage_not_counted_as_zero(self) -> None:
        acc = MetricsAccumulator()
        acc.on_event(make_event(EventType.LLM_REQUEST_END, seq=1, payload={"usage_input_tokens": None, "usage_output_tokens": None}))
        snapshot = acc.snapshot(RUN)
        assert snapshot is not None
        assert snapshot.metrics.input_tokens is None
        assert snapshot.metrics.output_tokens is None
        assert snapshot.metrics.usage_unknown_requests == 1

    def test_mixed_usage_sums_known_only(self) -> None:
        acc = MetricsAccumulator()
        acc.on_event(make_event(EventType.LLM_REQUEST_END, seq=1, payload={"usage_input_tokens": 5, "usage_output_tokens": 2}))
        acc.on_event(make_event(EventType.LLM_REQUEST_END, seq=2, payload={"usage_input_tokens": None, "usage_output_tokens": None}))
        snapshot = acc.snapshot(RUN)
        assert snapshot is not None
        assert snapshot.metrics.input_tokens == 5
        assert snapshot.metrics.usage_unknown_requests == 1

    def test_cancelled_tool_calls_counted_separately(self) -> None:
        acc = MetricsAccumulator()
        acc.on_event(make_event(EventType.TOOL_CALL_ERROR, seq=1, payload={"status": "cancelled"}))
        acc.on_event(make_event(EventType.TOOL_CALL_ERROR, seq=2, payload={"status": "error"}))
        snapshot = acc.snapshot(RUN)
        assert snapshot is not None
        assert snapshot.metrics.tool_calls_cancelled == 1
        assert snapshot.metrics.tool_calls_failed == 1

    def test_negative_duration_protected(self) -> None:
        acc = MetricsAccumulator()
        acc.on_event(make_event(EventType.AGENT_START, seq=1, utc_time="2026-09-24T00:00:10.000000Z"))
        acc.on_event(make_event(EventType.AGENT_END, seq=2, utc_time="2026-09-24T00:00:05.000000Z", payload={"status": "finished"}))
        snapshot = acc.snapshot(RUN)
        assert snapshot is not None
        assert snapshot.metrics.duration_seconds is None
        assert "negative_duration" in snapshot.metrics.anomalies

    def test_unknown_run_snapshot_none(self) -> None:
        assert MetricsAccumulator().snapshot("missing") is None

    def test_reordered_delivery_counts_commutatively(self) -> None:
        acc = MetricsAccumulator()
        acc.on_event(make_event(EventType.TOOL_CALL_END, seq=2))
        acc.on_event(make_event(EventType.TOOL_CALL_START, seq=1))
        snapshot = acc.snapshot(RUN)
        assert snapshot is not None
        assert snapshot.metrics.tool_calls_started == 1
        assert snapshot.metrics.tool_calls_completed == 1


class TestSanitization:
    def test_sensitive_keys_masked_recursively(self) -> None:
        sanitized = sanitize_fields(
            {
                "api_key": "sk-live-secret",
                "Authorization": "Bearer xyz",
                "nested": {"github_token": "ghp_x", "ok": "value"},
                "list": [{"password": "pw"}, "plain"],
            }
        )
        assert sanitized["api_key"] == MASK
        assert sanitized["Authorization"] == MASK
        assert sanitized["nested"]["github_token"] == MASK
        assert sanitized["nested"]["ok"] == "value"
        assert sanitized["list"][0]["password"] == MASK
        assert sanitized["list"][1] == "plain"

    def test_long_values_truncated_with_marker(self) -> None:
        long_text = "x" * (MAX_VALUE_CHARS + 50)
        sanitized = sanitize_fields({"output": long_text})
        assert sanitized["output"].startswith("x" * 100)
        assert "truncated 50 chars" in sanitized["output"]
        assert len(sanitized["output"]) < len(long_text)

    def test_bytes_summarized(self) -> None:
        assert sanitize_fields({"blob": b"\x00\x01"})["blob"] == "<2 bytes>"


class TestStructuredLogger:
    def test_records_with_correlation(self) -> None:
        logger = StructuredLogger()
        record = logger.info(
            "run started",
            correlation={"run_id": RUN, "session_id": SESSION},
            fields={"task_chars": 12},
        )
        assert record.level == "info"
        assert record.correlation["run_id"] == RUN
        assert logger.records()[-1] is record
        assert record.utc_time.endswith("Z")

    def test_sink_failure_counted_not_raised(self) -> None:
        def bad_sink(record) -> None:
            raise RuntimeError("sink down")

        logger = StructuredLogger(sink=bad_sink)
        logger.error("still works")  # 不抛出
        assert logger.failures == 1

    def test_max_records_bounded(self) -> None:
        logger = StructuredLogger(max_records=2)
        for index in range(4):
            logger.debug(f"message {index}")
        records = logger.records()
        assert len(records) == 2
        assert records[-1].message == "message 3"
