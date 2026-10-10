# 发布前清单（可执行版）

## GitHub 上传准备（2026-10-07）

目标仓库为 `https://github.com/1925885369-sys/-agent.git`，步骤见 [上传指南](GITHUB_UPLOAD.md)。以下历史勾选只适用于所标注版本，不代表当前工作区完成全量验收。

- 运行 `python scripts/check_public_release.py --output .release/public-release-audit.json` 检查候选文件与可达 Git 历史。
- GitHub CI 只执行无私有 PDF 的基础回归与前端构建；真实材料验收仍在本地执行。
- 远程网络与权限待验证；当前本地 Git 作者为 `Codex <codex@localhost>`，提交前须设置自己的身份。
- 已有历史模型结果不覆盖，不重新调用付费评测。
- 本轮干净源码快照基础回归 `81 passed`，前端构建通过；不代表本轮完成后端全量或全量 E2E。
- 候选文件与可达历史模式扫描通过，审计清单位于本地 `.release/`；演示视频人工复核仍待完成。

每一项都写成**可以照抄的命令或可以逐条核对的断言**。打勾表示已在本机实跑通过；`[ ]` 表示尚未执行或需要重跑，**不得凭印象打勾**。

## 0. 红线（先读再动手）

- [ ] 任何测试、评测、脚本都**不得**读写正式数据目录 `.data`。跑之前先设 `APP_DATA_DIR` 指向独立目录（如 `.data-probe`）。
- [ ] `tests/conftest.py` 会把指向 `.data` 的 `APP_DATA_DIR` **直接判错退出**；`frontend/scripts/run-e2e.mjs` 同样拒绝在 `.data` 上跑。看到这条报错说明隔离在起作用，不要去改隔离代码。
- [ ] 不把模型密钥、公开参考材料原件、`.data*` 目录、`frontend/dist/`、`frontend/test-results/` 提交进仓库。
- [ ] 不启动正式后端做测试（除 `npm run test:e2e` 自带的 8011 端口隔离实例外）。

## 1. 后端回归

```bash
PYTHONIOENCODING=utf-8 PYTHONPATH=. python -m pytest tests -q
```

- [x] 2026-09-30 本机全量执行退出码 0，收集 446 项测试；测试目录与正式 `.data` 隔离。
- [ ] 表格链路专项（可单独先跑，快得多）：

```bash
PYTHONIOENCODING=utf-8 PYTHONPATH=. python -m pytest tests/test_table_block.py tests/test_table_extraction.py tests/test_table_store.py tests/test_table_review_api.py tests/test_report_tables.py tests/test_financial_statements.py tests/test_pdf_tables.py tests/test_delivery_wording.py -q
```

- [ ] 确认负例仍在跑（`tests/test_table_extraction.py` 的篡改 / 换年 / 跨主体 / 缺前导块四组）——负例被删掉时，"100% 通过"就没有意义。

## 2. 前端

```bash
cd frontend && npx tsc -b
cd frontend && npm run test:e2e
```

- [x] 2026-09-30 前端生产构建（含类型检查）退出码 0。
- [x] 2026-09-30 E2E 在桌面与移动端两个 project 上通过 45 项、跳过 1 项，退出码 0；后端冷启动播种 6 个虚构案例较慢（脚本给了 180 秒等待窗口）。
- [ ] E2E 使用 `.data-e2e`（或 `APP_DATA_DIR`），跑完确认正式 `.data` 未被改动。

## 3. 表格链路

```bash
PYTHONIOENCODING=utf-8 PYTHONPATH=. APP_DATA_DIR=.data-probe-trackD python scripts/evaluate_tables.py
```

- [ ] 六套虚构案例的表召回与单元格准确率为 1.0000（当前基线：61/61 张表、1923/1923 格）。
- [ ] 待确认表数为 0；跨页续表 6/6 被识别。
- [ ] 参考材料段按分层规则只做定位；**允许出现大量待确认**（当前：募集说明书 60%–73% 带表名单位，评级报告 100% 待确认）。这是已知缺口，不得为了好看而删除这段输出。
- [ ] 表格契约不变量抽查：`validate_table_block()` 对「不可追溯 / 采用无依据 / 冲突被自动择一 / 原表缺坐标」四类必须返回 `blocked`。
- [ ] 核对接口只追加不覆盖：更正一个单元格后，原始提取值仍可查。
- [ ] 三出口一致性：同一张表在前端、Markdown、Word 的数值、年度、单位与表注一致（Word 由 `exports._add_markdown_table()` 转换）。

## 4. 确定性评测（零模型成本）

```bash
python -m scripts.evaluate --mode deterministic --suite bond --case all --output docs/assets/bond-two-document-deterministic.json
python -m scripts.check_bond_similarity --reference .data/demo_materials/demo-北投 --generated-root .data/demo_materials
```

- [ ] 字段准确率、关键冲突召回率达到 [`EVALUATION.md`](EVALUATION.md) 的验收线；结果文件已更新且可复现。
- [ ] 该命令同样要带独立 `APP_DATA_DIR`，且 `.data/demo_materials` 只读。

## 5. 真实模型评测（需 `LLM_API_KEY`）

