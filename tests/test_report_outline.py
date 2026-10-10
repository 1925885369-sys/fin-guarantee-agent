from __future__ import annotations

from backend.app.report_outline import (
    chapter_label, cn_number, cn_paren, content_blocks, grouped_sections, markdown_blocks,
    part_display_title, part_label, section_labels,
)


def _sections(*keys: str) -> list[dict]:
    from backend.app.chapter_prompts import BOND_CHAPTER_TITLES
    return [{"section_key": key, "title": BOND_CHAPTER_TITLES.get(key, key), "content": ""} for key in keys]


def test_chinese_numeral_and_label_style():
    assert [cn_number(value) for value in (1, 5, 10, 11, 15, 20, 21)] == [
        "一", "五", "十", "十一", "十五", "二十", "二十一",
    ]
    assert cn_paren(1) == "（一）"
    # 分部名自带“第X部分”前缀时，一级编号不重复书写。
    assert part_display_title("第一部分 发行人与本次债券") == "发行人与本次债券"
    assert part_display_title("报告摘要及审查范围") == "报告摘要及审查范围"
    assert part_label(2, "第一部分 发行人与本次债券") == "二、发行人与本次债券"
    assert part_label(6, "附录") == "六、附录"
    assert chapter_label(1, "审查范围、材料状态和免责声明") == "（一）审查范围、材料状态和免责声明"


def test_content_blocks_number_third_level_and_clamp_deeper_headings():
    content = (
        "### 审查范围\n\n本项目以示例公司为审查对象，材料不足。\n\n"
        "### 材料状态\n\n- 核心材料缺口：无\n- 补充材料缺口：1 项\n\n"
        "### 使用边界\n\n> 本报告不构成投资建议。"
    )
    blocks = content_blocks(content)
    headings = [item for item in blocks if item["kind"] == "heading"]
    assert [item["label"] for item in headings] == ["1. 审查范围", "2. 材料状态", "3. 使用边界"]
    assert {item["level"] for item in headings} == {3}
    assert [item["kind"] for item in blocks] == ["heading", "paragraph", "heading", "list", "heading", "quote"]
    assert blocks[3]["ordered"] is False

    # 模型把标题写成 `##`（与章标题同级）时，正文标题仍被压在三级，不会升到章之上。
    clamped = content_blocks("## 模型标题\n\n正文。\n\n#### 更深标题\n\n正文。")
    levels = [item["level"] for item in clamped if item["kind"] == "heading"]
    assert levels == [3, 4]
    assert [item["label"] for item in clamped if item["kind"] == "heading"] == ["1. 模型标题", "更深标题"]


def test_markdown_blocks_render_one_level_below_chapter():
    rendered = markdown_blocks("### 审查范围\n\n正文。")
    # 章标题是 `###`，正文分块必须更深一级（`####`）。
    assert rendered.startswith("#### 1. 审查范围")
    assert not rendered.startswith("### 1. ")


def test_grouped_sections_number_parts_and_chapters_per_part():
    sections = _sections("scope", "bond_overview", "governance", "operations", "proceeds", "evidence", "legacy_extra")
    groups = grouped_sections(sections)
    # 无章节的分部不参与编号，序号在已生成的分部间连续（不跳号）。
    assert [group["label"] for group in groups] == [
        "一、报告摘要及审查范围", "二、发行人与本次债券", "三、附录", "四、legacy_extra",
    ]
    assert [chapter["label"] for chapter in groups[1]["chapters"]] == [
        "（一）发行人主体、股权和治理", "（二）主营业务及经营稳定性", "（三）募集资金用途及专户安排",
    ]
    assert groups[3]["chapters"] == [] and groups[3]["section"]["section_key"] == "legacy_extra"


def test_section_labels_expose_part_and_chapter_numbers():
    labels = section_labels(_sections("scope", "governance", "operations"))
    assert labels["scope"] == {
        "part": "报告摘要及审查范围", "part_number": "一", "part_label": "一、报告摘要及审查范围",
        "chapter_number": "（一）", "chapter_label": "（一）审查范围、材料状态和免责声明",
    }
    assert labels["operations"]["chapter_number"] == "（二）"
    assert labels["operations"]["part_label"] == "二、发行人与本次债券"
