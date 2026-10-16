"""P1-2 三级小节：请求指令、响应组装、结构修复与上下文注入（正例 + 反例）。

覆盖 PLAN_new_p0_p1.md P1-2：
- 有小节配置时输出契约切换为 subsections（固定三级结构替代 required_sections 组织句）；
- 缺失、重复、未知 subsection_id → ``report_repair_required``（同一次结构修复）；
- 服务端按目录顺序渲染小节标题、组装整章正文；
- 没有小节配置时行为与旧版完全一致（普通 content 响应）。
"""
from __future__ import annotations

import pytest

from backend.app import chapter_guard, llm
from backend.app.chapter_guard import build_chapter_context
from backend.app.main import startup
from backend.app.professional import sync_fact_assertions


@pytest.fixture(autouse=True)
def _ensure_db():
    """批内首个触库用例之前初始化隔离库（否则 no such table）。"""
    startup()


SUBSECTIONS = [
    {"subsection_id": "sub-governance-1", "title": "主体及控制关系", "prompt": ""},
    {"subsection_id": "sub-governance-2", "title": "治理核验边界", "prompt": "关注授权矩阵与审批流程"},
]


class _Entry:
    """模拟模型返回的 subsections 条目（与 LlmSubsection 同形状）。"""

    def __init__(self, subsection_id: str, content: str, citation_ids: list[str] | None = None):
        self.subsection_id = subsection_id
        self.content = content
        self.citation_ids = citation_ids or []


# ---------------- apply_subsection_contract ----------------

def test_subsection_contract_replaces_output_convention_and_org_instruction():
    prompt = (
        "你是发行主体审查员。本章目标：梳理发行人主体信息。 "
        "必须保留以下内容区块：主体及控制关系、治理核验边界。 "
        "输出严格 JSON：{\"content\":\"Markdown正文\",\"citation_ids\":[\"允许的引用ID\"]}。"
    )
    compiled = llm.apply_subsection_contract(prompt, SUBSECTIONS)
    assert "必须保留以下内容区块" not in compiled
    assert "\"content\":\"Markdown正文\"" not in compiled
    assert "\"subsections\":[" in compiled
    assert "sub-governance-1" in compiled and "sub-governance-2" in compiled
    assert "关注授权矩阵与审批流程" in compiled


def test_subsection_contract_is_idempotent():
    prompt = (
        "你是发行主体审查员。 "
        "输出严格 JSON：{\"content\":\"Markdown正文\",\"citation_ids\":[\"允许的引用ID\"]}。"
    )
    once = llm.apply_subsection_contract(prompt, SUBSECTIONS)
    twice = llm.apply_subsection_contract(once, SUBSECTIONS)
    assert once == twice


def test_subsection_contract_appends_when_custom_prompt_lacks_convention():
    compiled = llm.apply_subsection_contract("自定义要求：写得简洁。", SUBSECTIONS)
    assert "自定义要求：写得简洁。" in compiled
    assert "subsections" in compiled
    assert "sub-governance-1" in compiled


def test_context_subsections_filters_disabled_and_untitled():
    items = [
        {"subsection_id": "sub-1", "title": "保留", "prompt": ""},
        {"subsection_id": "sub-2", "title": "禁用", "prompt": "", "enabled": False},
        {"subsection_id": "sub-3", "title": "  ", "prompt": ""},
        {"subsection_id": "", "title": "无ID", "prompt": ""},
    ]
    assert [item["subsection_id"] for item in llm._context_subsections({"subsections": items})] == ["sub-1"]
    assert llm._context_subsections(None) == []


# ---------------- assemble_subsections ----------------

def test_assemble_subsections_renders_titles_and_merges_citations():
    entries = [
        _Entry("sub-governance-1", "正文一。", ["E01"]),
        _Entry("sub-governance-2", "正文二。"),
    ]
    content, citations = llm.assemble_subsections(entries, SUBSECTIONS, {"E01": "c-1"}, {"c-1"}, False)
    assert content == "### 主体及控制关系\n\n正文一。\n\n### 治理核验边界\n\n正文二。"
    assert citations == ["c-1"]


def test_assemble_subsections_missing_duplicate_unknown_return_none():
    assert llm.assemble_subsections(
        [_Entry("sub-governance-1", "一")], SUBSECTIONS, {}, set(), False
    ) is None
    assert llm.assemble_subsections(
        [_Entry("sub-governance-1", "一"), _Entry("sub-governance-1", "二")],
        SUBSECTIONS, {}, set(), False,
    ) is None
    assert llm.assemble_subsections(
        [_Entry("sub-governance-1", "一"), _Entry("sub-not-expected", "二")],
        SUBSECTIONS, {}, set(), False,
    ) is None


