"""引文已有数字但尚未建记录 → 先补建可追溯记录（《风控约束分层》方案 §二 line 23）。

落点说明（这条最容易做错的地方）：
**不在章节校验**。章节校验的数字允许集第一项就是 evidence 的 quotes
（`chapter_guard._allowed_texts`），这类数字根本进不了 `unexpected_numbers`；
真正把它判成"编造"的是后置支持层的 `missing_record`。所以补建只发生在
`p0.assess_section_claims` 的数字判定分支里。

三条必须守住的边界：
1. 补建**不升为可支持**（`needs_review` 仍进未决项、仍阻断最终交付）；
2. 整句里**有一个数字没命中就整句不补建**（否则等于给所有找不到的数字开后门）；
3. 补建记录**不带科目/期间/主体**（原文有数字 ≠ 是该科目的值）。
"""
import uuid

import pytest

from backend.app.database import connect, json_dump, now_iso, rows
from backend.app.p0 import (
    _backfill_disclosure_records, _locate_mention_in_sources, _merge_supplemental_records,
    report_quality, validate_report_support,
)


@pytest.fixture(autouse=True)
def _ensure_schema():
    from backend.app.main import startup

    startup()


# ----------------------------------------------------------------- 建库样板 --


def _project(company: str = "补建记录测试企业") -> str:
    project_id = f"test-backfill-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,"
            "created_at,updated_at,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", company, 500, 12, "流动资金", "review", now_iso(), now_iso(), now_iso()),
        )
    return project_id


def _cleanup(project_id: str) -> None:
    tables = (
        "report_claims", "report_sections", "citations", "sources", "documents", "projects",
    )
    with connect() as conn:
        for table in tables:
            key = "id" if table == "projects" else "project_id"
            conn.execute(f"DELETE FROM {table} WHERE {key}=?", (project_id,))


def _fixture(project_id: str, source_text: str) -> str:
    """一条可定位来源 + 引用；返回 citation id。"""
    citation_id = f"citation-{uuid.uuid4()}"
    document_id = f"doc-{uuid.uuid4()}"
    source_id = f"source-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,"
            "quality,locator_count,warning,created_at,parser,ocr_status,page_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, project_id, "审计报告.pdf", "财务报表", "application/pdf", "hash", 100,
             "parsed", 1, 1, "", now_iso(), "native", "not_required", 1),
        )
        conn.execute(
            "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,"
            "bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, project_id, document_id, "pdf_page", "第 1 页", source_text, 1,
             json_dump([0, 0, 1, 1]), "native", f"hash-{source_id}"),
        )
        conn.execute(
            "INSERT INTO citations (id,project_id,source_id,document_id,label,quote,locator_type,locator) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (citation_id, project_id, source_id, document_id, "审计报告.pdf · 第 1 页", source_text,
             "pdf_page", "第 1 页"),
        )
    return citation_id


def _section(project_id: str, content: str, citation_id: str, records: list[dict],
             *, section_key: str = "financial") -> str:
    section_id = f"section-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,"
            "review_status,updated_at,table_records_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (section_id, project_id, section_key, "财务分析", content, json_dump([citation_id]),
             "draft", now_iso(), json_dump(records)),
        )
    return section_id


def _base_record(label: str = "营业收入", period: str = "2025", value: str = "1234.00") -> dict:
    """本章**已有记录体系**（这是补建的前提：没有记录是 `infrastructure_error`，不补建）。"""
    return {
        "record_id": f"cell:r01c01@{uuid.uuid4().hex[:6]}", "value": value, "unit": "万元",
        "label": label, "period": period, "subject": "补建记录测试企业", "scope": "合并口径",
        "source_ids": ["source-1"], "page": 1, "verification": "ok",
    }


def _statuses(project_id: str) -> dict[str, str]:
    return {
        item["claim_text"].rstrip("。").strip(): item["support_status"]
        for item in rows("SELECT claim_text,support_status FROM report_claims WHERE project_id=?",
                         (project_id,))
    }


def _backfilled(project_id: str) -> list[dict]:
    found: list[dict] = []
    for section in rows("SELECT evidence_records_json FROM report_sections WHERE project_id=?",
                        (project_id,)):
        from backend.app.database import json_load

        for record in json_load(section.get("evidence_records_json") or "[]"):
            if isinstance(record, dict) and record.get("origin_type") == "citation_backfill":
                found.append(record)
    return found


