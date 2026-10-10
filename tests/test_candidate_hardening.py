from __future__ import annotations

from backend.app import llm
from backend.app.bond_documents import CASE_SPECS
from backend.app.database import rows
from backend.app.main import startup
from backend.app.workflow import retrieve_candidate_evidence


def test_qualitative_signal_page_is_reserved_for_candidate_retrieval():
    startup()
    evidence = retrieve_candidate_evidence("demo-bond-qualitative")
    text = "\n".join(item.get("text", "") for item in evidence)
    assert "共用联系人" in text or "缺少客户盖章" in text


def test_bond_deterministic_candidate_taxonomies_match_gold(monkeypatch):
    startup()
    monkeypatch.setattr(llm, "is_configured", lambda: False)
    expected = {item.id: set(item.expected_candidate_taxonomies) for item in CASE_SPECS}
    for project_id, taxonomies in expected.items():
        evidence = retrieve_candidate_evidence(project_id)
        checks = rows("SELECT * FROM cross_checks WHERE project_id=?", (project_id,))
        findings, _ = llm.identify_candidate_findings(evidence, checks)
        assert {item["taxonomy"] for item in findings} == taxonomies, project_id


def test_material_insufficiency_does_not_become_governance_candidate():
    source = [{"id": "s1", "text": "治理机制披露概括，尚未提供具体审批权限和制衡执行证据。"}]
    findings, _ = llm.identify_candidate_findings(source, [])
    assert findings == []


def test_bond_warning_suppresses_duplicate_collateral_candidate(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_structured_call", lambda *_args, **_kwargs: ({"findings": [{
        "taxonomy": "collateral_enforceability", "title": "反担保登记风险",
        "rationale": "质押通知未送达，可能影响效力", "potential_impact": "处置受限",
        "likelihood": 2, "impact": 3, "support_source_ids": ["s1"], "counter_source_ids": [],
        "questions": ["核验登记"], "mitigation": "补充登记证明", "confidence": 0.8,
    }]}, llm.LlmUsage()))
    findings, _ = llm.identify_candidate_findings(
        [{"id": "s1", "text": "质押通知未送达债务人，可能影响质押效力。"}],
        [{"rule_key": "counter_guarantee", "status": "warning", "title": "反担保净覆盖与权利状态", "source_ids": ["s1"]}],
    )
    assert findings == []


def test_governance_does_not_treat_actual_controller_as_control_weakness():
    source = [{
        "id": "ordinary-governance",
        "text": "发行人披露实际控制人为张某，付款执行审批流程，未披露独立制衡异常。",
    }]
    assert llm.deterministic_governance_candidates(source) == []


def test_governance_does_not_treat_customer_concentration_as_permission_concentration():
    source = [{
        "id": "ordinary-concentration",
        "text": "实际控制人持股59%，第一大客户占比25%，前五大客户占比55%；合同、印章和付款权限执行岗位分离。",
    }]
    assert llm.deterministic_governance_candidates(source) == []


def test_key_person_candidate_with_authenticity_evidence_is_merged_into_authenticity():
    source_text = {
        "s1": "实际控制人持股59%，合同、印章和付款权限执行岗位分离。",
        "s2": "两份大额合同缺少客户盖章，关键销售负责人离职后尚未完成客户交接。",
    }
    finding = {
        "taxonomy": "key_person", "title": "关键审批权限集中", "rationale": "关键销售负责人离职",
        "potential_impact": "交易和回款核验存在不确定性", "likelihood": 2, "impact": 2,
        "support_source_ids": ["s1", "s2"], "counter_source_ids": [],
        "questions": ["核验交接"], "mitigation": "补充交接和合同证据", "confidence": 0.7,
    }
    normalized = llm._normalize_candidate_taxonomy(finding, source_text)
    assert normalized is not None
    assert normalized["taxonomy"] == "authenticity"


def test_qualitative_model_overgeneration_is_reduced_to_one_authenticity_candidate(monkeypatch):
    startup()
    evidence = retrieve_candidate_evidence("demo-bond-qualitative")
    authenticity_source = next(item for item in evidence if "缺少客户盖章" in item.get("text", ""))
    governance_source = next(item for item in evidence if "实际控制人" in item.get("text", "") and "岗位分离" in item.get("text", ""))
    support = [authenticity_source["id"], governance_source["id"]]

    def candidate(taxonomy: str, title: str) -> dict:
        return {
            "taxonomy": taxonomy, "title": title, "rationale": "合同缺少盖章且关键销售负责人离职",
            "potential_impact": "交易真实性和回款核验存在不确定性", "likelihood": 2, "impact": 2,
            "support_source_ids": support, "counter_source_ids": [],
            "questions": ["核验合同和人员交接"], "mitigation": "补充签章、函证和交接证据", "confidence": 0.8,
        }

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_structured_call", lambda *_args, **_kwargs: ({"findings": [
        candidate("authenticity", "合同真实性风险"),
        candidate("governance", "关键审批权限集中"),
        candidate("key_person", "关键人员离职风险"),
    ]}, llm.LlmUsage()))

    findings, _ = llm.identify_candidate_findings(evidence, [])
    assert len(findings) == 1
    assert findings[0]["taxonomy"] == "authenticity"
