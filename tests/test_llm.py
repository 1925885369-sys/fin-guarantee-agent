from backend.app import llm
from backend.app.schemas import OneShotResult


def test_model_fact_output_filters_unknown_fields_and_sources(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({"facts": [
        {"fact_key": "revenue", "period": "2025", "value": 100, "unit": "万元", "raw_value": "100万元", "source_ids": ["allowed"], "confidence": 0.9},
        {"fact_key": "made_up", "period": "2025", "value": 1, "unit": "万元", "source_ids": ["allowed"], "confidence": 0.9},
        {"fact_key": "net_profit", "period": "2025", "value": 2, "unit": "万元", "source_ids": ["missing"], "confidence": 0.9},
    ]}, llm.LlmUsage()))
    facts, _ = llm.extract_facts([{"id": "allowed", "locator": "第1页", "text": "营业收入100万元"}])
    assert len(facts) == 1
    assert facts[0]["fact_key"] == "revenue"


def test_model_fact_output_accepts_nullable_optional_fields(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({"facts": [
        {
            "fact_key": "top_customer_share",
            "period": None,
            "value": 35,
            "unit": None,
            "text_value": None,
            "raw_value": None,
            "source_ids": ["allowed"],
            "confidence": None,
        },
    ]}, llm.LlmUsage()))

    facts, _ = llm.extract_facts([{"id": "allowed", "locator": "第1页", "text": "第一大客户占比35%"}])

    assert facts[0]["period"] == ""
    assert facts[0]["unit"] == ""
    assert facts[0]["confidence"] == 0.8


def test_report_rewrite_filters_unapproved_citations(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({"content": "收入100万元", "citation_ids": ["ok", "bad"]}, llm.LlmUsage()))
    result, _ = llm.rewrite_section("财务", "收入100万元", [{"id": "ok", "quote": "收入100万元"}])
    assert result.citation_ids == ["ok"]


def test_one_shot_accepts_model_field_and_single_source_aliases():
    result = OneShotResult.model_validate({
        "facts": [
            {
                "field": "revenue",
                "period": "2025",
                "value": 100,
                "unit": "万元",
                "source_id": "source-1",
            },
            {
                "field": "net_profit",
                "period": "2025",
                "value": 10,
                "unit": "万元",
                "citation_ids": ["source-2"],
            },
        ],
        "warning_keys": [],
        "citation_ids": ["source-1"],
    })

    assert result.facts[0].fact_key == "revenue"
    assert result.facts[0].source_ids == ["source-1"]
    assert result.facts[1].source_ids == ["source-2"]


def test_one_shot_normalizes_fact_mapping_and_reuses_top_level_citations():
    body = llm._normalize_one_shot_body({
        "facts": {"revenue": 9500, "main_business": "电子元器件贸易"},
        "warning_keys": {"revenue_tax": True, "cash_profit": False},
        "citation_ids": ["source-1", "source-2"],
    })

    result = OneShotResult.model_validate(body)

    assert result.warning_keys == ["revenue_tax"]
    assert result.facts[0].fact_key == "revenue"
    assert result.facts[0].value == 9500
    assert result.facts[0].source_ids == ["source-1", "source-2"]
    assert result.facts[1].text_value == "电子元器件贸易"
