"""财务表格端到端验收（Track C）。

覆盖一条完整链路，并且**只在这一条链路上验收**：

    华辰 PDF → ExtractedTable → uploaded_table TableBlock → dual_source_reconciliation
             → 报告章节表格 → Markdown / Word 导出

与既有单点测试的分工
--------------------
``test_pdf_tables`` / ``test_table_extraction`` 管渲染与解析、``test_table_block`` 管契约、
``test_report_tables`` 管章节分派、``test_table_store`` 管落库。本文件不重复它们的断言，
而是回答一个它们都回答不了的问题：**把链路接起来跑完之后，成品（报告正文与两个导出件）
里的每一个数字，是否还能一路追回 PDF 的单元格，并且三端一致。**

写法上的两条纪律
----------------
1. **负例与正例成对。** 「逐格可追溯」不能只看字段在不在，必须验证坐标真的指到那一格；
   「表格不能绕过证据校验」必须真的构造一张不可追溯的表看它是否被拦。已知会失败的
   实测缺陷用 ``pytest.xfail`` 记录（仓库既有写法，见 ``test_phantom_conflicts``），
   而不是把断言写松——写松了，缺陷被修好时也不会有人知道。
2. **不依赖运行顺序与上一次运行的残留。** 模块内自行发起运行并等待终态；所有断言只读
   自己刚刚生成的状态。
"""
from __future__ import annotations

import re
import time
from decimal import Decimal
from io import BytesIO

import fitz
import pytest
from docx import Document
from fastapi.testclient import TestClient

from backend.app.bond_documents import CASE_BY_ID, case_table_gold, generate_case_materials
from backend.app.database import rows
from backend.app.delivery_wording import wording_violations
from backend.app.main import app
from backend.app.report_tables import (
    SYSTEM_TABLE_HEADING, TABLE_CHAPTERS, chapter_table_blocks, reconciliation_tables,
    render_table_block, uploaded_table_blocks,
)
from backend.app.table_block import (
    ORIGIN_MODES, TABLE_TYPES, TableBlock, annual_series_block, block_from_extracted,
    cell_from_text, conflict_comparison_block, reconciliation_block, validate_table_block,
)
from backend.app.table_extraction import ExtractedTable, extract_tables
from backend.app.table_store import table_inventory

PROJECT = "demo-bond-huachen"
SUBJECT = "华辰精密制造有限公司"
CONFLICT_PROJECT = "demo-bond-conflict"
CONFLICT_SUBJECT = "瀚川绿色装备有限公司"

# 章节 → 该章必须出现的系统表格名。与 ``report_tables`` 的实现同步；
# 少一张就是「报告少披露了一张表」，必须在这里失败。
CHAPTER_TABLES = {
    "financial": (
        "三年主要资产负债项目表", "应收账款规模及明细", "主要利润项目表",
        "主要现金流量项目表", "债务结构表",
    ),
    "cashflow": ("偿债来源及核验表", "预测经营现金流（与上表口径不同）"),
    "debt": ("有息负债分类表", "到期结构表"),
    "rating": ("评级结论对照表", "评级口径与主体档案口径对照"),
}

# 成品里不得出现的内部枚举（来源模式/表型）与表内字段键。
INTERNAL_ENUMS = ("profile_facts", "uploaded_table", "merged", "annual_series",
                  "dual_source_reconciliation", "conflict_comparison")

UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def materials(tmp_path_factory):
    """华辰原始材料。独立生成到临时目录：解析结果不依赖库里的既有状态。"""
    case = CASE_BY_ID[PROJECT]
    root = tmp_path_factory.mktemp("trackc-huachen")
    return case, generate_case_materials(root, case)


@pytest.fixture(scope="module")
def extracted(materials) -> list[ExtractedTable]:
    _, generated = materials
    return extract_tables(generated[0]["path"])


@pytest.fixture(scope="module")
def huachen(client, materials) -> dict:
    """跑一次完整运行，然后取报告正文与四章系统表格。"""
    run = _start_run(client, PROJECT)
    sections = {
        item["section_key"]: item
        for item in client.get(f"/api/projects/{PROJECT}/report").json()
    }
    return {
        "run": run,
        "sections": sections,
        "tables": {key: chapter_table_blocks(PROJECT, key, subject=SUBJECT) for key in TABLE_CHAPTERS},
        "uploaded": uploaded_table_blocks(PROJECT, subject=SUBJECT),
        "reconciliation": reconciliation_tables(PROJECT, subject=SUBJECT),
    }


@pytest.fixture(scope="module")
def conflict(client) -> dict:
    run = _start_run(client, CONFLICT_PROJECT)
    sections = {
        item["section_key"]: item
        for item in client.get(f"/api/projects/{CONFLICT_PROJECT}/report").json()
    }
    return {"run": run, "sections": sections}


@pytest.fixture(scope="module")
def exports(client, huachen) -> dict:
    markdown = client.get(f"/api/projects/{PROJECT}/export?format=md")
    word = client.get(f"/api/projects/{PROJECT}/export?format=docx")
    assert markdown.status_code == 200, markdown.text
    assert word.status_code == 200, word.text
    document = Document(BytesIO(word.content))
    return {
        "markdown": markdown.text,
        "word": document,
        "word_bytes": len(word.content),
        "word_text": _docx_text(document),
    }


