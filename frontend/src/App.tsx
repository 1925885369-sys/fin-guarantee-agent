import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, ArrowRight, Building2, Check, CheckCircle2, ChevronDown, ChevronRight,
  CircleDollarSign, CircleHelp, ClipboardCheck, Database, Download, FileCheck2,
  FileSpreadsheet, FileText, FolderPlus, Gauge, LayoutDashboard, LoaderCircle, LockKeyhole,
  Menu, PanelRightClose, Pencil, Play, Plus, RefreshCw, RotateCcw, SearchCheck,
  ShieldAlert, Square, Upload, X,
} from "lucide-react";
import { api } from "./api";
import type {
  Citation, CrossCheck, Fact, Health, Inventory, Project, ProjectSummary, ReportSection,
  Risk, Run, RunNode, Severity,
} from "./types";

type View = "overview" | "documents" | "checks" | "risks" | "report";
type ProjectRef = { id: string; token?: string };

const EMPTY_INVENTORY: Inventory = { documents: [], gaps: [], readiness: "empty" };
const views: Array<{ id: View; label: string; icon: typeof LayoutDashboard }> = [
  { id: "overview", label: "项目概览", icon: LayoutDashboard },
  { id: "documents", label: "材料与事实", icon: FileText },
  { id: "checks", label: "交叉核验", icon: SearchCheck },
  { id: "risks", label: "风险清单", icon: ShieldAlert },
  { id: "report", label: "尽调报告", icon: ClipboardCheck },
];
const severityText: Record<Severity, string> = { high: "高风险", medium: "中风险", low: "低风险" };

function SeverityBadge({ level }: { level: Severity }) {
  return <span className={`severity severity-${level}`}>{severityText[level]}</span>;
}

