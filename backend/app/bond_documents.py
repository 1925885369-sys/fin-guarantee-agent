"""Deterministic fictional bond disclosure packs used by the demo and eval suite.

The public 北投 documents are structural references only.  No reference text,
issuer fact, intermediary name, report number, or visual identity is used here.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from decimal import Decimal
from pathlib import Path
from typing import Literal, Union

import fitz
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .database import connect, json_dump, now_iso
from .facts import FACT_REGISTRY
from .financial_statements import AnnualStatements, build_case_statements
from .parsers import parse_document, sha256
from .pdf_tables import FinancialTable, PdfWriter, RenderedTable, TableCell, cell_value, render_table
from .table_extraction import TABLE_PARSER_VERSION
from .table_store import persist_tables


TEMPLATE_VERSION = "bond-disclosure-v6-tables"
FICTION_NOTICE = "虚构案例，仅用于系统演示"
PROSPECTUS_NAME = "募集说明书（申报稿）.pdf"
RATING_NAME = "信用评级报告（拟发行版）.pdf"


class FinancialSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    year: int
    revenue: Decimal
    net_profit: Decimal
    operating_cash_flow: Decimal
    total_assets: Decimal
    total_liabilities: Decimal
    equity: Decimal
    current_assets: Decimal
    current_liabilities: Decimal
    cash: Decimal
    restricted_cash: Decimal
    other_receivables: Decimal
    short_term_debt: Decimal
    debt_due_12m: Decimal
    long_term_debt: Decimal
    bonds_payable: Decimal
    interest_bearing_debt: Decimal
    cost_of_goods_sold: Decimal
    finance_expense: Decimal
    interest_expense: Decimal
    accounts_receivable: Decimal
    overdue_receivables: Decimal
    inventory: Decimal
    cash_received_from_sales: Decimal
    investing_cash_flow: Decimal
    financing_cash_flow: Decimal

    @model_validator(mode="after")
    def balance_sheet_balances(self) -> "FinancialSnapshot":
        if self.total_assets != self.total_liabilities + self.equity:
            raise ValueError(f"{self.year}年资产负债表不平：资产必须等于负债加权益")
        if self.interest_bearing_debt != self.short_term_debt + self.long_term_debt + self.bonds_payable:
            raise ValueError(f"{self.year}年有息负债与分项不一致")
        if self.restricted_cash > self.cash:
            raise ValueError(f"{self.year}年受限资金不得超过货币资金")
        return self


class CashflowForecast(BaseModel):
    """债券存续期经营现金流预测的披露。

    它与财务表所载**历史实际数**主体相同，但期间与口径都不同（前瞻性预测 vs 已审
    实际），因此**不得**注册为同一字段的数值冲突。``PLAN_material.md`` Phase 1.3
    要求的「来源性质」与「待核验问题」就落在 ``basis`` 与 ``verification_gap``。
    """

    model_config = ConfigDict(frozen=True)
    amount: Decimal
    basis: str = "发行人对债券存续期经营活动现金流量净额的预测值，属前瞻性信息，未经审计"
    verification_gap: str = "预测所依据的在手订单、产能利用率与客户回款账期支持文件未随本核心材料包提供"


def _forecast(amount: Decimal) -> CashflowForecast:
    return CashflowForecast(amount=amount)


class DeclaredConflict(BaseModel):
    fact_key: str
    period: str
    prospectus_value: Union[Decimal, str]
    rating_value: Union[Decimal, str]


class BondCaseSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    short: str
    company: str
    industry: str
    bond_name: str
    issue_amount: Decimal
    rating_issue_amount: Decimal
    registered_amount: Decimal
    term_years: int
    coupon_rate: Decimal
    issuer_rating: str
    bond_rating: str
    rating_outlook: str = "稳定"
    controller: str
    controller_share: Decimal
    main_business: str
    proceeds_amount: Decimal
    proceeds_purpose: str
    forecast: CashflowForecast
    debt_due_12m: Decimal
    top_customer: Decimal
    top5_customer: Decimal
    top_supplier: Decimal
    top5_supplier: Decimal
    related_unsupported: Decimal
    guarantee_scope: str
    guarantee_cap: Decimal
    counter_value: Decimal
    counter_rights: str
    governance_note: str
    qualitative_note: str = ""
    full_length: bool = False
    financials: tuple[FinancialSnapshot, ...] = Field(min_length=3)
    statements: tuple[AnnualStatements, ...] = ()
    conflicts: tuple[DeclaredConflict, ...] = ()
    expected_warnings: tuple[str, ...] = ()
    expected_candidate_taxonomies: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_case(self) -> "BondCaseSpec":
        years = [item.year for item in self.financials]
        if years != sorted(years) or len(set(years)) != len(years):
            raise ValueError("财务期间必须按年度递增且不得重复")
        declared = {(item.fact_key, item.period) for item in self.conflicts}
        if self.issue_amount != self.rating_issue_amount and ("bond_issue_amount", "2026") not in declared:
            raise ValueError("募集说明书和评级报告发行规模不一致时必须显式声明冲突")
        return self

    @property
    def latest(self) -> FinancialSnapshot:
        return self.financials[-1]

    @property
    def forecast_cashflow(self) -> Decimal:
        return self.forecast.amount

    @property
    def forecast_period(self) -> str:
        """2026 年发行、期限 N 年 → 到期年为 2026 + N。"""
        return f"2026—{2026 + self.term_years}年（债券存续期）"

    @property
    def latest_operating_cash_flow(self) -> Decimal:
        """最近一年已审实际经营现金流净额，用于与预测值并列展示。"""
        return self.latest.operating_cash_flow

    def forecast_disclosure(self) -> str:
        """把预测值的期间、口径与待核验事项写成一句可直接披露的话。"""
        return (
            f"{self.forecast_period}预测经营活动现金流量净额{_fmt(self.forecast.amount)}万元"
            f"（{self.forecast.basis}）；该口径与财务表所载{self.latest.year}年实际"
            f"{_fmt(self.latest_operating_cash_flow)}万元不同期间、不同口径，两者不得混同或相互替代。"
            f"待核验事项：{self.forecast.verification_gap}。"
        )


def _d(value: float | int | str) -> Decimal:
    return Decimal(str(value))


def _statements(
    revenue: int, net_profit: int, ocf: int, assets: int, liabilities: int,
    current_assets: int, current_liabilities: int, debt: int, interest: int,
    receivable: int, overdue: int, debt_due_12m: int, related_unsupported: int,
) -> tuple[AnnualStatements, ...]:
    """按案例级标量构造三年报表。聚合值算式见 ``financial_statements``。"""
    return build_case_statements(
        revenue=revenue, net_profit=net_profit, ocf=ocf, assets=assets, liabilities=liabilities,
        current_assets=current_assets, current_liabilities=current_liabilities, debt=debt,
        interest=interest, receivable=receivable, overdue=overdue, debt_due_12m=debt_due_12m,
        related_unsupported=related_unsupported,
    )


def _case(**values: object) -> BondCaseSpec:
    """构造案例。``statements`` 是唯一数据源，``financials`` 由它派生。"""
    statements = values.pop("statements", ())
    if statements:
        values["financials"] = tuple(
            FinancialSnapshot(year=item.year, **item.snapshot()) for item in statements
        )
        values["statements"] = statements
    return BondCaseSpec(**values)


CASE_SPECS: tuple[BondCaseSpec, ...] = (
    _case(
        id="demo-bond-huachen", short="huachen", company="华辰精密制造有限公司", industry="汽车零部件制造",
        bond_name="华辰精密制造有限公司2026年面向专业投资者公开发行公司债券（第一期）",
        issue_amount=_d(20000), rating_issue_amount=_d(20000), registered_amount=_d(30000), term_years=3,
        coupon_rate=_d("4.8"), issuer_rating="AA-", bond_rating="AA+", controller="陈嘉诚", controller_share=_d(58),
        main_business="精密汽车结构件、传动部件及新能源车热管理组件的研发、生产和销售",
        proceeds_amount=_d(20000), proceeds_purpose="补充生产经营流动资金并置换到期银行借款",
        forecast=_forecast(_d(9000)), debt_due_12m=_d(20500), top_customer=_d(36), top5_customer=_d(67),
        top_supplier=_d(31), top5_supplier=_d(64), related_unsupported=_d(1200),
        guarantee_scope="债券本金、利息、违约金及实现债权费用", guarantee_cap=_d(23000), counter_value=_d(16000),
        counter_rights="拟以厂房和设备提供反担保，其中厂房为第二顺位，估值基准日距申报日超过12个月",
        governance_note="董事会五名成员中三名由实际控制人提名，重大付款执行双人复核。",
        full_length=True,
        statements=_statements(48000, 2800, 6500, 72000, 50400, 31000, 28200, 31000, 3800, 12000, 3000, 20500, 1200),
        expected_warnings=("material_completeness", "debt_maturity", "leverage_liquidity", "receivable_quality", "customer_concentration", "supplier_concentration", "related_party", "counter_guarantee", "cashflow_debt_service", "bullet_maturity"),
    ),
    _case(
        id="demo-bond-ancheng", short="ancheng", company="安澄医疗器械有限公司", industry="医疗器械制造",
        bond_name="安澄医疗器械有限公司2026年面向专业投资者公开发行公司债券（第一期）",
        issue_amount=_d(12000), rating_issue_amount=_d(12000), registered_amount=_d(20000), term_years=3,
        coupon_rate=_d("4.2"), issuer_rating="AA-", bond_rating="AA+", controller="林澄", controller_share=_d(51),
        main_business="医用影像部件和微创手术耗材的研发、生产与销售", proceeds_amount=_d(12000),
        proceeds_purpose="补充医疗器械生产经营流动资金", forecast=_forecast(_d(15000)), debt_due_12m=_d(3500),
        top_customer=_d(18), top5_customer=_d(48), top_supplier=_d(20), top5_supplier=_d(52), related_unsupported=_d(0),
        guarantee_scope="债券本金、利息、违约金及实现债权费用", guarantee_cap=_d(14500), counter_value=_d(15000),
        counter_rights="募集说明书披露拟设第一顺位抵押；权属、估值、登记及优先顺位以正式文件为准，相关原件未纳入本核心材料包",
        governance_note="董事会、合同审批、印章保管和网银权限相互分离。",
        statements=_statements(36000, 2800, 5200, 65000, 30000, 30000, 15000, 18000, 1800, 5500, 300, 3500, 0),
        expected_warnings=("material_completeness",),
    ),
    _case(
        id="demo-bond-rongyi", short="rongyi", company="融益供应链贸易有限公司", industry="供应链贸易",
        bond_name="融益供应链贸易有限公司2026年面向专业投资者公开发行公司债券（第一期）",
        issue_amount=_d(15000), rating_issue_amount=_d(18000), registered_amount=_d(20000), term_years=2,
        coupon_rate=_d("5.3"), issuer_rating="A+", bond_rating="AA", controller="周益民", controller_share=_d(62),
        main_business="工业原材料供应链采购、仓储及分销服务", proceeds_amount=_d(12000),
        proceeds_purpose="补充采购周转资金", forecast=_forecast(_d(8000)), debt_due_12m=_d(16000),
        top_customer=_d(35), top5_customer=_d(66), top_supplier=_d(38), top5_supplier=_d(70), related_unsupported=_d(800),
        guarantee_scope="债券本金、利息、违约金及实现债权费用", guarantee_cap=_d(18000), counter_value=_d(9000),
        counter_rights="拟设第二顺位抵押，第一顺位债权余额尚未提供，估值基准日距申报日15个月",
        governance_note="关联企业往来800万元尚未提供贸易合同和货权凭证。",
        statements=_statements(42000, 1500, 1800, 56000, 40000, 25000, 23000, 28000, 2600, 12000, 2800, 16000, 800),
        conflicts=(DeclaredConflict(fact_key="bond_issue_amount", period="2026", prospectus_value=_d(15000), rating_value=_d(18000)), DeclaredConflict(fact_key="revenue", period="2025", prospectus_value=_d(42000), rating_value=_d(39000)), DeclaredConflict(fact_key="interest_bearing_debt", period="2025", prospectus_value=_d(28000), rating_value=_d(32000))),
        expected_warnings=("material_completeness", "bond_term_consistency", "proceeds_coverage", "financial_tie_out", "interest_coverage", "debt_maturity", "leverage_liquidity", "receivable_quality", "customer_concentration", "supplier_concentration", "related_party", "counter_guarantee", "cashflow_debt_service", "bullet_maturity"),
    ),
    _case(
        id="demo-bond-jinghe", short="jinghe", company="景和城市服务有限公司", industry="城市综合服务",
        bond_name="景和城市服务有限公司2026年面向专业投资者公开发行公司债券（第一期）",
        issue_amount=_d(10000), rating_issue_amount=_d(10000), registered_amount=_d(15000), term_years=3,
        coupon_rate=_d("4.9"), issuer_rating="A+", bond_rating="AA", controller="许景和", controller_share=_d(66),
        main_business="园区运营、物业管理及城市设施维护服务", proceeds_amount=_d(10000),
        proceeds_purpose="补充城市服务项目运营资金", forecast=_forecast(_d(9000)), debt_due_12m=_d(7000),
        top_customer=_d(28), top5_customer=_d(58), top_supplier=_d(22), top5_supplier=_d(55), related_unsupported=_d(1800),
        guarantee_scope="仅债券本金", guarantee_cap=_d(10000), counter_value=_d(8500),
        counter_rights="拟质押部分应收账款，质押通知尚未送达相关债务人",
        governance_note="实际控制人同时控制合同审批、印章及网银复核，财务负责人不能独立否决付款。",
        statements=_statements(24000, 1800, 2600, 42000, 25000, 16000, 13000, 16000, 1600, 6000, 900, 7000, 1800),
        expected_warnings=("material_completeness", "related_party", "guarantee_scope", "counter_guarantee", "cashflow_debt_service", "bullet_maturity"),
        expected_candidate_taxonomies=("governance",),
    ),
    _case(
        id="demo-bond-conflict", short="conflict", company="瀚川绿色装备有限公司", industry="节能环保装备",
        bond_name="瀚川绿色装备有限公司2026年面向专业投资者公开发行公司债券（第一期）",
        issue_amount=_d(20000), rating_issue_amount=_d(24000), registered_amount=_d(30000), term_years=3,
        coupon_rate=_d("5.1"), issuer_rating="AA-", bond_rating="AA+", controller="顾瀚川", controller_share=_d(55),
        main_business="工业节能系统及绿色制造装备的设计、生产和工程服务", proceeds_amount=_d(15000),
        proceeds_purpose="补充工程项目周转资金", forecast=_forecast(_d(11000)), debt_due_12m=_d(21000),
        top_customer=_d(32), top5_customer=_d(65), top_supplier=_d(26), top5_supplier=_d(61), related_unsupported=_d(2500),
        guarantee_scope="债券本金、利息、违约金及实现债权费用", guarantee_cap=_d(24000), counter_value=_d(14000),
        counter_rights="拟抵押厂房存在在先抵押，登记顺位待确认，估值基准日距申报日18个月",
        governance_note="工程项目回款与验收资料尚未随本核心材料包提供，需与合同台账及审计报表交叉核验。",
        statements=_statements(50000, 2200, 1000, 70000, 51000, 27000, 25000, 36000, 4200, 15000, 4200, 21000, 2500),
        conflicts=(
            DeclaredConflict(fact_key="bond_issue_amount", period="2026", prospectus_value=_d(20000), rating_value=_d(24000)),
            DeclaredConflict(fact_key="revenue", period="2025", prospectus_value=_d(50000), rating_value=_d(43000)),
            DeclaredConflict(fact_key="interest_bearing_debt", period="2025", prospectus_value=_d(36000), rating_value=_d(41000)),
        ),
        expected_warnings=("material_completeness", "bond_term_consistency", "proceeds_coverage", "financial_tie_out", "interest_coverage", "debt_maturity", "leverage_liquidity", "receivable_quality", "customer_concentration", "supplier_concentration", "related_party", "counter_guarantee", "cashflow_debt_service", "bullet_maturity"),
    ),
    _case(
        id="demo-bond-qualitative", short="qualitative", company="启明数字产业有限公司", industry="数字产业服务",
        bond_name="启明数字产业有限公司2026年面向专业投资者公开发行公司债券（第一期）",
        issue_amount=_d(8000), rating_issue_amount=_d(8000), registered_amount=_d(12000), term_years=3,
        coupon_rate=_d("4.7"), issuer_rating="A+", bond_rating="AA", controller="沈启明", controller_share=_d(59),
        main_business="企业数字化平台实施、数据治理和运维服务", proceeds_amount=_d(8000),
        proceeds_purpose="补充数字化项目交付资金", forecast=_forecast(_d(9000)), debt_due_12m=_d(1500),
        top_customer=_d(25), top5_customer=_d(55), top_supplier=_d(18), top5_supplier=_d(49), related_unsupported=_d(0),
        guarantee_scope="债券本金、利息、违约金及实现债权费用", guarantee_cap=_d(9500), counter_value=_d(10000),
        counter_rights="第一顺位应收账款质押拟于发行前完成登记",
        governance_note="合同、印章和付款权限执行岗位分离。",
        qualitative_note="抽查说明披露两份大额合同缺少客户盖章，尚待取得签署原件、客户回函、发票和付款流水核验合同真实性。",
        statements=_statements(18000, 1600, 2400, 30000, 15000, 14000, 8000, 9000, 900, 3500, 300, 1500, 0),
        expected_warnings=("material_completeness", "counter_guarantee"), expected_candidate_taxonomies=("authenticity",),
    ),
)

CASE_BY_ID = {case.id: case for case in CASE_SPECS}


PROSPECTUS_SECTIONS = (
    ("risk", "第一节 风险提示及说明"), ("terms", "第二节 发行条款"),
    ("proceeds", "第三节 募集资金运用"), ("issuer", "第四节 发行人基本情况"),
    ("financial", "第五节 发行人财务状况"), ("credit", "第六节 发行人信用状况"),
    ("enhancement", "第七节 增信情况"), ("tax", "第八节 税项"),
    ("disclosure", "第九节 信息披露安排"), ("protection", "第十节 投资者保护机制"),
    ("default", "第十一节 违约事项及纠纷解决机制"), ("holders", "第十二节 债券持有人会议规则"),
    ("trustee", "第十三节 债券受托管理人"), ("institutions", "第十四节 发行有关机构"),
    ("statements", "第十五节 发行人及有关机构声明"), ("reference", "第十六节 备查文件"),
)

FULL_COUNTS = {"risk": 5, "terms": 3, "proceeds": 3, "issuer": 10, "financial": 15, "credit": 4, "enhancement": 4, "tax": 1, "disclosure": 2, "protection": 2, "default": 2, "holders": 3, "trustee": 3, "institutions": 2, "statements": 3, "reference": 1}
MEDIUM_COUNTS = {"risk": 3, "terms": 2, "proceeds": 2, "issuer": 5, "financial": 8, "credit": 2, "enhancement": 2, "tax": 1, "disclosure": 1, "protection": 1, "default": 1, "holders": 2, "trustee": 2, "institutions": 1, "statements": 1, "reference": 1}


def _fmt(value: Decimal) -> str:
    return f"{value:,.0f}"


def _counter_guarantee_coverage(case: BondCaseSpec) -> str:
    """State both relevant denominators without implying that either is recoverable."""
    issue_coverage = case.counter_value / case.issue_amount * 100
    liability_coverage = case.counter_value / case.guarantee_cap * 100
    return (
        f"反担保折后价值占本期发行规模{issue_coverage:.1f}%，"
        f"占拟议保证责任上限{liability_coverage:.1f}%。"
    )


def _rating_version_note(case: BondCaseSpec) -> str | None:
    if case.issue_amount == case.rating_issue_amount:
        return None
    return "本评级报告载明的发行规模依据前版发行方案；最终条款以募集说明书及发行公告为准。上述版本差异须并列核验，不能自动采用任一数值。"


def _rating_financial_reconciliation_note(case: BondCaseSpec) -> str:
    if any(item.fact_key in {"revenue", "interest_bearing_debt"} for item in case.conflicts):
        return "评级报告与募集说明书在营业收入或有息负债上存在口径或版本差异，应保留全部来源断言并取得审计报表、明细台账及差异调节表核验。"
    return "评级分析对有息债务、权益工具和受限资金采用自身口径调整；相关调整应与发行人原始报表并列核验。"


def _prospectus_pages(case: BondCaseSpec, key_pages: dict[str, int] | None = None) -> list[dict]:
    """构造募集说明书的页序。

    ``key_pages`` 为上一遍实际渲染得到的章节起始页；为 ``None`` 时目录先写占位，
    正式渲染时再用真实页码重绘（跨页续表会让页数与登录页数不一致，不能靠估算）。
    """
    counts = FULL_COUNTS if case.full_length else MEDIUM_COUNTS
    # 目录每节一行。行尾必须是点线引导的页码——这是真实募集说明书的写法，
    # 也是 ``bond_sections._toc_like_indices`` 识别目录页的依据。写成「（第N页）」
    # 会让整页 17 个章节标题被当成真实章节锚点，所有章节都被定位到目录页。
    toc_lines = [
        f"{title}{'…' * 14}{key_pages[f'{key}_1'] if key_pages and f'{key}_1' in key_pages else index}"
        for index, (key, title) in enumerate(PROSPECTUS_SECTIONS, 1)
    ]
    pages = [
        {"key": "cover", "title": case.bond_name, "subtitle": "募集说明书（申报稿）", "body": [f"发行人：{case.company}", f"注册规模：{_fmt(case.registered_amount)}万元；本期发行规模：{_fmt(case.issue_amount)}万元", "拟在符合监管规定的证券交易场所面向专业投资者公开发行公司债券（模拟），具体发行和挂牌安排以最终发行文件为准", "拟由东辰融资担保有限公司（虚构）提供第三方连带责任保证", f"模板版本：{TEMPLATE_VERSION}"]},
        {"key": "statement", "title": "声明", "body": ["本申报稿由虚构案例数据生成，仅用于测试材料解析、事实核验和报告引用能力。", "发行人对申报稿披露信息承担相应责任；本材料不构成真实发行文件、投资建议或担保承诺。", "中国证监会及有关证券交易场所的审核或发行程序不代表对发行人偿债能力或债券价值作出判断。"]},
        {"key": "major", "title": "重大事项提示", "body": _major_matters(case)},
        {"key": "definitions", "title": "释义", "body": [f"发行人、公司：指{case.company}。", "本期债券：指本申报稿所述面向专业投资者公开发行公司债券。", "拟议保证：指尚待正式保证合同、内部授权和反担保登记文件确认的增信安排。", "评级报告：指虚构评级机构出具的拟发行版信用评级报告。"]},
        {"key": "toc", "title": "目录", "body": toc_lines},
    ]
    if case.full_length:
        pages.append({"key": "toc_2", "title": "目录（续）", "body": ["财务附表、评级指标、持有人会议规则、受托管理条款及备查文件均按本目录所列章节连续编号。"]})
    for key, title in PROSPECTUS_SECTIONS:
        for index in range(1, counts[key] + 1):
            pages.append({"key": f"{key}_{index}", "title": title, "subtitle": _page_topic(key, index), "body": _prospectus_body(case, key, index)})
    return pages


def _rating_pages(case: BondCaseSpec) -> list[dict]:
    target = 22 if case.full_length else 16
    titles = [
        "信用评级报告封面", "声明及跟踪评级安排", "评级结果与发行要素", "财务概况与评级口径",
        "评级观点、正面因素与关注事项", "评级模型与方法论", "发行人概况", "本期债券概况",
        "行业与经营环境", "主营业务与竞争力", "治理、关联方与管理", "财务分析：资产与资本结构",
        "财务分析：盈利和现金流", "债务结构与偿债能力", "拟议增信及评级提升逻辑", "评级结论与调级因素",
    ]
    if target > len(titles):
        titles[8:8] = ["行业政策与区域环境", "业务板块分析", "客户供应商及项目储备", "资产质量与应收款项", "流动性与融资渠道", "同业比较"]
    pages = []
    for index, title in enumerate(titles[:target], 1):
        key = "rating_cover" if index == 1 else "rating_summary" if index == 3 else "rating_financial" if index == 4 else "rating_enhancement" if "增信" in title else f"rating_{index}"
        pages.append({"key": key, "title": title, "body": _rating_body(case, title, index)})
    return pages


def _major_matters(case: BondCaseSpec) -> list[str]:
    latest = case.latest
    return [
        f"2026年本期发行规模为{_fmt(case.issue_amount)}万元，期限{case.term_years}年，采用每年付息、到期一次性偿还本金的方式。",
        f"截至2025年末，发行人有息负债为{_fmt(latest.interest_bearing_debt)}万元，未来12个月到期债务为{_fmt(case.debt_due_12m)}万元。",
        f"2025年第一大客户占比{case.top_customer}%，前五大客户占比{case.top5_customer}%，业务集中度变化可能影响回款稳定性。",
        "拟议保证和反担保安排尚待正式合同、授权、估值更新及权利登记材料核验，不应视为已经生效。",
    ]


def _page_topic(key: str, index: int) -> str:
    topics = {
        "risk": ("财务风险", "经营风险", "管理风险", "政策风险", "本期债券特有风险"),
        "terms": ("本期债券基本条款", "发行、登记与偿付安排", "增信及投资者适当性"),
        "proceeds": ("募集资金用途", "专户管理与支付控制", "历史募集资金使用"),
        "issuer": ("基本信息", "历史沿革", "股权结构", "治理机制", "主营业务", "客户结构", "供应商结构", "关联方", "内控制度", "发展规划"),
        "financial": (
            "财务数据基础及口径", "合并资产负债表", "主要利润项目表", "主要现金流量项目表",
            "有息负债明细", "货币资金与其他应收款明细", "应收账款账龄结构", "应收账款主要欠款方",
            "存货明细", "盈利能力分析", "现金流质量分析", "资本结构分析",
            "或有事项与关联交易", "偿债指标分析", "财务分析结论",
        ),
        "credit": ("信用评级", "银行授信", "债务履约", "资信变化"),
        "enhancement": ("拟议保证安排", "保证责任边界", "反担保概要", "增信事项待核验清单"),
        "tax": ("税项说明",), "disclosure": ("定期信息披露", "重大事项披露"),
        "protection": ("资信维持承诺", "救济措施"), "default": ("违约事件", "争议解决"),
        "holders": ("会议权限", "召集和表决", "决议落实"), "trustee": ("受托管理职责", "持续监测", "违约处置"),
        "institutions": ("中介机构", "联系方式"), "statements": ("发行人声明", "主承销商声明", "服务机构声明"),
        "reference": ("备查文件",),
    }
    values = topics[key]
    return values[min(index - 1, len(values) - 1)]


def _prospectus_body(case: BondCaseSpec, key: str, index: int) -> list[str]:
    f = case.latest
    standard = [
        f"发行人围绕{case.industry}开展经营，本节信息由结构化虚构案例数据生成，相关判断仍需结合原始底稿复核。",
        "本节披露内容用于说明发行交易和信用风险，不代表拟议保证人已经完成独立尽调或作出审批决定。",
    ]
    if key == "terms" and index == 1:
        return [f"债券名称：{case.bond_name}。", f"2026年债券注册规模：{_fmt(case.registered_amount)}万元。", f"2026年本期发行规模：{_fmt(case.issue_amount)}万元。", f"2026年债券期限：{case.term_years}年；票面利率：{case.coupon_rate}%。", "付息频率：每年付息一次；还本方式：到期一次性偿还本金。", "发行对象：符合相关规定的专业投资者；发行及挂牌安排以最终发行文件为准；主承销商：华衡证券有限责任公司（虚构）；债券受托管理人：华衡证券有限责任公司（虚构）。"]
    if key == "proceeds" and index == 1:
        return [f"募集资金金额：{_fmt(case.proceeds_amount)}万元。", f"募集资金用途：{case.proceeds_purpose}。", "募集资金应进入专项账户，付款申请应对应预算、合同、发票及审批记录；用途变更应履行约定程序并及时披露。"]
    if key == "issuer" and index == 1:
        return [f"发行人名称：{case.company}。", f"主营业务：{case.main_business}。", f"实际控制人：{case.controller}；实控人持股比例：{case.controller_share}%。", case.governance_note]
    if key == "issuer" and index in {5, 6, 7, 8}:
        return [f"2025年第一大客户占比：{case.top_customer}%；前五大客户占比：{case.top5_customer}%。", f"2025年第一大供应商占比：{case.top_supplier}%；前五大供应商占比：{case.top5_supplier}%。", f"2025年无合同关联方往来：{_fmt(case.related_unsupported)}万元。", case.qualitative_note or "发行人披露主要交易均执行合同、验收和付款审批流程，实际执行情况仍需底稿验证。"]
    # 财务章节的正文只写口径、会计政策与核验边界，不逐项复述表内数字；数字一律
    # 由同页的受控表格承载（见 ``_page_tables``）。
    if key == "financial" and index == 1:
        years = [item.year for item in case.financials]
        return [
            "发行人财务会计信息及主要财务指标",
            f"本申报稿财务信息涵盖{years[0]}年度至{years[-1]}年度，报表口径为合并口径，会计期间为公历年度，金额单位为万元。",
            "资产负债项目为时点数，以各年度12月31日为基准；利润与现金流量项目为期间数。",
            "资产负债表的递延所得税、金融工具公允价值和合并抵销等调整事项未在本申报稿逐项列示；"
            "相关底稿应结合审计报告附注复核，本材料不构成对会计政策适当性的结论。",
            "现金流量表中的投资活动与筹资活动按净额列示，其与期初、期末现金余额的勾稽关系已在表注中说明。",
            "本页及本章各表均由结构化虚构案例数据生成，用于测试表格解析与事实核验能力，"
            "未经审计报告原件复核，不得作为真实发行文件使用。",
        ]
    if key == "financial" and index == 2:
        return [
            "本表按合并口径列示各年度年末余额，最新年度列在最左。",
            "流动与非流动资产、流动与非流动负债均按分项加总，资产总计与负债和所有者权益总计相等；"
            "各年度均由系统在生成期校验，未通过校验的材料不会产出。",
            "受限资金为货币资金的其中项，不参与货币资金的加总；其余「其中」项同理。",
        ]
    if key == "financial" and index == 3:
        return [
            "本表按期间数列示，最新年度列在最左；信用减值损失以“-”号填列表示损失。",
            "利息费用与利息收入为财务费用的其中项，不参与财务费用的加总；期间费用为销售、管理、研发与财务费用之和。",
            "其他收益与投资收益合计用于衔接毛利与营业利润，其构成与可持续性应结合政府补助文件及被投资单位资料核验。",
        ]
    if key == "financial" and index == 4:
        return [
            "本表按期间数列示。期末现金及现金等价物余额＝期初余额＋经营、投资、筹资活动净额之和，并与资产负债表货币资金一致。",
            "经营活动现金流量净额为已审期间的实际数，其质量应结合销售收现比例、应收账款账龄与客户回款账期判断。",
            # 预测值与历史实际数主体相同但期间与口径都不同；本页必须把口径写清楚，
            # 否则（已实测）读者会把预测值误当成最近一年实际数，与财务表的实际数
            # 混为一谈——那不是数据冲突，是口径披露缺失。
            case.forecast_disclosure(),
            "债券本金集中于到期日偿付，发行人应提前落实偿债专户归集、备用授信和资产处置预案。",
        ]
    if key == "financial" and index == 5:
        return [
            "有息负债＝短期借款＋长期借款＋应付债券，与资产负债表口径一致；应付票据、应付账款等经营性负债不计入有息负债。",
            f"未来12个月到期债务为{_fmt(case.debt_due_12m)}万元，其覆盖情况见偿债指标页；"
            "到期债务明细台账、交叉违约条款与银行授信合同未随本核心材料包提供。",
            "有息负债分项在取得借款合同、债券募集文件与还款计划后方可逐笔核验。",
        ]
    if key == "financial" and index == 6:
        return [
            "货币资金按库存现金、银行存款与其他货币资金列示；受限资金为其中项，其规模不得超过货币资金合计。",
            "其他应收款中的关联方往来为未提供合同支持的部分，应按关联方清单、合同与资金流水逐笔核验。",
            "受限资金的解除条件、质押或冻结依据需取得银行函证与协议原件，本材料不构成对资金可动用性的结论。",
        ]
    if key == "financial" and index == 7:
        return [
            "账龄按“1年以内（含1年）”“1至2年”“2至3年”“3年以上”四档划分，各档余额合计等于应收账款账面余额。",
            "账面价值＝账面余额−坏账准备；坏账准备的计提政策与比例应与会计政策及审计报告附注一致。",
            "账龄划分以发票日期或合同约定付款日为基础，不同基础会导致账龄分布差异，核验时应确认所选基础。",
        ]
    if key == "financial" and index == 8:
        return [
            "主要欠款方按其期末余额列示，并标注是否为关联方；合计不超过应收账款账面余额。",
            "欠款方名称、关联关系与余额需与销售合同、发票、物流单据及回款流水交叉核验；"
            "关联方标识以发行人披露为准，不得仅凭名称推断。",
        ]
    if key == "financial" and index == 9:
        return [
            "存货按原材料、在产品、库存商品与发出商品列示，合计等于资产负债表存货科目。",
            "发出商品尚未满足收入确认条件，其后续结转与跌价准备计提情况需结合销售合同与盘点记录核验。",
        ]
    if key == "financial":
        # 10—15：盈利能力、现金流质量、资本结构、或有事项、偿债指标与分析结论。
        return standard + [
            f"本页围绕“{_page_topic(key, index)}”说明变动原因与偿债影响范围，表内数值以本章各表为准，正文不重复列示。",
            "涉及变化的判断应指向具体核验动作：取得原始底稿、台账、合同或函证，并保留未取得部分的材料边界。",
        ]
    if key == "credit" and index == 1:
        return [f"发行人主体信用等级：{case.issuer_rating}；评级展望：{case.rating_outlook}。", f"本期债券信用等级：{case.bond_rating}。", "上述评级为虚构评级机构意见，不代表拟议保证人的独立判断。"]
    if key == "enhancement" and index == 1:
        return ["保证人：东辰融资担保有限公司（虚构）。", "保证方式：拟提供不可撤销的连带责任保证。", f"保证责任范围：{case.guarantee_scope}。", "保证期间：拟自本期债券主债务履行期限届满之日起三年。", f"保证责任上限：{_fmt(case.guarantee_cap)}万元。"]
    if key == "enhancement" and index >= 2:
        return [f"反担保折后价值：{_fmt(case.counter_value)}万元。", _counter_guarantee_coverage(case), f"反担保顺位及权属状态：{case.counter_rights}。", "上述增信安排仅为申报阶段拟议方案，最终权利义务以正式保证合同、反担保合同、内部授权、估值报告和登记文件为准。"]
    if key == "protection":
        return ["投资者保护措施：发行人资信维持承诺、重大事项通知、负面事项救济和债券持有人会议机制。", "交叉违约条款：发行人或重要子公司发生约定金额以上债务违约且未在宽限期内纠正时，应通知受托管理人并按约定启动救济。"]
    if key == "risk" and index == 2 and case.qualitative_note:
        return [case.qualitative_note, "前述事项为发行人材料披露的待核验信息，不能替代合同原件、客户回函、资金流水和访谈证据。"] + standard
    if key == "reference":
        return ["备查文件包括最近三年审计报告、法律意见书、受托管理协议、持有人会议规则、拟议保证合同和反担保材料。", "本核心材料包未附上述原件，系统应将其列为补充核验材料，保留待核验边界，不得据此推断原件已核验。"]
    return standard + [f"就“{_page_topic(key, index)}”事项，发行人已在申报稿中进行概括披露；相关数据、合同效力和权利状态应由担保项目人员取得原始材料后交叉核验。", "如后续文件与本申报稿存在差异，应保留全部来源断言并提交人工复核，不得自动选择所谓正确值。"]


def _rating_body(case: BondCaseSpec, title: str, index: int) -> list[str]:
    f = case.latest
    rating_revenue = next((c.rating_value for c in case.conflicts if c.fact_key == "revenue"), f.revenue)
    rating_debt = next((c.rating_value for c in case.conflicts if c.fact_key == "interest_bearing_debt"), f.interest_bearing_debt)
    if index == 1:
        return [case.bond_name, "信用评级报告（拟发行版）", "中衡资信评估有限公司（虚构）", f"报告编号：ZHPJ-2026-{case.short.upper()}-01", TEMPLATE_VERSION]
    if index == 2:
        return ["本报告为虚构演示材料。评级结论仅作为信用风险分析参考，不构成投资建议、担保审批意见或任何交易承诺。", "评级机构将在债券存续期内根据约定进行跟踪评级；重大不利事项可能导致评级调整。"]
    if index == 3:
        body = [f"发行人及评级结果：{case.company}，主体信用等级：{case.issuer_rating}，评级展望：{case.rating_outlook}。", f"债项及评级结果：本期债券信用等级：{case.bond_rating}。", f"2026年评级口径发行规模：{_fmt(case.rating_issue_amount)}万元；债券期限：{case.term_years}年；固定利率，每年付息一次，到期一次性偿还本金。", f"募集资金用途：{case.proceeds_purpose}。"]
        if note := _rating_version_note(case):
            body.append(note)
        return body
    if index == 4:
        return [f"2025年评级报告营业收入：{_fmt(_d(rating_revenue))}万元。", f"2025年评级口径有息负债：{_fmt(_d(rating_debt))}万元。", f"2025年净利润：{_fmt(f.net_profit)}万元；经营活动现金流量净额：{_fmt(f.operating_cash_flow)}万元。", _rating_financial_reconciliation_note(case)]
    if "观点" in title:
        return [f"评级机构认为，{case.company}在{case.industry}领域具备一定经营基础。", f"关注事项包括：{case.governance_note}", f"评级下调触发因素：经营现金流持续弱化、再融资渠道明显收窄、治理缺陷扩大或拟议保证能力下降。", "评级机构意见不等同于担保机构独立判断。"]
    if "增信" in title:
        return ["拟由东辰融资担保有限公司（虚构）提供第三方连带责任保证。", f"拟议保证范围：{case.guarantee_scope}；拟议责任上限：{_fmt(case.guarantee_cap)}万元。", f"拟议反担保折后价值：{_fmt(case.counter_value)}万元；{_counter_guarantee_coverage(case)}", f"反担保权利状态：{case.counter_rights}。", f"在拟议保证合法有效且持续可执行的假设下，债项评级较主体评级由{case.issuer_rating}提升至{case.bond_rating}；该假设须以正式法律文件核验。"]
    if "结论" in title:
        return [f"中衡资信评估有限公司（虚构）评定{case.company}主体信用等级为{case.issuer_rating}，评级展望为{case.rating_outlook}，本期债券信用等级为{case.bond_rating}。", "评级结论基于拟发行条款和拟议增信假设，不构成担保机构审批结论。"]
    return [f"本页围绕“{title}”分析发行人的业务基础、财务表现、流动性和拟议增信安排。", f"2025年发行人资产总额{_fmt(f.total_assets)}万元，负债总额{_fmt(f.total_liabilities)}万元，所有者权益{_fmt(f.equity)}万元。", f"第一大客户占比{case.top_customer}%，前五大客户占比{case.top5_customer}%；相关集中度变化是持续跟踪事项。", "评级分析所用数据来自发行人披露，审计原件、流水及合同证据不在本核心材料包内。"]


def _annual_desc(case: BondCaseSpec) -> list[AnnualStatements]:
    """按参考材料的年度列顺序（最新年度在最左）排列。"""
    return list(reversed(case.statements))


def _row(label: str, values: tuple[str, ...], kind: str = "normal", indent: bool = False) -> tuple[TableCell, ...]:
    return (
        TableCell(("　　" + label) if indent else label, kind, "left"),
        *[TableCell(text, kind, "right") for text in values],
    )


def _group(label: str, width: int) -> tuple[TableCell, ...]:
    """分组行：只有首格有文字，其余留白（与参考材料一致）。"""
    return (TableCell(label, "group", "left"), *[TableCell("", "group", "right") for _ in range(width)])


def _balance_sheet_table(case: BondCaseSpec) -> FinancialTable:
    """三年合并资产负债表。约 38 行，必然跨页——这正是续表头重复的验证场景。"""
    years = _annual_desc(case)
    sheets = [item.balance_sheet for item in years]
    numeric = lambda getter: tuple(cell_value(getter(item)) for item in sheets)  # noqa: E731
    return FinancialTable(
        table_id=f"{case.short}-prospectus-balance-sheet", table_key="consolidated_balance_sheet",
        caption="合并资产负债表",
        unit="万元",
        columns=("项目", *[f"{item.year} 年末" for item in years]),
        rows=(
            _group("资产：", len(years)),
            _row("货币资金", numeric(lambda s: s.cash)),
            _row("其中：受限资金", numeric(lambda s: s.restricted_cash), indent=True),
            _row("应收账款", numeric(lambda s: s.accounts_receivable)),
            _row("其他应收款", numeric(lambda s: s.other_receivables)),
            _row("存货", numeric(lambda s: s.inventory)),
            _row("预付款项及其他流动资产", numeric(lambda s: s.prepayments_and_other_current)),
            _row("流动资产合计", numeric(lambda s: s.current_assets), "subtotal"),
            _row("固定资产", numeric(lambda s: s.fixed_assets)),
            _row("在建工程", numeric(lambda s: s.construction_in_progress)),
            _row("无形资产", numeric(lambda s: s.intangible_assets)),
            _row("长期待摊费用", numeric(lambda s: s.long_term_deferred_expense)),
            _row("其他非流动资产", numeric(lambda s: s.other_non_current_assets)),
            _row("非流动资产合计", numeric(lambda s: s.non_current_assets), "subtotal"),
            _row("资产总计", numeric(lambda s: s.total_assets), "subtotal"),
            _group("负债：", len(years)),
            _row("短期借款", numeric(lambda s: s.short_term_debt)),
            _row("应付票据", numeric(lambda s: s.notes_payable)),
            _row("应付账款", numeric(lambda s: s.accounts_payable)),
            _row("合同负债", numeric(lambda s: s.contract_liabilities)),
            _row("其他流动负债", numeric(lambda s: s.other_current_liabilities)),
            _row("流动负债合计", numeric(lambda s: s.current_liabilities), "subtotal"),
            _row("长期借款", numeric(lambda s: s.long_term_debt)),
            _row("应付债券", numeric(lambda s: s.bonds_payable)),
            _row("其他非流动负债", numeric(lambda s: s.other_non_current_liabilities)),
            _row("非流动负债合计", numeric(lambda s: s.non_current_liabilities), "subtotal"),
            _row("负债合计", numeric(lambda s: s.total_liabilities), "subtotal"),
            _group("所有者权益：", len(years)),
            _row("实收资本", numeric(lambda s: s.paid_in_capital)),
            _row("资本公积", numeric(lambda s: s.capital_reserve)),
            _row("盈余公积", numeric(lambda s: s.surplus_reserve)),
            _row("未分配利润", numeric(lambda s: s.retained_earnings)),
            _row("所有者权益合计", numeric(lambda s: s.equity), "subtotal"),
            _row("负债和所有者权益总计", numeric(lambda s: s.total_liabilities + s.equity), "subtotal"),
        ),
        notes=("注：本表为虚构案例数据，各年度均满足「资产总计＝负债合计＋所有者权益合计」。",),
    )


def _income_statement_table(case: BondCaseSpec) -> FinancialTable:
    years = _annual_desc(case)
    incomes = [item.income_statement for item in years]
    numeric = lambda getter: tuple(cell_value(getter(item)) for item in incomes)  # noqa: E731
    return FinancialTable(
        table_id=f"{case.short}-prospectus-income-statement", table_key="income_statement",
        caption="主要利润项目表",
        unit="万元",
        columns=("项目", *[f"{item.year} 年度" for item in years]),
        rows=(
            _row("一、营业收入", numeric(lambda s: s.revenue), "subtotal"),
            _row("减：营业成本", numeric(lambda s: s.cost_of_goods_sold)),
            _row("税金及附加", numeric(lambda s: s.taxes_and_surcharges), indent=True),
            _row("销售费用", numeric(lambda s: s.selling_expense), indent=True),
            _row("管理费用", numeric(lambda s: s.administrative_expense), indent=True),
            _row("研发费用", numeric(lambda s: s.research_expense), indent=True),
            _row("财务费用", numeric(lambda s: s.finance_expense), indent=True),
            _row("其中：利息费用", numeric(lambda s: s.interest_expense), indent=True),
            _row("利息收入", numeric(lambda s: s.interest_income), indent=True),
            _row("加：其他收益", numeric(lambda s: s.other_income)),
            _row("投资收益", numeric(lambda s: s.investment_income)),
            _row('信用减值损失（损失以"-"号填列）', numeric(lambda s: s.credit_impairment_loss)),
            _row("二、营业利润", numeric(lambda s: s.operating_profit), "subtotal"),
            _row("加：营业外收入", numeric(lambda s: s.non_operating_income)),
            _row("减：营业外支出", numeric(lambda s: s.non_operating_expense)),
            _row("三、利润总额", numeric(lambda s: s.total_profit), "subtotal"),
            _row("减：所得税费用", numeric(lambda s: s.income_tax_expense)),
            _row("四、净利润", numeric(lambda s: s.net_profit), "subtotal"),
        ),
        notes=("注：本表为虚构案例数据；利息费用与利息收入为财务费用的其中项，不参与加总。",),
    )


def _cash_flow_table(case: BondCaseSpec) -> FinancialTable:
    years = _annual_desc(case)
    flows = [item.cash_flow_statement for item in years]
    numeric = lambda getter: tuple(cell_value(getter(item)) for item in flows)  # noqa: E731
    return FinancialTable(
        table_id=f"{case.short}-prospectus-cash-flow", table_key="cash_flow_statement",
        caption="主要现金流量项目表",
        unit="万元",
        columns=("项目", *[f"{item.year} 年度" for item in years]),
        rows=(
            _group("一、经营活动产生的现金流量：", len(years)),
            _row("销售商品、提供劳务收到的现金", numeric(lambda s: s.cash_received_from_sales)),
            _row("收到的税费返还", numeric(lambda s: s.tax_refund_received)),
            _row("收到其他与经营活动有关的现金", numeric(lambda s: s.other_operating_inflow)),
            _row("经营活动现金流入小计", numeric(lambda s: s.operating_inflow), "subtotal"),
            _row("购买商品、接受劳务支付的现金", numeric(lambda s: s.goods_and_services_paid)),
            _row("支付给职工以及为职工支付的现金", numeric(lambda s: s.employee_paid)),
            _row("支付的各项税费", numeric(lambda s: s.taxes_paid)),
            _row("支付其他与经营活动有关的现金", numeric(lambda s: s.other_operating_outflow)),
            _row("经营活动现金流出小计", numeric(lambda s: s.operating_outflow), "subtotal"),
            _row("经营活动产生的现金流量净额", numeric(lambda s: s.operating_cash_flow), "subtotal"),
            _group("二、投资活动产生的现金流量：", len(years)),
            _row("投资活动现金流入小计", numeric(lambda s: s.investing_inflow)),
            _row("投资活动现金流出小计", numeric(lambda s: s.investing_outflow)),
            _row("投资活动产生的现金流量净额", numeric(lambda s: s.investing_cash_flow), "subtotal"),
            _group("三、筹资活动产生的现金流量：", len(years)),
            _row("筹资活动现金流入小计", numeric(lambda s: s.financing_inflow)),
            _row("筹资活动现金流出小计", numeric(lambda s: s.financing_outflow)),
            _row("其中：偿还债务支付的现金", numeric(lambda s: s.debt_repayment), indent=True),
            _row("分配股利、利润或偿付利息支付的现金", numeric(lambda s: s.interest_and_dividend_paid), indent=True),
            _row("筹资活动产生的现金流量净额", numeric(lambda s: s.financing_cash_flow), "subtotal"),
            _row("四、现金及现金等价物净增加额", numeric(lambda s: s.net_increase), "subtotal"),
            _row("加：期初现金及现金等价物余额", numeric(lambda s: s.opening_cash)),
            _row("五、期末现金及现金等价物余额", numeric(lambda s: s.closing_cash), "subtotal"),
        ),
        notes=("注：期末余额＝期初余额＋经营活动、投资活动与筹资活动净额之和，与资产负债表货币资金一致。",),
    )


def _detail_table(
    case: BondCaseSpec, *, kind: str, caption: str, rows: tuple[tuple[str, tuple[Decimal, ...]], ...],
    total: tuple[Decimal, ...] = (), trailing: tuple[tuple[str, tuple[Decimal, ...]], ...] = (),
    unit: str = "万元", notes: tuple[str, ...] = (),
) -> FinancialTable:
    """科目明细表：项目＋三年金额，合计行加粗，其中项排在合计之后。

    不设「占比」列——三年各配一列占比会把表头拉成 7 列、年度列只剩约 54pt，
    长科目名与千分位金额都放不下，反而制造丢字。占比另有指标层承担。
    """
    years = _annual_desc(case)
    body = [_row(label, tuple(cell_value(value) for value in amounts)) for label, amounts in rows]
    if total:
        body.append(_row("合计", tuple(cell_value(value) for value in total), "subtotal"))
    body.extend(_row(label, tuple(cell_value(value) for value in amounts), indent=True) for label, amounts in trailing)
    return FinancialTable(
        table_id=f"{case.short}-prospectus-{kind}", table_key="interest_bearing_debt_detail" if kind == "interest-bearing-debt" else kind.replace("-", "_"),
        caption=caption,
        unit=unit,
        columns=("项目", *[f"{item.year} 年末" for item in years]),
        rows=tuple(body),
        notes=notes,
    )


def _receivable_aging_table(case: BondCaseSpec) -> FinancialTable:
    years = _annual_desc(case)
    details = [item.receivables for item in years]
    labels = [label for label, _ in details[0].aging.rows()]
    rows = []
    for index, label in enumerate(labels):
        amounts = tuple(item.aging.rows()[index][1] for item in details)
        rows.append(_row(label, tuple(cell_value(value) for value in amounts)))
    rows.append(_row("合计", tuple(cell_value(item.gross_balance) for item in details), "subtotal"))
    rows.append(_row("坏账准备", tuple(cell_value(item.bad_debt_provision) for item in details)))
    rows.append(_row("账面价值", tuple(cell_value(item.carrying_value) for item in details), "subtotal"))
    # 逾期应收账款必须真的**出现在材料里**：事实层宣称它有值，材料却不披露，
    # 等于让事实引用一页不含该值的材料（实测：运行后该科目被抽取流程清掉、
    # 报告表格里整行消失，而事实声称它存在）。
    rows.append(_row("其中：逾期应收账款", tuple(cell_value(item.overdue_balance) for item in details), indent=True))
    return FinancialTable(
        table_id=f"{case.short}-prospectus-receivable-aging", table_key="receivables_aging",
        caption="应收账款账龄结构",
        unit="万元",
        columns=("账龄", *[f"{item.year} 年末" for item in years]),
        rows=tuple(rows),
        notes=("注：账龄余额合计等于应收账款账面余额；账面价值＝账面余额−坏账准备。",),
    )


def _receivable_debtor_table(case: BondCaseSpec) -> FinancialTable:
    latest = case.statements[-1].receivables
    rows = [
        (TableCell(item.name, "normal", "left"),
         TableCell("是" if item.related_party else "否", "normal", "center"),
         TableCell(cell_value(item.balance), "normal", "right"),
         TableCell(item.reason, "normal", "left"))
        for item in latest.top_debtors
    ]
    rows.append((
        TableCell("合计", "subtotal", "left"),
        TableCell("-", "subtotal", "center"),
        TableCell(cell_value(latest.top_debtor_total), "subtotal", "right"),
        TableCell("", "subtotal", "left"),
    ))
    return FinancialTable(
        table_id=f"{case.short}-prospectus-receivable-debtors", table_key="receivables_debtors",
        caption=f"截至{latest_year(case)}年末主要应收账款欠款方",
        unit="万元",
        columns=("债务人名称", "是否为关联方", "期末余额", "未收回原因"),
        rows=tuple(rows),
        notes=("注：合计不超过应收账款账面余额；关联方标识以发行人披露为准，需与关联方清单交叉核验。",),
    )


def latest_year(case: BondCaseSpec) -> int:
    return case.statements[-1].year


def _page_tables(case: BondCaseSpec, role: str, key: str) -> tuple[FinancialTable, ...]:
    """按页给出该页应当渲染的表格。非表格页返回空元组。"""
    if role == "prospectus":
        if key == "terms_2":
            return (FinancialTable(
                table_id=f"{case.short}-prospectus-terms", table_key="offering_terms",
                caption="本期债券发行要素",
                # 本表混合万元、年、%，单位行如实写全——它同时是解析器定位表名的锚点。
                unit="万元、%、年",
                columns=("发行要素", "申报稿披露"),
                rows=tuple(_row(label, (value,)) for label, value in (
                    ("本期金额", f"{_fmt(case.issue_amount)}万元"),
                    ("存续年限", f"{case.term_years}年"),
                    ("固定票息", f"{case.coupon_rate}%"),
                    ("偿付方式", "每年付息、到期还本"),
                )),
            ),)
        if key == "financial_2":
            return (_balance_sheet_table(case),)
        if key == "financial_3":
            return (_income_statement_table(case),)
        if key == "financial_4":
            return (_cash_flow_table(case),)
        if key == "financial_5":
            return (_detail_table(
                case, kind="interest-bearing-debt", caption="有息负债明细",
                rows=tuple(
                    (label, tuple(getattr(item.balance_sheet, field) for item in _annual_desc(case)))
                    for label, field in (("短期借款", "short_term_debt"), ("长期借款", "long_term_debt"), ("应付债券", "bonds_payable"))
                ),
                total=tuple(item.balance_sheet.interest_bearing_debt for item in _annual_desc(case)),
                notes=("注：有息负债＝短期借款＋长期借款＋应付债券，与资产负债表口径一致。",),
            ),)
        if key == "financial_6":
            years = _annual_desc(case)
            return (_detail_table(
                case, kind="cash-detail", caption="货币资金明细",
                rows=tuple(
                    (name, tuple(_find_amount(item.account_details.cash_items, name) for item in years))
                    for name in _union_names(years, "cash_items")
                ),
                total=tuple(item.account_details.cash_total for item in years),
                trailing=(("其中：受限资金", tuple(item.account_details.cash_restricted for item in years)),),
            ), _detail_table(
                case, kind="other-receivable-detail", caption="其他应收款明细",
                rows=tuple(
                    (name, tuple(_find_amount(item.account_details.other_receivable_items, name) for item in years))
                    for name in _union_names(years, "other_receivable_items")
                ),
                total=tuple(item.account_details.other_receivable_total for item in years),
            ))
        if key == "financial_7":
            return (_receivable_aging_table(case),)
        if key == "financial_8":
            return (_receivable_debtor_table(case),)
        if key == "financial_9":
            years = _annual_desc(case)
            return (_detail_table(
                case, kind="inventory-detail", caption="存货明细",
                rows=tuple(
                    (name, tuple(_find_amount(item.account_details.inventory_items, name) for item in years))
                    for name in _union_names(years, "inventory_items")
                ),
                total=tuple(item.account_details.inventory_total for item in years),
            ),)
    if role == "rating" and key == "rating_financial":
        return (_rating_financial_table(case),)
    return ()


def _find_amount(items: tuple, name: str) -> Decimal:
    for row in items:
        if row.name == name:
            return row.amount
    return Decimal("0")


def _union_names(years: list[AnnualStatements], attribute: str) -> list[str]:
    names: list[str] = []
    for item in years:
        for row in getattr(item.account_details, attribute):
            if row.name not in names:
                names.append(row.name)
    return names


def _rating_financial_table(case: BondCaseSpec) -> FinancialTable:
    """评级文件只展示采用的主要财务指标与口径调整，不复制三张报表。"""
    years = _annual_desc(case)
    revenue_rating = _d(next((c.rating_value for c in case.conflicts if c.fact_key == "revenue"), case.latest.revenue))
    debt_rating = _d(next((c.rating_value for c in case.conflicts if c.fact_key == "interest_bearing_debt"), case.latest.interest_bearing_debt))
    columns = ("项目", *[f"{item.year} 年度" for item in years])
    latest_index = 0  # 年度列已倒序，最新年度在最左
    def with_override(values: list[Decimal], override: Decimal) -> list[Decimal]:
        output = list(values)
        output[latest_index] = override
        return output
    rows = (
        _row("营业收入", tuple(cell_value(value) for value in with_override([item.income_statement.revenue for item in years], revenue_rating))),
        _row("净利润", tuple(cell_value(item.income_statement.net_profit) for item in years)),
        _row("经营活动现金流量净额", tuple(cell_value(item.cash_flow_statement.operating_cash_flow) for item in years)),
        _row("有息负债", tuple(cell_value(value) for value in with_override([item.balance_sheet.interest_bearing_debt for item in years], debt_rating))),
    )
    return FinancialTable(
        table_id=f"{case.short}-rating-financial", table_key="rating_financial_summary",
        caption="评级采用的主要财务指标",
        unit="万元",
        columns=columns,
        rows=rows,
        notes=(
            "注：评级机构对营业收入与有息负债采用自身口径调整，上表最新年度列为评级口径值，"
            "与发行人财务表所载数值并列展示，不得自动择一采信。",
        ),
    )


REVIEW_HINT = "审阅提示：本页为结构化虚构披露。引用本页信息时应保留文件名称、页码和原文片段；未附底稿的事项应标记为材料不足。"


def _page_decorator(case: BondCaseSpec, accent: tuple[float, float, float]):
    def decorate(page: fitz.Page, page_no: int) -> None:
        page.draw_rect(fitz.Rect(0, 0, 595, 18), color=accent, fill=accent)
        page.draw_line(fitz.Point(46, 54), fitz.Point(549, 54), color=accent, width=1.2)
        page.insert_text((46, 42), case.company, fontsize=8.5, fontname="china-s", color=(0.25, 0.25, 0.25))
        page.insert_text((380, 42), FICTION_NOTICE, fontsize=8.5, fontname="china-s", color=(0.55, 0.18, 0.16))
        page.draw_line(fitz.Point(46, 802), fitz.Point(549, 802), color=(0.75, 0.75, 0.75), width=0.5)
        page.insert_textbox(
            fitz.Rect(46, 810, 549, 830), f"{TEMPLATE_VERSION}  |  {FICTION_NOTICE}  |  {page_no}",
            fontsize=8, fontname="china-s", color=(0.45, 0.45, 0.45), align=1,
        )
    return decorate


def _draw_review_hint(writer: PdfWriter) -> None:
    writer.ensure_space(82)
    assert writer.page is not None
    writer.page.draw_rect(fitz.Rect(52, writer.y, 543, writer.y + 74), color=(0.78, 0.80, 0.79), fill=(0.96, 0.97, 0.96), width=0.6)
    writer.text_block(
        fitz.Rect(62, writer.y + 12, 533, writer.y + 64), REVIEW_HINT, size=9.2,
    )
    writer.y += 82


def _draw_pdf(
    path: Path | None, case: BondCaseSpec, role: Literal["prospectus", "rating"], pages: list[dict],
) -> tuple[dict[str, int], int, list[RenderedTable]]:
    """渲染一份材料。

    ``path`` 为 ``None`` 时只做一次干跑以取得各章节的起始页码（目录需要真实页码），
    不落盘。返回值第三项是渲染器记录的表格几何——它是表格金标的独立基准，
    与解析器的抽取结果互不依赖，两者才能互相校验。
    """
    doc = fitz.open()
    accent = (0.10, 0.28, 0.25) if role == "prospectus" else (0.38, 0.14, 0.12)
    writer = PdfWriter(doc, decorate=_page_decorator(case, accent), accent=accent)
    key_pages: dict[str, int] = {}
    rendered: list[RenderedTable] = []
    for index, item in enumerate(pages, 1):
        writer.start_section(item["title"], item.get("subtitle", ""), cover=index == 1)
        key_pages[item["key"]] = writer.page_number
        for table in _page_tables(case, role, item["key"]):
            rendered.append(render_table(writer, table))
        for paragraph in item.get("body", []):
            writer.paragraph(paragraph)
        _draw_review_hint(writer)
    page_count = writer.page_number
    doc.set_metadata({
        "title": pages[0]["title"], "author": "研担虚构案例生成器",
        "subject": FICTION_NOTICE, "keywords": f"{TEMPLATE_VERSION};fictional",
    })
    if path is not None:
        doc.save(path, garbage=4, deflate=True)
    doc.close()
    return key_pages, page_count, rendered


def _render_document(
    path: Path, case: BondCaseSpec, role: Literal["prospectus", "rating"], build_pages,
) -> tuple[dict[str, int], int, list[RenderedTable]]:
    """两遍渲染：先干跑取得真实页码，再带目录页码正式渲染。

    跨页续表会让章节的页数与登录页数不一致，目录必须用实际渲染出来的页码。
    """
    key_pages, _, _ = _draw_pdf(None, case, role, build_pages(None))
    return _draw_pdf(path, case, role, build_pages(key_pages))


def generate_case_materials(root: Path, case: BondCaseSpec) -> list[dict]:
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "case-manifest.json"
    reusable: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            # 复用条件是「这份材料由**当前**渲染器产出」，不能只看版本号与 spec 哈希：
            # 渲染器或解析器规则变了，旧 PDF 会在版本号与 spec 都对得上的情况下被一直复用
            # （实测：第 13 页发行要素表的单位行为空，表名与单位永远抽不出来）。
            # 判据与 ``_seed_is_current`` 对齐——两个入口用两套判据就会一边判"要重播"、
            # 另一边又"复用旧文件"，结果什么都没重新生成。
            if (
                manifest.get("template_version") == TEMPLATE_VERSION
                and manifest.get("spec_sha256") == _spec_sha256(case)
                and manifest.get("tables")
                and (manifest.get("table_gold") or {}).get("gold_path") == "gold/financial_tables.json"
                and manifest.get("declared_tables_sha256") == _declared_tables_sha256(case)
            ):
                reusable = {item["role"]: item for item in manifest.get("documents", [])}
        except (OSError, ValueError, TypeError):
            reusable = {}
    output = []
    for role, filename, kind, build_pages in (
        ("prospectus", PROSPECTUS_NAME, "募集说明书", _prospectus_pages),
        ("rating", RATING_NAME, "信用评级报告", lambda item, pages=None: _rating_pages(item)),
    ):
        path = root / filename
        manifest_item = reusable.get(role, {})
        rendered: list[RenderedTable] = []
        if path.exists() and manifest_item.get("filename") == filename and manifest_item.get("key_pages"):
            parsed = parse_document(path)
            key_pages = {key: int(value) for key, value in manifest_item["key_pages"].items()}
            if not _materials_match_declared(case, role, build_pages(case, key_pages), parsed):
                # 既有 PDF 与当前渲染器的产出对不上 → 重新渲染。
                #
                # 不能只看 manifest 的版本号与 spec 哈希：manifest 里的 ``tables`` 是
                # "当前渲染器**会**产出什么"，不是 "PDF 里**实际**有什么"。实测踩过——
                # 第 13 页发行要素表的单位行是空的（旧渲染器产物），而 manifest 看起来
                # 一切正常，旧 PDF 于是一直被复用，那张表的表名与单位永远抽不出来。
                # 只有把 PDF **解析回来看**才能自证。
                key_pages, page_count, rendered = _render_document(
                    path, case, role, lambda pages: build_pages(case, pages),
                )
                parsed = parse_document(path)
        else:
            # 跨页续表让章节页数不等于登录页数，所以先渲染一遍取得真实页码，
            # 再带着真实页码渲染目录。
            key_pages, page_count, rendered = _render_document(path, case, role, lambda pages: build_pages(case, pages))
            parsed = parse_document(path)
        if parsed.warning or parsed.page_count < max(key_pages.values(), default=0):
            raise ValueError(f"{case.id} {filename} 渲染后解析不完整：{parsed.warning or parsed.page_count}")
        if not rendered:
            # 复用已生成材料时没有渲染几何，表格金标改由解析结果之外的声明结构承载；
            # 此处补一次干跑，保证金标随材料一同更新，而不是静默退化成空集。
            _, _, rendered = _draw_pdf(None, case, role, build_pages(case, key_pages))
        output.append({
            "id": f"bond-{case.short}-{role}", "project_id": case.id, "filename": filename, "kind": kind,
            "path": path, "role": role,
            "document_role": "prospectus" if role == "prospectus" else "rating_report",
            "page_count": parsed.page_count, "parsed": parsed, "key_pages": key_pages,
            "rendered_tables": rendered,
        })
    return output


def _declared_tables_sha256(case: BondCaseSpec) -> str:
    """当前渲染器**应当**产出哪些表（表名 + 单位 + 列）的指纹。

    渲染器改了但 manifest 只看版本号与 spec 哈希时，陈旧 PDF 会被一直复用
    （实测：发行要素表的单位行为空，表名与单位永远抽不出来）。把声明形状写进
    manifest 并在启动时比对，是**不解析 PDF** 就能发现渲染器变更的廉价判据。
    """
    shapes = [
        [table.caption, table.unit, list(table.columns)]
        for role, pages in (("prospectus", _prospectus_pages(case)), ("rating", _rating_pages(case)))
        for item in pages
        for table in _page_tables(case, role, item["key"])
    ]
    payload = json.dumps(sorted(shapes, key=lambda item: (item[0], item[1])), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _declared_table_shapes(case: BondCaseSpec, role: str, pages: list[dict]) -> set[tuple[str, str]]:
    """当前渲染器**应当**产出哪些表（表名 + 单位），不需要真的渲染。"""
    shapes: set[tuple[str, str]] = set()
    for item in pages:
        for table in _page_tables(case, role, item["key"]):
            shapes.add((table.caption, table.unit))
    return shapes


def _materials_match_declared(case: BondCaseSpec, role: str, pages: list[dict], parsed) -> bool:
    """既有 PDF 是否与当前渲染器的声明一致。

    比对表名与单位：单位行缺失是渲染器变更最直接的痕迹（表名锚定规则要求
    「单位：行有内容」，单位丢了表名也就丢了）。
    """
    try:
        actual = {(table.caption, table.unit) for table in parsed.tables}
    except AttributeError:
        return False
    return actual == _declared_table_shapes(case, role, pages)


def _source_id(case: BondCaseSpec, role: str, page: int) -> str:
    return f"bond-{case.short}-{role}-p{page:03d}"


def _spec_sha256(case: BondCaseSpec) -> str:
    payload = json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _backfill_document_tables(case: BondCaseSpec, root: Path, document_rows) -> None:
    """给既有材料补上表格落库（不重新渲染、不改动材料文件）。"""
    filenames = {str(item["id"]): str(item["filename"]) for item in document_rows}
    with connect() as conn:
        for document_id, filename in filenames.items():
            path = root / filename
            if not path.is_file():
                continue
            try:
                parsed = parse_document(path)
            except (OSError, ValueError):
                continue
            if parsed.tables:
                persist_tables(conn, case.id, document_id, parsed.tables)


def _seed_is_current(case: BondCaseSpec, root: Path) -> bool:
    manifest_path = root / "case-manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if manifest.get("template_version") != TEMPLATE_VERSION or manifest.get("spec_sha256") != _spec_sha256(case):
        return False
    # 只看版本号与 spec 哈希是不够的：manifest 记的是「按当时的模板生成的」，
    # 渲染器或解析器的规则变了它察觉不到。实测踩过——`.data/demo_materials/` 里
    # 仍是旧渲染器产出的 PDF（发行要素表的单位行是空的），而 manifest 因版本号与
    # spec 都对得上而被判为「最新」，于是旧 PDF 一直被沿用，那张表的表名与单位
    # 永远抽不出来、只能标待人工确认。
    # 表格清单与金标路径是「这份材料由当前渲染器生成」的可验证标记。
    if not manifest.get("tables"):
        return False
    if (manifest.get("table_gold") or {}).get("gold_path") != "gold/financial_tables.json":
        return False
    if manifest.get("declared_tables_sha256") != _declared_tables_sha256(case):
        return False
    documents = manifest.get("documents", [])
    if len(documents) != 2:
        return False
    for item in documents:
        path = root / item.get("filename", "")
        if not path.is_file() or sha256(path) != item.get("sha256"):
            return False
    with connect() as conn:
        project = conn.execute("SELECT id FROM projects WHERE id=? AND project_type='bond_guarantee'", (case.id,)).fetchone()
        document_rows = conn.execute("SELECT filename,page_count,sha256,document_role FROM documents WHERE project_id=?", (case.id,)).fetchall()
        source_count = conn.execute("SELECT COUNT(*) count FROM sources WHERE project_id=?", (case.id,)).fetchone()["count"]
        fact_keys = {
            item["fact_key"]
            for item in conn.execute("SELECT DISTINCT fact_key FROM facts WHERE project_id=?", (case.id,)).fetchall()
        }
    expected = {
        (item["filename"], item["pages"], item["sha256"], "prospectus" if item["role"] == "prospectus" else "rating_report")
        for item in documents
    }
    actual = {(item["filename"], item["page_count"], item["sha256"], item["document_role"]) for item in document_rows}
    # A prior interrupted extraction can remove all facts while leaving the
    # source documents intact.  Do not call case_facts here: it requires the
    # generated page map, which this fast idempotency check deliberately does
    # not load.
    return bool(
        project
        and actual == expected
        and source_count == sum(item["pages"] for item in documents)
        and {"bond_term_years", "issuer_rating", "bond_rating"}.issubset(fact_keys)
    )


def _delete_orphan_run_state(project_id: str) -> None:
    with connect() as conn:
        for table in ("llm_attempts", "run_nodes", "run_artifacts", "policy_snapshots"):
            conn.execute(
                f"DELETE FROM {table} WHERE project_id=? AND run_id NOT IN (SELECT id FROM runs WHERE project_id=?)",
                (project_id, project_id),
            )


def _ensure_gold_annotations(case: BondCaseSpec, materials_factory) -> None:
    """补齐缺失的金标注解。

    ``materials_factory`` 是惰性工厂：已经齐全时直接返回，不会去生成材料——
    表格金标需要渲染几何，若在启动快速路径上无条件生成，每个案例每次启动都要
    多跑两遍渲染。
    """
    required = {"fact", "warning", "citation", "tie_out", "table", "table_cell", "table_note"}
    if case.conflicts:
        required.add("conflict")
    with connect() as conn:
        present = {
            item["annotation_type"]
            for item in conn.execute(
                "SELECT DISTINCT annotation_type FROM gold_annotations WHERE project_id=? AND version=?",
                (case.id, TEMPLATE_VERSION),
            ).fetchall()
        }
        if required <= present:
            return
        facts = [dict(item) for item in conn.execute("SELECT * FROM facts WHERE project_id=?", (case.id,)).fetchall()]
    for fact in facts:
        fact["source_ids"] = json.loads(fact.get("source_ids") or "[]")
    if facts:
        _seed_gold(case, facts, case_table_gold(case, materials_factory()))


def _fact(case: BondCaseSpec, key: str, period: str, value: Decimal | None, unit: str, text: str, role: str, page_key: str, page_maps: dict[str, dict[str, int]]) -> dict:
    definition = FACT_REGISTRY[key]
    page = page_maps[role][page_key]
    return {
        "id": f"bond-fact-{case.short}-{hashlib.sha256(f'{key}|{period}|{role}|{value}|{text}'.encode()).hexdigest()[:16]}",
        "fact_key": key, "category": definition.category, "name": definition.label, "period": period,
        "value": float(value) if value is not None else None, "unit": unit, "text_value": text,
        "raw_value": str(value) if value is not None else text, "source_ids": [_source_id(case, role, page)],
        "confidence": 0.99, "extraction_method": "seed", "review_status": "unreviewed",
    }


def case_facts(case: BondCaseSpec, materials: list[dict]) -> list[dict]:
    page_maps = {item["role"]: item["key_pages"] for item in materials}
    f = case.latest
    items = [
        _fact(case, "bond_name", "2026", None, "", case.bond_name, "prospectus", "terms_1", page_maps),
        _fact(case, "registered_issue_amount", "2026", case.registered_amount, "万元", "", "prospectus", "terms_1", page_maps),
        _fact(case, "bond_issue_amount", "2026", case.issue_amount, "万元", "", "prospectus", "terms_1", page_maps),
        _fact(case, "bond_term_years", "2026", _d(case.term_years), "年", "", "prospectus", "terms_1", page_maps),
        _fact(case, "coupon_rate", "2026", case.coupon_rate, "%", "", "prospectus", "terms_1", page_maps),
        _fact(case, "interest_frequency", "2026", None, "", "每年付息一次", "prospectus", "terms_1", page_maps),
        _fact(case, "principal_repayment", "2026", None, "", "到期一次性偿还本金", "prospectus", "terms_1", page_maps),
        _fact(case, "underwriter", "2026", None, "", "华衡证券有限责任公司（虚构）", "prospectus", "terms_1", page_maps),
        _fact(case, "trustee", "2026", None, "", "华衡证券有限责任公司（虚构）", "prospectus", "terms_1", page_maps),
        _fact(case, "investor_scope", "2026", None, "", "符合相关规定的专业投资者", "prospectus", "terms_1", page_maps),
        _fact(case, "issuer_rating", "2026", None, "", case.issuer_rating, "rating", "rating_summary", page_maps),
        _fact(case, "bond_rating", "2026", None, "", case.bond_rating, "rating", "rating_summary", page_maps),
        _fact(case, "rating_outlook", "2026", None, "", case.rating_outlook, "rating", "rating_summary", page_maps),
        _fact(case, "rating_downgrade_trigger", "2026", None, "", "经营现金流持续弱化、再融资渠道明显收窄、治理缺陷扩大或拟议保证能力下降", "rating", "rating_5", page_maps),
        _fact(case, "rating_revenue", "2025", _d(next((c.rating_value for c in case.conflicts if c.fact_key == "revenue"), f.revenue)), "万元", "", "rating", "rating_financial", page_maps),
        _fact(case, "rating_total_debt", "2025", _d(next((c.rating_value for c in case.conflicts if c.fact_key == "interest_bearing_debt"), f.interest_bearing_debt)), "万元", "", "rating", "rating_financial", page_maps),
        _fact(case, "rating_issue_amount", "2026", case.rating_issue_amount, "万元", "", "rating", "rating_summary", page_maps),
        _fact(case, "proceeds_amount", "2026", case.proceeds_amount, "万元", "", "prospectus", "proceeds_1", page_maps),
        _fact(case, "proceeds_purpose", "2026", None, "", case.proceeds_purpose, "prospectus", "proceeds_1", page_maps),
        _fact(case, "repayment_account", "2026", None, "", "拟设募集资金专项账户和偿债账户", "prospectus", "proceeds_1", page_maps),
        _fact(case, "operating_cashflow_forecast", "2029", case.forecast.amount, "万元",
              f"{case.forecast_period}预测值；{case.forecast.basis}", "prospectus", "financial_4", page_maps),
        _fact(case, "controller", "2026", None, "", case.controller, "prospectus", "issuer_1", page_maps),
        _fact(case, "controller_share", "2026", case.controller_share, "%", "", "prospectus", "issuer_1", page_maps),
        _fact(case, "main_business", "2026", None, "", case.main_business, "prospectus", "issuer_1", page_maps),
        _fact(case, "top_customer_share", "2025", case.top_customer, "%", "", "prospectus", "issuer_5", page_maps),
        _fact(case, "top5_customer_share", "2025", case.top5_customer, "%", "", "prospectus", "issuer_5", page_maps),
        _fact(case, "top_supplier_share", "2025", case.top_supplier, "%", "", "prospectus", "issuer_5", page_maps),
        _fact(case, "top5_supplier_share", "2025", case.top5_supplier, "%", "", "prospectus", "issuer_5", page_maps),
        _fact(case, "related_unsupported_amount", "2025", case.related_unsupported, "万元", "", "prospectus", "issuer_5", page_maps),
        _fact(case, "guarantor_name", "2026", None, "", "东辰融资担保有限公司（虚构）", "prospectus", "enhancement_1", page_maps),
        _fact(case, "guarantee_scope", "2026", None, "", case.guarantee_scope, "prospectus", "enhancement_1", page_maps),
        _fact(case, "guarantee_period", "2026", None, "", "主债务履行期限届满之日起三年（拟议）", "prospectus", "enhancement_1", page_maps),
        _fact(case, "guarantee_cap", "2026", case.guarantee_cap, "万元", "", "prospectus", "enhancement_1", page_maps),
        _fact(case, "counter_guarantee_value", "2026", case.counter_value, "万元", "", "prospectus", "enhancement_2", page_maps),
        _fact(case, "counter_guarantee_priority", "2026", None, "", case.counter_rights, "prospectus", "enhancement_2", page_maps),
        _fact(case, "counter_guarantee_rights", "2026", None, "", case.counter_rights, "prospectus", "enhancement_2", page_maps),
        _fact(case, "investor_protection", "2026", None, "", "资信维持承诺、重大事项通知、负面事项救济和持有人会议", "prospectus", "protection_1", page_maps),
        _fact(case, "cross_default_clause", "2026", None, "", "约定金额以上债务违约且宽限期内未纠正时触发", "prospectus", "protection_1", page_maps),
    ]
    for snapshot in case.financials:
        for key, value in (
            ("revenue", snapshot.revenue), ("cost_of_goods_sold", snapshot.cost_of_goods_sold),
            ("net_profit", snapshot.net_profit), ("operating_cash_flow", snapshot.operating_cash_flow),
            ("cash_received_from_sales", snapshot.cash_received_from_sales),
            ("investing_cash_flow", snapshot.investing_cash_flow),
            ("financing_cash_flow", snapshot.financing_cash_flow),
            ("total_assets", snapshot.total_assets),
            ("total_liabilities", snapshot.total_liabilities), ("equity", snapshot.equity),
            ("current_assets", snapshot.current_assets), ("current_liabilities", snapshot.current_liabilities),
            ("cash", snapshot.cash), ("restricted_cash", snapshot.restricted_cash),
            ("other_receivables", snapshot.other_receivables),
            ("short_term_debt", snapshot.short_term_debt), ("debt_due_12m", snapshot.debt_due_12m),
            ("long_term_debt", snapshot.long_term_debt), ("bonds_payable", snapshot.bonds_payable),
            ("interest_bearing_debt", snapshot.interest_bearing_debt),
            ("finance_expense", snapshot.finance_expense), ("interest_expense", snapshot.interest_expense),
            ("accounts_receivable", snapshot.accounts_receivable), ("total_overdue_receivables", snapshot.overdue_receivables),
            ("inventory", snapshot.inventory),
        ):
            # 事实的来源页必须是该数值真正印在受控表格上的那一页：
            # 资产负债项目在第 2 页，利润项目在第 3 页，现金流量项目在第 4 页，
            # 有息负债分项在第 5 页。金标按此记录页码，解析器要能在该页逐格命中。
            if key in {"cost_of_goods_sold", "finance_expense", "interest_expense", "revenue", "net_profit"}:
                page_key = "financial_3"
            elif key in {"operating_cash_flow", "cash_received_from_sales", "investing_cash_flow", "financing_cash_flow"}:
                page_key = "financial_4"
            elif key in {"short_term_debt", "long_term_debt", "bonds_payable", "interest_bearing_debt"}:
                page_key = "financial_5"
            elif key == "total_overdue_receivables":
                page_key = "financial_7"      # 应收账款账龄结构页，该值印在这里
            else:
                page_key = "financial_2"
            items.append(_fact(case, key, str(snapshot.year), value, "万元", "", "prospectus", page_key, page_maps))
    items.extend([
        _fact(case, "bond_issue_amount", "2026", case.rating_issue_amount, "万元", "", "rating", "rating_summary", page_maps),
        _fact(case, "bond_term_years", "2026", _d(case.term_years), "年", "", "rating", "rating_summary", page_maps),
    ])
    for conflict in case.conflicts:
        if conflict.fact_key == "revenue":
            items.append(_fact(case, "revenue", conflict.period, _d(conflict.rating_value), "万元", "", "rating", "rating_financial", page_maps))
        elif conflict.fact_key == "interest_bearing_debt":
            items.append(_fact(case, "interest_bearing_debt", conflict.period, _d(conflict.rating_value), "万元", "", "rating", "rating_financial", page_maps))
    return items


def case_table_gold(case: BondCaseSpec, materials: list[dict]) -> list[dict]:
    """表格金标：把材料声明的表结构摊平成可逐格比对的注解。

    金标只描述**材料应当是什么样**——表名、单位、年度列、每个单元格的科目与数值，
    以及渲染器记录的页码跨度。它不引用解析器的抽取结果，两者相互独立，
    「解析准确率」才有意义。
    """
    output: list[dict] = []
    for material in materials:
        role = material["role"]
        for rendered in material.get("rendered_tables", []):
            declared = rendered.declared
            if declared is None:
                continue
            years = [
                int(column.split()[0]) for column in declared.columns[1:]
                if column.strip() and column.split()[0].isdigit()
            ]
            # 列键：年度列用年度，其余列用列名——与行键合起来是单元格的稳定地址，
            # 比 (行号, 列号) 更耐得住增删行列。
            column_keys = [
                str(years[index]) if index < len(years) else declared.columns[index + 1]
                for index in range(declared.column_count - 1)
            ]
            declared_conflicts = {(item.fact_key, item.period) for item in case.conflicts}
            # 没有年度列的表（如发行要素表），期次只能来自案例声明：同一个字段只声明了
            # 一个期次时才据此判定，不替材料编造期间。
            conflict_periods: dict[str, set[str]] = {}
            for fact_key_, period_ in declared_conflicts:
                conflict_periods.setdefault(fact_key_, set()).add(period_)
            output.append({
                "annotation_type": "table",
                "annotation_key": f"{role}:{rendered.table_id}",
                "expected": {
                    "table_id": rendered.table_id, "table_key": declared.table_key, "role": role,
                    "caption": declared.caption, "unit": declared.unit,
                    "entity": case.company, "scope": declared.scope, "currency": "CNY",
                    "columns": list(declared.columns),
                    # 行数与解析器口径一致：只计数据行，表头单独计数。
                    "row_count": len(declared.rows), "header_row_count": 1,
                    "column_count": declared.column_count,
                    "years": years, "start_page": rendered.start_page, "end_page": rendered.end_page,
                    "pages": sorted({cell.page for cell in rendered.cells}),
                    "header_repeat_pages": list(rendered.header_repeat_pages),
                    "notes": list(declared.notes),
                    "parser_version": TABLE_PARSER_VERSION,
                },
            })
            for cell in rendered.cells:
                # 表头行与首列不生成单元格金标：表头已由 table 注解的 columns 表达，
                # 首列是科目名。重复表达只会制造假差异。
                if cell.row == 0 or cell.column == 0:
                    continue
                subject = next(
                    (item.text for item in rendered.cells if item.row == cell.row and item.column == 0), "",
                )
                year = years[cell.column - 1] if cell.column - 1 < len(years) else None
                fact_key = row_fact_key(declared.table_key, _plain_label(subject))
                output.append({
                    "annotation_type": "table_cell",
                    "annotation_key": cell.cell_id,
                    "expected": {
                        "table_id": rendered.table_id,
                        "table_key": declared.table_key,
                        # 稳定地址：行键＝科目名，列键＝年度或列名
                        "row_key": _plain_label(subject), "row": cell.row,
                        "column_key": column_keys[cell.column - 1] if cell.column - 1 < len(column_keys) else "",
                        "column": cell.column,
                        "subject": subject.strip(),
                        # 主体与口径是核对匹配键的一部分，缺了就跨不了主体
                        "entity": case.company, "scope": declared.scope,
                        "currency": "CNY",
                        "year": year,
                        "raw": cell.text, "normalized": _gold_normalized(cell.text),
                        "kind": cell.kind, "unit": declared.unit, "page": cell.page,
                        "bbox": [round(value, 2) for value in cell.rect],
                        "fact_key": fact_key,
                        # 该格是否落在案例显式声明的冲突上
                        "expect_conflict": bool(fact_key) and (
                            (fact_key, str(year)) in declared_conflicts
                            if year is not None
                            else len(conflict_periods.get(fact_key, ())) == 1
                        ),
                    },
                })
            for index, note in enumerate(declared.notes):
                output.append({
                    "annotation_type": "table_note",
                    "annotation_key": f"{rendered.table_id}:note{index + 1}",
                    "expected": {"table_id": rendered.table_id, "text": note, "page": rendered.end_page},
                })
            if rendered.header_repeat_pages:
                output.append({
                    "annotation_type": "table_relation",
                    "annotation_key": f"{rendered.table_id}:continuation",
                    "expected": {
                        "table_id": rendered.table_id, "relation": "continuation_of",
                        "start_page": rendered.start_page,
                        "continued_pages": list(rendered.header_repeat_pages),
                        "repeats": ["header"], "not_repeated": ["caption", "unit"],
                    },
                })
    return output


# 表内科目名 → 事实字段。只有确实对应登记字段的行才登记，其余留空，
# 避免为了「映射率好看」编造对应关系。
ROW_FACT_KEYS = {
    "货币资金": "cash", "应收账款": "accounts_receivable", "其他应收款": "other_receivables",
    "存货": "inventory", "短期借款": "short_term_debt", "长期借款": "long_term_debt",
    "应付债券": "bonds_payable", "流动资产合计": "current_assets",
    "资产总计": "total_assets", "流动负债合计": "current_liabilities",
    "负债合计": "total_liabilities", "所有者权益合计": "equity",
    "一、营业收入": "revenue", "减：营业成本": "cost_of_goods_sold",
    "财务费用": "finance_expense", "其中：利息费用": "interest_expense",
    "四、净利润": "net_profit", "销售商品、提供劳务收到的现金": "cash_received_from_sales",
    "经营活动产生的现金流量净额": "operating_cash_flow",
    "投资活动产生的现金流量净额": "investing_cash_flow",
    "筹资活动产生的现金流量净额": "financing_cash_flow",
}


# 同一个「合计」在不同表里含义不同，全局映射做不到，只能按表覆盖。
TABLE_ROW_FACT_KEYS: dict[str, dict[str, str]] = {
    "interest_bearing_debt_detail": {"合计": "interest_bearing_debt"},
    "receivables_aging": {"合计": "accounts_receivable"},
    "offering_terms": {"本期金额": "bond_issue_amount", "存续年限": "bond_term_years"},
}


def row_fact_key(table_key: str, row_label: str) -> str | None:
    """行 → 事实字段：先查按表覆盖，再查全局映射。"""
    return TABLE_ROW_FACT_KEYS.get(table_key, {}).get(row_label) or ROW_FACT_KEYS.get(row_label)


def _plain_label(text: str) -> str:
    """去掉缩进与空白，得到用于映射事实字段的科目名。"""
    return text.replace("　", "").strip()


def _gold_normalized(text: str) -> str | None:
    """金标里的规范化值：只做口径内可解释的转换，不猜。

    ``-`` 在披露口径里表示零，``—`` 与空白表示无数据——两者不能混为一谈，
    所以这里返回 ``"0"`` 与 ``None`` 两个不同结果。
    """
    raw = (text or "").strip()
    if not raw or raw in {"—", "——"}:
        return None
    if raw in {"-", "－"}:
        return "0"
    return raw.replace(",", "").replace("，", "")


def _seed_gold(case: BondCaseSpec, facts: list[dict], table_gold: list[dict] | None = None) -> None:
    now = now_iso()
    unique: dict[tuple[str, str, float | None, str], dict] = {}
    for fact in facts:
        key = (fact["fact_key"], fact["period"], fact["value"], fact["text_value"])
        unique.setdefault(key, fact)
    with connect() as conn:
        conn.execute("DELETE FROM gold_annotations WHERE project_id=? AND id LIKE ?", (case.id, f"gold-{case.short}-%"))
        for index, fact in enumerate(unique.values(), 1):
            source = conn.execute("SELECT document_id,locator,page,text FROM sources WHERE id=?", (fact["source_ids"][0],)).fetchone()
            expected = {
                "fact_key": fact["fact_key"], "period": fact["period"], "value": fact["value"],
                "unit": fact["unit"], "text_value": fact["text_value"],
                "document_id": source["document_id"] if source else "",
                "locator": source["locator"] if source else "",
                "page": source["page"] if source else None,
                "quote": (source["text"][:300] if source else ""),
            }
            conn.execute(
                "INSERT INTO gold_annotations (id,project_id,annotation_type,annotation_key,expected_json,source_ids,version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"gold-{case.short}-fact-{index:03d}", case.id, "fact", f"{fact['fact_key']}:{fact['period']}:{index}", json_dump(expected), json_dump(fact["source_ids"]), TEMPLATE_VERSION, now),
            )
        for index, conflict in enumerate(case.conflicts, 1):
            conflict_facts = [fact for fact in facts if fact["fact_key"] == conflict.fact_key and fact["period"] == conflict.period]
            conflict_sources = list(dict.fromkeys(source_id for fact in conflict_facts for source_id in fact["source_ids"]))
            expected = {**conflict.model_dump(mode="json"), "assertions": [
                {"value": fact["value"], "text_value": fact["text_value"], "source_ids": fact["source_ids"]}
                for fact in conflict_facts
            ]}
            conn.execute(
                "INSERT INTO gold_annotations (id,project_id,annotation_type,annotation_key,expected_json,source_ids,version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"gold-{case.short}-conflict-{index:02d}", case.id, "conflict", f"{conflict.fact_key}:{conflict.period}", json_dump(expected), json_dump(conflict_sources), TEMPLATE_VERSION, now),
            )
        for index, rule_key in enumerate(case.expected_warnings, 1):
            conn.execute(
                "INSERT INTO gold_annotations (id,project_id,annotation_type,annotation_key,expected_json,source_ids,version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"gold-{case.short}-warning-{index:02d}", case.id, "warning", rule_key, json_dump({"rule_key": rule_key, "status": "warning"}), "[]", TEMPLATE_VERSION, now),
            )
        source_rows = conn.execute("SELECT id FROM sources WHERE project_id=? ORDER BY document_id,page", (case.id,)).fetchall()
        for index, source in enumerate(source_rows, 1):
            conn.execute(
                "INSERT INTO gold_annotations (id,project_id,annotation_type,annotation_key,expected_json,source_ids,version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"gold-{case.short}-citation-{index:03d}", case.id, "citation", source["id"], json_dump({"source_id": source["id"]}), json_dump([source["id"]]), TEMPLATE_VERSION, now),
            )
        conn.execute(
            "INSERT INTO gold_annotations (id,project_id,annotation_type,annotation_key,expected_json,source_ids,version,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"gold-{case.short}-tie-out", case.id, "tie_out", "balance_equation", json_dump({"expected_status": "passed"}), "[]", TEMPLATE_VERSION, now),
        )
        for index, item in enumerate(table_gold or (), 1):
            conn.execute(
                "INSERT INTO gold_annotations (id,project_id,annotation_type,annotation_key,expected_json,source_ids,version,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"gold-{case.short}-{item['annotation_type']}-{index:04d}", case.id,
                    item["annotation_type"], item["annotation_key"],
                    json_dump(item["expected"]), "[]", TEMPLATE_VERSION, now,
                ),
            )


def _archive_legacy_materials(case: BondCaseSpec, root: Path) -> None:
    """Keep the former supporting pack without exposing it as default input."""
    if not root.exists():
        return
    core_names = {PROSPECTUS_NAME, RATING_NAME, "case-manifest.json"}
    legacy_names = {"募集说明书.pdf", "信用评级报告.pdf"}
    archive = root / ("extended_pack" if case.id == "demo-bond-huachen" else "legacy_supporting_pack")
    for path in list(root.iterdir()):
        if not path.is_file() or path.name in core_names:
            continue
        if path.name in legacy_names or path.suffix.lower() in {".pdf", ".docx", ".xlsx"}:
            archive.mkdir(parents=True, exist_ok=True)
            target = archive / path.name
            if target.exists():
                stem, suffix, index = path.stem, path.suffix, 2
                while (archive / f"{stem}-{index}{suffix}").exists():
                    index += 1
                target = archive / f"{stem}-{index}{suffix}"
            path.replace(target)


def _seed_candidate_and_task(case: BondCaseSpec, page_maps: dict[str, dict[str, int]]) -> None:
    if not case.expected_candidate_taxonomies:
        return
    taxonomy = case.expected_candidate_taxonomies[0]
    if taxonomy == "governance":
        title = "关键审批权限集中于实控人"
        rationale = "募集说明书披露实际控制人同时控制合同审批、印章和网银复核，独立否决机制不足。"
        impact = "内部制衡不足可能增加未经授权付款和募集资金使用偏离风险。"
        questions = ["取得授权矩阵、印章台账和网银权限清单，抽查付款审批记录。"]
        mitigation = "发行前落实分级授权和双人复核，并将募集资金支付纳入专户监管。"
        page_key = "issuer_1"
    else:
        title = "大额合同真实性及募集资金流向待核验"
        rationale = "募集说明书披露部分大额合同缺少客户盖章，合同真实性尚待原件及外部证据核验。"
        impact = "交易真实性不足可能削弱经营回款和债券第一偿债来源。"
        questions = ["取得合同原件、客户函证、发票和付款流水并执行四项勾稽。"]
        mitigation = "发行前完成独立函证和专户流向核验，异常交易不得纳入偿债来源。"
        page_key = "risk_2"
    source_id = _source_id(case, "prospectus", page_maps["prospectus"][page_key])
    finding_id = f"candidate-{case.id}"
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO candidate_findings (id,project_id,taxonomy,title,rationale,potential_impact,likelihood,impact,severity,support_source_ids,counter_source_ids,questions,mitigation,status,confidence,decision_note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (finding_id, case.id, taxonomy, title, rationale, impact, 2, 3, "high", json_dump([source_id]), "[]", json_dump(questions), mitigation, "proposed", 0.9, "", now, now),
        )
        conn.execute(
            "INSERT INTO review_tasks (id,project_id,task_type,priority,title,description,status,related_ids,required_evidence,resolution_note,assignee,due_date,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"task-{case.id}", case.id, "candidate_finding", "high", f"复核候选风险：{title}", rationale, "open", json_dump([finding_id]), questions[0], "", "", "", now, now),
        )


def seed_case(case: BondCaseSpec, root: Path) -> tuple[list[dict], list[dict]]:
    _archive_legacy_materials(case, root)
    if _seed_is_current(case, root):
        # Older generated packs predate the document_role column and some
        # interrupted runs may have removed all derived facts.  Repair only
        # those missing pieces in place; do not reset a healthy project's run
        # history or model-derived output merely because the process started.
        with connect() as conn:
            conn.execute(
                "UPDATE documents SET document_role=? WHERE project_id=? AND id=? AND COALESCE(document_role,'')=''",
                ("prospectus", case.id, f"bond-{case.short}-prospectus"),
            )
            conn.execute(
                "UPDATE documents SET document_role=? WHERE project_id=? AND id=? AND COALESCE(document_role,'')=''",
                ("rating_report", case.id, f"bond-{case.short}-rating"),
            )
            fact_count = conn.execute("SELECT COUNT(*) AS count FROM facts WHERE project_id=?", (case.id,)).fetchone()["count"]
            table_count = conn.execute("SELECT COUNT(*) AS count FROM document_tables WHERE project_id=?", (case.id,)).fetchone()["count"]
            document_rows = conn.execute(
                "SELECT id,page_count FROM documents WHERE project_id=?", (case.id,)
            ).fetchall()
        if not table_count and document_rows:
            # 材料在“表格落库”这个能力之前就已播种，而快速路径不会重新播种，
            # 于是这些项目的表格永远是空的——``uploaded_table`` 生成不出来，
            # 报告只能退回 profile_facts 汇总。这里**不重新渲染材料**，只把既有
            # PDF 重新解析一遍并补上表格；材料文件本身一个字节都不动。
            _backfill_document_tables(case, root, document_rows)
        if not fact_count:
            # Facts are part of the deterministic fictional fixture.  Restore
            # them when a previous failed run removed them, while preserving
            # any existing assertions, reports and run history.
            materials = generate_case_materials(root, case)
            facts = case_facts(case, materials)
            now = now_iso()
            with connect() as conn:
                for fact in facts:
                    conn.execute(
                        "INSERT OR IGNORE INTO facts (id,project_id,category,name,period,value,unit,text_value,source_ids,confidence,fact_key,raw_value,extraction_method,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (fact["id"], case.id, fact["category"], fact["name"], fact["period"], fact["value"], fact["unit"], fact["text_value"], json_dump(fact["source_ids"]), fact["confidence"], fact["fact_key"], fact["raw_value"], fact["extraction_method"], fact["review_status"]),
                    )
            _ensure_gold_annotations(case, lambda: materials)
        _delete_orphan_run_state(case.id)
        _ensure_gold_annotations(case, lambda: generate_case_materials(root, case))
        return [], []
    materials = generate_case_materials(root, case)
    facts = case_facts(case, materials)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,project_type,bond_name,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (case.id, secrets.token_urlsafe(24), case.company, float(case.issue_amount), case.term_years * 12, case.proceeds_purpose, "ready", now, now, "bond_guarantee", case.bond_name, now),
        )
        conn.execute("UPDATE projects SET company_name=?,amount=?,term_months=?,purpose=?,status='ready',project_type='bond_guarantee',bond_name=?,updated_at=? WHERE id=?", (case.company, float(case.issue_amount), case.term_years * 12, case.proceeds_purpose, case.bond_name, now, case.id))
        # Reset all project-derived state, including workflow artifacts.  Leaving
        # artifacts behind would let a new run reuse nodes whose result tables
        # have just been deleted.
        run_ids = [item["id"] for item in conn.execute("SELECT id FROM runs WHERE project_id=?", (case.id,)).fetchall()]
        for table in ("llm_attempts", "run_nodes", "run_artifacts", "policy_snapshots"):
            if run_ids and table in {"llm_attempts", "run_nodes"}:
                placeholders = ",".join("?" for _ in run_ids)
                conn.execute(f"DELETE FROM {table} WHERE run_id IN ({placeholders})", run_ids)
            else:
                conn.execute(f"DELETE FROM {table} WHERE project_id=?", (case.id,))
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", run_ids)
        for table in ("documents", "sources", "facts", "fact_assertions", "canonical_facts", "financial_metrics", "financial_tie_outs", "cross_checks", "risks", "candidate_findings", "review_tasks", "report_sections", "citations", "report_claims", "audit_events"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (case.id,))
        for material in materials:
            parsed = material["parsed"]
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,document_role,ocr_status,page_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (material["id"], case.id, material["filename"], material["kind"], "application/pdf", sha256(material["path"]), material["path"].stat().st_size, "parsed", parsed.quality, len(parsed.fragments), parsed.warning, now, parsed.parser, material["document_role"], parsed.ocr_status, parsed.page_count),
            )
            for fragment in parsed.fragments:
                source_id = _source_id(case, material["role"], int(fragment.page or 0))
                content_hash = hashlib.sha256(fragment.text.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (source_id, case.id, material["id"], fragment.locator_type, fragment.locator, fragment.text, fragment.page, json_dump(fragment.bbox), fragment.extraction_method, content_hash),
                )
            # 表格与文本片段一并落库：此前只存片段，材料里的表格在解析后就被丢弃，
            # 「上传原表 → 核对」这条链路没有数据可依。
            persist_tables(conn, case.id, material["id"], parsed.tables)
        for fact in facts:
            conn.execute(
                "INSERT INTO facts (id,project_id,category,name,period,value,unit,text_value,source_ids,confidence,fact_key,raw_value,extraction_method,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fact["id"], case.id, fact["category"], fact["name"], fact["period"], fact["value"], fact["unit"], fact["text_value"], json_dump(fact["source_ids"]), fact["confidence"], fact["fact_key"], fact["raw_value"], fact["extraction_method"], fact["review_status"]),
            )
    table_gold = case_table_gold(case, materials)
    rendered_tables = [table for item in materials for table in item.get("rendered_tables", [])]
    role_of = {table.table_id: item["role"] for item in materials for table in item.get("rendered_tables", [])}
    _seed_gold(case, facts, table_gold)
    _seed_candidate_and_task(case, {item["role"]: item["key_pages"] for item in materials})
    manifest = {
        "template_version": TEMPLATE_VERSION,
        "spec_sha256": _spec_sha256(case),
        "project_id": case.id,
        "fictional": True,
        "documents": [
            {
                "filename": item["filename"], "role": item["role"], "pages": item["page_count"],
                "sha256": sha256(item["path"]), "key_pages": item["key_pages"],
                # 表格清单：金标文件路径、解析器版本与文件哈希，供评测复现。
                "file_sha256": sha256(item["path"]),
                "table_parser_version": TABLE_PARSER_VERSION,
                "tables": [
                    {
                        "table_id": table.table_id, "caption": table.caption,
                        "unit": table.declared.unit if table.declared else "",
                        "start_page": table.start_page, "end_page": table.end_page,
                        "row_count": len(table.declared.rows) if table.declared else 0,
                        "column_count": table.declared.column_count if table.declared else 0,
                        "continued_pages": table.header_repeat_pages,
                    }
                    for table in item.get("rendered_tables", [])
                ],
            }
            for item in materials
        ],
        # 顶层表格清单：金标、manifest 与核对接口都以 table_key 跨案例引用，
        # table_id 仍在项目内唯一。页码、跨页关系与解析器版本随表记录。
        "tables": [
            {
                "table_key": table.declared.table_key if table.declared else "",
                "table_id": table.table_id,
                "role": role_of[table.table_id], "caption": table.caption,
                "scope": table.declared.scope if table.declared else "consolidated",
                "unit": table.declared.unit if table.declared else "",
                "pages": list(table.declared_pages),
                "start_page": table.start_page, "end_page": table.end_page,
                "continued_pages": list(table.header_repeat_pages),
                "row_count": len(table.declared.rows) if table.declared else 0,
                "column_count": table.declared.column_count if table.declared else 0,
                "parser_version": TABLE_PARSER_VERSION,
            }
            for table in rendered_tables
        ],
        "declared_tables_sha256": _declared_tables_sha256(case),
        "table_gold": {
            "annotation_count": len(table_gold),
            "gold_path": "gold/financial_tables.json",
            "parser_version": TABLE_PARSER_VERSION,
        },
        "declared_conflicts": [item.model_dump(mode="json") for item in case.conflicts],
        "expected_material_gaps": ["债务与偿债", "主体治理与关联方", "法律与发行决议", "担保合同", "反担保材料", "经营与交易真实性"],
    }
    (root / "case-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # 表格金标同时落盘一份：数据库里的注解供系统消费，文件供评测与人工复核复现，
    # 两者内容一致，避免了「只在库里、无法与材料一起归档」的问题。
    gold_path = root / manifest["table_gold"]["gold_path"]
    gold_path.parent.mkdir(parents=True, exist_ok=True)
    gold_path.write_text(
        json.dumps(
            {"project_id": case.id, "template_version": TEMPLATE_VERSION,
             "parser_version": TABLE_PARSER_VERSION, "annotations": table_gold},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return materials, facts


def evaluation_manifest() -> dict[str, dict]:
    output = {}
    for case in CASE_SPECS:
        # The seeded materials are already generated during startup; regenerate only metadata in memory.
        root = Path(".")
        expected = []
        for snapshot in case.financials:
            for key, value in (
                ("revenue", snapshot.revenue), ("cost_of_goods_sold", snapshot.cost_of_goods_sold),
                ("net_profit", snapshot.net_profit), ("operating_cash_flow", snapshot.operating_cash_flow),
                ("cash_received_from_sales", snapshot.cash_received_from_sales),
                ("investing_cash_flow", snapshot.investing_cash_flow), ("financing_cash_flow", snapshot.financing_cash_flow),
                ("total_assets", snapshot.total_assets), ("total_liabilities", snapshot.total_liabilities),
                ("equity", snapshot.equity), ("current_assets", snapshot.current_assets),
                ("current_liabilities", snapshot.current_liabilities), ("cash", snapshot.cash),
                ("restricted_cash", snapshot.restricted_cash), ("accounts_receivable", snapshot.accounts_receivable),
                ("other_receivables", snapshot.other_receivables), ("inventory", snapshot.inventory),
                ("short_term_debt", snapshot.short_term_debt), ("debt_due_12m", snapshot.debt_due_12m),
                ("long_term_debt", snapshot.long_term_debt), ("bonds_payable", snapshot.bonds_payable),
                ("interest_bearing_debt", snapshot.interest_bearing_debt),
                ("finance_expense", snapshot.finance_expense), ("interest_expense", snapshot.interest_expense),
                ("total_overdue_receivables", snapshot.overdue_receivables),
            ):
                expected.append({"fact_key": key, "period": str(snapshot.year), "value": float(value), "unit": "万元"})
        expected.extend([
            {"fact_key": "bond_issue_amount", "period": "2026", "value": float(case.issue_amount), "unit": "万元"},
            {"fact_key": "bond_term_years", "period": "2026", "value": float(case.term_years), "unit": "年"},
            {"fact_key": "coupon_rate", "period": "2026", "value": float(case.coupon_rate), "unit": "%"},
            {"fact_key": "rating_revenue", "period": "2025", "value": float(next((c.rating_value for c in case.conflicts if c.fact_key == "revenue"), case.latest.revenue)), "unit": "万元"},
            {"fact_key": "rating_total_debt", "period": "2025", "value": float(next((c.rating_value for c in case.conflicts if c.fact_key == "interest_bearing_debt"), case.latest.interest_bearing_debt)), "unit": "万元"},
            {"fact_key": "rating_issue_amount", "period": "2026", "value": float(case.rating_issue_amount), "unit": "万元"},
        ])
        output[case.id] = {
            "expected_fact_count": len(expected), "expected_facts": expected,
            "expected_warning_keys": list(case.expected_warnings),
            "expected_conflicts": [{"fact_key": item.fact_key, "period": item.period} for item in case.conflicts],
            "expected_candidate_taxonomies": list(case.expected_candidate_taxonomies),
        }
    return output
