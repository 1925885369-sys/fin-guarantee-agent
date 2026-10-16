"""报告采用的目录（P1-1）：把「配置目录」与「报告采用目录」分开记账。

两个概念
--------
- **配置目录**：``report_outlines`` 里的当前生效版本（项目覆盖 > 全局覆盖 > builtin），
  随时可编辑，编辑不自动改任何报告。
- **报告采用目录**：上一次**整份生成**开始时冻结的目录结构（版本、二级结构签名、
  每章标题与三级小节），随报告写入 ``report_outline_adoptions``。单章再生只允许
  更新**该章**的小节配置；**二级结构**（章节集合/顺序）变化必须整份重新生成。

判定（``regeneration_status``，报告接口消费）
-------------------------------------------
``outline_outdated`` 表示"报告采用的目录"与当前配置不一致；
``regeneration_requirement`` ∈ ``none`` / ``chapter`` / ``full_report`` / ``unknown``：

- ``full_report``：二级结构签名不一致（增删章节、排序变化）；
- ``chapter``：二级结构一致，但有章节的标题或小节配置变化——按章再生即可；
- ``none``：完全一致；
- ``unknown``：旧报告既无 adoption 记录、也无历史快照——**不得冒充一致**。

旧报告（无 adoption）优先用**历史快照**里冻结的目录做对照；没有快照才标 ``unknown``。
配置继续变更不影响旧报告的阅读（阅读走快照的冻结目录，与 adoption 无关）。
"""
from __future__ import annotations

import hashlib
import json

from .database import connect, json_dump, json_load, now_iso, row


def chapter_key_of(chapter: dict) -> str:
    """目录章节的业务键：内置章节用 ``template_key``、自定义章节用 ``chapter_id``。"""
    return str(chapter.get("template_key") or chapter.get("chapter_id") or "")


