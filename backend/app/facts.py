from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .database import connect, execute, json_dump, json_load, rows


@dataclass(frozen=True)
class FactDefinition:
    label: str
    category: str
    aliases: tuple[str, ...]
    unit_kind: str = "amount"


FACT_REGISTRY: dict[str, FactDefinition] = {
    "registered_capital": FactDefinition("注册资本", "主体治理", ("注册资本",)),
    "legal_representative": FactDefinition("法定代表人", "主体治理", ("法定代表人为", "法定代表人"), "text"),
    "registered_address": FactDefinition("注册地址", "主体治理", ("注册地址", "住所", "公司地址"), "text"),
    "establishment_date": FactDefinition("成立日期", "主体治理", ("成立日期", "成立于"), "text"),
    "audit_firm": FactDefinition("审计机构", "财务", ("审计机构", "会计师事务所"), "text"),
    "audit_opinion": FactDefinition("审计意见", "财务", ("审计意见", "审计意见类型"), "text"),
    "financial_statement_scope": FactDefinition("财务报表口径", "财务", ("合并财务报表", "母公司财务报表", "报表口径"), "text"),
    "revenue": FactDefinition("营业收入", "财务", ("财务报表营业收入", "主营业务收入", "营业收入", "收入")),
    "cost_of_goods_sold": FactDefinition("营业成本", "财务", ("主营业务成本", "营业成本", "成本")),
    "gross_profit": FactDefinition("毛利润", "财务", ("毛利润", "毛利额")),
    "tax_revenue": FactDefinition("纳税申报收入", "税务", ("增值税申报收入", "纳税申报收入", "纳税收入", "开票收入")),
    "operating_inflow": FactDefinition("经营性流入", "流水", ("银行经营性流入", "银行经营流入", "经营性流入", "经营流入", "销售回款")),
    "operating_outflow": FactDefinition("经营性流出", "流水", ("银行经营性流出", "经营性流出", "采购付款")),
    "net_profit": FactDefinition("净利润", "财务", ("净利润",)),
    "operating_cash_flow": FactDefinition("经营现金流净额", "财务", ("经营活动现金流量净额", "经营活动现金流净额", "经营现金流净额", "经营现金流")),
    "accounts_receivable": FactDefinition("应收账款", "财务", ("应收账款余额", "应收账款", "应收")),
    "inventory": FactDefinition("存货", "财务", ("存货余额", "存货")),
    "prepayments": FactDefinition("预付款项", "财务", ("预付款项", "预付账款")),
    "other_receivables": FactDefinition("其他应收款", "财务", ("其他应收款余额", "其他应收款")),
    "cash": FactDefinition("货币资金", "财务", ("货币资金", "现金及现金等价物")),
    "restricted_cash": FactDefinition("受限资金", "财务", ("受限货币资金", "受限资金", "受限制的货币资金")),
    "fixed_assets": FactDefinition("固定资产", "财务", ("固定资产净额", "固定资产")),
    "total_assets": FactDefinition("资产总额", "财务", ("资产总计", "资产总额", "资产")),
    "total_liabilities": FactDefinition("负债总额", "财务", ("负债合计", "负债总额", "负债")),
    "equity": FactDefinition("所有者权益", "财务", ("所有者权益合计", "净资产", "所有者权益")),
    "current_assets": FactDefinition("流动资产", "财务", ("流动资产合计", "流动资产")),
    "current_liabilities": FactDefinition("流动负债", "财务", ("流动负债合计", "流动负债")),
    "interest_bearing_debt": FactDefinition("有息负债", "债务", ("有息负债余额", "现有有息负债", "有息负债")),
    "short_term_debt": FactDefinition("短期借款", "债务", ("短期借款余额", "短期借款")),
    "long_term_debt": FactDefinition("长期借款", "债务", ("长期借款余额", "长期借款")),
    "bonds_payable": FactDefinition("应付债券", "债务", ("应付债券余额", "应付债券")),
    "finance_expense": FactDefinition("财务费用", "财务", ("财务费用",)),
    "interest_expense": FactDefinition("利息支出", "财务", ("利息费用", "利息支出")),
    "debt_due_12m": FactDefinition("未来12个月到期债务", "债务", ("一年内到期债务", "未来12个月到期债务", "未来12个月需偿还或续作", "一年内集中到期合计", "到期债务")),
    "credit_limit": FactDefinition("授信总额", "债务", ("授信总额", "综合授信额度")),
    "utilized_credit": FactDefinition("已用授信", "债务", ("已用授信", "授信使用余额")),
    "overdue_debt": FactDefinition("逾期债务", "债务", ("逾期债务", "贷款逾期")),
    "top_customer_share": FactDefinition("第一大客户占比", "经营", ("第一大客户占比", "第一大客户", "最大客户占比"), "percent"),
    "top5_customer_share": FactDefinition("前五大客户占比", "经营", ("前五大客户占比", "前5大客户占比", "前五大客户", "前5大客户"), "percent"),
    "top_supplier_share": FactDefinition("第一大供应商占比", "经营", ("第一大供应商占比", "最大供应商占比"), "percent"),
    "top5_supplier_share": FactDefinition("前五大供应商占比", "经营", ("前五大供应商占比", "前5大供应商占比", "前五大供应商", "前5大供应商"), "percent"),
    "order_backlog": FactDefinition("在手订单", "经营", ("在手订单金额", "在手服务订单", "在手订单", "在手合同")),
    "annual_purchases": FactDefinition("年度采购额", "经营", ("年度采购额", "采购金额合计")),
    "related_unsupported_amount": FactDefinition("无合同关联方往来", "关联方", ("无合同关联方往来", "未提供对应合同", "无商业背景的关联方往来")),
    "supporting_contract_amount": FactDefinition("已提供用途合同", "用途", ("用途合同合计", "合同金额合计", "已提供合同金额", "用途合同")),
    "own_funds": FactDefinition("自有资金投入", "融资申请", ("自有资金投入", "企业自筹资金")),
    "funding_gap": FactDefinition("资金缺口", "融资申请", ("资金缺口", "项目资金缺口")),
    "repayment_source": FactDefinition("第一还款来源", "融资申请", ("第一还款来源", "还款来源"), "text"),
    "guarantee_method": FactDefinition("担保方式", "担保方案", ("担保方式",), "text"),
    "guarantee_fee_rate": FactDefinition("担保费率", "担保方案", ("担保费率", "年担保费率"), "percent"),
    "deposit_ratio": FactDefinition("保证金比例", "担保方案", ("保证金比例",), "percent"),
    "collateral_appraised_value": FactDefinition("抵押物评估价值", "抵押物", ("抵押物评估价值", "评估价值")),
    "collateral_discounted_value": FactDefinition("折后抵押价值", "抵押物", ("折后抵押价值", "折扣后价值", "折后覆盖额", "按70%折扣计算覆盖额")),
    "collateral_valuation_age_months": FactDefinition("抵押物估值账龄", "抵押物", ("评估报告已出具", "评估报告出具于", "评估报告距今", "估值报告距今", "估值距今"), "months"),
    "collateral_haircut": FactDefinition("抵押折扣率", "抵押物", ("抵押折扣率", "抵押率", "折扣率"), "percent"),
    "prior_lien_amount": FactDefinition("在先抵押金额", "抵押物", ("在先抵押金额", "第一顺位抵押余额")),
    "collateral_rights_issue": FactDefinition("抵押权利瑕疵", "抵押物", ("权属瑕疵", "抵押权利瑕疵", "处置受限"), "text"),
    "external_guarantee": FactDefinition("对外担保", "担保", ("对外担保余额", "对外担保")),
    "contingent_liabilities": FactDefinition("其他或有负债", "担保", ("或有负债", "未决诉讼金额", "其他或有负债")),
    "overdue_receivables": FactDefinition("一年以上应收账款", "经营", ("一年以上应收账款", "一年以上应收", "长期逾期应收")),
    "total_overdue_receivables": FactDefinition("逾期应收账款", "经营", ("逾期应收账款合计", "逾期应收余额", "逾期应收账款")),
    "controller": FactDefinition("实际控制人", "主体", ("实际控制人为", "实际控制人"), "text"),
    "controller_share": FactDefinition("实控人持股比例", "主体治理", ("实控人持股比例", "实际控制人持股", "持股比例", "持股"), "percent"),
    "related_parties": FactDefinition("主要关联方", "关联方", ("主要关联方", "关联企业包括"), "text"),
    "main_business": FactDefinition("主营业务", "经营", ("主营业务", "主营"), "text"),
    "employee_count": FactDefinition("员工人数", "经营", ("员工人数", "在册员工"), "count"),
    "capacity_utilization": FactDefinition("产能利用率", "经营", ("产能利用率",), "percent"),
    "tax_arrears": FactDefinition("欠税金额", "税务", ("欠税金额", "税款欠缴情形")),
    "social_security_arrears": FactDefinition("社保欠缴情形", "经营", ("社保欠缴", "社会保险欠缴"), "text"),
    "cash_received_from_sales": FactDefinition("销售商品收到的现金", "财务", ("销售商品、提供劳务收到的现金", "销售商品收到的现金")),
    "investing_cash_flow": FactDefinition("投资活动现金流量净额", "财务", ("投资活动产生的现金流量净额", "投资活动现金流量净额")),
    "financing_cash_flow": FactDefinition("筹资活动现金流量净额", "财务", ("筹资活动产生的现金流量净额", "筹资活动现金流量净额")),
    # Bond guarantee facts. These remain source assertions; no value is inferred.
    # 「本期债券」单独作别名太泛：它是通用名词，会命中「本期债券发行要素」这类
    # 表格标题行，并把后续词当成债券名称（实测把「发行要素」抽成了债券名称，
    # 与真实债券名称形成假冲突）。只保留带「名称／简称」的完整字段名。
    "bond_name": FactDefinition(
        "债券名称", "债券发行", ("债券名称", "本期债券名称", "本期债券简称", "债项名称"), "text"
    ),
    "registered_issue_amount": FactDefinition("债券注册规模", "债券发行", ("注册金额", "注册规模", "注册总额")),
    "bond_issue_amount": FactDefinition("本期发行规模", "债券发行", ("发行金额", "发行规模", "本期发行规模")),
    "bond_term_years": FactDefinition("债券期限", "债券发行", ("债券期限", "期限"), "years"),
    "coupon_rate": FactDefinition("票面利率", "债券发行", ("票面利率", "债券利率"), "percent"),
    "interest_frequency": FactDefinition("付息频率", "债券发行", ("付息方式", "付息频率"), "text"),
    "principal_repayment": FactDefinition("还本方式", "债券发行", ("还本方式", "偿还本金", "本金偿付"), "text"),
    "bond_maturity_date": FactDefinition("债券到期日", "债券发行", ("到期日", "债券到期日"), "text"),
    # 评级报告的常见写法是"主体**长期**信用等级"（实测中邮原文就是"主体长期信用等级为AA+"），
    # 只收"主体信用等级"会漏掉这一类，主体评级因此取不到值。
    "issuer_rating": FactDefinition(
        "发行人主体评级", "评级",
        ("主体信用等级", "发行人主体信用等级", "主体长期信用等级", "长期信用等级"),
        "text",
    ),
    "bond_rating": FactDefinition("债项评级", "评级", ("债项信用等级", "本期债券信用等级", "债项评级"), "text"),
    "rating_outlook": FactDefinition("评级展望", "评级", ("评级展望", "展望"), "text"),
    "rating_downgrade_trigger": FactDefinition("评级下调触发因素", "评级", ("下调因素", "评级下调因素", "调级因素"), "text"),
    "rating_agency": FactDefinition("评级机构", "评级", ("评级机构", "联合资信", "中诚信", "新世纪"), "text"),
    # 评级日期只认"评级时间/评级日期/跟踪评级…"这类明确标签：不得用发行日、报表日或
    # 文件中任意日期替代（修订稿 §2.4）。别名收紧就是为了这一点。
    "rating_date": FactDefinition("评级时间", "评级", ("评级时间", "评级日期", "跟踪评级日期", "跟踪评级时间"), "text"),
    # 标签本身必须是成品口径：它会被渲染进报告与导出件，而「评级报告」是禁用表述
    # （成品统一写作「外部信用评价文件」）。实测该标签出现在核对表里，直接触发
    # 成品用词违规——一旦那张表接进出口，整份报告会被阻断交付。
    # 别名保留原写法，材料里仍是这么写的，检索不受影响。
    "rating_revenue": FactDefinition("评级口径营业收入", "评级", ("评级报告营业收入", "评级口径营业收入")),
    # The legacy key remains stable for existing evaluations; its value is an
    # interest-bearing-debt adjustment, not an issuer-wide total-debt figure.
    "rating_total_debt": FactDefinition("评级口径有息负债", "评级", ("评级口径有息负债", "评级报告有息负债")),
    "rating_total_assets": FactDefinition("评级口径资产总额", "评级", ("评级口径资产总额",)),
    "rating_total_liabilities": FactDefinition("评级口径负债总额", "评级", ("评级口径负债总额",)),
    "rating_net_profit": FactDefinition("评级口径净利润", "评级", ("评级口径净利润",)),
    "rating_operating_cash_flow": FactDefinition("评级口径经营活动现金流", "评级", ("评级口径经营活动现金流",)),
    "rating_issue_amount": FactDefinition("评级口径发行规模", "评级", ("评级口径发行规模",)),
    "underwriter": FactDefinition("主承销商", "发行结构", ("主承销商", "牵头主承销商"), "text"),
    "trustee": FactDefinition("债券受托管理人", "发行结构", ("受托管理人", "债券受托管理人"), "text"),
    "investor_scope": FactDefinition("发行对象", "发行结构", ("发行对象", "投资者范围"), "text"),
    "bond_exchange": FactDefinition("上市交易所", "发行结构", ("上市交易所", "挂牌交易所", "上海证券交易所", "深圳证券交易所"), "text"),
    "proceeds_amount": FactDefinition("募集资金金额", "募集资金", ("募集资金", "募集资金金额")),
    "proceeds_purpose": FactDefinition("募集资金用途", "募集资金", ("募集资金用途", "资金用途"), "text"),
    "repayment_account": FactDefinition("偿债专户", "偿债安排", ("偿债专户", "偿债账户", "专户"), "text"),
    # 别名必须覆盖材料的真实写法。旧别名只有「预测经营现金流」，而发行文件写的是
    # 「预测经营活动现金流量净额9,000万元」——对不上，于是运行后这条预测事实被
    # 抽取流程清掉、再也不出现，报告里的预测口径披露随之消失（实测：
    # facts 与 canonical_facts 双双为空，6,500 万元与 9,000 万元的口径对照丢失）。
    "operating_cashflow_forecast": FactDefinition(
        "预测经营现金流", "偿债安排",
        ("预测经营现金流", "偿债现金流预测", "预测经营活动现金流量净额", "预测经营活动现金流净额"),
    ),
    "guarantor_name": FactDefinition("保证人", "债券担保", ("保证人", "担保人", "融资担保机构"), "text"),
    "credit_enhancement_status": FactDefinition("增信状态", "债券担保", ("不设定增信措施", "无增信安排", "增信措施"), "text"),
    "guarantee_type": FactDefinition("保证方式", "债券担保", ("保证方式", "担保方式", "连带责任保证"), "text"),
    "guarantee_scope": FactDefinition("保证责任范围", "债券担保", ("保证范围", "担保责任范围"), "text"),
    "guarantee_period": FactDefinition("保证期间", "债券担保", ("保证期间", "担保期间"), "text"),
    "guarantee_cap": FactDefinition("保证责任上限", "债券担保", ("责任上限", "担保责任上限")),
    "counter_guarantee_value": FactDefinition("反担保折后价值", "反担保", ("反担保折后价值", "反担保覆盖价值")),
    "counter_guarantee_priority": FactDefinition("反担保顺位", "反担保", ("抵押顺位", "担保顺位"), "text"),
    "counter_guarantee_rights": FactDefinition("反担保权利状态", "反担保", ("权属状态", "抵押登记", "质押登记"), "text"),
    "investor_protection": FactDefinition("投资者保护措施", "投资者保护", ("投资者保护", "资信维持承诺", "救济措施"), "text"),
    "cross_default_clause": FactDefinition("交叉违约条款", "投资者保护", ("交叉违约", "交叉违约条款"), "text"),
    "legal_opinion_status": FactDefinition("法律意见状态", "发行合规", ("法律意见", "法律意见书", "合法合规"), "text"),
}

