"""报告章节正文的唯一写入入口与交付一致性判定。

缺陷背景（2026-09-19 基线运行 `86164ec2`）：`workflow._fallback_unsupported_sections()`
把模型正文覆盖成确定性模板时只更新 `content/citation_ids/validation_status/support_rate`，
于是出现三处交付不一致：

1. `generation_status` 仍是 `model_passed`、`visible_content_chars` 仍是被丢弃那份正文的
   长度 → 界面显示"模型润色已应用"，报告正文却是模板；
2. 覆盖用的裸模板不含冲突块与来源边界块（它们由 `build_chapter_context` 在草稿阶段附加）
   → 报告丢失必须并列披露的冲突事实，而支持率仍是 100%；
3. 替换后重跑支持校验，模板天然"有证据支持" → 指标自证循环，回退在数据上不可见。

本模块提供唯一写入入口：任何正文变更都在同一事务内同步来源、状态、诊断、篇幅、
哈希与强制披露，并由 `assess_delivery()` 给出与正文哈希绑定的交付判定。
"""
from __future__ import annotations

import uuid
from typing import Any

from .chapter_guard import SOURCE_ATTRIBUTION_BLOCK, assess_chapter_length, count_report_chars
from .database import connect, json_dump, json_load, now_iso, row, rows

CONTENT_ORIGINS = ("model", "deterministic", "human", "unknown")
FALLBACK_STAGES = ("chapter_validation", "report_support", "none")
DISCLOSURE_STATUSES = ("pending", "passed", "warning", "not_checked")
DELIVERY_STATUSES = ("pending", "eligible", "needs_review", "not_checked")


def content_sha256(content: str) -> str:
    import hashlib
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 章节生成尝试台账（第三点细化方案 §四）
#
# `chapter_generation_attempts` 保存每次"生成 → 校验 → 至多一次修复"的完整现场：冻结输入、
# 首稿与修复稿、分阶段诊断、是否采用与调用用量。`report_sections.accepted_attempt_id` 记录
# 现正文来自哪次成功，`latest_attempt_id` 记录最后一次尝试（可能失败/待核验/跳过），据此
# 才能区分"正文可用"与"最近一次尝试失败"。
# ---------------------------------------------------------------------------

ATTEMPT_STATUSES = ("passed", "needs_review", "failed", "skipped")
ATTEMPT_REPAIR_TYPES = ("none", "safety", "length", "json")


def record_chapter_attempt(*, project_id: str, section_key: str, status: str, stage: str = "",
                           repair_type: str = "none", adopted: bool = False,
                           usage: Any = None, diagnostics: dict | None = None,
                           draft_content: str = "", repair_content: str = "",
                           assembled_content: str = "", code_fingerprint: str = "",
                           frozen_input: dict | None = None, expected: dict | None = None,
                           run_id: str = "", attempt_id: str | None = None) -> str:
    """写入一次章节生成尝试，返回尝试 ID。

    只负责落库现场，不触碰 `report_sections`（是否采用由 `set_section_attempt` 决定，且必须
    在正文真正写库之后）；原文（首稿/修复稿）仅供诊断，不进入报告成品。

    三段正文**分列保存、互不冒充**（财务修复计划 §四 line 40）：

    - ``draft_json``   = 模型**原始首稿**（修复前）；
    - ``repair_json``  = **真实修复响应**——修复调用实际返回的那一版。修复失败时**不得**
      把返回的原被拒稿写进来充当修复稿（旧实现正是如此，台账因此无法还原真实修复行为）；
    - ``assembled_json`` = **组装后正文**：补回来源边界块、拼上系统表格、真正落进
      ``report_sections`` 的那一版。
    """
    attempt_id = attempt_id or str(uuid.uuid4())
    expected = expected or {}
    usage_payload: dict = {}
    if usage is not None:
        usage_payload = usage if isinstance(usage, dict) else dict(getattr(usage, "__dict__", {}) or {})
    with connect() as conn:
        conn.execute(
            "INSERT INTO chapter_generation_attempts (id,project_id,section_key,run_id,"
            "expected_revision,expected_content_sha256,expected_prompt_sha256,expected_materials_version,"
            "frozen_input_json,draft_json,repair_json,assembled_json,code_fingerprint,"
            "diagnostics_json,status,stage,repair_type,"
            "adopted,usage_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt_id, project_id, section_key, run_id,
                int(expected.get("expected_revision") or 0),
                str(expected.get("expected_content_sha256") or ""),
                str(expected.get("expected_prompt_sha256") or ""),
                str(expected.get("expected_materials_version") or ""),
                json_dump(frozen_input or {}),
                json_dump({"content": draft_content or ""}),
                json_dump({"content": repair_content or ""}),
                json_dump({"content": assembled_content or ""}),
                str(code_fingerprint or ""),
                json_dump(diagnostics or {}),
                str(status or "skipped"), str(stage or ""), str(repair_type or "none"),
                1 if adopted else 0,
                json_dump(usage_payload), now_iso(),
            ),
        )
    return attempt_id


