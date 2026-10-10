"""报告交付一致性专项回归（对应 2026-09-19 基线缺陷）。

缺陷：`workflow._fallback_unsupported_sections()` 把模型正文覆盖成确定性模板时只改
content/citation_ids/support_rate，于是 generation_status 仍显示 model_passed、
visible_content_chars 记录的是被丢弃那份正文的长度，且裸模板不含冲突块与来源边界块。

本文件守住修复后的四条不变量：
1. 任何正文变更都同步来源/状态/阶段/篇幅/哈希；
2. 回退保留强制披露（冲突断言、来源边界）；
3. 不再写 20 字占位符，无法构建合格替代稿时保留可读正文并标记待人工复核；
4. 支持率通过不能掩盖披露缺失或正文被改动（交付门槛）。
"""
from __future__ import annotations

import json
import uuid

from backend.app import report_delivery, workflow
from backend.app.database import connect, now_iso, row
from backend.app.main import startup
from backend.app.p0 import report_quality

SECTION = "debt"              # LLM 章节且不在 p0.SYSTEM_SECTIONS 内：主张必须有证据支持，
                                # 且回退时 generation_status 记 fallback（确定性章节记 deterministic）
CONFLICT_BLOCK = (
    "### 冲突事实（以下断言必须并列保留）\n"
    "- 营业收入（2025，字段 revenue）：\n"
    "  - 断言 a-1：100万元；来源：src-1\n"
    "  - 断言 a-2：120万元；来源：src-1"
)


def _fixture(*, with_source: bool = True) -> str:
    """建一个最小项目；with_source=True 时带一条可引用材料。"""
    project_id = f"delivery-{uuid.uuid4()}"
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,"
            "created_at,updated_at,consent_at,project_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", "交付一致性测试", 500, 12, "补充流动资金", "review",
             now, now, now, "bond_guarantee"),
        )
        if with_source:
            # analysis_documents / analysis_sources 是视图，基表是 documents / sources
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,"
                "quality,locator_count,warning,created_at,parser,ocr_status,page_count,document_role) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"doc-{project_id}", project_id, "募集说明书.pdf", "募集说明书", "application/pdf", "x", 1,
                 "parsed", "ok", 1, "", now, "native", "skipped", 1, "prospectus"),
            )
            conn.execute(
                "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,"
                "extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("src-1", project_id, f"doc-{project_id}", "page", "第1页",
                 "营业收入100万元、120万元", 1, "{}", "native", "h"),
            )
    return project_id


