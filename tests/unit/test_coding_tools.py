"""阶段 05 单测：Read / Write / Edit / Bash 四个编码工具。

覆盖 dependency-graph.md 阶段 05 的验收条件：
- 临时目录读写、edit 冲突、命令超时；
- 越界路径不修改任何文件；软链逃逸拒绝（平台支持时）。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coding_agent.ports.tool import ToolContext, ToolExecutionError
from coding_agent.tools.bash import BashTool
from coding_agent.tools.edit import EditTool
from coding_agent.tools.file_ops import MAX_READ_BYTES
from coding_agent.tools.read import ReadTool
from coding_agent.tools.write import WriteTool


class Token:
    def __init__(self) -> None:
        self.cancelled = False

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled


def ctx(workspace: Path, *, cancel: Token | None = None, deadline: float | None = None) -> ToolContext:
    return ToolContext(workspace=workspace, cancel=cancel, deadline=deadline)


def try_dir_link(target_dir: Path, link: Path) -> bool:
    """创建指向外部目录的链接：优先符号链接，Windows 下退化为 junction（无需特权）。"""
    try:
        link.symlink_to(target_dir, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target_dir)],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0 and link.exists()
    return False


class TestReadTool:
    async def test_numbered_lines(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
        result = await ReadTool().execute({"path": "a.py"}, ctx(tmp_path))
        assert result.output.splitlines()[0] == "a.py (3 lines)"
        assert "     1 | line1" in result.output
        assert "     3 | line3" in result.output

    async def test_line_range(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("l1\nl2\nl3\n", encoding="utf-8")
        result = await ReadTool().execute({"path": "a.py", "start_line": 2, "end_line": 2}, ctx(tmp_path))
        assert "(3 lines, showing 2-2)" in result.output
        assert "     2 | l2" in result.output
        assert "l1" not in result.output

    async def test_unicode_content(self, tmp_path: Path) -> None:
        (tmp_path / "zh.txt").write_text("你好，世界\n第二行\n", encoding="utf-8")
        result = await ReadTool().execute({"path": "zh.txt"}, ctx(tmp_path))
        assert "你好，世界" in result.output

    async def test_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / "empty.txt").write_text("", encoding="utf-8")
        result = await ReadTool().execute({"path": "empty.txt"}, ctx(tmp_path))
        assert "(empty file)" in result.output

    async def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "nope.txt"}, ctx(tmp_path))
        assert excinfo.value.kind == "file_not_found"

    async def test_binary_file_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "bin.dat"}, ctx(tmp_path))
        assert excinfo.value.kind == "binary_file"

    async def test_invalid_utf8_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "latin.txt").write_bytes(b"\xff\xfeplain bytes")
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "latin.txt"}, ctx(tmp_path))
        assert excinfo.value.kind == "invalid_encoding"

    async def test_too_large_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_bytes(b"a" * (MAX_READ_BYTES + 1))
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "big.txt"}, ctx(tmp_path))
        assert excinfo.value.kind == "file_too_large"

    async def test_directory_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "sub"}, ctx(tmp_path))
        assert excinfo.value.kind == "not_a_file"

    async def test_escape_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "../outside.txt"}, ctx(tmp_path))
        assert excinfo.value.kind == "path_escape"

    async def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
        link = tmp_path / "linkdir"
        if not try_dir_link(outside_dir, link):
            pytest.skip("directory links not available on this platform/account")
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "linkdir/secret.txt"}, ctx(tmp_path))
        assert excinfo.value.kind == "path_escape"

    async def test_start_beyond_file_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("only\n", encoding="utf-8")
        with pytest.raises(ToolExecutionError) as excinfo:
            await ReadTool().execute({"path": "a.txt", "start_line": 5}, ctx(tmp_path))
        assert excinfo.value.kind == "invalid_arguments"


class TestWriteTool:
    async def test_create_and_overwrite(self, tmp_path: Path) -> None:
        first = await WriteTool().execute({"path": "a.txt", "content": "hello\n"}, ctx(tmp_path))
        assert "created new file" in first.output
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "hello\n"

        second = await WriteTool().execute({"path": "a.txt", "content": "changed\n"}, ctx(tmp_path))
        assert "replaced existing file" in second.output
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "changed\n"

    async def test_unicode_roundtrip(self, tmp_path: Path) -> None:
        await WriteTool().execute({"path": "zh.txt", "content": "中文内容\n"}, ctx(tmp_path))
        assert (tmp_path / "zh.txt").read_text(encoding="utf-8") == "中文内容\n"

    async def test_nested_paths_created(self, tmp_path: Path) -> None:
        await WriteTool().execute({"path": "pkg/sub/mod.py", "content": "x = 1\n"}, ctx(tmp_path))
        assert (tmp_path / "pkg" / "sub" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"

    async def test_escape_rejected_without_writing(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await WriteTool().execute({"path": "../evil.txt", "content": "boom"}, ctx(tmp_path))
        assert excinfo.value.kind == "path_escape"
        assert not (tmp_path.parent / "evil.txt").exists()

    async def test_no_temp_files_left_behind(self, tmp_path: Path) -> None:
        await WriteTool().execute({"path": "a.txt", "content": "data"}, ctx(tmp_path))
        assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt"]

    async def test_invalid_content_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await WriteTool().execute({"path": "a.txt", "content": 42}, ctx(tmp_path))
        assert excinfo.value.kind == "invalid_arguments"

    async def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-dir"
        outside_dir.mkdir(exist_ok=True)
        link = tmp_path / "sneaky"
        if not try_dir_link(outside_dir, link):
            pytest.skip("directory links not available on this platform/account")
        with pytest.raises(ToolExecutionError) as excinfo:
            await WriteTool().execute({"path": "sneaky/x.txt", "content": "x"}, ctx(tmp_path))
        assert excinfo.value.kind == "path_escape"
        assert not (outside_dir / "x.txt").exists()


class TestEditTool:
    async def test_unique_replace_with_diff(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a\nb\nc\n", encoding="utf-8")
        result = await EditTool().execute(
            {"path": "a.py", "expected_old": "b", "replacement": "B"}, ctx(tmp_path)
        )
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a\nB\nc\n"
        assert "replaced 1 occurrence(s)" in result.output
        assert "-b" in result.output
        assert "+B" in result.output

    async def test_not_found_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
        with pytest.raises(ToolExecutionError) as excinfo:
            await EditTool().execute(
                {"path": "a.py", "expected_old": "zzz", "replacement": "x"}, ctx(tmp_path)
            )
        assert excinfo.value.kind == "replace_not_found"

    async def test_multiple_matches_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\nx = 2\n", encoding="utf-8")
        with pytest.raises(ToolExecutionError, match="2 times") as excinfo:
            await EditTool().execute(
                {"path": "a.py", "expected_old": "x = ", "replacement": "y = "}, ctx(tmp_path)
            )
        assert excinfo.value.kind == "replace_not_unique"
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\nx = 2\n"  # 原文件不被修改

    async def test_replace_all(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\nx = 2\n", encoding="utf-8")
        result = await EditTool().execute(
            {"path": "a.py", "expected_old": "x = ", "replacement": "y = ", "replace_all": True},
            ctx(tmp_path),
        )
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "y = 1\ny = 2\n"
        assert "replaced 2 occurrence(s)" in result.output

    async def test_empty_expected_old_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await EditTool().execute(
                {"path": "a.py", "expected_old": "", "replacement": "x"}, ctx(tmp_path)
            )
        assert excinfo.value.kind == "invalid_arguments"

    async def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await EditTool().execute(
                {"path": "nope.py", "expected_old": "a", "replacement": "b"}, ctx(tmp_path)
            )
        assert excinfo.value.kind == "file_not_found"


class TestBashTool:
    async def test_echo_success(self, tmp_path: Path) -> None:
        result = await BashTool().execute({"command": "echo hello"}, ctx(tmp_path))
        assert result.exit_code == 0
        assert "hello" in result.output
        assert "exit_code: 0" in result.output

    async def test_nonzero_exit_is_structured_result(self, tmp_path: Path) -> None:
        result = await BashTool().execute(
            {"command": 'python -c "import sys; sys.exit(3)"'}, ctx(tmp_path)
        )
        assert result.exit_code == 3
        assert "exit_code: 3" in result.output

    async def test_stdout_and_stderr_separated(self, tmp_path: Path) -> None:
        result = await BashTool().execute(
            {
                "command": (
                    'python -c "import sys; print(\'out-line\'); sys.stderr.write(\'err-line\')"'
                )
            },
            ctx(tmp_path),
        )
        assert "stdout:\nout-line" in result.output
        assert "stderr:\nerr-line" in result.output

    async def test_cwd_is_workspace(self, tmp_path: Path) -> None:
        result = await BashTool().execute(
            {"command": 'python -c "import os; print(os.getcwd())"'}, ctx(tmp_path)
        )
        assert str(tmp_path.resolve()).lower() in result.output.lower()

    async def test_timeout_kills_process_tree(self, tmp_path: Path) -> None:
        started = time.monotonic()
        with pytest.raises(ToolExecutionError) as excinfo:
            await BashTool().execute(
                {"command": 'python -c "import time; time.sleep(30)"', "timeout_seconds": 0.4},
                ctx(tmp_path),
            )
        elapsed = time.monotonic() - started
        error = excinfo.value
        assert error.kind == "timeout"
        assert elapsed < 10  # 未等待 30 秒的完整睡眠
        assert error.output is not None and "exit_code:" in error.output

    async def test_deadline_bounds_timeout(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await BashTool().execute(
                {"command": 'python -c "import time; time.sleep(30)"', "timeout_seconds": 25},
                ctx(tmp_path, deadline=0.4),
            )
        assert excinfo.value.kind == "timeout"

    async def test_cancelled_before_start(self, tmp_path: Path) -> None:
        token = Token()
        token.cancelled = True
        with pytest.raises(ToolExecutionError) as excinfo:
            await BashTool().execute({"command": "echo nope"}, ctx(tmp_path, cancel=token))
        assert excinfo.value.kind == "cancelled"

    async def test_large_output_is_capped(self, tmp_path: Path) -> None:
        result = await BashTool().execute(
            {"command": 'python -c "print(\'x\' * 2000000)"'}, ctx(tmp_path)
        )
        assert "truncated at" in result.output

    async def test_invalid_timeout_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ToolExecutionError) as excinfo:
            await BashTool().execute({"command": "echo hi", "timeout_seconds": -1}, ctx(tmp_path))
        assert excinfo.value.kind == "invalid_arguments"

    async def test_workspace_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone"
        with pytest.raises(ToolExecutionError) as excinfo:
            await BashTool().execute({"command": "echo hi"}, ctx(missing))
        assert excinfo.value.kind == "workspace_missing"


class TestSpecs:
    def test_specs_declare_schemas(self) -> None:
        for tool, name in ((ReadTool(), "read"), (WriteTool(), "write"), (EditTool(), "edit"), (BashTool(), "bash")):
            spec = tool.spec()
            assert spec.name == name
            assert spec.json_schema["type"] == "object"
            assert spec.json_schema["required"]
            assert spec.json_schema["additionalProperties"] is False