def set_section_attempt(project_id: str, section_key: str, attempt_id: str, *, adopted: bool = False) -> None:
    """更新章节的最新尝试指针；`adopted=True` 时同时把现正文指向这次尝试。

    失败/待核验/跳过一律只动 `latest_attempt_id`，`accepted_attempt_id` 保持上一次成功值，
    从而"单章再生失败不覆盖旧正文语义"在台账层面也成立。调用方是**采用与否的唯一权威**：
    写库成功后才以 `adopted=True` 调用；正文最终未落库（例如后置支持失败）时以
    `adopted=False` 调用，把尝试行上可能已被乐观写入的 adopted 标记纠正回来。
    """
    with connect() as conn:
        conn.execute(
            "UPDATE report_sections SET latest_attempt_id=? WHERE project_id=? AND section_key=?",
            (attempt_id, project_id, section_key),
        )
        if adopted:
            conn.execute(
                "UPDATE report_sections SET accepted_attempt_id=? WHERE project_id=? AND section_key=?",
                (attempt_id, project_id, section_key),
            )
        conn.execute(
            "UPDATE chapter_generation_attempts SET adopted=? WHERE id=?",
            (1 if adopted else 0, attempt_id),
        )


def _attempt_needs_review_count(diagnostics: dict) -> int:
    items = diagnostics.get("needs_review_items")
    if isinstance(items, list):
        return len(items)
    try:
        return int(diagnostics.get("needs_review_count") or 0)
    except (TypeError, ValueError):
        return 0


def latest_attempt(project_id: str, section_key: str) -> dict:
    """最新一次章节尝试的对外摘要；无记录时返回空占位（兼容历史章节）。"""
    item = row(
        "SELECT * FROM chapter_generation_attempts WHERE project_id=? AND section_key=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (project_id, section_key),
    )
    if not item:
        return {
            "status": "", "stage": "", "repair_type": "none", "adopted": False,
            "needs_review_count": 0, "diagnostics": {}, "attempt_id": "", "created_at": "",
        }
    diagnostics = json_load(item.get("diagnostics_json") or "{}") or {}
    return {
        "attempt_id": item["id"],
        "status": item.get("status") or "",
        "stage": item.get("stage") or "",
        "repair_type": item.get("repair_type") or "none",
        "adopted": bool(item.get("adopted")),
        "needs_review_count": _attempt_needs_review_count(diagnostics),
        "diagnostics": diagnostics,
        "created_at": item.get("created_at") or "",
    }


def disclosures_complete(content: str, required_disclosures: list[str] | None) -> bool:
    """强制披露（冲突块、来源边界块）是否逐字保留。与事实支持率分开计算。"""
    return all(block in (content or "") for block in (required_disclosures or []) if block)


def assess_length(content: str, length_policy: dict | None,
                  required_disclosures: list[str] | None) -> dict:
    """复用校验器的篇幅判定，并把 `status` 归一为落库字段名 `length_status`。"""
    assessment = assess_chapter_length(content or "", {
        "length_policy": length_policy or {},
        "required_disclosures": required_disclosures or [],
    })
    return {**assessment, "length_status": assessment["status"]}


