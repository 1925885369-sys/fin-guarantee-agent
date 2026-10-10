# 报告生成 Prompt 体系（后端 + 前端）

债券/贷款担保尽调报告的逐章 Prompt 是一个"**出厂契约 + 网页覆盖**"体系：强类型 YAML 契约决定章节能写什么、怎么校验；网页只能改写**送入模型的 System Prompt 文本**，改写按层级持久化在后端数据库。本文梳理全链路。

---

## 1. 三层 Prompt 解析（重要）

任一 LLM 章节（`llm: true`）生成时的 System Prompt 按以下优先级取第一个存在项：

```
① 项目覆盖   report_prompt_overrides(project_id, chapter_key)   —— 只影响该项目
② 全局默认   global_report_prompts(chapter_key)                 —— 网页“设为全局默认”，影响所有未①覆盖的项目
③ 出厂默认   backend/prompts/{bond_,}chapters.yaml 渲染         —— 代码内置
```

实现：`backend/app/chapter_prompts.py::chapter_prompt(chapter_key, project_id)`；
运行接线：`backend/app/llm.py::rewrite_section()`（`system_prompt = chapter_prompt(...)`）。

> 注意：覆盖只替换 **System Prompt 文本**。结构化契约（事实白名单、输出形状、引用上限、
> 数值/支持率校验）始终来自 YAML 与 `chapter_guard`/`p0` 校验器，网页改 Prompt 无法绕过这些硬校验；
> 确定性章节（`llm: false`）不调用模型，网页上只读。

## 2. 后端结构

### 2.1 出厂契约（强类型 YAML）
- 文件：`backend/prompts/chapters.yaml`（贷款）、`backend/prompts/bond_chapters.yaml`（债券）；
  章节集合 `CHAPTER_KEYS` / `BOND_CHAPTER_KEYS`。
- 模型（`chapter_prompts.py`，pydantic `extra="forbid"`，键必须精确匹配）：
  - `ChapterPromptPack {version, global_rules, chapters}` — 全量包；
  - `ChapterPromptConfig`：`llm`、`role`、`goal`、`required_inputs`、`required_sections`、
    `preserve_blocks`、`rules`、`forbidden`；
  - `ChapterInputContract`：`fact_keys / metric_keys / rule_keys / source_kinds` 白名单（未知键直接校验失败），
    `max_evidence_items`(≤36)、`max_evidence_chars`(≤30k)；
  - `ChapterOutputContract`：`required_phrases`、`required_missing_markers`、`forbidden_patterns`、
    `max_citations`、`attribution_mode`（**无字数项**：篇幅只由 `NarrativeContract` 决定）；
  - `NarrativeContract`：`tone`、`paragraph_order`、`preferred_min/max_chars`；历史配置中的 `min/max_paragraphs` 仅为兼容字段，不参与 Prompt 或章节校验。
    `list_mode(allowed/limited/prohibited)`、`avoid_openings`、`prohibited_terms`。
- 渲染：`chapter_system_prompt()` 把契约拼成 System Prompt（角色、目标、全局约束、区块、规则、
  禁止事项、行文顺序/段数/列表模式、JSON 输出声明）。`lru_cache` 常驻内存，改 YAML 需重启生效。

### 2.1.1 生成状态与证据支持状态（必须分开）

`report_sections` 上两个状态含义不同，禁止互相覆盖：

| 字段 | 含义 | 写入方 |
|---|---|---|
| `generation_status` | 模型结果是否真正进入报告：`model_passed` / `fallback` / `deterministic` / `skipped` | 生成流程（`workflow._generate_draft`）、单章重生成接口 |
| `support_validation_status` | 证据支持率算出的结论：`pending` / `passed` / `warning` | 仅 `p0.validate_report_support()` |
| `validation_status` | 兼容字段，与 `support_validation_status` 同值 | 同上 |

`validate_report_support()` **不得**覆盖 `generation_status` 与 `fallback_reason`——此前它把两者写在
同一列，导致 4 个模型回退章节在界面上全部显示为"通过"。历史行由启动迁移
`p0.migrate_report_generation_status()` 补齐（有真实回退原因 → `fallback`；确定性章节 →
`deterministic`；其余 LLM 章节 → `model_passed`）。

`validation_diagnostics` 存非敏感诊断（**不保存被拒绝的模型正文**），每条都带定位信息：

