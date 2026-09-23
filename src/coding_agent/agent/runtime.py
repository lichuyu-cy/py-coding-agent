"""Runtime：run 的生命周期、会话注册与运行锁、结果包装。

阶段 04 范围：
- `run(task, workspace, session_id?)` 与 `continue_run(session_id)` 入口；
- 内存会话注册表（阶段 19 由持久 SessionStore 取代）与每会话独占运行锁
  （补足阶段 02 记录的“无两个活动运行写同一 Session”）；
- 一切持久写入经 MessageLog；Runtime 是 RuntimeState 的唯一写者。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coding_agent.agent.loop import AgentLoop, LoopObserver, MinimalToolExecutor, RunLimits
from coding_agent.domain.errors import HarnessError
from coding_agent.domain.messages import (
    AssistantMessage,
    MessageLog,
    MessageMeta,
    ToolResult,
    UserMessage,
    new_id,
    new_message_id,
    utc_now_rfc3339,
)
from coding_agent.domain.state import RunStatus, RuntimeState, StateTrigger, StopReason
from coding_agent.ports.provider import Provider, ToolDefinition

DEFAULT_SYSTEM_PROMPT = (
    "You are a coding agent. You inspect and modify the workspace using the provided tools, "
    "then answer the user's request with a concise report."
)


class UnknownSessionError(HarnessError):
    """引用了不存在的会话。"""


class RunConflictError(HarnessError):
    """同一会话已有活动 run：拒绝第二个写者。"""


class ResumeValidationError(HarnessError):
    """继续运行前尾部校验失败（尾部为助手消息或存在未解决的调用）。"""


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    session_id: str
    status: RunStatus
    stop_reason: StopReason | None
    final_text: str
    turns: int
    tool_calls: int
    provider_calls: int
    limit_hit: str | None = None


class InMemorySessionRegistry:
    """阶段 04 的内存会话表与独占运行锁；阶段 19 由持久存储取代。"""

    def __init__(self) -> None:
        self._logs: dict[str, MessageLog] = {}
        self._active: set[str] = set()

    def create_session(self) -> MessageLog:
        log = MessageLog(new_id("sess"))
        self._logs[log.session_id] = log
        return log

    def get(self, session_id: str) -> MessageLog:
        try:
            return self._logs[session_id]
        except KeyError as exc:
            raise UnknownSessionError(f"unknown session {session_id!r}") from exc

    def register(self, log: MessageLog) -> None:
        """注册外部构造的会话（测试/迁移用）。"""
        if log.session_id in self._logs:
            raise HarnessError(f"session {log.session_id!r} already registered")
        self._logs[log.session_id] = log

    def is_active(self, session_id: str) -> bool:
        return session_id in self._active

    def try_acquire(self, session_id: str) -> bool:
        if session_id in self._active:
            return False
        self._active.add(session_id)
        return True

    def release(self, session_id: str) -> None:
        self._active.discard(session_id)


class AgentRuntime:
    """CLI / Server / Benchmark 共用的应用入口（阶段 18/22 复用同一接口）。"""

    def __init__(
        self,
        *,
        provider: Provider,
        executor: MinimalToolExecutor,
        limits: RunLimits | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        registry: InMemorySessionRegistry | None = None,
        observer: LoopObserver | None = None,
        tools: tuple[ToolDefinition, ...] = (),
        model_name: str = "default",
    ) -> None:
        self.registry = registry or InMemorySessionRegistry()
        self._workspaces: dict[str, Path] = {}
        self._loop = AgentLoop(
            provider=provider,
            executor=executor,
            limits=limits,
            system_prompt=system_prompt,
            tools=tools,
            model_name=model_name,
            observer=observer,
        )

    async def run(
        self, task: str, workspace: str | Path, session_id: str | None = None
    ) -> RunResult:
        """新建 run：追加用户任务消息，驱动循环，返回最终结果。"""
        if not isinstance(task, str) or not task.strip():
            raise HarnessError("task must be a non-empty string")
        log = (
            self.registry.get(session_id)
            if session_id is not None
            else self.registry.create_session()
        )
        if not self.registry.try_acquire(log.session_id):
            raise RunConflictError(f"session {log.session_id!r} already has an active run")
        try:
            resolved_workspace = Path(workspace)
            self._workspaces[log.session_id] = resolved_workspace
            state = RuntimeState(run_id=new_id("run")).transition(0, StateTrigger.START)
            user_message = UserMessage(
                meta=self._new_meta(log, state),
                content=task,
            )
            log.append(user_message)
            outcome = await self._loop.run(log=log, state=state, workspace=resolved_workspace)
            return self._to_result(log, outcome.state, outcome.turns, outcome.tool_calls, outcome.provider_calls, outcome.final_text, outcome.limit_hit)
        finally:
            self.registry.release(log.session_id)

    async def continue_run(
        self, session_id: str, workspace: str | Path | None = None
    ) -> RunResult:
        """从已提交历史继续（不追加用户消息）；尾部必须合法。

        workspace 优先使用显式传入值，否则回退到该会话上一次 run 记录的工作区。
        """
        log = self.registry.get(session_id)
        self._validate_tail(log)
        resolved = Path(workspace) if workspace is not None else self._workspaces.get(session_id)
        if resolved is None:
            raise UnknownSessionError(
                f"session {session_id!r} has no recorded workspace; pass workspace explicitly"
            )
        if not self.registry.try_acquire(session_id):
            raise RunConflictError(f"session {session_id!r} already has an active run")
        try:
            state = RuntimeState(run_id=new_id("run")).transition(0, StateTrigger.START)
            outcome = await self._loop.run(log=log, state=state, workspace=resolved)
            return self._to_result(log, outcome.state, outcome.turns, outcome.tool_calls, outcome.provider_calls, outcome.final_text, outcome.limit_hit)
        finally:
            self.registry.release(session_id)

    @staticmethod
    def _new_meta(log: MessageLog, state: RuntimeState) -> MessageMeta:
        return MessageMeta(
            id=new_message_id(),
            session_id=log.session_id,
            run_id=state.run_id,
            turn_id=state.current_turn,
            created_at=utc_now_rfc3339(),
        )

    @staticmethod
    def _validate_tail(log: MessageLog) -> None:
        messages = log.messages
        if not messages:
            raise ResumeValidationError("cannot continue: session has no committed messages")
        last = messages[-1]
        if isinstance(last, AssistantMessage):
            raise ResumeValidationError(
                "cannot continue from an assistant tail: no observation to respond to"
            )
        if not isinstance(last, (UserMessage, ToolResult)):
            raise ResumeValidationError(
                f"cannot continue: unsupported tail message type {last.message_type!r}"
            )
        for message in messages:
            if isinstance(message, AssistantMessage):
                for call in message.tool_calls:
                    if log.find_result(str(call.id)) is None:
                        raise ResumeValidationError(
                            f"cannot continue: tool call {call.id!r} has no final result"
                        )

    @staticmethod
    def _to_result(
        log: MessageLog,
        state: RuntimeState,
        turns: int,
        tool_calls: int,
        provider_calls: int,
        final_text: str,
        limit_hit: str | None,
    ) -> RunResult:
        return RunResult(
            run_id=state.run_id,
            session_id=log.session_id,
            status=state.status or RunStatus.ERROR,
            stop_reason=state.last_stop_reason,
            final_text=final_text,
            turns=turns,
            tool_calls=tool_calls,
            provider_calls=provider_calls,
            limit_hit=limit_hit,
        )
