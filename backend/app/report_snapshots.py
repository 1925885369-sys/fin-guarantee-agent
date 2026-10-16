"""完成任务时的不可变报告快照。

契约
----
``report_snapshots`` 表一行 = 一次已完成任务（run）的报告成品。快照一旦写入
**不再更新**，与可变的 ``report_sections`` 彻底解耦：材料、复核决议、Prompt
后续怎么变，都不会改写既有快照。

三条纪律
--------
1. **一份 run 最多一份快照**：``UNIQUE(run_id)`` 是硬约束，写入走
   ``INSERT ... ON CONFLICT(run_id) DO NOTHING``，天然幂等（重复采集不覆盖）。
2. **采集与"任务完成"同事务**：``capture_snapshot(..., conn=...)`` 必须在写入
   ``runs.status='completed'`` 的同一个事务里调用。宁可任务失败重跑，也不要
   「显示已完成但历史列表里没有」。
3. **引用原文就地冻结**：历史阅读不允许再打 ``/api/citations/{id}``——那些来源
   可能已随材料变化。payload 里的 ``citations`` 是全量拷贝。

``payload_json`` 形状（``schema_version = report-snapshot-v1``）
---------------------------------------------------------------
``sections`` / ``groups`` / ``citations`` / ``records`` / ``open_items`` /
``quality`` / ``material_labels`` / ``frozen_at``。

``sections`` 与 ``GET /api/projects/{id}/report`` 同形（含 ``blocks``、
``system_tables``、标题层级字段），前端可直接复用同一套渲染组件。
"""
from __future__ import annotations

import uuid

from .database import connect, json_dump, json_load, now_iso, row, rows

SCHEMA_VERSION = "report-snapshot-v1"

# report_sections 里需要 JSON 解码的列。与 ``main.report`` 的 ``_decode``
# 保持同一份清单：两处若不一致，快照与实时接口就会返回形状不同的 section。
SECTION_JSON_FIELDS = (
    "citation_ids", "allowed_source_ids", "validation_diagnostics", "required_disclosures",
)


def _decode_sections(items: list[dict]) -> list[dict]:
    for item in items:
        for field in SECTION_JSON_FIELDS:
            if field in item:
                item[field] = json_load(item[field])
    return items


def _outline_snapshot(project_id: str) -> dict:
    """生成时生效的目录快照（P1-2/P1-5）：历史报告按生成时目录展示。

    走 ``normalized_outline``（与 ``outline_for_grouping`` 同一入口）：快照里的目录标题
    也做成品用词规范化——历史回看的章节标题与当时导出的 MD/Word 必须逐字一致（P0-2）。
    目录模块不可读（旧库/隔离测试）时返回空表——快照其余部分照常冻结，不影响旧行为。
    """
    try:
        from .report_outline import normalized_outline

        return normalized_outline(project_id) or {}
    except Exception:
        return {}


def build_snapshot_payload(project_id: str) -> dict:
    """组装一次完整报告的**可冻结**表示。

    复用与 ``GET /report`` 相同的组装路径（``section_labels`` / ``section_blocks`` /
    ``table_snapshot_status``），避免快照与实时接口两套逻辑漂移。项目还没有任何
    章节时返回 ``{"sections": []}``，由 ``capture_snapshot`` 判定为"无可留存正文"。
    """
    from .main import _project_subject  # 延迟导入避免模块循环
    from .p0 import report_quality
    from .report_blocks import section_blocks
    from .report_outline import grouped_sections, section_labels
    from .report_tables import table_snapshot_status
    from .review_lifecycle import report_state

    raw_sections = rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,))
    if not raw_sections:
        return {"sections": [], "groups": [], "citations": [], "records": [],
                "open_items": [], "quality": {}, "material_labels": {}}
    # ``exports._citation_records`` 期望 citation_ids 仍是 JSON 字符串（它自己会解），
    # 而 ``_decode_sections`` 是**就地**替换字段值——两者若共用同一批 dict，
    # 先解码就会让 _citation_records 拿到 list。所以这里必须深拷贝分家。
    sections = _decode_sections([dict(item) for item in raw_sections])

    # 生成时目录快照（P1-4）：分组/编号与标题装饰用同一份冻结目录，历史报告不可变。
    outline_snapshot = _outline_snapshot(project_id)
    labels = section_labels(sections, outline=outline_snapshot or None)
    subject = _project_subject(project_id)
    state = report_state(project_id)
    for section in sections:
        info = labels.get(section["section_key"], {})
        section["part"] = info.get("part", "")
        section["part_number"] = info.get("part_number", "")
        section["part_label"] = info.get("part_label", "")
        section["chapter_number"] = info.get("chapter_number", "")
        section["chapter_label"] = info.get("chapter_label", "")
        section["blocks"] = section_blocks(project_id, section, subject=subject)
        if section["section_key"] in {"financial", "cashflow", "debt", "rating"}:
            section["system_tables"] = table_snapshot_status(project_id, section["section_key"], subject=subject)
        section["report_revision"] = state["report_revision"]

    # 引用原文全量冻结：历史阅读不再回查 /api/citations。
    # 走 exports._citation_records 与导出口径同源（同一个"首次引用顺序"编号）。
    from .exports import _citation_records

    _numbers, records = _citation_records(project_id, raw_sections)
    # 未决事项附录：导出件会附这一节，快照必须同样留存，否则历史报告与当时
    # 导出的 Word/Markdown 内容不一致。
    from .exports import _open_items_text

    _has_open, open_lines = _open_items_text(project_id)
    from .delivery_wording import material_numbers

    return {
        "sections": sections,
        "groups": grouped_sections(sections, outline=outline_snapshot or None),
        "citations": records,
        "open_items": list(open_lines),
        "quality": report_quality(project_id),
        "material_labels": material_numbers(project_id),
        # 生成时目录快照（P1-2/P1-5）：网页/Markdown/Word/历史回看共用冻结目录。
        "outline": _outline_snapshot(project_id),
        "frozen_at": now_iso(),
    }