function CreateProject({ close, created }: { close: () => void; created: (project: ProjectRef) => void }) {
  const [form, setForm] = useState({ company_name: "", amount: 500, term_months: 12, purpose: "补充流动资金", data_consent: false });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try { const result = await api.createProject(form); created({ id: result.id, token: result.accessToken }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "创建失败"); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation"><form className="project-modal" onSubmit={submit}>
    <div className="modal-head"><div><span>新建融资项目</span><h2>录入担保申请</h2></div><button type="button" className="icon-button" onClick={close} title="关闭"><X size={18} /></button></div>
    <label>融资主体<input required minLength={2} maxLength={80} value={form.company_name} onChange={(event) => setForm({ ...form, company_name: event.target.value })} placeholder="企业全称" /></label>
    <div className="form-grid"><label>申请金额（万元）<input required type="number" min={1} max={100000} value={form.amount} onChange={(event) => setForm({ ...form, amount: Number(event.target.value) })} /></label><label>期限（月）<input required type="number" min={1} max={120} value={form.term_months} onChange={(event) => setForm({ ...form, term_months: Number(event.target.value) })} /></label></div>
    <label>资金用途<textarea required minLength={2} maxLength={300} value={form.purpose} onChange={(event) => setForm({ ...form, purpose: event.target.value })} /></label>
    <label className="consent"><input type="checkbox" checked={form.data_consent} onChange={(event) => setForm({ ...form, data_consent: event.target.checked })} /><span>我确认仅上传公开或虚构材料，并知悉材料会发送到兼容模型服务处理。</span></label>
    {error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}
    <div className="modal-actions"><button type="button" className="secondary-button" onClick={close}>取消</button><button className="primary-button" disabled={busy || !form.data_consent}>{busy ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}创建项目</button></div>
  </form></div>;
}

function EmptyState({ onRun, running, hasDocuments, modelConfigured }: { onRun: () => void; running: boolean; hasDocuments: boolean; modelConfigured: boolean }) {
  return <div className="empty-state"><div className="empty-icon">{hasDocuments ? <SearchCheck size={28} /> : <Upload size={28} />}</div>
    <h2>{hasDocuments ? "材料已解析" : "先上传尽调材料"}</h2>
    <p>{hasDocuments ? (modelConfigured ? "启动尽调后生成核验、风险和带引用报告。" : "自定义项目需要配置模型 API Key；内置案例可直接运行确定性流程。") : "支持文本型 PDF、DOCX、XLSX；扫描件会被标记为不可解析。"}</p>
    {hasDocuments && <button className="primary-button" onClick={onRun} disabled={running}>{running ? <LoaderCircle className="spin" size={17} /> : <Play size={17} fill="currentColor" />}开始尽调</button>}
  </div>;
}

function Overview({ project, inventory, checks, risks, onNavigate, onRun, running, modelConfigured }: {
  project: Project; inventory: Inventory; checks: CrossCheck[]; risks: Risk[]; onNavigate: (view: View) => void;
  onRun: () => void; running: boolean; modelConfigured: boolean;
}) {
  const high = risks.filter((risk) => risk.severity === "high").length;
  if (!checks.length) return <EmptyState onRun={onRun} running={running} hasDocuments={inventory.documents.length > 0} modelConfigured={modelConfigured} />;
  return <div className="view-content">
    <div className="page-heading"><div><span className="eyebrow">项目总览</span><h1>{project.company_name}</h1></div><div className="review-state"><span className="state-dot" />待人工复核</div></div>
    <div className="metric-grid">
      <div className="metric"><CircleDollarSign size={20} /><span>担保申请</span><strong>{project.amount.toLocaleString()} 万元</strong><small>{project.term_months} 个月</small></div>
      <div className="metric"><FileCheck2 size={20} /><span>有效材料</span><strong>{inventory.documents.length} 份</strong><small>{inventory.gaps.length ? `${inventory.gaps.length} 类材料待补` : "核心材料已覆盖"}</small></div>
      <div className="metric"><SearchCheck size={20} /><span>核验规则</span><strong>{checks.length} 项</strong><small>{checks.filter((item) => item.status === "warning").length} 项预警 · {checks.filter((item) => item.status === "insufficient").length} 项不足</small></div>
      <div className="metric metric-alert"><ShieldAlert size={20} /><span>风险发现</span><strong>{risks.length} 项</strong><small>{high} 项高风险</small></div>
    </div>
    <div className="overview-grid"><section className="panel project-panel"><div className="panel-title"><h2>融资申请</h2><Building2 size={18} /></div><dl className="detail-list"><div><dt>融资主体</dt><dd>{project.company_name}</dd></div><div><dt>申请金额</dt><dd>{project.amount} 万元</dd></div><div><dt>期限</dt><dd>{project.term_months} 个月</dd></div><div className="full"><dt>资金用途</dt><dd>{project.purpose}</dd></div></dl></section>
      <section className="panel risk-summary"><div className="panel-title"><h2>重点风险</h2><button className="text-button" onClick={() => onNavigate("risks")}>查看全部 <ArrowRight size={15} /></button></div><div className="risk-summary-list">{risks.slice(0, 4).map((risk) => <button key={risk.id} onClick={() => onNavigate("risks")}><SeverityBadge level={risk.severity} /><span>{risk.title}</span><ChevronRight size={16} /></button>)}</div></section></div>
    <section className="panel next-actions"><div className="panel-title"><h2>人工复核重点</h2><Gauge size={18} /></div><div className="action-grid">{risks.slice(0, 3).map((risk, index) => <div className="action-item" key={risk.id}><span>{index + 1}</span><div><strong>{risk.title}</strong><p>{risk.action}</p></div></div>)}</div></section>
  </div>;
}

function FactTable({ projectId, facts, token, refresh, openCitation }: { projectId: string; facts: Fact[]; token?: string; refresh: () => Promise<void>; openCitation: (id: string) => void }) {
  const [editing, setEditing] = useState<Fact | null>(null);
  const save = async () => { if (!editing) return; await api.saveFact(projectId, editing, token); setEditing(null); await refresh(); };
  return <section className="panel fact-panel"><div className="panel-title"><div><h2>标准化事实</h2><small>{facts.length} 项 · 人工修正优先于模型结果</small></div><Database size={18} /></div>
    {!facts.length ? <div className="sub-empty">运行分析后在此复核抽取事实。</div> : <div className="fact-table">{facts.map((fact) => <div className="fact-row" key={fact.id}><span className="kind-tag">{fact.category}</span><div><strong>{fact.name}</strong><small>{fact.fact_key} · 置信度 {Math.round(fact.confidence * 100)}%</small></div><span>{fact.period || "未注明"}</span><strong>{fact.text_value || `${fact.value?.toLocaleString() ?? "-"} ${fact.unit}`}</strong><span className={fact.review_status === "human_verified" ? "status-ok" : "fact-method"}>{fact.review_status === "human_verified" ? "人工确认" : fact.extraction_method}</span><div className="row-tools">{fact.source_ids[0] && <button className="icon-button" onClick={() => openCitation(fact.source_ids[0])} title="查看证据"><FileText size={15} /></button>}<button className="icon-button" onClick={() => setEditing({ ...fact })} title="修正事实"><Pencil size={15} /></button></div></div>)}</div>}
    {editing && <div className="fact-editor"><div><strong>修正：{editing.name}</strong><button className="icon-button" onClick={() => setEditing(null)}><X size={16} /></button></div><div className="form-grid"><label>期间<input value={editing.period} onChange={(event) => setEditing({ ...editing, period: event.target.value })} /></label><label>数值<input type="number" value={editing.value ?? ""} onChange={(event) => setEditing({ ...editing, value: event.target.value === "" ? null : Number(event.target.value) })} /></label><label>单位<input value={editing.unit} onChange={(event) => setEditing({ ...editing, unit: event.target.value })} /></label></div><label>文本值<input value={editing.text_value} onChange={(event) => setEditing({ ...editing, text_value: event.target.value })} /></label><button className="primary-button" onClick={save}><Check size={16} />保存并标记人工确认</button></div>}
  </section>;
}

function Documents({ projectId, inventory, facts, token, onUpload, refresh, openCitation }: { projectId: string; inventory: Inventory; facts: Fact[]; token?: string; onUpload: (files: File[]) => Promise<void>; refresh: () => Promise<void>; openCitation: (id: string) => void }) {
  const inputRef = useRef<HTMLInputElement>(null); const [uploading, setUploading] = useState(false);
  const handleFiles = async (files: FileList | null) => { if (!files?.length) return; setUploading(true); try { await onUpload(Array.from(files)); } finally { setUploading(false); if (inputRef.current) inputRef.current.value = ""; } };
  return <div className="view-content"><div className="page-heading"><div><span className="eyebrow">证据底稿</span><h1>材料与事实</h1></div><button className="secondary-button" onClick={() => inputRef.current?.click()} disabled={uploading}>{uploading ? <LoaderCircle className="spin" size={17} /> : <Upload size={17} />}批量上传</button><input ref={inputRef} hidden multiple type="file" accept=".pdf,.docx,.xlsx" onChange={(event) => handleFiles(event.target.files)} /></div>
    <div className={`inventory-summary ${inventory.gaps.length ? "has-gaps" : ""}`}>{inventory.gaps.length ? <CircleHelp size={18} /> : <CheckCircle2 size={18} />}<span>{inventory.gaps.length ? `缺少：${inventory.gaps.map((gap) => gap.group).join("、")}` : "核心材料已覆盖"}</span><small>{inventory.documents.length} 份材料 · {inventory.documents.reduce((sum, item) => sum + item.locator_count, 0)} 个证据位置</small></div>
    <section className="panel table-panel"><div className="table-header"><span>材料</span><span>分类</span><span>解析质量</span><span>状态</span><span /></div>{inventory.documents.map((doc) => <div className="document-row" key={doc.id}><div className="file-cell"><div className={`file-icon ${doc.filename.endsWith(".xlsx") ? "excel" : ""}`}>{doc.filename.endsWith(".xlsx") ? <FileSpreadsheet size={20} /> : <FileText size={20} />}</div><div><strong>{doc.filename}</strong><small>{(doc.size / 1024).toFixed(1)} KB · {doc.locator_count} 个证据位置</small></div></div><span className="kind-tag">{doc.kind}</span><div className="quality"><div><span style={{ width: `${doc.quality * 100}%` }} /></div><small>{Math.round(doc.quality * 100)}%</small></div><span className={doc.parse_status === "parsed" ? "status-ok" : "status-warning"}>{doc.parse_status === "parsed" ? <><Check size={14} />已解析</> : doc.warning}</span>{doc.id.startsWith("demo-") ? <a className="icon-button" href={`/api/demo/materials/${doc.id}`} title="下载材料"><Download size={17} /></a> : <span />}</div>)}</section>
    <FactTable projectId={projectId} facts={facts} token={token} refresh={refresh} openCitation={openCitation} />
  </div>;
}

function Checks({ checks, openCitation }: { checks: CrossCheck[]; openCitation: (id: string) => void }) {
  return <div className="view-content"><div className="page-heading"><div><span className="eyebrow">规则引擎</span><h1>交叉核验</h1></div><div className="legend"><span className="dot high" />预警<span className="dot passed" />通过<span className="dot insufficient" />材料不足</div></div><div className="check-list">{checks.map((check) => { const max = Math.max(...check.values_json.map((item) => Math.abs(item.value)), 1); return <article className={`panel check-card check-${check.status}`} key={check.id}><div className="check-top"><div>{check.status === "warning" ? <SeverityBadge level={check.severity} /> : <span className={`check-status ${check.status}`}>{check.status === "passed" ? "已通过" : "材料不足"}</span>}<h2>{check.title}</h2></div>{check.status === "warning" ? <AlertTriangle size={19} /> : <CheckCircle2 size={19} />}</div><p>{check.summary}</p>{check.values_json.length > 0 && <div className="bar-group">{check.values_json.map((item, index) => <div className="bar-row" key={item.label}><span>{item.label}</span><div><i style={{ width: `${Math.max(8, Math.abs(item.value) / max * 100)}%` }} className={index ? "bar-alt" : ""} /></div><strong>{item.value.toLocaleString()}</strong></div>)}</div>}<div className="formula">公式：{check.formula}</div><div className="evidence-links">{check.source_ids.map((id) => <button onClick={() => openCitation(id)} key={id}><FileText size={14} />查看证据</button>)}</div></article>; })}</div></div>;
}

function Risks({ risks, openCitation }: { risks: Risk[]; openCitation: (id: string) => void }) {
  const [filter, setFilter] = useState<"all" | Severity>("all"); const filtered = filter === "all" ? risks : risks.filter((risk) => risk.severity === filter);
  return <div className="view-content"><div className="page-heading"><div><span className="eyebrow">风险审查</span><h1>风险清单</h1></div><div className="segmented">{(["all", "high", "medium", "low"] as const).map((item) => <button className={filter === item ? "active" : ""} key={item} onClick={() => setFilter(item)}>{item === "all" ? "全部" : severityText[item]}</button>)}</div></div><div className="risk-list">{filtered.map((risk, index) => <article className={`risk-card risk-border-${risk.severity}`} key={risk.id}><div className="risk-index">{String(index + 1).padStart(2, "0")}</div><div className="risk-body"><div className="risk-heading"><SeverityBadge level={risk.severity} /><span className="risk-category">{risk.category}</span><h2>{risk.title}</h2></div><p>{risk.description}</p><div className="risk-details"><div><span>潜在影响</span><strong>{risk.impact}</strong></div><div><span>核验与缓释</span><strong>{risk.action}</strong></div></div><div className="evidence-links">{risk.source_ids.map((id) => <button onClick={() => openCitation(id)} key={id}><FileText size={14} />查看证据</button>)}</div></div></article>)}</div></div>;
}

function Report({ project, sections, token, openCitation, refresh }: { project: Project; sections: ReportSection[]; token?: string; openCitation: (id: string) => void; refresh: () => Promise<void> }) {
  const [editing, setEditing] = useState<string | null>(null); const [draft, setDraft] = useState(""); const [busy, setBusy] = useState<string | null>(null);
  const save = async (section: ReportSection, reviewed = false) => { setBusy(section.section_key); try { await api.saveSection(project.id, section.section_key, draft || section.content, reviewed, token); setEditing(null); await refresh(); } finally { setBusy(null); } };
  const regenerate = async (section: ReportSection) => { setBusy(section.section_key); try { await api.regenerate(project.id, section.section_key, token); await refresh(); } finally { setBusy(null); } };
  return <div className="view-content report-view"><div className="page-heading"><div><span className="eyebrow">人工复核工作区</span><h1>尽调报告</h1></div><div className="export-group"><button className="secondary-button" onClick={() => api.exportReport(project.id, "md", project.company_name, token)}><Download size={16} />Markdown</button><button className="primary-button" onClick={() => api.exportReport(project.id, "docx", project.company_name, token)}><Download size={16} />导出 Word</button></div></div><div className="report-layout"><aside className="report-toc"><strong>报告目录</strong>{sections.map((section, index) => <a href={`#section-${section.section_key}`} key={section.id}><span>{String(index + 1).padStart(2, "0")}</span>{section.title}</a>)}</aside><main className="report-paper"><div className="report-cover"><span>融资担保尽调报告</span><h2>{project.company_name}</h2><p>Agent 初稿 · 等待人工复核</p></div><div className="report-notice"><AlertTriangle size={17} />本报告为风险审查辅助材料，不构成自动审批意见。</div>{sections.map((section, index) => <section className="report-section" id={`section-${section.section_key}`} key={section.id}><div className="section-heading"><span>{String(index + 1).padStart(2, "0")}</span><h2>{section.title}</h2><div className="section-tools"><button className="icon-button" title="重新生成" onClick={() => regenerate(section)} disabled={busy === section.section_key}><RefreshCw className={busy === section.section_key ? "spin" : ""} size={16} /></button><button className="icon-button" title="编辑章节" onClick={() => { setEditing(section.section_key); setDraft(section.content); }}><Pencil size={16} /></button></div></div>{editing === section.section_key ? <div className="editor"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} /><div><button className="text-button" onClick={() => setEditing(null)}>取消</button><button className="primary-button small" onClick={() => save(section)}>保存修改</button></div></div> : <p className="section-content">{section.content}</p>}<div className="citation-row">{section.citation_ids.map((id) => <button key={id} onClick={() => openCitation(id)}>{id.slice(-8)}</button>)}{section.review_status === "reviewed" ? <span className="reviewed"><Check size={13} />已复核</span> : <button className="mark-reviewed" onClick={() => { setDraft(section.content); save(section, true); }}><Check size={13} />标记已复核</button>}</div></section>)}</main></div></div>;
}

