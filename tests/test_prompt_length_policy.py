"""由逐章 Prompt 控制的报告篇幅。

本文件守四条线：

1. 校验器与 Prompt 编译器读**同一份**当前生效配置（项目 > 全局 > 出厂 YAML）。
   此前校验器读出厂 YAML、编译器读用户配置，用户改段落数/篇幅会被按出厂值误拒。
2. 篇幅目标就是用户在该章 Prompt 里设置的区间，**没有第二套系统硬顶**。
3. 字数只作写作目标：偏短只告警，偏长最多压缩一次；压缩后仍偏长但事实与证据
   校验通过时保留正文——不回退、不阻断交付。
4. 只统计**分析正文**：系统表格、冲突块、来源边界块都不计入篇幅。

注意：本仓库测试直接使用真实数据库，所以全局行必须快照后恢复。
"""
from __future__ import annotations

import json

from backend.app.chapter_guard import (
    assess_chapter_length, build_chapter_context, count_report_chars, validate_chapter_output,
)
from backend.app.chapter_prompts import (
    compile_structured_prompt, effective_chapter_config, load_chapter_config, prompt_edit_state,
)
from backend.app.database import execute, row
from backend.app.main import startup

PROJECT_ID = "test-length-policy-project"
CHAPTER = "financial"
# rating 的确定性模板能通过自身输出契约（financial/cashflow/debt 目前不能，见下）
REPAIR_CHAPTER = "rating"
# 移除硬顶之前的系统上限，用来证明"超过它不再被判不合格"。
FORMER_HARD_CAP = 2600
CONFLICT_BLOCK = "### 冲突事实（以下断言必须并列保留）\n- 断言 a-1：100万元；来源：s-1"


def _save_project_override(editable: dict) -> None:
    execute(
        "INSERT OR REPLACE INTO report_prompt_overrides "
        "(id,project_id,chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (f"{PROJECT_ID}-{CHAPTER}", PROJECT_ID, CHAPTER,
         compile_structured_prompt(REPAIR_CHAPTER, editable), "2026-01-01T00:00:00+00:00",
         json.dumps(editable, ensure_ascii=False), "", "structured"),
    )


def _cleanup() -> None:
    execute("DELETE FROM report_prompt_overrides WHERE project_id=?", (PROJECT_ID,))


def test_effective_config_resolves_project_then_global_then_yaml():
    startup()
    saved_global = row("SELECT * FROM global_report_prompts WHERE chapter_key=?", (CHAPTER,))
    try:
        execute("DELETE FROM global_report_prompts WHERE chapter_key=?", (CHAPTER,))
        factory_narrative = load_chapter_config(CHAPTER)["narrative_contract"]

        system = effective_chapter_config(CHAPTER, PROJECT_ID)
        assert system["length_policy"]["source"] == "system"
        assert system["narrative_contract"]["preferred_max_chars"] == factory_narrative["preferred_max_chars"]

        global_narrative = dict(factory_narrative, preferred_max_chars=1200, preferred_min_chars=600)
        editable = prompt_edit_state(CHAPTER, PROJECT_ID)[0]
        editable["narrative_contract"].update(global_narrative)
        execute(
            "INSERT OR REPLACE INTO global_report_prompts "
            "(chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) VALUES (?,?,?,?,?,?)",
            (CHAPTER, compile_structured_prompt(REPAIR_CHAPTER, editable), "2026-01-01T00:00:00+00:00",
             json.dumps(editable, ensure_ascii=False), "", "structured"),
        )
        from_global = effective_chapter_config(CHAPTER, PROJECT_ID)
        assert from_global["length_policy"]["source"] == "global"
        assert from_global["narrative_contract"]["preferred_max_chars"] == 1200

        project_narrative = dict(factory_narrative, preferred_max_chars=900, preferred_min_chars=300)
        editable["narrative_contract"].update(project_narrative)
        _save_project_override(editable)
        from_project = effective_chapter_config(CHAPTER, PROJECT_ID)
        assert from_project["length_policy"]["source"] == "project"
        assert from_project["narrative_contract"]["preferred_max_chars"] == 900
    finally:
        _cleanup()
        execute("DELETE FROM global_report_prompts WHERE chapter_key=?", (CHAPTER,))
        if saved_global:
            execute(
                "INSERT OR REPLACE INTO global_report_prompts "
                "(chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) VALUES (?,?,?,?,?,?)",
                (CHAPTER, saved_global["prompt"], saved_global["updated_at"], saved_global["structured_json"],
                 saved_global["legacy_prompt"], saved_global["migration_mode"]),
            )


