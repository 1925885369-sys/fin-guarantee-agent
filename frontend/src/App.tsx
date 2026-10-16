import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertTriangle, ArrowRight, Building2, Check, CheckCircle2, ChevronDown, ChevronRight, Table2,
  CircleDollarSign, CircleHelp, ClipboardCheck, Database, Download, FileCheck2,
  FileSpreadsheet, FileText, FolderPlus, Gauge, LayoutDashboard, LoaderCircle, LockKeyhole,
  Menu, PanelRightClose, Pencil, Play, RefreshCw, RotateCcw, SearchCheck, FileCog, Send,
  ShieldAlert, ShieldCheck, Square, Upload, X, GitCompareArrows, History, UsersRound,
  ArrowDown, ArrowUp, ArrowLeft, Eye, ListPlus, SlidersHorizontal, Trash2, FileUp, Info, Search,
  ListFilter,
} from "lucide-react";
import { DuplicateDocumentError, api } from "./api";
import type { DocumentKind, PromptConcurrency, UploadedDocument } from "./api";
import { DEFAULT_PAGE_SIZE, HelpTip, Pagination, ReadinessBar, SourceBadge, StatusBadge } from "./components/common";
import type { PageSize } from "./components/common";
import { ProjectIntakeWizard } from "./components/project-intake/ProjectIntakeWizard";
import { ProjectSwitcher } from "./components/project-switcher/ProjectSwitcher";
import { EntityProfileWorkspace } from "./features/entity-profiles";
import { ProjectSnapshotPanel } from "./features/project-snapshot";
import { isProjectVisible, isPurgedProject } from "./features/projects/projectVisibility";
import {
  AnalysisDetailModal, AnalysisSummaryCard, MetricsDrawer, RunFocus,
  countStatuses, deriveVerdict, latestMetricRecords, metricKindCount, metricPeriodRange, scopeLabel,
} from "./features/analysis";
import type { AnalysisResultRow } from "./features/analysis";
import type {
  AffectedSection, AuditEvent, CandidateFinding, Citation, CrossCheck, DocumentItem, EvidenceGraphSummary, Fact, FactGroup,
  FinancialMetric, FinancialTieOut, Health, Inventory, PolicyInfo, Project,
  BondSummary, EntityProfileSummary, MaterialQAResponse, ProjectEntity, ProjectProfileSnapshot, ProjectSummary,
  ProjectUpdatePayload,
  OutlineChapter, OutlinePart, OutlineSubsection, ReportOutline, ReportOutlineState,
  PromptConstraints, PromptEditableConfig, ReportBlock, ReportBlockCell, ReportBlockRow, ReportPrompt, ReportQuality, ReportSection,
  ReportSnapshot, ReportSnapshotSummary,
  ReportTableBlock, ReviewApplyResult, ReviewSummary, ReviewTask, Risk, Run, RunArtifact, RunNode, Severity, WorkflowStage,
  TableMatrix, TableSummary,
} from "./types";

type View = "profiles" | "projects" | "project-detail" | "due-diligence" | "evidence" | "analysis" | "review" | "solution" | "report" | "prompts" | "tables";
type ProjectRef = { id: string; token?: string };
type RiskNavigation = { projectId: string; returnView: "project-detail" | "due-diligence"; scrollY: number; riskId?: string };

const EMPTY_INVENTORY: Inventory = { documents: [], gaps: [], readiness: "empty" };
const severityText: Record<Severity, string> = { high: "高风险", medium: "中风险", low: "低风险" };
const stageIcons = { materials: FileText, analysis: SearchCheck, review: GitCompareArrows, delivery: ClipboardCheck } as const;

function deriveWorkflowStages(
  inventory: Inventory, run: Run | null, factGroups: FactGroup[], candidates: CandidateFinding[],
  tasks: ReviewTask[], sections: ReportSection[], quality: ReportQuality | null, reviewSummary: ReviewSummary | null,
): WorkflowStage[] {
  const materialReady = Boolean(inventory.complete_due_diligence_ready);
  const analysisComplete = run?.status === "completed";
  const analysisBlocked = Boolean(run && ["failed", "model_required", "cancelled"].includes(run.status));
  const reviewCount = reviewSummary?.total_unique_issues ?? 0;
  const reviewComplete = analysisComplete && reviewCount === 0;
  const reviewedSections = sections.filter((item) => item.review_status === "reviewed").length;
  const deliveryComplete = Boolean(sections.length && reviewedSections === sections.length && quality?.status === "passed" && reviewComplete);
  return [
    { key: "materials", label: "材料准备", view: "evidence", status: materialReady ? "completed" : "current", count: inventory.gaps.length, reason: materialReady ? "核心材料已解析" : inventory.documents.length ? "核心材料尚未齐备" : "等待上传募集说明书和评级报告" },
    { key: "analysis", label: "Agent 分析", view: "analysis", status: analysisComplete ? "completed" : analysisBlocked ? "blocked" : materialReady ? "current" : "pending", reason: analysisComplete ? "分析节点已完成" : analysisBlocked ? (run?.message || "最近一次运行未完成") : materialReady ? "可以启动 Agent" : "等待核心材料" },
    { key: "review", label: "人工复核", view: "review", status: reviewComplete ? "completed" : analysisComplete ? "current" : "pending", count: reviewCount, reason: reviewComplete ? "重大复核事项已处理" : analysisComplete ? `${reviewCount} 项关键事项待处理` : "等待 Agent 分析" },
    { key: "delivery", label: "报告交付", view: "report", status: deliveryComplete ? "completed" : sections.length && reviewComplete ? (quality?.status === "warning" ? "blocked" : "current") : "pending", count: sections.length ? sections.length - reviewedSections : undefined, reason: deliveryComplete ? "报告已满足交付条件" : sections.length ? `${reviewedSections}/${sections.length} 章已复核` : "等待分析生成报告" },
  ];
}

function WorkflowRail({ stages, active, navigate }: { stages: WorkflowStage[]; active: View; navigate: (view: View) => void }) {
  const accessibleLabels = { materials: "债券材料准备 · 募集说明书与评级报告", analysis: "债券 Agent 分析 · 财务与偿债核验", review: "债券担保人工复核 · 复核中心", delivery: "报告交付 · 债券融资担保尽调报告" };
  return <nav className="workflow-nav" aria-label="尽调工作流">{stages.map((stage, index) => { const Icon = stageIcons[stage.key]; const selected = active === stage.view || (stage.key === "review" && active === "solution"); return <button key={stage.key} aria-label={accessibleLabels[stage.key]} className={`${selected ? "active" : ""} stage-${stage.status}`} aria-current={selected ? "step" : undefined} onClick={() => navigate(stage.view)}><span className="stage-index">{stage.status === "completed" ? <Check size={13} /> : index + 1}</span><Icon size={17} /><span><strong>{stage.label}</strong><small>{stage.reason}</small></span>{stage.count ? <i>{stage.count}</i> : null}</button>; })}<button className={active === "solution" ? "active workflow-subview" : "workflow-subview"} onClick={() => navigate("solution")}><ShieldAlert size={16} /><span><strong>正式风险与缓释</strong><small>人工确认后的风险清单</small></span></button><button className={active === "tables" ? "active workflow-subview" : "workflow-subview"} onClick={() => navigate("tables")}><Table2 size={16} /><span><strong>表格核对</strong><small>系统数据表逐格核对</small></span></button></nav>;
}

function SeverityBadge({ level }: { level: Severity }) {
  return <span className={`severity severity-${level}`}>{severityText[level]}</span>;
}

function TableReview({ projectId, token, refresh, openCitation }: {
  projectId: string; token?: string; refresh: () => Promise<void>; openCitation: (citationId: string) => void;
}) {
  const [tables, setTables] = useState<TableSummary[]>([]);
  const [active, setActive] = useState<string>("");
  const [matrix, setMatrix] = useState<TableMatrix | null>(null);
  const [editing, setEditing] = useState<string>("");
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { api.tables(projectId, token).then((items) => {
    setTables(items);
    setActive((current) => current || items[0]?.table_id || "");
  }).catch((exc) => setError(String(exc.message || exc))); }, [projectId, token]);

  useEffect(() => {
    if (!active) { setMatrix(null); return; }
    api.tableDetail(projectId, active, token).then(setMatrix).catch((exc) => setError(String(exc.message || exc)));
  }, [projectId, active, token]);

  const submit = async (cellId: string, decision: "confirmed" | "corrected" | "rejected") => {
    setBusy(true); setError("");
    try {
      const response = await api.reviewTableCell(projectId, active, cellId, {
        decision,
        corrected_raw: decision === "corrected" ? draft : null,
        note: note || (decision === "confirmed" ? "已与原件核对一致" : "材料不足，待重新取证"),
      }, token);
      setMatrix((current) => current ? { ...current, cell_reviews: response.cell_reviews } : current);
      setEditing(""); setDraft(""); setNote("");
      setTables((items) => items.map((item) => item.table_id === active ? { ...item, cell_reviews: Object.keys(response.cell_reviews).length } : item));
      await refresh();
    } catch (exc) { setError(String((exc as Error).message || exc)); }
    finally { setBusy(false); }
  };

  const statusLabel: Record<string, string> = { passed: "校验通过", blocked: "校验未通过", needs_review: "待人工确认", pending: "未校验" };
  // 摘要必须从**当前**核对记录派生：服务端返回的 review_summary 是加载那一刻的快照，
  // 更正之后它不会自己变，直接展示会让用户看到「更正 0」而单元格已经显示更正。
  const reviewSummary = useMemo(() => {
    const reviews = Object.values(matrix?.cell_reviews || {});
    return {
      reviewed: reviews.length,
      corrected: reviews.filter((item) => item.decision === "corrected").length,
      rejected: reviews.filter((item) => item.decision === "rejected").length,
    };
  }, [matrix]);
  return <section className="table-review">
    <header className="table-review-head">
      <div><h2>表格核对</h2><p>表格与报告成品读同一份数据；核对只追加记录，原始值不会被覆盖。</p></div>
      <span className="table-review-count">{tables.length} 张表</span>
    </header>
    {error && <p className="table-review-error">{error}</p>}
    <div className="table-review-split">
      <aside className="table-review-list" aria-label="表格清单">
        {tables.map((item) => <button key={item.table_id} className={item.table_id === active ? "active" : ""} onClick={() => { setActive(item.table_id); setEditing(""); }}>
          <strong>{item.title}</strong>
          <small>{item.row_count} 行 · {item.unit || "无单位"} · {statusLabel[item.validation_status] || item.validation_status}</small>
          <span className={`table-badge ${item.validation_status}`}>{item.cell_reviews ? `已核对 ${item.cell_reviews}` : "未核对"}</span>
        </button>)}
        {!tables.length && <p className="table-review-empty">该项目暂无系统表格。</p>}
      </aside>
      <div className="table-review-matrix">
        {matrix ? <>
          <div className="table-review-meta">
            <strong>{matrix.title}</strong>
            <span>{matrix.origin_mode === "profile_facts" ? "已核验事实汇总" : matrix.origin_mode === "uploaded_table" ? "上传材料原表" : "档案与上传材料核对"}</span>
            <span>已核对 {reviewSummary.reviewed} · 更正 {reviewSummary.corrected} · 驳回 {reviewSummary.rejected}</span>
          </div>
          <div className="table-scroll">
            <table>
              <thead><tr>{matrix.columns.map((column) => <th key={column.key} className={column.role === "subject" ? "subject" : "value"}>{column.label}</th>)}</tr></thead>
              <tbody>
                {matrix.rows.map((row) => <tr key={row.row_key} className={row.kind}>
                  {row.cells.map((cell, index) => {
                    // 用接口给出的 display（有效值），不自己 join cell_reviews——
                    // 漏 join 就会把已更正的格显示成原始值。
                    const review = cell.review;
                    const display = cell.display;
                    return <td key={cell.cell_id} className={`${index === 0 ? "subject" : "value"} ${review ? `reviewed ${review.decision}` : ""}`}>
                      {index === 0 ? display : <button className="table-cell" onClick={() => { setEditing(editing === cell.cell_id ? "" : cell.cell_id); setDraft(display || ""); setNote(review?.note || ""); }}>
                        {display || "—"}
                        {review?.decision === "corrected" && <em title={`原始值：${review.original_raw}`}>更正</em>}
                        {review?.decision === "rejected" && <em className="rejected">驳回</em>}
                      </button>}
                      {editing === cell.cell_id && <div className="table-cell-editor">
                        <input value={draft} onChange={(event) => setDraft(event.target.value)} aria-label="更正值" />
                        <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="核对说明" aria-label="核对说明" />
                        {review && <p className="table-cell-original">原始值：{review.original_raw}</p>}
                        {cell.source_ids.length > 0 && <button className="link-button" onClick={() => openCitation(cell.source_ids[0])}>查看证据</button>}
                        <div className="table-cell-actions">
                          <button disabled={busy} onClick={() => submit(cell.cell_id, "confirmed")}>确认</button>
                          <button disabled={busy || !draft.trim()} onClick={() => submit(cell.cell_id, "corrected")}>更正</button>
                          <button disabled={busy} onClick={() => submit(cell.cell_id, "rejected")}>驳回</button>
                        </div>
                      </div>}
                    </td>;
                  })}
                </tr>)}
              </tbody>
            </table>
          </div>
          {matrix.notes.map((line) => <p className="table-review-note" key={line}>{line}</p>)}
        </> : <p className="table-review-empty">选择左侧的一张表开始核对。</p>}
      </div>
    </div>
  </section>;
}

function WorkflowOverview({ stages, run, nodes, readOnly, running, onRun, navigate }: { stages: WorkflowStage[]; run: Run | null; nodes: RunNode[]; readOnly: boolean; running: boolean; onRun: () => void; navigate: (view: View) => void }) {
  const current = stages.find((stage) => stage.status === "current" || stage.status === "blocked") || stages[3];
  const action = readOnly
    ? { label: "查看核心材料", view: "evidence" as View }
    : current.key === "materials" ? { label: "检查并补齐材料", view: "evidence" as View }
    : current.key === "analysis" && current.status !== "blocked" ? { label: running ? "Agent 分析中" : "运行 Agent", view: "analysis" as View }
    : current.key === "analysis" ? { label: "查看失败节点", view: "analysis" as View }
    : current.key === "review" ? { label: "处理复核事项", view: "review" as View }
    : { label: "复核并导出报告", view: "report" as View };
  const trigger = () => current.key === "analysis" && current.status === "current" && !running && !readOnly ? onRun() : navigate(action.view);
  return <section className="workflow-overview"><div className="workflow-stage-strip">{stages.map((stage, index) => <button key={stage.key} className={`stage-${stage.status}`} onClick={() => navigate(stage.view)}><span>{stage.status === "completed" ? <Check size={14} /> : index + 1}</span><div><strong>{stage.label}</strong><small>{stage.reason}</small></div></button>)}</div><div className={`next-step stage-${current.status}`}><div><span>当前步骤</span><strong>{current.label}</strong><p>{current.reason}{run && current.key === "analysis" ? ` · ${nodes.filter((node) => node.status === "completed").length}/${nodes.length || 11} 个节点完成` : ""}</p></div><button className="primary-button write-aware" onClick={trigger} disabled={running && current.key === "analysis"}>{running && current.key === "analysis" ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}{action.label}</button></div></section>;
}

function EmptyState({ onRun, running, hasDocuments, modelConfigured }: { onRun: () => void; running: boolean; hasDocuments: boolean; modelConfigured: boolean }) {
  return <div className="empty-state"><div className="empty-icon">{hasDocuments ? <SearchCheck size={28} /> : <Upload size={28} />}</div>
    <h2>{hasDocuments ? "债券材料已解析" : "先上传募集说明书和评级报告"}</h2>
    <p>{hasDocuments ? (modelConfigured ? "启动债券担保分析后生成偿债核验、担保风险和带引用报告。" : "自定义债券项目需要配置模型 API Key；内置案例可直接运行确定性流程。") : "核心材料为募集说明书和信用评级报告；补充材料支持文本型 PDF、DOCX、XLSX。"}</p>
    {hasDocuments && <button className="primary-button" onClick={onRun} disabled={running}>{running ? <LoaderCircle className="spin" size={17} /> : <Play size={17} fill="currentColor" />}开始债券担保分析</button>}
  </div>;
}

function Overview({ project, bondSummary, inventory, checks, risks, tasks, candidates, factGroups, entities, policy, snapshot, reviewSummary, onNavigate, onOpenRisk, onRun, running, modelConfigured, stages, run, nodes }: {
  project: Project; bondSummary: BondSummary | null; inventory: Inventory; checks: CrossCheck[]; risks: Risk[]; tasks: ReviewTask[];
  candidates: CandidateFinding[]; factGroups: FactGroup[]; entities: ProjectEntity[]; policy: PolicyInfo | null; onNavigate: (view: View) => void; onOpenRisk: (riskId?: string) => void;
  snapshot: ProjectProfileSnapshot | null; reviewSummary: ReviewSummary | null; onRun: () => void; running: boolean; modelConfigured: boolean; stages: WorkflowStage[]; run: Run | null; nodes: RunNode[];
}) {
  const projectGuarantor = entities.find((item) => item.entity_type === "guarantor")?.name;
  const projectCounterGuarantors = entities.filter((item) => item.entity_type === "counter_guarantor").map((item) => item.name);
  const legacySnapshotGuarantor = snapshot?.roles.find((item) => item.role === "guarantor")?.name;
  const legacySnapshotCounterGuarantors = snapshot?.roles.filter((item) => item.role === "counter_guarantor").map((item) => item.name) ?? [];
  const counterGuarantors = projectCounterGuarantors.length ? projectCounterGuarantors : legacySnapshotCounterGuarantors;
  const high = risks.filter((risk) => risk.severity === "high").length;
  const unresolved = reviewSummary?.total_unique_issues ?? 0;
  const isRealReference = project.data_class === "real_reference";
  const guaranteeApplicable = bondSummary?.guaranteeApplicable !== false;
  const tranches = bondSummary?.tranches ?? [];
  const termLabel = tranches.length
    ? tranches.map((item) => `${item.term_years} 年`).join(" / ")
    : `${bondSummary?.termYears ?? project.term_months / 12} 年`;
  if (!inventory.documents.length) return <div className="view-content"><EmptyState onRun={onRun} running={running} hasDocuments={false} modelConfigured={modelConfigured} /></div>;
  return <div className="view-content">
    <WorkflowOverview stages={stages} run={run} nodes={nodes} readOnly={isRealReference || Boolean(project.archived_at)} running={running} onRun={onRun} navigate={onNavigate} />
    {isRealReference && <div className="reference-banner"><ShieldAlert size={18} /><div><strong>真实参考项目 · 仅用于本地分析</strong><span>{guaranteeApplicable ? "当前报告基于募集说明书和信用评级报告；评级机构意见不等于担保机构判断，补充原件仍应按待核验事项跟踪。" : "募集说明书明确本期债券无增信安排，不适用于担保案例；评级机构意见不等于担保机构判断。"}</span></div></div>}
    {unresolved > 0 && <button className="unresolved-banner" onClick={() => onNavigate("review")}><AlertTriangle size={17} /><span><strong>存在未决事项</strong> 冲突事实、候选风险或尽调任务尚未处理</span><ArrowRight size={16} /></button>}
    <div className="metric-grid cockpit-five">
      <div className="metric"><UsersRound size={20} /><span>主体资料</span><strong>{snapshot ? `v${snapshot.version}` : "未绑定"}</strong><small>{snapshot ? `基准日 ${snapshot.baseline_date || "待确认"}` : "旧项目未绑定主体档案"}</small></div>
      <div className="metric"><FileCheck2 size={20} /><span>本次交易材料</span><strong>{inventory.documents.length} 份</strong><small>{inventory.complete_due_diligence_ready ? "两份核心材料与主体档案已齐备" : "募集说明书、评级报告或主体档案待补"}</small></div>
      <div className="metric"><FileText size={20} /><span>可生成报告</span><strong>{bondSummary?.reportLevel || (inventory.complete_due_diligence_ready ? "完整尽调" : "材料不足")}</strong><small>{inventory.complete_due_diligence_ready ? "与当前材料状态一致" : "核心材料尚未满足生成条件"}</small></div>
      <div className="metric metric-alert"><ClipboardCheck size={20} /><span>待人工处理</span><strong>{reviewSummary ? unresolved : "—"}</strong><small>{reviewSummary ? `${reviewSummary.conflict_count} 冲突 · ${reviewSummary.candidate_count} 候选 · ${reviewSummary.action_package_count} 行动包` : "等待唯一问题汇总"}</small></div>
      <div className="metric"><SearchCheck size={20} /><span>Agent 状态</span><strong>{running ? "运行中" : run?.status === "completed" ? unresolved ? "待复核" : "可交付" : run?.status === "failed" ? "失败" : "未启动"}</strong><small>{run ? `${run.progress}% · 模型成本 ¥${(run.cost || 0).toFixed(3)}` : "材料盘点后启动"}</small></div>
    </div>
    <div className="overview-grid"><section className="panel project-panel"><div className="panel-title"><h2>债券及担保交易</h2><Building2 size={18} /></div><dl className="detail-list"><div><dt>发行人</dt><dd>{snapshot?.roles.find((item) => item.role === "issuer")?.name || project.company_name} <SourceBadge source={snapshot ? "主体档案" : "项目录入"} /></dd></div><div><dt>债券名称</dt><dd>{bondSummary?.bondName || project.bond_name || "材料不足"} <SourceBadge source={bondSummary?.bondNameSource === "material" ? "募集说明书" : "项目录入"} /></dd></div><div><dt>本期发行规模</dt><dd>{bondSummary?.conflictedFields?.includes("bond_issue_amount") ? "材料冲突，待确认" : `${(bondSummary?.currentIssueAmount ?? project.amount).toLocaleString()} 万元`} <SourceBadge source={bondSummary?.conflictedFields?.includes("bond_issue_amount") ? "存在冲突" : bondSummary?.issueAmountSource === "material" ? "募集说明书" : "项目录入"} tone={bondSummary?.conflictedFields?.includes("bond_issue_amount") ? "danger" : "info"} /></dd></div><div><dt>主体/债项评级</dt><dd>{bondSummary?.issuerRating || "材料不足"} / {bondSummary?.bondRating || "材料不足"} <SourceBadge source="评级报告" /></dd></div><div><dt>增信安排</dt><dd>{bondSummary?.guaranteeType || (guaranteeApplicable ? "材料不足" : "无增信安排")} <SourceBadge source="募集说明书" /></dd></div><div><dt>保证人</dt><dd>{guaranteeApplicable ? projectGuarantor || legacySnapshotGuarantor || bondSummary?.guarantor || "暂未确定" : "不适用"} {guaranteeApplicable && <SourceBadge source={projectGuarantor ? "项目录入" : legacySnapshotGuarantor ? "主体档案" : "募集说明书"} />}</dd></div><div><dt>反担保方</dt><dd>{guaranteeApplicable ? counterGuarantors.join("、") || "暂未确定或不适用" : "不适用"}</dd></div><div><dt>保证期间</dt><dd>{guaranteeApplicable ? bondSummary?.guaranteePeriod || "材料不足" : "不适用"}</dd></div><div className="full"><dt>保证范围</dt><dd>{guaranteeApplicable ? bondSummary?.guaranteeScope || "材料不足" : "不适用"}</dd></div><div className="full"><dt>募集资金用途</dt><dd>{project.purpose}</dd></div></dl></section>
      <section className="panel risk-summary"><div className="panel-title"><h2>重点风险</h2><button className="text-button" onClick={() => onOpenRisk()}>查看方案 <ArrowRight size={15} /></button></div>{risks.length ? <div className="risk-summary-list">{risks.slice(0, 4).map((risk) => <button key={risk.id} onClick={() => onOpenRisk(risk.id)}><SeverityBadge level={risk.severity} /><span>{risk.title}</span><ChevronRight size={16} /></button>)}</div> : <div className="risk-summary-empty">暂无重点风险</div>}</section></div>
    {tranches.length > 0 && <section className="panel tranche-panel"><div className="panel-title"><div><h2>债券分品种</h2><small>本期总额不超过 {(bondSummary?.currentIssueAmount ?? project.amount).toLocaleString()} 万元，材料未披露各品种分配金额</small></div><GitCompareArrows size={18} /></div><div className="tranche-table"><div className="tranche-head"><span>品种</span><span>期限</span><span>分品种金额</span><span>兑付安排</span><span>到期日</span></div>{tranches.map((tranche) => <div className="tranche-row" key={tranche.tranche_key}><strong>{tranche.tranche_name}</strong><span>{tranche.term_years} 年</span><span>{tranche.issue_amount == null ? "未披露" : `${tranche.issue_amount.toLocaleString()} 万元`}</span><span>{tranche.repayment_method}</span><span>{tranche.maturity_date || "待披露"}</span></div>)}</div></section>}
    <section className="panel next-actions"><div className="panel-title"><h2>人工复核重点</h2><Gauge size={18} /></div><div className="action-grid">{risks.slice(0, 3).map((risk, index) => <div className="action-item" key={risk.id}><span>{index + 1}</span><div><strong>{risk.title}</strong><p>{risk.action}</p></div></div>)}</div></section>
    <div className="policy-footnote"><ShieldCheck size={15} />当前策略：{policy?.active.name || "债券发行融资担保保前审查政策"} · v{policy?.lastRun?.version || policy?.active.version || "-"} · {bondSummary?.reportLevel || "完整尽调"}</div>
  </div>;
}

function FactTable({ projectId, facts, token, refresh, openCitation }: { projectId: string; facts: Fact[]; token?: string; refresh: () => Promise<void>; openCitation: (id: string) => void }) {
  const [editing, setEditing] = useState<Fact | null>(null);
  const scopeLabel: Record<string, string> = { standalone: "单体", consolidated: "合并", entity: "主体", transaction: "本次交易", unknown: "口径待确认" };
  const save = async () => { if (!editing) return; await api.saveFact(projectId, editing, token); setEditing(null); await refresh(); };
  return <section className="panel fact-panel"><div className="panel-title"><div><h2>标准化事实</h2><small>{facts.length} 项</small><HelpTip label="标准化事实说明">人工修正优先于模型结果：同一事实被人工确认后，后续自动抽取不会再覆盖它。</HelpTip></div><Database size={18} /></div>
    {!facts.length ? <div className="sub-empty">运行分析后在此复核抽取事实。</div> : <div className="fact-table">{facts.map((fact) => <div className="fact-row" key={fact.id}><span className="kind-tag">{fact.category}</span><div><strong>{fact.name}</strong><small>{fact.fact_key} · {fact.subject || "主体待确认"} · {scopeLabel[fact.scope] || fact.scope} · {fact.period_type === "point_in_time" ? "时点" : fact.period_type === "duration" ? "期间" : "期间待确认"}</small></div><span>{fact.period || "未注明"}</span><strong>{fact.text_value || `${fact.value?.toLocaleString() ?? "-"} ${fact.unit}`}</strong><span className={fact.review_status === "human_verified" ? "status-ok" : "fact-method"}>{fact.review_status === "human_verified" ? "人工确认" : fact.extraction_method}</span><div className="row-tools">{fact.source_ids[0] && <button className="icon-button" onClick={() => openCitation(fact.source_ids[0])} title="查看证据"><FileText size={15} /></button>}<button className="icon-button" onClick={() => setEditing({ ...fact })} title="修正事实"><Pencil size={15} /></button></div></div>)}</div>}
    {editing && <div className="fact-editor"><div><strong>修正：{editing.name}</strong><button className="icon-button" onClick={() => setEditing(null)}><X size={16} /></button></div><div className="form-grid"><label>期间<input value={editing.period} onChange={(event) => setEditing({ ...editing, period: event.target.value })} /></label><label>数值<input type="number" value={editing.value ?? ""} onChange={(event) => setEditing({ ...editing, value: event.target.value === "" ? null : Number(event.target.value) })} /></label><label>单位<input value={editing.unit} onChange={(event) => setEditing({ ...editing, unit: event.target.value })} /></label></div><label>文本值<input value={editing.text_value} onChange={(event) => setEditing({ ...editing, text_value: event.target.value })} /></label><button className="primary-button" onClick={save}><Check size={16} />保存并标记人工确认</button></div>}
  </section>;
}

const UPLOAD_KINDS = ["募集说明书", "信用评级报告", "补充核验材料"] as const;
type UploadKind = typeof UPLOAD_KINDS[number];
/** 页面上的「补充核验材料」落到库里就是 ``其他材料``／``other``。 */
const UPLOAD_KIND_TO_DOCUMENT_KIND: Record<UploadKind, DocumentKind> = {
  募集说明书: "募集说明书", 信用评级报告: "信用评级报告", 补充核验材料: "其他材料",
};
const DOCUMENT_KIND_OPTIONS: DocumentKind[] = ["募集说明书", "信用评级报告", "其他材料"];

/** 核心材料只有两个固定材料位，用户不必从材料列表里判断文件该归哪一类。 */
const CORE_MATERIAL_SLOTS: { kind: DocumentKind; title: string; detail: string }[] = [
  { kind: "募集说明书", title: "募集说明书", detail: "发行条款、募集资金用途、发行人基本情况与财务披露" },
  { kind: "信用评级报告", title: "信用评级报告", detail: "外部信用评级结论、级别含义与评级差异分析" },
];