# ------------------------------------------------------ 单元：命中判定与记录形态 --


def test_locates_number_when_present_in_source():
    mention = {"raw": "12,345.67万元", "normalized_value": "12345.67", "normalized_unit": "万元"}
    source_id = _locate_mention_in_sources(mention, ["s1"], {"s1": "其他收益为12,345.67万元。"})
    assert source_id == "s1"


def test_locates_number_across_pdf_whitespace():
    """PDF 回流常在数字与单位之间带空格/换行；归一后仍应命中。"""
    mention = {"raw": "169.02亿元", "normalized_value": "169.02", "normalized_unit": "亿元"}
    assert _locate_mention_in_sources(mention, ["s1"], {"s1": "总资产169.02 亿元。"}) == "s1"


def test_does_not_locate_number_absent_from_source():
    mention = {"raw": "999亿元", "normalized_value": "999", "normalized_unit": "亿元"}
    assert _locate_mention_in_sources(mention, ["s1"], {"s1": "总资产169.02亿元。"}) == ""


def test_backfilled_record_shape_is_needs_review_disclosure():
    mentions = [{"raw": "169.02亿元", "normalized_value": "169.02", "normalized_unit": "亿元"}]
    records = _backfill_disclosure_records("financial", "总资产169.02亿元。", mentions,
                                           ["s1"], {"s1": "总资产169.02亿元。"})
    assert len(records) == 1
    record = records[0]
    assert record["record_type"] == "disclosure"
    assert record["status"] == "needs_review"
    assert record["value"] == "169.02" and record["unit"] == "亿元"
    assert record["source_ids"] == ["s1"]
    assert record["origin_type"] == "citation_backfill"
    assert record["backfilled_from_citation"] is True  # `_record` 把 extra 摊平到顶层
    # 刻意不带科目/期间/主体：原文有数字 ≠ 是该科目的值
    assert record["label"] == "" and record["period"] == "" and record["subject"] == ""


def test_backfilled_record_is_not_supportable():
    """补建只能降到"待核验"，不能变成"可支持"。"""
    from backend.app.evidence_records import supportable_records, review_records

    mentions = [{"raw": "169.02亿元", "normalized_value": "169.02", "normalized_unit": "亿元"}]
    records = _backfill_disclosure_records("financial", "总资产169.02亿元。", mentions,
                                           ["s1"], {"s1": "总资产169.02亿元。"})
    assert supportable_records(records) == []
    assert len(review_records(records)) == 1


def test_partial_hit_does_not_backfill_whole_claim():
    """一句两个数字、只有一个在原文里 → 整句不补建（防后门）。"""
    mentions = [
        {"raw": "169.02亿元", "normalized_value": "169.02", "normalized_unit": "亿元"},
        {"raw": "999亿元", "normalized_value": "999", "normalized_unit": "亿元"},
    ]
    assert _backfill_disclosure_records("financial", "总资产169.02亿元，负债999亿元。",
                                        mentions, ["s1"], {"s1": "总资产169.02亿元。"}) == []


# --------------------------------------------------------- 集成：支持层判定链路 --


def test_quote_number_without_record_becomes_needs_review_and_record_is_frozen():
    """材料里写着、只是没抽成记录 → 待核验 + 补建记录落库，不再计"无证据声明"。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "报告期内其他收益为12,345.67万元。")
        _section(project_id, "报告期内其他收益为12,345.67万元。", citation,
                 [_base_record()])
        result = validate_report_support(project_id)
        statuses = _statuses(project_id)
        assert statuses["报告期内其他收益为12,345.67万元"] == "needs_review"
        assert result["unsupported_claims"] == 0
        assert result["needs_review_claims"] == 1
        assert result["backfilled_records"] == 1
        assert len(_backfilled(project_id)) == 1
        # 阻断口径：待核验仍计入未决，交付状态保持 warning
        assert result["status"] == "warning"
        assert report_quality(project_id)["backfilled_records"] == 1
    finally:
        _cleanup(project_id)


def test_number_absent_from_source_still_unsupported():
    """材料里没有的数字**不补建**，仍判无证据（这条是防"补建变放行"的主负例）。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "报告期内其他收益为12,345.67万元。")
        _section(project_id, "报告期内其他收益为98,765.43万元。", citation, [_base_record()])
        result = validate_report_support(project_id)
        assert _statuses(project_id)["报告期内其他收益为98,765.43万元"] == "unsupported"
        assert result["unsupported_claims"] == 1
        assert result["backfilled_records"] == 0
        assert _backfilled(project_id) == []
    finally:
        _cleanup(project_id)


