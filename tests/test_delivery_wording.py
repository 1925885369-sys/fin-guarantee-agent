"""成品用词规范化与校验（Phase 5.3）。

规格见 ``PLAN3.md`` §3：报告成品里不出现原材料种类名，也不出现内部标识；
**规范化必须在保存前完成、再做校验与哈希**；工作台证据详情仍显示原件名称。
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from backend.app.database import execute, row
from backend.app.delivery_wording import (
    FORBIDDEN_TERMS, document_series, material_numbers, normalize_report_text, wording_violations,
)
from backend.app.main import app
from backend.app.report_delivery import delivery_blockers, write_report_section


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_alias_table_maps_material_names_to_product_wording() -> None:
    numbers = {"发行文件": "资料01", "外部信用评价文件": "资料02"}
    assert normalize_report_text("募集说明书", numbers) == "发行文件〔资料01〕"
    assert normalize_report_text("信用评级报告", numbers) == "外部信用评价文件〔资料02〕"
    assert normalize_report_text("评级报告", numbers) == "外部信用评价文件〔资料02〕"


def test_specific_alias_wins_over_the_shorter_one() -> None:
    """「评级报告复核及评级差异」必须先于「评级报告」替换，否则会留下残句。"""
    out = normalize_report_text("评级报告复核及评级差异", {})
    assert out == "外部信用评级与差异分析"
    assert "外部信用评价文件" not in out


def test_normalization_is_idempotent() -> None:
    """重复规范化不得叠加编号后缀——正文与哈希要求结果稳定。"""
    numbers = {"发行文件": "资料01"}
    once = normalize_report_text("据募集说明书披露", numbers)
    assert normalize_report_text(once, numbers) == once


def test_normalization_without_numbers_still_removes_forbidden_terms() -> None:
    assert normalize_report_text("募集说明书", {}) == "发行文件"


def test_violations_detect_terms_uuids_and_field_keys() -> None:
    text = (
        "据募集说明书披露，bond_issue_amount 为 20000，"
        "来源 0e8d2e49-71b9-42ac-ba31-9669570ddb58。"
    )
    kinds = {item["kind"] for item in wording_violations(text)}
    assert kinds == {"forbidden_term", "internal_uuid", "internal_field_key"}


def test_clean_text_has_no_violations() -> None:
    assert wording_violations("发行文件〔资料01〕第 9 页披露本期发行规模 20,000 万元。") == []


def test_document_series_is_stable_and_ordered(client) -> None:
    """资料编号写进成品并被引用，不能随查询顺序漂移。"""
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    first = document_series("demo-bond-huachen")
    assert document_series("demo-bond-huachen") == first
    assert sorted(first.values()) == ["资料01", "资料02"]
    numbers = material_numbers("demo-bond-huachen")
    assert numbers == {"发行文件": "资料01", "外部信用评价文件": "资料02"}


def test_write_normalizes_title(client) -> None:
    """章节标题在写入时规范化，且在读取（GET /report）侧对历史行同样规范化。

    旧实现只在导出时改标题，库里的 ``title`` 仍是"评级报告复核及评级差异"，
    GET /report 原样返回 → 网页显示禁用词、与 Word 导出不一致（P0-2 遗漏）。
    """
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    write_report_section(
        project_id="demo-bond-huachen", section_key="wording_probe", title="评级报告复核及评级差异",
        content="本章分析。", content_origin="deterministic",
    )
    stored = row(
        "SELECT title FROM report_sections WHERE project_id=? AND section_key=?",
        ("demo-bond-huachen", "wording_probe"),
    )
    assert stored["title"] == "外部信用评级与差异分析"
    execute(
        "DELETE FROM report_sections WHERE project_id=? AND section_key=?",
        ("demo-bond-huachen", "wording_probe"),
    )


def test_report_endpoint_normalizes_historical_titles(client) -> None:
    """写入侧没规范化的**历史行**，读取侧也必须规范化（交付一致性）。"""
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    write_report_section(
        project_id="demo-bond-huachen", section_key="wording_probe", title="用词探针",
        content="本章分析。", content_origin="deterministic",
    )
    # 模拟历史行：直接改库绕过写入侧规范化。
    execute(
        "UPDATE report_sections SET title=? WHERE project_id=? AND section_key=?",
        ("评级报告复核及评级差异", "demo-bond-huachen", "wording_probe"),
    )
    try:
        report = client.get("/api/projects/demo-bond-huachen/report").json()
        probe = next(item for item in report if item["section_key"] == "wording_probe")
        assert probe["title"] == "外部信用评级与差异分析"
        assert all(
            "评级报告复核及评级差异" not in str(item.get("part_label") or "")
            for item in report
        )
    finally:
        execute(
            "DELETE FROM report_sections WHERE project_id=? AND section_key=?",
            ("demo-bond-huachen", "wording_probe"),
        )


def test_table_block_normalization_covers_header_cells_and_notes() -> None:
    """表名／列标签／单元格／表注都要规范化——只在写入侧改正文不够：

    网页、Markdown、Word 都从冻结快照重新渲染表格，快照里的原文会直接上版面
    （实测：中邮「主体评级历史」表头列 ``评级报告``、覆盖表单元格 ``信用评级报告``）。
    规范化返回**副本**，冻结快照保持原称。
    """
    from backend.app.delivery_wording import normalize_table_block
    from backend.app.table_block import TableBlock, TableCell, TableColumn, TableRow

    raw = TableBlock(
        table_id="uploaded-p001-t01", table_type="annual_series", title="主体评级历史",
        subject="示例主体", scope="信用评级报告／原表口径（待核验）", currency="CNY", unit="",
        columns=(TableColumn(key="a", label="评级报告", role="disclosure"),),
        rows=(TableRow(row_key="r1", cells=(TableCell(raw="信用评级报告", status="text"),)),),
        notes=("注：来源为评级报告。",), origin_mode="uploaded_table",
    )
    numbers = {"发行文件": "资料01", "外部信用评价文件": "资料02"}
    clean = normalize_table_block(raw, numbers)
    assert clean.columns[0].label == "外部信用评价文件〔资料02〕"
    assert clean.rows[0].cells[0].raw == "外部信用评价文件〔资料02〕"
    assert "评级报告" not in clean.notes[0]
    assert "信用评级报告" not in clean.scope
    assert clean.title == "主体评级历史"
    # 冻结快照（原块）不被改动：证据详情与材料仍显示原称。
    assert raw.columns[0].label == "评级报告"
    assert raw.rows[0].cells[0].raw == "信用评级报告"


def test_section_blocks_normalizes_frozen_table_snapshot(monkeypatch, client) -> None:
    """展示层（网页/MD/Word 共用 ``section_blocks``）从快照渲染时同样规范化。"""
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    from backend.app import report_blocks
    from backend.app.table_block import TableBlock, TableCell, TableColumn, TableRow

    raw = TableBlock(
        table_id="uploaded-p001-t01", table_type="annual_series", title="主体评级历史",
        subject="华辰精密制造有限公司", scope="原表口径（待核验）", currency="CNY", unit="",
        columns=(TableColumn(key="a", label="评级报告", role="disclosure"),),
        rows=(TableRow(row_key="r1", cells=(TableCell(raw="公开发行债券（第二期）信用评级报告", status="text"),)),),
        notes=("注：来源为评级报告。",), origin_mode="uploaded_table",
    )
    monkeypatch.setattr(report_blocks, "chapter_table_blocks", lambda *args, **kwargs: [raw])
    section = {"section_key": "rating", "content": "本章分析。", "title": "评级报告复核及评级差异"}
    blocks = report_blocks.section_blocks("demo-bond-huachen", section, subject="华辰精密制造有限公司")
    table = next(block for block in blocks if block["kind"] == "table")
    assert table["columns"][0]["label"] == "外部信用评价文件〔资料02〕"
    assert all("评级报告" not in line for line in table["rows"])
    assert any("外部信用评价文件〔资料02〕" in line for line in table["rows"])
    assert "评级报告" not in table["notes"][0]
    assert raw.notes[0] == "注：来源为评级报告。"


def test_outline_titles_are_normalized_for_display(client) -> None:
    """生效目录的分部/章节标题也要规范化：标题直接进网页目录与 MD/Word 导出。

    内置目录标题取自元数据（``评级报告复核及评级差异``），项目覆盖的自定义标题
    同样可能带原材料名——规范化只改返回副本，不改目录存储。
    """
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    from backend.app.report_outline import outline_for_grouping

    outline = outline_for_grouping("demo-bond-huachen")
    assert outline and outline.get("parts"), "演示项目应有内置目录"
    titles = [str(chapter.get("title") or "") for part in outline["parts"] for chapter in part.get("chapters") or []]
    assert "外部信用评级与差异分析" in titles
    assert all("评级报告" not in title for title in titles), titles


def test_final_export_refuses_residual_forbidden_wording(client) -> None:
    """导出实际文本仍残留禁用词时，final 导出被拒绝并报位置（draft 仍可导出）。"""
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    from backend.app.exports import ExportWordingError, markdown_report

    write_report_section(
        project_id="demo-bond-huachen", section_key="wording_probe", title="用词探针",
        content="本章分析。", content_origin="deterministic",
    )
    from backend.app.database import execute
    # 绕过写入入口注入残留（模拟历史未规范化正文）。
    execute(
        "UPDATE report_sections SET content=? WHERE project_id=? AND section_key=?",
        ("本章分析。\n\n据募集说明书披露。", "demo-bond-huachen", "wording_probe"),
    )
    try:
        with pytest.raises(ExportWordingError) as excinfo:
            markdown_report("demo-bond-huachen", "final")
        assert "募集说明书" in str(excinfo.value)
        assert "行" in str(excinfo.value)
        assert "募集说明书" in markdown_report("demo-bond-huachen", "draft")
    finally:
        execute(
            "DELETE FROM report_sections WHERE project_id=? AND section_key=?",
            ("demo-bond-huachen", "wording_probe"),
        )


def test_write_normalizes_before_hashing(client) -> None:
    """正文与冻结披露一起规范化，且哈希在规范化之后计算。

    冻结披露要参与「逐字保留」比对：只改正文不改它，校验会立刻失败。
    """
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    block = "### 来源\n据募集说明书与信用评级报告披露。"
    content = f"本章分析。\n\n{block}"
    write_report_section(
        project_id="demo-bond-huachen", section_key="wording_probe", title="用词探针",
        content=content, required_disclosures=[block], deterministic_draft=content,
        content_origin="deterministic",
    )
    stored = row(
        "SELECT content,required_disclosures,content_sha256 FROM report_sections "
        "WHERE project_id=? AND section_key=?",
        ("demo-bond-huachen", "wording_probe"),
    )
    assert "募集说明书" not in stored["content"]
    assert "发行文件〔资料01〕" in stored["content"]
    # 冻结披露必须与正文同样规范化，否则「逐字保留」比对会判失败。
    assert "募集说明书" not in stored["required_disclosures"]
    assert stored["content_sha256"] == hashlib.sha256(stored["content"].encode("utf-8")).hexdigest()


def test_delivery_blockers_flag_wording_violations(client) -> None:
    """禁用词与内部标识出现时，最终导出被阻止。"""
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    polluted = "据募集说明书披露，字段 bond_name 指向 0e8d2e49-71b9-42ac-ba31-9669570ddb58。"
    write_report_section(
        project_id="demo-bond-huachen", section_key="wording_probe", title="用词探针",
        content=polluted, content_origin="deterministic",
    )
    try:
        # 直接写入会被规范化，所以这里绕过写入入口，模拟历史遗留的未规范化正文。
        from backend.app.database import execute
        execute(
            "UPDATE report_sections SET content=? WHERE project_id=? AND section_key=?",
            (polluted, "demo-bond-huachen", "wording_probe"),
        )
        blockers = delivery_blockers("demo-bond-huachen")
        assert any("成品用词不合规" in item for item in blockers), blockers
    finally:
        from backend.app.database import execute
        execute(
            "DELETE FROM report_sections WHERE project_id=? AND section_key=?",
            ("demo-bond-huachen", "wording_probe"),
        )


def test_reference_numbers_are_not_treated_as_data() -> None:
    """「发行文件〔资料01〕第 29 页」里的 01 与 29 不是被主张的数据值。

    规范化会把资料编号写成 ``〔资料01〕``，而对正文取数字的检查若先不剔除引用定位，
    就会把 ``01`` 当成一个新数字去证据里找——找不到，于是**正常引用被判成
    「无证据声明」**，交付哈希也随之失配。实测踩过：写入后重新校验，
    ``validated_content_sha256`` 再也绑不回正文。

    这里同时守住两条取数路径：``p0._numbers``（主张分类）与
    ``chapter_guard._number_mentions``（数字绑定/归属核对）。只修前者不够——
    记录绑定判定消费的是后者，"01" 会以「值 1、无单位」的形态进入数字判定，
    在该章冻结记录里查无同值记录后整句判 unsupported。
    """
    from backend.app.chapter_guard import _number_mentions
    from backend.app.delivery_wording import strip_references
    from backend.app.p0 import _numbers
    from backend.app.workflow import _numbers_supported

    assert strip_references("据发行文件〔资料01〕第 29 页披露") == "据发行文件披露"
    assert _numbers("据发行文件〔资料01〕第 29 页披露") == []
    assert _numbers("发行文件〔资料01〕披露营业收入 12,000 万元") == [12000.0]
    assert _number_mentions("据发行文件〔资料01〕第 29 页披露") == []
    assert [
        item["normalized_value"] for item in
        _number_mentions("据发行文件〔资料01〕第 9 页披露营业收入 100 万元。")
    ] == ["100"]
    assert _numbers_supported(
        "据发行文件〔资料01〕第 9 页披露营业收入 100 万元。",
        "营业收入100万元",
        [{"quote": "营业收入100万元"}],
    ) is True


def test_conflict_block_is_product_grade(client) -> None:
    """冲突块既进上下文又被逐字保留在成品里，因此不能带内部标识。"""
    from backend.app.database import rows

    client.get("/api/projects/demo-bond-huachen/bond-summary")
    content = rows(
        "SELECT content FROM report_sections WHERE project_id=? AND section_key=?",
        ("demo-bond-huachen", "bond_overview"),
    )
    if not content:
        pytest.skip("该演示项目尚未生成报告，跳过成品抽查")
    body = content[0]["content"]
    assert wording_violations(body) == [], wording_violations(body)
    assert "断言 " not in body
    assert "字段 " not in body
