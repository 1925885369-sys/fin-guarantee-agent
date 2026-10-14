"""事实抽取的数值健全性守卫。

这一组测试源自一次实测事故：表格化材料上线后，报告财务章的「资产总计 2025」
被写成 **504,004,586,441,328** 万元——那其实是三个年度的负债合计
（50400 + 45864 + 41328）被拼接成一个数。

根因链：

1. 抽取前用 ``re.sub(r"\\s+", "", text)`` 压掉整页空白（含换行），表格一行的
   ``负债合计 50,400 45,864 41,328`` 于是变成 ``50,40045,86441,328``；
2. ``_amount_numbers`` 的 ``[\\d,]*`` 把它整体当成一个 15 位数；
3. 通用抽取又只按「取最后一个数」猜列，把 2023 年的列值写成 2025 年。

旧的段落式排版靠 ``；`` 与 ``万元`` 隔开，侥幸没触发；换成真实表格结构后必然触发。
"""
from __future__ import annotations

import pytest

from backend.app.facts import (
    FACT_REGISTRY,
    _amount_numbers,
    _find_alias,
    deterministic_extract,
    source_supports_fact_value,
    strip_canonical_prefix,
    text_value_token,
)


def _page(text: str) -> list[dict]:
    return [{
        "id": "p1", "document_id": "d", "locator_type": "pdf_page",
        "locator": "第 1 页", "text": text,
    }]


def test_amount_scanning_rejects_malformed_thousands_grouping() -> None:
    """千分位不合法的数字串不是数字，是相邻数字丢了分隔——必须丢弃。"""
    assert _amount_numbers("负债合计 50,40045,86441,328") == []
    assert _amount_numbers("负债合计 50,400 45,864 41,328") == [50400.0, 45864.0, 41328.0]
    assert _amount_numbers("资产总计 72,000") == [72000.0]


def test_multi_column_row_never_yields_a_concatenated_value() -> None:
    """表格一行多列不得产出一个拼接出来的巨值。"""
    text = "项目 2025 年末 2024 年末 2023 年末\n负债合计 50,400 45,864 41,328"
    facts = deterministic_extract(_page(text))
    values = [item["value"] for item in facts if item.get("value") is not None]
    assert 504004586441328.0 not in values
    assert all(value < 10 ** 9 for value in values), values


def test_aligned_row_without_column_basis_is_not_guessed() -> None:
    """一行多个并列数值且无列对齐依据时，不猜——猜错就是把错误数字写进报告。

    这条行是有列对齐依据的反例：显式 ``2025年负债合计：50,400万元`` 仍然能抽到。
    """
    guessed = deterministic_extract(_page("负债合计 50,400 45,864 41,328"))
    assert all(
        item["fact_key"] != "total_liabilities" or item["value"] != 41328.0
        for item in guessed
    ), "不得把最后一列的值当成唯一期间的值"

    explicit = deterministic_extract(_page("2025年负债合计：50,400万元"))
    assert [(item["period"], item["value"]) for item in explicit if item["fact_key"] == "total_liabilities"] == [
        ("2025", 50400.0)
    ]


def test_excel_pipe_rows_still_align_to_header_periods() -> None:
    """守卫不能误伤正常路径：Excel 管道行仍按表头年份对齐。"""
    sources = [
        {"id": "h", "document_id": "d", "locator_type": "xlsx_range", "locator": "财务!A1:C1",
         "text": "指标（万元） | 2024 | 2025"},
        {"id": "r", "document_id": "d", "locator_type": "xlsx_range", "locator": "财务!A2:C2",
         "text": "营业收入 | 1000 | 1200"},
    ]
    facts = deterministic_extract(sources)
    assert [(item["period"], item["value"]) for item in facts if item["fact_key"] == "revenue"] == [
        ("2024", 1000.0), ("2025", 1200.0)
    ]


