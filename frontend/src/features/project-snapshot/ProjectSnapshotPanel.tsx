import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Building2, CheckCircle2, History, MapPin, RefreshCw, ShieldCheck, UserRound, UsersRound, X } from "lucide-react";
import { ErrorState, LoadingState, StatusBadge } from "../../components/common";
import type { ProfileRepository, ProjectProfileSnapshot, ProjectProfileSnapshotStatus } from "../entity-profiles/contracts";
import { ProfileApiError, profileRepository } from "../entity-profiles/profileRepository";
import { useCompactReadOnly } from "../entity-profiles/useCompactReadOnly";
import { ProfileFinancialMatrix } from "../entity-profiles/components/ProfileFinancialMatrix";
import { administrativeAddress, factForKey } from "../entity-profiles/profileSummary";
import "../entity-profiles/entityProfiles.css";

const roleLabels = { issuer: "发行人", guarantor: "保证人", counter_guarantor: "反担保方" } as const;

export interface ProjectSnapshotPanelProps {
  projectId: string;
  token?: string;
  repository?: ProfileRepository;
  readOnly?: boolean;
  onRefreshed?: (snapshot: ProjectProfileSnapshot) => void | Promise<void>;
  onOpenSource?: (sourceId: string) => void;
}

export function ProjectSnapshotPanel({ projectId, token, repository = profileRepository, readOnly = false, onRefreshed, onOpenSource }: ProjectSnapshotPanelProps) {
  const effectiveReadOnly = useCompactReadOnly(readOnly);
  const [snapshot, setSnapshot] = useState<ProjectProfileSnapshot | null>(null);
  const [status, setStatus] = useState<ProjectProfileSnapshotStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [projectedSourceIds, setProjectedSourceIds] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [snapshotResult, statusResult, evidenceResult] = await Promise.allSettled([
        repository.getProjectSnapshot(projectId, token),
        repository.getProjectSnapshotStatus(projectId, token),
        repository.getProjectEvidence(projectId, token),
      ]);
      if (snapshotResult.status === "rejected" && !(snapshotResult.reason instanceof ProfileApiError && snapshotResult.reason.status === 404)) throw snapshotResult.reason;
      if (statusResult.status === "rejected" && !(statusResult.reason instanceof ProfileApiError && statusResult.reason.status === 404)) throw statusResult.reason;
      setSnapshot(snapshotResult.status === "fulfilled" ? snapshotResult.value : null);
      setStatus(statusResult.status === "fulfilled" ? statusResult.value : null);
      setProjectedSourceIds(evidenceResult.status === "fulfilled" ? Object.fromEntries(evidenceResult.value.sources.map((source) => [source.origin_source_id, source.id])) : {});
    } catch (reason) {
      setSnapshot(null);
      setStatus(null);
      setError(reason instanceof Error ? reason.message : "主体快照读取失败");
    } finally {
      setLoading(false);
    }
  }, [projectId, repository, token]);

  useEffect(() => { void load(); }, [load]);

  const refresh = async () => {
    setBusy(true);
    setError("");
    try {
      const next = await repository.refreshProjectSnapshot(projectId, token);
      setConfirming(false);
      await load();
      await onRefreshed?.(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "主体快照刷新失败");
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <section className="ep-snapshot-panel"><LoadingState label="正在读取项目主体快照" /></section>;
  if (error && !snapshot) return <section className="ep-snapshot-panel"><ErrorState message={error} retry={() => void load()} /></section>;
  if (!snapshot) return <section className="ep-snapshot-panel ep-snapshot-empty"><UsersRound size={20} /><div><strong>旧项目未绑定主体档案</strong><span>现有项目材料仍可按原流程使用；系统不会自动补造主体事实。</span></div></section>;

  const issuerRole = snapshot.roles.find((role) => role.role === "issuer");
  const issuerFacts = snapshot.facts.filter((fact) => fact.role === "issuer" && fact.profile_id === issuerRole?.profile_id);
  const issuerSources = snapshot.sources.filter((source) => source.role === "issuer" && source.profile_id === issuerRole?.profile_id);
  const representative = factForKey(issuerFacts, "legal_representative");
  const address = factForKey(issuerFacts, "registered_address");

  return <section className="ep-snapshot-panel">
    <header><div><UsersRound size={19} /><span><strong>项目主体档案快照</strong><small>v{snapshot.version} · 基准日 {snapshot.baseline_date || "未提供"} · {snapshot.content_hash.slice(0, 10)}</small></span></div><div>{status?.analysis_stale ? <StatusBadge tone="danger">分析已失效</StatusBadge> : status?.has_newer_version ? <StatusBadge tone="info">主体库有新版本</StatusBadge> : <StatusBadge tone="success">当前快照已冻结</StatusBadge>}{status?.has_newer_version && !effectiveReadOnly && <button className="secondary-button ep-write-action" onClick={() => setConfirming(true)}><RefreshCw size={15} />刷新快照</button>}</div></header>
    {status?.has_newer_version && <div className="ep-snapshot-update"><History size={16} /><span><strong>主体库存在更新版本</strong>{status.reason || `项目当前使用主体档案 v${status.snapshot_profile_version ?? "-"}，主体库当前为 v${status.current_profile_version ?? "-"}。在用户确认刷新前，项目继续使用原冻结快照。`}</span></div>}
    {status?.analysis_stale && <div className="ep-snapshot-stale"><AlertTriangle size={16} /><span><strong>快照已刷新，需要重新运行 Agent</strong>提取及下游节点已失效，Word 导出应保持禁用。</span></div>}
    <div className="ep-snapshot-roles">{snapshot.roles.map((role) => {
      const roleStatus = status?.roles?.find((item) => item.profile_id === role.profile_id && item.role === role.role);
      return <article key={`${role.role}-${role.profile_id}`}><div><span>{roleLabels[role.role]}</span><strong>{role.name}</strong><small>主体档案 v{role.profile_version} · {role.baseline_date || "基准日未提供"}</small></div><b>{Math.round((role.completeness <= 1 ? role.completeness : role.completeness / 100) * 100)}%</b>{roleStatus?.has_newer_version ? <StatusBadge tone="info">可更新至 v{roleStatus.current_version}</StatusBadge> : <CheckCircle2 size={17} />}</article>;
    })}</div>
    {issuerRole && <div className="ep-snapshot-profile-body">
      <div className="ep-snapshot-identity" aria-label="发行人主体基本信息">
        <div><Building2 size={16} /><span><small>公司名称</small><strong>{issuerRole.name}</strong></span></div>
        <div><UserRound size={16} /><span><small>法定代表人</small><strong>{representative?.text_value || "材料不足"}</strong></span></div>
        <div><MapPin size={16} /><span><small>公司地址</small><strong>{administrativeAddress(address?.text_value || "")}</strong></span></div>
      </div>
      <ProfileFinancialMatrix
        facts={issuerFacts}
        sources={issuerSources}
        title="发行人三年财务基准（冻结快照）"
        onOpenSource={onOpenSource ? (sourceId) => onOpenSource(projectedSourceIds[sourceId] || sourceId) : undefined}
      />
    </div>}
    <footer><span><ShieldCheck size={15} />档案事实 {(snapshot.counts.confirmed || 0) + (snapshot.counts.single_source || 0)} 项 · {snapshot.counts.stale || 0} 项过期 · {snapshot.counts.conflicted || 0} 项冲突</span><small>证据状态：{status?.evidence_status || "状态待同步"}</small></footer>
    {error && <ErrorState message={error} />}
    {confirming && <div className="ep-drawer-backdrop"><section className="ep-refresh-dialog" role="dialog" aria-modal="true" aria-labelledby="ep-refresh-title"><header><div><span>请确认</span><h2 id="ep-refresh-title">刷新项目主体快照</h2></div><button className="icon-button" aria-label="关闭快照刷新确认" onClick={() => setConfirming(false)}><X size={18} /></button></header><div><AlertTriangle size={20} /><p>刷新后项目将采用主体库当前版本。现有事实抽取、规则、风险和报告将被标记为失效，重新运行 Agent 前不能导出 Word。</p></div><footer><button className="secondary-button" onClick={() => setConfirming(false)}>取消</button><button className="primary-button" disabled={busy} onClick={() => void refresh()}>{busy ? <RefreshCw className="spin" size={16} /> : <RefreshCw size={16} />}确认刷新</button></footer></section></div>}
  </section>;
}
