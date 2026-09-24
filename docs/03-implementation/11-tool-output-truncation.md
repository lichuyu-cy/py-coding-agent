# 阶段 11：Tool Output Truncation

## 1. 本节目标

交付输出裁剪与原件托管（模块 15），接管阶段 07 预留的 `OutputProcessor` 缝：

- `context/truncation.py`：`TruncationStrategy`（HEAD/TAIL/HEAD_TAIL）、`TruncationPolicy`、
  `DisplayedOutput(text, omitted_count, artifact_ref, strategy)`、`ArtifactStore`、
  `ToolOutputTruncator`（实现 pipeline 的 OutputProcessor 协议）；
- Pipeline 协议演进：`OutputProcessor.process(outcome, invocation)`（处理需要工具名与工作区）；
- Bootstrap 默认接线裁剪器；`RunLimits`/契约的 12,000 字符/输出默认值落地。

明确未实现：token 维度的配额（阶段 13 的 TokenManager 提供输入）；结构化表格/片段检索（扩展项）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/context/truncation.py` | `TruncationStrategy`、`TruncationPolicy`、`DisplayedOutput`、`ArtifactStore`、`ToolOutputTruncator`、`choose_strategy`、`ARTIFACT_DIR` | 裁剪策略、原件托管与 pipeline 适配 |
| `tests/unit/test_truncation.py` | 17 个用例 | 策略选择、三种裁剪、预算、Unicode、artifact 与失败路径 |
| `tests/integration/test_output_truncation.py` | 2 个用例 | 大输出经完整 run 的裁剪与观察 |
| `docs/03-implementation/11-tool-output-truncation.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/tools/pipeline.py` | `OutputProcessor.process(outcome, invocation)` 签名演进（原 `process(outcome)`）；调用点同步 | 裁剪需要工具名/工作区；阶段 07 文档记录的先期接口按本阶段定稿 |
| `src/coding_agent/bootstrap.py` | 默认注入 `ToolOutputTruncator()`；新增 `truncation_policy` 参数 | 生产路径默认裁剪 |
| `tests/unit/test_pipeline.py` | `SpyProcessor.process` 增加 invocation 形参 | 协议同步 |
| `.gitignore` | 忽略 `.coding-agent/`（artifact 目录） | 防止本仓库运行时产物入库 |

## 4. 每个文件的作用

- `truncation.py`：输入为工具输出文本、类型（工具名/error_kind）与工作区；输出为
  `DisplayedOutput`（显示文本 + 省略计数 + 引用）或经 `process` 返回的新 `ToolOutcome`。
  调用方：Pipeline（唯一运行时调用者）；Context/Bootstrap 由配置注入。
- `ArtifactStore`：完整输出写入 `workspace/.coding-agent/artifacts/<tool>-<hex>.txt`；
  单件上限 2,000,000 字符；存储失败静默降级为"无原件"（不抛错、不中断调用）。
- `tests`：单测覆盖策略与边界；集成覆盖"大输出 → 裁剪 → 归档 → 观察"全链路。

## 5. 核心实现逻辑

```text
choose_strategy(kind, error_kind):
  error_kind 非空 → TAIL（长错误优先保留尾部堆栈）
  kind == "read" → HEAD（文件读取保留头部）
  其余 → HEAD_TAIL（命令输出头尾兼顾）

truncate(kind, output, error_kind?, workspace?):
  ① len(output) ≤ max_chars → 原样返回（omitted=0、无引用）
  ② 超限 → ArtifactStore 存完整原件（失败则 ref=None）
  ③ budget = max_chars - 标记预留(200)；按策略切片（字符串切片天然 UTF-8 码点安全）
  ④ 省略标记含计数与引用："[... N characters omitted; full output: <ref|not stored>]"
  ⑤ 显示文本 = 保留片段 + 标记 ≤ max_chars

process(outcome, invocation)（pipeline 适配）:
  按 invocation.record.name / context.workspace 裁剪 → replace(outcome, content, artifact_ref)
