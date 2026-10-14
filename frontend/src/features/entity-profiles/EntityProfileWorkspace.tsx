import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowUp, Building2, CheckCircle2, Eye, FileUp, Search, ShieldCheck, X } from "lucide-react";
import { DEFAULT_PAGE_SIZE, EmptyState, ErrorState, HelpTip, LoadingState, Pagination } from "../../components/common";
import type { PageSize } from "../../components/common";
import type {
  EntityProfileDetail,
  EntityProfileReadiness,
  EntityProfileStatus,
  EntityProfileSummary,
  EntityProfileVersionSummary,
  ProfileRepository,
} from "./contracts";
import { ProfileBusinessOverview } from "./components/ProfileBusinessOverview";
import { ProfileFactsPanel } from "./components/ProfileFactsPanel";
import { ProfileFinancialMatrix } from "./components/ProfileFinancialMatrix";
import { ProfileGapsPanel } from "./components/ProfileGapsPanel";
import { ProfileImportDrawer } from "./components/ProfileImportDrawer";
import { ProfileReadinessPanel } from "./components/ProfileReadinessPanel";
import { ProfileSourcesPanel } from "./components/ProfileSourcesPanel";
import { MISSING_VALUE, ProfileStatusBadge, percent } from "./components/profilePresentation";
import { ProfileVersionPanel } from "./components/ProfileVersionPanel";
import { profileDataMode, profileRepository } from "./profileRepository";
import { useCompactReadOnly } from "./useCompactReadOnly";
import "./entityProfiles.css";

type ProfileTab = "overview" | "financial" | "facts" | "sources" | "status" | "versions";
const TABS: Array<{ key: ProfileTab; label: string }> = [
  { key: "overview", label: "概览" },
  { key: "financial", label: "财务资料" },
  { key: "facts", label: "结构化事实" },
  { key: "sources", label: "材料与证据" },
  { key: "status", label: "资料状态" },
  { key: "versions", label: "版本记录" },
];
const EMPTY_QUERY = { search: "", status: "all" as EntityProfileStatus | "all" };

export interface EntityProfileWorkspaceProps {
  repository?: ProfileRepository;
  initialProfileId?: string;
  readOnly?: boolean;
  onCreateProject?: (profile: EntityProfileSummary) => void;
}

/**
 * 发行人主体档案库：**清单表格 → 独立档案详情**。
 *
 * 首次进入只显示清单，不自动打开第一家公司——未选择时不展示无关详情。
 * 清单与详情共用同一组件实例，因此筛选、分页在返回时天然保留；滚动位置单独记录恢复。
 */
