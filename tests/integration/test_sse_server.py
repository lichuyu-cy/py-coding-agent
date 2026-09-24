"""阶段 18 集成测试：HTTP 生命周期、SSE 帧/重连/重放窗口、断连与中止语义。

使用 httpx ASGITransport 在进程内驱动 Starlette 应用（无需真实端口）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from coding_agent.bootstrap import build_runtime
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.observability.event_bus import EventBus
from coding_agent.providers.fake import FakeProvider, FakeResponse
from coding_agent.server.app import create_app

WS = None  # 由 fixture 注入


async def make_client(runtime, tmp_path: Path, *, heartbeat: float = 0.1) -> httpx.AsyncClient:
    app = create_app(runtime, workspace=tmp_path, heartbeat_seconds=heartbeat)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def collect_frames(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict | None = None,
    stop_event: str = "agent_end",
    max_frames: int = 100,
) -> list[dict]:
    frames: list[dict] = []
    async with client.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200, response.status_code
        current: dict = {}
        async for line in response.aiter_lines():
            if line == "":
                if current:
                    frames.append(current)
                    if current.get("event") == stop_event:
                        break
                current = {}
                if len(frames) >= max_frames:
                    break
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            current[key] = value
    return frames


async def poll_until_completed(client: httpx.AsyncClient, run_id: str, *, timeout: float = 5.0) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        info = (await client.get(f"/runs/{run_id}")).json()
        if info["completed"]:
            return info
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not complete within {timeout}s")


class TestHttpLifecycle:
    async def test_create_session_run_and_query(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        async with await make_client(runtime, tmp_path) as client:
            created = await client.post("/sessions")
            assert created.status_code == 200
            session_id = created.json()["session_id"]

            started = await client.post(f"/sessions/{session_id}/runs", json={"task": "hello"})
            assert started.status_code == 202
            run_id = started.json()["run_id"]

            info = await poll_until_completed(client, run_id)
            assert info["final_status"] == "finished"
            assert info["turns"] == 1

            session = (await client.get(f"/sessions/{session_id}")).json()
            assert session["message_count"] == 2
            assert session["tail_type"] == "assistant"
            assert session["active_run_id"] is None

    async def test_stable_error_codes(self, tmp_path: Path) -> None:
        runtime = build_runtime(provider=FakeProvider([]))
        async with await make_client(runtime, tmp_path) as client:
            missing_session = await client.get("/sessions/nope")
            assert missing_session.status_code == 404
            assert missing_session.json()["error"]["code"] == "unknown_session"

            missing_run = await client.get("/runs/nope")
            assert missing_run.status_code == 404
            assert missing_run.json()["error"]["code"] == "unknown_run"

            created = (await client.post("/sessions")).json()["session_id"]
            invalid = await client.post(f"/sessions/{created}/runs", json={"task": ""})
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_request"

    async def test_conflicting_run_rejected(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="slow", stop_reason=StopReason.END_TURN, delay_seconds=0.4)])
        runtime = build_runtime(provider=provider)
        async with await make_client(runtime, tmp_path) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            first = await client.post(f"/sessions/{session_id}/runs", json={"task": "one"})
            assert first.status_code == 202
            second = await client.post(f"/sessions/{session_id}/runs", json={"task": "two"})
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "run_conflict"
            await poll_until_completed(client, first.json()["run_id"])

    async def test_abort_endpoint(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="late", stop_reason=StopReason.END_TURN, delay_seconds=3.0)])
        runtime = build_runtime(provider=provider)
        async with await make_client(runtime, tmp_path) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            started = await client.post(f"/sessions/{session_id}/runs", json={"task": "stop me"})
            run_id = started.json()["run_id"]

            aborted = await client.post(f"/runs/{run_id}/abort", json={"reason": "user_request"})
            assert aborted.status_code == 200
            assert aborted.json()["first_request"] is True

            info = await poll_until_completed(client, run_id)
            assert info["final_status"] == "aborted"


class TestSseStreaming:
    async def test_stream_frames_format_and_terminal_once(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        async with await make_client(runtime, tmp_path) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            run_id = (
                await client.post(f"/sessions/{session_id}/runs", json={"task": "stream me"})
            ).json()["run_id"]

            frames = await collect_frames(client, f"/runs/{run_id}/events")
            events = [frame["event"] for frame in frames]
            assert events[0] == "agent_start"
            assert events[-1] == "agent_end"
            assert events.count("agent_end") == 1
            # id 为 seq 且单调
            seqs = [int(frame["id"]) for frame in frames]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
            # data 载荷完整
            first_payload = json.loads(frames[0]["data"])
            assert first_payload["type"] == "agent_start"
            assert first_payload["payload_version"] == 1
            assert first_payload["run_id"] == run_id

    async def test_reconnect_with_last_event_id_replays_without_duplicates(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider)
        async with await make_client(runtime, tmp_path) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            run_id = (
                await client.post(f"/sessions/{session_id}/runs", json={"task": "task"})
            ).json()["run_id"]
            await poll_until_completed(client, run_id)

            first_frames = await collect_frames(client, f"/runs/{run_id}/events")
            cursor = int(first_frames[1]["id"])  # 从第 2 个事件之后重连
            replay = await collect_frames(
                client, f"/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}
            )
            replayed_seqs = [int(frame["id"]) for frame in replay]
            assert replayed_seqs[0] == cursor + 1
            assert all(seq > cursor for seq in replayed_seqs)
            assert replay[-1]["event"] == "agent_end"

    async def test_expired_cursor_returns_replay_window_error(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="done", stop_reason=StopReason.END_TURN)])
        runtime = build_runtime(provider=provider, event_bus=EventBus(audit_limit=3))
        async with await make_client(runtime, tmp_path) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            run_id = (
                await client.post(f"/sessions/{session_id}/runs", json={"task": "task"})
            ).json()["run_id"]
            await poll_until_completed(client, run_id)

            response = await client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": "0"})
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "replay_window_expired"

    async def test_disconnect_does_not_abort_run(self, tmp_path: Path) -> None:
        provider = FakeProvider([FakeResponse(content="slow", stop_reason=StopReason.END_TURN, delay_seconds=0.5)])
        runtime = build_runtime(provider=provider)
        async with await make_client(runtime, tmp_path) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            run_id = (
                await client.post(f"/sessions/{session_id}/runs", json={"task": "task"})
            ).json()["run_id"]

            # 打开流，读到第一个事件后立即断开
            async with client.stream("GET", f"/runs/{run_id}/events") as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("event: agent_start"):
                        break
            # 断连不触发 Abort：运行照常完成
            info = await poll_until_completed(client, run_id)
            assert info["final_status"] == "finished"
            assert runtime.metrics.snapshot(run_id).metrics.abort_count == 0
