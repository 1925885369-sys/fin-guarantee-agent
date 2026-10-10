# 跨设备迁移指南

将完整项目（含中邮真实项目、历史数据）迁移到另一台 Windows 设备继续任务。

## 产品与历史数据边界

当前产品入口只支持债券发行融资担保项目。迁移包可能包含历史贷款项目、旧表结构、接口兼容字段和旧评测产物，这些内容只为保留既有数据、只读查看和回归兼容：不能用于新建贷款项目，不进入债券 Pilot、baseline 或指标汇总，也不作为当前产品能力对外宣称。

迁移过程不删除或重写历史贷款数据，不承诺移除旧表和兼容代码。若未来需要彻底下线历史结构，应先独立完成数据备份、字段映射、迁移校验和回滚演练。

## 迁移内容

- **完整代码**：backend / frontend / tests / scripts / docs
- **完整运行时数据** `.data/`：agent.db（历史项目与评测）、demo_materials（含中邮真实 PDF）、uploads
- **依赖与配置**：requirements.txt / Dockerfile / .dockerignore / run.ps1

> 压缩包约 56M，未包含 `node_modules/`、`frontend/dist/`（另一设备可重建）。

## 迁移步骤（目标设备）

### 1. 解压
将 `yandan-agent-full.tar.gz` 复制到目标设备，解压：
```powershell
tar -xzf yandan-agent-full.tar.gz
cd fin-guarantee-agent
```

### 2. 安装后端依赖
```powershell
python -m pip install -r requirements.txt
```

### 3. 构建前端
```powershell
Set-Location frontend
npm.cmd install
npm.cmd run build
Set-Location ..
```

### 4. 配置模型（可选，但自定义债券材料分析需用）
项目根目录创建 `.env`：
```text
LLM_API_KEY=你的DeepSeek密钥
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```
> 当前压缩包不含 `.env`（迁移时未配置）。不配模型时，虚构债券演示案例的确定性流程仍可运行；自定义债券材料分析会停在 `model_required`。历史贷款数据不因配置模型而恢复为可新建产品入口。

### 5. 启动
```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

浏览器打开 `http://127.0.0.1:8000`。

## 启动后验证

- `/api/health` 返回 `{"status":"ok","modelConfigured":true/false}`
- 项目列表含 6 套债券演示案例 + 中邮真实项目（`?data_class=real_reference`）
- 打开中邮项目 → 可查看统一完整尽调、补充核验材料和引用定位；真实参考项目默认禁止 Markdown、Word 和原始 PDF 导出
- 新建项目入口只提供债券发行融资担保；历史贷款项目如存在，仅按兼容策略保留，不进入默认项目入口或评测

## Docker 备用（无 Python/Node 环境时）

```bash
# 构建镜像（.dockerignore 已排除 .data/node_modules）
docker build -t yandan-agent .

# 运行：用卷挂载 .data 保留完整数据（含中邮真实项目）
docker run -p 8000:8000 -v "$(pwd)/.data:/app/.data" -e LLM_API_KEY=xxx yandan-agent
```

> **关键**：容器内若没有挂载 `.data`，中邮真实项目不会出现（seed 依赖 `.data/demo_materials` 中的 PDF）。API Key 用 `-e` 注入，不打进镜像。

## 注意事项

1. **`.env` 保密**：内含 API Key，勿将压缩包或 .env 公开分享。
2. **`.data` 是运行时数据**：迁移的是当前快照；后续新增的债券项目和债券评测保存在目标设备本机，历史数据不会被迁移脚本破坏性清理。
3. **长文档限制**：用户自定义上传默认 150 页上限；如需上传超长募集说明书（如 250+ 页），需在 `.env` 调大 `MAX_PAGES`。
4. **模型未配置时**：确定性演示可运行，但无法用真实模型润色报告。
