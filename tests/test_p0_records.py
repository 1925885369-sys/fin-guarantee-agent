"""p0 后置复查：数字按**该章冻结结构化记录**绑定的专项测试。

对应《第三点细化方案PLAN.md》§二.2：
- 数字先按千分位/符号/单位归一，再匹配**同一记录**的科目/期间/主体/口径；
- 禁止后置检查重新采用"整页数值池"；evidence_text 只支持无数字主张；
- 归属不明 → ``needs_review``（独立指标，不得自动算支持）；
- 核验要求句本身不算事实，但句中所附数字仍单独校验；
- 不存在的"材料不足/待核验即放行"路径。
"""
import uuid

import pytest

from backend.app.database import connect, json_dump, now_iso, rows
from backend.app.p0 import assess_section_claims, _frozen_records, _support_context, report_quality, validate_report_support


@pytest.fixture(autouse=True)
def _ensure_schema():
    """触库用例前确保 schema 已建立（与 test_evidence_records 同模式），
    避免本文件排在批内首个时"no such table: projects"。"""
    from backend.app.main import startup

    startup()


def _project(company: str = "记录绑定测试企业") -> str:
    project_id = f"test-p0rec-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", company, 500, 12, "流动资金", "review", now_iso(), now_iso(), now_iso()),
        )
    return project_id


def _cleanup(project_id: str) -> None:
    tables = (
        "evidence_edges", "report_claims", "gold_annotations", "financial_tie_outs", "project_entities",
        "audit_events", "candidate_findings", "review_tasks", "policy_snapshots", "financial_metrics",
        "canonical_facts", "fact_assertions", "cross_checks", "risks", "report_sections", "citations",
        "run_nodes", "runs", "facts", "sources", "documents",
    )
    with connect() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def _fixture(project_id: str, text: str) -> str:
    """建一条可定位来源 + 引用，返回 citation id。"""
    citation_id = f"citation-{uuid.uuid4()}"
    document_id = f"doc-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,ocr_status,page_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, project_id, "财务报表.pdf", "财务报表", "application/pdf", "hash", 100,
             "parsed", 1, 1, "", now_iso(), "native", "not_required", 1),
        )
        source_id = f"source-{uuid.uuid4()}"
        conn.execute(
            "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, project_id, document_id, "pdf_page", "第 1 页", text, 1, json_dump([0, 0, 1, 1]), "native", f"hash-{source_id}"),
        )
        conn.execute(
            "INSERT INTO citations (id,project_id,source_id,document_id,label,quote,locator_type,locator) VALUES (?,?,?,?,?,?,?,?)",
            (citation_id, project_id, source_id, document_id, "财务报表.pdf · 第 1 页", text, "pdf_page", "第 1 页"),
        )
    return citation_id


def _record(label: str, period: str, value: str, unit: str = "万元", subject: str = "记录绑定测试企业",
            scope: str = "合并口径", **overrides) -> dict:
    record = {
        "record_id": f"cell:r01c01@{uuid.uuid4().hex[:6]}", "value": value, "unit": unit, "label": label,
        "period": period, "subject": subject, "scope": scope, "source_ids": ["source-1"],
        "page": 1, "verification": "ok",
    }
    record.update(overrides)
    return record


def _section(project_id: str, content: str, citation_id: str, records: list[dict],
             *, section_key: str = "financial", section_id: str = "") -> str:
    section_id = section_id or f"section-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at,table_records_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (section_id, project_id, section_key, "财务分析", content, json_dump([citation_id]), "draft", now_iso(),
             json_dump(records)),
        )
    return section_id


def _status(project_id: str, section_id: str) -> dict[str, str]:
    return {
        item["claim_text"].rstrip("。").strip(): item["support_status"]
        for item in rows(
            "SELECT claim_text,support_status FROM report_claims WHERE project_id=? AND section_id=?",
            (project_id, section_id),
        )
    }