def test_output_contract_has_no_system_character_cap():
    """安全检查项里不再有字数上限——字数不参与安全判定。"""
    startup()
    for chapter_key in ("financial", "cashflow", "debt", "rating", "guarantee", "counter_guarantee"):
        contract = load_chapter_config(chapter_key)["output_contract"]
        assert "max_output_chars" not in contract, chapter_key
    # 安全项仍在。
    assert "forbidden_patterns" in load_chapter_config(CHAPTER)["output_contract"]


def test_effective_length_is_the_user_range_without_a_system_cap():
    """用户把上限设得比旧硬顶更高时，运行时用**用户的值**，不再被钳制。"""
    startup()
    try:
        editable = prompt_edit_state(CHAPTER, PROJECT_ID)[0]
        editable["narrative_contract"]["preferred_min_chars"] = 12000
        editable["narrative_contract"]["preferred_max_chars"] = 15000
        _save_project_override(editable)

        policy = effective_chapter_config(CHAPTER, PROJECT_ID)["length_policy"]
        assert policy["effective_min_chars"] == 12000
        assert policy["effective_max_chars"] == 15000   # 超过旧输入上限仍完整生效
        assert "hard_max_chars" not in policy
    finally:
        _cleanup()


def test_preview_and_validator_share_the_same_range():
    startup()
    try:
        editable = prompt_edit_state(CHAPTER, PROJECT_ID)[0]
        editable["narrative_contract"]["preferred_min_chars"] = 12000
        editable["narrative_contract"]["preferred_max_chars"] = 15000
        _save_project_override(editable)

        config = effective_chapter_config(CHAPTER, PROJECT_ID)
        policy = config["length_policy"]

        preview = compile_structured_prompt(CHAPTER, editable)
        assert f"正文篇幅目标为{policy['effective_min_chars']}至{policy['effective_max_chars']}字。" in preview
        # 校验器读到的必须是同一份篇幅区间（且无硬顶）。
        context = build_chapter_context(PROJECT_ID, CHAPTER, "据募集说明书披露，材料不足。")
        assert context["length_policy"]["effective_max_chars"] == policy["effective_max_chars"]
        assert "hard_max_chars" not in context["length_policy"]
    finally:
        _cleanup()


def test_prompt_schemas_accept_character_targets_above_previous_limits():
    from backend.app.chapter_prompts import NarrativeContract
    from backend.app.schemas import PromptNarrativeConfig

    assert NarrativeContract(preferred_min_chars=12000, preferred_max_chars=15000)
    assert PromptNarrativeConfig(preferred_min_chars=12000, preferred_max_chars=15000)


def test_input_hash_tracks_the_length_policy():
    startup()
    try:
        editable = prompt_edit_state(CHAPTER, PROJECT_ID)[0]
        editable["narrative_contract"]["preferred_max_chars"] = 1200
        _save_project_override(editable)
        before = build_chapter_context(PROJECT_ID, CHAPTER, "据募集说明书披露，材料不足。")["input_hash"]

        editable["narrative_contract"]["preferred_max_chars"] = 1800
        _save_project_override(editable)
        after = build_chapter_context(PROJECT_ID, CHAPTER, "据募集说明书披露，材料不足。")["input_hash"]

        assert before != after
    finally:
        _cleanup()


# ---------------------------------------------------------------- 篇幅口径 --

def _context_with_policy(min_chars: int = 1000, max_chars: int = 2600) -> dict:
    return {
        "conflict_block": CONFLICT_BLOCK,
        "length_policy": {
            "effective_min_chars": min_chars, "effective_max_chars": max_chars,
            "unit": "visible_chars",
        },
    }


