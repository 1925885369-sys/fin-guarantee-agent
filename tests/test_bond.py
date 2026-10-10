import sqlite3
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.app.bond_documents import CASE_SPECS, FICTION_NOTICE, TEMPLATE_VERSION, evaluation_manifest
from backend.app.main import app
from backend.app.parsers import classify_document
from backend.app.bond_rules import evaluate_bond_rules
from backend.app.database import connect, json_load, now_iso, rows
from backend.app.facts import FACT_REGISTRY, material_inventory
from backend.app.policy import load_policy
from backend.app.real_reference import BEITOU_PROJECT_ID, REAL_PROJECT_ID, seed_real_reference_projects


def _wait(client: TestClient, run_id: str) -> dict:
    for _ in range(1200):
        current = client.get(f"/api/runs/{run_id}").json()
        if current["status"] not in {"queued", "running"}:
            return current
        time.sleep(0.1)
    raise AssertionError("债券案例运行超时")


def test_bond_document_classification_precedes_generic_loan_categories():
    assert classify_document("募集说明书.pdf", "本期债券发行条款") == "募集说明书"
    assert classify_document("信用评级报告.pdf", "评级观点和评级结果") == "信用评级报告"
    assert classify_document("保证合同.pdf", "保证责任范围和保证期间") == "担保合同"


def test_flagship_bond_case_inventory_summary_rules_and_report():
    with TestClient(app) as client:
        inventory = client.get("/api/projects/demo-bond-huachen/document-inventory").json()
        assert inventory["readiness"] == "ready"
        assert len(inventory["documents"]) == 2
        assert inventory["gaps"] == []
        assert inventory["supplementary_gaps"]
        assert inventory["core_materials_ready"] is True
        assert inventory["complete_due_diligence_ready"] is True
        assert inventory["analysis_mode"] == "full_due_diligence"
        assert inventory["parse_quality"]["failed"] == 0
        assert sorted(item["page_count"] for item in inventory["documents"]) == [22, 72]

        summary = client.get("/api/projects/demo-bond-huachen/bond-summary").json()
        assert summary["issueAmount"] == 20000
        assert summary["termYears"] == 3
        assert summary["issuerRating"] == "AA-"
        assert summary["bondRating"] == "AA+"
        assert summary["reportLevel"] == "完整尽调"

        started = client.post("/api/projects/demo-bond-huachen/runs", json={"focus": "债券担保尽调"})
        assert started.status_code == 202
        completed = _wait(client, started.json()["id"])
        assert completed["status"] == "completed", completed
        assert completed["policy_profile"] == "bond-v1"

        checks = client.get("/api/projects/demo-bond-huachen/cross-checks").json()
        report = client.get("/api/projects/demo-bond-huachen/report").json()
        quality = client.get("/api/projects/demo-bond-huachen/report-quality").json()
        assert len(checks) == 19
        assert {item["status"] for item in checks} <= {"passed", "warning", "insufficient"}
        assert len(report) == 17
        assert {item["section_key"] for item in report} >= {"bond_overview", "rating", "guarantee", "counter_guarantee"}
        assert quality["invalid_citations"] == 0
        # 无证据主张不再通过替换/删除正文来消灭：正文保留，标记待人工复核。
        needs_review = [item for item in report if item.get("delivery_status") == "needs_review"]
        if quality["unsupported_numeric_claims"]:
            assert needs_review, "存在无证据主张时必须标记待人工复核"
        assert all(item.get("content_origin") for item in report)
        text = "\n".join(item["content"] for item in report)
        # 成品用词：原材料种类名一律换成规范化表述（见 PLAN3 §3）。
        assert "发行文件〔资料01〕" in text
        assert "募集说明书" not in text
        assert "评级机构认为" in text
        assert "未取得审计报告原件" in text
        assert "补充核验材料" in text
        for prohibited in ("建议通过", "建议拒绝", "核定担保额度", "核定担保费率"):
            assert prohibited not in text


def test_bond_evaluation_cases_are_bond_projects_with_core_materials():
    case_ids = ("demo-bond-ancheng", "demo-bond-rongyi", "demo-bond-jinghe", "demo-bond-conflict", "demo-bond-qualitative")
    with TestClient(app) as client:
        for project_id in case_ids:
            project = client.get(f"/api/projects/{project_id}").json()
            inventory = client.get(f"/api/projects/{project_id}/document-inventory").json()
            assert project["project_type"] == "bond_guarantee"
            assert inventory["core_materials_ready"] is True
            assert inventory["complete_due_diligence_ready"] is True
            assert inventory["analysis_mode"] == "full_due_diligence"
            assert len(inventory["documents"]) == 2
            assert sorted(item["page_count"] for item in inventory["documents"]) == [16, 43]


