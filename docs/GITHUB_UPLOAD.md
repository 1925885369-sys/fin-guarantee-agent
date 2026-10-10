# GitHub 上传指南

目标：`https://github.com/1925885369-sys/-agent.git`。仓库根目录为 `fin-guarantee-agent`，不是包含简历和其他项目的上层目录。

## 当前状态

2026-10-07 已配置 `origin`，补充忽略规则、发布检查脚本、README、产物索引和基础 CI。本轮只做上传准备，不自动提交或推送。

本地 GitHub 连接失败，提升权限后的自动审批服务返回 403，远程分支与访问权限尚未确认。下列网络命令需在可以访问 GitHub 的终端执行。

## 文件与基础检查

```powershell
git remote -v
.\.venv\Scripts\python.exe scripts/check_public_release.py --output .release/public-release-audit.json
git status --short
git diff --check
.\.venv\Scripts\python.exe -m pytest tests/test_public_release.py tests/test_parsers.py tests/test_fact_extraction_guards.py tests/test_table_block.py tests/test_llm.py tests/test_project_boundaries.py -q
cd frontend
npm.cmd ci
npm.cmd run build
cd ..
```

检查脚本覆盖候选文件及所有本地分支的可达历史，报告仅列位置和风险类型，不显示密钥内容。`.release/public-release-audit.json` 包含文件大小和 SHA-256 清单，不上传 GitHub。模式检查不能替代文档、视频和知识产权人工检查。

禁止提交 `.data*`、真实 PDF、上传文件、数据库、密钥、模型、导出报告、依赖目录和构建产物。历史评测 JSON 保持原文件并注明适用范围。个人迭代计划保留本地，不删除原件。

CI 执行上述基础回归和前端构建。完整业务回归需要另行运行；涉及北投、中邮的测试需要本地原始材料，详见 `RELEASE_CHECKLIST.md`。

2026-10-07 本地准备验证：仅复制候选源码形成干净快照，不带 `.data`、上传文件和真实 PDF，上述 6 个测试文件 `81 passed`；现有锁文件对应的前端生产构建成功。首次测试遇到系统临时目录权限问题，改用快照内独立 `--basetemp` 后通过。这不表示 GitHub Actions 已在线执行。

工作区与可达历史模式扫描通过，记录在 `.release/public-release-audit.json`。本轮未完整观看历史演示视频，公开前仍需本人核对其界面、文件名与业务内容。

## Git 身份与远程

当前作者配置为 `Codex <codex@localhost>`。创建新提交前使用自己的 GitHub 身份；noreply 邮箱可在 GitHub Settings → Emails 查看：

```powershell
git config user.name "1925885369-sys"
# 使用自己的 GitHub noreply 邮箱替换示例：
git config user.email "YOUR_GITHUB_NOREPLY_EMAIL"
git ls-remote --heads origin
```

如果远程已有分支，先 `git fetch origin` 并检查历史，不强制覆盖。远程为空时，确认文件检查通过后按以下步骤准备首次上传：

```powershell
git add --all
git diff --cached --stat
git diff --cached --name-only
.\.venv\Scripts\python.exe scripts/check_public_release.py
git commit -m "Prepare bond due diligence Agent portfolio for GitHub"
git push -u origin HEAD:main
```

仅在候选文件确认、检查通过并准备正式发布时执行 commit/push。登录使用 Git Credential Manager 的浏览器登录，不将 Token 写入远程 URL、代码或聊天。

上传后核对 README、源码、LICENSE、Actions 结果和文件排除情况。公开性与默认分支在 GitHub Settings 确认；当前网络环境无法代为核实。