def assess_delivery(*, content: str, required_disclosures: list[str] | None,
                    support_status: str, length_status: str,
                    validated_content_sha256: str = "") -> tuple[str, str, str]:
    """返回 (disclosure_status, delivery_status, validated_content_sha256)。

    - 强制披露缺失 → `warning`，且无论支持率多高都不可交付；
    - 只有支持通过、披露完整、且校验哈希与当前正文一致时才算 `eligible`；
    - 篇幅偏短或偏长只作诊断：篇幅是写作目标，不改变交付状态，也不阻断最终导出。
      ``hard_limit_exceeded`` 是历史库里的旧状态，仍按偏长诊断展示。
    """
    digest = content_sha256(content)
    disclosure = "passed" if disclosures_complete(content, required_disclosures) else "warning"
    if disclosure != "passed":
        delivery = "needs_review"
    elif support_status == "warning":
        delivery = "needs_review"
    elif support_status != "passed":
        delivery = "pending"          # 支持校验尚未完成，不能宣称可交付
    else:
        delivery = "eligible"
    validated = digest if delivery == "eligible" else validated_content_sha256
    return disclosure, delivery, validated


def content_diagnosis_mismatch(section: dict) -> bool:
    """正文与诊断是否互相矛盾。

    直接信号：`raw_output_chars` 与实际正文长度不符（2026-09-19 基线里 financial
    正文 493 字、诊断 1196 字，就是后置回退只改正文没改诊断造成的）；或正文哈希
    与最近校验哈希不一致。
    """
    content = section.get("content") or ""
    raw = section.get("raw_output_chars") or 0
    if raw and raw != len(content):
        return True
    digest = section.get("content_sha256") or ""
    validated = section.get("validated_content_sha256") or ""
    return bool(digest and validated and digest != validated)


def reconcile_historical_sections() -> int:
    """历史行诚实化：正文曾被替换但元数据没跟上的，纠正来源并标记后置回退。

    只处理"可确定"的情况（正文长度与诊断不符），无法确定的保持 `unknown/not_checked`，
    不推断为模型成功；不自动重新调用模型、不改变正文本身。
    """
    from .chapter_prompts import chapter_uses_llm

    fixed = 0
    # 确定性章节被后置回退时曾被记成 generation_status='fallback'，会把采用率分母算大。
    # 这是可确定纠正的：该章从未调用模型，回退阶段照留。
    for item in rows("SELECT id,section_key,generation_status FROM report_sections "
                     "WHERE generation_status='fallback'"):
        if not chapter_uses_llm(item["section_key"]):
            with connect() as conn:
                conn.execute("UPDATE report_sections SET generation_status='deterministic' WHERE id=?",
                             (item["id"],))
            fixed += 1
    for item in rows("SELECT * FROM report_sections"):
        if not content_diagnosis_mismatch(item):
            continue
        content = item.get("content") or ""
        with connect() as conn:
            conn.execute(
                "UPDATE report_sections SET content_origin=?,fallback_stage=?,delivery_status=?,"
                "disclosure_status=CASE WHEN disclosure_status='' THEN 'not_checked' ELSE disclosure_status END,"
                "raw_output_chars=?,visible_content_chars=?,content_sha256=?,validated_content_sha256='' "
                "WHERE id=?",
                (item.get("content_origin") or "unknown",
                 "report_support" if item.get("fallback_stage") in {"", "none"} else item.get("fallback_stage"),
                 "needs_review", len(content),
                 count_report_chars(content, required_disclosures=_as_json_list(item.get("required_disclosures"))),
                 content_sha256(content), item["id"]),
            )
        fixed += 1
    return fixed


def _as_json_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            import json
            decoded = json.loads(value)
        except Exception:
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []
    return [str(item) for item in (value or [])]


