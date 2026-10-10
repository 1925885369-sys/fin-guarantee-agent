from __future__ import annotations

from backend.app import bond_sections
from backend.app.bond_sections import build_section_extraction_plans, locate_document_sections
from backend.app.bond_tranches import deterministic_extract_tranches
from backend.app.config import settings
from backend.app.database import rows
from backend.app.facts import deterministic_extract
from backend.app.main import startup
from backend.app.real_reference import BEITOU_PROJECT_ID, REAL_PROJECT_ID
from backend.app.workflow import _deterministic_fact_is_issuer_safe, _model_fact_section_reason


def _sources(project_id: str, role: str) -> list[dict]:
    return rows(
        "SELECT s.*,d.document_role FROM sources s JOIN documents d ON d.id=s.document_id "
        "WHERE s.project_id=? AND d.document_role=? ORDER BY s.page,s.id",
        (project_id, role),
    )


def test_heading_locator_rejects_toc_and_body_mentions(monkeypatch):
    monkeypatch.setattr(bond_sections, "is_configured", lambda: False)
    sources = [
        {"id": "toc", "page": 9, "text": "第二节 发行条款................19\n第三节 募集资金运用................22\n第四节 发行人基本情况................30"},
        {"id": "body", "page": 16, "text": "具体发行安排详见第二节 发行条款。"},
        {"id": "terms", "page": 19, "text": "第二节 发行条款\n本期债券分为两个品种。"},
        {"id": "proceeds", "page": 22, "text": "第三节 募集资金运用\n募集资金用于补充流动资金。"},
        {"id": "financial", "page": 73, "text": "第五节 发行人财务状况"},
        {"id": "statements", "page": 77, "text": "二、发行人财务会计信息及主要财务指标"},
        {"id": "enhancement", "page": 127, "text": "第七节 增信情况\n本期债券无增信安排。"},
    ]

    located, _ = locate_document_sections(sources, "prospectus")
    by_role = {item.role: item for item in located}
    assert by_role["offering_terms"].start_page == 19
    assert by_role["use_of_proceeds"].start_page == 22
    assert by_role["financial_basis"].start_page == 73
    assert by_role["financial_statements"].start_page == 77


def test_end_anchor_caps_a_section_when_next_configured_heading_is_missing(monkeypatch):
    monkeypatch.setattr(bond_sections, "is_configured", lambda: False)
    sources = [
        {"id": "terms", "page": 1, "text": "第二节 发行条款\n本期债券发行安排。"},
        {"id": "terms-detail", "page": 2, "text": "本期发行规模不超过人民币20亿元。"},
        {"id": "proceeds", "page": 3, "text": "第三节 募集资金运用\n募集资金用于补充流动资金。"},
        {"id": "later", "page": 4, "text": "后续没有可识别的合同章节标题。"},
    ]

    located, _ = locate_document_sections(sources, "prospectus")
    offering = next(item for item in located if item.role == "offering_terms")

    assert offering.start_page == 1
    assert offering.end_page == 2


def test_issuer_section_rejects_subsidiary_and_wrong_scope_facts():
    located, _ = locate_document_sections([
        {"id": "terms", "page": 1, "text": "第二节 发行条款\n本期债券发行安排。"},
        {"id": "proceeds", "page": 2, "text": "第三节 募集资金运用"},
    ], "prospectus")
    section = next(item for item in located if item.role == "offering_terms")
    source_sections = {"terms": section}
    base = {
        "fact_key": "bond_issue_amount", "section_role": "offering_terms",
        "scope": "transaction", "value": 200000, "source_ids": ["terms"],
    }

    assert _model_fact_section_reason(
        {**base, "subject": "某子公司"}, issuer="北投公司", source_text={"terms": "本期发行规模20亿元"},
        source_sections=source_sections,
    ) == "non_issuer_subject"
    assert _model_fact_section_reason(
        {**base, "subject": "发行人", "scope": "standalone"}, issuer="北投公司",
        source_text={"terms": "本期发行规模20亿元"}, source_sections=source_sections,
    ) == "scope_not_allowed_for_section"
    assert _deterministic_fact_is_issuer_safe(
        {"value": 100, "source_ids": ["terms"]}, section,
        {"terms": "子公司甲公司营业收入为100万元"},
    ) is False


