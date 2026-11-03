"""服务商 json_object 门禁：请求里必须出现小写字面量 ``json``。

2026-10-26 实测：全部章节提示词只有大写 "JSON"（小写 0 次），DeepSeek 兼容接口回
``Prompt must contain the word 'json' in some form to use 'response_format' of
type 'json_object'``——检查大小写敏感，导致单章重生（以及全量生成）一律 HTTP 400。
这是与表格开关无关的**全局**门禁，修在 ``_chat_json`` 的统一入口。
"""
from __future__ import annotations

from backend.app.llm import JSON_KEYWORD_HINT, _ensure_json_keyword


def test_adds_literal_lowercase_json_when_missing() -> None:
    prompt = _ensure_json_keyword('输出严格 JSON：{"content":"正文"}。', "章节材料")
    assert "json" in prompt
    assert JSON_KEYWORD_HINT in prompt


def test_uppercase_json_alone_is_not_enough() -> None:
    """只有大写 "JSON" 仍要补——服务商按字面量小写检查（实测 400）。"""
    assert JSON_KEYWORD_HINT in _ensure_json_keyword("输出严格 JSON。", "正文")


def test_idempotent_when_keyword_already_present() -> None:
    system = "以 json 返回。"
    assert _ensure_json_keyword(system, "正文") == system
    # 用户提示里已有也算（服务商检查整段请求）
    assert _ensure_json_keyword("系统提示。", "返回 json 对象") == "系统提示。"
    # 二次调用不得叠加
    once = _ensure_json_keyword("系统提示。", "正文")
    assert _ensure_json_keyword(once, "正文") == once