```

脱敏与边界：artifact 不写公共日志；标记只含计数与引用；`_MARKER_RESERVE` 保证
"显示内容在预算内"可被严格断言（≤ max_chars）。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Pipeline | `ToolOutputTruncator.process` | — | OutputProcessor 协议 |
| Truncator | `ArtifactStore` | workspace 文件系统（受控子目录） | artifact_ref 字符串 |
| Loop/Context | 无直接调用 | — | outcome.content/artifact_ref 经既有字段进入 ToolResult |

## 7. 数据流变化

- 之前：工具输出原样进入 ToolResult 与模型观察（Bash 仅 1MiB/流捕获上限）；
  阶段 07 的 `artifact_ref` 只会承接工具自带 artifacts。
- 之后：超限输出 → 显示文本被裁剪（显式省略标记）→ 完整原件落 artifact →
  `ToolResult.artifact_ref` 指向原件；模型观察与历史都在预算内。

## 8. 设计原因与备选方案

- **按类型分策略**：错误保留尾部（堆栈在尾部）、文件读取保留头部（文件开头信息密度高）、
  命令输出头尾兼顾——对应设计中的 HEAD/TAIL/HEAD_TAIL。
- **标记预留固定额度（200 字符）**：让"显示 ≤ max_chars"成为精确断言，而非近似；
  备选：动态两遍计算（放弃：复杂度收益低）。
- **artifact 放 workspace 内受控目录**：用户可直接访问、路径天然包含在工作区校验内；
  备选：临时目录（放弃：可发现性差、跨重启语义含糊）。
- **存储失败降级**：裁剪永远成功，最坏情况是"无原件"标注——保证关键路径不因归档失败中断。
- **协议签名演进**：`process(outcome, invocation)` 是本阶段对阶段 07 预留缝的定稿
  （当时文档已注明"接口将按阶段 11 需要调整"）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_truncation.py` | 策略选择（read/错误/默认）、未超限不动、空输出、三种策略的保持与标记、严格预算（三种策略）、Unicode 安全、策略参数校验、artifact 存储/上限/失败降级/无工作区、pipeline 适配（截断 + 保留既有 ref）（17 例） | 17 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/integration/test_output_truncation.py` | 30k 字符 bash 输出：历史 ≤1000、含省略标记、原件可读、模型观察含标记与引用；3000 行 read：HEAD 保留头部（2 例） | 2 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01–10） | 279 passed | 同上（首跑含编译开销；单测耗时正常，最长 1.5s） |

## 10. 当前限制

- 配额按字符而非 token（阶段 13 接入 TokenManager 后统一）。
- artifact 单件上限 2,000,000 字符：超限时归档副本被截断并带标注（"原件"语义有上限）。
- artifact 目录当前不随会话/运行分层（同工作区共享一个目录）；清理策略未实现。
- 裁剪读取的是 `outcome.content`（完整字符串在内存中构造）：裁减的是"进入模型与历史的量"，
  不是工具侧的捕获量（Bash 仍有 1MiB/流上限）。

## 11. 后续依赖

- 阶段 13：`TruncationPolicy.max_chars` 将由 TokenManager 的配额驱动。
- 阶段 16：Metrics 可统计裁剪次数/省略字符数（事件字段已具备）。
- 阶段 19：artifact 引用随 ToolResult 持久化（相对路径 + workspace 校验）已有格式。

## 12. 面试解释

裁剪的目标是"模型看到有界的、仍可诊断的输出，同时原始事实不丢失"。我按输出类型分三种策略：
错误保尾部堆栈、文件读取保头部、命令输出头尾兼顾；完整原文落到工作区内受控的 artifact 目录，
进入模型与历史的是裁剪文本 + 省略计数 + 原件引用，所以任何一个大输出都能被事后完整审计。
几个工程细节：标记预留固定额度让"显示不超过预算"成为可精确断言的性质；字符串切片天然不会
截断 UTF-8 码点；归档失败时降级为"无原件"标注而不是让调用失败。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-24。

验收条件逐项核对：

- 显示内容在预算内：`test_displayed_text_within_budget`（三策略）+ 集成断言（≤1000/≤800）✅
- 用户能知晓发生裁剪且可访问授权原件：省略标记 + artifact 文件在集成测试中读取验证 ✅
- 空输出、超大、Unicode 边界、stdout/stderr 分离、artifact 缺失：
  空/超大/Unicode/存储失败（artifact 缺失）用例 ✅；stdout/stderr 分离由 Bash 工具层
  （阶段 05）保证，裁剪在其合并后的文本上进行（本阶段不改变工具输出格式）
- 存储失败仍返回有限文本并标注无原件：`test_storage_failure_still_returns_finite_text` ✅

设计偏差：无契约变更。实现注记：`OutputProcessor` 协议签名按本阶段需要定稿为
`process(outcome, invocation)`；artifact 目录 `.coding-agent/artifacts/` 为本项目自定路径
（设计中"受控路径"的落地形式）；`.gitignore` 已忽略该目录。
