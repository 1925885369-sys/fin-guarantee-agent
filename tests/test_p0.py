import uuid

import fitz
from fastapi.testclient import TestClient

from backend.app.database import connect, json_dump, now_iso, row, rows
from backend.app.main import app
from backend.app.metrics import compute_and_store_metrics
from backend.app.ocr import DisabledOcrProvider, OcrFragment
from backend.app.p0 import (
    build_evidence_graph,
    calculation_trace,
    compute_financial_tie_outs,
    report_quality,
    validate_report_support,
)
from backend.app.parsers import parse_pdf
from backend.app.professional import analysis_fact_rows, fact_groups, sync_fact_assertions


def _project(company: str = "P0测试企业") -> str:
    project_id = f"test-p0-{uuid.uuid4()}"
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


def _assertion(key: str, period: str, value: float, scope: str, subject: str = "", source_id: str = "") -> dict:
    return {
        "id": str(uuid.uuid4()), "fact_key": key, "period": period, "value": value, "unit": "万元",
        "raw_value": str(value), "source_ids": [source_id] if source_id else [], "extraction_method": "deterministic",
        "scope": scope, "subject": subject,
    }


def test_subject_scope_and_period_are_isolated_in_financial_metrics():
    project_id = _project("借款主体甲")
    try:
        assertions = [
            _assertion("revenue", "2025", 1000, "standalone"),
            _assertion("net_profit", "2025", 100, "standalone"),
            _assertion("revenue", "2025", 2000, "consolidated"),
            _assertion("net_profit", "2025", 400, "consolidated"),
            _assertion("revenue", "2025", 3000, "standalone", "关联企业乙"),
            _assertion("net_profit", "2025", 900, "standalone", "关联企业乙"),
        ]
        sync_fact_assertions(project_id, assertions)

        revenue_groups = [item for item in fact_groups(project_id) if item["fact_key"] == "revenue"]
        assert len(revenue_groups) == 3
        assert {(item["subject"], item["scope"]) for item in revenue_groups} == {
            ("借款主体甲", "standalone"), ("借款主体甲", "consolidated"), ("关联企业乙", "standalone"),
        }

        analysis_facts = analysis_fact_rows(project_id)
        assert {item["subject"] for item in analysis_facts} == {"借款主体甲"}
        assert {item["scope"] for item in analysis_facts} == {"consolidated"}
        metrics = compute_and_store_metrics(project_id, 500, "p0-test")
        net_margin = next(item for item in metrics if item["metric_key"] == "net_margin")
        assert net_margin["value"] == 20
        assert net_margin["scope"] == "consolidated"
        assert len(net_margin["trace_json"]["inputs"]) == 2
    finally:
        _cleanup(project_id)


def test_financial_tie_outs_pass_warn_and_report_insufficient_without_zero_fill():
    project_id = _project()
    try:
        sync_fact_assertions(project_id, [
            _assertion("total_assets", "2025", 1000, "standalone"),
            _assertion("total_liabilities", "2025", 600, "standalone"),
            _assertion("equity", "2025", 400, "standalone"),
            _assertion("total_assets", "2024", 1000, "standalone"),
            _assertion("total_liabilities", "2024", 700, "standalone"),
            _assertion("equity", "2024", 400, "standalone"),
            _assertion("total_assets", "2023", 900, "standalone"),
            _assertion("revenue", "2022", 800, "standalone"),
        ])
        tie_outs = compute_financial_tie_outs(project_id, "p0-test")
        balance = {item["period"]: item for item in tie_outs if item["check_key"] == "balance_equation"}
        assert balance["2025"]["status"] == "passed"
        assert balance["2024"]["status"] == "warning" and balance["2024"]["difference"] == -100
        assert balance["2023"]["status"] == "insufficient" and balance["2023"]["right_value"] is None
        assert "2022" not in balance
    finally:
        _cleanup(project_id)


class _FakeOcr:
    name = "fake_vision"
    available = True

    def extract_pdf(self, path, max_pages):
        return [OcrFragment(1, "营业收入 100 万元", [0.1, 0.2, 0.8, 0.3], 0.96)]


class _FailingOcr:
    name = "failing_vision"
    available = True

    def extract_pdf(self, path, max_pages):
        raise RuntimeError("provider unavailable")


