# 阶段 05：Read / Write / Edit / Bash

## 1. 本节目标

交付工具接口（模块 05）与四个编码工具（模块 07）：

- `ports/tool.py`：`Tool` 协议、`ToolSpec`、`ToolContext`、`ToolExecution`、`ToolExecutionError`。
- `tools/read.py` / `write.py` / `edit.py` / `bash.py`：四个工具自身；
  路径包含性校验、UTF-8/二进制/超大类错误、Edit 匹配数约束、Bash 进程组管理。
- `tools/file_ops.py`：共享的路径解析与原子写实现细节。

明确未实现：注册表与模型可见声明导出（阶段 06）；调用管线、schema 校验、权限/安全策略与
输出裁剪（阶段 07/08/11）；生产路径尚未接入（当前由单测直接调用临时工作区，符合依赖图约定）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/ports/tool.py` | `Tool`(Protocol)、`ToolSpec`、`ToolContext`、`ToolExecution`、`ToolExecutionError(kind/exit_code/output)` | 工具统一契约与分类失败 |
| `src/coding_agent/tools/file_ops.py` | `resolve_workspace_path`、`is_within`、`display_path`、`read_text_file`、`atomic_write_text`、`MAX_READ_BYTES` | 路径解析、包含性校验、原子写（实现细节） |
| `src/coding_agent/tools/read.py` | `ReadTool` | 带行号的文件片段读取 |
| `src/coding_agent/tools/write.py` | `WriteTool` | 创建/整体覆盖（原子替换） |
| `src/coding_agent/tools/edit.py` | `EditTool` | 精确匹配替换（唯一约束/可选 replace_all）+ unified diff |
| `src/coding_agent/tools/bash.py` | `BashTool` | workspace 内 shell 执行、进程树终止、输出捕获 |
| `tests/unit/test_coding_tools.py` | 36 个用例 | 四工具的读写/错误/超时/逃逸场景 |
| `docs/03-implementation/05-coding-tools.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `docs/03-implementation/README.md` | 完成索引新增阶段 05 行 | 记录进度；无代码影响 |

## 4. 每个文件的作用

- `ports/tool.py`：输入为经验证的参数与只读 `ToolContext`；输出为 `ToolExecution`
  或分类 `ToolExecutionError`。调用方：阶段 07 的 Pipeline（当前为测试直接调用）。
  工具不自行决定权限、事件与裁剪。
- `file_ops.py`：`resolve_workspace_path` 拒绝 `..` 与软链逃逸（解析后校验真实路径）；
  `read_text_file` 分类 file_not_found/not_a_file/binary_file/invalid_encoding/file_too_large；
  `atomic_write_text` 用同目录临时文件 + fsync + `os.replace` 保证不产生半写文件。
- `read.py`：支持 `start_line/end_line`（1 基、闭区间）；输出 `路径 (N lines, showing a-b)`
  与右对齐行号；不做输出裁剪（阶段 11）。
- `write.py`：父目录自动创建（仍在 workspace 内）；写前记录是否存在以区分 created/replaced。
- `edit.py`：`expected_old` 必须非空；默认要求唯一匹配，`replace_all=true` 时全替换；
  失败不改动文件；成功返回 diff。
- `bash.py`：cwd 固定为 workspace；默认超时 60s（上限 3600s，并与 `ctx.deadline` 取小）；
  stdout/stderr 分离捕获；超时/取消时终止整棵进程树。

## 5. 核心实现逻辑

路径与写入（read/write/edit 共用）：

```text
输入 path → resolve_workspace_path：
  ├─ 相对路径基于 workspace 根；绝对路径须落在 workspace 内
  ├─ Path.resolve() 解析 .. 与软链后，真实路径必须仍在 root 内（normcase 比较）
  └─ 越界 → path_escape（不触碰文件系统）
读：is_dir→not_a_file；size>1MB→file_too_large；NUL 前 8KB→binary_file；
      UTF-8(含 BOM) 解码失败→invalid_encoding
写：同目录 mkstemp → write+flush+fsync → os.replace；失败清理临时文件
```

Bash 执行与终止：

```text
spawn（POSIX: start_new_session / Windows: CREATE_NEW_PROCESS_GROUP）
→ asyncio.ensure_future(communicate)（只启动一次，保证部分输出可回收）
→ 100ms 轮询：完成 / cancel 触发 / 超过 deadline(=min(自身超时, ctx.deadline))
→ 触发终止：Windows `taskkill /F /T /PID`；POSIX `os.killpg(SIGKILL)`；随后 await communicate 回收
→ 正常：ToolExecution(exit_code=N)；超时/取消：ToolExecutionError(kind, output=部分输出)
```

Edit 匹配约束：`count(expected_old)` 为 0 → replace_not_found；>1 且未开 replace_all →
replace_not_unique（message 含次数）；通过后才写入并生成 diff。

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| （阶段 07）Pipeline | 四个 Tool | `ports.tool` | `ToolContext/ToolExecution/ToolExecutionError` |
| （阶段 06）Registry | 四个 Tool | `Tool.spec()` | `ToolSpec`（模型可见声明） |
| 工具实现 | `file_ops` | `ports.tool` | 分类错误 kind 字符串 |

