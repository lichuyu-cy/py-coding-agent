# Coding Agent Harness (Python)

从零实现的 Python Coding Agent Harness：自建 Agent Loop、工具管线、上下文管理、事件与恢复机制。
本仓库不是任何 TypeScript/Python 实现的移植，只借鉴 Pi Agent 的架构思想（以源码为设计参照）。

**当前状态：项目骨架（阶段 01）。** 尚无 Runtime 实现；各阶段能力与完成记录见
[docs/03-implementation/](docs/03-implementation/README.md)。设计文档位于工作区兄弟目录
`coding-agent-harness-design/`，开发按 `docs/01-overview/dependency-graph.md` 的 23 个阶段顺序推进，
每阶段一个提交（代码 + 测试 + 实现说明）。

## 目录结构

```text
src/coding_agent/
  domain/          # 消息、状态、错误值对象（无 I/O）
  ports/           # Provider / Tool / Store / Tokenizer 协议
  agent/           # Runtime、Loop、控制信号
  context/         # 上下文组装、预算、裁剪、压缩、技能
  tools/           # 注册表、管线、安全、Read/Write/Edit/Bash
  providers/       # Provider 实现（fake 优先）
  observability/   # 事件总线、指标、日志、流
  storage/         # 会话与检查点存储
  server/          # SSE HTTP 入口
  benchmark/       # SWE-bench 适配器（阶段 22 起）
tests/{unit,integration,e2e,fixtures}/
docs/03-implementation/   # 每阶段实现说明（NN-*.md）
```

## 运行测试

```bash
python -m pytest
```

无需安装即可运行（`pyproject.toml` 已配置 `pythonpath = ["src"]`）；如需安装：

```bash
python -m pip install -e ".[dev]"
```

## 边界说明

- 本地验证以确定性 Fake Provider 为主；不频繁调用真实模型。
- SWE-bench 正式评测只在阶段 23 执行一次；此前阶段不得运行。
- 安全边界如实声明：路径允许列表与命令词匹配降低误用风险，不构成 OS 级沙盒。
