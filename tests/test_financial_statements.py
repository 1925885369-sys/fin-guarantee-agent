"""三张报表模型与勾稽不变量的契约测试。

这组测试有两条职责：

1. **冻结**——华辰的 24 个聚合值与迁移前逐字段相等，规则、指标、评测的输入不动；
2. **勾稽**——8 条不变量在构造期成立，任何一条被破坏都应当在 ``build_case_statements``
   阶段报错，而不是留到渲染或评测才暴露。

唯一允许变化的是 ``investing_cash_flow`` 与 ``financing_cash_flow``：旧实现按比例派生，
与现金余额路径矛盾（实测 2025 年差 5,126 万元）。它们不参与任何规则或指标计算。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from backend.app.bond_documents import CASE_BY_ID, CASE_SPECS
from backend.app.financial_statements import build_case_statements


# 华辰迁移前的聚合值，逐字段取自旧 ``_financials(48000, 2800, 6500, 72000, 50400,
# 31000, 28200, 31000, 3800, 12000, 3000, 20500)`` 的输出。除投资与筹资净额外必须完全一致。
HUACHEN_FROZEN = {
    2023: {
        "revenue": 39360, "net_profit": 2296, "operating_cash_flow": 4680,
        "total_assets": 59040, "total_liabilities": 41328, "equity": 17712,
        "current_assets": 25420, "current_liabilities": 23124, "cash": 5592,
        "restricted_cash": 671, "other_receivables": 2362, "short_term_debt": 9151,
        "debt_due_12m": 16810, "long_term_debt": 11693, "bonds_payable": 4576,
        "interest_bearing_debt": 25420, "cost_of_goods_sold": 29126,
        "finance_expense": 3583, "interest_expense": 3116, "accounts_receivable": 9840,
        "overdue_receivables": 2460, "inventory": 4723, "cash_received_from_sales": 36998,
    },
    2024: {
        "revenue": 43680, "net_profit": 2548, "operating_cash_flow": 5590,
        "total_assets": 65520, "total_liabilities": 45864, "equity": 19656,
        "current_assets": 28210, "current_liabilities": 25662, "cash": 6206,
        "restricted_cash": 745, "other_receivables": 2621, "short_term_debt": 10156,
        "debt_due_12m": 18655, "long_term_debt": 12976, "bonds_payable": 5078,
        "interest_bearing_debt": 28210, "cost_of_goods_sold": 32323,
        "finance_expense": 3977, "interest_expense": 3458, "accounts_receivable": 10920,
        "overdue_receivables": 2730, "inventory": 5242, "cash_received_from_sales": 41059,
    },
    2025: {
        "revenue": 48000, "net_profit": 2800, "operating_cash_flow": 6500,
        "total_assets": 72000, "total_liabilities": 50400, "equity": 21600,
        "current_assets": 31000, "current_liabilities": 28200, "cash": 6820,
        "restricted_cash": 818, "other_receivables": 2880, "short_term_debt": 11160,
        "debt_due_12m": 20500, "long_term_debt": 14260, "bonds_payable": 5580,
        "interest_bearing_debt": 31000, "cost_of_goods_sold": 35520,
        "finance_expense": 4370, "interest_expense": 3800, "accounts_receivable": 12000,
        "overdue_receivables": 3000, "inventory": 5760, "cash_received_from_sales": 45120,
    },
}

# 允许重编的两个字段：旧比例值与新值不同，且不参与任何规则或指标。
REDERIVED = ("investing_cash_flow", "financing_cash_flow")

ALL_CASES = [(case.short, case) for case in CASE_SPECS]


def test_huachen_aggregates_are_frozen() -> None:
    """华辰的 24 个聚合值必须与迁移前逐字段相等。"""
    case = CASE_BY_ID["demo-bond-huachen"]
    assert len(case.statements) == 3
    for year, expected in HUACHEN_FROZEN.items():
        snapshot = next(item for item in case.financials if item.year == year)
        actual = {key: int(getattr(snapshot, key)) for key in expected}
        assert actual == expected, f"{year} 年聚合值发生漂移：{actual}"


def test_huachen_rederived_cash_flow_matches_cash_path() -> None:
    """投资与筹资净额必须与现金余额路径一致，而不是旧的比例值。"""
    case = CASE_BY_ID["demo-bond-huachen"]
    old = {2023: (-2657, 2034), 2024: (-2948, 2257), 2025: (-3240, 2480)}
    for statement in case.statements:
        snapshot = next(item for item in case.financials if item.year == statement.year)
        assert int(snapshot.investing_cash_flow) == int(statement.cash_flow_statement.investing_cash_flow)
        assert int(snapshot.financing_cash_flow) == int(statement.cash_flow_statement.financing_cash_flow)
        assert (int(snapshot.investing_cash_flow), int(snapshot.financing_cash_flow)) != old[statement.year]


@pytest.mark.parametrize("short,case", ALL_CASES, ids=[short for short, _ in ALL_CASES])
def test_balance_sheet_ties_out(short: str, case) -> None:
    for statement in case.statements:
        sheet = statement.balance_sheet
        assert sheet.total_assets == sheet.total_liabilities + sheet.equity, f"{short} {statement.year}"
        assert sheet.current_assets == (
            sheet.cash + sheet.accounts_receivable + sheet.other_receivables
            + sheet.inventory + sheet.prepayments_and_other_current
        )
        assert sheet.non_current_liabilities == (
            sheet.long_term_debt + sheet.bonds_payable + sheet.other_non_current_liabilities
        )
        assert sheet.interest_bearing_debt == (
            sheet.short_term_debt + sheet.long_term_debt + sheet.bonds_payable
        )
        assert sheet.restricted_cash <= sheet.cash


@pytest.mark.parametrize("short,case", ALL_CASES, ids=[short for short, _ in ALL_CASES])
def test_income_statement_ties_out(short: str, case) -> None:
    for statement in case.statements:
        income = statement.income_statement
        snapshot = next(item for item in case.financials if item.year == statement.year)
        assert income.net_profit == income.total_profit - income.income_tax_expense
        assert income.total_profit == (
            income.operating_profit + income.non_operating_income - income.non_operating_expense
        )
        assert income.net_profit == snapshot.net_profit, f"{short} {statement.year}"
        assert income.interest_expense == snapshot.interest_expense


@pytest.mark.parametrize("short,case", ALL_CASES, ids=[short for short, _ in ALL_CASES])
def test_cash_flow_reconciles_to_balance_sheet_cash(short: str, case) -> None:
    """现金勾稽：期初 + 经营 + 投资 + 筹资 = 期末，且期末等于表内货币资金。"""
    for statement in case.statements:
        flow = statement.cash_flow_statement
        assert flow.closing_cash == flow.opening_cash + flow.net_increase, f"{short} {statement.year}"
        assert flow.closing_cash == statement.balance_sheet.cash, f"{short} {statement.year}"
        assert flow.operating_cash_flow == flow.operating_inflow - flow.operating_outflow
        assert flow.operating_cash_flow == next(
            item for item in case.financials if item.year == statement.year
        ).operating_cash_flow


@pytest.mark.parametrize("short,case", ALL_CASES, ids=[short for short, _ in ALL_CASES])
def test_statement_chain_is_continuous(short: str, case) -> None:
    """上一年期末现金等于下一年期初现金。"""
    for previous, current in zip(case.statements, case.statements[1:]):
        assert previous.cash_flow_statement.closing_cash == current.cash_flow_statement.opening_cash, short


@pytest.mark.parametrize("short,case", ALL_CASES, ids=[short for short, _ in ALL_CASES])
def test_receivables_detail_ties_out(short: str, case) -> None:
    for statement in case.statements:
        detail = statement.receivables
        snapshot = next(item for item in case.financials if item.year == statement.year)
        assert detail.carrying_value == detail.gross_balance - detail.bad_debt_provision
        assert detail.aging.total == detail.gross_balance
        assert detail.carrying_value == snapshot.accounts_receivable, f"{short} {statement.year}"
        assert detail.overdue_balance == snapshot.overdue_receivables
        assert detail.top_debtor_total <= detail.gross_balance


@pytest.mark.parametrize("short,case", ALL_CASES, ids=[short for short, _ in ALL_CASES])
def test_account_details_tie_out(short: str, case) -> None:
    """每组明细的合计必须等于主表科目，且没有负值分项。"""
    for statement in case.statements:
        details = statement.account_details
        sheet = statement.balance_sheet
        assert sum(row.amount for row in details.cash_items) == details.cash_total == sheet.cash
        assert details.cash_restricted == sheet.restricted_cash
        assert sum(row.amount for row in details.other_receivable_items) == sheet.other_receivables
        assert sum(row.amount for row in details.inventory_items) == sheet.inventory
        assert sum(row.amount for row in details.interest_bearing_items) == sheet.interest_bearing_debt
        for rows in (
            details.cash_items, details.other_receivable_items,
            details.inventory_items, details.interest_bearing_items,
        ):
            assert all(row.amount >= 0 for row in rows), short


@pytest.mark.parametrize("short,case", ALL_CASES, ids=[short for short, _ in ALL_CASES])
def test_statement_snapshot_equals_financial_snapshot(short: str, case) -> None:
    """报表导出的聚合值与 ``BondCaseSpec.financials`` 必须完全一致——防止两套真相源。"""
    for statement in case.statements:
        snapshot = next(item for item in case.financials if item.year == statement.year)
        for key, value in statement.snapshot().items():
            assert value == getattr(snapshot, key), f"{short} {statement.year} {key}"


def test_construction_rejects_unbalanced_statement() -> None:
    """勾稽不变量必须在构造期生效，而不是留到渲染。"""
    from backend.app.financial_statements import BalanceSheet

    with pytest.raises(ValueError, match="资产负债表不平"):
        BalanceSheet(
            cash=Decimal("1"), restricted_cash=Decimal("0"), accounts_receivable=Decimal("1"),
            other_receivables=Decimal("0"), inventory=Decimal("0"),
            prepayments_and_other_current=Decimal("0"), fixed_assets=Decimal("1"),
            construction_in_progress=Decimal("0"), intangible_assets=Decimal("0"),
            long_term_deferred_expense=Decimal("0"), other_non_current_assets=Decimal("0"),
            short_term_debt=Decimal("0"), notes_payable=Decimal("0"),
            accounts_payable=Decimal("0"), contract_liabilities=Decimal("0"),
            other_current_liabilities=Decimal("0"), long_term_debt=Decimal("0"),
            bonds_payable=Decimal("0"), other_non_current_liabilities=Decimal("0"),
            paid_in_capital=Decimal("1"), capital_reserve=Decimal("0"),
            surplus_reserve=Decimal("0"), retained_earnings=Decimal("0"),
        )


def test_builder_rejects_interest_bearing_debt_beyond_current_liabilities() -> None:
    """短期有息负债超出流动负债合计时，案例结构与总额不自洽，应显式报错。"""
    with pytest.raises(ValueError, match="有息负债短期部分超出流动负债合计"):
        build_case_statements(
            revenue=10000, net_profit=500, ocf=400, assets=20000, liabilities=4000,
            current_assets=3000, current_liabilities=300, debt=19000, interest=300,
            receivable=800, overdue=100, debt_due_12m=0,
        )
