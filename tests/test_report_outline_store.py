"""报告版本化目录（report-outline）纯逻辑测试：生成/校验/规范化/指纹。

不依赖数据库——只覆盖 spec §二（builtin 生成）与 §一/§四（校验、规范化）。
落库与接口行为见 tests/test_report_outline_api.py。
"""
from __future__ import annotations

import re
import uuid

import pytest

from backend.app.chapter_prompts import BOND_CHAPTER_KEYS, BOND_CHAPTER_PARTS, load_chapter_config
from backend.app.report_outline_store import (
    BUILTIN_OUTLINE_VERSION,
    MAX_TITLE_LENGTH,
    builtin_outline,
    normalize_outline,
    outline_fingerprint,
    validate_outline,
)


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _all_chapters(outline: dict) -> list[dict]:
    return [chapter for part in outline["parts"] for chapter in part["chapters"]]


def _expected_subsection_titles(chapter_key: str) -> list[str]:
    """出厂 YAML 的 paragraph_order + required_sections 按序去重。"""
    config = load_chapter_config(chapter_key)
    narrative = config.get("narrative_contract") or {}
    ordered = list(narrative.get("paragraph_order") or []) + list(config.get("required_sections") or [])
    titles: list[str] = []
    for raw in ordered:
        title = str(raw).strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def _subsection(title: str, subsection_id: str = "sub-1") -> dict:
    return {"subsection_id": subsection_id, "title": title, "order": 0, "prompt": "", "enabled": True}


def _chapter(chapter_id: str, template_key, title: str, subsections: list[dict] | None = None,
             order: int = 0, generation_mode: str = "template", evidence_scope: str = "chapter_contract") -> dict:
    return {
        "chapter_id": chapter_id, "template_key": template_key, "title": title, "order": order,
        "generation_mode": generation_mode, "evidence_scope": evidence_scope,
        "subsections": subsections or [],
    }


def _outline(*chapters: dict) -> dict:
    return {"outline_version": "client-value-ignored",
            "parts": [{"part_id": "part-1", "title": "报告摘要及审查范围", "order": 7, "chapters": list(chapters)}]}


# --------------------------------------------------------------------- builtin 生成


def test_builtin_outline_is_deterministic_and_isolated():
    first = builtin_outline()
    second = builtin_outline()
    assert first == second, "两次调用必须完全一致（含 ID）"
    # 返回对象必须相互独立：改动一次调用的结果不得污染下一次。
    first["parts"][0]["chapters"][0]["title"] = "被污染"
    first["parts"][0]["chapters"][0]["subsections"].append({"subsection_id": "x"})
    third = builtin_outline()
    assert third["parts"][0]["chapters"][0]["title"] != "被污染"
    assert "x" not in [item["subsection_id"] for item in third["parts"][0]["chapters"][0]["subsections"]]
    assert third == second


def test_builtin_outline_shape_matches_factory_metadata():
    outline = builtin_outline()
    assert outline["outline_version"] == BUILTIN_OUTLINE_VERSION

    parts = outline["parts"]
    assert len(parts) == len(BOND_CHAPTER_PARTS) == 6
    assert [part["part_id"] for part in parts] == [f"part-{index}" for index in range(1, 7)]
    assert [part["order"] for part in parts] == [1, 2, 3, 4, 5, 6]
    # 分部标题与顺序取自 BOND_CHAPTER_PARTS。
    assert [part["title"] for part in parts] == [title for title, _ in BOND_CHAPTER_PARTS]

    chapters = _all_chapters(outline)
    assert len(chapters) == len(BOND_CHAPTER_KEYS) == 17
    assert [chapter["template_key"] for chapter in chapters] == list(BOND_CHAPTER_KEYS)
    assert all(chapter["chapter_id"] == f"chapter-{chapter['template_key']}" for chapter in chapters)
    assert all(chapter["generation_mode"] in {"generate", "template"} for chapter in chapters)
    assert all(chapter["evidence_scope"] == "chapter_contract" for chapter in chapters)
    # 章内 order 连续。
    for part in parts:
        assert [chapter["order"] for chapter in part["chapters"]] == list(range(1, len(part["chapters"]) + 1))


def test_builtin_subsections_match_factory_config_order_and_ids():
    for chapter in _all_chapters(builtin_outline()):
        key = chapter["template_key"]
        expected = _expected_subsection_titles(key)
        assert [item["title"] for item in chapter["subsections"]] == expected, key
        assert [item["subsection_id"] for item in chapter["subsections"]] == [
            f"sub-{key}-{index}" for index in range(1, len(expected) + 1)
        ]
        assert all(item["prompt"] == "" and item["enabled"] is True for item in chapter["subsections"])
        assert [item["order"] for item in chapter["subsections"]] == list(range(1, len(expected) + 1))


# --------------------------------------------------------------------- 校验拒绝分支


def test_validate_rejects_empty_outline():
    with pytest.raises(ValueError, match="至少需要 1 个二级章节"):
        validate_outline({"parts": [{"part_id": "part-1", "title": "p", "chapters": []}]})
    with pytest.raises(ValueError, match="parts"):
        validate_outline({"outline_version": "x"})


def test_validate_rejects_duplicate_chapter_id():
    outline = _outline(
        _chapter("chapter-scope", "scope", "审查范围、材料状态和免责声明"),
        _chapter("chapter-scope", None, "自定义但 ID 撞车"),
    )
    with pytest.raises(ValueError, match="章节 ID 全局重复"):
        validate_outline(outline)