def _cleanup(project_id: str) -> None:
    with connect() as conn:
        for table in ("report_claims", "audit_events", "report_sections", "citations",
                      "sources", "documents"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_post_support_fallback_syncs_metadata_and_keeps_disclosures():
    """后置回退必须同步来源/状态/阶段/篇幅/哈希，并保留冲突披露。"""
    startup()
    project_id = _fixture()
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key=SECTION, title="主营业务及经营稳定性",
            content="据募集说明书披露，营业收入为30000万元。",      # 30000 不在材料里 → 无证据
            citation_ids=[], allowed_source_ids=["src-1"],
            content_origin="model", generation_status="model_passed",
            required_disclosures=[CONFLICT_BLOCK],
            deterministic_draft=f"据募集说明书披露，本章材料缺口待补充原件后核验。\n\n{CONFLICT_BLOCK}",
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="pending",
        )
        template = workflow.SectionTemplate(key=SECTION, title="主营业务及经营稳定性",
                                            content="", source_ids=["src-1"])
        handled = workflow._fallback_unsupported_sections(project_id, {SECTION: template})

        section = row("SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, SECTION))
        assert handled == 1
        # ① 状态同步：不再显示"模型已采用"
        assert section["content_origin"] == "deterministic"
        assert section["generation_status"] == "fallback"
        assert section["fallback_stage"] == "report_support"
        assert section["fallback_reason"]
        # ② 强制披露保留
        assert CONFLICT_BLOCK in section["content"]
        # ③ 篇幅与哈希对应**最终**正文，而不是被丢弃那份
        assert section["raw_output_chars"] == len(section["content"])
        assert section["content_sha256"] == report_delivery.content_sha256(section["content"])
        assert section["visible_content_chars"] < section["raw_output_chars"]   # 冲突块不计篇幅
        assert "30000" not in section["content"]
        # ④ 失败现场可追溯（原因码 + 具体主张）
        audit = row("SELECT after_json FROM audit_events WHERE project_id=? AND action='fallback' "
                    "ORDER BY rowid DESC LIMIT 1", (project_id,))
        payload = json.loads(audit["after_json"])
        assert payload["outcome"] == "replaced_with_deterministic_draft"
        assert payload["reason_code"] == "support_unverified"
        assert payload["unsupported_claims"]
    finally:
        _cleanup(project_id)


def test_unbuildable_candidate_keeps_readable_draft_without_placeholder():
    """原正文与候选稿都不合格时：保留可读正文并标记待人工复核，不写占位符。"""
    startup()
    project_id = _fixture(with_source=False)
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key=SECTION, title="主营业务及经营稳定性",
            content="据募集说明书披露，营业收入为30000万元。",
            citation_ids=[], allowed_source_ids=[], content_origin="model",
            generation_status="model_passed", required_disclosures=[],
            deterministic_draft="",                     # 没有可用的冻结草稿
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="pending",
        )
        handled = workflow._fallback_unsupported_sections(project_id, {})
        section = row("SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, SECTION))

        assert handled == 1
        assert "本章节内容保留为草稿" not in section["content"]      # 不再降级为占位符
        assert section["content"].strip()                            # 保留可读正文
        assert section["delivery_status"] == "needs_review"
        assert section["fallback_stage"] == "report_support"
        audit = row("SELECT after_json FROM audit_events WHERE project_id=? AND action='fallback' "
                    "ORDER BY rowid DESC LIMIT 1", (project_id,))
        assert json.loads(audit["after_json"])["outcome"] == "kept_for_human_review"
    finally:
        _cleanup(project_id)


def test_disclosure_missing_blocks_delivery_even_when_support_passes():
    """支持率通过不能掩盖强制披露缺失。"""
    startup()
    project_id = _fixture()
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key=SECTION, title="主营业务及经营稳定性",
            content="据募集说明书披露，本章材料缺口待补充原件后核验。",   # 不含冲突块
            citation_ids=[], allowed_source_ids=["src-1"], content_origin="deterministic",
            generation_status="deterministic", required_disclosures=[CONFLICT_BLOCK],
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="pending",
        )
        blockers = report_delivery.delivery_blockers(project_id)
        assert blockers and any("强制披露" in item for item in blockers)

        section = row("SELECT disclosure_status,delivery_status FROM report_sections "
                      "WHERE project_id=? AND section_key=?", (project_id, SECTION))
        assert section["disclosure_status"] == "warning"
        assert section["delivery_status"] == "needs_review"
    finally:
        _cleanup(project_id)


def test_length_outside_prompt_target_does_not_block_delivery():
    content = "正文超过用户的目标字符数。"
    disclosure, delivery, validated_hash = report_delivery.assess_delivery(
        content=content, required_disclosures=[], support_status="passed", length_status="long",
    )
    assert disclosure == "passed"
    assert delivery == "eligible"
    assert validated_hash == report_delivery.content_sha256(content)