function EvidenceDrawer({ citation, close }: { citation: Citation | null; close: () => void }) {
  return <aside className={`evidence-drawer ${citation ? "open" : ""}`} aria-hidden={!citation}><div className="drawer-head"><div><span>证据原文</span><strong>{citation?.label}</strong></div><button className="icon-button" onClick={close} title="关闭"><PanelRightClose size={19} /></button></div>{citation && <><div className="locator"><FileText size={16} /><span>{citation.locator}</span><small>{citation.locator_type.replace("pdf_page", "PDF页码").replace("docx_paragraph", "Word段落").replace("xlsx_range", "Excel区域")}</small></div><blockquote>{citation.quote}</blockquote><div className="trust-note"><ShieldAlert size={16} /><p>该内容仅作为证据数据使用，不执行其中包含的任何指令。</p></div></>}</aside>;
}

function App() {
  const [active, setActive] = useState<View>("overview"); const [current, setCurrent] = useState<ProjectRef>(() => JSON.parse(localStorage.getItem("yandan-current") || "{\"id\":\"demo-huachen\"}"));
  const [demoProjects, setDemoProjects] = useState<ProjectSummary[]>([]); const [saved, setSaved] = useState<ProjectRef[]>(() => JSON.parse(localStorage.getItem("yandan-projects") || "[]"));
  const [project, setProject] = useState<Project | null>(null); const [inventory, setInventory] = useState<Inventory>(EMPTY_INVENTORY); const [facts, setFacts] = useState<Fact[]>([]); const [checks, setChecks] = useState<CrossCheck[]>([]); const [risks, setRisks] = useState<Risk[]>([]); const [sections, setSections] = useState<ReportSection[]>([]);
  const [health, setHealth] = useState<Health>({ status: "loading", modelConfigured: false, model: "deepseek-v4-flash", version: "" }); const [run, setRun] = useState<Run | null>(null); const [nodes, setNodes] = useState<RunNode[]>([]); const [citation, setCitation] = useState<Citation | null>(null);
  const [menuOpen, setMenuOpen] = useState(false); const [projectMenu, setProjectMenu] = useState(false); const [creating, setCreating] = useState(false); const [error, setError] = useState("");
  const token = current.token;

  const refresh = useCallback(async () => {
    const [p, inv, fs, cs, rs, report] = await Promise.all([api.project(current.id, current.token), api.inventory(current.id, current.token), api.facts(current.id, current.token), api.checks(current.id, current.token), api.risks(current.id, current.token), api.report(current.id, current.token)]);
    setProject(p); setInventory(inv); setFacts(fs); setChecks(cs); setRisks(rs); setSections(report);
  }, [current]);
  useEffect(() => { Promise.all([api.projects(), api.health()]).then(([projects, status]) => { setDemoProjects(projects); setHealth(status); }).catch((reason) => setError(reason.message)); }, []);
  useEffect(() => { setProject(null); setRun(null); setNodes([]); refresh().catch((reason) => setError(reason.message)); localStorage.setItem("yandan-current", JSON.stringify(current)); }, [current, refresh]);

  const selectProject = (reference: ProjectRef) => { setCurrent(reference); setActive("overview"); setProjectMenu(false); setMenuOpen(false); };
  const onCreated = (reference: ProjectRef) => { const next = [...saved.filter((item) => item.id !== reference.id), reference]; setSaved(next); localStorage.setItem("yandan-projects", JSON.stringify(next)); setCreating(false); selectProject(reference); };
  const followRun = async (runId: string) => { await api.streamRun(runId, token, (next) => { setRun(next); if (["completed", "failed", "cancelled", "model_required"].includes(next.status)) { api.nodes(next.id, token).then(setNodes); refresh(); } }); };
  const start = async () => { setError(""); try { const result = await api.startRun(current.id, token); await followRun(result.id); } catch (reason) { setError(reason instanceof Error ? reason.message : "启动失败"); } };
  const retry = async () => { if (!run) return; try { const result = await api.retryRun(run.id, token); await followRun(result.id); } catch (reason) { setError(reason instanceof Error ? reason.message : "重试失败"); } };
  const openSource = async (id: string) => { const citationId = id.startsWith("c-") ? id : `c-${id}`; try { setCitation(await api.citation(citationId, token)); } catch (reason) { setError(reason instanceof Error ? reason.message : "证据读取失败"); } };
  const running = run?.status === "running" || run?.status === "queued";

  const page = useMemo(() => {
    if (!project) return null;
    if (active === "overview") return <Overview project={project} inventory={inventory} checks={checks} risks={risks} onNavigate={setActive} onRun={start} running={running} modelConfigured={health.modelConfigured || current.id.startsWith("demo-")} />;
    if (active === "documents") return <Documents projectId={project.id} inventory={inventory} facts={facts} token={token} onUpload={async (files) => { await api.upload(project.id, files, token); await refresh(); }} refresh={refresh} openCitation={openSource} />;
    if (active === "checks") return checks.length ? <Checks checks={checks} openCitation={openSource} /> : <EmptyState onRun={start} running={running} hasDocuments={inventory.documents.length > 0} modelConfigured={health.modelConfigured} />;
    if (active === "risks") return risks.length ? <Risks risks={risks} openCitation={openSource} /> : <EmptyState onRun={start} running={running} hasDocuments={inventory.documents.length > 0} modelConfigured={health.modelConfigured} />;
    return sections.length ? <Report project={project} sections={sections} token={token} openCitation={openSource} refresh={refresh} /> : <EmptyState onRun={start} running={running} hasDocuments={inventory.documents.length > 0} modelConfigured={health.modelConfigured} />;
  }, [active, project, inventory, facts, checks, risks, sections, running, health.modelConfigured, token, refresh]);

  return <div className="app-shell"><aside className={`sidebar ${menuOpen ? "mobile-open" : ""}`}><div className="brand"><div className="brand-mark"><SearchCheck size={22} /></div><div><strong>研担</strong><span>融资担保尽调</span></div><button className="mobile-close" onClick={() => setMenuOpen(false)}><X size={20} /></button></div>
    <button className="project-switch" onClick={() => setProjectMenu(!projectMenu)}><span>当前项目</span><strong>{project?.company_name || "正在载入"}</strong><small>{project ? `${project.amount}万元 · ${project.term_months}个月` : ""}</small><ChevronDown size={15} /></button>
    {projectMenu && <div className="project-menu"><strong>演示案例</strong>{demoProjects.map((item) => <button key={item.id} onClick={() => selectProject({ id: item.id })}>{item.company_name}<small>{item.amount}万元</small></button>)}{saved.length > 0 && <strong>我的项目</strong>}{saved.map((item) => <button key={item.id} onClick={() => selectProject(item)}>{item.id === current.id && project ? project.company_name : `项目 ${item.id.slice(0, 8)}`}</button>)}<button className="new-project" onClick={() => { setCreating(true); setProjectMenu(false); }}><FolderPlus size={15} />新建项目</button></div>}
    <nav>{views.map(({ id, label, icon: Icon }) => <button key={id} className={active === id ? "active" : ""} onClick={() => { setActive(id); setMenuOpen(false); }}><Icon size={18} />{label}{id === "risks" && risks.length > 0 && <span className="nav-count">{risks.length}</span>}</button>)}</nav>
    <div className="sidebar-bottom"><div className="mode-status"><span className={`pulse ${health.modelConfigured ? "" : "offline"}`} /><div><strong>{health.modelConfigured ? health.model : "确定性演示模式"}</strong><small>{health.modelConfigured ? "模型已配置" : "自定义项目需模型"}</small></div></div><p>仅上传公开或虚构材料 · 24小时删除</p></div></aside>
    <main className="main-area"><header className="topbar"><div className="topbar-brand"><div className="topbar-brand-mark"><SearchCheck size={21} /></div><div><strong>研担</strong><span>融资担保智能尽调系统</span></div></div><button className="menu-button" onClick={() => setMenuOpen(true)} title="打开导航"><Menu size={20} /></button><div className="breadcrumb"><span>融资项目</span><ChevronRight size={14} /><strong>{project?.company_name || "载入中"}</strong></div><div className="top-actions"><span className="draft-badge">Agent 初稿</span><button className="primary-button" onClick={start} disabled={running || !project}>{running ? <LoaderCircle className="spin" size={16} /> : <Play size={16} fill="currentColor" />}{running ? "分析中" : checks.length ? "重新分析" : "开始尽调"}</button></div></header>
      {run && running && <div className="run-progress"><div className="progress-meta"><span><LoaderCircle className="spin" size={15} />{run.message}</span><strong>{run.progress}%</strong><button onClick={() => api.cancelRun(run.id, token)} title="取消任务"><Square size={14} fill="currentColor" /></button></div><div><span style={{ width: `${run.progress}%` }} /></div></div>}
      {run && !running && run.status !== "completed" && <div className="run-result"><div><LockKeyhole size={17} /><strong>{run.status === "model_required" ? "需要配置模型" : run.message}</strong><span>{run.error}</span></div><button className="secondary-button" onClick={retry}><RotateCcw size={15} />重试</button>{nodes.length > 0 && <small>{nodes.filter((node) => node.status === "completed").length}/{nodes.length} 个节点完成</small>}</div>}
      {error && <div className="error-banner"><AlertTriangle size={16} />{error}<button onClick={() => setError("")}><X size={15} /></button></div>}
      {!project ? <div className="loading-page"><LoaderCircle className="spin" size={26} />正在载入项目</div> : page}</main>
    <EvidenceDrawer citation={citation} close={() => setCitation(null)} />{menuOpen && <button className="mobile-overlay" onClick={() => setMenuOpen(false)} />}{creating && <CreateProject close={() => setCreating(false)} created={onCreated} />}
  </div>;
}

export default App;
