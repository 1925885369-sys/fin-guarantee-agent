"""交付评估的授权口径：章节证据 ID（`c-{analysis_source_id}`）必须能解析。

2026-10-27 实测（北投 operations 单章重生）：模型稿 3853 字、小节齐全、表格正常，
却因 `c-9185c07a-cce1-4346-a8d6-8374e5a4f90d` 被判"存在未授权引用"整章退回模板。

原因是**两套 ID 空间**：
- 章节证据 `chapter_guard._evidence_item()` 用 `c-{analysis_source_id}` 动态派生，
  模型只能看到这 12 条；
- 交付评估 `_support_context()` 查的是 `citations` 表（另一套引用池，104 条）。

两者不保证同步，于是"模型引用了本章发给它的材料"会被判未授权；而 safety 修复
只能改正文、改不了引用池，首稿与修复稿都必挂。

授权严格性由**章节级** `allowed_citation_ids` 承担，支持级只负责"能否解析到本项目来源"。
"""
from __future__ import annotations

import uuid

from backend.app.database import connect, now_iso
from backend.app.main import startup
from backend.app.p0 import _support_context, assess_section_claims


def _fixture() -> tuple[str, list[str]]:
    """最小项目：一份文档 + 两条来源（**不写 citations 表**，复现不同步）。"""
    project_id = f"support-cite-{uuid.uuid4()}"
    now = now_iso()
    source_ids = ["src-alpha", "src-beta"]
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,"
            "created_at,updated_at,consent_at,project_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", "授权口径测试", 500, 12, "补充流动资金", "review",
             now, now, now, "bond_guarantee"),
        )
        conn.execute(
            "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,"
            "quality,locator_count,warning,created_at,parser,ocr_status,page_count,document_role) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"doc-{project_id}", project_id, "募集说明书.pdf", "募集说明书", "application/pdf", "x", 1,
             "parsed", "ok", 1, "", now, "native", "skipped", 1, "prospectus"),
        )
        for index, source_id in enumerate(source_ids, start=1):
            conn.execute(
                "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,"
                "extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source_id, project_id, f"doc-{project_id}", "page", f"第{index}页",
                 f"第{index}条材料正文", index, "{}", "native", f"h{index}"),
            )
    return project_id, source_ids


def _cleanup(project_id: str) -> None:
    with connect() as conn:
        for table in ("report_claims", "audit_events", "report_sections", "citations", "sources", "documents"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_chapter_evidence_ids_are_resolvable():
    startup()
    project_id, source_ids = _fixture()
    try:
        citation_map, source_texts, valid_sources = _support_context(project_id)
        for source_id in source_ids:
            synthetic = f"c-{source_id}"
            assert synthetic in citation_map, "章节证据 ID 在交付评估里解析不到"
            assert citation_map[synthetic]["source_id"] == source_id
            assert source_id in valid_sources
    finally:
        _cleanup(project_id)


def test_citing_chapter_evidence_is_not_unauthorized():
    """模型引用本章证据 → 授权通过（章节级已守门，支持级不再重复设卡）。"""
    startup()
    project_id, source_ids = _fixture()
    try:
        section = {
            "section_key": "operations",
            "content": "发行人主营业务为城市轨道交通投融资与运营。",
            "citation_ids": [f"c-{source_ids[0]}"],
            "allowed_source_ids": list(source_ids),
        }
        assessment = assess_section_claims(project_id, section, *_support_context(project_id))
        assert assessment["invalid_citations"] == 0
    finally:
        _cleanup(project_id)


def test_citation_outside_project_sources_is_still_rejected():
    """放宽的是"本项目来源"，不是"任意 ID"：编造引用仍要判未授权。"""
    startup()
    project_id, source_ids = _fixture()
    try:
        section = {
            "section_key": "operations",
            "content": "发行人主营业务为城市轨道交通投融资与运营。",
            "citation_ids": ["c-does-not-exist"],
            "allowed_source_ids": list(source_ids),
        }
        assessment = assess_section_claims(project_id, section, *_support_context(project_id))
        assert assessment["invalid_citations"] == 1
    finally:
        _cleanup(project_id)
