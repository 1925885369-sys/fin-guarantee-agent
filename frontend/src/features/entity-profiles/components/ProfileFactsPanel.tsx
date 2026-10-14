import { Database, FileSearch } from "lucide-react";
import { EmptyState } from "../../../components/common";
import type { EntityProfileDetail, EntityProfileFact } from "../contracts";
import { displayFactValue, factSourceLabel, FactStatusBadge, MISSING_VALUE, ProfilePanelHeading } from "./profilePresentation";

const categoryLabels: Record<string, string> = {
  basic: "基本工商信息",
  identity: "基本工商信息",
  governance: "股权和实际控制人",
  ownership: "股权和实际控制人",
  financial: "财务数据",
  debt: "债务与到期结构",
  operations: "经营及交易",
  concentration: "客户与供应商集中度",
  related_party: "关联交易",
  legal: "诉讼、对外担保和合规",
};

function groupedFacts(facts: EntityProfileFact[]) {
  return facts.reduce<Record<string, EntityProfileFact[]>>((groups, fact) => {
    (groups[fact.category || "other"] ||= []).push(fact);
    return groups;
  }, {});
}

function firstSourceId(fact: EntityProfileFact) {
  return (fact.source_ids || [])[0] || fact.source_id || "";
}

/**
 * 结构化事实：按业务分类的紧凑表格，列为「字段 / 值 / 期间与口径 / 状态 / 来源」。
 * 英文事实键与内部标识不占列，收进字段名的 title 提示。
 */
export function ProfileFactsPanel({ profile, onOpenSource }: { profile: EntityProfileDetail; onOpenSource?: (sourceId: string) => void }) {
  const groups = Object.entries(groupedFacts(profile.facts));
  if (!groups.length) return <EmptyState title="尚无结构化事实" description="请导入 Excel 或 JSON 主数据，并为采用的事实绑定可定位证据。" />;
  return <div className="ep-fact-groups">{groups.map(([category, facts]) => <section className="ep-panel" key={category}>
    <ProfilePanelHeading title={categoryLabels[category] || category} description={`${facts.length} 项规范事实`} action={<Database size={18} />} />
    <div className="ep-table-scroll">
      <table className="ep-table ep-fact-table">
        <thead><tr><th>字段</th><th>值</th><th>期间与口径</th><th>状态</th><th>来源</th></tr></thead>
        <tbody>{facts.map((fact) => {
          const sourceId = firstSourceId(fact);
          return <tr key={fact.id}>
            <td title={`内部字段键：${fact.fact_key || MISSING_VALUE}`}><strong>{fact.name || fact.fact_key || MISSING_VALUE}</strong></td>
            <td className="ep-cell-value">{displayFactValue(fact.value, fact.text_value, fact.unit)}</td>
            <td>{[fact.period || "非期间事实", fact.scope || "口径未提供"].join(" · ")}</td>
            <td><FactStatusBadge status={fact.status} />{fact.status === "confirmed" && <span className="ep-muted-cell">已确认</span>}</td>
            <td>{sourceId && onOpenSource
              ? <button className="ep-source-link" onClick={() => onOpenSource(sourceId)} title={factSourceLabel(fact)}><FileSearch size={12} />{factSourceLabel(fact)}</button>
              : <span className="ep-muted-cell">{factSourceLabel(fact)}</span>}</td>
          </tr>;
        })}</tbody>
      </table>
    </div>
  </section>)}</div>;
}