export function EntityProfileWorkspace({
  repository = profileRepository,
  initialProfileId,
  readOnly = false,
  onCreateProject,
}: EntityProfileWorkspaceProps) {
  const effectiveReadOnly = useCompactReadOnly(readOnly);
  const [profiles, setProfiles] = useState<EntityProfileSummary[]>([]);
  const [draft, setDraft] = useState(EMPTY_QUERY);
  const [applied, setApplied] = useState(EMPTY_QUERY);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<PageSize>(DEFAULT_PAGE_SIZE);
  const changePageSize = (next: PageSize) => { setPageSize(next); setPage(1); };
  const [selectedId, setSelectedId] = useState(initialProfileId || "");
  const [detail, setDetail] = useState<EntityProfileDetail | null>(null);
  const [readiness, setReadiness] = useState<EntityProfileReadiness | null>(null);
  const [versions, setVersions] = useState<EntityProfileVersionSummary[]>([]);
  const [activeTab, setActiveTab] = useState<ProfileTab>("overview");
  const [focusSourceId, setFocusSourceId] = useState("");
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [listError, setListError] = useState("");
  const [detailError, setDetailError] = useState("");
  const [importTargetId, setImportTargetId] = useState("");
  const [notice, setNotice] = useState("");
  const requestSequence = useRef(0);
  const listScroll = useRef(0);

  const loadProfiles = useCallback(async (query = applied) => {
    setListLoading(true);
    setListError("");
    try {
      const items = await repository.listProfiles({ search: query.search, status: query.status });
      setProfiles(items);
      setPage(1);
    } catch (reason) {
      setProfiles([]);
      setListError(reason instanceof Error ? reason.message : "主体档案列表读取失败");
    } finally {
      setListLoading(false);
    }
  }, [repository, applied]);

  const loadDetail = useCallback(async (profileId: string) => {
    const sequence = ++requestSequence.current;
    if (!profileId) {
      setDetail(null);
      setReadiness(null);
      setVersions([]);
      setDetailError("");
      setDetailLoading(false);
      return;
    }
    setDetailLoading(true);
    setDetailError("");
    // 切换主体时先清空旧详情，避免「新主体标题 + 旧主体资料」错配；同一主体刷新（如导入后）保留现内容。
    setDetail((current) => (current && current.id === profileId ? current : null));
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

  useEffect(() => { void loadProfiles(EMPTY_QUERY); /* eslint-disable-line react-hooks/exhaustive-deps */ }, [repository]);
  useEffect(() => { void loadDetail(selectedId); }, [selectedId, loadDetail]);

  // 打开详情回到顶部；返回清单恢复离开时的滚动位置。
  useLayoutEffect(() => {
    window.scrollTo(0, selectedId ? 0 : listScroll.current);
  }, [selectedId]);

  const pageCount = Math.max(1, Math.ceil(profiles.length / pageSize));
  const rows = useMemo(() => profiles.slice((page - 1) * pageSize, page * pageSize), [profiles, page, pageSize]);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);

  const openDetail = (profileId: string) => {
    listScroll.current = window.scrollY;
    setSelectedId(profileId);
    setActiveTab("overview");
    setFocusSourceId("");
    setNotice("");
  };
  const backToList = () => {
    setSelectedId("");
    setActiveTab("overview");
    setFocusSourceId("");
    setDetailError("");
  };
  const openSource = (sourceId: string) => {
    setFocusSourceId(sourceId);
    setActiveTab("sources");
  };

  const handleImported = async (profile: EntityProfileDetail) => {
    setImportTargetId("");
    if (selectedId !== profile.id) {
      listScroll.current = window.scrollY;
      setSelectedId(profile.id);
      setActiveTab("overview");
    }
    setDetail(profile);
    setNotice(`已生成主体档案 v${profile.version}，当前详情已刷新。`);
    await Promise.all([loadProfiles(applied), loadDetail(profile.id)]);
  };

  const query = () => { setApplied(draft); void loadProfiles(draft); };
  const reset = () => { setDraft(EMPTY_QUERY); setApplied(EMPTY_QUERY); void loadProfiles(EMPTY_QUERY); };
  const filtered = Boolean(applied.search.trim() || applied.status !== "all");

  return <div className="view-content ep-workspace" data-profile-mode={profileDataMode}>
    <div className="ep-page-heading">
      <div>
        <div className="page-title-row"><h1>发行人主体档案库</h1><HelpTip label="发行人主体档案库说明">统一沉淀工商、股权、财务与债务资料，作为可复用的主体主数据；项目只读取版本化的冻结快照。</HelpTip></div>
        <p>共 {profiles.length} 个主体{filtered ? "（当前筛选结果）" : ""}</p>
      </div>
      {profileDataMode === "mock" && <div className="ep-mock-banner"><ShieldCheck size={16} />模拟主体档案 · 仅本地开发</div>}
    </div>

    <section className="ep-toolbar">
      <label><Search size={16} /><input aria-label="按名称或统一社会信用代码筛选" value={draft.search} onChange={(event) => setDraft({ ...draft, search: event.target.value })} onKeyDown={(event) => { if (event.key === "Enter") query(); }} placeholder="名称或统一社会信用代码" /></label>
      <select aria-label="档案状态筛选" value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value as EntityProfileStatus | "all" })}>
        <option value="all">全部状态</option>
        <option value="ready">可用于建项</option>
        <option value="stale">资料待更新</option>
        <option value="conflicted">存在冲突</option>
        <option value="incomplete">档案不完整</option>
      </select>
      <div className="ep-toolbar-actions">
        <button className="primary-button small" onClick={query}><Search size={14} />查询</button>
        <button className="secondary-button small" onClick={reset}>重置</button>
      </div>
    </section>

    {listError ? <ErrorState message={listError} retry={() => void loadProfiles(applied)} />
      : selectedId ? <main className="ep-profile-detail">
        {detailError ? <ErrorState message={detailError} retry={() => void loadDetail(selectedId)} />
          : detailLoading || !detail ? <LoadingState label="正在载入主体档案详情" />
          : <>
            <header className="ep-detail-header">
              <div className="ep-detail-heading">
                <button className="text-button ep-back" onClick={backToList}><ArrowLeft size={15} />返回档案库</button>
                <h2>{detail.name}</h2>
                <p>
                  <span>统一社会信用代码 {detail.unified_code || MISSING_VALUE}</span>
                  <span>当前版本 v{detail.version}</span>
                  <span>资料基准日 {detail.baseline_date || MISSING_VALUE}</span>
                  <span>最新财务期间 {detail.latest_financial_period || MISSING_VALUE}</span>
                </p>
              </div>
              <div className="ep-detail-actions">
                {!effectiveReadOnly && <button className="secondary-button ep-write-action" onClick={() => setImportTargetId(detail.id)}><FileUp size={16} />导入资料</button>}
                {!effectiveReadOnly && onCreateProject && <button className="primary-button ep-write-action" onClick={() => onCreateProject(detail)}><Building2 size={16} />基于主体建项</button>}
              </div>
            </header>

            {/* 状态与完整度只在摘要行出现一次；完整度衡量资料覆盖，不等于允许启动分析。 */}
            <div className="ep-summary-row">
              <ProfileStatusBadge status={detail.status} />
              <span>档案完整度 <b>{percent(detail.completeness)}%</b></span>
              <span>启动分析 <b className={readiness?.ready_for_agent ? "ep-ok" : "ep-warn"}>{readiness ? (readiness.ready_for_agent ? "已就绪" : "未就绪") : "检查中"}</b></span>
              <small>完整度衡量资料覆盖程度，不等于允许启动分析；能否启动由「资料状态」的门禁条件决定。</small>
            </div>

            {notice && <div className="ep-notice" role="status"><CheckCircle2 size={16} /><span>{notice}</span><button className="text-button" onClick={() => { setActiveTab("versions"); setNotice(""); }}>查看版本记录</button><button className="icon-button" onClick={() => setNotice("")} title="关闭提示"><X size={14} /></button></div>}

            <nav className="ep-tabs" aria-label="主体档案详情页签">{TABS.map((tab) => <button aria-current={activeTab === tab.key ? "page" : undefined} className={activeTab === tab.key ? "active" : ""} key={tab.key} onClick={() => setActiveTab(tab.key)}>{tab.label}</button>)}</nav>
            <div className="ep-tab-body">
              {activeTab === "overview" && <>
                <ProfileBusinessOverview profile={detail} onOpenFinancial={() => setActiveTab("financial")} onOpenSource={openSource} />
                <ProfileGapsPanel readiness={readiness} onOpenAll={() => setActiveTab("status")} />
              </>}
              {activeTab === "financial" && <ProfileFinancialMatrix facts={detail.facts} sources={detail.sources || []} onOpenSource={openSource} />}
              {activeTab === "facts" && <ProfileFactsPanel profile={detail} onOpenSource={openSource} />}
              {activeTab === "sources" && <ProfileSourcesPanel profile={detail} focusSourceId={focusSourceId} />}
              {activeTab === "status" && <ProfileReadinessPanel readiness={readiness} />}
              {activeTab === "versions" && <ProfileVersionPanel profileId={detail.id} versions={versions} repository={repository} />}
            </div>
          </>}
      </main>
      : <section className="ep-list-panel">
        {listLoading ? <LoadingState label="正在读取主体档案" />
          : !profiles.length ? <EmptyState
            title={filtered ? "没有符合条件的主体" : "暂无主体档案"}
            description={filtered ? "调整筛选条件后重新查询，或点击重置查看全部主体。" : "先在内部主体资料库完成建档，再创建本次债券项目。"} />
          : <>
            <div className="ep-table-scroll">
              <table className="ep-table ep-profile-table">
                <thead><tr><th>主体名称</th><th>统一社会信用代码</th><th>资料基准日</th><th>最新财务期间</th><th>档案状态</th><th>完整度</th><th>引用项目数</th><th>操作</th></tr></thead>
                <tbody>{rows.map((profile) => <tr key={profile.id}>
                  <td><strong className="ep-profile-name">{profile.name}</strong></td>
                  <td className="ep-code">{profile.unified_code || MISSING_VALUE}</td>
                  <td>{profile.baseline_date || MISSING_VALUE}</td>
                  <td>{profile.latest_financial_period || MISSING_VALUE}</td>
                  <td><ProfileStatusBadge status={profile.status} /></td>
                  <td>{percent(profile.completeness)}%</td>
                  <td>{profile.project_count}</td>
                  <td><div className="ep-row-actions">
                    <button className="text-button" onClick={() => openDetail(profile.id)}><Eye size={14} />详情</button>
                    {!effectiveReadOnly && <button className="text-button" onClick={() => setImportTargetId(profile.id)}><FileUp size={14} />导入资料</button>}
                    {!effectiveReadOnly && onCreateProject && <button className="text-button" onClick={() => onCreateProject(profile)}><Building2 size={14} />基于主体建项</button>}
                  </div></td>
                </tr>)}</tbody>
              </table>
            </div>
            <Pagination page={page} pageCount={pageCount} pageSize={pageSize} total={profiles.length} onPage={setPage} onPageSize={changePageSize} />
          </>}
      </section>}

    {importTargetId && !effectiveReadOnly && <ProfileImportDrawer profileId={importTargetId} repository={repository} onClose={() => setImportTargetId("")} onCommitted={(profile) => void handleImported(profile)} />}
  </div>;
}