def capture_snapshot(project_id: str, run_id: str, *, conn=None) -> str | None:
    """采集并写入该 run 的报告快照，返回 snapshot id；无正文时返回 ``None``。

    ``conn`` 非空时必须由调用方保证「与 runs.status='completed' 同事务」。
    重复采集同 run 走 ``ON CONFLICT(run_id) DO NOTHING``，返回既有快照 id。
    """
    from .report_delivery import content_sha256
    from .review_lifecycle import report_state

    payload = build_snapshot_payload(project_id)
    if not payload.get("sections"):
        return None

    state = report_state(project_id)
    quality = payload.get("quality") or {}
    project = row("SELECT company_name FROM projects WHERE id=?", (project_id,)) or {}
    title = f"{project.get('company_name') or '未命名项目'} 尽调报告"
    body = json_dump(payload)

    def _write(connection) -> str:
        existing = connection.execute(
            "SELECT id FROM report_snapshots WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            return existing["id"]
        snapshot_id = str(uuid.uuid4())
        connection.execute(
            "INSERT INTO report_snapshots (id,project_id,run_id,report_revision,review_revision,"
            "validation_status,support_rate,title,report_level,subject,schema_version,"
            "payload_json,content_sha256,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO NOTHING",
            (snapshot_id, project_id, run_id, int(state["report_revision"]), int(state["review_revision"]),
             str(quality.get("status") or ""), float(quality.get("support_rate") or 0),
             title, str(quality.get("report_level") or ""), str(project.get("company_name") or ""),
             SCHEMA_VERSION, body, content_sha256(body), now_iso()),
        )
        stored = connection.execute("SELECT id FROM report_snapshots WHERE run_id=?", (run_id,)).fetchone()
        return stored["id"] if stored else snapshot_id

    if conn is not None:
        return _write(conn)
    with connect() as connection:
        return _write(connection)


def list_snapshots(project_id: str) -> list[dict]:
    """历史报告摘要列表（不含 payload），按生成时间倒序。

    只列 ``runs.status='completed'`` 的任务：快照本就是完成任务时写入的，
    这里再对 runs 做一次内连接，防止将来出现"任务未完成却有快照"的脏数据
    被静默展示（脏数据应当暴露在日志里，而不是列进历史）。
    """
    return rows(
        "SELECT s.id,s.run_id,s.created_at,s.title,s.report_level,s.validation_status,"
        "s.support_rate,s.report_revision,s.review_revision "
        "FROM report_snapshots s JOIN runs r ON r.id=s.run_id AND r.status='completed' "
        "WHERE s.project_id=? ORDER BY s.created_at DESC",
        (project_id,),
    )


def get_snapshot(project_id: str, snapshot_id: str) -> dict | None:
    """完整快照。``project_id`` 必须入 WHERE——否则持有项目 A 令牌可以读项目 B 的快照。"""
    item = row(
        "SELECT s.*, r.status run_status FROM report_snapshots s "
        "LEFT JOIN runs r ON r.id=s.run_id WHERE s.id=? AND s.project_id=?",
        (snapshot_id, project_id),
    )
    if not item:
        return None
    payload = json_load(item.pop("payload_json") or "{}")
    item["payload"] = payload
    return item
