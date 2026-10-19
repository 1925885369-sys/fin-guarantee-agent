"""B3 建议与复核：结构归一、草案渲染、未结构化检测、落库/失效/交付门/确认接口。

对应《风控约束分层》方案 §四 line 52~56。守三条最容易错的边界：
1. 建议是**提议**，不进 `evidence_records`（不能给正文其他数字背书）；
2. 正文写了"建议承保 5000 万元"却不返回 `recommendations` → 必须拦（否则绕过人工复核）；
3. 改正文后原确认必须失效（同一事务），未确认/失效都阻断最终交付。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app import recommendations as rec_rules
from backend.app import report_delivery
from backend.app.database import connect, execute, now_iso, row
from backend.app.main import app, startup

PROJECT_ID = "recommendations-test"


def _context(records=None, draft="毛利率为40%。建议承保，额度5000万元。"):
    return {"chapter_key": "financial", "evidence_records": records or [], "draft": draft}


def _frozen():
    return [{"record_id": "fact:rev", "record_type": "fact", "value": "1000", "unit": "万元",
             "label": "营业收入", "status": "verified", "source_ids": ["src-1"]}]


def _valid(**overrides):
    base = {
        "recommendation_id": "r1", "recommendation_type": "accept",
        "content": "建议承保本期债券", "basis": "主体评级 AA-，资产负债率 62%",
        "basis_record_ids": ["fact:rev"], "preconditions": ["反担保登记完成"],
        "amount_value": "5000", "amount_unit": "万元", "rate_value": "1.2", "rate_unit": "%",
        "content_anchor": "建议承保",
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------ 结构归一 --

def test_valid_recommendation_is_normalized_with_draft_text():
    result = rec_rules.normalize_and_validate([_valid()], _context(_frozen()))
    assert not result["hard"] and not result["review"]
    item = result["items"][0]
    assert item["recommendation_type"] == "accept"
    assert item["amount_value"] == "5000"
    # 草案文案必须写明"待人工确认 + 不代表审批"——这是方案 §四 line 54 的措辞
    assert "待人工确认" in item["draft_text"] and "不代表" in item["draft_text"]
    assert item["basis_hash"]


def test_type_aliases_are_normalized():
    for raw, expected in (("建议承保", "accept"), ("拒保", "reject"), ("担保额度", "guarantee_amount"),
                          ("担保费率", "guarantee_rate"), ("其他", "other")):
        result = rec_rules.normalize_and_validate(
            [_valid(recommendation_type=raw)], _context(_frozen()))
        assert result["items"][0]["recommendation_type"] == expected, raw


def test_structural_errors_are_hard():
    cases = [
        (_valid(content=""), "缺少内容"),
        (_valid(basis_record_ids=["fact:nope"]), "不存在的依据记录"),
        (_valid(amount_value="五千万"), "额度不是可解析数值"),
        (_valid(rate_value="1.2%折扣"), "费率不是可解析数值"),
    ]
    for payload, keyword in cases:
        result = rec_rules.normalize_and_validate([payload], _context(_frozen()))
        assert result["hard"], payload
        assert any(keyword in reason for reason in result["hard"]), result["hard"]


def test_missing_basis_records_is_review_not_hard():
    """依据未结构化时保留待核验稿，不硬拒（方案 §三.3 的口径）。"""
    result = rec_rules.normalize_and_validate(
        [_valid(basis_record_ids=[])], _context(_frozen()))
    assert not result["hard"]
    assert any(item["code"] == "recommendation_basis_unstructured" for item in result["review"])


def test_anchor_missing_is_review():
    result = rec_rules.normalize_and_validate(
        [_valid(content_anchor="正文没有这句")], _context(_frozen()))
    assert any(item["code"] == "recommendation_anchor_not_found" for item in result["review"])


def test_recommendation_cap():
    payload = [_valid(recommendation_id=f"r{i}") for i in range(10)]
    result = rec_rules.normalize_and_validate(payload, _context(_frozen()))
    assert len(result["items"]) == rec_rules.MAX_RECOMMENDATIONS


# ---------------------------------------------------------------- 未结构化检测 --

def test_unstructured_proposal_is_detected():
    """决策词 + 数字却无结构化建议 → 必须拦（否则绕过人工复核）。"""
    findings = rec_rules.detect_unstructured_proposals("建议承保本期债券，额度5000万元。", [])
    assert findings


def test_structured_anchor_covers_the_sentence():
    structured = [{"content_anchor": "建议承保本期债券"}]
    findings = rec_rules.detect_unstructured_proposals("建议承保本期债券，额度5000万元。", structured)
    assert not findings


def test_non_decision_advice_is_not_flagged():
    """'建议补充材料/建议关注治理'没有决策动作，不算承保建议。"""
    assert not rec_rules.detect_unstructured_proposals("建议补充材料，并建议关注治理结构。", [])


def test_basis_hash_changes_with_content():
    first = rec_rules.normalize_and_validate([_valid()], _context(_frozen()))["items"][0]
    second = rec_rules.normalize_and_validate(
        [_valid(content="建议承保本期债券，条件更严")], _context(_frozen()))["items"][0]
    assert first["basis_hash"] != second["basis_hash"]
    # 只改确认状态不该让哈希变
    assert rec_rules.basis_hash({**first, "status": "confirmed"}) == first["basis_hash"]


# ------------------------------------------------------------ 落库 / 交付门 --

def _prepare_project() -> None:
    startup()
    execute("DELETE FROM report_recommendations WHERE project_id=?", (PROJECT_ID,))
    execute("DELETE FROM report_sections WHERE project_id=?", (PROJECT_ID,))
    execute("DELETE FROM projects WHERE id=?", (PROJECT_ID,))
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,"
            "created_at,updated_at,consent_at,project_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (PROJECT_ID, "token-1", "建议测试", 500, 12, "补充流动性", "review",
             now_iso(), now_iso(), now_iso(), "bond_guarantee"),
        )


def _write_section(content: str, items=None) -> None:
    report_delivery.write_report_section(
        project_id=PROJECT_ID, section_key="financial", title="财务趋势与盈利质量",
        content=content, citation_ids=[], allowed_source_ids=["src-1"], content_origin="model",
        generation_status="model_passed", required_disclosures=[],
        length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
        support_status="pending", calculations=[], recommendations=items,
    )


def test_recommendations_are_written_and_superseded_on_regenerate():
    _prepare_project()
    try:
        first = rec_rules.normalize_and_validate([_valid()], _context(_frozen()))["items"]
        _write_section("毛利率为40%。建议承保，额度5000万元。", first)
        active = [dict(item) for item in _rows("SELECT * FROM report_recommendations "
                                               "WHERE project_id=? AND status!='superseded'", (PROJECT_ID,))]
        assert len(active) == 1 and active[0]["status"] == "pending"

        # 重新生成 → 旧行 superseded，新行 pending（确认不能继承）
        second = rec_rules.normalize_and_validate([_valid(content="建议承保，条件调整")],
                                                  _context(_frozen()))["items"]
        _write_section("毛利率为40%。建议承保，条件调整。", second)
        active = [dict(item) for item in _rows("SELECT * FROM report_recommendations "
                                               "WHERE project_id=? AND status!='superseded'", (PROJECT_ID,))]
        superseded = _rows("SELECT * FROM report_recommendations WHERE project_id=? AND status='superseded'",
                           (PROJECT_ID,))
        assert len(active) == 1 and len(superseded) == 1
    finally:
        _cleanup()


def test_unconfirmed_recommendation_blocks_final_delivery():
    _prepare_project()
    try:
        items = rec_rules.normalize_and_validate([_valid()], _context(_frozen()))["items"]
        _write_section("毛利率为40%。建议承保，额度5000万元。", items)
        blockers = report_delivery.delivery_blockers(PROJECT_ID)
        assert any("建议草案尚未人工确认" in item for item in blockers), blockers

        # 逐条确认后该条消失
        execute("UPDATE report_recommendations SET status='confirmed' WHERE project_id=?",
                (PROJECT_ID,))
        blockers = report_delivery.delivery_blockers(PROJECT_ID)
        assert not any("建议草案尚未人工确认" in item for item in blockers)
    finally:
        _cleanup()


def test_write_section_stales_confirmed_recommendations():
    """改正文 → 已确认建议必须失效（同一事务），否则旧确认给新正文背书。"""
    _prepare_project()
    try:
        items = rec_rules.normalize_and_validate([_valid()], _context(_frozen()))["items"]
        _write_section("毛利率为40%。建议承保，额度5000万元。", items)
        execute("UPDATE report_recommendations SET status='confirmed' WHERE project_id=?",
                (PROJECT_ID,))
        _write_section("毛利率为41%。建议承保，额度5000万元。", None)
        statuses = {dict(item)["status"] for item in
                    _rows("SELECT status FROM report_recommendations WHERE project_id=?", (PROJECT_ID,))}
        assert statuses == {"stale"}, statuses
    finally:
        _cleanup()


def test_recommendation_decision_endpoint_confirms_and_guards_superseded():
    _prepare_project()
    try:
        items = rec_rules.normalize_and_validate([_valid()], _context(_frozen()))["items"]
        _write_section("毛利率为40%。建议承保，额度5000万元。", items)
        record = row("SELECT id FROM report_recommendations WHERE project_id=?", (PROJECT_ID,))
        headers = {"X-Project-Token": "token-1"}
        with TestClient(app, client=("127.0.0.1", 42401)) as client:
            listed = client.get(f"/api/projects/{PROJECT_ID}/report-recommendations", headers=headers)
            assert listed.status_code == 200
            assert listed.json()["counts"]["pending"] == 1
            confirmed = client.patch(
                f"/api/projects/{PROJECT_ID}/report-recommendations/{record['id']}",
                headers=headers, json={"decision": "confirmed", "note": "复核通过"},
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["status"] == "confirmed"
            assert confirmed.json()["decision_scope"] == "report_adoption"

            execute("UPDATE report_recommendations SET status='superseded' WHERE id=?", (record["id"],))
            blocked = client.patch(
                f"/api/projects/{PROJECT_ID}/report-recommendations/{record['id']}",
                headers=headers, json={"decision": "confirmed"},
            )
            assert blocked.status_code == 409
    finally:
        _cleanup()


# -------------------------------------------------------------------- 助手 --

def _rows(query: str, params=()):
    with connect() as conn:
        return conn.execute(query, params).fetchall()


def _cleanup() -> None:
    execute("DELETE FROM report_recommendations WHERE project_id=?", (PROJECT_ID,))
    execute("DELETE FROM report_sections WHERE project_id=?", (PROJECT_ID,))
    execute("DELETE FROM audit_events WHERE project_id=?", (PROJECT_ID,))
    execute("DELETE FROM projects WHERE id=?", (PROJECT_ID,))


def test_planned_structure_statement_is_not_an_unstructured_proposal():
    """陈述**拟议担保结构**不是"未结构化建议"（2026-10-17 开 structured 前复核发现）。

    担保章通篇写"本次担保额度为 5 亿元，占净资产 12%"，那是披露事实；
    若按"决策词 + 数字"判，整章会被拒稿回退。
    """
    assert not rec_rules.detect_unstructured_proposals("本次担保额度为5亿元，占净资产的12%。", [])
    assert not rec_rules.detect_unstructured_proposals("担保费率为1.2%，按年收取。", [])
    assert not rec_rules.detect_unstructured_proposals("拟提供连带责任保证，担保额度不超过8亿元。", [])


def test_actual_proposal_with_number_is_flagged():
    assert rec_rules.detect_unstructured_proposals("建议承保，担保额度5000万元。", [])
    assert rec_rules.detect_unstructured_proposals("建议将担保费率定为1.2%。", [])
