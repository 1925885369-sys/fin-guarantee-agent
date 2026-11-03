"""坐标重建（无边框三线表）的契约测试。

真实素材（北投评级报告 p4/p9/p16）只能做人工基准，不能进 CI；这里用合成 PDF 固化
四条契约：①无边框表能还原成正确行列；②缺标签行不吞数值；③碎片判定不误伤正常页；
④没有表头就不重建（宁可不做，也不猜列名）。
"""
from __future__ import annotations

import fitz

from backend.app.pdf_table_rebuild import _fragmentary, rebuild_page_tables


def _borderless_pdf(path, *, rows: int = 5, with_header: bool = True, missing_label_row: bool = False):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    columns = [60, 180, 280, 380, 480]
    y = 90
    page.insert_text((60, y - 10), "Table 1: borderless test", fontsize=9)
    if with_header:
        for x, text in zip(columns, ["Item", "2023", "2024", "2025", "2026"]):
            page.insert_text((x, y), text, fontsize=9)
        y += 20
    for index in range(rows):
        values = [f"Row{index + 1}"] + [f"{index + 1},{100 + index}.5" for _ in range(4)]
        if missing_label_row and index == 1:
            values = ["", *values[1:]]
        for x, text in zip(columns, values):
            if text:
                page.insert_text((x, y), text, fontsize=9)
        y += 20
    doc.save(path)
    doc.close()
    return path


def _captions(_lower, _upper):
    return ("Table 1: borderless test", "", "title_hint")


def test_borderless_table_is_rebuilt_with_header(tmp_path):
    path = _borderless_pdf(tmp_path / "borderless.pdf", rows=5)
    doc = fitz.open(path)
    tables = rebuild_page_tables(doc[0], 1, caption_for=_captions)
    doc.close()
    assert len(tables) == 1
    table = tables[0]
    assert table.columns == ["Item", "2023", "2024", "2025", "2026"]
    assert len(table.rows) == 5
    assert table.rows[0][0].text == "Row1"
    assert table.rows[0][1].text == "1,100.5"
    assert table.extraction_method == "coordinates"


def test_missing_label_row_keeps_numbers(tmp_path):
    """标签折行导致少一列时：补空标签、数值不丢，并留待核注记。"""
    path = _borderless_pdf(tmp_path / "missing_label.pdf", rows=4, missing_label_row=True)
    doc = fitz.open(path)
    tables = rebuild_page_tables(doc[0], 1, caption_for=_captions)
    doc.close()
    table = tables[0]
    assert len(table.columns) == 5
    blank_label_row = next(row for row in table.rows if not row[0].text.strip())
    assert blank_label_row[1].text == "2,101.5"
    assert any("行归属待人工核对" in note for note in table.notes)


def test_no_header_no_rebuild(tmp_path):
    """没有表头（首行即数值）时不重建：列语义不明，猜列名比不重建更糟。"""
    path = _borderless_pdf(tmp_path / "no_header.pdf", rows=4, with_header=False)
    doc = fitz.open(path)
    tables = rebuild_page_tables(doc[0], 1, caption_for=_captions)
    doc.close()
    assert tables == []


class _FakeTable:
    def __init__(self, rows: int):
        self._rows = [["x"] * 5 for _ in range(rows)]

    def extract(self):
        return self._rows


def test_fragmentary_detection_positive():
    """同页 ≥4 张、≥60% 是单行表 → 碎片（北投 p4=10/10、p16=6/7 实测）。"""
    assert _fragmentary([_FakeTable(1) for _ in range(10)]) is True
    assert _fragmentary([_FakeTable(1) for _ in range(6)] + [_FakeTable(3)]) is True


def test_fragmentary_detection_negative():
    """正常页：少表、或多行表 → 不触发（中邮 p6=2 张、p11=1 张，行为必须不变）。"""
    assert _fragmentary([_FakeTable(5), _FakeTable(5)]) is False
    assert _fragmentary([_FakeTable(15)]) is False
    assert _fragmentary([_FakeTable(1), _FakeTable(2), _FakeTable(3)]) is False