def _find(statuses: dict[str, str], needle: str) -> str:
    """按片段定位唯一主张的状态（``_claim_lines`` 会剥掉句首数字，键名不总是完整句）。"""
    matches = [value for key, value in statuses.items() if needle in key]
    assert len(matches) == 1, (needle, list(statuses))
    return matches[0]


def test_number_bound_to_same_frozen_record_with_matching_context_is_supported():
    """正例：数字命中冻结记录、科目/期间一致 → supported（且千分位/尾零归一）。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "2025年营业收入1234万元，净利润10万元。")
        section_id = _section(project_id, "2025年营业收入为1,234万元。", citation, [
            _record("营业收入", "2025", "1234.00", source_ids=["s-1"]),
            _record("净利润", "2025", "10.00"),
        ])
        valid = validate_report_support(project_id)
        assert valid["unsupported_claims"] == 0
        assert valid["needs_review_claims"] == 0
        assert valid["supported_claims"] == 1
        quality = report_quality(project_id)
        assert quality["unsupported_claims"] == 0 and quality["needs_review_claims"] == 0
        assert quality["unsupported_numeric_claims"] == 0
        assert _find(_status(project_id, section_id), "营业收入为1,234万元") == "direct"
    finally:
        _cleanup(project_id)


def test_number_only_in_page_text_not_in_frozen_records_is_unsupported():
    """负例：数字只在页面原文（evidence_text）里、不在冻结记录里 → unsupported，禁用整页数值池。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "2025年营业收入100万元，净利润10万元。")
        # 冻结记录里只有 200，页面原文里的 100 不得作为放行依据。
        section_id = _section(project_id, "2025年营业收入为100万元。", citation, [
            _record("营业收入", "2025", "200"),
        ])
        valid = validate_report_support(project_id)
        assert valid["unsupported_claims"] == 1 and valid["supported_claims"] == 0
        assert _find(_status(project_id, section_id), "营业收入为100万元") == "unsupported"
        assert report_quality(project_id)["status"] == "warning"
    finally:
        _cleanup(project_id)


def test_context_naming_another_subject_is_not_supported():
    """负例：数字命中记录，但正文把它写在**另一个科目**下 → label_mismatch，不通过。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "2025年经营活动现金流量净额300万元，投资活动现金流量净额400万元。")
        section_id = _section(project_id, "2025年投资活动现金流量净额为300万元。", citation, [
            _record("经营活动现金流量净额", "2025", "300"),
            _record("投资活动现金流量净额", "2025", "400"),
        ])
        valid = validate_report_support(project_id)
        assert valid["unsupported_claims"] == 1
        assert _find(_status(project_id, section_id), "投资活动现金流量净额为300万元") == "unsupported"
    finally:
        _cleanup(project_id)


def test_context_naming_another_period_is_not_supported():
    """负例：期间错配（2024 年末值写成 2025 年末）→ period_mismatch，不通过。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "2024年末应付债券100万元，2025年末应付债券120万元。")
        section_id = _section(project_id, "应付债券2025年末为100万元。", citation, [
            _record("应付债券", "2024", "100"),
            _record("应付债券", "2025", "120"),
        ])
        valid = validate_report_support(project_id)
        assert valid["unsupported_claims"] == 1
        assert _find(_status(project_id, section_id), "应付债券2025年末为100万元") == "unsupported"
    finally:
        _cleanup(project_id)


