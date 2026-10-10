import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, FileSearch, LoaderCircle, Upload, X } from "lucide-react";
import { ErrorState, StatusBadge } from "../../../components/common";
import type {
  EntityProfileDetail,
  ProfileCandidateCorrection,
  ProfileImportCandidate,
  ProfileImportJob,
  ProfileRepository,
} from "../contracts";
import { displayFactValue } from "./profilePresentation";

const terminalStatuses = new Set(["needs_mapping", "ready_to_commit", "failed", "committed"]);
const statusLabels: Record<ProfileImportJob["status"], string> = {
  queued: "等待解析",
  parsing: "正在解析",
  needs_mapping: "需要人工处理",
  ready_to_commit: "候选待提交",
  failed: "解析失败",
  committed: "已生成新版本",
};

function candidateHasEvidence(candidate: ProfileImportCandidate) {
  return candidate.source_ids.length > 0 && !candidate.issue_codes.includes("missing_evidence");
}

export function ProfileImportDrawer({
  profileId,
  repository,
  onClose,
  onCommitted,
}: {
  profileId: string;
  repository: ProfileRepository;
  onClose: () => void;
  onCommitted: (profile: EntityProfileDetail) => void;
}) {
  const [packageKind, setPackageKind] = useState("financial");
  const [baselineDate, setBaselineDate] = useState("");
  const [period, setPeriod] = useState("");
  const [scope, setScope] = useState("consolidated");
  const [structuredFile, setStructuredFile] = useState<File | null>(null);
  const [evidenceFiles, setEvidenceFiles] = useState<File[]>([]);
  const [job, setJob] = useState<ProfileImportJob | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [corrections, setCorrections] = useState<Record<string, ProfileCandidateCorrection>>({});
  const [changeSummary, setChangeSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!job || terminalStatuses.has(job.status)) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await repository.getImport(job.id);
        setJob(next);
        if (terminalStatuses.has(next.status)) {
          setSelected(new Set(next.candidates.filter(candidateHasEvidence).map((item) => item.id)));
        }
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "导入状态读取失败");
      }
    }, 900);
    return () => window.clearTimeout(timer);
  }, [job, repository]);

  const selectedCandidates = useMemo(
    () => (job?.candidates || []).filter((candidate) => selected.has(candidate.id)),
    [job, selected],
  );
  const canCommit = Boolean(
    job
    && ["needs_mapping", "ready_to_commit"].includes(job.status)
    && selectedCandidates.length
    && selectedCandidates.every(candidateHasEvidence),
  );

  const start = async () => {
    if (!structuredFile) return;
    setBusy(true);
    setError("");
    try {
      const next = await repository.startImport({
        target_profile_id: profileId,
        package_kind: packageKind,
        baseline_date: baselineDate || null,
        period: period || null,
        scope,
        structured_file: structuredFile,
        evidence_files: evidenceFiles,
      });
      setJob(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "主体资料导入失败");
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    if (!job || !canCommit) return;
    setBusy(true);
    setError("");
    try {
      const profile = await repository.commitImport(job.id, {
        selected_candidate_ids: selectedCandidates.map((candidate) => candidate.id),
        corrections: Object.values(corrections).filter((item) => selected.has(item.candidate_id)),
        change_summary: changeSummary.trim() || `导入${packageKind}资料包`,
      });
      setJob({ ...job, status: "committed", committed_version: profile.version, progress: 100 });
      onCommitted(profile);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "候选事实提交失败");
    } finally {
      setBusy(false);
    }
  };

  const updateCorrection = (candidate: ProfileImportCandidate, patch: Partial<ProfileCandidateCorrection>) => {
    setCorrections((current) => ({
      ...current,
      [candidate.id]: { ...(current[candidate.id] || {}), ...patch, candidate_id: candidate.id },
    }));
  };

  return <div className="ep-drawer-backdrop"><section className="ep-import-drawer" role="dialog" aria-modal="true" aria-labelledby="ep-import-title">
    <header><div><span>主体资料版本化导入</span><h2 id="ep-import-title">导入主体档案材料</h2></div><button className="icon-button" aria-label="关闭主体资料导入" onClick={onClose}><X size={18} /></button></header>
    <div className="ep-import-body">
      {!job ? <section className="ep-import-form">
        <div className="ep-form-grid">
          <label>资料包类型<select value={packageKind} onChange={(event) => setPackageKind(event.target.value)}><option value="identity">工商与身份</option><option value="ownership">股权与控制</option><option value="financial">财务报表</option><option value="debt">债务与或有事项</option><option value="operations">经营及集中度</option><option value="legal">法律与合规</option></select></label>
          <label>资料基准日<input type="date" value={baselineDate} onChange={(event) => setBaselineDate(event.target.value)} /></label>
          <label>财务期间<input value={period} onChange={(event) => setPeriod(event.target.value)} placeholder="例如：2025年度" /></label>
          <label>数据口径<select value={scope} onChange={(event) => setScope(event.target.value)}><option value="consolidated">合并口径</option><option value="standalone">母公司口径</option><option value="entity">主体口径</option></select></label>
        </div>
        <label className="ep-file-field"><span>结构化主数据（必填）</span><small>支持 XLSX 或 JSON；只负责提供候选值，不能代替原始证据。</small><input aria-label="结构化主数据" type="file" accept=".xlsx,.json" onChange={(event) => setStructuredFile(event.target.files?.[0] || null)} />{structuredFile && <b>{structuredFile.name}</b>}</label>
        <label className="ep-file-field"><span>证据文件</span><small>支持 PDF、DOCX 或 XLSX；事实必须能够定位到原文。</small><input aria-label="主体证据文件" type="file" multiple accept=".pdf,.docx,.xlsx" onChange={(event) => setEvidenceFiles(Array.from(event.target.files || []))} />{!!evidenceFiles.length && <b>{evidenceFiles.map((file) => file.name).join("、")}</b>}</label>
      </section> : <>
        <section className="ep-import-progress"><div><FileSearch size={18} /><span><strong>{statusLabels[job.status]}</strong><small>{job.files.length} 份文件 · {job.candidates.length} 项候选</small></span><b>{job.progress}%</b></div><i><span style={{ width: `${job.progress}%` }} /></i></section>
        <section className="ep-import-files">{job.files.map((file) => <div key={file.id}><span>{file.filename}</span><StatusBadge tone={file.status === "parsed" ? "success" : file.status === "failed" ? "danger" : "info"}>{file.status === "parsed" ? "已解析" : file.status === "failed" ? "失败" : "处理中"}</StatusBadge><small>{file.parse_quality == null ? "质量待评估" : `解析质量 ${Math.round(file.parse_quality * 100)}%`}</small></div>)}</section>
        {!!job.issues.length && <section className="ep-import-issues">{job.issues.map((issue, index) => <p className={issue.severity} key={`${issue.code}-${index}`}><AlertTriangle size={15} />{issue.message}</p>)}</section>}
        {terminalStatuses.has(job.status) && job.status !== "failed" && job.status !== "committed" && <section className="ep-candidate-list">
          <div className="ep-candidate-heading"><div><h3>候选事实审阅</h3><p>仅显式勾选且具有有效来源的候选会进入新版本。</p></div><span>{selectedCandidates.length}/{job.candidates.length} 已选择</span></div>
          {job.candidates.map((candidate) => { const correction = corrections[candidate.id]; const supported = candidateHasEvidence(candidate); return <article className={!supported ? "unsupported" : ""} key={candidate.id}>
            <label className="ep-candidate-check"><input type="checkbox" checked={selected.has(candidate.id)} disabled={!supported} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(candidate.id); else next.delete(candidate.id); return next; })} /><span><strong>{candidate.name}</strong><small>{candidate.fact_key} · 置信度 {Math.round(candidate.confidence * 100)}%</small></span></label>
            <div className="ep-candidate-values"><label>规范值<input value={correction?.text_value ?? candidate.text_value ?? ""} placeholder={candidate.value == null ? "文本值" : String(candidate.value)} onChange={(event) => updateCorrection(candidate, { text_value: event.target.value })} /></label><label>期间<input value={correction?.period ?? candidate.period ?? ""} onChange={(event) => updateCorrection(candidate, { period: event.target.value })} /></label><label>口径<select value={correction?.scope ?? candidate.scope} onChange={(event) => updateCorrection(candidate, { scope: event.target.value })}><option value="consolidated">合并</option><option value="standalone">母公司</option><option value="entity">主体</option></select></label></div>
            <div className="ep-candidate-evidence"><FileSearch size={15} /><span><b>{candidate.source_label}</b>{candidate.locator} · {candidate.quote || "无原文摘录"}</span></div>
            {!supported && <p className="ep-candidate-warning"><AlertTriangle size={14} />无可靠证据，不能提交：{displayFactValue(candidate.value, candidate.text_value, candidate.unit)}</p>}
          </article>; })}
          <label className="ep-change-summary">版本变更说明<textarea value={changeSummary} onChange={(event) => setChangeSummary(event.target.value)} placeholder="例如：更新2025年度审计数据及当前债务结构" /></label>
        </section>}
        {job.status === "committed" && <div className="ep-import-complete"><Check size={22} /><strong>已生成主体档案 v{job.committed_version}</strong><span>历史版本保持不变，引用该主体的既有项目不会被静默刷新。</span></div>}
      </>}
      {error && <ErrorState message={error} />}
    </div>
    <footer><button className="secondary-button" onClick={onClose}>{job?.status === "committed" ? "完成" : "取消"}</button>{!job ? <button className="primary-button" disabled={!structuredFile || busy} onClick={start}>{busy ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}开始解析</button> : !terminalStatuses.has(job.status) ? <span><LoaderCircle className="spin" size={15} />解析完成后进入人工审阅</span> : job.status !== "committed" && job.status !== "failed" ? <button className="primary-button" disabled={!canCommit || busy} onClick={commit}>{busy ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}提交为新版本</button> : null}</footer>
  </section></div>;
}