function ParseStatusTag({ doc }: { doc: DocumentItem }) {
  if (doc.parse_status === "parsed") {
    return <span className="status-ok"><Check size={14} />{doc.ocr_status === "completed" ? "OCR已解析" : "已解析"}</span>;
  }
  if (doc.parse_status === "ocr_required") return <span className="status-warning">需要 OCR / 多模态解析</span>;
  return <span className="status-warning" title={doc.warning}>{doc.parse_status === "failed" ? (doc.warning || "解析失败") : (doc.warning || "解析异常")}</span>;
}

function DocumentClassificationSelect({ doc, busy, onChange }: { doc: DocumentItem; busy: boolean; onChange: (kind: DocumentKind) => void }) {
  const value = (DOCUMENT_KIND_OPTIONS as string[]).includes(doc.kind) ? doc.kind : "其他材料";
  return <select
    className="document-kind-select"
    aria-label={`修改 ${doc.filename} 分类`}
    value={value}
    disabled={busy}
    onChange={(event) => onChange(event.target.value as DocumentKind)}
  >{DOCUMENT_KIND_OPTIONS.map((option) => <option key={option}>{option}</option>)}</select>;
}

function DocumentMeta({ doc }: { doc: DocumentItem }) {
  return <small>{(doc.size / 1024).toFixed(1)} KB · {doc.locator_count} 个证据位置 · {doc.parser || "native"}{doc.page_count ? ` · ${doc.page_count}页` : ""}</small>;
}

function DocumentQuality({ doc }: { doc: DocumentItem }) {
  return <div className="quality"><div><span style={{ width: `${doc.quality * 100}%` }} /></div><small>{Math.round(doc.quality * 100)}%</small></div>;
}

function MaterialSlot({ slot, documents, editable, classifying, busy, onUpload, onClassify, onView }: {
  slot: typeof CORE_MATERIAL_SLOTS[number]; documents: DocumentItem[]; editable: boolean; classifying: string;
  busy: boolean; onUpload: () => void; onClassify: (documentId: string, kind: DocumentKind) => void; onView: (documentId: string) => void;
}) {
  const primary = documents.find((doc) => doc.parse_status === "parsed") || documents[0] || null;
  const rest = documents.filter((doc) => doc !== primary);
  const ready = Boolean(primary && primary.parse_status === "parsed");
  return <article className={`material-slot ${ready ? "ready" : documents.length ? "warning" : "empty"}`} data-material-slot={slot.kind}>
    <header>
      <div className="material-slot-title">
        {slot.kind === "募集说明书" ? <FileText size={19} /> : <ShieldCheck size={19} />}
        <div><h3>{slot.title}<HelpTip label={`${slot.title}用途`}>主要提供：{slot.detail}。</HelpTip></h3></div>
      </div>
      <StatusBadge tone={ready ? "success" : "warning"}>{ready ? "已就绪" : documents.length ? "解析未完成" : "尚未上传"}</StatusBadge>
    </header>
    {primary ? <div className="material-slot-file">
      <div className="file-cell"><div className="file-icon">{slot.kind === "募集说明书" ? <FileText size={20} /> : <ShieldCheck size={20} />}</div>
        <div><strong>{primary.filename}</strong><DocumentMeta doc={primary} /></div></div>
      <DocumentQuality doc={primary} />
      <ParseStatusTag doc={primary} />
      <div className="material-slot-actions">
        {editable && <button className="secondary-button small" onClick={onUpload} disabled={busy}>{primary.parse_status === "failed" ? <RefreshCw size={14} /> : <Upload size={14} />}{primary.parse_status === "failed" ? "重新上传并解析" : "重新上传"}</button>}
        <button className="text-button" onClick={() => onView(primary.id)}><Eye size={14} />查看材料</button>
      </div>
    </div> : <div className="material-slot-empty">
      <p>尚未上传{slot.title}</p>
      {editable ? <button className="secondary-button" onClick={onUpload} disabled={busy}><Upload size={15} />上传{slot.title}</button>
        : <small>内置参考项目为只读，材料由演示数据提供。</small>}
    </div>}
    {primary && editable && <div className="material-slot-classify">
      <span>分类</span>
      <DocumentClassificationSelect doc={primary} busy={classifying === primary.id} onChange={(kind) => onClassify(primary.id, kind)} />
      <small>分类决定这份材料能否计入核心材料就绪度</small>
    </div>}
    {rest.length > 0 && <details className="material-slot-extra">
      <summary>另有 {rest.length} 份同类材料</summary>
      {rest.map((doc) => <div className="material-slot-file compact" key={doc.id}>
        <div className="file-cell"><div className="file-icon"><FileText size={18} /></div><div><strong>{doc.filename}</strong><DocumentMeta doc={doc} /></div></div>
        <DocumentQuality doc={doc} />
        <ParseStatusTag doc={doc} />
        {editable && <DocumentClassificationSelect doc={doc} busy={classifying === doc.id} onChange={(kind) => onClassify(doc.id, kind)} />}
      </div>)}
    </details>}
  </article>;
}

function UploadMaterialModal({ initialKind, limits, onClose, upload, onUploaded, onViewExisting }: {
  initialKind: UploadKind | ""; limits?: Health["uploadLimits"];
  onClose: () => void; upload: (file: File, kind: UploadKind) => Promise<UploadedDocument>;
  onUploaded: () => Promise<void>; onViewExisting: (documentId: string) => void;
}) {
  const [kind, setKind] = useState<UploadKind | "">(initialKind);
  const [file, setFile] = useState<File | null>(null);
  const [stage, setStage] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [result, setResult] = useState<{ filename: string; kind: UploadKind; document: UploadedDocument } | null>(null);
  const [error, setError] = useState("");
  const [duplicate, setDuplicate] = useState<{ message: string; documentId: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const maxFileMb = limits?.maxFileMb ?? 15;
  const maxFiles = limits?.maxFiles ?? 8;
  const formats = (limits?.formats ?? [".pdf", ".docx", ".xlsx"]).map((item) => item.replace(".", "").toUpperCase()).join("、");

  const run = async (target: File, targetKind: UploadKind) => {
    setStage("busy"); setError(""); setDuplicate(null); setResult(null);
    try {
      const document = await upload(target, targetKind);
      setResult({ filename: target.name, kind: targetKind, document });
      setStage("done");
      await onUploaded();
    } catch (reason) {
      if (reason instanceof DuplicateDocumentError) {
        setDuplicate({ message: reason.message, documentId: reason.documentId });
        setStage("idle");
        return;
      }
      setError(reason instanceof Error ? reason.message : "材料上传失败");
      setStage("error");
    }
  };
  const pick = (files: FileList | null) => {
    const chosen = files?.[0];
    if (!chosen) return;
    setFile(chosen); setDuplicate(null); setError(""); setResult(null); setStage("idle");
  };
  const submit = () => { if (file && kind) void run(file, kind); };

  return <div className="modal-backdrop upload-backdrop"><section className="upload-modal" role="dialog" aria-modal="true" aria-labelledby="upload-modal-title">
    <header className="upload-modal-head"><div><span>材料与证据</span><h2 id="upload-modal-title">上传材料</h2></div>
      <button className="icon-button" onClick={onClose} aria-label="关闭上传材料弹窗"><X size={18} /></button></header>
    <div className="upload-modal-body">
      <label className="upload-field">材料类型
        <select aria-label="材料类型" value={kind} disabled={stage === "busy"} onChange={(event) => setKind(event.target.value as UploadKind)}>
          <option value="">请选择材料类型</option>
          {UPLOAD_KINDS.map((option) => <option key={option} value={option}>{option}</option>)}
        </select>
        <small>核心材料必须明确指定类型，不依赖文件名自动分类。补充核验材料不改变报告等级。</small>
      </label>
      <div className={`upload-drop ${file ? "has-file" : ""}`}>
        <FileUp size={22} />
        <strong>{file ? file.name : "选择材料文件"}</strong>
        <small>{file ? `${(file.size / 1024).toFixed(1)} KB` : `支持 ${formats}；单个文件不超过 ${maxFileMb} MB，每个项目最多 ${maxFiles} 份`}</small>
        <input ref={inputRef} hidden type="file" accept=".pdf,.docx,.xlsx" aria-label="选择材料文件"
          onChange={(event) => { pick(event.target.files); if (inputRef.current) inputRef.current.value = ""; }} />
        <button className="secondary-button" disabled={stage === "busy"} onClick={() => inputRef.current?.click()}>{file ? "重新选择文件" : "选择文件"}</button>
      </div>
      {!kind && <p className="upload-hint"><Info size={14} />请先选择材料类型，再上传文件。</p>}
      {stage === "busy" && <div className="upload-status busy"><LoaderCircle className="spin" size={15} />正在上传并解析材料，请勿关闭窗口…</div>}
      {duplicate && <div className="upload-status duplicate"><AlertTriangle size={15} /><div><strong>该文件已上传</strong><span>{duplicate.message}</span></div>
        {duplicate.documentId && <button className="secondary-button small" onClick={() => { onViewExisting(duplicate.documentId); onClose(); }}><Eye size={14} />查看材料</button>}</div>}
      {error && <div className="upload-status error"><AlertTriangle size={15} /><div><strong>上传或解析失败</strong><span>{error}</span></div>
        {file && kind && <button className="secondary-button small" onClick={submit}><RefreshCw size={14} />重试</button>}</div>}
      {result && <div className="upload-status done"><CheckCircle2 size={15} /><div><strong>{result.filename}</strong>
        <span>分类：{result.kind} · {result.document.parseStatus === "parsed" ? "已解析" : result.document.parseStatus === "ocr_required" ? "需要 OCR / 多模态解析" : (result.document.warning || "解析未完成")}
          {result.document.kind !== UPLOAD_KIND_TO_DOCUMENT_KIND[result.kind] ? `（系统初判为「${result.document.kind}」，已按所选类型修正）` : ""}</span></div>
        {result.document.parseStatus === "failed" && file && kind && <button className="secondary-button small" onClick={submit}><RefreshCw size={14} />重试解析</button>}
        {result.document.parseStatus === "failed" && <small className="upload-retry-hint">重试会重新解析同一份文件，不会重复占用材料位。</small>}
      </div>}
    </div>
    <footer className="upload-modal-foot">
      <button className="secondary-button" onClick={onClose}>关闭</button>
      <button className="primary-button" disabled={!file || !kind || stage === "busy"} onClick={submit}>{stage === "busy" ? <LoaderCircle className="spin" size={15} /> : <Upload size={15} />}上传</button>
    </footer>
  </section></div>;
}

function Documents({ project, inventory, facts, token, editable, snapshot, running, limits, health, onRun, onUpload, refresh, openCitation }: { project: Project; inventory: Inventory; facts: Fact[]; token?: string; editable: boolean; snapshot: ProjectProfileSnapshot | null; running: boolean; limits?: Health["uploadLimits"]; health: Health; onRun: () => void; onUpload: (files: File[]) => Promise<void>; refresh: () => Promise<void>; openCitation: (id: string, focus?: string) => void }) {
  const projectId = project.id;
  const batchInputRef = useRef<HTMLInputElement>(null); const [uploading, setUploading] = useState(false);
  const [graph, setGraph] = useState<EvidenceGraphSummary | null>(null); const [graphLoading, setGraphLoading] = useState(false);
  const [documentError, setDocumentError] = useState(""); const [classifying, setClassifying] = useState("");
  const [tab, setTab] = useState<"materials" | "facts" | "qa">("materials");
  const [question, setQuestion] = useState(""); const [qaResult, setQaResult] = useState<MaterialQAResponse | null>(null);
  const [qaLoading, setQaLoading] = useState(false); const [qaError, setQaError] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false); const [uploadKind, setUploadKind] = useState<UploadKind | "">("");
  const [highlightId, setHighlightId] = useState("");
  const openUpload = (kind: UploadKind | "") => { setUploadKind(kind); setUploadOpen(true); };
  const handleBatchFiles = async (files: FileList | null) => { if (!files?.length) return; setUploading(true); setDocumentError(""); try { await onUpload(Array.from(files)); } catch (reason) { setDocumentError(reason instanceof Error ? reason.message : "材料上传失败"); } finally { setUploading(false); if (batchInputRef.current) batchInputRef.current.value = ""; } };
  const classify = async (documentId: string, kind: DocumentKind) => { setClassifying(documentId); setDocumentError(""); try { await api.classifyDocument(projectId, documentId, kind, token); await refresh(); } catch (reason) { setDocumentError(reason instanceof Error ? reason.message : "材料分类修改失败"); } finally { setClassifying(""); } };
  const askQuestion = async () => {
    if (!question.trim() || qaLoading) return;
    setQaLoading(true); setQaError("");
    try { setQaResult(await api.materialQA(projectId, question.trim(), [], token)); }
    catch (reason) { setQaError(reason instanceof Error ? reason.message : "材料问答暂不可用"); }
    finally { setQaLoading(false); }
  };
  const readinessLabel = inventory.analysis_mode === "full_due_diligence" ? "完整尽调材料已齐备" : inventory.documents.length ? "材料不足" : "尚未上传材料";
  // 只按 ``kind`` 归类：核心材料就绪度由后端按 ``kind`` 与解析状态算出，
  // 若这里再引入 ``document_role`` 判断，就可能出现「界面显示已就绪、闸门仍关闭」，
  // 或某份材料在两个区域都不显示的错位。
  const coreDocuments = inventory.documents.filter((doc) => CORE_MATERIAL_SLOTS.some((slot) => slot.kind === doc.kind));
  const additionalDocuments = inventory.documents.filter((doc) => !coreDocuments.includes(doc));
  // 就绪度直接用后端按「已解析覆盖度」算出的比例：按文件个数推算会在两份核心
  // 文件都解析失败时显示 90%，与「启动 Agent」的禁用状态自相矛盾。
  const fullReadiness = Math.round((inventory.full_due_diligence_readiness ?? inventory.core_pre_review_readiness ?? 0) * 100);
  const supplementaryGaps = inventory.supplementary_gaps ?? [];
  const coreReady = Boolean(inventory.complete_due_diligence_ready);
  const snapshotReady = Boolean(snapshot);
  const agentBlockers = [
    !coreDocuments.some((doc) => doc.kind === "募集说明书" && doc.parse_status === "parsed") ? "募集说明书尚未解析完成" : "",
    !coreDocuments.some((doc) => doc.kind === "信用评级报告" && doc.parse_status === "parsed") ? "信用评级报告尚未解析完成" : "",
    snapshotReady ? "" : "主体档案快照不可用",
  ].filter(Boolean);
  const agentDisabled = !inventory.complete_due_diligence_ready || running;
  const viewDocument = (documentId: string) => {
    setHighlightId(documentId);
    window.requestAnimationFrame(() => document.getElementById(`document-row-${documentId}`)?.scrollIntoView({ block: "center", behavior: "smooth" }));
    window.setTimeout(() => setHighlightId((current) => current === documentId ? "" : current), 2600);
  };
  const documentRows = (documents: DocumentItem[]) => documents.map((doc) => <div className={`document-row ${highlightId === doc.id ? "highlight" : ""}`} id={`document-row-${doc.id}`} key={doc.id}><div className="file-cell"><div className={`file-icon ${doc.filename.endsWith(".xlsx") ? "excel" : ""}`}>{doc.filename.endsWith(".xlsx") ? <FileSpreadsheet size={20} /> : <FileText size={20} />}</div><div><strong>{doc.filename}</strong><DocumentMeta doc={doc} /></div></div>{editable ? <DocumentClassificationSelect doc={doc} busy={classifying === doc.id} onChange={(kind) => classify(doc.id, kind)} /> : <span className="kind-tag">{doc.kind}</span>}<DocumentQuality doc={doc} /><ParseStatusTag doc={doc} />{doc.id.startsWith("demo-") ? <a className="icon-button" href={`/api/demo/materials/${doc.id}`} title="下载材料"><Download size={17} /></a> : <button className="icon-button" title="查看材料" aria-label={`查看 ${doc.filename}`} onClick={() => viewDocument(doc.id)}><Eye size={17} /></button>}</div>);
  const retrievalLabel = health.retrieval?.mode === "hybrid" ? "本地向量 + 关键词" : "关键词检索";
  const qaStatusLabel: Record<MaterialQAResponse["status"], string> = {
    answered: "已回答", insufficient: "证据不足", retrieved_only: "仅返回检索结果",
    model_unavailable: "模型暂不可用", retrieval_unavailable: "检索暂不可用",
  };
  return <div className="view-content"><div className="page-heading"><div><span className="eyebrow">证据底稿</span><h1>材料与证据</h1></div><div className="heading-actions evidence-actions"><div className="segmented"><button className={tab === "materials" ? "active" : ""} onClick={() => setTab("materials")}>材料清单</button><button className={tab === "facts" ? "active" : ""} onClick={() => setTab("facts")}>标准化事实</button><button className={tab === "qa" ? "active" : ""} onClick={() => setTab("qa")}>材料问答</button></div>{/* 位置稳定约束：本排四个按钮必须常驻。上传材料一旦按 tab 条件渲染，页面标题的
        justify-content: space-between（styles.css:161）就会把三个标签整体右移。
        将来若要隐藏它，请改用保留占位的做法，不要用条件渲染。 */}
    {editable && <button className="primary-button upload-action" onClick={() => openUpload("")}><Upload size={17} />上传材料</button>}</div></div>
    {/* 三个标签共用这一个内容容器：宽度、上间距、底色都由它统一，切换时页面框架完全不动。
        注意不得给它加 overflow —— 材料清单末尾的 .analysis-gate 是 position:sticky，
        祖先一旦有 overflow，吸附会在容器内失效。 */}
    <div className="evidence-tab-content" role="tabpanel" aria-label="材料与证据内容">
    {tab === "materials" ? <>
    <div className={`inventory-summary ${inventory.gaps.length ? "has-gaps" : ""}`}>{inventory.gaps.length ? <CircleHelp size={18} /> : <CheckCircle2 size={18} />}<span><strong>{readinessLabel}</strong>{inventory.gaps.length ? ` · 缺少：${inventory.gaps.map((gap) => gap.group).join("、")}` : " · 核心材料已覆盖"}</span><small>{inventory.documents.length} 份材料 · {inventory.documents.reduce((sum, item) => sum + item.locator_count, 0)} 个证据位置</small><button className="text-button" disabled={graphLoading || !inventory.documents.length} onClick={async () => { setGraphLoading(true); try { setGraph(await api.evidenceGraph(projectId, token)); } finally { setGraphLoading(false); } }}>{graphLoading ? <LoaderCircle className="spin" size={14} /> : <SearchCheck size={14} />}证据链概览</button></div>
    {documentError && <div className="inline-error document-error"><AlertTriangle size={15} />{documentError}<button className="icon-button" onClick={() => setDocumentError("")} title="关闭"><X size={14} /></button></div>}
    {graph && <section className="evidence-graph-summary"><div><strong>{graph.edge_count.toLocaleString()}</strong><span>条证据关系</span></div><div><strong>{graph.entities.length}</strong><span>个识别主体</span></div><div><strong>{graph.edge_summary.length}</strong><span>类关系路径</span></div><p>{graph.edge_summary.slice(0, 4).map((item) => `${item.from_type} → ${item.to_type}（${item.count}）`).join(" · ")}{graph.truncated ? " · 明细已截断" : ""}</p><button className="icon-button" title="关闭概览" onClick={() => setGraph(null)}><X size={15} /></button></section>}
    {project.analysis_stale && <div className="material-stale-banner"><RefreshCw size={16} /><strong>{(project.report_stale_reasons || []).includes("project_info_changed") ? "项目基础信息已变更，请重新运行尽调" : "材料已变化，需要重新运行 Agent"}</strong><span>旧分析和报告仍可查看，但不代表当前材料状态。</span></div>}
    <div className="material-readiness-grid"><ReadinessBar label="完整尽调就绪度" value={fullReadiness} detail={coreReady ? "募集说明书、评级报告和主体档案均已满足要求" : `尚未满足：${agentBlockers.join("、") || "核心材料尚未解析完成"}`} tone={coreReady ? "success" : "warning"} /></div>
    {!coreReady && tab === "materials" && <section className="material-onboarding" data-testid="material-onboarding">
      {editable ? <FileUp size={19} /> : <Info size={19} />}
      <div><strong>{coreReady ? "完整尽调材料已齐备" : "项目已创建。请先上传募集说明书和信用评级报告，材料解析完成后即可启动完整尽调。"}</strong>
        <small>{inventory.documents.length ? "补充核验材料不影响完整尽调等级，可随时追加。" : `支持 ${(limits?.formats ?? [".pdf", ".docx", ".xlsx"]).map((item) => item.replace(".", "").toUpperCase()).join("、")}，单文件不超过 ${limits?.maxFileMb ?? 15} MB。`}</small></div>
      {editable ? <div className="material-onboarding-actions">
        <button className="secondary-button" onClick={() => openUpload("募集说明书")}><Upload size={15} />上传募集说明书</button>
        <button className="secondary-button" onClick={() => openUpload("信用评级报告")}><Upload size={15} />上传信用评级报告</button>
      </div> : <small>内置参考项目为只读，材料由演示数据提供。</small>}
    </section>}
    <ProjectSnapshotPanel projectId={project.id} token={token} readOnly={!editable} onOpenSource={openCitation} onRefreshed={async () => { await refresh(); }} />
    <section className="material-section"><div className="material-section-heading"><div><FileCheck2 size={18} /><div><h2>完整尽调核心材料</h2><p>募集说明书与信用评级报告，用于覆盖发行、财务、募集资金用途及受托管理披露事项</p></div></div><StatusBadge tone={coreReady ? "success" : "warning"}>{coreReady ? "已齐备" : "待补充"}</StatusBadge></div>
      <div className="material-slot-grid">{CORE_MATERIAL_SLOTS.map((slot) => <MaterialSlot key={slot.kind} slot={slot}
        documents={inventory.documents.filter((doc) => doc.kind === slot.kind)}
        editable={editable} classifying={classifying} busy={uploading}
        onUpload={() => openUpload(UPLOAD_KINDS.find((item) => UPLOAD_KIND_TO_DOCUMENT_KIND[item] === slot.kind) as UploadKind)}
        onClassify={classify} onView={viewDocument} />)}</div></section>
    <section className="material-section"><div className="material-section-heading"><div><FolderPlus size={18} /><div><h2>补充核验材料</h2><p>债务、法律、担保、反担保及经营原件用于增强证据与交叉验证，不影响报告等级</p></div></div>{editable && <button className="secondary-button" onClick={() => batchInputRef.current?.click()} disabled={uploading}>{uploading ? <LoaderCircle className="spin" size={15} /> : <Upload size={15} />}上传补充材料</button>}<input ref={batchInputRef} hidden multiple type="file" accept=".pdf,.docx,.xlsx" onChange={(event) => handleBatchFiles(event.target.files)} /></div>
      {additionalDocuments.length ? <div className="panel table-panel material-table"><div className="table-header"><span>材料</span><span>分类</span><span>解析质量</span><span>状态</span><span /></div>{documentRows(additionalDocuments)}</div> : <div className="review-empty material-empty">当前未上传补充核验材料，待核验事项将按资料包聚合。</div>}</section>
    {inventory.gaps.length > 0 && <section className="panel material-gap-packages"><div className="panel-title"><h2>完整尽调核心材料缺口</h2><span>{inventory.gaps.length} 个资料包</span></div>{inventory.gaps.map((gap) => <details key={gap.issue_key || gap.group}><summary><div><strong>{gap.group}</strong><small>{gap.affected_rule_ids?.length || 0} 项核验规则受影响</small></div><StatusBadge tone="danger">阻塞</StatusBadge></summary><p>所需材料：{gap.expectedKinds.join("、") || "由复核人员确认"}</p></details>)}</section>}
    {supplementaryGaps.length > 0 && <section className="panel material-gap-packages"><div className="panel-title"><h2>补充核验材料</h2><span>{supplementaryGaps.length} 个资料包</span></div>{supplementaryGaps.map((gap) => <details key={gap.issue_key || gap.group}><summary><div><strong>{gap.group}</strong><small>{gap.affected_rule_ids?.length || 0} 项核验规则受影响</small></div><StatusBadge tone="warning">待核验</StatusBadge></summary><p>建议补充：{gap.expectedKinds.join("、") || "由复核人员确认"}</p></details>)}</section>}
    {editable && <section className="analysis-gate"><div><span>当前可生成</span><strong>{coreReady ? "完整尽调" : "材料不足"}</strong><small>{snapshot ? `将使用主体档案快照 v${snapshot.version}` : "当前项目未绑定主体档案"} · {supplementaryGaps.length} 个补充核验资料包{running ? " · Agent 运行中，材料暂不可修改" : ""}</small>{!coreReady && agentBlockers.length > 0 && <small className="analysis-gate-blockers">启动条件未满足：{agentBlockers.join("、")}</small>}</div><button className="primary-button" disabled={agentDisabled} onClick={onRun}>{running ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}启动 Agent</button></section>}
    </> : tab === "facts" ? <FactTable projectId={projectId} facts={facts} token={token} refresh={refresh} openCitation={openCitation} /> : <section className="panel material-qa" data-testid="material-qa">
      <div className="material-qa-heading"><p>回答引用仅来自当前项目材料，冲突来源会并列显示。</p><span className={`retrieval-mode ${health.retrieval?.mode === "hybrid" ? "ready" : "fallback"}`}><SearchCheck size={14} />{retrievalLabel}</span></div>
      <div className="material-qa-notice"><ShieldCheck size={15} /><span>检索和索引在本机运行。生成回答时，引用片段会发送到此项目配置的模型服务。</span></div>
      <div className="material-qa-compose"><textarea value={question} maxLength={1000} placeholder="输入问题，例如：募集资金具体用于哪些项目？" onChange={(event) => setQuestion(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void askQuestion(); } }} /><button className="primary-button" disabled={!question.trim() || qaLoading || !inventory.documents.length} onClick={askQuestion}>{qaLoading ? <LoaderCircle className="spin" size={16} /> : <Send size={15} />}提问</button></div>
      {qaError && <div className="inline-error"><AlertTriangle size={15} />{qaError}</div>}
      {qaLoading && <div className="material-qa-loading"><LoaderCircle className="spin" size={16} />正在检索项目材料并生成回答</div>}
      {!qaLoading && qaResult && <div className="material-qa-result">
        <div className="material-qa-result-head"><StatusBadge tone={qaResult.status === "answered" ? "success" : "warning"}>{qaStatusLabel[qaResult.status]}</StatusBadge>{qaResult.conflict_detected && <span className="material-qa-conflict"><AlertTriangle size={14} />命中待复核的冲突来源</span>}{qaResult.retrieval_metrics && <small className="material-qa-metrics">{qaResult.retrieval_metrics.chunk_count} 个片段 · 检索 {qaResult.retrieval_metrics.query_ms} ms{qaResult.retrieval_metrics.index_build_ms ? ` · 建索引 ${qaResult.retrieval_metrics.index_build_ms} ms` : ""}</small>}</div>
        <p className="material-qa-answer">{qaResult.answer}</p>
        {qaResult.claims.map((claim, index) => <div className="material-qa-claim" key={`${index}-${claim.text}`}><p>{claim.text}</p><div>{claim.source_ids.flatMap((sourceId) => qaResult.citations.filter((citation) => citation.source_id === sourceId)).map((citation) => <button key={citation.chunk_id} className="material-qa-citation" onClick={() => openCitation(citation.source_id, citation.quote)}><FileText size={13} /><span>{citation.filename}{citation.page ? ` · 第 ${citation.page} 页` : ""}</span></button>)}</div></div>)}
        <div className="material-qa-evidence"><strong>检索到的原文</strong>{qaResult.citations.map((citation) => <article key={citation.chunk_id}><div><span>{citation.filename}{citation.page ? ` · 第 ${citation.page} 页` : ""}</span>{citation.chunk_type === "table" && <small>表格 {citation.table_key}</small>}</div><p>{citation.quote}</p><button className="link-button" onClick={() => openCitation(citation.source_id, citation.quote)}>查看原页</button></article>)}</div>
      </div>}
    </section>}
    </div>
    {uploadOpen && <UploadMaterialModal initialKind={uploadKind} limits={limits} onClose={() => setUploadOpen(false)}
      upload={(file, kind) => api.uploadOne(projectId, file, UPLOAD_KIND_TO_DOCUMENT_KIND[kind], token)}
      onUploaded={async () => { await refresh(); }} onViewExisting={viewDocument} />}
  </div>;
}

/* 运行节点标签与步骤条 / 状态栏 / 指标抽屉 / 核验分区已抽到
   `src/features/analysis/`（Layer 1–3 全部在分析模块内），Check 只负责组装数据与详情渲染。 */

