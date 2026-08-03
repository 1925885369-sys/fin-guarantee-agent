# 模型 API 启用指南

本项目通过后端环境变量调用 OpenAI 兼容接口。API Key 只应提供给后端进程，不得写入前端、README、`.env.example` 或 Git 提交。

## 本地临时启用

在同一个 PowerShell 窗口中完成配置和启动：

```powershell
Set-Location "C:\Users\Jamie\Desktop\agent构建\fin-guarantee-agent"

$secureKey = Read-Host "请输入模型 API Key" -AsSecureString
$env:LLM_API_KEY = [System.Net.NetworkCredential]::new("", $secureKey).Password
$env:LLM_BASE_URL = "https://api.deepseek.com"
$env:LLM_MODEL = "deepseek-v4-flash"

python -c "import os; print('Key configured:', bool(os.getenv('LLM_API_KEY')))"
python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8001
```

`LLM_MODEL` 必须使用模型服务商实际支持的模型名称。关闭该 PowerShell 窗口后，临时环境变量会失效。

启动命令运行期间不要关闭终端。浏览器访问：

```text
http://127.0.0.1:8001/
http://127.0.0.1:8001/api/health
```

健康接口应显示 `modelConfigured: true`，并返回预期模型名称。健康接口只检查配置是否存在；仍需运行一次自定义项目分析才能验证密钥权限、模型名称和结构化输出均可用。

## 使用项目启动脚本

`run.ps1` 默认使用端口 `8000`。如 PowerShell 禁止执行脚本，仅对当前终端临时放行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
./run.ps1
```

环境变量仍须在运行脚本前、同一个 PowerShell 窗口中设置。若端口 `8000` 已有旧服务，应先在旧服务终端按 `Ctrl+C` 关闭，或继续使用 `8001`。

配置在后端启动时读取。修改 API Key、接口地址或模型名称后，必须重启后端。

## Render 部署

在 Render 服务的 Environment 配置中设置：

```text
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
LLM_API_KEY=<以 Secret 方式填写>
```

保存后重新部署，并通过线上 `/api/health` 检查配置。不要将真实 Key 写入 `render.yaml`。

## 常见问题

- `modelConfigured: false`：启动后端的进程没有收到 `LLM_API_KEY`，或访问的是修改配置前启动的旧服务。
- 浏览器无法打开：对应端口没有运行中的 Uvicorn 服务，或启动终端已经关闭。
- 模型调用返回 404：通常是 `LLM_BASE_URL` 或 `LLM_MODEL` 与服务商不匹配。
- 模型调用返回 401/403：检查 Key 是否有效、是否有对应模型权限及账户余额。
- 仅创建 `.env` 不生效：当前项目不会自动加载 `.env`，应使用环境变量启动。