def test_assemble_subsections_lenient_round_skips_missing():
    assembled = llm.assemble_subsections(
        [_Entry("sub-governance-1", "只有第一节")], SUBSECTIONS, {}, set(), False,
        require_complete=False,
    )
    assert assembled == ("### 主体及控制关系\n\n只有第一节", [])


def test_assemble_subsections_filters_unknown_citations_when_not_strict():
    entries = [
        _Entry("sub-governance-1", "一", ["E01", "E99"]),
        _Entry("sub-governance-2", "二"),
    ]
    _, citations = llm.assemble_subsections(entries, SUBSECTIONS, {"E01": "c-1"}, {"c-1"}, False)
    assert citations == ["c-1"]


def test_assemble_subsections_empty_body_is_incomplete_in_strict_round():
    """空正文不算完成（P1-2）：首稿/修复稿同为严格口径 → None；

    只有历史兼容的宽松轮（``require_complete=False``）才保留光标题占位。
    """
    entries = [_Entry("sub-governance-1", ""), _Entry("sub-governance-2", "二")]
    assert llm.assemble_subsections(entries, SUBSECTIONS, {}, set(), False) is None
    lenient = llm.assemble_subsections(
        entries, SUBSECTIONS, {}, set(), False, require_complete=False,
    )
    assert lenient is not None and lenient[0].startswith("### 主体及控制关系\n\n### 治理核验边界")


# ---------------- _finalize_report_section ----------------

def test_finalize_requires_complete_subsections():
    body = {
        "subsections": [{"subsection_id": "sub-governance-1", "content": "一", "citation_ids": []}],
        "citation_ids": [],
    }
    with pytest.raises(llm.StructuredLlmError) as excinfo:
        llm._finalize_report_section(body, {}, set(), False, SUBSECTIONS, require_complete=True)
    assert excinfo.value.error_type == "report_repair_required"
    assert excinfo.value.raw_content


def test_finalize_rejects_empty_content_without_subsections():
    with pytest.raises(llm.StructuredLlmError) as excinfo:
        llm._finalize_report_section(
            {"content": "", "citation_ids": []}, {}, set(), False, [], require_complete=True,
        )
    assert excinfo.value.error_type == "report_repair_required"


def test_finalize_keeps_plain_content_when_no_subsections():
    result = llm._finalize_report_section(
        {"content": "整章正文", "citation_ids": ["E01"]}, {"E01": "c-1"}, {"c-1"}, False, [],
        require_complete=True,
    )
    assert result.content == "整章正文"
    assert result.citation_ids == ["c-1"]


def test_finalize_lenient_round_keeps_content_without_subsections():
    """宽松轮：模型没回 subsections 时保留整章 content，不得清空。"""
    result = llm._finalize_report_section(
        {"content": "修复后的整章正文", "citation_ids": []}, {}, set(), False, SUBSECTIONS,
        require_complete=False,
    )
    assert result.content == "修复后的整章正文"


def test_finalize_repair_round_requires_complete_subsections():
    """修复稿同样校验小节 ID（P1-2）：修复后仍缺/空 → report_repair_incomplete。"""
    incomplete = {
        "subsections": [{"subsection_id": "sub-governance-1", "content": "只有一节", "citation_ids": []}],
        "citation_ids": [],
    }
    with pytest.raises(llm.StructuredLlmError) as excinfo:
        llm._finalize_report_section(
            incomplete, {}, set(), False, SUBSECTIONS, require_complete=True, repair_round=True,
        )
    assert excinfo.value.error_type == "report_repair_incomplete"
    assert "修复后" in str(excinfo.value)
    assert excinfo.value.raw_content

    empty_body = {
        "subsections": [
            {"subsection_id": "sub-governance-1", "content": "  ", "citation_ids": []},
            {"subsection_id": "sub-governance-2", "content": "二", "citation_ids": []},
        ],
        "citation_ids": [],
    }
    with pytest.raises(llm.StructuredLlmError) as excinfo:
        llm._finalize_report_section(
            empty_body, {}, set(), False, SUBSECTIONS, require_complete=True, repair_round=True,
        )
    assert excinfo.value.error_type == "report_repair_incomplete"
    assert "空正文" in str(excinfo.value)


