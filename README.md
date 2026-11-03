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

首次使用先克隆仓库；已有本地项目时直接在现有项目根目录操作：

```powershell
git clone https://github.com/1925885369-sys/fin-guarantee-agent.git
cd fin-guarantee-agent
```

安装 Python 环境和基础依赖，再构建网页：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
cd frontend
npm.cmd ci
npm.cmd run build
cd ..
```

`setup.ps1` 默认只安装基础依赖，BM25 检索可直接使用。也可用以下命令手动创建相同的 Python 环境，然后执行上面的前端安装与构建命令：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果要调用模型运行 Agent，在**启动后端前、同一个 PowerShell 窗口**完成配置；只查看已有案例可跳过：

```powershell
$secureKey = Read-Host "模型 API Key" -AsSecureString
$env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
$env:LLM_BASE_URL = Read-Host "服务商 OpenAI 兼容接口地址（例如 https://api.deepseek.com）"
$env:LLM_MODEL = Read-Host "服务商支持的精确模型 ID"
```

这些环境变量只在当前窗口及其子进程中有效。接口地址、模型 ID 和密钥须来自同一服务商的可用配置。

启动完整系统：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

看到 `Uvicorn running on http://127.0.0.1:8000` 后打开 **http://127.0.0.1:8000**，健康接口为 **http://127.0.0.1:8000/api/health**。首次启动会生成虚构 PDF，请等待启动完成。构建后的网页由后端提供，无需另外启动前端服务。

终端持续显示服务日志是正常现象；其他命令请另开终端。停止服务按 `Ctrl+C`。修改模型配置或后端代码后须重启；修改前端后须重新构建，并在构建完成后启动或重启后端。

**前端开发模式**：保留后端在 `8000` 运行，在项目根目录另开终端执行：

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```

此模式访问 **http://127.0.0.1:5173**，`/api` 默认代理至 `8000`，前端修改会自动更新。不要在已有服务占用 `8000` 时重复启动后端。构建产物不提交 Git。

### macOS / Linux

首次使用执行以下命令：

```bash
git clone https://github.com/1925885369-sys/fin-guarantee-agent.git
cd fin-guarantee-agent
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd frontend
npm ci
npm run build
cd ..
```

已有克隆时跳过 `git clone` 和紧随其后的 `cd`。需要调用模型时，在同一个终端先配置。以下输入命令使用 Bash；使用 zsh 等其他 shell 时请先运行 `bash`：

```bash
read -r -s -p "模型 API Key: " LLM_API_KEY; printf '\n'
export LLM_API_KEY
read -r -p "服务商 OpenAI 兼容接口地址: " LLM_BASE_URL
export LLM_BASE_URL
read -r -p "服务商支持的精确模型 ID: " LLM_MODEL
export LLM_MODEL
```

随后启动后端：

```bash
.venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

访问 **http://127.0.0.1:8000**。开发前端时，另开终端进入项目的 `frontend` 目录，执行 `npm run dev` 并访问 `5173`。

### 模型与语义检索

模型配置须在启动前完成。健康接口的 `modelConfigured` 只表示配置存在；权限和输出能力需实际分析验证。模型调用会产生费用，预算见 [.env.example](.env.example)，完整说明见 [模型配置指南](docs/API_SETUP.md)。

可选的本地语义检索在 Windows 项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WithRetrieval
.\.venv\Scripts\python.exe -m scripts.prepare_retrieval_model
```

该步骤会额外安装 CPU 检索依赖并下载 BGE 模型；准备完成后重启后端。`requirements-retrieval.txt` 中的 CPU PyTorch wheel 依赖平台支持，macOS 不应直接照搬 Windows 安装命令，可先使用基础 BM25。服务启动不会自动下载模型。Docker 的 `.env` 加载方式与本地脚本不同，见 [Docker 指南](docs/DOCKER_DEVELOPMENT.md)。

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

完整回归在项目根目录使用 `.\.venv\Scripts\python.exe -m pytest -q`（Windows）或 `.venv/bin/python -m pytest -q`（macOS / Linux）；E2E 在 `frontend` 目录使用 `npm.cmd run test:e2e`（Windows）或 `npm run test:e2e`（macOS / Linux），需安装 Playwright 的 Edge 通道。涉及北投、中邮的用例需要本机另备公开参考材料，新克隆不会自带这些文件。隔离目录和门禁见 [发布验收清单](docs/RELEASE_CHECKLIST.md)。

六套虚构金标案例的确定性 / Agent / 一次性 Baseline 历史结果均已保留。历史 18 次模型评测只适用于对应代码、Prompt 和材料版本，不能直接代表当前版本或真实客户效果；表格渲染与解析闭环的准确率也不代表第三方文档泛化能力。

- [债券评测](docs/EVALUATION.md) · [历史对比](docs/BOND_FINAL_EVALUATION.md) · [产物索引](docs/assets/README.md)
- [产品需求](docs/PRD.md) · [架构](docs/ARCHITECTURE.md) · [Prompt 体系](docs/prompt-system.md)
- [表格评测与限制](docs/table-evaluation.md) · [演示流程](docs/DEMO_SCRIPT.md)
- [GitHub 上传指南](docs/GITHUB_UPLOAD.md)

## 数据与使用边界

只提交源码、虚构案例定义、文档与已检查的历史评测产物。密钥、数据库、用户上传材料、北投/中邮 PDF、导出报告和本地向量模型均在 Git 忽略范围内。

项目不包含实习单位的客户数据、内部知识库、内部报告模板或内部 Prompt。MIT 许可覆盖本项目代码，不授予第三方材料再分发权。当前版本不提供生产级用户权限管理、工商司法等外部数据接口或自动审批决定。
