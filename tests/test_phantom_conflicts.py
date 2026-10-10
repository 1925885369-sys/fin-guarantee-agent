"""假冲突守卫。

「来源冲突」是报告里最有分量的结论之一——它要求人工去核验差异。一旦它是抽取噪声
造出来的，就会消耗真实的复核工时，也让真正的冲突淹没在噪声里。

实测出过两类假冲突：

1. **同一个值被判成两个**：抽取阶段把 ``text_value_token`` 的返回值（带 ``text:``
   前缀）当 ``text_value`` 存库，冲突分组阶段又对存库值跑一次同一个函数；
   它不幂等，第二次得到 ``text:text陈嘉诚`` → 与人工断言的真值签名不同 →
   「实际控制人：陈嘉诚 vs 陈嘉诚」被判成冲突。
2. **把表格标题当成字段值**：``bond_name`` 的别名里有裸的「本期债券」，命中了
   表格标题行「本期债券发行要素」，把后续词「发行要素」抽成了债券名称。

结论应当收敛到：**冲突集合 == 案例显式声明的冲突集合**。
"""
from __future__ import annotations

import pytest

from backend.app.bond_documents import CASE_SPECS, generate_case_materials
from backend.app.facts import deterministic_extract, text_value_token
from backend.app.professional import _semantic_conflict_groups


def _extracted(case, tmp_path) -> list[dict]:
    facts: list[dict] = []
    for material in generate_case_materials(tmp_path, case):
        sources = [
            {
                "id": f"{case.short}-p{fragment.page:03d}", "document_id": f"doc-{case.short}",
                "locator_type": fragment.locator_type, "locator": fragment.locator,
                "text": fragment.text, "page": fragment.page,
            }
            for fragment in material["parsed"].fragments
        ]
        facts.extend(deterministic_extract(sources))
    return facts


@pytest.mark.parametrize("fact_key,raw", [
    ("controller", "陈嘉诚"),
    ("bond_name", "华辰精密制造有限公司2026年面向专业投资者公开发行公司债券第一期"),
    ("guarantee_scope", "债券本金、利息、违约金及实现债权费用"),
])
def test_text_value_token_is_idempotent(fact_key: str, raw: str) -> None:
    """抽取阶段会用返回值覆盖 text_value，因此它必须幂等。"""
    once = text_value_token(fact_key, raw)
    assert once is not None
    assert text_value_token(fact_key, once) == once


def test_rating_comparison_sentence_yields_no_value() -> None:
    """一句里出现两个评级的，是比较句，不是字段取值。

    实测评级报告有「债项评级较主体评级由AA-提升至AA+」：取第一个会把债项评级写成
    主体评级的 AA- ——不只是假冲突，是把错误等级写进报告。
    """
    assert text_value_token("bond_rating", "较主体评级由AA-提升至AA+") is None
    assert text_value_token("bond_rating", "AA+") == "rating:AA+"
    assert text_value_token("bond_rating", "为AA+") == "rating:AA+"
    assert text_value_token("issuer_rating", "主体信用等级为AA-") == "rating:AA-"


def test_extracted_ratings_match_the_case_spec(tmp_path) -> None:
    """抽取出的主体/债项评级必须与案例声明一致。"""
    for case in CASE_SPECS:
        facts = _extracted(case, tmp_path)
        produced = {
            key: {str(item["text_value"]) for item in facts if item["fact_key"] == key}
            for key in ("issuer_rating", "bond_rating")
        }
        assert produced["issuer_rating"] == {f"rating:{case.issuer_rating}"}, case.short
        assert produced["bond_rating"] == {f"rating:{case.bond_rating}"}, case.short


