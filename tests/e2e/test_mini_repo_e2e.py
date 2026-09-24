"""阶段 21 端到端：Mini Coding Repo 的完整修复流程（模块 26 验收）。

在临时 Git 仓库中放置一个可修复的失败测试；Fake Provider 脚本驱动真实
Runtime/Pipeline/工具完成「读取 → 编辑 → 运行测试 → 报告」，断言：
文件 diff、仓库内测试通过、工具观察与最终报告一致。
不接触真实用户仓库；git 仅用于临时目录。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coding_agent.bootstrap import build_runtime
from coding_agent.domain.messages import UserMessage
from coding_agent.domain.state import RunStatus, StopReason
from coding_agent.providers.fake import FakeProvider, FakeResponse, ScriptedToolCall

GIT = shutil.which("git")

BUGGY_CALC = '''"""Simple calculator module."""


def add(a, b):
    """Return the sum of a and b."""
    return a - b
'''

FIXED_CALC = '''"""Simple calculator module."""


def add(a, b):
    """Return the sum of a and b."""
    return a + b
'''

TEST_CALC = '''from calc import add


def test_add_positive_numbers():
    assert add(2, 3) == 5
'''


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    if GIT is None:
        pytest.skip("git is required for the mini repo E2E")
    repo = tmp_path / "mini-repo"
    repo.mkdir()
    (repo / "calc.py").write_text(BUGGY_CALC, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    run_git(repo, "init", "-q")
    run_git(repo, "add", ".")
    run_git(
        repo,
        "-c",
        "user.name=harness-e2e",
        "-c",
        "user.email=harness@example.invalid",
        "commit",
        "-q",
        "-m",
        "initial buggy version",
    )
    return repo


class TestMiniRepoRepair:
    async def test_read_edit_run_tests_then_report(self, mini_repo: Path) -> None:
        """完整修复路径：读失败测试 → 读实现 → 修复 → 跑测试 → 最终报告。"""
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "test_calc.py"}, id="call_1"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="read", arguments={"path": "calc.py"}, id="call_2"),),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="edit",
                            arguments={
                                "path": "calc.py",
                                "expected_old": "return a - b",
                                "replacement": "return a + b",
                            },
                            id="call_3",
                        ),
                    ),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": "python -m pytest -q"}, id="call_4"),),
                ),
                FakeResponse(
                    content="Fixed calc.add; the repository test passes.", stop_reason=StopReason.END_TURN
                ),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("Make the failing test pass.", mini_repo)

        assert result.status is RunStatus.FINISHED
        assert result.turns == 5
        assert result.tool_calls == 4
        assert result.final_text == "Fixed calc.add; the repository test passes."

        # 文件系统事实：唯一一处修改且符合预期
        assert (mini_repo / "calc.py").read_text(encoding="utf-8") == FIXED_CALC
        diff = run_git(mini_repo, "diff")
        assert "-    return a - b" in diff
        assert "+    return a + b" in diff
        stat = run_git(mini_repo, "diff", "--stat")
        assert stat.count("1 file changed") == 1

        # 仓库内测试确实通过（工具观察）
        log = runtime.registry.get(result.session_id)
        pytest_result = log.find_result("call_4")
        assert pytest_result is not None
        assert pytest_result.status.value == "completed"
        assert "1 passed" in pytest_result.content

        # 最终报告与事实一致：消息顺序 user → ... → assistant(final)
        texts = [message.content for message in log.messages if isinstance(message, UserMessage)]
        assert texts == ["Make the failing test pass."]

    async def test_failed_edit_observed_then_repaired(self, mini_repo: Path) -> None:
        """tool→result→repair：编辑冲突作为结构化失败进入下一请求，第二次修复成功。"""
        provider = FakeProvider(
            [
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="edit",
                            arguments={
                                "path": "calc.py",
                                "expected_old": "return a * b",  # 冲突：文件中不存在
                                "replacement": "return a + b",
                            },
                            id="call_1",
                        ),
                    ),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(
                        ScriptedToolCall(
                            name="edit",
                            arguments={
                                "path": "calc.py",
                                "expected_old": "return a - b",
                                "replacement": "return a + b",
                            },
                            id="call_2",
                        ),
                    ),
                ),
                FakeResponse(
                    stop_reason=StopReason.TOOL_CALLS,
                    tool_calls=(ScriptedToolCall(name="bash", arguments={"command": "python -m pytest -q"}, id="call_3"),),
                ),
                FakeResponse(content="Recovered from the edit conflict.", stop_reason=StopReason.END_TURN),
            ]
        )
        runtime = build_runtime(provider=provider)
        result = await runtime.run("Fix the failing test.", mini_repo)

        assert result.status is RunStatus.FINISHED
        log = runtime.registry.get(result.session_id)
        failed = log.find_result("call_1")
        assert failed is not None
        assert failed.status.value != "completed"
        assert failed.error_kind == "replace_not_found"

        # 失败观察（含结构化 kind）完整进入第二次请求；最终文件为修复后版本
        second_request = provider.requests[1]
        assert any("replace_not_found" in message.content for message in second_request.messages)
        assert (mini_repo / "calc.py").read_text(encoding="utf-8") == FIXED_CALC

        passed = log.find_result("call_3")
        assert passed is not None
        assert "1 passed" in passed.content
