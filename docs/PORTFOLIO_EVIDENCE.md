# 简历与面试证据保留说明

整理日期：2026-10-10。本说明只建立证据索引，不重新评测，不修改历史结果。

## 对外可引用的历史证据

| 能力 | 原始证据 | 适用范围 |
| --- | --- | --- |
| Agent 效果 | [Agent v2](assets/bond-agent-evaluation-post-hardening-v2.json) | 六套虚构债券案例，各三次，共 18 次；原结果版本 |
| Baseline 对照 | [Baseline v4](assets/bond-baseline-evaluation-v4.json) | 同六案例、18 次 one-shot 对照，不是完整 Agent 工作流 |
| 对比与成本 | [历史对比报告](BOND_FINAL_EVALUATION.md) | 展示效果、成本、耗时与回退率，不只展示优点 |
| 迭代与失败定位 | [失败记录](EVALUATION_FAILURES_FINAL.md)、[历史结果索引](assets/README.md) | 旧结果与失败版本保留，不选择性删除 |
| 确定性回归 | [双文档结果](assets/bond-two-document-deterministic.json) | 不调用模型，不代表模型生成效果 |
| 财务表结构 | [表格评测](table-evaluation.md) | 虚构材料闭环回归；真实材料定位不计入准确率 |
| 工程验收历史 | [发布清单](RELEASE_CHECKLIST.md) | 文档记录，不替代原始完整日志，也不证明当前代码全量通过 |

以上链接为既有结果，不因本次整理变为当前版本的验收结果。历史模型评测未记录精确 Git 提交时，标记“未知”，不能使用归档时的 HEAD 冒充。

## 简历表述参考

> 面向债券发行融资担保场景，构建确定性规则与 LLM 协同的尽调辅助 Agent；在六套虚构债券案例、各三次的历史评测中，字段准确率为 100%，较 one-shot baseline 提升约 11.5 个百分点，规则风险召回率提升约 34.1 个百分点，并实现来源追溯、冲突保留和无依据输出拦截。

面试补充边界：样本规模小、材料与金标为自建虚构案例；报告事实支持率为程序校验口径，不是专家认可率。Agent 同时承担更多工作，成本与耗时不能当作同等任务的效率比较。历史 Agent 有约 19.4% 的章节校验回退率，最终输出支持率不等于模型首次输出正确率。北投、中邮用于真实材料兼容验证，没有真实客户效果或生产准确率结论。

## 本地私有归档

运行以下命令，只复制已有结果，不调用模型，不连接数据库：

```powershell
.\.venv\Scripts\python.exe scripts/archive_portfolio_evidence.py
```

输出位置：`.release/portfolio-evidence/<归档时间>/`。该路径已被 Git 忽略。

- `manifest.json`：原始来源、SHA-256、文件大小、归档时 Git 状态和原结果版本。
- `files/docs/assets/`：既有评测 JSON、失败/说明文档与历史视频副本。
- `files/frontend/test-results/`：已有截图、trace 和运行状态，不声明已完成脱敏。
- `files/acceptance_output/`：已有真实模型验收明细与报告，仅本地保留。
- `files/.data/demo_materials/`：只保留六套虚构案例的清单和财务表金标，不复制 PDF。
- `files/backend/`：归档时的 Prompt、政策和核心定义；不是已证明对应历史评测的输入。

副本校验逐文件完成。重复归档使用新目录，不覆盖原归档。归档不等于数据库备份，所有 `.data*` 数据库与材料继续保留原位。

## 缺口与公开边界

当前能找到的 `.last-run.json` 仅记录 `passed`，不能证明测试数量、退出码、执行命令或对应代码版本。未找到完整日志时不把记忆中的通过数量写成新验收记录。后续免费回归应保留 pytest JUnit XML、Playwright JSON/HTML 报告、构建完整日志，以及执行前后的 Git 提交和工作区状态；存在跳过或失败时一并记录。

真实验收中的 `status=completed` 只代表流程完成；交付阻断、章节回退和无依据输出必须另外核对。不要把 `acceptance_output` 的残留失败解读为通过。

公开前不得发送数据库、访问令牌、API Key、真实 PDF、真实导出报告或未脱敏 trace。历史视频是底稿，投递前检查界面、文件名、数据来源和版本。公开结果仍需人工核对敏感信息与版权。

在提取必要结果并确认独有运行/决议审计记录已备份前，不删除 `.data-review-real-*`、`.data-accept` 等真实验证目录。E2E/API/UI 临时目录也须确认未被进程使用；本次不执行清理。