def test_table_rows_align_to_header_years() -> None:
    """表头给出的年度列顺序就是对齐依据，不必猜「最后一个数是哪个年度」。

    实测过的事故：`负债合计 50,400 45,864 41,328` 在整页去空白后变成一个 15 位数；
    加上「不猜」守卫后不再出错，但也不再抽取——材料改成表格驱动后，抽取器必须能读表格。
    """
    text = "项目 2025 年末 2024 年末 2023 年末\n负债合计 50,400 45,864 41,328"
    facts = deterministic_extract(_page(text))
    assert {
        (item["period"], item["value"]) for item in facts if item["fact_key"] == "total_liabilities"
    } == {("2025", 50400.0), ("2024", 45864.0), ("2023", 41328.0)}


def test_qualified_label_is_not_the_field_itself() -> None:
    """`流动负债合计` 含 `负债合计` 子串，但它是另一个字段。

    实测过的事故：`流动负债合计` 的三年值被抽成 `负债合计`，进而把权益推导成
    「资产 − 流动负债」，权益三年全部变成假冲突。
    """
    text = (
        "项目 2025 年末 2024 年末 2023 年末\n"
        "流动负债合计 23,000 20,930 18,860\n"
        "非流动负债合计 17,000 15,470 13,940\n"
        "负债合计 40,000 36,400 32,800"
    )
    facts = deterministic_extract(_page(text))
    assert {
        (item["period"], item["value"]) for item in facts if item["fact_key"] == "total_liabilities"
    } == {("2025", 40000.0), ("2024", 36400.0), ("2023", 32800.0)}


def test_qualified_only_text_yields_nothing() -> None:
    """文本里只有带限定词的科目时，宁可不抽，也不张冠李戴。"""
    facts = deterministic_extract(_page("项目 2025 年末\n流动负债合计 23,000"))
    assert all(item["fact_key"] != "total_liabilities" for item in facts)


def test_source_support_check_is_callable() -> None:
    """``source_supports_fact_value`` 曾引用一个从未定义的 ``_compact``。

    它在真实模型路径（债券项目 ``strict_source_support=True``）下会被调用，
    一旦触发就是 NameError 让整次运行失败；纯确定性路径不会碰到，所以长期潜伏。
    """
    assert source_supports_fact_value(
        {"fact_key": "cash", "raw_value": "6,820"}, "货币资金 6,820 6,206 5,592"
    ) is True
    assert source_supports_fact_value(
        {"fact_key": "cash", "raw_value": "9,999"}, "货币资金 6,820 6,206 5,592"
    ) is False


@pytest.mark.parametrize("raw,expected", [("50,400", 50400.0), ("1,234,567", 1234567.0), ("980", 980.0)])
def test_well_formed_grouping_still_parses(raw: str, expected: float) -> None:
    assert _amount_numbers(raw) == [expected]


@pytest.mark.parametrize(
    "text",
    [
        "保证人主体信用等级为AA+",
        "担保人主体信用等级为AA",
        "可比企业主体信用等级为AA",
        "同行业可比企业主体信用等级为AA-",
    ],
)
def test_rating_alias_ignores_other_subjects(text: str) -> None:
    """评级别名是通用词组，必须限定本主体。

    评级报告同时披露保证人评级与可比企业评级，这些句子里的"主体信用等级"此前会被
    当成发行人主体评级抽走——实测北投/中邮评级报告都含保证人评级，一旦污染，评级章的
    主体评级与债项评级会整体错位。
    """
    assert _find_alias("issuer_rating", FACT_REGISTRY["issuer_rating"].aliases, text) is None


def test_rating_alias_keeps_the_issuer_when_other_subjects_are_nearby() -> None:
    """限定只作用于紧邻小句：前文有保证人评级时，后面的发行人评级仍必须取到。"""
    text = "保证人主体信用等级为AA+；发行人主体信用等级为AAA"
    assert _find_alias("issuer_rating", FACT_REGISTRY["issuer_rating"].aliases, text) is not None
    assert _find_alias("issuer_rating", FACT_REGISTRY["issuer_rating"].aliases, "发行人主体信用等级为AAA") is not None