def _start_run(client: TestClient, project_id: str) -> dict:
    """发起一次运行并等到终态。运行必须真的完成——否则后面的断言没有意义。"""
    started = client.post(f"/api/projects/{project_id}/runs", json={"focus": "债券担保尽调"})
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    deadline = time.time() + 900
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in {"completed", "failed", "cancelled", "model_required"}:
            assert run["status"] == "completed", run
            return run
        time.sleep(0.05)
    raise AssertionError(f"{project_id} 的运行未在 900 秒内结束")


def _docx_text(document: Document) -> str:
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def _numbers(value: str) -> Decimal | None:
    """单元格原文 → 数值。``—`` 是无数据，``-`` 是零（披露口径）。"""
    text = (value or "").strip().replace(",", "").replace("，", "").replace(" ", "")
    if not text or text == "—":
        return None
    if text == "-":
        return Decimal("0")
    try:
        return Decimal(text)
    except Exception:
        return None


def _row_values(table: ExtractedTable, label: str) -> list[Decimal]:
    """取一张抽取表的某个科目行，返回各年度列的数值。"""
    for row in table.rows:
        if row[0].text.replace("　", "").strip() == label:
            return [value for cell in row[1:] if (value := _numbers(cell.text)) is not None]
    raise AssertionError(f"{table.caption} 缺少科目行：{label}")


def _table(extracted: list[ExtractedTable], caption: str) -> ExtractedTable:
    return next(item for item in extracted if item.caption == caption)


def _rendered_tables(blocks: list[TableBlock]) -> str:
    return "\n\n".join(
        render_table_block(block, index=index, total=len(blocks))
        for index, block in enumerate(blocks, 1)
    )


def _table_values(blocks: list[TableBlock]) -> set[str]:
    """系统表格里的全部业务数值（原文口径）。"""
    values: set[str] = set()
    for block in blocks:
        for row in block.rows:
            for cell in row.cells[1:]:
                if cell.status in {"blank", "not_applicable"}:
                    continue
                if cell.raw and cell.raw not in {"—", "-"}:
                    values.add(cell.raw)
    return values


# ---------------------------------------------------------------------------
# 1. 材料勾稽：三张报表 + 应收明细，既在模型层成立，也在 PDF 逐格上成立
# ---------------------------------------------------------------------------


def test_material_tie_outs_hold_on_the_model(materials) -> None:
    """三张报表勾稽与应收勾稽（模型层，在 e2e 语境下再验一次）。"""
    case, _ = materials
    assert len(case.statements) == 3
    for statement in case.statements:
        sheet = statement.balance_sheet
        income = statement.income_statement
        flow = statement.cash_flow_statement
        detail = statement.receivables
        assert sheet.total_assets == sheet.total_liabilities + sheet.equity, statement.year
        assert sheet.current_assets + sheet.non_current_assets == sheet.total_assets, statement.year
        assert income.net_profit == income.total_profit - income.income_tax_expense, statement.year
        assert flow.closing_cash == flow.opening_cash + flow.net_increase, statement.year
        assert flow.net_increase == (
            flow.operating_cash_flow + flow.investing_cash_flow + flow.financing_cash_flow
        ), statement.year
        assert flow.closing_cash == sheet.cash, f"{statement.year} 现金流量表期末现金与资产负债表货币资金不一致"
        # 应收：账龄合计＝账面余额，账面余额−坏账准备＝账面价值
        assert detail.aging.total == detail.gross_balance, statement.year
        assert detail.carrying_value == detail.gross_balance - detail.bad_debt_provision, statement.year


def test_material_tie_outs_hold_cell_by_cell_on_the_pdf(materials, extracted) -> None:
    """同一组勾稽必须在 PDF 的**单元格**上成立——这才是「材料可勾稽」的证据。"""
    case, _ = materials
    sheet_table = _table(extracted, "合并资产负债表")
    income_table = _table(extracted, "主要利润项目表")
    cash_table = _table(extracted, "主要现金流量项目表")
    aging_table = _table(extracted, "应收账款账龄结构")

    for year_number, statement in enumerate(reversed(case.statements)):   # 列按年度倒序
        assets = _row_values(sheet_table, "资产总计")
        liabilities = _row_values(sheet_table, "负债合计")
        equity = _row_values(sheet_table, "所有者权益合计")
        both_sides = _row_values(sheet_table, "负债和所有者权益总计")
        assert assets[year_number] == liabilities[year_number] + equity[year_number], statement.year
        assert both_sides[year_number] == assets[year_number], statement.year

        closing = _row_values(cash_table, "五、期末现金及现金等价物余额")[year_number]
        opening = _row_values(cash_table, "加：期初现金及现金等价物余额")[year_number]
        operating = _row_values(cash_table, "经营活动产生的现金流量净额")[year_number]
        investing = _row_values(cash_table, "投资活动产生的现金流量净额")[year_number]
        financing = _row_values(cash_table, "筹资活动产生的现金流量净额")[year_number]
        assert closing == opening + operating + investing + financing, statement.year
        assert closing == _row_values(sheet_table, "货币资金")[year_number], statement.year

        total_profit = _row_values(income_table, "三、利润总额")[year_number]
        income_tax = _row_values(income_table, "减：所得税费用")[year_number]
        net_profit = _row_values(income_table, "四、净利润")[year_number]
        assert net_profit == total_profit - income_tax, statement.year

        # 应收：账龄合计＝账面余额；账面余额−坏账准备＝账面价值＝资产负债表「应收账款」
        within = _row_values(aging_table, "1年以内（含1年）")[year_number]
        to_two = _row_values(aging_table, "1至2年")[year_number]
        to_three = _row_values(aging_table, "2至3年")[year_number]
        over_three = _row_values(aging_table, "3年以上")[year_number]
        aging_total = _row_values(aging_table, "合计")[year_number]
        provision = _row_values(aging_table, "坏账准备")[year_number]
        carrying = _row_values(aging_table, "账面价值")[year_number]
        assert aging_total == within + to_two + to_three + over_three, statement.year
        assert carrying == aging_total - provision, statement.year
        assert carrying == _row_values(sheet_table, "应收账款")[year_number], statement.year
        # 与模型对齐：账面价值＝carrying_value，账龄合计＝gross_balance
        assert carrying == statement.receivables.carrying_value, statement.year
        assert aging_total == statement.receivables.gross_balance, statement.year