def test_ocr_adapter_marks_required_and_preserves_page_bbox(tmp_path):
    path = tmp_path / "scan.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()

    disabled = parse_pdf(path, DisabledOcrProvider())
    assert disabled.ocr_status == "required" and disabled.fragments == []

    parsed = parse_pdf(path, _FakeOcr())
    assert parsed.ocr_status == "completed" and parsed.parser == "fake_vision"
    assert parsed.fragments[0].locator_type == "pdf_ocr_block"
    assert parsed.fragments[0].page == 1 and parsed.fragments[0].bbox == [0.1, 0.2, 0.8, 0.3]

    failed = parse_pdf(path, _FailingOcr())
    assert failed.ocr_status == "failed" and "OCR失败" in failed.warning


def test_claim_validation_and_evidence_graph_trace_every_calculation():
    project_id = _project("证据链测试企业")
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,ocr_status,page_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("doc-p0", project_id, "财务报表.pdf", "财务报表", "application/pdf", "hash", 100, "parsed", 1, 1, "", now_iso(), "native", "not_required", 1),
            )
            conn.execute(
                "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("source-p0", project_id, "doc-p0", "pdf_page", "第 1 页", "2025年营业收入100万元，净利润10万元。", 1, json_dump([0, 0, 1, 1]), "native", "source-hash"),
            )
            conn.execute(
                "INSERT INTO citations (id,project_id,source_id,document_id,label,quote,locator_type,locator) VALUES (?,?,?,?,?,?,?,?)",
                ("citation-p0", project_id, "source-p0", "doc-p0", "财务报表.pdf · 第 1 页", "2025年营业收入100万元，净利润10万元。", "pdf_page", "第 1 页"),
            )
        sync_fact_assertions(project_id, [
            _assertion("revenue", "2025", 100, "standalone", source_id="source-p0"),
            _assertion("net_profit", "2025", 10, "standalone", source_id="source-p0"),
        ])
        metrics = compute_and_store_metrics(project_id, 500, "p0-test")
        metric = next(item for item in metrics if item["metric_key"] == "net_margin")
        with connect() as conn:
            conn.execute(
                "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("section-p0", project_id, "financial", "财务分析", "2025年营业收入为100万元。", json_dump(["citation-p0"]), "draft", now_iso()),
            )
            conn.execute(
                "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("section-system-p0", project_id, "disclaimer", "声明", "本报告不构成审批结论，存在风险提示。", "[]", "draft", now_iso()),
            )

        valid = validate_report_support(project_id)
        assert valid["unsupported_claims"] == 0 and valid["invalid_citations"] == 0
        assert report_quality(project_id)["factual_section_citation_coverage"] == 1
        graph = build_evidence_graph(project_id)
        relationships = {(item["from_type"], item["relation"], item["to_type"]) for item in graph["edge_summary"]}
        assert {
            ("document", "contains", "source"),
            ("source", "supports", "assertion"),
            ("assertion", "canonicalizes", "canonical_fact"),
            ("canonical_fact", "input_to", "metric"),
            ("source", "cited_by", "report_claim"),
        } <= relationships
        trace = calculation_trace(project_id, metric["id"])
        assert len(trace["inputs"]) == 2
        assert all(item["sources"][0]["page"] == 1 for item in trace["inputs"])

        with connect() as conn:
            conn.execute(
                "UPDATE report_sections SET content=?,citation_ids=? WHERE id='section-p0'",
                ("2025年营业收入为9999万元。", json_dump(["citation-p0", "missing-citation"])),
            )
        invalid = validate_report_support(project_id)
        assert invalid["unsupported_claims"] == 1 and invalid["invalid_citations"] == 1
        stored = report_quality(project_id)
        assert stored["status"] == "warning" and stored["support_rate"] < 1
    finally:
        _cleanup(project_id)


def test_claim_type_separates_requirements_and_boundaries_from_business_facts():
    from backend.app.p0 import _claim_type

    # 正例：审查要求 / 建议核验动作 / 来源边界声明都不是业务事实主张。
    assert _claim_type("到期本金应与存量债务分别呈示") == "requirement"
    assert _claim_type("建议取得募集说明书原件") == "requirement"
    assert _claim_type("需函证应收账款") == "requirement"
    assert _claim_type("本次披露未经独立核验") == "boundary"
    assert _claim_type("本报告不构成审批结论") == "boundary"
    # 反例：带数值的业务陈述必须仍走事实判定，不能被顺手归入要求/边界
    # （年份 2025 已被 _numbers 过滤，剩下的 1,234 才是被主张的数据值）。
    assert _claim_type("2025 年末有息负债为 1,234 万元") == "numeric_fact"
    assert _claim_type("2025 年营业收入为 100 万元") == "numeric_fact"
    # 含"应"但陈述的是既定事实、且带数值 → 仍是事实，不因"应"被吞掉。
    assert _claim_type("期末应收账款应为 500 万元") == "numeric_fact"


