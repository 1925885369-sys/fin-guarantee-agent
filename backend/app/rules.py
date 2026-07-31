from __future__ import annotations

from dataclasses import dataclass

from .database import json_load
from .facts import canonical_key


@dataclass
class RuleResult:
    key: str
    title: str
    severity: str
    status: str
    summary: str
    values: list[dict]
    source_ids: list[str]
    formula: str
    fact_ids: list[str]


RULE_META = {
    "revenue_tax": ("报表收入与纳税收入核验", "medium"),
    "revenue_bank": ("销售收入与经营流入核验", "high"),
    "ar_growth": ("应收账款增速核验", "high"),
    "cash_profit": ("利润与经营现金流核验", "high"),
    "leverage": ("资产负债与流动性核验", "medium"),
    "customer": ("客户集中度核验", "high"),
    "related": ("关联方资金往来核验", "high"),
    "purpose": ("融资用途合同覆盖核验", "medium"),
    "collateral": ("抵押物覆盖与估值时效核验", "high"),
    "debt_due": ("短期债务集中度核验", "high"),
}


def _prepare(facts: list[dict]) -> list[dict]:
    prepared = []
    for fact in facts:
        item = dict(fact)
        item["fact_key"] = item.get("fact_key") or canonical_key(item.get("name", ""))
        source_ids = item.get("source_ids", [])
        item["source_ids"] = json_load(source_ids) if isinstance(source_ids, str) else source_ids
        prepared.append(item)
    return prepared


def _for_key(facts: list[dict], key: str) -> list[dict]:
    return [item for item in facts if item.get("fact_key") == key and item.get("value") is not None]


def _latest(facts: list[dict], key: str) -> dict | None:
    candidates = _for_key(facts, key)
    return max(candidates, key=lambda item: item.get("period", ""), default=None)


def _common(facts: list[dict], keys: list[str], count: int = 1) -> list[tuple[str, list[dict]]]:
    by_key = {key: {item.get("period", ""): item for item in _for_key(facts, key)} for key in keys}
    common = set.intersection(*(set(items) for items in by_key.values())) if by_key else set()
    periods = sorted((period for period in common if period), reverse=True)[:count]
    return [(period, [by_key[key][period] for key in keys]) for period in periods]


def _result(key: str, status: str, summary: str, values: list[dict], facts: list[dict], formula: str) -> RuleResult:
    title, severity = RULE_META[key]
    sources: list[str] = []
    fact_ids: list[str] = []
    for fact in facts:
        if fact.get("id"):
            fact_ids.append(fact["id"])
        sources.extend(fact.get("source_ids", []))
    return RuleResult(key, title, severity, status, summary, values, list(dict.fromkeys(sources)), formula, fact_ids)


def _insufficient(key: str, required: str, formula: str) -> RuleResult:
    return _result(key, "insufficient", f"材料不足：缺少{required}，本项未作风险判断。", [], [], formula)


