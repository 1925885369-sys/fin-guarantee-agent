# 研担：中小企业融资担保尽调 Agent

面向融资担保项目经理和风险审查人员的中文尽调工作台。系统读取文本型 PDF、DOCX、XLSX，抽取带证据定位的标准事实，通过确定性规则完成跨材料核验，再生成可人工复核的风险清单和尽调报告。系统不作自动审批决定。

## 当前能力

- 支持不同版式文本型 PDF、DOCX、XLSX；扫描件、加密或损坏文件给出明确状态。
- 事实统一到标准字段、期间和“万元/%/个月”，保留原值、置信度及页码/段落/单元格证据。
- 10 项规则支持 `passed / warning / insufficient`，缺字段不会按零值计算。
- 新建项目、批量上传、材料缺口、事实人工修正、节点重试、报告编辑和 Markdown/DOCX 导出闭环。
- 非演示项目使用随机访问令牌；公开模式持久化限流并在 24 小时后删除项目。
- 内置制造、贸易、服务三家虚构企业。无 API Key 时可完整运行三套确定性演示案例。

## 本地启动

要求 Python 3.9+、Node.js 20+：

```powershell
python -m pip install -r requirements.txt
Set-Location frontend
npm.cmd install
npm.cmd run build
Set-Location ..
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

访问 `http://127.0.0.1:8000`，API 文档位于 `/docs`。

自定义项目需要配置兼容模型：

```text
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-key
LLM_MODEL=deepseek-chat
```

密钥只由后端读取。公开体验仅允许上传公开或虚构材料，材料会发送到所配置的模型服务。

## 验证

```powershell
python -m pytest
python -m scripts.evaluate --mode deterministic --case all
python -m scripts.evaluate --mode agent --case all --repeat 3 --output docs/assets/agent-evaluation.json
python -m scripts.evaluate --mode baseline --case all --repeat 3 --output docs/assets/baseline-evaluation.json
Set-Location frontend
npm.cmd run build
npx.cmd playwright test
```

本地已验证：13 项后端测试通过、4 项桌面/移动端 Playwright 测试通过。三套虚构标准案例的确定性回归结果为字段覆盖 100%、风险召回 100%、引用精确率 100%、章节引用覆盖率 96.67%。这些结果不代表真实业务表现。

## 作品集材料

- [产品需求文档](docs/PRD.md)
- [技术与 Agent 架构](docs/ARCHITECTURE.md)
- [评测说明](docs/EVALUATION.md)
- [演示脚本](docs/DEMO_SCRIPT.md)
- [简历项目描述](docs/RESUME_BULLETS.md)
- [演示视频底稿](docs/assets/due-diligence-demo.webm)
- [公开发布清单](docs/RELEASE_CHECKLIST.md)

## 项目边界

首版不包含 OCR、工商/征信外部接口、联网新闻、多用户协作和正式审批流。“不同版式”不等于任意扫描件或任意语义均可正确识别；无法确认的事实必须标记为材料不足。
