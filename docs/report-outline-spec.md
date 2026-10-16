# 报告目录配置（report-outline）实现规范

> 依据：`PLAN_new.md` §二/§三/§四、`PLAN_new_p0_p1.md` P1-1、`PLAN.md` §三/§四.3。
> 本文是**后端（P1-1）与前端（P1-3）共用的接口契约**，也是 P1-2（小节响应）/P1-4（自定义章节检索）的读取依据。实现与本文冲突时，先改本文再改代码。

## 一、数据模型

保存到新表（`database.py` SCHEMA 追加；无需 MIGRATIONS 加列）：

```sql
CREATE TABLE IF NOT EXISTS report_outlines (
  scope TEXT NOT NULL,                 -- 'project' | 'global'
  project_id TEXT NOT NULL DEFAULT '', -- global 行固定 ''
  outline_version TEXT NOT NULL,       -- 每次保存生成新 uuid
  payload_json TEXT NOT NULL,          -- 完整目录 JSON（见下）
  updated_at TEXT NOT NULL,
  PRIMARY KEY (scope, project_id)
);
```

目录 JSON（payload）——**顺序即顺序**（`order` 由服务端每次保存重排为 1..n，客户端传的 order 一律忽略并重算）：

```json
{
  "outline_version": "<uuid>",
  "parts": [
    {
      "part_id": "part-1",
      "title": "报告摘要及审查范围",
      "order": 1,
      "chapters": [
        {
          "chapter_id": "chapter-scope",
          "template_key": "scope",
          "title": "审查范围、材料状态和免责声明",
          "order": 1,
          "generation_mode": "template",
          "evidence_scope": "chapter_contract",
          "subsections": [
            {"subsection_id": "sub-scope-1", "title": "审查范围", "order": 1, "prompt": "", "enabled": true}
          ]
        }
      ]
    }
  ]
}
```

字段规则：

- `part_id`：内置分部固定 `part-1`~`part-6`（与 `BOND_CHAPTER_PARTS` 顺序一致）；一级分部**暂不编辑**（接口不接受增删改分部，仅原样透传）。
- `chapter_id`：内置章节 = `chapter-<template_key>`（如 `chapter-governance`）；自定义章节 = `chapter-<uuid>`。**改名不换 ID。**
- `template_key`：内置章节 = yaml 键；**自定义章节 = `null`**。
- `generation_mode`：**服务端派生，只读**。内置 `chapter_uses_llm(key)` 为真 → `generate`，否则 `template`；自定义章节恒 `generate`。保存时忽略客户端值，按此规则重写。
- `evidence_scope`：**服务端派生，只读**。内置 = `chapter_contract`（沿用现有章节输入契约）；自定义 = `prompt_retrieval`（P1-4 的 Prompt 驱动检索）。保存时重写。
- `subsections`：`subsection_id` 章内唯一、稳定（改名不换）；`prompt` 空串=沿用章级要求；`enabled: false` = 暂不参与生成。每章 ≤ 30 条，整份目录章节数 ≤ 60。
- 标题：去除首尾空白、不得含换行、长度 1–60 字符。
- `material_roles` / `document_ids`（可选，P1-4）：材料角色（document_role 白名单）与文件白名单；空 = 默认检索本项目全部已解析材料；文件白名单仅项目范围有意义（全局配置不保存项目文件 ID）；各 ≤ 20/30 项。

## 二、默认目录（builtin）生成

`builtin_outline()` 由现有元数据生成，**纯函数、确定性**（每次调用结果一致，含 ID）：

1. 遍历 `BOND_CHAPTER_PARTS`（6 分部 × 17 章）。
2. 章节 `title` 取 `BOND_CHAPTER_TITLES`；`chapter_id = "chapter-" + key`。
3. `subsections` 初始化：“从有效配置的行文顺序、内容要求按顺序去重初始化”（`PLAN_new.md` §六）——
   取**出厂 YAML** 的 `narrative_contract.paragraph_order` + `required_sections`，按出现顺序去重，每条一个小节；
   `subsection_id = f"sub-{key}-{n}"`（n 从 1 起）；`prompt = ""`；`enabled = true`。
4. `outline_version = "builtin-v1"`（内置标识，非 uuid）。

## 三、读取链与合并

- 项目范围（`scope=project`）：项目覆盖 > 全局覆盖 > builtin。逐字段是**整份覆盖**（不是 merge）：谁存在用谁，空则回落。
- 全局范围（`scope=global`）：全局覆盖 > builtin。
- 提供函数：
  - `effective_outline(project_id) -> dict`：项目链结果（P1-2/P1-4 生成侧读取用）。
  - `stored_outline(scope, project_id) -> dict | None`：该 scope 已存行（无覆盖 None）。
  - `outline_scope_state(scope, project_id) -> dict`：给接口用，含 `outline`/`source`/`outline_version`/`config_sha256`（无覆盖时 version 与 sha 均为 `""`）。
  - `outline_fingerprint(outline) -> str`：`sha256(payload_json 规范化序列化)`；每次保存与读取现算，作为并发基准。
  - `outline_for_chapter(project_id, chapter_key)`（P1-2 用）：在 effective_outline 中按 template_key 找章节，返回 None（不在目录/被删）或章节 dict。

## 四、接口契约

三接口，均挂在项目路径下（复用项目令牌/归档门禁，写入权限与 P0-A 同一判据）：

```text
GET    /api/projects/{id}/report-outline?scope=project|global
PUT    /api/projects/{id}/report-outline?scope=project|global
DELETE /api/projects/{id}/report-outline?scope=project|global
```

GET 响应：

