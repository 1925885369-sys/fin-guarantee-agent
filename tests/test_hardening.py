from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import replace

import pytest

from backend.app import llm, workflow
from backend.app.database import connect, execute, now_iso, row, rows
from backend.app.facts import canonical_key, deterministic_extract
from backend.app.main import startup
from backend.app.schemas import LlmSection
from scripts.evaluate import classify_error, evaluation_key, evaluation_metadata, summarize


def test_oversized_structured_prompt_is_trimmed_without_breaking_json():
    prompt = json.dumps({"task": "extract", "sources": [{"id": "s1", "text": "证据" * 5000}]}, ensure_ascii=False)
    fitted = llm._fit_json_prompt(prompt, 1000)
    parsed = json.loads(fitted)
    assert len(fitted) <= 1000
    assert parsed["sources"][0]["id"] == "s1"
    assert parsed["sources"][0]["text"]


def test_oversized_unstructured_prompt_is_rejected_instead_of_truncated():
    with pytest.raises(llm.StructuredLlmError) as exc_info:
        llm._fit_json_prompt("x" * 2000, 1000)
    assert exc_info.value.error_type == "scope_error"


class FakeHttpError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.response = type("Response", (), {"status_code": status_code})()


class FakeResponse:
    def __init__(self, content: str = "{}", status_code: int = 200, finish_reason: str = "stop"):
        self.content = content
        self.status_code = status_code
        self.finish_reason = finish_reason

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FakeHttpError(self.status_code)

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}, "finish_reason": self.finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


class FakeClient:
    def __init__(self, responses, calls):
        self.responses = responses
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, *_args, **_kwargs):
        self.calls.append(1)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _settings():
    return replace(
        llm.settings, llm_api_key="test-key", llm_base_url="https://invalid.test",
        max_run_model_cost_cny=2, max_run_model_calls=40,
    )


def test_structured_client_retries_429_then_succeeds(monkeypatch):
    calls = []
    responses = [FakeResponse(status_code=429), FakeResponse('{"content":"材料不足","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)

    body, usage = llm._chat_json("system", "prompt", schema=LlmSection)

    assert body["content"] == "材料不足"
    assert len(calls) == 2
    assert usage.cost > 0


def test_structured_client_uses_split_timeout_configuration(monkeypatch):
    captured = {}
    calls = []
    responses = [FakeResponse('{"content":"ok","citation_ids":[]}')]

    def client_factory(**kwargs):
        captured.update(kwargs)
        return FakeClient(responses, calls)

    configured = replace(
        _settings(), llm_connect_timeout_seconds=11, llm_read_timeout_seconds=61,
        llm_write_timeout_seconds=31, llm_pool_timeout_seconds=12,
    )
    monkeypatch.setattr(llm, "settings", configured)
    monkeypatch.setattr(llm.httpx, "Client", client_factory)
    llm._chat_json("system", "prompt", schema=LlmSection)

    timeout = captured["timeout"]
    assert timeout.connect == 11
    assert timeout.read == 61
    assert timeout.write == 31
    assert timeout.pool == 12


def test_structured_client_returns_non_sensitive_response_diagnostics(monkeypatch):
    content = '{"content":"ok","citation_ids":[]}'
    calls = []
    responses = [FakeResponse(content, finish_reason="stop")]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))

    _, usage = llm._chat_json("system", "prompt", schema=LlmSection)

    assert usage.finish_reason == "stop"
    assert usage.response_chars == len(content)
    assert usage.response_keys == ["citation_ids", "content"]
    assert usage.response_field_counts == {"citation_ids": 0}
    assert usage.response_value_types == {"content": "str", "citation_ids": "list"}


