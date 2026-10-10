import { Database, FileText } from "lucide-react";
import { EmptyState } from "../../../components/common";
import type { EntityProfileDetail, EntityProfileFact } from "../contracts";
import { displayFactValue, FactStatusBadge, ProfilePanelHeading } from "./profilePresentation";

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

export function ProfileFactsPanel({ profile }: { profile: EntityProfileDetail }) {
  const groups = Object.entries(groupedFacts(profile.facts));
  if (!groups.length) return <EmptyState title="尚无结构化事实" description="请导入 Excel 或 JSON 主数据，并为采用的事实绑定可定位证据。" />;
  return <div className="ep-fact-groups">{groups.map(([category, facts]) => <section className="ep-panel" key={category}>
    <ProfilePanelHeading title={categoryLabels[category] || category} description={`${facts.length} 项规范事实`} action={<Database size={18} />} />
    <div className="ep-fact-table">{facts.map((fact) => <article className="ep-fact-row" key={fact.id}>
      <div><strong>{fact.name || fact.fact_key}</strong><small>{fact.fact_key} · {fact.period || "非期间事实"} · {fact.scope || "口径未提供"}</small></div>
      <b>{displayFactValue(fact.value, fact.text_value, fact.unit)}</b>
      <FactStatusBadge status={fact.status} />
      <div className="ep-fact-source"><FileText size={14} /><span>{fact.source_label || "来源待补充"}{fact.page ? ` · 第${fact.page}页` : ""}</span></div>
    </article>)}</div>
  </section>)}</div>;
}
