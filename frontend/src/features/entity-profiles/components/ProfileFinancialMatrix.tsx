import { Calculator, FileSearch, Landmark } from "lucide-react";
import type { EntityProfileFact, EntityProfileSource } from "../contracts";
import { financialFact, latestAnnualPeriods } from "../profileSummary";
import { displayFactValue, ProfilePanelHeading } from "./profilePresentation";

const FACT_GROUPS = [
  { label: "资产负债表", rows: [
    ["cash", "货币资金"], ["restricted_cash", "其中：受限资金"],
    ["accounts_receivable", "应收账款"], ["other_receivables", "其他应收款"],
    ["inventory", "存货"], ["current_assets", "流动资产"], ["total_assets", "资产总额"],
    ["short_term_debt", "短期借款及短期债务"], ["debt_due_12m", "一年内到期债务"],
    ["current_liabilities", "流动负债"], ["long_term_debt", "长期借款"],
    ["bonds_payable", "应付债券"], ["interest_bearing_debt", "有息负债"],
    ["total_liabilities", "负债总额"], ["equity", "所有者权益"],
  ] },
  { label: "利润表", rows: [
    ["revenue", "营业收入"], ["cost_of_goods_sold", "营业成本"],
    ["finance_expense", "财务费用"], ["interest_expense", "利息支出"], ["net_profit", "净利润"],
  ] },
  { label: "现金流量表", rows: [
    ["cash_received_from_sales", "销售商品、提供劳务收到的现金"],
    ["operating_cash_flow", "经营活动现金流量净额"],
    ["investing_cash_flow", "投资活动现金流量净额"],
    ["financing_cash_flow", "筹资活动现金流量净额"],
  ] },
] as const;

type MetricDefinition = {
  key: string;
  label: string;
  unit: string;
  inputs: string[];
  calculate: (values: number[]) => number | null;
};

const CORE_METRICS: MetricDefinition[] = [
  { key: "debt_ratio", label: "资产负债率", unit: "%", inputs: ["total_liabilities", "total_assets"], calculate: ([liabilities, assets]) => assets ? liabilities / assets * 100 : null },
  { key: "current_ratio", label: "流动比率", unit: "倍", inputs: ["current_assets", "current_liabilities"], calculate: ([assets, liabilities]) => liabilities ? assets / liabilities : null },
  { key: "available_cash", label: "可用现金", unit: "万元", inputs: ["cash", "restricted_cash"], calculate: ([cash, restricted]) => cash >= restricted ? cash - restricted : null },
  { key: "cash_short_debt_ratio", label: "现金短债覆盖", unit: "倍", inputs: ["cash", "restricted_cash", "debt_due_12m"], calculate: ([cash, restricted, due]) => due && cash >= restricted ? (cash - restricted) / due : null },
  { key: "interest_bearing_debt_capitalization", label: "有息债务资本化率", unit: "%", inputs: ["interest_bearing_debt", "equity"], calculate: ([debt, equity]) => debt + equity ? debt / (debt + equity) * 100 : null },
  { key: "receivables_revenue_ratio", label: "应收收入比", unit: "%", inputs: ["accounts_receivable", "revenue"], calculate: ([receivables, revenue]) => revenue ? receivables / revenue * 100 : null },
  { key: "cash_profit_ratio", label: "现金利润比", unit: "倍", inputs: ["operating_cash_flow", "net_profit"], calculate: ([cashflow, profit]) => profit ? cashflow / profit : null },
  { key: "operating_cashflow_debt_ratio", label: "经营现金流债务比", unit: "%", inputs: ["operating_cash_flow", "interest_bearing_debt"], calculate: ([cashflow, debt]) => debt ? cashflow / debt * 100 : null },
];

const unusableStatuses = new Set(["stale", "conflicted", "insufficient"]);

function sourceForFact(fact: EntityProfileFact | undefined, sources: EntityProfileSource[]) {
  if (!fact) return undefined;
  const sourceId = (fact.source_ids || [])[0] || fact.source_id;
  return sources.find((source) => source.id === sourceId);
}

function factValue(fact: EntityProfileFact | undefined) {
  return fact && !unusableStatuses.has(fact.status) && fact.value != null ? fact.value : null;
}

function metricValue(metric: MetricDefinition, facts: EntityProfileFact[], year: string) {
  const values = metric.inputs.map((key) => factValue(financialFact(facts, key, year)));
  if (values.some((value) => value == null)) return null;
  return metric.calculate(values as number[]);
}

function formatMetric(value: number | null, unit: string) {
  if (value == null || !Number.isFinite(value)) return "无法计算";
  const decimals = unit === "万元" ? 0 : 2;
  return `${value.toLocaleString(undefined, { maximumFractionDigits: decimals, minimumFractionDigits: decimals })} ${unit}`;
}

