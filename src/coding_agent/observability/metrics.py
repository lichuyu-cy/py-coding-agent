"""Metrics：以事件流汇总运行指标（只读聚合；不影响工具决策）。

- `MetricsAccumulator` 以内联 handler 订阅 EventBus（订阅者失败被总线隔离）；
- 按 event_id 去重（重复投递/乱序重放幂等）；计数为可交换操作；
- token 缺失（usage 为 None）记 unknown，不猜测为 0；
- 终结时（AGENT_END）自动生成快照并 persist（阶段 19 改由 Session 持久化）；
- `snapshot(run_id)` 可随时读取只读摘要（供 SSE/诊断查询）。

时长：取 AGENT_START 与 AGENT_END 的 UTC 时间差；两端缺失或为负 → None 并记录 anomaly
（负时长防护：时钟异常不得产生看似有效的指标）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from coding_agent.domain.events import AgentEvent, EventType
from coding_agent.domain.messages import utc_now_rfc3339
from coding_agent.observability.event_bus import EventBus

__all__ = ["MetricSnapshot", "MetricsAccumulator", "RunMetrics"]


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """一个 run 的汇总指标（缺失项为 None，不记为 0）。"""

    llm_requests_started: int = 0
    llm_requests_completed: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_unknown_requests: int = 0
    tool_calls_started: int = 0
    tool_calls_completed: int = 0
    tool_calls_failed: int = 0
    tool_calls_cancelled: int = 0
    compactions: int = 0
    compaction_tokens_before: int = 0
    compaction_tokens_after: int = 0
    steering_count: int = 0
    follow_up_count: int = 0
    abort_count: int = 0
    errors_by_stage: tuple[tuple[str, int], ...] = ()
    turns: int = 0
    final_status: str | None = None
    limit_hit: str | None = None
    duration_seconds: float | None = None
    completed: bool = False
    events_seen: int = 0
    anomalies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """某 run 的只读指标快照。"""

    run_id: str
    session_id: str
    metrics: RunMetrics
    generated_at: str


@dataclass
class _RunState:
    session_id: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    input_known: int = 0
    output_known: int = 0
    input_known_any: bool = False
    output_known_any: bool = False
    errors: dict[str, int] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    compaction_before: int = 0
    compaction_after: int = 0
    turns: int = 0
    final_status: str | None = None
    limit_hit: str | None = None
    completed: bool = False
    anomalies: list[str] = field(default_factory=list)
    events_seen: int = 0

    def bump(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


class MetricsAccumulator:
    """事件流 → 指标的幂等聚合器。"""

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}
        self._seen_event_ids: set[str] = set()
        self._persisted: list[MetricSnapshot] = []

    @classmethod
    def connect(cls, bus: EventBus) -> "MetricsAccumulator":
        """以内联 handler 订阅总线（异常由总线隔离并记录）。"""
        accumulator = cls()
        bus.subscribe(handler=accumulator.on_event)
        return accumulator

    # ---- 事件消费 ----

    def on_event(self, event: AgentEvent) -> None:
        if event.event_id in self._seen_event_ids:
            return  # 重复投递/重放幂等
        self._seen_event_ids.add(event.event_id)
        state = self._runs.setdefault(event.run_id, _RunState(session_id=event.session_id))
        state.session_id = event.session_id or state.session_id
        state.events_seen += 1
        payload: Mapping = event.payload
        event_type = event.type

        if event_type is EventType.AGENT_START:
            state.started_at = event.utc_time
        elif event_type is EventType.LLM_REQUEST_START:
            state.bump("llm_requests_started")
        elif event_type is EventType.LLM_REQUEST_END:
            state.bump("llm_requests_completed")
            input_tokens = payload.get("usage_input_tokens")
            output_tokens = payload.get("usage_output_tokens")
            if input_tokens is None or output_tokens is None:
                state.bump("usage_unknown_requests")
            if isinstance(input_tokens, int):
                state.input_known += input_tokens
                state.input_known_any = True
            if isinstance(output_tokens, int):
                state.output_known += output_tokens
                state.output_known_any = True
        elif event_type is EventType.TOOL_CALL_START:
            state.bump("tool_calls_started")
        elif event_type is EventType.TOOL_CALL_END:
            state.bump("tool_calls_completed")
        elif event_type is EventType.TOOL_CALL_ERROR:
            if payload.get("status") == "cancelled":
                state.bump("tool_calls_cancelled")
            else:
                state.bump("tool_calls_failed")
        elif event_type is EventType.COMPACTION_END:
            state.bump("compactions")
            before = payload.get("token_before")
            after = payload.get("token_after")
            if isinstance(before, int):
                state.compaction_before += before
            if isinstance(after, int):
                state.compaction_after += after
        elif event_type is EventType.STEERING:
            state.bump("steering_count")
        elif event_type is EventType.FOLLOW_UP:
            state.bump("follow_up_count")
        elif event_type is EventType.ABORT:
            state.bump("abort_count")
        elif event_type is EventType.ERROR:
            stage = str(payload.get("stage", "unknown"))
            state.errors[stage] = state.errors.get(stage, 0) + 1
        elif event_type is EventType.AGENT_END:
            state.ended_at = event.utc_time
            state.final_status = payload.get("status")
            state.limit_hit = payload.get("limit_hit")
            turns = payload.get("turns")
            if isinstance(turns, int):
                state.turns = turns
            state.completed = True
            self._persisted.append(self._build_snapshot(event.run_id, state))

    # ---- 快照 ----

    def snapshot(self, run_id: str) -> MetricSnapshot | None:
        state = self._runs.get(run_id)
        if state is None:
            return None
        return self._build_snapshot(run_id, state)

    def persisted(self) -> tuple[MetricSnapshot, ...]:
        """终结时自动输出的快照（阶段 19 改为写入 Session）。"""
        return tuple(self._persisted)

    def persist(self, snapshot: MetricSnapshot) -> None:
        self._persisted.append(snapshot)

    # ---- 内部 ----

    def _build_snapshot(self, run_id: str, state: _RunState) -> MetricSnapshot:
        duration: float | None = None
        anomalies = list(state.anomalies)
        if state.started_at is not None and state.ended_at is not None:
            delta = (_parse_utc(state.ended_at) - _parse_utc(state.started_at)).total_seconds()
            if delta < 0:
                anomalies.append("negative_duration")
                duration = None
            else:
                duration = delta
        metrics = RunMetrics(
            llm_requests_started=state.counters.get("llm_requests_started", 0),
            llm_requests_completed=state.counters.get("llm_requests_completed", 0),
            input_tokens=state.input_known if state.input_known_any else None,
            output_tokens=state.output_known if state.output_known_any else None,
            usage_unknown_requests=state.counters.get("usage_unknown_requests", 0),
            tool_calls_started=state.counters.get("tool_calls_started", 0),
            tool_calls_completed=state.counters.get("tool_calls_completed", 0),
            tool_calls_failed=state.counters.get("tool_calls_failed", 0),
            tool_calls_cancelled=state.counters.get("tool_calls_cancelled", 0),
            compactions=state.counters.get("compactions", 0),
            compaction_tokens_before=state.compaction_before,
            compaction_tokens_after=state.compaction_after,
            steering_count=state.counters.get("steering_count", 0),
            follow_up_count=state.counters.get("follow_up_count", 0),
            abort_count=state.counters.get("abort_count", 0),
            errors_by_stage=tuple(sorted(state.errors.items())),
            turns=state.turns,
            final_status=state.final_status,
            limit_hit=state.limit_hit,
            duration_seconds=duration,
            completed=state.completed,
            events_seen=state.events_seen,
            anomalies=tuple(anomalies),
        )
        return MetricSnapshot(
            run_id=run_id,
            session_id=state.session_id,
            metrics=metrics,
            generated_at=utc_now_rfc3339(),
        )
