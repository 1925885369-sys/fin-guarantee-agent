# 模型 API 启用指南

本项目通过后端环境变量调用 OpenAI 兼容接口。API Key 只应提供给后端进程，不得写入前端、README、`.env.example` 或 Git 提交。

## 本地临时启用

先按 [README](../README.md#本地启动) 安装依赖并构建前端。下面的命令从项目根目录执行；如果后端正在运行，先在其终端按 `Ctrl+C` 停止，再在同一个 PowerShell 窗口中完成配置和启动：

```powershell
$secureKey = Read-Host "请输入模型 API Key" -AsSecureString
$env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
$env:LLM_BASE_URL = Read-Host "服务商 OpenAI 兼容接口地址（例如 https://api.deepseek.com）"
$env:LLM_MODEL = Read-Host "服务商支持的精确模型 ID"
$env:MAX_RUN_MODEL_COST_CNY = "2"
$env:MAX_RUN_MODEL_CALLS = "40"
$env:LLM_BATCH_MAX_CHARS = "24000"
$env:LLM_EXTRACT_BATCH_MAX_SOURCES = "3"
$env:LLM_MAX_OUTPUT_TOKENS = "5000"
$env:LLM_REPORT_MAX_OUTPUT_TOKENS = "10000"
$env:LLM_DISABLE_THINKING = "true"

.\.venv\Scripts\python.exe -c "import os; print('Key configured:', bool(os.getenv('LLM_API_KEY')))"
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

`LLM_MODEL` 必须使用模型服务商实际支持的模型名称。关闭该 PowerShell 窗口后，临时环境变量会失效。

启动命令运行期间终端会持续显示日志，其他操作请另开窗口。浏览器访问：

```text
http://127.0.0.1:8000/
http://127.0.0.1:8000/api/health
```

健康接口应显示 `modelConfigured: true`，并返回预期模型名称。健康接口只检查配置是否存在；仍需运行一次自定义项目分析才能验证密钥权限、模型名称和结构化输出均可用。

`8000/` 的网页依赖事先构建的 `frontend/dist`；全新克隆只启动后端不会自动构建网页。使用前端开发服务时，另开终端进入 `frontend` 执行 `npm.cmd ci`、`npm.cmd run dev`，访问 `http://127.0.0.1:5173`，其 API 默认连接后端 `8000`。

## 可选 OCR / 多模态接口

文本型 PDF 继续使用本地解析，不会发送图片。只有低文本质量 PDF 且以下配置完整时，系统才逐页调用兼容多模态接口：

```powershell
$env:OCR_ENABLED = "true"
$env:OCR_BASE_URL = "https://your-compatible-provider.example/v1"
$env:OCR_MODEL = "your-vision-model"
$env:OCR_API_KEY = $env:LLM_API_KEY
$env:OCR_MAX_PAGES = "30"
```

OCR 服务可以与文本模型不同。未配置时扫描件会标记为“需要 OCR”，不会静默返回空事实；健康接口通过 `ocrConfigured` 和 `ocrModel` 显示实际状态。图片内容仍按不可信数据处理，提示词只允许转录，不执行文档中的指令。

## 使用项目启动脚本

`run.ps1` 固定使用项目 `.venv` 和端口 `8000`，不会自动加载 `.env`。从项目根目录运行以下命令可处理脚本执行策略限制，无需修改全局策略：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

环境变量仍须在运行脚本前、同一个 PowerShell 窗口中设置。若端口 `8000` 已有旧服务，应先在旧服务终端按 `Ctrl+C` 关闭。确实需要备用端口时，直接使用项目解释器：

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

已构建网页可访问 `http://127.0.0.1:8001`。如果使用前端开发服务，必须在启动 Vite 的终端设置对应代理再启动：

```powershell
# 从项目根目录、另一个终端执行
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:8001"
cd frontend
npm.cmd run dev
```

配置在后端启动时读取。修改 API Key、接口地址或模型名称后，必须重启后端。

## Render 部署

在 Render 服务的 Environment 配置中设置：

```text
LLM_BASE_URL=<服务商 OpenAI 兼容接口地址>
LLM_MODEL=<该服务商支持的精确模型 ID>
LLM_API_KEY=<以 Secret 方式填写>
```

保存后重新部署，并通过线上 `/api/health` 检查配置。不要将真实 Key 写入 `render.yaml`。

## 常见问题

- `modelConfigured: false`：启动后端的进程没有收到 `LLM_API_KEY`，或访问的是修改配置前启动的旧服务。
- 浏览器无法打开：对应端口没有运行中的 Uvicorn 服务，或启动终端已经关闭。
- 模型调用返回 404：通常是 `LLM_BASE_URL` 或 `LLM_MODEL` 与服务商不匹配。
- 模型调用返回 401/403：检查 Key 是否有效、是否有对应模型权限及账户余额。
- 仅创建 `.env` 不生效：本地脚本不会自动加载 `.env`，应使用环境变量启动；Docker Compose 通过 `env_file` 加载本地 `.env`。
- 长章节超时：代码的读取超时默认是 `300` 秒，`.env.example` 与其一致；环境变量中的旧值会覆盖默认值，修改后须重启后端。
