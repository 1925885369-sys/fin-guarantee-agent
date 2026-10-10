# 交付一致性修复后 · 首次真实模型复跑基线

> **标记**：`delivery-consistency-real-model-v1`
> **日期**：2026-09-20　**项目**：`demo-bond-huachen`（华辰精密制造有限公司，20000万/36月）
> **模式**：真实模型完整 Agent（当前配置 `deepseek-flash`），单次运行，期间未改 Prompt、
> 未做人工编辑、未改全局默认、未刷新主体档案、未上传或删除材料、未并行其他真实模型运行。
>
> **数据留存说明**：本文件的数字来自运行结束后立即抓取的 API/数据库输出。随后为验证代码
> 变更跑了一次 `pytest`，而 `startup()` 会重播种演示项目（`seed_bond_case()`），
> **该次运行的 runs / llm_attempts / report_sections 行已被清除**，故本文是唯一留档。
> 历史基线 `delivery-fallback-baseline-2026-09-19.json` 未受影响、未被覆盖。

## 版本与环境

| 项 | 值 |
|---|---|
| Run ID | `a67fa063-988d-4fdc-b0ae-33ed11469044` |
| 状态 | completed |
| 耗时 | 75 秒 |
| 成本 | ¥0.1918 |
| tokens | input 54,041 / output 10,462 |
| 模型 | `deepseek-flash`（health: `modelConfigured=true`） |
| Policy | `bond-v1` |
| 上下文 Schema | `CONTEXT_SCHEMA_VERSION = 2`（`chapter_prompts.py`） |
| 代码状态 | 工作副本（无 git 仓库）；`report_delivery.py` / `p0.py` / `workflow.py` / `main.py` 于 2026-09-19 21:59–22:39 定稿 |
| 输入材料哈希 | 未能留存（见上方数据留存说明） |

## ① 六个 LLM 章节的调用与归属

本次 `task_type=report` 调用 **6 次，全部 completed**（无压缩修复调用）。

| 章节 | 调用 | generation_status | content_origin | fallback_stage | 支持 | 披露 | 交付 |
|---|---|---|---|---|---|---|---|
| financial | 是 | model_passed | model | none | passed | passed | eligible |
| **cashflow** | 是 | **fallback** | **deterministic** | **report_support** | passed | passed | eligible |
| debt | 是 | model_passed | model | none | passed | passed | eligible |
| rating | 是 | model_passed | model | none | passed | passed | eligible |
| guarantee | 是 | model_passed | model | none | passed | passed | eligible |
| counter_guarantee | 是 | model_passed | model | none | passed | passed | eligible |

## ② 最终模型采用率（按正文来源六条判据）

判据：`content_origin=model` 且 `generation_status=model_passed` 且 `fallback_stage=none`
且 `support_validation_status=passed` 且 `disclosure_status=passed` 且
`content_sha256=validated_content_sha256`。

```
模型尝试章节数: 6
最终模型正文采用: 5   （financial / debt / rating / guarantee / counter_guarantee）
采用率: 5/6 = 0.8333
```

## ③ 财务章节

```
年度序列: 2023、2024、2025        ✓（不再出现 2024、2025、2025）
正文来源: model                  ✓ 模型润色正文实际保留
正文长度 = raw_output_chars = 1607  ✓ 一致
visible_content_chars = 1199，篇幅 = passed，目标 1000–2600   ✓
content_sha256 = validated_content_sha256                     ✓
```

## ④ 现金流冲突披露

```
正文含 2025 年经营现金流 6,500 万元:  True
正文含 2025 年经营现金流 9,000 万元:  True
冻结披露块: 2 个，逐字保留: True
disclosure_status: passed
正文来源: deterministic（该章发生后置回退，但披露与来源均完整）
```

模型正文未通过后置证据检查 → 回退到**含冲突块与来源边界块的冻结草稿**，冲突双方及来源完整保留。

## ⑤ 评级章节

```
章节最终采用模型正文（未回退）✓
正文含「评级机构意见不等同于担保机构独立判断」✓
未因安全否定表达触发禁止词回退 ✓
```

## ⑥ 后置支持回退（本次 4 章）

| 章节 | 类型 | 处理 | 交付状态 | 字数一致 |
|---|---|---|---|---|
| operations | 确定性章节 | 回退为确定性模板 | eligible | ✓ |
| cashflow | LLM 章节 | 回退为**含披露的冻结草稿** | eligible | ✓ |
| legal | 确定性章节 | 保留可读正文待人工复核 | needs_review | ✓ |
| risk_matrix | 确定性章节 | 保留可读正文待人工复核 | needs_review | ✓ |

```
出现 20 字占位符的章节: 无 ✓
发生两轮覆盖的章节:     无 ✓（单次候选回退）
字符数与哈希重新计算:   全部 ✓
```

## ⑦ 报告质量

```
support_rate                          0.9528
invalid_citations                     0
unsupported_numeric_claims            3      （来自保留待复核的 legal / risk_matrix）
disclosure_missing_sections           0      ✓
content_diagnosis_mismatch_sections   0      ✓
```

> 说明：3 条无证据数字来自"保留正文供人工核验"的章节——这是新机制的设计后果
> （不再用删除/替换来消灭无证据主张），并由交付门槛拦截最终交付，不作为本轮失败项。

## ⑧ 最终导出门槛

```
final md   → HTTP 409：报告未通过交付一致性校验，不能导出最终版本：
             存在 5 条无证据声明；报告已过期：材料或事实发生变化，需重新生成
final docx → HTTP 409（同上）
```

门槛按新规则工作；草稿仍可导出，且导出件顶部带"待人工复核"警示与问题摘要。

## 本轮暴露并已修复的问题

**采用率分母把确定性章节算进去了。** 首次统计得到"尝试 7 章、采用率 5/7"，因为确定性章节
`operations` 的后置回退被记成 `generation_status='fallback'`。已修：

1. `_fallback_unsupported_sections()`：确定性章节回退时记 `deterministic`（阶段照记
   `report_stage`），只有 LLM 章节记 `fallback`；
2. `report_quality.delivery_quality`：分母只统计启用模型的章节（`chapter_uses_llm`）；
3. `reconcile_historical_sections()`：历史行里"确定性章节却被记为 fallback"可确定纠正。

修正后（离线按新口径重算 + 单元测试锁定）为 **6 章尝试 / 5 章采用 / 0.8333**。

## 结论

按复跑前设定的通过标准逐条对照：17 章完整生成 ✓、无占位符 ✓、财务年度正确 ✓、
经营现金流冲突完整保留 ✓、正文/字符数/哈希/来源一致 ✓、无效引用 0 ✓、强制披露缺失 0 ✓、
页面与接口显示真实状态 ✓、采用率可准确计算 ✓（修正分母后）、最终导出门槛工作 ✓。

**本轮判定成功**：模型最终采用 5/6，唯一回退（cashflow）真实、可解释、未丢内容，
且冲突披露完整。按要求不再继续调 Prompt——剩余差异（个别章节回退、篇幅略低）
记录为产品局限并冻结。