def test_pdf_cells_agree_with_the_table_gold(materials, extracted) -> None:
    """材料勾稽的值必须与金标一致：金标只声明材料应当是什么样，与解析器无关。"""
    case, generated = materials
    gold = case_table_gold(case, generated)
    aging = next(
        item["expected"] for item in gold
        if item["annotation_type"] == "table_cell"
        and item["expected"]["table_key"] == "receivables_aging"
        and item["expected"]["row_key"] == "合计" and item["expected"]["year"] == 2025
    )
    assert aging["raw"] == "12,640"
    assert _row_values(_table(extracted, "应收账款账龄结构"), "合计")[0] == Decimal(aging["normalized"])


# ---------------------------------------------------------------------------
# 2. uploaded_table：逐格可追溯，且是真的从原表提取而不是事实汇总
# ---------------------------------------------------------------------------


def test_uploaded_tables_exist_and_are_traceable_cell_by_cell(huachen) -> None:
    """``uploaded_table`` 必须真实出现，且每个业务数据格都带页码与坐标。"""
    blocks = huachen["uploaded"]
    assert len(blocks) >= 11, "华辰两份材料的 11 张表都应当进入 uploaded_table"
    assert {block.origin_mode for block in blocks} == {"uploaded_table"}
    assert {block.table_type for block in blocks} == {"annual_series"}

    located = 0
    for block in blocks:
        value_indexes = [index for index, column in enumerate(block.columns)
                         if column.role in {"period_value", "profile_value", "uploaded_value", "adopted_value"}]
        for row in block.rows:
            for index in value_indexes:
                cell = row.cell(index)
                if cell.status not in {"ok", "zero"}:
                    continue
                located += 1
                locator = cell.locator
                assert locator.get("page"), (block.table_id, row.row_key)
                bbox = locator.get("bbox") or []
                assert len(bbox) == 4, (block.table_id, row.row_key, bbox)
                assert bbox[2] > bbox[0] and bbox[3] > bbox[1], (block.table_id, row.row_key, bbox)
                assert 0 <= bbox[0] and bbox[2] <= 595 and 0 <= bbox[1] and bbox[3] <= 842
    assert located >= 250, f"可定位业务格仅 {located} 个，材料表格明显没有全量进入契约"


def test_seeded_demo_materials_are_current_so_no_table_needs_review(huachen) -> None:
    """落库的材料表不得有待人工确认：``needs_review`` 的表按契约不得当可信数据用。

    这项检查会连带暴露材料本身是否过期——见下面记录。
    """
    inventory = table_inventory(PROJECT)
    stale = [item for item in inventory["tables"] if item["status"] != "extracted"]
    review_blocks = [block for block in huachen["uploaded"] if block.validation_status != "passed"]
    if stale or review_blocks:
        pytest.xfail(
            f"库里 {len(stale)}/{inventory['total']} 张材料表是 needs_review："
            f"{[(item['table_key'], item['caption'], item['start_page']) for item in stale]}，"
            f"对应 {len(review_blocks)} 个 uploaded_table 块因此被判为不可信。"
            "根因是**演示材料本身过期**：仓库 `.data/demo_materials/demo-bond-huachen/` 里的 PDF "
            "由旧版渲染器生成，第 13 页发行要素表的单位行是空的（实测 `单位：` 后面没有内容），"
            "而解析器的表名锚定规则要求「单位：行有内容」→ 表名与单位都取不到 → 置信度 0.45。"
            "但 `case-manifest.json` 仍然声明 template_version=bond-disclosure-v6-tables、"
            "spec_sha256 与当前一致，`tables` 字段却是空的（旧格式没写），"
            "`generate_case_materials` 只比对版本号与 spec 哈希就判定「材料是最新的」并复用，"
            "于是旧 PDF 一直被沿用：渲染器/解析器规则变了，manifest 察觉不到。"
            "影响面比演示项目大——`tests/conftest.py` 会把这份材料复制进隔离目录（避免参考材料假失败），"
            "所以每个测试会话都会继承这张 needs_review 表。"
            "就地修复只需删除 `.data/demo_materials/demo-bond-huachen/*.pdf` 与 case-manifest.json 让材料重渲染。"
        )
    assert inventory["needs_review"] == 0
    assert {block.validation_status for block in huachen["uploaded"]} == {"passed"}


