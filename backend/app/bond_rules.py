from __future__ import annotations

from .facts import canonical_key
from .rules import RuleResult
from .policy import PolicyPack


def _prepare(facts: list[dict]) -> list[dict]:
    out = []
    for item in facts:
        row = dict(item)
        row["fact_key"] = row.get("fact_key") or canonical_key(row.get("name", ""))
        sources = row.get("source_ids", [])
        if isinstance(sources, str):
            import json
            try:
                sources = json.loads(sources)
            except json.JSONDecodeError:
                sources = [sources]
        row["source_ids"] = sources or []
        out.append(row)
    return out


def _fact(facts: list[dict], key: str) -> dict | None:
    candidates = [f for f in facts if f.get("fact_key") == key and (f.get("value") is not None or f.get("text_value"))]
    return max(candidates, key=lambda f: str(f.get("period", "")), default=None)


def _sources(items: list[dict]) -> tuple[list[str], list[str]]:
    source_ids: list[str] = []
    fact_ids: list[str] = []
    for item in items:
        fact_ids.extend([item["id"]] if item.get("id") else [])
        source_ids.extend(item.get("source_ids", []))
    return list(dict.fromkeys(source_ids)), list(dict.fromkeys(fact_ids))


def _result(policy: PolicyPack, key: str, status: str, summary: str, values: list[dict], facts: list[dict], formula: str, result: float | None = None) -> RuleResult:
    rule = policy.rule(key)
    source_ids, fact_ids = _sources(facts)
    return RuleResult(key, rule.title, rule.severity, status, summary, values, source_ids, formula, fact_ids, rule.thresholds, result, summary, policy.version)


def _insufficient(policy: PolicyPack, key: str, required: str, formula: str) -> RuleResult:
    return _result(policy, key, "insufficient", f"材料不足：缺少{required}，本项未作风险判断。", [], [], formula)


