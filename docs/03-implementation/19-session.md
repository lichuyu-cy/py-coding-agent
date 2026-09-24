# 阶段 19：Session

## 1. 本节目标

交付跨请求可读的会话持久化（模块 23）：原子会话存储、乐观版本控制与跨实例运行锁。

- `ports/store.py`：`SessionRecord`/`SessionAppend`/`SummaryRecord`/`SessionStore` 协议与
  store 错误族（协议契约的一部分，实现层必须抛这些类型）；
- `storage/session_store.py`：`SQLiteSessionStore`——WAL、事务追加、版本校验、
  运行锁表、尾部半写过滤、schema 版本拒绝迁移；
- Runtime 接入：新会话创建持久记录、消息逐条同步落库（与事实日志同序）、
  未命中会话从 store 水合（含最新摘要）、store 运行锁跨实例互斥、workspace 绑定校验；
- 压缩摘要持久化：ContextManager 记录提交前先经 `record_sink` 写 store，重启后回填。

明确未实现：follow-up 队列位置的持久化（仍为内存态，崩溃恢复语义属阶段 20）；
加密存储与多机数据库后端（扩展项）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/ports/store.py` | `SessionRecord`、`SessionAppend`、`SummaryRecord`、`SessionStore`、`SESSION_SCHEMA_VERSION`、store 错误族 | 持久化协议与契约错误 |
| `src/coding_agent/storage/session_store.py` | `SQLiteSessionStore`（create/load/append/acquire_run_lock/release_run_lock/close） | 单机可靠存储实现 |
| `tests/unit/test_session_store.py` | 14 个用例 | 读回/原子性/版本冲突/锁/半写过滤/损坏/schema 拒绝/摘要/转换互逆 |
| `tests/integration/test_session_persistence.py` | 8 个用例 | 落库顺序/工具组关联/重启水合/跨实例锁/workspace 绑定/摘要持久化 |
| `docs/03-implementation/19-session.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/domain/messages.py` | `MessageLog` 增加 `on_append` 观察者钩子 | 追加成功后同步落库；默认 `None`，行为不变 |
| `src/coding_agent/agent/runtime.py` | `session_store` 参数；`_create_session_log`/`_get_session_log`/`_attach_persistence`/`_persist_summary`/`_restore_compaction`/`_bind_workspace`/`_acquire_store_lock`/`_release_store_lock`；`WorkspaceMismatchError` | store 为 `None` 时走原内存路径，阶段 04–18 行为不变 |
| `src/coding_agent/context/builder.py` | `ContextManager` 增加 `record_sink`/`set_record_sink`/`restore_record`；压缩记录提交前调用 sink | sink 失败则内存记录不提交（原子性）；未注入时行为不变 |
| `src/coding_agent/context/compaction.py` | `summary_payload`/`summary_from_payload`/`summary_record_of`/`compaction_record_from_store` | 摘要 ↔ 持久化载荷互逆；缺失字段回退 `unknown` |
| `src/coding_agent/bootstrap.py` | `session_store` 透传 | 组装层 |
| `src/coding_agent/storage/session_store.py` | 错误族改为自 `ports` 重导出 | 避免 agent 层依赖 storage 实现 |

## 4. 每个文件的作用

- `ports/store.py`：Runtime（agent 层）只依赖此协议；`append(expected_version, records)`
  以乐观版本并发控制做原子提交；`acquire_run_lock` 提供会话级独占运行锁。
  错误族（`UnknownSessionRecordError`/`VersionConflictError`/`SummaryVersionConflictError`/
  `SessionSchemaVersionError`/`SessionCorruptionError`）定义在此，运行时可直接捕获而
  不依赖 SQLite 实现。
- `storage/session_store.py`：`create` 返回 `SessionRecord(version=0)`；`append` 在
  `BEGIN IMMEDIATE` 事务内校验版本、按 ordinal 连续插入消息、逐条插入摘要（重复
  summary_version 拒绝）、更新会话版本后 COMMIT，任何异常 ROLLBACK。`load` 校验
  schema 版本（不迁移），逐行解析消息 JSON：仅允许过滤最后一条半写记录
  （`filtered_tail` 计数），其余破损显式失败；摘要 JSON 损坏同样显式失败。
- `agent/runtime.py`：`_attach_persistence` 把 `log.on_append` 挂到 store：每条消息
  在追加成功后被同步写出（事实日志与持久日志同序），版本号乐观推进。
  `_get_session_log` 在内存未命中时 load→重建 `MessageLog`→`registry.register`→
  恢复压缩记录→挂持久化；`_restore_compaction` 取最新摘要回填 ContextManager。
- `context/builder.py`：压缩记录提交前调用 `record_sink`；失败向上传播使压缩不被确认。
- 测试文件：单测直接驱动 store 契约；集成测试用真实 `FakeProvider` 脚本驱动
  Runtime，验证「重启后新实例」的完整水合路径。

## 5. 核心实现逻辑

```text
run(task, workspace, session_id?)/continue_run(session_id)：
  session_id 为空 → store.create（或内存注册表）→ MessageLog + 挂持久化
  session_id 给定 → registry 命中直接返回；未命中 → store.load → 逐条重建 →
                    register → restore_compaction → 挂持久化
  registry.try_acquire（进程内互斥，阶段 04 语义）
  _bind_workspace（store 模式：拒绝静默切换目录）
  store.acquire_run_lock(session_id, run_id)（跨实例互斥）
  循环执行 … 每条消息 log.append → on_append → store.append(事务)
  finally：release_run_lock → registry.release

