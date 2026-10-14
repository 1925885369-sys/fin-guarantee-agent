"""校验器三态与共用主张判定（第三点细化方案 §二.2、修订稿 §3.1）。

覆盖团队冻结的接口与语义：

- 三态 ``passed / needs_review / rejected`` 与 ``needs_review_items`` 结构；
- 数字→**同一条记录**四维绑定；跨记录拼接 → ``needs_review``；
- 展示精度按记录原始小数位以 ``ROUND_HALF_UP`` 匹配；
- 裸数字归属不明 → ``ambiguous_attribution``；
- 主体"后缀相似"不再放行（近名公司要拦），登记简称仍过；
- 中期/另一口径"有记录即过、无记录即拒"，两条一律禁令撤销；
- "5.18 个百分点"类自行计算结果必须拒绝；
- ``generation_mode=generate`` 时草稿数字不进允许集。
"""
from __future__ import annotations

from decimal import Decimal

from backend.app.chapter_guard import (
    _entity_is_allowed, _normalize_number, _party_compatible, _round_half_up, _values_match,
    validate_chapter_candidate, validate_chapter_output,
)
from backend.app.chapter_prompts import load_chapter_config

FROZEN_KEYS = (
    "status", "reasons", "safety_reasons", "invalid_citations", "length", "length_only_failure",
    "unexpected_numbers", "attribution_conflicts", "safe_unit_conversions",
    "ignored_format_numbers", "page_numbers_ignored", "forbidden_matches",
    "needs_review_items", "needs_review_count",
)


def _ctx(key: str, *, has_missing: bool = False, **over) -> dict:
    config = load_chapter_config(key)
    context = {
        "chapter_key": key,
        "prompt_config": config,
        "prompt_version": "2.1.0",
        "prompt_sha256": "prompt-hash",
        "draft": "据募集说明书披露，材料不足。",
        "facts": [], "metrics": [], "cross_checks": [],
        "evidence": [{
            "id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书", "quote": "无",
        }],
        "conflicts": [], "conflict_block": "", "has_missing": has_missing,
        "allowed_citation_ids": ["c-source-1"],
    }
    context.update(over)
    return context


def _record(value, **over) -> dict:
    record = {
        "value": value, "unit": "万元", "label": "应付债券", "period": "2025",
        "subject": "华辰精密制造有限公司", "scope": "合并口径",
        "source_ids": ["s-1"], "page": 1,
    }
    record.update(over)
    return record


# --------------------------------------------------------------- 冻结接口 --

def test_frozen_interface_alias_and_keys():
    """``validate_chapter_candidate`` 是同一可调用对象；全部旧键 + 新键齐备。"""
    assert validate_chapter_candidate is validate_chapter_output
    result = validate_chapter_output("据募集说明书披露，材料不足。", ["c-source-1"], _ctx("debt"))
    for key in FROZEN_KEYS:
        assert key in result, key
    assert result["status"] in {"passed", "needs_review", "rejected"}
    assert isinstance(result["needs_review_items"], list)
    assert isinstance(result["needs_review_count"], int)


# ------------------------------------------------------------- 三态与结构 --

def test_three_states_and_needs_review_item_shape():
    # ① passed：无数字、无违规
    passed = validate_chapter_output("据募集说明书披露，材料不足。", ["c-source-1"], _ctx("debt"))
    assert passed["status"] == "passed"
    assert passed["needs_review_items"] == [] and passed["needs_review_count"] == 0

    # ② rejected：新数字是硬失败，不是待核验
    rejected = validate_chapter_output(
        "据募集说明书披露，新增金额9999万元；材料不足。", ["c-source-1"], _ctx("debt"))
    assert rejected["status"] == "rejected"
    assert rejected["needs_review_items"] == []

    # ③ needs_review：裸数字在多个单位下出现 → 归属不明
    context = _ctx("debt", table_records=[
        _record("100", unit="万元", label="应付债券"),
        _record("100", unit="%", label="占比"),
    ])
    review = validate_chapter_output("据募集说明书披露，合计为100；材料不足。", ["c-source-1"], context)
    assert review["status"] == "needs_review"
    assert review["reasons"] == []
    assert review["needs_review_count"] == len(review["needs_review_items"]) == 1
    item = review["needs_review_items"][0]
    assert set(item) == {"code", "excerpt", "position", "detail"}
    assert item["code"] == "ambiguous_attribution"
    assert isinstance(item["position"], int) and item["position"] >= 0
    assert isinstance(item["detail"], str) and item["detail"]
    assert "100" in item["excerpt"]