function Checks({ projectId, token, checks, metrics, tieOuts, policy, openCitation, run, nodes, onRetry, evidenceOpen }: {
  projectId: string; token?: string; checks: CrossCheck[]; metrics: FinancialMetric[]; tieOuts: FinancialTieOut[];
  policy: PolicyInfo | null; openCitation: (id: string) => void; run: Run | null; nodes: RunNode[]; onRetry?: () => void;
  evidenceOpen?: boolean;
}) {
  const resetKey = `${projectId}:${run?.id ?? ""}`;
  const [drawerOpen, setDrawerOpen] = useState(false);
  // 当前打开的是哪张核验卡片的详情弹窗（两卡互斥，同时只开一个）。
  const [detailId, setDetailId] = useState<"tie-outs" | "checks" | null>(null);
  // 切换项目 / 切换运行：关闭指标抽屉与详情弹窗，避免看到上一个项目/运行的结果。
  useEffect(() => { setDrawerOpen(false); setDetailId(null); }, [resetKey]);

  const tieOutCounts = useMemo(() => countStatuses(tieOuts), [tieOuts]);
  const checkCounts = useMemo(() => countStatuses(checks), [checks]);
  const verdict = useMemo(() => deriveVerdict({
    total: tieOutCounts.total + checkCounts.total,
    warning: tieOutCounts.warning + checkCounts.warning,
    insufficient: tieOutCounts.insufficient + checkCounts.insufficient,
    passed: tieOutCounts.passed + checkCounts.passed,
  }), [tieOutCounts, checkCounts]);

  // L2 财务汇总：三个数只描述数据量，不出现任何"通过/异常"字样（异常只由核验卡片表达）。
  const metricKinds = useMemo(() => metricKindCount(metrics), [metrics]);
  const latestMetrics = useMemo(() => latestMetricRecords(metrics), [metrics]);
  const validMetricCount = useMemo(() => latestMetrics.filter((item) => item.value !== null && item.value !== undefined).length, [latestMetrics]);
  const metricRange = useMemo(() => metricPeriodRange(metrics), [metrics]);

  const tieRows = useMemo<AnalysisResultRow[]>(() => tieOuts.map((item) => ({
    id: item.id, status: item.status, title: item.title, summary: item.rationale,
    meta: `${item.period || "—"} · ${scopeLabel(item.scope)}`,
  })), [tieOuts]);
  const checkRows = useMemo<AnalysisResultRow[]>(() => checks.map((item) => ({
    id: item.id, status: item.status, title: item.title, summary: item.summary,
    meta: scopeLabel(item.scope),
  })), [checks]);

  const numberText = (value: number | null | undefined) => (value === null || value === undefined ? "—" : value.toLocaleString(undefined, { maximumFractionDigits: 4 }));

  const renderTieDetail = useCallback((row: AnalysisResultRow) => {
    const item = tieOuts.find((entry) => entry.id === row.id);
    if (!item) return null;
    const overTolerance = item.difference !== null && item.tolerance !== null && Math.abs(item.difference) > item.tolerance;
    return <>
      <dl>
        <dt>左值</dt><dd>{numberText(item.left_value)}</dd>
        <dt>右值</dt><dd>{numberText(item.right_value)}</dd>
        <dt>差异</dt><dd className={overTolerance ? "is-over" : undefined}>{numberText(item.difference)}</dd>
        <dt>容差</dt><dd>{numberText(item.tolerance)}</dd>
      </dl>
      {item.formula && <p className="arr-formula">{item.formula}</p>}
      <div className="arr-foot">
        {item.policy_version && <span className="policy-chip">v{item.policy_version}</span>}
        <div className="evidence-links">{item.source_ids.map((id) => <button key={id} onClick={() => openCitation(id)}><FileText size={14} />查看证据</button>)}</div>
      </div>
    </>;
  }, [tieOuts, openCitation]);

  const renderCheckDetail = useCallback((row: AnalysisResultRow) => {
    const item = checks.find((entry) => entry.id === row.id);
    if (!item) return null;
    const max = Math.max(...item.values_json.map((value) => Math.abs(value.value)), 1);
    const thresholds = Object.entries(item.thresholds || {}).map(([key, value]) => `${key}=${value}`).join(" · ") || "无";
    return <>
      <div className="arr-detail-head"><SeverityBadge level={item.severity} /></div>
      {item.values_json.length > 0 && <div className="bar-group">{item.values_json.map((value, index) => <div className="bar-row" key={value.label}><span>{value.label}</span><div><i style={{ width: `${Math.max(8, Math.abs(value.value) / max * 100)}%` }} className={index ? "bar-alt" : ""} /></div><strong>{value.value.toLocaleString()}</strong></div>)}</div>}
      <dl>
        <dt>公式</dt><dd className="arr-mono">{item.formula}</dd>
        <dt>阈值</dt><dd>{thresholds}</dd>
        <dt>政策版本</dt><dd>v{item.policy_version || policy?.active.version || "-"}</dd>
      </dl>
      <div className="evidence-links">{item.source_ids.map((id) => <button key={id} onClick={() => openCitation(id)}><FileText size={14} />查看证据</button>)}</div>
    </>;
  }, [checks, policy, openCitation]);

  return <div className="view-content">
    <div className="page-heading"><div><span className="eyebrow">确定性风控内核</span><h1>财务与交叉核验</h1></div><div className="policy-chip"><ShieldCheck size={14} />v{policy?.lastRun?.version || policy?.active.version || "-"} · {checks.length} 条规则</div></div>
    <RunFocus run={run} nodes={nodes} projectId={projectId} formatDateTime={formatDateTime} onRetry={onRetry} verdict={verdict} />
    <section className="analysis-finance-summary">
      <div className="afs-title"><Table2 size={18} /><strong>财务指标</strong></div>
      <div className="afs-stat"><span>指标种类</span><strong>{metricKinds} 类</strong></div>
      <div className="afs-stat"><span>有效计算</span><strong>{validMetricCount} / {latestMetrics.length}</strong></div>
      <div className="afs-stat"><span>期间范围</span><strong>{metricRange}</strong></div>
      <span className="afs-note">异常数量见下方核验卡片</span>
      <button className="primary-button afs-action" onClick={() => setDrawerOpen(true)}><ListFilter size={16} />查看全部指标</button>
    </section>
    {/* 两张核验模块摘要卡片并排；完整结果在各自的详情弹窗里（取消页内折叠结果区）。 */}
    <div className="analysis-cards">
      <AnalysisSummaryCard id="tie-outs" title="报表勾稽校验" description="同一主体、期间及报表口径内核对，缺项不按零值处理。"
        help={<p>同一主体、期间及报表口径内核对左右值，差异超过容差记为异常；缺项按材料不足处理，不按零值。</p>}
        icon={GitCompareArrows} counts={tieOutCounts} rows={tieRows}
        onOpenDetail={() => setDetailId("tie-outs")} detailLabelId="analysis-tie-outs-detail" />
      <AnalysisSummaryCard id="checks" title="核验规则" description="确定性内核执行的规则校验，不调用模型。"
        help={<p>规则由确定性内核按政策阈值执行，不调用模型；阈值与公式见每条结果的详情。</p>}
        icon={ShieldCheck} counts={checkCounts} rows={checkRows}
        onOpenDetail={() => setDetailId("checks")} detailLabelId="analysis-checks-detail" />
    </div>
    <AnalysisDetailModal open={detailId === "tie-outs"} onClose={() => setDetailId(null)}
      id="tie-outs" title="报表勾稽校验" description="同一主体、期间及报表口径内核对，缺项不按零值处理。"
      icon={GitCompareArrows} rows={tieRows} counts={tieOutCounts}
      emptyText="材料尚不足以执行资产负债表勾稽" emptyIcon={GitCompareArrows}
      renderDetail={renderTieDetail} resetKey={resetKey} evidenceOpen={evidenceOpen} />
    <AnalysisDetailModal open={detailId === "checks"} onClose={() => setDetailId(null)}
      id="checks" title="核验规则" description="确定性内核执行的规则校验，不调用模型。"
      icon={ShieldCheck} rows={checkRows} counts={checkCounts}
      emptyText="尚未生成核验规则结果" emptyIcon={ShieldCheck}
      renderDetail={renderCheckDetail} resetKey={resetKey} evidenceOpen={evidenceOpen} />
    <MetricsDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} metrics={metrics} projectId={projectId} token={token} openCitation={openCitation} evidenceOpen={evidenceOpen} />
  </div>;
}

function ReviewCenter({ projectId, token, factGroups, candidates, tasks, auditEvents, reviewSummary, snapshot, checks, policy, refresh, openCitation, readOnly = false }: {
  projectId: string; token?: string; factGroups: FactGroup[]; candidates: CandidateFinding[]; tasks: ReviewTask[];
  auditEvents: AuditEvent[]; reviewSummary: ReviewSummary | null; snapshot: ProjectProfileSnapshot | null;
  checks: CrossCheck[]; policy: PolicyInfo | null; refresh: () => Promise<void>; openCitation: (id: string, focus?: string) => void; readOnly?: boolean;
}) {
  type ItemKind = "conflict" | "risk" | "material";
  type ReviewItem = {
    key: string; kind: ItemKind; title: string;
    status: "open" | "resolved" | "waived" | "proposed" | "confirmed" | "rejected";
    priority?: Severity; severity?: Severity; summary: string;
    conflict?: FactGroup; risk?: CandidateFinding; task?: ReviewTask;
  };
  const conflicts = factGroups.filter((item) => item.status === "conflicted");
  const actionPackages = Array.from(new Map(tasks.filter((item) => !["fact_conflict", "candidate_finding"].includes(item.task_type)).map((item) => [item.issue_key || `${item.task_type}:${item.title}`, item])).values());

  const items: ReviewItem[] = [
    ...conflicts.map((group) => ({ key: `conflict:${group.id}`, kind: "conflict" as const, title: group.name, status: "open" as const, summary: `${group.assertions.length} 个来源披露值不一致，需人工确认采用值`, conflict: group })),
    ...candidates.map((item) => ({ key: `risk:${item.id}`, kind: "risk" as const, title: item.title, status: item.status, severity: item.severity, summary: item.potential_impact || item.rationale.slice(0, 48), risk: item })),
    ...actionPackages.map((task) => ({ key: `material:${task.issue_key || task.id}`, kind: "material" as const, title: task.title, status: task.status, priority: task.priority, summary: task.description, task })),
  ];
  const openItems = items.filter((item) => ["open", "proposed"].includes(item.status));

  const [activeView, setActiveView] = useState<"items" | "notices" | "records">("items");
  const [keyword, setKeyword] = useState("");
  const [kindFilter, setKindFilter] = useState<"all" | ItemKind>("all");
  const [statusFilter, setStatusFilter] = useState<"all" | "open" | "done">("open");
  const [selectedKey, setSelectedKey] = useState("");
  const [mobileDetail, setMobileDetail] = useState(false);
  const [busy, setBusy] = useState("");
  // 复核更新后的受影响模型章节与再生入口（收口方案 §三：复核更新后显示受影响模型章节
  // 及再生入口）。带上 projectId 避免切换项目后残留上一个项目的结果。
  const [applyResult, setApplyResult] = useState<{ projectId: string; sections: AffectedSection[] } | null>(null);
  const [sourceLabels, setSourceLabels] = useState<Record<string, string>>({});
  const pendingSources = useRef<Set<string>>(new Set());

  const filtered = items.filter((item) => {
    if (kindFilter !== "all" && item.kind !== kindFilter) return false;
    const isOpen = ["open", "proposed"].includes(item.status);
    if (statusFilter === "open" && !isOpen) return false;
    if (statusFilter === "done" && isOpen) return false;
    const text = `${item.title} ${item.summary}`.toLowerCase();
    return !keyword.trim() || text.includes(keyword.trim().toLowerCase());
  }).sort((left, right) => (["open", "proposed"].includes(left.status) ? 0 : 1) - (["open", "proposed"].includes(right.status) ? 0 : 1));

  // 切换项目或筛选后清除已失效的选中项，避免右侧展示其他项目/其他筛选下的问题
  useEffect(() => {
    setSelectedKey((current) => (filtered.some((item) => item.key === current) ? current : ""));
    setMobileDetail(false);
  }, [projectId]);
  useEffect(() => {
    if (filtered.length === 1) { setSelectedKey(filtered[0].key); return; }
    setSelectedKey((current) => (filtered.some((item) => item.key === current) ? current : ""));
  }, [keyword, kindFilter, statusFilter, items.length]);
  // 改筛选后回到清单（手机端），避免停留在已被筛掉的详情上
  useEffect(() => { setMobileDetail(false); }, [keyword, kindFilter, statusFilter]);

  const selected = items.find((item) => item.key === selectedKey) || null;

  // 规则键 → 中文核验名称（用现有政策规则与已执行核验，不另写规则清单）
  const ruleTitles = useMemo(() => {
    const map: Record<string, string> = {};
    (policy?.active.rules || []).forEach((rule) => { map[rule.key] = rule.title; });
    checks.forEach((check) => { if (check.rule_key && check.title) map[check.rule_key] = check.title; });
    return map;
  }, [policy, checks]);
  const checkByRule = useMemo(() => {
    const map: Record<string, CrossCheck> = {};
    checks.forEach((check) => { if (check.rule_key) map[check.rule_key] = check; });
    return map;
  }, [checks]);
  const ruleTitle = (key: string) => ruleTitles[key] || "核验名称待补充";
  const categoryOf = (ruleKey: string) => {
    const text = `${ruleKey} ${ruleTitles[ruleKey] || ""}`;
    if (/rating|评级/.test(text)) return "评级类核验";
    if (/debt|偿债|偿付|流动|现金|负债|利息/.test(text)) return "偿债能力核验";
    if (/revenue|profit|经营|收入|利润|应收|存货/.test(text)) return "经营与财务核验";
    if (/guarantee|担保|增信|反担保|抵押|质押/.test(text)) return "担保安排核验";
    if (/legal|litigation|诉讼|法律|合规|处罚/.test(text)) return "法律与合规核验";
    return "其他核验";
  };

  // 证据入口显示材料名称与定位，不再显示截短 ID。
  // 只有冲突断言与候选风险持有真正的来源 ID；任务上的 related_ids 是内部事实/规则 ID，不能当来源解析。
  const sourceIdsOf = (item: ReviewItem | null) => {
    if (!item) return [] as string[];
    if (item.conflict) return item.conflict.assertions.flatMap((assertion) => assertion.source_ids || []).filter(Boolean);
    if (item.risk) return [...item.risk.support_source_ids, ...item.risk.counter_source_ids].filter(Boolean);
    return [] as string[];
  };
  const ensureSources = useCallback((ids: string[]) => {
    const missing = ids.filter((id) => id && !sourceLabels[id] && !pendingSources.current.has(id));
    if (!missing.length) return;
    missing.forEach((id) => pendingSources.current.add(id));
    void Promise.all(missing.map((id) => api.citation(id, token)
      .then((citation) => [id, `${citation.label}${citation.page ? ` · 第${citation.page}页` : citation.locator ? ` · ${citation.locator}` : ""}`] as const)
      .catch(() => [id, "来源待补充"] as const)))
      .then((pairs) => setSourceLabels((current) => ({ ...current, ...Object.fromEntries(pairs) })));
  }, [sourceLabels, token]);
  useEffect(() => { ensureSources(sourceIdsOf(selected)); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [selectedKey, projectId]);

  const act = async (key: string, action: () => Promise<unknown>) => { setBusy(key); try { await action(); await refresh(); } finally { setBusy(""); } };
  const regenerateAffected = async (item: AffectedSection) => {
    await act(item.section_key, () => api.regenerate(projectId, item.section_key, token));
    // 已再生的章节从受影响清单移除（该章已按当前版本重出；再生成会重置其复核状态）。
    setApplyResult((current) => current ? { ...current, sections: current.sections.filter((entry) => entry.section_key !== item.section_key) } : current);
  };
  const reportNeedsApply = Boolean(reviewSummary?.report_stale);

  const kindLabel: Record<ItemKind, string> = { conflict: "事实冲突", risk: "待确认风险", material: "待补资料" };
  const statusLabel = (status: ReviewItem["status"]) => status === "open" ? "待处理" : status === "proposed" ? "待确认" : status === "confirmed" ? "已确认" : status === "rejected" ? "已驳回" : status === "resolved" ? "已解决" : "已豁免";
  const statusTone = (status: ReviewItem["status"]) => ["open", "proposed"].includes(status) ? "warning" : status === "rejected" ? "danger" : "success";
  const levelText = (item: ReviewItem) => {
    const level = item.kind === "risk" ? item.severity : item.priority;
    const word = level === "high" ? "高" : level === "medium" ? "中" : "低";
    if (!level) return "—";
    return item.kind === "risk" ? `${word}风险` : `${word}优先级`;
  };
  const taxonomyLabels: Record<string, string> = { governance: "治理", continuity: "经营连续性", authenticity: "交易真实性", fund_diversion: "资金挪用", key_person: "关键人物", collateral_enforceability: "抵押可执行性" };
  const actionLabels: Record<string, string> = { resolve_fact_group: "确认冲突采用值", decide_candidate: "确认候选风险", update_review_task: "更新尽调任务", apply_review: "应用复核并刷新报告", project_update: "修改项目基础信息", project_archive: "归档项目", project_restore: "恢复项目" };
  const entityLabels: Record<string, string> = { project: "项目", fact_group: "事实冲突", candidate_finding: "候选风险", review_task: "尽调任务", report: "报告", document: "材料" };
  const actorLabels: Record<string, string> = { human: "人工操作", system: "系统", agent: "Agent" };

  const busySpinner = <LoaderCircle className="spin" size={14} />;
  const sourceButton = (id: string, focus?: string) => <button className="rc-source" key={id} onClick={() => openCitation(id, focus)}><FileText size={13} />{sourceLabels[id] || "读取来源…"}</button>;

  return <div className="view-content rc-view">
    {reportNeedsApply && <div className="report-block-warning"><RefreshCw size={16} /><span>复核决议已更新，报告仍为旧版本。</span><button className="secondary-button small" disabled={busy === "__apply__"} onClick={() => act("__apply__", async () => { const result = await api.applyReview(projectId, token); setApplyResult({ projectId, sections: (result.affected_sections || []).filter((item) => item.llm && item.regenerable) }); })}>{busy === "__apply__" ? busySpinner : <RefreshCw size={14} />}应用复核并刷新报告</button></div>}
    {applyResult?.projectId === projectId && applyResult.sections.length > 0 && <div className="report-block-warning rc-affected"><RefreshCw size={16} /><span>复核已应用：{applyResult.sections.length} 个模型章节受复核更新影响。保存与复核更新不会自动再生，可按需逐个重新生成；重新生成会重置该章复核状态。</span><div className="rc-affected-actions">{applyResult.sections.map((item) => <button key={item.section_key} className="secondary-button small" disabled={busy === item.section_key} title={`重新生成“${item.title}”并重置其复核状态`} onClick={() => regenerateAffected(item)}>{busy === item.section_key ? busySpinner : <RefreshCw size={14} />}重新生成“{item.title}”</button>)}</div></div>}
    <div className="page-heading"><div><div className="page-title-row"><h1>复核中心</h1><HelpTip label="复核中心说明">集中处理事实冲突、待确认风险与待补资料事项；资料提示只是信息，不计入待办。</HelpTip></div></div></div>
    <section className="rc-summary">
      <div><span>待处理事项</span><strong>{reviewSummary?.total_unique_issues ?? openItems.length}</strong></div>
      <div><span>事实冲突</span><strong>{reviewSummary?.conflict_count ?? conflicts.length}</strong></div>
      <div><span>待确认风险</span><strong>{reviewSummary?.candidate_count ?? candidates.filter((item) => item.status === "proposed").length}</strong></div>
      <div><span>待补资料</span><strong>{reviewSummary?.action_package_count ?? actionPackages.filter((item) => item.status === "open").length}</strong></div>
      <HelpTip label="汇总口径说明">四项均取后端去重结果：受同一材料缺口影响的多个核验项合并为一条待补资料事项，不重复计入待处理事项。</HelpTip>
    </section>
    {!reviewSummary && <div className="rc-pending"><CircleHelp size={16} />正在等待后端唯一问题汇总；为避免重复计数，页面不自行累加冲突、候选项和任务。</div>}

    <nav className="rc-tabs" role="tablist">
      <button className={activeView === "items" ? "active" : ""} onClick={() => setActiveView("items")}>复核事项 <span>{openItems.length}</span></button>
      <button className={activeView === "notices" ? "active" : ""} onClick={() => setActiveView("notices")}>资料提示 <span>{reviewSummary?.information_notice_count ?? 0}</span></button>
      <button className={activeView === "records" ? "active" : ""} onClick={() => setActiveView("records")}>处理记录 <span>{auditEvents.length}</span></button>
    </nav>

    {activeView === "items" && <div className={`rc-split ${mobileDetail ? "show-detail" : ""}`}>
      <section className="rc-list-panel">
        <div className="rc-filters">
          <label><Search size={15} /><input aria-label="搜索复核事项" value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="事项名称或影响摘要" /></label>
          <select aria-label="事项类型筛选" value={kindFilter} onChange={(event) => setKindFilter(event.target.value as "all" | ItemKind)}>
            <option value="all">全部类型</option><option value="conflict">事实冲突</option><option value="risk">待确认风险</option><option value="material">待补资料</option>
          </select>
          <select aria-label="处理状态筛选" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as "all" | "open" | "done")}>
            <option value="open">待处理</option><option value="done">已处理</option><option value="all">全部状态</option>
          </select>
        </div>
        {!filtered.length ? <div className="rc-empty"><CheckCircle2 size={18} />{statusFilter === "open" && !keyword.trim() && kindFilter === "all" ? "当前无待处理事项" : "没有符合条件的事项"}</div>
          : <div className="rc-table-scroll"><table className="rc-table">
            <thead><tr><th>事项名称</th><th>事项类型</th><th>优先级 / 风险</th><th>处理状态</th></tr></thead>
            <tbody>{filtered.map((item) => <tr key={item.key} className={item.key === selectedKey ? "selected" : ""} onClick={() => { setSelectedKey(item.key); setMobileDetail(true); }}>
              <td><strong>{item.title}</strong><small>{item.summary}</small></td>
              <td>{kindLabel[item.kind]}</td>
              <td>{item.kind === "conflict" ? "—" : levelText(item)}</td>
              <td><StatusBadge tone={statusTone(item.status)}>{statusLabel(item.status)}</StatusBadge></td>
            </tr>)}</tbody>
          </table></div>}
      </section>

      <section className="rc-detail">
        <button className="text-button rc-back" onClick={() => setMobileDetail(false)}><ArrowLeft size={15} />返回清单</button>
        {!selected ? <div className="rc-empty"><CircleHelp size={18} />请选择左侧事项查看详情</div> : <>
          <header className="rc-detail-head">
            <h2>{selected.title}</h2>
            <div className="rc-meta">
              <StatusBadge tone="neutral">{kindLabel[selected.kind]}</StatusBadge>
              {selected.kind !== "conflict" && <StatusBadge tone={selected.kind === "risk" ? "danger" : "warning"}>{levelText(selected)}</StatusBadge>}
              <StatusBadge tone={statusTone(selected.status)}>{statusLabel(selected.status)}</StatusBadge>
              <span>负责人 {selected.task?.assignee || "未分配"}</span>
              <span>截止日期 {selected.task?.due_date || "未设置"}</span>
            </div>
          </header>

          <section className="rc-block">
            <h3>影响与缺口</h3>
            {selected.kind === "material" && <p>实际缺少：{selected.task?.source_requirement || selected.task?.required_evidence || "待补充相关原始资料"}</p>}
            {selected.kind === "risk" && <p>可能影响：{selected.risk?.potential_impact || "尚未评估"}</p>}
            <p>{selected.kind === "conflict" ? selected.summary : selected.task?.description || selected.summary}</p>
            {selected.task?.blocking_level === "blocking" && <p className="rc-blocking"><AlertTriangle size={14} />该缺口会阻塞相关核验，必须补齐后才能得出确定性结论。</p>}
          </section>

          {selected.kind === "material" && <section className="rc-block">
            <h3>受影响核验项{selected.task?.affected_rule_ids?.length ? <HelpTip label="受影响核验项说明">按业务类别组织；中文名称取自现有项目核验结果与政策规则，未知代码显示“核验名称待补充”。</HelpTip> : null}</h3>
            {(() => {
              const ids = selected.task?.affected_rule_ids || [];
              if (!ids.length) return <p className="rc-muted">尚未关联到具体核验项。</p>;
              const grouped = ids.reduce<Record<string, string[]>>((result, key) => { (result[categoryOf(key)] ||= []).push(key); return result; }, {});
              return <>{Object.entries(grouped).map(([category, keys]) => <div className="rc-checks" key={category}>
                <strong>{category}<small>{keys.length} 项</small></strong>
                {keys.map((key) => {
                  const check = checkByRule[key];
                  const unknown = ruleTitle(key) === "核验名称待补充";
                  return <div className="rc-check-row" key={key}>
                    <span>{ruleTitle(key)}{unknown && <HelpTip label="核验代码说明">内部代码：{key}</HelpTip>}</span>
                    <small>{check?.summary || check?.rationale || (unknown ? "该项尚未纳入本次核验结果" : "因资料不足无法判断")}</small>
                  </div>;
                })}
              </div>)}</>;
            })()}
          </section>}

          {selected.kind === "conflict" && <section className="rc-block">
            <h3>事实冲突</h3>
            <div className="rc-assertions">{selected.conflict?.assertions.map((assertion) => <div className="rc-assertion" key={assertion.id}>
              <strong>{assertion.text_value || `${assertion.value?.toLocaleString() ?? "—"}${assertion.unit || ""}`}</strong>
              <span>期间 {assertion.period || selected.conflict?.period || "未注明"} · 口径 {assertion.scope || "未注明"}</span>
              <span>来源 {assertion.source_ids?.length ? "见下方证据入口" : "来源待补充"}</span>
              <div className="rc-sources">{(assertion.source_ids || []).map((id) => sourceButton(id, assertion.raw_value || assertion.text_value || (assertion.value != null ? String(assertion.value) : "")))}</div>
            </div>)}</div>
            {!readOnly && <div className="rc-actions"><button className="primary-button small" disabled={!selectedKey || busy === selected.conflict?.id} onClick={() => { const group = selected.conflict; if (!group) return; const first = group.assertions[0]?.id; void act(group.id, () => api.resolveFactGroup(projectId, group.id, first, "人工核对原始材料后确认采用该来源", token)); }}>{busy === selected.conflict?.id ? busySpinner : <Check size={14} />}确认采用第一条披露值</button></div>}
          </section>}

          {selected.kind === "risk" && <section className="rc-block">
            <h3>待确认风险<HelpTip label="风险信息说明">置信度 {Math.round((selected.risk?.confidence || 0) * 100)}%，内部分类 {taxonomyLabels[selected.risk?.taxonomy || ""] || selected.risk?.taxonomy || "未分类"}。</HelpTip></h3>
            <p>发现依据：{selected.risk?.rationale}</p>
            <p>待核验问题：{selected.risk?.questions?.join("；") || "无"}</p>
            <p>缓释建议：{selected.risk?.mitigation || "暂无"}</p>
            <div className="rc-sources"><span>支持材料</span>{(selected.risk?.support_source_ids || []).map((id) => sourceButton(id))}{(selected.risk?.support_source_ids || []).length ? null : <small className="rc-muted">无</small>}</div>
            <div className="rc-sources"><span>反证材料</span>{(selected.risk?.counter_source_ids || []).map((id) => sourceButton(id))}{(selected.risk?.counter_source_ids || []).length ? null : <small className="rc-muted">未检索到</small>}</div>
            {!readOnly && selected.risk?.status === "proposed" && <div className="rc-actions">
              <button className="secondary-button small" disabled={busy === selected.risk.id} onClick={() => act(selected.risk!.id, () => api.decideCandidate(projectId, selected.risk!.id, "rejected", "人工复核后未确认该风险", token))}><X size={14} />驳回</button>
              <button className="primary-button small" disabled={busy === selected.risk.id} onClick={() => act(selected.risk!.id, () => api.decideCandidate(projectId, selected.risk!.id, "confirmed", "人工复核支持证据后确认", token))}><Check size={14} />确认风险</button>
            </div>}
            {selected.risk?.decision_note && <p className="rc-note">处理说明：{selected.risk.decision_note}</p>}
          </section>}

          {selected.kind === "material" && selected.task && <>
            {(() => { const task = selected.task!; return <>
              {task.resolution_note && <section className="rc-block"><h3>处理说明</h3><p>{task.resolution_note}</p></section>}
              {!readOnly && task.status === "open" && <div className="rc-actions">
                <button className="secondary-button small" disabled={busy === task.id} onClick={() => act(task.id, () => api.updateReviewTask(projectId, task, "waived", "人工判断无需继续核验", token))}>{busy === task.id ? busySpinner : <X size={14} />}豁免</button>
                <button className="primary-button small" disabled={busy === task.id} onClick={() => act(task.id, () => api.updateReviewTask(projectId, task, "resolved", "已取得材料并完成人工核验", token))}><Check size={14} />标记已解决</button>
              </div>}
            </>; })()}
          </>}
        </>}
      </section>
    </div>}

    {activeView === "notices" && <section className="rc-notices">
      <div className="rc-notice-row"><SourceBadge source="单一来源" tone="info" /><div><strong>{snapshot?.counts.single_source ?? 0} 项主体事实尚未独立验证</strong><p>这些事实保留来源状态，但不要求逐项人工确认，不计入待处理事项。</p></div></div>
      {snapshot?.counts.stale ? <div className="rc-notice-row"><SourceBadge source="接近或已经过期" tone="warning" /><div><strong>{snapshot.counts.stale} 项资料需要后续更新</strong><p>同一资料包只会生成一条待补资料事项。</p></div></div> : null}
      <div className="rc-notice-row"><SourceBadge source="口径说明" tone="neutral" /><div><strong>不同期间、合并与母公司口径不会自动判为冲突</strong><p>只有同主体、同期间、同口径的实质差异才进入事实冲突。</p></div></div>
    </section>}

    {activeView === "records" && <section className="rc-records">
      {!auditEvents.length ? <div className="rc-empty"><History size={18} />暂无处理记录</div> : <div className="rc-table-scroll"><table className="rc-table">
        <thead><tr><th>时间</th><th>操作人</th><th>处理动作</th><th>关联事项</th></tr></thead>
        <tbody>{auditEvents.slice(0, 200).map((event) => <tr key={event.id}>
          <td>{new Date(event.created_at).toLocaleString("zh-CN")}</td>
          <td>{actorLabels[event.actor] || "人工操作"}</td>
          <td>{actionLabels[event.action] || "复核处理"}</td>
          <td>{entityLabels[event.entity_type] || "相关事项"}{event.entity_id ? ` · ${String(event.entity_id).slice(0, 8)}` : ""}</td>
        </tr>)}</tbody>
      </table></div>}
    </section>}
  </div>;
}

