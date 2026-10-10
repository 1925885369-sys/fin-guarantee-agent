"""Complete source tables, units, scopes and report delivery regressions."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app import llm, report_source_tables, report_tables
from backend.app.chapter_guard import count_report_chars
from backend.app.database import connect, init_db, rows
from backend.app.main import app
from backend.app.report_blocks import section_blocks
from backend.app.report_tables import chapter_table_blocks, system_table_markdown
from backend.app.table_block import block_from_extracted
from backend.app.table_extraction import ExtractedCell, ExtractedTable
from backend.app.table_store import load_tables, persist_tables


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as value:
        yield value


def extracted(caption, headers, body, *, unit="万元", document_id="doc-1", page=9, status="extracted"):
    return ExtractedTable(
        table_key="p009-t01", caption=caption, unit=unit, page=page, pages=[page],
        columns=headers, document_id=document_id, status=status,
        rows=[[ExtractedCell(r, c, text, (10, 20 + r * 10, 90, 30 + r * 10), page)
               for c, text in enumerate(row)] for r, row in enumerate(body)],
    )


def test_multilevel_headers_preserve_amount_percentage_and_precision():
    table = extracted("货币资金明细", ["项目", "2025 年末", "", "2024 年末", ""], [
        ["", "金额", "占比", "金额", "占比"],
        ["银行存款", "468,449.14", "99.20", "339,873.02", "99.08"],
        ["库存现金", "-", "0.00", "1.83", "0.00"],
    ], unit="万元、%")
    block = block_from_extracted(table, table_id="t", subject="公司", document_id=table.document_id)
    assert [column.label for column in block.columns[1:]] == [
        "2025 年末 · 金额（万元）", "2025 年末 · 占比", "2024 年末 · 金额（万元）", "2024 年末 · 占比",
    ]
    assert [cell.unit for cell in block.rows[0].cells[1:]] == ["万元", "%", "万元", "%"]
    assert block.rows[0].cell(1).raw == "468,449.14"
    assert block.rows[1].cell(1).raw == "-" and block.rows[1].cell(1).normalized == "0"
    assert block.rows[0].cell(2).locator["column"] == 2
    assert block.validation_status == "passed"


def test_row_units_and_group_subtotal_rows_are_preserved():
    table = extracted("财务数据", ["项目", "2025 年"], [
        ["资产：", ""], ["资产总额（亿元）", "274.13"], ["净资产收益率（%）", "2.00"],
        ["经营活动产生的现金流量净额（万元）", "-1,230.08"],
    ], unit="亿元、%")
    block = block_from_extracted(table, table_id="t", subject="公司")
    assert [row.kind for row in block.rows] == ["group", "normal", "normal", "subtotal"]
    assert [row.cell(1).unit for row in block.rows[1:]] == ["万元", "%", "万元"]
    assert block.rows[1].cell(1).raw == "2,741,300"
    assert block.rows[1].cell(1).locator["original_raw"] == "274.13"
    assert block.rows[-1].cell(1).normalized == "-1230.08"


def test_statement_periods_are_newest_first_and_missing_standard_items_stay_explicit():
    table = extracted("合并资产负债表", ["项目", "2023 年末", "2024 年末", "2025 年末"], [
        ["货币资金", "8,000", "9,000", "10,000"],
        ["资产总计", "80,000", "90,000", "100,000"],
    ])
    table = report_source_tables._standard_statement_rows(table, "balance", securities=False)
    block = block_from_extracted(table, table_id="t-standard", subject="公司", document_id="doc-1")
    assert [column.label for column in block.columns[1:]] == [
        "2025 年末（万元）", "2024 年末（万元）", "2023 年末（万元）",
    ]
    cash = next(row for row in block.rows if row.row_key == "货币资金")
    assert [cash.cell(i).raw for i in (1, 2, 3)] == ["10,000", "9,000", "8,000"]
    required = next(row for row in block.rows if row.row_key == "短期借款")
    assert [required.cell(i).raw for i in (1, 2, 3)] == ["未披露"] * 3
    assert all(required.cell(i).status == "not_applicable" for i in (1, 2, 3))


def test_source_tables_keep_documents_separate_and_mark_missing(monkeypatch):
    consolidated = extracted("合并资产负债表", ["项目", "2025 年末"], [["资产总计", "72,000.00"]])
    standalone = extracted("母公司资产负债表", ["项目", "2025 年末"], [["资产总计", "68,000.00"]], document_id="doc-2")
    monkeypatch.setattr(report_source_tables, "load_tables", lambda *_: [consolidated, standalone])
    monkeypatch.setattr(report_source_tables, "rows", lambda query, *_: [
        {"id": "doc-1", "document_role": "prospectus"}, {"id": "doc-2", "document_role": "prospectus"},
    ] if "FROM documents" in query else [])
    monkeypatch.setattr(report_source_tables, "document_series", lambda *_: {})
    monkeypatch.setattr(report_source_tables, "material_numbers", lambda *_: {})
    blocks = report_source_tables.chapter_source_table_blocks("p", "financial", subject="公司")
    assert len({block.table_id for block in blocks}) == len(blocks)
    assert blocks[0].scope == "合并口径" and blocks[1].scope == "母公司口径"
    assert blocks[0].rows[0].cell(1).locator["document_id"] == "doc-1"
    assert blocks[1].rows[0].cell(1).locator["document_id"] == "doc-2"
    coverage = blocks[-1]
    income = next(row for row in coverage.rows if row.row_key == "income")
    assert income.cell(1).raw == "未识别到原表"


def test_one_source_table_with_both_scopes_is_split_before_reporting(monkeypatch):
    table = extracted("财务数据", ["项目", "2025 年末"], [
        ["合并口径", ""], ["资产总计", "100,000"],
        ["母公司口径", ""], ["资产总计", "80,000"],
    ])
    monkeypatch.setattr(report_source_tables, "load_tables", lambda *_: [table])
    monkeypatch.setattr(report_source_tables, "rows", lambda query, *_: [
        {"id": "doc-1", "document_role": "prospectus"},
    ] if "FROM documents" in query else [])
    monkeypatch.setattr(report_source_tables, "document_series", lambda *_: {})
    monkeypatch.setattr(report_source_tables, "material_numbers", lambda *_: {})
    blocks = report_source_tables.chapter_source_table_blocks("p", "financial", subject="公司")
    scopes = [block for block in blocks if block.origin_mode == "uploaded_table" and block.title == "财务数据（合并口径）"
              or block.origin_mode == "uploaded_table" and block.title == "财务数据（母公司口径）"]
    assert len(scopes) == 2
    assert {block.scope for block in scopes} == {"合并口径", "母公司口径"}
    assert {block.rows[0].cell(1).raw for block in scopes} == {"100,000", "80,000"}
    assert len({block.table_id for block in scopes}) == 2


def _canonical_row(fact_key, period, value, scope):
    return {
        "fact_key": fact_key, "period": period, "value": value, "unit": "万元",
        "text_value": "", "scope": scope, "period_type": "annual", "source_ids": "[]",
    }


def _fake_report_rows(payload):
    """按 SQL 分派：口径清单 / 指定口径取数 / 冲突清单。"""
    def fake(query, params=()):
        if "DISTINCT scope" in query:
            seen: list[str] = []
            for item in payload:
                if item["scope"] not in seen:
                    seen.append(item["scope"])
            return [{"scope": scope} for scope in seen]
        if "c.scope=?" in query:
            return [item for item in payload if item["scope"] == params[1]]
        return []
    return fake


def test_report_facts_are_isolated_by_table_scope(monkeypatch):
    """取数按**表级口径**隔离：选定口径后只取该口径，不再按科目择优。

    修订稿明确禁止"逐科目选择口径后把整表标为合并"——那会让同一张表里不同科目
    来自不同口径。
    """
    payload = [
        _canonical_row("total_assets", "2025", 100000, "consolidated"),
        _canonical_row("total_assets", "2025", 80000, "standalone"),
    ]
    monkeypatch.setattr(report_tables, "rows", _fake_report_rows(payload))
    assert report_tables._facts("p", "consolidated")["total_assets"]["2025"]["value"] == 100000
    assert report_tables._facts("p", "standalone")["total_assets"]["2025"]["value"] == 80000


def test_report_scope_tables_never_cross_replace_values(monkeypatch):
    """正例：合并与母公司**分别成表**，各自只取本口径的值，不得交叉替换。

    北投验收锚点即依赖这一点：合并资产总计 96,717,233.20 万元、母公司 57,372,249.51 万元，
    一张表只能显示一个，必须分表。
    """
    payload = [
        _canonical_row("total_assets", "2025", 100000, "consolidated"),
        _canonical_row("total_assets", "2025", 80000, "standalone"),
    ]
    monkeypatch.setattr(report_tables, "rows", _fake_report_rows(payload))
    blocks = report_tables.financial_table_blocks("p", subject="某公司")
    merged = next(b for b in blocks if b.title == "三年主要资产负债项目表" and b.scope == "合并口径")
    parent = next(b for b in blocks if b.title == "三年主要资产负债项目表" and b.scope == "母公司口径")
    assert merged.table_id != parent.table_id
    assert merged.rows[0].cells[1].raw == "100,000"
    assert parent.rows[0].cells[1].raw == "80,000"


def test_report_scope_tables_mark_missing_items_instead_of_borrowing(monkeypatch):
    """负例：合并有资产、仅母公司有现金 → 合并表的货币资金必须是"未披露"，不得取母公司值。"""
    payload = [
        _canonical_row("total_assets", "2025", 100000, "consolidated"),
        _canonical_row("cash", "2025", 999, "standalone"),
    ]
    monkeypatch.setattr(report_tables, "rows", _fake_report_rows(payload))
    blocks = report_tables.financial_table_blocks("p", subject="某公司")
    merged = next(b for b in blocks if b.title == "三年主要资产负债项目表" and b.scope == "合并口径")
    cash_row = next(row for row in merged.rows if row.row_key == "货币资金")
    assert cash_row.cells[1].raw == "未披露"
    assert cash_row.cells[1].status == "not_applicable"
    parent = next(b for b in blocks if b.title == "三年主要资产负债项目表" and b.scope == "母公司口径")
    assert next(row for row in parent.rows if row.row_key == "货币资金").cells[1].raw == "999"


def test_report_scope_tables_keep_missing_periods_visible(monkeypatch):
    """负例：同一口径内某科目缺一个期间 → 该格必须是"未披露"，不得由另一口径补位。

    期间范围由**该口径自己**的数据决定（合并只有 2025 就不出现 2024 列）；在期间范围内，
    某科目缺值时必须如实写"未披露"，哪怕另一口径恰好有这一年的数。
    """
    payload = [
        _canonical_row("total_assets", "2025", 100000, "consolidated"),
        _canonical_row("total_assets", "2024", 90000, "consolidated"),
        _canonical_row("cash", "2025", 1234, "consolidated"),
        # 母公司另有 2024 年现金：不属于合并口径，绝不允许补进合并表
        _canonical_row("cash", "2024", 999, "standalone"),
    ]
    monkeypatch.setattr(report_tables, "rows", _fake_report_rows(payload))
    blocks = report_tables.financial_table_blocks("p", subject="某公司")
    merged = next(b for b in blocks if b.title == "三年主要资产负债项目表" and b.scope == "合并口径")
    cash_row = next(row for row in merged.rows if row.row_key == "货币资金")
    assert [cell.raw for cell in cash_row.cells[1:]] == ["1,234", "未披露"]
    assert cash_row.cells[2].status == "not_applicable"


def test_reparse_isolates_history_reviews_by_parser_version():
    """重抽（解析版本升级）后，旧的人工核对意见不得按新行号套到新表上。

    人工核对按 ``(table_id, cell_id)`` 匹配。``source_table_id`` 把 ``parser_version``
    编进了 table_id，所以升级解析版本会改变单元格身份：旧意见只作为历史保留，不会再
    命中新表（修订稿 §2.3「保留旧记录和人工更正，不将旧意见按新行号套用」）。
    同版本重复构建必须保持稳定，否则正常的人工核对会被判成失效。
    """
    def table(version):
        return ExtractedTable(
            table_key="p009-t01", caption="合并资产负债表", unit="万元", page=9, pages=[9],
            parser_version=version, document_id="doc-1", columns=["项目", "2025"], rows=[],
        )

    old = report_source_tables.source_table_id(table("table-extractor-v2"))
    new = report_source_tables.source_table_id(table("table-extractor-v3"))
    assert old != new
    assert report_source_tables.source_table_id(table("table-extractor-v3")) == new


def test_unreliable_rating_table_is_visible_but_never_marked_passed(monkeypatch):
    table = extracted("", ["项目", "2025 年"], [["资产总额（亿元）", "274.13"], ["全部债务（亿元）", "144.53"]], unit="", status="needs_review")
    monkeypatch.setattr(report_source_tables, "load_tables", lambda *_: [table])
    monkeypatch.setattr(report_source_tables, "rows", lambda query, *_: [
        {"id": "doc-1", "document_role": "rating_report"},
    ] if "FROM documents" in query else [])
    monkeypatch.setattr(report_source_tables, "document_series", lambda *_: {})
    monkeypatch.setattr(report_source_tables, "material_numbers", lambda *_: {})
    blocks = report_source_tables.chapter_source_table_blocks("p", "rating", subject="公司")
    assert blocks[0].validation_status == "needs_review"
    assert blocks[-1].rows[0].cell(1).raw == "待人工核验"
    assert blocks[0].rows[0].cell(1).unit == "万元"
    assert blocks[0].rows[0].cell(1).raw == "2,741,300"


def test_reparse_archives_old_values_and_versions_review_table_identity():
    init_db()
    old = extracted("资产负债表", ["项目", "2025 年末"], [["资产总计", "10,000"]])
    old.parser_version = "table-extractor-v1"
    current = extracted("资产负债表", ["项目", "2025 年末"], [["资产总计", "11,000"]])
    with connect() as conn:
        conn.execute("DELETE FROM document_tables WHERE project_id='history-project' AND document_id='history-doc'")
        conn.execute("DELETE FROM document_table_history WHERE project_id='history-project' AND document_id='history-doc'")
        persist_tables(conn, "history-project", "history-doc", [old])
        prior_id = report_source_tables.source_table_id(old)
        persist_tables(conn, "history-project", "history-doc", [current])
    history = rows(
        "SELECT parser_version,snapshot_json FROM document_table_history "
        "WHERE project_id='history-project' AND document_id='history-doc'",
    )
    active = load_tables("history-project")
    assert len(history) == 1
    assert history[0]["parser_version"] == "table-extractor-v1"
    assert json.loads(history[0]["snapshot_json"])["rows_json"].find("10,000") >= 0
    assert active[0].rows[0][1].text == "11,000"
    assert report_source_tables.source_table_id(active[0]) != prior_id


def test_complete_statements_and_schedules_reach_report_blocks(client):
    project = "demo-bond-huachen"
    expected = {"合并资产负债表": 34, "主要利润项目表": 18, "主要现金流量项目表": 24,
                "货币资金明细": 5, "其他应收款明细": 4, "应收账款账龄结构": 8}
    originals = {table.caption: table for table in load_tables(project)}
    blocks = chapter_table_blocks(project, "financial", subject="华辰精密制造有限公司")
    complete = {block.title: block for block in blocks if block.origin_mode == "uploaded_table"}
    for title, minimum in expected.items():
        assert title in complete
        assert len(complete[title].rows) >= minimum
        cells = [cell for row in complete[title].rows for cell in row.cells
                 if cell.locator.get("table_key") == originals[title].table_key]
        assert len(cells) == sum(len(row) for row in originals[title].rows)
        for source_row in originals[title].rows:
            for source_cell in source_row:
                cell = next(item for item in cells if item.locator["row"] == source_cell.row
                            and item.locator["column"] == source_cell.column)
                assert cell.locator["original_raw"] == source_cell.text or cell.raw == source_cell.text
                assert cell.locator["page"] == source_cell.page
                assert cell.locator["document_id"] == originals[title].document_id
    markdown = system_table_markdown(project, "financial", subject="华辰精密制造有限公司")
    api_blocks = section_blocks(project, {"section_key": "financial", "content": markdown}, subject="华辰精密制造有限公司")
    assert all(block["present_in_content"] for block in api_blocks if block["kind"] == "table")
    assert count_report_chars("分析正文。\n\n" + markdown, system_table_block=markdown) == 5


def test_real_zhongyou_keeps_full_consolidated_and_standalone_statements(client):
    projects = rows("SELECT id FROM projects WHERE company_name LIKE ?", ("%中邮%",))
    if not projects:
        pytest.skip("真实参考材料未安装")
    for project in projects:
        originals = load_tables(project["id"])
        if not any(table.caption == "表/图合并资产负债表" for table in originals):
            continue
        blocks = chapter_table_blocks(project["id"], "financial", subject="中邮证券有限责任公司")
        complete = {block.title: block for block in blocks if block.origin_mode == "uploaded_table"}
        for title, count, scope in (
            ("表/图合并资产负债表", 45, "合并口径"), ("表/图合并利润表", 26, "合并口径"),
            ("表/图合并现金流量表", 43, "合并口径"), ("表/图母公司资产负债表", 43, "母公司口径"),
            ("表/图母公司利润表", 25, "母公司口径"), ("表/图母公司现金流量表", 43, "母公司口径"),
        ):
            assert len(complete[title].rows) >= count
            assert complete[title].scope == scope
        cash = next(row for row in complete["表/图合并资产负债表"].rows if row.row_key == "货币资金")
        assert cash.cell(1).raw == "472,231.65"
        return
    pytest.skip("中邮完整报表未抽取")


def test_model_request_does_not_ask_model_to_echo_complete_tables(monkeypatch):
    table = "### 系统整理数据表\n\n| 科目 | 2025 年 |\n| --- | --- |\n| 资产 | 72,000 |"
    monkeypatch.setattr(llm, "chapter_prompt", lambda *_: "system")
    _, request, *_ = llm._report_request("财务分析", "正文。\n\n" + table, [], None, "financial", {
        "project_id": "p", "system_table_block": table, "system_table_role": "正文只写分析。", "prompt_config": {},
    })
    assert request["deterministic_draft"] == "正文。"
    assert request["system_tables"] == table
    assert "不要在返回的 content 中复制" in request["system_table_rules"]
    assert json.dumps(request, ensure_ascii=False).count("72,000") == 1