# ------------------------------------------------------- 同记录四维 vs 拼接 --

def test_same_record_binding_vs_cross_record_stitching():
    """四维必须由**同一条记录**同时满足；拼接只能进 needs_review。"""
    context = _ctx("debt", allowed_periods=["2024", "2025"], table_records=[
        _record("300", label="经营活动现金流量净额", period="2024"),
        _record("300", label="投资活动现金流量净额", period="2025"),
        _record("999", label="筹资活动现金流量净额", period="2025"),
    ])
    # 同一条记录同时满足"科目=投资 + 期间=2025" → 通过
    ok = validate_chapter_output(
        "据募集说明书披露，投资活动现金流量净额2025年为300万元；材料不足。", ["c-source-1"], context)
    assert ok["status"] == "passed"
    assert not [reason for reason in ok["reasons"] if "不匹配" in reason]

    # 科目来自记录 B、期间来自记录 A —— 拼接 → needs_review（不得通过、也不轻易 rejected）
    stitched = validate_chapter_output(
        "据募集说明书披露，投资活动现金流量净额2024年为300万元；材料不足。", ["c-source-1"], context)
    assert stitched["status"] == "needs_review"
    assert not [reason for reason in stitched["reasons"] if "不匹配" in reason]
    assert any(item["code"] == "record_binding_ambiguous" for item in stitched["needs_review_items"])

    # 某维对**所有**同值记录都不成立 → 真实错误 rejected
    mismatch = validate_chapter_output(
        "据募集说明书披露，筹资活动现金流量净额为300万元；材料不足。", ["c-source-1"], context)
    assert mismatch["status"] == "rejected"
    assert any("科目不匹配" in reason for reason in mismatch["reasons"])


def test_shopping_cash_flow_written_as_investing_is_rejected():
    """现金流科目登记表：经营/投资/筹资用别名区分，错配即 reject。"""
    context = _ctx("cashflow", table_records=[
        _record("751716.86", label="经营活动现金流量净额"),
        _record("751716.86", label="投资活动现金流量净额"),
        _record("600000", label="筹资活动现金流量净额"),
    ])
    bad = validate_chapter_output(
        "据募集说明书披露，投资活动现金流量净额为751,716.86万元；材料不足。", ["c-source-1"], context)
    assert bad["status"] == "passed"
    # "筹资"科目写成该值（同值记录只有经营/投资）→ 对全体记录都不成立 → reject
    wrong = validate_chapter_output(
        "据募集说明书披露，筹资活动现金流量净额为751,716.86万元；材料不足。", ["c-source-1"], context)
    assert wrong["status"] == "rejected"
    assert any("科目不匹配" in reason for reason in wrong["reasons"])


# ------------------------------------------------------------- 展示精度 --

def test_round_half_up_is_not_bankers_rounding():
    assert _round_half_up(Decimal("2.345"), 2) == Decimal("2.35")   # half-up；half-even 会得 2.34
    assert _round_half_up(Decimal("2.355"), 2) == Decimal("2.36")


def test_display_precision_matches_by_record_decimal_places():
    # 记录存 4 位小数、报告按 2 位小数展示（ROUND_HALF_UP）→ 命中
    assert _values_match(Decimal("9.89"), 2, Decimal("9.8901"), 4)
    assert _values_match(Decimal("58.94"), 2, Decimal("58.9354"), 4)
    # 舍入后仍不同 → 不命中（无任意误差容忍）
    assert not _values_match(Decimal("9.79"), 2, Decimal("9.8901"), 4)
    # 归一化只做无损处理，不固定两位
    assert _normalize_number("9.8901") == "9.8901"
    assert _normalize_number("70.0") == "70"