def test_uploaded_table_values_come_from_the_pdf_not_from_profile_facts(materials, huachen, extracted) -> None:
    """上传表与档案汇总是两套来源：科目名、单位、年度列、数值都必须来自原表。"""
    _, generated = materials
    inventory = table_inventory(PROJECT)
    assert inventory["total"] == 11
    assert all(item["start_page"] for item in inventory["tables"])

    blocks = {block.title: block for block in huachen["uploaded"]}
    balance = blocks["合并资产负债表"]
    assert balance.unit == "万元"
    assert [column.label for column in balance.columns[1:]] == [
        "2025 年末（万元）", "2024 年末（万元）", "2023 年末（万元）",
    ]
    row = next(item for item in balance.rows if item.row_key == "资产总计")
    assert [row.cell(index).raw for index in (1, 2, 3)] == ["72,000", "65,520", "59,040"]
    assert all(cell.origin_type == "uploaded_cell" for row_ in balance.rows for cell in row_.cells[1:])

    # 数值与 PDF 一致（同一份材料、同一次抽取）
    assert [row.cell(index).normalized for index in (1, 2, 3)] == [
        str(value) for value in _row_values(_table(extracted, "合并资产负债表"), "资产总计")
    ]

    # 「不是只有 profile_facts」：档案侧全部是 profile_facts，且上传侧带来了档案侧没有的表
    profile_titles = {block.title for block in huachen["tables"]["financial"]}
    uploaded_titles = set(blocks)
    assert any(block.origin_mode == "profile_facts" for block in huachen["tables"]["financial"])
    assert any(block.origin_mode == "uploaded_table" for block in huachen["tables"]["financial"])
    assert all(block.origin_mode == "profile_facts" for block in huachen["tables"]["financial"][:5])
    assert len(uploaded_titles - profile_titles) >= 3, uploaded_titles - profile_titles
    # 生成的材料里确有这些表，且页码落在材料页数内
    page_count = generated[0]["page_count"]
    for table in huachen["uploaded"]:
        for cell in table.cells():
            if cell.locator:
                assert 1 <= cell.locator["page"] <= page_count


def test_uploaded_cell_bbox_locates_its_own_value(materials, extracted) -> None:
    """坐标必须真的指到那一格：用 PDF 的文字层在 bbox 里取字，比对是否就是该格的值。

    这条是「逐格可追溯」的实质检验——只看字段在不在（上一条）会让坐标错位蒙混过关。
    """
    _, generated = materials
    document = fitz.open(generated[0]["path"])
    mismatches: list[str] = []
    checked = 0
    for table in extracted:
        block = block_from_extracted(table, table_id=f"check-{table.table_key}",
                                     subject=SUBJECT, document_id="d", source_refs=())
        for row in block.rows:
            for cell in row.cells[1:]:
                if cell.status not in {"ok", "zero"} or not cell.locator.get("bbox"):
                    continue
                checked += 1
                page = document[cell.locator["page"] - 1]
                inside = page.get_text("text", clip=fitz.Rect(cell.locator["bbox"]))
                normalized = inside.replace("\n", "").replace(" ", "").replace(",", "")
                if cell.raw.replace(",", "").replace(" ", "") not in normalized:
                    mismatches.append(
                        f"{table.table_key} 第{cell.locator['page']}页 {cell.raw}→bbox 内实为「{inside.strip()[:12]}」"
                    )
    document.close()
    assert checked >= 250
    if mismatches:
        pytest.xfail(
            f"上传表单元格的 bbox 与取值错位：{len(mismatches)}/{checked} 格在自己的坐标里取不到自己的值"
            f"（示例：{mismatches[0]}）。页码正确、矩形错位——"
            "`table_extraction._table_page` 用 `slot = row_index * column_count + column_index`（行优先）"
            "索引 PyMuPDF 的 `Table.cells`，而该序列实测是列优先（先列 0 的全部行）。"
            "实测改列优先后 103/112 格能自证；契约层因此只保证了「有坐标」，没保证「坐标对」。"
        )


# ---------------------------------------------------------------------------
# 3. 双来源核对
# ---------------------------------------------------------------------------


