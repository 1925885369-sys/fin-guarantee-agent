import { useEffect, useMemo, useState } from "react";
import { GitCompareArrows, History } from "lucide-react";
import { EmptyState, ErrorState, LoadingState, StatusBadge } from "../../../components/common";
import type { EntityProfileFact, EntityProfileVersionDetail, EntityProfileVersionSummary, ProfileRepository } from "../contracts";
import { displayFactValue, ProfilePanelHeading } from "./profilePresentation";

type FactDiff = { key: string; name: string; before: EntityProfileFact | null; after: EntityProfileFact | null; kind: "added" | "removed" | "changed" };

function identity(fact: EntityProfileFact) {
  return `${fact.fact_key}|${fact.period || ""}|${fact.scope || ""}`;
}

function comparable(fact: EntityProfileFact) {
  return JSON.stringify([fact.value, fact.text_value, fact.unit, fact.status, fact.source_ids]);
}

function versionDiff(before: EntityProfileVersionDetail, after: EntityProfileVersionDetail): FactDiff[] {
  const left = new Map(before.facts.map((fact) => [identity(fact), fact]));
  const right = new Map(after.facts.map((fact) => [identity(fact), fact]));
  const differences: FactDiff[] = [];
  for (const key of new Set([...left.keys(), ...right.keys()])) {
    const previous = left.get(key) || null;
    const next = right.get(key) || null;
    if (!previous && next) differences.push({ key, name: next.name, before: null, after: next, kind: "added" });
    else if (previous && !next) differences.push({ key, name: previous.name, before: previous, after: null, kind: "removed" });
    else if (previous && next && comparable(previous) !== comparable(next)) differences.push({ key, name: next.name, before: previous, after: next, kind: "changed" });
  }
  return differences;
}

export function ProfileVersionPanel({ profileId, versions, repository }: { profileId: string; versions: EntityProfileVersionSummary[]; repository: ProfileRepository }) {
  const [beforeVersion, setBeforeVersion] = useState<number | null>(versions[1]?.version ?? null);
  const [afterVersion, setAfterVersion] = useState<number | null>(versions[0]?.version ?? null);
  const [details, setDetails] = useState<{ before: EntityProfileVersionDetail; after: EntityProfileVersionDetail } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setBeforeVersion(versions[1]?.version ?? null);
    setAfterVersion(versions[0]?.version ?? null);
    setDetails(null);
  }, [profileId, versions]);

  const compare = async () => {
    if (beforeVersion == null || afterVersion == null) return;
    setLoading(true);
    setError("");
    try {
      const [before, after] = await Promise.all([
        repository.getVersion(profileId, beforeVersion),
        repository.getVersion(profileId, afterVersion),
      ]);
      setDetails({ before, after });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "历史版本读取失败");
    } finally {
      setLoading(false);
    }
  };
  const diffs = useMemo(() => details ? versionDiff(details.before, details.after) : [], [details]);

  if (!versions.length) return <EmptyState title="尚无版本记录" description="首次建档或导入提交后会形成不可变的版本记录。" />;
  return <div className="ep-version-stack">
    <section className="ep-panel">
      <ProfilePanelHeading title="版本时间线" description="更新会创建新版本，不覆盖历史事实和来源。" action={<History size={18} />} />
      <div className="ep-version-timeline">{versions.map((version) => <article key={version.version}><span className="ep-version-node" /><div><strong>v{version.version} · {version.change_summary || "主体档案更新"}</strong><small>{new Date(version.created_at).toLocaleString("zh-CN")} · {version.created_by || "系统"}</small><p>基准日 {version.baseline_date || "待确认"} · {version.fact_count} 项事实 · {version.source_count} 份来源</p></div>{version.current && <StatusBadge tone="success">当前版本</StatusBadge>}</article>)}</div>
    </section>
    <section className="ep-panel">
      <ProfilePanelHeading title="版本差异" help="按事实键、期间和口径逐项比较；期间或口径不同不会被误判为冲突。" action={<GitCompareArrows size={18} />} />
      <div className="ep-version-compare-controls"><label>基准版本<select value={beforeVersion ?? ""} onChange={(event) => setBeforeVersion(Number(event.target.value))}>{versions.map((item) => <option value={item.version} key={item.version}>v{item.version}</option>)}</select></label><label>比较版本<select value={afterVersion ?? ""} onChange={(event) => setAfterVersion(Number(event.target.value))}>{versions.map((item) => <option value={item.version} key={item.version}>v{item.version}</option>)}</select></label><button className="secondary-button" disabled={beforeVersion == null || afterVersion == null || beforeVersion === afterVersion || loading} onClick={compare}><GitCompareArrows size={15} />比较</button></div>
      {error && <ErrorState message={error} retry={compare} />}
      {loading ? <LoadingState label="正在比较主体版本" /> : details ? !diffs.length ? <div className="ep-ready-empty">两个版本的规范事实没有差异</div> : <div className="ep-diff-list">{diffs.map((diff) => <article key={diff.key}><StatusBadge tone={diff.kind === "added" ? "success" : diff.kind === "removed" ? "warning" : "info"}>{diff.kind === "added" ? "新增" : diff.kind === "removed" ? "移除" : "变更"}</StatusBadge><strong>{diff.name}</strong><div><span>{diff.before ? displayFactValue(diff.before.value, diff.before.text_value, diff.before.unit) : "—"}</span><b>→</b><span>{diff.after ? displayFactValue(diff.after.value, diff.after.text_value, diff.after.unit) : "—"}</span></div></article>)}</div> : <EmptyState title="选择两个版本进行比较" description="比较结果只展示真实变化，不修改任何历史版本。" />}
    </section>
  </div>;
}
