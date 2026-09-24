"""SSE 编码：把 AgentEvent 编码为 id/event/data 帧。

- `id` 为 run 内 seq（客户端用 Last-Event-ID 重连重放）；
- `event` 为事件类型；`data` 为 JSON（含 event_id/run_id/session_id/seq/utc_time/payload/payload_version）；
- 心跳为注释帧（": ping"），不占用 id。
"""

from __future__ import annotations

import json

from coding_agent.domain.events import AgentEvent

__all__ = ["HEARTBEAT_FRAME", "encode_sse", "encode_sse_error"]

HEARTBEAT_FRAME = ": ping\n\n"


def encode_sse(event: AgentEvent) -> str:
    payload = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type.value}\ndata: {payload}\n\n"


def encode_sse_error(code: str, message: str) -> str:
    payload = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
    return f"event: error\ndata: {payload}\n\n"