def test_rating_date_is_normalized_and_internal_prefixes_never_leak() -> None:
    """评级日期只认明确标签并归一成 ``date:YYYY-MM-DD``；内部签名前缀不得印进成品。

    修订稿 §2.4：日期不能用发行日、报表日或文件中任意日期替代——别名只收
    "评级时间/评级日期/跟踪评级…"，段标题这种没有数值的上下文取不到值。
    同时 ``agency:`` / ``date:`` 必须与 ``rating:`` 一样被剥掉：它们只是内部签名，
    实测漏出去过「主体评级为rating:AA-」。
    """
    assert text_value_token("rating_date", "评级时间：2025年5月30日") == "date:2025-05-30"
    assert text_value_token("rating_date", "主体评级结果 债项评级结果 评级时间") is None
    assert text_value_token("rating_date", "评级时间为空") is None
    # **幂等**：抽取阶段存库的就是 date: 输出，冲突分组会对存库值再跑一次——
    # 不能二次解析会让 rating_date 永远选不进 canonical（北投/中邮实测）。
    assert text_value_token("rating_date", "date:2026-07-03") == "date:2026-07-03"
    assert text_value_token("rating_date", "date:2026-13-03") is None  # 非法月份不取
    for value, expected in (
        ("agency:中诚信国际信用评级有限责任公司", "中诚信国际信用评级有限责任公司"),
        ("date:2025-05-30", "2025-05-30"),
        ("rating:AAA", "AAA"),
        ("outlook:稳定", "稳定"),
    ):
        assert strip_canonical_prefix(value) == expected


def test_rating_outlook_reads_the_rating_slash_outlook_pair() -> None:
    """评级报告的固定写法「主体评级AAA/稳定」周围**没有**"评级展望"标签。

    实测北投正文就是"发行人及评级结果北京市基础设施投资有限公司AAA/稳定"——只按标签找
    永远取不到展望。这条规则必须不依赖别名是否存在。
    """
    from backend.app.facts import _extract_labelled_text

    text = "发行人及评级结果北京市基础设施投资有限公司AAA/稳定债项及评级结果"
    assert _extract_labelled_text("rating_outlook", "评级展望", text)[0] == "outlook:稳定"
    assert _extract_labelled_text("rating_outlook", "展望", text)[0] == "outlook:稳定"


def test_rating_agency_treats_the_alias_itself_as_the_value() -> None:
    """机构名的别名**就是值**：实测北投 p1 是"中诚信国际信用评级有限责任公司|编号:…"。

    别名只是前缀简称（注册表收"中诚信"，拼写表收"中诚信国际"），所以要用别名所在的窗口
    去匹配正式拼写，而不是把别名当标签去它后面找值。
    """
    from backend.app.facts import _extract_labelled_text

    text = "信用评级报告中诚信国际信用评级有限责任公司|编号:CCXI-20262275D-01"
    assert _extract_labelled_text("rating_agency", "中诚信", text)[0] == "agency:中诚信国际"
    assert _extract_labelled_text("rating_agency", "联合资信", "联合资信评估股份有限公司")[0] == "agency:联合资信"


def test_rating_date_uses_the_report_signature_date_only() -> None:
    """报告出具日期视同评级时间（产品已确认），但**只认紧跟机构名的日期**。

    实测北投 p2 结尾是"中诚信国际信用评级有限责任公司\x012026年7月3日"（``\\x01`` 是
    ``_boundary_compact`` 的换行边界）；而正文里的方法学变更日期、预判年份都不是评级时间
    （修订稿 §2.4：日期不能用发行日、报表日或文件中任意日期替代）。
    """
    from backend.app.facts import _extract_labelled_text

    assert _extract_labelled_text(
        "rating_date", "中诚信", "中诚信国际信用评级有限责任公司\x012026年7月3日",
    )[0] == "date:2026-07-03"
    # 反例：中间隔了"于"这类字词 → 不是署名日期
    assert _extract_labelled_text("rating_date", "联合资信", "因联合资信于2026年5月及6月对…")[0] is None
    # 反例：正文里的预判年份
    assert _extract_labelled_text("rating_date", "中诚信", "中诚信国际预计2026年二季度中国GDP增长")[0] is None