def delivery_blockers(project_id: str) -> list[str]:
    """阻止「最终复核通过 / 最终导出」的原因清单；空列表表示可交付。

    覆盖方案 §三.3 的门槛：无证据主张、无效引用、强制披露缺失、正文与最近校验
    哈希不一致、报告过期。**支持率通过不能掩盖这些**——旧实现正是靠替换正文把
    支持率刷回 100%，从指标上看不出任何问题。
    """
    from .p0 import validate_report_support      # 延迟导入，避免 p0 → report_delivery 成环

    if not row("SELECT id FROM report_sections WHERE project_id=? LIMIT 1", (project_id,)):
        return ["报告尚未生成"]
    quality = validate_report_support(project_id)
    problems: list[str] = []
    if quality["unsupported_claims"]:
        problems.append(f"存在 {quality['unsupported_claims']} 条无证据声明")
    if quality["invalid_citations"]:
        problems.append(f"存在 {quality['invalid_citations']} 条无效引用")
    if quality.get("disclosure_missing_sections"):
        problems.append(f"{quality['disclosure_missing_sections']} 个章节缺少强制披露（冲突断言或来源边界块）")
    mismatch = row(
        "SELECT COUNT(*) count FROM report_sections WHERE project_id=? "
        "AND validated_content_sha256!='' AND content_sha256!=validated_content_sha256",
        (project_id,),
    )
    if mismatch and mismatch["count"]:
        problems.append(f"{mismatch['count']} 个章节的正文在最近一次校验之后被改动")
    stale = row("SELECT report_stale FROM projects WHERE id=?", (project_id,))
    if stale and stale["report_stale"]:
        problems.append("报告已过期：材料或事实发生变化，需重新生成")
    # 修订稿 §3.3：模型生成失败（含一次修复后仍失败）的章节须处理后才能最终交付。
    # 判据以 **fallback_reason** 为准：`p0.validate_report_support` 会回写兼容字段
    # `validation_status`（本函数第一步就会跑它），只靠 validation_status='failed' 会被冲掉。
    # 两类情况不阻断：
    #   - 「跳过」章节（未配置模型/证据不足/预算耗尽）fallback_reason 为空；
    #   - 确定性章节单章重生成的占位原因 NO_MODEL_REASON（明文"不是模型回退"）。
    from .chapter_guard import NO_MODEL_REASON

    failed = row(
        "SELECT COUNT(*) count FROM report_sections WHERE project_id=? AND ("
        "(COALESCE(fallback_reason,'') NOT IN ('', ?) AND generation_status!='skipped') "
        "OR validation_status='failed')",
        (project_id, NO_MODEL_REASON),
    )
    if failed and failed["count"]:
        problems.append(
            f"{failed['count']} 个章节的模型生成失败（含一次修复），须重新生成或人工处理后才能交付"
        )
    # §一 line 7 / §四 line 38：一次修复后**仅剩未决项**的章节保留模型待核验稿，但
    # **禁止最终交付**——必须人工处理（复核通过或重新生成）后才能出报告。
    # 不能只靠上面的 fallback_reason 判据：这类章节的 fallback_reason 是**空的**
    # （它不是回退，正文就是模型稿），漏判会让待核验稿随报告直接交付出去。
    pending_review = row(
        "SELECT COUNT(*) count FROM report_sections WHERE project_id=? "
        "AND generation_status='model_needs_review'",
        (project_id,),
    )
    if pending_review and pending_review["count"]:
        problems.append(
            f"{pending_review['count']} 个章节为模型待核验稿，须人工复核后方可最终交付"
        )
    # 修订稿 §二.2 末条：待核验事实计入独立指标并阻止最终交付，不能被"材料不足"四字自动豁免。
    # δ 线在 quality 上新增 needs_review_claims；未落地时回退到 report_claims 计数，避免漏网。
    needs_review_claims = quality.get("needs_review_claims")
    if needs_review_claims is None:
        pending = row(
            "SELECT COUNT(*) count FROM report_claims WHERE project_id=? AND support_status='needs_review'",
            (project_id,),
        )
        needs_review_claims = int((pending or {}).get("count") or 0)
    if needs_review_claims:
        problems.append(f"存在 {int(needs_review_claims)} 条待核验事实未决，处理后方可最终交付")
    # 修订稿 §四：最新一次尝试失败且未被后续成功覆盖 → 阻断，避免"最新尝试失败但正文还是旧的
    # 成功稿"被当成可交付。历史章节 latest_attempt_id 为空，不参与判断。
    failed_attempt = row(
        "SELECT COUNT(*) count FROM report_sections s "
        "JOIN chapter_generation_attempts a ON a.id=s.latest_attempt_id "
        "WHERE s.project_id=? AND a.status='failed' AND s.accepted_attempt_id!=s.latest_attempt_id",
        (project_id,),
    )
    if failed_attempt and failed_attempt["count"]:
        problems.append(
            f"{failed_attempt['count']} 个章节的最新生成尝试失败且未被后续成功覆盖，"
            "须重新生成或人工处理后才能交付"
        )
    problems.extend(_wording_problems(project_id))
    problems.extend(_table_problems(project_id))
    return problems


