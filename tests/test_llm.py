import json

from backend.app import llm
from backend.app.config import settings
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
    # 允许集以别名 E01 下发，E99 不在集内 → 过滤后只剩映射回真实 ID 的那条。
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({"content": "收入100万元", "citation_ids": ["E01", "E99"]}, llm.LlmUsage()))
    result, _ = llm.rewrite_section("财务", "收入100万元", [{"id": "ok", "quote": "收入100万元"}])
    assert result.citation_ids == ["ok"]


def test_report_rewrite_declares_aliased_citations_and_maps_them_back(monkeypatch):
    """引用 ID 以短别名下发，模型回显别名，解析后立即映射回真实 ID。

    别名只改模型看到的形式：白名单校验、落库与报告 API 拿到的仍是 ``c-*``。
    """
    captured = {}

    def fake_chat(_system, prompt, *_args, **_kwargs):
        captured.update(json.loads(prompt))
        return {"content": "Supported draft", "citation_ids": ["E01"]}, llm.LlmUsage()

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", fake_chat)

    result, _ = llm.rewrite_section(
        "Financial analysis",
        "Supported draft",
        [{"id": "c-source-1", "source_id": "source-1", "quote": "Supported draft"}],
    )

    assert captured["allowed_citation_ids"] == ["E01"]
    assert [item["id"] for item in captured["evidence"]] == ["E01"]
    assert "c-source-1" not in json.dumps(captured, ensure_ascii=False)
    assert captured["citation_rules"] == list(llm.REPORT_CITATION_RULES)
    assert result.citation_ids == ["c-source-1"]


def test_report_rewrite_rejects_source_ids_outside_the_alias_set(monkeypatch):
    """别名集之外的 ID 仍被拦下——别名不放松引用白名单。"""
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: (
        {"content": "收入100万元", "citation_ids": ["E01", "c-source-9"]}, llm.LlmUsage(),
    ))
    result, _ = llm.rewrite_section(
        "财务", "收入100万元", [{"id": "c-source-1", "quote": "收入100万元"}],
    )
    assert result.citation_ids == ["c-source-1"]


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


def test_json_parser_wraps_bare_fact_objects_without_inventing_fields():
    body = llm._parse_json_content(
        '{"fact_key":"revenue","period":"2025","value":100,"source_ids":["s1"]}'
        '{"fact_key":"net_profit","period":"2025","value":10,"source_ids":["s2"]}'
    )

    assert [item["fact_key"] for item in body["facts"]] == ["revenue", "net_profit"]
    assert body["citation_ids"] == ["s1", "s2"]


def test_one_shot_normalizes_common_model_schema_drift():
    body = llm._normalize_one_shot_body({
        "facts": [
            {
                "field": "revenue",
                "period": 2025,
                "value": "9,500万元",
                "scope": "company",
                "period_type": "annual",
                "source_id": {"id": "source-1"},
                "confidence": "90%",
            },
            {"fact_key": "net_profit", "value": "not disclosed", "source_ids": []},
            None,
        ],
        "warning_keys": [
            {"rule_key": "revenue_tax"},
            {"warning_key": "cash_profit"},
            "cash_profit",
        ],
        "citation_ids": [{"source_id": "source-1"}],
    })

    result = OneShotResult.model_validate(body)

    assert result.warning_keys == ["revenue_tax", "cash_profit"]
    assert len(result.facts) == 2
    assert result.facts[0].period == "2025"
    assert result.facts[0].value == 9500
    assert result.facts[0].scope == "unknown"
    assert result.facts[0].period_type == "unknown"
    assert result.facts[0].confidence == 0.9
    assert result.facts[1].value is None
    assert result.facts[1].source_ids == ["source-1"]


def test_one_shot_drops_uncited_or_unstructured_facts_and_caps_schema_lists():
    body = llm._normalize_one_shot_body({
        "facts": [
            {"fact_key": "revenue", "value": 100},
            "revenue=100",
            *(
                {"fact_key": f"field-{index}", "value": index, "source_id": "source-1"}
                for index in range(105)
            ),
        ],
        "warning_keys": [f"rule-{index}" for index in range(25)],
        "citation_ids": [],
    })

    result = OneShotResult.model_validate(body)

    assert len(result.facts) == 100
    assert all(fact.source_ids == ["source-1"] for fact in result.facts)
    assert len(result.warning_keys) == 19


