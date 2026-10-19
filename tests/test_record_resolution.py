"""记录索引与科目名解析（2026-10-18 北投真实试跑驱动）。

那一轮 financial/cashflow/debt 三章连续回退，逐条核对后确认**不是模型算错**：

1. `_record_index` 逐单元格列，80 条上限被系统表 56 行占满，
   上传表的明细科目（`一年内到期的非流动负债`/`加：其他收益`/`投资收益…`）一条都进不去
   → 模型只能拿系统表里的「流动负债合计」去凑 → 复算必然不符；
2. `resolve_input_token` 精确匹配 label，模型写「其他收益」而材料写「加：其他收益」
   → `input_not_frozen`；
3. `MODEL_METRIC_KEYS` 没有 `name`，模型不知道 `cash_profit_ratio` 就是
   「经营现金流净额与净利润比值」→ 拿 fact 去除以它 → 量纲错。

本文件守住这三条。真实数据验证见 2026-10-18 日志：按科目名重算后
4/4 全部 verified，且复算值与模型声明完全一致。
"""

from backend.app.calculations import normalize_label_for_match, resolve_input_token
from backend.app.chapter_guard import _compact_periods, _record_index
from backend.app.llm import MODEL_METRIC_KEYS


def _rec(record_id, label, *, period="2025", unit="万元", rtype="table",
         value="1", scope="合并口径", status="verified"):
    return {
        "record_id": record_id, "record_type": rtype, "label": label,
        "period": period, "unit": unit, "value": value, "scope": scope,
        "status": status,
    }


# ------------------------------------------------------------ 科目名归一化 --


def test_statement_prefixes_are_stripped():
    assert normalize_label_for_match("加：其他收益") == "其他收益"
    assert normalize_label_for_match("其中：流动资产合计") == "流动资产合计"
    assert normalize_label_for_match("减：营业成本") == "营业成本"
    assert normalize_label_for_match("（一）货币资金") == "货币资金"


def test_parenthetical_note_is_stripped_but_plain_brackets_kept():
    """只去带「填列/亏损/损失」的尾部括号说明，普通括号保留（否则两个科目会被合并）。"""
    assert normalize_label_for_match("投资收益（损失以“-”号填列）") == "投资收益"
    assert normalize_label_for_match("短期借款（银行）") == "短期借款（银行）"


def test_numeric_prefix_requires_separator():
    """数字前缀必须带顿号或点号 —— 「一年内到期的非流动负债」不能被截断。"""
    assert normalize_label_for_match("一年内到期的非流动负债") == "一年内到期的非流动负债"
    assert normalize_label_for_match("一、营业收入") == "营业收入"


def test_whitespace_is_ignored():
    assert normalize_label_for_match(" 其他 收益 ") == "其他收益"


# ------------------------------------------------------- 输入 token 解析 --


def test_natural_label_resolves_to_prefixed_record():
    records = [_rec("t:1", "加：其他收益")]
    assert resolve_input_token("其他收益@2025", records) == ["t:1"]


def test_exact_match_wins_over_normalized():
    """精确命中优先：否则归一化会把两个本来不同的科目混成一个。"""
    records = [_rec("t:1", "其他收益"), _rec("t:2", "加：其他收益")]
    assert resolve_input_token("其他收益@2025", records) == ["t:1"]


def test_record_id_still_resolves():
    records = [_rec("t:1", "加：其他收益")]
    assert resolve_input_token("t:1", records) == ["t:1"]


def test_period_must_match():
    records = [_rec("t:1", "其他收益", period="2024")]
    assert resolve_input_token("其他收益@2025", records) == []


def test_scope_disambiguates_same_label_and_period():
    records = [_rec("t:1", "其他收益", scope="合并口径"),
               _rec("t:2", "其他收益", scope="母公司口径")]
    assert resolve_input_token("其他收益@2025", records, scope="合并口径") == ["t:1"]


def test_unknown_label_returns_empty_not_guess():
    assert resolve_input_token("不存在的科目@2025", [_rec("t:1", "货币资金")]) == []


# ------------------------------------------------------------- 记录索引 --


def test_index_aggregates_same_label_across_periods():
    records = [_rec(f"t:{year}", "短期借款", period=str(year)) for year in (2023, 2024, 2025)]
    index = _record_index(records)
    assert len(index) == 1
    assert index[0]["label"] == "短期借款"
    assert index[0]["periods"] == "2023-2025"


def test_index_excludes_disclosure_fragments():
    """disclosure 的 label 是引文片段（「合计」「其中」），不是规范科目名。"""
    records = [_rec("d:1", "合计", rtype="disclosure"), _rec("t:1", "短期借款")]
    assert [item["label"] for item in _record_index(records)] == ["短期借款"]


def test_index_drops_records_without_year_period():
    records = [_rec("t:1", "1年以内（含1年）", period=""), _rec("t:2", "货币资金")]
    assert [item["label"] for item in _record_index(records)] == ["货币资金"]


def test_index_puts_multi_period_subjects_first():
    """按 label 字典序排会把「短期借款」挤到末尾被截断，必须多期优先。"""
    records = [_rec("t:1", "AAA单期", period="2025")]
    records += [_rec(f"t:{year}", "ZZZ三期", period=str(year)) for year in (2023, 2024, 2025)]
    assert _record_index(records)[0]["label"] == "ZZZ三期"


def test_index_marks_multiple_scopes():
    records = [_rec("t:1", "短期借款", scope="合并口径"),
               _rec("t:2", "短期借款", scope="母公司口径")]
    assert _record_index(records)[0]["scopes"] == "合并口径/母公司口径"


def test_index_skips_conflict_records():
    records = [_rec("t:1", "短期借款", status="conflict")]
    assert _record_index(records) == []


def test_index_keeps_all_subjects_when_budget_allows():
    """覆盖度是这条修复的核心：明细科目不能被系统表挤掉。"""
    records = [_rec(f"sys:{i}", f"系统表科目{i}") for i in range(60)]
    records += [_rec("t:1", "一年内到期的非流动负债"), _rec("t:2", "加：其他收益")]
    labels = {item["label"] for item in _record_index(records)}
    assert {"一年内到期的非流动负债", "加：其他收益"} <= labels


# ------------------------------------------------------- 期间压缩辅助 --


def test_compact_periods_ranges_and_lists():
    assert _compact_periods(["2023", "2024", "2025"]) == "2023-2025"
    assert _compact_periods(["2023", "2025"]) == "2023,2025"
    assert _compact_periods(["2025"]) == "2025"
    assert _compact_periods([]) == ""


# --------------------------------------------------------- 指标投影字段 --


def test_metric_projection_includes_chinese_name():
    """没有中文名，模型会以为系统没算这个指标而重复造轮子（cashflow 三条栽在这里）。"""
    assert "name" in MODEL_METRIC_KEYS
