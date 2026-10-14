import { ArrowRight, Building2, FileSearch, Landmark } from "lucide-react";
import type { EntityProfileDetail, EntityProfileFact } from "../contracts";
import { administrativeAddress, factForKey, financialFact, latestAnnualPeriods } from "../profileSummary";
import { financialFactValue, formatMetricValue, metricByKey } from "./ProfileFinancialMatrix";
import { displayFactValue, factSourceLabel, FactStatusBadge, MISSING_VALUE, ProfilePanelHeading } from "./profilePresentation";

const SUMMARY_ROWS: Array<{ key: string; label: string }> = [
  { key: "total_assets", label: "资产总额" },
  { key: "total_liabilities", label: "负债总额" },
  { key: "revenue", label: "营业收入" },
  { key: "net_profit", label: "净利润" },
  { key: "operating_cash_flow", label: "经营活动现金流" },
];

function sourceIdOf(fact: EntityProfileFact | undefined) {
  return fact ? (fact.source_ids || [])[0] || fact.source_id || "" : "";
}

/**
 * 工商字段行：名称与信用代码已在标题区展示，这里不再重复。
 * 有绑定证据的字段可以直接跳到「材料与证据」的对应一行。
 */
function FieldRow({ label, value, fact, missing, onOpenSource }: {
  label: string; value: string; fact?: EntityProfileFact; missing?: boolean; onOpenSource?: (sourceId: string) => void;
}) {
  const sourceId = sourceIdOf(fact);
  return <div className="ep-field-row">
    <dt>{label}</dt>
    <dd>
      <span className={missing ? "ep-missing-value" : ""}>{value}</span>
      {fact && fact.status !== "confirmed" && <FactStatusBadge status={fact.status} />}
      {sourceId && onOpenSource && <button className="ep-source-link" onClick={() => onOpenSource(sourceId)} title={factSourceLabel(fact)}><FileSearch size={12} />证据</button>}
    </dd>
  </div>;
}

/**
 * 概览：工商字段行 + 最近完整年度财务摘要。
 *
 * 财务摘要只展示**可用**事实——过期、冲突、材料不足的事实不参与计算，也不当正常值展示；
 * 完整三年报表与确定性指标在「财务资料」页。
 */
export function ProfileBusinessOverview({ profile, onOpenFinancial, onOpenSource }: {
  profile: EntityProfileDetail;
  onOpenFinancial: () => void;
  onOpenSource?: (sourceId: string) => void;
}) {
  const representative = factForKey(profile.facts, "legal_representative");
  const address = factForKey(profile.facts, "registered_address");
  const established = factForKey(profile.facts, "establishment_date");
  const capital = factForKey(profile.facts, "registered_capital");
  const year = latestAnnualPeriods(profile.facts, 1)[0] || "";
  const summaryFacts = year ? SUMMARY_ROWS.map((row) => financialFact(profile.facts, row.key, year)) : [];
  const citedSources = new Set(summaryFacts.map((fact) => sourceIdOf(fact)).filter(Boolean));
  const debtRatio = year ? metricByKey("debt_ratio", profile.facts, year) : null;

  return <div className="ep-business-overview">
    <section className="ep-panel">
      <ProfilePanelHeading title="工商信息" help="注册地址仅作界面脱敏展示（保留到省市区县层级），原始地址与证据定位保持不变。" action={<Building2 size={18} />} />
      <dl className="ep-field-list">
        <FieldRow label="法定代表人" fact={representative} value={representative?.text_value || MISSING_VALUE}
          missing={!representative?.text_value} onOpenSource={onOpenSource} />
        <FieldRow label="注册地址" fact={address} value={administrativeAddress(address?.text_value || "")}
          missing={!address?.text_value} onOpenSource={onOpenSource} />
        <FieldRow label="成立日期" fact={established} value={established?.text_value || MISSING_VALUE}
          missing={!established?.text_value} onOpenSource={onOpenSource} />
        <FieldRow label="注册资本" fact={capital}
          value={capital ? displayFactValue(capital.value, capital.text_value, capital.unit) : MISSING_VALUE}
          missing={capital ? financialFactValue(capital) == null && !capital.text_value : true} onOpenSource={onOpenSource} />
      </dl>
    </section>

    <section className="ep-panel">
      <ProfilePanelHeading
        title="财务摘要"
        description={year ? `${year} 年 · 合并口径优先 · 单位以各字段标注为准` : "尚无完整会计年度的结构化财务事实"}
        action={<Landmark size={18} />}
      />
      {!year ? <div className="ep-financial-empty"><Landmark size={20} /><span><strong>尚无完整年度财务数据</strong><small>缺失字段显示“待补充”，不会以零值代替。</small></span></div> : <>
        <dl className="ep-field-list">
          {SUMMARY_ROWS.map((row) => {
            const fact = financialFact(profile.facts, row.key, year);
            const usable = financialFactValue(fact) != null;
            return <FieldRow key={row.key} label={row.label} fact={fact} missing={!usable}
              value={usable ? displayFactValue(fact!.value, fact!.text_value, fact!.unit) : MISSING_VALUE}
              onOpenSource={onOpenSource} />;
          })}
          <FieldRow label="资产负债率" value={debtRatio == null ? MISSING_VALUE : formatMetricValue(debtRatio, "%")}
            missing={debtRatio == null} />
        </dl>
        <footer className="ep-panel-foot">
          <span>{citedSources.size ? `摘要字段绑定 ${citedSources.size} 份证据文件` : "摘要字段尚未绑定可定位证据"}</span>
          <button className="secondary-button small" onClick={onOpenFinancial}>查看完整财务资料<ArrowRight size={14} /></button>
        </footer>
      </>}
    </section>
  </div>;
}