def test_report_rewrite_uses_the_larger_report_input_budget(monkeypatch):
    """章节改写要带整章事实与证据上下文，必须用报告预算，而不是抽取批次预算。

    事实密集的章节（财务/债务）上下文接近 4 万字符：沿用批次上限时会在把 evidence
    裁剪到 128 字符后仍然超限，于是整章被放弃、退回确定性草稿。
    """
    captured: dict = {}

    def fake_chat(system_prompt, prompt, *, schema=None, normalizer=None,
                  allowed_source_ids=None, context=None, max_input_chars=None):
        captured["max_input_chars"] = max_input_chars
        return {"content": "收入100万元", "citation_ids": []}, llm.LlmUsage()

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    llm.rewrite_section("财务", "收入100万元", [{"id": "ok", "quote": "收入100万元"}])

    assert captured["max_input_chars"] == settings.llm_report_max_chars
    assert settings.llm_report_max_chars > settings.llm_batch_max_chars


def test_fit_json_prompt_only_trims_above_the_limit_it_is_given():
    payload = json.dumps({"evidence": [{"quote": "长" * 5000}]}, ensure_ascii=False)
    assert llm._fit_json_prompt(payload, 100000) == payload
    trimmed = llm._fit_json_prompt(payload, 2000)
    assert len(trimmed) <= 2000 < len(payload)


def test_length_repair_reuses_the_same_payload_and_aliases(monkeypatch):
    """篇幅修复是**唯一一次**额外调用：直接压缩被拒稿，不再先做一次主稿重写。

    修订稿 §3.3"每章最多一次额外内容调整"；此前 `rewrite_section(length_repair=…)` 会先做
    一次全量重写再压缩（真实链路 3 次调用），本用例把修复路径钉在一次调用上。
    """
    calls: list[dict] = []

    def fake_chat(system_prompt, prompt, *, schema=None, normalizer=None,
                  allowed_source_ids=None, context=None, max_input_chars=None):
        calls.append(json.loads(prompt))
        return ({"content": "修正后的正文", "citation_ids": ["E01"]},
                llm.LlmUsage(input_tokens=120, output_tokens=40, cost=0.012))

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_args, **_kwargs: "system")

    chapter_context = {
        "project_id": "project-1",
        "evidence": [{"id": "c-1", "source_id": "s-1", "quote": "收入100万元"}],
        "facts": [{"id": "f-1", "fact_key": "revenue", "period": "2025", "value": 100,
                   "unit": "万元", "source_ids": ["s-1"]}],
        "length_policy": {"effective_min_chars": 1000, "effective_max_chars": 2600, "hard_max_chars": 2600},
    }
    result, usage = llm.rewrite_section(
        "财务趋势与盈利质量", "草稿", [{"id": "c-1", "source_id": "s-1", "quote": "收入100万元"}],
        chapter_key="financial", chapter_context=chapter_context, strict_citations=True,
        length_repair={"message": "章节超出目标篇幅上限", "visible_content_chars": 3000,
                       "effective_min_chars": 1000, "effective_max_chars": 2600,
                       "previous_content": "过长正文" * 500},
    )

    # 只有一次调用：修复稿直接由压缩请求产出，不再先做一次主稿重写。
    assert len(calls) == 1
    repair = calls[0]["length_repair"]
    assert repair["target_min_chars"] == 1000 and repair["target_max_chars"] == 2600
    assert repair["rule"] == llm.LENGTH_REPAIR_RULE
    assert repair["previous_content"] == "过长正文" * 500

    # 同一份证据、同一别名表——修复不得扩大或改变证据范围
    assert calls[0]["allowed_citation_ids"] == ["E01"]
    assert "source-1" not in json.dumps(calls[0], ensure_ascii=False)

    assert result.content == "修正后的正文"
    assert result.citation_ids == ["c-1"]
    assert (usage.input_tokens, usage.output_tokens) == (120, 40)


def test_no_repair_call_without_a_length_verdict(monkeypatch):
    calls = []

    def fake_chat(system_prompt, prompt, *, schema=None, normalizer=None,
                  allowed_source_ids=None, context=None, max_input_chars=None):
        calls.append(json.loads(prompt))
        return {"content": "收入100万元", "citation_ids": []}, llm.LlmUsage()

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_args, **_kwargs: "system")
    llm.rewrite_section("财务", "收入100万元", [{"id": "c-1", "quote": "收入100万元"}], chapter_key="financial")
    assert len(calls) == 1