def evaluate_bond_rules(
    facts: list[dict], issue_amount: float, *, policy: PolicyPack,
    material_gaps: list[dict] | None = None, conflicts: list[dict] | None = None,
    tranches: list[dict] | None = None,
) -> list[RuleResult]:
    facts = _prepare(facts)
    conflicts = conflicts or []
    def conflict_for(key: str) -> dict | None:
        return next((item for item in conflicts if item.get("fact_key") == key), None)

    checks: list[RuleResult] = []
    core_keys = ("bond_name", "bond_issue_amount", "issuer_rating", "bond_rating")
    core = [_fact(facts, key) for key in core_keys]
    valid_tranches = [
        item for item in (tranches or [])
        if isinstance(item.get("term_years"), int) and item["term_years"] > 0
    ]
    has_term = bool(valid_tranches) or bool(_fact(facts, "bond_term_years")) or bool(conflict_for("bond_term_years"))
    missing_core = sum(1 for key, item in zip(core_keys, core) if not item and not conflict_for(key)) + int(not has_term)
    gaps = material_gaps or []
    checks.append(_result(policy, "material_completeness", "warning" if gaps or missing_core else "passed", f"缺少{len(gaps)}类债券担保材料，核心交易字段缺少{missing_core}项。", [{"label": "材料缺口", "value": len(gaps)}, {"label": "核心字段缺口", "value": missing_core}], [], "材料缺口数；核心字段缺口数", float(len(gaps) + missing_core)))

    amount = _fact(facts, "bond_issue_amount")
    rating_amount = _fact(facts, "rating_issue_amount") or _fact(facts, "registered_issue_amount")
    if not amount or not rating_amount:
        conflict = conflict_for("bond_issue_amount")
        if conflict:
            assertions = conflict.get("assertions", [])
            checks.append(_result(policy, "bond_term_consistency", "warning", "募集说明书与评级报告发行规模存在未解决冲突，系统不自动选择口径。", [], assertions, "冲突来源并列展示，人工确认后重算"))
        else:
            checks.append(_insufficient(policy, "bond_term_consistency", "募集说明书和评级报告发行规模", "募集说明书规模-评级报告规模"))
    else:
        diff = abs(amount["value"] - rating_amount["value"])
        checks.append(_result(policy, "bond_term_consistency", "warning" if diff else "passed", f"募集说明书与评级报告发行规模差异{diff:g}万元。", [{"label": "募集说明书发行规模", "value": amount["value"]}, {"label": "评级口径发行规模", "value": rating_amount["value"]}], [amount, rating_amount], "募集说明书发行规模-评级口径发行规模", diff))

    issuer, bond = _fact(facts, "issuer_rating"), _fact(facts, "bond_rating")
    if not issuer or not bond:
        checks.append(_insufficient(policy, "rating_consistency", "主体评级和债项评级", "评级差异及增信逻辑"))
    else:
        upgrade = 0 if issuer["text_value"] == bond["text_value"] else 1
        checks.append(_result(policy, "rating_consistency", "passed" if upgrade else "warning", f"主体评级{issuer['text_value']}，债项评级{bond['text_value']}，评级差异{upgrade}级。", [ {"label": "主体评级", "value": 1}, {"label": "债项评级", "value": 1} ], [issuer, bond], "评级差异级数", float(upgrade)))

    proceeds = _fact(facts, "proceeds_amount")
    purpose = _fact(facts, "proceeds_purpose")
    proceeds_account = _fact(facts, "repayment_account")
    if not proceeds or not purpose or not proceeds_account:
        checks.append(_insufficient(policy, "proceeds_coverage", "募集资金用途、金额及专户安排", "用途金额/发行规模；专户闭环"))
    else:
        coverage = proceeds["value"] / (amount["value"] if amount else issue_amount) * 100
        checks.append(_result(policy, "proceeds_coverage", "warning" if coverage < 100 else "passed", f"已披露募集资金用途覆盖{coverage:.1f}%，并提供专户安排。", [{"label": "用途覆盖率", "value": round(coverage, 2)}], [proceeds, purpose, proceeds_account], "募集资金用途金额/发行规模；专户闭环", coverage))

    revenue = _fact(facts, "revenue")
    cash = _fact(facts, "operating_cash_flow")
    operating_inflow = _fact(facts, "operating_inflow")
    debt = _fact(facts, "interest_bearing_debt")
    interest = _fact(facts, "interest_expense")
    if revenue and operating_inflow:
        ratio = operating_inflow["value"] / revenue["value"] * 100 if revenue["value"] else 0
        rule = policy.rule("revenue_cash_quality")
        checks.append(_result(policy, "revenue_cash_quality", "warning" if not rule.thresholds["min_pct"] <= ratio <= rule.thresholds["max_pct"] else "passed", f"银行经营性流入/收入为{ratio:.1f}%。", [{"label": "经营性流入/收入", "value": ratio}], [revenue, operating_inflow], "银行经营性流入/营业收入", ratio))
    elif conflict_for("revenue"):
        checks.append(_result(policy, "revenue_cash_quality", "warning", "营业收入存在未解决冲突，无法确认收入与经营性流入口径。", [], conflict_for("revenue").get("assertions", []), "冲突来源并列展示，人工确认后重算"))
    else:
        checks.append(_insufficient(policy, "revenue_cash_quality", "营业收入和银行经营性流入", "银行经营性流入/营业收入"))

    if cash and interest and interest["value"]:
        coverage = cash["value"] / interest["value"]
        checks.append(_result(policy, "interest_coverage", "warning" if coverage < policy.rule("interest_coverage").thresholds["minimum"] else "passed", f"经营现金流/利息支出为{coverage:.2f}倍。", [{"label": "利息覆盖倍数", "value": coverage}], [x for x in (cash, interest) if x], "经营现金流/利息支出", coverage))
    else:
        checks.append(_insufficient(policy, "interest_coverage", "经营现金流和利息支出", "经营现金流/利息支出"))

    due = _fact(facts, "debt_due_12m")
    if due and debt and debt["value"]:
        ratio = due["value"] / debt["value"] * 100
        checks.append(_result(policy, "debt_maturity", "warning" if ratio >= policy.rule("debt_maturity").thresholds["due_12m_pct"] else "passed", f"一年内到期债务占有息负债{ratio:.1f}%。", [{"label": "到期集中度", "value": ratio}], [due, debt], "一年内到期债务/有息负债", ratio))
    else:
        checks.append(_insufficient(policy, "debt_maturity", "一年内到期债务和有息负债", "一年内到期债务/有息负债"))

    assets, liabilities, current_assets, current_liabilities = (_fact(facts, k) for k in ("total_assets", "total_liabilities", "current_assets", "current_liabilities"))
    if assets and liabilities and current_assets and current_liabilities and assets["value"] and current_liabilities["value"]:
        leverage = liabilities["value"] / assets["value"] * 100
        current = current_assets["value"] / current_liabilities["value"]
        rule = policy.rule("leverage_liquidity")
        status = "warning" if leverage >= rule.thresholds["debt_ratio_pct"] or current < rule.thresholds["current_ratio"] else "passed"
        checks.append(_result(policy, "leverage_liquidity", status, f"资产负债率{leverage:.1f}%，流动比率{current:.2f}。", [{"label": "资产负债率", "value": leverage}, {"label": "流动比率", "value": current}], [assets, liabilities, current_assets, current_liabilities], "负债/资产；流动资产/流动负债", leverage))
    else:
        checks.append(_insufficient(policy, "leverage_liquidity", "资产负债表及流动性科目", "负债/资产；流动资产/流动负债"))

    receivable, overdue = _fact(facts, "accounts_receivable"), _fact(facts, "total_overdue_receivables")
    if not receivable or not overdue or not receivable["value"]:
        checks.append(_insufficient(policy, "receivable_quality", "应收账款和逾期应收", "逾期应收/应收账款"))
    else:
        ratio = overdue["value"] / receivable["value"] * 100
        threshold = policy.rule("receivable_quality").thresholds["overdue_pct"]
        checks.append(_result(policy, "receivable_quality", "warning" if ratio >= threshold else "passed", f"逾期应收占应收账款{ratio:.1f}%。", [{"label": "逾期应收占比", "value": ratio}], [receivable, overdue], "逾期应收/应收账款", ratio))

    def share_rule(rule_key: str, first_key: str, five_key: str, label: str) -> None:
        first, five = _fact(facts, first_key), _fact(facts, five_key)
        if not first and not five:
            checks.append(_insufficient(policy, rule_key, f"第一大或前五大{label}占比", f"{label}集中度")); return
        values = [item for item in (first, five) if item]
        rule = policy.rule(rule_key); status = "warning" if (first and first["value"] >= rule.thresholds["top1_pct"]) or (five and five["value"] >= rule.thresholds["top5_pct"]) else "passed"
        checks.append(_result(policy, rule_key, status, "；".join(f"{x['name']}{x['value']:.1f}%" for x in values), [{"label": x["name"], "value": x["value"]} for x in values], values, f"第一大{label}≥30%或前五大≥60%", max(x["value"] for x in values)))
    share_rule("customer_concentration", "top_customer_share", "top5_customer_share", "客户")
    share_rule("supplier_concentration", "top_supplier_share", "top5_supplier_share", "供应商")

    other, equity, related = _fact(facts, "other_receivables"), _fact(facts, "equity"), _fact(facts, "related_unsupported_amount")
    if not related and not (other and equity and equity["value"]):
        checks.append(_insufficient(policy, "related_party", "关联方往来或其他应收款及净资产", "关联方金额；其他应收/净资产"))
    else:
        ratio = other["value"] / equity["value"] * 100 if other and equity and equity["value"] else 0
        rule = policy.rule("related_party"); status = "warning" if (related and related.get("value", 0) > 0) or ratio >= rule.thresholds["other_receivables_equity_pct"] else "passed"
        checks.append(_result(policy, "related_party", status, f"关联方异常金额{(related or {}).get('value', 0):g}万元，其他应收/净资产{ratio:.1f}%。", [{"label": "关联方异常金额", "value": (related or {}).get("value", 0)}, {"label": "其他应收/净资产", "value": ratio}], [x for x in (related, other, equity) if x], "关联异常金额>0或其他应收/净资产≥20%", ratio))

    guarantee, cap = _fact(facts, "guarantee_scope"), _fact(facts, "guarantee_cap")
    if not guarantee:
        checks.append(_insufficient(policy, "guarantee_scope", "担保合同和保证责任范围", "保证责任覆盖债券本息及费用"))
    else:
        scope_text = guarantee.get("text_value", "")
        no_enhancement = any(term in scope_text for term in ("无担保", "无增信", "不提供保证"))
        required_scope = ("本金", "利息", "违约金", "实现债权费用")
        missing_scope = [term for term in required_scope if term not in scope_text]
        if no_enhancement:
            summary = "债券文件明确无增信安排，不适用于债券发行融资担保项目。"
            checks.append(_result(policy, "guarantee_scope", "warning", summary, [], [guarantee], "保证责任范围完整性；保证责任上限/发行规模"))
        elif missing_scope:
            summary = f"保证责任范围为{scope_text}，缺少覆盖：{'、'.join(missing_scope)}。"
            checks.append(_result(policy, "guarantee_scope", "warning", summary, [], [x for x in (guarantee, cap) if x], "保证责任范围完整性；保证责任上限/发行规模"))
        elif not cap or cap.get("value") is None:
            checks.append(_insufficient(policy, "guarantee_scope", "保证责任上限", "保证责任范围完整性；保证责任上限/发行规模"))
        else:
            coverage = cap["value"] / issue_amount * 100 if issue_amount else 0
            summary = f"保证责任范围为{scope_text}，责任上限覆盖发行规模{coverage:.1f}%。"
            checks.append(_result(policy, "guarantee_scope", "warning" if coverage < 100 else "passed", summary, [{"label": "责任上限覆盖率", "value": coverage}], [guarantee, cap], "保证责任范围完整性；保证责任上限/发行规模", coverage))

    counter = _fact(facts, "counter_guarantee_value")
    if not counter:
        checks.append(_insufficient(policy, "counter_guarantee", "反担保估值和权利状态", "反担保折后价值/担保敞口"))
    else:
        issue_coverage = counter["value"] / issue_amount * 100 if issue_amount else 0
        guarantee_cap = _fact(facts, "guarantee_cap")
        liability_coverage = counter["value"] / guarantee_cap["value"] * 100 if guarantee_cap and guarantee_cap.get("value") else None
        rights = _fact(facts, "counter_guarantee_rights")
        if not rights:
            checks.append(_insufficient(policy, "counter_guarantee", "反担保权属、顺位和登记状态", "反担保折后价值/担保敞口；权利状态"))
        else:
            rights_text = rights.get("text_value", "")
            if any(marker in rights_text for marker in ("未纳入本核心材料包", "正式文件未提供", "正式文件未纳入")):
                checks.append(_result(
                    policy, "counter_guarantee", "insufficient",
                    "材料不足：募集说明书仅披露拟议反担保，未提供正式权属、估值、登记及优先顺位文件，本项不评价反担保可执行性。",
                    [], [counter, rights, *([guarantee_cap] if guarantee_cap else [])],
                    "反担保折后价值/发行规模；反担保折后价值/保证责任上限；正式权利文件", None,
                ))
            else:
                bad_rights = any(x in rights_text for x in ("瑕疵", "未登记", "在先", "查封", "第二顺位", "待确认", "尚未", "拟于", "超过12个月"))
                values = [{"label": "反担保/本期发行规模", "value": issue_coverage}]
                if liability_coverage is not None:
                    values.append({"label": "反担保/保证责任上限", "value": liability_coverage})
                summary = f"反担保折后价值占本期发行规模{issue_coverage:.1f}%"
                if liability_coverage is not None:
                    summary += f"，占保证责任上限{liability_coverage:.1f}%"
                summary += f"。权利状态：{rights_text}。"
                checks.append(_result(policy, "counter_guarantee", "warning" if issue_coverage < 100 or bad_rights else "passed", summary, values, [counter, rights, *([guarantee_cap] if guarantee_cap else [])], "反担保折后价值/发行规模；反担保折后价值/保证责任上限；权利状态", issue_coverage))

    for key, title, required in (("investor_protection", "投资者保护与违约条款", "投资者保护措施"), ("legal_issuance", "法律意见与发行决议", "法律意见和发行决议")):
        fact = _fact(facts, "investor_protection" if key == "investor_protection" else "legal_opinion_status")
        checks.append(_result(policy, key, "passed" if fact else "insufficient", f"已取得{required}。" if fact else f"材料不足：缺少{required}，本项未作风险判断。", [{"label": "材料状态", "value": 1 if fact else 0}], [fact] if fact else [], required))

    rating_revenue, rating_debt = _fact(facts, "rating_revenue"), _fact(facts, "rating_total_debt")
    audit_revenue, audit_debt = _fact(facts, "revenue"), _fact(facts, "interest_bearing_debt")
    tie_inputs = [x for x in (rating_revenue, audit_revenue, rating_debt, audit_debt) if x]
    if len(tie_inputs) < 4:
        conflict_inputs = [item for key in ("revenue", "rating_revenue", "interest_bearing_debt", "rating_total_debt") for item in ([conflict_for(key)] if conflict_for(key) else [])]
        if conflict_inputs:
            assertions = [assertion for item in conflict_inputs for assertion in item.get("assertions", [])]
            checks.append(_result(policy, "financial_tie_out", "warning", "评级或审计财务口径存在未解决冲突，未进行单值勾稽。", [], assertions, "冲突来源并列展示，人工确认后重算"))
        else:
            checks.append(_insufficient(policy, "financial_tie_out", "评级报告与审计报表可比财务口径", "评级口径-审计口径"))
    else:
        diffs = [abs(rating_revenue["value"] - audit_revenue["value"]), abs(rating_debt["value"] - audit_debt["value"])]
        status = "warning" if any(diff > 0.05 * max(abs(audit_revenue["value"]), abs(audit_debt["value"]), 1) for diff in diffs) else "passed"
        checks.append(_result(policy, "financial_tie_out", status, f"评级与审计收入差异{diffs[0]:g}万元、有息负债差异{diffs[1]:g}万元。", [{"label": "收入差异", "value": diffs[0]}, {"label": "有息负债差异", "value": diffs[1]}], tie_inputs, "评级口径有息负债-审计口径有息负债", max(diffs)))
    for key in ("cashflow_debt_service", "bullet_maturity"):
        if key == "cashflow_debt_service":
            if cash and due and due["value"]:
                dscr = cash["value"] / due["value"]
                checks.append(_result(policy, key, "warning" if dscr < policy.rule(key).thresholds["minimum_dscr"] else "passed", f"经营现金流对一年内到期债务覆盖{dscr:.2f}倍。", [{"label": "DSCR", "value": dscr}], [cash, due], "经营现金流/一年内到期债务", dscr))
            else: checks.append(_insufficient(policy, key, "经营现金流和到期债务", "经营现金流/一年内到期债务"))
        else:
            principal = _fact(facts, "bond_issue_amount")
            cashflow = _fact(facts, "operating_cashflow_forecast") or cash
            if principal and cashflow and cashflow["value"]:
                coverage = cashflow["value"] / principal["value"] * 100
                checks.append(_result(policy, key, "warning" if coverage < policy.rule(key).thresholds["minimum_coverage_pct"] else "passed", f"到期本金对可用现金流覆盖{coverage:.1f}%。", [{"label": "到期本金现金流覆盖率", "value": coverage}], [principal, cashflow], "偿债现金流/到期本金", coverage))
            else: checks.append(_insufficient(policy, key, "债券本金和偿债现金流预测", "偿债现金流/到期本金"))
    return checks