def test_structured_client_retries_5xx_and_stops_after_three_timeouts(monkeypatch):
    calls = []
    responses = [FakeResponse(status_code=500), FakeResponse('{"content":"恢复","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    assert llm._chat_json("system", "prompt", schema=LlmSection)[0]["content"] == "恢复"
    assert len(calls) == 2

    calls.clear()
    responses[:] = [llm.httpx.ReadTimeout("timeout") for _ in range(3)]
    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection)
    assert captured.value.error_type == "transport"
    assert len(calls) == 3


def test_structured_client_falls_back_when_thinking_parameter_is_rejected(monkeypatch):
    calls = []
    payloads = []
    responses = [FakeResponse(status_code=400), FakeResponse('{"content":"ok","citation_ids":[]}')]

    class CapturingClient(FakeClient):
        def post(self, *_args, **kwargs):
            payloads.append(copy.deepcopy(kwargs["json"]))
            return super().post(*_args, **kwargs)

    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: CapturingClient(responses, calls))

    body, _ = llm._chat_json("system", "prompt", schema=LlmSection)

    assert body["content"] == "ok"
    assert "thinking" in payloads[0]
    assert "thinking" not in payloads[1]


@pytest.mark.parametrize("status,error_type", [(401, "authentication"), (403, "authentication"), (404, "model_not_found")])
def test_structured_client_does_not_retry_permanent_http_errors(monkeypatch, status, error_type):
    calls = []
    responses = [FakeResponse(status_code=status)]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection)

    assert captured.value.error_type == error_type
    assert len(calls) == 1


def test_structured_client_repairs_invalid_json_once(monkeypatch):
    calls = []
    responses = [FakeResponse("not-json"), FakeResponse('{"content":"已修复","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))

    body, _ = llm._chat_json("system", "prompt", schema=LlmSection)

    assert body["content"] == "已修复"
    assert len(calls) == 2


@pytest.mark.parametrize("content", [
    '```json\n{"content":"ok","citation_ids":[]}\n```',
    'Here is the JSON result:\n{"content":"ok","citation_ids":[]}\nDone.',
])
def test_structured_client_parses_wrapped_json_without_repair(monkeypatch, content):
    calls = []
    responses = [FakeResponse(content)]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))

    body, _ = llm._chat_json("system", "prompt", schema=LlmSection)

    assert body["content"] == "ok"
    assert len(calls) == 1


def test_structured_client_sends_invalid_output_to_repair(monkeypatch):
    calls = []
    payloads = []
    responses = [FakeResponse("broken-json-marker"), FakeResponse('{"content":"ok","citation_ids":[]}')]

    class CapturingClient(FakeClient):
        def post(self, *_args, **kwargs):
            payloads.append(copy.deepcopy(kwargs["json"]))
            return super().post(*_args, **kwargs)

    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: CapturingClient(responses, calls))

    body, _ = llm._chat_json("system", "prompt", schema=LlmSection)

    assert body["content"] == "ok"
    assert len(payloads) == 2
    assert "broken-json-marker" not in json.dumps(payloads[0])
    assert "broken-json-marker" in json.dumps(payloads[1])
    assert "only one complete JSON object" in payloads[1]["messages"][-1]["content"]
    assert payloads[0]["thinking"] == {"type": "disabled"}


def test_extract_prompt_limits_registry_to_relevant_document_categories(monkeypatch):
    captured = {}

    def fake_chat(_system, prompt, **_kwargs):
        captured.update(json.loads(prompt))
        return {"facts": []}, llm.LlmUsage()

    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    monkeypatch.setattr(llm, "is_configured", lambda: True)

    llm.extract_facts([{
        "id": "s1", "kind": "抵押物", "locator": "第1页",
        "text": "折后抵押价值600万元，评估报告出具于8个月前。",
    }])

    registry = captured["field_registry"]
    assert "collateral_discounted_value" in registry
    assert "collateral_valuation_age_months" in registry
    assert "revenue" not in registry
    assert len(registry) < len(llm.FACT_REGISTRY)
    assert any("缺失字段直接省略" in item for item in captured["output_rules"])


