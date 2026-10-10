import { useCallback, useEffect, useRef, useState } from "react";
import { Building2, ChevronRight, FileUp, Search, ShieldCheck } from "lucide-react";
import { EmptyState, ErrorState, LoadingState, ReadinessBar } from "../../components/common";
import type {
  EntityProfileDetail,
  EntityProfileReadiness,
  EntityProfileStatus,
  EntityProfileSummary,
  EntityProfileVersionSummary,
  ProfileRepository,
} from "./contracts";
import { ProfileFactsPanel } from "./components/ProfileFactsPanel";
import { ProfileBusinessOverview } from "./components/ProfileBusinessOverview";
import { ProfileImportDrawer } from "./components/ProfileImportDrawer";
import { ProfileReadinessPanel } from "./components/ProfileReadinessPanel";
import { ProfileSourcesPanel } from "./components/ProfileSourcesPanel";
import { ProfileStatusBadge, percent } from "./components/profilePresentation";
import { ProfileVersionPanel } from "./components/ProfileVersionPanel";
import { profileDataMode, profileRepository } from "./profileRepository";
import { useCompactReadOnly } from "./useCompactReadOnly";
import "./entityProfiles.css";

type ProfileTab = "overview" | "facts" | "sources" | "versions";
const tabs: Array<{ key: ProfileTab; label: string }> = [
  { key: "overview", label: "概览与就绪度" },
  { key: "facts", label: "结构化事实" },
  { key: "sources", label: "材料与证据" },
  { key: "versions", label: "版本记录" },
];

export interface EntityProfileWorkspaceProps {
  repository?: ProfileRepository;
  initialProfileId?: string;
  readOnly?: boolean;
  onCreateProject?: (profile: EntityProfileSummary) => void;
}

