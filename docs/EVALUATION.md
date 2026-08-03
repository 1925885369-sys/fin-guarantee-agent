# 评测方案与当前结果

## 数据集

标准集全部为虚构数据：华辰精密制造（制造）、启航供应链贸易（贸易）、星桥数字科技服务（服务）。三套材料使用不同横纵表头、文字表达和材料缺口，并标注标准事实、预期规则预警和证据位置。

## 运行方式

```powershell
python -m scripts.evaluate --mode deterministic --case all
python -m scripts.evaluate --mode agent --case all --repeat 3 --api-base http://127.0.0.1:8001 --output docs/assets/agent-evaluation.json
python -m scripts.evaluate --mode baseline --case all --repeat 3 --output docs/assets/baseline-evaluation.json
```

`agent` 会复制案例材料但不复制标准事实，让模型重新抽取并运行完整工作流；`baseline` 在一次模型调用内抽取事实并判断规则。二者固定温度 `0.2`。

## 当前已验证

真实 Agent 和 one-shot baseline 均使用 `deepseek-v4-flash`，三家案例各运行三次，每种模式 9 次。正式结果保存在 [`assets/agent-evaluation.json`](assets/agent-evaluation.json) 和 [`assets/baseline-evaluation.json`](assets/baseline-evaluation.json)。

| 指标 | 确定性回归 | 真实 Agent | One-shot baseline | 目标 |
| --- | ---: | ---: | ---: | ---: |
| 标注事实命中率 | 100% | 100% | 71.27% | ≥90% |
| 风险召回率 | 100% | 100% | 90.74% | ≥85% |
| 风险精确率 | 100% | 100% | 93.33% | - |
| 风险 F1 | 100% | 100% | 88.36% | - |
| 引用有效率 | 100% | 100% | 100% | ≥95% |
| 章节引用覆盖率 | 96.67% | 96.67% | 不生成报告 | ≥90% |
| 平均耗时 | 约 2.5 秒 | 176.4 秒 | 71.3 秒 | ≤5 分钟 |
| 最慢单次 | - | 290.5 秒 | 93.8 秒 | ≤5 分钟 |
| 平均模型成本 | 0 元 | 0.190 元 | 0.079 元 | ≤2 元 |
| 最高单次成本 | - | 0.324 元 | 0.108 元 | ≤2 元 |

Agent 相对 one-shot baseline 将标注事实命中率提升 28.73 个百分点、风险召回率提升 9.26 个百分点、风险 F1 提升 11.64 个百分点。代价是平均时延约为 baseline 的 2.47 倍，模型成本约为 2.39 倍。baseline 的事实命中率单次波动范围为 36.84%–89.47%，且有一次启航案例风险召回仅 16.67%；Agent 九次运行的风险召回均为 100%。

测试：后端 `20 passed`；Playwright 桌面/移动端历史验收 `4 passed`。

“标注事实命中率”逐项核对字段键、数值和材料明确出现的期间，不再使用抽取数量代替准确性。当前标准集没有完整标注所有可抽取事实，因此额外事实不直接计为错误。“引用有效率”验证引用 ID 存在且属于当前项目，尚不等同于人工语义蕴含判断。

## 解释边界

全部指标来自三家虚构标准案例，不代表真实客户或真实业务效果。当前标准集规模较小，且“引用有效率”验证引用 ID 存在并属于当前项目，尚不等同于人工语义蕴含判断。baseline 不生成报告，因此不与 Agent 比较章节引用覆盖率。