def test_invalid_json_diagnostic_does_not_include_response_content():
    error = llm.StructuredLlmError(
        "invalid_json",
        "JSONDecodeError pos=42 chars=3500 finish_reason=length output_tokens=3500",
    )
    diagnostic = llm._safe_error(error, "invalid_json")
    assert "finish_reason=length" in diagnostic
    assert "output_tokens=3500" in diagnostic


def test_schema_error_diagnostic_only_contains_validation_locations():
    from pydantic import ValidationError

    try:
        LlmSection.model_validate({"content": 1, "citation_ids": ["ok"]})
    except ValidationError as exc:
        diagnostic = llm._safe_error(exc, "schema_error")
    else:
        raise AssertionError("expected schema validation error")

    assert "content" in diagnostic
    assert "input_value" not in diagnostic


def test_structured_client_rejects_unknown_source_without_retry(monkeypatch):
    calls = []
    responses = [FakeResponse('{"content":"收入100万元","citation_ids":["invented"]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection, allowed_source_ids={"allowed"})

    assert captured.value.error_type == "invalid_source"
    assert len(calls) == 1


def test_llm_attempt_telemetry_is_hashed_and_redacted(monkeypatch):
    startup()
    run_id = f"telemetry-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (id,project_id,status,current_node,progress,message,cancel_requested,started_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, "demo-bond-huachen", "running", "draft", 90, "test", 0, now_iso()),
        )
    calls = []
    responses = [FakeResponse('{"content":"材料不足","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: FakeClient(responses, calls))
    try:
        llm._chat_json(
            "system-secret", "高度敏感的完整原文", schema=LlmSection,
            context=llm.LlmCallContext("demo-bond-huachen", run_id, "draft", "report"),
        )
        attempt = row("SELECT * FROM llm_attempts WHERE run_id=?", (run_id,))
        serialized = json.dumps(attempt, ensure_ascii=False)
        assert attempt["schema_name"] == "LlmSection"
        assert len(attempt["request_hash"]) == 64
        assert "system-secret" not in serialized
        assert "高度敏感的完整原文" not in serialized
        assert "test-key" not in serialized
    finally:
        execute("DELETE FROM llm_attempts WHERE run_id=?", (run_id,))
        execute("DELETE FROM runs WHERE id=?", (run_id,))


def test_evaluation_identity_error_classification_and_stability():
    metadata = evaluation_metadata()
    first = evaluation_key("agent", "demo-bond-huachen", 1, "deepseek-v4-flash", metadata)
    assert first == evaluation_key("agent", "demo-bond-huachen", 1, "deepseek-v4-flash", metadata)
    assert first != evaluation_key("agent", "demo-bond-huachen", 2, "deepseek-v4-flash", metadata)
    assert classify_error(FakeHttpError(429)) == "rate_limit"
    assert classify_error(RuntimeError("模型调用失败（schema_error）")) == "schema_error"
    payload = summarize([
        {"case": "demo", "status": "completed", "risk_recall": 0.8, "model_cost_cny": 0.1},
        {"case": "demo", "status": "completed", "risk_recall": 1.0, "model_cost_cny": 0.2},
        {"case": "demo", "status": "failed", "error_type": "transport", "model_cost_cny": 0.0},
    ])
    assert payload["success_rate"] == pytest.approx(2 / 3, abs=0.0001)
    assert payload["statistics"]["risk_recall"]["stddev"] == 0.1
    assert payload["per_case_stability"]["demo"]["risk_recall"]["range"] == 0.2
    assert payload["error_counts"] == {"transport": 1}


def test_candidate_retrieval_deduplicates_and_caps(monkeypatch):
    calls = []

    def fake_retrieve(_project_id, query, top_k):
        calls.append((query, top_k))
        return [{"id": "shared"}, *({"id": f"{len(calls)}-{index}"} for index in range(8))]

    monkeypatch.setattr(workflow, "retrieve", fake_retrieve)
    evidence = workflow.retrieve_candidate_evidence("project", cap=36)

    assert len(calls) == 6
    assert all(top_k == 8 for _, top_k in calls)
    assert len({item["id"] for item in evidence}) == len(evidence)
    assert len(evidence) == 36


def test_extraction_batches_limit_source_count_and_characters():
    sources = [
        {"id": "s1", "text": "a" * 4},
        {"id": "s2", "text": "b" * 4},
        {"id": "s3", "text": "c" * 4},
        {"id": "s4", "text": "d" * 4},
    ]

    batches = workflow._split_extraction_sources(sources, max_chars=9, max_sources=3)

    assert [[item["id"] for item in batch] for batch in batches] == [["s1", "s2"], ["s3", "s4"]]


def test_short_extraction_source_set_is_preserved_in_original_order():
    sources = [
        {"id": "s2", "document_id": "d1", "text": "second"},
        {"id": "s1", "document_id": "d1", "text": "first"},
    ]

    selected = workflow._select_model_extraction_sources(
        sources, [], max_sources=48, per_topic=2,
    )

    assert [item["id"] for item in selected] == ["s2", "s1"]


def test_long_extraction_selection_covers_topics_documents_and_deduplicates():
    sources = []
    expected_topic_sources = set()
    for index, terms in enumerate(workflow.EXTRACTION_TOPIC_TERMS.values()):
        source_id = f"topic-{index:02d}"
        expected_topic_sources.add(source_id)
        sources.append({
            "id": source_id,
            "document_id": "prospectus" if index % 2 == 0 else "rating",
            "text": f"{terms[0]} supporting disclosure",
        })
    sources.extend(
        {"id": f"boilerplate-{index:03d}", "document_id": "prospectus", "text": "generic disclosure"}
        for index in range(240)
    )
    sources.append(dict(sources[0]))

    selected = workflow._select_model_extraction_sources(
        sources, [], max_sources=48, per_topic=2,
    )

    selected_ids = [item["id"] for item in selected]
    assert len(selected_ids) <= 48
    assert len(selected_ids) == len(set(selected_ids))
    assert expected_topic_sources <= set(selected_ids)
    assert {item["document_id"] for item in selected} == {"prospectus", "rating"}
    source_positions = {}
    for index, item in enumerate(sources):
        source_positions.setdefault(item["id"], index)
    assert [source_positions[source_id] for source_id in selected_ids] == sorted(
        source_positions[source_id] for source_id in selected_ids
    )


def test_extraction_selection_prioritizes_source_with_more_deterministic_facts():
    sources = [
        {"id": "plain-1", "document_id": "d1", "text": "generic disclosure"},
        {"id": "rich", "document_id": "d1", "text": "generic disclosure"},
        {"id": "plain-2", "document_id": "d1", "text": "generic disclosure"},
    ]
    deterministic = [
        {"fact_key": "revenue", "source_ids": ["rich"]},
        {"fact_key": "net_profit", "source_ids": ["rich"]},
    ]

    selected = workflow._select_model_extraction_sources(
        sources, deterministic, max_sources=1, per_topic=1,
    )

    assert [item["id"] for item in selected] == ["rich"]


def test_extraction_budget_reserves_downstream_attempts_and_caps_cost(monkeypatch):
    configured = replace(
        workflow.settings,
        max_run_model_calls=40,
        llm_downstream_reserved_calls=12,
        max_run_model_cost_cny=2,
        llm_extract_max_cost_ratio=0.6,
    )
    monkeypatch.setattr(workflow, "settings", configured)

    monkeypatch.setattr(workflow, "_run_model_totals", lambda _run_id: (25, 1.19))
    assert workflow._extraction_budget_available("run")

    monkeypatch.setattr(workflow, "_run_model_totals", lambda _run_id: (26, 1.19))
    assert not workflow._extraction_budget_available("run")

    monkeypatch.setattr(workflow, "_run_model_totals", lambda _run_id: (0, 1.2))
    assert not workflow._extraction_budget_available("run")


def test_candidates_are_deduplicated_against_rule_title_and_evidence(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    common = {
        "rationale": "材料显示异常", "potential_impact": "可能影响资金使用",
        "likelihood": 2, "impact": 2, "support_source_ids": ["s1"], "counter_source_ids": [],
        "questions": ["请核验原因"], "mitigation": "补充凭证", "confidence": 0.8,
    }
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({"findings": [
        {**common, "taxonomy": "fund_diversion", "title": "用途覆盖不足"},
        {**common, "taxonomy": "fund_diversion", "title": "资金异常"},
        {**common, "taxonomy": "governance", "title": "审批权限集中"},
    ]}, llm.LlmUsage()))
    rules = [{"rule_key": "purpose_coverage", "title": "用途覆盖不足", "status": "warning", "source_ids": ["s1", "s2"]}]

    findings, _ = llm.identify_candidate_findings([{
        "id": "s1", "locator": "第1页", "text": "实控人掌握采购审批权限，材料显示异常。",
    }], rules)

    # A generic approval reference without payment control or independent-check
    # weakness is a review task, not a qualitative governance risk.
    assert findings == []


def test_governance_key_person_overlap_prefers_governance_control_risk():
    common = {
        "rationale": "The controlling shareholder holds payment approval authority.",
        "potential_impact": "Internal checks may be ineffective.",
        "likelihood": 2,
        "impact": 2,
        "support_source_ids": ["control-source"],
        "counter_source_ids": [],
        "questions": ["Verify delegated authority."],
        "mitigation": "Introduce independent approval.",
        "confidence": 0.8,
    }
    findings = [
        {**common, "taxonomy": "key_person", "title": "Key-person concentration"},
        {**common, "taxonomy": "governance", "title": "Approval authority concentration"},
        {
            **common,
            "taxonomy": "key_person",
            "title": "Lead engineer has no replacement",
            "rationale": "No successor is available for the lead engineer.",
            "support_source_ids": ["continuity-source"],
        },
        {
            **common,
            "taxonomy": "key_person",
            "title": "Named controller",
            "support_source_ids": ["controller-source"],
        },
        {
            **common,
            "taxonomy": "key_person",
            "title": "Renewal evidence missing",
            "support_source_ids": ["missing-source"],
        },
        {
            **common,
            "taxonomy": "governance",
            "title": "Guarantee approval is unclear",
            "support_source_ids": ["guarantee-source"],
        },
    ]
    source_text = {
        "control-source": "实控人控制采购审批和网银复核。",
        "continuity-source": "核心工程师离职后暂无替代人员。",
        "controller-source": "实控人为赵启明。",
        "missing-source": "未提供下一年度客户续约确认。",
        "guarantee-source": "企业现有对外担保300万元。",
    }

    output = llm._deduplicate_candidate_taxonomies(findings, source_text)

    assert [(item["taxonomy"], item["support_source_ids"]) for item in output] == [
        ("governance", ["control-source"]),
        ("key_person", ["continuity-source"]),
    ]
    assert output[0]["title"] == "关键审批权限集中"


def test_continuity_candidate_requires_negative_continuity_evidence():
    common = {
        "taxonomy": "continuity", "rationale": "Order coverage may be uncertain.",
        "potential_impact": "Operations could be affected.", "likelihood": 2, "impact": 2,
        "counter_source_ids": [], "questions": ["Verify orders."], "mitigation": "Obtain contracts.",
        "confidence": 0.8,
    }
    findings = [
        {**common, "title": "Order-to-financing mismatch", "support_source_ids": ["positive-order"]},
        {**common, "title": "Orders were cancelled", "support_source_ids": ["cancelled-order"]},
    ]
    source_text = {
        "positive-order": "企业目前在手订单1200万元。",
        "cancelled-order": "主要客户取消订单，生产计划中断。",
    }

    output = llm._deduplicate_candidate_taxonomies(findings, source_text)

    assert [(item["title"], item["support_source_ids"]) for item in output] == [
        ("Orders were cancelled", ["cancelled-order"]),
    ]


def test_financial_decline_and_conditional_guarantee_do_not_create_candidates():
    def candidate(taxonomy: str, source_id: str) -> dict:
        return {
            "taxonomy": taxonomy, "title": "待核验事项", "rationale": "材料披露相关事项。",
            "potential_impact": "可能产生影响。", "likelihood": 2, "impact": 2,
            "support_source_ids": [source_id], "counter_source_ids": [], "questions": [],
            "mitigation": "补充材料核验。", "confidence": 0.8,
        }

    assert llm._normalize_candidate_taxonomy(
        candidate("continuity", "financial"),
        {"financial": "2023至2025年利润及经营现金流连续下降。"},
    ) is None
    assert llm._normalize_candidate_taxonomy(
        candidate("continuity", "subsidiary"),
        {"subsidiary": "发行人子公司主要客户取消订单，生产计划中断。"},
    ) is None
    assert llm._normalize_candidate_taxonomy(
        candidate("collateral_enforceability", "conditional"),
        {"conditional": "如担保人发生减资、合并、停产停业，债权人有权要求追加担保。"},
    ) is None


def test_model_facts_do_not_create_conflicts_with_same_source_extraction():
    base = {
        "subject": "Test borrower", "scope": "standalone", "period": "2025", "unit": "万元",
        "text_value": "", "confidence": 0.9,
    }
    deterministic = [
        {**base, "fact_key": "revenue", "value": 2800, "source_ids": ["finance-source"]},
        {**base, "fact_key": "supporting_contract_amount", "value": 680, "source_ids": ["operations-source"]},
    ]
    model_facts = [
        {**base, "fact_key": "revenue", "value": 3000, "source_ids": ["finance-source"]},
        {**base, "fact_key": "revenue", "value": 680, "source_ids": ["operations-source"]},
        {**base, "fact_key": "revenue", "value": 2700, "source_ids": ["independent-source"]},
        {**base, "period": "", "fact_key": "revenue", "value": 9999, "source_ids": ["finance-source"]},
    ]

    output = workflow._filter_redundant_model_facts(deterministic, model_facts)

    assert [(item["value"], item["source_ids"]) for item in output] == [(2700, ["independent-source"])]


def test_model_facts_require_field_semantics_in_the_cited_source():
    base = {
        "subject": "Test borrower", "scope": "standalone", "period": "2025", "unit": "万元",
        "text_value": "", "confidence": 0.9,
    }
    model_facts = [
        {**base, "fact_key": "revenue", "value": 48, "source_ids": ["customer-share"]},
        {**base, "fact_key": "total_liabilities", "value": 700, "source_ids": ["specific-debt"]},
        {**base, "fact_key": "revenue", "value": 6800, "source_ids": ["audit"]},
    ]
    source_text = {
        "customer-share": "2025年第一大客户收入占比48%。",
        "specific-debt": "2025年有息负债700万元。",
        "audit": "审计报告列示2025年营业收入6800万元。",
    }

    output = workflow._filter_redundant_model_facts([], model_facts, source_text)

    assert [(item["fact_key"], item["value"]) for item in output] == [("revenue", 6800)]


def test_explicit_governance_control_concentration_has_deterministic_candidate(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_structured_call", lambda *_args, **_kwargs: ({"findings": []}, llm.LlmUsage()))
    evidence = [{
        "id": "control-source", "locator": "第2段",
        "text": "实控人同时负责销售、采购审批与网银复核，财务负责人无法独立否决付款。",
    }]

    findings, usage = llm.identify_candidate_findings(evidence, [])

    assert usage.cost == 0
    assert [(item["taxonomy"], item["support_source_ids"]) for item in findings] == [
        ("governance", ["control-source"]),
    ]


def test_node_retry_reuses_unchanged_upstream_artifacts_and_blocks_parallel_run():
    startup()
    first = workflow.start_run("demo-bond-ancheng")
    with pytest.raises(ValueError, match="活动任务"):
        workflow.start_run("demo-bond-ancheng")
    for _ in range(9000):  # ≈180s：本机冷启动演示运行可达 40–90s，30s 会把"未跑完"误判为失败
        current = row("SELECT status FROM runs WHERE id=?", (first,))
        if current and current["status"] in workflow.TERMINAL:
            break
        time.sleep(0.02)
    assert current["status"] == "completed"
    execute("UPDATE run_nodes SET status='degraded' WHERE run_id=? AND node_key='candidate'", (first,))

    second = workflow.retry_run(first, "candidate")
    for _ in range(9000):  # ≈180s：本机冷启动演示运行可达 40–90s，30s 会把"未跑完"误判为失败
        current = row("SELECT status,resumed_from_run_id FROM runs WHERE id=?", (second,))
        if current and current["status"] in workflow.TERMINAL:
            break
        time.sleep(0.02)

    assert current["status"] == "completed"
    assert current["resumed_from_run_id"] == first
    reused = rows("SELECT node_key,source_run_id FROM run_artifacts WHERE run_id=? AND source_run_id<>''", (second,))
    assert {item["node_key"] for item in reused} == {"inventory", "extract", "conflict", "metrics", "cross_check"}
    assert {item["source_run_id"] for item in reused} == {first}

    canonical = row("SELECT id,resolution_note FROM canonical_facts WHERE project_id=? ORDER BY rowid LIMIT 1", ("demo-bond-ancheng",))
    execute("UPDATE canonical_facts SET resolution_note=? WHERE id=?", ("人工复核触发指标失效", canonical["id"]))
    try:
        third = workflow.retry_run(second, "candidate")
        for _ in range(9000):  # ≈180s：本机冷启动演示运行可达 40–90s，30s 会把"未跑完"误判为失败
            current = row("SELECT status,resumed_from_run_id FROM runs WHERE id=?", (third,))
            if current and current["status"] in workflow.TERMINAL:
                break
            time.sleep(0.02)
        assert current["status"] == "completed"
        third_reused = rows("SELECT node_key FROM run_artifacts WHERE run_id=? AND source_run_id<>''", (third,))
        assert {item["node_key"] for item in third_reused} == {"inventory", "extract", "conflict"}
        assert row("SELECT attempt FROM run_nodes WHERE run_id=? AND node_key='metrics'", (third,))["attempt"] == 1
    finally:
        execute("UPDATE canonical_facts SET resolution_note=? WHERE id=?", (canonical["resolution_note"], canonical["id"]))


def test_candidate_failure_degrades_without_losing_deterministic_report(monkeypatch):
    startup()
    monkeypatch.setattr(workflow, "identify_candidate_findings", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("candidate unavailable")))
    run_id = workflow.start_run("demo-bond-huachen")
    for _ in range(9000):  # ≈180s：本机冷启动演示运行可达 40–90s，30s 会把"未跑完"误判为失败
        current = row("SELECT status FROM runs WHERE id=?", (run_id,))
        if current and current["status"] in workflow.TERMINAL:
            break
        time.sleep(0.02)

    assert current["status"] == "completed"
    assert row("SELECT status FROM run_nodes WHERE run_id=? AND node_key='candidate'", (run_id,))["status"] == "degraded"
    report = rows("SELECT content FROM report_sections WHERE project_id=?", ("demo-bond-huachen",))
    assert any("定性风险识别未完成" in item["content"] for item in report)
    assert row("SELECT COUNT(*) count FROM risks WHERE project_id=? AND source_type='rule'", ("demo-bond-huachen",))["count"] > 0


def test_report_rewrite_failure_falls_back_per_section(monkeypatch):
    startup()
    monkeypatch.setattr(workflow, "is_configured", lambda: True)
    # 模拟 is_configured 会同时放开抽取节点的模型分支；本用例只验证草稿改写失败的回退，
    # 因此把模型抽取置空，让分节确定性抽取承担事实来源。
    monkeypatch.setattr(workflow, "extract_facts", lambda *_args, **_kwargs: ([], workflow.LlmUsage()))
    monkeypatch.setattr(workflow, "rewrite_section", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rewrite unavailable")))
    run_id = workflow.start_run("demo-bond-huachen")
    for _ in range(9000):  # ≈180s：本机冷启动演示运行可达 40–90s，30s 会把"未跑完"误判为失败
        current = row("SELECT status FROM runs WHERE id=?", (run_id,))
        if current and current["status"] in workflow.TERMINAL:
            break
        time.sleep(0.02)
    assert current["status"] == "completed"
    assert row("SELECT COUNT(*) count FROM report_sections WHERE project_id=?", ("demo-bond-huachen",))["count"] == 17


def test_unsupported_report_fallback_keeps_non_null_support_rate():
    startup()
    project_id = f"fallback-{uuid.uuid4()}"
    section_id = f"section-{uuid.uuid4()}"
    template = workflow.SectionTemplate(
        key="financial",
        title="Financial analysis",
        content="Revenue was 9999 ten-thousand yuan.",
        source_ids=[],
    )
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (project_id, "token", "Fallback test", 500, 12, "Working capital", "review", now_iso(), now_iso(), now_iso()),
            )
            conn.execute(
                "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (section_id, project_id, template.key, template.title, template.content, "[]", "draft", now_iso()),
            )

        fallback_count = workflow._fallback_unsupported_sections(project_id, {template.key: template})
        quality = workflow.validate_report_support(project_id)
        section = row(
            "SELECT content,support_rate,validation_status,delivery_status,fallback_stage,content_origin "
            "FROM report_sections WHERE id=?", (section_id,))

        # 交付一致性修复后：单次候选回退；无法构建合格替代稿时保留可读正文并标记待人工复核，
        # 不再做第二轮"占位符降级"，也不再把不支持主张静默删除以换取干净指标。
        assert fallback_count == 1
        assert "本章节内容保留为草稿" not in section["content"]
        assert section["content"].strip() == template.content.strip()
        assert section["delivery_status"] == "needs_review"
        assert section["fallback_stage"] == "report_support"
        assert section["support_rate"] is not None
        assert quality["unsupported_claims"] >= 1
        assert quality["needs_review_sections"] >= 1
        assert row(
            "SELECT COUNT(*) count FROM audit_events WHERE project_id=? AND action='fallback'",
            (project_id,),
        )["count"] == 1
    finally:
        with connect() as conn:
            for table in ("report_claims", "audit_events", "report_sections"):
                conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_report_validation_warning_keeps_run_in_human_review(monkeypatch):
    startup()
    monkeypatch.setattr(workflow, "is_configured", lambda: False)
    # 桩必须与 p0.validate_report_support 的返回契约一致：工作流在质量校验后读取
    # quality["status"]（workflow.py:1442）。缺少该键会抛 KeyError('status')，
    # 运行随即被异常处理器改写为“分析失败”，与下方断言形成竞态。
    monkeypatch.setattr(
        workflow,
        "validate_report_support",
        lambda _project_id: {
            "unsupported_claims": 2, "invalid_citations": 1, "support_rate": 0.8, "status": "warning",
        },
    )
    run_id = workflow.start_run("demo-bond-huachen")
    for _ in range(9000):  # ≈180s：本机冷启动演示运行可达 40–90s，30s 会把"未跑完"误判为失败
        current = row("SELECT status FROM runs WHERE id=?", (run_id,))
        if current and current["status"] in workflow.TERMINAL:
            break
        time.sleep(0.02)

    assert current["status"] == "completed"
    assert row("SELECT status FROM run_nodes WHERE run_id=? AND node_key='verify'", (run_id,))["status"] == "warning"
    assert "人工复核" in row("SELECT message FROM runs WHERE id=?", (run_id,))["message"]
