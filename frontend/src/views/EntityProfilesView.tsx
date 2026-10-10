import { useEffect, useMemo, useState } from "react";
import { Building2, FileText, History, Search, ShieldCheck, UsersRound } from "lucide-react";
import { api } from "../api";
import { EmptyState, ErrorState, LoadingState, ReadinessBar, StatusBadge } from "../components/common";
import { ProfileSummaryCard } from "../components/profiles/ProfileSummaryCard";
import { useEntityProfiles } from "../hooks/useEntityProfiles";
import type { EntityProfileDetail, EntityProfileStatus, ProfileFactStatus } from "../types";

const statusLabels: Record<ProfileFactStatus, string> = {
  confirmed: "",
  single_source: "单一来源，尚未独立验证",
  stale: "资料已过期",
  conflicted: "存在冲突",
  insufficient: "材料不足",
};
const statusTones: Record<ProfileFactStatus, "success" | "info" | "warning" | "danger" | "neutral"> = {
  confirmed: "success",
  single_source: "info",
  stale: "warning",
  conflicted: "danger",
  insufficient: "neutral",
};
const categoryLabels: Record<string, string> = {
  basic: "基本工商信息",
  governance: "股权和实际控制人",
  financial: "财务数据",
  debt: "债务与到期结构",
  operations: "经营及交易",
  legal: "诉讼、对外担保和关联交易",
  evidence: "材料与证据",
  history: "历史版本和审计记录",
};

export function EntityProfilesView({ openCreate }: { openCreate: () => void }) {
  const { profiles, loading, error, load } = useEntityProfiles();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<EntityProfileStatus | "all">("all");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<EntityProfileDetail | null>(null);
  const [detailError, setDetailError] = useState("");

  useEffect(() => {
    if (!selectedId && profiles[0]) {
      const populated = profiles.find((profile) => Object.values(profile.counts || {}).some((count) => Number(count) > 0));
      setSelectedId((populated || profiles[0]).id);
    }
  }, [profiles, selectedId]);
  useEffect(() => {
    if (!selectedId) return;
    setDetail(null);
    setDetailError("");
    api.entityProfile(selectedId)
      .then(setDetail)
      .catch((reason) => setDetailError(reason instanceof Error ? reason.message : "档案详情读取失败"));
  }, [selectedId]);

  const groups = useMemo(
    () => Object.entries(detail?.facts.reduce<Record<string, typeof detail.facts>>((result, fact) => {
      (result[fact.category] ||= []).push(fact);
      return result;
    }, {}) || {}),
    [detail],
  );

  return <div className="view-content profiles-view">
    <div className="page-heading">
      <div><span className="eyebrow">Reusable entity master data</span><h1>主体档案库</h1></div>
      <button className="primary-button write-aware" onClick={openCreate}><Building2 size={16} />基于发行人新建债券担保项目</button>
    </div>
    <section className="profile-toolbar">
      <label><Search size={16} /><input aria-label="搜索主体档案" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="企业名称或统一社会信用代码" /><button className="text-button" onClick={() => load(search, status)}>搜索</button></label>
      <select aria-label="档案状态筛选" value={status} onChange={(event) => {
        const next = event.target.value as EntityProfileStatus | "all";
        setStatus(next);
        void load(search, next);
      }}>
        <option value="all">全部状态</option>
        <option value="ready">可用于建项</option>
        <option value="stale">资料待更新</option>
        <option value="conflicted">存在冲突</option>
        <option value="incomplete">档案不完整</option>
      </select>
    </section>
    {error ? <ErrorState message={`主体档案功能当前不可用：${error}`} retry={() => load(search, status)} />
      : loading ? <LoadingState label="正在读取主体档案" />
      : !profiles.length ? <EmptyState title="暂无主体档案" description="先在业务系统沉淀工商、股权、财务及债务资料，再创建本次债券项目。" />
      : <div className="profile-workspace">
        <aside className="profile-list">{profiles.map((profile) => <ProfileSummaryCard key={profile.id} profile={profile} selected={selectedId === profile.id} onSelect={() => setSelectedId(profile.id)} />)}</aside>
        <main className="profile-detail">{detailError ? <ErrorState message={detailError} /> : !detail ? <LoadingState label="正在载入档案详情" /> : <>
          <header><div><span>主体档案 v{detail.version}</span><h2>{detail.name}</h2><p>{detail.unified_code || "统一社会信用代码待补充"} · 资料基准日 {detail.baseline_date || "待确认"}</p></div><StatusBadge tone={detail.status === "ready" ? "success" : detail.status === "conflicted" ? "danger" : "warning"}>{detail.status === "ready" ? "可用于建项" : detail.status === "stale" ? "资料待更新" : detail.status === "conflicted" ? "存在冲突" : "档案不完整"}</StatusBadge></header>
          <ReadinessBar label="档案完整度" value={detail.completeness || 0} detail={`最新财务期间 ${detail.latest_financial_period || "待补充"}`} tone={(detail.completeness <= 1 ? detail.completeness * 100 : detail.completeness) >= 80 ? "success" : "warning"} />
          {groups.length ? groups.map(([category, facts]) => <section className="profile-fact-group" key={category}>
            <div className="panel-title"><h3>{categoryLabels[category] || category}</h3>{category === "governance" ? <UsersRound size={17} /> : category === "history" ? <History size={17} /> : category === "evidence" ? <FileText size={17} /> : <ShieldCheck size={17} />}</div>
            {facts.map((fact) => <div className="profile-fact-row" key={fact.id}>
              <div><strong>{fact.name || fact.fact_key}</strong><span>{fact.period || "非期间事实"} · {fact.scope || "主体口径"}</span></div>
              <b>{fact.value != null ? `${fact.value}${fact.unit || ""}` : fact.text_value || "材料不足"}</b>
              {fact.status !== "confirmed" && <StatusBadge tone={statusTones[fact.status]}>{statusLabels[fact.status]}</StatusBadge>}
              <small>{fact.source_label || "来源待补充"}{fact.page ? ` · 第${fact.page}页` : ""}</small>
            </div>)}
          </section>) : <EmptyState title="档案事实待补充" description="该主体已建档，但尚未沉淀可复用的结构化事实。" />}
        </>}</main>
      </div>}
  </div>;
}