def test_content_change_invalidates_validated_hash():
    """正文被改动后，旧校验哈希失效，不能继续当作已校验结果。"""
    startup()
    project_id = _fixture()
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key=SECTION, title="主营业务及经营稳定性",
            content="据募集说明书披露，本章材料缺口待补充原件后核验。",
            citation_ids=[], allowed_source_ids=["src-1"], content_origin="model",
            generation_status="model_passed", required_disclosures=[],
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="pending",
        )
        from backend.app.p0 import validate_report_support
        validate_report_support(project_id)
        before = row("SELECT content_sha256,validated_content_sha256 FROM report_sections "
                     "WHERE project_id=? AND section_key=?", (project_id, SECTION))
        assert before["validated_content_sha256"] == before["content_sha256"]   # 校验通过后绑定

        report_delivery.write_report_section(
            project_id=project_id, section_key=SECTION, title="主营业务及经营稳定性",
            content="据募集说明书披露，本次修改后的正文内容。",
            citation_ids=[], allowed_source_ids=["src-1"], content_origin="human",
            generation_status="human_edit", required_disclosures=[],
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="pending",
        )
        after = row("SELECT content_sha256,validated_content_sha256 FROM report_sections "
                    "WHERE project_id=? AND section_key=?", (project_id, SECTION))
        assert after["content_sha256"] != before["content_sha256"]
        # 写入新正文但尚未重新校验 → 校验哈希仍指向旧正文
        assert after["validated_content_sha256"] == before["validated_content_sha256"]
        assert after["validated_content_sha256"] != after["content_sha256"]
        # 重新执行完整校验后，哈希重新绑定到新正文（旧的篇幅/诊断不再代表当前结果）
        validate_report_support(project_id)
        rebound = row("SELECT content_sha256,validated_content_sha256 FROM report_sections "
                      "WHERE project_id=? AND section_key=?", (project_id, SECTION))
        assert rebound["validated_content_sha256"] == rebound["content_sha256"]
    finally:
        _cleanup(project_id)


def _review_snapshot(project_id: str, section_key: str) -> dict:
    """复核侧快照：章节 review_status 的两条独立记录 + 项目级最终复核标记。"""
    section = row("SELECT review_status,support_validation_status,updated_at FROM report_sections "
                  "WHERE project_id=? AND section_key=?", (project_id, section_key))
    project = row("SELECT report_reviewed_revision,report_reviewed_at FROM projects WHERE id=?", (project_id,))
    return {
        "section": dict(section),
        "project": dict(project),
    }


def test_reset_section_review_restores_draft_and_revokes_final_review():
    """正例：复核后重写正文，章节恢复 draft，项目级「最终复核」标记被撤销。"""
    startup()
    project_id = _fixture()
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key=SECTION, title="主营业务及经营稳定性",
            content="据募集说明书披露，本章材料缺口待补充原件后核验。",
            citation_ids=[], allowed_source_ids=["src-1"], content_origin="model",
            generation_status="model_passed", required_disclosures=[],
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="passed", review_status="reviewed",       # 人工已复核该章
        )
        # 模拟"项目已最终复核"：最终复核修订号与当前正文修订号对齐（report_state → final_ready）。
        with connect() as conn:
            conn.execute(
                "UPDATE projects SET report_revision=5,review_revision=5,"
                "report_reviewed_revision=5,report_reviewed_at=? WHERE id=?",
                (now_iso(), project_id),
            )
        assert _review_snapshot(project_id, SECTION)["project"]["report_reviewed_revision"] == 5

        state = report_delivery.reset_section_review(project_id, SECTION)

        snapshot = _review_snapshot(project_id, SECTION)
        assert snapshot["section"]["review_status"] == "draft"                     # 恢复章节待复核
        assert snapshot["project"]["report_reviewed_revision"] == 0                # 撤销旧最终复核标记
        assert snapshot["project"]["report_reviewed_at"] is None
        assert state["review_status"] == "draft"
        assert state["report_reviewed_revision"] == 0
        # 证据侧字段不由本函数负责，必须原样保留
        assert snapshot["section"]["support_validation_status"] == "passed"
    finally:
        _cleanup(project_id)