def test_requirement_and_boundary_claims_are_not_counted_as_unsupported_while_facts_still_are():
    project_id = _project("要求边界集成测试企业")
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,ocr_status,page_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("doc-req", project_id, "募集说明书.pdf", "募集说明书", "application/pdf", "hash", 100, "parsed", 1, 1, "", now_iso(), "native", "not_required", 1),
            )
            conn.execute(
                "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("source-req", project_id, "doc-req", "pdf_page", "第 1 页", "2025年营业收入100万元。", 1, json_dump([0, 0, 1, 1]), "native", "source-hash-req"),
            )
            conn.execute(
                "INSERT INTO citations (id,project_id,source_id,document_id,label,quote,locator_type,locator) VALUES (?,?,?,?,?,?,?,?)",
                ("citation-req", project_id, "source-req", "doc-req", "募集说明书.pdf · 第 1 页", "2025年营业收入100万元。", "pdf_page", "第 1 页"),
            )
            # 一段正文里同时含要求句、边界句与一条**带数值**的业务事实：
            # 来源文本里只有"营业收入100万元"，没有任何有息负债/1,234 的证据。
            content = (
                "到期本金应与存量债务分别呈示。建议取得募集说明书原件。"
                "需函证应收账款。本次披露未经独立核验。2025 年末有息负债为 1,234 万元。"
            )
            conn.execute(
                "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                ("section-req", project_id, "financial", "财务分析", content, json_dump(["citation-req"]), "draft", now_iso()),
            )

        valid = validate_report_support(project_id)
        # 只有那条数值事实被拦；要求句与边界句不产生无证据声明。
        assert valid["unsupported_claims"] == 1

        claims = {
            item["claim_text"].rstrip("。").strip(): item
            for item in rows(
                "SELECT claim_text,claim_type,support_status FROM report_claims WHERE project_id=? AND section_id='section-req'",
                (project_id,),
            )
        }
        assert claims["到期本金应与存量债务分别呈示"]["claim_type"] == "requirement"
        assert claims["建议取得募集说明书原件"]["claim_type"] == "requirement"
        assert claims["需函证应收账款"]["claim_type"] == "requirement"
        assert claims["本次披露未经独立核验"]["claim_type"] == "boundary"
        assert claims["2025 年末有息负债为 1,234 万元"]["claim_type"] == "numeric_fact"
        # 要求 / 边界句状态为 not_applicable（与 narrative 一致），不带 unsupported。
        for text in ("到期本金应与存量债务分别呈示", "建议取得募集说明书原件",
                     "需函证应收账款", "本次披露未经独立核验"):
            assert claims[text]["support_status"] == "not_applicable"
        # 反例：数值事实仍判 unsupported。
        assert claims["2025 年末有息负债为 1,234 万元"]["support_status"] == "unsupported"

        quality = report_quality(project_id)
        assert quality["factual_claims"] == 1
        assert quality["unsupported_claims"] == 1
        assert quality["status"] == "warning"
    finally:
        _cleanup(project_id)


def test_p0_endpoints_require_project_token_and_six_cases_have_gold_annotations():
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "company_name": "P0接口测试企业", "amount": 500, "term_months": 12,
            "purpose": "流动资金", "data_consent": True,
        }).json()
        project_id, token = created["id"], created["accessToken"]
        endpoints = ("entities", "financial-tie-outs", "evidence-graph", "report-quality", "gold-standard")
        for endpoint in endpoints:
            assert client.get(f"/api/projects/{project_id}/{endpoint}").status_code == 403
            assert client.get(f"/api/projects/{project_id}/{endpoint}", headers={"X-Project-Token": token}).status_code == 200

        demo_ids = [item["id"] for item in client.get("/api/projects").json()]
        assert len(demo_ids) == 6
        for demo_id in demo_ids:
            annotations = client.get(f"/api/projects/{demo_id}/gold-standard").json()
            assert annotations and {item["annotation_type"] for item in annotations} >= {"fact", "warning", "citation", "tie_out"}
        _cleanup(project_id)
