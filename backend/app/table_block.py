"""``TableBlock v1``：报告表格的统一契约。

定位
----
材料渲染用的是 ``pdf_tables.FinancialTable``（管版式），报告交付用的是 ``TableBlock``
（管数据与可追溯）。两者通过本模块的构造器连接，前端、Markdown 和 Word 输出的都是
同一份 ``TableBlock``，因此三个出口不可能出现数值不一致。

三种表型与三种来源模式
----------------------
表型由 ``table_type`` 区分，列由表型动态决定：

- ``annual_series``：科目／年度列；
- ``dual_source_reconciliation``：科目／档案值／上传值／采用值／状态；
- ``conflict_comparison``：事项／期间与口径／资料及定位／披露值／状态。

来源模式由 ``origin_mode`` 区分，且**不得互相冒充**：

- ``profile_facts``：冻结主体档案里只有事实，没有原表，生成的只能是事实汇总；
- ``uploaded_table``：来自上传 PDF 的原表，逐格可定位到页码与坐标；
- ``merged``：档案与上传材料核对后的结果。

安全不变量
----------
1. 每个业务数据单元格都必须可追溯（有来源，或明确标注为人工录入并给出依据）；
2. 同一主体、同一期间、同一口径下的多个披露值**必须全部保留**，不得只留一个；
3. 数值冲突时 ``adopted`` 必须为空并标注待人工确认，**禁止自动择一**。
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable


TABLE_BLOCK_VERSION = "table-block-v1"

TABLE_TYPES = ("annual_series", "dual_source_reconciliation", "conflict_comparison")
ORIGIN_MODES = ("profile_facts", "uploaded_table", "merged")
DISPLAY_MODES = ("report", "appendix", "evidence")

# 单元格取值状态。``adopted`` 是核对表专用，与 ``ok`` 分开：
# 「这个值是对的」和「这个值是核对后采用的」不是同一件事。
CELL_STATUSES = (
    "ok", "zero", "not_applicable", "blank", "text",
    "single_source", "conflict", "adopted", "needs_review",
)

# ``period`` 是描述性期间列（冲突表的「期间与口径」），``period_value`` 才是
# 年度表里承载数据的年度列——两者都可叫「期间」，但一个要追溯、一个不要。
COLUMN_ROLES = (
    "subject", "period", "period_value", "profile_value", "uploaded_value", "adopted_value",
    "status", "disclosure", "reference", "detail",
)

SUBJECT_COLUMN = "科目"
# 由事实汇总而成的表，必须在表注里自证这一点——「不得写成原表提取」是契约要求，
# 靠每次调用自觉加注会漏，因此在构造器里强制。
PROFILE_FACTS_DISCLAIMER = "注：本表为系统依据已核验事实生成的口径汇总，不是从原始报表逐格提取。"
ANNUAL_COLUMNS = ("profile_value", "uploaded_value", "adopted_value", "status")


@dataclass(frozen=True)
class TableColumn:
    key: str
    label: str
    role: str
    unit: str = ""

    def __post_init__(self) -> None:
        if self.role not in COLUMN_ROLES:
            raise ValueError(f"未知的列角色：{self.role}")


@dataclass(frozen=True)
class TableCell:
    """一个业务数据单元格。

    ``raw`` 是原始字符串，``normalized`` 是规范化后的数值字符串（``None`` 表示无数据）。
    两者都保留：核对要以原文为准，计算要用规范化值。
    """

    raw: str = ""
    normalized: str | None = None
    unit: str = ""
    status: str = "ok"
    origin_type: str = ""  # profile_fact | uploaded_cell | manual | derived
    fact_key: str | None = None
    metric_key: str | None = None
    source_ids: tuple[str, ...] = ()
    locator: dict = field(default_factory=dict)
    adoption_basis: str = ""
    manual_note: str = ""

    def __post_init__(self) -> None:
        if self.status not in CELL_STATUSES:
            raise ValueError(f"未知的单元格状态：{self.status}")

    @property
    def traceable(self) -> bool:
        """这个单元格是否有可查的来路。

        ``conflict`` 格本身不需要来源——它记录的就是「两个来源不一致」，
        来源挂在同一行的两个披露值上。``blank``/``not_applicable`` 无数据可追。
        人工录入必须有依据，否则只是一个没有出处的数字。
        """
        if self.origin_type == "manual":
            return bool(self.adoption_basis or self.manual_note)
        if self.status in {"blank", "not_applicable", "conflict"}:
            return True
        return bool(self.source_ids or self.locator)

    def to_dict(self) -> dict:
        return {
            "raw": self.raw, "normalized": self.normalized, "unit": self.unit,
            "status": self.status, "origin_type": self.origin_type,
            "fact_key": self.fact_key, "metric_key": self.metric_key,
            "source_ids": list(self.source_ids), "locator": self.locator,
            "adoption_basis": self.adoption_basis, "manual_note": self.manual_note,
        }


@dataclass(frozen=True)
class TableRow:
    row_key: str
    cells: tuple[TableCell, ...]
    kind: str = "normal"  # normal | group | subtotal

    def cell(self, index: int) -> TableCell:
        return self.cells[index] if index < len(self.cells) else TableCell()

    def to_dict(self) -> dict:
        return {"row_key": self.row_key, "kind": self.kind, "cells": [cell.to_dict() for cell in self.cells]}


@dataclass(frozen=True)
class TableBlock:
    table_id: str
    table_type: str
    title: str
    subject: str
    scope: str
    currency: str
    unit: str
    columns: tuple[TableColumn, ...]
    rows: tuple[TableRow, ...]
    display_mode: str = "report"
    origin_mode: str = "profile_facts"
    source_refs: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    version: str = TABLE_BLOCK_VERSION
    validation_status: str = "pending"
    validation_summary: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.table_type not in TABLE_TYPES:
            raise ValueError(f"未知的表型：{self.table_type}")
        if self.origin_mode not in ORIGIN_MODES:
            raise ValueError(f"未知的来源模式：{self.origin_mode}")
        if self.display_mode not in DISPLAY_MODES:
            raise ValueError(f"未知的展示模式：{self.display_mode}")
        if self.currency != "CNY":
            raise ValueError("本系统只处理人民币口径材料")
        for row in self.rows:
            if len(row.cells) != len(self.columns):
                raise ValueError(f"第 {row.row_key} 行的单元格数与列数不一致")

    @property
    def period_columns(self) -> tuple[TableColumn, ...]:
        return tuple(column for column in self.columns if column.role in {"period", "period_value"})

    def cells(self) -> list[TableCell]:
        return [cell for row in self.rows for cell in row.cells]

    def content_hash(self) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "version": self.version, "table_id": self.table_id, "table_type": self.table_type,
            "title": self.title, "display_mode": self.display_mode, "origin_mode": self.origin_mode,
            "subject": self.subject, "scope": self.scope, "currency": self.currency, "unit": self.unit,
            "columns": [{"key": c.key, "label": c.label, "role": c.role, "unit": c.unit} for c in self.columns],
            "rows": [row.to_dict() for row in self.rows],
            "source_refs": list(self.source_refs), "notes": list(self.notes),
            "validation_status": self.validation_status, "validation_summary": self.validation_summary,
        }


def table_block_from_dict(payload: dict) -> TableBlock:
    columns = tuple(TableColumn(**item) for item in payload.get("columns", []))
    rows = tuple(TableRow(
        row_key=str(item.get("row_key") or ""), kind=str(item.get("kind") or "normal"),
        cells=tuple(TableCell(
            raw=str(cell.get("raw") or ""), normalized=cell.get("normalized"),
            unit=str(cell.get("unit") or ""), status=str(cell.get("status") or "ok"),
            origin_type=str(cell.get("origin_type") or ""), fact_key=cell.get("fact_key"),
            metric_key=cell.get("metric_key"), source_ids=tuple(cell.get("source_ids") or []),
            locator=dict(cell.get("locator") or {}), adoption_basis=str(cell.get("adoption_basis") or ""),
            manual_note=str(cell.get("manual_note") or ""),
        ) for cell in item.get("cells", [])),
    ) for item in payload.get("rows", []))
    return TableBlock(
        table_id=str(payload["table_id"]), table_type=str(payload["table_type"]),
        title=str(payload["title"]), subject=str(payload.get("subject") or ""),
        scope=str(payload.get("scope") or ""), currency=str(payload.get("currency") or "CNY"),
        unit=str(payload.get("unit") or ""), columns=columns, rows=rows,
        display_mode=str(payload.get("display_mode") or "report"),
        origin_mode=str(payload.get("origin_mode") or "profile_facts"),
        source_refs=tuple(payload.get("source_refs") or []), notes=tuple(payload.get("notes") or []),
        version=str(payload.get("version") or TABLE_BLOCK_VERSION),
        validation_status=str(payload.get("validation_status") or "pending"),
        validation_summary=dict(payload.get("validation_summary") or {}),
    )


# ---------------------------------------------------------------------------
# 校验：不变量在这里强制成立，且**失败要能阻止导出**，不能只是打个标记
# ---------------------------------------------------------------------------


# 只有承载业务数据的列才需要来源。科目列是行标签、状态列是判读结果、
# 期间与资料列是定位信息——它们不是「数据」，追它们是形式主义。
VALUE_ROLES = ("period_value", "profile_value", "uploaded_value", "adopted_value", "disclosure")


def validate_table_block(block: TableBlock) -> tuple[str, dict]:
    """校验一个 ``TableBlock``，返回 ``(状态, 摘要)``。

    ``blocked`` 表示这份表不得进入最终版交付：有业务数据单元格无法追溯，
    或有冲突未被并列保留（例如被自动择一采信）。
    """
    value_indexes = [index for index, column in enumerate(block.columns) if column.role in VALUE_ROLES]

    def value_cells():
        for row in block.rows:
            for index in value_indexes:
                yield row.row_key, row.cell(index)

    untraceable = [key for key, cell in value_cells() if not cell.traceable]
    missing_basis = [
        key for key, cell in value_cells() if cell.status == "adopted" and not cell.adoption_basis
    ]
    conflicts = [key for key, cell in value_cells() if cell.status == "conflict"]
    auto_adopted = [
        key for key, cell in value_cells()
        if cell.status == "conflict" and cell.normalized not in {None, ""}
    ]
    # 格级待核验必须**汇总**到整表：修订稿要求"将格级 needs_review 汇总为整表待核验并
    # 阻止最终交付"，不能只在一行摘要里写个标记就当作已处理。
    cell_review = [key for key, cell in value_cells() if cell.status == "needs_review"]
    unattributed = []
    if block.origin_mode == "uploaded_table":
        # 上传材料的原表要求「页码与坐标」两者齐全——契约里就是这么写的。
        # 只有页码没有坐标，说明线框恢复失败，属于「无法稳定恢复」，不得按可信数据用。
        unattributed = [
            key for key, cell in value_cells()
            if cell.status not in {"blank", "not_applicable", "conflict"}
            and not (cell.locator.get("page") and cell.locator.get("bbox"))
        ]
    summary = {
        "cells": len(block.cells()),
        "value_cells": len(value_indexes) * len(block.rows),
        "untraceable": sorted(set(untraceable)),
        "missing_adoption_basis": sorted(set(missing_basis)),
        "conflict_rows": sorted(set(conflicts)),
        "auto_adopted_conflicts": sorted(set(auto_adopted)),
        "cells_needing_review": sorted(set(cell_review)),
        "unattributed_cells": sorted(set(unattributed)),
        "origin_mode": block.origin_mode,
        "table_type": block.table_type,
    }
    if untraceable or missing_basis or auto_adopted or unattributed:
        return "blocked", summary
    if cell_review:
        # 待核验与"不得交付"是两件事：这里给出 needs_review，调用方按 `!= "passed"`
        # 判定即阻断最终交付（``report_tables.blocked_reason`` 就是这么判的）。
        return "needs_review", summary
    return "passed", summary


def validated(block: TableBlock) -> TableBlock:
    """返回带校验结果的新 ``TableBlock``（本身不可变）。"""
    status, summary = validate_table_block(block)
    return TableBlock(**{**block.__dict__, "validation_status": status, "validation_summary": summary})


def conflicts_are_preserved(row_cells: Iterable[TableCell]) -> bool:
    """冲突行必须保留全部披露值，且采用值为空。"""
    cells = list(row_cells)
    disclosed = [cell for cell in cells if cell.status in {"conflict", "single_source"}]
    if len(disclosed) < 2:
        return True
    adopted = next((cell for cell in cells if cell.status == "adopted"), None)
    return adopted is None or not adopted.normalized


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------


def annual_series_block(
    *, table_id: str, title: str, subject: str, scope: str, unit: str,
    period_labels: Iterable[str], rows: Iterable[tuple[str, tuple[TableCell, ...]]],
    source_refs: Iterable[str] = (), notes: Iterable[str] = (),
    origin_mode: str = "profile_facts",
) -> TableBlock:
    """年度表：科目／年度列。"""
    labels = list(period_labels)
    columns = (TableColumn(key="subject", label=SUBJECT_COLUMN, role="subject"),) + tuple(
        TableColumn(key=f"period_{index}", label=label, role="period_value", unit=unit)
        for index, label in enumerate(labels)
    )
    built = tuple(
        TableRow(row_key=label, cells=(TableCell(raw=label, status="text", origin_type="derived"), *cells))
        for label, cells in rows
    )
    note_list = [note for note in notes if note]
    if origin_mode == "profile_facts" and not any("不是从原始报表逐格提取" in note for note in note_list):
        note_list.append(PROFILE_FACTS_DISCLAIMER)
    return validated(TableBlock(
        table_id=table_id, table_type="annual_series", title=title, subject=subject, scope=scope,
        currency="CNY", unit=unit, columns=columns, rows=built, origin_mode=origin_mode,
        source_refs=tuple(source_refs), notes=tuple(note_list),
    ))


def cell_from_text(raw: str, *, unit: str = "", origin_type: str = "derived",
                   source_ids: Iterable[str] = (), locator: dict | None = None,
                   fact_key: str | None = None) -> TableCell:
    """把单元格原文转成 ``TableCell``，状态由披露口径决定（``-`` 是零、``—`` 是无数据）。"""
    from .table_extraction import normalize_cell

    kind, value = normalize_cell(raw)
    status = {"blank": "blank", "not_applicable": "not_applicable", "zero": "zero",
              "number": "ok", "text": "text"}[kind]
    return TableCell(
        raw=raw, normalized=(str(value) if value is not None else None), unit=unit,
        status=status, origin_type=origin_type, fact_key=fact_key,
        source_ids=tuple(source_ids), locator=locator or {},
    )


def profile_facts_block(
    *, table_id: str, title: str, subject: str, unit: str,
    facts: Iterable[dict], source_refs: Iterable[str] = (),
) -> TableBlock:
    """冻结档案只有事实、没有原表时生成的**事实汇总**。

    ``origin_mode`` 固定为 ``profile_facts``，标题也必须说明这是汇总——
    不得让下游把它当成「从原始表提取」。
    """
    grouped: dict[str, dict[str, TableCell]] = {}
    periods: list[str] = []
    for fact in facts:
        period = str(fact.get("period") or "")
        if period and period not in periods:
            periods.append(period)
        grouped.setdefault(str(fact.get("name") or fact.get("fact_key") or ""), {})[period] = cell_from_text(
            fact.get("raw_value") if fact.get("raw_value") not in (None, "") else str(fact.get("value") or ""),
            unit=str(fact.get("unit") or unit), origin_type="profile_fact",
            source_ids=fact.get("source_ids") or (), fact_key=fact.get("fact_key"),
        )
    rows = [
        (label, tuple(values.get(period, TableCell(raw="—", status="not_applicable")) for period in periods))
        for label, values in grouped.items()
    ]
    return annual_series_block(
        table_id=table_id, title=title, subject=subject, scope="主体档案",
        unit=unit, period_labels=periods, rows=rows,
        source_refs=source_refs, origin_mode="profile_facts",
        notes=("注：本表为冻结主体档案的事实汇总，不是从原始报表逐格提取。",),
    )


ADOPTION_BASIS = {
    "agreed": "两来源取值一致",
    "profile_only": "仅档案有值，上传材料未覆盖该科目",
    "uploaded_only": "仅上传材料有值，档案无对应事实",
    "conflict": "两来源取值不同，并列保留待人工确认",
}


def reconciliation_block(
    *, table_id: str, title: str, subject: str, scope: str, period: str,
    profile_cells: dict[str, TableCell], uploaded_cells: dict[str, TableCell],
    source_refs: Iterable[str] = (), notes: Iterable[str] = (),
    left_label: str = "档案值", right_label: str = "上传值",
) -> TableBlock:
    """双来源核对表：科目／档案值／上传值／采用值／状态。

    安全不变量在这里落地：**两来源取值不同时，采用值必须为空**，不得自动择一；
    完整的两个披露值都留在表里，状态标为冲突。
    """
    columns = (
        TableColumn(key="subject", label=SUBJECT_COLUMN, role="subject"),
        TableColumn(key="profile", label=left_label, role="profile_value"),
        TableColumn(key="uploaded", label=right_label, role="uploaded_value"),
        TableColumn(key="adopted", label="采用值", role="adopted_value"),
        TableColumn(key="status", label="状态", role="status"),
    )
    labels = list(dict.fromkeys([*profile_cells, *uploaded_cells]))
    rows: list[TableRow] = []
    for label in labels:
        profile = profile_cells.get(label) or TableCell(raw="—", status="not_applicable")
        uploaded = uploaded_cells.get(label) or TableCell(raw="—", status="not_applicable")
        has_profile = profile.status not in {"not_applicable", "blank"}
        has_upload = uploaded.status not in {"not_applicable", "blank"}
        if has_profile and has_upload and profile.normalized != uploaded.normalized:
            adopted = TableCell(status="conflict", adoption_basis=ADOPTION_BASIS["conflict"])
            status = "conflict"
        elif has_profile and has_upload:
            adopted = TableCell(
                raw=profile.raw, normalized=profile.normalized, unit=profile.unit,
                status="adopted", origin_type="derived",
                source_ids=tuple(dict.fromkeys([*profile.source_ids, *uploaded.source_ids])),
                adoption_basis=ADOPTION_BASIS["agreed"],
            )
            status = "一致"
        elif has_upload:
            adopted = TableCell(
                raw=uploaded.raw, normalized=uploaded.normalized, unit=uploaded.unit,
                status="adopted", origin_type="uploaded_cell",
                source_ids=uploaded.source_ids, locator=uploaded.locator,
                adoption_basis=ADOPTION_BASIS["uploaded_only"],
            )
            status = f"单来源（{right_label[:2]}）"
        elif has_profile:
            adopted = TableCell(
                raw=profile.raw, normalized=profile.normalized, unit=profile.unit,
                status="adopted", origin_type="profile_fact",
                source_ids=profile.source_ids, adoption_basis=ADOPTION_BASIS["profile_only"],
            )
            status = f"单来源（{left_label[:2]}）"
        else:
            adopted = TableCell(status="not_applicable")
            status = "无数据"
        rows.append(TableRow(
            row_key=label,
            cells=(
                TableCell(raw=label, status="text", origin_type="derived"),
                profile, uploaded, adopted,
                TableCell(raw=status, status="text", origin_type="derived"),
            ),
        ))
    return validated(TableBlock(
        table_id=table_id, table_type="dual_source_reconciliation", title=title,
        subject=subject, scope=f"{scope}／{period}", currency="CNY", unit="",
        columns=columns, rows=tuple(rows), origin_mode="merged",
        source_refs=tuple(source_refs), notes=tuple(notes),
    ))


def block_from_financial_table(
    table, *, table_id: str, subject: str, scope: str = "合并口径", origin_mode: str = "profile_facts",
    source_refs: Iterable[str] = (), row_fact_keys: dict[str, str] | None = None,
) -> TableBlock:
    """把渲染器的声明表（``pdf_tables.FinancialTable``）转成 ``annual_series`` 契约。

    渲染与报告由此共用一份数据：版式变化不会改动报告里的数值，反之亦然。
    """
    keys = row_fact_keys or {}
    period_labels = [column for column in table.columns[1:]]
    rows = []
    for row in table.rows:
        label = row[0].text.replace("　", "").strip()
        cells = tuple(
            cell_from_text(cell.text, unit=table.unit, origin_type="derived",
                           source_ids=source_refs, fact_key=keys.get(label))
            for cell in row[1:]
        )
        rows.append((label, cells))
    return annual_series_block(
        table_id=table_id, title=table.caption, subject=subject, scope=scope, unit=table.unit,
        period_labels=period_labels, rows=rows, source_refs=source_refs,
        notes=table.notes, origin_mode=origin_mode,
    )


def block_from_extracted(
    table, *, table_id: str, subject: str, scope: str = "上传材料", document_id: str = "",
    source_refs: Iterable[str] = (),
    page_source_refs: dict[int, tuple[str, ...]] | None = None,
) -> TableBlock:
    """把解析器的抽取表（``table_extraction.ExtractedTable``）转成 ``uploaded_table`` 契约。

    每个单元格带上页码与坐标：上传材料的原表必须逐格可定位，否则不得进入核对。
    """
    headers, body, units = _extracted_headers(table)
    source_units = list(units)
    for index, unit in enumerate(units):
        if index > 0 and unit in {"元", "万元", "亿元"}:
            headers[index] = f"{headers[index]}（万元）"
            units[index] = "万元"
    columns = tuple(
        TableColumn(key=f"column_{index}", label=label,
                    role=business_column_role(label, index), unit=units[index])
        for index, label in enumerate(headers)
    )
    rows = []
    unresolved_units: list[str] = []
    for row_index, row in enumerate(body):
        if not row:
            continue
        label = row[0].text.strip()
        row_unit = _explicit_unit(label)
        cells = []
        for index, cell in enumerate(row):
            locator = {"page": cell.page, "bbox": [round(value, 2) for value in cell.bbox]} if cell.bbox else {"page": cell.page}
            if document_id:
                locator["document_id"] = document_id
            locator["table_key"] = table.table_key
            locator["row"] = cell.row
            locator["column"] = cell.column
            # 单位按「单元格 → 行科目 → 列 → 整表」依次确定：单元格自己写明的单位最优先，
            # 其次才是行科目标注、列标注与整表标注。混合单位只有在所属业务格明确时才换算。
            is_value_column = columns[index].role in VALUE_COLUMN_ROLES
            column_unit = source_units[index] if is_value_column else _explicit_unit(table.unit)
            source_unit = _explicit_unit(cell.text) or row_unit or column_unit
            display_raw, display_unit = cell.text, source_unit
            if (index > 0 and is_value_column and not source_unit and
                    re.fullmatch(r"[-+]?[\d,，]+(?:\.\d+)?", cell.text.strip())):
                unresolved_units.append(f"{row_index}:{index}")
            if source_unit in {"元", "万元", "亿元"}:
                locator["original_raw"] = cell.text
                locator["original_unit"] = source_unit
                if is_value_column:
                    try:
                        # 单元格自己可能带单位（"-1,234元"）或千分位：先剥成纯数字再换算。
                        # 否则 Decimal 会因单位字符抛错、被 except 静默吞掉，换算失效，
                        # 同一张表里就会混用"元"与"万元"两种单位。负号必须保留。
                        numeric = Decimal(re.sub(r"[^0-9.+-]", "", cell.text))
                        factor = {"元": Decimal("0.0001"), "万元": Decimal(1), "亿元": Decimal("10000")}[source_unit]
                        display_raw = _decimal_display(numeric * factor)
                        display_unit = "万元"
                    except (InvalidOperation, ValueError):
                        pass
            if display_raw == "未披露":
                built = TableCell(raw=display_raw, unit=display_unit, status="not_applicable",
                                  origin_type="uploaded_cell", locator=locator)
            else:
                built = cell_from_text(display_raw, unit=display_unit, origin_type="uploaded_cell",
                                       source_ids=(page_source_refs or {}).get(cell.page, tuple(source_refs)), locator=locator)
            cells.append(built)
        if len(cells) != len(columns):
            raise ValueError("原表行宽与表头不一致，需人工核验")
        kind = "group" if all(cell.status == "blank" for cell in cells[1:]) else (
            "subtotal" if re.search(r"合计|总计|净额|净利润|利润总额", label) else "normal"
        )
        row_key = label if not any(item.row_key == label for item in rows) else f"{label}:{row_index}"
        rows.append(TableRow(row_key=row_key, cells=tuple(cells), kind=kind))
    block = validated(TableBlock(
        table_id=table_id, table_type="annual_series", title=table.caption or f"第 {table.page} 页表格",
        subject=subject, scope=scope, currency=table.currency,
        unit="万元" if any(unit == "万元" for unit in units[1:]) else table.unit,
        columns=columns, rows=tuple(rows), source_refs=tuple(source_refs),
        notes=tuple(table.notes), origin_mode="uploaded_table",
    ))
    if table.status == "needs_review" or unresolved_units or any(label == "列待核验" for label in headers[1:]):
        # 抽取本身不可靠时整表标记待确认，不允许下游按可信数据使用。
        _, summary = validate_table_block(block)
        summary["extraction_status"] = table.status
        summary["extraction_confidence"] = table.confidence
        if unresolved_units:
            summary["unit_ambiguities"] = unresolved_units
        if any(label == "列待核验" for label in headers[1:]):
            summary["period_ambiguity"] = True
        return TableBlock(**{**block.__dict__, "validation_status": "needs_review", "validation_summary": summary})
    return block


def _explicit_unit(label: str) -> str:
    units = re.findall(r"亿元|万元|元|%|％|倍|天|个月|人|笔", label)
    return units[0].replace("％", "%") if len(set(units)) == 1 else ""


def _decimal_display(value: Decimal) -> str:
    text = format(value, ",f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# 列的业务角色：单位换算必须先判断"这一列业务上是不是金额列"。
# 序号、期间名词、名称/说明列里的数字不是金额——一旦套用整表单位，序号 "1" 会被
# 换算成 0.0001 万元、"2025" 会被当作金额乘 10000。这是"单位误换算"的直接来源。
#
# 判定顺序很关键：**年份开头的列是数据列**（"2025 年末"、"2025 年末 · 金额（万元）"
# 都是金额列，单位来自列名或整表单位），只有"期间/年度/年份/时点"这类**名词**列才是
# 期间列。若把年份开头的列一并排除，财务报表最常见的"项目 + 各年末金额"布局会丢掉
# 全部单位（既有测试 test_multilevel_headers_preserve_amount_percentage_and_precision
# 正是这个布局）。
BUSINESS_COLUMN_ROLES = (
    ("reference", r"序号|编号|^序$"),
    ("disclosure", r"名称|机构|银行|说明|备注|摘要|资料|来源|索引"),
)
YEAR_HEADER_RE = re.compile(r"^\s*(?:19|20)\d{2}")
PERIOD_NOUN_RE = re.compile(r"期间|年份|年度|时点|日期|^期限$|^时间$")
# 只有这些角色参与金额换算。
VALUE_COLUMN_ROLES = ("period_value", "profile_value", "uploaded_value", "adopted_value")
MONEY_UNITS = ("元", "万元", "亿元")


def business_column_role(label: str, index: int) -> str:
    """列的业务角色。第 0 列恒为科目列；其余按表头判定，默认按数值/金额列处理。

    判不出来时保守地当数值列（保持既有换算行为），只有明确是序号/期间名词/说明列
    才排除——宁可漏排除，也不把真正的金额列排除掉导致"该换算的没换算"。
    """
    if index == 0:
        return "subject"
    text = re.sub(r"\s+", "", label or "")
    if not text or text == "列待核验":
        return "period_value"
    if YEAR_HEADER_RE.match(text):
        return "period_value"
    for role, pattern in BUSINESS_COLUMN_ROLES:
        if re.search(pattern, text):
            return role
    if PERIOD_NOUN_RE.search(text):
        return "period"
    return "period_value"


# 子列表头可接受的字面：金额/占比一类的语义标签、年度标签、以及纯单位标签。
_SUBHEADER_LABEL_RE = re.compile(
    r"金额|余额|占比|比例|发行前|发行后|项目|(?:19|20)\d{2}\s*年(?:末|度)?"
    r"|[（(]?(?:亿元|万元|元|%|％|倍|天)[）)]?"
)
# 相邻两列的 x 区间之间可容许的间隙（pt）。真实表相邻列共用边框、间隙≈0；
# 超过它说明两列之间有不属于同一父表头的断裂，父表头的跨越到此为止。
_COLUMN_GAP_TOLERANCE = 5.0


def _is_numeric_cell(text: str) -> bool:
    from .table_extraction import normalize_cell

    return normalize_cell(text)[0] in {"number", "zero"}


def _column_intervals(rows, column_count: int) -> list[tuple[float, float] | None]:
    """每一列在页面上的 x 区间 ``(x0, x1)``；取该列第一个带坐标的单元格。

    合并单元格覆盖到的槽位 bbox 为 ``None``（PyMuPDF 只给合并原点一个跨列 bbox），
    因此逐列取第一个非空坐标即可还原列边界，而不会被合并格带偏。
    """
    intervals: list[tuple[float, float] | None] = [None] * column_count
    for row in rows:
        for index, cell in enumerate(row):
            if index >= column_count:
                break
            if intervals[index] is None and cell.bbox and len(cell.bbox) == 4:
                intervals[index] = (float(cell.bbox[0]), float(cell.bbox[2]))
    return intervals


def _usable_geometry(intervals) -> bool:
    """几何是否足以判定列区间。

    要求每列都有坐标，且各列左边界严格递增——否则（bbox 全为 ``None`` 的纯文本来源，
    或测试夹具里所有列共用同一 x 区间）无法区分列，只能保守回退。
    """
    if any(interval is None for interval in intervals):
        return False
    starts = [interval[0] for interval in intervals]
    return all(starts[index] < starts[index + 1] for index in range(len(starts) - 1))


def _columns_adjacent(left, right, tolerance: float = _COLUMN_GAP_TOLERANCE) -> bool:
    """两列在 x 上是否相邻（间隙不超过容差）。"""
    if left is None or right is None:
        return False
    return right[0] - left[1] <= tolerance


def _inherit_parent_headers(headers: list[str], subheaders: list[list[str]], intervals) -> None:
    """把父表头标签继承给子列——只沿它**实际跨越**的列区间，而不是一路左填。

    原实现把「上一个非空表头」当作父级，遇到空列就继承，完全不看父单元格到底跨了哪几列，
    所以会跨父级、跨语义把标签涂到不相干的列上（例如把「2025 年末」的左填一直推到
    与它无关的后续分组）。这里按原表几何关系恢复：

    - 父表头起始于它所在列，沿**水平相邻**的列逐列向右继承；
    - 遇到下一个非空表头（另一个父级）即停；
    - 遇到几何断裂（相邻列 x 区间之间有明显间隙）即停——那道断裂就是父表头跨越的边界。

    几何不可用（bbox 缺失或所有列共用同一区间）时无法判定跨越范围，保守回退到原有行为。
    """
    if not subheaders:
        return
    if _usable_geometry(intervals):
        for parent_index in range(1, len(headers)):
            label = headers[parent_index]
            if not label:
                continue
            for index in range(parent_index + 1, len(headers)):
                if headers[index]:
                    break  # 下一个父表头开始了，本父表头的区间到此为止
                if not any(row[index] for row in subheaders):
                    break  # 该列没有子标签，父表头不必覆盖它
                if not _columns_adjacent(intervals[index - 1], intervals[index]):
                    break  # 几何断裂：这一列不在父表头跨越的区间内，禁止跨父级左填
                headers[index] = label
        return
    parent = ""
    for index in range(1, len(headers)):
        if headers[index]:
            parent = headers[index]
        elif any(row[index] for row in subheaders):
            headers[index] = parent


def _extracted_headers(table) -> tuple[list[str], list, list[str]]:
    """Flatten only explicit subheaders; keep original data columns and cell positions."""
    headers = [re.sub(r"\s+", " ", label).strip() for label in table.columns]
    body = list(table.rows)
    # 列 x 区间必须在剥离子表头之前取——子表头行本身带坐标，是列边界的重要来源。
    intervals = _column_intervals(body, len(headers))
    subheaders = []
    while body:
        texts = [cell.text.strip() for cell in body[0]]
        if len(texts) != len(headers) or texts[0] not in {"", "项目", "科目"}:
            break
        meaningful = [text for text in texts[1:] if text]
        if not meaningful or not all(_SUBHEADER_LABEL_RE.fullmatch(text) for text in meaningful):
            break
        # 守卫：子表头行除首列外不应出现可归一化为数值的单元格。续页首行常常就是业务数据
        #（表头留在上一页），一旦把它当成子表头剥离，整条业务行就永久丢失、也不可追溯。
        if any(_is_numeric_cell(text) for text in texts[1:]):
            break
        subheaders.append(texts)
        body.pop(0)
    # 父表头只作用于它实际跨越的列（几何关系），禁止跨父级随意左填。
    _inherit_parent_headers(headers, subheaders, intervals)
    labels, units = [], []
    for index, header in enumerate(headers):
        parts = list(dict.fromkeys(part for part in [header, *(row[index] for row in subheaders)] if part))
        label = " · ".join(parts) or ("项目" if index == 0 else "列待核验")
        unit = _explicit_unit(label)
        if not unit and re.search(r"占比|比例", label):
            unit = "%" if "%" in table.unit or "％" in table.unit else ""
        if not unit and "金额" in label:
            monetary = re.findall(r"亿元|万元|元", table.unit)
            unit = monetary[0] if len(set(monetary)) == 1 else ""
        # 整表单位只回退给数值/金额列：序号、期间、名称/说明列不参与金额换算。
        if business_column_role(label, index) in VALUE_COLUMN_ROLES:
            unit = unit or _explicit_unit(table.unit)
            if "金额" in label and not unit:
                table_money_units = re.findall(r"亿元|万元|元", table.unit)
                if len(set(table_money_units)) == 1:
                    unit = table_money_units[0]
        units.append(unit)
        labels.append(label)
    years = [re.search(r"(?:19|20)\d{2}", label) for label in labels[1:]]
    if years and all(years):
        ordered = sorted(range(1, len(labels)), key=lambda index: int(years[index - 1].group()), reverse=True)
        if ordered != list(range(1, len(labels))):
            permutation = [0, *ordered]
            labels = [labels[index] for index in permutation]
            units = [units[index] for index in permutation]
            body = [[row[index] for index in permutation] for row in body]
    return labels, body, units


def conflict_comparison_block(
    *, table_id: str, title: str, subject: str,
    conflicts: Iterable[dict], source_refs: Iterable[str] = (), notes: Iterable[str] = (),
) -> TableBlock:
    """冲突对照表：事项／期间与口径／资料及定位／披露值／状态。

    ``conflicts`` 每项形如 ``{"fact_key", "period", "scope", "assertions": [{"value","raw","source_ids","locator"}]}``。
    同一事项的全部披露值都保留在「披露值」列里，状态恒为冲突——本表存在的意义就是
    让人看到分歧，因此不设「采用值」列。
    """
    columns = (
        TableColumn(key="item", label="事项", role="subject"),
        TableColumn(key="period", label="期间与口径", role="period"),
        TableColumn(key="reference", label="资料及定位", role="reference"),
        TableColumn(key="disclosure", label="披露值", role="disclosure"),
        TableColumn(key="status", label="状态", role="status"),
    )
    rows: list[TableRow] = []
    for conflict in conflicts:
        # 事项列用业务标签，不用字段键——成品里不得出现内部字段名。
        item = str(conflict.get("label") or conflict.get("fact_key") or "")
        period = str(conflict.get("period") or "")
        scope = str(conflict.get("scope") or "")
        disclosures = []
        references = []
        for assertion in conflict.get("assertions", []):
            value = assertion.get("raw") or assertion.get("value")
            disclosures.append(str(value))
            locator = assertion.get("locator") or {}
            references.append(str(locator.get("document_id") or locator.get("page") or ""))
        rows.append(TableRow(
            row_key=f"{item}:{period}",
            cells=(
                TableCell(raw=item, status="text", origin_type="derived"),
                TableCell(raw=f"{period}／{scope}" if scope else period, status="text", origin_type="derived"),
                TableCell(raw="；".join(reference for reference in references if reference), status="text", origin_type="derived",
                          source_ids=tuple(dict.fromkeys(
                              source for assertion in conflict.get("assertions", [])
                              for source in assertion.get("source_ids", [])
                          ))),
                TableCell(raw=" ／ ".join(disclosures), status="conflict", origin_type="derived"),
                TableCell(raw="冲突", status="text", origin_type="derived"),
            ),
        ))
    return validated(TableBlock(
        table_id=table_id, table_type="conflict_comparison", title=title, subject=subject,
        scope="冲突对照", currency="CNY", unit="", columns=columns, rows=tuple(rows),
        origin_mode="profile_facts", source_refs=tuple(source_refs), notes=tuple(notes),
    ))