append 事务（存储层）：
  BEGIN IMMEDIATE → 版本校验（不符 → VersionConflictError + ROLLBACK）
  → 分配起始 ordinal → 插入消息 → 插入摘要（重复版本拒绝）→ 版本 +1 → COMMIT

压缩提交（阶段 14 约束不变，新增持久化）：
  重建成功 → record_sink 写 store（成功）→ 内存 _records 更新 → 事件上报；
  sink 失败 → 异常向上传播，内存记录不提交，历史不受影响
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Runtime | `SessionStore` 协议 | `SQLiteSessionStore` | `SessionRecord`、`SessionAppend` |
| Runtime | `MessageLog.on_append` | 存储 append | `message_to_dict` |
| Runtime | `ContextManager.restore_record` | CompactionRecord | 阶段 14 摘要 |
| ContextManager | `record_sink` | `Runtime._persist_summary` | `SummaryRecord` |
| （阶段 20）Checkpoint | 稳定点保存 | SessionStore | 已提交版本 |
| （阶段 18）Server | 会话读取 | SessionStore | 只读 load |

## 7. 数据流变化

- 之前：消息只存在于 `MessageLog`（内存）；进程退出即丢失；stage 09 的运行锁仅进程内。
- 之后：每条消息追加成功即原子落库；压缩记录与消息共用同一会话版本行；
  重启后新 Runtime 实例可从 store 水合继续（历史、摘要、工作区绑定均恢复）。
- 不变：历史 append-only；`MessageLog` 仍是事实源；store 只是其持久投影。

## 8. 设计原因与备选方案

- **每条消息一次事务**：崩溃最多丢失"正在追加的那一条"，不会出现半批数据；
  比"整轮 run 一次提交"更细，代价是小事务开销（本地 SQLite 可接受）。
- **乐观版本而非悲观锁**：并发写者冲突显式失败（`VersionConflictError`），
  调用方可重载后重试；比长事务锁简单且无死锁。
- **运行锁独立于消息事务**：锁是会话级互斥（跨实例），与写入事务解耦；
  锁表行含 run_id，非持有者释放不生效。
- **半写过滤仅限最后一条**：崩溃残留只可能出现在写尾部；更深的损坏必须显式失败，
  避免带着不确定历史继续运行（设计文档模块 23 异常约束）。
- **schema 版本拒绝迁移**：本期无迁移需求，显式失败优于静默猜测。
- **workspace 绑定只在 store 模式强制**：持久会话身份含工作区指纹；
  内存模式保持阶段 04 的"最近使用"语义，避免破坏既有行为。
- 备选：先日志文件再回放（放弃：需要自己实现事务与损坏检测）；把摘要存进消息流
  （放弃：摘要不是消息事实，混合会破坏 append-only 语义）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_session_store.py` | 读回顺序与版本；批内原子（冲突不写）；未知会话；独占锁/跨连接可见；单条半写过滤；多条坏行拒绝；中段损坏拒绝；schema 拒绝迁移；摘要读回与版本冲突；摘要损坏；载荷互逆与缺失回退（14 例） | 14 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest tests/integration/test_session_persistence.py` | 消息按序落库；工具组关联保留；重启 run 水合后再追加；restart continue 水合尾部；跨实例锁冲突→释放后继续；workspace 切换拒绝；无 store 行为不变；压缩摘要落库与重启恢复（8 例） | 8 passed | 同上 |
| `python -m pytest` | 全量（含阶段 01–18） | 416 passed | 同上 |

## 10. 当前限制

- follow-up 队列位置仍为内存态：重启后进行中的 follow-up 项不自动恢复（阶段 20 处理）。
- `SessionRecord.run_metadata` 已预留未填充（阶段 20 Checkpoint 使用）。
- 消息逐条同步写 SQLite：高并发多会话时单连接串行化（本期单机 CLI/Server 场景足够）。
- 无 store 模式下的内存会话不参与任何持久化（设计使然）。

## 11. 后续依赖

- 阶段 20：基于已提交版本做 Checkpoint/Resume；崩溃注入时以 store 的提交边界判断
  "不确定副作用不自动重放"；`run_metadata` 将承载稳定点信息。
- 阶段 21：把"重启水合"纳入故障注入矩阵；Mini Repo 端到端复用 SQLiteSessionStore。
- 阶段 22：SWE-bench 适配器每个实例一个持久会话（隔离与可审计）。

## 12. 面试解释

会话持久化的核心是"已完成的事实绝不丢失、未完成的事实绝不假装完成"。消息逐条
在事务里落库，崩溃最多丢最后一条半写记录（加载时被过滤并计数）；版本号提供乐观
并发控制，两个写者同时推进时后到者显式冲突而不是静默覆盖。跨实例互斥用数据库
锁表而不是文件锁，避免"锁文件残留"这类工程问题；压缩摘要与消息共用同一会话版本，
保证重启后摘要版本继续单调、不会重放旧摘要。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本提交（代码+测试+本文档同提交）；
完成日期：2026-09-24。

验收条件逐项核对（dependency-graph.md 阶段 19）：

- 读回消息顺序：`test_create_append_load_preserves_order_and_versions`、
  `test_messages_persist_in_order`；工具关联 `test_tool_round_persists_calls_and_results` ✅
- 冲突运行拒绝：`test_exclusive_acquire_and_release`、
  `test_store_run_lock_blocks_second_runtime`（跨实例）✅
- 模块 23 测试清单：崩溃模拟（半写过滤）、并发冲突（版本冲突）、迁移拒绝 ✅

设计偏差：无。实现注记：错误族定义在 `ports/store.py`（协议契约），
`storage/session_store.py` 重导出以兼容导入路径；`on_append` 钩子属事实日志的
观察者扩展，不改 append-only 语义。