function statusLabel(fact: EntityProfileFact | undefined) {
  if (!fact) return "材料不足";
  if (fact.status === "confirmed" || fact.status === "single_source") return "";
  return fact.status === "stale" ? "资料已过期" : fact.status === "conflicted" ? "存在冲突" : "材料不足";
}

export function ProfileFinancialMatrix({
  facts,
  sources = [],
  title = "近三年主要财务报表",
  onOpenSource,
}: {
  facts: EntityProfileFact[];
  sources: EntityProfileSource[];
  title?: string;
  onOpenSource?: (sourceId: string) => void;
}) {
  const years = latestAnnualPeriods(facts);
  if (!years.length) return <section className="ep-panel"><ProfilePanelHeading title={title} description="仅展示三个完整会计年度" action={<Landmark size={18} />} /><div className="ep-financial-empty"><Landmark size={20} /><span><strong>三年财务数据尚未形成结构化事实</strong><small>缺失字段保持“材料不足”，不会以零值代替。</small></span></div></section>;

  return <section className="ep-panel ep-financial-matrix">
    <ProfilePanelHeading title={title} description="三个完整年度 · 合并口径优先 · 单位以各字段标注为准" action={<Landmark size={18} />} />
    <div className="ep-financial-table" role="table" aria-label={title}>
      <div className="ep-financial-row ep-financial-head" role="row"><span role="columnheader">科目</span>{years.map((year) => <strong role="columnheader" key={year}>{year}年</strong>)}</div>
      {FACT_GROUPS.map((group) => <div className="ep-financial-group" key={group.label}>
        <div className="ep-financial-group-label">{group.label}</div>
        {group.rows.map(([key, label]) => <div className="ep-financial-row" role="row" key={key}>
          <strong role="rowheader">{label}</strong>
          {years.map((year) => {
            const fact = financialFact(facts, key, year);
            const source = sourceForFact(fact, sources);
            const sourceLabel = source ? `${source.filename}${source.page ? ` P${source.page}` : ""}` : fact?.source_label || "";
            const stateLabel = statusLabel(fact);
            return <span className={!fact || fact.status === "insufficient" ? "missing" : ""} role="cell" key={year}>
              {fact ? displayFactValue(fact.value, fact.text_value, fact.unit) : "材料不足"}
              {(stateLabel || (!onOpenSource && sourceLabel)) && <small>{[stateLabel, !onOpenSource ? sourceLabel : ""].filter(Boolean).join(" · ")}</small>}
              {source && onOpenSource && <button className="ep-source-link" onClick={() => onOpenSource(source.id)} title={source.quote}><FileSearch size={12} />{source.page ? `查看 P${source.page}` : source.locator}</button>}
            </span>;
          })}
        </div>)}
      </div>)}
    </div>
    <div className="ep-financial-mobile" aria-label={`${title}移动端视图`}>{years.map((year) => <article key={year}><h4>{year}年 · 合并口径</h4>{FACT_GROUPS.map((group) => <div className="ep-mobile-financial-group" key={group.label}><b>{group.label}</b>{group.rows.map(([key, label]) => { const fact = financialFact(facts, key, year); return <div key={key}><span>{label}</span><strong>{fact ? displayFactValue(fact.value, fact.text_value, fact.unit) : "材料不足"}</strong></div>; })}</div>)}<div className="ep-mobile-financial-group ep-mobile-metrics"><b>系统确定性指标</b>{CORE_METRICS.map((metric) => <div key={metric.key}><span>{metric.label}</span><strong>{formatMetric(metricValue(metric, facts, year), metric.unit)}</strong></div>)}</div></article>)}</div>
    <div className="ep-derived-heading"><div><Calculator size={16} /><span><strong>系统确定性指标</strong><small>仅由上表同年度、同口径事实计算；缺项或分母为零时不估算。</small></span></div></div>
    <div className="ep-metric-matrix" role="table" aria-label="融资担保核心财务指标">
      <div className="ep-metric-row ep-metric-head" role="row"><span role="columnheader">指标</span>{years.map((year) => <strong role="columnheader" key={year}>{year}年</strong>)}</div>
      {CORE_METRICS.map((metric) => <div className="ep-metric-row" role="row" key={metric.key}><strong role="rowheader">{metric.label}</strong>{years.map((year) => <span role="cell" className={metricValue(metric, facts, year) == null ? "missing" : ""} key={year}>{formatMetric(metricValue(metric, facts, year), metric.unit)}</span>)}</div>)}
    </div>
  </section>;
}