```json
{
  "unexpected_numbers": [{"raw": "9000.0万元", "normalized_value": "9000", "normalized_unit": "万元",
                          "reason": "new_numeric_value", "context": "…为9,000.0万元，来源…"}],
  "safe_unit_conversions": [{"raw": "2亿元", "normalized_value": "20000", "normalized_unit": "万元",
                             "reason": "safe_unit_conversion"}],
  "ignored_format_numbers": ["1", "2", "3"],
  "page_numbers_ignored": ["28", "31", "32"],
  "forbidden_matches": [{"term": "投资建议", "context": "本报告不构成投资建议",
                         "classification": "safe_negative"}],
  "source_attribution_restored": false
}
```

只有 `classification: "forbidden"` 与 `reason: "new_numeric_value"` 会触发回退；`safe_negative`
与 `safe_unit_conversion` 仅作记录。

### 2.1.2 校验器的三类降误报规则

1. **数字比对做格式归一**（`_number_mentions`）：千分位、小数尾零与展示精度视为同一数值
   （`48,000`＝`48000`、`70.0`＝`70`、`9.8901`＝`9.89`——系统指标存 4 位、报告按 2 位引用是惯例）。
   **等值量级换算允许**：按预定义换算表 `MONEY_UNIT_FACTORS`（元/万元/亿元）折算到万元后严格
   相等即视为同一个数（`20000万元`＝`2亿元`），记为 `safe_unit_conversion`；换算后不等的
   （`20000万元` ≠ `2.5亿元`）仍然拒绝。Markdown 标题编号、有序列表编号、引用别名（`E01`）、
   **页码引用（`第 28、31、32 页`）**在提取前剔除；年度交给 `allowed_periods` 单独约束，
   且 4 位数金额/比率（`1939万元`）不再被当成年度。真正新增的金额/比例/评级数字仍然拒绝。
2. **否定语境豁免**（`_is_negated_mention`）：命中位置前一小句内出现否定词即放行——
   "不得推断授信获批概率""不构成投资建议""不得表述为已具备可执行覆盖"是合规边界表述。
   "不排除/不否认"之类 hedge 不豁免；"预计授信能够获批""建议投资本期债券"仍然拒绝。
   此前靠逐词字符串替换处理，每加一个词就要写一次例外。
3. **来源归属改为确定性保留块**（`SOURCE_ATTRIBUTION_BLOCK`）：使用冻结主体档案时由系统把
   "> 数据来源边界：…"写入确定性草稿，模型漏写由 `_preserve_chapter_boundaries()` 补回，
   校验器只要求该块存在，不再要求模型复述"冻结主体档案"字样（等价表述不再导致整章回退）。
   该块与冲突块一样**不计入**篇幅。

### 2.1.3 章节上下文瘦身与引用别名（模型侧 vs 校验侧）

`chapter_guard.build_chapter_context()` 返回的 dict 身兼两职：既是**服务端校验上下文**（`validate_chapter_output` 读 `facts[].assertion_status`、`scope`、`origin_types` 等，并据此拼装允许数字/主体白名单），也是**模型输入**。因此：

- **不得为省 token 直接删该 dict 的字段**。裁剪只发生在 `llm.rewrite_section` 的 payload 边界
  （`_model_context()` 投影副本），校验侧拿到的仍是完整上下文，白名单与 `input_hash` 语义不变。
- 投影保留模型必须看到的：`assertion_status`（单来源事实要求正文写"尚未独立验证"）、`scope`
  （口径）、`text_value`（定性事实）、`origin_types`（"冻结主体档案"归属）、
  `cross_checks.status/summary`（勾稽状态）、`conflict_block`、`allowed_periods`、`prompt_config`。
- 投影丢弃纯簿记：`input_hash`、`prompt_sha256/version`、`relevant_review_tasks`、行 UUID、
  `profile_snapshot_id`、原始 `source_ids`、`review_status`、嵌套重复的 draft/evidence 等。
  实测 6 个债券 LLM 章节省 32%–55%（financial 35.5k → 16.2k 字符），允许事实集、允许引用集与
  冲突块逐项一致。
- **引用别名**：`allowed_citation_ids` 与 evidence `id` 以 `E01…` 形式下发，模型回显别名，
  解析后立即映射回真实 `c-*`（落库、报告 API、白名单校验均不变）。别名集之外的 ID 仍被拒绝；
  `_structured_call(allowed_source_ids=…)` 必须同步传别名集，否则模型返回别名会被判 `invalid_source`。
