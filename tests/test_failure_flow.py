"""一次修复（安全优先）与失败处理的专项回归（修订稿 §3.3）。

覆盖：

1. 安全被拒 → 恰好一次安全修复 → 通过则采用，模型调用总次数为 2；
2. 修复稿仍被拒 → 回退确定性正文、阶段 ``chapter_validation``、原因含最初原因且提及修复，
   且**没有第二次修复**（模型调用总次数仍为 2）；
3. 仅篇幅超限 → 共用同一"每章一次"配额，只压缩一次；
4. 单章再生失败：不写入新正文、保留旧正文、记录失败并返回失败响应；
5. 后置支持校验不覆盖最初的失败诊断。

环境事实：本机 ``is_configured() == False``，因此这里用假模型桩驱动模型分支，
不依赖真实模型配置。
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException

from backend.app import llm, workflow
from backend.app.chapter_guard import build_chapter_context
from backend.app.database import connect, execute, now_iso, row
from backend.app.llm import LlmUsage, StructuredLlmError
from backend.app.main import startup
from backend.app.schemas import LlmSection, SectionRegenerateRequest

PROJECT_ID = "demo-bond-huachen"
CHAPTER = "rating"                       # 模型章节，且确定性模板能通过自身输出契约
# 命中 `_report_boundary_safe` 的"冒充已决定"结论。
# ⚠️ 不能用"建议通过担保"：《风控约束分层》§一 允许承保/拒保**建议**，
# 该句已从禁止表移除（2026-10-17）。用带决策主语"本所"的既成表述才仍被拦。
FORBIDDEN = "本所同意担保"
INITIAL_REASON = "章节包含禁止的承保或审批结论"


def _context() -> dict:
    """用**真实确定性章节正文**构造上下文：rating 的模板能通过自身输出契约。"""
    from backend.app.bond_report import generate_bond_chapters

    startup()
    chapter = next(item for item in generate_bond_chapters(PROJECT_ID, False) if item.key == CHAPTER)
    return build_chapter_context(PROJECT_ID, CHAPTER, chapter.content, chapter.source_ids)


def _section(content: str, context: dict) -> LlmSection:
    return LlmSection(content=content, citation_ids=list(context.get("allowed_citation_ids") or []))


def _attempt(context: dict) -> dict:
    return workflow.run_chapter_model_attempt(
        "评级", context["draft"], [], chapter_key=CHAPTER, chapter_context=context,
        call_context=workflow.LlmCallContext(PROJECT_ID, "", "draft", "report"),
    )


@pytest.fixture(autouse=True)
def _neutralise_support_gate(monkeypatch):
    """本文件的用例只考察"每章一次修复配额"与台账，不考察证据支持语义。

    桩正文用的是 ``chapter_context["draft"]``，也就是**确定性模板**文本；它只在
    ``content_origin=deterministic`` 下才被判为有支持。支持检查并入同一候选评估后
    （计划 §四 line 33），把它当**模型正文**做真实支持评估会整体不过，于是每个用例都
    落进回退分支——那是另一码事。

    桩打在 ``_delivery_assessment``（最底层）而不是 ``_assess_candidate_support``：
    这样"探针章节带没带冻结记录"仍走真实构造，用例自己再打同层桩即可覆盖本桩；
    支持语义由 ``test_support_failure_*`` 三例覆盖（它们在更上层 monkeypatch）。
    """

    def _pass(*_args, **_kwargs):
        return {"passed": True, "reasons": [], "unsupported_claims": 0,
                "needs_review_claims": 0, "invalid_citations": 0, "unsupported_claim_items": []}

    monkeypatch.setattr(workflow, "_delivery_assessment", _pass)


# ------------------------------------------------ 安全修复：正例 / 负例 --

def test_safety_rejection_triggers_exactly_one_repair_and_passes(monkeypatch):
    """首稿被安全拒绝 → 恰好一次安全修复 → 修复稿通过 → 采用修复稿。"""
    context = _context()
    calls: list[str] = []

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        if safety_repair is not None:
            calls.append("repair")
            # 修复输入必须是被拒稿正文与具体诊断。
            assert safety_repair.get("previous_content")
            assert safety_repair.get("reasons")
            return _section(chapter_context["draft"], chapter_context), LlmUsage()
        calls.append("rewrite")
        # 首稿在被拒稿正文后附加禁止结论 → 命中 _report_boundary_safe。
        return _section(f"{chapter_context['draft']}\n{FORBIDDEN}。", chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)

    outcome = _attempt(context)

    assert calls == ["rewrite", "repair"]                 # 首次 + 恰好一次安全修复
    assert outcome["generation_status"] == "model_passed"
    assert outcome["fallback_reason"] == ""
    assert outcome["fallback_stage"] == "none"
    assert outcome["repair_attempted"] is True
    assert FORBIDDEN not in outcome["content"]
    # 本跑首拒原因随台账冻结（收口方案 §五"逐条证明"）：验收不再从历史失败行猜原因。
    attempt = row("SELECT diagnostics_json FROM chapter_generation_attempts "
                  "WHERE project_id=? AND section_key=? ORDER BY rowid DESC LIMIT 1",
                  (PROJECT_ID, CHAPTER))
    diagnostics = json.loads(attempt["diagnostics_json"])
    assert any("禁止" in item for item in diagnostics.get("first_rejection_reasons") or [])


def test_safety_repair_failure_falls_back_without_a_second_repair(monkeypatch):
    """修复稿仍被拒 → 回退确定性正文、阶段 chapter_validation、保留最初原因、无第二次修复。"""
    context = _context()
    calls: list[str] = []

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        if safety_repair is not None:
            calls.append("repair")
            return _section(f"{chapter_context['draft']}\n{FORBIDDEN}。", chapter_context), LlmUsage()
        calls.append("rewrite")
        return _section(f"{chapter_context['draft']}\n{FORBIDDEN}。", chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)

    outcome = _attempt(context)

    assert calls == ["rewrite", "repair"]                 # 只有一次修复，绝不第二次
    assert outcome["generation_status"] == "fallback"
    assert outcome["fallback_stage"] == "chapter_validation"
    assert outcome["repair_attempted"] is True
    assert INITIAL_REASON in outcome["fallback_reason"]   # 保留最初校验原因
    assert "已尝试一次修复仍未通过" in outcome["fallback_reason"]
    assert outcome["content"] == context["draft"]         # 回退确定性正文

    diagnostics = workflow.chapter_diagnostics(outcome["validation"])
    assert diagnostics["safety_repair_failed"] is True
    assert diagnostics["safety_repair_reasons"]


def test_safety_repair_needs_review_draft_is_adopted_not_discarded(monkeypatch):
    """修复稿仅剩未决项（needs_review）→ 采用**修复稿**并收口为 `model_needs_review`。

    收口方案 §三：「安全修复函数在 ``passed`` 或 ``needs_review`` 时返回真实修复稿；
    不能因修复后待核验而退回原被拒稿。」旧实现只在 ``passed`` 时采纳，修复稿带着
    未决项时会把（已修复安全的）修复稿丢掉、退回被拒首稿并记回退——那次修复机会
    白花，可交付稿混进回退统计。

    触发片段只用本章草稿里**已有的数字**（48,000 / 31,000，不引入新数字），句尾裸数字
    产生 ambiguous_attribution 未决项而不产生硬失败——真实校验链路，不蒙桩。
    """
    context = _context()
    calls: list[str] = []
    NEEDS_REVIEW_SUFFIX = "公司期末余额为48,000与31,000。"

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        if safety_repair is not None:
            calls.append("repair")
            return _section(f"{chapter_context['draft']}\n{NEEDS_REVIEW_SUFFIX}", chapter_context), LlmUsage()
        calls.append("rewrite")
        return _section(f"{chapter_context['draft']}\n{FORBIDDEN}。", chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)

    outcome = _attempt(context)

    assert calls == ["rewrite", "repair"]                  # 仍然恰好一次修复
    assert outcome["generation_status"] == "model_needs_review"   # 正文是修复稿、状态待核验
    assert outcome["fallback_reason"] == ""
    assert outcome["fallback_stage"] == "none"
    assert NEEDS_REVIEW_SUFFIX in outcome["content"]       # 采用的是修复稿，不是被拒首稿
    assert FORBIDDEN not in outcome["content"]
    assert outcome["repair_attempted"] is True
    assert outcome["validation"]["status"] == "needs_review"
    # 修复稿的未决项是真实校验产物（ambiguous_attribution），不是空壳标记。
    codes = {item.get("code") for item in outcome["validation"].get("needs_review_items") or []}
    assert "ambiguous_attribution" in codes


def test_length_only_failure_uses_one_compression(monkeypatch):
    """仅篇幅超限时共用同一"每章一次"配额：只做一次篇幅压缩。"""
    base = _context()
    context = {**base, "length_policy": {**base["length_policy"], "effective_max_chars": 5}}
    calls: list[str] = []

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        if length_repair is not None:
            # 修复输入必须包含被拒稿（首稿模型正文），否则压缩没有对象。
            assert length_repair.get("previous_content")
        calls.append("length" if length_repair else "rewrite")
        return _section(context["draft"], chapter_context or context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)

    outcome = _attempt(context)

    # 首次改写 + 一次篇幅压缩（压缩请求自带 previous_content），共用"每章一次"配额。
    assert calls == ["rewrite", "length"]
    assert outcome["generation_status"] == "model_passed"  # 篇幅只提示，不回退
    assert outcome["repair_attempted"] is True


def test_real_model_calls_are_capped_at_two_per_chapter(monkeypatch):
    """真实调用链上限：首稿 + 恰好一次修复 == 2 次模型调用（篇幅与安全两种场景）。

    直接在 `llm._chat_json` 层计次。篇幅场景此前是"首稿 + 全量重写 + 压缩"共 3 次调用，
    本用例把它钉死在 2 次（修订稿 §3.3"每章最多一次额外内容调整"）。
    """
    from backend.app import llm

    def drive(chapter_ctx: dict, replies: list[str]):
        calls: list[dict] = []

        def fake_chat(system_prompt, prompt, *, schema=None, normalizer=None,
                      allowed_source_ids=None, context=None, max_input_chars=None):
            index = len(calls)
            request = json.loads(prompt)
            calls.append(request)
            reply_text = replies[min(index, len(replies) - 1)]
            citations = list(chapter_ctx.get("allowed_citation_ids") or [])
            # P1-2：rating 章带内置小节（构建期注入 required_subsections）→ 应答按小节分条，
            # 与真实契约一致；无小节配置的上下文仍回旧协议的整章 content。
            required = request.get("required_subsections") or []
            if required:
                return ({
                    "subsections": [
                        {"subsection_id": item["subsection_id"], "content": reply_text,
                         "citation_ids": citations}
                        for item in required
                    ],
                    "citation_ids": [],
                }, LlmUsage())
            return ({"content": reply_text, "citation_ids": citations}, LlmUsage())

        monkeypatch.setattr(llm, "is_configured", lambda: True)
        monkeypatch.setattr(llm, "_chat_json", fake_chat)
        monkeypatch.setattr(llm, "chapter_prompt", lambda *_args, **_kwargs: "system")
        outcome = workflow.run_chapter_model_attempt(
            "评级", chapter_ctx["draft"], [], chapter_key=CHAPTER, chapter_context=chapter_ctx,
            call_context=workflow.LlmCallContext(PROJECT_ID, "", "draft", "report"),
        )
        return calls, outcome

    base = _context()
    # ① 仅篇幅超限：首稿合格但超长 → 一次定向压缩；共 2 次调用，压缩输入=首稿正文。
    long_context = {**base, "length_policy": {**base["length_policy"], "effective_max_chars": 5}}
    calls, outcome = drive(long_context, [base["draft"], base["draft"]])
    assert len(calls) == 2
    assert outcome["generation_status"] == "model_passed"      # 篇幅只提示，不回退
    assert "length_repair" not in calls[0] and "length_repair" in calls[1]
    assert str(base["draft"]).strip()[:40] in str(calls[1]["length_repair"]["previous_content"])
    assert calls[0]["allowed_citation_ids"] == calls[1]["allowed_citation_ids"]   # 同一引用白名单

    # ② 安全被拒：首稿带禁止结论 → 一次安全修复；共 2 次调用。
    calls, outcome = drive(base, [f"{base['draft']}\n{FORBIDDEN}。", base["draft"]])
    assert len(calls) == 2
    assert outcome["generation_status"] == "model_passed"
    assert "safety_repair" not in calls[0] and "safety_repair" in calls[1]


# ---------------------------------------------- 单章再生失败保留旧正文 --

def test_regenerate_failure_keeps_old_body_and_records_failure(monkeypatch):
    """单章再生模型失败时不写入新正文，保留旧正文为旧版本并记录失败。"""
    from backend.app import main

    startup()
    execute("DELETE FROM report_sections WHERE project_id=? AND section_key=?", (PROJECT_ID, CHAPTER))
    execute(
        "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,"
        "review_status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"sec-keep-{uuid.uuid4()}", PROJECT_ID, CHAPTER, "评级",
         "旧模型正文 OLD_KEEP_BODY 987654321。", "[]", "draft", now_iso()),
    )

    def fake_attempt(*_args, **_kwargs):
        return {
            "content": "模型新正文 NO_KEEP_SHOULD_NOT_WRITE。",
            "citation_ids": None,
            "usage": LlmUsage(),
            "validation": {"status": "fallback", "reasons": [f"{INITIAL_REASON}（已尝试一次修复仍未通过）"]},
            "generation_status": "fallback",
            "fallback_reason": f"{INITIAL_REASON}（已尝试一次修复仍未通过）",
            "fallback_stage": "chapter_validation",
            "repair_attempted": True,
        }

    monkeypatch.setattr(main, "run_chapter_model_attempt", fake_attempt)
    try:
        response = main.regenerate_section(PROJECT_ID, CHAPTER, x_project_token="")
        stored = row("SELECT content,validation_status,fallback_reason,fallback_stage FROM report_sections "
                     "WHERE project_id=? AND section_key=?", (PROJECT_ID, CHAPTER))

        assert "OLD_KEEP_BODY" in stored["content"]
        assert "NO_KEEP_SHOULD_NOT_WRITE" not in stored["content"]
        assert stored["validation_status"] == "failed"
        assert stored["fallback_stage"] == "chapter_validation"
        assert INITIAL_REASON in stored["fallback_reason"]
        assert response["generationStatus"] == "failed"
        audit = row("SELECT after_json FROM audit_events WHERE project_id=? AND "
                    "action='report_section_regenerate_failed' ORDER BY rowid DESC LIMIT 1", (PROJECT_ID,))
        assert audit and INITIAL_REASON in json.loads(audit["after_json"])["failure"]
    finally:
        execute("DELETE FROM report_sections WHERE project_id=? AND section_key=?", (PROJECT_ID, CHAPTER))


# ------------------------------------- 后置校验不覆盖最初失败诊断 --

def _failure_fixture(*, with_source: bool = True) -> str:
    """最小项目：可引用材料 + 一条带最初失败诊断的模型章节。"""
    project_id = f"failure-flow-{uuid.uuid4()}"
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,"
            "created_at,updated_at,consent_at,project_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", "失败流测试", 500, 12, "补充流动资金", "review",
             now, now, now, "bond_guarantee"),
        )
        if with_source:
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
        for table in ("report_claims", "audit_events", "report_sections", "citations", "sources", "documents"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_post_support_fallback_does_not_overwrite_initial_reason():
    """章节已有最初失败诊断时，后置支持回退只写诊断与审计，不覆盖 fallback_reason/阶段。"""
    from backend.app import report_delivery

    startup()
    project_id = _failure_fixture()
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key="debt", title="债务结构",
            content="据募集说明书披露，营业收入为30000万元。",     # 30000 不在材料里 → 无证据
            citation_ids=[], allowed_source_ids=["src-1"],
            content_origin="deterministic", generation_status="fallback",
            fallback_reason=INITIAL_REASON, fallback_stage="chapter_validation",
            required_disclosures=[],
            deterministic_draft="据募集说明书披露，本章材料缺口待补充原件后核验。",
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="pending",
        )
        template = workflow.SectionTemplate(key="debt", title="债务结构", content="", source_ids=["src-1"])
        handled = workflow._fallback_unsupported_sections(project_id, {"debt": template})

        section = row("SELECT fallback_reason,fallback_stage,validation_diagnostics FROM report_sections "
                      "WHERE project_id=? AND section_key=?", (project_id, "debt"))
        assert handled == 1
        assert section["fallback_reason"] == INITIAL_REASON      # 最初诊断被保留
        assert section["fallback_stage"] == "chapter_validation"  # 阶段同样不被覆盖
        diagnostics = json.loads(section["validation_diagnostics"])
        assert diagnostics.get("post_support_unverified") is True
        audit = row("SELECT after_json FROM audit_events WHERE project_id=? AND action='fallback' "
                    "ORDER BY rowid DESC LIMIT 1", (project_id,))
        assert json.loads(audit["after_json"])["preserved_initial_reason"] is True
    finally:
        _cleanup(project_id)


def test_post_support_fallback_writes_reason_when_initially_empty():
    """原本没有失败诊断的章节，后置支持回退仍写支持失败原因（不回归）。"""
    from backend.app import report_delivery

    startup()
    project_id = _failure_fixture()
    try:
        report_delivery.write_report_section(
            project_id=project_id, section_key="debt", title="债务结构",
            content="据募集说明书披露，营业收入为30000万元。",
            citation_ids=[], allowed_source_ids=["src-1"],
            content_origin="model", generation_status="model_passed",
            fallback_reason="", fallback_stage="none",
            required_disclosures=[],
            deterministic_draft="据募集说明书披露，本章材料缺口待补充原件后核验。",
            length_policy={"effective_min_chars": 1, "effective_max_chars": 5000},
            support_status="pending",
        )
        template = workflow.SectionTemplate(key="debt", title="债务结构", content="", source_ids=["src-1"])
        workflow._fallback_unsupported_sections(project_id, {"debt": template})

        section = row("SELECT fallback_reason,fallback_stage FROM report_sections "
                      "WHERE project_id=? AND section_key=?", (project_id, "debt"))
        # 预期变更（α 细化后）：确定性草稿本身也可能被判为待核验，后置回退因此可能落在
        # "保留正文供人工核验"分支；两条分支都属支持失败，均须写入支持原因并标记 report_support。
        assert section["fallback_reason"]
        assert ("后置证据检查" in section["fallback_reason"]
                or "证据支持" in section["fallback_reason"])
        assert section["fallback_stage"] == "report_support"
    finally:
        _cleanup(project_id)


# ------------------------------------------- JSON 修复占配额 / needs_review --

def _cleanup_attempts() -> None:
    execute("DELETE FROM chapter_generation_attempts WHERE project_id=? AND section_key=?",
            (PROJECT_ID, CHAPTER))


def test_json_parse_failure_uses_single_repair_opportunity(monkeypatch):
    """首次输出不可解析 → 恰好一次 JSON 修复 → 通过即采用；调用总次数为 2。"""
    context = _context()
    calls: list = []

    def fake_rewrite(*_args, **_kwargs):
        calls.append("rewrite")
        exc = StructuredLlmError("report_repair_required", "首次输出不可解析")
        exc.raw_content = '{"content": "broken'
        raise exc

    def fake_repair_json(title, content, evidence, context=None, *, chapter_key=None,
                         chapter_context=None, strict_citations=False, raw_content="", error_type="",
                         drop_report=None):
        calls.append(("repair_json", raw_content, error_type))
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    monkeypatch.setattr(llm, "repair_section_json", fake_repair_json, raising=False)
    try:
        outcome = _attempt(context)
        assert calls[0] == "rewrite"
        assert calls[1][0] == "repair_json"
        assert calls[1][1] == '{"content": "broken'          # raw_content 透传
        assert calls[1][2] == "report_repair_required"
        assert len(calls) == 2                                # 只有一次修复机会
        assert outcome["generation_status"] == "model_passed"
        assert outcome["repair_attempted"] is True
        attempt = row("SELECT status,repair_type FROM chapter_generation_attempts "
                      "WHERE project_id=? AND section_key=? ORDER BY rowid DESC LIMIT 1", (PROJECT_ID, CHAPTER))
        assert attempt["repair_type"] == "json"
        assert attempt["status"] == "passed"
    finally:
        _cleanup_attempts()


def test_json_repair_failure_falls_back_without_a_second_repair(monkeypatch):
    """JSON 修复稿仍被拒 → 配额已用，不再触发安全修复，回退确定性正文。"""
    context = _context()
    calls: list[str] = []

    def fake_rewrite(*_args, **_kwargs):
        calls.append("rewrite")
        exc = StructuredLlmError("report_repair_required", "首次输出不可解析")
        exc.raw_content = "raw"
        raise exc

    def fake_repair_json(*_args, **kwargs):
        calls.append("repair_json")
        ctx = kwargs["chapter_context"]
        return _section(f"{ctx['draft']}\n{FORBIDDEN}。", ctx), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    monkeypatch.setattr(llm, "repair_section_json", fake_repair_json, raising=False)
    try:
        outcome = _attempt(context)
        assert calls == ["rewrite", "repair_json"]            # 绝不再追加安全修复
        assert outcome["generation_status"] == "fallback"
        assert outcome["fallback_stage"] == "chapter_validation"
        assert outcome["content"] == context["draft"]
        attempt = row("SELECT status,repair_type FROM chapter_generation_attempts "
                      "WHERE project_id=? AND section_key=? ORDER BY rowid DESC LIMIT 1", (PROJECT_ID, CHAPTER))
        assert attempt["status"] == "failed" and attempt["repair_type"] == "json"
    finally:
        _cleanup_attempts()


def test_needs_review_triggers_one_safety_repair_and_adopts(monkeypatch):
    """needs_review 与 rejected 同样触发一次安全修复；修复通过 → 采用。"""
    context = _context()
    calls: list[str] = []
    seen = {"n": 0}

    def fake_validate(content, citation_ids, ctx):
        seen["n"] += 1
        if seen["n"] == 1:
            return {"status": "needs_review", "reasons": [],
                    "needs_review_items": [{"reason_code": "ambiguous_attribution"}],
                    "needs_review_count": 1, "length": {"status": "ok"}, "length_only_failure": False}
        return {"status": "passed", "reasons": [], "length": {"status": "ok"}, "length_only_failure": False}

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        if safety_repair is not None:
            calls.append("repair")
            return _section(chapter_context["draft"], chapter_context), LlmUsage()
        calls.append("rewrite")
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    monkeypatch.setattr(workflow, "validate_chapter_output", fake_validate)
    try:
        outcome = _attempt(context)
        assert calls == ["rewrite", "repair"]
        assert outcome["generation_status"] == "model_passed"
        assert outcome["validation"]["status"] == "passed"
        assert outcome["repair_attempted"] is True
    finally:
        _cleanup_attempts()


def test_needs_review_after_repair_is_adopted_but_recorded(monkeypatch):
    """修复后仍 needs_review → 采用正文，记 `model_needs_review`（计划 §四 line 38）。

    三态必须分清：全部通过是 `model_passed`（**合格采用**）；"修复后仅剩未决项"是
    `model_needs_review`——正文照旧采用（`content_origin=model`、`adopted=True`），
    但**不计入合格采用率**。旧实现把两者都记成 `model_passed`，采用率因此虚高。
    """
    context = _context()
    calls: list[str] = []

    def fake_validate(content, citation_ids, ctx):
        return {"status": "needs_review", "reasons": [],
                "needs_review_items": [{"reason_code": "ambiguous_attribution"}],
                "needs_review_count": 1, "length": {"status": "ok"}, "length_only_failure": False}

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        calls.append("repair" if safety_repair is not None else "rewrite")
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    monkeypatch.setattr(workflow, "validate_chapter_output", fake_validate)
    try:
        outcome = _attempt(context)
        assert calls == ["rewrite", "repair"]
        assert outcome["generation_status"] == "model_needs_review"   # 采用，但不合格
        assert outcome["validation"]["status"] == "needs_review"      # 仅剩未决项
        assert workflow.is_model_content_status(outcome["generation_status"]) is True
        attempt = row("SELECT status,adopted,diagnostics_json FROM chapter_generation_attempts "
                      "WHERE project_id=? AND section_key=? ORDER BY rowid DESC LIMIT 1", (PROJECT_ID, CHAPTER))
        assert attempt["status"] == "needs_review"
        assert attempt["adopted"] == 1
        diagnostics = json.loads(attempt["diagnostics_json"])
        assert diagnostics.get("needs_review_count") == 1
    finally:
        _cleanup_attempts()


def test_model_needs_review_does_not_raise_the_adoption_rate(monkeypatch):
    """`model_needs_review` 计入"尝试过模型"、不计入"合格采用"（计划 §四 line 38）。

    采用率的分母是"实际尝试模型生成的章节数"，分子只认
    `model_passed` + `content_origin=model` + `delivery_status=eligible`。
    待核验稿三点里差最后一点，所以它**抬高分母、不抬高分子**——采用率因此下降而不是虚高。
    用增量断言：项目里其它章节的状态不影响本用例。
    """
    from backend.app.p0 import report_quality

    def _attempted_adopted() -> tuple[int, int]:
        quality = report_quality(PROJECT_ID)["delivery_quality"]
        return quality["model_attempted_sections"], quality["model_adopted_sections"]

    context = _context()          # 保证库已初始化、章节配置可用
    assert context

    def fake_validate(content, citation_ids, ctx):
        return {"status": "needs_review", "reasons": [],
                "needs_review_items": [{"reason_code": "ambiguous_attribution"}],
                "needs_review_count": 1, "length": {"status": "ok"}, "length_only_failure": False}

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    monkeypatch.setattr(workflow, "validate_chapter_output", fake_validate)
    section_id = f"sec-needs-review-{uuid.uuid4()}"
    execute("DELETE FROM report_sections WHERE project_id=? AND section_key=?", (PROJECT_ID, "debt"))
    try:
        outcome = _attempt(context)
        assert outcome["generation_status"] == "model_needs_review"
        attempted_before, adopted_before = _attempted_adopted()

        execute(
            "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,"
            "review_status,updated_at,generation_status,content_origin,delivery_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (section_id, PROJECT_ID, "debt", "债务分析", "本章为叙述性说明。", "[]", "draft", now_iso(),
             outcome["generation_status"], "model", "needs_review"),
        )
        attempted_after, adopted_after = _attempted_adopted()
        assert attempted_after == attempted_before + 1, "待核验稿要算作尝试过模型"
        assert adopted_after == adopted_before, "待核验稿不得计入合格采用"
    finally:
        execute("DELETE FROM report_sections WHERE id=?", (section_id,))
        execute("DELETE FROM report_sections WHERE project_id=? AND section_key=?", (PROJECT_ID, "debt"))
        _cleanup_attempts()


# ---------------------------------------------------- 409 并发冲突（再生） --

def test_regenerate_returns_409_on_version_conflict_without_overwriting(monkeypatch):
    """写库前版本不一致 → 409，保留并发修改，不覆盖正文（§四）。"""
    from backend.app import main

    startup()
    execute("DELETE FROM report_sections WHERE project_id=? AND section_key=?", (PROJECT_ID, CHAPTER))
    execute(
        "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,"
        "review_status,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"sec-409-{uuid.uuid4()}", PROJECT_ID, CHAPTER, "评级", "旧正文 OLD_BODY_409。", "[]", "draft", now_iso()),
    )
    before = row("SELECT content FROM report_sections WHERE project_id=? AND section_key=?",
                 (PROJECT_ID, CHAPTER))

    def fake_attempt(*_args, **_kwargs):
        return {
            "content": "本章为叙述性说明，未引入新增数据。", "citation_ids": [], "usage": LlmUsage(),
            "validation": {"status": "passed", "reasons": []}, "generation_status": "model_passed",
            "fallback_reason": "", "fallback_stage": "none", "repair_attempted": False, "attempt_id": "",
        }

    monkeypatch.setattr(main, "run_chapter_model_attempt", fake_attempt)
    try:
        with pytest.raises(HTTPException) as exc:
            main.regenerate_section(
                PROJECT_ID, CHAPTER, x_project_token="",
                payload=SectionRegenerateRequest(expected_revision=999999),
            )
        assert exc.value.status_code == 409
        after = row("SELECT content FROM report_sections WHERE project_id=? AND section_key=?",
                    (PROJECT_ID, CHAPTER))
        assert after["content"] == before["content"]          # 并发修改未被覆盖
    finally:
        execute("DELETE FROM report_sections WHERE project_id=? AND section_key=?", (PROJECT_ID, CHAPTER))


# ------------------------------------------------ 预算丢弃证据与允许集收窄 --

def _drop_context() -> dict:
    """两条证据的最小章节上下文（不触库）：source-2 将被预算整条丢弃。"""
    from backend.app.chapter_prompts import load_chapter_config

    evidence = [
        {"id": "c-source-1", "source_id": "source-1", "document_kind": "募集说明书",
         "quote": "短期借款余额为12345万元。"},
        {"id": "c-source-2", "source_id": "source-2", "document_kind": "募集说明书",
         "quote": "长期借款余额为67890万元。"},
    ]
    return {
        "chapter_key": "debt",
        "prompt_config": load_chapter_config("debt"),
        "prompt_version": "t", "prompt_sha256": "t",
        "draft": "据募集说明书披露，材料不足。",
        "facts": [], "metrics": [], "cross_checks": [],
        "evidence": evidence, "conflicts": [], "conflict_block": "",
        "has_missing": False, "allowed_citation_ids": [item["id"] for item in evidence],
        "input_hash": "h",
    }


def _stub_rewrite(*, drop_source: str | None):
    """假模型桩：按需把 source-2 记为"整条丢弃"，返回引用被丢证据的正文。"""
    def fake(title, content, evidence, context=None, *, chapter_key=None, chapter_context=None,
             strict_citations=False, safety_repair=None, length_repair=None, drop_report=None):
        if drop_source and drop_report is not None:
            drop_report.setdefault("dropped_evidence_ids", []).append(drop_source)
            drop_report["budget_exceeded"] = True
        return (
            LlmSection(content="据募集说明书披露，长期借款余额为67890万元；材料不足。",
                       citation_ids=["c-source-2"]),
            LlmUsage(),
        )
    return fake


def test_dropped_evidence_cannot_support_content(monkeypatch):
    """§五收官判据：裁剪掉的证据不能支持正文。

    预算把 source-2 整条丢弃后，允许集与引用白名单必须同步收窄：正文引用被丢证据的
    67890万元 判"新数字"、其引用判"未授权引用"，章节不得采用该正文。
    """
    context = _drop_context()
    monkeypatch.setattr(workflow, "rewrite_section", _stub_rewrite(drop_source="source-2"))
    result = workflow.run_chapter_model_attempt(
        "负债", context["draft"], [], chapter_key="debt", chapter_context=context,
        call_context=workflow.LlmCallContext("p-drop", "", "draft", "report"),
        record_attempt=False,
    )
    # 收窄就地生效：证据与引用白名单只留模型实际收到的材料
    assert [item["source_id"] for item in context["evidence"]] == ["source-1"]
    assert context["allowed_citation_ids"] == ["c-source-1"]
    # 正文未被采用，失败原因可定位到"新数字"与"未授权引用"
    assert result["generation_status"] == "fallback"
    assert "新数字" in result["fallback_reason"]
    assert "未授权引用" in result["fallback_reason"]


def test_kept_evidence_still_supports_content(monkeypatch):
    """正例对照：不丢弃时，同一正文通过（收窄逻辑不得误伤正常路径）。"""
    context = _drop_context()
    monkeypatch.setattr(workflow, "rewrite_section", _stub_rewrite(drop_source=None))
    result = workflow.run_chapter_model_attempt(
        "负债", context["draft"], [], chapter_key="debt", chapter_context=context,
        call_context=workflow.LlmCallContext("p-keep", "", "draft", "report"),
        record_attempt=False,
    )
    assert [item["source_id"] for item in context["evidence"]] == ["source-1", "source-2"]
    assert result["generation_status"] == "model_passed"
    assert result["fallback_reason"] == ""


# ------------------------------------------------ 项二：表格回声与自绘表格 --

def test_model_drawn_markdown_table_is_rejected(monkeypatch):
    """系统表格由服务端拼接：模型自行绘制/回声走样的 Markdown 表格一律拒绝。"""
    context = _drop_context()
    table = "| 科目 | 2025 年 |\n| --- | --- |\n| 资产 | 72,000 |"

    def fake(title, content, evidence, context=None, *, chapter_key=None, chapter_context=None,
             strict_citations=False, safety_repair=None, length_repair=None, drop_report=None):
        return (
            LlmSection(content=f"据募集说明书披露，短期借款余额为12345万元。\n\n{table}",
                       citation_ids=["c-source-1"]),
            LlmUsage(),
        )

    monkeypatch.setattr(workflow, "rewrite_section", fake)
    result = workflow.run_chapter_model_attempt(
        "负债", context["draft"], [], chapter_key="debt", chapter_context=context,
        call_context=workflow.LlmCallContext("p-table", "", "draft", "report"),
        record_attempt=False,
    )
    assert result["generation_status"] == "fallback"
    assert "自行绘制" in result["fallback_reason"]


def test_legacy_boundary_sentence_is_migrated_at_runtime(monkeypatch):
    """旧编译句就地迁移：删掉"必须逐字保留/由系统附入"的矛盾句，补上新边界句；
    用户自定义内容不受影响（三项遗留修复方案 §二）。"""
    from backend.app.chapter_prompts import GENERATE_BOUNDARY_SENTENCE
    from backend.app import llm

    legacy_prompt = (
        "业务目标：评估偿债能力。\n"
        "系统数据表、冲突披露块与来源边界块必须逐字保留，表格一律由系统附入、不得自行绘制；"
    )
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_: legacy_prompt)
    system_prompt, request, *_ = llm._report_request(
        "财务分析", "正文。", [], None, "financial",
        {"project_id": "p", "prompt_config": {"generation_mode": "generate"},
         "system_table_block": "| 科目 | 值 |\n| --- | --- |\n| 资产 | 1 |"},
    )
    assert "必须逐字保留" not in system_prompt
    assert GENERATE_BOUNDARY_SENTENCE in system_prompt
    assert "业务目标：评估偿债能力。" in system_prompt          # 用户内容保留
    assert isinstance(request["system_tables"], dict)           # 请求已是摘要


def test_pre_delivery_probe_carries_frozen_records(monkeypatch):
    """生成期支持探针必须携带统一冻结记录与内容来源（三项遗留修复方案 §一）。

    否则探针章节读不到该章记录，数字主张一律判"无证据"，无表格章节的模型正文会被
    误换回确定性稿（表格章此前靠"双双失败保留模型正文"掩盖了这一点）。
    """
    captured: dict = {}

    def fake(project_id, section, content, citation_ids, citation_map, source_texts, valid_sources,
             required_disclosures):
        # 探针章节的构造在 `_assess_candidate_support` 里，它把 section 交给
        # `_delivery_assessment`；桩在这一层才能看到探针真正传出去的内容。
        captured.update(section)
        return {"passed": True, "reasons": [], "unsupported_claims": 0,
                "needs_review_claims": 0, "invalid_citations": 0, "unsupported_claim_items": []}

    monkeypatch.setattr(workflow, "_delivery_assessment", fake)
    from backend.app import p0 as p0_module

    monkeypatch.setattr(p0_module, "_support_context", lambda project_id: ({}, {}, set()))
    context = {
        "evidence": [{"source_id": "source-1"}],
        "evidence_records": [{"record_id": "metric:debt_ratio:2025"}],
        "conflict_block": "", "system_table_block": "", "source_attribution_block": "",
    }
    ok = workflow._candidate_passes_delivery_for(
        "p-probe", "debt", "内容", [], context, content_origin="deterministic")
    assert ok is True
    assert "metric:debt_ratio" in captured["evidence_records_json"]
    assert captured["content_origin"] == "deterministic"
    assert captured["section_key"] == "debt"


# -------------------------------------- 台账：三段正文分列、互不冒充（§四 line 40） --

def test_ledger_records_the_real_repair_response_not_the_rejected_draft(monkeypatch):
    """安全修复失败的台账必须存**真实修复响应**，不能拿被拒稿冒充。

    旧实现里 `repair_chapter_safety` 在修复稿仍被拒时返回 ``original_result``，
    调用方随即把 `repair_content` 记成那份**被拒稿** → 台账无法还原"模型到底修出了什么"。
    本用例让修复调用返回一段可识别的正文并让它继续被拒，检查三段落库内容互不相同：
    ``draft_json`` = 原始首稿、``repair_json`` = 真实修复响应、``assembled_json`` = 组装后正文。
    """
    context = _context()
    draft_marker = "首稿正文-DRAFT-MARK"
    repair_marker = "修复稿正文-REAL-REPAIR-MARK"
    calls: list[str] = []

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        if safety_repair is not None:
            calls.append("repair")
            return _section(repair_marker, chapter_context), LlmUsage()
        calls.append("rewrite")
        return _section(draft_marker, chapter_context), LlmUsage()

    def fake_validate(content, citation_ids, ctx):
        # 首稿与修复稿都被拒 → 走"已尝试一次修复仍未通过"的确定性回退分支
        return {"status": "rejected", "reasons": [INITIAL_REASON],
                "length": {"status": "ok"}, "length_only_failure": False}

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    monkeypatch.setattr(workflow, "validate_chapter_output", fake_validate)
    try:
        outcome = _attempt(context)
        assert calls == ["rewrite", "repair"]
        assert outcome["generation_status"] == "fallback"
        assert outcome["fallback_stage"] == "chapter_validation"

        attempt = row(
            "SELECT draft_json,repair_json,assembled_json,code_fingerprint,frozen_input_json "
            "FROM chapter_generation_attempts WHERE project_id=? AND section_key=? "
            "ORDER BY rowid DESC LIMIT 1", (PROJECT_ID, CHAPTER))
        assert json.loads(attempt["draft_json"])["content"] == draft_marker
        # 关键断言：修复稿是真实响应，不是被拒的首稿
        assert json.loads(attempt["repair_json"])["content"] == repair_marker
        assert json.loads(attempt["repair_json"])["content"] != draft_marker
        # 组装后正文 = 本次真正采用（回退）的那一版
        assert json.loads(attempt["assembled_json"])["content"] == outcome["content"]
        assert attempt["code_fingerprint"]
    finally:
        _cleanup_attempts()


def test_ledger_freezes_the_complete_model_input(monkeypatch):
    """台账的 ``frozen_input_json.payload`` 保存**完整冻结输入**（原文/引文/系统块/允许引用）。

    只存哈希与规模无法回答"这条数字当时到底在不在输入里"。摘要字段保持原样（旧消费方读它们）。
    """
    context = _context()

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    try:
        outcome = _attempt(context)
        assert outcome["generation_status"] == "model_passed"
        attempt = row("SELECT frozen_input_json FROM chapter_generation_attempts "
                      "WHERE project_id=? AND section_key=? ORDER BY rowid DESC LIMIT 1",
                      (PROJECT_ID, CHAPTER))
        frozen = json.loads(attempt["frozen_input_json"])
        # 摘要字段保留
        assert frozen["prompt_sha256"] and frozen["input_hash"]
        assert frozen["allowed_citation_count"] == len(context.get("allowed_citation_ids") or [])
        # 完整输入
        payload = frozen["payload"]
        assert payload["prompt_text"] == context.get("prompt_text")
        assert payload["allowed_citation_ids"] == list(context.get("allowed_citation_ids") or [])
        assert len(payload["evidence"]) == len(context.get("evidence") or [])
        for item in payload["evidence"]:
            assert set(item) == {"id", "source_id", "document_kind", "page", "quote"}
        assert "system_table_block" in payload
    finally:
        _cleanup_attempts()


# ------------------------------ 支持检查并入同一候选评估（§四 line 33） --

def _support(passed: bool, unsupported: int = 0) -> dict:
    return {
        "passed": passed,
        "reasons": [] if passed else [f"模型正文未通过前置证据支持检查（{unsupported} 条主张无证据支持）"],
        "unsupported_claims": unsupported, "needs_review_claims": 0, "invalid_citations": 0,
        "unsupported_claim_items": [] if passed else [
            {"claim_index": 7, "claim_text": "系统计算的周转率为11.75次。",
             "reason_code": "missing_record", "matched_record_ids": [],
             "unsupported_numbers": ["11.75"]}],
    }


def _stub_support(monkeypatch, sequence: list[dict]) -> list[int]:
    """按调用次序返回支持评估结果；返回一个"调用次数"计数器便于断言。"""
    seen: list[int] = []

    def fake(project_id, section_key, content, citation_ids, chapter_context, *, content_origin="model"):
        seen.append(1)
        index = min(len(seen) - 1, len(sequence) - 1)
        return sequence[index]

    monkeypatch.setattr(workflow, "_assess_candidate_support", fake)
    return seen


def test_support_failure_consumes_the_shared_repair_opportunity(monkeypatch):
    """支持未过与章节被拒**共用**那一次修复：不是"章节通过、再直接回退"。

    首轮支持未过 → 触发一次安全修复 → 修复稿支持通过 → 采用模型正文。
    模型调用总次数为 2（首稿 + 修复），且修复输入里点名了"哪一句没支持"。
    """
    context = _context()
    calls: list[str] = []
    seen_details: list[dict] = []
    seen = _stub_support(monkeypatch, [_support(False, 1), _support(True)])

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        if safety_repair is not None:
            calls.append("repair")
            seen_details.append(safety_repair)
            return _section(chapter_context["draft"], chapter_context), LlmUsage()
        calls.append("rewrite")
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    try:
        outcome = _attempt(context)
        assert calls == ["rewrite", "repair"], "支持问题必须用同一次修复机会"
        assert outcome["generation_status"] == "model_passed"
        assert outcome["validation"]["status"] == "passed"
        assert outcome["repair_attempted"] is True
        assert len(seen) == 2, "首轮 + 修复后各评估一次"
        # 修复输入里带上了无支持主张的明细（否则模型只能盲改）
        assert seen_details and seen_details[0].get("support_failed") is True
        assert seen_details[0].get("support_detail_items")[0]["claim_index"] == 7
        # 本跑首拒支持明细随台账冻结：验收报告可直接点名主张与原因码。
        attempt = row("SELECT diagnostics_json FROM chapter_generation_attempts "
                      "WHERE project_id=? AND section_key=? ORDER BY rowid DESC LIMIT 1",
                      (PROJECT_ID, CHAPTER))
        diagnostics = json.loads(attempt["diagnostics_json"])
        assert (diagnostics.get("first_rejection_support_items") or [{}])[0].get("claim_index") == 7
    finally:
        _cleanup_attempts()


def test_support_failure_after_repair_still_falls_back(monkeypatch):
    """负例：修复稿支持仍不过 → 照样回退确定性正文（并入评估不是放宽）。"""
    context = _context()
    calls: list[str] = []
    _stub_support(monkeypatch, [_support(False, 2), _support(False, 2)])

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        calls.append("repair" if safety_repair is not None else "rewrite")
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    try:
        outcome = _attempt(context)
        assert calls == ["rewrite", "repair"]        # 仍只有一次修复，不追加第二次
        assert outcome["generation_status"] == "fallback"
        assert outcome["fallback_stage"] == "chapter_validation"
        assert "前置证据支持" in outcome["fallback_reason"]
        assert "已尝试一次修复仍未通过" in outcome["fallback_reason"]
        attempt = row("SELECT diagnostics_json FROM chapter_generation_attempts "
                      "WHERE project_id=? AND section_key=? ORDER BY rowid DESC LIMIT 1",
                      (PROJECT_ID, CHAPTER))
        diagnostics = json.loads(attempt["diagnostics_json"])
        assert diagnostics.get("support_failed") is True
        assert diagnostics.get("support_unsupported_claims") == 2
        # 逐条明细随诊断冻结（收口方案 §五"逐条证明"）：主张位置/原因码可直接核对。
        items = diagnostics.get("support_failed_items") or []
        assert items and items[0]["claim_index"] == 7
        assert items[0]["reason_code"] == "missing_record"
    finally:
        _cleanup_attempts()


def test_passing_support_does_not_add_a_repair_call(monkeypatch):
    """负例：支持本来就通过时不多花一次调用——并入评估只作用于"会失败"的候选。"""
    context = _context()
    calls: list[str] = []
    seen = _stub_support(monkeypatch, [_support(True)])

    def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                     chapter_context=None, strict_citations=False,
                     length_repair=None, safety_repair=None, drop_report=None):
        calls.append("rewrite")
        return _section(chapter_context["draft"], chapter_context), LlmUsage()

    monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)
    try:
        outcome = _attempt(context)
        assert calls == ["rewrite"]
        assert outcome["repair_attempted"] is False
        assert outcome["generation_status"] == "model_passed"
        assert len(seen) == 1
    finally:
        _cleanup_attempts()


def test_support_failure_details_are_condensed_for_the_repair_prompt():
    """修复提示只带"无支持的主张句＋原因码/匹配记录/其中数字"，不把被拒稿整段回灌。"""
    validation = {
        "support_failed": True,
        "support_detail_items": [
            {"claim_index": 7, "claim_text": "系统计算的周转率为11.75次。",
             "reason_code": "missing_record", "matched_record_ids": [],
             "unsupported_numbers": ["11.75"]},
            {"claim_index": 9, "claim_text": "存货周转率为0.36次。",
             "reason_code": "explicit_mismatch", "matched_record_ids": ["metric:inventory_turnover:2025"],
             "unsupported_numbers": ["0.36"]},
        ],
    }
    details = llm._safety_repair_details(validation)
    assert len(details["unsupported_claims"]) == 2
    assert details["unsupported_claims"][0] == {
        "claim_index": 7, "claim_text": "系统计算的周转率为11.75次。",
        "reason_code": "missing_record", "matched_record_ids": [],
        "unsupported_numbers": ["11.75"],
    }
    # 原因码与匹配记录随明细下发（收口方案 §三）：修复模型要能区分"记录没建"与"科目对不上"。
    assert details["unsupported_claims"][1]["reason_code"] == "explicit_mismatch"
    assert details["unsupported_claims"][1]["matched_record_ids"] == ["metric:inventory_turnover:2025"]
    # 未声明支持失败时不产生该字段（不制造噪声）
    assert "unsupported_claims" not in llm._safety_repair_details({"support_failed": False})