def test_fictional_bond_documents_are_long_form_traceable_and_unbranded():
    forbidden = ("北京市基础设施投资有限公司", "中诚信国际", "CCXI-")
    with TestClient(app) as client:
        for case in CASE_SPECS:
            documents = rows("SELECT * FROM documents WHERE project_id=? ORDER BY kind", (case.id,))
            sources = rows("SELECT * FROM sources WHERE project_id=?", (case.id,))
            assert len(documents) == 2
            assert len(sources) == sum(item["page_count"] for item in documents)
            assert all(item["page"] and item["locator"] == f"第 {item['page']} 页" for item in sources)
            full_text = "\n".join(item["text"] for item in sources)
            assert FICTION_NOTICE in full_text
            assert not any(term in full_text for term in forbidden)
            expected_pages = [22, 72] if case.full_length else [16, 43]
            assert sorted(item["page_count"] for item in documents) == expected_pages


def test_fictional_materials_keep_simulation_and_evidence_boundaries_explicit():
    """The packs must not imply a venue, unmodelled conflict, or effective collateral."""
    with TestClient(app) as client:
        texts = {
            case.id: "\n".join(item["text"] for item in rows("SELECT text FROM sources WHERE project_id=?", (case.id,)))
            for case in CASE_SPECS
        }
        for text in texts.values():
            assert "北京证券交易所" not in text
            assert "拟在符合监管规定的证券交易场所" in text
            assert "符合相关规定的专业投资者" in text

        assert "56,000万元" not in texts["demo-bond-conflict"]
        assert "前版发行方案" in texts["demo-bond-conflict"]
        assert "前版发行方案" in texts["demo-bond-rongyi"]
        assert "评级报告总债务" not in texts["demo-bond-rongyi"]
        assert "评级口径有息负债" in texts["demo-bond-rongyi"]
        assert "共用联系人" not in texts["demo-bond-qualitative"]
        assert "负责人离职" not in texts["demo-bond-qualitative"]
        assert "缺少客户盖章" in texts["demo-bond-qualitative"]

        ancheng_facts = client.get("/api/projects/demo-bond-ancheng/facts").json()
        ancheng_checks = {item.key: item for item in evaluate_bond_rules(ancheng_facts, 12000, policy=load_policy("bond-v1"))}
        assert ancheng_checks["counter_guarantee"].status == "insufficient"
        assert "不评价反担保可执行性" in ancheng_checks["counter_guarantee"].summary


def test_counter_guarantee_reports_both_issue_and_guarantee_cap_denominators():
    facts = [
        {"id": "counter", "fact_key": "counter_guarantee_value", "name": "反担保折后价值", "value": 16000, "unit": "万元", "source_ids": ["src-counter"]},
        {"id": "rights", "fact_key": "counter_guarantee_rights", "name": "反担保权利状态", "text_value": "第一顺位抵押已完成登记", "source_ids": ["src-rights"]},
        {"id": "cap", "fact_key": "guarantee_cap", "name": "保证责任上限", "value": 23000, "unit": "万元", "source_ids": ["src-cap"]},
    ]
    checks = {item.key: item for item in evaluate_bond_rules(facts, 20000, policy=load_policy("bond-v1"))}
    counter = checks["counter_guarantee"]
    assert counter.status == "warning"
    assert "占本期发行规模80.0%，占保证责任上限69.6%" in counter.summary
    assert [item["label"] for item in counter.values] == ["反担保/本期发行规模", "反担保/保证责任上限"]
    assert FACT_REGISTRY["rating_total_debt"].label == "评级口径有息负债"


def test_gold_annotations_resolve_to_real_pdf_pages():
    with TestClient(app):
        for case in CASE_SPECS:
            valid_sources = {item["id"] for item in rows("SELECT id FROM sources WHERE project_id=?", (case.id,))}
            annotations = rows("SELECT * FROM gold_annotations WHERE project_id=? AND version=?", (case.id, TEMPLATE_VERSION))
            assert annotations
            for annotation in annotations:
                expected = json_load(annotation["expected_json"])
                source_ids = json_load(annotation["source_ids"])
                assert set(source_ids) <= valid_sources
                if annotation["annotation_type"] == "fact":
                    assert expected["page"] >= 1
                    assert expected["locator"] == f"第 {expected['page']} 页"
                    assert expected["quote"]