def test_ambiguous_attribution_becomes_needs_review_not_supported():
    """数字命中但归属不唯一（同值多记录、语境无线索）→ needs_review，独立计数、不算支持。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "本期披露金额100万元。")
        section_id = _section(project_id, "本期金额为100万元。", citation, [
            _record("营业收入", "2025", "100"),
            _record("净利润", "2024", "100"),
        ])
        valid = validate_report_support(project_id)
        assert valid["needs_review_claims"] == 1
        assert valid["unsupported_claims"] == 0 and valid["supported_claims"] == 0
        assert valid["status"] == "warning"
        assert valid["needs_review_claim_items"] and valid["needs_review_claim_items"][0]["section_key"] == "financial"
        assert _find(_status(project_id, section_id), "本期金额为100万元") == "needs_review"
        quality = report_quality(project_id)
        assert quality["needs_review_claims"] == 1
        assert quality["needs_review_claim_items"][0]["claim_text"].rstrip("。") == "本期金额为100万元"
        assert quality["supported_claims"] == 0 and quality["status"] == "warning"
    finally:
        _cleanup(project_id)


def test_unit_conversion_is_accepted_when_record_matches():
    """等值单位归一：记录 2 亿元 ≡ 正文 20000 万元。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "2025年营业收入2亿元。")
        section_id = _section(project_id, "2025年营业收入为20000万元。", citation, [
            _record("营业收入", "2025", "2", unit="亿元"),
        ])
        validate_report_support(project_id)
        assert _find(_status(project_id, section_id), "营业收入为20000万元") == "direct"
    finally:
        _cleanup(project_id)


def test_requirement_sentence_with_attached_number_is_still_validated():
    """核验要求句本身不算事实，但句中附带的可核验数字仍单独进事实校验。"""
    from backend.app.p0 import _claim_type

    # 带数字的"建议/应"句不再被归为 requirement，而是按业务事实照常判定。
    assert _claim_type("建议核对应收账款500万元") != "requirement"
    project_id = _project()
    try:
        citation = _fixture(project_id, "2025年营业收入100万元。")
        section_id = _section(
            project_id,
            "建议核对2025年营业收入100万元。建议核对应收账款500万元。",
            citation,
            [_record("营业收入", "2025", "100")],
        )
        validate_report_support(project_id)
        statuses = _status(project_id, section_id)
        # 句中所附数字有记录 → 通过；无记录（500）→ 不因是"建议"句而被豁免。
        assert _find(statuses, "营业收入100万元") == "direct"
        assert _find(statuses, "应收账款500万元") == "unsupported"
    finally:
        _cleanup(project_id)


def test_material_insufficient_wording_does_not_exempt_unbacked_number():
    """'材料不足' 不能豁免数字校验：数字无冻结记录支持时仍判 unsupported。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "2025年营业收入100万元。")
        section_id = _section(project_id, "材料不足，2025年营业收入为500万元。", citation, [
            _record("营业收入", "2025", "100"),
        ])
        valid = validate_report_support(project_id)
        assert valid["unsupported_claims"] == 1
        assert _status(project_id, section_id)["材料不足，2025年营业收入为500万元"] == "unsupported"
    finally:
        _cleanup(project_id)


def test_frozen_records_are_read_from_stored_section_when_not_passed_in():
    """冻结输入读取：section 未携带记录时，按 (project_id, section_key) 读该章已存记录。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "2025年营业收入100万元。")
        _section(project_id, "2025年营业收入为100万元。", citation, [_record("营业收入", "2025", "100")])
        records = _frozen_records(project_id, {"section_key": "financial"})
        assert records and records[0]["label"] == "营业收入"
        citation_map, source_texts, valid_sources = _support_context(project_id)
        probe = {"section_key": "financial", "content": "2025年营业收入为100万元。", "citation_ids": [citation]}
        assessment = assess_section_claims(project_id, probe, citation_map, source_texts, valid_sources)
        assert assessment["supported"] == 1 and assessment["unsupported_claims"] == 0
    finally:
        _cleanup(project_id)


def test_needs_review_status_is_persisted_in_report_claims():
    """report_claims 可写 support_status='needs_review'（供并行线做交付阻断）。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "本期披露金额100万元。")
        _section(project_id, "本期金额为100万元。", citation, [
            _record("营业收入", "2025", "100"),
            _record("净利润", "2024", "100"),
        ])
        validate_report_support(project_id)
        stored = rows(
            "SELECT support_status FROM report_claims WHERE project_id=? AND support_status='needs_review'",
            (project_id,),
        )
        assert len(stored) == 1
    finally:
        _cleanup(project_id)
