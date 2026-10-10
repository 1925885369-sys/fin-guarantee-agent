import { Building2, FileSpreadsheet, FileText, MapPin, UserRound } from "lucide-react";
import type { ReactNode } from "react";
import type { EntityProfileDetail, EntityProfileFact } from "../contracts";
import { administrativeAddress, factForKey } from "../profileSummary";
import { displayFactValue, FactStatusBadge, ProfilePanelHeading } from "./profilePresentation";
import { ProfileFinancialMatrix } from "./ProfileFinancialMatrix";

function evidenceTitle(fact?: EntityProfileFact) {
  if (!fact) return "未取得可定位来源";
  return `${fact.source_label || "来源待补充"}${fact.page ? ` · 第${fact.page}页` : ""}`;
}

function BusinessFact({ label, fact, value, icon }: {
  label: string; fact?: EntityProfileFact; value: string; icon: ReactNode;
}) {
  return <article className="ep-business-fact" title={evidenceTitle(fact)}>
    <div className="ep-business-icon">{icon}</div>
    <div><span>{label}</span><strong>{value}</strong><small><FileText size={12} />{evidenceTitle(fact)}</small></div>
    {fact && <FactStatusBadge status={fact.status} />}
  </article>;
}

export function ProfileBusinessOverview({ profile }: { profile: EntityProfileDetail }) {
  const representative = factForKey(profile.facts, "legal_representative");
  const address = factForKey(profile.facts, "registered_address");
  const established = factForKey(profile.facts, "establishment_date");
  const capital = factForKey(profile.facts, "registered_capital");

  return <div className="ep-business-overview">
    <section className="ep-panel">
      <ProfilePanelHeading title="工商信息摘要" description="地址仅作界面脱敏展示，原始地址和证据定位保持不变" action={<Building2 size={18} />} />
      <div className="ep-business-grid">
        <BusinessFact label="公司名称" value={profile.name} icon={<Building2 size={17} />} />
        <BusinessFact label="法定代表人" fact={representative} value={representative?.text_value || "材料不足"} icon={<UserRound size={17} />} />
        <BusinessFact label="公司地址" fact={address} value={administrativeAddress(address?.text_value || "")} icon={<MapPin size={17} />} />
        <BusinessFact label="统一社会信用代码" value={profile.unified_code || "材料不足"} icon={<FileText size={17} />} />
        <BusinessFact label="成立日期" fact={established} value={established?.text_value || "材料不足"} icon={<FileText size={17} />} />
        <BusinessFact label="注册资本" fact={capital} value={capital ? displayFactValue(capital.value, capital.text_value, capital.unit) : "材料不足"} icon={<FileSpreadsheet size={17} />} />
      </div>
    </section>

    <ProfileFinancialMatrix facts={profile.facts} sources={profile.sources} />
  </div>;
}