```json
{
  "scope": "project",
  "outline": { ...目录 JSON... },
  "source": "project|global|builtin",
  "outline_version": "",            // 该 scope 已存版本；无覆盖 = ""
  "config_sha256": "",              // 该 scope 并发基准；无覆盖 = ""
  "effective_source": "builtin",    // 项目链生效来源（仅 scope=project 有意义）
  "has_project_override": false,
  "editable": true, "edit_reason": "",
  "global_editable": true, "global_edit_reason": ""
}
```

PUT 请求：

```json
{
  "outline": { ...完整目录 JSON（无需带 outline_version）... },
  "expected_outline_version": "…或 null",
  "expected_outline_sha256": "…或 null"
}
```

- 并发检查：两值**传了就严格比对**（现状为空也拦——与 P0-A `_check_prompt_concurrency` 同口径），不符 409。不传=兼容旧客户端跳过。
- 校验（任一不符 → 422，逐条说明）：至少 1 个二级章节；`chapter_id` 全局唯一；`subsection_id` 章内唯一；内置 `template_key` 必须 ∈ `BOND_CHAPTER_KEYS` 且 `chapter_id` 与 `template_key` 对应一致；`template_key=null` 仅限 `chapter-<uuid>` 形式 ID；标题/条数规则见 §一。
- 服务端重写：`order` 全部重排；`generation_mode`/`evidence_scope` 派生；`outline_version` 换新 uuid。
- 事务：单行 UPSERT + `audit_event`（`report_outline_update`，`scope` 进详情）。**不调用模型、不修改报告正文、不删除项目其他配置。**
- 响应 = 同 GET（保存后的新状态）。

DELETE：删该 scope 行（幂等）；审计 `report_outline_reset`；响应 = 同 GET（回落后状态）。
项目 scope 删除后回落全局/内置（`PLAN_new.md` §二第 19 行“撤销项目覆盖后恢复当前全局版本”）。

权限：`scope=project` 写用 `_require_prompt_editable(project_id, request)`；`scope=global` 写用 `_require_global_prompt_editable(project_id, request)`。GET 一律可读（`prompt_edit_permissions` 返回 `editable`/`global_editable`/理由）。

## 五、存储与版本语义

- 只存**当前版本**；旧版本通过 `audit_events`（before/after 摘要）与报告快照留档。
- 报告生成时（P1-2）：`outline_version` + `outline_fingerprint` 进入章节 `input_hash`、尝试台账与报告快照，历史报告按生成时目录展示。
- 保存目录**不自动再生报告**（与 Prompt 保存一致：只标记待再生）。

## 六、前端契约（P1-3 用）

- API：`reportOutline(id, token, scope)` / `saveReportOutline(id, token, scope, outline, concurrency)` / `resetReportOutline(id, token, scope, concurrency)`；`concurrency = { expectedOutlineVersion, expectedOutlineSha256 }`，**空值整体省略字段**（同 P0-A 的 `_concurrencyBody` 教训）。
- 类型（types.ts）：

```ts
export type OutlineSubsection = { subsection_id: string; title: string; order: number; prompt: string; enabled: boolean };
export type OutlineChapter = { chapter_id: string; template_key: string | null; title: string; order: number; generation_mode: "generate" | "template"; evidence_scope: "chapter_contract" | "prompt_retrieval"; subsections: OutlineSubsection[] };
export type OutlinePart = { part_id: string; title: string; order: number; chapters: OutlineChapter[] };
export type ReportOutline = { outline_version: string; parts: OutlinePart[] };
export type ReportOutlineState = { scope: "project"|"global"; outline: ReportOutline; source: string; outline_version: string; config_sha256: string; effective_source: string; has_project_override: boolean; editable: boolean; edit_reason: string; global_editable: boolean; global_edit_reason: string };
```

- UI（`PLAN_new.md` §四）：左侧二级章节目录（改名/删除/上移下移/分部内新增）；右侧显示该章三级小节编辑列表（标题、Prompt、排序、启用）＋原有章级 Prompt 编辑区保留；两种范围分别缓存草稿；删除前确认；409 复用 `concurrencyHint()`；确定性章节小节区显示“暂不参与模型生成”。

## 七、生成集成（P1-4 自定义章节）

- **章节集合跟随生效目录**：生成时遍历 `effective_outline` 的 parts/chapters——内置章节按目录顺序生成（标题以目录为准，section_key 仍是内置键）；目录中被删除的内置章节不再生成（删除只影响今后报告结构）；目录不可读/为空时回退 17 章全量。
- **自定义章节**（`template_key=null`）：section_key = `chapter_id`；无确定性草稿（模型直接撰写）；`chapter_prompts.is_custom_chapter_key()` 识别 `chapter-<uuid>` 形态并合成配置（llm/generate/空白名单/宽松篇幅），章级 Prompt 走既有覆盖链（`report_prompt_overrides` 以 chapter_id 为键）。
- **Prompt 驱动检索**：`build_chapter_context` 的 evidence_query 追加目录章节标题/章级要求与小节任务；`retrieve_chunks` 用 `allowed_roles`（=material_roles）与 `document_ids` 筛选（空 = 默认全部已解析材料）。
- **再生**：自定义章节以空草稿全新撰写——旧模型正文不得成为下一次生成的边界。
- **展示**：`grouped_sections`/`section_labels` 接受 `outline`（`outline_for_grouping(project_id)`）——标题以目录为准、按目录顺序分组；不在目录中的旧正文附后可见。网页/GET /report/Markdown/Word 四出口同源；快照用冻结 outline。
- **API**：`GET /report-prompts` 返回目录中的自定义章节（`chapter_key`=chapter_id、`llm=true`、`title`/`part` 取自目录）；PATCH/预览/重置对自定义章节放开（非 uuid 未知键仍 404）。