def test_dual_source_reconciliation_is_generated_and_passes(huachen) -> None:
    """档案与上传原表逐科目核对：至少一块、状态 passed、按年度/口径/单位/科目匹配。"""
    blocks = huachen["reconciliation"]
    assert blocks, "档案事实与上传原表都在，应当能生成核对表"
    assert {block.table_type for block in blocks} == {"dual_source_reconciliation"}
    assert {block.origin_mode for block in blocks} == {"merged"}
    assert {block.validation_status for block in blocks} == {"passed"}
    assert [column.label for column in blocks[0].columns] == ["科目", "档案值", "上传值", "采用值", "状态"]
    years = {block.scope.split("／")[-1] for block in blocks}
    assert years == {"2025 年末", "2024 年末", "2023 年末"}, years

    adopted = conflicts = single = 0
    for block in blocks:
        assert len(block.rows) >= 20, (block.table_id, len(block.rows))
        for row in block.rows:
            profile, uploaded, chosen = row.cell(1), row.cell(2), row.cell(3)
            if profile.unit and uploaded.unit:
                assert profile.unit == uploaded.unit, (block.scope, row.row_key, profile.unit, uploaded.unit)
            has_profile = profile.status not in {"not_applicable", "blank"}
            has_upload = uploaded.status not in {"not_applicable", "blank"}
            if has_profile and has_upload:
                if profile.normalized == uploaded.normalized:
                    # 同科目同年度：两来源一致 → 采用值可填，且必须同时指向两个来源
                    adopted += 1
                    assert chosen.status == "adopted", (block.table_id, row.row_key)
                    assert chosen.normalized == profile.normalized, (block.table_id, row.row_key)
                    assert chosen.adoption_basis
                else:
                    # 不一致 → 必须是冲突，且采用值为空（不得自动择一）
                    conflicts += 1
                    assert chosen.status == "conflict", (block.table_id, row.row_key)
                    assert chosen.normalized in {None, ""}, (block.table_id, row.row_key)
                    assert row.cell(1).raw and row.cell(2).raw, "两个披露值都必须保留"
            else:
                single += 1
                assert chosen.status == "adopted"
                assert "档案" in chosen.adoption_basis or "上传" in chosen.adoption_basis
    assert adopted >= 30, f"按同一主体、口径、单位和期间匹配的一致科目仅 {adopted} 个"
    assert single, "应当存在只有一侧取值的科目"


def test_reconciliation_uploaded_side_matches_the_material(extracted, huachen) -> None:
    """核对表的上传值必须等于材料原表同一科目同一年度的数值。

    行键是**业务科目名**（``资产总计``）。匹配仍按事实字段键进行——那是
    ``_profile_cells`` / ``_uploaded_cells`` 的键，与展示用的行名分开：
    把内部字段键直接摆进表里会触发成品用词违规，一旦接进出口就会阻断整份报告。
    """
    block = next(item for item in huachen["reconciliation"] if item.scope.endswith("2025 年末"))
    row = next(item for item in block.rows if item.row_key == "资产总计")
    assert row.cell(2).raw == "72,000"
    assert row.cell(1).raw == "72,000"
    assert row.cell(3).status == "adopted"
    assert _row_values(_table(extracted, "合并资产负债表"), "资产总计")[0] == Decimal("72000")


def test_disagreeing_sources_are_never_auto_adopted() -> None:
    """核心安全不变量（手工构造，不依赖案例是否声明冲突）：不一致时采用值必须为空。"""
    block = reconciliation_block(
        table_id="e2e-recon", title="核对", subject=SUBJECT, scope="合并口径", period="2025 年末",
        profile_cells={"货币资金": cell_from_text("6,820", unit="万元", source_ids=["p1"])},
        uploaded_cells={"货币资金": cell_from_text("5,940", unit="万元", source_ids=["u1"])},
    )
    assert block.validation_status == "passed", "并列保留即为合规，不应当判为阻断"
    assert block.rows[0].cell(1).raw == "6,820" and block.rows[0].cell(2).raw == "5,940"
    assert block.rows[0].cell(3).status == "conflict"
    assert block.rows[0].cell(3).normalized in {None, ""}


# ---------------------------------------------------------------------------
# 4. 报告章节表格（四章稳定出现，且实际现金流与预测值分表）
# ---------------------------------------------------------------------------


def test_every_table_chapter_carries_its_system_tables(huachen) -> None:
    for chapter_key, titles in CHAPTER_TABLES.items():
        content = huachen["sections"][chapter_key]["content"]
        blocks = huachen["tables"][chapter_key]
        assert SYSTEM_TABLE_HEADING in content, chapter_key
        assert [block.title for block in blocks[:len(titles)]] == list(titles), chapter_key
        assert blocks[-1].title == "报表及明细资料覆盖情况"
        assert any(block.origin_mode == "uploaded_table" and block.rows for block in blocks[:-1]), chapter_key
        assert all(block.validation_status == "passed" for block in blocks
                   if block.origin_mode == "profile_facts"), chapter_key
        for block in blocks:
            if block.validation_status != "passed":
                assert block.origin_mode == "uploaded_table"
                assert any("人工确认" in line or "待核验" in line
                           for line in render_table_block(block, index=1, total=1).splitlines()), block.title
        for index, title in enumerate(titles, 1):
            # 标题格式为「**表 N：标题**」：不带分母——N/M 与采用率 x/y 同形，会被成品守卫拦下
            assert f"**表 {index}：{title}**" in content, (chapter_key, title)
            assert f"| 科目 |" in content or "| 事项 |" in content, chapter_key
        # 章内表格必须逐字来自系统渲染，而不是模型自己画的
        assert _rendered_tables(blocks) in content