def test_beitou_real_material_section_regression():
    startup()
    prospectus = _sources(BEITOU_PROJECT_ID, "prospectus")
    rating = _sources(BEITOU_PROJECT_ID, "rating_report")
    prospectus_sections, _ = locate_document_sections(prospectus, "prospectus")
    rating_sections, _ = locate_document_sections(rating, "rating_report")
    prospectus_by_role = {item.role: item for item in prospectus_sections}
    rating_by_role = {item.role: item for item in rating_sections}

    assert prospectus_by_role["offering_terms"].start_page == 19
    assert prospectus_by_role["use_of_proceeds"].start_page == 22
    assert prospectus_by_role["financial_basis"].start_page == 73
    assert prospectus_by_role["financial_statements"].start_page == 77
    assert prospectus_by_role["financial_analysis"].start_page == 88
    assert prospectus_by_role["credit_enhancement"].start_page == 127
    assert prospectus_by_role["excluded_appendix"].start_page == 188
    assert rating_by_role["rating_result"].start_page == 3
    assert rating_by_role["excluded_formula"].start_page == 22
    assert rating_by_role["excluded_rating_symbols"].start_page == 23
    assert rating_by_role["excluded_contact"].start_page == 24

    plans, diagnostics, _ = build_section_extraction_plans(BEITOU_PROJECT_ID, prospectus + rating)
    assert diagnostics == []
    offering = next(item for item in plans if item.section.role == "offering_terms")
    assert [source["page"] for source in offering.sources] == [19, 20, 21]
    assert all(source["page"] < 121 for source in offering.sources)
    tranches = deterministic_extract_tranches(list(offering.sources))
    assert [item["term_years"] for item in tranches] == [3, 10]
    assert all(item["issue_amount"] is None for item in tranches)

    enhancement = next(item for item in plans if item.section.role == "credit_enhancement")
    enhancement_facts = deterministic_extract(list(enhancement.sources), set(enhancement.section.allowed_fact_keys))
    assert any(item["fact_key"] == "credit_enhancement_status" and item["text_value"] == "无增信安排" for item in enhancement_facts)
    # Exclusion roles are persisted for auditability but intentionally never
    # create an extraction plan.
    assert rating_by_role["excluded_formula"].allowed_fact_keys == ()


def test_zhongyou_remains_dynamic_guaranteed_reference():
    startup()
    prospectus = _sources(REAL_PROJECT_ID, "prospectus")
    rating_sources = _sources(REAL_PROJECT_ID, "rating_report")
    sections, _ = locate_document_sections(prospectus, "prospectus")
    by_role = {item.role: item for item in sections}
    rating_sections, _ = locate_document_sections(rating_sources, "rating_report")
    rating_by_role = {item.role: item for item in rating_sections}
    assert by_role["credit_enhancement"].status == "located"
    assert by_role["credit_enhancement"].start_page == 149
    assert rating_by_role["rating_result"].status == "located"
    assert rating_by_role["rating_result"].start_page == 4
    offering = by_role["offering_terms"]
    if offering.status != "located":
        # Documents with a different heading convention are safely marked for
        # one document-level locator task rather than inferred by project ID.
        assert offering.status in {"missing", "ambiguous"}
        return
    ordered = sorted(prospectus, key=lambda item: (item.get("page") or 0, item["id"]))
    start = next(index for index, item in enumerate(ordered) if item["id"] == offering.start_source_id)
    end = next(index for index, item in enumerate(ordered) if item["id"] == offering.end_source_id)
    tranches = deterministic_extract_tranches(ordered[start:end + 1])
    assert [item["term_years"] for item in tranches] == [3, 5]
    assert all(item["issue_amount"] is None for item in tranches)


def test_real_reference_financial_statements_keep_statement_scope_and_row_identity():
    startup()
    expected = {
        BEITOU_PROJECT_ID: {
            "consolidated_revenue": 1875971.19,
            "parent_revenue": 33366.31,
            "consolidated_assets": 96717233.20,
            "parent_assets": 57372249.51,
        },
        REAL_PROJECT_ID: {
            "consolidated_revenue": 127102.11,
            "parent_revenue": 119953.94,
            "consolidated_assets": 2741290.13,
            "parent_assets": 2729368.97,
        },
    }
    for project_id, values in expected.items():
        sources = _sources(project_id, "prospectus")
        plans, diagnostics, _ = build_section_extraction_plans(project_id, sources)
        assert diagnostics == []
        statement = next(item for item in plans if item.section.role == "financial_statements")
        facts = deterministic_extract(list(statement.sources), set(statement.section.allowed_fact_keys))
        revenue = {(item.get("scope"), item["period"]): item["value"] for item in facts if item["fact_key"] == "revenue"}
        assets = {(item.get("scope"), item["period"]): item["value"] for item in facts if item["fact_key"] == "total_assets"}
        assert revenue[("consolidated", "2025")] == values["consolidated_revenue"]
        assert revenue[("standalone", "2025")] == values["parent_revenue"]
        assert assets[("consolidated", "2025")] == values["consolidated_assets"]
        assert assets[("standalone", "2025")] == values["parent_assets"]
        assert all(item["value"] >= 0 for item in facts if item["fact_key"] == "total_assets")


def test_extract_query_limit_covers_every_section_slot() -> None:
    """检索配额必须 ≥ 合同里各 section 的字段槽位总数。

    配额是**跨章节累计**的硬截断：一旦小于槽位总数，靠后的章节（评级、债务、现金流、
    控制人）会连检索都不发起——实测配额 24 时诊断出 ``retrieval_unchecked_fields=66``，
    评级章因此拿不到任何评级事实、只能写"材料不足"。这条测试把"配额"与"合同规模"绑在
    一起：合同新增字段时会先在这里失败，提醒同步评估配额。
    """
    pack = bond_sections.load_extraction_contract()
    slots = sum(
        len(section.allowed_fact_keys)
        for document in pack.documents.values()
        for section in document.sections.values()
    )
    assert slots > 0
    assert settings.retrieval_extract_query_limit >= slots