function Risks({ risks, openCitation, onBack, targetRiskId }: { risks: Risk[]; openCitation: (id: string) => void; onBack: () => void; targetRiskId?: string }) {
  const [filter, setFilter] = useState<"all" | Severity>("all"); const [highlightedRiskId, setHighlightedRiskId] = useState(""); const filtered = filter === "all" ? risks : risks.filter((risk) => risk.severity === filter);
  const countAt = (likelihood: number, impact: number) => risks.filter((risk) => risk.likelihood === likelihood && risk.impact_score === impact).length;
  useEffect(() => {
    if (!targetRiskId) { setHighlightedRiskId(""); return; }
    setFilter("all"); setHighlightedRiskId(targetRiskId);
    const first = window.requestAnimationFrame(() => window.requestAnimationFrame(() => document.getElementById(`risk-card-${targetRiskId}`)?.scrollIntoView({ block: "center", behavior: "instant" as ScrollBehavior })));
    const timer = window.setTimeout(() => setHighlightedRiskId(""), 2600);
    return () => { window.cancelAnimationFrame(first); window.clearTimeout(timer); };
  }, [targetRiskId]);
  return <div className="view-content"><div className="risk-back-row"><button className="secondary-button small" onClick={onBack}><ArrowLeft size={14} />返回项目详情</button></div><div className="page-heading"><div><span className="eyebrow">风险审查与条件设计</span><h1>风险与担保方案</h1></div><div className="segmented">{(["all", "high", "medium", "low"] as const).map((item) => <button className={filter === item ? "active" : ""} key={item} onClick={() => setFilter(item)}>{item === "all" ? "全部" : severityText[item]}</button>)}</div></div>
    <div className="risk-workspace"><section className="panel matrix-panel"><div className="panel-title"><h2>3×3 风险矩阵</h2><span>可能性 × 影响</span></div><div className="risk-matrix"><span className="axis-y">影响</span>{[3, 2, 1].flatMap((impact) => [1, 2, 3].map((likelihood) => { const score = likelihood * impact; const level = score >= 6 ? "high" : score >= 3 ? "medium" : "low"; return <div className={`matrix-cell matrix-${level}`} key={`${likelihood}-${impact}`}><strong>{countAt(likelihood, impact)}</strong><small>{likelihood}×{impact}</small></div>; }))}<span className="axis-x">可能性 →</span></div></section><section className="panel conditions-panel"><div className="panel-title"><h2>风险缓释及担保条件</h2><ShieldCheck size={18} /></div><div>{risks.slice(0, 6).map((risk, index) => <div className="condition-row" key={risk.id}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{risk.title}</strong><p>{risk.condition_text || risk.action}</p></div></div>)}</div></section></div>
    <div className="risk-list">{filtered.length ? filtered.map((risk, index) => <article id={`risk-card-${risk.id}`} className={`risk-card risk-border-${risk.severity} ${highlightedRiskId === risk.id ? "risk-highlight" : ""}`} key={risk.id}><div className="risk-index">{String(index + 1).padStart(2, "0")}</div><div className="risk-body"><div className="risk-heading"><SeverityBadge level={risk.severity} /><span className="risk-category">{risk.category} · {risk.source_type === "candidate" ? "人工确认候选" : "确定性规则"}</span><h2>{risk.title}</h2><span className="risk-score">{risk.likelihood}×{risk.impact_score}</span></div><p>{risk.description}</p><div className="risk-details"><div><span>潜在影响</span><strong>{risk.impact}</strong></div><div><span>核验与缓释</span><strong>{risk.action}</strong></div></div><div className="evidence-links">{risk.source_ids.map((id) => <button onClick={() => openCitation(id)} key={id}><FileText size={14} />查看证据</button>)}</div></div></article>) : <div className="panel risk-empty">暂无风险记录</div>}</div></div>;
}

function clonePromptConfig(config: PromptEditableConfig): PromptEditableConfig {
  return {
    ...config,
    required_sections: [...config.required_sections],
    rules: [...config.rules],
    narrative_contract: { ...config.narrative_contract, paragraph_order: [...config.narrative_contract.paragraph_order] },
  };
}

/** 生成 UUID（章节/小节 ID 用）；老环境无 `crypto.randomUUID` 时退化为 v4 形态随机串。 */
function newOutlineUuid(): string {
  const cryptoRef = globalThis.crypto as { randomUUID?: () => string } | undefined;
  if (cryptoRef?.randomUUID) return cryptoRef.randomUUID();
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
    const value = Math.floor(Math.random() * 16);
    return (char === "x" ? value : (value & 0x3) | 0x8).toString(16);
  });
}

/** 自定义章节：`template_key=null`（ID 形态 `chapter-<uuid>`）、模型生成、Prompt 驱动检索。 */
function newOutlineChapter(): OutlineChapter {
  return { chapter_id: `chapter-${newOutlineUuid()}`, template_key: null, title: "新章节", order: 1, generation_mode: "generate", evidence_scope: "prompt_retrieval", subsections: [] };
}

/** 自定义小节：ID 形态 `sub-<uuid>`（前端拟、服务端保存时原样接受）。 */
function newOutlineSubsection(): OutlineSubsection {
  return { subsection_id: `sub-${newOutlineUuid()}`, title: "新小节", order: 1, prompt: "", enabled: true };
}

/** 深拷贝目录草稿：编辑只改草稿，不污染服务端返回的基准（用于并发比较）。 */
function cloneOutline(outline: ReportOutline): ReportOutline {
  return { ...outline, parts: outline.parts.map((part) => ({ ...part, chapters: part.chapters.map((chapter) => ({ ...chapter, subsections: chapter.subsections.map((subsection) => ({ ...subsection })) })) })) };
}

/** 把各级 `order` 重排为 1..n；服务端保存时也会重排，客户端保持一致便于比较与展示。 */
function normalizeOutline(outline: ReportOutline): ReportOutline {
  return {
    ...outline,
    parts: outline.parts.map((part, partIndex) => ({
      ...part,
      order: partIndex + 1,
      chapters: part.chapters.map((chapter, chapterIndex) => ({
        ...chapter,
        order: chapterIndex + 1,
        subsections: chapter.subsections.map((subsection, subsectionIndex) => ({ ...subsection, order: subsectionIndex + 1 })),
      })),
    })),
  };
}

function formatPromptUpdatedAt(value?: string | null): string {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function PromptStringListEditor({ label, hint, items, onChange, disabled, placeholder }: { label: string; hint: string; items: string[]; onChange: (next: string[]) => void; disabled: boolean; placeholder: string }) {
  const replace = (index: number, value: string) => onChange(items.map((item, itemIndex) => itemIndex === index ? value : item));
  const move = (index: number, direction: -1 | 1) => {
    const next = [...items]; const target = index + direction;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]]; onChange(next);
  };
  return <section className="prompt-field prompt-list-field"><div className="prompt-field-head"><div><label>{label}</label><small>{hint}</small></div><button className="icon-button" type="button" disabled={disabled} title={`新增${label}`} onClick={() => onChange([...items, ""])}><ListPlus size={16} /></button></div><div className="prompt-list-editor">{items.map((item, index) => <div className="prompt-list-row" key={`${label}-${index}`}><span>{String(index + 1).padStart(2, "0")}</span><input aria-label={`${label} ${index + 1}`} value={item} placeholder={placeholder} disabled={disabled} onChange={(event) => replace(index, event.target.value)} /><div><button className="icon-button compact" type="button" title="上移" disabled={disabled || index === 0} onClick={() => move(index, -1)}><ArrowUp size={14} /></button><button className="icon-button compact" type="button" title="下移" disabled={disabled || index === items.length - 1} onClick={() => move(index, 1)}><ArrowDown size={14} /></button><button className="icon-button compact danger" type="button" title="删除" disabled={disabled} onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={14} /></button></div></div>)}{!items.length && <p className="prompt-list-empty">尚未配置{label}</p>}</div></section>;
}

function PromptGenerationHelp({ llm }: { llm: boolean }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("keydown", close);
    return () => document.removeEventListener("keydown", close);
  }, [open]);
  return <span className="prompt-generation-help"><button type="button" className="prompt-help-button" aria-label="查看生成方式说明" aria-expanded={open} onClick={() => setOpen((value) => !value)}><CircleHelp size={15} /></button><span className={`prompt-help-tooltip ${open ? "open" : ""}`} role="tooltip">{llm ? "模型根据确定性草稿进行受控改写" : "由固定规则、任务和证据模板生成，不调用模型改写"}</span></span>;
}

function PromptConstraintGroup({ title, values }: { title: string; values: string[] }) {
  if (!values.length) return null;
  return <div className="prompt-constraint-group"><strong>{title}</strong><div>{values.map((value) => <span key={value}>{value}</span>)}</div></div>;
}