def test_end_to_end_precision_accepted_and_rejected():
    context = _ctx("debt", metrics=[{
        "metric_key": "ratio", "period": "2025", "value": 9.8901, "unit": "%", "status": "calculated",
    }])
    ok = validate_chapter_output("据募集说明书披露，该指标为9.89%；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in ok["reasons"] if "新数字" in reason]
    bad = validate_chapter_output("据募集说明书披露，该指标为9.79%；材料不足。", ["c-source-1"], context)
    assert [reason for reason in bad["reasons"] if "新数字" in reason]


# ----------------------------------------------------------- 裸数字归属 --

def test_bare_number_with_unique_record_passes():
    context = _ctx("debt", table_records=[_record("4405922.45", label="资产总计")])
    ok = validate_chapter_output(
        "据募集说明书披露，资产总计为4405922.45万元；材料不足。", ["c-source-1"], context)
    assert ok["status"] == "passed"


def test_bare_number_without_unique_attribution_needs_review():
    context = _ctx("debt", table_records=[
        _record("100", unit="万元", label="应付债券"),
        _record("100", unit="万元", label="短期借款"),
    ])
    result = validate_chapter_output("据募集说明书披露，其金额为100；材料不足。", ["c-source-1"], context)
    assert result["status"] == "needs_review"
    assert result["needs_review_items"][0]["code"] == "ambiguous_attribution"
    # 同一句里写清科目后即唯一 → 不再是待核验
    clear = validate_chapter_output(
        "据募集说明书披露，应付债券为100万元；材料不足。", ["c-source-1"], context)
    assert clear["status"] in {"passed", "needs_review"}
    assert not [reason for reason in clear["reasons"] if "新数字" in reason]


# -------------------------------------------------- 主体：后缀相似不放行 --

def test_suffix_similarity_no_longer_allows_near_name_company():
    # 近名公司：不再因共享后缀放行
    assert not _party_compatible("华辰新能源有限公司", "华辰精密制造有限公司")
    assert not _entity_is_allowed("华辰新能源有限公司", {"发行人华辰精密制造有限公司"})
    # 全等 / 包含 仍放行
    assert _party_compatible("华辰精密制造有限公司", "发行人华辰精密制造有限公司")
    assert _entity_is_allowed("华辰精密制造有限公司", {"发行人华辰精密制造有限公司"})
    # 登记简称：解析到同一规范名称
    registry = {
        "京投": "北京市基础设施投资有限公司",
        "北京市基础设施投资有限公司": "北京市基础设施投资有限公司",
    }
    assert _party_compatible("京投", "北京市基础设施投资有限公司", registry)
    assert not _party_compatible("京投", "华辰精密制造有限公司", registry)


def test_shared_legal_name_tail_allows_run_on_context_heads():
    """两侧 run-on 头部不同、但共享**完整法定名尾段（字号≥6字）**时按同一主体放行。

    中邮 rating 实测：引文侧"…5855 号/特此公告＋联合资信评估股份有限公司"、
    产出侧"评级结论根据＋联合资信评估股份有限公司"——包含关系不成立致误判"新主体"。
    """
    allowed = {"号联合资信评估股份有限公司通过对中邮证券有限责任公司", "特此公告联合资信评估股份有限公司"}
    assert _entity_is_allowed("评级结论根据联合资信评估股份有限公司", allowed)
    # 近名公司仍拦：公共尾段只有法定后缀（字号为空）。
    assert not _entity_is_allowed("华辰新能源有限公司", {"发行人华辰精密制造有限公司"})
    # 字号不足 6 字仍拦：浙/苏两省交投的公共尾段字号仅 5 字（"省交通投资"）。
    assert not _entity_is_allowed("浙江省交通投资集团有限公司", {"江苏省交通投资集团有限公司"})
    # 边界：共享字号恰为 6 字 → 放行（残边界：两地同字号公司按同一主体处理）。
    assert _entity_is_allowed("上海东方资产评估股份有限公司", {"北京东方资产评估股份有限公司"})
    # 法定后缀不同的排他性不受影响（有限责任公司 vs 有限公司 尾段不等即拦）。
    assert not _entity_is_allowed("杭州云和数据有限责任公司", {"杭州云和数据有限公司"})


def test_attribution_party_blocks_near_name_company():
    context = _ctx("debt", table_records=[_record("100", label="应付债券")])
    near = validate_chapter_output(
        "据募集说明书披露，华辰新能源有限公司应付债券为100万元；材料不足。", ["c-source-1"], context)
    assert near["status"] == "rejected"
    assert any("主体不匹配" in reason for reason in near["reasons"])


# -------------------------------------------- 中期/另一口径与两条禁令撤销 --

def test_interim_period_allowed_only_with_record():
    """中期期间：有记录即过，无记录即拒（不再"一律禁止"）。"""
    context = _ctx("cashflow", allowed_periods=["2025"])
    bad = validate_chapter_output(
        "据募集说明书披露，2025年半年度经营活动现金流为1200万元；材料不足。", ["c-source-1"], context)
    assert bad["status"] == "rejected"
    assert [reason for reason in bad["reasons"] if "新数字" in reason]
    context["table_records"] = [_record(
        "1200", label="经营活动现金流量净额", period="2025", period_type="interim")]
    good = validate_chapter_output(
        "据募集说明书披露，2025年半年度经营活动现金流为1200万元；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in good["reasons"] if "季度或半年度" in reason or "新数字" in reason]
    assert good["status"] in {"passed", "needs_review"}


def test_quarter_and_scope_blanket_prohibitions_are_removed():
    context = _ctx("cashflow")
    result = validate_chapter_output(
        "据募集说明书披露，不采用母公司口径，亦不引用季度数据；材料不足。", ["c-source-1"], context)
    assert not [reason for reason in result["reasons"] if "季度或半年度" in reason or "混入" in reason]


def test_project_prompt_limited_scope_is_enforced():
    context = _ctx("cashflow")
    context["prompt_config"] = {**load_chapter_config("cashflow"), "rules": ["本章仅采用合并口径分析。"]}
    result = validate_chapter_output(
        "据募集说明书披露，母公司口径下资产负债率为70%；材料不足。", ["c-source-1"], context)
    assert any("限定" in reason for reason in result["reasons"])


# --------------------------------------------------- 自行计算 / generate --

def test_self_computed_percentage_points_are_rejected():
    """"5.18 个百分点"是自行计算结果：个百分点作为独立单位，值不命中记录即拒。"""
    result = validate_chapter_output(
        "据募集说明书披露，资产负债率较上年下降5.18个百分点；材料不足。", ["c-source-1"], _ctx("debt"))
    assert result["status"] == "rejected"
    assert [reason for reason in result["reasons"] if "新数字" in reason]


def test_generate_mode_excludes_draft_from_allowed_numbers():
    """generate 模式：草稿不进允许集（草稿里的数字不能为正文背书）；rewrite 保持现状。"""
    generated = _ctx("debt")
    generated["draft"] = "据募集说明书披露，发行规模为20000万元；材料不足。"
    blocked = validate_chapter_output(
        "据募集说明书披露，发行规模为20000万元；材料不足。", ["c-source-1"], generated)
    assert [reason for reason in blocked["reasons"] if "新数字" in reason]

    rewrite = _ctx("bond_overview")
    rewrite["draft"] = "据募集说明书披露，拟议发行规模为20000万元；材料不足。"
    allowed = validate_chapter_output(
        "据募集说明书披露，拟议发行规模为20000万元；材料不足。", ["c-source-1"], rewrite)
    assert not [reason for reason in allowed["reasons"] if "新数字" in reason]
