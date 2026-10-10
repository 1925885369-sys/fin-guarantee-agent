from __future__ import annotations

import re
from dataclasses import dataclass

from .chapter_prompts import BOND_CHAPTER_TITLES
from .database import row, rows, json_load
from .facts import FACT_REGISTRY, material_inventory


@dataclass
class BondChapter:
    key: str
    title: str
    content: str
    source_ids: list[str]


def _facts(project_id: str) -> list[dict]:
    items = rows("SELECT * FROM facts WHERE project_id=? ORDER BY category,name,period", (project_id,))
    for item in items:
        item["source_ids"] = json_load(item.get("source_ids", "[]"))
    return items


def _canonical_facts(project_id: str) -> list[dict]:
    items = rows("SELECT * FROM canonical_facts WHERE project_id=? ORDER BY category,name,period", (project_id,))
    for item in items:
        item["assertion_ids"] = json_load(item.get("assertion_ids", "[]"))
    return items


def _display_number(value: object) -> str:
    """数值展示：千分位，**不出现科学计数法**。

    ``f"{value:g}"`` 对大数会输出科学计数法——实测报告正文里印出过
    "2023年2.18035e+06万元" 这种写法（修订稿也点名要防"科学计数法"）。
    报告是给人看、要进 Word 的，必须是 2,180,348.56。
    """
    from decimal import Decimal, InvalidOperation

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    text = f"{number:,f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _value(facts: list[dict], key: str, default: str = "材料不足") -> str:
    candidates = [x for x in facts if x.get("fact_key") == key]
    if not candidates:
        return default
    item = max(candidates, key=lambda x: str(x.get("period", "")))
    if item.get("status") == "conflicted":
        return "存在来源冲突，待人工确认"
    if item.get("status") == "insufficient":
        return default
    if item.get("text_value"):
        # 存库值带内部签名前缀（text:/rating:/outlook:），成品必须剥掉。
        from .facts import strip_canonical_prefix
        return strip_canonical_prefix(item["text_value"])
    if item.get("value") is not None:
        return f"{_display_number(item['value'])}{item.get('unit', '')}"
    return default


def _sources(project_id: str, kinds: set[str] | None = None) -> list[str]:
    query = (
        "SELECT s.id,d.kind FROM analysis_sources s JOIN analysis_documents d "
        "ON d.id=s.document_id AND d.project_id=s.project_id WHERE s.project_id=?"
    )
    params: list[object] = [project_id]
    if kinds:
        query += " AND d.kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(sorted(kinds))
    return [x["id"] for x in rows(query, tuple(params))]


def _fact_sources(project_id: str, keys: set[str]) -> list[str]:
    placeholders = ",".join("?" for _ in keys)
    output: list[str] = []
    for item in rows(
        f"SELECT source_ids FROM fact_assertions WHERE project_id=? AND fact_key IN ({placeholders}) "
        "AND assertion_status NOT IN ('stale','conflicted','insufficient')",
        (project_id, *sorted(keys)),
    ):
        output.extend(json_load(item.get("source_ids", "[]")))
    return list(dict.fromkeys(output))


ANNUAL_PERIOD_RE = re.compile(r"(?:19|20)\d{2}")


def _preferred_scope(facts: list[dict]) -> str:
    """本章采用的报表口径：合并优先（与 chapter_guard.selected_scope 同一规则）。"""
    scopes = {
        str(item.get("scope") or "") for item in facts
        if str(item.get("scope") or "") in {"consolidated", "standalone"}
    }
    return "consolidated" if "consolidated" in scopes else "standalone" if "standalone" in scopes else ""


def _year_fact(items: list[dict], scope: str) -> dict | None:
    """同一年度内选定一条事实：优先采用口径 → 冲突原样保留 → 优先已确认取值。

    采用口径内存在冲突时**不得改取另一口径**，否则会用另一个口径的数字掩盖冲突。
    """
    in_scope = [item for item in items if str(item.get("scope") or "") == scope] if scope else []
    pool = in_scope or items
    conflicted = [item for item in pool if item.get("status") == "conflicted"]
    if conflicted:
        return conflicted[0]
    confirmed = [item for item in pool if item.get("status") == "confirmed" and item.get("value") is not None]
    if confirmed:
        return confirmed[0]
    valued = [item for item in pool if item.get("value") is not None]
    return valued[0] if valued else (pool[0] if pool else None)


