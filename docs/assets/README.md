# 评测与演示产物索引

这些文件是历史运行记录，不会因当前代码修改而自动更新。引用时须注明套件、模式、样本数量和版本；虚构案例得分不代表真实客户效果。

## 债券套件历史记录

- `bond-agent-evaluation-post-hardening-v2.json`：六套虚构债券案例，各三次，共 18 次 Agent 运行。
- `bond-baseline-evaluation-v4.json`：债券 one-shot Baseline 历史对照，六案例各三次。
- `../BOND_FINAL_EVALUATION.md`：上述历史版本与加固前版本的对比。
- `bond-agent-evaluation-post-prompt.json`：加固前记录。
- `bond-baseline-evaluation-v2.json`、`bond-baseline-evaluation-v3-1.json`：中间 baseline 记录。
- `bond-two-document-deterministic*.json`、`bond-deterministic-evaluation.json`：对应版本的确定性回归。
- `bond-pilot-v2-*`、`bond-baseline-pilot-*`：对应阶段的 Pilot 检查点。

后续章节抽取、表格、复核与报告调整不在这些结果的效力范围内。重新评测须使用新文件名，保留原始结果。

## 旧套件与调试记录

`professional-*`、`deterministic-professional-*`、`pilot-*`、`agent-evaluation.json`、`baseline-evaluation.json` 等保留迭代历史，不能用作当前债券系统效果宣称。调试 smoke 文件默认不加入新提交。

`due-diligence-demo.webm` 是旧版无旁白演示底稿，界面与当前版本可能不同，不作为最终投递视频。

北投、中邮原始 PDF 和真实项目报告均不在本目录公开。所有计入上述准确率的案例为虚构样本。
