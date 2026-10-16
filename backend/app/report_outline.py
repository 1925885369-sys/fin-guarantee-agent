"""报告标题层级与论文式编号的单一来源。

报告正文以 Markdown 分块存储：章标题来自 `report_sections.title`，章内分块标题是
正文里的 `### 标题`。编号**不写入正文**，而是在导出与前端展示时按层级计算，原因：

1. LLM 受控改写会整体替换正文，写死的编号会丢失或被改写；
2. 章节可单独重新生成，写死编号会重复累加；
3. Markdown、Word、前端三个出口需要完全一致的编号。

层级体例（论文式）：一级 一、二、三…；二级 （一）（二）…；三级 1. 2. 3.…
"""

from __future__ import annotations

import re

from .chapter_prompts import BOND_CHAPTER_PARTS

CN_DIGITS = "一二三四五六七八九十"
_PART_PREFIX_RE = re.compile(r"^第[一二三四五六七八九十百]+部分[\s　]*")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*+]\s+")
_ORDERED_RE = re.compile(r"^\d+[.、]\s+")


def cn_number(value: int) -> str:
    """1 → 一、11 → 十一、20 → 二十、21 → 二十一。"""
    if value <= 0:
        return str(value)
    if value <= 10:
        return CN_DIGITS[value - 1]
    if value < 20:
        return "十" + CN_DIGITS[value - 11]
    tens, ones = divmod(value, 10)
    suffix = CN_DIGITS[ones - 1] if ones else ""
    if tens > 10:  # 超出报告实际需要，退化为阿拉伯数字
        return str(value)
    return CN_DIGITS[tens - 1] + "十" + suffix


def cn_paren(value: int) -> str:
    """1 → （一）。"""
    return f"（{cn_number(value)}）"


def part_display_title(part_title: str) -> str:
    """去掉分部名里自带的“第X部分 ”前缀，避免与一级编号重复。"""
    stripped = _PART_PREFIX_RE.sub("", part_title).strip()
    return stripped or part_title


def part_label(index: int, part_title: str) -> str:
    """一级标题：一、报告摘要及审查范围。"""
    return f"{cn_number(index)}、{part_display_title(part_title)}"


def chapter_label(index: int, title: str) -> str:
    """二级标题：（一）审查范围、材料状态和免责声明。"""
    return f"{cn_paren(index)}{title}"