def _wording_problems(project_id: str) -> list[str]:
    """成品用词：禁用表述、内部 UUID、内部字段键都必须零出现。

    扫描**完整成品结构**（旧计划剩余项 P0-2），不只正文：章标题与目录标签、系统表格
    （表名／列标签／单元格／表注／口径）。只扫正文会漏掉「标题里是禁用词、表头里是
    禁用词」这类遗漏——版面上明明有禁用词，检查却通过。展示/导出层已做规范化转换，
    这里扫的是**转换后的实际呈现文本**：凡规范化后仍残留的一律报位置并阻断交付。
    """
    from .delivery_wording import material_numbers, normalize_report_text, wording_violations

    sections = rows(
        "SELECT * FROM report_sections WHERE project_id=?", (project_id,)
    )
    numbers = material_numbers(project_id)
    violations: list[dict] = []
    for section in sections:
        key = str(section.get("section_key") or "")
        body = str(section.get("content") or "")
        for item in wording_violations(body):
            violations.append({**item, "location": f"{key} 正文"})
        # 标题走读取侧同一套规范化：新旧行都按最终显示文本判。
        title = normalize_report_text(str(section.get("title") or ""), numbers)
        for item in wording_violations(title):
            violations.append({**item, "location": f"{key} 章标题"})
    # 系统表格：从快照渲染的同一份 blocks（网页/MD/Word 共用），规范化后再扫。
    from .report_blocks import section_blocks

    subject_row = row("SELECT company_name FROM projects WHERE id=?", (project_id,))
    subject = str((subject_row or {}).get("company_name") or "")
    for section in sections:
        key = str(section.get("section_key") or "")
        try:
            blocks = section_blocks(project_id, section, subject=subject)
        except Exception:  # 表格问题不阻断正文；扫描失败跳过该章表格
            continue
        for block in blocks:
            if block.get("kind") != "table":
                continue
            table = block.get("table") or {}
            table_id = str(block.get("table_id") or "")
            fields: list[tuple[str, str]] = [("表名", str(block.get("title") or ""))]
            fields += [(f"表头列「{col.get('label')}」", str(col.get("label") or ""))
                       for col in (block.get("columns") or []) if isinstance(col, dict)]
            fields += [(f"表注{i + 1}", str(note)) for i, note in enumerate(block.get("notes") or [])]
            for row_item in table.get("rows") or []:
                for cell in row_item.get("cells") or []:
                    fields.append((f"单元格（{row_item.get('row_key')}）", str(cell.get("raw") or "")))
            for field, text in fields:
                for item in wording_violations(text):
                    violations.append({**item, "location": f"{key} {table_id} {field}"})
    if not violations:
        return []
    summary = "；".join(sorted({f"{item.get('location')}：{item['value']}×{item['count']}" for item in violations}))
    return [f"成品用词不合规：{summary}"]


def _table_problems(project_id: str) -> list[str]:
    """系统表格校验未通过时阻止交付（计划：表格校验失败时最终版导出被阻止）。"""
    from .report_tables import blocked_reason

    subject = row("SELECT company_name FROM projects WHERE id=?", (project_id,))
    reason = blocked_reason(project_id, subject=str((subject or {}).get("company_name") or ""))
    return [f"系统数据表校验未通过：{reason}"] if reason else []


def review_notice(project_id: str) -> str:
    """草稿导出时必须携带的"待人工复核"警示（含问题摘要）。"""
    problems = delivery_blockers(project_id)
    if not problems:
        return ""
    return "本报告为待人工复核草稿，未通过最终交付校验：" + "；".join(problems) + "。"


