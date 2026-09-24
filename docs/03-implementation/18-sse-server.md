# 阶段 18：SSE Web Server

## 1. 本节目标

交付 HTTP/SSE 适配器（模块 22）：

- `server/schemas.py`：Pydantic HTTP 入参/出参（仅边界校验，不进入核心 Runtime）；
- `server/sse.py`：`id/event/data` 帧编码 + 心跳注释帧；
- `server/app.py`：`create_app(runtime, workspace=...)` — Session 创建/查询、Run 启动/查询/中止、事件订阅（Last-Event-ID 重放、重放窗口错误、断连仅退订）。

基础设施：Starlette（ASGI 框架）+ uvicorn（可选 `[server]` extra）；测试用 httpx ASGITransport 进程内直连。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/server/schemas.py` | `RunRequest`、`AbortRequest`、`SessionCreated`、`SessionInfo`、`RunAccepted`、`RunInfo`、`ErrorEnvelope` | HTTP 边界模型 |
| `src/coding_agent/server/sse.py` | `encode_sse`、`encode_sse_error`、`HEARTBEAT_FRAME` | SSE 帧编码 |
| `src/coding_agent/server/app.py` | `create_app`、`Service` | 路由与运行协调 |
| `tests/integration/test_sse_server.py` | 8 个用例 | 生命周期/错误码/SSE/重连/断连/中止 |
| `docs/03-implementation/18-sse-server.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/agent/control.py` | `RunControl` 记录 `session_id` | 会话→活动 run 查询 |
| `src/coding_agent/agent/runtime.py` | `run(..., run_id=)` 可选注入；新增只读查询 `active_run_ids/active_run_id/workspace_of` | Server 需提前获知 run 标识与状态 |
| `pyproject.toml` | dependencies 增 `pydantic>=2`、`starlette>=0.37`；dev 增 `httpx`；新增 `[server]` extra（uvicorn） | HTTP 边界依赖 |

## 4. 每个文件的作用

- `app.py`：输入为 HTTP 请求；输出为 JSON/SSE 响应。只调用 Runtime 公共入口
  （run/abort/submit_*）与总线查询；不 import 工具实现；不触碰状态机写权限。
- `sse.py`：`id`=run 内 seq（重连游标），`event`=类型，`data`=事件 JSON；心跳不占 id。
- 测试：ASGITransport 直连（无端口），覆盖格式、重连、窗口、断连与中止语义。

## 5. 核心实现逻辑

```text
路由：POST /sessions；GET /sessions/{id}；POST /sessions/{id}/runs（202 + run_id）；
      GET /runs/{id}；POST /runs/{id}/abort；GET /runs/{id}/events（SSE）
启动：同步前置校验（未知会话/冲突）→ 预取 run_id（Runtime 支持注入）→ 登记 → 调度任务 →
      sleep(0) 保证 run 已入活动表再返回 202
SSE：游标 = Last-Event-ID 或 ?cursor=；先重放审计（after_seq>cursor）→ 再订阅邮箱实时转发；
      AGENT_END 后关闭；心跳注释帧（默认 15s，可配置）；断连 finally 退订（不 Abort）
错误：404 unknown_session/unknown_run；409 run_conflict / replay_window_expired；
      422 invalid_request；一律 {"error": {"code","message"}}
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| 外部客户端 | 路由 | Runtime | run/abort/查询 |
| SSE 生成器 | EventBus | `events(after_seq)`/`subscribe/drain` | 重放与实时 |
| 测试 | httpx ASGITransport | Starlette app | 进程内驱动 |

## 7. 数据流变化

- 之前：Runtime 只能被进程内代码调用。
- 之后：HTTP 可创建会话、启动任务、订阅事件（含重连）、显式中止、查询最终状态；
  SSE 仅观察者语义（断连不等于 Abort）。

## 8. 设计原因与备选方案

- **Starlette 而非 FastAPI**：环境无 FastAPI；Starlette 是同等基础设施（FastAPI 的底座），
  设计允许"Web 框架仅承担基础设施角色"；
- **run_id 注入**：消除"启动后才得知 ID"的竞态；sync 前置校验 + Runtime 权威锁双保险；
- **重放窗口检查**：审计为有界 deque，游标早于保留窗口时返回 409 而不是静默丢事件；
- **断连语义**：生成器 finally 退订；运行继续（测试断言 abort_count==0）；
- 备选：长轮询（放弃：SSE 是设计指定形态）；认证/多租户（明确不在本期范围）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/integration/test_sse_server.py` | 会话创建/启动/查询、稳定错误码（404/409/422）、并发冲突、abort 端点、SSE 帧格式与终结唯一、Last-Event-ID 无重复重放、过期游标 409、断连不中止（8 例） | 8 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–17） | 394 passed in 18.21s | 同上 |

## 10. 当前限制

- 无认证/多租户（设计明示不在本期范围）；单进程内存态（阶段 19 后具备持久会话）。
- 无 TLS/压缩；uvicorn 需 `pip install -e ".[server]"` 后自行启动。
- SSE 帧暂不区分流式增量的合并策略（客户端按 event 名称过滤即可）。

## 11. 后续依赖

- 阶段 19：会话持久化后 GET /sessions 可跨重启；run 查询可结合 Session 版本。
- 阶段 21：SSE 断连/重连纳入故障注入矩阵。

## 12. 面试解释

HTTP 层做成"薄适配器"：路由只调用 Runtime 的公共入口，状态机、锁与事件都留在核心层；
启动 run 的竞态用两个手段消除——同步前置校验加运行前注入 run_id，再让步一个调度周期保证
202 返回时活动表已可见。SSE 的关键语义有三个：id 用 run 内 seq 支持 Last-Event-ID 精确重放；
游标早于有界审计窗口时明确 409 而不是静默丢数据；客户端断连只退订，绝不隐式中止运行。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-24。

验收条件逐项核对：

- 客户端可启动任务、订阅、重连、显式中止并查询最终状态：
  `test_create_session_run_and_query`、`test_stream_frames_format_and_terminal_once`、
  `test_reconnect_with_last_event_id_replays_without_duplicates`、`test_abort_endpoint` ✅
- HTTP 参数校验、并发 run 冲突、SSE 格式、重连游标、断连后 Run 继续：
  对应用例（422/409/帧格式/重放/`test_disconnect_does_not_abort_run`）✅

设计偏差：无契约变更。实现注记：以 Starlette 替代 FastAPI（环境未安装 FastAPI，设计仅要求
Web 框架承担基础设施角色）；`Runtime.run` 增加可选 `run_id` 注入参数（Server 竞态消除）；
`GET /runs/{id}` 的字段来自 Metrics 快照（阶段 16 契约复用）。
