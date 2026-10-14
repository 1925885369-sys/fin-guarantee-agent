"""成品用词规范化与校验。

报告成品里不出现原材料的种类名，也不出现内部标识（见 ``PLAN3.md`` §3）：

| 禁用表述 | 成品显示 |
| --- | --- |
| 募集说明书 | 发行文件〔资料NN〕 |
| 信用评级报告 / 评级报告 | 外部信用评价文件〔资料NN〕 |
| 评级报告复核及评级差异 | 外部信用评级与差异分析 |

两条纪律
--------
1. **规范化在保存前完成，再做校验与哈希。** 正文化名与 ``content_sha256`` 一旦不同步，
   历史交付一致性校验会整体失真。因此本模块只在 ``write_report_section`` 这一个写入
   入口被调用，正文、冻结披露与确定性草稿**一起**规范化——冻结披露要参与「逐字保留」
   比对，只改正文不改它就会把校验判成失败。
2. **不改证据存储。** 后台仍保留原文件名、材料类型、证据 ID 与页码；工作台的证据详情
   仍然显示原件名称。本模块只处理成品文本，不触碰 ``documents`` / ``sources``。
"""
from __future__ import annotations

import re

from .database import rows


# 顺序即优先级：更长、更具体的表述必须先替换，否则「信用评级报告」会被
# 「评级报告」先吃掉一半，留下「信用外部信用评价文件」这种残句。
# 每项为 ``(禁用表述, 成品表述, 用于查资料编号的材料标签)``；标签为空表示不编号。
MATERIAL_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("评级报告复核及评级差异", "外部信用评级与差异分析", ""),
    ("信用评级报告", "外部信用评价文件", "外部信用评价文件"),
    ("评级报告", "外部信用评价文件", "外部信用评价文件"),
    ("募集说明书", "发行文件", "发行文件"),
)

# 成品里不允许出现的表达（别名表中所有被替换掉的写法）。
FORBIDDEN_TERMS: tuple[str, ...] = tuple(source for source, _, _ in MATERIAL_ALIASES)

_INTERNAL_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# 引用定位：资料编号与页码。它们**不是被主张的数据值**，任何对正文取数字的检查
# 都必须先把它们剔除——否则「发行文件〔资料01〕第 29 页」会被读成两个数字，
# 而证据里当然找不到，于是正常引用被判成「无证据声明」。
# 页码部分与 ``chapter_guard.PAGE_REFERENCE_RE`` 同源，避免两套规则对不上。
# 证据引用别名（E01/E05，与 ``chapter_guard.CITATION_ALIAS_RE`` 同源）：不剔除时
# "（E05、E06、E07）"里的 05/06/07 会被 ``\d+`` 读成数值 5/6/7（数字前是数字，
# 字母 lookbehind 拦不住后半个字符）——北投 governance 实测据此误报。
# 范围连接号必须含**全角破折号**（—–―－）：PDF 回流把"第77—81页"写成全角破折号，
# 类里只有 ASCII `-` 时漏剔，77/81 会被读成裸数字（北投 financial 缺口句实测）。
PAGE_REFERENCE_PATTERN = r"第\s*[\d,，、\s\-~～—–―－至]+\s*页"
CITATION_ALIAS_PATTERN = r"\bE\d{2,}\b"
REFERENCE_PATTERN = re.compile(rf"〔\s*资料\s*\d+\s*〕|{PAGE_REFERENCE_PATTERN}|{CITATION_ALIAS_PATTERN}")


def strip_references(text: str) -> str:
    return REFERENCE_PATTERN.sub("", text or "")
_SNAKE_TOKEN = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")

MATERIAL_ROLES: tuple[tuple[str, str], ...] = (
    ("prospectus", "发行文件"),
    ("rating_report", "外部信用评价文件"),
)


def document_series(project_id: str) -> dict[str, str]:
    """项目内稳定的资料编号：``document_id -> 资料NN``。

    按材料角色排序（发行文件在前），角色内按文件名排序，保证同一项目每次生成的
    编号一致——编号会写进成品并被引用，不能随查询顺序漂移。
    """
    records = rows(
        "SELECT id,document_role,filename FROM documents WHERE project_id=? ORDER BY filename",
        (project_id,),
    )
    ordered = sorted(
        records,
        key=lambda item: (
            next((index for index, (role, _) in enumerate(MATERIAL_ROLES)
                  if role == str(item.get("document_role") or "")), len(MATERIAL_ROLES)),
            str(item.get("filename") or ""),
        ),
    )
    return {str(item["id"]): f"资料{index:02d}" for index, item in enumerate(ordered, 1)}


def material_numbers(project_id: str) -> dict[str, str]:
    """成品材料标签 -> 资料编号，用于给别名补上编号后缀。"""
    series = document_series(project_id)
    numbers: dict[str, str] = {}
    for record in rows(
        "SELECT id,document_role FROM documents WHERE project_id=? ORDER BY filename",
        (project_id,),
    ):
        role = str(record.get("document_role") or "")
        label = next((name for key, name in MATERIAL_ROLES if key == role), "")
        if label and label not in numbers:
            numbers[label] = series.get(str(record["id"]), "")
    return numbers


def normalize_report_text(text: str, numbers: dict[str, str] | None = None) -> str:
    """把成品文本里的材料种类名换成规范化表述。幂等。"""
    if not text:
        return text
    lookup = numbers or {}
    output = text
    for source, target, label in MATERIAL_ALIASES:
        series = lookup.get(label, "") if label else ""
        output = output.replace(source, f"{target}〔{series}〕" if series else target)
    return output


def normalize_document(project_id: str, text: str) -> str:
    """便捷入口：按项目取资料编号后规范化。"""
    return normalize_report_text(text, material_numbers(project_id))


def wording_violations(text: str) -> list[dict]:
    """成品用词校验：禁用词、内部 UUID、内部字段键都必须为零出现。"""
    from .facts import FACT_REGISTRY

    body = text or ""
    violations: list[dict] = []
    for term in FORBIDDEN_TERMS:
        count = body.count(term)
        if count:
            violations.append({"kind": "forbidden_term", "value": term, "count": count})
    for match in sorted(set(_INTERNAL_UUID.findall(body))):
        violations.append({"kind": "internal_uuid", "value": match, "count": body.count(match)})
    for token in sorted(set(_SNAKE_TOKEN.findall(body))):
        if token in FACT_REGISTRY:
            violations.append({"kind": "internal_field_key", "value": token, "count": body.count(token)})
    return violations