NAME_TO_KEY = {definition.label: key for key, definition in FACT_REGISTRY.items()}
EXPECTED_MATERIAL_GROUPS = {
    "申请与主体": {"融资申请", "企业信息"},
    "财务报表": {"财务报表"},
    "流水与纳税": {"银行流水", "纳税资料"},
    "经营与应收": {"合同订单", "应收账款"},
    "债务与担保": {"负债担保"},
    "抵押与访谈": {"抵押物", "访谈纪要"},
}

BOND_EXPECTED_MATERIAL_GROUPS = {
    "募集说明书": {"募集说明书"},
    "信用评级报告": {"信用评级报告"},
}

# The bond guarantee product has one report level.  A parsed prospectus and
# rating report are the required public-information package.  The documents
# below remain useful for independent verification, but their absence must not
# downgrade the report to a different diligence mode.
BOND_SUPPLEMENTARY_MATERIAL_GROUPS = {
    "债务与偿债": {"债务明细", "偿债安排", "银行流水"},
    "主体治理与关联方": {"企业信息", "治理材料", "关联方"},
    "法律与发行决议": {"法律意见", "发行决议"},
    "担保合同": {"担保合同"},
    "反担保材料": {"反担保", "抵押物"},
    "经营与交易真实性": {"合同订单", "访谈纪要", "纳税资料"},
}

BOND_MATERIAL_KEYWORDS = {
    "募集说明书": ("募集说明书", "发行条款"),
    "信用评级报告": ("信用评级报告", "评级报告", "评级结果"),
    "审计与最新财务": ("审计报告", "资产负债表", "利润表", "现金流量表"),
    "债务与偿债": ("债务明细", "到期债务", "偿债", "银行流水"),
    "募集资金用途": ("募集资金用途", "专户管理", "资金使用计划"),
    "主体治理与关联方": ("股权结构", "实际控制人", "关联方", "治理"),
    "法律与发行决议": ("法律意见", "董事会决议", "股东会决议", "发行决议"),
    "承销与受托管理": ("承销协议", "受托管理", "持有人会议规则"),
    "担保合同": ("担保合同", "保证合同", "保证责任范围"),
    "反担保材料": ("反担保", "抵押", "质押", "权属证明", "估值报告"),
    "经营与交易真实性": ("客户", "供应商", "订单", "纳税", "访谈"),
}
MATERIAL_KEYWORDS = {
    "申请与主体": ("申请金额", "实际控制人", "营业执照"),
    "财务报表": ("营业收入", "资产总额", "资产总计"),
    "流水与纳税": ("经营性流入", "银行流水", "纳税申报", "增值税申报"),
    "经营与应收": ("客户", "供应商", "应收账款", "采购合同"),
    "债务与担保": ("有息负债", "到期债务", "对外担保", "连带责任保证"),
    "抵押与访谈": ("抵押", "评估报告", "现场访谈", "访谈"),
}


def canonical_key(name: str) -> str:
    if name in FACT_REGISTRY:
        return name
    if name in NAME_TO_KEY:
        return NAME_TO_KEY[name]
    compact = re.sub(r"\s+", "", name)
    for key, definition in FACT_REGISTRY.items():
        if any(alias in compact for alias in definition.aliases):
            return key
    return ""