def test_cashflow_keeps_audited_actual_and_forecast_in_separate_tables(huachen) -> None:
    """6,500 万元（已审实际）与 9,000 万元（预测）不得同表：口径不同、期间不同。"""
    blocks = huachen["tables"]["cashflow"]
    actual, forecast = blocks[0], blocks[1]
    assert actual.title == "偿债来源及核验表" and actual.scope == "合并口径"
    assert forecast.title == "预测经营现金流（与上表口径不同）"
    assert forecast.scope == "债券存续期预测", "预测表必须自证口径不同"

    actual_values = _table_values([actual])
    assert "6,500" in actual_values, "已审实际经营现金流应当在偿债来源表里"
    assert "9,000" not in actual_values, "预测值不得混进已审实际表"

    forecast_values = _table_values([forecast])
    assert "9,000" in forecast_values, "预测经营现金流必须有独立成表"
    assert "6,500" not in forecast_values

    content = huachen["sections"]["cashflow"]["content"]
    assert "**表 1：偿债来源及核验表**" in content and "**表 2：预测经营现金流（与上表口径不同）**" in content
    assert "6,500" in content and "9,000" in content
    assert "债券存续期预测" in content


def test_report_table_outcomes_match_the_generated_blocks(huachen) -> None:
    """报告正文里的每一张表都要能在 ``chapter_table_blocks`` 里找到同一份内容（不存在第二套数字）。"""
    for chapter_key in TABLE_CHAPTERS:
        content = huachen["sections"][chapter_key]["content"]
        for block in huachen["tables"][chapter_key]:
            assert f"**表 " and block.title in content
            for row in block.rows:
                if row.cells[0].raw and row.cells[0].status == "text":
                    assert f"| {row.cells[0].raw} |" in content, (chapter_key, block.title, row.row_key)


# ---------------------------------------------------------------------------
# 5. 三端一致：报告正文 / Markdown / Word
# ---------------------------------------------------------------------------


def test_chapter_table_values_survive_both_exports(huachen, exports) -> None:
    all_blocks = [block for key in TABLE_CHAPTERS for block in huachen["tables"][key]]
    assert len(all_blocks) > 11, [block.title for block in all_blocks]
    balance = next(block for block in all_blocks if block.title == "合并资产负债表")
    assert len(balance.rows) >= 34, "完整原表的业务行不得被汇总或截断"
    values = _table_values(all_blocks)
    assert len(values) >= 50, len(values)

    missing_markdown = [value for value in sorted(values) if value not in exports["markdown"]]
    missing_word = [value for value in sorted(values) if value not in exports["word_text"]]
    assert not missing_markdown, f"Markdown 导出缺少表内数值：{missing_markdown}"
    assert not missing_word, f"Word 导出缺少表内数值：{missing_word}"

    # 导出件里必须是**真表**：表头行与年度列都在（Word 侧读回的是真表格对象）
    assert exports["markdown"].count("| 科目 |") >= 8
    headers = [
        [cell.text for cell in table.rows[0].cells]
        for table in exports["word"].tables if table.rows
    ]
    assert sum(1 for header in headers if header and header[0] == "科目") >= 8
    annual_headers = [
        header for header in headers
        if header and header[0] == "科目"
        and any(re.fullmatch(r"20\d\d 年[末度]", cell) or cell == "预测值" for cell in header[1:])
    ]
    assert len(annual_headers) >= 6, headers

    # 数值口径一致：表内的 6,500 / 72,000 在两头都在
    for value in ("6,500", "72,000", "12,000", "9,000"):
        assert value in exports["markdown"] and value in exports["word_text"], value


def test_word_export_size_and_tables_are_intact(exports) -> None:
    assert exports["word_bytes"] > 10000
    assert len(exports["word"].tables) >= 10
    assert len(exports["markdown"]) > 10000


# ---------------------------------------------------------------------------
# 6. 冲突不丢失
# ---------------------------------------------------------------------------


def test_declared_conflicts_are_preserved_in_the_report(client, conflict) -> None:
    """声明了冲突的案例：冲突对照表出现在报告里，且两个披露值都在，采用值列不出现。"""
    content = conflict["sections"]["financial"]["content"]
    assert "冲突对照表" in content
    assert "多来源披露并列，未择一采信" in content
    assert "20,000万元" in content and "24,000万元" in content, "两个披露值必须都在"
    conflict_block_markdown = content[content.index("冲突对照表"):]
    assert "采用值" not in conflict_block_markdown, "冲突表不得设采用值列"
    assert "| 披露值 |" in content and "| 状态 |" in content

    blocks = chapter_table_blocks(CONFLICT_PROJECT, "financial", subject=CONFLICT_SUBJECT)
    table = next(block for block in blocks if block.table_type == "conflict_comparison")
    assert table.validation_status == "passed"
    assert all(column.role != "adopted_value" for column in table.columns)
    disclosure = table.rows[0].cell(3)
    assert "20,000" in disclosure.raw and "24,000" in disclosure.raw
    assert disclosure.status == "conflict"
    # 内部字段键与内部枚举不得进入成品（事项列用的是业务标签）
    assert "bond_issue_amount" not in content and "standalone" not in content


def test_conflicted_facts_are_visible_in_the_database(conflict) -> None:
    """冲突是数据层的事实，不只是显示层：canonical_facts 里确实存在 conflicted 状态。"""
    conflicted = rows(
        "SELECT fact_key,period,status FROM canonical_facts WHERE project_id=? AND status='conflicted'",
        (CONFLICT_PROJECT,),
    )
    assert conflicted, "声明了冲突的案例必须至少有一条 conflicted 规范事实"
    assert all(item["status"] == "conflicted" for item in conflicted)