def write_report_section(*, project_id: str, section_key: str, content: str,
                         citation_ids: list[str] | None = None,
                         allowed_source_ids: list[str] | None = None,
                         content_origin: str = "unknown", generation_status: str = "",
                         fallback_reason: str = "", fallback_stage: str = "none",
                         required_disclosures: list[str] | None = None,
                         deterministic_draft: str | None = None,
                         length_policy: dict | None = None,
                         diagnostics: dict | None = None,
                         table_records: list[dict] | None = None,
                         evidence_records: list[dict] | None = None,
                         table_blocks: list | None = None,
                         prompt_version: str = "", prompt_sha256: str = "", input_hash: str = "",
                         review_status: str = "draft", support_status: str = "pending",
                         title: str = "", section_id: str = "") -> dict:
    """写入章节正文，并同步来源、状态、诊断、篇幅与哈希（单一入口）。

    调用方负责传入**当次真实依据**（来源、状态、阶段、冻结披露），本函数只做
    “同一份正文对应同一套元数据”的强制对齐，避免出现正文与诊断互相矛盾的记录。

    成品用词规范化在此处、且在**校验与哈希之前**完成：正文、冻结披露与确定性草稿
    必须一起规范化——冻结披露要参与「逐字保留」比对，只改正文不改它就会把校验判成
    失败；而哈希必须在规范化之后计算，否则正文与哈希不同步。
    """
    from .delivery_wording import material_numbers, normalize_report_text

    numbers = material_numbers(project_id)
    content = normalize_report_text(content, numbers)
    # 章节标题同样要在保存前规范化：`章节标题` 直接进报告目录与前端标题栏，
    # 旧实现只在**导出**时规范化标题，库里的 `title` 仍是原材料名（"评级报告复核及
    # 评级差异"），GET /report 读取原样返回 → 网页/前端显示禁用词、与 Word 导出不一致
    # （旧计划剩余项 P0-2 的"评级章标题"遗漏）。`title` 为空（多数更新路径不带）时保持空。
    if title:
        title = normalize_report_text(str(title), numbers)
    required_disclosures = [
        normalize_report_text(block, numbers) for block in (required_disclosures or [])
    ]
    if deterministic_draft is not None:
        deterministic_draft = normalize_report_text(deterministic_draft, numbers)

    disclosures = [block for block in (required_disclosures or []) if block]
    length = assess_length(content, length_policy, disclosures)
    disclosure_status, delivery_status, validated_hash = assess_delivery(
        content=content, required_disclosures=disclosures, support_status=support_status,
        length_status=length["length_status"],
    )
    existing = row(
        "SELECT id,validated_content_sha256 FROM report_sections WHERE project_id=? AND section_key=?",
        (project_id, section_key),
    )
    digest = content_sha256(content)
    payload: dict[str, Any] = {
        "content": content,
        "citation_ids": json_dump(citation_ids or []),
        "allowed_source_ids": json_dump(sorted(set(allowed_source_ids or []))),
        "review_status": review_status,
        "updated_at": now_iso(),
        "content_origin": content_origin,
        "content_sha256": digest,
        "validated_content_sha256": validated_hash or (existing or {}).get("validated_content_sha256", ""),
        "generation_status": generation_status,
        "fallback_reason": fallback_reason,
        "fallback_stage": fallback_stage,
        "disclosure_status": disclosure_status,
        "delivery_status": delivery_status,
        "required_disclosures": json_dump(disclosures),
        "validation_diagnostics": json_dump(diagnostics or {}),
        "support_validation_status": support_status,
        "validation_status": support_status,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "input_hash": input_hash,
        "length_status": length["length_status"],
        "raw_output_chars": length["raw_output_chars"],
        "visible_content_chars": length["visible_content_chars"],
        "effective_min_chars": length["effective_min_chars"],
        "effective_max_chars": length["effective_max_chars"],
    }
    if deterministic_draft is not None:
        payload["deterministic_draft"] = deterministic_draft
    if table_records is not None:
        # 冻结结构化允许记录：审计与后置支持校验都读这一份，不再依赖重新取表。
        payload["table_records_json"] = json_dump(table_records)
    if evidence_records is not None:
        # 统一冻结记录（fact/table/metric/rule）：章节校验与后置支持校验的唯一记录来源。
        payload["evidence_records_json"] = json_dump(evidence_records)
    with connect() as conn:
        # 修订稿 §3.3：正文一旦被重新写入，恢复章节待复核、撤销旧的「最终复核」标记，
        # 且与正文/状态/哈希**同一事务**。放在 upsert **之前**：人工编辑（update_section）
        # 在同一请求里显式给出的复核结论由随后的 payload 落定，不被这里回退；而项目级
        # 最终复核标记因正文变化必须撤销（否则界面继续显示「最终版已就绪」）。
        reset_section_review(project_id, section_key, conn=conn)
        if existing:
            assignments = ", ".join(f"{key}=?" for key in payload)
            conn.execute(
                f"UPDATE report_sections SET {assignments} WHERE project_id=? AND section_key=?",
                (*payload.values(), project_id, section_key),
            )
        else:
            payload.update({"id": section_id or f"{project_id}-{section_key}",
                            "project_id": project_id, "section_key": section_key, "title": title})
            columns = ", ".join(payload)
            placeholders = ", ".join("?" for _ in payload)
            conn.execute(
                f"INSERT INTO report_sections ({columns}) VALUES ({placeholders})",
                tuple(payload.values()),
            )
        # 表格快照与正文/状态/哈希在**同一事务**冻结（第三点细化方案 §四：禁止提交正文后
        # 另行保存表格快照）；blocks 来自生成期的一次构建（chapter_context["table_blocks"]），
        # 与表格正文、结构化记录同源，不再重新查询当前表格。
        if (table_blocks is not None and section_key in {"financial", "cashflow", "debt", "rating"}
                and content_origin in {"model", "deterministic"}):
            from .report_tables import save_table_snapshot

            issuer_row = row("SELECT company_name FROM projects WHERE id=?", (project_id,))
            save_table_snapshot(
                project_id, section_key, subject=str((issuer_row or {}).get("company_name") or ""),
                blocks=list(table_blocks), conn=conn,
            )
    return {"content_sha256": digest, "length_status": length["length_status"],
            "visible_content_chars": length["visible_content_chars"],
            "disclosure_status": disclosure_status, "delivery_status": delivery_status}


