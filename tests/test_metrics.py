"""``metrics``：计划 §三 新增指标（周转率 / 结构占比 / 三年速动比率）的计算口径。

计划 §三 的要求：周转率用**期初期末平均余额**（缺期初不计算，不用期末余额替代）；
占比用同主体同口径同期数据；全部用 Decimal 计算并冻结公式与输入事实 ID；
分母为零不生成可用指标。

这些用例全部**纯内存构造**，不触数据库。
"""
from __future__ import annotations

from backend.app.metrics import calculate_metrics


def _fact(key: str, period: str, value, *, unit: str = "万元", scope: str = "consolidated") -> dict:
    return {
        "id": f"{key}-{period}", "fact_key": key, "period": period, "value": value,
        "unit": unit, "source_ids": [f"src-{key}"], "scope": scope,
        "subject_id": "entity-1",
    }


def _by_key(metrics: list[dict]) -> dict[tuple[str, str], dict]:
    return {(item["metric_key"], item["period"]): item for item in metrics}


BASE = [
    _fact("revenue", "2023", 2_000_000), _fact("revenue", "2024", 1_400_000),
    _fact("revenue", "2025", 1_800_000),
    _fact("cost_of_goods_sold", "2023", 3_000_000), _fact("cost_of_goods_sold", "2024", 2_600_000),
    _fact("cost_of_goods_sold", "2025", 3_000_000),
    _fact("accounts_receivable", "2023", 180_000), _fact("accounts_receivable", "2024", 160_000),
    _fact("accounts_receivable", "2025", 140_000),
    _fact("inventory", "2023", 7_200_000), _fact("inventory", "2024", 8_200_000),
    _fact("inventory", "2025", 8_400_000),
    _fact("total_assets", "2023", 87_000_000), _fact("total_assets", "2024", 92_000_000),
    _fact("total_assets", "2025", 96_000_000),
    _fact("current_assets", "2023", 13_800_000), _fact("current_assets", "2024", 14_900_000),
    _fact("current_assets", "2025", 15_100_000),
    _fact("total_liabilities", "2023", 57_000_000), _fact("total_liabilities", "2024", 61_000_000),
    _fact("total_liabilities", "2025", 62_000_000),
    _fact("current_liabilities", "2023", 9_900_000), _fact("current_liabilities", "2024", 9_700_000),
    _fact("current_liabilities", "2025", 9_600_000),
]


def test_turnover_uses_opening_and_closing_average():
    """周转率＝流量／平均余额，而平均余额取（期初＋期末）/2。

    2024：营业收入 1,400,000 ／（(180,000+160,000)/2＝170,000）＝8.2353
    2025：营业收入 1,800,000 ／（(160,000+140,000)/2＝150,000）＝12.0000
    """
    found = _by_key(calculate_metrics(BASE, 0))
    assert found[("ar_turnover", "2024")]["value"] == round(1_400_000 / 170_000, 4)
    assert found[("ar_turnover", "2025")]["value"] == round(1_800_000 / 150_000, 4)
    assert found[("ar_turnover", "2025")]["unit"] == "次"
    assert found[("ar_turnover", "2025")]["name"] == "应收账款周转率"

    assert found[("inventory_turnover", "2025")]["value"] == round(
        3_000_000 / ((8_200_000 + 8_400_000) / 2), 4)
    assert found[("inventory_turnover", "2024")]["unit"] == "次"


def test_turnover_skips_the_year_without_opening_balance():
    """负例：**缺期初**的年度不出周转率，且绝不用期末余额替代。

    2023 没有 2022 年末余额 → 不生成 (ar_turnover, 2023)；
    若实现改成"用期末余额"，2023 会凭空出现 2,000,000/180,000＝11.1111。
    """
    found = _by_key(calculate_metrics(BASE, 0))
    assert ("ar_turnover", "2023") not in found
    assert ("inventory_turnover", "2023") not in found


def test_structure_ratios_use_the_same_total_and_current_leg():
    """结构占比＝(合计−流动部分)／合计 ×100。"""
    found = _by_key(calculate_metrics(BASE, 0))
    assert found[("non_current_assets_ratio", "2023")]["value"] == round(
        (87_000_000 - 13_800_000) / 87_000_000 * 100, 4)
    assert found[("non_current_liabilities_ratio", "2023")]["value"] == round(
        (57_000_000 - 9_900_000) / 57_000_000 * 100, 4)
    assert found[("non_current_assets_ratio", "2025")]["unit"] == "%"
    assert found[("non_current_liabilities_ratio", "2025")]["name"] == "非流动负债占比"


def test_quick_ratio_is_emitted_for_every_available_year():
    """三年速动比率：（流动资产−存货）／流动负债，逐年各出一条。"""
    found = _by_key(calculate_metrics(BASE, 0))
    for period in ("2023", "2024", "2025"):
        assert ("quick_ratio", period) in found, period
    assert found[("quick_ratio", "2023")]["value"] == round(
        (13_800_000 - 7_200_000) / 9_900_000, 4)


def test_zero_denominator_produces_no_metric():
    """负例：分母为零时不生成可用指标（不能拿 inf/NaN 当"计算值"）。"""
    facts = [
        _fact("revenue", "2024", 1_000), _fact("revenue", "2025", 1_000),
        # 期初、期末都是 0 → 平均余额 0
        _fact("accounts_receivable", "2024", 0), _fact("accounts_receivable", "2025", 0),
        _fact("total_assets", "2025", 0), _fact("current_assets", "2025", 0),
    ]
    found = _by_key(calculate_metrics(facts, 0))
    assert ("ar_turnover", "2025") not in found
    assert ("non_current_assets_ratio", "2025") not in found


def test_new_metrics_freeze_formula_and_input_facts():
    """每个新指标都要能回溯：非空公式 ＋ 输入事实 ID（冻结计算链路）。"""
    for item in calculate_metrics(BASE, 0):
        if item["metric_key"] not in (
                "ar_turnover", "inventory_turnover",
                "non_current_assets_ratio", "non_current_liabilities_ratio"):
            continue
        assert item["formula"], item
        assert item["input_fact_ids"], item
        assert item["source_ids"], item
        # 周转率必须同时冻结流量与期初、期末三条输入（否则链路上少一环）
        if item["metric_key"] in ("ar_turnover", "inventory_turnover"):
            assert len(item["input_fact_ids"]) == 3, item
        else:
            assert len(item["input_fact_ids"]) == 2, item
