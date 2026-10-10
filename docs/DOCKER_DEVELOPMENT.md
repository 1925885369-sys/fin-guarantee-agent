# Docker 多设备开发

这套配置让每台设备运行自己的开发容器：代码通过 Git 同步，Python 和 Node 依赖由 Docker 固定，运行数据保存在本机 `.data`。

## 首次使用

需要安装 Docker Desktop，并确认 Docker Compose 可用：

```powershell
docker compose version
```

在每台设备上执行：

```powershell
git clone <仓库地址>
cd fin-guarantee-agent
Copy-Item .env.example .env
notepad .env
docker compose -f compose.dev.yml up --build
```

在 `.env` 中填写 `LLM_API_KEY`。不要把 `.env` 提交到 Git。

打开：

- 前端开发页面：`http://localhost:5173`
- 后端 API 和 Swagger：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/api/health`

修改 `backend/` 或 `frontend/src/` 后会自动重新加载。修改 `requirements.txt` 后重新执行：

```powershell
docker compose -f compose.dev.yml build backend
docker compose -f compose.dev.yml up
```

## 日常协作

每台设备使用自己的分支：

```powershell
git pull
git checkout -b feature/my-change
git add .
git commit -m "describe the change"
git push -u origin feature/my-change
```

`.data`、数据库文件和上传材料只属于当前设备，已经被 `.gitignore` 忽略。不要把多个设备的 `.data` 目录放在网盘里同时写入 SQLite；需要共享结果时提交代码，或使用单独的共享开发服务器/数据库。

停止服务：

```powershell
docker compose -f compose.dev.yml down
```

删除本机开发数据库和上传数据（谨慎使用）：

```powershell
Remove-Item -Recurse -Force .data
```

## VS Code

安装 **Dev Containers** 扩展后，在项目根目录执行 `Dev Containers: Reopen in Container`。VS Code 会使用同一个 `compose.dev.yml`，并自动转发 8000 和 5173 端口。

## 让同一局域网的其他设备访问

运行服务的电脑使用 `ipconfig` 查看局域网 IP，例如 `192.168.1.20`。其他设备访问 `http://192.168.1.20:5173`。如果无法访问，需要在 Docker Desktop/Windows 防火墙放行 TCP 5173；不要把 `.env` 或数据库目录暴露给局域网。