def reset_section_review(project_id: str, section_key: str, *, conn=None) -> dict:
    """把某章恢复为待复核（`draft`），并在同一事务内撤销旧的「最终复核」标记。

    修订稿 §3.3：「成功后在同一事务保存正文、引用、表格/证据快照、生成状态和哈希，
    恢复章节待复核，撤销旧最终复核标记。」本函数负责后两项：

    - `report_sections.review_status` → `draft`（该章重新进入人工复核）；
    - 撤销项目级「最终复核」标记 `projects.report_reviewed_revision` /
      `report_reviewed_at`。`review_lifecycle.report_state()` 正是用
      `report_reviewed_revision == report_revision and report_revision > 0` 判定
      `final_ready`；正文一旦被重写，旧的最终复核结论就不再代表当前正文，必须回退为
      未复核，否则界面会继续显示「最终版已就绪」。

    幂等：重复调用结果一致——章节已是 `draft`、项目已未复核时为空更新（连 `updated_at`
    都不动）。

    `conn` 让调用方把本操作并入**自己的事务**（例如与 `write_report_section` 同批提交）；
    不传则内部自建连接并单独提交。仅触碰复核侧字段，不改证据侧
    `support_validation_status`（那由 `p0.validate_report_support` 负责）。
    """
    timestamp = now_iso()

    def _apply(connection) -> None:
        # 仅在该章确为已复核时才回退，避免幂等重复调用改动 updated_at。
        connection.execute(
            "UPDATE report_sections SET review_status='draft',updated_at=? "
            "WHERE project_id=? AND section_key=? AND review_status!='draft'",
            (timestamp, project_id, section_key),
        )
        # 仅在确有待撤销的最终复核标记时才回退，保证重复调用为空操作。
        connection.execute(
            "UPDATE projects SET report_reviewed_revision=0,report_reviewed_at=NULL,updated_at=? "
            "WHERE id=? AND (report_reviewed_revision!=0 OR report_reviewed_at IS NOT NULL)",
            (timestamp, project_id),
        )

    if conn is not None:
        _apply(conn)
    else:
        with connect() as owned:
            _apply(owned)
    return {
        "project_id": project_id,
        "section_key": section_key,
        "review_status": "draft",
        "report_reviewed_revision": 0,
        "report_reviewed_at": None,
    }
