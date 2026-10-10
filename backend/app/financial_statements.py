"""虚构债券案例的显式三张报表模型。

背景
----
旧实现（``bond_documents._financials``）用 11 个标量乘以固定比例派生出
``FinancialSnapshot`` 的 26 个聚合值。它无法渲染成真实财务报表，也无法作为
表格解析的金标；更关键的是，它**无法通过三张报表勾稽**：

- 货币资金由「流动资产 × 0.22」派生，而现金流量表由「OCF + 投资 + 筹资」派生，
  两者互不相干。实测 2025 年现金余额 6,206 → 6,820（+614 万元），而现金流量表
  隐含净增加 6,500 − 3,240 + 2,480 = 5,740 万元，**相差 5,126 万元**。

本模块的定位
------------
1. 把 26 个聚合值展开成可渲染、可解析、可逐格勾稽的报表行；
2. 所有子项加总等于既有聚合值——**冻结**：规则、指标、评测的输入一个不动；
3. 用 pydantic 校验器把勾稽不变量钉死在构造期，构造不出来就报错，不留隐患。

唯一的例外
----------
``investing_cash_flow`` 与 ``financing_cash_flow`` 必须重编。只有它们按现金余额
路径反推，现金勾稽才成立。这两个字段不参与任何规则或指标计算（``bond_rules``
与 ``metrics`` 只用 ``operating_cash_flow`` / ``interest_bearing_debt`` 等），
因此重编不产生任何判定漂移。

配平约定
--------
每个分项明细的**最后一项是配平项**，取「合计 − 其余分项」。比例只用来生成其余
分项，因此合计永远精确成立，年际扰动与舍入都不会破坏勾稽。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator


def _d(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value))


def _q(value: Decimal | int | str) -> Decimal:
    """量化到整数万元，与旧实现保持同一舍入方式。"""
    return Decimal(str(value)).quantize(Decimal("1"))


YEAR_SCALES = ((2023, _d("0.82")), (2024, _d("0.91")), (2025, _d("1")))
OCF_SCALES = ((2023, _d("0.72")), (2024, _d("0.86")), (2025, _d("1")))
# 非冻结子项的年际扰动，避免各年呈现机械化等比例；配平项吸收差额，不影响总额。
YEAR_VARIATION = ((2023, _d("0.97")), (2024, _d("1.03")), (2025, _d("1")))
EFFECTIVE_TAX_RATE = ((2023, _d("0.24")), (2024, _d("0.22")), (2025, _d("0.2037")))
OPENING_CASH_2023_RATIO = _d("0.75")

CURRENT_BALANCE_LABEL = "年末"
CURRENT_PERIOD_LABEL = "年度"


# ---------------------------------------------------------------------------
# 报表模型
# ---------------------------------------------------------------------------


class BalanceSheet(BaseModel):
    """合并资产负债表。分组行不参与加总；每个分部的最后一项为配平项。"""

    model_config = ConfigDict(frozen=True)

    cash: Decimal
    restricted_cash: Decimal  # 「其中」项，不加总
    accounts_receivable: Decimal
    other_receivables: Decimal
    inventory: Decimal
    prepayments_and_other_current: Decimal  # 配平项
    fixed_assets: Decimal
    construction_in_progress: Decimal
    intangible_assets: Decimal
    long_term_deferred_expense: Decimal
    other_non_current_assets: Decimal  # 配平项
    short_term_debt: Decimal
    notes_payable: Decimal
    accounts_payable: Decimal
    contract_liabilities: Decimal
    other_current_liabilities: Decimal  # 配平项
    long_term_debt: Decimal
    bonds_payable: Decimal
    other_non_current_liabilities: Decimal  # 配平项
    paid_in_capital: Decimal
    capital_reserve: Decimal
    surplus_reserve: Decimal
    retained_earnings: Decimal  # 配平项

    @property
    def current_assets(self) -> Decimal:
        return (
            self.cash + self.accounts_receivable + self.other_receivables
            + self.inventory + self.prepayments_and_other_current
        )

    @property
    def non_current_assets(self) -> Decimal:
        return (
            self.fixed_assets + self.construction_in_progress + self.intangible_assets
            + self.long_term_deferred_expense + self.other_non_current_assets
        )

    @property
    def total_assets(self) -> Decimal:
        return self.current_assets + self.non_current_assets

    @property
    def current_liabilities(self) -> Decimal:
        return (
            self.short_term_debt + self.notes_payable + self.accounts_payable
            + self.contract_liabilities + self.other_current_liabilities
        )

    @property
    def non_current_liabilities(self) -> Decimal:
        return self.long_term_debt + self.bonds_payable + self.other_non_current_liabilities

    @property
    def total_liabilities(self) -> Decimal:
        return self.current_liabilities + self.non_current_liabilities

    @property
    def equity(self) -> Decimal:
        return (
            self.paid_in_capital + self.capital_reserve + self.surplus_reserve
            + self.retained_earnings
        )

    @property
    def interest_bearing_debt(self) -> Decimal:
        return self.short_term_debt + self.long_term_debt + self.bonds_payable

    @model_validator(mode="after")
    def balance_sheet_balances(self) -> "BalanceSheet":
        if self.total_assets != self.total_liabilities + self.equity:
            raise ValueError("资产负债表不平：资产总计必须等于负债合计加所有者权益合计")
        if self.restricted_cash > self.cash:
            raise ValueError("受限资金不得超过货币资金")
        for label, value in (
            ("货币资金", self.cash),
            ("应收账款", self.accounts_receivable),
            ("其他应收款", self.other_receivables),
            ("存货", self.inventory),
            ("预付款项及其他流动资产", self.prepayments_and_other_current),
            ("其他非流动资产", self.other_non_current_assets),
            ("其他流动负债", self.other_current_liabilities),
            ("其他非流动负债", self.other_non_current_liabilities),
            ("未分配利润", self.retained_earnings),
        ):
            if value < 0:
                raise ValueError(f"{label}为负数，说明分项比例与合计不匹配")
        return self


class IncomeStatement(BaseModel):
    """主要利润项目表。``所得税费用`` 由税率倒推，保证利润总额与净利润勾稽。"""

    model_config = ConfigDict(frozen=True)

    revenue: Decimal
    cost_of_goods_sold: Decimal
    taxes_and_surcharges: Decimal
    selling_expense: Decimal
    administrative_expense: Decimal
    research_expense: Decimal
    finance_expense: Decimal
    interest_expense: Decimal  # 「其中」项，不加总
    interest_income: Decimal  # 「其中」项，不加总
    other_income: Decimal  # 配平项：政府补助等其他收益
    investment_income: Decimal
    credit_impairment_loss: Decimal  # 损失以"-"号填列
    non_operating_income: Decimal
    non_operating_expense: Decimal
    income_tax_expense: Decimal

    @property
    def gross_profit(self) -> Decimal:
        return self.revenue - self.cost_of_goods_sold

    @property
    def period_expenses(self) -> Decimal:
        return (
            self.selling_expense + self.administrative_expense
            + self.research_expense + self.finance_expense
        )

    @property
    def fee_and_other_finance(self) -> Decimal:
        return self.finance_expense - self.interest_expense + self.interest_income

    @property
    def operating_profit(self) -> Decimal:
        return (
            self.gross_profit - self.taxes_and_surcharges - self.period_expenses
            + self.other_income + self.investment_income + self.credit_impairment_loss
        )

    @property
    def total_profit(self) -> Decimal:
        return self.operating_profit + self.non_operating_income - self.non_operating_expense

    @property
    def net_profit(self) -> Decimal:
        return self.total_profit - self.income_tax_expense

    @model_validator(mode="after")
    def income_statement_ties(self) -> "IncomeStatement":
        if self.income_tax_expense < 0:
            raise ValueError("所得税费用为负数，说明利润总额低于净利润")
        if self.credit_impairment_loss > 0:
            raise ValueError("信用减值损失应以负数或零填列")
        if self.other_income < 0:
            raise ValueError("其他收益为负数，说明期间费用比例高于毛利")
        if self.fee_and_other_finance < 0:
            raise ValueError("财务费用中的手续费及其他为负数")
        return self


class CashFlowStatement(BaseModel):
    """主要现金流量项目表。期末余额由期初余额与净增加额勾稽得出。"""

    model_config = ConfigDict(frozen=True)

    opening_cash: Decimal
    cash_received_from_sales: Decimal
    tax_refund_received: Decimal
    other_operating_inflow: Decimal
    goods_and_services_paid: Decimal
    employee_paid: Decimal
    taxes_paid: Decimal
    other_operating_outflow: Decimal  # 配平项
    investing_inflow: Decimal
    investing_outflow: Decimal
    financing_inflow: Decimal
    financing_outflow: Decimal
    debt_repayment: Decimal
    interest_and_dividend_paid: Decimal

    @property
    def operating_inflow(self) -> Decimal:
        return self.cash_received_from_sales + self.tax_refund_received + self.other_operating_inflow

    @property
    def operating_outflow(self) -> Decimal:
        return self.goods_and_services_paid + self.employee_paid + self.taxes_paid + self.other_operating_outflow

    @property
    def operating_cash_flow(self) -> Decimal:
        return self.operating_inflow - self.operating_outflow

    @property
    def investing_cash_flow(self) -> Decimal:
        return self.investing_inflow - self.investing_outflow

    @property
    def financing_cash_flow(self) -> Decimal:
        return self.financing_inflow - self.financing_outflow

    @property
    def net_increase(self) -> Decimal:
        return self.operating_cash_flow + self.investing_cash_flow + self.financing_cash_flow

    @property
    def closing_cash(self) -> Decimal:
        return self.opening_cash + self.net_increase

    @model_validator(mode="after")
    def cash_flows_tie(self) -> "CashFlowStatement":
        for label, value in (
            ("投资活动现金流入小计", self.investing_inflow),
            ("投资活动现金流出小计", self.investing_outflow),
            ("筹资活动现金流入小计", self.financing_inflow),
            ("筹资活动现金流出小计", self.financing_outflow),
            ("偿还债务支付的现金", self.debt_repayment),
            ("分配股利、利润或偿付利息支付的现金", self.interest_and_dividend_paid),
        ):
            if value < 0:
                raise ValueError(f"{label}为负数")
        if self.debt_repayment + self.interest_and_dividend_paid != self.financing_outflow:
            raise ValueError("筹资活动现金流出分项与流出小计不一致")
        return self


class ReceivableAging(BaseModel):
    """应收账款账龄结构。账龄合计必须等于应收账款账面余额。"""

    model_config = ConfigDict(frozen=True)

    within_1y: Decimal
    from_1y_to_2y: Decimal
    from_2y_to_3y: Decimal
    over_3y: Decimal  # 配平项

    @property
    def total(self) -> Decimal:
        return self.within_1y + self.from_1y_to_2y + self.from_2y_to_3y + self.over_3y

    def rows(self) -> list[tuple[str, Decimal]]:
        return [
            ("1年以内（含1年）", self.within_1y),
            ("1至2年", self.from_1y_to_2y),
            ("2至3年", self.from_2y_to_3y),
            ("3年以上", self.over_3y),
        ]


class ReceivableDebtor(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    related_party: bool
    balance: Decimal
    reason: str


class ReceivablesDetail(BaseModel):
    """应收账款明细：账面余额、坏账准备、账面价值、账龄与主要欠款方。"""

    model_config = ConfigDict(frozen=True)

    gross_balance: Decimal
    bad_debt_provision: Decimal
    overdue_balance: Decimal
    aging: ReceivableAging
    top_debtors: tuple[ReceivableDebtor, ...] = ()

    @property
    def carrying_value(self) -> Decimal:
        return self.gross_balance - self.bad_debt_provision

    @property
    def top_debtor_total(self) -> Decimal:
        return sum((item.balance for item in self.top_debtors), Decimal("0"))

    @model_validator(mode="after")
    def receivables_tie(self) -> "ReceivablesDetail":
        if self.bad_debt_provision < 0:
            raise ValueError("坏账准备不得为负数")
        if self.carrying_value <= 0:
            raise ValueError("应收账款账面价值必须为正数")
        if self.aging.total != self.gross_balance:
            raise ValueError("账龄余额合计必须等于应收账款账面余额")
        if self.overdue_balance > self.gross_balance:
            raise ValueError("逾期应收账款不得超过应收账款账面余额")
        if self.top_debtor_total > self.gross_balance:
            raise ValueError("主要欠款方合计不得超过应收账款账面余额")
        return self


class AccountDetailRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    amount: Decimal
    note: str = ""


class AccountDetails(BaseModel):
    """货币资金、其他应收款、存货与有息负债明细；每组明细的合计等于主表科目。"""

    model_config = ConfigDict(frozen=True)

    cash_total: Decimal
    cash_restricted: Decimal
    cash_items: tuple[AccountDetailRow, ...]
    other_receivable_total: Decimal
    other_receivable_items: tuple[AccountDetailRow, ...]
    inventory_total: Decimal
    inventory_items: tuple[AccountDetailRow, ...]
    interest_bearing_total: Decimal
    interest_bearing_items: tuple[AccountDetailRow, ...]

    @model_validator(mode="after")
    def details_tie(self) -> "AccountDetails":
        groups = (
            ("货币资金明细", self.cash_items, self.cash_total),
            ("其他应收款明细", self.other_receivable_items, self.other_receivable_total),
            ("存货明细", self.inventory_items, self.inventory_total),
            ("有息负债明细", self.interest_bearing_items, self.interest_bearing_total),
        )
        for label, rows, total in groups:
            if not rows:
                raise ValueError(f"{label}为空")
            if any(row.amount < 0 for row in rows):
                raise ValueError(f"{label}存在负值分项")
            if sum((row.amount for row in rows), Decimal("0")) != total:
                raise ValueError(f"{label}分项合计与主表科目不一致")
        if self.cash_restricted > self.cash_total:
            raise ValueError("受限资金不得超过货币资金")
        return self


class AnnualStatements(BaseModel):
    """单个年度的三张报表与明细，是渲染、解析与金标的唯一数据源。"""

    model_config = ConfigDict(frozen=True)

    year: int
    debt_due_12m: Decimal  # 披露口径，不是资产负债表科目，不参与有息负债加总
    balance_sheet: BalanceSheet
    income_statement: IncomeStatement
    cash_flow_statement: CashFlowStatement
    receivables: ReceivablesDetail
    account_details: AccountDetails

    def snapshot(self) -> dict[str, Decimal]:
        """导出与旧 ``FinancialSnapshot`` 完全对应的聚合值。"""
        bs, inc, cf = self.balance_sheet, self.income_statement, self.cash_flow_statement
        return {
            "revenue": inc.revenue,
            "net_profit": inc.net_profit,
            "operating_cash_flow": cf.operating_cash_flow,
            "total_assets": bs.total_assets,
            "total_liabilities": bs.total_liabilities,
            "equity": bs.equity,
            "current_assets": bs.current_assets,
            "current_liabilities": bs.current_liabilities,
            "cash": bs.cash,
            "restricted_cash": bs.restricted_cash,
            "other_receivables": bs.other_receivables,
            "short_term_debt": bs.short_term_debt,
            "debt_due_12m": self.debt_due_12m,
            "long_term_debt": bs.long_term_debt,
            "bonds_payable": bs.bonds_payable,
            "interest_bearing_debt": bs.interest_bearing_debt,
            "cost_of_goods_sold": inc.cost_of_goods_sold,
            "finance_expense": inc.finance_expense,
            "interest_expense": inc.interest_expense,
            "accounts_receivable": self.receivables.carrying_value,
            "overdue_receivables": self.receivables.overdue_balance,
            "inventory": bs.inventory,
            "cash_received_from_sales": cf.cash_received_from_sales,
            "investing_cash_flow": cf.investing_cash_flow,
            "financing_cash_flow": cf.financing_cash_flow,
        }


# ---------------------------------------------------------------------------
# 分项比例：以 2025 年为基准，按年度扰动调整；每组的最后一项为配平项。
# ---------------------------------------------------------------------------

NON_CURRENT_MIX = (
    ("fixed_assets", _d("0.5463")),
    ("construction_in_progress", _d("0.1659")),
    ("intangible_assets", _d("0.1268")),
    ("long_term_deferred_expense", _d("0.0239")),
)
CURRENT_LIABILITY_MIX = (
    ("notes_payable", _d("0.1149")),
    ("accounts_payable", _d("0.3496")),
    ("contract_liabilities", _d("0.0702")),
)
PAID_IN_CAPITAL_RATIO = _d("0.370370")  # 实收资本占最近一年所有者权益
CAPITAL_RESERVE_RATIO = _d("0.287037")  # 资本公积占最近一年所有者权益
SURPLUS_RESERVE_RATIO = _d("0.075926")  # 盈余公积占当年所有者权益
CASH_RATIO = _d("0.22")
EXPENSE_MIX = (
    ("taxes_and_surcharges", _d("0.00708")),
    ("selling_expense", _d("0.04125")),
    ("administrative_expense", _d("0.05958")),
    ("research_expense", _d("0.05042")),
)
INVESTMENT_INCOME_RATIO = _d("0.03083")
CREDIT_IMPAIRMENT_RATIO = _d("0.00404")
INVESTMENT_SHARE_OF_GAP = _d("0.45")  # 缺口在投资收益与其他收益之间的分配上限
NON_OPERATING_INCOME_RATIO = _d("0.0001")
NON_OPERATING_EXPENSE_RATIO = _d("0.0031")
BAD_DEBT_PROVISION_RATIO = _d("0.0533")
AGING_MIX = (_d("0.7801"), _d("0.1250"), _d("0.0570"))
CASH_MIX = (("库存现金", _d("0.0018")), ("银行存款", _d("0.8858")))
INVENTORY_MIX = (("原材料", _d("0.3785")), ("在产品", _d("0.2188")), ("库存商品", _d("0.3542")))
OTHER_RECEIVABLE_MIX = (("押金及保证金", _d("0.0118")),)
TAX_REFUND_RATIO = _d("0.0065")
OTHER_OPERATING_INFLOW_RATIO = _d("0.0153")
OPERATING_OUTFLOW_MIX = (_d("0.8294"), _d("0.1201"), _d("0.0318"))
INVESTING_INFLOW_RATIO = _d("0.18")
FINANCING_INFLOW_RATIO = _d("0.29")
DIVIDEND_PAYOUT_RATIO = _d("0.10")
INVESTING_SHARE = _d("0.68")

TOP_DEBTORS = (
    ("华东汽车集团股份有限公司（虚构）", True, _d("0.1520"), "信用期内，按合同账期结算"),
    ("江淮动力系统有限公司（虚构）", False, _d("0.1180"), "信用期内，按合同账期结算"),
    ("新能热管理科技（虚构）", False, _d("0.0930"), "部分逾期，已启动催收"),
    ("北方车桥制造有限公司（虚构）", False, _d("0.0740"), "信用期内，按合同账期结算"),
)


def _share(base: Decimal, ratio: Decimal, variation: Decimal) -> Decimal:
    return _q(base * ratio * variation)


def _fit_to_total(values: list[Decimal], total: Decimal) -> list[Decimal]:
    """把一组分项收敛到合计以内：超额部分从最大项开始依次扣减。

    量化到整数万元会让「按比例展开的分项之和」比合计多出几个单位，若直接取差额
    作配平项就会得到负数。这里先把分项压回合计以内，配平项再取剩余差额。
    """
    out = list(values)
    excess = sum(out) - total
    for index in sorted(range(len(out)), key=lambda position: out[position], reverse=True):
        if excess <= 0:
            break
        cut = min(out[index], excess)
        out[index] -= cut
        excess -= cut
    if excess > 0:
        raise ValueError("分项合计超出可用总额，无法收敛")
    return out


def build_year(
    *,
    year: int,
    scale: Decimal,
    variation: Decimal,
    tax_rate: Decimal,
    revenue: Decimal,
    net_profit: Decimal,
    operating_cash_flow: Decimal,
    total_assets: Decimal,
    total_liabilities: Decimal,
    current_assets: Decimal,
    current_liabilities: Decimal,
    interest_bearing_debt: Decimal,
    interest_expense: Decimal,
    receivable: Decimal,
    overdue: Decimal,
    debt_due_12m: Decimal,
    restricted_ratio: Decimal,
    related_unsupported: Decimal,
    opening_cash: Decimal,
    paid_in_capital: Decimal,
    capital_reserve: Decimal,
) -> AnnualStatements:
    """构造单个年度。聚合值算式与旧实现逐字一致，保证冻结。"""
    cost_of_goods_sold = _q(revenue * _d("0.74"))
    finance_expense = _q(interest_expense * _d("1.15"))
    inventory = _q(revenue * _d("0.12"))
    other_receivables = _q(revenue * _d("0.06"))
    cash_received = _q(revenue * _d("0.94"))
    # 货币资金按「流动资产 × 0.22」派生，但不得超过流动资产扣除其它分项后的余量：
    # 少数案例（如融益、瀚川）的流动资产本就装不下按比例派生的分项，旧比例在那里
    # 自相矛盾。此时取上限，货币资金是唯一让位项——它不参与任何规则判定。
    cash = min(_q(current_assets * CASH_RATIO), current_assets - receivable - other_receivables - inventory)
    restricted_cash = _q(cash * restricted_ratio)
    non_current_assets = total_assets - current_assets
    non_current_liabilities = total_liabilities - current_liabilities
    equity = total_assets - total_liabilities

    # 有息负债的流动／非流动拆分先按 36%／18%／46% 的目标比例，再就可用空间收敛：
    # 非流动负债装不下长期部分时，超出部分回落到流动侧。``interest_bearing_debt``
    # 本身是规则输入，始终精确；拆分不参与任何规则或指标计算。
    desired_short = _q(interest_bearing_debt * _d("0.36"))
    desired_bonds = _q(interest_bearing_debt * _d("0.18"))
    desired_long = interest_bearing_debt - desired_short - desired_bonds
    long_term_total = min(desired_long + desired_bonds, non_current_liabilities)
    bonds_share = desired_bonds / (desired_long + desired_bonds) if desired_long + desired_bonds else Decimal("0")
    bonds_payable = _q(long_term_total * bonds_share)
    long_term_debt = long_term_total - bonds_payable
    short_term_debt = interest_bearing_debt - long_term_total
    if short_term_debt > current_liabilities:
        raise ValueError("有息负债短期部分超出流动负债合计，案例的负债结构与总额不自洽")

    non_current_values = {key: _share(non_current_assets, ratio, variation) for key, ratio in NON_CURRENT_MIX}
    # 流动负债先安置短期有息负债，其余分项在剩余空间内按比例分配到不挤爆配平项。
    current_remaining = current_liabilities - short_term_debt
    mix_keys = [key for key, _ in CURRENT_LIABILITY_MIX]
    current_liability_values = dict(zip(mix_keys, _fit_to_total(
        [_share(current_liabilities, ratio, variation) for _, ratio in CURRENT_LIABILITY_MIX],
        current_remaining,
    )))
    surplus_reserve = _share(equity, SURPLUS_RESERVE_RATIO, variation)

    balance_sheet = BalanceSheet(
        cash=cash, restricted_cash=restricted_cash, accounts_receivable=receivable,
        other_receivables=other_receivables, inventory=inventory,
        prepayments_and_other_current=current_assets - cash - receivable - other_receivables - inventory,
        fixed_assets=non_current_values["fixed_assets"],
        construction_in_progress=non_current_values["construction_in_progress"],
        intangible_assets=non_current_values["intangible_assets"],
        long_term_deferred_expense=non_current_values["long_term_deferred_expense"],
        other_non_current_assets=non_current_assets - sum(non_current_values.values()),
        short_term_debt=short_term_debt,
        notes_payable=current_liability_values["notes_payable"],
        accounts_payable=current_liability_values["accounts_payable"],
        contract_liabilities=current_liability_values["contract_liabilities"],
        other_current_liabilities=current_remaining - sum(current_liability_values.values()),
        long_term_debt=long_term_debt, bonds_payable=bonds_payable,
        other_non_current_liabilities=non_current_liabilities - long_term_total,
        paid_in_capital=paid_in_capital, capital_reserve=capital_reserve,
        surplus_reserve=surplus_reserve,
        retained_earnings=equity - paid_in_capital - capital_reserve - surplus_reserve,
    )

    expense_values = {key: _share(revenue, ratio, variation) for key, ratio in EXPENSE_MIX}
    credit_impairment_loss = -_share(revenue, CREDIT_IMPAIRMENT_RATIO, variation)
    non_operating_income = max(_share(revenue, NON_OPERATING_INCOME_RATIO, variation), Decimal("1"))
    non_operating_expense = _share(revenue, NON_OPERATING_EXPENSE_RATIO, variation)

    # 利润表按「其他收益与投资收益分摊缺口 → 所得税费用配平」两段构造，保证
    # 「营业利润 + 营业外收入 − 营业外支出 = 利润总额」与「利润总额 − 所得税 = 净利润」
    # 同时成立。投资收益是弹性项而非固定比例：缺口为负时说明税前经营利润已经
    # 高于目标，此时上调酌量性费用把缺口压回零，避免出现负的收益科目。
    total_profit_target = _q(net_profit / (Decimal("1") - tax_rate))
    operating_profit_target = total_profit_target - non_operating_income + non_operating_expense
    gross_profit = revenue - cost_of_goods_sold
    discretionary = ("selling_expense", "administrative_expense", "research_expense")

    def _income_gap() -> Decimal:
        """其他收益与投资收益需要共同填补的缺口。"""
        return (
            operating_profit_target - gross_profit + expense_values["taxes_and_surcharges"]
            + sum(expense_values[key] for key in discretionary) + finance_expense
            - credit_impairment_loss
        )

    gap = _income_gap()
    if gap < 0:
        pool = sum(expense_values[key] for key in discretionary)
        if pool + gap <= 0:
            raise ValueError("案例的税前经营利润高于目标，且无酌量性费用可上调")
        scale_up = (pool - gap) / pool
        for key in discretionary:
            expense_values[key] = _q(expense_values[key] * scale_up)
        gap = _income_gap()
    investment_income = min(_share(revenue, INVESTMENT_INCOME_RATIO, variation), _q(gap * INVESTMENT_SHARE_OF_GAP))
    other_income = gap - investment_income

    # 所得税费用是最后配平项，使净利润精确回到冻结值。
    actual_operating_profit = (
        gross_profit - expense_values["taxes_and_surcharges"]
        - sum(expense_values[key] for key in discretionary) - finance_expense
        + other_income + investment_income + credit_impairment_loss
    )
    actual_total_profit = actual_operating_profit + non_operating_income - non_operating_expense

    income_statement = IncomeStatement(
        revenue=revenue, cost_of_goods_sold=cost_of_goods_sold, **expense_values,
        finance_expense=finance_expense, interest_expense=interest_expense,
        interest_income=_q(interest_expense * _d("0.06")), other_income=other_income,
        investment_income=investment_income, credit_impairment_loss=credit_impairment_loss,
        non_operating_income=non_operating_income, non_operating_expense=non_operating_expense,
        income_tax_expense=actual_total_profit - net_profit,
    )
    if income_statement.net_profit != net_profit:
        raise ValueError(f"{year}年利润表未能勾稽到冻结净利润：{income_statement.net_profit} != {net_profit}")

    # 经营活动：流入小计由销售收现 + 税费返还 + 其他流入构成；
    # 流出小计 = 流入小计 − 经营净现金流（既有的冻结值），末项配平。
    tax_refund = _share(cash_received, TAX_REFUND_RATIO, variation)
    other_operating_inflow = _share(cash_received, OTHER_OPERATING_INFLOW_RATIO, variation)
    operating_outflow = (
        cash_received + tax_refund + other_operating_inflow - operating_cash_flow
    )
    outflow_values = [_q(operating_outflow * ratio) for ratio in OPERATING_OUTFLOW_MIX]

    # 投资与筹资净额按现金余额路径反推：期末现金 = 期初 + 经营 + 投资 + 筹资。
    net_increase = cash - opening_cash
    investing_cash_flow = _q((net_increase - operating_cash_flow) * INVESTING_SHARE)
    financing_cash_flow = net_increase - operating_cash_flow - investing_cash_flow
    investing_inflow = max(_q(cash * INVESTING_INFLOW_RATIO), Decimal("1"))
    financing_inflow = max(_q(interest_bearing_debt * FINANCING_INFLOW_RATIO), Decimal("1"))
    financing_outflow = financing_inflow - financing_cash_flow
    interest_and_dividend_paid = interest_expense + _q(net_profit * DIVIDEND_PAYOUT_RATIO)

    cash_flow_statement = CashFlowStatement(
        opening_cash=opening_cash, cash_received_from_sales=cash_received,
        tax_refund_received=tax_refund, other_operating_inflow=other_operating_inflow,
        goods_and_services_paid=outflow_values[0], employee_paid=outflow_values[1],
        taxes_paid=outflow_values[2],
        other_operating_outflow=operating_outflow - sum(outflow_values),
        investing_inflow=investing_inflow, investing_outflow=investing_inflow - investing_cash_flow,
        financing_inflow=financing_inflow, financing_outflow=financing_outflow,
        debt_repayment=financing_outflow - interest_and_dividend_paid,
        interest_and_dividend_paid=interest_and_dividend_paid,
    )

    bad_debt_provision = _q(receivable * BAD_DEBT_PROVISION_RATIO)
    gross_balance = receivable + bad_debt_provision
    aging = ReceivableAging(
        within_1y=_share(gross_balance, AGING_MIX[0], variation),
        from_1y_to_2y=_share(gross_balance, AGING_MIX[1], variation),
        from_2y_to_3y=_share(gross_balance, AGING_MIX[2], variation),
        over_3y=gross_balance - _share(gross_balance, AGING_MIX[0], variation)
        - _share(gross_balance, AGING_MIX[1], variation) - _share(gross_balance, AGING_MIX[2], variation),
    )
    receivables = ReceivablesDetail(
        gross_balance=gross_balance, bad_debt_provision=bad_debt_provision,
        overdue_balance=overdue, aging=aging,
        top_debtors=tuple(
            ReceivableDebtor(name=name, related_party=related, balance=_q(gross_balance * ratio), reason=reason)
            for name, related, ratio, reason in TOP_DEBTORS
        ),
    )

    cash_amounts = _fit_to_total(
        [_share(cash, ratio, variation) for _, ratio in CASH_MIX], cash,
    )
    cash_items = [AccountDetailRow(name=name, amount=amount) for (name, _), amount in zip(CASH_MIX, cash_amounts)]
    cash_items.append(AccountDetailRow(name="其他货币资金", amount=cash - sum(item.amount for item in cash_items)))

    receivable_amounts = _fit_to_total(
        [_share(revenue, ratio, variation) for _, ratio in OTHER_RECEIVABLE_MIX], other_receivables,
    )
    other_receivable_items = [
        AccountDetailRow(name=name, amount=amount)
        for (name, _), amount in zip(OTHER_RECEIVABLE_MIX, receivable_amounts)
    ]
    if related_unsupported > 0:
        # 关联方往来是案例声明的敞口，但不能超过其他应收款扣掉押金后的余额。
        headroom = other_receivables - sum(item.amount for item in other_receivable_items)
        other_receivable_items.insert(0, AccountDetailRow(
            name="关联方往来（未提供合同）", amount=min(related_unsupported, max(headroom, Decimal("0"))),
        ))
    other_receivable_items.append(AccountDetailRow(
        name="备用金及其他", amount=other_receivables - sum(item.amount for item in other_receivable_items),
    ))

    inventory_amounts = _fit_to_total(
        [_share(inventory, ratio, variation) for _, ratio in INVENTORY_MIX], inventory,
    )
    inventory_items = [
        AccountDetailRow(name=name, amount=amount)
        for (name, _), amount in zip(INVENTORY_MIX, inventory_amounts)
    ]
    inventory_items.append(AccountDetailRow(name="发出商品", amount=inventory - sum(item.amount for item in inventory_items)))

    account_details = AccountDetails(
        cash_total=cash, cash_restricted=restricted_cash, cash_items=tuple(cash_items),
        other_receivable_total=other_receivables, other_receivable_items=tuple(other_receivable_items),
        inventory_total=inventory, inventory_items=tuple(inventory_items),
        interest_bearing_total=interest_bearing_debt,
        interest_bearing_items=(
            AccountDetailRow(name="短期借款", amount=short_term_debt),
            AccountDetailRow(name="长期借款", amount=long_term_debt),
            AccountDetailRow(name="应付债券", amount=bonds_payable),
        ),
    )
    return AnnualStatements(
        year=year, debt_due_12m=debt_due_12m, balance_sheet=balance_sheet,
        income_statement=income_statement, cash_flow_statement=cash_flow_statement,
        receivables=receivables, account_details=account_details,
    )


def build_case_statements(
    *,
    revenue: int,
    net_profit: int,
    ocf: int,
    assets: int,
    liabilities: int,
    current_assets: int,
    current_liabilities: int,
    debt: int,
    interest: int,
    receivable: int,
    overdue: int,
    debt_due_12m: int,
    restricted_ratio: Decimal = _d("0.12"),
    related_unsupported: int = 0,
) -> tuple[AnnualStatements, ...]:
    """按案例级 11 个标量构造三年报表，返回 2023→2025 升序序列。

    实收资本与资本公积按最近一年所有者权益定档，三年保持同一数额；盈余公积与
    未分配利润在年内配平，因此既贴近真实权益结构，又保证「资产 = 负债 + 权益」。
    """
    latest_equity = _q(_d(assets)) - _q(_d(liabilities))
    paid_in_capital = _q(latest_equity * PAID_IN_CAPITAL_RATIO)
    capital_reserve = _q(latest_equity * CAPITAL_RESERVE_RATIO)
    opening_cash: Decimal | None = None
    output: list[AnnualStatements] = []
    for (year, scale), (_, ocf_scale), (_, variation), (_, tax_rate) in zip(
        YEAR_SCALES, OCF_SCALES, YEAR_VARIATION, EFFECTIVE_TAX_RATE
    ):
        annual_revenue = _q(_d(revenue) * scale)
        annual_debt = _q(_d(debt) * scale)
        if opening_cash is None:
            opening_cash = _q(_q(_d(current_assets) * scale * _d("0.22")) * OPENING_CASH_2023_RATIO)
        statements = build_year(
            year=year, scale=scale, variation=variation, tax_rate=tax_rate,
            revenue=annual_revenue,
            net_profit=_q(_d(net_profit) * scale),
            operating_cash_flow=_q(_d(ocf) * ocf_scale),
            total_assets=_q(_d(assets) * scale),
            total_liabilities=_q(_d(liabilities) * scale),
            current_assets=_q(_d(current_assets) * scale),
            current_liabilities=_q(_d(current_liabilities) * scale),
            interest_bearing_debt=annual_debt,
            interest_expense=_q(_d(interest) * scale),
            receivable=_q(_d(receivable) * scale),
            overdue=_q(_d(overdue) * scale),
            debt_due_12m=_q(_d(debt_due_12m) * scale),
            restricted_ratio=restricted_ratio,
            related_unsupported=_d(related_unsupported),
            opening_cash=opening_cash,
            paid_in_capital=paid_in_capital, capital_reserve=capital_reserve,
        )
        opening_cash = statements.balance_sheet.cash
        output.append(statements)
    return tuple(output)
