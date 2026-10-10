"""解析出的表格的持久化。

在此之前，材料里的表格只是解析过程中的临时产物：``parse_document`` 产出
``ParsedDocument.tables``，随后被丢弃，只有文本片段进了库。于是「上传原表 → 核对」
这条链路根本没有数据可依。

上传材料与演示材料**共用这一条落库路径**，因此两者在报告与核对接口里的形态完全一致。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .config import settings
from .database import connect, now_iso, rows
from .table_extraction import TABLE_PARSER_VERSION, ExtractedCell, ExtractedTable


def _dump_cell(cell: ExtractedCell) -> dict:
    return {
        "row": cell.row, "column": cell.column, "text": cell.text,
        "page": cell.page, "bbox": list(cell.bbox) if cell.bbox else None,
    }


def _load_cell(payload: dict) -> ExtractedCell:
    bbox = payload.get("bbox")
    return ExtractedCell(
        row=int(payload.get("row", 0)), column=int(payload.get("column", 0)),
        text=str(payload.get("text") or ""), page=int(payload.get("page") or 0),
        bbox=tuple(float(value) for value in bbox) if bbox else None,
    )


def persist_tables(conn, project_id: str, document_id: str, tables: list[ExtractedTable]) -> int:
    """把一份材料的表格写入库。重复解析同一材料时先清旧再写，避免留下幽灵表。"""
    old = conn.execute(
        "SELECT * FROM document_tables WHERE project_id=? AND document_id=?", (project_id, document_id),
    ).fetchall()
    archived_at = now_iso()
    for record in old:
        snapshot = {key: record[key] for key in record.keys()}
        conn.execute(
            "INSERT INTO document_table_history(history_id,project_id,document_id,table_id,parser_version,snapshot_json,archived_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (f"dth-{uuid.uuid4().hex[:16]}", project_id, document_id, record["id"],
             record["parser_version"], json.dumps(snapshot, ensure_ascii=False), archived_at),
        )
    conn.execute("DELETE FROM document_tables WHERE project_id=? AND document_id=?", (project_id, document_id))
    now = now_iso()
    for index, table in enumerate(tables, 1):
        conn.execute(
            "INSERT INTO document_tables (id,project_id,document_id,table_key,caption,caption_origin,unit,currency,scope,"
            "start_page,end_page,continued_from,headers_json,rows_json,notes_json,extraction_method,confidence,"
            "status,parser_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"dt-{uuid.uuid4().hex[:16]}", project_id, document_id,
                table.table_key or f"table-{index:02d}", table.caption, table.caption_origin,
                table.unit, table.currency,
                table.scope, table.page, table.pages[-1] if table.pages else table.page,
                table.continued_from,
                json.dumps(list(table.columns), ensure_ascii=False),
                json.dumps([[_dump_cell(cell) for cell in row] for row in table.rows], ensure_ascii=False),
                json.dumps(list(table.notes), ensure_ascii=False),
                table.extraction_method, table.confidence, table.status,
                table.parser_version, now,
            ),
        )
    return len(tables)


def store_document_tables(project_id: str, document_id: str, tables: list[ExtractedTable]) -> int:
    with connect() as conn:
        return persist_tables(conn, project_id, document_id, tables)


def load_tables(project_id: str, document_id: str | None = None) -> list[ExtractedTable]:
    """读回某个项目（或某份材料）的表格，供报告与核对使用。"""
    if document_id:
        records = rows(
            "SELECT * FROM document_tables WHERE project_id=? AND document_id=? ORDER BY start_page, id",
            (project_id, document_id),
        )
    else:
        records = rows("SELECT * FROM document_tables WHERE project_id=? ORDER BY start_page, id", (project_id,))
    output: list[ExtractedTable] = []
    for record in records:
        table = ExtractedTable(
            table_key=str(record["table_key"] or ""),
            caption=str(record["caption"] or ""),
            # 迁移前落库的旧行没有这一列 → 回落 "none"：不假装表名有可靠来源。
            caption_origin=(
                str(record["caption_origin"] or "none")
                if "caption_origin" in record.keys() else "none"
            ),
            unit=str(record["unit"] or ""),
            currency=str(record["currency"] or "CNY"),
            scope=str(record["scope"] or ""),
            page=int(record["start_page"] or 0),
            pages=[int(record["start_page"] or 0), *(
                [int(record["end_page"])] if int(record["end_page"] or 0) != int(record["start_page"] or 0) else []
            )],
            continued_from=record["continued_from"],
            columns=[str(item) for item in json.loads(record["headers_json"] or "[]")],
            rows=[
                [_load_cell(cell) for cell in row]
                for row in json.loads(record["rows_json"] or "[]")
            ],
            notes=[str(item) for item in json.loads(record["notes_json"] or "[]")],
            extraction_method=str(record["extraction_method"] or ""),
            confidence=float(record["confidence"] or 0),
            status=str(record["status"] or "extracted"),
            document_id=str(record["document_id"]),
            storage_id=str(record["id"]),
            parser_version=str(record["parser_version"] or ""),
        )
        output.append(table)
    return output


def document_for_table(project_id: str, table_key: str) -> str:
    """表键 → 承载它的材料 ID（供核对时定位原件）。"""
    record = rows(
        "SELECT document_id FROM document_tables WHERE project_id=? AND table_key=? LIMIT 1",
        (project_id, table_key),
    )
    return str(record[0]["document_id"]) if record else ""


def table_inventory(project_id: str) -> dict:
    """材料表格清单：有多少张、多少张待人工确认、覆盖哪些表键。"""
    records = rows(
        "SELECT table_key,caption,status,start_page,end_page FROM document_tables "
        "WHERE project_id=? ORDER BY start_page, id",
        (project_id,),
    )
    return {
        "total": len(records),
        "needs_review": sum(1 for item in records if item["status"] == "needs_review"),
        "tables": [
            {
                "table_key": item["table_key"], "caption": item["caption"], "status": item["status"],
                "start_page": item["start_page"], "end_page": item["end_page"],
            }
            for item in records
        ],
    }


def reparse_stale_tables(project_id: str) -> list[dict]:
    """Reparse stored source files with a new table parser, keeping prior rows in history."""
    from .parsers import parse_document
    from .table_extraction import TABLE_PARSER_VERSION

    stale = rows(
        "SELECT DISTINCT d.id,d.filename FROM documents d JOIN document_tables t "
        "ON t.document_id=d.id AND t.project_id=d.project_id "
        "WHERE d.project_id=? AND t.parser_version!=?",
        (project_id, TABLE_PARSER_VERSION),
    )
    parsed = []
    upload_dir = settings.resolved_data_dir / "uploads" / project_id
    for document in stale:
        candidates = list(upload_dir.glob(f"{document['id']}_*")) if upload_dir.is_dir() else []
        if not candidates:
            material_root = settings.resolved_data_dir / "demo_materials"
            if material_root.is_dir():
                candidates = list(material_root.rglob(str(document["filename"])))
        if not candidates:
            raise FileNotFoundError(f"无法重新读取原始材料：{document['filename']}")
        result = parse_document(Path(candidates[0]), max_pages=settings.max_pages)
        parsed.append((str(document["id"]), result.tables))
    if parsed:
        with connect() as conn:
            for document_id, tables in parsed:
                persist_tables(conn, project_id, document_id, tables)
    return [{"document_id": document_id, "tables": len(tables)} for document_id, tables in parsed]
