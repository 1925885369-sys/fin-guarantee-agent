"""章节 JSON 解析容错：字符串内裸换行（控制字符）不得判结构失败。

2026-10-27 实测：``table_mode: allowed`` 放开后，"主营业务及经营稳定性"单章重生
输出从 947 tokens 涨到 5151 tokens，模型把带表格的 Markdown 直接塞进 ``content``
字符串，换行**没有**转义成 ``\\n``。标准 ``json.loads`` 抛
``Invalid control character at``，整章被记为"首次输出结构不可解析"
（``report_repair_required``），唯一一次修复又犯同样的错 → ``status=failed``，
正文退回确定性草稿（因此"点了重生仍然没有表"）。
"""
from __future__ import annotations

import json

import pytest

from backend.app.llm import _parse_json_content, _repair_message

TABLE_BODY = (
    "### 主营业务概述\n\n"
    "发行人为北京市国资委出资成立的国有独资公司（E02）。\n\n"
    "| 年度 | 票款及智慧轨道交通服务收入（亿元） | 占主营业务收入比重 |\n"
    "| --- | --- | --- |\n"
    "| 2025 | 81.34 | 43.36% |\n"
    "| 2024 | 79.10 | 41.02% |\n\n"
    "### 结论\n"
)


def _raw_with_literal_newlines(body: str) -> str:
    """模拟模型输出：JSON 字符串里直接写字面换行（未转义）。"""
    return '{"content":"' + body + '","citation_ids":["E01"]}'


def test_literal_newlines_inside_markdown_table_are_accepted() -> None:
    raw = _raw_with_literal_newlines(TABLE_BODY)
    # 先自证这段输入确实是"非法严格 JSON、合法宽松 JSON"，
    # 否则用例会在解析器改坏时报假绿。
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    parsed = _parse_json_content(raw)
    assert parsed["content"].count("|") == TABLE_BODY.count("|")
    assert "2025" in parsed["content"]
    assert parsed["citation_ids"] == ["E01"]


def test_literal_tabs_and_carriage_returns_are_accepted() -> None:
    body = "第一列\t第二列\r\n第三行"
    parsed = _parse_json_content(_raw_with_literal_newlines(body))
    assert parsed["content"] == body


def test_fenced_block_with_literal_newlines_is_accepted() -> None:
    raw = "```json\n" + _raw_with_literal_newlines(TABLE_BODY) + "\n```"
    parsed = _parse_json_content(raw)
    assert "| 年度 |" in parsed["content"]


def test_escaped_newlines_still_parse() -> None:
    raw = json.dumps({"content": TABLE_BODY, "citation_ids": ["E01"]}, ensure_ascii=False)
    assert _parse_json_content(raw)["content"] == TABLE_BODY


def test_non_json_markdown_still_rejected() -> None:
    """放宽的只是控制字符——整段不是 JSON 时仍按结构失败处理，不得静默放行。"""
    with pytest.raises(json.JSONDecodeError):
        _parse_json_content(TABLE_BODY)


def test_repair_message_requires_escaped_newlines() -> None:
    message = _repair_message('{"content":"a\nb"}', "invalid_json", None)
    assert r"\n" in message
    assert "escaped" in message