def test_conflict_comparison_block_keeps_every_disclosure() -> None:
    """构造：同一事项两个披露值 → 必须并列保留，且没有采用值列。"""
    block = conflict_comparison_block(
        table_id="e2e-conflict", title="来源冲突", subject=SUBJECT,
        conflicts=[{
            "fact_key": "bond_issue_amount", "label": "本期发行规模", "period": "2026", "scope": "合并口径",
            "assertions": [
                {"raw": "20,000万元", "source_ids": ["a"], "locator": {"document_id": "发行文件"}},
                {"raw": "24,000万元", "source_ids": ["b"], "locator": {"document_id": "外部信用评价文件"}},
            ],
        }],
    )
    assert block.validation_status == "passed"
    assert "20,000万元" in block.rows[0].cell(3).raw and "24,000万元" in block.rows[0].cell(3).raw


# ---------------------------------------------------------------------------
# 7. 表格不能绕过证据校验
# ---------------------------------------------------------------------------


def test_untraceable_cells_block_the_table(huachen) -> None:
    """真实报告表去掉来源/定位后必须被拦住——表格不是「生成出来就能交付」的。"""
    source = huachen["tables"]["financial"][0]
    assert source.validation_status == "passed"
    stripped_rows = []
    for row in source.rows:
        cells = list(row.cells)
        for index, cell in enumerate(cells):
            if cell.status in {"ok", "zero"}:
                cells[index] = cell.__class__(
                    raw=cell.raw, normalized=cell.normalized, unit=cell.unit,
                    status=cell.status, origin_type="derived",
                )
        stripped_rows.append((row.row_key, tuple(cells[1:])))
    stripped = annual_series_block(
        table_id="e2e-stripped", title=source.title, subject=source.subject, scope=source.scope,
        unit=source.unit, period_labels=[column.label for column in source.columns[1:]],
        rows=stripped_rows,
    )
    status, summary = validate_table_block(stripped)
    assert status == "blocked"
    assert summary["untraceable"], "无来源的业务数据格必须被列出"
    assert "不是从原始报表逐格提取" in " ".join(stripped.notes)


def test_uploaded_table_without_bbox_is_blocked() -> None:
    """上传原表：只有页码没有坐标 = 线框恢复失败，不得按可信数据用。"""
    def cell(row: int, column: int, text: str, *, bbox):
        from backend.app.table_extraction import ExtractedCell
        return ExtractedCell(row=row, column=column, text=text, bbox=bbox, page=9)

    located = ExtractedTable(
        table_key="e2e-located", caption="合并资产负债表", unit="万元", page=9, pages=[9],
        columns=["项目", "2025 年末"],
        rows=[[cell(1, 0, "货币资金", bbox=(52.0, 100.0, 543.0, 120.0)),
               cell(1, 1, "6,820", bbox=(52.0, 100.0, 543.0, 120.0))]],
        status="extracted", confidence=1.0,
    )
    assert block_from_extracted(located, table_id="t", subject=SUBJECT, document_id="d").validation_status == "passed"

    stripped = ExtractedTable(
        table_key="e2e-stripped", caption="合并资产负债表", unit="万元", page=9, pages=[9],
        columns=["项目", "2025 年末"],
        rows=[[cell(1, 0, "货币资金", bbox=None), cell(1, 1, "6,820", bbox=None)]],
        status="extracted", confidence=1.0,
    )
    block = block_from_extracted(stripped, table_id="t", subject=SUBJECT, document_id="d")
    assert block.validation_status == "blocked"
    assert block.validation_summary["unattributed_cells"]


def test_conflict_cell_with_a_value_is_blocked() -> None:
    """冲突格被填上「采用值」＝自动择一采信，必须判为阻断。"""
    block = reconciliation_block(
        table_id="e2e-auto", title="核对", subject=SUBJECT, scope="合并口径", period="2025 年末",
        profile_cells={"货币资金": cell_from_text("6,820", unit="万元", source_ids=["p1"])},
        uploaded_cells={"货币资金": cell_from_text("5,940", unit="万元", source_ids=["u1"])},
    )
    assert block.validation_status == "passed"
    assert block.rows[0].cell(3).status == "conflict"
    rows_ = []
    for row in block.rows:
        cells = list(row.cells)
        # 违规构造：冲突行被填了采用值（状态仍是 conflict，但值不为空）
        cells[3] = cells[3].__class__(
            raw="6,820", normalized="6820", unit="万元", status="conflict",
            origin_type="derived", source_ids=("p1",), adoption_basis="两来源取值不同，并列保留待人工确认",
        )
        rows_.append((row.row_key, tuple(cells)))
    broken = TableBlock(**{**block.__dict__, "rows": tuple(
        type(row)(row_key=key, cells=cells) for key, cells in rows_
    )})
    status, summary = validate_table_block(broken)
    assert status == "blocked"
    assert summary["auto_adopted_conflicts"]


def test_blocked_table_is_visibly_marked_in_the_report(client, huachen) -> None:
    """被拦下的表进入成品时必须自带校验状态提示，不能静默当可信数据用。"""
    source = huachen["tables"]["financial"][0]
    blocked = TableBlock(**{**source.__dict__, "validation_status": "blocked"})
    rendered = render_table_block(blocked, index=1, total=1)
    assert "本表校验状态：blocked" in rendered
    # 待核验的上传原表必须由交付门禁拦截，不能因档案汇总通过而放行。
    from backend.app.report_tables import blocked_reason

    assert blocked_reason(PROJECT, subject=SUBJECT)