def _split_markdown_blocks(content: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    for raw in re.split(r"\n\s*\n", (content or "").strip()):
        lines = [line.rstrip() for line in raw.splitlines() if line.strip()]
        if lines:
            blocks.append(lines)
    return blocks


def content_blocks(content: str) -> list[dict]:
    """把章内正文解析为带层级与编号的展示分块。

    正文里最浅的标题层级对应报告的三级标题（编号 1. 2. 3.），更深的标题降为
    四级（不编号）。最深的分块标题也不会升到章标题之上，避免模型输出 `##` 时
    与章标题同级。
    """
    raw_blocks = _split_markdown_blocks(content)
    depths = [len(match.group(1)) for lines in raw_blocks if (match := _HEADING_RE.match(lines[0].lstrip()))]
    shallowest = min(depths) if depths else 0

    blocks: list[dict] = []
    heading_index = 0
    for lines in raw_blocks:
        match = _HEADING_RE.match(lines[0].lstrip())
        if match:
            depth = len(match.group(1))
            text = match.group(2).strip()
            if depth <= shallowest:
                heading_index += 1
                blocks.append({
                    "kind": "heading", "level": 3, "index": heading_index,
                    "number": f"{heading_index}.", "label": f"{heading_index}. {text}", "text": text,
                })
            else:
                blocks.append({"kind": "heading", "level": 4, "index": None, "number": "", "label": text, "text": text})
            body = lines[1:]
        else:
            body = lines
        if not body:
            continue
        if all(_BULLET_RE.match(line.lstrip()) for line in body):
            blocks.append({"kind": "list", "ordered": False, "items": [_BULLET_RE.sub("", line.lstrip()) for line in body]})
        elif all(_ORDERED_RE.match(line.lstrip()) for line in body):
            blocks.append({"kind": "list", "ordered": True, "items": [_ORDERED_RE.sub("", line.lstrip()) for line in body]})
        elif all(line.lstrip().startswith(">") for line in body):
            blocks.append({"kind": "quote", "text": " ".join(line.lstrip()[1:].strip() for line in body)})
        elif all(line.strip().startswith("|") and line.strip().endswith("|") for line in body):
            blocks.append({"kind": "table", "rows": [line.strip() for line in body]})
        else:
            blocks.append({"kind": "paragraph", "text": " ".join(line.strip() for line in body)})
    return blocks


def markdown_blocks(content: str) -> str:
    """按报告层级重排章内分块的 Markdown 标题级别（三级 → `####`）。

    章标题是 `###`，正文分块必须更深一级，否则 Markdown 中章与节同级。
    """
    output: list[str] = []
    for block in content_blocks(content):
        if block["kind"] == "heading":
            output.append(f"{'#' * (block['level'] + 1)} {block['label']}")
        elif block["kind"] == "list":
            if block.get("ordered"):
                output.append("\n".join(f"{index}. {item}" for index, item in enumerate(block["items"], start=1)))
            else:
                output.append("\n".join(f"- {item}" for item in block["items"]))
        elif block["kind"] == "quote":
            output.append(f"> {block['text']}")
        elif block["kind"] == "table":
            output.append("\n".join(block["rows"]))
        else:
            output.append(block["text"])
    return "\n\n".join(output)


def normalized_outline(project_id: str) -> dict | None:
    """读取生效目录并做**展示用词规范化**（历史报告只做展示转换，P0-2）。

    网页/导出/快照共用：目录标题里的原材料名在返回副本上替换，目录存储保持原文。
    """
    try:
        from .report_outline_store import effective_outline

        outline = effective_outline(project_id)
    except Exception:
        return None
    return _normalize_outline_titles(project_id, outline)


def outline_for_grouping(project_id: str) -> dict | None:
    """生效目录（展示装饰用，P1）：读取失败返回 None（调用方回退旧行为）。

    返回前对**分部与章节标题**做成品用词规范化（旧计划剩余项 P0-2）——内置目录标题
    （如 ``评级报告复核及评级差异``）、项目/全局覆盖自定义标题都可能带原材料名，
    而目录标签直接进网页目录与 MD/Word 导出。只改返回副本，不改目录存储。
    """
    return normalized_outline(project_id)


def _normalize_outline_titles(project_id: str, outline: dict | None) -> dict | None:
    if not outline:
        return outline
    from .delivery_wording import material_numbers, normalize_report_text

    numbers = material_numbers(project_id)
    parts: list[dict] = []
    for part in outline.get("parts") or []:
        chapters = [
            {**chapter, "title": normalize_report_text(str(chapter.get("title") or ""), numbers)}
            for chapter in part.get("chapters") or []
        ]
        parts.append({
            **part,
            "title": normalize_report_text(str(part.get("title") or ""), numbers),
            "chapters": chapters,
        })
    return {**outline, "parts": parts}


def grouped_sections(sections: list[dict], parts=BOND_CHAPTER_PARTS,
                     outline: dict | None = None) -> list[dict]:
    """按报告分部归集章节并编号。

    返回 `[{"part", "label", "chapters": [{"section", "number", "label"}]}]`；
    未列入任何分部的历史章节各自成为一个后续分部（`chapters` 为空，`section` 指向自身）。

    ``outline``（生效目录）提供时按目录分组与排序——章节标题以目录为准（改名立即生效，
    P1-4）；不在目录中的 section（被删章节的旧正文尚未再生的情形）仍附在最后，保证可见。
    """
    by_key = {section["section_key"]: section for section in sections}
    groups: list[dict] = []
    seen: set[str] = set()
    if outline:
        for part in outline.get("parts") or []:
            chapters = []
            for index, chapter in enumerate(part.get("chapters") or [], start=1):
                key = str(chapter.get("template_key") or chapter.get("chapter_id") or "")
                section = by_key.get(key)
                if not section:
                    continue
                seen.add(key)
                title = str(chapter.get("title") or section["title"])
                chapters.append({
                    "section": section,
                    "number": cn_paren(index),
                    "label": chapter_label(index, title),
                })
            if chapters:
                part_title = str(part.get("title") or "")
                groups.append({"part": part_title, "label": part_label(len(groups) + 1, part_title), "chapters": chapters})
    else:
        for part_title, keys in parts:
            present = [by_key[key] for key in keys if key in by_key]
            if not present:
                continue
            chapters = []
            for index, section in enumerate(present, start=1):
                seen.add(section["section_key"])
                chapters.append({
                    "section": section,
                    "number": cn_paren(index),
                    "label": chapter_label(index, section["title"]),
                })
            groups.append({"part": part_title, "label": part_label(len(groups) + 1, part_title), "chapters": chapters})
    for section in sections:
        if section["section_key"] in seen:
            continue
        number = cn_number(len(groups) + 1)
        groups.append({
            "part": section["title"],
            "label": f"{number}、{section['title']}",
            "chapters": [],
            "section": section,
        })
    return groups


def section_labels(sections: list[dict], parts=BOND_CHAPTER_PARTS,
                   outline: dict | None = None) -> dict[str, dict]:
    """`section_key` → 分部与章的编号信息，供报告 API 与前端展示使用。"""
    labels: dict[str, dict] = {}
    for group in grouped_sections(sections, parts, outline=outline):
        part_number = group["label"].split("、", 1)[0]
        for chapter in group["chapters"]:
            labels[chapter["section"]["section_key"]] = {
                "part": group["part"],
                "part_number": part_number,
                "part_label": group["label"],
                "chapter_number": chapter["number"],
                "chapter_label": chapter["label"],
            }
        fallback = group.get("section")
        if fallback is not None:
            labels[fallback["section_key"]] = {
                "part": group["part"],
                "part_number": part_number,
                "part_label": group["label"],
                "chapter_number": "",
                "chapter_label": "",
            }
    return labels
