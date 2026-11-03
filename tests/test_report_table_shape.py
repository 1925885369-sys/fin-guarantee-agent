"""正文管道表识别：GFM 允许省略行尾竖线。

2026-10-27 排查：放开 ``table_mode`` 后模型开始出表，但 ``content_blocks`` 旧判定要求
每行**首尾都有** ``|``。模型常写 ``| 2025 | 81.34 | 43.36%``（省略行尾），整块被降级成
paragraph —— 前端只显示一堆竖线文本，业务侧看到的就是"仍然没有表"。
"""
from __future__ import annotations

from backend.app.chapter_guard import _MARKDOWN_TABLE_ROW, count_report_chars
from backend.app.report_outline import content_blocks
from backend.app.workflow import _MARKDOWN_TABLE_LINE_RE

BOTH_EDGES = """### 收入结构

| 年度 | 收入（亿元） | 占比 |
| --- | --- | --- |
| 2025 | 81.34 | 43.36% |
"""

TRAILING_OMITTED = """### 收入结构

| 年度 | 收入（亿元） | 占比
| --- | --- | ---
| 2025 | 81.34 | 43.36%
"""


def _table_blocks(content: str) -> list[dict]:
    return [block for block in content_blocks(content) if block["kind"] == "table"]


def test_table_with_both_edges_is_table() -> None:
    blocks = _table_blocks(BOTH_EDGES)
    assert len(blocks) == 1
    assert len(blocks[0]["rows"]) == 3


def test_table_with_trailing_pipes_omitted_is_still_table() -> None:
    blocks = _table_blocks(TRAILING_OMITTED)
    assert len(blocks) == 1, "省略行尾竖线的合法 GFM 表格被降级成了段落"
    assert blocks[0]["rows"][2].startswith("| 2025")


def test_table_without_delimiter_row_is_not_table() -> None:
    """没有表头分隔行就不是表格——不能把普通竖线文本当表渲染。"""
    content = "### 说明\n\n| 这是正文里的竖线符号 | 不是表格\n| 第二行同样\n"
    assert _table_blocks(content) == []


def test_multiline_prose_starting_with_pipe_is_not_table() -> None:
    content = "### 说明\n\n| 单行以竖线开头但只有一列\n"
    assert _table_blocks(content) == []


def test_table_rows_excluded_from_visible_length() -> None:
    """正文表格行不计入篇幅（计划要求），省略尾竖线时同样要剔除。"""
    with_table = count_report_chars(TRAILING_OMITTED)
    without_table = count_report_chars("### 收入结构\n\n表格已省略。\n")
    assert with_table - without_table < 20


def test_line_patterns_accept_omitted_trailing_pipe() -> None:
    line = "| 2025 | 81.34 | 43.36%"
    assert _MARKDOWN_TABLE_ROW.search(line)
    assert _MARKDOWN_TABLE_LINE_RE.search(line)
    # 单列、只有一个竖线的行不算表格行
    assert not _MARKDOWN_TABLE_ROW.search("| 单列说明")
    assert not _MARKDOWN_TABLE_LINE_RE.search("| 单列说明")