def normalize_period(raw: str) -> str:
    match = re.search(r"20\d{2}", str(raw))
    return match.group(0) if match else ""


def normalize_numeric(value: float, unit: str, kind: str) -> tuple[float, str]:
    if kind == "percent":
        return float(value), "%"
    if kind == "months":
        return float(value), "个月"
    if kind == "count":
        return float(value), "人"
    if kind == "years":
        return float(value), "年"
    multiplier = 1.0
    if "亿元" in unit:
        multiplier = 10000
    elif "千元" in unit:
        multiplier = 0.1
    elif unit == "元" or ("元" in unit and "万元" not in unit):
        multiplier = 0.0001
    return round(float(value) * multiplier, 6), "万元"


def _numbers(text: str) -> list[float]:
    return [float(item.replace(",", "")) for item in re.findall(r"(?<!\d)(-?\d[\d,]*(?:\.\d+)?)(?!\d)", text)]


def _unit(text: str, kind: str) -> str:
    if kind == "percent":
        return "%"
    if kind == "months":
        return "个月"
    if kind == "count":
        return "人"
    if kind == "years":
        return "年"
    for unit in ("亿元", "万元", "千元", "元"):
        if unit in text:
            return unit
    return "万元"


def _label_segment(text: str, alias: str) -> tuple[str, str]:
    """Return the text immediately after an alias, up to the next field separator."""
    match = re.search(re.escape(alias), text)
    if not match:
        return "", ""
    field_start = max((text.rfind(separator, 0, match.start()) for separator in "|，。；、;"), default=-1) + 1
    before = text[field_start:match.start()]
    after = text[match.end():]
    # In a conventional horizontal Excel row the values follow the label as
    # `| value_1 | value_2`; retain all value cells for year alignment.
    if after.startswith("|"):
        return before, after[1:]
    return before, re.split(r"[|，。；、;]", after, maxsplit=1)[0]


_YEAR_TOKEN = re.compile(r"(20\d{2})\s*年(?:度|末|初|底)?")

# 带限定词的科目名不是被限定字段本身。例如「流动负债合计」「非流动资产合计」
# 分别包含「负债合计」「资产合计」，但它们是不同的字段。
_QUALIFIED_LABELS = frozenset({"total_assets", "total_liabilities", "accounts_receivable", "equity"})

# Financial facts are comparable only when the statement basis is the same.
# Carry the latest explicit table heading across continuation pages.
FINANCIAL_FACT_KEYS = frozenset({
    "revenue", "net_profit", "operating_cash_flow", "cost_of_goods_sold",
    "cash_received_from_sales", "investing_cash_flow", "financing_cash_flow",
    "interest_bearing_debt", "short_term_debt", "long_term_debt", "bonds_payable",
    "finance_expense", "interest_expense", "other_receivables", "total_overdue_receivables",
    "debt_due_12m", "restricted_cash", "total_assets", "total_liabilities", "equity",
    "current_assets", "current_liabilities", "accounts_receivable", "inventory", "cash",
    "rating_revenue", "rating_total_debt", "rating_total_assets", "rating_total_liabilities",
    "rating_net_profit", "rating_operating_cash_flow",
})
_FINANCIAL_SECTION_ROLES = frozenset({
    "financial_statements", "financial_analysis", "rating_financial_summary",
    "rating_financial_analysis", "rating_financial_appendix",
})
_CONSOLIDATED_STATEMENT_MARKERS = tuple(
    f"\u5408\u5e76{suffix}" for suffix in ("\u8d44\u4ea7\u8d1f\u503a\u8868", "\u5229\u6da6\u8868", "\u73b0\u91d1\u6d41\u91cf\u8868", "\u8d22\u52a1\u62a5\u8868")
)
_PARENT_STATEMENT_MARKERS = tuple(
    f"\u6bcd\u516c\u53f8{suffix}" for suffix in ("\u8d44\u4ea7\u8d1f\u503a\u8868", "\u5229\u6da6\u8868", "\u73b0\u91d1\u6d41\u91cf\u8868", "\u8d22\u52a1\u62a5\u8868")
)


def financial_scope_hint(text: str) -> str | None:
    """Return an explicit consolidated or parent-company statement basis."""
    compact = re.sub(r"\s+", "", str(text or ""))
    if any(term in compact for term in _PARENT_STATEMENT_MARKERS):
        return "standalone"
    if any(term in compact for term in _CONSOLIDATED_STATEMENT_MARKERS):
        return "consolidated"
    return None


def financial_scope_by_source(source_rows: list[dict]) -> dict[str, str]:
    """Infer statement scope and carry it over continuation pages."""
    output: dict[str, str] = {}
    current_by_document: dict[str, str] = {}
    for source in sorted(source_rows, key=lambda item: (
        str(item.get("document_id") or ""), int(item.get("page") or 0), str(item.get("id") or ""),
    )):
        document_id = str(source.get("document_id") or "")
        role = str(source.get("_section_role") or "")
        marker = financial_scope_hint(str(source.get("text") or ""))
        previous_scope = current_by_document.get(document_id)
        if marker:
            current_by_document[document_id] = marker
        # A page that introduces the next statement basis can contain the
        # tail of the previous table as well. Its page-level source cannot be
        # assigned safely to one basis, so leave this page unscoped while
        # carrying the new basis to following continuation pages.
        scope = None if marker and previous_scope and marker != previous_scope else current_by_document.get(document_id)
        if not scope and role in {"rating_financial_summary", "rating_financial_analysis", "rating_financial_appendix"}:
            scope = "consolidated"
        if scope:
            output[str(source.get("id"))] = scope
    return output
_LABEL_QUALIFIERS = ("流动", "非流动", "有息", "或有", "短期", "长期", "净", "固定", "无形", "抵押", "其他", "逾期")


def _line_year_alignment(lines: list[str]) -> dict[int, list[str]]:
    """物理行索引 → 该行所属表格的年度列顺序。

    PDF 表格的表头行（``项目 2025 年末 2024 年末 2023 年末``）本身就是列对齐依据，
    与 Excel 表头年份同理。有了它，``负债合计 50,400 45,864 41,328`` 就能按列对齐成
    三个年度的取值，而不是被猜成"取最后一个数"。

    表头之后的行沿用该表头，直到出现新的表头行或空行。
    """
    alignment: dict[int, list[str]] = {}
    current: list[str] = []
    for index, line in enumerate(lines):
        years = _YEAR_TOKEN.findall(line)
        # 表头行：同一行出现两个以上年度，且年份之外的内容很短（是表头不是正文句子）。
        if len(years) >= 2 and len(re.sub(r"20\d{2}\s*年(?:度|末|初|底)?|[\s|]", "", line)) <= 8:
            current = years
            alignment[index] = current
            continue
        if not line.strip():
            current = []
            continue
        if current:
            alignment[index] = current
    return alignment


def _line_index_of_alias(lines: list[str], alias: str) -> int | None:
    for index, line in enumerate(lines):
        if alias in line:
            return index
    return None


def _physical_line_segment(
    text: str, alias: str, *, reject_qualified: bool = False,
) -> tuple[str, str] | None:
    """在别名所在的**物理行**内取它后面的片段。

    通用抽取原本在「整页去空白」的文本上取片段，表格一行多列时会跨行抓到别的行的
    数字（实测把三个年度的负债合计拼成一个数）。物理行是表格里唯一可靠的分隔。
    行内空格同样要保留——它是同一行里相邻数值之间仅有的分隔依据。

    ``reject_qualified`` 用于跳过「流动负债合计」这类带限定词的科目名，**并继续往后
    找**下一个出现位置：只跳过不继续找，会在遇到限定词行时把整条合法科目一起丢掉
    （实测 `负债合计` 因此抽不出来）。
    """
    for line in _heading_lines_for_fact_support(text):
        if alias not in line:
            continue
        # 管道是 Excel/表格的单元格分隔，空格不是——所以先收紧管道两侧，
        # 再把剩余空白折叠成单空格（保留它作为同一行内相邻数值的唯一分隔）。
        spaced = re.sub(r"\s*\|\s*", "|", line)
        spaced = re.sub(r"[ \t　]+", " ", spaced).strip()
        before, segment = _label_segment(spaced, alias)
        if reject_qualified and before.endswith(_LABEL_QUALIFIERS):
            continue
        return before, segment
    return None


# 评级字段的别名（"主体信用等级"/"债项信用等级"/"展望"）是通用词组，评级报告里
# 还会同时出现**其他主体**的评级：保证人、担保人、可比企业、同行业等。若不限定
# 主体，`保证人主体信用等级为 AA+` 会被当成发行人主体评级抽走——实测北投/中邮
# 评级报告都含保证人评级，一旦污染，评级章的主体与债项评级会整体错位。
RATING_OTHER_SUBJECT_TERMS = (
    # "担保方"必须与"担保人/保证人"同列：实测中邮评级报告写的是
    # 「担保方邮政集团的主体长期信用等级为AAApi」，漏掉它就会把担保方的等级
    # （AAA）当成发行人主体评级，直接违反"中邮主体(AA+)与债项(AAA)不同"的验收项。
    "保证人", "担保人", "担保方", "担保机构", "被担保", "反担保", "增信主体", "差补方",
    "可比企业", "可比公司", "可比主体", "同行业", "同业", "可比",
    "其他主体", "第三方", "关联方",
)