def outline_adoption_payload(outline: dict | None) -> dict:
    """把一份目录压成「报告采用目录」记录（版本 / 结构签名 / 每章条目）。

    ``structure_sha256`` 只覆盖**二级结构**（章节键 + 顺序）：它是"能否单章再生"的
    判据；章节标题与三级小节属于单章配置，变化只要求按章再生。
    """
    outline = outline or {}
    chapters: list[dict] = []
    for part in outline.get("parts") or []:
        for chapter in part.get("chapters") or []:
            chapters.append({
                "key": chapter_key_of(chapter),
                "title": str(chapter.get("title") or ""),
                "order": len(chapters) + 1,
                "subsections": [
                    {
                        "subsection_id": str(item.get("subsection_id") or ""),
                        "title": str(item.get("title") or ""),
                        "order": int(item.get("order") or index + 1),
                    }
                    for index, item in enumerate(chapter.get("subsections") or [])
                ],
            })
    structure = [{"key": item["key"], "order": item["order"]} for item in chapters]
    return {
        "outline_version": str(outline.get("outline_version") or ""),
        "structure_sha256": hashlib.sha256(
            json.dumps(structure, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "chapters": chapters,
    }


def freeze_adoption(project_id: str) -> dict:
    """整份生成**开始时**冻结当次采用的目录（读取失败返回空 payload，不阻断生成）。"""
    try:
        from .report_outline_store import effective_outline

        return outline_adoption_payload(effective_outline(project_id))
    except Exception:
        return {"outline_version": "", "structure_sha256": "", "chapters": []}


def save_adoption(project_id: str, payload: dict, *, run_id: str = "", conn=None) -> None:
    """生成**完成时**写入报告采用目录（随报告快照同事务）。"""
    statement = (
        "INSERT INTO report_outline_adoptions(project_id,outline_version,structure_sha256,chapters_json,run_id,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
        "outline_version=excluded.outline_version,structure_sha256=excluded.structure_sha256,"
        "chapters_json=excluded.chapters_json,run_id=excluded.run_id,updated_at=excluded.updated_at"
    )
    params = (project_id, str(payload.get("outline_version") or ""),
              str(payload.get("structure_sha256") or ""),
              json_dump(list(payload.get("chapters") or [])), run_id, now_iso())
    if conn is not None:
        conn.execute(statement, params)
    else:
        with connect() as owned:
            owned.execute(statement, params)


def adoption_state(project_id: str) -> dict | None:
    record = row(
        "SELECT outline_version,structure_sha256,chapters_json,run_id,updated_at "
        "FROM report_outline_adoptions WHERE project_id=?",
        (project_id,),
    )
    if not record:
        return None
    return {
        "outline_version": str(record["outline_version"] or ""),
        "structure_sha256": str(record["structure_sha256"] or ""),
        "chapters": json_load(record["chapters_json"]) or [],
        "run_id": str(record["run_id"] or ""),
        "updated_at": record["updated_at"],
    }


def update_chapter_adoption(project_id: str, chapter_key: str) -> bool:
    """单章再生后刷新**该章**的采用条目（标题与小节）；结构变化时不更新、返回 False。

    只改这一章：其余章节的采用条目保持原样——单章再生没有重新生成别的章，
    不能顺手把它们标成"已跟上新配置"。
    """
    adopted = adoption_state(project_id)
    if adopted is None:
        return False
    current = freeze_adoption(project_id)
    if str(adopted["structure_sha256"]) != str(current["structure_sha256"]):
        return False
    updated = [dict(item) for item in adopted["chapters"]]
    replacement = next((item for item in current["chapters"] if item["key"] == chapter_key), None)
    if replacement is None:
        return False
    for index, item in enumerate(updated):
        if item.get("key") == chapter_key:
            updated[index] = dict(replacement)
            break
    else:
        return False
    save_adoption(
        project_id,
        {"outline_version": adopted["outline_version"], "structure_sha256": adopted["structure_sha256"],
         "chapters": updated},
        run_id=str(adopted.get("run_id") or ""),
    )
    return True


def _chapter_differs(current: dict, adopted: dict) -> bool:
    if str(current.get("title") or "") != str(adopted.get("title") or ""):
        return True
    current_subs = [
        (str(item.get("subsection_id") or ""), str(item.get("title") or ""), int(item.get("order") or 0))
        for item in current.get("subsections") or []
    ]
    adopted_subs = [
        (str(item.get("subsection_id") or ""), str(item.get("title") or ""), int(item.get("order") or 0))
        for item in adopted.get("subsections") or []
    ]
    return current_subs != adopted_subs


def _snapshot_adoption(project_id: str) -> dict | None:
    """旧报告兜底：最近一份历史快照里冻结的目录（无则为 None）。"""
    record = row(
        "SELECT payload_json FROM report_snapshots WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
        (project_id,),
    )
    if not record:
        return None
    try:
        payload = json.loads(record["payload_json"] or "{}")
    except (ValueError, TypeError):
        return None
    outline = payload.get("outline") or {}
    if not outline.get("parts"):
        return None
    return outline_adoption_payload(outline)


def regeneration_status(project_id: str) -> dict:
    """报告接口用：``outline_outdated`` 与 ``regeneration_requirement``。

    旧报告（无 adoption）优先历史快照；既无 adoption 又无快照 → ``unknown``。
    """
    empty = {
        "outline_outdated": False, "regeneration_requirement": "none",
        "adopted_outline_version": "", "current_outline_version": "",
        "outline_evidence": "none", "outline_changed_chapters": [],
    }
    if not row("SELECT 1 FROM report_sections WHERE project_id=? LIMIT 1", (project_id,)):
        return empty  # 尚无报告：没有"报告采用的目录"可比，生成时自然用当前配置。
    try:
        from .report_outline_store import effective_outline

        current = outline_adoption_payload(effective_outline(project_id))
    except Exception:
        return {**empty, "regeneration_requirement": "unknown", "outline_evidence": "unavailable"}
    adopted = adoption_state(project_id)
    evidence = "adoption"
    if adopted is None:
        snapshot_payload = _snapshot_adoption(project_id)
        evidence = "snapshot"
        if snapshot_payload is None:
            return {**empty, "regeneration_requirement": "unknown", "outline_evidence": "none"}
        adopted = snapshot_payload
    if str(adopted["structure_sha256"]) != str(current["structure_sha256"]):
        return {
            "outline_outdated": True, "regeneration_requirement": "full_report",
            "adopted_outline_version": adopted["outline_version"],
            "current_outline_version": current["outline_version"],
            "outline_evidence": evidence,
            "outline_changed_chapters": [item["key"] for item in current["chapters"]],
        }
    adopted_by_key = {item["key"]: item for item in adopted["chapters"]}
    changed = [
        item["key"] for item in current["chapters"]
        if item["key"] not in adopted_by_key or _chapter_differs(item, adopted_by_key[item["key"]])
    ]
    requirement = "chapter" if changed else "none"
    return {
        "outline_outdated": requirement != "none", "regeneration_requirement": requirement,
        "adopted_outline_version": adopted["outline_version"],
        "current_outline_version": current["outline_version"],
        "outline_evidence": evidence,
        "outline_changed_chapters": changed,
    }