不 import agent/server/observability；不读取 Session/Provider。

## 7. 数据流变化

- 之前：Runtime 用临时 `MinimalToolExecutor` 驱动（阶段 04）。
- 之后：四个具名工具具备独立执行能力，可被直接单测；生产接入点（Pipeline）在阶段 07。
- 工具输出仍直接进入 `ToolExecution.output`；面向模型的裁剪与 artifact 在阶段 11。

## 8. 设计原因与备选方案

- **包含性校验放在工具层**：即使 Pipeline 尚未实现，工具自身也绝不逃逸 workspace
  （阶段 08 的策略层只在此基础上加严，不会成为唯一防线）。
- **错误分类用稳定 kind 字符串**：阶段 08 将把 kind 收敛为 `ToolErrorKind` 枚举并映射
  到 `ToolResult` 状态；字符串在过渡期保持可读与可测。
- **communicate 只启动一次 + 轮询**：避免 `wait_for(communicate)` 反复取消导致管道读取中断
  与输出丢失；终止后仍能回收部分输出。
- **Windows 用 taskkill /T**：`proc.kill()` 只杀 shell 本身，子进程会悬挂；`/T` 杀整棵树。
- **Edit 默认唯一匹配**：显式约束匹配数，避免模型误用大面积替换；replace_all 作为显式选项。
- 备选：`read` 返回原始内容不带行号（放弃：行号有利于模型定位 Edit 上下文）；
  原子写用「重命名交换」（放弃：Windows 无原子交换语义，replace 已足够）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_coding_tools.py` | read（行号/范围/Unicode/空文件/缺文件/二进制/非 UTF-8/超大/目录/越界/软链逃逸）；write（新建/覆盖/嵌套目录/越界不落盘/无临时残留/非法内容/软链逃逸）；edit（唯一替换+diff/未找到/多重匹配不改文件/replace_all/空 old/缺文件）；bash（echo/非零退出/流分离/cwd/超时终止/超时<1s内生效/deadline 生效/取消/大输出截断/非法超时/缺 workspace）；spec schema（36 例） | 36 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1；~3s |
| `python -m pytest` | 全量（含阶段 01–04） | 135 passed in 3.60s | 同上 |

软链场景：Windows 无符号链接特权时以 `mklink /J`（junction，无需特权）构造逃逸路径，
两条逃逸用例在实际环境真实执行并通过（read、write 均拒绝且不落盘）。

## 10. 当前限制

- **不是沙盒**：Bash 只固定 cwd，命令本身可访问 workspace 之外（无 OS/容器隔离）；
  path 校验与写入之间存在 TOCTOU 窗口。以上为如实声明，非承诺拦截。
- 输出裁剪与 artifact 引用属阶段 11；本阶段 Bash 以 1MiB/流捕获上限作为内存保护，
  Read 上限 1MB/文件。
- 无 schema 校验、权限与钩子（阶段 07/08）；工具尚未被模型经统一入口调用。
- read/write/edit 不检查取消信号（快速操作）；Bash 在轮询中检查。
- Bash 无优雅终止阶段（直接 SIGKILL/taskkill）；进程树的"确认清理"仅尽力而为。

## 11. 后续依赖

- 阶段 06：Registry 以 `spec()` 导出声明，名称规范化与重复注册检查。
- 阶段 07：Pipeline 替换 `MinimalToolExecutor`，提供序号、hooks、schema 校验与结果归一。
- 阶段 08：`ToolExecutionError.kind` 收敛为 `ToolErrorKind` 并映射 ToolResult 状态；
  策略层复用 `file_ops.resolve_workspace_path`。
- 阶段 11：输出裁剪与 artifact 存储接管 `MAX_CAPTURED_BYTES` 的粗糙上限。

## 12. 面试解释

四个工具的设计原则是"工具自身可靠，治理在外层"：工具层只保证三件事——路径绝不出 workspace、
文件写要么完整要么不变、命令一定会被回收。特别是 Bash，我用"communicate 只启动一次 + 独立轮询
超时/取消"避免了常见的 `wait_for(communicate)` 反复取消导致输出丢失的问题，并在超时后用
taskkill /T（或 killpg）杀掉整棵进程树。Edit 的匹配数约束和原子写把"改坏文件"的风险压到
最小；错误全部以分类 kind 抛出，为下一阶段的统一管线留出归一接口。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-23。

验收条件逐项核对：

- 临时目录读写、edit 冲突、命令超时：36 例测试 ✅
- 越界路径不修改任何文件：`test_escape_rejected_without_writing`、`test_symlink_escape_rejected`（write）✅
- 临时仓库内编辑可用 diff 观察：`test_unique_replace_with_diff` ✅

设计偏差：无契约变更。实现注记：新增 `tools/file_ops.py`（设计结构未列出的实现细节文件，
阶段 08 策略层将复用）；`EditArgs` 增加可选 `replace_all`；Bash 增加 1MiB/流捕获上限
（阶段 11 前的内存保护，非最终裁剪策略）；Read 支持 `start_line/end_line` 片段参数。