def evaluate_rules(facts: list[dict], application_amount: float) -> list[RuleResult]:
    facts = _prepare(facts)
    results: list[RuleResult] = []

    pair = _common(facts, ["revenue", "tax_revenue"])
    if not pair:
        results.append(_insufficient("revenue_tax", "同期间营业收入或纳税申报收入", "|营业收入-纳税收入|/|营业收入|"))
    else:
        period, (revenue, tax) = pair[0]
        base = abs(float(revenue["value"]))
        ratio = abs(float(revenue["value"]) - float(tax["value"])) / base * 100 if base else 0
        warning = base == 0 or ratio >= 10
        summary = f"{period}年两类收入差异率为{ratio:.1f}%，{'达到' if warning else '未达到'}10%关注阈值。"
        results.append(_result("revenue_tax", "warning" if warning else "passed", summary,
                               [{"label": "报表收入", "value": revenue["value"]}, {"label": "纳税收入", "value": tax["value"]}],
                               [revenue, tax], "|营业收入-纳税收入|/|营业收入|"))

    pair = _common(facts, ["revenue", "operating_inflow"])
    if not pair:
        results.append(_insufficient("revenue_bank", "同期间营业收入或银行经营性流入", "经营性流入/营业收入"))
    else:
        period, (revenue, bank) = pair[0]
        ratio = float(bank["value"]) / float(revenue["value"]) * 100 if revenue["value"] else 0
        warning = not 80 <= ratio <= 120
        results.append(_result("revenue_bank", "warning" if warning else "passed",
                               f"{period}年经营性流入覆盖营业收入{ratio:.1f}%，{'超出' if warning else '处于'}80%–120%核验区间。",
                               [{"label": "报表收入", "value": revenue["value"]}, {"label": "经营性流入", "value": bank["value"]}],
                               [revenue, bank], "经营性流入/营业收入"))

    periods = _common(facts, ["revenue", "accounts_receivable"], 2)
    if len(periods) < 2:
        results.append(_insufficient("ar_growth", "连续两期营业收入和应收账款", "应收增速-收入增速"))
    else:
        latest_period, (rev_new, ar_new) = periods[0]
        _, (rev_old, ar_old) = periods[1]
        if not rev_old["value"] or not ar_old["value"]:
            results.append(_insufficient("ar_growth", "非零的上期营业收入和应收账款", "应收增速-收入增速"))
        else:
            rev_growth = (float(rev_new["value"]) / float(rev_old["value"]) - 1) * 100
            ar_growth = (float(ar_new["value"]) / float(ar_old["value"]) - 1) * 100
            gap = ar_growth - rev_growth
            warning = gap >= 15
            results.append(_result("ar_growth", "warning" if warning else "passed",
                                   f"截至{latest_period}年，应收增速{ar_growth:.1f}%，收入增速{rev_growth:.1f}%，差值{gap:.1f}个百分点。",
                                   [{"label": "应收增速", "value": round(ar_growth, 1)}, {"label": "收入增速", "value": round(rev_growth, 1)}],
                                   [rev_new, ar_new, rev_old, ar_old], "应收增速-收入增速"))

    pair = _common(facts, ["net_profit", "operating_cash_flow"])
    if not pair:
        results.append(_insufficient("cash_profit", "同期间净利润或经营现金流净额", "净利润>0且经营现金流<0"))
    else:
        period, (profit, cashflow) = pair[0]
        warning = float(profit["value"]) > 0 and float(cashflow["value"]) < 0
        results.append(_result("cash_profit", "warning" if warning else "passed",
                               f"{period}年净利润{profit['value']:.0f}万元，经营现金流净额{cashflow['value']:.0f}万元，{'存在背离' if warning else '未触发正利润负现金流条件'}。",
                               [{"label": "净利润", "value": profit["value"]}, {"label": "经营现金流", "value": cashflow["value"]}],
                               [profit, cashflow], "净利润>0且经营现金流<0"))

    group = _common(facts, ["total_assets", "total_liabilities", "current_assets", "current_liabilities"])
    if not group:
        results.append(_insufficient("leverage", "同期间资产、负债、流动资产和流动负债", "负债/资产；流动资产/流动负债"))
    else:
        period, (assets, liabilities, current_assets, current_liabilities) = group[0]
        if not assets["value"] or not current_liabilities["value"]:
            results.append(_insufficient("leverage", "非零的资产总额和流动负债", "负债/资产；流动资产/流动负债"))
        else:
            leverage = float(liabilities["value"]) / float(assets["value"]) * 100
            current_ratio = float(current_assets["value"]) / float(current_liabilities["value"])
            warning = leverage >= 70 or current_ratio < 1.2
            results.append(_result("leverage", "warning" if warning else "passed",
                                   f"{period}年资产负债率{leverage:.1f}%，流动比率{current_ratio:.2f}，{'触发' if warning else '未触发'}阈值。",
                                   [{"label": "资产负债率", "value": round(leverage, 1)}, {"label": "流动比率", "value": round(current_ratio, 2)}],
                                   [assets, liabilities, current_assets, current_liabilities], "负债/资产；流动资产/流动负债"))

    customer = _latest(facts, "top_customer_share")
    if not customer:
        results.append(_insufficient("customer", "第一大客户收入占比", "第一大客户占比>=30%"))
    else:
        warning = float(customer["value"]) >= 30
        results.append(_result("customer", "warning" if warning else "passed",
                               f"第一大客户收入占比{customer['value']:.1f}%，{'达到' if warning else '未达到'}30%关注阈值。",
                               [{"label": "第一大客户占比", "value": customer["value"]}], [customer], "第一大客户占比>=30%"))

    related = _latest(facts, "related_unsupported_amount")
    if not related:
        results.append(_insufficient("related", "无合同支持的关联方往来金额", "无合同关联方往来>0"))
    else:
        warning = float(related["value"]) > 0
        results.append(_result("related", "warning" if warning else "passed",
                               f"识别无合同支持的关联方往来{related['value']:.0f}万元。" if warning else "未识别无合同支持的关联方往来。",
                               [{"label": "异常往来", "value": related["value"]}], [related], "无合同关联方往来>0"))

    contract = _latest(facts, "supporting_contract_amount")
    if not contract:
        results.append(_insufficient("purpose", "用途合同金额", "用途合同金额/申请金额"))
    else:
        coverage = float(contract["value"]) / application_amount * 100 if application_amount else 0
        warning = coverage < 100
        results.append(_result("purpose", "warning" if warning else "passed",
                               f"已提供用途合同覆盖申请金额{coverage:.1f}%，{'低于' if warning else '达到'}100%。",
                               [{"label": "用途合同", "value": contract["value"]}, {"label": "申请金额", "value": application_amount}],
                               [contract], "用途合同金额/申请金额"))

    collateral = _latest(facts, "collateral_discounted_value")
    age = _latest(facts, "collateral_valuation_age_months")
    if not collateral or not age:
        results.append(_insufficient("collateral", "折后抵押价值或估值账龄", "折后价值/申请金额；估值账龄"))
    else:
        coverage = float(collateral["value"]) / application_amount * 100 if application_amount else 0
        warning = coverage < 100 or float(age["value"]) > 12
        results.append(_result("collateral", "warning" if warning else "passed",
                               f"抵押物折后覆盖率{coverage:.1f}%，估值距今{age['value']:.0f}个月，{'触发' if warning else '未触发'}阈值。",
                               [{"label": "折后抵押价值", "value": collateral["value"]}, {"label": "估值账龄（月）", "value": age["value"]}],
                               [collateral, age], "折后价值/申请金额；估值账龄"))

    due = _latest(facts, "debt_due_12m")
    debt = _latest(facts, "interest_bearing_debt")
    if not due or not debt or not debt["value"]:
        results.append(_insufficient("debt_due", "未来12个月到期债务或有息负债", "到期债务/有息负债"))
    else:
        ratio = float(due["value"]) / float(debt["value"]) * 100
        warning = ratio >= 50
        results.append(_result("debt_due", "warning" if warning else "passed",
                               f"未来12个月到期债务占有息负债{ratio:.1f}%，{'达到' if warning else '未达到'}50%集中度阈值。",
                               [{"label": "一年内到期", "value": due["value"]}, {"label": "有息负债", "value": debt["value"]}],
                               [due, debt], "到期债务/有息负债"))
    return results