def test_count_report_chars_ignores_markers_whitespace_and_conflict_block():
    content = "## 标题\n\n正文一百字。\n\n- 第一项\n- 第二项\n\n> 引用一句\n\n**重点**。"
    # 标题(2) 正文一百字。(6) 第一项(3) 第二项(3) 引用一句(4) 重点。(3)
    assert count_report_chars(content) == 21
    # 确定性冲突块是系统生成的，且被要求逐字保留——不计入篇幅。
    with_conflict = f"{CONFLICT_BLOCK}\n\n{content}"
    assert count_report_chars(with_conflict, CONFLICT_BLOCK) == 21


def test_long_system_table_is_not_counted_as_body():
    """系统表格由后端附入成品，模型不必复述，因此也不该把章节顶到篇幅上限。"""
    table = "### 表 1：发行人主要财务数据\n\n| 科目 | 2025年 |\n| --- | --- |\n| " + "资产总额 | 72,000万元 |\n| " * 200
    body = "据募集说明书披露，材料不足。"
    context = _context_with_policy(min_chars=0, max_chars=200)
    context["required_disclosures"] = [table]

    plain = assess_chapter_length(body, context)
    assert plain["status"] == "passed"
    assert plain["visible_content_chars"] == len(body)

    with_table = assess_chapter_length(f"{table}\n\n{body}", context)
    assert with_table["status"] == "passed"
    assert with_table["visible_content_chars"] == len(body)
    # 全文字符数仅用于审计：它包含表格，所以明显更大。
    assert with_table["raw_output_chars"] > with_table["visible_content_chars"]


def test_assess_chapter_length_boundaries():
    context = _context_with_policy()   # 目标区间 1000~2600
    for size, expected in [(999, "short"), (1000, "passed"), (1800, "passed"), (2600, "passed")]:
        result = assess_chapter_length("字" * size, context)
        assert result["status"] == expected, (size, result)
        assert result["visible_content_chars"] == size

    # 用户把上限设得更紧：可见正文超出自设上限即 long。
    tighter = _context_with_policy(max_chars=1200)
    assert assess_chapter_length("字" * 1201, tighter)["status"] == "long"

    # 超出旧硬顶也只是 long——没有 hard_limit_exceeded 这个状态了。
    beyond = assess_chapter_length("字" * (FORMER_HARD_CAP + 400), context)
    assert beyond["status"] == "long"
    assert beyond["raw_output_chars"] == FORMER_HARD_CAP + 400


def test_length_never_makes_a_safe_chapter_fail():
    """正文超过旧硬顶，但只要事实、引用与结论边界都干净，就不再被判不合格。"""
    safe = {"allowed_citation_ids": [], "evidence": [], "prompt_config": load_chapter_config(CHAPTER),
            "conflict_block": "", "length_policy": _context_with_policy()["length_policy"]}

    long_output = validate_chapter_output("字" * (FORMER_HARD_CAP + 400), [], safe)
    assert long_output["status"] == "passed"
    assert long_output["reasons"] == []
    assert long_output["length"]["status"] == "long"
    assert long_output["length_only_failure"] is True

    short = validate_chapter_output("字" * 500, [], safe)
    assert short["length"]["status"] == "short"
    assert short["length_only_failure"] is False

    # 安全校验失败时不允许走篇幅修复
    unsafe = dict(safe, allowed_citation_ids=["c-1"])
    unsafe["evidence"] = [{"id": "c-1", "document_kind": "募集说明书", "quote": "x"}]
    rejected = validate_chapter_output("字" * 3000, ["c-9"], unsafe)
    assert rejected["status"] == "rejected"
    assert rejected["length_only_failure"] is False


def test_length_repair_does_not_launder_safety_failures():
    """压缩不能换来安全豁免：压缩稿引入无证据数字时仍被拒绝。"""
    context = {"allowed_citation_ids": [], "evidence": [],
               "prompt_config": load_chapter_config(CHAPTER), "conflict_block": "",
               "draft": "据募集说明书披露，发行规模为20000万元；材料不足。",
               "length_policy": _context_with_policy()["length_policy"]}
    original = validate_chapter_output("字" * 3000, [], context)
    assert original["status"] == "passed" and original["length_only_failure"] is True

    repaired = validate_chapter_output("据募集说明书披露，发行规模为30000万元；材料不足。", [], context)
    assert repaired["status"] == "rejected"                 # 30000 不在草稿与证据里
    assert any("新数字" in reason for reason in repaired["reasons"])
    assert repaired["length_only_failure"] is False