def test_validate_rejects_duplicate_subsection_id():
    outline = _outline(_chapter(
        "chapter-scope", "scope", "审查范围、材料状态和免责声明",
        [_subsection("A", "sub-scope-1"), _subsection("B", "sub-scope-1")],
    ))
    with pytest.raises(ValueError, match="小节 ID 章内重复"):
        validate_outline(outline)


def test_validate_rejects_unknown_template_key():
    outline = _outline(_chapter("chapter-not-a-key", "not-a-key", "未知章节"))
    with pytest.raises(ValueError, match="未知的内置章节 template_key"):
        validate_outline(outline)


def test_validate_rejects_builtin_id_template_key_mismatch():
    outline = _outline(_chapter("chapter-governance", "scope", "ID 与 key 不对应"))
    with pytest.raises(ValueError, match="内置章节 ID 必须与 template_key 对应"):
        validate_outline(outline)


def test_validate_rejects_illegal_custom_chapter_id():
    outline = _outline(_chapter("chapter-scope", None, "自定义 ID 非法"))
    with pytest.raises(ValueError, match="chapter-<uuid>"):
        validate_outline(outline)


def test_validate_rejects_overlong_and_blank_titles():
    with pytest.raises(ValueError, match=f"不得超过 {MAX_TITLE_LENGTH} 字符"):
        validate_outline(_outline(_chapter("chapter-scope", "scope", "长" * (MAX_TITLE_LENGTH + 1))))
    with pytest.raises(ValueError, match="不能为空"):
        validate_outline(_outline(_chapter("chapter-scope", "scope", "   ")))
    with pytest.raises(ValueError, match="不得包含换行"):
        validate_outline(_outline(_chapter("chapter-scope", "scope", "含\n换行")))


def test_validate_accepts_valid_custom_chapter():
    custom_id = f"chapter-{uuid.uuid4()}"
    outline = _outline(
        _chapter("chapter-scope", "scope", "审查范围、材料状态和免责声明"),
        _chapter(custom_id, None, "自定义章节"),
    )
    validate_outline(outline)  # 不抛异常


# --------------------------------------------------------------------- 规范化


def test_normalize_reorders_and_derives_fields():
    custom_id = f"chapter-{uuid.uuid4()}"
    outline = _outline(
        _chapter("chapter-scope", "scope", "  审查范围、材料状态和免责声明  ", order=9,
                 generation_mode="generate", evidence_scope="prompt_retrieval"),
        _chapter("chapter-governance", "governance", "发行人主体、股权和治理", order=4,
                 generation_mode="template", evidence_scope="chapter_contract"),
        _chapter(custom_id, None, "自定义章节", order=1,
                 generation_mode="template", evidence_scope="chapter_contract"),
    )
    outline["parts"][0]["order"] = 5

    normalized = normalize_outline(outline)
    assert normalized["parts"][0]["order"] == 1
    assert [chapter["order"] for chapter in normalized["parts"][0]["chapters"]] == [1, 2, 3]
    # 标题去首尾空白。
    assert normalized["parts"][0]["chapters"][0]["title"] == "审查范围、材料状态和免责声明"

    scope, governance, custom = normalized["parts"][0]["chapters"]
    # 派生字段按 template_key 重写：忽略客户端值。
    assert scope["generation_mode"] == "template" and scope["evidence_scope"] == "chapter_contract"
    assert governance["generation_mode"] == "generate" and governance["evidence_scope"] == "chapter_contract"
    # 自定义章节恒为 generate + prompt_retrieval，template_key=null。
    assert custom["template_key"] is None
    assert custom["generation_mode"] == "generate" and custom["evidence_scope"] == "prompt_retrieval"

    # 换新 outline_version（uuid），不沿用客户端值。
    assert normalized["outline_version"] != "client-value-ignored"
    assert UUID_RE.match(normalized["outline_version"])
    # 入参不被修改。
    assert outline["parts"][0]["chapters"][0]["title"] == "  审查范围、材料状态和免责声明  "


def test_normalize_renumbers_subsections():
    outline = _outline(_chapter(
        "chapter-scope", "scope", "审查范围、材料状态和免责声明",
        [_subsection("A", "sub-scope-1"), _subsection("B", "sub-scope-2")],
    ))
    outline["parts"][0]["chapters"][0]["subsections"][0]["order"] = 8
    normalized = normalize_outline(outline)
    assert [item["order"] for item in normalized["parts"][0]["chapters"][0]["subsections"]] == [1, 2]
    assert [item["subsection_id"] for item in normalized["parts"][0]["chapters"][0]["subsections"]] == [
        "sub-scope-1", "sub-scope-2"]


def test_normalize_validates_first():
    with pytest.raises(ValueError):
        normalize_outline({"parts": []})


# --------------------------------------------------------------------- 指纹


def test_outline_fingerprint_is_stable_and_key_order_independent():
    outline = builtin_outline()
    assert outline_fingerprint(outline) == outline_fingerprint(builtin_outline())

    reordered_keys = {key: outline[key] for key in reversed(list(outline))}
    assert outline_fingerprint(outline) == outline_fingerprint(reordered_keys), "键序不影响指纹"

    changed = builtin_outline()
    changed["parts"][0]["chapters"][0]["title"] = "改了标题"
    assert outline_fingerprint(changed) != outline_fingerprint(outline)
