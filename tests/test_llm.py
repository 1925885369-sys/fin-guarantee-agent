from backend.app import llm


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


def test_report_rewrite_filters_unapproved_citations(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({"content": "收入100万元", "citation_ids": ["ok", "bad"]}, llm.LlmUsage()))
    result, _ = llm.rewrite_section("财务", "收入100万元", [{"id": "ok", "quote": "收入100万元"}])
    assert result.citation_ids == ["ok"]
