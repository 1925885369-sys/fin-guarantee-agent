# 评测方案与当前结果

## 数据集

标准集全部为虚构数据：华辰精密制造（制造）、启航供应链贸易（贸易）、星桥数字科技服务（服务）。三套材料使用不同横纵表头、文字表达和材料缺口，并标注标准事实、预期规则预警和证据位置。

## 运行方式

```powershell
python -m scripts.evaluate --mode deterministic --case all
python -m scripts.evaluate --mode agent --case all --repeat 3 --output docs/assets/agent-evaluation.json
python -m scripts.evaluate --mode baseline --case all --repeat 3 --output docs/assets/baseline-evaluation.json
```

`agent` 会复制案例材料但不复制标准事实，让模型重新抽取并运行完整工作流；`baseline` 在一次模型调用内抽取事实并判断规则。二者固定温度 `0.2`。

## 当前已验证

| 指标 | 确定性三案例结果 | 目标 |
| --- | ---: | ---: |
| 字段覆盖率 | 100% | ≥90% |
| 风险召回率 | 100% | ≥85% |
| 引用精确率 | 100% | ≥95% |
| 章节引用覆盖率 | 96.67% | ≥90% |
| 平均耗时 | 1.665 秒 | ≤5 分钟 |
| 模型成本 | 0 元 | ≤2 元 |

测试：后端 `13 passed`；Playwright 桌面/移动端 `4 passed`。

## 尚未完成

当前环境没有 `LLM_API_KEY`，因此真实 `agent` 和 `baseline` 三次评测尚未执行。不得把确定性虚构案例结果描述成真实客户效果，也不得在简历中声称模型评测已达标。