# -------------------------------------------------- 单章重生成的一次压缩路径 --

def _regenerate_with_fake_model(monkeypatch, reply) -> dict:
    """跑一次单章重生成，正文由 ``reply`` 决定。

    返回 ``{"before": 重生成前正文, "stored": 落库行, "calls": 调用记录}``。
    """
    import time
    from fastapi.testclient import TestClient
    from backend.app import main, workflow
    from backend.app.llm import LlmUsage
    from backend.app.main import app
    from backend.app.schemas import LlmSection

    startup()
    with TestClient(app) as client:
        run_id = workflow.start_run("demo-bond-huachen")
        # 截止时间而非固定轮询次数：本机冷启动（jieba/检索模型首载）时演示运行可达 40–90 秒，
        # 原先 1500×0.02s≈30s 的预算会把"还没跑完"误判成失败。
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = row("SELECT status FROM runs WHERE id=?", (run_id,))
            if current and current["status"] in workflow.TERMINAL:
                break
            time.sleep(0.05)
        assert current["status"] == "completed"
        before = row("SELECT * FROM report_sections WHERE project_id='demo-bond-huachen' AND section_key=?",
                     (REPAIR_CHAPTER,))
        assert before and before["content"]
        # Keep a marker in the rendered body only. Regeneration must rebuild from
        # the current deterministic chapter and never feed this stale body back.
        execute("UPDATE report_sections SET content=? WHERE project_id='demo-bond-huachen' AND section_key=?",
                ("旧模型正文 OLD_MODEL_ONLY 987654321。", REPAIR_CHAPTER))

        calls: list[dict] = []

        def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                         chapter_context=None, strict_citations=False, length_repair=None,
                         safety_repair=None, drop_report=None):
            calls.append({
                "chapter_key": chapter_key, "length_repair": length_repair,
                "content_input": content,
                "input_hash": (chapter_context or {}).get("input_hash"),
                "allowed": list((chapter_context or {}).get("allowed_citation_ids") or []),
            })
            return (LlmSection(content=reply(content, length_repair),
                               citation_ids=list((chapter_context or {}).get("allowed_citation_ids") or [])),
                    LlmUsage())

        saved_override = row(
            "SELECT * FROM report_prompt_overrides WHERE project_id='demo-bond-huachen' AND chapter_key=?",
            (REPAIR_CHAPTER,),
        )
        try:
            editable = prompt_edit_state(REPAIR_CHAPTER, "demo-bond-huachen")[0]
            editable["narrative_contract"]["preferred_min_chars"] = 0
            editable["narrative_contract"]["preferred_max_chars"] = 80   # 远小于模板草稿
            execute(
                "INSERT OR REPLACE INTO report_prompt_overrides "
                "(id,project_id,chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("length-e2e", "demo-bond-huachen", REPAIR_CHAPTER, compile_structured_prompt(REPAIR_CHAPTER, editable),
                 "2026-01-01T00:00:00+00:00", json.dumps(editable, ensure_ascii=False), "", "structured"),
            )
            # 首次调用在 main 的命名空间里；篇幅压缩走 workflow.repair_chapter_length，
            # 落在 workflow 的命名空间里——两处都要打桩，否则压缩那次会打到真实实现。
            monkeypatch.setattr(main, "rewrite_section", fake_rewrite)
            monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)

            response = client.post(
                f"/api/projects/demo-bond-huachen/report/sections/{REPAIR_CHAPTER}/regenerate",
            )
            assert response.status_code == 200
            stored = row("SELECT * FROM report_sections WHERE project_id='demo-bond-huachen' AND section_key=?",
                         (REPAIR_CHAPTER,))
            return {"before": before["content"], "stored": stored, "calls": calls}
        finally:
            execute("DELETE FROM report_prompt_overrides WHERE id=?", ("length-e2e",))
            if saved_override:
                execute(
                    "INSERT OR REPLACE INTO report_prompt_overrides "
                    "(id,project_id,chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (saved_override["id"], "demo-bond-huachen", REPAIR_CHAPTER, saved_override["prompt"],
                     saved_override["updated_at"], saved_override["structured_json"],
                     saved_override["legacy_prompt"], saved_override["migration_mode"]),
                )