function PromptWorkspace({ project, token }: { project: Project; token?: string }) {
  const [prompts, setPrompts] = useState<ReportPrompt[]>([]);
  // 草稿按「范围:章节」双键缓存：切换范围或章节再切回来，未保存的编辑都在
  // （P0-A 要求"两种编辑范围分别缓存草稿，切换章节不丢未保存内容"）。
  const [drafts, setDrafts] = useState<Record<string, PromptEditableConfig>>({});
  const [scope, setScope] = useState<"project" | "global">("project");
  const [activeChapterId, setActiveChapterId] = useState("");
  const [tab, setTab] = useState<"structured" | "preview">("structured");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState("");
  const [message, setMessage] = useState("");
  const [loadError, setLoadError] = useState("");
  const [previewPrompt, setPreviewPrompt] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [constraintModalOpen, setConstraintModalOpen] = useState(false);
  // 报告目录：基准状态与整份草稿都按范围缓存（切换章节/范围都不丢未保存的目录编辑）。
  const [outlineStates, setOutlineStates] = useState<Record<string, ReportOutlineState>>({});
  const [outlineDrafts, setOutlineDrafts] = useState<Record<string, ReportOutline>>({});
  const [outlineLoading, setOutlineLoading] = useState(true);
  const [outlineError, setOutlineError] = useState("");
  const [savingOutline, setSavingOutline] = useState(false);
  const [renameChapterId, setRenameChapterId] = useState("");
  const [renameValue, setRenameValue] = useState("");

  // 当前范围的目录基准与草稿（均按范围缓存，切换范围互不影响）。
  const outlineDraft = outlineDrafts[scope];
  const outlineState = outlineStates[scope];

  const draftKey = useCallback((chapterKey: string) => `${scope}:${chapterKey}`, [scope]);

  const load = useCallback(async () => {
    setLoading(true); setOutlineLoading(true);
    setLoadError(""); setOutlineError("");
    // 目录加载失败只影响目录区、不崩整页：两块并行拉取，各自落到自己的错误位。
    const [promptResult, outlineResult] = await Promise.allSettled([
      api.reportPrompts(project.id, token, scope),
      api.reportOutline(project.id, token, scope),
    ]);
    if (promptResult.status === "fulfilled") {
      const rows = promptResult.value;
      setPrompts(rows);
      // **保留已有草稿**：同一个范围内已经编辑过、尚未保存的内容不能被这次加载覆盖。
      // 首次进入该范围时没有草稿，才用服务端返回的配置填充。
      setDrafts((current) => Object.fromEntries(rows.map((item) => {
        const key = `${scope}:${item.chapter_key}`;
        return [key, current[key] ?? clonePromptConfig(item.editable_config)];
      })));
    } else {
      setPrompts([]);
      setLoadError(promptResult.reason instanceof Error ? promptResult.reason.message : "无法读取报告 Prompt");
    }
    if (outlineResult.status === "fulfilled") {
      const state = outlineResult.value;
      setOutlineStates((current) => ({ ...current, [scope]: state }));
      // 同样保留已有草稿：未保存的目录编辑不被这次加载覆盖。
      setOutlineDrafts((current) => current[scope] ? current : { ...current, [scope]: cloneOutline(state.outline) });
    } else {
      setOutlineError(outlineResult.reason instanceof Error ? outlineResult.reason.message : "无法读取报告目录");
    }
    setLoading(false); setOutlineLoading(false);
  }, [project.id, token, scope]);

  useEffect(() => { load(); }, [load]);

  // 目录保存/重置后**只重读章节配置**（P1-2）：目录变了，章节集合与自定义章节的
  // 键都会变，Prompt 列表必须跟着刷新；已编辑未保存的草稿按 key 保留，不被这次
  // 重读覆盖（新增章节没有草稿，用服务端返回的配置填充）。
  const reloadPrompts = useCallback(async () => {
    try {
      const rows = await api.reportPrompts(project.id, token, scope);
      setPrompts(rows);
      setDrafts((current) => Object.fromEntries(rows.map((item) => {
        const key = `${scope}:${item.chapter_key}`;
        return [key, current[key] ?? clonePromptConfig(item.editable_config)];
      })));
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : "无法读取报告 Prompt");
    }
  }, [project.id, token, scope]);

  const chapterGroups = useMemo(() => {
    const groups: { part: string; items: { item: ReportPrompt; index: number }[] }[] = [];
    prompts.forEach((item) => {
      const part = item.part || "报告章节";
      const last = groups[groups.length - 1];
      if (last && last.part === part) last.items.push({ item, index: last.items.length });
      else groups.push({ part, items: [{ item, index: 0 }] });
    });
    return groups;
  }, [prompts]);

  type NavChapter = { chapterId: string; templateKey: string | null; title: string; generationMode: "generate" | "template"; index: number; outlineDirty: boolean };
  type NavPart = { partId: string; partTitle: string; chapters: NavChapter[] };

  // 左侧导航优先用**目录草稿**渲染（分部 → 二级章节）；目录接口不可用时回落到 Prompt 分组，
  // 这样目录加载失败也不会让整页失去章级 Prompt 编辑能力。
  const navParts = useMemo<NavPart[]>(() => {
    if (outlineDraft) {
      const baseline = outlineState?.outline;
      return outlineDraft.parts.map((part) => ({
        partId: part.part_id,
        partTitle: part.title,
        chapters: part.chapters.map((chapter, chapterIndex) => {
          const baseChapter = baseline?.parts.find((item) => item.part_id === part.part_id)?.chapters.find((item) => item.chapter_id === chapter.chapter_id);
          return {
            chapterId: chapter.chapter_id,
            templateKey: chapter.template_key,
            title: chapter.title,
            generationMode: chapter.generation_mode,
            index: chapterIndex + 1,
            outlineDirty: baseChapter ? JSON.stringify(chapter) !== JSON.stringify(baseChapter) : false,
          };
        }),
      }));
    }
    return chapterGroups.map((group) => ({
      partId: group.part,
      partTitle: group.part,
      chapters: group.items.map(({ item, index }) => ({
        chapterId: `chapter-${item.chapter_key}`,
        templateKey: item.chapter_key,
        title: item.title,
        generationMode: item.llm ? "generate" : "template",
        index: index + 1,
        outlineDirty: false,
      })),
    }));
  }, [outlineDraft, outlineState, chapterGroups]);

  const navChapters = useMemo(() => navParts.flatMap((part) => part.chapters), [navParts]);
  const activeNavChapter = navChapters.find((chapter) => chapter.chapterId === activeChapterId) || null;
  const activeOutlineChapter = outlineDraft
    ? outlineDraft.parts.flatMap((part) => part.chapters).find((chapter) => chapter.chapter_id === activeChapterId) || null
    : null;
  // 章节查找统一走 templateKey || chapterId：内置章节按 template_key 命中，
  // 自定义章节（template_key=null）按目录 chapter_id 命中后端返回的同构条目。
  const active = prompts.find((item) => item.chapter_key === (activeNavChapter?.templateKey || activeNavChapter?.chapterId));
  const activeDraft = active ? (drafts[draftKey(active.chapter_key)] || active.editable_config) : null;

  // 默认选中：与旧行为一致——优先第一个「模型生成」章节，否则第一个章节。
  // 当前选中项仍存在时保留，切换范围/编辑目录都不会无故跳章。
  useEffect(() => {
    if (!navChapters.length) return;
    setActiveChapterId((current) => navChapters.some((chapter) => chapter.chapterId === current)
      ? current
      : (navChapters.find((chapter) => chapter.templateKey && prompts.find((item) => item.chapter_key === chapter.templateKey)?.llm) || navChapters[0]).chapterId);
  }, [navChapters, prompts]);

  // 编辑权限以**后端返回的权限字段**为准：范围不同判据不同——项目级看 `editable`，
  // 全局级看 `global_editable`（全局是部署级配置，只要求非公开 + 本机直连 + 无转发头）。
  // 权限字段缺失（旧响应）时才退回 data_class 判断。
  const scopeEditable = scope === "global" ? active?.global_editable : active?.editable;
  const readOnly = scopeEditable === undefined
    ? project.data_class === "real_reference"
    : scopeEditable === false;
  const scopeEditReason = scope === "global" ? active?.global_edit_reason : active?.edit_reason;
  // 目录权限与 Prompt 同一口径：项目级看 editable、全局级看 global_editable。
  const outlineScopeEditable = scope === "global" ? outlineState?.global_editable : outlineState?.editable;
  const outlineReadOnly = outlineScopeEditable === undefined ? project.data_class === "real_reference" : outlineScopeEditable === false;
  const outlineEditReason = (scope === "global" ? outlineState?.global_edit_reason : outlineState?.edit_reason) || "";
  const outlineDirty = Boolean(outlineDraft && outlineState && JSON.stringify(outlineDraft) !== JSON.stringify(outlineState.outline));
  const totalChapters = outlineDraft ? outlineDraft.parts.reduce((sum, part) => sum + part.chapters.length, 0) : 0;
  const previewSignature = activeDraft ? JSON.stringify(activeDraft) : "";
  useEffect(() => {
    if (tab !== "preview" || !active || !active.llm || !activeDraft) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setPreviewLoading(true); setPreviewError("");
      api.previewReportPrompt(project.id, active.chapter_key, activeDraft, token)
        .then((result) => { if (!cancelled) setPreviewPrompt(result.prompt); })
        .catch((reason) => { if (!cancelled) setPreviewError(reason instanceof Error ? reason.message : "无法生成完整 Prompt 预览"); })
        .finally(() => { if (!cancelled) setPreviewLoading(false); });
    }, 320);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [active?.chapter_key, previewSignature, project.id, tab, token]);

  useEffect(() => {
    if (!constraintModalOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setConstraintModalOpen(false);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [constraintModalOpen]);

  const updateRow = (chapterKey: string, values: Partial<ReportPrompt>) => setPrompts((current) => current.map((item) => item.chapter_key === chapterKey ? { ...item, ...values } : item));
  type PromptDraftPatch = Omit<Partial<PromptEditableConfig>, "narrative_contract"> & { narrative_contract?: Partial<PromptEditableConfig["narrative_contract"]> };
  const updateDraft = (chapterKey: string, value: PromptDraftPatch) => setDrafts((current) => {
    const key = draftKey(chapterKey);
    const base = current[key] || prompts.find((item) => item.chapter_key === chapterKey)?.editable_config;
    if (!base) return current;
    return { ...current, [key]: { ...base, ...value, narrative_contract: value.narrative_contract ? { ...base.narrative_contract, ...value.narrative_contract } : base.narrative_contract } };
  });
  const sourceText = (item: ReportPrompt) => item.source === "project" ? "项目自定义" : item.source === "global" ? "全局默认" : "系统默认";
  const canEdit = Boolean(active?.llm && !readOnly);
  const isChanged = Boolean(active && activeDraft && JSON.stringify(activeDraft) !== JSON.stringify(active.editable_config));
  const canSetGlobalDefault = Boolean(active?.llm && !readOnly && saving !== active.chapter_key && activeDraft);
  const effectiveUpdatedAt = active?.overridden ? active.updated_at : active?.global_overridden ? active.global_updated_at : null;
  // 并发基准随范围取：项目级用 `config_sha256`，全局级用 `global_config_sha256`；
  // 两者都带 `prompt_contract_version`（编译器升级后同一份配置会渲染出不同文本）。
  // 该范围尚无覆盖时哈希为空串，`_concurrencyBody/_concurrencyQuery` 会整体省略该字段，
  // 避免"预期为空"被后端当成冲突。
  const concurrencyBasis = (): PromptConcurrency => ({
    configSha256: scope === "global" ? active?.global_config_sha256 : active?.config_sha256,
    promptContractVersion: active?.prompt_contract_version,
  });
  const concurrencyHint = (reason: unknown) => {
    const text = reason instanceof Error ? reason.message : "";
    // 409 = 别的会话改过配置，或编译器版本变了 → 明确让用户重读，不自动重试。
    return /409|已被其他会话修改|编译器版本/.test(text) ? `${text}（请重新加载后再保存）` : text;
  };

  const save = async () => {
    if (!active || !activeDraft || !canEdit) return;
    setSaving(active.chapter_key); setMessage("");
    try {
      const basis = concurrencyBasis();
      const saved = scope === "global"
        ? await api.setGlobalReportPrompt(project.id, active.chapter_key, activeDraft, token, basis)
        : await api.saveReportPrompt(project.id, active.chapter_key, activeDraft, token, basis);
      updateRow(active.chapter_key, saved);
      setDrafts((current) => ({ ...current, [draftKey(active.chapter_key)]: clonePromptConfig(saved.editable_config) }));
      setPreviewPrompt(saved.prompt);
      setMessage(scope === "global"
        ? `已保存“${active.title}”的全局默认配置；有项目覆盖的项目仍优先使用项目配置（保存不会自动调用模型）。现有报告不变，需要时在项目内重新生成该章。`
        : `已保存“${active.title}”的项目级配置；对应章节将在报告中标记为待再生（保存不会自动调用模型），需要时在该章重新生成。`);
    } catch (reason) { setLoadError(concurrencyHint(reason) || "保存 Prompt 失败"); }
    finally { setSaving(""); }
  };
  const reset = async () => {
    if (!active) return;
    setSaving(active.chapter_key); setMessage("");
    try {
      const basis = concurrencyBasis();
      const resetRow = scope === "global"
        ? await api.resetGlobalReportPrompt(project.id, active.chapter_key, token, basis)
        : await api.resetReportPrompt(project.id, active.chapter_key, token, basis);
      updateRow(active.chapter_key, resetRow);
      setDrafts((current) => ({ ...current, [draftKey(active.chapter_key)]: clonePromptConfig(resetRow.editable_config) }));
      setPreviewPrompt(resetRow.prompt);
      setMessage(scope === "global"
        ? `“${active.title}”的全局默认已撤销，回落系统出厂配置；已有项目覆盖不受影响。`
        : `“${active.title}”已恢复为${resetRow.global_overridden ? "全局默认" : "系统默认"}配置。`);
    } catch (reason) { setLoadError(concurrencyHint(reason) || "恢复默认配置失败"); }
    finally { setSaving(""); }
  };
  // "设为全局默认"是范围切换的快捷方式：把当前草稿写进全局链（项目覆盖不动）。
  const setGlobalDefault = async () => {
    if (!active || !activeDraft || !canEdit) return;
    setSaving(active.chapter_key); setMessage("");
    try {
      const globalRow = await api.setGlobalReportPrompt(project.id, active.chapter_key, activeDraft, token, {
        configSha256: active.global_config_sha256,
        promptContractVersion: active.prompt_contract_version,
      });
      updateRow(active.chapter_key, globalRow);
      if (!active.overridden) setDrafts((current) => ({ ...current, [`global:${active.chapter_key}`]: clonePromptConfig(globalRow.editable_config) }));
      setPreviewPrompt(globalRow.prompt);
      setMessage(`“${active.title}”已${active.global_overridden ? "更新" : "设为"}全局默认配置。`);
    } catch (reason) { setLoadError(concurrencyHint(reason) || "设置全局默认失败"); }
    finally { setSaving(""); }
  };
  const resetGlobalDefault = async () => {
    if (!active) return;
    setSaving(active.chapter_key); setMessage("");
    try {
      const row = await api.resetGlobalReportPrompt(project.id, active.chapter_key, token, {
        configSha256: active.global_config_sha256,
        promptContractVersion: active.prompt_contract_version,
      });
      updateRow(active.chapter_key, row);
      if (!active.overridden) setDrafts((current) => ({ ...current, [`global:${active.chapter_key}`]: clonePromptConfig(row.editable_config) }));
      setPreviewPrompt(row.prompt);
      setMessage(`“${active.title}”的全局默认已撤销。`);
    } catch (reason) { setLoadError(concurrencyHint(reason) || "撤销全局默认失败"); }
    finally { setSaving(""); }
  };

  const constraints: PromptConstraints = active?.constraints || { required_inputs: [], input_contract: {}, preserve_blocks: [], forbidden: [], output_contract: {}, global_rules: {} };
  // 篇幅口径由后端计算（length_policy），前端只做"最小值不大于最大值"的即时提醒；
  // 系统已无字符硬上限，不再有"越硬顶"这种前端判断。
  const draftPreferredMax = activeDraft?.narrative_contract.preferred_max_chars ?? 0;
  const draftPreferredMin = activeDraft?.narrative_contract.preferred_min_chars ?? 0;
  const activeChapterNumber = chapterGroups.flatMap((group) => group.items).find(({ item }) => item.chapter_key === active?.chapter_key)?.index ?? 0;

  // ------- 报告目录（report-outline）编辑 -------
  // 所有目录编辑只改当前范围的草稿；保存成功后用服务端响应整体替换草稿与基准。
  const mutateOutline = (mutator: (outline: ReportOutline) => ReportOutline) => {
    setOutlineDrafts((current) => {
      const base = current[scope];
      if (!base) return current;
      return { ...current, [scope]: normalizeOutline(mutator(base)) };
    });
  };
  const patchChapter = (chapterId: string, patch: Partial<OutlineChapter>) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => ({ ...part, chapters: part.chapters.map((chapter) => chapter.chapter_id === chapterId ? { ...chapter, ...patch } : chapter) })) }));
  const moveChapter = (partId: string, chapterId: string, direction: -1 | 1) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => { if (part.part_id !== partId) return part; const index = part.chapters.findIndex((chapter) => chapter.chapter_id === chapterId); const target = index + direction; if (index < 0 || target < 0 || target >= part.chapters.length) return part; const chapters = [...part.chapters]; [chapters[index], chapters[target]] = [chapters[target], chapters[index]]; return { ...part, chapters }; }) }));
  const removeChapter = (chapterId: string) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => ({ ...part, chapters: part.chapters.filter((chapter) => chapter.chapter_id !== chapterId) })) }));
  const addChapter = (partId: string) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => part.part_id === partId ? { ...part, chapters: [...part.chapters, newOutlineChapter()] } : part) }));
  const patchSubsection = (chapterId: string, subsectionId: string, patch: Partial<OutlineSubsection>) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => ({ ...part, chapters: part.chapters.map((chapter) => chapter.chapter_id === chapterId ? { ...chapter, subsections: chapter.subsections.map((subsection) => subsection.subsection_id === subsectionId ? { ...subsection, ...patch } : subsection) } : chapter) })) }));
  const moveSubsection = (chapterId: string, subsectionId: string, direction: -1 | 1) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => ({ ...part, chapters: part.chapters.map((chapter) => { if (chapter.chapter_id !== chapterId) return chapter; const index = chapter.subsections.findIndex((subsection) => subsection.subsection_id === subsectionId); const target = index + direction; if (index < 0 || target < 0 || target >= chapter.subsections.length) return chapter; const subsections = [...chapter.subsections]; [subsections[index], subsections[target]] = [subsections[target], subsections[index]]; return { ...chapter, subsections }; }) })) }));
  const removeSubsection = (chapterId: string, subsectionId: string) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => ({ ...part, chapters: part.chapters.map((chapter) => chapter.chapter_id === chapterId ? { ...chapter, subsections: chapter.subsections.filter((subsection) => subsection.subsection_id !== subsectionId) } : chapter) })) }));
  const addSubsection = (chapterId: string) => mutateOutline((outline) => ({ ...outline, parts: outline.parts.map((part) => ({ ...part, chapters: part.chapters.map((chapter) => chapter.chapter_id === chapterId ? { ...chapter, subsections: [...chapter.subsections, newOutlineSubsection()] } : chapter) })) }));
  const beginRename = (chapter: NavChapter) => { setRenameChapterId(chapter.chapterId); setRenameValue(chapter.title); };
  const commitRename = () => {
    const title = renameValue.replace(/[\r\n]+/g, " ").trim();
    if (renameChapterId && title) patchChapter(renameChapterId, { title });
    setRenameChapterId(""); setRenameValue("");
  };
  const confirmRemoveChapter = (chapter: NavChapter) => {
    if (!window.confirm(`删除章节「${chapter.title}」？该章节及其小节会从目录中移除（保存后生效）。`)) return;
    if (activeChapterId === chapter.chapterId) setActiveChapterId("");
    removeChapter(chapter.chapterId);
  };
  const confirmRemoveSubsection = (chapter: OutlineChapter, subsection: OutlineSubsection) => {
    if (!window.confirm(`删除小节「${subsection.title}」？保存后生效。`)) return;
    removeSubsection(chapter.chapter_id, subsection.subsection_id);
  };
  const saveOutline = async () => {
    if (!outlineDraft || !outlineState || outlineReadOnly || savingOutline) return;
    setSavingOutline(true); setMessage(""); setOutlineError("");
    try {
      const saved = await api.saveReportOutline(project.id, token, scope, outlineDraft, {
        expectedOutlineVersion: outlineState.outline_version,
        expectedOutlineSha256: outlineState.config_sha256,
      });
      setOutlineStates((current) => ({ ...current, [scope]: saved }));
      setOutlineDrafts((current) => ({ ...current, [scope]: cloneOutline(saved.outline) }));
      await reloadPrompts();
      setMessage(scope === "global"
        ? "已保存全局默认报告目录；对所有未单独覆盖的项目生效（保存不会自动再生报告）。"
        : "已保存项目报告目录；相关报告将标记为待再生（保存不会自动再生报告）。");
    } catch (reason) { setOutlineError(concurrencyHint(reason) || "保存目录失败"); }
    finally { setSavingOutline(false); }
  };
  const resetOutline = async () => {
    if (!outlineState || savingOutline) return;
    if (!window.confirm(scope === "global" ? "恢复默认目录将撤销本范围的目录自定义并回落内置目录，确定继续？" : "恢复默认目录将撤销本项目目录覆盖并回落全局/内置目录，确定继续？")) return;
    setSavingOutline(true); setMessage(""); setOutlineError("");
    try {
      const state = await api.resetReportOutline(project.id, token, scope, {
        expectedOutlineVersion: outlineState.outline_version,
        expectedOutlineSha256: outlineState.config_sha256,
      });
      setOutlineStates((current) => ({ ...current, [scope]: state }));
      setOutlineDrafts((current) => ({ ...current, [scope]: cloneOutline(state.outline) }));
      await reloadPrompts();
      setMessage("已恢复默认报告目录。");
    } catch (reason) { setOutlineError(concurrencyHint(reason) || "恢复默认目录失败"); }
    finally { setSavingOutline(false); }
  };

  // 范围切换对**目录与 Prompt 两块同时生效**（同一 scope 状态）。
  const scopeSwitch = <div className="prompt-scope-bar">
    <div className="prompt-scope-switch" role="group" aria-label="编辑范围">
      <button type="button" className={scope === "project" ? "active" : ""} aria-pressed={scope === "project"} onClick={() => setScope("project")}>项目配置</button>
      <button type="button" className={scope === "global" ? "active" : ""} aria-pressed={scope === "global"} onClick={() => setScope("global")}>全局默认</button>
    </div>
    <span className="prompt-scope-note">{scope === "project"
      ? "只影响当前项目；已有项目覆盖时优先于全局默认。"
      : "对所有未单独覆盖的项目生效。"}</span>
  </div>;

  const subsectionEditor = activeOutlineChapter ? <section className="outline-subsection-editor">
    <div className="outline-subsection-head">
      <div className="outline-subsection-head-copy"><strong>三级小节</strong><HelpTip label="三级小节说明">小节用于拆分本章的写作顺序与要求，保存后写入报告目录。留空的 Prompt 沿用本章要求；取消勾选则暂不参与生成。</HelpTip></div>
      <span className="outline-subsection-count">{activeOutlineChapter.subsections.length} 个小节</span>
    </div>
    {activeOutlineChapter.generation_mode === "template" && <div className="outline-subsection-note"><Info size={15} /><span>本章由固定规则确定性生成，本小节暂不参与模型生成；标题仍可编辑，Prompt 暂不生效。</span></div>}
    <div className="outline-subsection-list">
      {activeOutlineChapter.subsections.map((subsection, index) => <div className="outline-subsection-card" key={subsection.subsection_id}>
        <div className="outline-subsection-row">
          <span className="outline-subsection-index">{String(index + 1).padStart(2, "0")}</span>
          <input className="outline-subsection-title" aria-label={`小节 ${index + 1} 标题`} value={subsection.title} maxLength={60} disabled={outlineReadOnly} placeholder="小节标题" onChange={(event) => patchSubsection(activeOutlineChapter.chapter_id, subsection.subsection_id, { title: event.target.value.replace(/[\r\n]+/g, " ") })} />
          <label className="outline-subsection-toggle" title="取消勾选后该小节暂不参与生成"><input type="checkbox" checked={subsection.enabled} disabled={outlineReadOnly} onChange={(event) => patchSubsection(activeOutlineChapter.chapter_id, subsection.subsection_id, { enabled: event.target.checked })} />参与生成</label>
          {!outlineReadOnly && <div className="outline-subsection-actions"><button className="icon-button compact" type="button" aria-label="上移小节" title="上移" disabled={index === 0} onClick={() => moveSubsection(activeOutlineChapter.chapter_id, subsection.subsection_id, -1)}><ArrowUp size={14} /></button><button className="icon-button compact" type="button" aria-label="下移小节" title="下移" disabled={index === activeOutlineChapter.subsections.length - 1} onClick={() => moveSubsection(activeOutlineChapter.chapter_id, subsection.subsection_id, 1)}><ArrowDown size={14} /></button><button className="icon-button compact danger" type="button" aria-label="删除小节" title="删除" onClick={() => confirmRemoveSubsection(activeOutlineChapter, subsection)}><Trash2 size={14} /></button></div>}
        </div>
        <textarea className="outline-subsection-prompt" aria-label={`小节 ${index + 1} Prompt`} rows={2} value={subsection.prompt} disabled={outlineReadOnly || activeOutlineChapter.generation_mode === "template"} placeholder="补充该小节的写作要求；留空沿用本章要求。" onChange={(event) => patchSubsection(activeOutlineChapter.chapter_id, subsection.subsection_id, { prompt: event.target.value })} />
      </div>)}
      {!activeOutlineChapter.subsections.length && <p className="outline-subsection-empty">本章暂无小节，可新增小节来拆分写作顺序。</p>}
    </div>
    {!outlineReadOnly && <button className="secondary-button small outline-subsection-add" type="button" disabled={activeOutlineChapter.subsections.length >= 30} onClick={() => addSubsection(activeOutlineChapter.chapter_id)}><ListPlus size={14} />新增小节</button>}
    {outlineReadOnly && <p className="outline-subsection-readonly">{outlineEditReason || "当前环境不允许修改报告目录。"}</p>}
  </section> : null;

  const outlineActionBar = outlineDraft ? <footer className="outline-action-bar">
    <span>{outlineReadOnly ? (outlineEditReason || "当前环境不允许修改报告目录。") : outlineDirty ? "目录存在未保存的修改" : "目录与已保存版本一致"}</span>
    <div>
      <button className="text-button" type="button" disabled={outlineReadOnly || savingOutline} onClick={resetOutline}>恢复默认目录</button>
      <button className="primary-button" type="button" disabled={!outlineDirty || outlineReadOnly || savingOutline} onClick={saveOutline}>{savingOutline ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}保存目录</button>
    </div>
  </footer> : null;
  return <div className="view-content prompt-workspace"><div className="page-heading"><div><span className="eyebrow">报告交付配置</span><div className="page-title-row"><h1>报告 Prompt</h1><HelpTip label="报告 Prompt 页面说明">业务写作要求可编辑；证据范围、引用约束和禁止事项固定由系统执行，不随章节配置改变。</HelpTip></div></div><div className="prompt-summary"><FileCog size={16} /><span>{prompts.filter((item) => item.overridden).length} 个项目自定义 · {prompts.filter((item) => item.global_overridden).length} 个全局默认</span></div></div>
    {message && <div className="inline-success prompt-feedback"><Check size={15} />{message}</div>}
    {loadError && <div className="inline-error prompt-feedback"><AlertTriangle size={15} />{loadError}<button className="icon-button" onClick={() => setLoadError("")} title="关闭"><X size={14} /></button></div>}
    {outlineError && <div className="inline-error prompt-feedback"><AlertTriangle size={15} />{outlineError}<button className="icon-button" onClick={() => setOutlineError("")} title="关闭"><X size={14} /></button></div>}
    {loading || !navChapters.length ? <div className="loading-page prompt-loading"><LoaderCircle className="spin" size={24} />正在载入章节 Prompt</div> : <section className="prompt-workbench">
      <aside className="prompt-chapter-nav" aria-label="报告章节"><div className="prompt-chapter-nav-head"><strong>报告章节</strong><span>{navChapters.length}</span></div>{navParts.map((part) => <div className="prompt-part-group" key={part.partId}><div className="prompt-part-label">{part.partTitle}</div>{part.chapters.map((chapter) => { const promptItem = prompts.find((item) => item.chapter_key === (chapter.templateKey || chapter.chapterId)); const dirty = chapter.outlineDirty || (promptItem ? JSON.stringify(drafts[`${scope}:${promptItem.chapter_key}`] || promptItem.editable_config) !== JSON.stringify(promptItem.editable_config) : false); const canEditChapter = Boolean(outlineDraft && !outlineReadOnly); return <div className="outline-nav-row" key={chapter.chapterId}>{renameChapterId === chapter.chapterId ? <div className="outline-nav-rename"><input value={renameValue} autoFocus maxLength={60} aria-label="章节名称" onChange={(event) => setRenameValue(event.target.value.replace(/[\r\n]+/g, " "))} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); commitRename(); } else if (event.key === "Escape") { setRenameChapterId(""); } }} /><button className="icon-button compact" type="button" aria-label="确认改名" title="确认" onClick={commitRename}><Check size={14} /></button><button className="icon-button compact" type="button" aria-label="取消改名" title="取消" onClick={() => setRenameChapterId("")}><X size={14} /></button></div> : <><button className={`prompt-chapter-option ${chapter.chapterId === activeChapterId ? "active" : ""}`} aria-current={chapter.chapterId === activeChapterId ? "page" : undefined} onClick={() => { setActiveChapterId(chapter.chapterId); setTab("structured"); }}><span className="prompt-index">{chapter.index}、</span><span><strong>{chapter.title}</strong></span>{dirty && <i title="存在未保存修改" />}</button>{canEditChapter && <div className="outline-nav-actions"><button className="icon-button compact" type="button" aria-label="重命名章节" title="重命名" onClick={() => beginRename(chapter)}><Pencil size={13} /></button><button className="icon-button compact" type="button" aria-label="上移章节" title="上移" disabled={chapter.index <= 1} onClick={() => moveChapter(part.partId, chapter.chapterId, -1)}><ArrowUp size={13} /></button><button className="icon-button compact" type="button" aria-label="下移章节" title="下移" disabled={chapter.index >= part.chapters.length} onClick={() => moveChapter(part.partId, chapter.chapterId, 1)}><ArrowDown size={13} /></button><button className="icon-button compact danger" type="button" aria-label="删除章节" title="删除" onClick={() => confirmRemoveChapter(chapter)}><Trash2 size={13} /></button></div>}</>}</div>; })}{outlineDraft && !outlineReadOnly && <button className="outline-add-chapter" type="button" disabled={totalChapters >= 60} onClick={() => addChapter(part.partId)}><FolderPlus size={14} />新增章节</button>}</div>)}</aside>
      <main className="prompt-detail"><div className="prompt-mobile-picker"><label htmlFor="prompt-chapter-picker">报告章节</label><select id="prompt-chapter-picker" value={activeChapterId} onChange={(event) => { setActiveChapterId(event.target.value); setTab("structured"); }}>{navParts.map((part) => <optgroup label={part.partTitle} key={part.partId}>{part.chapters.map((chapter) => <option value={chapter.chapterId} key={chapter.chapterId}>{chapter.index}、{chapter.title}</option>)}</optgroup>)}</select></div>
        {active && activeDraft ? <>
        <header className="prompt-detail-head"><div><span>{active.part || "报告章节"} · 第 {activeChapterNumber + 1} 章</span><h2>{active.title}</h2></div><div className="prompt-detail-badges"><span className={active.llm ? "llm" : "deterministic"}>{active.llm ? "模型生成" : "确定性生成"}</span><PromptGenerationHelp llm={active.llm} />{!activeNavChapter?.templateKey && <span className="custom">自定义章节</span>}<span className={active.source === "system" ? "default" : "custom"}>{sourceText(active)}</span>{active.migration_mode !== "structured" && <span className="legacy">历史配置已迁移</span>}</div></header>
        {scopeSwitch}
        {scope === "global" && active.overridden && <div className="prompt-scope-warning"><AlertTriangle size={15} /><span>当前项目已为本章设置项目配置，<strong>仍优先使用项目配置</strong>；本次改动只影响其他未覆盖的项目。</span></div>}
        {scope === "global" && !active.overridden && active.effective_source === "global" && <div className="prompt-scope-note prompt-scope-effective">当前项目尚未覆盖本章，正在生效的就是这套全局默认。</div>}
        <div className="prompt-tabs" role="tablist"><button className={tab === "structured" ? "active" : ""} role="tab" aria-selected={tab === "structured"} onClick={() => setTab("structured")}><SlidersHorizontal size={16} />结构化配置</button><button className={tab === "preview" ? "active" : ""} role="tab" aria-selected={tab === "preview"} onClick={() => setTab("preview")}><Eye size={16} />完整 Prompt</button></div>
        {tab === "structured" ? <div className="prompt-structured"><fieldset disabled={!canEdit}>
          <section className="prompt-card"><div className="prompt-card-head"><strong>写作视角</strong></div><div className="prompt-field-grid"><section className="prompt-field"><label htmlFor="prompt-role">角色</label><small>定义本章的业务写作视角</small><input id="prompt-role" value={activeDraft.role} onChange={(event) => updateDraft(active.chapter_key, { role: event.target.value })} /></section><section className="prompt-field"><label htmlFor="prompt-tone">行文语气</label><small>在正式尽调语言范围内调整表达</small><input id="prompt-tone" value={activeDraft.narrative_contract.tone} onChange={(event) => updateDraft(active.chapter_key, { narrative_contract: { tone: event.target.value } })} /></section></div></section>
          <section className="prompt-card"><div className="prompt-card-head"><label htmlFor="prompt-goal">章节目标</label><small>说明本章需要回答的业务问题</small></div><textarea id="prompt-goal" rows={3} value={activeDraft.goal} onChange={(event) => updateDraft(active.chapter_key, { goal: event.target.value })} /></section>
          <details className="prompt-card prompt-collapsible"><summary><div><strong>内容要求</strong></div><span className="prompt-card-brief">必需 {activeDraft.required_sections.length} 项 · 规则 {activeDraft.rules.length} 项</span><ChevronDown size={15} /></summary><div className="prompt-card-body"><PromptStringListEditor label="必需报告内容" hint="生成内容必须覆盖这些业务区块" items={activeDraft.required_sections} disabled={!canEdit} placeholder="例如：偿债来源" onChange={(required_sections) => updateDraft(active.chapter_key, { required_sections })} /><PromptStringListEditor label="写作规则" hint="约束本章的表述方式和分析重点" items={activeDraft.rules} disabled={!canEdit} placeholder="例如：明确披露口径与待核验事项" onChange={(rules) => updateDraft(active.chapter_key, { rules })} /></div></details>
          <details className="prompt-card prompt-collapsible"><summary><div><strong>行文组织</strong></div><span className="prompt-card-brief">顺序 {activeDraft.narrative_contract.paragraph_order.length} 项</span><ChevronDown size={15} /></summary><div className="prompt-card-body"><PromptStringListEditor label="行文顺序" hint="按顺序组织自然段" items={activeDraft.narrative_contract.paragraph_order} disabled={!canEdit} placeholder="例如：财务概览" onChange={(paragraph_order) => updateDraft(active.chapter_key, { narrative_contract: { paragraph_order } })} /><section className="prompt-field"><div className="prompt-field-head"><div><label>篇幅</label><small>控制分析正文长度，不改变证据、引用与结论边界</small></div></div><div className="prompt-narrative-grid"><label>目标最小字符<input type="number" min="0" value={activeDraft.narrative_contract.preferred_min_chars} onChange={(event) => updateDraft(active.chapter_key, { narrative_contract: { preferred_min_chars: Number(event.target.value) } })} /></label><label>目标最大字符<input type="number" min="200" value={activeDraft.narrative_contract.preferred_max_chars} onChange={(event) => updateDraft(active.chapter_key, { narrative_contract: { preferred_max_chars: Number(event.target.value) } })} /></label><label>列表使用<select value={activeDraft.narrative_contract.list_mode} onChange={(event) => updateDraft(active.chapter_key, { narrative_contract: { list_mode: event.target.value } })}><option value="allowed">允许</option><option value="limited">仅限必要事项</option><option value="prohibited">不使用列表</option></select></label></div></section>
          <div className="prompt-length-policy">
            <span>目标篇幅：{draftPreferredMin}～{draftPreferredMax} 字符</span>
            <span>只统计分析正文：系统表格、冲突披露与来源边界块不计入。</span>

            {draftPreferredMin > draftPreferredMax && <em className="prompt-length-warning">目标最小字符不能大于目标最大字符。</em>}
            <HelpTip label="篇幅计算说明">只统计分析正文，系统表格、冲突披露与来源边界块不计入。篇幅是写作目标：偏短时提示，偏长时最多压缩一次；仍偏长也保留正文，不因字数回退、不阻断交付。单章调用额度 10000 tokens，字符数受模型响应能力影响。</HelpTip>
          </div></div></details>
          <details className="prompt-card prompt-collapsible prompt-extra"><summary><div><strong>补充业务要求</strong><HelpTip label="补充业务要求说明">用于保留历史整段 Prompt，或录入本章额外的写作要求。</HelpTip></div><span className="prompt-card-brief">{activeDraft.additional_requirements.trim() ? `已填写 ${activeDraft.additional_requirements.trim().length} 字` : "未填写"}</span><ChevronDown size={15} /></summary><div className="prompt-card-body"><textarea id="prompt-extra" aria-label="补充业务要求" rows={8} value={activeDraft.additional_requirements} onChange={(event) => updateDraft(active.chapter_key, { additional_requirements: event.target.value })} placeholder="输入补充要求；系统仍会执行下方只读风控约束。" /></div></details>
        </fieldset>
          {!active.llm && <div className="deterministic-prompt-note"><ShieldCheck size={17} /><div><strong>本章配置只读</strong><p>可查看章节配置全文，不能修改业务写作要求。</p></div></div>}{readOnly && active.llm && <p className="prompt-readonly">{scopeEditReason || (scope === "global" ? "当前环境不允许修改全局默认 Prompt。" : "当前环境不允许修改本项目 Prompt 配置。")}</p>}
          <section className="prompt-constraints-summary"><div><ShieldCheck size={16} /><div><strong>只读风控约束</strong><HelpTip label="只读风控约束说明">证据边界、禁止事项和引用规则由系统固定执行，不随章节配置改变。</HelpTip></div></div><button className="secondary-button small" type="button" onClick={() => setConstraintModalOpen(true)}>查看详情</button></section>
          {active.llm && !readOnly && <footer className="prompt-action-bar"><span>{isChanged ? "存在未保存的修改" : effectiveUpdatedAt ? `当前配置已保存 · 最近更新：${formatPromptUpdatedAt(effectiveUpdatedAt)}` : "当前使用系统默认配置"}</span><div>{scope === "global"
              ? <>{active.global_overridden && <button className="text-button" disabled={saving === active.chapter_key} onClick={reset}>{saving === active.chapter_key ? <LoaderCircle className="spin" size={15} /> : null}撤销全局默认</button>}<button className="primary-button" disabled={!isChanged || saving === active.chapter_key} onClick={save}>{saving === active.chapter_key ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}保存全局默认</button></>
              : <>{active.overridden && <button className="text-button" disabled={saving === active.chapter_key} onClick={reset}>撤销项目覆盖</button>}<button className="secondary-button" disabled={!canSetGlobalDefault} onClick={setGlobalDefault}>{active.global_overridden ? "更新全局默认" : "设为全局默认"}</button>{active.global_overridden && <button className="text-button" disabled={saving === active.chapter_key} onClick={resetGlobalDefault}>撤销全局默认</button>}<button className="primary-button" disabled={!isChanged || saving === active.chapter_key} onClick={save}>{saving === active.chapter_key ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}保存项目配置</button></>}</div></footer>}
        </div> : <div className="prompt-preview"><div><strong>{active.llm ? "最终发送给模型的指令" : "章节配置全文"}</strong><span>{active.llm ? "由服务端使用当前草稿实时编译" : "由系统固定规则生成"}</span></div>{active.llm ? previewLoading ? <div className="prompt-preview-loading"><LoaderCircle className="spin" size={20} />正在编译完整 Prompt</div> : previewError ? <div className="inline-error"><AlertTriangle size={15} />{previewError}</div> : <textarea aria-label={`${active.title} 完整 Prompt`} value={previewPrompt || active.prompt} readOnly /> : <textarea aria-label={`${active.title} 章节配置全文`} value={active.prompt} readOnly />}</div>}
        </> : <>
        <header className="prompt-detail-head"><div><span>{active?.part || "报告目录"}</span><h2>{activeNavChapter?.title || "报告目录"}</h2></div><div className="prompt-detail-badges"><span className="llm">模型生成</span><span className="custom">自定义章节</span></div></header>
        {scopeSwitch}
        <p className="prompt-readonly">该章节尚无可用 Prompt 配置；请先保存报告目录，或等待服务端同步后重试。</p>
        </>}
        {outlineLoading && !outlineDraft && <div className="outline-loading"><LoaderCircle className="spin" size={16} />正在载入报告目录</div>}
        {subsectionEditor}
        {outlineActionBar}
      </main>
    </section>}
    {constraintModalOpen && <div className="prompt-constraint-modal-backdrop" role="presentation" onClick={() => setConstraintModalOpen(false)}><section className="prompt-constraint-modal" role="dialog" aria-modal="true" aria-labelledby="prompt-constraint-modal-title" onClick={(event) => event.stopPropagation()}><header className="prompt-constraint-modal-head"><div><strong id="prompt-constraint-modal-title">只读风控约束</strong><span>不可编辑 · 由系统固定执行</span></div><button className="icon-button" type="button" aria-label="关闭风控约束详情" title="关闭" onClick={() => setConstraintModalOpen(false)}><X size={16} /></button></header><div className="prompt-constraint-modal-body"><div className="prompt-constraints-body"><PromptConstraintGroup title="必须保留" values={constraints.preserve_blocks} /><PromptConstraintGroup title="禁止事项" values={constraints.forbidden} /><PromptConstraintGroup title="允许材料类型" values={constraints.input_contract.source_kinds || []} /><PromptConstraintGroup title="输入字段" values={[...(constraints.input_contract.fact_keys || []), ...(constraints.input_contract.metric_keys || []), ...(constraints.input_contract.rule_keys || [])]} /><PromptConstraintGroup title="输出必备表述" values={[...(constraints.output_contract.required_phrases || []), ...(constraints.output_contract.required_missing_markers || [])]} /><div className="prompt-constraint-group"><strong>引用规则</strong><p>最多 {constraints.output_contract.max_citations ?? 0} 条引用 · 篇幅由本章 Prompt 的目标字符范围决定</p></div><div className="prompt-constraint-group global-rules"><strong>全局规则</strong>{Object.entries(constraints.global_rules).map(([key, value]) => <p key={key}>{value}</p>)}</div></div></div></section></div>}
  </div>;
}

const GENERATION_STATUS_LABELS: Record<string, string> = {
  // 计划 §四 line 38/42：三种状态必须在界面上区分开——
  // 「模型已采用」（全部检查通过）/「模型待核验」（仅剩未决项，正文仍是模型稿）/
  // 「本次失败」（回退确定性稿）。`model_needs_review` 不计入合格采用率。
  model_passed: "模型润色已应用",
  model_needs_review: "模型待核验",
  fallback: "模型润色已回退",
  deterministic: "确定性生成",
  skipped: "本次未调用模型",
  human_edit: "人工编辑",
};

/** 正文真实来源（比 generation_status 更可信：后者曾被后置回退污染过）。 */
const CONTENT_ORIGIN_LABELS: Record<string, string> = {
  model: "模型润色",
  deterministic: "确定性模板",
  human: "人工编辑",
  unknown: "历史来源未核实",
};

/** 回退原因精简成可读标签；完整原因保留在 tooltip 里。 */
const FALLBACK_REASON_LABELS: [string, string][] = [
  ["新数字", "出现未授权数字"],
  ["禁止表达", "命中禁止表达"],
  ["未授权引用", "存在未授权引用"],
  ["冻结快照来源归属", "主体档案来源归属缺失"],
  ["来源边界块", "来源边界块未保留"],
  ["未授权的完整年度", "出现未授权年度"],
  ["冲突断言块", "冲突断言块未保留"],
  ["篇幅修复后仍超出", "篇幅仍超出目标"],
  ["篇幅", "篇幅超出目标"],
  ["模型调用或结构校验失败", "模型调用失败"],
];

function fallbackReasonLabel(reason: string) {
  for (const [needle, label] of FALLBACK_REASON_LABELS) {
    if (reason.includes(needle)) return label;
  }
  return "其他校验未通过";
}

function reportGenerationBadge(section: ReportSection) {
  const origin = section.content_origin || "";
  const status = section.generation_status || "";
  if (!origin && !status) return null;
  const reason = section.fallback_reason || "";
  // 「模型已采用」与「模型待核验」必须分开显示（计划 §四 line 42）：两者的
  // `content_origin` 都是 `model`，只按来源显示会把"仅剩未决项"的稿件混进"已采用"，
  // 而它其实**不计入合格采用率**。这里以 generation_status 为准并降一档色调。
  const needsReview = status === "model_needs_review";
  const originLabel = needsReview
    ? "模型润色（待核验）"
    : CONTENT_ORIGIN_LABELS[origin] || GENERATION_STATUS_LABELS[status] || status;
  const tone = needsReview ? "warning"
    : origin === "model" ? "passed"
    : origin === "human" ? "neutral"
    : origin === "deterministic" ? "warning" : "neutral";
  // 后置回退（正文已是模板但历史上出现过模型结果）要单独说明，不能只显示来源
  const note = section.fallback_stage === "report_support" && reason
    ? ` · 处理记录：${fallbackReasonLabel(reason)}` : "";
  // 文风写作提示（计划 §二 line 17）：**不阻断保存与交付**，但要让人看得见——
  // 否则它就只剩 Prompt 里一句无人复核的指引。悬停看具体项。
  const styleNotes = section.validation_diagnostics?.style_notes || [];
  const styleNote = styleNotes.length ? " · 写作提示" : "";
  // Prompt 时效（计划 §四 line 41）：正文生成时的 Prompt 与**当前生效** Prompt 已不同。
  // 只提示"这一章是按旧 Prompt 写的、要不要重跑由你决定"——保存 Prompt 不会自动再生。
  const promptStale = section.prompt_outdated === true;
  const staleNote = promptStale ? " · Prompt 已更新" : "";
  const tooltip = [
    reason || originLabel,
    promptStale ? "本章正文由旧版 Prompt 生成，可重新生成以应用当前 Prompt" : "",
    ...styleNotes,
  ].filter(Boolean).join("；");
  return (
    <span className={`section-generation ${tone}`} title={tooltip}>
      正文来源：{originLabel}{note}{styleNote}{staleNote}
    </span>
  );
}

/** 交付一致性：强制披露是否完整、是否可交付。与证据支持率分开显示。 */
function reportDeliveryBadge(section: ReportSection) {
  const disclosure = section.disclosure_status || "";
  const delivery = section.delivery_status || "";
  if (!disclosure && !delivery) return null;
  const tone = delivery === "eligible" ? "passed" : delivery === "needs_review" ? "warning" : "neutral";
  const label = delivery === "eligible" ? "可交付"
    : delivery === "needs_review" ? "待人工复核"
    : delivery === "pending" ? "待校验" : "未校验";
  const disclosureLabel = disclosure === "passed" ? "披露完整" : disclosure === "warning" ? "披露不完整" : "";
  return (
    <span className={`section-delivery ${tone}`} title={`强制披露：${disclosureLabel || "未校验"}`}>
      {disclosureLabel ? `${disclosureLabel} · ` : ""}{label}
    </span>
  );
}

const LENGTH_STATUS_LABELS: Record<string, string> = {
  passed: "篇幅符合",
  short: "篇幅偏短",
  long: "篇幅偏长",
  // 系统硬顶已移除，这个状态只可能出现在历史报告里。
  hard_limit_exceeded: "超出旧硬上限",
};

/** 章节篇幅徽标：字符数与目标范围均由后端给出，前端只负责展示。 */
function reportLengthBadge(section: ReportSection) {
  const status = section.length_status || "not_checked";
  const policy = section.length_policy || {};
  if (status === "not_checked") {
    return <span className="section-length unknown" title="历史报告未记录篇幅诊断">正文 {section.visible_content_chars ?? 0} 字符</span>;
  }
  const repairNote = !section.length_repair_attempted ? ""
    : section.validation_diagnostics?.length_repair_failed ? " · 压缩稿未通过校验，保留原正文"
    : status === "passed" ? " · 已完成一次篇幅压缩"
    : " · 已压缩一次，仍偏长";
  return <span className={`section-length ${status}`}>
    正文 {section.visible_content_chars ?? 0} 字符 · 目标 {policy.effective_min_chars ?? 0}～{policy.effective_max_chars ?? 0}
    {LENGTH_STATUS_LABELS[status] ? ` · ${LENGTH_STATUS_LABELS[status]}` : ""}{repairNote}
  </span>;
}

