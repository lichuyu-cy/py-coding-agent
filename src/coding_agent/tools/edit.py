"""Edit 工具：按旧文本精确匹配替换，匹配数有显式约束（唯一或全部）。"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from typing import Any

from coding_agent.ports.tool import ToolContext, ToolExecution, ToolExecutionError, ToolSpec
from coding_agent.tools.file_ops import (
    atomic_write_text,
    display_path,
    read_text_file,
    resolve_workspace_path,
)

_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to the file, relative to the workspace."},
        "expected_old": {
            "type": "string",
            "description": "Exact existing text to replace. Must match exactly once unless replace_all is true.",
        },
        "replacement": {"type": "string", "description": "Replacement text (may be empty to delete)."},
        "replace_all": {
            "type": "boolean",
            "description": "Replace every occurrence instead of requiring a unique match.",
        },
    },
    "required": ["path", "expected_old", "replacement"],
    "additionalProperties": False,
}


class EditTool:
    """输入：path + expected_old + replacement（可选 replace_all）；输出：替换摘要 + unified diff。"""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="edit",
            description=(
                "Replace an exact text occurrence in a workspace file and return a unified diff. "
                "By default the old text must match exactly once; set replace_all to change every occurrence."
            ),
            json_schema=_EDIT_SCHEMA,
        )

    async def execute(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolExecution:
        expected_old = args.get("expected_old")
        replacement = args.get("replacement")
        if not isinstance(expected_old, str) or expected_old == "":
            raise ToolExecutionError("invalid_arguments", "expected_old must be a non-empty string")
        if not isinstance(replacement, str):
            raise ToolExecutionError("invalid_arguments", "replacement must be a string")
        replace_all = args.get("replace_all", False)
        if not isinstance(replace_all, bool):
            raise ToolExecutionError("invalid_arguments", "replace_all must be a boolean")

        path = resolve_workspace_path(args.get("path"), ctx.workspace)
        old_content = read_text_file(path)
        occurrences = old_content.count(expected_old)
        if occurrences == 0:
            raise ToolExecutionError(
                "replace_not_found",
                "expected_old text was not found in the file (content must match exactly, including whitespace)",
            )
        if occurrences > 1 and not replace_all:
            raise ToolExecutionError(
                "replace_not_unique",
                f"expected_old matches {occurrences} times; provide more context or set replace_all",
            )
        new_content = (
            old_content.replace(expected_old, replacement)
            if replace_all
            else old_content.replace(expected_old, replacement, 1)
        )
        atomic_write_text(path, new_content)

        rel = display_path(path, ctx.workspace)
        diff = difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
        diff_text = "".join(diff)
        header = f"edited {rel}: replaced {occurrences if replace_all else 1} occurrence(s)"
        output = f"{header}\n{diff_text}" if diff_text else header
        return ToolExecution(output=output)