def _render_fact(item: dict | None) -> str:
    if not item:
        return "材料不足"
    status = item.get("status")
    if status == "conflicted":
        return "存在来源冲突"
    if status == "insufficient":
        return "材料不足"
    if item.get("value") is not None:
        return f"{_display_number(item['value'])}{item.get('unit', '')}"
    return item.get("text_value") or "材料不足"


def _annual_series(facts: list[dict], key: str, scope: str = "") -> str:
    """最近三个完整年度的取值序列，**按年度去重**。

    此前按行取最后三条，同一年的合并/单体口径会各占一个位置，于是输出
    “2024年、2025年、2025年”。现在先按年度归并、再选口径。
    """
    scope = scope or _preferred_scope(facts)
    by_year: dict[str, list[dict]] = {}
    for item in facts:
        period = str(item.get("period") or "")
        if item.get("fact_key") != key or not ANNUAL_PERIOD_RE.fullmatch(period):
            continue
        by_year.setdefault(period, []).append(item)
    years = sorted(by_year)[-3:]
    if len(years) < 3:
        return "完整年度不足三期"
    return "、".join(f"{year}年{_render_fact(_year_fact(by_year[year], scope))}" for year in years)


def _tranche_summary(project_id: str) -> str:
    tranches = rows(
        "SELECT tranche_name,term_years,maturity_date,issue_amount,amount_scope,allocation_status "
        "FROM bond_tranches WHERE project_id=? AND extraction_method!='legacy_reference_seed' ORDER BY term_years,tranche_key",
        (project_id,),
    )
    if not tranches:
        return "材料不足"
    parts = []
    for item in tranches:
        text = f"{item['tranche_name']}"
        if item.get("maturity_date"):
            text += f"，兑付日期为{item['maturity_date']}"
        if item.get("issue_amount") is not None:
            text += f"，分品种金额为{_display_number(item['issue_amount'])}万元"
        elif item.get("allocation_status") == "not_disclosed":
            text += "，分品种金额未披露"
        parts.append(text)
    return "；".join(parts)


def _tranche_source_ids(project_id: str) -> list[str]:
    return [
        source_id
        for item in rows("SELECT source_ids FROM bond_tranches WHERE project_id=? AND extraction_method!='legacy_reference_seed' ORDER BY tranche_key", (project_id,))
        for source_id in json_load(item.get("source_ids", "[]"))
    ]


