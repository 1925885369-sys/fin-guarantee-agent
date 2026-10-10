import pytest

from backend.app import bond_tranches
from backend.app import workflow
from backend.app.bond_tranches import (
    analyze_deterministic_tranches,
    deterministic_extract_tranches,
    extract_bond_tranches,
)
from backend.app.database import connect, now_iso, rows


def _project(project_id: str, project_type: str = "bond_guarantee") -> None:
    now = now_iso()
    with connect() as conn:
        conn.execute("DELETE FROM bond_tranches WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,project_type,bond_name,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, "test-token", "品种抽取测试企业", 20000, 36, "补充经营资金", "ready", now, now, project_type, "测试债券", now),
        )


def _cleanup(project_id: str) -> None:
    with connect() as conn:
        for table in ("bond_tranches", "review_tasks", "facts", "fact_assertions", "canonical_facts", "documents", "sources"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_no_tranches_when_no_variety_disclosure():
    rows_ = [{"id": "a", "text": "本期债券募集资金用于补充流动资金。"}]
    assert deterministic_extract_tranches(rows_) == []


def test_single_tranche_from_this_issue_term():
    rows_ = [{"id": "a", "text": "本期债券期限为5年，按年付息，到期一次性偿还本金。"}]
    tranches = deterministic_extract_tranches(rows_)
    assert len(tranches) == 1
    assert tranches[0]["term_years"] == 5
    assert tranches[0]["tranche_key"] == "variety-1"
    assert tranches[0]["issue_amount"] is None
    assert tranches[0]["allocation_status"] == "not_disclosed"


def test_two_varieties():
    rows_ = [{"id": "a", "text": "品种一为3年期，品种二为5年期。"}]
    tranches = deterministic_extract_tranches(rows_)
    assert [item["term_years"] for item in tranches] == [3, 5]
    assert [item["tranche_key"] for item in tranches] == ["variety-1", "variety-2"]


def test_multi_variety_no_loss_or_duplicate():
    rows_ = [{"id": "a", "text": "品种一为2年期，品种二为3年期，品种三为5年期，品种四为7年期。"}]
    tranches = deterministic_extract_tranches(rows_)
    assert len(tranches) == 4
    assert [item["term_years"] for item in tranches] == [2, 3, 5, 7]
    assert len({item["tranche_key"] for item in tranches}) == 4


def test_per_variety_amount_not_disclosed_stays_null():
    rows_ = [{"id": "a", "text": "品种一为3年期，品种二为5年期。付息频率为按年付息，本期债券到期一次性偿还本金。"}]
    tranches = deterministic_extract_tranches(rows_)
    assert all(item["issue_amount"] is None for item in tranches)
    assert all(item["allocation_status"] == "not_disclosed" for item in tranches)
    assert all(item["repayment_method"] == "每年付息，到期一次性偿还本金" for item in tranches)


def test_per_variety_amounts_are_disclosed():
    rows_ = [{"id": "a", "text": "品种一为3年期，发行金额为10,000万元；品种二为5年期，发行金额为5,000万元。"}]
    tranches = deterministic_extract_tranches(rows_)
    assert [item["issue_amount"] for item in tranches] == [10000.0, 5000.0]
    assert all(item["allocation_status"] == "disclosed" for item in tranches)
    assert all(item["amount_scope"] == "tranche" for item in tranches)


def test_deterministic_analysis_retains_conflicted_and_unresolved_varieties():
    analysis = analyze_deterministic_tranches([{
        "id": "a",
        "text": "品种一为3年期，品种一为5年期；本期同时设置品种二，具体期限另行公告。",
    }])

    assert analysis.accepted_tranches == []
    assert [(item.ordinal, item.reason) for item in analysis.ambiguous_items] == [
        ("一", "同一品种披露了不同期限"),
        ("二", "材料提及该品种但未识别到明确期限"),
    ]


def test_aggregate_issue_amount_not_copied_to_varieties():
    rows_ = [{"id": "a", "text": "本期债券分为品种一和品种二，品种一为3年期，品种二为5年期。本期发行规模不超过20,000万元。"}]
    tranches = deterministic_extract_tranches(rows_)
    assert all(item["issue_amount"] is None for item in tranches)
    assert all(item["allocation_status"] == "not_disclosed" for item in tranches)


def test_extract_bond_tranches_persists_deterministic_tranches():
    from backend.app.main import startup

    startup()
    project_id = "test-bond-tranches-write"
    _project(project_id)
    try:
        tranches = extract_bond_tranches(
            project_id,
            [{"id": "a", "kind": "募集说明书", "text": "品种一为3年期，品种二为5年期。"}],
            20000, allow_llm=False,
        )
        assert len(tranches) == 2
        stored = rows("SELECT * FROM bond_tranches WHERE project_id=? ORDER BY term_years", (project_id,))
        assert len(stored) == 2
        assert all(item["extraction_method"] == "deterministic" for item in stored)
        assert all(item["confidence"] is None for item in stored)
    finally:
        _cleanup(project_id)


def test_llm_fallback_valid_candidate_persisted_as_model(monkeypatch):
    from backend.app.main import startup

    startup()
    project_id = "test-bond-tranche-llm-valid"
    _project(project_id)
    monkeypatch.setattr(bond_tranches, "is_configured", lambda: True)

    def fake_llm(source_rows, issue_amount, context=None):
        return [{
            "tranche_name": "品种一", "term_years": 3, "issue_amount": 10000,
            "amount_unit": "万元", "source_ids": ["a"], "extraction_method": "model",
        }], None

    monkeypatch.setattr(bond_tranches, "llm_extract_tranche_candidates", fake_llm)
    try:
        tranches = extract_bond_tranches(
            project_id,
            [{"id": "a", "kind": "募集说明书", "text": "本期债券设置品种一，其期限安排为3年整，品种一发行金额为10,000万元。"}],
            20000,
        )
        assert len(tranches) == 1
        stored = rows("SELECT * FROM bond_tranches WHERE project_id=?", (project_id,))
        assert stored[0]["extraction_method"] == "model"
        assert stored[0]["issue_amount"] == 10000
        assert stored[0]["amount_scope"] == "tranche"
    finally:
        _cleanup(project_id)


def test_llm_fallback_rejects_copied_total_and_records_task(monkeypatch):
    from backend.app.main import startup

    startup()
    project_id = "test-bond-tranche-llm-reject"
    _project(project_id)
    monkeypatch.setattr(bond_tranches, "is_configured", lambda: True)

    def fake_llm(source_rows, issue_amount, context=None):
        return [{
            "tranche_name": "品种一", "term_years": 3, "issue_amount": 20000,
            "amount_unit": "万元", "source_ids": ["a"], "extraction_method": "model",
        }], None

    monkeypatch.setattr(bond_tranches, "llm_extract_tranche_candidates", fake_llm)
    try:
        tranches = extract_bond_tranches(
            project_id,
            [{"id": "a", "kind": "募集说明书", "text": "本期设置品种一，其期限安排为3年整。本期发行规模不超过20,000万元。"}],
            20000,
        )
        assert tranches == []
        tasks = rows("SELECT * FROM review_tasks WHERE project_id=? AND task_type='bond_tranche_verification'", (project_id,))
        assert tasks
        assert "复制了项目总规模" in tasks[0]["description"] or "金额无法在原文中定位" in tasks[0]["description"]
    finally:
        _cleanup(project_id)


def test_llm_candidate_rejects_invalid_term_and_source():
    rows_ = [{"id": "a", "kind": "募集说明书", "text": "品种一为3年期。"}]
    ok, reason = bond_tranches._validate_candidate(
        {"tranche_name": "品种一", "term_years": -1, "issue_amount": None, "source_ids": ["a"]}, rows_, 20000,
    )
    assert ok is False and "正整数" in reason
    ok, reason = bond_tranches._validate_candidate(
        {"tranche_name": "品种一", "term_years": 3, "issue_amount": None, "source_ids": ["not-here"]}, rows_, 20000,
    )
    assert ok is False and "不属于" in reason


def test_llm_candidate_requires_name_term_and_maturity_in_same_cited_evidence():
    rows_ = [{"id": "a", "kind": "募集说明书", "text": "品种一为3年期，到期日为2029年6月1日。"}]
    ok, reason = bond_tranches._validate_candidate(
        {"tranche_name": "品种二", "term_years": 3, "maturity_date": "2029-06-01", "issue_amount": None, "source_ids": ["a"]},
        rows_, 20000,
    )
    assert ok is False and "名称或序号" in reason
    ok, reason = bond_tranches._validate_candidate(
        {"tranche_name": "品种一", "term_years": 5, "maturity_date": "2029-06-01", "issue_amount": None, "source_ids": ["a"]},
        rows_, 20000,
    )
    assert ok is False and "期限无法" in reason
    ok, reason = bond_tranches._validate_candidate(
        {"tranche_name": "品种一", "term_years": 3, "maturity_date": "2030-06-01", "issue_amount": None, "source_ids": ["a"]},
        rows_, 20000,
    )
    assert ok is False and "到期日无法" in reason


def test_partial_deterministic_result_uses_one_llm_fallback_and_preserves_clear_variety(monkeypatch):
    from backend.app.main import startup

    startup()
    project_id = "test-bond-tranche-partial"
    _project(project_id)
    monkeypatch.setattr(bond_tranches, "is_configured", lambda: True)
    calls = []

    def fake_llm(source_rows, issue_amount, context=None):
        calls.append(source_rows)
        return [{
            "tranche_name": "品种二", "term_years": 5, "maturity_date": "",
            "issue_amount": None, "amount_unit": "万元", "source_ids": ["a"],
            "extraction_method": "model",
        }], None

    monkeypatch.setattr(bond_tranches, "llm_extract_tranche_candidates", fake_llm)
    try:
        tranches = extract_bond_tranches(
            project_id,
            [{"id": "a", "kind": "募集说明书", "text": "品种一为3年期；同时设置品种二，其期限安排为5年整。"}],
            20000,
        )
        assert len(calls) == 1
        assert [(item["term_years"], item["extraction_method"]) for item in tranches] == [
            (3, "deterministic"), (5, "model"),
        ]
    finally:
        _cleanup(project_id)


def test_unresolved_conflict_creates_one_idempotent_review_task(monkeypatch):
    from backend.app.main import startup

    startup()
    project_id = "test-bond-tranche-conflict-task"
    _project(project_id)
    monkeypatch.setattr(bond_tranches, "is_configured", lambda: True)
    monkeypatch.setattr(bond_tranches, "llm_extract_tranche_candidates", lambda *_args, **_kwargs: ([], None))
    sources = [{"id": "a", "kind": "募集说明书", "text": "品种一为3年期，品种一为5年期。"}]
    try:
        assert extract_bond_tranches(project_id, sources, 20000) == []
        assert extract_bond_tranches(project_id, sources, 20000) == []
        tasks = rows(
            "SELECT * FROM review_tasks WHERE project_id=? AND task_type='bond_tranche_verification' AND status='open'",
            (project_id,),
        )
        assert len(tasks) == 1
        assert "不同期限" in tasks[0]["description"]
    finally:
        _cleanup(project_id)


def test_loan_project_does_not_generate_bond_tranches(monkeypatch):
    from backend.app.main import startup

    startup()
    project_id = "test-loan-tranche-guard"
    _project(project_id, project_type="loan_guarantee")
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,ocr_status,page_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{project_id}-doc", project_id, "融资申请书.pdf", "融资申请", "application/pdf", "h", 100, "parsed", 0.9, 1, "", now, "native", "not_required", 1),
        )
        conn.execute(
            "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"{project_id}-src", project_id, f"{project_id}-doc", "page", "第 1 页", "品种一为3年期", 1, "[]", "native", "h"),
        )
    monkeypatch.setattr(workflow, "is_configured", lambda: False)
    try:
        with pytest.raises(RuntimeError, match="MODEL_REQUIRED"):
            workflow._extract_project_facts("run-1", project_id)
        assert not rows("SELECT id FROM bond_tranches WHERE project_id=?", (project_id,))
    finally:
        _cleanup(project_id)
