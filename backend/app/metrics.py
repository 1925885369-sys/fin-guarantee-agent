from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal

from .database import connect, json_dump, json_load, rows


METRIC_NAMES = {
    "revenue_growth": ("营业收入增长率", "%"),
    "profit_growth": ("净利润增长率", "%"),
    "gross_margin": ("毛利率", "%"),
    "net_margin": ("净利率", "%"),
    "cash_profit_ratio": ("现金利润比", "倍"),
    "debt_ratio": ("资产负债率", "%"),
    "current_ratio": ("流动比率", "倍"),
    "available_cash": ("可用现金", "万元"),
    "cash_short_debt_ratio": ("现金短债覆盖", "倍"),
    "interest_bearing_debt_capitalization": ("有息债务资本化率", "%"),
    "receivables_revenue_ratio": ("应收收入比", "%"),
    "operating_cashflow_debt_ratio": ("经营现金流债务比", "%"),
    "quick_ratio": ("速动比率", "倍"),
    "ar_turnover_days": ("应收账款周转天数", "天"),
    "inventory_turnover_days": ("存货周转天数", "天"),
    # 计划 §三 新增白名单：周转率（次，用期初期末平均余额）与结构占比。
    # 名称必须与 METRIC_NAMES 一致——指标记录按 ``name`` 做中文科目绑定。
    "ar_turnover": ("应收账款周转率", "次"),
    "inventory_turnover": ("存货周转率", "次"),
    "non_current_assets_ratio": ("非流动资产占比", "%"),
    "non_current_liabilities_ratio": ("非流动负债占比", "%"),
    "dscr": ("债务偿付覆盖率", "倍"),
    "guarantee_to_equity": ("担保申请额/净资产", "%"),
    "contingent_ratio": ("或有负债率", "%"),
    "collateral_coverage": ("抵押净覆盖率", "%"),
}


# 指标**叙述别名**（2026-10-16 旧计划剩余项复验）：正文常把指标写成"分子/分母"的
# 说明性短语（"可用资金对未来12个月到期债务的覆盖"）而非规范名（"现金短债覆盖"），
# 原 `_record_binding` 词表只含规范名 → 造出假 label_mismatch（华辰 financial #28
# 的 0.29 倍实测）。别名随**指标记录**冻结（写成 `extra.label_aliases`），词表侧按
# 别名子串匹配；别名**只由公式/规范名机械派生**，不做算术放宽（数字支持仍须记录）。
# ⚠️ 别名须 **≤ ``SUBJECT_LABEL_WINDOW``（16 字）**：绑定只看数字前 16 字窗口，超长
#    别名永远配不上。故登记**有辨识度的短核心短语**（"到期债务的覆盖"等），不登记整句。
METRIC_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    # 覆盖率/比值类：正文写"X 对 Y 的覆盖""X 覆盖 Y"。
    "cash_short_debt_ratio": (
        "到期债务的覆盖", "短期债务的覆盖", "现金短债覆盖率", "可用资金覆盖",
        "一年内到期债务的覆盖", "12个月到期债务的覆盖", "未来12个月到期债务的覆盖",
    ),
    "debt_ratio": ("负债率", "资产负债率"),
    "current_ratio": ("流动比",),
    "quick_ratio": ("速动比",),
    "available_cash": ("可动用资金", "非受限资金", "可用货币资金", "可用资金"),
    "operating_cashflow_debt_ratio": ("现金流的覆盖", "经营现金流债务覆盖率"),
    # 偿债覆盖倍数的散文写法（"2025年偿债覆盖倍数为0.42倍"——华辰 debt #12 实测：
    # 规范名"债务偿付覆盖率"与正文用词配不上会造出假 label_mismatch）。
    "dscr": ("偿债覆盖倍数", "偿债覆盖率", "债务偿付覆盖倍数", "偿债覆盖"),
    "cash_profit_ratio": ("对净利润的覆盖",),
    "guarantee_to_equity": ("对净资产的覆盖", "担保额占净资产"),
    "collateral_coverage": ("抵押净覆盖倍率", "抵押的覆盖"),
}


def _decimal(value) -> Decimal | None:
    """事实值 → Decimal；不可解析或缺失返回 None（宁可不出指标，也不猜数）。"""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _by_key(facts: list[dict]) -> dict[str, dict[str, dict]]:
    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for fact in facts:
        if fact.get("value") is not None:
            grouped[fact["fact_key"]][fact.get("period", "")] = fact
    return grouped