def generate_bond_chapters(project_id: str, configured: bool = False) -> list[BondChapter]:
    project = row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not project:
        raise ValueError(f"项目不存在：{project_id}")
    facts = _canonical_facts(project_id) or _facts(project_id)
    inventory = material_inventory(project_id)
    guarantee_applicable = bool(inventory.get("guarantee_applicable", True))
    tranche_text = _tranche_summary(project_id)
    checks = rows("SELECT rule_key,title,status,summary FROM cross_checks WHERE project_id=? ORDER BY rowid", (project_id,))
    risks = rows("SELECT severity,title,description,action FROM risks WHERE project_id=? ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END", (project_id,))
    gaps = "、".join(x["group"] for x in inventory["gaps"]) or "无"
    supplementary_gaps = "、".join(x["group"] for x in inventory["supplementary_gaps"]) or "无"
    status = "完整尽调" if inventory["complete_due_diligence_ready"] else "材料不足"
    severity_label = {"high": "高", "medium": "中", "low": "低"}
    risk_text = "\n".join(
        f"- 【{severity_label.get(x['severity'], '提示')}】{x['title']}：{x['description']}。后续核验：{x['action']}。"
        for x in risks
    ) or "现有材料尚未形成正式规则风险，仍应结合后续补充材料开展复核。"

    def check_text(keys: set[str]) -> str:
        selected = [item for item in checks if item["rule_key"] in keys]
        if not selected:
            return "材料不足，当前未能形成与本章直接相关的确定性核验结论。"
        result: list[str] = []
        for item in selected:
            status_label = {
                "passed": "已完成核验",
                "warning": "提示关注",
                "insufficient": "材料不足",
            }.get(item["status"], "待核验")
            result.append(f"{item['title']}：{status_label}。{item['summary']}")
        return " ".join(result)

    def section(*parts: tuple[str, str]) -> str:
        """Render deterministic content in the same Markdown shape used by the exporter."""
        return "\n\n".join(f"### {heading}\n\n{body}" for heading, body in parts)

    open_tasks = rows(
        "SELECT title,description FROM review_tasks WHERE project_id=? AND status='open' ORDER BY priority",
        (project_id,),
    )
    all_sources = _sources(project_id)
    bond_sources = _sources(project_id, {"募集说明书", "信用评级报告"})
    financial_sources = _sources(project_id, {"审计报告", "财务报表", "债务明细", "银行流水", "偿债安排"}) or _fact_sources(project_id, {
        "revenue", "net_profit", "operating_cash_flow", "accounts_receivable", "total_overdue_receivables",
        "interest_bearing_debt", "debt_due_12m", "interest_expense", "operating_cashflow_forecast",
    })
    guarantee_sources = _sources(project_id, {"担保合同", "反担保", "抵押物", "法律意见"}) or _fact_sources(project_id, {
        "guarantor_name", "guarantee_scope", "guarantee_period", "guarantee_cap",
        "counter_guarantee_value", "counter_guarantee_priority", "counter_guarantee_rights",
    })
    governance_sources = _sources(project_id, {"企业信息", "治理材料", "关联方", "发行决议"}) or _fact_sources(project_id, {"controller", "controller_share", "related_unsupported_amount"})
    operations_sources = _sources(project_id, {"合同订单", "访谈纪要"}) or _fact_sources(project_id, {"main_business", "top_customer_share", "top5_customer_share", "top_supplier_share", "top5_supplier_share"})
    has_profile_sources = bool(rows("SELECT 1 FROM analysis_sources WHERE project_id=? AND origin_type='profile_snapshot' LIMIT 1", (project_id,)))
    attribution = "以下事实基于募集说明书、信用评级报告、主体档案及现有补充材料形成；发行人披露和评级机构意见不等同于独立底稿核验，最终以证据索引所列原件为准。"
    sections = [
        ("scope", BOND_CHAPTER_TITLES["scope"], section(
            ("审查范围", f"本项目以{project['company_name']}及其拟发行债券“{_value(facts, 'bond_name')}”为审查对象，当前报告级别为{status}。{attribution}"),
            ("材料状态", f"阻断报告生成的核心材料缺口：{gaps}。补充核验材料缺口：{supplementary_gaps}。缺失材料不以零值或推断替代，相关事项仅保留为待核验边界。"),
            ("使用边界", "本报告用于呈示现有材料中的事实、核验结果和待处理事项，不构成承保、审批、评级或投资建议。"),
        ), all_sources),
        ("bond_overview", BOND_CHAPTER_TITLES["bond_overview"], section(
            ("发行条款", f"据募集说明书披露，本期债券拟发行规模为{_value(facts, 'bond_issue_amount')}，品种及期限为{tranche_text}，票面利率为{_value(facts, 'coupon_rate')}；{_value(facts, 'interest_frequency')}，{_value(facts, 'principal_repayment')}。"),
            ("增信结构", f"募集说明书所载拟议保证人为{_value(facts, 'guarantor_name')}，拟议保证范围为{_value(facts, 'guarantee_scope')}。" if guarantee_applicable else "据募集说明书披露，本期债券不设增信措施，保证及反担保事项不适用。"),
            ("审查边界", "前述发行条款及拟议保证安排均以正式签署文件和最终发行文件为准；相关合同未提供时，应标记为材料不足并在后续核验中补充。"),
        ), _fact_sources(project_id, {"bond_issue_amount", "bond_term_years", "coupon_rate", "interest_frequency", "principal_repayment"}) + _tranche_source_ids(project_id) + guarantee_sources),
        ("governance", BOND_CHAPTER_TITLES["governance"], section(
            ("主体及控制关系", f"据募集说明书披露，发行人实际控制人及持股信息为：{_value(facts, 'controller', '材料不足')}。主体身份、股权和控制关系仍应以工商档案及公司治理原件为准。"),
            ("治理核验边界", "授权矩阵、关联方决策程序和重大融资审批权限尚未取得完整原件，材料不足时无法对治理机制的实际执行情况作出判断。"),
        ), governance_sources),
        ("operations", BOND_CHAPTER_TITLES["operations"], section(
            ("经营与集中度事实", f"据募集说明书披露，第一大客户占比为{_value(facts, 'top_customer_share')}，前五大客户占比为{_value(facts, 'top5_customer_share')}；第一大供应商占比为{_value(facts, 'top_supplier_share')}，前五大供应商占比为{_value(facts, 'top5_supplier_share')}。"),
            ("审查边界", "合同、订单、实际回款和客户续签情况尚需结合原件、银行流水及函证资料核验；前述材料未提供的部分应持续标注为材料不足。"),
        ), operations_sources),
        ("proceeds", BOND_CHAPTER_TITLES["proceeds"], section(
            ("募集资金用途", f"据募集说明书披露，募集资金金额为{_value(facts, 'proceeds_amount')}，拟用于{_value(facts, 'proceeds_purpose')}。"),
            ("专户及核验边界", f"募集资金专户和偿债账户的现有信息为：{_value(facts, 'repayment_account')}。应以合同、发票、付款流水及用途变更授权逐笔核验用途闭环；相关材料未提供时，结论为材料不足。"),
        ), _sources(project_id, {"募集说明书", "募集资金用途", "银行流水"})),
        ("financial", BOND_CHAPTER_TITLES["financial"], section(
            ("财务概览", ("据本项目冻结主体档案，" if has_profile_sources else "据募集说明书披露，") + f"最近三个完整年度营业收入分别为{_annual_series(facts, 'revenue')}，净利润分别为{_annual_series(facts, 'net_profit')}，经营活动现金流分别为{_annual_series(facts, 'operating_cash_flow')}。"),
            ("财务结构与核验", f"现有材料中与财务结构、流动性和应收质量相关的确定性核验结果如下：{check_text({'financial_tie_out', 'leverage_liquidity', 'receivable_quality', 'revenue_cash_quality', 'interest_coverage'})}"),
            ("审查边界", "募集说明书、评级报告与主体档案的同期间数值应按来源并列核对，不应互相替代。未取得审计报告原件时，相关财务内容仅代表披露信息，仍待以审计及财务原件交叉核验。"),
        ), financial_sources),
        ("cashflow", BOND_CHAPTER_TITLES["cashflow"], section(
            ("第一偿债来源", f"据募集说明书披露，本期债券第一偿债来源为{_value(facts, 'repayment_source', '销售回款')}。经营活动现金流仅作为现有材料中的相关事实，不替代募集说明书明确的偿债安排。"),
            ("历史现金流与预测", f"最近三个完整年度经营活动现金流为{_annual_series(facts, 'operating_cash_flow')}。现有偿债现金流预测值为{_value(facts, 'operating_cashflow_forecast')}，其适用范围和实现情况仍需依据原始资料核验。"),
            ("兑付安排与审查边界", f"与现金流及偿债安排相关的确定性核验结果如下：{check_text({'cashflow_debt_service', 'bullet_maturity', 'revenue_cash_quality'})}。预测值须与银行流水、订单回款和偿债专户余额逐期勾稽；上述原件未提供的部分为材料不足，待核验。"),
        ), financial_sources + bond_sources),
        ("debt", BOND_CHAPTER_TITLES["debt"], section(
            ("债务概览", f"现有材料所载有息债务为{_value(facts, 'interest_bearing_debt')}，未来12个月到期债务为{_value(facts, 'debt_due_12m')}，利息支出为{_value(facts, 'interest_expense')}。上述数值应与债务明细、财务报表及偿债安排保持同一口径。"),
            ("本期债券兑付影响", f"据募集说明书披露，本期债券{_value(facts, 'principal_repayment')}。到期本金安排应与发行人既有债务到期结构分别呈示，不以本期发行规模替代存量债务。"),
            ("审查边界", f"与债务到期集中度和流动性相关的确定性核验结果如下：{check_text({'debt_maturity', 'bullet_maturity', 'leverage_liquidity'})}。债务台账、授信合同及偿债资金计划未齐备的部分为材料不足，待核验。"),
        ), financial_sources),
        ("rating", BOND_CHAPTER_TITLES["rating"], section(
            ("评级结论", f"评级机构认为，发行人主体评级为{_value(facts, 'issuer_rating')}，本期债项评级为{_value(facts, 'bond_rating')}，评级展望为{_value(facts, 'rating_outlook')}。"),
            ("评级关注因素", f"评级报告所列关注因素为：{_value(facts, 'rating_downgrade_trigger')}。相关表述仅反映评级报告所载观点及其评级口径。"),
            ("使用边界", "评级机构意见不等同于担保机构独立判断。评级报告未披露或与其他来源口径不同的事项，应保留材料不足或口径差异说明，不以评级观点替代其他事实。"),
        ), _fact_sources(project_id, {"issuer_rating", "bond_rating", "rating_outlook", "rating_downgrade_trigger", "rating_revenue", "rating_total_debt"})),
        ("investor_protection", BOND_CHAPTER_TITLES["investor_protection"], section(
            ("已披露条款", f"据募集说明书披露，投资者保护措施为：{_value(facts, 'investor_protection')}；交叉违约条款为：{_value(facts, 'cross_default_clause')}。"),
            ("审查边界", "通知期限、持有人会议表决门槛及增信措施变更后的救济安排，应以受托管理协议和正式条款原文为准；未提供的内容标记为材料未披露或材料不足。"),
        ), _sources(project_id, {"募集说明书", "受托管理"})),
        ("guarantee", BOND_CHAPTER_TITLES["guarantee"], section(
            ("拟议保证结构", f"据募集说明书披露，拟议保证人为{_value(facts, 'guarantor_name')}，拟议保证范围为{_value(facts, 'guarantee_scope')}。"),
            ("范围与期间", f"募集说明书披露的拟议保证期间为{_value(facts, 'guarantee_period')}，拟议责任上限为{_value(facts, 'guarantee_cap')}。前述安排仅作为披露事实呈示。"),
            ("合同状态与核验边界", "未取得签署版保证合同和授权文件，保证责任未生效，不得表述为已经生效。相关合同和授权材料不足时，需在后续核验中补充。"),
        ) if guarantee_applicable else section(("不适用", "据募集说明书披露，本期债券不设增信措施，本章不适用。")), guarantee_sources or bond_sources),
        ("counter_guarantee", BOND_CHAPTER_TITLES["counter_guarantee"], section(
            ("反担保事实", f"据募集说明书披露，拟议反担保折后价值为{_value(facts, 'counter_guarantee_value')}。"),
            ("权利状态与覆盖口径", f"现有披露中的顺位及权利状态为：{_value(facts, 'counter_guarantee_priority')}；{_value(facts, 'counter_guarantee_rights')}。该等信息不替代对合同、权证和登记原件的核验。"),
            ("可执行性边界", "未取得反担保合同、权证、估值和登记文件，材料不足，无法判断可执行性或变现能力，不得表述为已具备可执行覆盖。"),
        ) if guarantee_applicable else section(("不适用", "本期债券不设增信措施，反担保事项不适用。")), guarantee_sources or bond_sources),
        ("legal", BOND_CHAPTER_TITLES["legal"], section(
            ("现有披露", f"关联方异常往来为{_value(facts, 'related_unsupported_amount')}；法律意见状态为{_value(facts, 'legal_opinion_status')}。"),
            ("审查边界", "关联方资金往来应结合合同、资金流水和决策依据核验。诉讼、行政处罚和或有负债如无独立证明，均应标记为材料不足，待核验。"),
        ), _sources(project_id, {"关联方", "法律意见", "访谈纪要"})),
        ("risk_matrix", BOND_CHAPTER_TITLES["risk_matrix"], section(
            ("风险呈示说明", "本节仅汇总已进入正式风险清单的规则风险及其对应核验动作；未经确认的候选风险不进入正式风险矩阵。"),
            ("已识别风险", risk_text),
        ), _sources(project_id)),
        ("tasks", BOND_CHAPTER_TITLES["tasks"], section(
            ("行动包说明", "以下事项按材料缺口、事实冲突、规则核验和已确认风险归集，同一根因应由同一行动包跟踪并留存补充证据。"),
            ("开放事项", "\n".join(f"- {item['title']}：{item['description']}" for item in open_tasks) or "暂无开放任务。"),
        ), _sources(project_id)),
        ("monitoring", BOND_CHAPTER_TITLES["monitoring"], section(
            ("跟踪范围", "持续跟踪范围包括募集资金专户、偿债账户、债务到期、评级展望、核心客户、关联方资金往来、保证责任变化及反担保登记状态。"),
            ("触发后的处理边界", "发生重大事项时，应按受托管理协议、正式保证合同及项目留痕要求更新材料、复核受影响事实，并重新生成受影响章节。"),
        ), bond_sources + guarantee_sources),
        ("evidence", BOND_CHAPTER_TITLES["evidence"], section(
            ("证据使用说明", f"本报告基于{len(inventory['documents'])}份材料生成，当前报告级别为{status}。"),
            ("材料缺口", f"核心材料缺口为：{gaps}；补充核验材料缺口为：{supplementary_gaps}。所有数字、事实和风险均应通过证据索引定位至PDF页码、Word段落或Excel区域；缺失材料不得以零值或模型推断代替。"),
        ), all_sources),
    ]
    return [BondChapter(key, title, content, list(dict.fromkeys(sources))) for key, title, content, sources in sections]
