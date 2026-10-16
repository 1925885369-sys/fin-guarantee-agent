"""P1-4 自定义章节：配置合成、目录跟随生成、Prompt 驱动检索、展示分组。

覆盖 PLAN_new_p0_p1.md P1-4 / PLAN.md P1-D：
- 章节集合跟随生效目录（内置改名/删除/排序；自定义章节空模板 + 模型生成）；
- 自定义章节配置路径（chapter_uses_llm / load_chapter_config 合成 / 覆盖链可用）；
- Prompt 驱动检索（章级/小节 prompt 进 evidence_query；material_roles/document_ids 筛选）；
- 展示侧 grouped_sections/section_labels 支持 outline（改名立即生效、被删章节附后）。
"""
from __future__ import annotations

import uuid

import pytest

from backend.app import chapter_guard, workflow
from backend.app.chapter_prompts import (
    editable_config_from_factory, is_custom_chapter_key, load_chapter_config, chapter_uses_llm,
)
from backend.app.main import startup
from backend.app.report_outline import grouped_sections, section_labels
from backend.app.report_outline_store import builtin_outline, normalize_outline


CUSTOM_ID = f"chapter-{uuid.uuid4()}"


@pytest.fixture(autouse=True)
def _ensure_db():
    startup()


# ---------------- 配置合成 ----------------

def test_custom_chapter_key_recognition():
    assert is_custom_chapter_key(CUSTOM_ID)
    assert not is_custom_chapter_key("governance")
    assert not is_custom_chapter_key("chapter-not-uuid")
    assert not is_custom_chapter_key("")


def test_custom_chapter_uses_llm_and_synthesized_config():
    assert chapter_uses_llm(CUSTOM_ID) is True
    config = load_chapter_config(CUSTOM_ID)
    assert config["llm"] is True
    assert config["generation_mode"] == "generate"
    assert config["role"]
    # 无事实白名单：检索与生成任务完全由目录标题/小节/Prompt 组成。
    assert config["input_contract"]["fact_keys"] == []
    assert config["input_contract"]["source_kinds"] == []
    assert config["output_contract"]["required_missing_markers"]


def test_unknown_non_uuid_key_stays_readonly():
    assert chapter_uses_llm("totally-unknown") is False
    config = load_chapter_config("totally-unknown")
    assert config["llm"] is False and "禁止模型改写" in config["goal"]


def test_custom_chapter_editable_config_fields():
    editable = editable_config_from_factory(CUSTOM_ID)
    for field in ("role", "goal", "required_sections", "rules", "narrative_contract"):
        assert field in editable, field
    assert editable["narrative_contract"]["preferred_min_chars"] > 0


# ---------------- 目录跟随生成 ----------------

def _outline_with_custom(title: str = "经营持续性专项分析") -> dict:
    outline = builtin_outline()
    part = outline["parts"][2]  # 第二部分 财务、偿债与评级
    part["chapters"] = [c for c in part["chapters"] if c["template_key"] != "rating"]
    for c in part["chapters"]:
        if c["template_key"] == "financial":
            c["title"] = "【改名】财务趋势分析"
    part["chapters"].append({
        "chapter_id": CUSTOM_ID, "template_key": None, "title": title,
        "order": len(part["chapters"]) + 1, "subsections": [],
    })
    return outline


def test_templates_follow_outline(monkeypatch):
    from backend.app.report_outline_store import effective_outline

    monkeypatch.setattr(
        "backend.app.report_outline_store.effective_outline",
        lambda project_id: _outline_with_custom(),
    )
    templates = workflow._templates({"id": "demo-bond-huachen"}, [], [], [], {})
    keys = [item.key for item in templates]
    # rating 被删除；financial 标题按目录改名；自定义章节以 chapter_id 为键、空草稿。
    assert "rating" not in keys
    financial = next(item for item in templates if item.key == "financial")
    assert financial.title == "【改名】财务趋势分析"
    custom = next((item for item in templates if item.key == CUSTOM_ID), None)
    assert custom is not None and custom.content == "" and custom.source_ids == []
    # 目录顺序优先：financial 在自定义章节之前（rating 已删除）。
    assert keys.index("financial") < keys.index(CUSTOM_ID)


