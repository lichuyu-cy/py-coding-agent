"""阶段 01 冒烟测试：包结构可导入、版本可读。

覆盖 dependency-graph.md 阶段 01 的验收条件：
- 空应用可导入
- 测试入口可执行
"""

import coding_agent


def test_package_importable() -> None:
    assert isinstance(coding_agent.__version__, str)


def test_layers_importable() -> None:
    import coding_agent.agent
    import coding_agent.benchmark
    import coding_agent.context
    import coding_agent.domain
    import coding_agent.observability
    import coding_agent.ports
    import coding_agent.providers
    import coding_agent.server
    import coding_agent.storage
    import coding_agent.tools

    assert coding_agent.domain is not None


def test_version_format() -> None:
    parts = coding_agent.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