function renderInlineMarkdown(text: string) {
  return text.split(/(\*\*.+?\*\*)/).filter(Boolean).map((part, index) => (part.startsWith("**") && part.endsWith("**")
    ? <strong key={index}>{part.slice(2, -2)}</strong>
    : <span key={index}>{part}</span>));
}

/** 表格来源模式：三种模式不得互相冒充，标签与表格核对工作区保持一致。 */
const TABLE_ORIGIN_MODE_LABELS: Record<string, string> = {
  profile_facts: "已核验事实汇总",
  uploaded_table: "上传材料原表",
  merged: "档案与上传材料核对",
};

const TABLE_VALIDATION_LABELS: Record<string, string> = {
  passed: "校验通过", blocked: "校验未通过", needs_review: "待人工确认", pending: "未校验",
};

/** 逐格来源类型：只在 tooltip 里露出来源性质，正文不出现内部字段名。 */
const CELL_ORIGIN_TYPE_LABELS: Record<string, string> = {
  profile_fact: "主体档案事实", uploaded_cell: "上传材料原表", manual: "人工录入", derived: "系统计算",
};

/** 段落正文的来源标注：直接读 section.content_origin，不猜生成状态。 */
const PROSE_ORIGIN_LABELS: Record<string, string> = {
  model: "模型分析正文",
  deterministic: "确定性模板",
  human: "人工编辑正文",
  unknown: "来源未核实正文",
};

/** 承载数值的列角色；右对齐并用等宽数字。其余列是科目、期间、状态等描述性内容。 */
const NUMERIC_COLUMN_ROLES = new Set(["period_value", "profile_value", "uploaded_value", "adopted_value", "disclosure"]);

/**
 * 判断一个 table 块是**受控数据表**还是旧版正文管道表。
 *
 * 两者 kind 都是 "table"，只能按字段形态区分：受控表一定有 table_id / columns，
 * 且 rows 是对象数组；管道表的 rows 是 `| a | b |` 字符串数组。
 * 后端未升级或旧数据走管道表分支，两种形态都不允许白屏。
 */
function isDataTableBlock(block: ReportBlock): block is ReportTableBlock {
  if (block.kind !== "table") return false;
  const candidate = block as ReportTableBlock;
  if (typeof candidate.table_id === "string" && candidate.table_id) return true;
  if (Array.isArray(candidate.columns) && candidate.columns.length > 0) return true;
  const rows: unknown = candidate.rows;
  return Array.isArray(rows) && rows.length > 0 && typeof rows[0] === "object" && rows[0] !== null;
}

/** 保留原表的千分位、精度和零值写法，与导出件一致。 */
function reportCellValue(cell: ReportBlockCell) {
  if ((cell.status === "not_applicable" || cell.status === "blank") && cell.raw !== "未披露") return "—";
  const normalized = String(cell.normalized ?? "").trim();
  const raw = String(cell.raw ?? "").trim();
  return raw || normalized || "—";
}

/** 受控数据表：与 /tables 接口读同一份 TableBlock，只渲染不改写数值。 */
function ReportDataTable({ block, openCitation }: { block: ReportTableBlock; openCitation: (id: string) => void }) {
  const columns = block.table?.columns || block.columns || [];
  const rows = block.table?.rows || (block.rows || []).filter((row): row is ReportBlockRow => typeof row === "object" && row !== null);
  const notes = (block.notes || []).filter(Boolean);
  const unit = String(block.unit || "").trim() || columns.map((column) => String(column.unit || "").trim()).find(Boolean) || "";
  const originMode = TABLE_ORIGIN_MODE_LABELS[block.origin_mode || ""] || String(block.origin_mode || "");
  const validation = block.validation_status || "pending";
  return <figure className="report-data-table" data-table-id={block.table_id} data-origin-mode={block.origin_mode || ""}>
    <figcaption className="report-data-table-caption">
      <span className="report-block-chip system">系统整理数据表</span>
      <strong className="report-data-table-title">{block.title || block.table_id}</strong>
      <span className="report-data-table-meta">
        {unit && <span>单位：{unit}</span>}
        {block.scope && <span>口径：{block.scope}</span>}
        {originMode && <span>来源：{originMode}</span>}
        <span className={`report-data-table-status ${validation}`}>{TABLE_VALIDATION_LABELS[validation] || validation}</span>
      </span>
    </figcaption>
    {(columns.length > 0 || rows.length > 0) && <div className="report-table-scroll">
      <table>
        <thead><tr>{columns.map((column) => <th key={column.key} scope="col" className={column.role === "subject" ? "subject" : NUMERIC_COLUMN_ROLES.has(column.role) ? "value" : "label"}>{column.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, rowIndex) => <tr key={`${row.row_key}-${rowIndex}`} className={row.kind || "normal"}>
            {(row.cells || []).map((cell, cellIndex) => {
              const column = columns[cellIndex];
              const status = cell.status || "";
              const conflict = status === "conflict";
              const evidence = (cell.source_ids || [])[0];
              const detail = [
                CELL_ORIGIN_TYPE_LABELS[cell.origin_type || ""], cell.adoption_basis, cell.manual_note,
              ].filter(Boolean).join(" · ");
              const value = <>{reportCellValue(cell)}{conflict && <em className="report-cell-flag">冲突</em>}</>;
              return <td key={`${row.row_key || rowIndex}-${cellIndex}`} data-cell-status={status}
                className={`${column?.role === "subject" ? "subject" : column && NUMERIC_COLUMN_ROLES.has(column.role) ? "value" : "label"}${conflict ? " status-conflict" : ""}`}>
                {evidence
                  ? <button type="button" className="report-cell-value" title={`查看证据${detail ? ` · ${detail}` : ""}`} onClick={() => openCitation(evidence)}>{value}</button>
                  : <span className="report-cell-value" title={detail || undefined}>{value}</span>}
              </td>;
            })}
          </tr>)}
        </tbody>
      </table>
    </div>}
    {notes.length > 0 && <div className="report-data-table-notes">{notes.map((note) => <p key={note}>{note}</p>)}</div>}
  </figure>;
}

/**
 * 章内正文的层级渲染：三级标题（1.）与四级标题、列表、引用、表格。
 *
 * 表格分两种：受控数据表（逐格带状态与证据）与旧版正文管道表；
 * 段落按 section.content_origin 标注来源（模型分析正文 / 确定性模板）。
 * 没有 blocks 字段（旧数据或后端未升级）时退回正文原文，不白屏。
 */
function ReportBlocks({ section, openCitation }: { section: ReportSection; openCitation: (id: string) => void }) {
  const blocks = section.blocks;
  if (!blocks || !blocks.length) return <p>{section.content}</p>;
  const proseOrigin = PROSE_ORIGIN_LABELS[section.content_origin || ""] || PROSE_ORIGIN_LABELS.unknown;
  return <>{blocks.map((block, index) => {
    if (block.kind === "heading") {
      const Heading = (block.level === 4 ? "h4" : "h3") as "h3" | "h4";
      return <Heading className={`report-block-heading level-${block.level || 3}`} key={index}>{block.number ? <span>{block.number}</span> : null}{block.text}</Heading>;
    }
    if (block.kind === "list") {
      const items = (block.items || []).map((item, itemIndex) => <li key={itemIndex}>{renderInlineMarkdown(item)}</li>);
      return block.ordered
        ? <ol className="report-block-list" key={index}>{items}</ol>
        : <ul className="report-block-list" key={index}>{items}</ul>;
    }
    if (block.kind === "quote") return <blockquote className="report-block-quote" key={index}>{renderInlineMarkdown(block.text || "")}</blockquote>;
    if (block.kind === "table") {
      if (isDataTableBlock(block)) return <ReportDataTable key={index} block={block} openCitation={openCitation} />;
      const rows = (block.rows || [])
        .filter((row) => typeof row === "string")
        .map((row) => row.replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim()))
        .filter((cells) => !cells.every((cell) => /^[-: ]*$/.test(cell)));
      // 管道表也放进可横向滚动的容器，避免窄屏被表格撑破。
      return <div className="report-markup-table-scroll" key={index}><table className="report-block-table"><tbody>{rows.map((cells, rowIndex) => <tr key={rowIndex}>{cells.map((cell, cellIndex) => (rowIndex === 0 ? <th key={cellIndex}>{cell}</th> : <td key={cellIndex}>{cell}</td>))}</tr>)}</tbody></table></div>;
    }
    if (block.kind === "paragraph") {
      // 来源标注只打在每段正文的起首（连续段落共用同一个 section.content_origin），
      // 免得每段都挂一个标签把正文压得看不出层次。
      const leadsProse = index === 0 || blocks[index - 1].kind !== "paragraph";
      return <Fragment key={index}>
        {leadsProse && <span className="report-block-chip prose">{proseOrigin}</span>}
        <p className="report-block-paragraph">{renderInlineMarkdown(block.text || "")}</p>
      </Fragment>;
    }
    return <p key={index}>{renderInlineMarkdown(block.text || "")}</p>;
  })}</>;
}

function Report({ project, bondSummary, sections, quality, snapshot, token, openCitation, refresh, hasOpenMajor }: { project: Project; bondSummary: BondSummary | null; sections: ReportSection[]; quality: ReportQuality | null; snapshot: ProjectProfileSnapshot | null; token?: string; openCitation: (id: string) => void; refresh: () => Promise<void>; hasOpenMajor: boolean }) {
  const [editing, setEditing] = useState<string | null>(null); const [draft, setDraft] = useState(""); const [busy, setBusy] = useState<string | null>(null);
  const [tableRefreshError, setTableRefreshError] = useState("");
  const [expandedCitations, setExpandedCitations] = useState<Set<string>>(new Set());
  const [expandedSections, setExpandedSections] = useState<Set<string>>(() => new Set(sections.slice(0, 1).map((item) => item.section_key)));
  const reportTitle = bondSummary?.bondName || project.company_name;
  const reportLabel = "债券发行融资担保尽调报告";
  const save = async (section: ReportSection, reviewed = false) => { setBusy(section.section_key); try { await api.saveSection(project.id, section.section_key, draft || section.content, reviewed, token); setEditing(null); await refresh(); } finally { setBusy(null); } };
  const regenerate = async (section: ReportSection) => { setBusy(section.section_key); try { await api.regenerate(project.id, section.section_key, token); await refresh(); } finally { setBusy(null); } };
  const markReviewedReady = project.data_class !== "real_reference" && !project.archived_at && quality?.status === "passed" && !project.analysis_stale && !project.report_stale && !hasOpenMajor;
  const markReviewed = async () => { setBusy("__report-review__"); try { await api.markReportReviewed(project.id, token); await refresh(); } finally { setBusy(null); } };
  const staleTableSections = sections.filter((section) => ["financial", "cashflow", "debt", "rating"].includes(section.section_key) && section.system_tables?.outdated);
  const refreshSystemTables = async () => {
    setBusy("__table-refresh__"); setTableRefreshError("");
    try {
      const revision = staleTableSections[0]?.report_revision ?? project.report_revision ?? 0;
      const hashes = Object.fromEntries(staleTableSections.map((section) => [section.section_key, section.content_sha256 || ""]));
      await api.refreshReportTables(project.id, staleTableSections.map((section) => section.section_key), revision, hashes, token);
      await refresh();
    } catch (reason) {
      setTableRefreshError(reason instanceof Error ? reason.message : "更新系统表格失败");
    } finally { setBusy(null); }
  };
  const canExport = project.data_class !== "real_reference";
  const draftWordReady = canExport;
  const finalWordReady = canExport && quality?.status === "passed" && !project.analysis_stale && !project.report_stale && project.report_state === "final_ready";
  const wordReady = draftWordReady;
  const wordExportTitle = "导出草稿 Word；最终版需应用最新复核、通过校验并标记已复核";
  return <div className="view-content report-view"><div className="page-heading"><div><span className="eyebrow">人工复核工作区</span><h1>尽调报告</h1></div><div className="export-group"><span className={`report-quality-chip ${quality?.status === "passed" ? "passed" : "warning"}`}><ShieldCheck size={14} />事实支持率 {quality ? `${Math.round(quality.support_rate * 100)}%` : "待校验"}</span>{canExport ? <><button className="secondary-button export-action" onClick={() => api.exportReport(project.id, "md", reportTitle, token, Boolean(bondSummary))}><Download size={16} />Markdown</button><button className="primary-button export-action" disabled={!wordReady} title={wordExportTitle} onClick={() => api.exportReport(project.id, "docx", reportTitle, token, Boolean(bondSummary), "draft")}><Download size={16} />导出 Word</button></> : <span className="export-restricted"><LockKeyhole size={14} />真实材料默认不提供导出</span>}<button className="secondary-button export-action" disabled={!markReviewedReady || busy === "__report-review__"} title={markReviewedReady ? "标记当前报告为已复核" : "需先应用复核、通过校验并关闭重大事项"} onClick={markReviewed}>{busy === "__report-review__" ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}标记已复核</button><button className="secondary-button export-action" disabled={!finalWordReady} title={finalWordReady ? "导出最终已复核 Word" : "需应用最新复核、通过校验、关闭重大事项并标记已复核"} onClick={() => api.exportReport(project.id, "docx", reportTitle, token, Boolean(bondSummary), "final")}><FileCheck2 size={16} />最终 Word</button></div></div>{staleTableSections.length > 0 && <div className="report-block-warning"><FileSpreadsheet size={16} /><span>{staleTableSections.length} 个章节的系统表格需要更新。更新会保留分析正文并将相关章节设为待复核，不调用模型。</span><button className="secondary-button small" disabled={busy === "__table-refresh__" || Boolean(project.archived_at)} onClick={refreshSystemTables}>{busy === "__table-refresh__" ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}更新系统表格</button></div>}{quality?.regeneration_requirement === "full_report" && <div className="report-block-warning"><AlertTriangle size={16} /><span>报告目录的二级结构已变更（章节增删或排序），单章再生无法使报告与目录一致；请整份重新生成报告。</span></div>}{quality?.regeneration_requirement === "chapter" && <div className="report-block-warning"><AlertTriangle size={16} /><span>{`报告目录有 ${quality.outline_changed_chapters?.length || 1} 个章节的小节配置已变化，按章再生对应章节即可更新（不影响其他章节）。`}</span></div>}{tableRefreshError && <div className="report-block-warning" role="alert"><AlertTriangle size={16} />{tableRefreshError}</div>}<div className="report-layout"><aside className="report-toc"><strong>报告目录</strong>{sections.map((section, index) => { const showPart = Boolean(section.part_label) && section.part_label !== sections[index - 1]?.part_label; const standalone = Boolean(section.part_label) && !section.chapter_label; return <Fragment key={section.id}>{showPart && !standalone && <span className="toc-part">{section.part_label}</span>}<a href={`#section-${section.section_key}`}><span>{section.chapter_number || (standalone || !section.chapter_label ? "" : String(index + 1).padStart(2, "0"))}</span>{standalone ? section.part_label : section.title}</a></Fragment>; })}</aside><main className="report-paper"><div className="report-cover"><span>{reportLabel}</span><h2>{reportTitle}</h2>{bondSummary?.reportLevel && <p>报告状态：{bondSummary.reportLevel}</p>}{snapshot && <p>主体档案快照：v{snapshot.version} · 基准日 {snapshot.baseline_date || "待确认"}</p>}<p>募集说明书与评级报告版本：本次运行所用已解析文件</p><p>Agent 初稿 · 等待人工复核</p>{project.data_class === "real_reference" && <p>真实公开材料本地参考 · 不属于评测案例</p>}{hasOpenMajor && <strong className="cover-warning"><AlertTriangle size={16} />存在未决重大事项</strong>}</div><div className={`report-quality-line ${quality?.status === "passed" ? "passed" : "warning"}`}><div><strong>{quality?.supported_claims ?? 0}/{quality?.factual_claims ?? 0}</strong><span>事实性主张已获得证据支持</span></div><div><strong>{quality?.invalid_citations ?? 0}</strong><span>无效引用</span></div><div><strong>{quality?.unsupported_claims ?? 0}</strong><span>无证据主张</span></div></div>{quality?.status === "warning" && <div className="report-block-warning"><AlertTriangle size={16} />存在无证据事实或无效引用，相关章节不能标记为已复核，Word 导出已禁用。</div>}{project.analysis_stale && <div className="report-block-warning"><RefreshCw size={16} />{(project.report_stale_reasons || []).includes("project_info_changed") ? "项目基础信息已变更，请重新运行尽调" : "材料或主体快照已变化，需要重新运行 Agent"}；重跑前 Word 导出已禁用。</div>}<div className={`report-notice ${hasOpenMajor ? "major" : ""}`}><AlertTriangle size={17} />{hasOpenMajor ? "本报告包含未处理冲突、高风险候选项或高优先级任务，详见摘要与附录。" : "本报告为风险审查辅助材料，不构成自动审批意见。"}</div>{sections.map((section, index) => { const citationsExpanded = expandedCitations.has(section.section_key); const sectionExpanded = expandedSections.has(section.section_key); const visibleCitations = citationsExpanded ? section.citation_ids : section.citation_ids.slice(0, 3); const showPart = Boolean(section.part_label) && section.part_label !== sections[index - 1]?.part_label; const standalone = Boolean(section.part_label) && !section.chapter_label; return <Fragment key={section.id}>{showPart && !standalone && <h3 className="report-part-heading">{section.part_label}</h3>}<section className={`report-section ${sectionExpanded ? "mobile-expanded" : "mobile-collapsed"}`} id={`section-${section.section_key}`}><div className="section-heading"><h2 className="section-title">{section.chapter_number ? <span className="section-number">{section.chapter_number}</span> : null}<span className="section-name">{standalone ? section.part_label : section.title}</span></h2><div className="section-status"><span className={`section-quality ${section.validation_status === "passed" ? "passed" : "warning"}`}>{Math.round((section.support_rate ?? 0) * 100)}% 支持</span>{reportLengthBadge(section)}{reportGenerationBadge(section)}{reportDeliveryBadge(section)}</div><button className="mobile-section-toggle icon-button" title={sectionExpanded ? "收起章节" : "展开章节"} onClick={() => setExpandedSections((current) => { const next = new Set(current); if (next.has(section.section_key)) next.delete(section.section_key); else next.add(section.section_key); return next; })}><ChevronDown size={16} /></button><div className="section-tools"><button className="icon-button" title="重新生成" onClick={() => regenerate(section)} disabled={busy === section.section_key}><RefreshCw className={busy === section.section_key ? "spin" : ""} size={16} /></button><button className="icon-button" title="编辑章节" onClick={() => { setEditing(section.section_key); setDraft(section.content); }}><Pencil size={16} /></button></div></div><div className="section-collapsible">{editing === section.section_key ? <div className="editor"><textarea value={draft} onChange={(event) => setDraft(event.target.value)} /><div><button className="text-button" onClick={() => setEditing(null)}>取消</button><button className="primary-button small" onClick={() => save(section)}>保存修改</button></div></div> : <div className="section-content"><ReportBlocks section={section} openCitation={openCitation} /></div>}<div className="citation-row">{visibleCitations.map((id) => <button key={id} onClick={() => openCitation(id)}>{id.slice(-8)}</button>)}{section.citation_ids.length > 3 && <button className="citation-more" onClick={() => setExpandedCitations((current) => { const next = new Set(current); if (next.has(section.section_key)) next.delete(section.section_key); else next.add(section.section_key); return next; })}>{citationsExpanded ? "收起引用" : `查看全部 ${section.citation_ids.length} 条`}</button>}{section.review_status === "reviewed" ? <span className="reviewed"><Check size={13} />已复核</span> : <button className="mark-reviewed" disabled={section.validation_status !== "passed" || hasOpenMajor} title={section.validation_status !== "passed" ? "证据校验未通过" : hasOpenMajor ? "存在未决重大事项" : "标记已复核"} onClick={() => { setDraft(section.content); save(section, true); }}><Check size={13} />标记已复核</button>}</div></div></section></Fragment>; })}</main></div></div>;
}

/* 证据原文重排。抽取出来的整页文本本身就是一行一条记录，只是浏览器把换行折叠掉了，
   所以这里按行重新摆一遍，并把表格页的数值列右对齐。

   不变量：lines.flatMap((line) => [...line.parts.map((part) => part.text), line.break]).join("") === quote
   每个 part 都是原文里连续、不重叠、顺序不变的切片——重排只搬运字符，绝不增删、改写或重排。
   所以选中整段复制得到的仍是逐字节一致的原文。

   两条护栏，都是为了不制造「看起来对的错」：
   1. 整页判定。不是表格页（行数不足、列数不合法、表格行占比过低）时全部按原样渲染，
      正文引用、两行引用永远不会被排成假表格。
   2. 列数精确相等。只有值个数恰好等于列数的行进栅格；把 2 个值放进 3 列会让 2024 年的
      数字出现在 2023 年表头下面——在尽调工具里这比不对齐危险得多，宁可退回普通文本行。 */
type EvidencePart = { text: string; kind: "plain" | "label" | "value" | "gap" };
type EvidenceLineLayout = { parts: EvidencePart[]; kind: "plain" | "row" | "header"; hit: boolean; break: string };
type EvidenceLayout = { lines: EvidenceLineLayout[]; columns: number; valueWidth: number };
type EvidenceToken = { text: string; start: number; end: number };

/** 白名单单位：只吸收明确跟在数字后面的计量单位。年/月/日刻意不在表内，
    否则「公司成立于 2001 年」会被当成数据行。 */
const EVIDENCE_UNIT_PATTERN = "万亿元|百万元|千万元|万元|亿元|千元|元|万|亿|％|%|‰|倍|股|人|天|个|项|次";
const EVIDENCE_NUMBER_RE = new RegExp(`^[-+]?\\d[\\d,，]*(?:\\.\\d+)?(?:${EVIDENCE_UNIT_PATTERN})?$`);
/** 占位符在中文财报里表示「无此项」，必须占一列；否则整行数值会左移一列、落到错误的年份下面。 */
const EVIDENCE_PLACEHOLDER_RE = /^(?:[—–]|--|N\/A|不适用|无)$/i;

/** 取 token 时带偏移量。偏移量是分段模型的前提，不能用 split(/\s+/) 丢掉位置。 */
function evidenceTokens(line: string): EvidenceToken[] {
  const tokens: EvidenceToken[] = [];
  const pattern = /\S+/g;
  let match = pattern.exec(line);
  while (match) {
    tokens.push({ text: match[0], start: match.index, end: match.index + match[0].length });
    match = pattern.exec(line);
  }
  return tokens;
}

/** 逐行判定：从右往左吃掉数值和占位符，左边剩下的就是标签。返回 null 表示按原样渲染。 */
function classifyEvidenceLine(line: string): { cells: EvidenceToken[] } | null {
  const tokens = evidenceTokens(line);
  let cursor = tokens.length;
  while (cursor > 0 && (EVIDENCE_NUMBER_RE.test(tokens[cursor - 1].text) || EVIDENCE_PLACEHOLDER_RE.test(tokens[cursor - 1].text))) cursor -= 1;
  const values = tokens.slice(cursor);
  if (!values.length) return null;                                   // 吃不出数值：资产：、单位：万元
  if (cursor === 0) return null;                                     // 左边没有标签：页码、折行续写的纯数字
  if (!values.some((token) => /\d/.test(token.text))) return null;   // 整行都是占位符
  const label = line.slice(0, values[0].start);
  if (!label.trim()) return null;
  if (/[。！？]/.test(label)) return null;                            // 正文句子
  if (values.length === 1 && /[，、；]/.test(label)) return null;     // 「截至 2025 年末，公司资产总计 2,741,290.13」
  return { cells: [{ text: label, start: 0, end: values[0].start }, ...values] };
}

/** 年份表头：命中数恰好等于列数才认。这样「项目 2025 年末 2024 年末 2023 年末」的年份
    才会落在下方数值列的正上方，而不是贴着左边飘。 */
function classifyEvidenceHeader(line: string, columns: number): { cells: EvidenceToken[] } | null {
  if (/。/.test(line)) return null;
  const yearRe = /(?:19|20)\d{2}\s*(?:年(?:末|度|初)?|\d{1,2}\s*月\s*\d{1,2}\s*日)?/g;
  const cells: EvidenceToken[] = [];
  let match = yearRe.exec(line);
  while (match) {
    cells.push({ text: match[0], start: match.index, end: match.index + match[0].length });
    match = yearRe.exec(line);
  }
  if (cells.length !== columns) return null;
  if (!line.slice(0, cells[0].start).trim()) return null;
  return { cells: [{ text: line.slice(0, cells[0].start), start: 0, end: cells[0].start }, ...cells] };
}

/** 取值个数的众数（并列取大）。取众数而不是最大值，是为了防止某一行跑偏成 5 个值
    就把全页按 5 列排、把所有正常的 3 值行踢出对齐。 */
function evidenceColumnCount(counts: number[]): number {
  const tally = new Map<number, number>();
  counts.forEach((count) => tally.set(count, (tally.get(count) ?? 0) + 1));
  let best = 0; let bestHits = 0;
  for (const [value, hits] of tally) if (hits > bestHits || (hits === bestHits && value > best)) { best = value; bestHits = hits; }
  return best;
}

/** 打通事实原值（2741290.13，无千分位）与页面文本（2,741,290.13）。 */
function evidenceNumericKey(value: string): string { return value.replace(/[\s,，]/g, ""); }

/** 目标至少 4 位数字才定位。漏判只是不高亮，误判会把复核人带到错误的一行，代价不对等。 */
function evidenceFocusKey(focus: string): string {
  const key = evidenceNumericKey(focus);
  return (key.match(/\d/g)?.length ?? 0) >= 4 ? key : "";
}

function evidenceLineHits(text: string, key: string): boolean {
  const hay = evidenceNumericKey(text);
  const at = hay.indexOf(key);
  if (at < 0) return false;
  const before = at === 0 ? "" : hay[at - 1];
  const after = at + key.length >= hay.length ? "" : hay[at + key.length];
  return !/[\d.]/.test(before) && !/[\d.]/.test(after);   // 2741290.13 不许命中 12,741,290.13 内部
}

/** 把一行的单元格切片组装成 parts：单元格之间的空白原样留成 gap，一个字符都不丢。 */
function evidenceRowParts(line: string, cells: EvidenceToken[]): EvidencePart[] {
  const parts: EvidencePart[] = [];
  let cursor = 0;
  cells.forEach((cell, index) => {
    if (cell.start > cursor) parts.push({ text: line.slice(cursor, cell.start), kind: "gap" });
    parts.push({ text: line.slice(cell.start, cell.end), kind: index === 0 ? "label" : "value" });
    cursor = cell.end;
  });
  if (cursor < line.length) parts.push({ text: line.slice(cursor), kind: "gap" });
  return parts;
}

function layoutEvidenceQuote(quote: string, focus: string): EvidenceLayout {
  const rawLines = quote.split("\n");
  const key = evidenceFocusKey(focus);
  const parsed = rawLines.map(classifyEvidenceLine);
  const counts = parsed.map((item) => (item ? item.cells.length - 1 : 0)).filter((count) => count > 0);
  const nonEmpty = rawLines.filter((line) => line.trim()).length;
  const columns = evidenceColumnCount(counts);
  const alignable = counts.length >= 3 && columns >= 2 && columns <= 6 && nonEmpty > 0 && counts.length / nonEmpty >= 0.3;

  let widest = 0;
  if (alignable) parsed.forEach((item) => { if (item && item.cells.length - 1 === columns) item.cells.slice(1).forEach((cell) => { widest = Math.max(widest, cell.text.length); }); });

  const lines: EvidenceLineLayout[] = rawLines.map((line, index): EvidenceLineLayout => {
    const parsedItem = parsed[index];
    const isRow = alignable && parsedItem !== null && parsedItem.cells.length - 1 === columns;
    const header = !isRow && alignable ? classifyEvidenceHeader(line, columns) : null;
    const cells = isRow && parsedItem ? parsedItem.cells : header ? header.cells : null;
    const valueHit = cells ? cells.slice(1).some((cell) => evidenceNumericKey(cell.text) === key) : false;
    return {
      parts: cells ? evidenceRowParts(line, cells) : [{ text: line, kind: "plain" }],
      kind: isRow ? "row" : header ? "header" : "plain",
      hit: Boolean(key) && (valueHit || evidenceLineHits(line, key)),
      break: index < rawLines.length - 1 ? "\n" : "",
    };
  });
  // 每行是各自独立的栅格容器，轨道宽度必须按整页算出固定像素值：用 auto/max-content
  // 会各行独立解析，列根本对不齐。
  return { lines, columns: alignable ? columns : 0, valueWidth: Math.min(140, Math.max(84, widest * 7.4 + 14)) };
}

function EvidenceLine({ line, columns, valueWidth }: { line: EvidenceLineLayout; columns: number; valueWidth: number }) {
  const grid = line.kind !== "plain";
  return <span className={`evidence-line ${line.kind}${line.hit ? " hit" : ""}`} style={grid ? { gridTemplateColumns: `minmax(0, 1fr) repeat(${columns}, ${valueWidth}px)` } : undefined}>{line.parts.map((part, index) => part.kind === "gap" ? <Fragment key={index}>{part.text}</Fragment> : <span key={index} className={part.kind === "value" ? "evidence-value" : undefined}>{part.text}</span>)}</span>;
}