def risks_from_checks(checks: list[RuleResult]) -> list[dict]:
    mapping = {
        "revenue_tax": ("收入真实性", "报表与纳税收入差异较大", "不同口径收入存在明显差异，收入确认需进一步核实", "抽查发票、申报表和收入明细并形成差异调节表"),
        "revenue_bank": ("经营与现金流", "收入回款匹配度异常", "收入真实性及回款能力存在不确定性", "补充主要账户明细并抽查合同、发票和回款闭环"),
        "ar_growth": ("经营与现金流", "应收账款增长过快", "资金占用扩大并可能形成坏账", "取得应收明细、期后回款及主要客户函证"),
        "cash_profit": ("财务", "利润与经营现金流背离", "账面盈利未形成可支配现金", "分析现金流差异并设置回款监管条件"),
        "leverage": ("财务", "杠杆或短期流动性承压", "债务负担和短期偿付压力偏高", "核实流动资产质量并制定债务偿付计划"),
        "customer": ("经营", "客户集中度偏高", "核心客户流失将显著影响偿债来源", "核查合作稳定性、在手订单和替代客户计划"),
        "related": ("关联交易", "关联方往来缺少商业依据", "可能存在资金占用或贷款挪用风险", "提供合同、发票、审批记录并核查资金最终流向"),
        "purpose": ("融资用途", "用途材料未完全覆盖申请金额", "部分资金用途缺少合同支持", "补齐用途材料并实施受托支付"),
        "collateral": ("反担保", "抵押物覆盖或估值时效不足", "处置价值可能不能覆盖担保敞口", "重新评估并追加保证或其他抵押物"),
        "debt_due": ("偿债", "短期债务集中到期", "续贷依赖较高，存在流动性压力", "取得授信批复和还款计划并设置债务监测条件"),
    }
    output = []
    for check in checks:
        if check.status != "warning":
            continue
        category, title, impact, action = mapping[check.key]
        output.append({"severity": check.severity, "category": category, "title": title,
                       "description": check.summary, "impact": impact, "action": action,
                       "source_ids": check.source_ids})
    return output
