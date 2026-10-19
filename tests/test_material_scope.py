"""B4 材料范围：单一解析器、fail-closed、全局不存项目文件 ID。

对应《风控约束分层》方案 §二 line 16 / §四 line 56~57。守三条：
1. 优先级 Prompt 业务层 > 目录章节 > 不额外收窄；
2. 显式 `document_ids` **全部无效时 fail-closed**，绝不退化成"全部材料"（否则用户
   明确排除的文件会被偷偷放回来）；
3. 全局配置**不保存项目文件 ID**（保存时剥离、读取时清洗历史脏行）。
"""
from __future__ import annotations

from backend.app import chapter_guard
from backend.app.chapter_prompts import global_prompt_edit_state
from backend.app.main import _strip_global_document_ids
from backend.app.schemas import PromptEditableConfig, ReportPromptUpdate


def _scope(config=None, outline=None, owned=("doc-1", "doc-2")):
    """用 monkeypatch 顶掉 `rows`，让解析器只依赖"本项目拥有哪些文件"。"""
    original = chapter_guard.rows

    def fake_rows(query: str, params=()):
        assert "analysis_documents" in query
        return [{"id": item} for item in params[1:] if item in owned]

    chapter_guard.rows = fake_rows
    try:
        return chapter_guard._resolve_material_scope(
            "p1", "financial", config or {}, outline or {})
    finally:
        chapter_guard.rows = original


def _config(document_ids=None, roles=None):
    return {"material_scope": {"document_ids": list(document_ids or []),
                               "material_roles": list(roles or []),
                               "source_kinds": []}}


# ------------------------------------------------------------------ 优先级 --

def test_empty_scope_does_not_narrow():
    scope = _scope()
    assert scope["document_ids"] == [] and scope["invalid"] is False
    assert scope["source"] == ""


def test_prompt_scope_wins_over_outline_scope():
    scope = _scope(_config(["doc-1"]), {"document_ids": ["doc-2"], "material_roles": ["审计报告"]})
    assert scope["document_ids"] == ["doc-1"]
    assert scope["source"] == "prompt"
    # 角色从 Prompt 拿不到时回落目录
    assert scope["material_roles"] == ["审计报告"]


def test_outline_scope_used_when_prompt_has_none():
    scope = _scope({}, {"document_ids": ["doc-2"], "material_roles": []})
    assert scope["document_ids"] == ["doc-2"]
    assert scope["source"] == "outline"


# -------------------------------------------------------------- fail-closed --

def test_unowned_document_ids_are_dropped():
    scope = _scope(_config(["doc-1", "doc-other"]))
    assert scope["document_ids"] == ["doc-1"]
    assert scope["invalid"] is False


def test_all_invalid_document_ids_fail_closed():
    """显式给了文件却全不属于本项目 → 空匹配集（不是"全部材料"）。"""
    scope = _scope(_config(["doc-a", "doc-b"]))
    assert scope["document_ids"] == []
    assert scope["invalid"] is True


# ------------------------------------------------------------- 全局文件 ID --

def test_global_save_strips_document_ids_but_keeps_kinds_and_roles():
    payload = ReportPromptUpdate(config=PromptEditableConfig(
        role="财务分析员", goal="分析财务结构",
        material_scope={"source_kinds": ["募集说明书"], "material_roles": ["审计报告"],
                        "document_ids": ["doc-1", "doc-2"]},
    ))
    stripped = _strip_global_document_ids(payload)
    assert stripped.config.material_scope.document_ids == []
    assert stripped.config.material_scope.source_kinds == ["募集说明书"]
    assert stripped.config.material_scope.material_roles == ["审计报告"]


def test_global_save_without_document_ids_is_untouched():
    payload = ReportPromptUpdate(config=PromptEditableConfig(role="财务分析员", goal="分析财务结构"))
    assert _strip_global_document_ids(payload).config.material_scope.document_ids == []


def test_global_prompt_edit_state_drops_document_ids():
    """读取侧清洗历史脏行：全局链上不应出现项目文件 ID。"""
    config, source, _mode = global_prompt_edit_state("financial")
    assert source in {"global", "system"}
    assert (config.get("material_scope") or {}).get("document_ids") in (None, [])


# --------------------------------------------------------------- 类型证据 --

from backend.app.chapter_guard import _type_evidence_advisories


def _evidence(*kinds):
    return [{"id": f"e{i}", "document_kind": kind} for i, kind in enumerate(kinds)]


def test_rating_view_without_rating_evidence_needs_review():
    items = _type_evidence_advisories("评级机构认为发行人偿债能力较强。", {"evidence": _evidence("募集说明书")})
    assert [item["code"] for item in items] == ["rating_evidence_missing"]


def test_rating_view_with_rating_evidence_passes():
    items = _type_evidence_advisories("评级机构认为发行人偿债能力较强。", {"evidence": _evidence("信用评级报告")})
    assert items == []


def test_contract_effective_without_contract_evidence_needs_review():
    items = _type_evidence_advisories("保证合同已生效，担保责任已承担。", {"evidence": _evidence("募集说明书")})
    assert [item["code"] for item in items] == ["contract_evidence_missing"]


def test_contract_effective_with_contract_evidence_passes():
    items = _type_evidence_advisories("保证合同已生效。", {"evidence": _evidence("担保合同")})
    assert items == []


def test_plain_text_has_no_type_evidence_findings():
    assert _type_evidence_advisories("发行人营业收入为1,000万元。", {"evidence": _evidence("募集说明书")}) == []


# --------------------------------------------------- 目录保存的范围剥离 --

from backend.app.report_outline_store import normalize_outline


def _outline_payload():
    return {"parts": [{
        "part_id": "part-1", "title": "第一部分 发行人与本次债券", "order": 1,
        "chapters": [{
            "chapter_id": "chapter-governance", "template_key": "governance",
            "title": "发行人主体、股权和治理", "order": 1,
            "document_ids": ["doc-1", "doc-2"], "material_roles": ["审计报告"],
            "subsections": [],
        }],
    }]}


def test_global_outline_drops_document_ids_but_keeps_roles():
    normalized = normalize_outline(_outline_payload(), scope="global")
    chapter = normalized["parts"][0]["chapters"][0]
    assert chapter["document_ids"] == []
    assert chapter["material_roles"] == ["审计报告"]


def test_project_outline_keeps_document_ids():
    normalized = normalize_outline(_outline_payload(), scope="project")
    assert normalized["parts"][0]["chapters"][0]["document_ids"] == ["doc-1", "doc-2"]