- [ ] 已封存的历史记录不覆盖：`bond-agent-evaluation-post-prompt.json`、`bond-agent-evaluation-post-hardening-v2.json`、`bond-baseline-evaluation-v2/v3-1/v4.json`（各含 18 次运行明细）。
- [ ] 重新跑 Agent / baseline 矩阵须用 `--resume` 与预算停止线，命令见 [`EVALUATION.md`](EVALUATION.md)；未跑完的组合不得用旧数字顶替。
- [ ] 表格改造（渲染器 / 解析器 / 报告四章表格）之后，**尚未重跑**真实模型矩阵——`[ ]` 待重跑，不要把改造前的 18 次结果表述为改造后的效果。

## 6. 交付与用词

- [ ] 成品正文不出现「募集说明书」「信用评级报告」等材料种类名（应为「发行文件〔资料NN〕」「外部信用评价文件〔资料NN〕」）。
- [ ] 成品不出现内部 UUID 与字段键（`delivery_wording.py` 的校验用例覆盖）。
- [ ] 数据源：`tests/test_delivery_wording.py`、`tests/test_report_delivery.py`。
- [ ] 报告导出含未决重大事项时显示红色警示。
- [ ] 未确认候选风险进入正式风险清单的数量为 0。

## 7. 仓库卫生

- [ ] `.gitignore` 覆盖：`.data/`、`.data-probe*`、`.data-e2e`、`frontend/dist/`、`frontend/test-results/`、`frontend/playwright-report/`、`__pycache__/`、`.pytest_cache/`、`node_modules/`、`.llm-smoke/`、`*.db`。
- [ ] `git status` 干净：没有 `__pycache__/`、构建产物、探测数据目录、密钥文件被跟踪。
- [ ] 参考材料原件（`.data/demo_materials/**`）不在版本库内。
- [ ] `LICENSE`（MIT，Jamie，2026）在仓库根目录。
- [ ] `README.md` 的快速开始命令与测试命令可从零复现。

## 8. 文档

- [ ] [`README.md`](../README.md)：能力、快速开始、跑测试、测试隔离、目录结构、虚构案例边界声明齐备。
- [ ] [`ARCHITECTURE.md`](ARCHITECTURE.md)：表格链路六段与三条安全不变量已写清。
- [ ] [`PRD.md`](PRD.md)：表格相关目标、流程、工作区与验收项已写清。
- [ ] [`table-evaluation.md`](table-evaluation.md)：评测命令与实测数字为本轮实跑结果。
- [ ] 历史文档保留，不做删除式「清理」；旧结论标注日期与适用版本。

## 9. 公开发布（本轮范围外）

- [ ] 对公开地址执行创建、上传、分析、证据、导出和 24 小时清理冒烟测试。
- [ ] 录制本人旁白的两分钟视频，并把在线 Demo、GitHub 和视频汇总为作品集入口。

---

## 历史红项（2026-09-24 实跑，2026-09-30 未复现）

2026-09-24 的运行结果为 **402 项：通过 398、失败 3、跳过 1**（表格链路的 8 个专项测试文件全部通过）。下表仅记录当时的失败；2026-09-30 本机全量执行已以退出码 0 完成，收集 446 项测试。

| 失败用例 | 现象 | 与表格改造的关系 |
| --- | --- | --- |
| `tests/test_api.py::test_demo_workflow_and_exports` | 报告正文断言 `"/5"` 不得出现，但系统表格标题格式为 `**表 1/5：…**`，命中该断言 | **直接由表格标题格式引起**：表格编号用了 `表 N/M`。要么改标题格式（如 `表 1（共 5）`），要么把断言收窄到评分写法，二者取一 |
| `tests/test_prompt_length_policy.py::test_regenerate_repairs_over_long_chapter_once_then_falls_back` | 期望 2 次模型调用（首次 + 一次长度修复），实际 1 次 | 疑与"表格不计入正文篇幅"后的长度判定变化有关，待确认 |
| `tests/test_hardening.py::test_node_retry_reuses_unchanged_upstream_artifacts_and_blocks_parallel_run` | 重试时上游产物复用集合只拿到 `{inventory}`，期望 `{inventory, extract, conflict, metrics, cross_check}` | 疑与解析阶段新增表格落库后输入哈希变化有关，待确认 |

此表保留作历史对照，不再代表当前测试状态。

## 历史记录（保留，不再作为当前基线）

以下条目来自 2026-09 之前的清单，保留作历史对照。**其中的测试数量与 E2E 数量已过期**，当前数量以第 1、2 节实跑输出为准，不得直接引用：

- [x] 本地后端 89 项测试、前端生产构建与桌面/移动端 Playwright 12 项通过
- [x] 六家虚构债券发行企业案例、19 条 bond-v1 规则确定性评测、PRD、架构和简历描述
- [x] 当前生产构建与桌面/移动端 Playwright 回归（含债券旗舰材料及 17 章报告）
- [x] 保留旧贷款套件 professional-v5/v6/v7 结果作为历史回归，不作为债券效果宣称
- [x] 评测检查点、断点续跑、20 元预算停止线、错误分类和对比报告生成器
- [x] 结构化模型网关、调用审计、节点产物、节点级重试和候选/章节降级
- [ ] 完成三个债券案例 Agent pilot 并保存结构化结果
- [ ] 运行债券套件 Agent 与 one-shot baseline 各 18 次并生成对比报告

本清单初版未覆盖 GitHub 上传；2026-09-30 已完成本地上传前文件与测试检查，仍需指定 GitHub 仓库地址后提交并推送。公开发布前不得把模型密钥写入仓库。