def test_templates_fallback_to_builtin_when_outline_empty(monkeypatch):
    monkeypatch.setattr("backend.app.report_outline_store.effective_outline", lambda project_id: {})
    templates = workflow._templates({"id": "demo-bond-huachen"}, [], [], [], {})
    assert len(templates) == 17


# ---------------- Prompt 驱动检索 ----------------

def test_build_context_retrieval_uses_outline_prompts(monkeypatch):
    captured = {}

    def fake_retrieve(project_id, query, top_k=0, **kwargs):
        captured["query"] = query
        captured.update(kwargs)
        return []

    monkeypatch.setattr("backend.app.retrieval.retrieve_chunks", fake_retrieve)
    monkeypatch.setattr(
        chapter_guard, "_outline_chapter_config",
        lambda project_id, chapter_key: {
            "chapter": {
                "chapter_id": CUSTOM_ID, "template_key": None,
                "title": "担保圈专项分析",
                "prompt": "关注互保与关联担保",
                "material_roles": ["prospectus", "rating_report"],
                "document_ids": ["doc-1"],
            },
            "subsections": [
                {"subsection_id": "sub-x-1", "title": "互保结构", "prompt": "列出互保对象"},
            ],
            "outline_version": "v1", "material_roles": ["prospectus", "rating_report"],
            "document_ids": ["doc-1"],
        },
    )
    ctx = chapter_guard.build_chapter_context("demo-bond-huachen", CUSTOM_ID, "", None)
    # 查询包含章节标题/章级要求/小节任务。
    for term in ("担保圈专项分析", "关注互保与关联担保", "互保结构", "列出互保对象"):
        assert term in captured["query"], f"检索查询缺少：{term}"
    assert captured.get("allowed_roles") == {"prospectus", "rating_report"}
    assert captured.get("document_ids") == {"doc-1"}
    assert ctx["outline_version"] == "v1"
    assert [s["subsection_id"] for s in ctx["subsections"]] == ["sub-x-1"]