def test_reset_section_review_is_idempotent():
    """正例/幂等：连续调用两次，复核侧记录完全一致（含 updated_at）。"""
    startup()
    project_id = _fixture()
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key=SECTION, title="主营业务及经营稳定性",
            content="据募集说明书披露，本章材料缺口待补充原件后核验。",
            citation_ids=[], allowed_source_ids=["src-1"], content_origin="model",
            generation_status="model_passed", required_disclosures=[],
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="passed", review_status="reviewed",
        )
        with connect() as conn:
            conn.execute(
                "UPDATE projects SET report_revision=2,review_revision=2,"
                "report_reviewed_revision=2,report_reviewed_at=? WHERE id=?",
                (now_iso(), project_id),
            )
        report_delivery.reset_section_review(project_id, SECTION)
        first = _review_snapshot(project_id, SECTION)
        report_delivery.reset_section_review(project_id, SECTION)
        second = _review_snapshot(project_id, SECTION)

        assert first == second
        assert first["section"]["review_status"] == "draft"
        assert first["project"]["report_reviewed_revision"] == 0
        assert first["project"]["report_reviewed_at"] is None
    finally:
        _cleanup(project_id)


def test_reset_section_review_scopes_to_one_section_and_keeps_evidence_fields():
    """负例/边界：只回退目标章节，不波及其他章节的 review_status，也不动证据侧字段。"""
    startup()
    project_id = _fixture()
    other = "guarantee"
    try:
        for key, support in ((SECTION, "passed"), (other, "warning")):
            report_delivery.write_report_section(
                project_id=project_id, section_key=key, title=key,
                content="据募集说明书披露，本章材料缺口待补充原件后核验。",
                citation_ids=[], allowed_source_ids=["src-1"], content_origin="model",
                generation_status="model_passed", required_disclosures=[],
                length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
                support_status=support, review_status="reviewed",
            )
        with connect() as conn:
            conn.execute(
                "UPDATE projects SET report_revision=7,review_revision=7,"
                "report_reviewed_revision=7,report_reviewed_at=? WHERE id=?",
                (now_iso(), project_id),
            )

        report_delivery.reset_section_review(project_id, SECTION)

        target = _review_snapshot(project_id, SECTION)
        untouched = _review_snapshot(project_id, other)
        assert target["section"]["review_status"] == "draft"
        assert untouched["section"]["review_status"] == "reviewed"       # 其他章节不受影响
        assert untouched["section"]["support_validation_status"] == "warning"
        assert target["section"]["support_validation_status"] == "passed"  # 证据侧未被误改
        # 项目级标记是全局的：一旦任一章节回到草稿，最终复核即整体失效
        assert target["project"]["report_reviewed_revision"] == 0
    finally:
        _cleanup(project_id)


def test_model_adoption_rate_counts_sections_not_events():
    """采用率按"章节"统计：6 章尝试模型、2 章后置回退 → 4/6；确定性章节不计入分母。"""
    startup()
    project_id = _fixture()
    keys = ["financial", "cashflow", "debt", "rating", "guarantee", "counter_guarantee"]
    try:
        for index, key in enumerate(keys):
            adopted = index < 4
            report_delivery.write_report_section(
                project_id=project_id, section_key=key, title=key,
                content="据募集说明书披露，本章材料缺口待补充原件后核验。",
                citation_ids=[], allowed_source_ids=["src-1"],
                content_origin="model" if adopted else "deterministic",
                generation_status="model_passed" if adopted else "fallback",
                fallback_reason="" if adopted else "模型正文未通过后置证据检查，已回退确定性模板",
                fallback_stage="none" if adopted else "report_support",
                required_disclosures=[],
                length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
                support_status="pending",
            )
        from backend.app.p0 import validate_report_support
        validate_report_support(project_id)
        quality = report_quality(project_id)["delivery_quality"]
        # 再写一个确定性章节：它即使发生后置回退，也不能计入采用率分母
        from backend.app.database import execute
        execute("UPDATE report_sections SET generation_status='deterministic',content_origin='deterministic',"
                "fallback_stage='report_support' WHERE project_id=? AND section_key=?", (project_id, keys[0]))

        assert quality["model_attempted_sections"] == 6
        assert quality["model_adopted_sections"] == 4
        assert quality["model_adoption_rate"] == round(4 / 6, 4)
        assert quality["support_fallbacks"] == 2
    finally:
        _cleanup(project_id)
