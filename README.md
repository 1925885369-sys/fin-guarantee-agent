# 研担 · 债券尽调 Agent

面向债券担保项目经理的 AI 尽调工作台：从主体档案、募集说明书和信用评级报告中组织可追溯事实，完成指标计算、规则核验和人工复核，生成报告初稿并导出 Word。

这是个人作品集项目。默认六套企业和材料为虚构案例；北投、中邮真实公开材料仅在本地作适配参考，原始 PDF 不包含在仓库中。

> **持续开发中**：仓库仍在活跃迭代，功能与文档会持续变化。GitHub CI 只验证基础回归（后端冒烟测试、发布文件检查）与前端依赖安装、类型检查、构建，**不代表完整业务验收通过**。推荐工具版本：Node.js **24.11.1** 与 npm **11.6.2**；`frontend/package.json` 已固定全部直接依赖版本并声明 `packageManager`，请勿改回 `latest`。

## 业务流程

```text
选择发行人档案或直接建项
  → 上传募集说明书和评级报告
  → 解析与定向章节抽取
  → 财务口径与跨来源核验
  → 指标计算与候选风险识别
  → 处理冲突、候选风险和行动包
  → 应用复核决议并刷新报告
  → 预览及 Markdown / Word 导出
```

- **主体资料**：身份与股权、近三年财务主要科目、档案版本和项目快照。
- **事实与证据**：页码、段落、单元格定位；区分主体、年度和合并/母公司口径，冲突来源并列保留。
- **规则与模型协作**：代码负责指标和 19 条 `bond-v1` 规则；LLM 补充抽取、提出候选风险并辅助报告表达，输出接受证据与引用校验。
- **Prompt 工程**：按业务章节定义分析重点、输出结构、引用要求与报告边界，提供项目级 Prompt 编辑。
- **复核与交付**：候选风险经人工决议后进入正式风险；决议更新使旧报告失效，需显式刷新。报告支持草稿导出与最终交付门禁。
- **材料检索**：默认 BM25；本地 BGE 模型准备完成后使用混合检索，模型缺失时保留 BM25 路径。

当前主要针对文本型 PDF、DOCX、XLSX。扫描件需要额外 OCR 配置；复杂无边框表、表头恢复、跨页口径和真实材料报告质量仍需逐例验收。报告为辅助尽调初稿，不代替原件核验或审批。

## 本地启动

要求 Python **3.12**、Node.js **24.11.1** 与 npm **11.6.2**（推荐组合，CI 已按此固定）和 Git。密钥只提供给后端；`.env.example` 是配置说明，当前启动脚本不会自动读取 `.env`。

### Windows PowerShell

在仓库根目录安装 Python 环境：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

`setup.ps1` 会安装基础依赖和可选的 CPU 语义检索依赖。只体验 BM25 时，可手动安装基础依赖：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

启动后端：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

另开终端启动前端：

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```

打开 **http://127.0.0.1:5173**；后端健康接口为 **http://127.0.0.1:8000/api/health**。首次启动会生成虚构 PDF，耗时较长。

若只使用一个端口，在 `frontend` 运行 `npm.cmd run build`，启动后端后访问 **http://127.0.0.1:8000**。构建产物不提交 Git。

### macOS / Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

另开终端，在 `frontend` 运行 `npm ci` 和 `npm run dev`。

### 模型与语义检索

在启动后端的 PowerShell 终端配置：

```powershell
$secureKey = Read-Host "模型 API Key" -AsSecureString
$env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
$env:LLM_BASE_URL = "https://api.deepseek.com"
$env:LLM_MODEL = "deepseek-v4-flash"
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

使用服务商实际支持的模型 ID。健康接口的 `modelConfigured` 只表示配置存在；权限和输出能力需实际分析验证。模型调用会产生费用，预算见 [.env.example](.env.example)，完整说明见 [模型配置指南](docs/API_SETUP.md)。

语义检索需要安装 `requirements-retrieval.txt`，并单独运行 `python -m scripts.prepare_retrieval_model` 准备模型；服务启动不会自动下载。Docker 开发见 [Docker 指南](docs/DOCKER_DEVELOPMENT.md)。

## 技术与结构

| 层次 | 实现 |
| --- | --- |
| 工作台 | React、TypeScript、Vite、Lucide |
| API 与状态 | Python、FastAPI、Pydantic、SQLite |
| 材料解析 | PyMuPDF、python-docx、openpyxl |
| 检索 | jieba、BM25；可选 sentence-transformers / BGE |
| Agent | 固定业务工作流、结构化模型调用、节点重试、预算与调用审计 |
| 报告 | 章节 Prompt、事实校验、结构化表格、Markdown / Word |
| 验收 | pytest、Playwright 桌面及移动端 |

`backend/app` 保存领域与工作流代码，`backend/prompts` 保存章节契约，`backend/policies` 保存规则包；`frontend/src` 为工作台，`tests` 和 `frontend/tests` 为回归场景，`docs` 为产品与评测文档。

## 验证与评测口径

GitHub CI 执行基础后端回归、发布文件检查，以及前端在 Ubuntu 与 Windows 双平台的 `npm ci`、类型检查与构建，不需要真实 PDF、模型密钥或向量模型。这是基础检查，**不等于完整业务验收通过**。

```powershell
.\.venv\Scripts\python.exe scripts/check_public_release.py --output .release/public-release-audit.json
.\.venv\Scripts\python.exe -m pytest tests/test_public_release.py tests/test_parsers.py tests/test_fact_extraction_guards.py tests/test_table_block.py tests/test_llm.py tests/test_project_boundaries.py -q
cd frontend
npm.cmd ci
npm.cmd run typecheck
npm.cmd run build
```

完整回归使用 `python -m pytest -q`；E2E 使用 `npm.cmd run test:e2e`，需安装 Playwright 的 Edge 通道。涉及北投、中邮的用例需要本机另备公开参考材料，新克隆不会自带这些文件。隔离目录和门禁见 [发布验收清单](docs/RELEASE_CHECKLIST.md)。

六套虚构金标案例的确定性 / Agent / 一次性 Baseline 历史结果均已保留。历史 18 次模型评测只适用于对应代码、Prompt 和材料版本，不能直接代表当前版本或真实客户效果；表格渲染与解析闭环的准确率也不代表第三方文档泛化能力。

- [债券评测](docs/EVALUATION.md) · [历史对比](docs/BOND_FINAL_EVALUATION.md) · [产物索引](docs/assets/README.md)
- [产品需求](docs/PRD.md) · [架构](docs/ARCHITECTURE.md) · [Prompt 体系](docs/prompt-system.md)
- [表格评测与限制](docs/table-evaluation.md) · [演示流程](docs/DEMO_SCRIPT.md)
- [GitHub 上传指南](docs/GITHUB_UPLOAD.md)

## 数据与使用边界

只提交源码、虚构案例定义、文档与已检查的历史评测产物。密钥、数据库、用户上传材料、北投/中邮 PDF、导出报告和本地向量模型均在 Git 忽略范围内。

项目不包含实习单位的客户数据、内部知识库、内部报告模板或内部 Prompt。MIT 许可覆盖本项目代码，不授予第三方材料再分发权。当前版本不提供生产级用户权限管理、工商司法等外部数据接口或自动审批决定。
