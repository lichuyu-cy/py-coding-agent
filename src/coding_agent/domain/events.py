"""Domain 事件：稳定的 typed 事件契约。

- 事件是"已发生事实"的通知；消费者（Metrics/Logger/Stream/SSE）不得驱动控制状态；
- `seq` 在 run 内单调唯一；`event_id` 全局唯一；时间 UTC RFC3339；
- `payload_version` 随载荷结构演进；payload 只放摘要信息（不携带密钥/大正文）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

PAYLOAD_VERSION = 1


class EventType(StrEnum):
    """事件类型目录（生产者在阶段 15–20 逐步接入）。"""

    AGENT_START = "agent_start"
    AGENT_END = "agent_end"
    LLM_REQUEST_START = "llm_request_start"
    LLM_REQUEST_STREAM = "llm_request_stream"  # 易失增量事件（可丢弃）
    LLM_REQUEST_END = "llm_request_end"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    TOOL_CALL_ERROR = "tool_call_error"
    CONTEXT_BUILD = "context_build"
    COMPACTION_START = "compaction_start"
    COMPACTION_END = "compaction_end"
    STEERING = "steering"
    FOLLOW_UP = "follow_up"
    ABORT = "abort"
    ERROR = "error"
    SESSION_SAVE = "session_save"
    CHECKPOINT_SAVE = "checkpoint_save"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """一条已确认事实的事件记录。"""

    type: EventType
    event_id: str
    session_id: str
    run_id: str
    seq: int
    utc_time: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    payload_version: int = PAYLOAD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "utc_time": self.utc_time,
            "payload": dict(self.payload),
            "payload_version": self.payload_version,
        }