def _latest(grouped: dict[str, dict[str, dict]], key: str) -> dict | None:
    values = grouped.get(key, {})
    return values[max(values)] if values else None


def _common(grouped: dict[str, dict[str, dict]], keys: list[str], count: int = 1) -> list[tuple[str, list[dict]]]:
    periods = set.intersection(*(set(grouped.get(key, {})) for key in keys)) if keys else set()
    periods = sorted((period for period in periods if period), reverse=True)[:count]
    return [(period, [grouped[key][period] for key in keys]) for period in periods]


def _sources(facts: list[dict]) -> list[str]:
    return list(dict.fromkeys(source for fact in facts for source in fact.get("source_ids", [])))


def _metric(key: str, period: str, value: float, formula: str, facts: list[dict]) -> dict:
    name, unit = METRIC_NAMES[key]
    return {
        "id": str(uuid.uuid4()), "metric_key": key, "name": name, "period": period,
        "value": round(value, 4), "unit": unit, "formula": formula,
        "input_fact_ids": [fact["id"] for fact in facts], "source_ids": _sources(facts), "status": "calculated",
        "subject_id": facts[0].get("subject_id", "") if facts else "",
        "scope": facts[0].get("scope", "unknown") if facts else "unknown",
        "trace_json": {
            "formula": formula,
            "inputs": [{"fact_id": fact["id"], "fact_key": fact.get("fact_key", ""), "value": fact.get("value"), "unit": fact.get("unit", ""), "period": fact.get("period", "")} for fact in facts],
        },
    }