function EvidenceDrawer({ citation, focus, close }: { citation: Citation | null; focus: string; close: () => void }) {
  const drawerRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const lastTrigger = useRef<HTMLElement | null>(null);
  const closeAction = useRef(close);
  const opened = Boolean(citation);
  const layout = useMemo(() => (citation ? layoutEvidenceQuote(citation.quote || "", focus) : null), [citation, focus]);
  const hitCount = layout ? layout.lines.filter((line) => line.hit).length : 0;

  // `close` 是父级每次渲染新建的内联箭头函数。放进 ref 交给下面的焦点效应间接调用，
  // 焦点效应才能只依赖 `opened`——否则运行轮询期间任意一次父级重渲染都会让效应重跑，
  // 并把 lastTrigger 覆盖成证据层内部的关闭按钮。
  useEffect(() => { closeAction.current = close; }, [close]);

  // 抽屉自己就是滚动容器（fixed + overflow-y:auto），不能用 scrollIntoView：它会连带滚动
  // 背后页面，而 html 上的 scroll-behavior:smooth 会让这个过程变成不可测的动画。
  useEffect(() => {
    const box = drawerRef.current;
    if (!box || !citation) return;
    box.scrollTop = 0;
    const target = box.querySelector<HTMLElement>(".evidence-line.hit");
    if (!target) return;
    const headroom = Math.max(24, (box.clientHeight - target.getBoundingClientRect().height) / 2);
    const offset = target.getBoundingClientRect().top - box.getBoundingClientRect().top;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    box.scrollTo({ top: box.scrollTop + offset - headroom, behavior: reduce ? "auto" : "smooth" });
  }, [citation, focus]);

  // 焦点归还必须在状态更新之前完成：本抽屉关闭后立刻带上 aria-hidden="true"，
  // 若还用被动效应 cleanup 兜底，会出现"焦点仍留在 aria-hidden 子树里"的一帧，Chromium 会因此告警。
  const restoreFocus = useCallback(() => {
    const trigger = lastTrigger.current;
    lastTrigger.current = null;
    if (trigger && document.contains(trigger)) trigger.focus();
  }, []);

  const requestClose = useCallback(() => { restoreFocus(); closeAction.current(); }, [restoreFocus]);

  // 层级与焦点：证据层 z 40（styles.css:490）高于指标抽屉 38，打开时把焦点移入本层，
  // 关闭时还给触发按钮（设计规格 §4.2「抽屉内打开证据」）。依赖只取 `opened` 布尔值——
  // 证据层打开期间换一条 citation 引用不该重跑效应，否则会把触发按钮记错。
  // 移焦目标是头部已有的关闭按钮：本组件被报告 / 材料 / 主体快照 / 指标轨迹四处共用，
  // 给容器加 role 或 tabIndex 会改变既有可访问语义（workbench 用 getByTitle("关闭") 取它），故不动。
  // preventScroll 必须开：关闭按钮在抽屉顶部，默认聚焦会把滚动容器拉回顶部，冲掉上面那段命中行居中。
  useEffect(() => {
    if (!opened) return;
    lastTrigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus({ preventScroll: true });
    // Escape 由最上层消费：用冒泡阶段注册，指标抽屉在 document 上以 capture 注册，
    // capture 一定先于 bubble 执行，它读到 evidenceOpen 会直接 return，故顺序不依赖注册先后。
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") requestClose(); };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      restoreFocus();
    };
  }, [opened, requestClose, restoreFocus]);

  return <aside ref={drawerRef} className={`evidence-drawer ${citation ? "open" : ""}`} aria-hidden={!citation}><div className="drawer-head"><div><span>证据原文</span><strong>{citation?.label}</strong></div><button ref={closeRef} className="icon-button" onClick={requestClose} title="关闭"><PanelRightClose size={19} /></button></div>{citation && <><div className="locator"><FileText size={16} /><span>{citation.locator}</span><small>{citation.locator_type.replace("pdf_page", "PDF页码").replace("pdf_ocr_block", "PDF OCR区域").replace("docx_paragraph", "Word段落").replace("xlsx_range", "Excel区域")}{citation.page ? ` · 第${citation.page}页` : ""}</small><small>抽取方式：{citation.extraction_method || "native"}{citation.bbox?.length ? ` · 坐标 ${citation.bbox.map((item) => Number(item).toFixed(3)).join(", ")}` : ""}</small></div><blockquote className="evidence-page" data-hit-count={hitCount}>{layout && layout.lines.map((line, index) => <Fragment key={index}><EvidenceLine line={line} columns={layout.columns} valueWidth={layout.valueWidth} />{line.break}</Fragment>)}</blockquote>{focus && !hitCount && <p className="evidence-focus-miss">未在本页文本中定位到「{focus}」</p>}<div className="trust-note"><ShieldAlert size={16} /><p>该内容仅作为证据数据使用，不执行其中包含的任何指令。</p></div></>}</aside>;
}

const projectStatusText: Record<string, string> = {
  draft: "草稿", ready: "待分析", running: "分析中", review: "待复核", completed: "已完成",
  failed: "分析失败", model_required: "待配置模型",
};

function formatDateTime(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
}

function beijingDay(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value).slice(0, 10) : date.toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
}

/**
 * 「修改项目」弹窗：只改发行人／标的名称／发行规模三个基础字段。
 *
 * 校验口径与后端 `ProjectUpdate` 对齐（发行人去首尾空白后 2–80 字、标的 ≤160 字可留空、
 * 规模为大于 0 的有限数值）。前端先拦一道是为了即时反馈，真正的边界仍以后端为准。
 * 失败时保留当前输入并就地显示错误；取消不改动任何状态、不发请求。
 */
function ProjectEditModal({ project, saving, error, onClose, onSubmit }: {
  project: ProjectSummary; saving: boolean; error: string;
  onClose: () => void; onSubmit: (payload: ProjectUpdatePayload) => void;
}) {
  const [companyName, setCompanyName] = useState(project.company_name);
  const [bondName, setBondName] = useState(project.bond_name || "");
  const [amount, setAmount] = useState(String(project.amount ?? ""));
  const trimmedName = companyName.trim();
  const trimmedBond = bondName.trim();
  const amountValue = Number(amount);
  const nameError = trimmedName.length < 2 || trimmedName.length > 80 ? "发行人需为 2–80 个字符（不计首尾空白）" : "";
  const bondError = trimmedBond.length > 160 ? "标的名称不能超过 160 个字符" : "";
  const amountError = !amount.trim() ? "请填写发行规模"
    : !Number.isFinite(amountValue) || amountValue <= 0 ? "发行规模需为大于 0 的有限数值（万元）" : "";
  const invalid = Boolean(nameError || bondError || amountError);
  const submit = () => {
    if (saving || invalid) return;
    // 只提交真正变化的字段：无变化时后端不写库、不审计、不置失效。
    const payload: ProjectUpdatePayload = {};
    if (trimmedName !== project.company_name) payload.company_name = trimmedName;
    if (trimmedBond !== (project.bond_name || "")) payload.bond_name = trimmedBond;
    if (amountValue !== project.amount) payload.amount = amountValue;
    onSubmit(payload);
  };
  return <div className="modal-backdrop edit-project-backdrop" role="presentation" onClick={saving ? undefined : onClose}>
    <section className="edit-project-modal" role="dialog" aria-modal="true" aria-labelledby="edit-project-title" onClick={(event) => event.stopPropagation()}>
      <header className="edit-project-head">
        <div><span>项目基础信息</span><h2 id="edit-project-title">修改项目</h2></div>
        <button className="icon-button" type="button" onClick={onClose} disabled={saving} aria-label="关闭修改项目弹窗"><X size={18} /></button>
      </header>
      <div className="edit-project-body">
        <label>发行人
          <input value={companyName} aria-invalid={Boolean(nameError)} placeholder="材料中披露的发行人名称" onChange={(event) => setCompanyName(event.target.value)} />
          {nameError && <small className="field-error" role="alert">{nameError}</small>}
        </label>
        <label>标的名称
          <input value={bondName} aria-invalid={Boolean(bondError)} placeholder="可留空" onChange={(event) => setBondName(event.target.value)} />
          {bondError && <small className="field-error" role="alert">{bondError}</small>}
        </label>
        <label>发行规模（万元）
          <input type="number" min="0.01" step="any" value={amount} aria-invalid={Boolean(amountError)} placeholder="大于 0 的数值" onChange={(event) => setAmount(event.target.value)} />
          {amountError && <small className="field-error" role="alert">{amountError}</small>}
        </label>
        {project.issuer_profile_id && <p className="edit-project-note">该项目已绑定主体档案：此处只能纠正发行人名称，<strong>不能更换实际发行主体</strong>。主体档案、冻结快照与既有材料证据都不受本次修改影响。</p>}
        <p className="edit-project-note muted">保存后需重新运行尽调；旧报告仍可查看并导出草稿，但不能再被标记为最新最终报告。</p>
        {error && <div className="inline-error" role="alert"><AlertTriangle size={15} />{error}</div>}
      </div>
      <footer className="edit-project-foot">
        <button className="secondary-button" type="button" onClick={onClose} disabled={saving}>取消</button>
        <button className="primary-button" type="button" disabled={saving || invalid} onClick={submit}>
          {saving ? <LoaderCircle className="spin" size={15} /> : <Pencil size={15} />}保存
        </button>
      </footer>
    </section>
  </div>;
}

function ProjectList({ projects, saved, onSelect, onCreate, onArchive, onRestore, onUpdate }: {
  projects: ProjectSummary[]; saved: ProjectRef[]; onSelect: (project: ProjectSummary) => void; onCreate: () => void;
  onArchive: (project: ProjectSummary) => Promise<void>; onRestore: (project: ProjectSummary) => Promise<void>;
  onUpdate: (project: ProjectSummary, payload: ProjectUpdatePayload) => Promise<void>;
}) {
  type ProjectListState = { archiveView: "active" | "archived"; draft: { issuer: string; bond: string; minAmount: string; maxAmount: string; from: string; to: string; status: string }; filters: { issuer: string; bond: string; minAmount: string; maxAmount: string; from: string; to: string; status: string }; page: number };
  const persisted = (() => { try { return JSON.parse(sessionStorage.getItem("yandan-project-list") || "null") as Partial<ProjectListState> | null; } catch { return null; } })();
  const emptyFilters = { issuer: "", bond: "", minAmount: "", maxAmount: "", from: "", to: "", status: "" };
  const [archiveView, setArchiveView] = useState<"active" | "archived">(persisted?.archiveView === "archived" ? "archived" : "active");
  const [draft, setDraft] = useState(persisted?.draft || emptyFilters);
  const [filters, setFilters] = useState(persisted?.filters || emptyFilters);
  const [page, setPage] = useState(persisted?.page || 1);
  const [confirming, setConfirming] = useState<string>("");
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState("");
  const [editTarget, setEditTarget] = useState<ProjectSummary | null>(null);
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState("");
  const [notice, setNotice] = useState("");
  const [pageSize, setPageSize] = useState<PageSize>(DEFAULT_PAGE_SIZE);
  const changePageSize = (next: PageSize) => { setPageSize(next); setPage(1); };
  const tokenIds = useMemo(() => new Set(saved.map((item) => item.id)), [saved]);
  const visible = useMemo(() => {
    const min = filters.minAmount.trim() ? Number(filters.minAmount) : null;
    const max = filters.maxAmount.trim() ? Number(filters.maxAmount) : null;
    return projects.filter((item) => {
      const archived = Boolean(item.archived_at);
      if (archiveView === "active" ? archived : !archived) return false;
      if (filters.issuer.trim() && !item.company_name.toLocaleLowerCase().includes(filters.issuer.trim().toLocaleLowerCase())) return false;
      if (filters.bond.trim() && !(item.bond_name || "").toLocaleLowerCase().includes(filters.bond.trim().toLocaleLowerCase())) return false;
      if (min != null && item.amount < min) return false;
      if (max != null && item.amount > max) return false;
      const day = beijingDay(item.created_at);
      if (filters.from && day < filters.from) return false;
      if (filters.to && day > filters.to) return false;
      if (filters.status && item.status !== filters.status) return false;
      return true;
    }).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }, [archiveView, filters, projects]);
  const pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
  const rows = visible.slice((page - 1) * pageSize, page * pageSize);
  useEffect(() => { setPage(1); }, [archiveView, filters]);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);
  // 刷新后项目可能不再落在当前页（改名后不匹配筛选、或总数变少），页码越界时回到最后一页。
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);
  useEffect(() => { sessionStorage.setItem("yandan-project-list", JSON.stringify({ archiveView, draft, filters, page } satisfies ProjectListState)); }, [archiveView, draft, filters, page]);
  const query = () => {
    const min = draft.minAmount.trim() ? Number(draft.minAmount) : null;
    const max = draft.maxAmount.trim() ? Number(draft.maxAmount) : null;
    if ((min != null && (!Number.isFinite(min) || min < 0)) || (max != null && (!Number.isFinite(max) || max < 0))) { setError("发行规模必须是非负数"); return; }
    if (min != null && max != null && min > max) { setError("发行规模下限不能大于上限"); return; }
    if (draft.from && draft.to && draft.from > draft.to) { setError("建立日期起始不能晚于结束日期"); return; }
    setError(""); setFilters({ ...draft });
  };
  const reset = () => { const empty = { issuer: "", bond: "", minAmount: "", maxAmount: "", from: "", to: "", status: "" }; setDraft(empty); setFilters(empty); setError(""); };
  const actArchive = async (item: ProjectSummary) => {
    setBusy(item.id); setError("");
    try { await (item.archived_at ? onRestore(item) : onArchive(item)); setConfirming(""); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "项目状态更新失败"); }
    finally { setBusy(""); }
  };
  /** 修改按钮的禁用原因：无令牌优先，其次是已归档。空字符串表示可修改。 */
  const editBlockReason = (item: ProjectSummary) => !tokenIds.has(item.id)
    ? "缺少项目令牌，无法修改该项目"
    : item.archived_at ? "项目已归档，请先恢复项目后再修改" : "";
  const actUpdate = async (item: ProjectSummary, payload: ProjectUpdatePayload) => {
    setEditSaving(true); setEditError("");
    try {
      await onUpdate(item, payload);
      setEditTarget(null);
      setNotice(Object.keys(payload).length
        ? `「${payload.company_name || item.company_name}」项目基础信息已变更，请重新运行尽调。`
        : "未检测到变化，项目信息保持不变。");
    }
    catch (reason) { setEditError(reason instanceof Error ? reason.message : "项目基础信息保存失败"); }
    finally { setEditSaving(false); }
  };
  return <div className="view-content project-list-view">
    <div className="page-heading"><div><span className="eyebrow">Project workspace</span><div className="page-title-row"><h1>债券项目驾驶舱</h1><HelpTip label="债券项目驾驶舱说明">查看已建立的债券项目、按条件筛选，并进入尽调详情；列表支持按发行人、规模、日期和状态筛选。</HelpTip></div></div><button className="primary-button" onClick={onCreate}><FolderPlus size={16} />新建项目</button></div>
    {notice && <div className="project-edit-notice" role="status"><CheckCircle2 size={15} /><span>{notice}</span><button className="icon-button" onClick={() => setNotice("")} title="关闭提示"><X size={14} /></button></div>}
    <section className="panel project-filter-panel"><div className="project-list-tabs" role="tablist"><button className={archiveView === "active" ? "active" : ""} onClick={() => setArchiveView("active")}>未归档 <span>{projects.filter((item) => !item.archived_at).length}</span></button><button className={archiveView === "archived" ? "active" : ""} onClick={() => setArchiveView("archived")}>已归档 <span>{projects.filter((item) => Boolean(item.archived_at)).length}</span></button></div><div className="project-filters"><label>发行人<input value={draft.issuer} onChange={(event) => setDraft({ ...draft, issuer: event.target.value })} placeholder="输入发行人名称" /></label><label>标的名称<input value={draft.bond} onChange={(event) => setDraft({ ...draft, bond: event.target.value })} placeholder="输入债券名称" /></label><label>规模下限<input type="number" min="0" value={draft.minAmount} onChange={(event) => setDraft({ ...draft, minAmount: event.target.value })} placeholder="万元" /></label><label>规模上限<input type="number" min="0" value={draft.maxAmount} onChange={(event) => setDraft({ ...draft, maxAmount: event.target.value })} placeholder="万元" /></label><label>建立日期起<input type="date" value={draft.from} onChange={(event) => setDraft({ ...draft, from: event.target.value })} /></label><label>建立日期止<input type="date" value={draft.to} onChange={(event) => setDraft({ ...draft, to: event.target.value })} /></label><label>项目状态<select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value })}><option value="">全部状态</option>{Object.entries(projectStatusText).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label><div className="project-filter-actions"><button className="primary-button small" onClick={query}><SearchCheck size={14} />查询</button><button className="secondary-button small" onClick={reset}>重置</button></div></div>{error && <div className="inline-error"><AlertTriangle size={15} />{error}</div>}</section>
    <section className="panel project-table-panel"><div className="panel-title"><div><h2>{archiveView === "active" ? "已建立项目" : "已归档项目"}</h2><small>共 {visible.length} 个项目 · 第 {page}/{pageCount} 页</small></div><SlidersHorizontal size={18} /></div>{!rows.length ? <div className="project-empty"><FolderPlus size={24} /><strong>{projects.length ? "没有匹配的项目" : "还没有建立项目"}</strong><span>{projects.length ? "调整筛选条件后重新查询。" : "点击右上角新建项目开始建立债券项目。"}</span></div> : <div className="project-table-scroll"><table className="project-table"><thead><tr><th>发行人</th><th>标的名称</th><th className="value">发行规模（万元）</th><th>项目建立时间</th><th>项目状态</th><th>操作</th></tr></thead><tbody>{rows.map((item) => <tr key={item.id}><td><strong>{item.company_name}</strong></td><td>{item.bond_name || "未填写标的名称"}</td><td className="value">{item.amount.toLocaleString()}</td><td>{formatDateTime(item.created_at)}</td><td><span className={`project-status status-${item.status}`}>{projectStatusText[item.status] || item.status}</span>{item.archived_at && <small className="archived-time">归档于 {formatDateTime(item.archived_at)}</small>}</td><td><div className="project-row-actions"><button className="text-button" onClick={() => onSelect(item)}><Eye size={14} />详情</button><button className="text-button" disabled={Boolean(editBlockReason(item))} title={editBlockReason(item) || "修改项目基础信息"} onClick={() => { setEditError(""); setEditTarget(item); }}><Pencil size={14} />修改</button>{tokenIds.has(item.id) ? confirming === item.id ? <span className="confirm-inline"><span>确认{item.archived_at ? "恢复" : "归档"}？</span><button className="text-button" disabled={busy === item.id} onClick={() => actArchive(item)}>确认</button><button className="text-button muted" onClick={() => setConfirming("")}>取消</button></span> : <button className={`text-button ${item.archived_at ? "restore" : "danger"}`} onClick={() => setConfirming(item.id)}>{item.archived_at ? <RefreshCw size={14} /> : <Trash2 size={14} />}{item.archived_at ? "恢复" : "归档"}</button> : <span className="muted">需要项目令牌</span>}</div></td></tr>)}</tbody></table></div>}<Pagination page={page} pageCount={pageCount} pageSize={pageSize} total={visible.length} onPage={setPage} onPageSize={changePageSize} /></section>
    {editTarget && <ProjectEditModal project={editTarget} saving={editSaving} error={editError} onClose={() => { if (editSaving) return; setEditTarget(null); setEditError(""); }} onSubmit={(payload) => actUpdate(editTarget, payload)} />}
  </div>;
}

/** 「尽调报告任务」窗口的历史报告标签：列表（分页）与只读阅读视图。
 *
 * 阅读视图复用报告交付页的同一套渲染（ReportBlocks），因此历史报告与当前报告
 * 的正文排版不会漂移；差别只在**去掉全部编辑/复核/导出入口**——历史快照不可改。
 * 引用按钮改为就地展示快照里冻结的引文原文：那些来源可能已随材料变化，
 * 不能再打 /api/citations 解析。
 */
function ReportHistoryPanel({ projectId, token }: { projectId: string; token?: string }) {
  const [items, setItems] = useState<ReportSnapshotSummary[]>([]);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<PageSize>(DEFAULT_PAGE_SIZE);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<ReportSnapshot | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [quote, setQuote] = useState<{ label: string; text: string } | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setDetail(null);
    setPage(1);
    api.reportHistory(projectId, token)
      .then((rows) => { if (alive) { setItems(rows); setError(""); } })
      .catch((reason) => { if (alive) setError(reason instanceof Error ? reason.message : "历史报告读取失败"); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [projectId, token]);

  const openDetail = async (snapshotId: string) => {
    setDetailLoading(true);
    try {
      const item = await api.reportHistoryDetail(projectId, snapshotId, token);
      setDetail(item);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史报告打开失败");
    } finally {
      setDetailLoading(false);
    }
  };

  if (detail) {
    const frozenSources = new Map((detail.payload.citations || []).map((item) => [item.id, item]));
    const openFrozenCitation = (citationId: string) => {
      const found = frozenSources.get(citationId);
      setQuote(found
        ? { label: found.label || "来源文件", text: found.quote || "该来源没有留存引文原文。" }
        : { label: "来源待补充", text: "该引用未包含在本快照中。" });
    };
    return <div className="report-history-reader">
      <div className="report-history-reader-head">
        <button className="secondary-button small" onClick={() => { setDetail(null); setQuote(null); }}>
          <ArrowLeft size={14} />返回历史报告列表
        </button>
        <span className="report-history-reader-meta">
          {detail.title} · 生成于 {formatDateTime(detail.created_at)} · 该快照为只读留存
        </span>
      </div>
      <div className="report-history-paper">
        <div className="report-cover">
          <span>历史报告快照</span>
          <h2>{detail.title}</h2>
          <p>生成时间：{formatDateTime(detail.created_at)}</p>
          <p>留存正文 {detail.payload.sections?.length ?? 0} 章 · 引用 {detail.payload.citations?.length ?? 0} 条</p>
          <p>本文件为该次任务完成时的留存版本，不随后续材料或复核决定变动</p>
        </div>
        {(detail.payload.sections || []).map((section, index, all) => {
          const showPart = Boolean(section.part_label) && section.part_label !== all[index - 1]?.part_label;
          const standalone = Boolean(section.part_label) && !section.chapter_label;
          return <Fragment key={section.id}>
            {showPart && !standalone && <h3 className="report-part-heading">{section.part_label}</h3>}
            <section className="report-section" id={`history-${section.section_key}`}>
              <div className="section-heading">
                <h2 className="section-title">
                  {section.chapter_number ? <span className="section-number">{section.chapter_number}</span> : null}
                  <span className="section-name">{standalone ? section.part_label : section.title}</span>
                </h2>
              </div>
              <div className="section-collapsible">
                <div className="section-content"><ReportBlocks section={section} openCitation={openFrozenCitation} /></div>
                {(section.citation_ids || []).length > 0 && <div className="citation-row">
                  {section.citation_ids.map((id) => <button key={id} onClick={() => openFrozenCitation(id)}>{id.slice(-8)}</button>)}
                </div>}
              </div>
            </section>
          </Fragment>;
        })}
        {(detail.payload.open_items || []).length > 0 && <section className="report-section">
          <div className="section-heading"><h2 className="section-title"><span className="section-name">未决事项附录</span></h2></div>
          <div className="section-content"><ul className="report-block-list">
            {(detail.payload.open_items || []).map((line, index) => <li key={index}>{line}</li>)}
          </ul></div>
        </section>}
      </div>
      {quote && <div className="modal-backdrop" role="presentation" onClick={() => setQuote(null)}>
        <section className="trace-modal" role="dialog" aria-modal="true" aria-label="快照引文" onClick={(event) => event.stopPropagation()}>
          <div className="modal-head">
            <div><span>该次任务留存的引文原文</span><h2>{quote.label}</h2></div>
            <button className="icon-button" onClick={() => setQuote(null)} aria-label="关闭引文" title="关闭"><X size={18} /></button>
          </div>
          <p className="report-history-quote">{quote.text}</p>
        </section>
      </div>}
    </div>;
  }

  if (loading) return <div className="loading-panel"><LoaderCircle className="spin" size={21} />正在载入历史报告</div>;
  if (error) return <div className="error-banner"><AlertTriangle size={16} />{error}</div>;
  if (!items.length) return <div className="project-empty">
    <History size={23} />
    <strong>暂无已留存报告</strong>
    <span>任务完成后会自动留存一份只读报告快照。上线前完成的历史任务不在列表中。</span>
  </div>;

  const start = (page - 1) * pageSize;
  const visible = items.slice(start, start + pageSize);
  return <div className="report-history-list">
    {detailLoading && <div className="report-history-loading"><LoaderCircle className="spin" size={16} />正在打开快照</div>}
    <table className="report-history-table">
      <thead><tr><th>报告名称</th><th>生成时间</th><th>详情</th></tr></thead>
      <tbody>
        {visible.map((item) => <tr key={item.id}>
          <td><strong>{item.title}</strong><small>任务 {item.run_id.slice(0, 8)} · 留存正文快照</small></td>
          <td>{formatDateTime(item.created_at)}</td>
          <td><button className="text-button" onClick={() => openDetail(item.id)}><Eye size={14} />查看</button></td>
        </tr>)}
      </tbody>
    </table>
    <Pagination total={items.length} page={page} pageCount={Math.max(1, Math.ceil(items.length / pageSize))}
                pageSize={pageSize} onPage={setPage} onPageSize={(size: PageSize) => { setPageSize(size); setPage(1); }} />
  </div>;
}

/** 「尽调报告任务」窗口：把流程、当前步骤、未决事项提示与指标卡收进弹窗，
 *  并并列提供历史报告查看。默认停在「当前任务」。 */
function ReportTaskModal({ onClose, children, projectId, token }: {
  onClose: () => void; children: ReactNode; projectId: string; token?: string;
}) {
  const [tab, setTab] = useState<"current" | "history">("current");
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  // 焦点约束：打开时把焦点移进弹窗，关闭时还给触发按钮；Tab 在弹窗内循环。
  useEffect(() => {
    previousFocus.current = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.stopPropagation(); onClose(); return; }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      )).filter((item) => !item.hasAttribute("disabled") && item.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      previousFocus.current?.focus?.();
    };
  }, [onClose]);

  return <div className="modal-backdrop report-task-backdrop" role="presentation" onClick={onClose}>
    <section className="report-task-modal" role="dialog" aria-modal="true" aria-labelledby="report-task-title"
             tabIndex={-1} ref={dialogRef} onClick={(event) => event.stopPropagation()}>
      <header className="report-task-head">
        <div><h2 id="report-task-title">尽调报告任务</h2><small>当前任务进度与历史留存报告</small></div>
        <button className="icon-button" onClick={onClose} aria-label="关闭尽调报告任务" title="关闭"><X size={18} /></button>
      </header>
      <div className="report-task-tabs" role="tablist">
        <button role="tab" aria-selected={tab === "current"} className={tab === "current" ? "active" : ""}
                onClick={() => setTab("current")}><SearchCheck size={16} />当前任务</button>
        <button role="tab" aria-selected={tab === "history"} className={tab === "history" ? "active" : ""}
                onClick={() => setTab("history")}><History size={16} />历史报告</button>
      </div>
      <div className="report-task-body">
        {tab === "current" ? children : <ReportHistoryPanel projectId={projectId} token={token} />}
      </div>
    </section>
  </div>;
}

function ProjectDetailView({ project, bondSummary, inventory, checks, risks, tasks, candidates, factGroups, entities, policy, snapshot, reviewSummary, stages, run, nodes, token, onBack, onNavigate, onOpenRisk, onRun, running, modelConfigured }: {
  project: Project; bondSummary: BondSummary | null; inventory: Inventory; checks: CrossCheck[]; risks: Risk[]; tasks: ReviewTask[]; candidates: CandidateFinding[]; factGroups: FactGroup[]; entities: ProjectEntity[]; policy: PolicyInfo | null; snapshot: ProjectProfileSnapshot | null; reviewSummary: ReviewSummary | null; stages: WorkflowStage[]; run: Run | null; nodes: RunNode[]; token?: string; onBack: () => void; onNavigate: (view: View) => void; onOpenRisk: (riskId?: string) => void; onRun: () => void; running: boolean; modelConfigured: boolean;
}) {
  const unresolved = reviewSummary?.total_unique_issues ?? 0;
  const realReference = project.data_class === "real_reference";
  const [taskOpen, setTaskOpen] = useState(false);
  return <div className="project-detail-view">
    <div className="project-detail-toolbar"><button className="secondary-button small" onClick={onBack}><ArrowLeft size={14} />返回项目列表</button><div className="project-detail-actions">{project.archived_at && <span className="archive-banner"><Info size={14} />项目已归档 · 只读</span>}</div></div>
    <header className="project-title-panel"><div className="project-title-main"><span className="project-title-label">项目主体</span><h1>{project.company_name}</h1><div className="project-title-details"><span><small>项目标的</small><strong>{bondSummary?.bondName || project.bond_name || "未填写"}</strong></span><span><small>建项时间</small><strong>{formatDateTime(project.created_at)}</strong></span></div></div><div className="project-title-status"><span className="review-state"><span className="state-dot" />{unresolved} 项待复核</span>{realReference && <span className="reference-badges"><span>真实公开材料</span><span>非评测案例</span></span>}</div></header>
    <button className="secondary-button report-task-button" onClick={() => setTaskOpen(true)}>
      <ClipboardCheck size={17} /><span>尽调报告任务</span>
      <small>{running ? "运行中" : run?.status === "completed" ? (unresolved ? `${unresolved} 项待复核` : "已完成") : run?.status === "failed" ? "上次运行失败" : "未启动"} · 查看进度与历史报告</small>
    </button>
    {taskOpen && <ReportTaskModal projectId={project.id} token={token} onClose={() => setTaskOpen(false)}>
      <Overview project={project} bondSummary={bondSummary} inventory={inventory} checks={checks} risks={risks} tasks={tasks} candidates={candidates} factGroups={factGroups} entities={entities} policy={policy} snapshot={snapshot} reviewSummary={reviewSummary} onNavigate={onNavigate} onOpenRisk={onOpenRisk} onRun={onRun} running={running} modelConfigured={modelConfigured} stages={stages} run={run} nodes={nodes} />
    </ReportTaskModal>}
  </div>;
}

