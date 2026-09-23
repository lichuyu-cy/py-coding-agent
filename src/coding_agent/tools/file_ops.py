"""编码工具共享的路径解析与文件 I/O 助手（实现细节，非模型可见）。

- `resolve_workspace_path`：把模型给出的相对/绝对路径解析为 workspace 内的真实路径；
  `..` 与软链在解析后校验；越界一律拒绝（tools 层的包含性底线，
  阶段 08 的 SafetyPolicy 在此基础上叠加策略）。
- `read_text_file`：UTF-8 读取，二进制/非 UTF-8/超大文件给出显式分类错误。
- `atomic_write_text`：同目录临时文件 + fsync + os.replace 原子替换。
- 已知限制：解析与写入之间存在 TOCTOU 窗口，且符号链接可在解析后被替换；
  本层不做 OS 级隔离，相关声明见阶段 08 文档。
"""

from __future__ import annotations

import codecs
import os
import tempfile
from pathlib import Path

from coding_agent.ports.tool import ToolExecutionError

MAX_READ_BYTES = 1_000_000
_BINARY_SNIFF_BYTES = 8192


def is_within(path: Path, root: Path) -> bool:
    """判断 path 是否位于 root 之内（Windows 下大小写不敏感）。"""
    p = os.path.normcase(str(path))
    r = os.path.normcase(str(root))
    return p == r or p.startswith(r + os.sep)


def resolve_workspace_path(raw: object, workspace: Path) -> Path:
    """解析并校验目标路径。

    - 相对路径基于 workspace；绝对路径仅当位于 workspace 内才允许；
    - 解析软链与 `..` 后的真实路径必须仍在 workspace 内，否则 path_escape；
    - 解析不要求目标已存在（写新文件场景），仅做包含性校验。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ToolExecutionError("invalid_arguments", "path must be a non-empty string")
    root = workspace.resolve()
    candidate = Path(raw)
    target = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = target.resolve()
    except OSError as exc:  # 例：Windows 下非法字符、路径过长
        raise ToolExecutionError("invalid_arguments", f"cannot resolve path {raw!r}: {exc}") from exc
    if not is_within(resolved, root):
        raise ToolExecutionError(
            "path_escape", f"path {raw!r} escapes the workspace root"
        )
    return resolved


def display_path(path: Path, workspace: Path) -> str:
    """给出相对 workspace 的展示路径（尽最大努力，绝不抛错）。"""
    try:
        return str(path.relative_to(workspace.resolve()))
    except (ValueError, OSError):
        return str(path)


def read_text_file(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> str:
    """读取文本文件；分类错误：file_not_found / not_a_file / file_too_large /
    binary_file / invalid_encoding。"""
    if path.is_dir():
        raise ToolExecutionError("not_a_file", f"{path} is a directory, not a file")
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise ToolExecutionError("file_not_found", f"file not found: {path}") from exc
    if size > max_bytes:
        raise ToolExecutionError(
            "file_too_large",
            f"file is {size} bytes, exceeding the {max_bytes} byte read limit",
        )
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ToolExecutionError("file_not_found", f"file not found: {path}") from exc
    except OSError as exc:
        raise ToolExecutionError("file_not_found", f"cannot read {path}: {exc}") from exc
    if b"\x00" in data[:_BINARY_SNIFF_BYTES]:
        raise ToolExecutionError("binary_file", f"{path} looks like a binary file (NUL byte found)")
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError("invalid_encoding", f"{path} is not valid UTF-8: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolExecutionError(
            "invalid_encoding",
            f"{path} is not valid UTF-8 (byte {exc.start}); declare an encoding-specific tool if needed",
        ) from exc


def atomic_write_text(path: Path, content: str) -> None:
    """同目录临时文件 + fsync + os.replace：要么旧内容、要么新内容，不产生半写文件。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    except OSError as exc:
        raise ToolExecutionError("spawn_failed", f"cannot prepare write for {path}: {exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise ToolExecutionError("spawn_failed", f"cannot write {path}: {exc}") from exc
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