def test_regenerate_compresses_once_and_keeps_the_long_body(monkeypatch):
    """单章重生成：超上限 → 一次压缩（同一上下文）→ 仍偏长也保留，不回退。

    这条端点此前没有测试覆盖；同时守住"两次调用复用同一章节上下文（同一 input_hash）"。
    """
    run = _regenerate_with_fake_model(monkeypatch, lambda content, repair: content)
    stored, calls = run["stored"], run["calls"]

    assert "OLD_MODEL_ONLY" not in calls[0]["content_input"]

    assert len(calls) == 2                        # 首次 + 一次压缩，不重复
    assert calls[0]["length_repair"] is None
    repair = calls[1]["length_repair"]
    assert repair["effective_max_chars"] == 80
    assert repair["visible_content_chars"] > 80
    assert calls[0]["input_hash"] == calls[1]["input_hash"]     # 同一份上下文
    assert calls[0]["allowed"] == calls[1]["allowed"]           # 同一引用白名单

    # 压缩稿安全通过 → 采用它，即便仍然偏长；不因篇幅回退或阻断。
    assert stored["generation_status"] == "model_passed"
    assert stored["fallback_reason"] == ""
    assert stored["length_repair_attempted"] == 1
    assert stored["effective_max_chars"] == 80
    assert stored["length_status"] == "long"
    assert stored["support_validation_status"] == "passed"


def test_regenerate_keeps_the_original_body_when_compression_breaks_a_fact(monkeypatch):
    """压缩稿引入事实错误时，保留压缩前的合格正文，状态不降级。"""

    def reply(content: str, repair: dict | None) -> str:
        # 首次返回模板正文（合格但偏长）；压缩稿凭空多出一个草稿与证据里都没有的
        # 数字——典型的事实错误，必须被拒绝，但不能连累原正文。
        return "据募集说明书披露，担保费率为 9876.54 万元。" if repair else content

    run = _regenerate_with_fake_model(monkeypatch, reply)
    stored, calls = run["stored"], run["calls"]

    assert len(calls) == 2
    assert "OLD_MODEL_ONLY" not in stored["content"]
    assert "9876.54" not in stored["content"]                # 压缩稿的新数字未污染原稿
    assert stored["generation_status"] == "model_passed"
    assert stored["fallback_reason"] == ""
    assert stored["length_repair_attempted"] == 1
    assert stored["length_status"] == "long"