def _chinese_int(text: str) -> int | None:
    """中文数字串 → 整数（支持 〇/零一二…九 逐位，及 十/十X/二十X/三十X）。"""
    if not text:
        return None
    digits = {"〇": 0, "○": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if all(char in digits for char in text):
        return int("".join(str(digits[char]) for char in text))
    if "十" in text:
        left, _, right = text.partition("十")
        tens = digits.get(left) if left else 1
        ones = digits.get(right, 0) if right else 0
        if tens is None or ones is None or (right and right not in digits):
            return None
        return tens * 10 + ones
    return None


# 评级报告署名日期也会用中文数字落款（实测中邮末页"联合资信评估股份有限公司 评级总监：
# 二〇二六年七月十日"）。阿拉伯数字分支之外补这一支；必须仍**紧跟**机构名（可隔署名栏
# 标签"评级总监："），不以标签形态出现的正文年份不得当评级时间（修订稿 §2.4）。
_CHINESE_SIGNATURE_DATE_RE = re.compile(
    r"([〇○零一二三四五六七八九]{2,4})\s*年\s*([一二三四五六七八九十]{1,3})\s*月"
    r"(?:\s*([一二三四五六七八九十]{1,3})\s*日)?"
)


def _signature_date(text: str, spelling: str) -> tuple[str, str] | None:
    """评级机构署名日期 → ``(date:YYYY-MM-DD, 年份)``；不满足"紧跟"约束返回 None。

    支持阿拉伯数字（北投"中诚信国际信用评级有限责任公司2026年7月3日"）与中文数字
    （中邮末页），机构名后可隔署名栏"评级总监："。日期必须紧跟机构名：正文里
    "中诚信国际预计2026年二季度…"这类年份仍然不取。
    """
    for match in re.finditer(re.escape(spelling), text or ""):
        tail = (text or "")[match.end():].lstrip(" \t|｜,，、:：\x01")
        tail = re.sub(r"^评级总监[：:]?", "", tail).lstrip(" \t\x01")
        arabic = re.match(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?", tail)
        if arabic:
            day = arabic.group(3) or "01"
            return f"date:{arabic.group(1)}-{int(arabic.group(2)):02d}-{int(day):02d}", arabic.group(1)
        chinese = _CHINESE_SIGNATURE_DATE_RE.match(tail)
        if chinese:
            year = _chinese_int(chinese.group(1))
            month = _chinese_int(chinese.group(2))
            day = _chinese_int(chinese.group(3)) if chinese.group(3) else 1
            if year and month and day and 1 <= month <= 12 and 1 <= day <= 31:
                return f"date:{year}-{month:02d}-{day:02d}", str(year)
    return None


def _find_alias(
    fact_key: str, aliases: tuple[str, ...], text: str, *, strict_financial_labels: bool = False,
) -> str | None:
    is_rating = fact_key in {"issuer_rating", "bond_rating", "rating_outlook"}
    if fact_key == "rating_date":
        # 报告出具日期形态：**评级机构名后面紧跟年月日**（阿拉伯或中文数字，可隔署名栏
        # "评级总监："）。产品已确认该日期即评级时间。要求日期**紧跟**机构名，避免把
        # 正文里"中诚信国际预计2026年二季度…"这类年份当成评级时间。
        for _canonical, spellings in RATING_AGENCIES:
            for spelling in spellings:
                if _signature_date(text or "", spelling):
                    return spelling
    for alias in aliases:
        for match in re.finditer(re.escape(alias), text):
            # Short prose aliases need boundary checks to avoid mapping "净资产"
            # to total assets or "负债率" to total liabilities.
            left = text[max(0, match.start() - 10):match.start()]
            broad_left = text[max(0, match.start() - 30):match.start()]
            right = text[match.end():match.end() + 6]
            # 评级只认本主体：紧邻的小句里出现保证人/可比企业等限定词时跳过该处命中。
            # 只看紧邻小句（按标点切分 + 10 字窗口）而不是大窗口——否则
            # "保证人主体信用等级为AA+；发行人主体信用等级为AAA" 里后面那条正确命中
            # 也会被前文的"保证人"连带跳过。continue 只跳过当前匹配，后续命中仍会取到。
            if is_rating and any(
                term in re.split(r"[。；;，,、\n：:]", left)[-1] for term in RATING_OTHER_SUBJECT_TERMS
            ):
                continue
            if fact_key == "revenue":
                if strict_financial_labels and alias in {"\u6536\u5165", "\u603b\u6536\u5165"} and not left.endswith(("\u8425\u4e1a", "\u8425\u4e1a\u603b", "\u4e3b\u8425\u4e1a\u52a1")):
                    continue
                if left.endswith("占"):
                    continue
                if "第一大客户" in broad_left or ("客户" in broad_left and "贡献" in broad_left):
                    continue
                if alias == "收入" and any(left.endswith(prefix) for prefix in ("纳税", "税务", "纳税申报", "增值税申报", "开票")):
                    continue
                if alias == "收入" and right.startswith(("占比", "比例")):
                    continue
            if fact_key == "cost_of_goods_sold" and alias == "成本" and any(
                left.endswith(prefix) for prefix in ("人员", "研发", "项目", "分包", "融资", "资金")
            ):
                continue
            if fact_key == "total_assets" and alias == "资产" and (
                any(left.endswith(prefix) for prefix in ("净", "流动", "固定", "抵押", "无形"))
                or right.startswith("负债率")
            ):
                continue
            if fact_key == "total_liabilities" and alias == "负债" and (
                any(left.endswith(prefix) for prefix in ("有息", "流动", "或有", "其他或有", "短期", "长期"))
                or right.startswith("率")
            ):
                continue
            if strict_financial_labels and fact_key == "total_assets" and alias == "\u8d44\u4ea7" and not right.startswith(("\u603b\u8ba1", "\u603b\u989d", "\u5408\u8ba1")):
                continue
            if strict_financial_labels and fact_key == "total_liabilities" and alias == "\u8d1f\u503a" and not right.startswith(("\u603b\u8ba1", "\u603b\u989d", "\u5408\u8ba1")):
                continue
            if strict_financial_labels and fact_key == "current_assets" and alias in ("\u6d41\u52a8\u8d44\u4ea7", "\u6d41\u52a8\u8d44\u4ea7\u5408\u8ba1") and left.endswith("\u975e"):
                continue
            if strict_financial_labels and fact_key == "cash" and alias != "\u8d27\u5e01\u8d44\u91d1":
                continue
            if fact_key == "accounts_receivable":
                if any(term in left for term in ("逾期", "一年以上", "长期")) or left.endswith("其他"):
                    continue
            return alias
    return None


def source_supports_fact(fact_key: str, text: str) -> bool:
    """Return whether a cited fragment explicitly names the asserted field."""
    definition = FACT_REGISTRY.get(fact_key)
    if not definition:
        return False
    compact = re.sub(r"\s+", "", text or "")
    return _find_alias(fact_key, definition.aliases, compact) is not None


def source_supports_fact_value(fact: dict, text: str) -> bool:
    """Validate an LLM field against its labelled raw disclosure.

    This intentionally checks the value in the same physical line or nearby
    table/paragraph window as a field alias.  A page-level numeric match would
    otherwise let an unrelated historical bond amount or table column support a
    current-transaction assertion.
    """
    fact_key = str(fact.get("fact_key") or "")
    definition = FACT_REGISTRY.get(fact_key)
    raw_value = _compact(str(fact.get("raw_value") or ""))
    if not definition or not raw_value:
        return False
    strict_financial_labels = (
        str(fact.get("section_role") or fact.get("_section_role") or "") in _FINANCIAL_SECTION_ROLES
        and fact_key in FINANCIAL_FACT_KEYS
    )
    for line in _heading_lines_for_fact_support(text):
        compact_line = _compact(line)
        alias = _find_alias(fact_key, definition.aliases, compact_line, strict_financial_labels=strict_financial_labels)
        if alias and raw_value in compact_line:
            return True
    if strict_financial_labels:
        return False
    # PDF tables sometimes split a row into two physical lines. Limit the
    # validation window to the field label and immediately following cells.
    compact = _boundary_compact(text)
    for alias in sorted(definition.aliases, key=len, reverse=True):
        start = compact.find(alias)
        while start >= 0:
            window = compact[start:start + 260]
            boundary = window.find("\x01")
            if boundary >= 0:
                window = window[:boundary + 180]
            if raw_value in window:
                return True
            start = compact.find(alias, start + len(alias))
    return False


def _heading_lines_for_fact_support(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").replace("\r", "\n").split("\n") if line.strip()]


def _inline_years(text: str) -> list[str]:
    """Read an explicit paired-year prefix such as ``2024/2025营业收入...``."""
    match = re.search(r"(20\d{2})\s*[/／、~至-]\s*(20\d{2})", text)
    return list(match.groups()) if match else []


def _negative_statement(before: str, segment: str) -> bool:
    return bool(
        re.search(r"(?:无|未|不存在|没有)$", before)
        or re.match(r"(?:为)?(?:无|未发现|不存在|没有)", segment)
        or ("无" in before and not _numbers(before) and not _numbers(segment))
    )


# --- Text-fact value semantics ------------------------------------------------
# Free-text fields share no common vocabulary, so deterministic extraction must
# stay conservative: it only accepts a labelled value (not a sentence fragment)
# and text_conflict_token() decides whether a stored value is a plausible value
# for its field before it is allowed to drive a fact conflict.
RATING_TOKEN_RE = re.compile(r"(?<![A-Za-z])(?:AAA|BBB|BB|CCC|CC|AA|A|C)\s*([＋+－\-]?)(?![A-Za-z])")
# 等级符号左右都不得紧邻 ASCII 字母。实测中邮评级原文写的是
# "邮政集团主体长期信用等级为AAApi"——若允许后随字母，"AAA" 会被当成独立等级取走，
# 把**担保方**的等级记成发行人主体评级（验收明确要求中邮主体 AA+ 与债项 AAA 不同）。
# 中文紧跟不受影响（"（品种一）AAA北京市…" 仍正常命中），同时避免在英文词里切出 A/C。
RATING_OUTLOOKS = ("稳定", "负面", "正面", "发展中", "观察", "列入评级观察", "列入观察")
# (canonical short name, spellings that may appear in the prospectus/rating text)
RATING_AGENCIES = (
    ("中诚信国际", ("中诚信国际信用评级有限责任公司", "中诚信国际")),
    ("联合资信", ("联合资信评估股份有限公司", "联合资信")),
    ("大公国际", ("大公国际资信评估有限公司", "大公国际")),
    ("新世纪", ("上海新世纪资信评估投资服务有限公司", "上海新世纪", "新世纪评级", "新世纪")),
    ("中债资信", ("中债资信评估有限责任公司", "中债资信")),
    ("东方金诚", ("东方金诚国际信用评估有限公司", "东方金诚")),
    ("中证鹏元", ("中证鹏元资信评估股份有限公司", "鹏元资信", "中证鹏元")),
    ("惠誉博华", ("惠誉博华信用评级有限公司", "惠誉博华")),
    ("惠誉", ("惠誉国际信用评级有限公司", "惠誉")),
    ("穆迪", ("穆迪投资者服务公司", "穆迪")),
    ("标普", ("标普信用评级（中国）有限公司", "标普全球", "标普")),
)
# A value for a text field should never start with a connector/function word.
TEXT_BAD_BEGIN = ("、", "（", "。", "，", "的", "之", "之间", "与", "和", "同", "为", "系",
                  "于", "因", "按", "以", "在", "对", "向", "由", "本", "被", "及", "而", "等", "将",
                  "其", "指", "由", "相关", "…", ".")
# Markers that indicate the captured window ran into a clause rather than a value.
TEXT_BAD_MARKERS = (
    "不得", "应当", "之间", "控制下", "相互独立", "情况经", "本息", "未偿还份额", "持有人会议",
    "事项", "之一", "之日起", "视为", "项下的", "且不", "情形", "该等", "对重大", "涉及", "上述",
    "书面说明", "有关", "之约定", "合并范围内", "同一实际控制人", "（一）", "（二）", "（三）",
)
_TEXT_ALIAS_LABELS = (
    "实际控制人", "控股股东", "发行人", "本公司", "公司名称", "注册地址", "住所", "法定代表人",
    "债券名称", "本期债券", "债项名称", "债券受托管理人", "受托管理人", "牵头主承销商", "主承销商",
    "信用评级机构", "评级机构", "主体信用等级", "发行人主体信用等级", "债项信用等级",
    "本期债券信用等级", "评级展望", "保证人", "担保人", "募集资金用途", "资金用途", "主营业务",
    "发行对象", "投资者范围",
)


def _strip_runs(value: str) -> str:
    """Reject values that are an OCR/table legend run (e.g. ``AAAAAA…``)."""
    return re.sub(r"(.)\1{4,}", "", value)


CANONICAL_VALUE_PREFIXES = ("text:", "rating:", "outlook:", "agency:", "date:")


def strip_canonical_prefix(value: object) -> str:
    """把存库的规范化文本还原成成品口径。

    ``text:`` / ``rating:`` / ``outlook:`` 是**内部**的签名前缀，用于让不同写法的
    同一个值比较相等。它们不该出现在报告里——实测漏出去过：正文印成
    「发行人主体评级为rating:AA-，评级展望为outlook:稳定」。
    """
    text = str(value or "")
    for prefix in CANONICAL_VALUE_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def text_value_token(fact_key: str, raw: object) -> str | None:
    """Canonical token for a text-fact value, or None when it is not a value.

    Two assertions only conflict when their canonical tokens differ; a token of
    None means the stored text is not a plausible field value and must not force
    (or participate in) a conflict.

    **必须幂等**：抽取阶段会把本函数的返回值当作 ``text_value`` 存库，冲突分组阶段
    又会对存库的 ``text_value`` 再跑一次。不剥掉已有的前缀，第二次就会得到
    ``text:text陈嘉诚``——与人工断言的真值 ``text:陈嘉诚`` 签名不同，于是同一个值
    被判成来源冲突（实测：实际控制人「陈嘉诚」vs「陈嘉诚」）。
    """
    value = str(raw or "")
    if not value.strip():
        return None
    for prefix in CANONICAL_VALUE_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if _strip_runs(value) != value and len(value) - len(_strip_runs(value)) > 2:
        return None
    if fact_key in ("issuer_rating", "bond_rating"):
        # Ratings live on a small vocabulary; recover just the grade token so
        # "AAA", "主体信用等级为AAA" and "的债项评级为AAA" compare equal.
        #
        # 一段里出现**两个不同评级**时一律不取值：那是比较句而不是字段取值。
        # 实测：评级报告「债项评级较主体评级由AA-提升至AA+」——取第一个会把
        # 债项评级写成主体评级的 AA-，不只是假冲突，是写错报告的取值错误。
        grades = {
            match.group(0).replace("＋", "+").replace("－", "-").replace(" ", "")
            for match in RATING_TOKEN_RE.finditer(value)
        }
        if len(grades) != 1:
            return None
        return "rating:" + grades.pop()
    if fact_key == "rating_outlook":
        for outlook in RATING_OUTLOOKS:
            if outlook in value:
                return "outlook:" + outlook
        return None
    if fact_key == "rating_agency":
        for canonical, spellings in RATING_AGENCIES:
            if any(spelling in value for spelling in spellings):
                return "agency:" + canonical
        return None
    if fact_key in ("establishment_date", "bond_maturity_date", "rating_date"):
        # **幂等**：抽取阶段存库的就是本函数的 ``date:`` 输出（前缀已在上方剥掉），
        # 冲突分组阶段会对存库值再跑一次。只认"年月日"原文形态会让已规范化的
        # "2026-07-03" 二次解析落空 → 该断言被当成垃圾、签名 None →
        # `rating_date` 永远选不进 canonical（实测北投/中邮：facts 有值、canonical 为空）。
        normalized = re.fullmatch(r"((?:19|20)\d{2})-(\d{1,2})-(\d{1,2})", value.strip())
        if normalized:
            month, day = int(normalized.group(2)), int(normalized.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"date:{normalized.group(1)}-{month:02d}-{day:02d}"
        match = re.search(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?", value)
        if not match:
            return None
        day = match.group(3) or "01"
        return f"date:{match.group(1)}-{int(match.group(2)):02d}-{int(day):02d}"
    cleaned = value.strip()
    for label in _TEXT_ALIAS_LABELS:
        if cleaned.startswith(label):
            cleaned = cleaned[len(label):]
            break
    cleaned = re.sub(r"^[为是：:=＝]+", "", cleaned).strip()
    compacted = re.sub(r"[\s　，,。.、；;:：|\"\'“”‘’（）()【】\[\]—\-]+", "", cleaned)
    if not compacted:
        return None
    if compacted[0] in TEXT_BAD_BEGIN or any(marker in compacted for marker in TEXT_BAD_MARKERS):
        return None
    return "text:" + compacted


def _amount_noise(fact_key: str, kind: str, number: float, following: str) -> bool:
    """True when a numeric candidate is period/percentage noise, not an amount.

    A figure that is immediately a percentage or a ``20xx`` year must not be
    recorded as an amount-denominated fact; these two shapes are the source of
    the bulk of phantom conflicts on table-dense prospectuses.
    """
    if kind == "percent":
        return False
    literal_is_year = number >= 1900 and number <= 2099 and abs(number - round(number)) < 1e-9
    if following.startswith("%"):
        return True
    if kind == "amount" and literal_is_year and following.startswith(("年", "末", "初")):
        return True
    return False


def _compact(text: str) -> str:
    """去掉全部空白，用于「同一物理行内」的字面比对。"""
    return re.sub(r"\s+", "", str(text or ""))


def _boundary_compact(text: str) -> str:
    """Collapse spaces but preserve line breaks as \x01 clause boundaries."""
    out: list[str] = []
    for char in str(text):
        if char in " \t　 ":
            continue
        if char in "\r\n\f":
            if out and out[-1] != "\x01":
                out.append("\x01")
            continue
        out.append(char)
    return "".join(out)


# 合法的千分位：每三位一组。不合法说明这是**相邻的两个（多个）数字被拼在了一起**。
_GROUPED_NUMBER = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")


def _amount_numbers(text: str) -> list[float]:
    """Numeric candidates from prose, dropping figures that are percentages.

    Amount-denominated fields keep borrowing an unrelated ``…5.5%`` share,
    growth rate or concentration from the same paragraph; those follow the
    number with ``%`` and are never an amount.

    另外拒绝**千分位不合法**的数字。抽取前会把整页空白（含换行）压掉，于是表格里
    一行多个数值的 ``负债合计 50,400 45,864 41,328`` 会变成 ``50,40045,86441,328``，
    被 ``[\\d,]*`` 当成一个 15 位数 ``504004586441328``。实测这会直接产出错误的
    资产总额并进入报告。这里丢弃而非猜测切分位置：少一个候选可以接受，
    凭空造出一个数值不行。
    """
    output: list[float] = []
    for match in re.finditer(r"(?<!\d)(-?\d[\d,]*(?:\.\d+)?)(?!\d)", text):
        token = match.group(1)
        if "," in token and not _GROUPED_NUMBER.fullmatch(token):
            continue
        following = text[match.end():match.end() + 1]
        if following == "%":
            continue
        output.append(float(token.replace(",", "")))
    return output


def _extract_labelled_text(fact_key: str, alias: str, text: str) -> tuple[str | None, str]:
    """Extract a text-fact value only when the alias is used as a field label.

    The old behaviour scanned the whole compacted page and returned up to 80
    characters after the *first* mention of the alias, so common phrases such as
    "实际控制人控制下的关联公司", "本期债券", "债券受托管理人" or legend tables
    produced garbage assertions (sentence fragments) that later surfaced as
    phantom fact conflicts.  A value is now accepted only for the label forms:
    ``alias为/是/：X``, ``alias|X`` (row cell) or a rating letter directly after
    the alias; X ends at the first punctuation/newline boundary.
    """
    compacted = _boundary_compact(text)
    # 机构名的**别名本身就是值**（别名表列的就是各机构的正式名与简称），不能当"标签"去它
    # 后面找值：实测北投 p1 是"中诚信国际信用评级有限责任公司|编号:CCXI-…"，标签后面没有
    # 机构名，于是整个字段取不到。别名命中即可归一到 agency:短名。
    if fact_key == "rating_agency":
        spelling_token = text_value_token(fact_key, alias)
        if spelling_token and re.search(re.escape(alias), compacted):
            return spelling_token, ""
        # 别名可能只是机构名的**前缀简称**（注册表收"中诚信"，而拼写表收"中诚信国际"），
        # 此时用别名所在的一小段窗口去匹配正式拼写，仍然只认全名/通用简称。
        for match in re.finditer(re.escape(alias), compacted):
            window_token = text_value_token(fact_key, compacted[match.start():match.start() + 48])
            if window_token:
                return window_token, ""
    # 评级结果的固定写法「主体评级AAA/稳定」：展望紧跟在等级之后用斜杠连接，**周围没有
    # "评级展望"标签**（实测北投正文就是"…有限公司AAA/稳定"），靠标签找不到。
    if fact_key == "rating_outlook":
        outlooks = "|".join(sorted(RATING_OUTLOOKS, key=len, reverse=True))
        for paired in re.finditer(
            r"(?:AAA|AA|A|BBB|BB|B|CCC|CC|C)(?:[+-])?[ \t]*[/／][ \t]*(" + outlooks + ")",
            compacted,
        ):
            paired_token = text_value_token(fact_key, paired.group(1))
            if paired_token:
                return paired_token, ""
    is_rating = fact_key in ("issuer_rating", "bond_rating")
    indicator_in_alias = alias.endswith(("为", "是", "：", ":"))
    for match in re.finditer(re.escape(alias), compacted):
        end = match.end()
        next_char = compacted[end:end + 1] if end < len(compacted) else ""
        prev_char = compacted[match.start() - 1] if match.start() > 0 else ""
        direct_value = False
        if indicator_in_alias:
            value_start = end
        elif next_char in "为是：:＝=":
            value_start = end + 1
        elif is_rating and (next_char in "(（" or re.match(r"[A-Za-z0-9]", next_char)):
            value_start = end
        elif next_char == "|" and (match.start() == 0 or prev_char in "\x01|"):
            value_start = end + 1
        elif (match.start() == 0 or prev_char in "\x01|，。；;：:、（(") and next_char and \
                next_char not in "、，。；;:：|（(" and not next_char.isdigit() and next_char != "\x01":
            # Prose field form "…，主营汽车零部件精密加工，…" where the label is
            # followed immediately by its value (no 为/是/： separator).  These are
            # accepted only for short values so a sentence-initial alias cannot
            # swallow the rest of a long clause.
            value_start = end
            direct_value = True
        else:
            continue
        if value_start >= len(compacted):
            continue
        stop = len(compacted)
        for separator in "，。；;|\x01":
            index = compacted.find(separator, value_start)
            if index != -1:
                stop = min(stop, index)
        token_text = compacted[value_start:stop][:80]
        if is_rating:
            # 评级的值就是**紧跟在标签后的那一个等级符号**。不能把
            # "AA+，本期债项信用等级为AAA" 整段交给 text_value_token——它见到两个不同等级
            # 会整体判否（那是为防"由AA-提升至AA+"这类比较句设计的），结果连正确的 AA+
            # 一起丢掉。实测中邮原文正是"公司主体长期信用等级为AA+，本期债项信用等级为AAA"。
            window = compacted[value_start:value_start + 12]
            immediate = RATING_TOKEN_RE.search(window)
            if immediate:
                # 别名与等级之间若隔着比较/变化词（"债项评级较主体评级由AA-提升至AA+"），
                # 紧邻取值会拿到**比较基准**的等级 AA-，把债项评级写错（实测华辰案例）。
                # 整条跳过：这不是字段取值句，正确等级由"债项及评级结果：…AA+"等确切
                # 写法提供，取不到就宁缺毋滥。
                if re.search(r"较|比|由|升至|降至|提升|下调|上调", window[:immediate.start()]):
                    continue
                token_text = immediate.group(0)
        token = text_value_token(fact_key, token_text)
        if token is None:
            continue
        if direct_value and len(token) > 34:
            continue
        preceding = compacted[max(0, match.start() - 160):match.start()]
        years = re.findall(r"20\d{2}", preceding)
        return token, (years[-1] if years else "")
    if fact_key == "rating_date":
        # 报告出具日期（产品已确认视同评级时间）：**紧跟**在评级机构名之后的年月日
        # （阿拉伯或中文数字，可隔署名栏"评级总监："）。没有"评级时间："标签，但位置
        # 固定（机构署名处）。必须"紧跟"，否则正文里"中诚信国际预计2026年二季度…"
        # 这类年份会被当成评级时间（修订稿 §2.4 禁止）。
        for _canonical, spellings in RATING_AGENCIES:
            for spelling in spellings:
                found = _signature_date(compacted, spelling)
                if found:
                    return found
    return None, ""


def _text_fact_is_plausible(fact_key: str, token: str, source_text: str, section_role: str) -> bool:
    """Reject sentence fragments captured as names in long disclosure prose."""
    value = str(token or "")
    if value.startswith("text:"):
        value = value[5:]
    if fact_key in {"guarantor_name", "trustee"}:
        if any(term in value for term in ("\u6709\u6743", "\u5e94\u5f53", "\u6839\u636e", "\u7f3a\u5c11", "\u8303\u56f4", "\u53d1\u751f", "\u6309\u7167", "\u4ef2\u88c1", "\u8d23\u4efb", "\u6388\u4fe1", "\u989d\u5ea6", "\u83b7\u5f97", "\u622a\u81f3")):
            return False
        return any(term in value for term in ("\u6709\u9650\u516c\u53f8", "\u56fd\u6709\u72ec\u8d44\u516c\u53f8", "\u96c6\u56e2", "\u94f6\u884c", "\u8bc1\u5238", "\u4fdd\u9669"))
    if fact_key == "legal_representative":
        if section_role == "issuer_identity" and any(term in source_text for term in ("\u5b50\u516c\u53f8", "\u4e0b\u5c5e\u516c\u53f8", "\u63a7\u80a1\u516c\u53f8")):
            return False
        return len(value) <= 12 and not any(term in value for term in ("\u8425\u4e1a\u6267\u7167", "\u7ecf\u8425\u8303\u56f4", "\u5730\u5740"))
    return True


def deterministic_extract(source_rows: list[dict], allowed_fact_keys: set[str] | None = None) -> list[dict]:
    """Best-effort extraction for labelled rows and common Chinese due-diligence prose."""
    registry = {
        key: definition for key, definition in FACT_REGISTRY.items()
        if allowed_fact_keys is None or key in allowed_fact_keys
    }
    headers: dict[tuple[str, str], list[str]] = {}
    for source in source_rows:
        if source.get("locator_type") != "xlsx_range":
            continue
        sheet = str(source.get("locator", "")).split("!", 1)[0]
        years = re.findall(r"20\d{2}", source["text"])
        if len(years) >= 2:
            headers[(source["document_id"], sheet)] = years

    scope_by_source = financial_scope_by_source(source_rows)
    role_by_source = {str(item.get("id")): str(item.get("_section_role") or "") for item in source_rows}
    strict_financial_scope = any(
        str(item.get("_section_role") or "") in _FINANCIAL_SECTION_ROLES
        for item in source_rows
    )
    output: list[dict] = []
    seen: set[tuple[str, str, float | None, str, str]] = set()
    for source in source_rows:
        text = source["text"]
        compact = re.sub(r"\s+", "", text)
        section_role = str(source.get("_section_role") or "")
        strict_financial_labels = section_role in _FINANCIAL_SECTION_ROLES
        lines = _heading_lines_for_fact_support(text)
        alignment = _line_year_alignment(lines)
        report_period_hint = next(
            (years[0] for index, years in sorted(alignment.items()) if len(years) >= 2 and index < len(lines)),
            "",
        )
        explicit_keys: set[str] = set()
        current_registered_capital = any(
            re.match(r"^\s*\u6ce8\u518c\u8d44\u672c(?:[:\uff1a\s]|$)", line)
            for line in lines
        )
        if "credit_enhancement_status" in registry:
            no_enhancement = next(
                (marker for marker in ("无增信安排", "不设定增信措施", "无担保安排") if marker in compact),
                "",
            )
            if no_enhancement:
                item = _fact_dict(
                    "credit_enhancement_status", "", None, "", "无增信安排", no_enhancement,
                    source["id"], 0.99,
                )
                key = ("credit_enhancement_status", "", None, "无增信安排", source["id"])
                if key not in seen:
                    output.append(item)
                    seen.add(key)
        # PDF financial narratives commonly repeat the same labelled field for
        # several years on one page. The generic pass below intentionally picks
        # one nearby segment; collect every explicit `YYYY年字段：数值单位`
        # assertion first so no period is silently dropped.
        for fact_key, definition in registry.items():
            if fact_key == "registered_capital" and section_role in {"issuer_identity", "rating_issuer_profile"} and not current_registered_capital:
                continue
            if definition.unit_kind == "text":
                continue
            # A year at the beginning of the same sentence applies to every
            # following semicolon-delimited labelled field.
            matched_offsets: set[int] = set()
            for alias in sorted(definition.aliases, key=len, reverse=True):
                pattern = re.compile(
                    rf"{re.escape(alias)}[：:](-?\d[\d,]*(?:\.\d+)?)"
                    rf"\s*(亿元|万元|千元|元|%|年|个月|人)?"
                )
                for match in pattern.finditer(compact):
                    if match.start() in matched_offsets:
                        continue
                    left = compact[max(0, match.start() - 8):match.start()]
                    if fact_key == "total_liabilities" and left.endswith(("流动", "有息", "或有", "短期", "长期")):
                        continue
                    if fact_key == "total_assets" and left.endswith(("流动", "固定", "净", "抵押")):
                        continue
                    if fact_key == "accounts_receivable" and left.endswith(("逾期", "其他", "长期")):
                        continue
                    sentence_start = compact.rfind("。", 0, match.start()) + 1
                    years_in_sentence = re.findall(r"20\d{2}", compact[sentence_start:match.start()])
                    if not years_in_sentence:
                        continue
                    period = years_in_sentence[-1]
                    raw = float(match.group(1).replace(",", ""))
                    raw_unit = match.group(2) or _unit(match.group(0), definition.unit_kind)
                    following = compact[match.end(1):match.end(1) + 1]
                    if _amount_noise(fact_key, definition.unit_kind, raw, following):
                        continue
                    value, unit = normalize_numeric(raw, raw_unit, definition.unit_kind)
                    item = _fact_dict(fact_key, period, value, unit, "", match.group(1), source["id"], 0.94)
                    identity = (fact_key, period, value, "", source["id"])
                    if identity not in seen:
                        output.append(item)
                        seen.add(identity)
                    explicit_keys.add(fact_key)
                    matched_offsets.add(match.start())
        for fact_key, definition in registry.items():
            if fact_key == "registered_capital" and section_role in {"issuer_identity", "rating_issuer_profile"} and not current_registered_capital:
                continue
            if fact_key in explicit_keys:
                continue
            alias = _find_alias(fact_key, definition.aliases, compact, strict_financial_labels=strict_financial_labels)
            if not alias:
                continue
            if definition.unit_kind == "text":
                token, period = _extract_labelled_text(fact_key, alias, text)
                if token is None:
                    continue
                if not _text_fact_is_plausible(fact_key, token, text, section_role):
                    continue
                item = _fact_dict(fact_key, period, None, "", token, token, source["id"], 0.78)
                key = (fact_key, period, None, token, source["id"])
                if key not in seen:
                    output.append(item)
                    seen.add(key)
                continue

            sheet = str(source.get("locator", "")).split("!", 1)[0]
            years = headers.get((source["document_id"], sheet), []) if source.get("locator_type") == "xlsx_range" else []
            inline_years = _inline_years(compact)
            before, segment = _label_segment(compact, alias)
            qualified = fact_key in _QUALIFIED_LABELS
            line_segment = _physical_line_segment(text, alias, reject_qualified=qualified)
            if line_segment is None and qualified:
                # 该字段在本文本里只以带限定词的形式出现（「流动负债合计」等），
                # 没有可用的科目行，不抽。
                continue
            if line_segment is not None:
                before, segment = line_segment
            line_index = _line_index_of_alias(lines, alias)
            aligned_years = alignment.get(line_index) if line_index is not None else None
            alias_start = compact.find(alias)
            preceding_years = re.findall(r"20\d{2}", compact[:alias_start]) if alias_start >= 0 else []
            local_period = (
                normalize_period(before)
                or (inline_years[-1] if inline_years else "")
                or report_period_hint
                or (preceding_years[-1] if preceding_years else normalize_period(text))
            )
            if fact_key == "operating_cashflow_forecast":
                # 预测期是**区间**（发行文件写「2026—2029年（债券存续期）」），而
                # ``normalize_period`` 取首个年份。存续期的末年才是到期年，也是案例
                # 声明与偿债规则比对的对象；取首年会与冻结样本的期间对不上。
                span = re.search(r"(20\d{2})\s*[—\-~至到]\s*(20\d{2})", before)
                if span:
                    local_period = span.group(2)
            numbers = _numbers(segment) if definition.unit_kind == "percent" else _amount_numbers(segment)
            if not numbers and not _negative_statement(before, segment):
                # Only the related-party label conventionally puts its amount before the label.
                # Never borrow an unrelated number from the rest of a paragraph.
                if fact_key == "related_unsupported_amount":
                    preceding_numbers = _numbers(compact[max(0, alias_start - 80):alias_start])
                    numbers = preceding_numbers[-1:] if preceding_numbers else []
            if not numbers:
                if not _negative_statement(before, segment):
                    continue
                numbers = [0.0]
            unit = _unit(segment or text, definition.unit_kind)

            if definition.unit_kind == "percent":
                percent_context = compact[alias_start:alias_start + 120] if alias_start >= 0 else segment
                percent_match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", percent_context)
                value = float(percent_match.group(1)) if percent_match else numbers[-1]
                periods_values = [((inline_years[-1] if inline_years else local_period), value)]
            elif definition.unit_kind == "months":
                month_match = re.search(r"(\d+(?:\.\d+)?)\s*个月", segment)
                value = float(month_match.group(1)) if month_match else numbers[-1]
                periods_values = [(local_period or str(date.today().year), value)]
            elif aligned_years and len(numbers) == len(aligned_years):
                # PDF 表格行：按表头年度列对齐。这是「不猜」的正解——不是猜哪个数
                # 属于哪个年度，而是用表格自己的列顺序。
                periods_values = list(zip(aligned_years, numbers))
            elif years and len(numbers) >= len(years):
                periods_values = list(zip(years, numbers[-len(years):]))
            elif inline_years and len(numbers) >= len(inline_years) and re.search(r"\d\s*[/／]\s*-?\d", segment):
                periods_values = list(zip(inline_years, numbers[-len(inline_years):]))
            elif inline_years:
                periods_values = [(inline_years[-1], numbers[-1])]
            else:
                if len(numbers) > 1:
                    # 标签所在行有多个并列数值，而没有任何列对齐依据（既非 Excel 表头
                    # 年份、也非「2024/2025」式配对年份）时，无法判断哪个数属于本字段。
                    # 旧行为取最后一个，实测会把 2023 年的列值写成 2025 年的值。
                    # 猜错的代价是把错误数字写进报告，因此宁可不抽。
                    continue
                value = numbers[-1]
                if fact_key == "supporting_contract_amount" and "合计" in compact:
                    value = numbers[-1]
                periods_values = [(local_period, value)]

            for period, raw_number in periods_values:
                value, normalized_unit = normalize_numeric(raw_number, unit, definition.unit_kind)
                item = _fact_dict(fact_key, period, value, normalized_unit, "", str(raw_number), source["id"], 0.86)
                key = (fact_key, period, value, "", source["id"])
                if key not in seen:
                    output.append(item)
                    seen.add(key)

    if not any(item["fact_key"] == "overdue_receivables" for item in output):
        aging_groups: dict[tuple[str, str], list[tuple[float, str]]] = {}
        for source in source_rows:
            if source.get("locator_type") != "xlsx_range":
                continue
            label, separator, remainder = source["text"].partition("|")
            label = re.sub(r"\s+", "", label)
            if not separator or not (
                re.fullmatch(r"1[-–—~至]2年", label)
                or re.fullmatch(r"[2-9]\d*年以上", label)
            ):
                continue
            amount_cell = remainder.split("|", 1)[0]
            amounts = _numbers(amount_cell)
            if not amounts:
                continue
            sheet = str(source.get("locator", "")).split("!", 1)[0]
            aging_groups.setdefault((source["document_id"], sheet), []).append((amounts[0], source["id"]))

        for values in aging_groups.values():
            raw_total = sum(value for value, _ in values)
            total, unit = normalize_numeric(raw_total, "万元", "amount")
            item = _fact_dict(
                "overdue_receivables", "", total, unit, "", " + ".join(str(value) for value, _ in values),
                values[0][1], 0.95,
            )
            item["source_ids"] = [source_id for _, source_id in values]
            output.append(item)

    # Balance-sheet identity is deterministic and keeps model prompts free from arithmetic.
    equity_seen = {(item["period"], item["source_ids"][0]) for item in output if item["fact_key"] == "equity" and item["source_ids"]}
    assets = {(item["period"], item["source_ids"][0]): item for item in output if item["fact_key"] == "total_assets" and item["source_ids"]}
    liabilities = {(item["period"], item["source_ids"][0]): item for item in output if item["fact_key"] == "total_liabilities" and item["source_ids"]}
    for identity, asset in assets.items():
        liability = liabilities.get(identity)
        if not liability or identity in equity_seen:
            continue
        value = asset["value"] - liability["value"]
        output.append(_fact_dict("equity", asset["period"], value, "万元", "", f"{asset['value']}-{liability['value']}", asset["source_ids"][0], 0.95))
    # Attach the statement basis after all row extraction and drop financial
    # values whose basis cannot be established in a bond financial section.
    scoped_output: list[dict] = []
    issuer_identity_text_seen: set[tuple[str, str]] = set()
    for item in output:
        source_ids = item.get("source_ids") or []
        source_id = str(source_ids[0]) if source_ids else ""
        if item.get("fact_key") in {"legal_representative", "registered_capital"} and role_by_source.get(source_id) == "issuer_identity":
            identity_key = (item["fact_key"], "entity")
            if identity_key in issuer_identity_text_seen:
                continue
            issuer_identity_text_seen.add(identity_key)
        if item.get("fact_key") not in FINANCIAL_FACT_KEYS:
            scoped_output.append(item)
            continue
        scope = scope_by_source.get(source_id)
        if scope:
            item["scope"] = scope
        elif strict_financial_scope:
            continue
        scoped_output.append(item)
    return scoped_output


def _fact_dict(
    fact_key: str, period: str, value: float | None, unit: str, text_value: str,
    raw_value: str, source_id: str, confidence: float,
) -> dict:
    definition = FACT_REGISTRY[fact_key]
    return {
        "id": str(uuid.uuid4()), "category": definition.category, "fact_key": fact_key,
        "name": definition.label, "period": period, "value": value, "unit": unit,
        "text_value": text_value, "raw_value": raw_value, "source_ids": [source_id],
        "confidence": confidence, "extraction_method": "deterministic", "review_status": "unreviewed",
    }


def merge_facts(*fact_groups: Iterable[dict]) -> list[dict]:
    chosen: dict[tuple[str, str, str, str], dict] = {}
    for group in fact_groups:
        for fact in group:
            key = (fact["fact_key"], fact.get("subject", ""), fact.get("scope", "unknown"), fact.get("period", ""))
            current = chosen.get(key)
            rank = (fact.get("review_status") == "human_verified", fact.get("confidence", 0))
            current_rank = ((current or {}).get("review_status") == "human_verified", (current or {}).get("confidence", 0))
            if current is None or rank > current_rank:
                chosen[key] = fact
    return list(chosen.values())


def replace_extracted_facts(project_id: str, extracted: list[dict]) -> None:
    existing = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
    human = [item for item in existing if item.get("review_status") == "human_verified"]
    merged = merge_facts(extracted, human)
    with connect() as conn:
        conn.execute("DELETE FROM facts WHERE project_id=? AND review_status!='human_verified'", (project_id,))
        for fact in merged:
            if fact.get("review_status") == "human_verified":
                continue
            conn.execute(
                "INSERT INTO facts (id,project_id,category,name,period,value,unit,text_value,source_ids,confidence,fact_key,raw_value,extraction_method,review_status,subject,subject_id,scope,period_type,origin_type,origin_ref) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fact.get("id", str(uuid.uuid4())), project_id, fact["category"], fact["name"], fact.get("period", ""),
                 fact.get("value"), fact.get("unit", ""), fact.get("text_value", ""), json_dump(fact.get("source_ids", [])),
                 fact.get("confidence", 0.8), fact["fact_key"], fact.get("raw_value", ""),
                 fact.get("extraction_method", "model"), fact.get("review_status", "unreviewed"), fact.get("subject", ""),
                 fact.get("subject_id", ""), fact.get("scope", "unknown"), fact.get("period_type", "unknown"),
                 fact.get("origin_type", "document"), fact.get("origin_ref", "")),
            )


def decode_facts(items: list[dict]) -> list[dict]:
    for item in items:
        item["source_ids"] = json_load(item["source_ids"])
    return items


def material_inventory(project_id: str) -> dict:
    documents = rows("SELECT * FROM documents WHERE project_id=? ORDER BY created_at", (project_id,))
    project = rows(
        "SELECT project_type,analysis_target,analysis_stale,counter_guarantee_status,profile_snapshot_id "
        "FROM projects WHERE id=?",
        (project_id,),
    )
    bond_mode = bool(project and project[0].get("project_type") == "bond_guarantee")
    expected_groups = BOND_EXPECTED_MATERIAL_GROUPS if bond_mode else EXPECTED_MATERIAL_GROUPS
    supplementary_groups = BOND_SUPPLEMENTARY_MATERIAL_GROUPS if bond_mode else {}
    keywords = BOND_MATERIAL_KEYWORDS if bond_mode else MATERIAL_KEYWORDS
    kinds = {item["kind"] for item in documents if item["parse_status"] == "parsed"}
    source_text = " ".join(item["text"] for item in rows("SELECT text FROM sources WHERE project_id=?", (project_id,)))
    def covered(group: str, expected: set[str]) -> bool:
        if kinds & expected:
            return True
        # A prospectus normally names audit, legal and guarantee documents as
        # references. Mentioning them is not evidence that the original file
        # was uploaded, so bond completeness is determined by document kind.
        if bond_mode:
            return False
        if group == "抵押与访谈" and any(phrase in source_text for phrase in ("未提供抵押", "无抵押物", "缺少抵押")):
            return any(phrase in source_text for phrase in ("折后抵押价值", "评估价值", "现场访谈", "访谈确认"))
        return any(keyword in source_text for keyword in keywords[group])

    coverage = {group: covered(group, expected) for group, expected in expected_groups.items()}
    supplementary_coverage = {
        group: covered(group, expected)
        for group, expected in supplementary_groups.items()
    }
    no_enhancement = False
    if bond_mode:
        canonical_guarantee_rows = rows(
            "SELECT value,text_value,status FROM canonical_facts WHERE project_id=? AND fact_key IN ('guarantee_type','credit_enhancement_status')",
            (project_id,),
        )
        guarantee_rows = canonical_guarantee_rows or rows(
            "SELECT value,text_value,'' status FROM facts WHERE project_id=? AND fact_key IN ('guarantee_type','credit_enhancement_status')",
            (project_id,),
        )
        no_enhancement = any(
            any(marker in str(item.get("text_value") or item.get("value") or "") for marker in ("无增信", "不设定增信", "无担保安排"))
            and item.get("status") != "conflicted"
            for item in guarantee_rows
        )
        # Before a reference project is re-run, its old seed assertions are
        # deliberately excluded from analysis.  The underlying prospectus can
        # still establish the narrow material-gate fact that no enhancement is
        # applicable, without turning any transaction term into an active fact.
        if not no_enhancement:
            no_enhancement = any(
                marker in source_text
                for marker in ("本期债券不设定增信措施", "本期债券无增信安排", "不设定增信措施")
            )
        # The prospectus has positively established that guarantee documents
        # are inapplicable. Keep that fact visible without creating a false
        # supplementary-evidence gap.
        if no_enhancement:
            supplementary_coverage["担保合同"] = True
    profile_state = {"present": False, "complete": False, "counts": {}, "blocking_reasons": []}
    if bond_mode and project and project[0].get("profile_snapshot_id"):
        from .entity_profiles import get_project_snapshot, snapshot_readiness

        profile_state = snapshot_readiness(project_id)
        snapshot = get_project_snapshot(project_id) or {}
        issuer_keys = {
            item.get("fact_key") for item in snapshot.get("facts", [])
            if item.get("role") == "issuer" and item.get("status") not in {"stale", "insufficient"}
        }
        if issuer_keys & {"controller", "controller_share", "related_parties"}:
            supplementary_coverage["主体治理与关联方"] = True
        if {"interest_bearing_debt", "debt_due_12m"} <= issuer_keys:
            supplementary_coverage["债务与偿债"] = True
        if issuer_keys & {"main_business", "top_customer_share", "top_supplier_share"}:
            supplementary_coverage["经营与交易真实性"] = True
    if bond_mode and project and project[0].get("counter_guarantee_status") == "not_applicable":
        supplementary_coverage["反担保材料"] = True
    gaps = [
        {"group": group, "expectedKinds": sorted(expected), "status": "missing", "blocking_level": "blocking"}
        for group, expected in expected_groups.items()
        if not coverage[group]
    ]
    supplementary_gaps = [
        {"group": group, "expectedKinds": sorted(expected), "status": "missing", "blocking_level": "non_blocking"}
        for group, expected in supplementary_groups.items()
        if not supplementary_coverage[group]
    ]
    failed = sum(item["parse_status"] != "parsed" for item in documents)
    core_materials_ready = bool(
        bond_mode
        and coverage.get("募集说明书")
        and coverage.get("信用评级报告")
    )
    readiness = (
        "ready" if core_materials_ready else "partial" if documents else "empty"
    ) if bond_mode else ("ready" if documents and not gaps and not failed else "partial" if documents else "empty")
    profile_gate_ready = bool(
        not (project and project[0].get("profile_snapshot_id"))
        or profile_state.get("ready_for_agent")
    )
    complete_due_diligence_ready = bool(bond_mode and core_materials_ready and profile_gate_ready)
    # Kept as a response alias for old clients. New clients must use the
    # unified complete_due_diligence_ready field and full_due_diligence mode.
    core_pre_review_ready = complete_due_diligence_ready
    analysis_mode = "full_due_diligence" if complete_due_diligence_ready else "insufficient" if bond_mode else readiness
    parsed = len(documents) - failed
    average_quality = round(
        sum(float(item.get("quality") or 0) for item in documents) / len(documents), 4
    ) if documents else 0.0
    core_groups = ("募集说明书", "信用评级报告")
    core_readiness = sum(bool(coverage.get(group)) for group in core_groups) / len(core_groups) if bond_mode else 0.0
    full_readiness = core_readiness if bond_mode else sum(bool(value) for value in coverage.values()) / len(coverage) if coverage else 0.0
    # Historical projects can retain their stored target, but externally they
    # are always represented as the product's single report mode.
    target = "full_due_diligence" if bond_mode else project[0].get("analysis_target", "full_due_diligence") if project else "full_due_diligence"
    blocking_items = gaps
    non_blocking_gaps = supplementary_gaps
    if project:
        execute(
            "UPDATE projects SET effective_analysis_mode=? WHERE id=?",
            (analysis_mode, project_id),
        )
    return {
        "documents": documents,
        "gaps": gaps,
        "supplementary_gaps": supplementary_gaps,
        "readiness": readiness,
        "core_materials_ready": core_materials_ready,
        "core_pre_review_ready": core_pre_review_ready,
        "complete_due_diligence_ready": complete_due_diligence_ready,
        "analysis_mode": analysis_mode,
        "effective_analysis_mode": analysis_mode,
        "analysis_target": target,
        "analysis_stale": bool(project and project[0].get("analysis_stale")),
        "core_pre_review_readiness": round(core_readiness, 4),
        "full_due_diligence_readiness": round(full_readiness, 4),
        "blocking_items": blocking_items,
        "non_blocking_gaps": non_blocking_gaps,
        "profile_snapshot": profile_state,
        "guarantee_applicable": not no_enhancement,
        "parse_quality": {
            "total": len(documents),
            "parsed": parsed,
            "failed": failed,
            "average": average_quality,
        },
    }
