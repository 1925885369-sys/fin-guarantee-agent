# 债券发行融资担保 Agent 评测

> **表格结构评测单独成文：** 材料表格的解析、`TableBlock v1` 契约与核对链路见 [`table-evaluation.md`](table-evaluation.md)。本文件只覆盖事实抽取、指标、规则、报告与真实模型评测，两者不混算——表格单元格准确率高不代表事实抽取准确率高，反之亦然。

## 评测边界

当前默认评测套件为 `bond`，六家企业及全部材料均为虚构数据：

- `demo-bond-huachen`：制造企业，高应收、客户集中和到期偿债压力。
- `demo-bond-ancheng`：两份核心材料口径一致、经营稳健的对照案例。
- `demo-bond-rongyi`：募集说明书与评级报告发行规模存在冲突。
- `demo-bond-jinghe`：治理权限集中、关联往来和保证责任边界风险。
- `demo-bond-conflict`：评级、审计和访谈口径存在多处冲突。
- `demo-bond-qualitative`：合同真实性、资金流向和关键人员风险。

债券套件默认只使用募集说明书和信用评级报告两份核心 PDF；审计、债务、流水、法律、保证合同和反担保材料只作为华辰 `extended_pack`，不参与默认双文档评测。只有两份核心材料时，完整尽调相关规则必须返回 `insufficient` 或材料缺口，不得被募集说明书备查文件目录误判为已上传。旧贷款案例仅保留代码回归，不在默认项目列表或债券评测中使用。

## 当前确定性结果

2026-08-09 使用 `bond-v1` 政策包完成六案例双文档本地回归，不调用 LLM。结构化结果见 [`assets/bond-two-document-deterministic.json`](assets/bond-two-document-deterministic.json)。

| 指标 | 当前结果 | 验收线 |
| --- | ---: | ---: |
| 完成率 | 6/6 | 6/6 |
| 债券字段准确率 | 100% | >=90% |
| 最低单案例字段准确率 | 100% | >=85% |
| 确定性规则风险召回率 | 97.62% | >=85% |
| 确定性规则风险精确率 | 93.72% | - |
| 关键冲突召回率 | 100% | 100% |
| 候选风险 taxonomy 召回率 | 100% | >=80% |
| 候选风险引用有效率 | 100% | >=95% |
| 无证据数字 | 0 | 0 |
| 平均耗时 | 397 ms | <=5 分钟 |
| 模型成本 | 0 元 | <=2 元/案例 |

确定性模式不执行逐章模型润色，因此报告事实支持率、章节引用覆盖率和真实候选风险精确率不在该结果中计算。当前确定性套件对没有标注候选风险的案例按“不适用”理解，不能使用汇总中的候选精确率替代真实模型评测。

## 运行命令

本地确定性回归：

```powershell
python -m scripts.evaluate --mode deterministic --suite bond --case all --output docs/assets/bond-two-document-deterministic.json
python -m scripts.check_bond_similarity --reference .data/demo_materials/demo-北投 --generated-root .data/demo_materials
```

V2 真实模型试运行需在同一 PowerShell 中配置 `LLM_API_KEY`。先运行三个门槛案例；以下停止线合计 3 元，计入历史成本：

```powershell
python -m scripts.evaluate --mode agent --suite bond --case demo-bond-ancheng --repeat 1 --budget-cny 1 --output docs/assets/bond-pilot-v2-ancheng.json
python -m scripts.evaluate --mode agent --suite bond --case demo-bond-jinghe --repeat 1 --budget-cny 1 --output docs/assets/bond-pilot-v2-jinghe.json
python -m scripts.evaluate --mode agent --suite bond --case demo-bond-qualitative --repeat 1 --budget-cny 1 --output docs/assets/bond-pilot-v2-qualitative.json
python -m scripts.check_bond_pilots
```

Pilot 必须分别满足：安澄无候选风险；景和仅有一个 `governance`；启明仅有一个 `authenticity`。通过后运行 V2 Agent 和债券 one-shot baseline 各 18 次。两批停止线合计 8.2 元，按已记录成本仍在总预算 20 元内：

```powershell
python -m scripts.evaluate --mode agent --suite bond --case all --repeat 3 --resume --budget-cny 6 --output docs/assets/bond-agent-evaluation-post-hardening-v2.json
python -m scripts.evaluate --mode baseline --suite bond --case all --repeat 3 --resume --budget-cny 2.2 --output docs/assets/bond-baseline-evaluation-v2.json
python -m scripts.compare_evaluations --agent-pre docs/assets/bond-agent-evaluation-post-prompt.json --agent-post docs/assets/bond-agent-evaluation-post-hardening-v2.json --baseline docs/assets/bond-baseline-evaluation-v2.json --output docs/BOND_PROMPT_EVALUATION_V2.md --failures-output docs/EVALUATION_FAILURES_PROMPT_V2.md
```

`--resume` 按模式、案例、次数、模型、Prompt 版本和政策哈希跳过已完成组合。每完成一个案例即原子写入检查点；单次失败会记录标准错误类型后继续，累计成本达到预算后停止后续调用。

## Baseline V3 诊断 Pilot

如果 baseline 检查点出现 `empty_baseline_output=true`，不得直接运行 18 次矩阵。V3 已增加非敏感诊断：模型原始 facts 数量、`finish_reason`、输出 Token、响应字段计数、输入材料字符数和空输出原因；不保存完整响应或原文。

在更新代码后先运行华辰单案例：

```powershell
python -m scripts.evaluate --mode baseline --suite bond --case demo-bond-huachen --repeat 1 --budget-cny 0.2 --output docs/assets/bond-baseline-pilot-v3-2-huachen.json
python -m scripts.check_bond_baseline_pilot
```

只有 Pilot gate 通过（至少 10 个有效事实、来源有效率 >=95%、无非法来源、至少命中一条华辰规则且 `finish_reason` 不能为 `length`）后，才运行 baseline V3.1 全矩阵。V2 空输出文件和 V3.1 失败 Pilot 均保留为诊断记录，不覆盖：

```powershell
python -m scripts.evaluate --mode baseline --suite bond --case all --repeat 3 --resume --budget-cny 2.2 --output docs/assets/bond-baseline-evaluation-v3-2.json
```

## 真实模型验收

现有 `bond-agent-evaluation-post-prompt.json` 与 `bond-baseline-evaluation.json` 是不可覆盖的 V1 真实记录。V2 完成后按以下标准验收：

- 债券字段提取准确率 >=90%。
- 财务和债券计算准确率 100%。
- 关键冲突召回率 100%。
- 确定性规则风险召回率 >=85%。
- 候选风险召回率 >=80%，精确率 >=85%。
- 负样本误报率 <=10%，重复候选项为 0。
- 引用精确率 >=95%，无证据数字为 0。
- 章节校验回退率 <=20%，时延异常数为 0。
- 未确认候选风险进入正式风险清单数量为 0。
- 单案例运行时间 <=5 分钟，单报告模型成本 <=2 元。
- 同一案例三次运行的关键召回率波动不超过 15 个百分点。

## 指标解释

字段准确率逐项核对字段键、数值、单位和材料明确出现的期间。冲突召回要求保留所有来源断言且不得自动选值。规则指标只统计确定性 `warning`；`insufficient` 不等同于通过，也不转化为风险。候选风险只有人工确认后才进入正式风险。

报告校验按主张检查引用和数字：数字必须能在来源、事实、指标或规则计算中找到；无效引用、新增数字或审批结论会触发章节模板回退。结构化引用有效不等同于人工完成的语义蕴含判断，最终报告仍需项目经理和风险审查人员复核。
