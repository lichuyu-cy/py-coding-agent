# 阶段 12：Skill Progressive Loading

## 1. 本节目标

交付技能系统（模块 16）：发现、验证、元数据常驻、正文按需加载。

- `context/skills.py`：`SkillRegistry`、`SkillMetadata`、`SkillBody`、`SkillError`/`UnknownSkillError`；
  目录约定 `<search_dir>/<name>/SKILL.md`（可选 frontmatter：name/description/version）。
- Runtime/Loop 接线：`run(..., skills=[...])` 选择技能；元数据段常驻 system，正文仅选中加载。

明确未实现：远程技能仓库；技能热重载（每次 run 启动时重新发现即可）。

## 2. 新增内容

| 文件 | 类/函数/接口/Enum/数据结构 | 用途 |
| --- | --- | --- |
| `src/coding_agent/context/skills.py` | `SkillRegistry`、`SkillMetadata`、`SkillBody`、`SkillError`、`UnknownSkillError`、`SKILLS_DIR`、`MAX_SKILL_BYTES` | 技能索引与按需加载 |
| `tests/unit/test_skills.py` | 15 个用例 | 发现/去重/加载/错误路径 |
| `tests/integration/test_skills_flow.py` | 5 个用例 | 元数据常驻、正文按需、错误传播 |
| `docs/03-implementation/12-skill-loading.md` | — | 本文档 |

## 3. 修改内容

| 已有文件 | 修改点 | 原因与兼容影响 |
| --- | --- | --- |
| `src/coding_agent/agent/loop.py` | `run(..., extra_sections=())`；`_build_request` 透传 `extra_sections` | 阶段 10 预留的注入点启用 |
| `src/coding_agent/agent/runtime.py` | `run/continue_run` 增加 `skills` 参数；`_skill_sections` 组装元数据段与选中正文 | 新能力；既有调用不变 |

## 4. 每个文件的作用

- `skills.py`：输入为搜索目录与技能名；输出为元数据清单/正文/段落。调用方：Runtime 的
  `_skill_sections`。发现阶段一次性校验（路径包含性、编码、大小、frontmatter）；
  加载阶段重复校验（文件可能已被删除/变更）。
- 测试：单测覆盖注册表语义；集成验证"只有选中的正文出现在 Provider 快照"。

## 5. 核心实现逻辑

```text
发现（SkillRegistry.__init__）：
  搜索目录顺序 → 目录内按名称排序 → 每个 <name>/SKILL.md：
    resolve 后必须仍在搜索目录内（防软链逃逸）→ UTF-8 严格解码 → ≤100KB →
    frontmatter 解析（缺失头部时用目录名/首行）→ 同名单去重（先者优先）
元数据段：Available skills (load on demand): - name: description（不含正文）
select_for_turn(names)：按选择顺序去重 → 逐个 load_body（重读文件，再次校验）
```

## 6. 与已有模块的关联

| 调用方 | 本节模块 | 被调用方 | 共享结构/接口 |
| --- | --- | --- | --- |
| Runtime | `_skill_sections` | `SkillRegistry` | `SkillMetadata`/`SkillBody` |
| Loop | `extra_sections` | `ContextManager.build` | `PromptSection` |

技能内容只是提示文本，不改变工具治理路径（不可越过 Safety/Pipeline）。

## 7. 数据流变化

- 之前：请求段落固定为 system + project（+ 预留 extra_sections）。
- 之后：存在技能目录时 system 追加元数据段；被选中的技能正文作为
  `skill:<name>` 段落进入本次 run 的每个请求。

## 8. 设计原因与备选方案

- **元数据常驻/正文按需**：元数据成本恒定（几十 token），正文只在需要时支付；
- **加载时重校验**：发现与加载之间文件可能变化（删除/编码损坏），必须显式失败而非静默；
- **去重策略（先者优先）**：多目录覆盖时行为确定；备选：后者覆盖（放弃：难以推理）；
- **frontmatter 最简解析**：避免引入 YAML 依赖；支持无头部文件（目录名 + 首行）。

## 9. 测试与结果

| 命令 | 场景/断言 | 结果 | 环境/耗时 |
| --- | --- | --- | --- |
| `python -m pytest tests/unit/test_skills.py tests/integration/test_skills_flow.py` | 排序/去重/无头部/未闭合头部/软链逃逸/缺目录；加载与删除后请求/超大/坏编码/选择去重/未知技能；元数据常驻与正文按需（20 例） | 20 passed | Windows 11 (24H2)；Python 3.13.14；pytest 9.1.1 |
| `python -m pytest` | 全量（含阶段 01–11） | 298 passed in 8.73s | 同上 |

## 10. 当前限制

- 技能选择为"每次 run 显式传入"；无模型自主选择/多来源聚合（扩展项）。
- 技能目录固定为 `<workspace>/.coding-agent/skills`；无版本协商/签名校验。
- 选中技能正文对整个 run 生效（不区分 turn）。

## 11. 后续依赖

- 阶段 15：技能加载可发 SkillLoaded 类事件（当前未接线）。
- 阶段 18：SSE 的 Run 请求可携带 skills 列表（API 已就绪）。

## 12. 面试解释

技能系统的取舍是"入口便宜、正文昂贵"：system 里永远只有名称和描述（几十 token），
真正几百行的操作手册只有被选中时才读取进上下文档。发现和加载是两次独立校验——发现保证
索引可信（含防软链逃逸），加载时重新读取并再次校验（文件可能已被改动），任何异常都显式
报错；去重规则固定为"先发现的目录优先"，让多来源覆盖行为可推理。

## 13. 追踪信息

设计文档版本：设计基线 1.0（2026-09-23）；Git commit：本次提交；完成日期：2026-09-24。

验收条件逐项核对：

- 只有选中的正文出现在 Provider 快照，未选择时 token 成本低：
  `test_selected_body_only_in_requests_others_metadata_only`、`test_no_selection_keeps_metadata_cheap` ✅
- 未选中不加载、重复选择、坏编码、内容超限和删除后请求：
  `test_load_selected_body`（按需）、`test_select_dedupes_and_keeps_order`、`test_invalid_encoding_rejected`、
  `test_too_large_rejected`、`test_deleted_file_reported`、`test_body_missing_after_selection_rejected` ✅

设计偏差：无契约变更。实现注记：`SKILLS_DIR` 固定约定为 `.coding-agent/skills`（与 artifact
目录一致）；`select_for_turn` 返回正文而非仅名称（服务"加入下轮 Context"的直接需求）。
