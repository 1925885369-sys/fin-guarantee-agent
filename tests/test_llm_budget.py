"""报告任务的输入预算与 JSON 修复链约束（第三点细化方案 §2.1 / §3.2）。

- §2.1：报告任务输入预算不足时，按章节相关性选取**完整记录和完整证据片段**，
  **不截断单元格或任意截取原文**；无法容纳必要材料时返回 ``input_budget_exceeded``。
- §3.2：报告任务**关闭底层独立 JSON 修复链**，JSON/Schema 修复占用上层那次机会；
  网络失败仍只重试同一请求，尝试分别记账。

本文件只覆盖 ``backend/app/llm.py`` 的 ``_fit_json_prompt`` / ``_chat_json`` 行为，
不触库（``_persist_attempt`` 在无 ``run_id`` 时本就短路）。
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from backend.app import llm
from backend.app.schemas import LlmSection


# --------------------------------------------------------------------- 工具 --

def _settings():
    return replace(
        llm.settings, llm_api_key="test-key", llm_base_url="https://invalid.test",
        max_run_model_cost_cny=2, max_run_model_calls=40,
    )


def _report_request(evidence, facts=(), system_tables="", extra_context=None):
    """按 ``_report_request`` 的形状构造一份最小的报告请求体。"""
    return {
        "section": "财务",
        "generation_scope": "直接撰写",
        "allowed_citation_ids": [item["id"] for item in evidence],
        "evidence": list(evidence),
        "chapter_context": {
            "project_id": "p1",
            "facts": list(facts),
            **(extra_context or {}),
        },
        "system_tables": system_tables,
    }


class _FakeResponse:
    def __init__(self, content: str = "{}", status_code: int = 200, finish_reason: str = "stop"):
        self.content = content
        self.status_code = status_code
        self.finish_reason = finish_reason

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {
            "choices": [{"message": {"content": self.content}, "finish_reason": self.finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


class _FakeClient:
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


# ------------------------------------------------- 1. 报告超预算只丢整条 --

def test_report_over_budget_drops_whole_items_and_keeps_strings_intact():
    evidence = [{"id": f"c-{i}", "quote": "证据" * 120 + f"-{i}"} for i in range(10)]
    facts = [{"fact_key": "revenue", "period": "2025", "value": 100,
              "unit": "万元", "raw_value": "收入100万元"}]
    request = _report_request(evidence, facts)
    original = json.dumps(request, ensure_ascii=False)
    limit = 1500
    assert len(original) > limit

    fitted = llm._fit_json_prompt(original, limit, drop_items_only=True)

    assert len(fitted) <= limit
    parsed = json.loads(fitted)
    survivors = parsed["evidence"]
    # 确实丢失了一部分：不是原样返回，也不是全丢。
    assert 0 < len(survivors) < len(evidence)
    # 丢的是**完整条目**：存活项是原列表的前缀，逐条逐字与原值完全一致。
    assert survivors == evidence[: len(survivors)]
    for kept, source in zip(survivors, evidence):
        assert kept["quote"] == source["quote"]      # 无一被截断
    # evidence 尚未丢完就不会碰 facts。
    assert parsed["chapter_context"]["facts"] == facts


def test_report_budget_drops_evidence_before_context_records():
    evidence = [{"id": f"c-{i}", "quote": "证据" * 120} for i in range(6)]
    facts = [{"fact_key": "revenue", "name": "营业收入" * 40, "period": "2025"} for _ in range(8)]
    request = _report_request(evidence, facts)
    original = json.dumps(request, ensure_ascii=False)
    limit = 1300
    assert len(original) > limit

    parsed = json.loads(llm._fit_json_prompt(original, limit, drop_items_only=True))

    # evidence 先被丢光，再轮到 chapter_context 的记录类列表。
    assert parsed["evidence"] == []
    kept_facts = parsed["chapter_context"]["facts"]
    assert 0 <= len(kept_facts) < len(facts)
    assert kept_facts == facts[: len(kept_facts)]     # 仍是完整条目前缀


# ------------------------------------------------- 2. input_budget_exceeded --

def test_report_budget_exceeded_when_only_necessary_material_remains():
    # 系统表格是必须逐字保留的必要材料，且不是可丢弃列表。
    request = _report_request([], [], system_tables="系统表格" * 2000)
    original = json.dumps(request, ensure_ascii=False)

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._fit_json_prompt(original, 1000, drop_items_only=True)

    assert captured.value.error_type == "input_budget_exceeded"


def test_report_budget_exceeded_after_dropping_all_droppable_items():
    # 有可丢证据，但丢光后必要材料仍超限 → 仍报 input_budget_exceeded。
    request = _report_request([{"id": "c-1", "quote": "证据" * 50}],
                              system_tables="系统表格" * 2000)
    original = json.dumps(request, ensure_ascii=False)

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._fit_json_prompt(original, 1000, drop_items_only=True)

    assert captured.value.error_type == "input_budget_exceeded"


# ------------------------------------------------- 3. 非 report 旧行为不变 --

def test_non_report_budget_keeps_legacy_truncation_behavior():
    payload = json.dumps({"evidence": [{"quote": "长" * 5000}]}, ensure_ascii=False)

    # 默认（非报告任务）仍是“对最长 text/quote 对半截断”，不是整条丢弃。
    truncated = json.loads(llm._fit_json_prompt(payload, 2000))
    assert truncated["evidence"][0]["quote"]                       # 非空
    assert len(truncated["evidence"][0]["quote"]) < 5000           # 被截断

    # 同一 payload 走报告口径则整条丢弃，不截断字符串。
    dropped = json.loads(llm._fit_json_prompt(payload, 2000, drop_items_only=True))
    assert dropped["evidence"] == []


# ------------------------------------------------- 4. report 关闭 JSON 修复 --

def test_report_first_generation_does_not_trigger_low_level_json_repair(monkeypatch):
    calls = []
    responses = [_FakeResponse("not-json"), _FakeResponse('{"content":"已修复","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient(responses, calls))

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection,
                       context=llm.LlmCallContext(task_type="report"))

    # 不再发起底层修复请求：只调用 1 次，且错误类型可区分。
    assert len(calls) == 1
    assert captured.value.error_type == "report_repair_required"
    assert "invalid_json" in str(captured.value)


def test_report_schema_failure_also_skips_low_level_repair(monkeypatch):
    calls = []
    responses = [_FakeResponse('{"content":1,"citation_ids":[]}'),
                 _FakeResponse('{"content":"已修复","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient(responses, calls))

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection,
                       context=llm.LlmCallContext(task_type="report"))

    assert len(calls) == 1
    assert captured.value.error_type == "report_repair_required"
    assert "schema_error" in str(captured.value)


def test_report_failure_records_attempt_with_repair_required_type(monkeypatch):
    recorded = []
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm, "_run_budget", lambda context: None)
    monkeypatch.setattr(llm, "_persist_attempt",
                        lambda context, attempt, status, request_hash, schema_name, usage,
                        error_type="", error_message="": recorded.append(
                            (attempt, status, error_type, error_message)))
    monkeypatch.setattr(llm.httpx, "Client",
                        lambda **_: _FakeClient([_FakeResponse("not-json")], []))

    with pytest.raises(llm.StructuredLlmError):
        llm._chat_json("system", "prompt", schema=LlmSection,
                       context=llm.LlmCallContext(project_id="p", run_id="r",
                                                  node_key="draft", task_type="report"))

    # 每次尝试分别记账，且归到可区分的失败类型。
    assert recorded == [(1, "failed", "report_repair_required", recorded[0][3])]
    assert recorded[0][3]                                  # 诊断文案非空
    assert "not-json" not in recorded[0][3]                # 不回灌原始响应正文


def test_non_report_invalid_json_still_uses_the_repair_call(monkeypatch):
    calls = []
    responses = [_FakeResponse("not-json"), _FakeResponse('{"content":"已修复","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient(responses, calls))

    body, _ = llm._chat_json("system", "prompt", schema=LlmSection)

    assert body["content"] == "已修复"
    assert len(calls) == 2                                  # 非报告任务仍修复一次


# ------------------------------------ 5. report_repair_required 携带原文 --

def test_report_repair_required_carries_raw_content_for_invalid_json(monkeypatch):
    """报告首次输出 JSON 不可解析时，异常携带原始模型输出供上层做一次修复。

    ``raw_content`` 只挂在属性上：不进入 ``message``，也不落 ``llm_attempts`` 正文。
    """
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient([_FakeResponse("not-json")], []))

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection,
                       context=llm.LlmCallContext(task_type="report"))

    assert captured.value.error_type == "report_repair_required"
    assert captured.value.raw_content == "not-json"
    assert "not-json" not in str(captured.value)             # 原文不进 message


def test_report_repair_required_carries_raw_content_for_schema_error(monkeypatch):
    """Schema 校验失败同样携带原始输出——两条路径都要能拿到修复输入。"""
    raw = '{"content":1,"citation_ids":[]}'
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient([_FakeResponse(raw)], []))

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection,
                       context=llm.LlmCallContext(task_type="report"))

    assert captured.value.error_type == "report_repair_required"
    assert captured.value.raw_content == raw
    assert raw not in str(captured.value)                    # 原文不进 message


def test_non_report_error_does_not_carry_raw_content(monkeypatch):
    """非报告任务保持旧行为：异常不携带原始输出。"""
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient([_FakeResponse("not-json")], []))

    with pytest.raises(llm.StructuredLlmError) as captured:
        llm._chat_json("system", "prompt", schema=LlmSection)

    assert captured.value.raw_content is None


# ------------------------------------------- 6. drop_report 整条丢弃收集 --

def test_report_budget_collector_records_dropped_evidence_source_ids():
    """整条丢弃时按丢弃顺序收集被丢 evidence 的 source_id，并置 budget_exceeded。"""
    evidence = [{"id": f"c-{i}", "source_id": f"src-{i}", "quote": "证据" * 120 + f"-{i}"}
                for i in range(10)]
    request = _report_request(evidence)
    original = json.dumps(request, ensure_ascii=False)
    limit = 1500
    assert len(original) > limit

    collector: dict = {}
    fitted = llm._fit_json_prompt(original, limit, drop_items_only=True, drop_report=collector)

    kept = json.loads(fitted)["evidence"]
    assert 0 < len(kept) < len(evidence)                     # 确实发生了整条丢弃
    assert collector["budget_exceeded"] is True
    # 从尾部开始丢，收集器按丢弃先后记录 source_id
    assert collector["dropped_evidence_ids"] == [
        f"src-{i}" for i in range(len(evidence) - 1, len(kept) - 1, -1)
    ]


def test_report_budget_collector_maps_alias_back_to_source_id():
    """模型侧只发短别名时，靠证据映射表把被丢条目还原成 source_id。"""
    evidence = [{"id": f"E{i}", "quote": "证据" * 120} for i in range(6)]
    request = _report_request([])
    request["evidence"] = evidence
    original = json.dumps(request, ensure_ascii=False)
    limit = 1200
    assert len(original) > limit

    collector: dict = {}
    fitted = llm._fit_json_prompt(
        original, limit, drop_items_only=True, drop_report=collector,
        evidence_sources={f"E{i}": f"src-{i}" for i in range(6)},
    )

    kept = json.loads(fitted)["evidence"]
    assert 0 < len(kept) < len(evidence)
    assert collector["budget_exceeded"] is True
    assert collector["dropped_evidence_ids"] == [
        f"src-{i}" for i in range(len(evidence) - 1, len(kept) - 1, -1)
    ]


def test_report_budget_collector_untouched_when_within_budget():
    """未超预算：收集器保持传入原样（不写入任何键）。"""
    request = _report_request([{"id": "c-1", "source_id": "src-1", "quote": "短证据"}])
    original = json.dumps(request, ensure_ascii=False)

    collector: dict = {}
    assert llm._fit_json_prompt(original, len(original) + 100, drop_items_only=True,
                                drop_report=collector) == original
    assert collector == {}


def test_non_report_budget_does_not_collect_dropped_evidence():
    """非报告任务走旧截断口径，不收集被丢 evidence。"""
    payload = json.dumps({"evidence": [{"id": "c-0", "source_id": "src-0", "quote": "长" * 5000}]},
                         ensure_ascii=False)
    collector: dict = {}

    llm._fit_json_prompt(payload, 2000, drop_report=collector)     # 默认 drop_items_only=False

    assert collector == {}


def test_rewrite_section_forwards_the_drop_report_collector(monkeypatch):
    """``rewrite_section(drop_report=…)`` 端到端回填收集器，且报告链路只调用一次模型。"""
    calls: list = []
    monkeypatch.setattr(llm, "settings", replace(
        _settings(), llm_report_max_chars=1200, llm_model="deepseek-x",
    ))
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_args, **_kwargs: "system")
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient(
        [_FakeResponse('{"content":"正文","citation_ids":[]}')], calls))

    evidence = [{"id": f"c-{i}", "source_id": f"src-{i}", "quote": "证据" * 200, "filename": f"f{i}"}
                for i in range(8)]
    chapter_context = {"project_id": "p1", "evidence": list(evidence), "facts": []}
    collector: dict = {}

    result, _usage = llm.rewrite_section(
        "财务", "草稿", evidence, llm.LlmCallContext(task_type="report"),
        chapter_key="financial", chapter_context=chapter_context,
        strict_citations=True, drop_report=collector,
    )

    assert len(calls) == 1                                   # JSON 修复链关闭，只 1 次调用
    assert collector.get("budget_exceeded") is True
    assert collector.get("dropped_evidence_ids")
    assert all(item.startswith("src-") for item in collector["dropped_evidence_ids"])
    assert result.content == "正文"


def test_report_task_still_retries_transport_failures(monkeypatch):
    calls = []
    responses = [llm.httpx.ReadTimeout("timeout"),
                 _FakeResponse('{"content":"ok","citation_ids":[]}')]
    monkeypatch.setattr(llm, "settings", _settings())
    monkeypatch.setattr(llm.httpx, "Client", lambda **_: _FakeClient(responses, calls))
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)

    body, _ = llm._chat_json("system", "prompt", schema=LlmSection,
                             context=llm.LlmCallContext(task_type="report"))

    assert body["content"] == "ok"
    assert len(calls) == 2                                  # 传输层仍重试同一请求
