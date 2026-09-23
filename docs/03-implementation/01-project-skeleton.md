# 阶段 01：Project Skeleton

## 1. 本节目标

建立可导入、可执行测试的空应用骨架：包结构、构建与测试配置、仓库 README、实现记录目录。

明确未实现的后续能力：Runtime/Loop、消息与运行态类型、Provider、工具、上下文、事件、SSE、
存储与恢复等全部运行时能力，由后续阶段交付。本阶段不包含任何运行逻辑。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `pyproject.toml` | — | 构建元数据（setuptools + src 布局）；pytest 配置（testpaths、`pythonpath=["src"]`、asyncio 模式） |
| `README.md` | — | 项目定位、目录结构、测试命令、边界声明 |
| `.gitignore` | — | 忽略 Python 缓存/构建产物与编辑器目录 |
| `src/coding_agent/__init__.py` | `__version__` | 包根；提供版本信息 |
| `src/coding_agent/domain/__init__.py` 等 10 个分层包 `__init__.py` | — | 占位并声明各层职责与依赖方向（domain/ports/agent/context/tools/providers/observability/storage/server/benchmark） |
| `tests/unit/test_smoke.py` | `test_package_importable`、`test_layers_importable`、`test_version_format` | 冒烟：包与各层可导入、版本格式合法 |
| `docs/03-implementation/README.md` | — | 实现记录规范入口与完成索引 |
| `docs/03-implementation/01-project-skeleton.md` | — | 本文档 |

## 3. 修改内容

无（仓库首个阶段提交，此前只有一次空的初始化提交）。

## 4. 每个文件的作用

- `pyproject.toml`：声明包名 `coding-agent-harness`、Python ≥3.11、可选的 dev 依赖（pytest、
  pytest-asyncio）。pytest 通过 `pythonpath=["src"]` 直接找到源码，无需安装即可运行测试。
- `README.md`：面向仓库读者的入口，说明当前状态（阶段 01）、目录结构、测试命令与安全边界声明。
- 各层 `__init__.py`：仅含 docstring，声明该层的职责与允许的依赖方向；不含任何可执行代码，
  避免把未来能力写成已实现。
- `tests/unit/test_smoke.py`：调用方为 pytest；输入为无；输出为断言结果。

## 5. 核心实现逻辑

无运行逻辑。验证链路：`python -m pytest` → 收集 `tests/` → `pythonpath` 注入 `src/` →
`import coding_agent` 及 10 个分层包成功 → 断言版本为三段数字串。

## 6. 与已有模块的关联

无（首阶段无运行时模块）。后续所有模块都将在此骨架的包结构内就位：阶段 02 → `domain/`；
阶段 03 → `ports/`、`providers/`；阶段 04 → `agent/`；依此类推。

## 7. 数据流变化

无消息、工具、事件数据流；仅建立代码、测试与文档的目录约定。

## 8. 设计原因与备选方案

- **src 布局**：隔离"已安装包"与"仓库源码"，避免从仓库根目录误导入未安装代码；备选 flat 布局
  （放弃：后续阶段出现多样入口时更易混淆）。
- **pytest `pythonpath` 配置而非强制 editable 安装**：保证"克隆即可测"，不污染本机环境；
  备选 `pip install -e .`（保留为 README 中的可选步骤）。
- **10 个分层包一次建齐**：与设计文档 `architecture.md` 的目标结构一致，让依赖方向在目录层面
  可见；各包内只有职责声明，不虚构模块文件。
- **包名 `coding_agent`**：与设计文档结构示例一致；发行名用 `coding-agent-harness`。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest` | 包可导入、10 个分层包可导入、版本格式合法（3 用例） | 3 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1；< 1s |

## 10. 当前限制

无任何运行时能力：没有消息类型、没有 Provider、没有工具、没有事件与存储；`domain` 等包是空的
职责声明。安全边界（路径/命令策略）尚未实现，也不对本阶段做任何安全承诺。

## 11. 后续依赖

阶段 02 在 `domain/` 下新增 `messages.py`、`state.py`、`errors.py` 并配套单测；
阶段 03 在 `ports/` 与 `providers/` 落地 Provider 协议与 Fake。所有后续阶段沿用本骨架的
目录与测试配置，不预期迁移。

## 12. 面试解释

我先把骨架做成"可导入、可测试、依赖方向可见"的最小形态：用 src 布局和十个分层包把
domain→ports→agent→实现→入口的依赖方向固化在目录里，并用 pytest 的 pythonpath 让测试零安装可跑。
这个阶段刻意不写任何运行代码，避免在依赖未就绪时把未来能力写成"已完成"；验收就是
仓库克隆后一条命令能通过冒烟测试。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 空应用可导入：`tests/unit/test_smoke.py::test_package_importable`、`::test_layers_importable` ✅
- 测试入口可执行：`python -m pytest` 全绿 ✅

设计偏差：无。
