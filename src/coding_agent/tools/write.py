"""Write 工具：创建或整体覆盖 workspace 内文本文件（原子替换）。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coding_agent.ports.tool import ToolContext, ToolExecution, ToolExecutionError, ToolSpec
from coding_agent.tools.file_ops import atomic_write_text, display_path, resolve_workspace_path

_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to the file, relative to the workspace."},
        "content": {"type": "string", "description": "Full new content of the file (UTF-8)."},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


class WriteTool:
    """输入：path + content；输出：写入摘要。父目录不存在时自动创建（仍在 workspace 内）。"""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write",
            description=(
                "Create or overwrite a UTF-8 text file in the workspace with the given content. "
                "The write is atomic (temp file + rename) and must stay inside the workspace."
            ),
            json_schema=_WRITE_SCHEMA,
        )

    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution:
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolExecutionError("invalid_arguments", "content must be a string")
        path = resolve_workspace_path(args.get("path"), ctx.workspace)
        if path.is_dir():
            raise ToolExecutionError("not_a_file", f"{path} is a directory, not a file")

        existed = path.exists()
        atomic_write_text(path, content)

        rel = display_path(path, ctx.workspace)
        payload_bytes = len(content.encode("utf-8"))
        lines = len(content.splitlines())
        action = "replaced existing file" if existed else "created new file"
        return ToolExecution(
            output=f"wrote {payload_bytes} bytes ({lines} lines) to {rel}; {action}"
        )
