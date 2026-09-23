"""Bash 工具：在 workspace 内执行 shell 命令，管理进程组并在超时/取消时终止整棵进程树。

平台差异：
- POSIX：`start_new_session=True` 建新进程组，终止用 `os.killpg(SIGKILL)`；
- Windows：`CREATE_NEW_PROCESS_GROUP`，终止用 `taskkill /F /T /PID`（杀整棵树）。

安全边界（如实声明）：cwd 固定为 workspace，但 shell 命令本身不受路径解析保护；
本工具不是沙盒，不能拦截命令对 workspace 之外文件的访问。更强隔离需 OS/容器级方案。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from coding_agent.ports.provider import CancelSignal
from coding_agent.ports.tool import ToolContext, ToolExecution, ToolExecutionError, ToolSpec

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 3600.0
MAX_CAPTURED_BYTES = 1_048_576
_POLL_INTERVAL_SECONDS = 0.1

_BASH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Shell command to run inside the workspace (POSIX sh; cmd.exe on Windows).",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_TIMEOUT_SECONDS,
            "description": f"Optional timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS:g}, max {MAX_TIMEOUT_SECONDS:g}).",
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}


def _creation_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class BashTool:
    """输入：command（可选 timeout_seconds）；输出：exit_code + stdout/stderr 摘要文本。"""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="bash",
            description=(
                "Run a shell command with the workspace as the working directory and return "
                "its exit code, stdout and stderr. The process tree is terminated on timeout or cancellation."
            ),
            json_schema=_BASH_SCHEMA,
        )

    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolExecutionError("invalid_arguments", "command must be a non-empty string")
        timeout = self._resolve_timeout(args, ctx)
        workspace = ctx.workspace.resolve()
        if not workspace.is_dir():
            raise ToolExecutionError("workspace_missing", f"workspace directory does not exist: {workspace}")
        if ctx.cancel is not None and ctx.cancel.is_cancelled:
            raise ToolExecutionError("cancelled", "command was cancelled before it started")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_creation_kwargs(),
            )
        except OSError as exc:
            raise ToolExecutionError("spawn_failed", f"failed to start command: {exc}") from exc

        failure, stdout_bytes, stderr_bytes = await self._watch_and_collect(
            proc, timeout, ctx.cancel
        )
        output = self._format_output(stdout_bytes, stderr_bytes, proc.returncode)
        if failure is not None:
            kind, message = failure
            raise ToolExecutionError(kind, message, exit_code=proc.returncode, output=output)
        exit_code = proc.returncode if proc.returncode is not None else -1
        return ToolExecution(output=output, exit_code=exit_code)

    @staticmethod
    def _resolve_timeout(args: Mapping[str, Any], ctx: ToolContext) -> float:
        raw = args.get("timeout_seconds")
        if raw is None:
            timeout = DEFAULT_TIMEOUT_SECONDS
        elif isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0 or raw > MAX_TIMEOUT_SECONDS:
            raise ToolExecutionError(
                "invalid_arguments",
                f"timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS:g}], got {raw!r}",
            )
        else:
            timeout = float(raw)
        if ctx.deadline is not None:
            timeout = min(timeout, max(ctx.deadline, 0.001))
        return timeout

    async def _watch_and_collect(
        self,
        proc: asyncio.subprocess.Process,
        timeout: float,
        cancel: CancelSignal | None,
    ) -> tuple[tuple[str, str] | None, bytes, bytes]:
        """等待进程结束；超时或取消时终止进程树。

        返回 (failure, stdout_bytes, stderr_bytes)；failure 为 (kind, message) 或 None。
        communicate 只启动一次，终止后仍可拿到已读取的部分输出。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        communicate = asyncio.ensure_future(proc.communicate())
        failure: tuple[str, str] | None = None
        try:
            while True:
                done, _ = await asyncio.wait({communicate}, timeout=_POLL_INTERVAL_SECONDS)
                if done:
                    break
                if cancel is not None and cancel.is_cancelled:
                    failure = ("cancelled", "command was cancelled")
                    break
                if loop.time() >= deadline:
                    failure = ("timeout", f"command timed out after {timeout:g}s")
                    break
            if failure is not None:
                await self._terminate_tree(proc)
        except asyncio.CancelledError:
            # 外层任务被取消（如 Pipeline 的 deadline 包裹）：先回收进程树再传播取消，避免悬挂。
            await self._terminate_tree(proc)
            raise
        finally:
            # 无论正常结束还是被终止，都在此回收 communicate 结果（进程已退出，管道已关闭）。
            stdout_bytes, stderr_bytes = await communicate
        return failure, stdout_bytes or b"", stderr_bytes or b""

    async def _terminate_tree(self, proc: asyncio.subprocess.Process) -> None:
        """终止整棵进程树并回收；尽力而为，绝不向上抛错。"""
        if proc.returncode is not None:
            return
        if os.name == "nt":
            with contextlib.suppress(OSError):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        else:
            import signal

            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()

    @staticmethod
    def _format_output(stdout_bytes: bytes, stderr_bytes: bytes, exit_code: int | None) -> str:
        stdout_text = BashTool._safe_decode(stdout_bytes)
        stderr_text = BashTool._safe_decode(stderr_bytes)
        shown_code = exit_code if exit_code is not None else -1
        parts = [f"exit_code: {shown_code}"]
        if stdout_text:
            parts.append(f"stdout:\n{stdout_text}")
        if stderr_text:
            parts.append(f"stderr:\n{stderr_text}")
        if not stdout_text and not stderr_text:
            parts.append("(no output)")
        return "\n".join(parts)

    @staticmethod
    def _safe_decode(data: bytes) -> str:
        text = data[:MAX_CAPTURED_BYTES].decode("utf-8", errors="replace")
        if len(data) > MAX_CAPTURED_BYTES:
            text += f"\n(stream truncated at {MAX_CAPTURED_BYTES} bytes; capture limit)"
        return text