def test_partial_hit_claim_stays_unsupported():
    """一句话里混入一个材料里没有的数字 → 整句仍是 unsupported（不拆半句放行）。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "报告期内其他收益为12,345.67万元。")
        _section(project_id, "报告期内其他收益为12,345.67万元，营业外收入为98,765.43万元。",
                 citation, [_base_record()])
        result = validate_report_support(project_id)
        assert result["unsupported_claims"] == 1
        assert result["backfilled_records"] == 0
    finally:
        _cleanup(project_id)


def test_no_backfill_when_chapter_has_no_records():
    """整章没有冻结记录是 `infrastructure_error`（待补记录/再生），不是"引文补建"的场景。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "报告期内其他收益为12,345.67万元。")
        _section(project_id, "报告期内其他收益为12,345.67万元。", citation, [])
        result = validate_report_support(project_id)
        assert result["backfilled_records"] == 0
        assert _backfilled(project_id) == []
    finally:
        _cleanup(project_id)


def test_merge_supplemental_records_is_idempotent():
    """重复跑后置检查不得叠加同一批补建记录。"""
    project_id = _project()
    try:
        citation = _fixture(project_id, "报告期内其他收益为12,345.67万元。")
        section_id = _section(project_id, "报告期内其他收益为12,345.67万元。", citation,
                              [_base_record()])
        records = _backfill_disclosure_records(
            "financial", "报告期内其他收益为12,345.67万元。",
            [{"raw": "12,345.67万元", "normalized_value": "12345.67", "normalized_unit": "万元"}],
            [f"s-{project_id}"], {"s-1": "x"},
        )
        assert records == []  # 未命中来源
        section = rows("SELECT * FROM report_sections WHERE id=?", (section_id,))[0]
        payload = _backfill_disclosure_records(
            "financial", "报告期内其他收益为12,345.67万元。",
            [{"raw": "12,345.67万元", "normalized_value": "12345.67", "normalized_unit": "万元"}],
            ["s1"], {"s1": "报告期内其他收益为12,345.67万元。"},
        )
        # 两次**各自独立事务**：`_frozen_evidence_records` 走独立连接读库，
        # 同一未提交事务里连续调用会读不到前一次写入（真实链路每章各一次，不会这样用）。
        with connect() as conn:
            first = _merge_supplemental_records(project_id, conn, section, payload)
        with connect() as conn:
            second = _merge_supplemental_records(project_id, conn, section, payload)
        assert first == 1 and second == 0
        assert len(_backfilled(project_id)) == 1
    finally:
        _cleanup(project_id)


def test_covered_number_does_not_block_backfill_of_uncovered_one():
    """同句里**已被记录接住**的数字不要求原文命中，也不该阻止另一个数字补建。

    2026-10-18 北投 financial 实测：`3,689,393.72万元` 来自系统表格（原文写法不同），
    `368.48亿元` 只在评级报告原文里。旧规则要求整句所有数字都逐字命中原文，
    于是整句放弃补建 → 该主张被判 `missing_record` → 整章回退。
    """
    from backend.app.p0 import _backfill_disclosure_records, _number_mentions

    claim = ("2025年末货币资金为3,689,393.72万元（E01），"
             "评级报告披露同期未受限货币资金为368.48亿元（E11），材料不足。")
    mentions = _number_mentions(claim)
    # 原文只包含"368.48亿元"（模拟评级报告原句），不包含系统表格那个数
    texts = {"src-a": "截至2025年末，公司未受限货币资金为368.48亿元。"}
    records = [{"record_id": "t:1", "label": "货币资金", "value": "3689393.72", "unit": "万元"}]

    without_records = _backfill_disclosure_records("financial", claim, mentions, list(texts), texts)
    assert without_records == []          # 旧行为：整句放弃

    with_records = _backfill_disclosure_records(
        "financial", claim, mentions, list(texts), texts, records)
    assert len(with_records) == 1         # 只补建未覆盖的那个
    assert with_records[0]["value"] == "3684800"
    assert with_records[0]["status"] == "needs_review"