def test_build_context_builtin_chapter_roles_default_none(monkeypatch):
    captured = {}

    def fake_retrieve(project_id, query, top_k=0, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("backend.app.retrieval.retrieve_chunks", fake_retrieve)
    chapter_guard.build_chapter_context("demo-bond-huachen", "governance", "草稿", None)
    assert captured.get("allowed_roles") is None
    assert captured.get("document_ids") is None


# ---------------- 展示分组 ----------------

def _sections_fixture():
    return [
        {"section_key": "financial", "title": "财务趋势与盈利质量"},
        {"section_key": "rating", "title": "评级报告复核及评级差异"},
        {"section_key": CUSTOM_ID, "title": "担保圈专项分析"},
        {"section_key": "stale-chapter", "title": "旧章节"},
    ]


def test_grouped_sections_follow_outline_titles_and_order():
    outline = _outline_with_custom()
    groups = grouped_sections(_sections_fixture(), outline=outline)
    assert any(g["part"] == "第二部分 财务、偿债与评级" for g in groups)
    financial_group = next(g for g in groups if g["part"] == "第二部分 财务、偿债与评级")
    assert financial_group["label"].startswith("一、")
    # 财务章标题用目录改名后的标题。
    financial_chapter = next(c for c in financial_group["chapters"] if c["section"]["section_key"] == "financial")
    assert "【改名】财务趋势分析" in financial_chapter["label"]
    # rating 被删除后其旧正文仍附后可见。
    assert any("评级报告复核及评级差异" in g["label"] and not g["chapters"] for g in groups)
    # 自定义章节按其目录分部位置出现在分组中。
    assert any(c["section"]["section_key"] == CUSTOM_ID for c in financial_group["chapters"])
    assert any("旧章节" in g["label"] for g in groups)


def test_section_labels_with_outline_cover_custom():
    outline = _outline_with_custom()
    labels = section_labels(_sections_fixture(), outline=outline)
    assert labels[CUSTOM_ID]["chapter_number"]
    assert labels[CUSTOM_ID]["part"]
    # 不在目录的 section 仍有兜底 label（章号为空）。
    assert "stale-chapter" in labels


def test_normalize_outline_keeps_retrieval_constraints():
    outline = _outline_with_custom()
    outline["parts"][2]["chapters"][-1]["material_roles"] = ["prospectus", "audit_report"]
    outline["parts"][2]["chapters"][-1]["document_ids"] = ["doc-a", "doc-b"]
    normalized = normalize_outline(outline)
    custom = next(
        c for part in normalized["parts"] for c in part["chapters"]
        if c["chapter_id"] == CUSTOM_ID
    )
    assert custom["material_roles"] == ["prospectus", "audit_report"]
    assert custom["document_ids"] == ["doc-a", "doc-b"]
    assert custom["generation_mode"] == "generate"
    assert custom["evidence_scope"] == "prompt_retrieval"


# ---------------- API：report-prompts 返回自定义章节 ----------------

def test_report_prompts_includes_custom_chapter_and_writes(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app.main import app
    from backend.app.report_outline_store import save_outline

    monkeypatch.setattr(
        "backend.app.report_outline_store.effective_outline",
        lambda project_id: _outline_with_custom(),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.get("/api/projects/demo-bond-huachen/report-prompts")
    assert response.status_code == 200
    payload = response.json()
    custom = next((item for item in payload if item["chapter_key"] == CUSTOM_ID), None)
    assert custom is not None, "report-prompts 未返回自定义章节"
    assert custom["llm"] is True
    assert custom["title"] == "经营持续性专项分析"
    assert "第二部分" in (custom.get("part") or "")
    # 未知非 uuid 键仍然 404（用旧 prompt 形式绕过结构校验，直达章节判据）。
    assert client.patch(
        "/api/projects/demo-bond-huachen/report-prompts/totally-unknown",
        json={"prompt": "任意文本"},
    ).status_code == 404


def test_reset_report_prompt_supports_custom_chapter(monkeypatch):
    """P1-2：项目级重置支持自定义章节——删除项目覆盖后按全局/出厂回落。"""
    from fastapi.testclient import TestClient

    from backend.app.chapter_prompts import chapter_prompt
    from backend.app.database import execute
    from backend.app.main import app

    monkeypatch.setattr(
        "backend.app.report_outline_store.effective_outline",
        lambda project_id: _outline_with_custom(),
    )
    client = TestClient(app, client=("127.0.0.1", 50001))
    try:
        saved = client.patch(
            f"/api/projects/demo-bond-huachen/report-prompts/{CUSTOM_ID}",
            json={"prompt": "自定义章节的项目级补充要求：关注订单与产能。"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["overridden"] is True
        reset = client.delete(f"/api/projects/demo-bond-huachen/report-prompts/{CUSTOM_ID}")
        assert reset.status_code == 200, reset.text
        body = reset.json()
        assert body["overridden"] is False
        # 自定义章节标题/分部取生效目录（与 GET /report-prompts 同口径）。
        assert body["title"] == "经营持续性专项分析"
        assert "第二部分" in (body.get("part") or "")
        # 回落链路生效：项目覆盖已删除，生效 Prompt 不再包含项目补充要求。
        assert "关注订单与产能" not in chapter_prompt(CUSTOM_ID, "demo-bond-huachen")
    finally:
        execute(
            "DELETE FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?",
            ("demo-bond-huachen", CUSTOM_ID),
        )


def test_outline_retrieval_constraint_validation():
    from backend.app.report_outline_store import validate_outline

    outline = _outline_with_custom()
    outline["parts"][2]["chapters"][-1]["material_roles"] = "not-a-list"
    with pytest.raises(ValueError):
        validate_outline(outline)
    outline = _outline_with_custom()
    outline["parts"][2]["chapters"][-1]["document_ids"] = [1, 2]
    with pytest.raises(ValueError):
        validate_outline(outline)