def test_repair_section_json_raises_incomplete_when_still_partial(monkeypatch):
    """JSON 结构修复轮：模型仍返回不完整小节 → report_repair_incomplete（明确失败）。"""
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({
        "subsections": [{"subsection_id": "sub-governance-1", "content": "仍只有一节", "citation_ids": []}],
        "citation_ids": [],
    }, llm.LlmUsage()))
    with pytest.raises(llm.StructuredLlmError) as excinfo:
        llm.repair_section_json(
            "治理", "草稿正文", [{"id": "c-1", "source_id": "s-1", "quote": "引用"}],
            chapter_key=None, chapter_context=_chapter_context_with_subsections(),
            raw_content="{}", error_type="report_repair_required",
        )
    assert excinfo.value.error_type == "report_repair_incomplete"


# ---------------- rewrite_section 集成（mock 模型层） ----------------

def _chapter_context_with_subsections() -> dict:
    return {"prompt_config": {}, "subsections": [dict(item) for item in SUBSECTIONS]}


def test_rewrite_section_assembles_subsections(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({
        "subsections": [
            {"subsection_id": "sub-governance-1", "content": "正文一", "citation_ids": ["E01"]},
            {"subsection_id": "sub-governance-2", "content": "正文二", "citation_ids": []},
        ],
        "citation_ids": [],
    }, llm.LlmUsage()))
    result, _ = llm.rewrite_section(
        "治理", "草稿正文", [{"id": "c-1", "source_id": "s-1", "quote": "引用"}],
        chapter_key=None, chapter_context=_chapter_context_with_subsections(),
    )
    assert result.content == "### 主体及控制关系\n\n正文一\n\n### 治理核验边界\n\n正文二"
    assert result.citation_ids == ["c-1"]


def test_rewrite_section_incomplete_subsections_raise_repair_required(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: ({
        "subsections": [{"subsection_id": "sub-governance-1", "content": "仅一节", "citation_ids": []}],
        "citation_ids": [],
    }, llm.LlmUsage()))
    with pytest.raises(llm.StructuredLlmError) as excinfo:
        llm.rewrite_section(
            "治理", "草稿正文", [{"id": "c-1", "source_id": "s-1", "quote": "引用"}],
            chapter_key=None, chapter_context=_chapter_context_with_subsections(),
        )
    assert excinfo.value.error_type == "report_repair_required"
    # 被拒响应原文（raw_content）随异常携带——修复请求据此回灌。
    assert excinfo.value.raw_content and "subsections" in excinfo.value.raw_content


def test_rewrite_section_without_subsections_keeps_plain_content(monkeypatch):
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "_chat_json", lambda *_: (
        {"content": "整章正文", "citation_ids": ["E01"]}, llm.LlmUsage(),
    ))
    result, _ = llm.rewrite_section(
        "治理", "草稿正文", [{"id": "c-1", "source_id": "s-1", "quote": "引用"}],
        chapter_key=None, chapter_context={"prompt_config": {}, "subsections": []},
    )
    assert result.content == "整章正文"
    assert result.citation_ids == ["c-1"]


# ---------------- build_chapter_context 注入 ----------------

def test_build_chapter_context_injects_outline_and_changes_input_hash(monkeypatch):
    startup()
    sync_fact_assertions("demo-bond-huachen")
    monkeypatch.setattr(
        chapter_guard, "_outline_chapter_config",
        lambda project_id, chapter_key: {
            "chapter": {"title": "治理与主体"},
            "subsections": [dict(item) for item in SUBSECTIONS],
            "outline_version": "outline-v-test",
            "material_roles": [], "document_ids": [],
        },
    )
    with_outline = build_chapter_context("demo-bond-huachen", "governance", "草稿正文")
    assert with_outline["outline_version"] == "outline-v-test"
    assert [item["subsection_id"] for item in with_outline["subsections"]] == [
        "sub-governance-1", "sub-governance-2",
    ]
    assert with_outline["outline_sha256"]

    empty = {"chapter": None, "subsections": [], "outline_version": "", "material_roles": [], "document_ids": []}
    monkeypatch.setattr(chapter_guard, "_outline_chapter_config", lambda project_id, chapter_key: dict(empty))
    without_outline = build_chapter_context("demo-bond-huachen", "governance", "草稿正文")
    assert without_outline["subsections"] == []
    assert without_outline["outline_version"] == "" and without_outline["outline_sha256"] == ""
    assert without_outline["input_hash"] != with_outline["input_hash"]