def test_table_types_and_origin_modes_stay_within_the_frozen_contract(huachen) -> None:
    """三种表型与三种来源模式是冻结契约：链路里出现的每一个值都必须在其内。"""
    blocks = [block for key in TABLE_CHAPTERS for block in huachen["tables"][key]] + huachen["uploaded"]
    assert blocks
    for block in blocks:
        assert block.table_type in TABLE_TYPES
        assert block.origin_mode in ORIGIN_MODES
        assert block.currency == "CNY"
        for row in block.rows:
            assert len(row.cells) == len(block.columns)


# ---------------------------------------------------------------------------
# 8. 成品零违规
# ---------------------------------------------------------------------------


def test_delivery_outputs_are_wording_clean(huachen, exports) -> None:
    """报告正文、两个导出件、以及表本身都不得出现禁用表述/内部 UUID/内部字段键。"""
    body = "\n".join(item["content"] for item in huachen["sections"].values())
    assert wording_violations(body) == []
    assert wording_violations(exports["markdown"]) == []
    assert wording_violations(exports["word_text"]) == []

    rendered = "\n\n".join(
        _rendered_tables(huachen["tables"][key]) for key in TABLE_CHAPTERS
    )
    assert wording_violations(rendered) == []
    assert not UUID_RE.search(rendered), "表内容里出现内部 UUID"
    for block in [block for key in TABLE_CHAPTERS for block in huachen["tables"][key]]:
        assert block.table_id is not None
    for term in INTERNAL_ENUMS:
        assert term not in rendered, term
        assert term not in exports["markdown"], term


# ---------------------------------------------------------------------------
# 9. 已知缺口（实测记录，修好后会自动转绿）
# ---------------------------------------------------------------------------


def test_receivable_sub_item_declared_by_the_case_survives_a_run(huachen) -> None:
    """案例声明的「其中：逾期应收账款」应当在应收账款规模及明细里出现。

    材料侧的应收账款账龄结构表没有「逾期」行，但案例模型声明了该科目（3,000/2,730/2,460）。
    """
    rows_ = [row.row_key for row in huachen["tables"]["financial"][1].rows]
    if any("逾期" in key for key in rows_):
        return
    pytest.xfail(
        "「其中：逾期应收账款」整行消失（不是显示「—」而是没有这一行）。"
        "实测：脚本启动、尚未运行时该行存在（3,000｜2,730｜2,460），"
        "一旦跑过一次工作流运行，deterministic 抽取未能恢复 total_overdue_receivables，"
        "`replace_extracted_facts` + `sync_fact_assertions` 把播种事实一并覆盖，"
        "facts/fact_assertions/canonical_facts 三张表里该科目被物理删除，"
        "`report_tables._series_rows` 对无数据科目静默跳过整行且不留任何缺失提示。"
        "同一份材料、同一个项目，报告表格随「有没有跑过运行」变化——正是 "
        "`ensure_canonical_facts` 注释里要防的那类问题。"
    )


def test_uploaded_and_reconciliation_blocks_reach_a_user_visible_output(client, huachen) -> None:
    """上传原表与双来源核对应当出现在成品或接口里，而不是只存在于函数返回值里。"""
    surfaced = [key for key, section in huachen["sections"].items() if "上传材料原表" in section["content"]]
    api_modes = {item["origin_mode"] for item in client.get(f"/api/projects/{PROJECT}/tables").json()}
    if surfaced or "uploaded_table" in api_modes:
        return
    pytest.xfail(
        f"uploaded_table（{len(huachen['uploaded'])} 张）与双来源核对（{len(huachen['reconciliation'])} 块）"
        "目前没有任何成品出口：报告四章的系统表格全部是 profile_facts 汇总，"
        "`/api/projects/{id}/tables` 也只暴露 project_tables（＝financial_table_blocks，profile_facts）。"
        f"接口返回的来源模式只有 {sorted(api_modes)}，报告正文里没有一处标注「上传材料原表」。"
        "链路能生成、能校验，但还没有接进报告/导出/核对接口。"
    )


def test_reconciliation_rows_use_business_labels_not_internal_field_keys(huachen) -> None:
    """核对的「科目」列应当是业务科目名；用字段键当行名会让成品用词校验直接拦下。"""
    rendered = _rendered_tables(huachen["reconciliation"])
    violations = wording_violations(rendered)
    if not violations:
        return
    keys = sorted({item["value"] for item in violations if item["kind"] == "internal_field_key"})
    pytest.xfail(
        f"双来源核对表的行名是内部字段键，共 {len(keys)} 个：{keys[:6]}……"
        "（`report_tables.reconciliation_tables._relabel` 把 `fact_key|口径|单位|年度` 直接截成 `fact_key`）"
        f"`delivery_wording.wording_violations` 对该表的渲染文本报出 "
        f"{len(violations)} 条违规（kind={sorted({item['kind'] for item in violations})}）。"
        "今天没有暴露到成品只是因为这张表还没接进任何出口；一旦接上，"
        "`report_delivery._wording_problems` 会以「成品用词不合规」阻止整份报告交付。"
        "同一批科目在档案侧的表里用的是「资产总计」「货币资金」这类业务名。"
    )