def test_prefixed_and_plain_assertions_share_one_signature() -> None:
    """带前缀的抽取值与不带前缀的人工值必须是同一个签名——否则真值被判成冲突。"""
    extracted = text_value_token("controller", "陈嘉诚")          # 抽取存库时的形态
    human = text_value_token("controller", "陈嘉诚")              # 人工录入的形态
    token = text_value_token("controller", extracted)             # 分组时再跑一次
    assert token == human
    groups = _semantic_conflict_groups("controller", [
        {"value": None, "text_value": extracted},
        {"value": None, "text_value": "陈嘉诚"},
    ])
    assert len(groups) == 1, "同一个值不得分成两组"


def test_table_caption_is_not_mistaken_for_a_bond_name(tmp_path) -> None:
    """「本期债券发行要素」这类表格标题行不是债券名称字段。"""
    case = CASE_SPECS[0]
    names = [
        item["text_value"] for item in _extracted(case, tmp_path) if item["fact_key"] == "bond_name"
    ]
    assert names, "应至少抽到一条债券名称"
    assert "发行要素" not in names, names
    assert all("发行要素" not in str(name) for name in names), names
    assert any("华辰精密制造有限公司" in str(name) for name in names), names


@pytest.mark.parametrize("short", [case.short for case in CASE_SPECS])
def test_no_conflict_the_case_did_not_declare(short: str, tmp_path) -> None:
    """抽取不得造出案例没声明的冲突——这是「假冲突」的定义。

    只断言 `produced ⊆ declared`：多出来的必须为零。漏报是另一类问题，
    见下面的 ``test_declared_conflicts_are_realised``。
    """
    case = next(item for item in CASE_SPECS if item.short == short)
    facts = _extracted(case, tmp_path)
    grouped: dict[tuple[str, str], list[dict]] = {}
    for item in facts:
        grouped.setdefault((item["fact_key"], item["period"]), []).append(item)

    produced = {
        key for key, items in grouped.items()
        if len(_semantic_conflict_groups(key[0], items)) > 1
    }
    declared = {(item.fact_key, item.period) for item in case.conflicts}
    assert produced - declared == set(), f"{short} 出现未声明的冲突：{produced - declared}"


@pytest.mark.parametrize("short,missing", [("rongyi", ("revenue", "2025")), ("conflict", ("revenue", "2025"))])
def test_declared_conflicts_are_realised(short: str, missing: tuple, tmp_path) -> None:
    """已知缺口：部分声明的冲突在端到端链路里产生不出来。

    ``merge_facts`` 按 ``(字段, 主体, 口径, 期间)`` 只保留置信度最高的一条：
    招股书的营业收入走通用抽取得 0.86，评级报告的同字段走显式「年份：数值」得 0.94，
    于是 42,000 被 39,000 覆盖，两条断言不会并存，冲突也就无从判定。

    这是 ``merge_facts`` 的既有设计（先选一条值），不是抽取噪声，也不该靠放宽冲突
    判定来绕过。记录在此，等计划里「双来源核对」落地后由 `TableBlock` 的
    档案值/上传值并列来承载这类差异。
    """
    case = next(item for item in CASE_SPECS if item.short == short)
    facts = _extracted(case, tmp_path)
    items = [item for item in facts if (item["fact_key"], item["period"]) == missing]
    groups = _semantic_conflict_groups(missing[0], items)
    if len(groups) > 1:
        return
    pytest.xfail(
        "声明冲突未产生：merge_facts 只保留置信度最高的一条，两条断言不并存 "
        f"（当前抽取到 {[(i['period'], i['value']) for i in items]}）"
    )


def test_no_phantom_conflicts_on_ordinary_text_facts(tmp_path) -> None:
    """文本类事实（实际控制人、债券名称、担保范围）不得出现来源冲突。"""
    for case in CASE_SPECS:
        facts = _extracted(case, tmp_path)
        for key in ("controller", "bond_name", "guarantee_scope", "main_business"):
            items = [item for item in facts if item["fact_key"] == key and item["period"] == ""]
            if len(items) < 2:
                continue
            groups = _semantic_conflict_groups(key, items)
            assert len(groups) <= 1, f"{case.short} {key} 出现假冲突：{groups}"
