# 报告交付一致性（2026-09-19 修复）

## 缺陷

`workflow._fallback_unsupported_sections()` 在支持校验失败时把章节正文覆盖成确定性模板，
但只更新 `content/citation_ids/validation_status/support_rate`，造成四类交付不一致：

| 后果 | 表现 | 证据（基线快照 `86164ec2`） |
|---|---|---|
| 元数据说谎 | 界面显示"模型润色已应用"，正文却是模板 | financial：正文 493 字 / 诊断 1196 字 |
| 强制披露丢失 | 覆盖用裸模板，不含冲突块与来源边界块 | cashflow 正文不含冲突断言；冲突事实未并列披露 |
| 占位符进报告 | 第二轮把正文换成 20 字占位符 | legal、risk_matrix 正文 = "本章节内容保留为草稿…" |
| 指标自证循环 | 替换后重跑支持校验，模板天然有支持 → 支持率回到 100% | 报告质量全部显示正常 |

## 修复后的机制

### 1. 单一写入入口
`report_delivery.write_report_section()`：任何正文变更（完整生成、后置回退、单章重生成、
人工编辑、应用复核结果）都在同一事务内同步

```
content / citation_ids / allowed_source_ids
content_origin            model | deterministic | human | unknown
generation_status         model_passed | fallback | deterministic | skipped | human_edit
fallback_reason / fallback_stage    chapter_validation | report_support | none
validation_diagnostics
length_status / raw_output_chars / visible_content_chars / effective_min|max_chars
support_validation_status / validation_status（兼容字段）
disclosure_status / delivery_status
content_sha256 / validated_content_sha256
```

### 2. 冻结上下文与强制披露
生成时把 `deterministic_draft` 与 `required_disclosures`（冲突块、来源边界块）一并落库；
后置回退用**冻结草稿**构建候选正文，而不是裸模板。

### 3. 一次候选回退（删除占位符降级）
| 原正文 | 带完整披露的候选草稿 | 处理 |
|---|---|---|
| 通过 | 不检查 | 保留原正文 |
| 不通过 | 通过 | 替换为候选草稿，`fallback_stage=report_support` |
| 不通过 | 仍不通过 | **保留可读正文**，`delivery_status=needs_review` |
| 无可用上下文 | 无法构建 | 保留正文，标记无法完成交付校验 |

### 4. 交付门槛
`delivery_blockers()`：无证据主张 / 无效引用 / 强制披露缺失 / 正文与最近校验哈希不一致 /
报告过期 → 阻止"标记已复核"与最终导出（Word 与 Markdown 同一门槛）；草稿仍可导出，
但导出件顶部带"待人工复核"警示与问题摘要。

### 5. 统计与历史
- 系统生成的强制披露（冲突块、来源边界块）**不再计入支持率**——它们由系统撰写且必须
  逐字保留，其中的断言编号不可能从材料里找到支持（旧实现里正是这类行制造"无证据声明"）。
- `report_quality.delivery_quality`：模型尝试数、采用数、章节回退数、后置回退数、
  待人工复核数、披露缺失数、正文与诊断不一致数、采用率。
- 启动迁移 `reconcile_historical_sections()`：正文与诊断不符的历史行 → 纠正来源、
  标记后置回退、`needs_review`、重算篇幅与哈希；无法确定来源的保持 `unknown/not_checked`。

## 验收

| 验收项 | 结果 |
|---|---|
| 正文的哈希、来源、字符数、诊断一致 | ✅ 单一写入入口强制对齐；专项测试 |
| 回退保留冲突与来源披露 | ✅ 用冻结草稿回退；测试断言冲突块逐字保留 |
| 不再出现 20 字占位符 | ✅ 占位符分支已删除 |
| 支持率通过不掩盖回退/披露缺失 | ✅ `delivery_status=needs_review` + 交付门槛 |
| 历史不一致可检出并可纠正 | ✅ 基线 17 章中 6 章被检出（与 8 条覆盖审计一致） |
| 不新增付费模型调用 | ✅ 全部离线验收 |

测试：`tests/test_report_delivery.py`（5 项）+ 既有回归，全量 254 项。
基线快照：`docs/assets/delivery-fallback-baseline-2026-09-19.json`。

## 执行与验收明细（2026-09-20）

| 方案步骤 | 状态 | 落点 |
|---|---|---|
| ① 保存快照、建失败复现测试 | ✅ | `docs/assets/delivery-fallback-baseline-2026-09-19.json`（17 章正文+元数据、8 条覆盖审计、10 条模型调用）；`tests/test_report_delivery.py` |
| ② 统一正文写入与状态/篇幅/哈希同步 | ✅ | `report_delivery.write_report_section()`；新增 7 列（content_origin / content_sha256 / validated_content_sha256 / fallback_stage / disclosure_status / delivery_status / required_disclosures / deterministic_draft） |
| ③ 替换两轮覆盖、冻结上下文与强制披露 | ✅ | `_fallback_unsupported_sections()` 单次候选回退；占位符分支删除；`assess_section_claims()` 拆分计算与持久化 |
| ④ 对齐四条路径与最终门槛 | ✅ | 完整生成、后置回退、单章重生成、人工编辑、应用复核均走统一入口；`delivery_blockers()` 同时用于最终复核与最终导出（Word/Markdown 同门槛），草稿导出带"待人工复核"警示 |
| ⑤ 前端状态与统计、评测修正 | ✅ | 章节徽标：正文来源/证据支持/强制披露+交付状态；`report_quality.delivery_quality`；`scripts/evaluate.py` 不再用 `validation_status` 推断采用率 |
| ⑥ 离线验收（不新增付费调用） | ✅ | 基线 17 章中 6 章被 `content_diagnosis_mismatch()` 检出（与 8 条覆盖审计一致）；启动迁移 `reconcile_historical_sections()` 纠正 |

测试：全量 **254 项**，连续两轮通过；前端 `tsc` 与构建通过。

### 实施中的一处超出方案范围的修正

**系统生成的强制披露不再计入证据支持率**。冲突断言块与来源边界块由系统撰写、且被要求逐字保留，
其中的断言编号（如 `断言 a-1`）不可能从材料里找到数字支持——把它们当章节主张评分，
只会持续制造"无证据声明"并（旧实现里）触发整章替换。修复后 `assess_section_claims()`
在提取主张前剔除这两类块。

### 尚未执行

- **真实模型复跑**：按方案 §五.7，离线验收完成后再单独确认预算；历史结果不覆盖。
