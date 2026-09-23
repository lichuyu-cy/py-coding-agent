"""Read 工具：读取 workspace 内文本文件的片段，输出带行号。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coding_agent.ports.tool import ToolContext, ToolExecution, ToolExecutionError, ToolSpec
from coding_agent.tools.file_ops import display_path, read_text_file, resolve_workspace_path

_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to the file, relative to the workspace."},
        "start_line": {"type": "integer", "minimum": 1, "description": "First line to read (1-based)."},
        "end_line": {"type": "integer", "minimum": 1, "description": "Last line to read (1-based, inclusive)."},
    },
    "required": ["path"],
    "additionalProperties": False,
}


class ReadTool:
    """输入：path（可选 start_line/end_line）；输出：带行号的文本片段。"""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read",
            description=(
                "Read a UTF-8 text file from the workspace and return it with line numbers. "
                "Optionally restrict to a 1-based inclusive line range."
            ),
            json_schema=_READ_SCHEMA,
        )

    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution:
        path = resolve_workspace_path(args.get("path"), ctx.workspace)
        content = read_text_file(path)
        lines = content.splitlines()
        total = len(lines)

        start = self._optional_line(args, "start_line") or 1
        end = self._optional_line(args, "end_line") or total
        if total == 0:
            start = 1
            end = 0
        if start > max(total, 1):
            raise ToolExecutionError(
                "invalid_arguments",
                f"start_line {start} is beyond the end of the file ({total} lines)",
            )
        end = min(end, total)
        if end < start and total > 0:
            raise ToolExecutionError(
                "invalid_arguments", f"end_line {end} is before start_line {start}"
            )

        rel = display_path(path, ctx.workspace)
        if total == 0:
            header = f"{rel} (empty file)"
        elif start == 1 and end == total:
            header = f"{rel} ({total} lines)"
        else:
            header = f"{rel} ({total} lines, showing {start}-{end})"
        numbered = [f"{number:>6} | {lines[number - 1]}" for number in range(start, end + 1)]
        output = "\n".join([header, *numbered])
        return ToolExecution(output=output)

    @staticmethod
    def _optional_line(args: Mapping[str, Any], key: str) -> int | None:
        value = args.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ToolExecutionError("invalid_arguments", f"{key} must be a positive integer")
        return value