def calculate_metrics(facts: list[dict], application_amount: float) -> list[dict]:
    blocked_statuses = {"stale", "conflicted", "insufficient"}
    facts = [
        fact for fact in facts
        if fact.get("assertion_status") not in blocked_statuses
        and fact.get("review_status") not in blocked_statuses
        and fact.get("status") not in blocked_statuses
    ]
    financial_scopes = {fact.get("scope") for fact in facts if fact.get("scope") in {"consolidated", "standalone"}}
    preferred_scope = "consolidated" if "consolidated" in financial_scopes else "standalone" if "standalone" in financial_scopes else None
    if preferred_scope:
        facts = [fact for fact in facts if fact.get("scope") in {preferred_scope, "entity", "transaction", "unknown", None}]
    grouped = _by_key(facts)
    output: list[dict] = []

    for key, metric_key in (("revenue", "revenue_growth"), ("net_profit", "profit_growth")):
        periods = sorted((period for period in grouped.get(key, {}) if period), reverse=True)[:2]
        if len(periods) == 2:
            new, old = grouped[key][periods[0]], grouped[key][periods[1]]
            if old["value"]:
                output.append(_metric(metric_key, periods[0], (new["value"] / old["value"] - 1) * 100, "(本期-上期)/上期", [new, old]))

    pair = _common(grouped, ["gross_profit", "revenue"])
    if pair and pair[0][1][1]["value"]:
        period, (gross, revenue) = pair[0]
        output.append(_metric("gross_margin", period, gross["value"] / revenue["value"] * 100, "毛利润/营业收入", [gross, revenue]))
    elif (pair := _common(grouped, ["revenue", "cost_of_goods_sold"])) and pair[0][1][0]["value"]:
        period, (revenue, cost) = pair[0]
        output.append(_metric("gross_margin", period, (revenue["value"] - cost["value"]) / revenue["value"] * 100, "(营业收入-营业成本)/营业收入", [revenue, cost]))

    calculations = [
        ("net_margin", ["net_profit", "revenue"], lambda x: x[0]["value"] / x[1]["value"] * 100, "净利润/营业收入"),
    ]
    for metric_key, keys, calculator, formula in calculations:
        pair = _common(grouped, keys)
        if pair and pair[0][1][1]["value"]:
            period, metric_facts = pair[0]
            output.append(_metric(metric_key, period, calculator(metric_facts), formula, metric_facts))

    # Portfolio-facing core metrics are calculated for the latest three
    # complete years. The LLM receives only these deterministic results and
    # never calculates ratios itself.
    annual_calculations = [
        ("cash_profit_ratio", ["operating_cash_flow", "net_profit"], lambda x: x[0]["value"] / x[1]["value"], "经营现金流净额/净利润", 1),
        ("debt_ratio", ["total_liabilities", "total_assets"], lambda x: x[0]["value"] / x[1]["value"] * 100, "负债总额/资产总额", 1),
        ("current_ratio", ["current_assets", "current_liabilities"], lambda x: x[0]["value"] / x[1]["value"], "流动资产/流动负债", 1),
        ("cash_short_debt_ratio", ["cash", "restricted_cash", "debt_due_12m"], lambda x: (x[0]["value"] - x[1]["value"]) / x[2]["value"], "(货币资金-受限资金)/一年内到期债务", 2),
        ("interest_bearing_debt_capitalization", ["interest_bearing_debt", "equity"], lambda x: x[0]["value"] / (x[0]["value"] + x[1]["value"]) * 100, "有息负债/(有息负债+所有者权益)", None),
        ("receivables_revenue_ratio", ["accounts_receivable", "revenue"], lambda x: x[0]["value"] / x[1]["value"] * 100, "应收账款/营业收入", 1),
        ("operating_cashflow_debt_ratio", ["operating_cash_flow", "interest_bearing_debt"], lambda x: x[0]["value"] / x[1]["value"] * 100, "经营现金流净额/有息负债", 1),
    ]
    for metric_key, keys, calculator, formula, denominator_index in annual_calculations:
        for period, metric_facts in reversed(_common(grouped, keys, count=3)):
            if len(period) != 4 or not period.isdigit():
                continue
            if denominator_index is None:
                denominator_ok = metric_facts[0]["value"] + metric_facts[1]["value"] != 0
            else:
                denominator_ok = metric_facts[denominator_index]["value"] != 0
            if denominator_ok:
                output.append(_metric(metric_key, period, calculator(metric_facts), formula, metric_facts))

    for period, metric_facts in reversed(_common(grouped, ["cash", "restricted_cash"], count=3)):
        if len(period) == 4 and period.isdigit() and metric_facts[0]["value"] >= metric_facts[1]["value"]:
            output.append(_metric("available_cash", period, metric_facts[0]["value"] - metric_facts[1]["value"], "货币资金-受限资金", metric_facts))

    # 三年速动比率（计划 §三）：原实现只出最新一期，正文写"速动比率三年均低于1倍"时
    # 只有 2025 有记录，另外两年无处可依。
    for period, metric_facts in reversed(_common(
            grouped, ["current_assets", "inventory", "current_liabilities"], count=3)):
        if len(period) != 4 or not period.isdigit():
            continue
        if metric_facts[2]["value"]:
            output.append(_metric("quick_ratio", period,
                                  (metric_facts[0]["value"] - metric_facts[1]["value"]) / metric_facts[2]["value"],
                                  "(流动资产-存货)/流动负债", metric_facts))

    # 周转率（计划 §三）：营业收入／平均应收账款、营业成本／平均存货。
    # 平均余额＝（期初＋期末）/2；**缺期初数据不计算**，不用期末余额替代。
    # 用 Decimal 计算，避免二进制浮点在"贴不贴边"上抖动。
    for metric_key, flow_key, balance_key, formula in (
        ("ar_turnover", "revenue", "accounts_receivable", "营业收入/平均应收账款"),
        ("inventory_turnover", "cost_of_goods_sold", "inventory", "营业成本/平均存货"),
    ):
        flow_by_period = grouped.get(flow_key, {})
        balance_by_period = grouped.get(balance_key, {})
        for period in sorted(
                (item for item in flow_by_period if len(item) == 4 and item.isdigit()), reverse=True)[:3]:
            opening = balance_by_period.get(str(int(period) - 1))
            closing = balance_by_period.get(period)
            flow = flow_by_period.get(period)
            if not (opening and closing and flow):
                continue
            flow_value = _decimal(flow["value"])
            opening_value = _decimal(opening["value"])
            closing_value = _decimal(closing["value"])
            if flow_value is None or opening_value is None or closing_value is None:
                continue
            average = (opening_value + closing_value) / 2
            if average == 0:
                continue
            output.append(_metric(metric_key, period, float(flow_value / average),
                                  formula, [flow, opening, closing]))

    # 结构占比（计划 §三）：非流动资产／资产总计、非流动负债／负债合计。
    # 分子由同一份合计减去流动部分得到（资产总计−流动资产合计），不引入外部数字；
    # 公式里显式写出派生关系，输入事实 ID 同时冻结合计与流动项。
    for metric_key, total_key, flow_key, formula in (
        ("non_current_assets_ratio", "total_assets", "current_assets",
         "非流动资产/资产总计（非流动资产=资产总计-流动资产合计）"),
        ("non_current_liabilities_ratio", "total_liabilities", "current_liabilities",
         "非流动负债/负债合计（非流动负债=负债合计-流动负债合计）"),
    ):
        for period, metric_facts in reversed(_common(grouped, [total_key, flow_key], count=3)):
            if len(period) != 4 or not period.isdigit():
                continue
            total_value = _decimal(metric_facts[0]["value"])
            flow_value = _decimal(metric_facts[1]["value"])
            if total_value is None or flow_value is None or total_value == 0:
                continue
            output.append(_metric(metric_key, period,
                                  float((total_value - flow_value) / total_value * 100),
                                  formula, metric_facts))

    group = _common(grouped, ["accounts_receivable", "revenue"])
    if group and group[0][1][1]["value"]:
        period, metric_facts = group[0]
        output.append(_metric("ar_turnover_days", period, metric_facts[0]["value"] / metric_facts[1]["value"] * 365, "应收账款/营业收入×365", metric_facts))
    group = _common(grouped, ["inventory", "cost_of_goods_sold"])
    if group and group[0][1][1]["value"]:
        period, metric_facts = group[0]
        output.append(_metric("inventory_turnover_days", period, metric_facts[0]["value"] / metric_facts[1]["value"] * 365, "存货/营业成本×365", metric_facts))

    due, cashflow, interest = _latest(grouped, "debt_due_12m"), _latest(grouped, "operating_cash_flow"), _latest(grouped, "interest_expense")
    if due and cashflow and due["value"] + (interest["value"] if interest else 0):
        metric_facts = [cashflow, due] + ([interest] if interest else [])
        interest_value = interest["value"] if interest else 0
        output.append(_metric("dscr", due.get("period", ""), (cashflow["value"] + interest_value) / (due["value"] + interest_value), "(经营现金流+利息)/(一年内到期债务+利息)", metric_facts))

    equity = _latest(grouped, "equity")
    if equity and equity["value"]:
        output.append(_metric("guarantee_to_equity", equity.get("period", ""), application_amount / equity["value"] * 100, "担保申请额/所有者权益", [equity]))
        contingent_facts = [item for item in (_latest(grouped, "external_guarantee"), _latest(grouped, "contingent_liabilities")) if item]
        if contingent_facts:
            output.append(_metric("contingent_ratio", equity.get("period", ""), sum(item["value"] for item in contingent_facts) / equity["value"] * 100, "(对外担保+其他或有负债)/所有者权益", [*contingent_facts, equity]))

    collateral = _latest(grouped, "collateral_discounted_value")
    if collateral and application_amount:
        output.append(_metric("collateral_coverage", collateral.get("period", ""), collateral["value"] / application_amount * 100, "折后抵押价值/担保申请额", [collateral]))
    return output


