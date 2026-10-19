"""受限表达式求值器（`backend/app/calculations.py`）的单元测试。

对应《风控约束分层与模型分析能力调整方案》§三.2：
「采用受限数学表达式解析器及 Decimal 运算，不执行模型代码；支持四则运算、括号、平均、
绝对值、最小值和最大值。首版不执行任意函数及预测模型。」
「业务输入必须引用冻结记录，不得凭空增加增长假设、折扣率或调整值。」

本文件只测**纯计算**与**安全边界**，不涉及数据库与模型响应结构。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from backend.app.calculations import (
    CalculationError, close_enough, evaluate, normalize_expression,
)


def test_arithmetic_and_precedence():
    assert evaluate("1+2*3") == Decimal(7)
    assert evaluate("(1+2)*3") == Decimal(9)
    assert evaluate("10/4") == Decimal("2.5")
    assert evaluate("2*(3+4)-5") == Decimal(9)
    assert evaluate("-3+5") == Decimal(2)
    # 前导正负号可叠加，不做奇怪解释
    assert evaluate("--3") == Decimal(3)


def test_percent_literal_is_divided_by_hundred():
    """`%` 只作为**数字字面量**的后缀（额度/费率场景常用）。"""
    assert evaluate("50%") == Decimal("0.5")
    assert evaluate("1200*30%") == Decimal("360")
    assert evaluate("(1-20%)*100") == Decimal("80")


def test_functions_are_whitelisted_only():
    assert evaluate("avg(1,2,3)") == Decimal(2)
    assert evaluate("mean(2,4)") == Decimal(3)
    assert evaluate("abs(-2.5)") == Decimal("2.5")
    assert evaluate("min(3,1,2)") == Decimal(1)
    assert evaluate("max(3,1,2)") == Decimal(3)
    # 嵌套与表达式参数
    assert evaluate("avg(abs(-4),2)") == Decimal(3)
    assert evaluate("max(a,0)", {"a": Decimal("-7")}) == Decimal(0)

    for bad in ("sqrt(4)", "round(1.5)", "sum(1,2)", "exp(1)", "log(10)"):
        with pytest.raises(CalculationError) as excinfo:
            evaluate(bad)
        assert excinfo.value.code == "formula_invalid"


def test_variables_must_be_frozen_inputs():
    assert evaluate("a/b", {"a": Decimal(10), "b": Decimal(4)}) == Decimal("2.5")
    # 大小写不敏感（材料里科目名常大小写混用）
    assert evaluate("revenue*2", {"Revenue": Decimal(5)}) == Decimal(10)

    with pytest.raises(CalculationError) as excinfo:
        evaluate("a + growth_rate", {"a": Decimal(1)})
    assert excinfo.value.code == "input_not_frozen"
    assert "growth_rate" in str(excinfo.value)


def test_division_by_zero_is_explicit_not_infinity():
    for expression in ("1/0", "a/(b-b)"):
        with pytest.raises(CalculationError) as excinfo:
            evaluate(expression, {"a": Decimal(1), "b": Decimal(2)})
        assert excinfo.value.code == "division_by_zero"


def test_malformed_expressions_are_rejected():
    for expression in ("1+", "(1+2", "1+2)", "avg(1,)", "1,,2", "", "   "):
        with pytest.raises(CalculationError) as excinfo:
            evaluate(expression)
        assert excinfo.value.code == "formula_invalid", expression


def test_no_code_execution_surface():
    """任何越出四则+白名单函数的写法都必须被词法阶段拦下。"""
    for expression in (
        "__import__('os').system('ls')",
        "abs.__class__",
        "a[0]",
        "1 if 1 else 2",
        "1<2",
        "'1'+'2'",
        "open('x')",
        "lambda: 1",
        "1;2",
    ):
        with pytest.raises(CalculationError):
            evaluate(expression, {"a": Decimal(1)})


def test_fullwidth_and_chinese_punctuation_are_normalized():
    """模型常照抄材料的全角写法；排版差异不算公式错误。"""
    assert normalize_expression("（1＋2）") == "(1+2)"
    assert evaluate("（营业收入－营业成本）／营业收入",
                    {"营业收入": Decimal(100), "营业成本": Decimal(60)}) == Decimal("0.4")
    assert evaluate("1200×30％") == Decimal("360")


def test_close_enough_with_declared_precision():
    """给了"批准的展示精度"就按精度对齐判——这正是方案 §三.2 的判据。"""
    computed = Decimal("0.333333333")
    assert close_enough(computed, Decimal("0.3333"), precision=4)
    assert not close_enough(computed, Decimal("0.33"), precision=4)
    assert close_enough(Decimal("1234.5678"), Decimal("1234.57"), precision=2)
    assert not close_enough(Decimal("100"), Decimal("101"), precision=2)


def test_close_enough_without_precision_is_strict():
    """没给精度时不放宽展示精度：只吸收末位噪声。"""
    assert close_enough(Decimal("0.3333333333"), Decimal("0.333333333"))
    assert not close_enough(Decimal("0.333333333"), Decimal("0.3333"))
    assert not close_enough(Decimal("100"), Decimal("101"))


def test_decimal_avoids_binary_float_drift():
    """0.1+0.2 在 float 下不等于 0.3；Decimal 必须精确。"""
    assert evaluate("0.1+0.2") == Decimal("0.3")
    assert evaluate("0.3-0.1-0.2") == Decimal(0)


# ------------------------------------------------------------------ 系统复算与口径检查 --

from backend.app.calculations import HARD_ERROR_CODES, REVIEW_CODES, verify_calculation


def _rec(record_id: str, value, *, unit="万元", label="", period="2025",
         subject="发行人", scope="合并", status="verified") -> dict:
    return {
        "record_id": record_id, "value": str(value), "unit": unit, "label": label,
        "period": period, "subject": subject, "scope": scope, "status": status,
    }


def test_verified_when_inputs_frozen_and_result_matches():
    frozen = [
        _rec("fact:rev", "1000", label="营业收入"),
        _rec("fact:cost", "600", label="营业成本"),
    ]
    outcome = verify_calculation({
        "calculation_id": "calc-1", "name": "毛利率", "definition": "毛利/营业收入",
        "formula": "(营业收入-营业成本)/营业收入",
        "input_record_ids": ["fact:rev", "fact:cost"], "value": "0.4", "unit": "倍",
    }, frozen)
    assert outcome["status"] == "verified", outcome
    assert Decimal(outcome["computed"]) == Decimal("0.4")


def test_record_id_can_be_used_directly_in_formula():
    frozen = [_rec("a", "10"), _rec("b", "4")]
    outcome = verify_calculation({
        "formula": "a/b", "input_record_ids": ["a", "b"], "value": "2.5", "unit": "倍",
    }, frozen)
    assert outcome["status"] == "verified", outcome


def test_unfrozen_input_is_hard_error_not_guessed():
    """方案 §三.2：业务输入必须引用冻结记录，不得凭空增加增长假设。"""
    frozen = [_rec("fact:rev", "1000", label="营业收入")]
    outcome = verify_calculation({
        "formula": "营业收入*(1+growth_rate)", "input_record_ids": ["fact:rev"],
        "value": "1100", "unit": "万元",
    }, frozen)
    assert outcome["status"] == "rejected"
    assert outcome["reason_code"] in HARD_ERROR_CODES
    assert outcome["reason_code"] == "input_not_frozen"

    # 声明了未冻结的记录 ID
    outcome = verify_calculation({
        "formula": "x", "input_record_ids": ["fact:nope"], "value": "1",
    }, frozen)
    assert outcome["reason_code"] == "input_not_frozen"


def test_value_mismatch_is_rejected():
    frozen = [_rec("a", "10"), _rec("b", "4")]
    outcome = verify_calculation({
        "formula": "a/b", "input_record_ids": ["a", "b"], "value": "3.0",
    }, frozen)
    assert outcome["status"] == "rejected"
    assert outcome["reason_code"] == "value_mismatch"


def test_division_by_zero_is_rejected():
    frozen = [_rec("a", "10"), _rec("b", "0")]
    outcome = verify_calculation({
        "formula": "a/b", "input_record_ids": ["a", "b"], "value": "0",
    }, frozen)
    assert outcome["status"] == "rejected"
    assert outcome["reason_code"] == "division_by_zero"


def test_unit_dimension_mismatch_is_hard_error():
    frozen = [_rec("a", "10", unit="万元"), _rec("b", "4", unit="人")]
    outcome = verify_calculation({
        "formula": "a/b", "input_record_ids": ["a", "b"], "value": "2.5",
    }, frozen)
    assert outcome["status"] == "rejected"
    assert outcome["reason_code"] == "unit_dimension_mismatch"


def test_unit_scale_mismatch_needs_review_not_silent_conversion():
    """同族不同量级（万元 vs 元）不静默换算，标待核验。"""
    frozen = [_rec("a", "10", unit="万元"), _rec("b", "4", unit="元")]
    outcome = verify_calculation({
        "formula": "a/b", "input_record_ids": ["a", "b"], "value": "2.5",
    }, frozen)
    assert outcome["status"] == "needs_review"
    assert outcome["reason_code"] == "unit_scale_mismatch"


def test_subject_mismatch_needs_review():
    frozen = [_rec("a", "10", subject="发行人"), _rec("b", "4", subject="保证人")]
    outcome = verify_calculation({
        "formula": "a/b", "input_record_ids": ["a", "b"], "value": "2.5",
    }, frozen)
    assert outcome["status"] == "needs_review"
    assert outcome["reason_code"] == "subject_mismatch"


def test_period_mismatch_needs_review_unless_cross_period_explained():
    """方案 §三.2：增长率允许明确的跨期比较；没说明就待核验。"""
    frozen = [_rec("a", "110", period="2025"), _rec("b", "100", period="2024")]
    record = {"formula": "(a-b)/b", "input_record_ids": ["a", "b"], "value": "0.1"}
    outcome = verify_calculation(record, frozen)
    assert outcome["status"] == "needs_review"
    assert outcome["reason_code"] == "period_mismatch"

    explained = {**record, "cross_period_reason": "同比增长率，跨 2024→2025 比较"}
    assert verify_calculation(explained, frozen)["status"] == "verified"


def test_needs_review_input_taints_result():
    """待核验输入不能靠"算对了"洗白。"""
    frozen = [_rec("a", "10", status="needs_review"), _rec("b", "4")]
    outcome = verify_calculation({
        "formula": "a/b", "input_record_ids": ["a", "b"], "value": "2.5",
    }, frozen)
    assert outcome["status"] == "needs_review"
    assert outcome["reason_code"] == "input_needs_review"


def test_declared_precision_is_enforced():
    frozen = [_rec("a", "100"), _rec("b", "3")]
    base = {"formula": "a/b", "input_record_ids": ["a", "b"], "precision": 4}
    assert verify_calculation({**base, "value": "33.3333"}, frozen)["status"] == "verified"
    assert verify_calculation({**base, "value": "33.33"}, frozen)["status"] == "rejected"


def test_error_codes_are_partitioned():
    """硬错误与待核验错误必须分属两个集合，不能混用（决定"修复"还是"保留待核验稿"）。"""
    assert not (HARD_ERROR_CODES & REVIEW_CODES)
    assert "input_not_frozen" in HARD_ERROR_CODES
    assert "input_needs_review" in REVIEW_CODES


# ------------------------------------------------- 位置别名（v1..vN）--


def _records_two() -> list[dict]:
    return [
        {"record_id": "t1:r01c01", "value": "1064339.75", "unit": "万元", "label": "短期借款",
         "period": "2025", "status": "verified"},
        {"record_id": "t1:r02c01", "value": "2444030.98", "unit": "万元", "label": "一年内到期的非流动负债",
         "period": "2025", "status": "verified"},
    ]


def test_positional_alias_v1_v2_makes_record_ids_usable():
    """记录 ID 里的 `:` / `@` / `-` 进不了表达式，所以必须靠 v1..vN 位置别名。

    真实试跑反馈：模型写 `v1+v2` 之前，任何"用 record_id 当变量名"的公式都会被词法拒掉。
    """
    verdict = verify_calculation(
        {"calculation_id": "c1", "formula": "v1+v2",
         "input_record_ids": ["t1:r01c01", "t1:r02c01"],
         "value": "3508370.73", "unit": "万元", "period": "2025", "precision": 2},
        _records_two(),
    )
    assert verdict["status"] == "verified", verdict
    assert verdict["computed"] == "3508370.73"


def test_positional_alias_survives_same_label_across_periods():
    """同名科目跨期：label 只能登记一次（会被静默覆盖），位置别名才分得清。"""
    records = [
        {"record_id": "metric:debt_ratio:2024", "value": "66.0533", "unit": "%", "label": "资产负债率",
         "period": "2024", "status": "derived"},
        {"record_id": "metric:debt_ratio:2025", "value": "64.8358", "unit": "%", "label": "资产负债率",
         "period": "2025", "status": "derived"},
    ]
    verdict = verify_calculation(
        {"calculation_id": "c2", "formula": "v1-v2",
         "input_record_ids": ["metric:debt_ratio:2024", "metric:debt_ratio:2025"],
         "value": "1.22", "unit": "个百分点", "period": "2025", "precision": 2,
         "cross_period_reason": "同比"},
        records,
    )
    assert verdict["status"] == "verified", verdict
    assert verdict["computed"] == "1.2175"


# ------------------------------------- 百分比口径与业务键解析（2026-10-18 真实试跑）--


def _ratio_records() -> list[dict]:
    return [
        {"record_id": "bal:r07c01", "value": "137227.58", "unit": "万元", "label": "应收账款",
         "period": "2025", "scope": "合并口径", "status": "verified"},
        {"record_id": "bal:r08c01", "value": "1251848.55", "unit": "万元", "label": "其他应收款",
         "period": "2025", "scope": "合并口径", "status": "verified"},
        {"record_id": "bal:r01c01", "value": "96717233.2", "unit": "万元", "label": "资产总计",
         "period": "2025", "scope": "合并口径", "status": "verified"},
    ]


def test_percent_declaration_matches_ratio_computation():
    """模型写 formula=(v1+v2)/v3、value="1.44"、unit="%"：复算得 0.01436。

    百分数只是**表示口径**（×100），不认它会让所有占比类计算永远 value_mismatch
    （2026-10-18 北投 financial c13/c14、debt 实测）。
    """
    verdict = verify_calculation({
        "calculation_id": "c13", "formula": "(v1+v2)/v3",
        "input_record_ids": ["bal:r07c01", "bal:r08c01", "bal:r01c01"],
        "value": "1.44", "unit": "%", "period": "2025", "precision": 2,
    }, _ratio_records())
    assert verdict["status"] == "verified", verdict
    assert verdict["computed"].startswith("1.4362")


def test_percent_tolerance_does_not_apply_to_other_units():
    verdict = verify_calculation({
        "calculation_id": "c14", "formula": "(v1+v2)/v3",
        "input_record_ids": ["bal:r07c01", "bal:r08c01", "bal:r01c01"],
        "value": "1.44", "unit": "倍", "period": "2025", "precision": 2,
    }, _ratio_records())
    assert verdict["status"] == "rejected" and verdict["reason_code"] == "value_mismatch"


def test_business_key_inputs_resolve_to_records():
    """`科目名@期间` 由系统解析成记录 ID —— 模型记不住 `report-...:r01c01` 这种编号。"""
    records = _ratio_records()
    verdict = verify_calculation({
        "calculation_id": "c1", "formula": "v1+v2",
        "input_record_ids": ["应收账款@2025", "其他应收款@2025"],
        "value": "1389076.13", "unit": "万元", "period": "2025",
        "scope": "合并口径", "precision": 2,
    }, records)
    assert verdict["status"] == "verified", verdict
    assert [item["record_id"] for item in verdict["inputs"]] == ["bal:r07c01", "bal:r08c01"]


def test_business_key_prefers_declared_scope():
    """同名同期在合并/母公司两套报表里都有 → 按计算自己声明的口径筛。"""
    records = _ratio_records() + [
        {"record_id": "bal:r07c01-parent", "value": "50000", "unit": "万元", "label": "应收账款",
         "period": "2025", "scope": "母公司口径", "status": "verified"},
    ]
    verdict = verify_calculation({
        "calculation_id": "c1", "formula": "v1",
        "input_record_ids": ["应收账款@2025"],
        "value": "137227.58", "unit": "万元", "period": "2025",
        "scope": "合并口径", "precision": 2,
    }, records)
    assert verdict["status"] == "verified", verdict
    assert verdict["inputs"][0]["record_id"] == "bal:r07c01"


def test_business_key_without_period_is_ambiguous():
    records = _ratio_records() + [
        {"record_id": "bal:r07c04", "value": "100000", "unit": "万元", "label": "应收账款",
         "period": "2024", "scope": "合并口径", "status": "verified"},
    ]
    verdict = verify_calculation({
        "calculation_id": "c1", "formula": "v1",
        "input_record_ids": ["应收账款"],
        "value": "137227.58", "unit": "万元", "precision": 2,
    }, records)
    assert verdict["status"] == "needs_review" and verdict["reason_code"] == "input_ambiguous"


def test_unknown_business_key_is_still_not_frozen():
    verdict = verify_calculation({
        "calculation_id": "c1", "formula": "v1",
        "input_record_ids": ["虚构科目@2025"],
        "value": "1", "unit": "万元",
    }, _ratio_records())
    assert verdict["status"] == "rejected" and verdict["reason_code"] == "input_not_frozen"