def test_core_transaction_materials_allow_unified_complete_diligence():
    project_id = "test-bond-complete-diligence"
    now = now_iso()
    with connect() as conn:
        conn.execute("DELETE FROM documents WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,project_type,bond_name,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, "test-token", "完整尽调测试企业", 10000, 36, "补充经营资金", "ready", now, now, "bond_guarantee", "测试债券", now),
        )
        for index, kind in enumerate(("募集说明书", "信用评级报告"), 1):
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,ocr_status,page_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"core-doc-{index}", project_id, f"{kind}.pdf", kind, "application/pdf", f"hash-{index}", 100, "parsed", 0.98, 1, "", now, "native", "not_required", 1),
            )
    try:
        inventory = material_inventory(project_id)
        assert inventory["core_materials_ready"] is True
        assert inventory["complete_due_diligence_ready"] is True
        assert inventory["analysis_mode"] == "full_due_diligence"
        assert inventory["gaps"] == []
        assert inventory["supplementary_gaps"]
    finally:
        with connect() as conn:
            conn.execute("DELETE FROM documents WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_bond_guarantee_scope_rejects_no_enhancement_case():
    facts = [{
        "id": "f1", "fact_key": "guarantee_scope", "name": "保证责任范围", "text_value": "本期债券无担保安排",
        "value": None, "source_ids": ["src-1"], "period": "2026",
    }]
    checks = evaluate_bond_rules(facts, 1000, policy=load_policy("bond-v1"))
    guarantee = next(item for item in checks if item.key == "guarantee_scope")
    assert guarantee.status == "warning"
    assert "不适用于" in guarantee.summary


def test_bond_rules_do_not_infer_missing_coverage_inputs():
    facts = [
        {"id": "scope", "fact_key": "guarantee_scope", "name": "保证责任范围", "text_value": "本金、利息、违约金及实现债权费用", "value": None, "source_ids": ["src-scope"]},
        {"id": "counter", "fact_key": "counter_guarantee_value", "name": "反担保折后价值", "value": 1200, "unit": "万元", "source_ids": ["src-counter"]},
    ]
    checks = {item.key: item for item in evaluate_bond_rules(facts, 1000, policy=load_policy("bond-v1"))}
    assert checks["guarantee_scope"].status == "insufficient"
    assert checks["counter_guarantee"].status == "insufficient"


def test_bond_summary_marks_conflicted_issue_amount_without_selecting_a_source():
    with TestClient(app) as client:
        started = client.post("/api/projects/demo-bond-conflict/runs", json={"focus": "债券冲突复核"})
        assert started.status_code == 202
        completed = _wait(client, started.json()["id"])
        assert completed["status"] == "completed", completed
        summary = client.get("/api/projects/demo-bond-conflict/bond-summary").json()
        assert "bond_issue_amount" in summary["conflictedFields"]
        assert summary["issueAmount"] is None
        report = client.get("/api/projects/demo-bond-conflict/report").json()
        overview = next(item for item in report if item["section_key"] == "bond_overview")
        assert "存在来源冲突" in overview["content"]


def test_real_reference_project_is_traceable_grouped_and_evaluation_isolated():
    with TestClient(app) as client:
        default_projects = client.get("/api/projects").json()
        references = client.get("/api/projects?data_class=real_reference").json()
        assert REAL_PROJECT_ID not in {item["id"] for item in default_projects}
        assert {item["id"] for item in references} == {REAL_PROJECT_ID, BEITOU_PROJECT_ID}
        assert all(item["evaluation_eligible"] == 0 for item in references)
        assert REAL_PROJECT_ID not in evaluation_manifest()

        inventory = client.get(f"/api/projects/{REAL_PROJECT_ID}/document-inventory").json()
        assert inventory["complete_due_diligence_ready"] is True
        assert inventory["analysis_mode"] == "full_due_diligence"
        assert sorted(item["page_count"] for item in inventory["documents"]) == [29, 252]

        summary = client.get(f"/api/projects/{REAL_PROJECT_ID}/bond-summary").json()
        assert summary["dataClass"] == "real_reference"
        assert summary["evaluationEligible"] is False
        assert summary["reportLevel"] == "完整尽调"
        assert summary["currentIssueAmount"] == 200000
        # Real references start from raw PDFs. Legacy seeds stay visible only
        # as stale audit rows and cannot populate current analysis facts.
        assert summary["registeredAmount"] in {None, ""}
        assert summary["termYears"] is None
        # 分档来自对原始 PDF 的真实抽取：干净库（尚未运行抽取）时为空，
        # 一旦存在即为材料披露的两个品种（3 年期/5 年期）；抽取前金额与分配状态一律未披露。
        tranches = summary["tranches"]
        assert [item["term_years"] for item in tranches] == [3, 5]
        assert all(item["issue_amount"] is None for item in tranches)
        assert all(item["allocation_status"] == "not_disclosed" for item in tranches)

        # 真实参考项目是"原始材料项目"：只提供原始 PDF，不携带事实与分档；
        # 事实必须由按章抽取的真实运行产出，历史手写种子已被设计性弃用。
        assert client.get(f"/api/projects/{REAL_PROJECT_ID}/facts").json() == []
        assert not rows("SELECT id FROM facts WHERE project_id=?", (REAL_PROJECT_ID,))
        assert [item["term_years"] for item in rows(
            "SELECT term_years FROM bond_tranches WHERE project_id=? ORDER BY term_years", (REAL_PROJECT_ID,)
        )] == [3, 5]
        assert not rows("SELECT id FROM gold_annotations WHERE project_id=?", (REAL_PROJECT_ID,))
        assert client.get(f"/api/projects/{REAL_PROJECT_ID}/export?format=md").status_code == 403

        beitou_inventory = client.get(f"/api/projects/{BEITOU_PROJECT_ID}/document-inventory").json()
        assert beitou_inventory["complete_due_diligence_ready"] is True
        assert sorted(item["page_count"] for item in beitou_inventory["documents"]) == [24, 228]
        assert "担保合同" not in {item["group"] for item in beitou_inventory["supplementary_gaps"]}
        assert beitou_inventory["guarantee_applicable"] is False

        beitou = client.get(f"/api/projects/{BEITOU_PROJECT_ID}/bond-summary").json()
        assert beitou["currentIssueAmount"] == 200000
        assert beitou["registeredAmount"] in {None, ""}
        assert beitou["issuerRating"] in {None, ""}
        assert beitou["bondRating"] in {None, ""}
        assert beitou["guaranteeType"] in {None, ""}
        assert beitou["guaranteeApplicable"] is False
        # 同上：抽取前无分档；一旦抽取产出，其金额在披露前保持未取得。
        assert [item["term_years"] for item in beitou["tranches"]] == [3, 10]
        assert not rows("SELECT id FROM facts WHERE project_id=?", (BEITOU_PROJECT_ID,))
        assert BEITOU_PROJECT_ID not in evaluation_manifest()
        assert not rows("SELECT id FROM gold_annotations WHERE project_id=?", (BEITOU_PROJECT_ID,))
        assert client.get(f"/api/projects/{BEITOU_PROJECT_ID}/export?format=md").status_code == 403


def test_real_reference_seed_repairs_a_partially_imported_document():
    with TestClient(app):
        before = rows("SELECT id FROM documents WHERE project_id=?", (REAL_PROJECT_ID,))
        assert len(before) == 2
        try:
            with connect() as conn:
                conn.execute("DELETE FROM sources WHERE document_id='real-zy-rating'")
                conn.execute("DELETE FROM documents WHERE id='real-zy-rating'")
            assert seed_real_reference_projects() == 2
            repaired = rows("SELECT * FROM documents WHERE project_id=?", (REAL_PROJECT_ID,))
            assert len(repaired) == 2
            assert rows("SELECT id FROM sources WHERE document_id='real-zy-rating'")
        finally:
            seed_real_reference_projects()


def test_legacy_real_reference_directory_is_read_without_being_moved(tmp_path, monkeypatch):
    import backend.app.real_reference as real_reference

    legacy = tmp_path / "demo_materials" / "demo-中邮"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(real_reference, "settings", SimpleNamespace(resolved_data_dir=tmp_path))
    assert real_reference._material_root() == legacy
    assert legacy.is_dir()
    assert not (tmp_path / "demo_materials" / real_reference.REAL_MATERIAL_DIR).exists()


def test_tranche_extraction_does_not_assume_two_varieties():
    import backend.app.real_reference as real_reference

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sources (id TEXT, project_id TEXT, document_id TEXT, page INTEGER, text TEXT)")
    assert real_reference._extract_tranches(conn) == []
    conn.execute(
        "INSERT INTO sources VALUES (?,?,?,?,?)",
        ("single-p1", REAL_PROJECT_ID, "real-zy-prospectus", 1,
         "本期债券仅设品种一，品种一为4年期，付息频率为按年付息。本期债券到期一次性偿还本金。"),
    )
    tranches = real_reference._extract_tranches(conn)
    assert len(tranches) == 1
    assert tranches[0]["term_years"] == 4
    assert tranches[0]["issue_amount"] is None


def test_annual_series_dedupes_years_across_reporting_scopes():
    """同一年度的合并/单体口径不得各占一个位置（否则出现“2025年、2025年”）。"""
    from backend.app.bond_report import _annual_series

    facts = [
        {"fact_key": "revenue", "period": "2023", "scope": "consolidated", "status": "confirmed", "value": 100, "unit": "万元"},
        {"fact_key": "revenue", "period": "2024", "scope": "consolidated", "status": "confirmed", "value": 200, "unit": "万元"},
        {"fact_key": "revenue", "period": "2025", "scope": "standalone", "status": "confirmed", "value": 999, "unit": "万元"},
        {"fact_key": "revenue", "period": "2025", "scope": "consolidated", "status": "confirmed", "value": 300, "unit": "万元"},
    ]
    assert _annual_series(facts, "revenue") == "2023年100万元、2024年200万元、2025年300万元"


def test_annual_series_keeps_conflict_and_does_not_switch_scope():
    """采用口径内同年度冲突时原样显示，且不改取另一口径掩盖冲突。"""
    from backend.app.bond_report import _annual_series

    facts = [
        {"fact_key": "revenue", "period": "2023", "scope": "consolidated", "status": "confirmed", "value": 100, "unit": "万元"},
        {"fact_key": "revenue", "period": "2024", "scope": "consolidated", "status": "confirmed", "value": 200, "unit": "万元"},
        {"fact_key": "revenue", "period": "2025", "scope": "consolidated", "status": "conflicted", "value": 300, "unit": "万元"},
        {"fact_key": "revenue", "period": "2025", "scope": "standalone", "status": "confirmed", "value": 999, "unit": "万元"},
    ]
    series = _annual_series(facts, "revenue")
    assert series.endswith("2025年存在来源冲突")
    assert "999" not in series


def test_annual_series_reports_insufficient_full_years():
    from backend.app.bond_report import _annual_series

    facts = [
        {"fact_key": "revenue", "period": "2024", "scope": "consolidated", "status": "confirmed", "value": 200, "unit": "万元"},
        {"fact_key": "revenue", "period": "2025", "scope": "consolidated", "status": "confirmed", "value": 300, "unit": "万元"},
    ]
    assert _annual_series(facts, "revenue") == "完整年度不足三期"


def test_tranche_ratings_come_from_the_rating_text_only():
    """分品种债项评级只能来自评级原文，**不得把整体等级复制到各品种**（修订稿 §2.4）。

    北投/中邮的 ``tranche_name`` 都带期限后缀（"品种一（3年期）"），而评级原文写的是
    "（品种一）AAA"——所以匹配前必须先取序数前缀，否则永远命中不了。
    """
    from backend.app.bond_tranches import tranche_label, tranche_ratings_from_text

    names = ["品种一（3年期）", "品种二（10年期）"]
    assert [tranche_label(item) for item in names] == ["品种一", "品种二"]

    # 正例：逐品种披露（实测北投评级报告就是这种排版）
    assert tranche_ratings_from_text(
        "（第一期）（品种一）AAA北京市基础设施投资有限公司（第一期）（品种二）AAA发行要素", names,
    ) == {"品种一（3年期）": "AAA", "品种二（10年期）": "AAA"}
    # 正例：并列同级
    assert tranche_ratings_from_text("品种一和品种二信用等级均为AAA", names) == {
        "品种一（3年期）": "AAA", "品种二（10年期）": "AAA",
    }
    # 正例：分别不同等级，各自的等级不能串到对方头上
    assert tranche_ratings_from_text("（品种一）AAA，（品种二）AA+", names) == {
        "品种一（3年期）": "AAA", "品种二（10年期）": "AA+",
    }
    # 负例（关键）：只有整体评级时**一个品种都不能填**
    assert tranche_ratings_from_text("本期债券信用等级为AAA", names) == {}
    # 负例：只覆盖一个品种时，另一个不出现（调用方显示"未披露"）
    assert tranche_ratings_from_text("（品种一）AAA，品种二期限为10年期", names) == {
        "品种一（3年期）": "AAA",
    }