def test_full_generation_keeps_the_long_model_body(monkeypatch):
    """完整生成与单章再生成口径一致：超上限压缩一次，仍偏长也保留模型正文。

    用 rating 而不是 financial：financial/cashflow/debt 的确定性模板目前不满足自身
    输出契约（见文件顶部注释），那几章在基线上本来就会回退，测不出篇幅这一层。
    """
    from backend.app import workflow
    from backend.app.llm import LlmUsage
    from backend.app.schemas import LlmSection

    startup()
    project_id = "demo-bond-huachen"
    saved_override = row(
        "SELECT * FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?", (project_id, REPAIR_CHAPTER))
    try:
        editable = prompt_edit_state(REPAIR_CHAPTER, project_id)[0]
        editable["narrative_contract"]["preferred_min_chars"] = 0
        editable["narrative_contract"]["preferred_max_chars"] = 80   # 远小于模板草稿
        execute(
            "INSERT OR REPLACE INTO report_prompt_overrides "
            "(id,project_id,chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("length-e2e-full", project_id, REPAIR_CHAPTER, compile_structured_prompt(REPAIR_CHAPTER, editable),
             "2026-01-01T00:00:00+00:00", json.dumps(editable, ensure_ascii=False), "", "structured"),
        )
        calls: list[tuple[str, bool]] = []

        def fake_rewrite(title, content, evidence, call_context=None, *, chapter_key=None,
                         chapter_context=None, strict_citations=False, length_repair=None,
                         safety_repair=None, drop_report=None):
            calls.append((chapter_key, length_repair is not None))
            return (LlmSection(content=content,
                               citation_ids=list((chapter_context or {}).get("allowed_citation_ids") or [])),
                    LlmUsage())

        monkeypatch.setattr(workflow, "is_configured", lambda: True)
        # 抽取节点同样会走模型分支，这里只验证草稿改写，让确定性抽取供事实。
        monkeypatch.setattr(workflow, "extract_facts", lambda *_args, **_kwargs: ([], LlmUsage()))
        monkeypatch.setattr(workflow, "rewrite_section", fake_rewrite)

        import time
        run_id = workflow.start_run(project_id)
        for _ in range(9000):  # ≈180s：本机冷启动演示运行可达 40–90s，30s 会把"未跑完"误判为失败
            current = row("SELECT status FROM runs WHERE id=?", (run_id,))
            if current and current["status"] in workflow.TERMINAL:
                break
            time.sleep(0.02)
        assert current["status"] == "completed", row("SELECT error FROM runs WHERE id=?", (run_id,))["error"]

        chapter_calls = [item for item in calls if item[0] == REPAIR_CHAPTER]
        assert chapter_calls == [(REPAIR_CHAPTER, False), (REPAIR_CHAPTER, True)]   # 首次 + 一次压缩

        stored = row("SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, REPAIR_CHAPTER))
        assert stored["generation_status"] == "model_passed"     # 没有因篇幅回退
        assert stored["content_origin"] == "model"
        assert stored["fallback_reason"] == ""
        assert stored["length_repair_attempted"] == 1
        assert stored["effective_max_chars"] == 80
        assert stored["length_status"] == "long"
    finally:
        execute("DELETE FROM report_prompt_overrides WHERE id=?", ("length-e2e-full",))
        if saved_override:
            execute(
                "INSERT OR REPLACE INTO report_prompt_overrides "
                "(id,project_id,chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (saved_override["id"], project_id, REPAIR_CHAPTER, saved_override["prompt"],
                 saved_override["updated_at"], saved_override["structured_json"],
                 saved_override["legacy_prompt"], saved_override["migration_mode"]),
            )


# ------------------------------------------------------------ 输出额度 --

def test_report_task_gets_its_own_output_quota():
    """报告章节用独立额度，不受 LLM_MAX_OUTPUT_TOKENS 钳制；其他任务维持原额度。"""
    from backend.app.config import settings
    from backend.app.llm import _output_limit

    assert _output_limit("report", None) == settings.llm_report_max_output_tokens == 10000
    # 抽取等其他任务仍然受通用上限钳制。
    for task_type, expected in (("extract", 5000), ("candidate", 2500), ("baseline", 5000), ("tranche", 2000)):
        assert _output_limit(task_type, None) == expected, task_type
    assert _output_limit("section_locator", None) == settings.llm_max_output_tokens
    # 显式传入的额度仍然优先。
    assert _output_limit("report", 512) == 512


def test_output_limit_rejection_is_reported_clearly():
    """模型服务不接受 10000 的输出额度时，要留下能直接定位的错误而不是含糊的 400。"""
    import httpx
    from backend.app.llm import StructuredLlmError, raise_for_output_limit

    def response(body: str) -> httpx.Response:
        return httpx.Response(400, text=body, request=httpx.Request("POST", "https://example.invalid"))

    error = None
    try:
        raise_for_output_limit(response('{"error":{"message":"max_tokens is too large: 10000"}}'), 10000)
    except StructuredLlmError as exc:
        error = exc
    assert error is not None and error.error_type == "output_limit_rejected"
    assert "10000" in str(error)

    # 与输出额度无关的 400 原样放行，交给调用方既有的降级重试。
    raise_for_output_limit(response('{"error":{"message":"Unrecognized request argument: thinking"}}'), 10000)
    raise_for_output_limit(response('{"error":{"message":"invalid api key"}}'), 10000)