def test_rating_date_accepts_chinese_numeral_signature_date() -> None:
    """中文数字落款（实测中邮末页"联合资信评估股份有限公司 评级总监： 二〇二六年七月十日"）。

    署名日期必须紧跟机构名（可隔署名栏"评级总监："）；正文叙述里的中文年份、缺月日的
    年份、非法月日都不取（修订稿 §2.4 仍成立）。
    """
    from backend.app.facts import FACT_REGISTRY, _extract_labelled_text, _find_alias

    text = "特此公告 联合资信评估股份有限公司 评级总监： 二〇二六年七月十日"
    assert _extract_labelled_text("rating_date", "联合资信", text)[0] == "date:2026-07-10"
    assert _find_alias("rating_date", FACT_REGISTRY["rating_date"].aliases, text.replace(" ", "")) == "联合资信评估股份有限公司"
    # 反例：未被机构名"紧跟"的中文年份（叙述语）与缺月日的年份都不取。
    assert _extract_labelled_text("rating_date", "联合资信", "联合资信预计二〇二六年中国债券市场回暖")[0] is None
    assert _extract_labelled_text("rating_date", "联合资信", "联合资信评估股份有限公司 二〇二六年")[0] is None
    # 反例：非法月日（十三月）不取。
    assert _extract_labelled_text("rating_date", "联合资信", "联合资信评估股份有限公司 二〇二六年十三月十日")[0] is None


def test_rating_document_fallback_scans_whole_report_for_announcement_fields() -> None:
    """评级报告整篇兜底：公告/结论页不在任何 rating_* 节定位范围时，评级五字段仍可抽出。

    实测中邮评级报告 p2 公告页与 p24 评级结论页均不被章节定位覆盖，只按分节抽取时
    issuer_rating / bond_rating / rating_date 永远为空（2026-10-14 试跑实测）。
    """
    from backend.app.workflow import _rating_document_fallback_facts

    text = ("十三、评级结论\n基于对公司经营风险与财务风险的评估，联合资信确定中邮证券有限责任公司"
            "主体长期信用等级为AA+，本期债券信用等级为AAA，评级展望为稳定。\n特此公告\n"
            "联合资信评估股份有限公司\n评级总监：\n二〇二六年七月十日")
    source_rows = [{
        "id": "rating-p024", "document_id": "doc-rating", "document_role": "rating_report",
        "locator_type": "pdf_page", "locator": "第 24 页", "page": 24, "text": text,
    }]
    facts = _rating_document_fallback_facts(source_rows, [], {"rating-p024": text})
    got = {item["fact_key"]: item.get("text_value") for item in facts}
    assert got.get("issuer_rating") == "rating:AA+"
    assert got.get("bond_rating") == "rating:AAA"
    assert got.get("rating_outlook") == "outlook:稳定"
    assert got.get("rating_agency") == "agency:联合资信"
    assert got.get("rating_date") == "date:2026-07-10"
    # 全部命中项都标记为兜底来源（台账可区分"分节抽取"与"整篇兜底"），并带原文来源页。
    assert all(item["section_role"] == "rating_report_fallback" for item in facts)
    assert all(list(item["source_ids"]) == ["rating-p024"] for item in facts)
    # 负例：非评级报告文档不触发兜底（不扩大其它文档的字段来源）。
    assert _rating_document_fallback_facts(
        [{**source_rows[0], "document_role": "prospectus"}], [], {}) == []
