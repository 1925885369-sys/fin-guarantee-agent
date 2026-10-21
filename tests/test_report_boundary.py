"""报告边界安全（``workflow._report_boundary_safe``）的语境判定。

背景：旧实现按**裸子串**拦截，中邮反担保章因此被误退——正文写
"投资者认购本期债券即视为同意担保函全部条款"（复述募集说明书条款），
命中禁止词"同意担保"（它只是"同意担保函"的前缀）。
修订稿 §3.1 要求按完整语境判定，不得退化成固定窗口共现。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.workflow import _report_boundary_safe


def test_guarantee_letter_clause_is_not_our_decision():
    """复述材料条款 → 放行（真实误退回归）。"""
    content = (
        "担保人已就本期债券的本息偿付出具无条件不可撤销的担保函，"
        "投资者认购本期债券即视为同意担保函全部条款并接受担保函项下全部权利与义务（E09）。"
    )
    assert _report_boundary_safe(content) is True


def test_disclosure_wording_around_forbidden_word_is_allowed():
    """「募集说明书载明…」类引用 → 放行。"""
    content = "募集说明书载明，同意担保函适用中华人民共和国法律法规及部门规章。"
    assert _report_boundary_safe(content) is True


def test_our_own_guarantee_conclusion_is_still_blocked():
    """我方**已作出**的承保结论 → 必须拦截（负例，防止放宽过头）。

    ⚠️ "建议提供担保/建议拒绝担保"是**建议**（《风控约束分层》§一 允许承保、拒保建议），
    已从禁止表移除，属下方 `test_underwriting_recommendations_are_allowed` 的正例。
    """
    for content in (
        "同意担保该笔业务",
        "本所同意担保本次发行",
        "审批通过本期债券",
        "核定担保额度为5亿元",
        "审批倾向为通过",
    ):
        assert _report_boundary_safe(content) is False, content


def test_underwriting_recommendations_are_allowed():
    """方案 §一：允许承保、拒保、额度与费率**建议**——只是提议，须人工复核。"""
    for content in (
        "建议提供担保",
        "建议拒绝担保",
        "建议通过担保，待人工复核",
        "建议核定担保额度为5亿元（提议）",
        "建议担保费率为1.2%，需复核",
    ):
        assert _report_boundary_safe(content) is True, content


def test_material_shortage_statement_is_allowed():
    content = "截至本报告出具日，未取得与本期债券反担保相关的担保合同、权证，材料不足。"
    assert _report_boundary_safe(content) is True


def test_decision_marker_in_a_later_clause_does_not_pardon_an_earlier_one():
    """分句隔离：决策记号只作用于**同一分句**，跨句不豁免。"""
    content = "同意担保本次发行。另需说明，募集说明书载明担保函条款。"
    assert _report_boundary_safe(content) is False


def test_future_or_pending_wording_is_not_a_made_decision():
    """待办 vs 已决定：「再行/另行/届时」紧邻决策词时是**尚未发生**的动作。

    2026-10-21 中邮 financial 实测：模型写「材料并完成核对后，再行确定担保额度与担保费率；
    在此之前，不建议将本章数据作为核定担保额度或费率的唯一依据」，被"确定/核定"判成
    forbidden（`chapter_guard._decision_classification`）与越权结论（本函数），整章回退。
    """
    for content in (
        "材料补齐后再行核定担保额度；在此之前不建议使用。",
        "待取得担保合同后另行核定担保费率。",
        "届时确定担保额度，需人工复核。",
    ):
        assert _report_boundary_safe(content) is True, content

    # 反例：已经作出的决定不能被"后续"二字洗白——同一分句里出现决策主语即拦截。
    assert _report_boundary_safe("本所审核意见：核定担保额度为5亿元。") is False
    assert _report_boundary_safe("同意担保，后续再行确定担保额度。") is False
