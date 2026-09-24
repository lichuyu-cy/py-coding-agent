"""SSE HTTP 适配器：Session/Run 生命周期、事件订阅（Last-Event-ID 重放）与显式中止。

边界（设计约束）：
- 只调用 Runtime 的公共入口（run/abort/submit_*）与总线查询；不持有 Agent 状态机写权限；
- 默认断连仅解除订阅，不 Abort 运行；显式 Abort API 才改变运行；
- 游标早于保留窗口 → 409 replay_window_expired（明确重放窗口错误）；
- HTTP 错误载荷稳定：{"error": {"code", "message"}}。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from coding_agent.agent.runtime import AgentRuntime, RunConflictError, UnknownSessionError
from coding_agent.agent.control import UnknownRunError
from coding_agent.domain.events import EventType
from coding_agent.domain.messages import new_id
from coding_agent.server.schemas import (
    AbortRequest,
    ErrorBody,
    ErrorEnvelope,
    RunAccepted,
    RunInfo,
    RunRequest,
    SessionCreated,
    SessionInfo,
)
from coding_agent.server.sse import HEARTBEAT_FRAME, encode_sse

__all__ = ["create_app"]

DEFAULT_HEARTBEAT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 0.05


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message))
    return JSONResponse(body.model_dump(), status_code=status_code)


class Service:
    """把 Runtime 包装为 HTTP 可用的服务面（保持薄适配）。"""

    def __init__(self, runtime: AgentRuntime, workspace: Path, *, heartbeat_seconds: float) -> None:
        self.runtime = runtime
        self.workspace = workspace
        self.heartbeat_seconds = heartbeat_seconds
        self.run_tasks: dict[str, asyncio.Task] = {}
        self.run_sessions: dict[str, str] = {}

    # ---- 运行 ----

    async def start_run(self, session_id: str, payload: RunRequest) -> RunAccepted:
        # 同步前置校验（权威独占锁仍在 Runtime 内部，try_acquire）。
        self.runtime.registry.get(session_id)  # 未知会话 → UnknownSessionError
        if self.runtime.active_run_id(session_id) is not None:
            raise RunConflictError(f"session {session_id!r} already has an active run")
        run_id = new_id("run")
        # 先登记再调度：极快的 run 完成后 GET /runs 也能立即查询到。
        self.run_sessions[run_id] = session_id
        run_task = asyncio.ensure_future(
            self.runtime.run(
                payload.task,
                self.workspace,
                session_id=session_id,
                skills=payload.skills,
                run_id=run_id,
            )
        )
        self.run_tasks[run_id] = run_task

        def _cleanup(task: asyncio.Task, rid: str = run_id) -> None:
            self.run_tasks.pop(rid, None)
            if not task.cancelled():
                task.exception()  # 取回异常（已由事件/指标记录，避免悬挂告警）

        run_task.add_done_callback(_cleanup)
        # 让步一个调度周期：保证 202 返回前 run 已进入活动表（abort/冲突检查可见）。
        await asyncio.sleep(0)
        return RunAccepted(run_id=run_id, session_id=session_id)

    def run_info(self, run_id: str) -> RunInfo | None:
        session_id = self.run_sessions.get(run_id)
        snapshot = self.runtime.metrics.snapshot(run_id)
        if session_id is None and snapshot is None:
            return None
        metrics = snapshot.metrics if snapshot is not None else None
        return RunInfo(
            run_id=run_id,
            session_id=session_id or (snapshot.session_id if snapshot is not None else None),
            active=run_id in self.runtime.active_run_ids(),
            completed=bool(metrics and metrics.completed),
            final_status=metrics.final_status if metrics else None,
            limit_hit=metrics.limit_hit if metrics else None,
            turns=metrics.turns if metrics else 0,
            tool_calls=metrics.tool_calls_started if metrics else 0,
            duration_seconds=metrics.duration_seconds if metrics else None,
        )

    # ---- 事件流 ----

    def replay_window_start(self, run_id: str) -> int | None:
        """run 在审计中保留的最小 seq；None 表示审计中无该 run。"""
        events = self.runtime.bus.events(run_id=run_id)
        if not events:
            return None
        return events[0].seq

    async def event_stream(self, run_id: str, cursor: int):
        """SSE 生成器：先重放（游标之后），再实时订阅；断连仅退订。"""

        async def generate():
            bus = self.runtime.bus
            for event in bus.events(run_id=run_id, after_seq=cursor):
                yield encode_sse(event)
                if event.type is EventType.AGENT_END:
                    return
            subscription = bus.subscribe(run_id=run_id, cursor=cursor)
            try:
                idle = 0.0
                while True:
                    drained = bus.drain(subscription)
                    if drained:
                        idle = 0.0
                        for event in drained:
                            yield encode_sse(event)
                            if event.type is EventType.AGENT_END:
                                return
                        continue
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    idle += _POLL_INTERVAL_SECONDS
                    if self.heartbeat_seconds and idle >= self.heartbeat_seconds:
                        idle = 0.0
                        yield HEARTBEAT_FRAME
            finally:
                bus.unsubscribe(subscription)  # 断连默认仅解除订阅

        return StreamingResponse(generate(), media_type="text/event-stream")


def create_app(
    runtime: AgentRuntime,
    *,
    workspace: str | Path = ".",
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
) -> Starlette:
    """组装 SSE 服务；CLI/部署入口直接使用本工厂。"""
    service = Service(runtime, Path(workspace), heartbeat_seconds=heartbeat_seconds)

    async def create_session(request: Request) -> Response:
        log = runtime.registry.create_session()
        return JSONResponse(SessionCreated(session_id=log.session_id).model_dump())

    async def session_info(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        try:
            log = runtime.registry.get(session_id)
        except UnknownSessionError as err:
            return _error(404, "unknown_session", str(err))
        messages = log.messages
        return JSONResponse(
            SessionInfo(
                session_id=session_id,
                workspace=str(service.runtime.workspace_of(session_id) or service.workspace),
                message_count=len(messages),
                tail_type=messages[-1].message_type if messages else None,
                pending_follow_ups=runtime.pending_follow_ups(session_id),
                active_run_id=runtime.active_run_id(session_id),
            ).model_dump()
        )

    async def start_run(request: Request) -> Response:
        session_id = request.path_params["session_id"]
        try:
            payload = RunRequest.model_validate(await request.json())
        except ValidationError as err:
            return _error(422, "invalid_request", err.errors()[0]["msg"])
        except Exception:  # noqa: BLE001 - JSON 解析失败
            return _error(422, "invalid_request", "request body must be JSON")
        try:
            accepted = await service.start_run(session_id, payload)
        except UnknownSessionError as err:
            return _error(404, "unknown_session", str(err))
        except RunConflictError as err:
            return _error(409, "run_conflict", str(err))
        return JSONResponse(accepted.model_dump(), status_code=202)

    async def run_info(request: Request) -> Response:
        info = service.run_info(request.path_params["run_id"])
        if info is None:
            return _error(404, "unknown_run", "run is not known to this server")
        return JSONResponse(info.model_dump())

    async def abort_run(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        try:
            payload = AbortRequest.model_validate(await request.json() if await _has_body(request) else {})
        except ValidationError as err:
            return _error(422, "invalid_request", err.errors()[0]["msg"])
        try:
            first = runtime.abort(run_id, payload.reason or "user_request")
        except UnknownRunError as err:
            return _error(404, "unknown_run", str(err))
        return JSONResponse({"run_id": run_id, "aborted": True, "first_request": first})

    async def run_events(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        last_event_id = request.headers.get("last-event-id") or request.query_params.get("cursor")
        try:
            cursor = int(last_event_id) if last_event_id is not None else 0
        except ValueError:
            return _error(422, "invalid_request", "Last-Event-ID/cursor must be an integer seq")
        info = service.run_info(run_id)
        if info is None:
            return _error(404, "unknown_run", "run is not known to this server")
        window_start = service.replay_window_start(run_id)
        if window_start is not None and cursor < window_start - 1:
            return _error(
                409,
                "replay_window_expired",
                f"requested cursor {cursor} is older than the retained window (starts at {window_start})",
            )
        stream = await service.event_stream(run_id, cursor)
        return stream

    app = Starlette(
        routes=[
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions/{session_id}", session_info, methods=["GET"]),
            Route("/sessions/{session_id}/runs", start_run, methods=["POST"]),
            Route("/runs/{run_id}", run_info, methods=["GET"]),
            Route("/runs/{run_id}/abort", abort_run, methods=["POST"]),
            Route("/runs/{run_id}/events", run_events, methods=["GET"]),
        ]
    )
    app.state.service = service  # 供测试/部署观察
    return app


async def _has_body(request: Request) -> bool:
    body = await request.body()
    return bool(body)