export function EntityProfileWorkspace({
  repository = profileRepository,
  initialProfileId,
  readOnly = false,
  onCreateProject,
}: EntityProfileWorkspaceProps) {
  const effectiveReadOnly = useCompactReadOnly(readOnly);
  const [profiles, setProfiles] = useState<EntityProfileSummary[]>([]);
  const [selectedId, setSelectedId] = useState(initialProfileId || "");
  const [detail, setDetail] = useState<EntityProfileDetail | null>(null);
  const [readiness, setReadiness] = useState<EntityProfileReadiness | null>(null);
  const [versions, setVersions] = useState<EntityProfileVersionSummary[]>([]);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<EntityProfileStatus | "all">("all");
  const [activeTab, setActiveTab] = useState<ProfileTab>("overview");
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const requestSequence = useRef(0);

  const loadProfiles = useCallback(async (nextSearch = search, nextStatus = status) => {
    setListLoading(true);
    setListError("");
    try {
      const items = await repository.listProfiles({ search: nextSearch, status: nextStatus });
      setProfiles(items);
      setSelectedId((current) => current && items.some((item) => item.id === current) ? current : items[0]?.id || "");
    } catch (reason) {
      setProfiles([]);
      setSelectedId("");
      setListError(reason instanceof Error ? reason.message : "主体档案列表读取失败");
    } finally {
      setListLoading(false);
    }
  }, [repository, search, status]);

  const loadDetail = useCallback(async (profileId: string) => {
    if (!profileId) {
      setDetail(null);
      setReadiness(null);
      setVersions([]);
      return;
    }
    const sequence = ++requestSequence.current;
    setDetailLoading(true);
    setDetailError("");
    try {
      const [nextDetail, nextReadiness, nextVersions] = await Promise.all([
        repository.getProfile(profileId),
        repository.getReadiness(profileId),
        repository.listVersions(profileId),
      ]);
      if (sequence !== requestSequence.current) return;
      setDetail(nextDetail);
      setReadiness(nextReadiness);
      setVersions(nextVersions);
    } catch (reason) {
      if (sequence !== requestSequence.current) return;
      setDetail(null);
      setReadiness(null);
      setVersions([]);
      setDetailError(reason instanceof Error ? reason.message : "主体档案详情读取失败");
    } finally {
      if (sequence === requestSequence.current) setDetailLoading(false);
    }
  }, [repository]);

  useEffect(() => { void loadProfiles("", "all"); }, [repository]);
  useEffect(() => { void loadDetail(selectedId); }, [selectedId, loadDetail]);

  const handleImported = async (profile: EntityProfileDetail) => {
    setDetail(profile);
    setImportOpen(false);
    await Promise.all([loadProfiles(search, status), loadDetail(profile.id)]);
    setSelectedId(profile.id);
    setActiveTab("versions");
  };

  return <div className="ep-workspace" data-profile-mode={profileDataMode}>
    <div className="ep-page-heading"><div><span>Reusable entity master data</span><h1>发行人主体档案</h1><p>主体资料由系统数据库统一维护，项目使用版本化的冻结快照。</p></div>{profileDataMode === "mock" && <div className="ep-mock-banner"><ShieldCheck size={16} />模拟主体档案 · 仅本地开发</div>}</div>
    <section className="ep-toolbar">
      <label><Search size={16} /><input aria-label="搜索发行人主体档案" value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void loadProfiles(); }} placeholder="企业名称或统一社会信用代码" /><button className="text-button" onClick={() => void loadProfiles()}>搜索</button></label>
      <select aria-label="发行人主体档案状态" value={status} onChange={(event) => { const next = event.target.value as EntityProfileStatus | "all"; setStatus(next); void loadProfiles(search, next); }}><option value="all">全部状态</option><option value="ready">可用于建项</option><option value="stale">资料待更新</option><option value="conflicted">存在冲突</option><option value="incomplete">档案不完整</option></select>
    </section>
    {listError ? <ErrorState message={listError} retry={() => void loadProfiles()} /> : <div className="ep-master-detail">
      <aside className="ep-profile-list" aria-label="发行人主体列表">
        {listLoading ? <LoadingState label="正在读取主体档案" /> : !profiles.length ? <EmptyState title="没有符合条件的主体" description="调整检索条件，或先在内部主体资料库完成建档。" /> : profiles.map((profile) => <button className={profile.id === selectedId ? "selected" : ""} key={profile.id} onClick={() => setSelectedId(profile.id)}>
          <Building2 size={18} /><span><strong>{profile.name}</strong><small>{profile.unified_code || "统一社会信用代码待补充"}</small><small>基准日 {profile.baseline_date || "未提供"} · 被 {profile.project_count} 个项目引用</small></span><div><ProfileStatusBadge status={profile.status} /><b>{percent(profile.completeness)}%</b></div><ChevronRight size={16} />
        </button>)}
      </aside>
      <main className="ep-profile-detail">
        {detailError ? <ErrorState message={detailError} retry={() => void loadDetail(selectedId)} /> : detailLoading || !detail ? <LoadingState label="正在载入主体档案详情" /> : <>
          <header className="ep-detail-header"><div><span>主体档案 v{detail.version}</span><h2>{detail.name}</h2><p>{detail.unified_code} · 资料基准日 {detail.baseline_date || "未提供"} · 最新财务期间 {detail.latest_financial_period || "待补充"}</p></div><div><ProfileStatusBadge status={detail.status} />{!effectiveReadOnly && <button className="secondary-button ep-write-action" onClick={() => setImportOpen(true)}><FileUp size={16} />导入主体资料</button>}{!effectiveReadOnly && onCreateProject && <button className="primary-button ep-write-action" onClick={() => onCreateProject(detail)}><Building2 size={16} />基于主体建项</button>}</div></header>
          <ReadinessBar label="档案完整度" value={detail.completeness} detail={`${(detail.counts.confirmed || 0) + (detail.counts.single_source || 0)} 项可用事实 · ${(detail.counts.stale || 0) + (detail.counts.conflicted || 0) + (detail.counts.insufficient || 0)} 项需关注`} tone={readiness?.ready_for_agent ? "success" : "warning"} />
          <nav className="ep-tabs" aria-label="主体档案详情页签">{tabs.map((tab) => <button aria-current={activeTab === tab.key ? "page" : undefined} className={activeTab === tab.key ? "active" : ""} key={tab.key} onClick={() => setActiveTab(tab.key)}>{tab.label}</button>)}</nav>
          <div className="ep-tab-body">
            {activeTab === "overview" && <><ProfileBusinessOverview profile={detail} /><ProfileReadinessPanel readiness={readiness} /></>}
            {activeTab === "facts" && <ProfileFactsPanel profile={detail} />}
            {activeTab === "sources" && <ProfileSourcesPanel profile={detail} />}
            {activeTab === "versions" && <ProfileVersionPanel profileId={detail.id} versions={versions} repository={repository} />}
          </div>
        </>}
      </main>
    </div>}
    {importOpen && detail && !effectiveReadOnly && <ProfileImportDrawer profileId={detail.id} repository={repository} onClose={() => setImportOpen(false)} onCommitted={(profile) => void handleImported(profile)} />}
  </div>;
}