- `CONTEXT_SCHEMA_VERSION` 随上下文一起进入 `input_hash` 与 `llm_attempts.request_hash`，
  便于评测区分投影前后的版本。

### 2.1.2 章节元数据（标题与分部，单一来源）
- `chapter_prompts.py` 中的 `BOND_CHAPTER_TITLES`（17 章中文标题）与
  `BOND_CHAPTER_PARTS`（六大部分及章节分组）、`BOND_CHAPTER_PART`（章→分部反查）。
- 三个消费方全部引用该来源，不再各自维护：
  - `bond_report.py` 生成报告时写入 `report_sections.title`；
  - `report_outline.grouped_sections()`（Markdown/Word 导出分部、目录与论文式编号）；
  - `main.py` report-prompts API 的 `title`/`part` 字段——**未生成报告的项目同样返回中文标题**，
    不再回退为英文 chapter_key。
- `required_sections` 只进入 Prompt 文本（"必须保留以下内容区块"），校验器不做文本匹配；
  各章取值已与 `bond_report.py` 确定性草稿的分块标题逐章对齐（YAML 3.2.0）。
- **篇幅（`preferred_min_chars` / `preferred_max_chars`）**：由
  `chapter_prompts._length_guidance()` 渲染成"正文篇幅目标为 X 至 Y 字"进入 Prompt，
  两个编译器（出厂 `chapter_system_prompt`、编辑 `compile_structured_prompt`）共用同一措辞。
  运行时口径由 `effective_chapter_config()` 给出（`length_policy`）：**目标即用户设置**，
  系统没有独立的字符硬顶，也不再钳制用户值。
  - **下限是软约束**：只作指引与报告质量告警（`short`），不触发修复、不回退——证据稀薄的
    章节本就该写得短，硬性下限会让它整章回退成更短的模板草稿。
  - **上限是写作目标，不是交付门槛**：可见正文超出目标 → 最多做**一次**定向压缩
    （`workflow.repair_chapter_length()`，复用同一 payload、同一证据与同一引用别名表）。
    压缩稿安全通过就采用它（**仍然偏长也保留**）；压缩稿引入新数字/无效引用/禁止结论则
    保留压缩前的合格正文并记 `length_repair_failed`。两条分支都不回退、不阻断交付。
    完整生成与单章再生成共用这一个函数，口径不会分叉。
  - 篇幅判定与安全校验分开：`validate_chapter_output` 只判事实、引用、数字与结论边界
    （`reasons`），篇幅由 `assess_chapter_length` 单独给出（`length` /
    `length_only_failure`）。**字数不再进 `reasons`**，因此长正文不会被判不合格。
  - 偏长会使章节的 `delivery_status` 标为 `needs_review` 供人工留意，但篇幅**不进**
    `delivery_blockers()`，不阻断最终导出。
- 字符口径 `chapter_guard.count_report_chars()`：只统计**分析正文**——不计系统生成且强制
  逐字保留的冲突块、系统数据表、来源边界块，也不计 Markdown 标记与空白；
  `raw_output_chars`（全文字符数）仅供审计。**前端只展示后端算出的
  `visible_content_chars`，不另行实现**。
- **输出额度**：报告章节用 `LLM_REPORT_MAX_OUTPUT_TOKENS`（默认 10000），**不受**
  `LLM_MAX_OUTPUT_TOKENS`（默认 5000）钳制；抽取、候选项等其他任务维持原额度。
  该额度是**单次请求的输出上限**，不保证产出等量汉字，也仍受模型服务自身能力限制；
  服务端不接受时记为 `output_limit_rejected` 并带上请求额度与服务端原文。
- 分部与章节的**论文式标题层级与编号**（一、/（一）/1.）见 [report-structure.md](report-structure.md)，
  由 `report_outline.py` 在导出与展示时计算，不写入正文。

### 2.2 持久化（SQLite）
| 表 | 键 | 含义 |
|---|---|---|
| `report_prompt_overrides` | (project_id, chapter_key) | 项目级覆盖，网页“保存 Prompt”写入 |
| `global_report_prompts` | chapter_key | 全局默认，网页“设为全局默认”写入 |
| `audit_events` | — | `report_prompt_update/reset/global_update/global_reset` 全程留痕 |
| `report_sections` / `llm_attempts` | — | 生成时记录 prompt_version/prompt_sha256/input_hash、调用哈希（追溯用） |

启动迁移：`seed_global_prompts_from_latest_overrides()`（`main.startup()` 调用，一次性）——
若全局表为空，取各章“最近一次项目覆盖”作为全局默认初值。