def compute_and_store_metrics(project_id: str, application_amount: float, policy_version: str) -> list[dict]:
    from .professional import analysis_fact_rows

    calculated = calculate_metrics(analysis_fact_rows(project_id), application_amount)
    with connect() as conn:
        conn.execute("DELETE FROM financial_metrics WHERE project_id=?", (project_id,))
        for item in calculated:
            conn.execute(
                "INSERT INTO financial_metrics (id,project_id,metric_key,name,period,value,unit,formula,input_fact_ids,source_ids,status,policy_version,subject_id,scope,trace_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["id"], project_id, item["metric_key"], item["name"], item["period"], item["value"], item["unit"],
                 item["formula"], json_dump(item["input_fact_ids"]), json_dump(item["source_ids"]), item["status"], policy_version,
                 item["subject_id"], item["scope"], json_dump(item["trace_json"])),
            )
    return decode_metrics(rows("SELECT * FROM financial_metrics WHERE project_id=? ORDER BY metric_key,period", (project_id,)))


def decode_metrics(items: list[dict]) -> list[dict]:
    for item in items:
        item["input_fact_ids"] = json_load(item["input_fact_ids"])
        item["source_ids"] = json_load(item["source_ids"])
        item["trace_json"] = json_load(item.get("trace_json", "{}"))
    return items
