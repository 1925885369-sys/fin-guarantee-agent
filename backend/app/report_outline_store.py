"""报告版本化目录（report-outline）的生成、校验、规范化与落库。

实现依据：``docs/report-outline-spec.md`` §二/§三/§五。本模块只负责"目录 JSON 本身"，
**不调用模型、不修改报告正文**。读取链是**逐份整覆盖**（不是 merge）：

- ``scope=project``：项目覆盖 > 全局覆盖 > builtin；
- ``scope=global``：全局覆盖 > builtin。

并发基准 ``config_sha256`` 统一口径：对**库中 payload_json 原文**算 sha256；无覆盖返回空串。
同一份存库原文，GET 与保存响应算出的值必然一致。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from .chapter_prompts import (
    BOND_CHAPTER_KEYS,
    BOND_CHAPTER_PARTS,
    BOND_CHAPTER_TITLES,
    chapter_uses_llm,
    load_chapter_config,
)
from .database import connect, json_dump, now_iso, row


# 内置目录版本标识（非 uuid）：前端据此区分"未改动的出厂目录"。
BUILTIN_OUTLINE_VERSION = "builtin-v1"
OUTLINE_SCOPES = ("project", "global")
MAX_CHAPTERS = 60
MAX_SUBSECTIONS_PER_CHAPTER = 30
MAX_TITLE_LENGTH = 60
MAX_MATERIAL_ROLES = 20
MAX_DOCUMENT_IDS = 30

# 自定义章节 ID 形如 ``chapter-<uuid>``（spec §一：template_key=null 仅限该形式）。
_CUSTOM_CHAPTER_ID = re.compile(
    r"^chapter-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# --------------------------------------------------------------------- 生成


def _derive_fields(template_key: str | None) -> tuple[str, str]:
    """按 template_key 派生只读字段：内置章节用出厂 llm 判据，自定义章节恒为生成 + 检索。"""
    if template_key:
        return ("generate" if chapter_uses_llm(template_key) else "template", "chapter_contract")
    return ("generate", "prompt_retrieval")


def builtin_outline() -> dict:
    """由现有元数据**确定性**生成出厂目录（每次调用返回全新对象，调用方不共享可变状态）。

    分部来自 ``BOND_CHAPTER_PARTS``（part-1..part-6）；章节顺序/标题取自既有元数据；
    小节从**出厂 YAML** 的 ``narrative_contract.paragraph_order`` + ``required_sections``
    按出现顺序去重初始化，``subsection_id = f"sub-{key}-{n}"``（n 从 1 起）。
    """
    parts: list[dict] = []
    for part_index, (part_title, keys) in enumerate(BOND_CHAPTER_PARTS, start=1):
        chapters: list[dict] = []
        for chapter_index, key in enumerate(keys, start=1):
            config = load_chapter_config(key)
            narrative = config.get("narrative_contract") or {}
            ordered = list(narrative.get("paragraph_order") or []) + list(config.get("required_sections") or [])
            subsections: list[dict] = []
            seen: set[str] = set()
            for raw_title in ordered:
                title = str(raw_title).strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                subsections.append({
                    "subsection_id": f"sub-{key}-{len(subsections) + 1}",
                    "title": title,
                    "order": len(subsections) + 1,
                    "prompt": "",
                    "enabled": True,
                })
            generation_mode, evidence_scope = _derive_fields(key)
            chapters.append({
                "chapter_id": f"chapter-{key}",
                "template_key": key,
                "title": BOND_CHAPTER_TITLES.get(key, key),
                "order": chapter_index,
                "generation_mode": generation_mode,
                "evidence_scope": evidence_scope,
                "subsections": subsections,
            })
        parts.append({
            "part_id": f"part-{part_index}",
            "title": part_title,
            "order": part_index,
            "chapters": chapters,
        })
    return {"outline_version": BUILTIN_OUTLINE_VERSION, "parts": parts}


# --------------------------------------------------------------------- 读取链


def _scope_identity(scope: str, project_id: str) -> str:
    """global 行固定 project_id=''；project 行用真实项目 ID。"""
    return project_id if scope == "project" else ""


def _stored_row(scope: str, project_id: str) -> dict | None:
    identity = _scope_identity(scope, project_id)
    return row(
        "SELECT scope,project_id,outline_version,payload_json,updated_at "
        "FROM report_outlines WHERE scope=? AND project_id=?",
        (scope, identity),
    )


def stored_outline(scope: str, project_id: str) -> dict | None:
    """该 scope 已存目录（无覆盖返回 None）。payload 解析失败按无覆盖处理，不抛异常。"""
    record = _stored_row(scope, project_id)
    if not record:
        return None
    try:
        payload = json.loads(record["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def effective_outline(project_id: str) -> dict:
    """项目链生效目录：项目覆盖 > 全局覆盖 > builtin（生成侧 P1-2/P1-4 读取用）。"""
    override = stored_outline("project", project_id)
    if override is not None:
        return override
    global_override = stored_outline("global", "")
    if global_override is not None:
        return global_override
    return builtin_outline()


def outline_effective_source(project_id: str) -> str:
    if _stored_row("project", project_id):
        return "project"
    if _stored_row("global", ""):
        return "global"
    return "builtin"


def has_project_override(project_id: str) -> bool:
    return _stored_row("project", project_id) is not None


def payload_sha256(payload_json: str) -> str:
    """并发基准：对**库中 payload_json 原文**算 sha256（无覆盖由调用方给空串）。"""
    return hashlib.sha256(str(payload_json or "").encode("utf-8")).hexdigest()


def outline_scope_state(scope: str, project_id: str) -> dict:
    """接口用：返回该 scope 的 ``outline``/``source``/``outline_version``/``config_sha256``。

    - ``outline`` 是该 scope 的**生效**目录（project=项目链；global=全局链>builtin）；
    - ``source`` 是生效来源（project/global/builtin）；
    - ``outline_version``/``config_sha256`` 只反映**该 scope 自己的覆盖行**，无覆盖均为 ``""``。
    """
    record = _stored_row(scope, project_id)
    if scope == "global":
        outline = stored_outline("global", "") or builtin_outline()
        source = "global" if record else "builtin"
    else:
        outline = effective_outline(project_id)
        source = outline_effective_source(project_id)
    return {
        "outline": outline,
        "source": source,
        "outline_version": str(record["outline_version"]) if record else "",
        "config_sha256": payload_sha256(record["payload_json"]) if record else "",
    }


def outline_fingerprint(outline: dict) -> str:
    """目录指纹：``sha256(payload_json 规范化序列化)``（P1-2 进章节 input_hash 的基准）。

    与 ``config_sha256``（对库中原文算）不同：这里对**任意目录对象**做键有序的规范化
    序列化，便于生成侧在不同存储形态下拿到稳定指纹。
    """
    canonical = json.dumps(outline, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def outline_for_chapter(project_id: str, chapter_key: str) -> dict | None:
    """在 effective_outline 中按 ``template_key`` 找章节；不在目录/被删返回 None。"""
    for part in effective_outline(project_id).get("parts", []):
        if not isinstance(part, dict):
            continue
        for chapter in part.get("chapters", []) or []:
            if isinstance(chapter, dict) and str(chapter.get("template_key") or "") == chapter_key:
                return chapter
    return None


# --------------------------------------------------------------------- 校验


def _clean_title(value, label: str) -> str:
    text = "" if value is None else str(value)
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label}标题不得包含换行")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError(f"{label}标题不能为空")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValueError(f"{label}标题不得超过 {MAX_TITLE_LENGTH} 字符")
    return cleaned


def _validate_chapter(chapter: dict, seen_chapter_ids: set[str]) -> None:
    chapter_id = str(chapter.get("chapter_id") or "").strip()
    if not chapter_id:
        raise ValueError("章节 ID 不能为空")
    if chapter_id in seen_chapter_ids:
        raise ValueError(f"章节 ID 全局重复：{chapter_id}")
    seen_chapter_ids.add(chapter_id)
    _clean_title(chapter.get("title"), "章节")

    template_key = chapter.get("template_key")
    if template_key is None:
        if not _CUSTOM_CHAPTER_ID.match(chapter_id):
            raise ValueError(f"自定义章节 ID 必须为 chapter-<uuid> 形式：{chapter_id}")
    else:
        template_key = str(template_key)
        if template_key not in BOND_CHAPTER_KEYS:
            raise ValueError(f"未知的内置章节 template_key：{template_key}")
        if chapter_id != f"chapter-{template_key}":
            raise ValueError(f"内置章节 ID 必须与 template_key 对应：{chapter_id} 应为 chapter-{template_key}")

    subsections = chapter.get("subsections")
    if subsections is None:
        subsections = []
    if not isinstance(subsections, list):
        raise ValueError("章节的 subsections 必须是列表")
    if len(subsections) > MAX_SUBSECTIONS_PER_CHAPTER:
        raise ValueError(f"单个章节小节数不得超过 {MAX_SUBSECTIONS_PER_CHAPTER} 条")
    seen_subsection_ids: set[str] = set()
    for subsection in subsections:
        if not isinstance(subsection, dict):
            raise ValueError("小节格式不正确")
        subsection_id = str(subsection.get("subsection_id") or "").strip()
        if not subsection_id:
            raise ValueError("小节 ID 不能为空")
        if subsection_id in seen_subsection_ids:
            raise ValueError(f"小节 ID 章内重复：{subsection_id}")
        seen_subsection_ids.add(subsection_id)
        _clean_title(subsection.get("title"), "小节")

    # P1-4：可选的检索约束（材料角色 = document_role 白名单；文件白名单仅项目范围有意义，
    # 全局配置不保存项目文件 ID）。空列表/缺省 = 默认检索本项目全部已解析材料。
    for field, label, limit in (
        ("material_roles", "材料角色", MAX_MATERIAL_ROLES),
        ("document_ids", "文件 ID", MAX_DOCUMENT_IDS),
    ):
        value = chapter.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{label}必须是字符串列表")
        if len(value) > limit:
            raise ValueError(f"{label}不得超过 {limit} 项")


def validate_outline(payload: dict) -> None:
    """业务校验；任一不符抛 ``ValueError``（中文消息，调用方转 422）。"""
    if not isinstance(payload, dict):
        raise ValueError("报告目录必须是 JSON 对象")
    parts = payload.get("parts")
    if not isinstance(parts, list):
        raise ValueError("报告目录缺少 parts 列表")

    total_chapters = 0
    seen_chapter_ids: set[str] = set()
    for part_index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise ValueError(f"第 {part_index} 个分部格式不正确")
        chapters = part.get("chapters")
        if not isinstance(chapters, list):
            raise ValueError(f"第 {part_index} 个分部缺少 chapters 列表")
        for chapter in chapters:
            if not isinstance(chapter, dict):
                raise ValueError("章节格式不正确")
            total_chapters += 1
            _validate_chapter(chapter, seen_chapter_ids)

    if total_chapters == 0:
        raise ValueError("报告目录至少需要 1 个二级章节")
    if total_chapters > MAX_CHAPTERS:
        raise ValueError(f"报告目录章节数不得超过 {MAX_CHAPTERS} 个")


# --------------------------------------------------------------------- 规范化


def normalize_outline(payload: dict) -> dict:
    """校验并规范化：重排 order、派生只读字段、换新 outline_version。

    返回**全新对象**，不修改入参。副本/顺序以下游读取为准（spec §一：顺序即顺序）。
    """
    validate_outline(payload)
    parts_out: list[dict] = []
    for part_index, part in enumerate(payload.get("parts") or [], start=1):
        chapters_out: list[dict] = []
        for chapter_index, chapter in enumerate(part.get("chapters") or [], start=1):
            raw_key = chapter.get("template_key")
            template_key = None if raw_key is None else str(raw_key)
            generation_mode, evidence_scope = _derive_fields(template_key)

            subsections_out: list[dict] = []
            for subsection_index, subsection in enumerate(chapter.get("subsections") or [], start=1):
                subsections_out.append({
                    "subsection_id": str(subsection.get("subsection_id") or "").strip(),
                    "title": _clean_title(subsection.get("title"), "小节"),
                    "order": subsection_index,
                    "prompt": str(subsection.get("prompt") or ""),
                    "enabled": bool(subsection.get("enabled", True)),
                })
            chapters_out.append({
                "chapter_id": str(chapter.get("chapter_id") or "").strip(),
                "template_key": template_key,
                "title": _clean_title(chapter.get("title"), "章节"),
                "order": chapter_index,
                "generation_mode": generation_mode,
                "evidence_scope": evidence_scope,
                "subsections": subsections_out,
                # P1-4：可选检索约束（材料角色=document_role 白名单；文件白名单）。空 = 默认
                # 检索本项目全部已解析材料。validate_outline 已做类型/上限校验。
                "material_roles": [
                    str(role).strip() for role in (chapter.get("material_roles") or [])
                    if str(role).strip()
                ][:MAX_MATERIAL_ROLES],
                "document_ids": [
                    str(doc).strip() for doc in (chapter.get("document_ids") or [])
                    if str(doc).strip()
                ][:MAX_DOCUMENT_IDS],
            })
        parts_out.append({
            "part_id": str(part.get("part_id") or f"part-{part_index}"),
            "title": str(part.get("title") or ""),
            "order": part_index,
            "chapters": chapters_out,
        })
    return {"outline_version": str(uuid.uuid4()), "parts": parts_out}


# --------------------------------------------------------------------- 落库


def _chapter_count(outline: dict) -> int:
    total = 0
    for part in (outline or {}).get("parts", []) or []:
        if isinstance(part, dict):
            total += len(part.get("chapters") or [])
    return total


def _row_summary(record: dict | None) -> dict:
    if not record:
        return {}
    try:
        payload = json.loads(record.get("payload_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    return {
        "outline_version": str(record.get("outline_version") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "chapter_count": _chapter_count(payload if isinstance(payload, dict) else {}),
    }


def _outline_summary(outline: dict) -> dict:
    return {
        "outline_version": str((outline or {}).get("outline_version") or ""),
        "chapter_count": _chapter_count(outline),
    }


def _audit(project_id: str, action: str, scope: str, before: dict, after: dict) -> None:
    from .professional import audit_event  # 惰性导入，保持本模块可独立加载

    audit_event(project_id, action, "report_outline", scope, before, {"scope": scope, **after}, "human")


def save_outline(scope: str, project_id: str, payload: dict) -> dict:
    """单行 UPSERT + 审计。**不调用模型、不改正文、不删除项目其他配置**。

    ``project_id`` 是 URL 上的项目 ID（供审计归属）；写入行的 project_id 由 scope 决定
    （global 固定 ''）。返回规范化后的目录（含新 outline_version）。
    """
    if scope not in OUTLINE_SCOPES:
        raise ValueError(f"不支持的目录范围：{scope}")
    normalized = normalize_outline(payload)
    identity = _scope_identity(scope, project_id)
    before = row(
        "SELECT * FROM report_outlines WHERE scope=? AND project_id=?", (scope, identity))
    with connect() as conn:
        conn.execute(
            "INSERT INTO report_outlines (scope,project_id,outline_version,payload_json,updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(scope,project_id) DO UPDATE SET "
            "outline_version=excluded.outline_version,payload_json=excluded.payload_json,updated_at=excluded.updated_at",
            (scope, identity, normalized["outline_version"], json_dump(normalized), now_iso()),
        )
    _audit(project_id, "report_outline_update", scope, _row_summary(before), _outline_summary(normalized))
    return normalized


def delete_outline(scope: str, project_id: str) -> bool:
    """删该 scope 行（**幂等**）+ 审计；返回删除前是否存在覆盖行。"""
    if scope not in OUTLINE_SCOPES:
        raise ValueError(f"不支持的目录范围：{scope}")
    identity = _scope_identity(scope, project_id)
    before = row("SELECT * FROM report_outlines WHERE scope=? AND project_id=?", (scope, identity))
    with connect() as conn:
        conn.execute("DELETE FROM report_outlines WHERE scope=? AND project_id=?", (scope, identity))
    _audit(project_id, "report_outline_reset", scope, _row_summary(before), {})
    return before is not None