启动重编译：`recompile_structured_prompts()`（`main.startup()` / `lifespan` 调用）——
`chapter_prompt()` 返回的是**存库的 `prompt` 文本**，而结构化行的该文本由 `structured_json`
派生。编译器措辞变化（如新增篇幅指引）后，只有重编译才能让**已保存**的覆盖生效；否则网页
预览（现场编译）与实际生成（读库）会不一致。`preserved_legacy` 行是人工撰写的文本，不重编译。

### 2.3 REST 端点（均在项目鉴权下）
| 方法/路径 | 作用 |
|---|---|
| GET  `/api/projects/{id}/report-prompts` | 每章返回：`title`(中文，元数据源)、`part`(所属分部)、`prompt`(生效值)、`default_prompt`(出厂)、`overridden`(项目)、`updated_at`、`global_overridden`、`global_updated_at`、`llm`、`required_sections` |
| PATCH `/api/projects/{id}/report-prompts/{key}` | 保存项目覆盖 |
| DELETE `/api/projects/{id}/report-prompts/{key}` | 撤销项目覆盖（回落全局/出厂） |
| PATCH `/api/projects/{id}/report-prompts/global/{key}` | **设为/更新全局默认**（DB 落盘、跨重启生效） |
| DELETE `/api/projects/{id}/report-prompts/global/{key}` | 撤销全局默认（回落出厂 YAML） |

约束：`real_reference` 项目只读（403）；章节键必须在 `BOND_CHAPTER_KEYS`。

### 2.4 生成运行时的接线
```
workflow.draft 节点
 └─ chapter_guard.build_chapter_context()  → 确定性草稿 + 证据 + 冲突块 + 输入白名单
     └─ llm.rewrite_section(chapter_key, chapter_context)
         ├─ system = chapter_prompt(chapter_key, project_id)      # ①→②→③
         ├─ user   = JSON{确定性草稿、required_sections/preserve_blocks、允许引用、证据}
         └─ 返回后 chapter_guard.validate_chapter_output()
              → 通过则入库；不通过/异常则回退确定性草稿（validation_status=fallback 并审计）
verify 节点 → validate_report_support() 再校验证据/引用 → Word 导出前置门槛
```

## 3. 前端结构

- 入口：App 导航"报告 Prompt" → `PromptWorkspace`（`frontend/src/App.tsx`）。
- 布局：左侧章节导航按 API `part` 字段分为六大部分展示（中文分部名 + 中文章节名，序号 01–17 连续）；
  右侧配置面板按"写作视角 / 章节目标 / 内容要求 / 行文组织 / 补充业务要求"五张卡片分组，
  只读风控约束默认折叠，移动端用 `<optgroup>` 分组下拉。
- 数据：`api.reportPrompts()` 拉全章；编辑态仅存本地 `drafts`，**点按钮才写后端**。
- 每章状态徽标：`项目自定义` / `全局默认`（该项目未覆盖但全局存在）/ `出厂默认`；
  头部汇总显示"X 个项目自定义 · Y 个全局默认"。
- 按钮（仅 LLM 章节可编辑；真实参考项目整页只读）：
  - 保存 Prompt → 项目覆盖（PATCH project）；
  - 撤销本项目覆盖 → DELETE project；
  - **设为/更新全局默认** → PATCH `…/global/{key}`，立即对所有未覆盖项目生效；
  - **撤销全局默认** → DELETE `…/global/{key}`，回落出厂。
- api 封装：`frontend/src/api.ts`（`saveReportPrompt/resetReportPrompt/setGlobalReportPrompt/resetGlobalReportPrompt`）；
  类型 `ReportPrompt` 见 `frontend/src/types.ts`（含 `global_overridden/global_updated_at`）。

生效时机：**重新生成该章节**或**重新运行 Agent** 时读取最新 Prompt；已生成内容不追溯改写。

## 4. 运维速查
- 改全局默认：网页每章「设为全局默认」（存 DB，永久）；或改 YAML（需重启进程）。
- 恢复出厂：网页「撤销全局默认」。
- 项目级差异：网页「保存 Prompt」，仅该项目生效；优先级高于全局。
- 排查某次生成用了哪版 Prompt：`llm_attempts.request_hash` + `audit_events` + 章节 `prompt_version/prompt_sha256`。
- 已保存覆盖迁移为全局默认：启动时自动一次性执行（全局表为空时）。