function DueDiligenceView({ project, token, tasks, auditEvents, onBack, onReview, onNavigate, onOpenRisk }: { project: Project; token?: string; tasks: ReviewTask[]; auditEvents: AuditEvent[]; onBack: () => void; onReview: () => void; onNavigate: (view: View) => void; onOpenRisk: (riskId?: string) => void }) {
  const [tab, setTab] = useState<"runs" | "tasks">("runs");
  const [runs, setRuns] = useState<Run[]>([]); const [artifacts, setArtifacts] = useState<Record<string, RunArtifact[]>>({}); const [expanded, setExpanded] = useState<string>(""); const [loading, setLoading] = useState(true); const [error, setError] = useState("");
  const load = useCallback(async () => { try { const rows = await api.projectRuns(project.id, token); setRuns(rows); const expandable = rows.filter((item) => item.id === expanded || item.status === "running" || item.status === "queued").slice(0, 8); const loaded = await Promise.all(expandable.map(async (item) => [item.id, await api.artifacts(item.id, token)] as const)); setArtifacts((current) => ({ ...current, ...Object.fromEntries(loaded) })); setError(""); } catch (reason) { setError(reason instanceof Error ? reason.message : "尽调记录读取失败"); } finally { setLoading(false); } }, [expanded, project.id, token]);
  useEffect(() => { setLoading(true); setRuns([]); setArtifacts({}); load(); }, [load]);
  useEffect(() => { if (!runs.some((item) => item.status === "running" || item.status === "queued")) return; const timer = window.setInterval(load, 2000); return () => window.clearInterval(timer); }, [load, runs]);
  const taskLogs = (task: ReviewTask) => auditEvents.filter((item) => item.entity_type === "review_task" && item.entity_id === task.id);
  return <div className="view-content due-diligence-view"><div className="page-heading"><div><span className="eyebrow">Due diligence records</span><div className="page-title-row"><h1>尽调任务与成果</h1><HelpTip label="尽调任务与成果说明">当前报告和风险清单始终指向项目最新版本，历史版本在报告交付页的版本记录里查看。</HelpTip></div></div><button className="secondary-button" onClick={onBack}><ArrowDown size={15} className="rotate-90" />返回项目详情</button></div><div className="due-diligence-tabs"><button className={tab === "runs" ? "active" : ""} onClick={() => setTab("runs")}><SearchCheck size={16} />Agent 运行记录 <span>{runs.length}</span></button><button className={tab === "tasks" ? "active" : ""} onClick={() => setTab("tasks")}><ClipboardCheck size={16} />人工尽调任务 <span>{tasks.length}</span></button></div>{error && <div className="error-banner"><AlertTriangle size={16} />{error}</div>}{loading ? <div className="loading-panel"><LoaderCircle className="spin" size={21} />正在载入尽调记录</div> : tab === "runs" ? <section className="panel diligence-record-panel"><div className="panel-title"><div><h2>Agent 运行记录</h2><HelpTip label="Agent 运行记录说明">按开始时间倒序排列；展开单次运行可查看节点摘要、模型成本与当次产物。</HelpTip></div><button className="text-button" onClick={() => onNavigate("report")}><FileText size={14} />查看当前报告</button></div>{!runs.length ? <div className="project-empty"><SearchCheck size={23} /><strong>暂无 Agent 运行记录</strong><span>完成一次债券尽调后，运行进度和成果会显示在这里。</span></div> : <div className="diligence-run-list">{runs.map((item) => <article className={`diligence-run run-${item.status}`} key={item.id}><button className="diligence-run-head" onClick={() => setExpanded((value) => value === item.id ? "" : item.id)}><span className="run-chevron">{expanded === item.id ? <ChevronDown size={16} /> : <ChevronRight size={16} />}</span><span className="run-status-dot" /><div><strong>{item.message || item.id}</strong><small>{formatDateTime(item.started_at)}{item.finished_at ? ` · 结束 ${formatDateTime(item.finished_at)}` : " · 进行中"}</small></div><span className="run-progress-label">{item.progress}%</span><span className={`project-status status-${item.status}`}>{item.status === "completed" ? "已完成" : item.status === "running" || item.status === "queued" ? "进行中" : item.status === "cancelled" ? "已取消" : "失败"}</span></button>{expanded === item.id && <div className="diligence-run-detail"><div className="run-detail-grid"><div><span>当前节点</span><strong>{item.current_node || "—"}</strong></div><div><span>模型成本</span><strong>¥{(item.cost || 0).toFixed(3)}</strong></div><div><span>输入 / 输出</span><strong>{item.input_tokens || 0} / {item.output_tokens || 0} tokens</strong></div></div>{item.error && <div className="run-error"><AlertTriangle size={14} />{item.error}</div>}{artifacts[item.id]?.length ? <div className="artifact-list"><strong>当次产物记录</strong>{artifacts[item.id].map((artifact) => <div key={artifact.id}><span>{artifact.node_key}</span><small>{artifact.artifact_type} · {artifact.status} · {artifact.artifact_ids.length} 项</small></div>)}</div> : <div className="sub-empty">暂无已保存产物记录</div>}</div>}</article>)}</div>}</section> : <section className="panel diligence-record-panel"><div className="panel-title"><div><h2>人工尽调任务</h2><HelpTip label="人工尽调任务说明">集中列出需要人工处理的补充材料、事实核实与风险复核事项，含负责人、截止日期和所需证据。</HelpTip></div><button className="secondary-button small" onClick={onReview}><GitCompareArrows size={14} />进入复核中心</button></div>{!tasks.length ? <div className="project-empty"><ClipboardCheck size={23} /><strong>暂无人工尽调任务</strong><span>Agent 运行后会在此记录需要人工处理的事项。</span></div> : <div className="diligence-task-list">{tasks.map((task) => <details className={`diligence-task task-${task.status}`} key={task.id}><summary><SeverityBadge level={task.priority} /><span><strong>{task.title}</strong><small>{task.description}</small></span><span className="task-status">{task.status === "open" ? "开放" : task.status === "resolved" ? "已解决" : "已豁免"}</span></summary><div className="diligence-task-detail"><div className="task-detail-meta"><span>负责人 <strong>{task.assignee || "未分配"}</strong></span><span>截止日期 <strong>{task.due_date || "未设置"}</strong></span><span>所需证据 <strong>{task.source_requirement || task.required_evidence || "人工核验记录"}</strong></span></div><p>{task.resolution_note || "尚未填写处理说明"}</p>{taskLogs(task).length > 0 && <div className="task-audit-list"><strong>处理记录</strong>{taskLogs(task).map((event) => <div key={event.id}><span>{formatDateTime(event.created_at)}</span><span>{event.actor}</span><span>{event.action}</span></div>)}</div>}</div></details>)}</div>}</section>}<div className="diligence-outcome-links"><button className="panel-link" onClick={() => onNavigate("report")}><FileText size={17} /><span><strong>当前尽调报告</strong><small>查看项目最新报告版本</small></span><ArrowRight size={15} /></button><button className="panel-link" onClick={() => onNavigate("solution")}><ShieldAlert size={17} /><span><strong>当前风险清单</strong><small>查看已确认风险与缓释方案</small></span><ArrowRight size={15} /></button><button className="panel-link" onClick={() => onNavigate("evidence")}><FileCheck2 size={17} /><span><strong>当前材料</strong><small>查看项目材料与证据</small></span><ArrowRight size={15} /></button></div></div>;
}

function App() {
  const [active, setActive] = useState<View>(() => {
    const reference = JSON.parse(localStorage.getItem("yandan-current") || "{\"id\":\"demo-bond-huachen\"}") as ProjectRef;
    if (!isProjectVisible(reference)) return "projects";
    const stored = localStorage.getItem("yandan-active-view");
    return stored === "project-detail" || stored === "due-diligence" || stored === "solution" ? stored : "projects";
  }); const [current, setCurrent] = useState<ProjectRef>(() => {
    const reference = JSON.parse(localStorage.getItem("yandan-current") || "{\"id\":\"demo-bond-huachen\"}") as ProjectRef;
    return isProjectVisible(reference) ? reference : { id: "demo-bond-huachen" };
  });
  const [riskNavigation, setRiskNavigation] = useState<RiskNavigation | null>(() => {
    try {
      const stored = JSON.parse(sessionStorage.getItem("yandan-risk-navigation") || "null") as RiskNavigation | null;
      return stored?.projectId === current.id ? stored : null;
    } catch { return null; }
  });
  const pendingRiskScrollRestore = useRef<number | null>(null);
  const [demoProjects, setDemoProjects] = useState<ProjectSummary[]>([]); const [realProjects, setRealProjects] = useState<ProjectSummary[]>([]); const [userProjects, setUserProjects] = useState<ProjectSummary[]>([]); const [saved, setSaved] = useState<ProjectRef[]>(() => (JSON.parse(localStorage.getItem("yandan-projects") || "[]") as ProjectRef[]).filter((item) => !isPurgedProject(item.id)));
  const refreshSerial = useRef(0);
  const [project, setProject] = useState<Project | null>(null); const [bondSummary, setBondSummary] = useState<BondSummary | null>(null); const [inventory, setInventory] = useState<Inventory>(EMPTY_INVENTORY); const [facts, setFacts] = useState<Fact[]>([]); const [checks, setChecks] = useState<CrossCheck[]>([]); const [risks, setRisks] = useState<Risk[]>([]); const [sections, setSections] = useState<ReportSection[]>([]);
  const [factGroups, setFactGroups] = useState<FactGroup[]>([]); const [metrics, setMetrics] = useState<FinancialMetric[]>([]); const [policy, setPolicy] = useState<PolicyInfo | null>(null); const [tasks, setTasks] = useState<ReviewTask[]>([]); const [candidates, setCandidates] = useState<CandidateFinding[]>([]); const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [entities, setEntities] = useState<ProjectEntity[]>([]); const [tieOuts, setTieOuts] = useState<FinancialTieOut[]>([]); const [reportQuality, setReportQuality] = useState<ReportQuality | null>(null);
  const [profiles, setProfiles] = useState<EntityProfileSummary[]>([]); const [profilesError, setProfilesError] = useState(""); const [snapshot, setSnapshot] = useState<ProjectProfileSnapshot | null>(null); const [reviewSummary, setReviewSummary] = useState<ReviewSummary | null>(null);
  const [health, setHealth] = useState<Health>({ status: "loading", modelConfigured: false, model: "deepseek-v4-flash", version: "" }); const [run, setRun] = useState<Run | null>(null); const [nodes, setNodes] = useState<RunNode[]>([]); const [citation, setCitation] = useState<Citation | null>(null); const [citationFocus, setCitationFocus] = useState("");
  const [menuOpen, setMenuOpen] = useState(false); const [projectMenu, setProjectMenu] = useState(false); const [creating, setCreating] = useState(false); const [error, setError] = useState("");
  const [creatingProfileId, setCreatingProfileId] = useState("");
  const token = current.token;

  const refresh = useCallback(async () => {
    const refreshId = ++refreshSerial.current;
    const projectRef = current;
    const [p, inv, bs, fs, groups, metricRows, policyInfo, cs, rs, reviewTaskRows, candidateRows, auditRows, report, entityRows, tieOutRows, quality, latest] = await Promise.all([
      api.project(projectRef.id, projectRef.token), api.inventory(projectRef.id, projectRef.token), api.project(projectRef.id, projectRef.token).then((item) => item.project_type === "bond_guarantee" ? api.bondSummary(projectRef.id, projectRef.token) : Promise.resolve(null)), api.facts(projectRef.id, projectRef.token),
      api.factGroups(projectRef.id, projectRef.token), api.metrics(projectRef.id, projectRef.token), api.policy(projectRef.id, projectRef.token),
      api.checks(projectRef.id, projectRef.token), api.risks(projectRef.id, projectRef.token), api.reviewTasks(projectRef.id, projectRef.token),
      api.candidates(projectRef.id, projectRef.token), api.auditEvents(projectRef.id, projectRef.token), api.report(projectRef.id, projectRef.token),
      api.entities(projectRef.id, projectRef.token), api.tieOuts(projectRef.id, projectRef.token), api.reportQuality(projectRef.id, projectRef.token), api.latestRun(projectRef.id, projectRef.token),
    ]);
    if (refreshId !== refreshSerial.current) return;
    setProject(p); setBondSummary(bs); setInventory(inv); setFacts(fs); setFactGroups(groups); setMetrics(metricRows); setPolicy(policyInfo);
    setChecks(cs); setRisks(rs); setTasks(reviewTaskRows); setCandidates(candidateRows); setAuditEvents(auditRows); setSections(report);
    setEntities(entityRows); setTieOuts(tieOutRows); setReportQuality(quality);
    setRun(latest.run); setNodes(latest.nodes);
    const [snapshotResult, reviewResult] = await Promise.allSettled([api.profileSnapshot(projectRef.id, projectRef.token), api.reviewSummary(projectRef.id, projectRef.token)]);
    if (refreshId !== refreshSerial.current) return;
    setSnapshot(snapshotResult.status === "fulfilled" ? snapshotResult.value : null);
    setReviewSummary(reviewResult.status === "fulfilled" ? reviewResult.value : null);
  }, [current]);
  useEffect(() => { Promise.allSettled([api.projects("fictional_demo"), api.projects("real_reference"), api.projects("user_project"), api.health()]).then(([projects, references, users, status]) => { if (projects.status === "fulfilled") setDemoProjects(projects.value); if (references.status === "fulfilled") setRealProjects(references.value); if (users.status === "fulfilled") setUserProjects(users.value); if (status.status === "fulfilled") setHealth(status.value); const failure = [projects, references, status].find((item) => item.status === "rejected"); if (failure?.status === "rejected") setError(failure.reason instanceof Error ? failure.reason.message : "项目列表读取失败"); }); }, []);
  useEffect(() => { api.entityProfiles().then((rows) => { setProfiles(rows); setProfilesError(""); }).catch((reason) => { setProfiles([]); setProfilesError(reason instanceof Error ? reason.message : "主体档案功能不可用"); }); }, []);
  useEffect(() => { localStorage.setItem("yandan-active-view", active); }, [active]);
  // 已删除的用户贷款担保项目不应再留在浏览器本地缓存的已保存列表中。
  useEffect(() => { localStorage.setItem("yandan-projects", JSON.stringify(saved)); }, [saved]);
  useEffect(() => { setProject(null); setRun(null); setNodes([]); if (riskNavigation?.projectId && riskNavigation.projectId !== current.id) { setRiskNavigation(null); sessionStorage.removeItem("yandan-risk-navigation"); } refresh().catch((reason) => setError(reason.message)); localStorage.setItem("yandan-current", JSON.stringify(current)); }, [current, refresh]);

  useEffect(() => {
    if (active === "solution" || pendingRiskScrollRestore.current === null) return;
    const scrollY = pendingRiskScrollRestore.current;
    pendingRiskScrollRestore.current = null;
    const frame = window.requestAnimationFrame(() => window.scrollTo({ top: scrollY, behavior: "instant" as ScrollBehavior }));
    return () => window.cancelAnimationFrame(frame);
  }, [active]);

  const openRisks = (riskId?: string) => {
    const returnView = active === "due-diligence" ? "due-diligence" : "project-detail";
    const navigation: RiskNavigation = { projectId: current.id, returnView, scrollY: window.scrollY, ...(riskId ? { riskId } : {}) };
    setRiskNavigation(navigation);
    sessionStorage.setItem("yandan-risk-navigation", JSON.stringify(navigation));
    setActive("solution");
  };
  const returnFromRisks = () => {
    const navigation = riskNavigation?.projectId === current.id ? riskNavigation : null;
    sessionStorage.removeItem("yandan-risk-navigation");
    setRiskNavigation(null);
    if (navigation) pendingRiskScrollRestore.current = navigation.scrollY;
    setActive(navigation?.returnView || "project-detail");
  };
  const navigateView = (view: View) => { if (view === "solution") openRisks(); else setActive(view); };
  const selectProject = (reference: ProjectRef) => { sessionStorage.removeItem("yandan-risk-navigation"); setRiskNavigation(null); setProject(null); setCurrent(reference); setActive("project-detail"); setProjectMenu(false); setMenuOpen(false); };
  const onCreated = (reference: ProjectRef) => { const next = [...saved.filter((item) => item.id !== reference.id), reference]; setSaved(next); localStorage.setItem("yandan-projects", JSON.stringify(next)); api.projects("user_project").then(setUserProjects).catch(() => undefined); setCreating(false); setCreatingProfileId(""); setProject(null); setCurrent(reference); setActive("evidence"); };
  const projectToken = (id: string) => saved.find((item) => item.id === id)?.token;
  const archiveFromList = async (item: ProjectSummary) => { await api.archiveProject(item.id, projectToken(item.id)); const rows = await api.projects("user_project"); setUserProjects(rows); if (current.id === item.id) { const next = await api.project(item.id, token); setProject(next); } };
  const restoreFromList = async (item: ProjectSummary) => { await api.restoreProject(item.id, projectToken(item.id)); const rows = await api.projects("user_project"); setUserProjects(rows); };
  /** 保存项目基础信息：刷新列表（保留筛选与分页）与当前项目视图；PATCH 已回传最新详情。 */
  const updateFromList = async (item: ProjectSummary, payload: ProjectUpdatePayload) => {
    const updated = await api.updateProject(item.id, payload, projectToken(item.id));
    const rows = await api.projects("user_project");
    setUserProjects(rows);
    if (current.id === item.id) setProject(updated);
  };
  const followRun = async (runId: string) => { await api.streamRun(runId, token, (next) => { setRun(next); if (["completed", "failed", "cancelled", "model_required"].includes(next.status)) { api.nodes(next.id, token).then(setNodes); refresh(); } }); };
  const start = async () => { setError(""); try { const result = await api.startRun(current.id, token); await followRun(result.id); } catch (reason) { setError(reason instanceof Error ? reason.message : "启动失败"); } };
  const retry = async () => { if (!run) return; try { const result = await api.retryRun(run.id, token); await followRun(result.id); } catch (reason) { setError(reason instanceof Error ? reason.message : "重试失败"); } };
  const openSource = async (id: string, focus?: string) => { const citationId = id.startsWith("c-") ? id : `c-${id}`; try { const next = await api.citation(citationId, token); setCitationFocus(focus || ""); setCitation(next); } catch (reason) { setError(reason instanceof Error ? reason.message : "证据读取失败"); } };
  const running = run?.status === "running" || run?.status === "queued";
  const hasOpenMajor = factGroups.some((item) => item.status === "conflicted")
    || candidates.some((item) => item.status === "proposed" && item.severity === "high")
    || tasks.some((item) => item.status === "open" && item.priority === "high");
const MODULE_NAMES: Record<string, string> = { profiles: "发行人主体档案库", projects: "债券项目驾驶舱", "project-detail": "项目详情", "due-diligence": "尽调任务", evidence: "材料与证据", analysis: "Agent 分析", review: "人工复核", report: "报告交付", solution: "正式风险与缓释", tables: "表格核对", prompts: "报告 Prompt" };
  const stages = useMemo(() => deriveWorkflowStages(inventory, run, factGroups, candidates, tasks, sections, reportQuality, reviewSummary), [inventory, run, factGroups, candidates, tasks, sections, reportQuality, reviewSummary]);
  const currentStage = stages.find((stage) => stage.status === "current" || stage.status === "blocked") || stages[3];
  const readOnly = project?.data_class === "real_reference" || Boolean(project?.archived_at);
  const primaryAction = readOnly ? { label: "查看材料", view: "evidence" as View, run: false }
    : currentStage.key === "materials" ? { label: "上传材料", view: "evidence" as View, run: false }
    : currentStage.key === "analysis" && currentStage.status === "current" ? { label: running ? "分析中" : "运行 Agent", view: "analysis" as View, run: true }
    : currentStage.key === "analysis" ? { label: "查看失败节点", view: "analysis" as View, run: false }
    : currentStage.key === "review" ? { label: "处理复核", view: "review" as View, run: false }
    : { label: "检查报告", view: "report" as View, run: false };
  const executePrimaryAction = () => { if (primaryAction.run && !running) start(); else { setActive(primaryAction.view); setMenuOpen(false); } };

  const page = useMemo(() => {
    if (active === "profiles") return <EntityProfileWorkspace onCreateProject={(profile) => { setCreatingProfileId(profile.id); setCreating(true); }} />;
    if (active === "projects") return <ProjectList projects={userProjects} saved={saved} onSelect={(item) => { setCurrent({ id: item.id, token: projectToken(item.id) }); setProject(null); setActive("project-detail"); }} onCreate={() => { setCreatingProfileId(""); setCreating(true); }} onArchive={archiveFromList} onRestore={restoreFromList} onUpdate={updateFromList} />;
    if (!project) return null;
    if (active === "project-detail") return <ProjectDetailView project={project} bondSummary={bondSummary} inventory={inventory} checks={checks} risks={risks} tasks={tasks} candidates={candidates} factGroups={factGroups} entities={entities} policy={policy} snapshot={snapshot} reviewSummary={reviewSummary} stages={stages} run={run} nodes={nodes} token={token} onBack={() => setActive("projects")} onNavigate={navigateView} onOpenRisk={openRisks} onRun={start} running={running} modelConfigured={health.modelConfigured || current.id.startsWith("demo-")} />;
    if (active === "due-diligence") return <DueDiligenceView project={project} token={token} tasks={tasks} auditEvents={auditEvents} onBack={() => setActive("project-detail")} onReview={() => setActive("review")} onNavigate={navigateView} onOpenRisk={openRisks} />;
    if (active === "prompts") return <PromptWorkspace project={project} token={token} />;
    if (active === "evidence") return <Documents project={project} inventory={inventory} facts={facts} token={token} editable={project.data_class === "user_project" && !project.archived_at} snapshot={snapshot} running={running} limits={health.uploadLimits} health={health} onRun={start} onUpload={async (files) => { try { await api.upload(project.id, files, token); } finally { await refresh(); } }} refresh={refresh} openCitation={openSource} />;
    if (active === "tables") return <TableReview projectId={project.id} token={token} refresh={refresh} openCitation={openSource} />;
    if (active === "analysis") return checks.length || run ? <Checks projectId={project.id} token={token} checks={checks} metrics={metrics} tieOuts={tieOuts} policy={policy} openCitation={openSource} run={run} nodes={nodes} onRetry={retry} evidenceOpen={Boolean(citation)} /> : <EmptyState onRun={start} running={running} hasDocuments={inventory.documents.length > 0} modelConfigured={health.modelConfigured} />;
    if (active === "review") return <ReviewCenter projectId={project.id} token={token} factGroups={factGroups} candidates={candidates} tasks={tasks} auditEvents={auditEvents} reviewSummary={reviewSummary} snapshot={snapshot} checks={checks} policy={policy} refresh={refresh} openCitation={openSource} readOnly={readOnly} />;
    if (active === "solution") return <Risks risks={risks} openCitation={openSource} onBack={returnFromRisks} targetRiskId={riskNavigation?.projectId === current.id ? riskNavigation.riskId : undefined} />;
    return sections.length ? <Report project={project} bondSummary={bondSummary} sections={sections} quality={reportQuality} snapshot={snapshot} token={token} openCitation={openSource} refresh={refresh} hasOpenMajor={hasOpenMajor} /> : <EmptyState onRun={start} running={running} hasDocuments={inventory.documents.length > 0} modelConfigured={health.modelConfigured} />;
  }, [active, project, bondSummary, inventory, facts, factGroups, metrics, tieOuts, entities, policy, checks, risks, tasks, candidates, auditEvents, sections, reportQuality, running, health.modelConfigured, token, refresh, hasOpenMajor, stages, run, nodes, snapshot, reviewSummary, userProjects, saved, navigateView, openRisks, returnFromRisks, riskNavigation, current.id, citation]);

  return <div className="app-shell"><aside className={`sidebar ${menuOpen ? "mobile-open" : ""}`}><div className="brand"><div className="brand-mark"><SearchCheck size={22} /></div><div><strong>研担</strong><span>债券发行融资担保尽调</span></div><button className="mobile-close" onClick={() => setMenuOpen(false)}><X size={20} /></button></div>
    <ProjectSwitcher open={projectMenu} current={current} project={project} demoProjects={demoProjects} realProjects={realProjects} savedProjects={saved} onOpenChange={setProjectMenu} onSelect={selectProject} onCreate={() => { setCreatingProfileId(""); setCreating(true); }} />
    <div className="workflow-nav-wrap"><button className={active === "profiles" ? "dashboard-link active" : "dashboard-link"} onClick={() => { setActive("profiles"); setMenuOpen(false); }}><UsersRound size={18} /><span><strong>发行人主体档案库</strong></span></button><button className={active === "projects" || active === "project-detail" || active === "due-diligence" ? "dashboard-link active" : "dashboard-link"} onClick={() => { setActive("projects"); setMenuOpen(false); }}><LayoutDashboard size={18} /><span><strong>债券项目驾驶舱</strong></span></button><span className="workflow-caption">债券担保尽调流程</span><WorkflowRail stages={stages} active={active} navigate={(view) => { navigateView(view); setMenuOpen(false); }} /><button className={active === "prompts" ? "prompt-manager-link active" : "prompt-manager-link"} onClick={() => { setActive("prompts"); setMenuOpen(false); }}><FileCog size={16} /><span><strong>报告 Prompt</strong></span></button></div>
    <div className="sidebar-bottom"><div className="mode-status"><span className={`pulse ${health.modelConfigured ? "" : "offline"}`} /><div><strong>{health.modelConfigured ? health.model : "确定性演示模式"}</strong><small>{health.modelConfigured ? "模型已配置" : "自定义项目需模型"}</small></div></div><p>仅上传公开或虚构材料 · 24小时删除</p></div></aside>
    <main className="main-area"><header className="topbar"><div className="topbar-brand"><div className="topbar-brand-mark"><SearchCheck size={21} /></div><strong>研担</strong><i className="topbar-divider" aria-hidden="true" /><span>担保业务智能系统</span></div><i className="topbar-divider topbar-divider-end" aria-hidden="true" /><button className="menu-button" onClick={() => setMenuOpen(true)} title="打开导航"><Menu size={20} /></button><nav className="topbar-crumbs" aria-label="当前位置"><strong className="topbar-module">{MODULE_NAMES[active] || currentStage?.label || "担保业务智能系统"}</strong>{active !== "profiles" && active !== "projects" && <><i className="topbar-divider" aria-hidden="true" /><span className="topbar-project" title={project?.company_name || "载入中"}>{project?.company_name || "载入中"}</span></>}</nav>{active !== "profiles" && active !== "projects" && <div className="top-actions"><span className={`draft-badge stage-${currentStage.status}`}>{active === "prompts" ? "项目级配置" : currentStage.status === "completed" ? "流程已完成" : currentStage.status === "blocked" ? "存在阻塞" : currentStage.reason}</span><button className="primary-button context-action" onClick={executePrimaryAction} disabled={running && primaryAction.run || !project}>{running && primaryAction.run ? <LoaderCircle className="spin" size={16} /> : <ArrowRight size={16} />}{primaryAction.label}</button></div>}</header>
      {active !== "projects" && active !== "profiles" && run && running && <div className="run-progress"><div className="progress-meta"><span><LoaderCircle className="spin" size={15} />{run.message}</span><strong>{run.progress}%</strong><button onClick={() => api.cancelRun(run.id, token)} title="取消任务"><Square size={14} fill="currentColor" /></button></div><div><span style={{ width: `${run.progress}%` }} /></div></div>}
      {active !== "projects" && active !== "profiles" && run && !running && run.status !== "completed" && <div className="run-result"><div><LockKeyhole size={17} /><strong>{run.status === "model_required" ? "需要配置模型" : run.message}</strong><span>{run.error}</span></div><button className="secondary-button" onClick={retry}><RotateCcw size={15} />重试</button>{nodes.length > 0 && <small>{nodes.filter((node) => node.status === "completed").length}/{nodes.length} 个节点完成</small>}</div>}
      {error && <div className="error-banner"><AlertTriangle size={16} />{error}<button onClick={() => setError("")}><X size={15} /></button></div>}
      {active === "profiles" || active === "projects" ? page : !project ? error ? <div className="loading-page access-error-state"><AlertTriangle size={24} /><strong>{error}</strong><button className="secondary-button small" onClick={() => { setError(""); setActive("projects"); }}>返回项目列表</button></div> : <div className="loading-page"><LoaderCircle className="spin" size={26} />正在载入项目</div> : page}</main>
    <EvidenceDrawer citation={citation} focus={citationFocus} close={() => { setCitation(null); setCitationFocus(""); }} />{menuOpen && <button className="mobile-overlay" onClick={() => setMenuOpen(false)} />}{creating && <ProjectIntakeWizard profiles={profiles} profilesError={profilesError} initialIssuerProfileId={creatingProfileId} limits={health.uploadLimits} close={() => { setCreating(false); setCreatingProfileId(""); }} created={onCreated} />}
  </div>;
}

export default App;
