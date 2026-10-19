"""第三点细化方案 §五 · 隔离真实模型验收脚本。

在**已配置模型**的终端里运行（LLM_API_KEY / LLM_MODEL / [LLM_BASE_URL] 已在环境中）：

    cd C:\\Users\\Jamie\\Desktop\\agent构建\\fin-guarantee-agent
    .venv\\Scripts\\python.exe scripts\\stage6_acceptance.py            # 默认跑 北投/中邮/华辰

做什么：
  1. 隔离：正式 .data 复制到 .data-accept（SQLite backup 保证一致性；正式库只读，不写不删）；
  2. 逐项目跑完整工作流（真实模型、真实材料，不跳过）；
  3. 汇总交付物到 acceptance_output/：
     - report.md   总报告（各项目 run/调用数/费用/回退五分类/交付阻断/回退诊断明细）
     - <key>.json  结构化明细（各章生成状态、首稿与修复诊断 attempts、用量、证据快照、质量、交付门、
                   回退五分类 fallback_diagnosis 与明细）
     - <key>.md    报告正文；<key>.docx  Word 导出

退出码：0=全部完成；2=未检测到模型配置；3=有项目未跑完（缺失/失败）；
        1=参数或脚本错误。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parents[1]

PROJECTS = {
    "beitou": ("b98796c4-5445-4fba-a6f4-412f91a3d025", "北投"),
    "zhongyou": ("real-bond-zhongyou-2026-02", "中邮"),
    "huachen": ("demo-bond-huachen", "华辰（演示）"),
}

# 回退五分类（三项遗留修复方案 §三）：误退率只使用 suspected_false_rejection 作分子。
FALLBACK_BUCKETS = (
    "confirmed_valid_rejection", "suspected_false_rejection", "needs_human_review",
    "repaired_and_adopted", "final_failure",
)
# 明确指向"应当拒绝"的原因标记（与冻结记录无关的硬失败）。
VALID_REJECTION_MARKERS = (
    "自行绘制", "禁止的承保", "审批", "投资结论", "未授权引用",
    "新评级", "期间不匹配", "科目不匹配", "主体不匹配", "口径不匹配",
)
# "新主体"类拒绝**不自动确证**：实体比对对"两侧 run-on 头部不同"的同一主体可能误判
# （中邮 rating 实测——「联合资信评估股份有限公司」就在冻结引文里仍被判"新主体"，
# 却被本分类器归为确证拒绝，"疑似误退为零"因此不能建立在未经复核的实体归因上）。
ENTITY_REJECTION_MARKERS = ("新主体",)
# 明确指向"无法判定"的原因标记 → 待人工核验。
REVIEW_MARKERS = ("待核验", "归属不明", "无法", "ambiguous")


def _fmt_ms(value) -> str:
    """耗时显示：毫秒 → 秒（保留 1 位）。缺失时写 `-`，不要写 0 冒充"没花时间"。"""
    if value in (None, ""):
        return "-"
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return "-"
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


def _number_present(body: str, token: str) -> bool:
    """锚点是否以**任一等价书写形态**出现：去千分位、去尾零、忽略 % 符。

    成品用词规范化会写"96,717,233.2"（去尾零）并省略某些 % 符号；字面比对会把
    正确数字误判成缺失（三项遗留修复方案 §五锚点口径）。
    """
    import re as _re

    target = token.replace(",", "").rstrip("%％")
    if "." in target:
        target = target.rstrip("0").rstrip(".")
    for match in _re.finditer(r"\d[\d,]*(?:\.\d+)?", body or ""):
        value = match.group(0).replace(",", "")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        if value == target:
            return True
    return False


def _decode_records(raw) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError:
            return []
        if isinstance(decoded, list):
            return [item for item in decoded if isinstance(item, dict)]
    return []


def _frozen_number_keys(records: list[dict]) -> set:
    """冻结记录折算成 (归一化值, 归一化单位) 键（与校验器同口径）。"""
    from backend.app.evidence_records import evidence_number_keys

    return evidence_number_keys(records)


def _match_frozen_number(number: dict, keys: set, records: list[dict]) -> list[dict]:
    """被拒数字是否能在冻结记录里找到同值（同单位或裸数字）——命中即"记录存在却被拒"。"""
    value = str(number.get("normalized_value") or "")
    unit = str(number.get("normalized_unit") or "")
    if (value, unit) in keys or (value, "") in keys:
        return [
            {"record_id": record.get("record_id"), "record_type": record.get("record_type"),
             "value": record.get("value"), "unit": record.get("unit"),
             "label": record.get("label"), "status": record.get("status")}
            for record in records
            if str(record.get("value")) == value
        ][:3]
    return []


def _classify_failure(attempt: dict, records: list[dict], keys: set) -> tuple[str, list[dict], str]:
    """单次失败尝试 → (分类, 命中记录, 失败原因文本)。空分类表示未分类。"""
    try:
        detail = json.loads(attempt.get("diagnostics_json") or "{}")
    except ValueError:
        detail = {}
    reasons = [str(item) for item in (detail.get("safety_repair_reasons") or [])]
    # 支持检查并入同一候选评估后（计划 §四 line 33），"修复稿支持仍不过"的尝试只有在
    # `support_reasons` 里才有原因文本——不回填的话这些条目会因 reason_text 为空而落到
    # "未分类"，而验收硬性要求 unclassified = 0。
    if not reasons:
        reasons = [str(item) for item in (detail.get("support_reasons") or [])]
    # 旧路径也可能只有 fallback 原因文本：允许调用方在外层补（见 _diagnose_fallbacks）。
    reason_text = "；".join(reasons)
    hits: list[dict] = []
    for number in detail.get("unexpected_numbers") or []:
        hits.extend(_match_frozen_number(number, keys, records))
    if hits:
        # 被拒数字在冻结记录里同值存在 → 大概率是单位继承/精度/来源类型/格式导致的误退。
        return "suspected_false_rejection", hits, reason_text
    review_items = detail.get("needs_review_items") or []
    if review_items or any(marker in reason_text for marker in REVIEW_MARKERS):
        return "needs_human_review", [], reason_text
    if detail.get("support_failed"):
        # 候选含**无证据支持的主张**：不是"误退"（冻结记录里确实找不到支撑），也不是
        # 科目/期间错配那种确定错误——归待人工裁决最稳。
        return "needs_human_review", [], reason_text
    if any(marker in reason_text for marker in ENTITY_REJECTION_MARKERS):
        # 实体类拒绝归**待人工**而非确证：离线无法在验收脚本里复核实体比对
        # （对照冻结引文），"误退为零"不应建立在此类未经复核的归因上。
        return "needs_human_review", [], reason_text
    if (detail.get("attribution_conflicts") or []) or any(
            marker in reason_text for marker in VALID_REJECTION_MARKERS):
        return "confirmed_valid_rejection", [], reason_text
    if detail.get("unexpected_numbers"):
        # 有"新数字"但记录里查无同值：与校验器结论一致 → 确证拒绝。
        return "confirmed_valid_rejection", [], reason_text
    return "", [], reason_text


def _attempt_support_items(attempt: dict) -> list[dict]:
    """失败尝试的支持明细（主张位置/原因码/匹配记录）——收口方案 §五"逐条证明"用。"""
    try:
        detail = json.loads(attempt.get("diagnostics_json") or "{}")
    except ValueError:
        detail = {}
    return [
        {
            "claim_index": item.get("claim_index"),
            "claim_text": str(item.get("claim_text") or "")[:120],
            "reason_code": str(item.get("reason_code") or ""),
            "matched_record_ids": list(item.get("matched_record_ids") or [])[:5],
        }
        for item in (detail.get("support_failed_items") or [])[:4]
    ]


def _attempt_first_rejection(attempt: dict) -> str:
    """本跑首拒原因（诊断冻结的 ``first_rejection_reasons``；旧数据返回空串）。"""
    try:
        detail = json.loads(attempt.get("diagnostics_json") or "{}")
    except ValueError:
        detail = {}
    return "；".join(str(item) for item in (detail.get("first_rejection_reasons") or [])[:3])


def _attempt_first_rejection_support(attempt: dict) -> list[dict]:
    try:
        detail = json.loads(attempt.get("diagnostics_json") or "{}")
    except ValueError:
        detail = {}
    return [
        {
            "claim_index": item.get("claim_index"),
            "claim_text": str(item.get("claim_text") or "")[:120],
            "reason_code": str(item.get("reason_code") or ""),
            "matched_record_ids": list(item.get("matched_record_ids") or [])[:5],
        }
        for item in (detail.get("first_rejection_support_items") or [])[:4]
    ]


def _diagnose_fallbacks(sections: list[dict], attempts_by_section: dict[str, list[dict]]) -> dict:
    """按「章节尝试台账 + 首稿/修复诊断 + 冻结记录」做五分类（三项遗留修复方案 §三）。

    只对**回退条目**做诊断：``generation_status == "fallback"`` 的章节，外加"首稿被拒、
    一次修复被采用"的章节（repaired_and_adopted）。确定性/跳过/正常采用等非回退终态
    不参与分类、也不计未分类。

    统计口径：误退率只用 ``suspected_false_rejection`` 作分子；任何
    ``unclassified_fallbacks > 0`` 都不得宣称误退率已完成验收。
    """
    result = {bucket: [] for bucket in FALLBACK_BUCKETS}
    unclassified = 0
    for section in sections:
        section_key = str(section.get("section_key") or "")
        attempts = attempts_by_section.get(section_key) or []
        failed = [item for item in attempts if item.get("status") == "failed"]
        adopted = any(item.get("adopted") for item in attempts)
        records = _decode_records(section.get("evidence_records_json")) or _decode_records(
            section.get("table_records_json"))
        keys = _frozen_number_keys(records) if records else set()
        generation_status = str(section.get("generation_status") or "")
        # `model_needs_review`（修复后仅剩未决项）正文是模型稿、**不计入合格采用率**，
        # 交付状态为 needs_review——它和回退一样需要人工裁决后才算最终交付
        # （计划 §四 line 38）。不把它算进 `blocks_delivery` 会让"待核验章"在验收
        # 汇总里看起来和"全部通过"一样干净。
        blocks_delivery = generation_status in {"fallback", "model_needs_review"} or str(
            section.get("support_validation_status") or "") == "needs_review"
        item = {
            "section": section_key,
            "generation_status": generation_status,
            "content_origin": str(section.get("content_origin") or ""),
            "fallback_stage": str(section.get("fallback_stage") or "none"),
            "current_reason": str(section.get("fallback_reason") or ""),
            "blocks_final_delivery": blocks_delivery,
            "evidence_record_count": len(records),
        }
        if generation_status in {"model_passed", "model_needs_review"}:
            # 首稿被拒、一次修复被采用：只这一类非回退章节进入诊断。
            # **按本跑判定**：以最近一次尝试行自己的 repair_type 为准——它记录了这一跑是否
            # 真的发生过（安全/篇幅/JSON）修复。早期实现用"该章节历史上存在失败尝试"判定，
            # 会把本跑一次通过（repair_type=none）的章节误标成"修复采纳"，并用旧跑的失败
            # 原因顶替（中邮 rating 2026-10-14 实测：本跑首稿直过，却显示 16:09 旧原因）。
            # `model_needs_review` 同样可能来自修复采纳（正文就是那一次修复的输出）。
            latest = attempts[-1] if attempts else {}
            repair_used = str(latest.get("repair_type") or "none") != "none"
            if adopted and repair_used:
                # 首拒原因取**本跑**冻结值（旧数据缺该字段时退回最近失败尝试文本并注明
                # 其可能来自历史运行）。
                reason_text = _attempt_first_rejection(latest)
                support_items = _attempt_first_rejection_support(latest)
                if not reason_text and failed:
                    _bucket, _hits, reason_text = _classify_failure(failed[-1], records, keys)
                if not support_items and failed:
                    support_items = _attempt_support_items(failed[-1])
                item.update({
                    "first_reason": reason_text or "（首稿被拒原因未记录）",
                    "repair_result": "adopted", "matched_records": [],
                    "support_items": support_items,
                })
                result["repaired_and_adopted"].append(item)
            continue
        if generation_status != "fallback":
            # 确定性/跳过/空状态：不是回退条目。
            continue
        # 最终回退：按最后一次失败尝试分类；无失败尝试时按章节回退原因分类。
        if failed:
            bucket, hits, reason_text = _classify_failure(failed[-1], records, keys)
            item.update({
                "first_reason": reason_text,
                # "修复是否发生"看本跑这一行自己的 `repair_type`，**不能数失败尝试行数**：
                # 一次尝试只落一行（最终态），"修过但仍失败"时 `len(failed)==1`，
                # 旧口径会把"修了没救回来"显示成 `none`——2026-10-19 第五轮实证：
                # 中邮 cashflow / rating 的 repair_type=safety，报告却写"修复结果=none"，
                # 与上方 `model_*` 分支（已按 repair_type 判定）也不一致。
                "repair_result": (
                    "failed" if str(failed[-1].get("repair_type") or "none") != "none" else "none"
                ),
                "matched_records": hits,
                "support_items": _attempt_support_items(failed[-1]),
            })
        else:
            reason_text = item["current_reason"]
            item.update({"first_reason": reason_text, "repair_result": "none", "matched_records": []})
            if any(marker in reason_text for marker in REVIEW_MARKERS):
                bucket = "needs_human_review"
            elif any(marker in reason_text for marker in VALID_REJECTION_MARKERS):
                bucket = "confirmed_valid_rejection"
            else:
                bucket = ""
        if not bucket:
            # 计划外家族的就地归属：支持门未过（细节未持久化）→ 待人工；
            # 模型调用/输入预算类基础设施失败 → 记 infrastructure_failure（终态维度）。
            combined = f"{reason_text}；{item.get('current_reason') or ''}"
            if any(marker in combined for marker in ("证据检查", "前置证据支持", "后置证据")):
                bucket = "needs_human_review"
            elif any(marker in combined for marker in (
                    "模型调用或结构校验失败", "input_budget_exceeded", "report_repair_required")):
                bucket = "infrastructure_failure"
        if not bucket:
            unclassified += 1
            continue
        # final_failure 是"终态"维度（保留旧稿/确定性备用稿），与原因桶并行记录；
        # 明细输出以 final_failure + repaired_and_adopted 为准，逐条只列一次。
        item["classification"] = bucket
        if bucket in result:
            result[bucket].append(item)
        result["final_failure"].append(item)
    return {
        "fallback_diagnosis": {bucket: len(result[bucket]) for bucket in FALLBACK_BUCKETS},
        "fallback_diagnosis_items": result,
        "unclassified_fallbacks": unclassified,
    }


def _prepare_isolated_data(fresh: bool) -> Path:
    src = ROOT / ".data"
    dst = ROOT / ".data-accept"
    if fresh and dst.exists():
        print("[隔离] --fresh：重建 .data-accept")
        shutil.rmtree(dst, ignore_errors=True)
    if not (dst / "agent.db").exists():
        print("[隔离] 复制正式库 → .data-accept（文件树 + SQLite 一致性备份，约需数秒到一分钟）…")
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            src, dst, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("backups", "agent.db", "agent.db-wal", "agent.db-shm"),
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                # 路径含中文：用 as_uri() 做百分号编码（SQLite 会解码 %HH）；连接失败时回退普通连接
                # （只读用法，不改正式库内容；WAL 读连接可能瞬时创建 -shm，属 SQLite 正常机制）。
                try:
                    src_conn = sqlite3.connect((src / "agent.db").resolve().as_uri() + "?mode=ro", uri=True)
                except Exception:
                    src_conn = sqlite3.connect(str(src / "agent.db"))
                dst_conn = sqlite3.connect(str(dst / "agent.db"))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    src_conn.close()
                    dst_conn.close()
                last_error = None
                break
            except Exception as exc:  # 源库可能瞬时被占用
                last_error = exc
                print(f"  … 备份重试（{attempt + 1}/3）：{type(exc).__name__}: {exc}")
                time.sleep(1.5)
        if last_error is not None:
            raise RuntimeError(f"隔离库备份失败：{last_error}")
    else:
        print("[隔离] 复用现有 .data-accept（--fresh 可重建）")
    return dst


def _collect_project(project_id: str, label: str, run_id: str, out_dir: Path, key: str,
                     *, code_fingerprint: str = "") -> dict:
    from backend.app.database import row, rows

    data: dict = {"project_id": project_id, "label": label, "run_id": run_id}
    if code_fingerprint:
        data["code_fingerprint"] = code_fingerprint
    data["run"] = dict(row("SELECT * FROM runs WHERE id=?", (run_id,)) or {})
    sections = [dict(item) for item in rows(
        "SELECT section_key,title,generation_status,fallback_reason,fallback_stage,content_origin,"
        "length_status,support_validation_status,validation_status,delivery_status,visible_content_chars,"
        "json_array_length(COALESCE(citation_ids,'[]')) AS citations_count,"
        "input_hash,prompt_version,content_sha256,content,evidence_records_json,table_records_json,"
        "accepted_attempt_id,latest_attempt_id FROM report_sections WHERE project_id=? ORDER BY rowid",
        (project_id,),
    )]
    data["sections"] = [{k: v for k, v in item.items() if k != "content"} for item in sections]

    attempts_by_section: dict[str, list[dict]] = {}
    if row("SELECT name FROM sqlite_master WHERE type='table' AND name='chapter_generation_attempts'"):
        attempt_rows = [dict(item) for item in rows(
            "SELECT section_key,status,stage,repair_type,adopted,diagnostics_json,created_at "
            "FROM chapter_generation_attempts WHERE project_id=? ORDER BY created_at", (project_id,))]
        data["attempts"] = [
            {k: v for k, v in item.items() if k != "diagnostics_json"} for item in attempt_rows
        ]
        for item in attempt_rows:
            attempts_by_section.setdefault(item["section_key"], []).append(item)

    data["usage"] = dict(row(
        "SELECT COUNT(*) AS calls,COALESCE(SUM(cost),0) AS cost,COALESCE(SUM(input_tokens),0) AS input_tokens,"
        "COALESCE(SUM(output_tokens),0) AS output_tokens,"
        # §五 line 66 要求归档"耗时及费用"：只记费用会让"是复算拖慢还是模型拖慢"无从判断，
        # §三.3 也要求分别衡量本地验证与额外模型输出的耗时。
        "COALESCE(SUM(latency_ms),0) AS latency_ms,COALESCE(MAX(latency_ms),0) AS max_latency_ms "
        "FROM llm_attempts WHERE run_id=?", (run_id,)) or {})
    if int(data["usage"].get("calls") or 0):
        data["usage"]["avg_latency_ms"] = int(
            int(data["usage"].get("latency_ms") or 0) / int(data["usage"]["calls"]))
    data["table_snapshots"] = [dict(item) for item in rows(
        "SELECT chapter_key,input_hash,rule_version,updated_at FROM report_table_snapshots WHERE project_id=?",
        (project_id,))]

    # 自主计算与建议复核（风控约束分层 §三 / §四）：验收必须能回答
    # "模型自主算了几条、复算通过几条、被拒几条"和"建议草案是否逐条确认"。
    # 单独查而不并进上面的 sections SELECT：两个 JSON 列的体积会灌进报告正文。
    calc_sections: dict[str, dict] = {}
    for item in rows(
        "SELECT section_key,calculations_json,calculation_trace_hash FROM report_sections "
        "WHERE project_id=? ORDER BY rowid", (project_id,),
    ):
        items = json.loads(item.get("calculations_json") or "[]")
        if not items:
            continue
        calc_sections[item["section_key"]] = {
            "count": len(items),
            "verified": sum(1 for x in items if x.get("status") == "verified"),
            "needs_review": sum(1 for x in items if x.get("status") == "needs_review"),
            "rejected": sum(1 for x in items if x.get("status") == "rejected"),
            "trace_hash": str(item.get("calculation_trace_hash") or ""),
        }
    # 本地复算耗时（§三.3「分别衡量本地验证和额外模型输出的耗时」）：`report_sections`
    # 只冻结轨迹，`elapsed_ms` 留在尝试台账的 `calculation_summary` 里，所以从这里汇。
    # 多轮复算（首稿 + 修复稿）**累加**是有意的——它回答的是"这一章一共花了多久复算"。
    verify_ms = 0
    if row("SELECT name FROM sqlite_master WHERE type='table' AND name='chapter_generation_attempts'"):
        for item in rows(
            "SELECT diagnostics_json FROM chapter_generation_attempts WHERE project_id=?",
            (project_id,),
        ):
            try:
                diag = json.loads(item.get("diagnostics_json") or "{}")
            except (TypeError, ValueError):
                continue
            verify_ms += int((diag.get("calculation_summary") or {}).get("elapsed_ms") or 0)
    data["calculations"] = {
        "sections": calc_sections,
        "count": sum(v["count"] for v in calc_sections.values()),
        "verified": sum(v["verified"] for v in calc_sections.values()),
        "needs_review": sum(v["needs_review"] for v in calc_sections.values()),
        "rejected": sum(v["rejected"] for v in calc_sections.values()),
        "verify_ms": verify_ms,
    }
    # 建议复核：按状态计数。`superseded` 是历史留档（重新生成产生的新行），单列出来。
    data["recommendations"] = {}
    if row("SELECT name FROM sqlite_master WHERE type='table' AND name='report_recommendations'"):
        data["recommendations"] = {
            str(item["status"]): int(item["n"]) for item in rows(
                "SELECT status,COUNT(*) AS n FROM report_recommendations WHERE project_id=? GROUP BY status",
                (project_id,),
            )
        }

    try:
        from backend.app.p0 import validate_report_support
        data["quality"] = validate_report_support(project_id)
    except Exception as exc:
        data["quality"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        from backend.app.report_delivery import delivery_blockers
        data["delivery_blockers"] = delivery_blockers(project_id)
    except Exception as exc:
        data["delivery_blockers"] = [f"（评估失败 {type(exc).__name__}: {exc}）"]

    # 回退五分类（三项遗留修复方案 §三）：不再用关键词拍板——读 attempts 台账、首稿/修复
    # 诊断与统一冻结记录，按"记录命中/明确错误/无法判定"结构性判定；未分类必须为 0。
    diagnosis = _diagnose_fallbacks(data["sections"], attempts_by_section)
    data["fallback_diagnosis"] = diagnosis["fallback_diagnosis"]
    data["fallback_diagnosis_items"] = diagnosis["fallback_diagnosis_items"]
    data["unclassified_fallbacks"] = diagnosis["unclassified_fallbacks"]
    # 兼容旧字段名：suspected_false_rejection 即"误退"清单（误退率的分子）。
    data["suspicious_fallbacks"] = diagnosis["fallback_diagnosis_items"]["suspected_false_rejection"]

    if key == "beitou":
        body = "\n\n".join(str(item.get("content") or "") for item in sections)
        # 锚点按**数值等价**比对（三项遗留修复方案 §五）：规范化会去尾零/省略 % 符，
        # 字面串比对会把"96,717,233.2"对"96,717,233.20"误判成缺失。
        data["anchors"] = {
            token: _number_present(body, token)
            for token in ("96,717,233.20", "57,372,249.51", "751,716.86", "59.22%")
        }

    from backend.app.exports import docx_report, markdown_report
    (out_dir / f"{key}.md").write_text(markdown_report(project_id, "draft"), encoding="utf-8")
    try:
        (out_dir / f"{key}.docx").write_bytes(docx_report(project_id, "draft"))
    except Exception as exc:
        data["docx_error"] = f"{type(exc).__name__}: {exc}"

    (out_dir / f"{key}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="第三点细化方案 §五 隔离真实模型验收（退出码 0=机制通过 / 3=运行·材料未达标 / 4=跑完但机制未过）")
    parser.add_argument("--projects", default="beitou,zhongyou,huachen",
                        help="逗号分隔，可选：beitou / zhongyou / huachen")
    parser.add_argument("--fresh", action="store_true", help="重建隔离库 .data-accept")
    parser.add_argument("--timeout", type=int, default=3600, help="单项目等待上限（秒）")
    args = parser.parse_args()

    chosen = [item.strip() for item in args.projects.split(",") if item.strip()]
    unknown = [item for item in chosen if item not in PROJECTS]
    if unknown:
        print(f"未知项目：{unknown}（可选：{list(PROJECTS)}）")
        return 1

    try:
        isolated = _prepare_isolated_data(args.fresh)
    except Exception as exc:
        print(f"!! 隔离失败：{exc}")
        return 1
    # 必须在 import backend 之前设定数据目录（config 在 import 期读环境变量）
    os.environ["APP_DATA_DIR"] = str(isolated)
    sys.path.insert(0, str(ROOT))

    from backend.app.llm import is_configured
    if not is_configured():
        print("\n!! 未检测到模型配置（LLM_API_KEY 为空）：请在已配置模型的终端里运行本脚本。")
        return 2

    from backend.app.database import execute, init_db, now_iso, row
    from backend.app.workflow import TERMINAL, start_run

    init_db()
    out_dir = ROOT / "acceptance_output"
    out_dir.mkdir(exist_ok=True)

    # 保留旧基线（收口方案 §五「每次试跑使用新隔离目录与输出目录，保留旧基线」）：
    # 落盘前把上一轮产物快照进 acceptance_output/baselines/<时间戳>/，避免覆盖即丢失；
    # 当前交付物仍写 acceptance_output/ 根目录，保持既有消费方（归档脚本）不变。
    from backend.app.workflow import _code_fingerprint as _fingerprint_now

    try:
        code_fingerprint = _fingerprint_now()
    except Exception:
        code_fingerprint = ""
    previous = [
        item for item in out_dir.iterdir()
        if item.is_file() and (item.suffix in {".md", ".docx", ".json"} or item.name == "report.md")
    ]
    if previous:
        baseline_dir = out_dir / "baselines" / time.strftime("%Y%m%d-%H%M%S")
        baseline_dir.mkdir(parents=True, exist_ok=True)
        for item in previous:
            shutil.copy2(item, baseline_dir / item.name)
        print(f"[基线] 上一轮产物已快照到 {baseline_dir.relative_to(ROOT)}（{len(previous)} 个文件）")

    summary: list[dict] = []
    for key in chosen:
        project_id, label = PROJECTS[key]
        if not row("SELECT id FROM projects WHERE id=?", (project_id,)):
            print(f"\n!! 隔离库中找不到项目 {label}（{project_id}）——材料缺失，记录为失败（不以跳过代替）。")
            summary.append({"key": key, "label": label, "status": "missing_project"})
            continue
        print(f"\n[运行] {label}（{project_id}）…")
        started = time.time()
        # 隔离库副本会带着正式库的归档状态（实测：北投在正式库被归档 → 复制后 start_run
        # 直接拒绝，试跑整章缺失）。试跑在**隔离副本**上运行：先恢复归档态的项目保证
        # 验收可执行；正式库只读复制，不受影响。
        archived = row("SELECT archived_at FROM projects WHERE id=?", (project_id,))
        if archived and archived.get("archived_at"):
            execute("UPDATE projects SET archived_at=NULL,updated_at=? WHERE id=?",
                    (now_iso(), project_id))
            print("  [准备] 该项目在隔离库中处于归档态，已恢复（仅隔离副本，正式库不受影响）")
        try:
            run_id = start_run(project_id)
        except Exception as exc:
            print(f"  !! 启动失败：{type(exc).__name__}: {exc}")
            summary.append({"key": key, "label": label, "status": f"start_error: {exc}"})
            continue
        print(f"  run_id = {run_id}")
        last_state = ""
        while True:
            current = row("SELECT status,current_node FROM runs WHERE id=?", (run_id,))
            state = f"{current['status']} / {current['current_node']}"
            if state != last_state:
                print(f"  … {state}（{int(time.time() - started)}s）")
                last_state = state
            if current["status"] in TERMINAL:
                break
            if time.time() - started > args.timeout:
                print(f"  !! 超过 {args.timeout}s 等待上限，停止等待（运行可能仍在后台继续）。")
                break
            time.sleep(2)
        try:
            data = _collect_project(project_id, label, run_id, out_dir, key,
                                    code_fingerprint=code_fingerprint)
            quality = data.get("quality") or {}
            invalid_citations = -1 if "error" in quality else int(quality.get("invalid_citations") or 0)
            diagnosis_counts = data.get("fallback_diagnosis") or {}
            suspected = int(diagnosis_counts.get("suspected_false_rejection") or 0)
            unclassified = int(data.get("unclassified_fallbacks") or 0)
            # 机制通过（收口方案 §五）：跑完 + 正确披露不误退（疑似误退=0）+ 真实错误被拦
            # （未分类=0，确证拒绝逐条在案）+ 无效引用为零。业务可交付单独报告：
            # 合法未决项可以存在，但必须标记并阻止最终交付。
            mechanism_ok = (
                data["run"].get("status") == "completed"
                and suspected == 0 and unclassified == 0 and invalid_citations == 0
            )
            summary.append({
                "key": key, "label": label, "run_id": run_id,
                "run_status": data["run"].get("status"),
                "calls": data["usage"].get("calls"),
                "cost": data["usage"].get("cost"),
                "latency_ms": data["usage"].get("latency_ms"),
                "avg_latency_ms": data["usage"].get("avg_latency_ms"),
                "diagnosis": data.get("fallback_diagnosis") or {},
                "diagnosis_items": data.get("fallback_diagnosis_items") or {},
                "unclassified": unclassified,
                "blockers": len(data.get("delivery_blockers") or []),
                "invalid_citations": invalid_citations,
                "suspected_false_rejection": suspected,
                "mechanism_ok": mechanism_ok,
                "delivery_ok": not (data.get("delivery_blockers") or []),
                "calculations": data.get("calculations") or {},
                "recommendations": data.get("recommendations") or {},
            })
        except Exception as exc:
            print(f"  !! 汇总失败：{type(exc).__name__}: {exc}")
            summary.append({"key": key, "label": label, "run_id": run_id, "status": f"collect_error: {exc}"})

    lines = [
        "# 第三点细化方案 · 隔离真实模型验收报告",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 隔离库：.data-accept（正式 .data 只读复制，未写入正式库）",
        f"- 代码指纹：{code_fingerprint or '（未取到）'}",
        "",
    ]
    for item in summary:
        counts = item.get("diagnosis") or {}
        mechanism = "通过" if item.get("mechanism_ok") else (
            "未完成" if item.get("run_status") != "completed" else "未通过")
        delivery = "可交付" if item.get("delivery_ok") else f"阻断×{item.get('blockers', '-')}"
        lines.append(
            f"- {item.get('label')}: run={item.get('run_id', '-')} "
            f"status={item.get('run_status') or item.get('status') or '-'} "
            f"calls={item.get('calls', '-')} cost={item.get('cost', '-')} "
            f"模型耗时={_fmt_ms(item.get('latency_ms'))}（均 {_fmt_ms(item.get('avg_latency_ms'))}） "
            f"复算耗时={_fmt_ms((item.get('calculations') or {}).get('verify_ms'))} "
            f"确证拒绝={counts.get('confirmed_valid_rejection', '-')} "
            f"疑似误退={counts.get('suspected_false_rejection', '-')} "
            f"待人工={counts.get('needs_human_review', '-')} "
            f"修复采用={counts.get('repaired_and_adopted', '-')} "
            f"最终失败={counts.get('final_failure', '-')} "
            f"未分类={item.get('unclassified', '-')} 无效引用={item.get('invalid_citations', '-')} "
            f"｜机制={mechanism} ｜业务交付={delivery}"
        )
        calc = item.get("calculations") or {}
        rec = item.get("recommendations") or {}
        if calc:
            lines.append(
                f"  - 自主计算：{calc.get('count', 0)} 条（复算通过 {calc.get('verified', 0)} / "
                f"待核验 {calc.get('needs_review', 0)} / 未通过 {calc.get('rejected', 0)}）"
            )
        if rec:
            lines.append(
                f"  - 建议草案：合计 {sum(rec.values())} 条（待确认 {rec.get('pending', 0)} / "
                f"已确认 {rec.get('confirmed', 0)} / 已驳回 {rec.get('rejected', 0)} / "
                f"已失效 {rec.get('stale', 0)} / 已取代 {rec.get('superseded', 0)}）"
            )
    # 回退诊断明细（三项遗留修复方案 §三）：章节/首次原因/修复结果/命中记录/正文来源/是否阻止交付。
    # final_failure 已包含所有最终回退条目（疑似误退是其子集），逐条只列一次。
    # 自主计算与建议复核（风控约束分层 §三 / §四）：验收要能逐章对照
    # "模型自己算了什么、复算结果如何"，而不只是一个总数。
    detail_lines: list[str] = []
    for item in summary:
        calc = item.get("calculations") or {}
        for section_key, stats in (calc.get("sections") or {}).items():
            detail_lines.append(
                f"- [{item.get('label')}] {section_key}：{stats.get('count')} 条"
                f"（通过 {stats.get('verified')} / 待核验 {stats.get('needs_review')} / "
                f"未通过 {stats.get('rejected')}）trace={stats.get('trace_hash') or '-'}"
            )
    if detail_lines:
        lines += ["", "## 自主计算明细", ""] + detail_lines
    lines += ["", "## 回退诊断明细", ""]
    for item in summary:
        entries = item.get("diagnosis_items") or {}
        listed = (entries.get("final_failure") or []) + (entries.get("repaired_and_adopted") or [])
        if not listed and not item.get("unclassified"):
            lines.append(f"- [{item.get('label')}] 无回退条目")
        for entry in listed:
            label = entry.get("classification") or "repaired_and_adopted"
            records = ",".join(
                str(record.get("record_id") or "") for record in entry.get("matched_records") or []
            ) or "-"
            lines.append(
                f"- [{item.get('label')}] {entry.get('section')}（{label}）："
                f"原因={entry.get('first_reason') or entry.get('current_reason') or '-'}；"
                f"修复结果={entry.get('repair_result')}；命中记录={records}；"
                f"正文来源={entry.get('content_origin') or '-'}；"
                f"阻止交付={'是' if entry.get('blocks_final_delivery') else '否'}"
            )
            # 逐条明细（收口方案 §五"逐条证明正确披露不误退"）：点名主张、原因码与匹配记录，
            # 使"待人工/确证"的归因可以被直接核对，而不是只有计数。
            for claim in (entry.get("support_items") or [])[:2]:
                lines.append(
                    f"    - 主张#{claim.get('claim_index')} [{claim.get('reason_code') or '未标注'}]"
                    f" 匹配记录={','.join(claim.get('matched_record_ids') or []) or '-'}："
                    f"『{claim.get('claim_text') or ''}』"
                )
        if item.get("unclassified"):
            lines.append(
                f"- [{item.get('label')}] 未分类回退 {item['unclassified']} 条"
                "（unclassified_fallbacks > 0，不得宣称误退率已完成验收）"
            )
    # 验收结论：**机制通过**与**业务可交付**分开报告（收口方案 §五）。机制通过 = 跑完 +
    # 正确披露不误退 + 真实错误被拦（未分类=0）+ 无效引用为零；合法未决项可以存在，但
    # 必须标记并阻止最终交付（业务交付列如实呈现，不因"运行结束"就宣称通过）。
    lines += ["", "## 验收结论", ""]
    run_failures = [item for item in summary if item.get("run_status") != "completed"]
    mechanism_failures = [
        item for item in summary
        if item.get("run_status") == "completed" and not item.get("mechanism_ok")
    ]
    delivery_blocked = [item for item in summary if not item.get("delivery_ok", True)]
    if run_failures:
        lines.append("- 运行/材料未达标（必须修复后重跑）："
                     + "、".join(str(item.get("label")) for item in run_failures))
    if mechanism_failures:
        lines.append(
            "- 机制未通过：" + "；".join(
                f"{item.get('label')}（疑似误退={item.get('suspected_false_rejection', '-')}、"
                f"未分类={item.get('unclassified', '-')}、无效引用={item.get('invalid_citations', '-')}）"
                for item in mechanism_failures))
    if not run_failures and not mechanism_failures:
        lines.append("- 机制通过：正确披露不误退、真实错误被拦、无效引用为零，回退全部可分类。")
    if delivery_blocked:
        lines.append(
            "- 业务交付存在阻断（合法未决项可以存在，但必须标记并阻止最终交付）："
            + "；".join(f"{item.get('label')} 阻断×{item.get('blockers', '-')}" for item in delivery_blocked))
    else:
        lines.append("- 业务交付：全部项目无交付阻断。")
    lines += ["", "> 明细见各项目 <key>.json / <key>.md / <key>.docx。"]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print("\n[完成] 交付物已写入 acceptance_output/：", ", ".join(sorted(p.name for p in out_dir.iterdir())))
    print("\n".join(lines))
    # 退出码区分"运行结束"与"验收通过"（收口方案 §五）：
    #   0 = 机制通过；3 = 运行/材料未达标；4 = 跑完但机制未过（误退/未分类/无效引用）。
    if run_failures:
        return 3
    if mechanism_failures:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