def test_repair_section_length_makes_exactly_one_call(monkeypatch):
    """压缩调用自身不再触发修复：整条链路最多“润色 1 次 + 压缩 1 次”。"""
    calls: list[dict] = []

    def fake_chat(system_prompt, prompt, *, schema=None, normalizer=None,
                  allowed_source_ids=None, context=None, max_input_chars=None):
        calls.append(json.loads(prompt))
        return {"content": "压缩后的正文", "citation_ids": []}, llm.LlmUsage(output_tokens=10)

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_args, **_kwargs: "system")

    result, _usage = llm.repair_section_length(
        "财务趋势与盈利质量", "确定性草稿", [{"id": "c-1", "quote": "收入100万元"}],
        chapter_key="financial", previous_content="第一次模型正文",
        length_policy={"message": "章节超出目标篇幅上限", "visible_content_chars": 3000,
                       "effective_min_chars": 1000, "effective_max_chars": 2600},
    )

    assert len(calls) == 1
    assert calls[0]["length_repair"]["previous_content"] == "第一次模型正文"
    assert calls[0]["length_repair"]["target_max_chars"] == 2600
    assert result.content == "压缩后的正文"


def test_repair_section_json_makes_exactly_one_call_with_bounded_raw_output(monkeypatch):
    """报告首次输出不可解析时，上层用同一份上下文做**恰好一次**修复（第三点细化方案 §3.2）。

    - 复用同一别名表与证据范围：真实 ``source_id`` 不随请求下发模型；
    - “原始输出 + 错误类型”按 ``_repair_message`` 语义回灌，并沿用 12000 字符限制；
    - 本函数自身不再触发任何后续修复（调用次数恒为 1）。
    """
    calls: list[dict] = []
    captured_sources: dict = {}

    def fake_chat(system_prompt, prompt, *, schema=None, normalizer=None,
                  allowed_source_ids=None, context=None, max_input_chars=None,
                  drop_report=None, evidence_sources=None):
        calls.append(json.loads(prompt))
        captured_sources["evidence_sources"] = evidence_sources
        return ({"content": "修复后的正文", "citation_ids": ["E01", "E99"]},
                llm.LlmUsage(input_tokens=10, output_tokens=5))

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_args, **_kwargs: "system")

    chapter_context = {
        "project_id": "project-1",
        "evidence": [{"id": "c-1", "source_id": "s-1", "quote": "收入100万元"}],
    }
    raw = "HEAD" + ("x" * 20000) + "TAIL"
    result, _usage = llm.repair_section_json(
        "财务趋势与盈利质量", "草稿",
        [{"id": "c-1", "source_id": "s-1", "quote": "收入100万元"}],
        chapter_key="financial", chapter_context=chapter_context,
        strict_citations=False, raw_content=raw, error_type="schema_error",
    )

    assert len(calls) == 1                                   # 恰好一次，不再触发后续修复
    repair = calls[0]["json_repair"]
    assert repair["error_type"] == "schema_error"
    assert "TAIL" in repair["instruction"]                   # 保留结尾原文
    assert "HEAD" not in repair["instruction"]               # 只回灌受限的尾部 12000 字符
    assert "<previous_output>" in repair["instruction"]
    # 同一别名表、同一证据范围：真实 source_id 不下发模型
    assert calls[0]["allowed_citation_ids"] == ["E01"]
    assert "s-1" not in json.dumps(calls[0], ensure_ascii=False)
    assert captured_sources["evidence_sources"] == {"E01": "s-1"}
    # 返回结果按别名映射回真实引用，并被 strict_citations 过滤
    assert result.content == "修复后的正文"
    assert result.citation_ids == ["c-1"]


def test_report_request_uses_the_effective_config_from_context(monkeypatch):
    """required_sections / preserve_blocks 取自上下文的当前生效配置，而不是出厂 YAML。

    否则会出现"前端保存配置 ≠ 模型实际请求 ≠ 章节校验"的三方不一致。
    """
    captured: dict = {}

    def fake_chat(system_prompt, prompt, *, schema=None, normalizer=None,
                  allowed_source_ids=None, context=None, max_input_chars=None):
        captured.update(json.loads(prompt))
        return {"content": "正文", "citation_ids": []}, llm.LlmUsage()

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", fake_chat)
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_args, **_kwargs: "system")

    llm.rewrite_section(
        "财务趋势与盈利质量", "草稿", [{"id": "c-1", "quote": "收入100万元"}], chapter_key="financial",
        chapter_context={
            "project_id": "project-1",
            "prompt_config": {"required_sections": ["自定义区块"], "preserve_blocks": ["自定义保留块"]},
        },
    )

    assert captured["required_sections"] == ["自定义区块"]
    assert captured["preserve_blocks"] == ["自定义保留块"]
