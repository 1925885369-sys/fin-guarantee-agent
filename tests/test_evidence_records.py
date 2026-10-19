"""统一冻结记录（evidence_records）专项测试——「三项遗留修复方案」§一。

覆盖：
1. 四类记录构建：type/status/scope 映射、metric 计算链路、rule 阈值与结果；
2. 允许集分层：matchable（章节）/ supportable（支持）/ review（待核验降级）；
3. ``_numeric_binding_status`` 三层判定：direct / needs_review / unsupported；
4. 冻结读取与 legacy 迁移（evidence-records-v1 / legacy:table-records-v1 / legacy_unfrozen）；
5. 确定性内容的模板句不再误报（无数字→not_applicable；带数字→system_derived）；
6. 指标句、规则阈值句在有计算链路时通过；无链路指标只降级待核验、不得算支持。
"""
from __future__ import annotations

import uuid

import pytest

from backend.app.database import connect, json_dump, now_iso, rows
from backend.app.evidence_records import (
    RECORD_VERSION, build_evidence_records, evidence_number_keys, matchable_records,
    records_from_legacy, review_records, supportable_records,
)
from backend.app.p0 import _frozen_evidence_records, _numeric_binding_status, assess_section_claims


@pytest.fixture(autouse=True)
def _ensure_schema():
    """触库用例前确保 schema 已建立（与 test_failure_flow 的 startup() 同模式），
    避免本文件排在批内首个时"no such table"。"""
    from backend.app.main import startup

    startup()


# ------------------------------------------------------------------ 工具 --

def _records_for(**overrides):
    base = {
        "facts": [], "metrics": [], "checks": [], "table_records": [],
    }
    base.update(overrides)
    return build_evidence_records(**base)


def _project(company: str = "统一记录测试企业") -> str:
    project_id = f"test-evrec-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, "token", company, 500, 12, "流动资金", "review", now_iso(), now_iso(), now_iso()),
        )
    return project_id


def _cleanup(project_id: str) -> None:
    tables = (
        "evidence_edges", "report_claims", "citations", "report_sections", "sources", "documents",
    )
    with connect() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def _source_and_citation(project_id: str) -> tuple[str, str]:
    citation_id = f"citation-{uuid.uuid4()}"
    document_id = f"doc-{uuid.uuid4()}"
    source_id = f"source-{uuid.uuid4()}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,ocr_status,page_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, project_id, "财务报表.pdf", "财务报表", "application/pdf", "hash", 100,
             "parsed", 1, 1, "", now_iso(), "native", "not_required", 1),
        )
        conn.execute(
            "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, project_id, document_id, "pdf_page", "第 1 页", "材料原文", 1,
             json_dump([0, 0, 1, 1]), "native", f"hash-{source_id}"),
        )
        conn.execute(
            "INSERT INTO citations (id,project_id,source_id,document_id,label,quote,locator_type,locator) VALUES (?,?,?,?,?,?,?,?)",
            (citation_id, project_id, source_id, document_id, "财务报表.pdf · 第 1 页", "材料原文", "pdf_page", "第 1 页"),
        )
    return citation_id, source_id


def _section_row(project_id: str, content: str, citation_id: str, *, section_key: str = "financial",
                 content_origin: str = "model", evidence_records: list[dict] | None = None,
                 table_records: list[dict] | None = None, allowed_source_ids: list[str] | None = None) -> str:
    section_id = f"section-{uuid.uuid4()}"
    columns = ["id", "project_id", "section_key", "title", "content", "citation_ids", "review_status",
               "updated_at", "content_origin", "table_records_json", "allowed_source_ids", "evidence_records_json"]
    values = [section_id, project_id, section_key, "财务分析", content, json_dump([citation_id]), "draft",
              now_iso(), content_origin, json_dump(table_records or []), json_dump(allowed_source_ids or []),
              json_dump(evidence_records or [])]
    with connect() as conn:
        conn.execute(
            f"INSERT INTO report_sections ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values),
        )
    return section_id


def _status(project_id: str, section_id: str) -> dict[str, str]:
    return {
        item["claim_text"].rstrip("。").strip(): item["support_status"]
        for item in rows(
            "SELECT claim_text,support_status FROM report_claims WHERE section_id=? ORDER BY claim_index",
            (section_id,),
        )
    }


# ------------------------------------------------------------ 1. 构建层 --

def test_build_covers_four_types_with_status_and_scope():
    records = _records_for(
        facts=[{
            "id": "f1", "fact_key": "revenue", "name": "营业收入", "period": "2025",
            "period_type": "duration", "scope": "consolidated", "value": 48000, "unit": "万元",
            "raw_value": "48,000", "source_ids": ["source-1"], "assertion_status": "asserted",
            "origin_type": "document", "subject": "华辰精密制造有限公司",
        }, {
            "id": "f2", "fact_key": "revenue", "name": "营业收入", "period": "2024",
            "scope": "consolidated", "value": 39000, "unit": "万元", "source_ids": [],
            "assertion_status": "conflict", "origin_type": "document", "subject": "",
        }],
        metrics=[{
            "metric_key": "debt_ratio", "name": "资产负债率", "period": "2025", "value": 64.8,
            "unit": "%", "formula": "负债合计/资产合计", "input_fact_ids": ["f1"],
            "source_ids": ["source-1"], "status": "calculated",
        }, {
            "metric_key": "current_ratio", "name": "流动比率", "period": "2025", "value": 1.58,
            "unit": "倍", "formula": "", "input_fact_ids": [], "source_ids": [], "status": "calculated",
        }],
        checks=[{
            "rule_key": "interest_coverage", "title": "利息保障倍数", "status": "passed",
            "thresholds": {"minimum": 1.5},
            "values": [{"label": "利息覆盖倍数", "value": 1.71}],
            "source_ids": ["source-1"], "fact_ids": ["f1"], "formula": "EBITDA/利息支出",
        }],
        table_records=[{
            "record_id": "t1", "value": "100.00", "unit": "万元", "label": "应付债券",
            "period": "2025", "subject": "华辰精密制造有限公司", "scope": "合并口径",
            "source_ids": ["source-1"], "locator": {"page": 9}, "page": 9, "verification": "ok",
        }],
    )
    by_type = {}
    for record in records:
        by_type.setdefault(record["record_type"], []).append(record)
    assert set(by_type) == {"fact", "table", "metric", "rule"}
    assert all(record["record_version"] == RECORD_VERSION for record in records)

    # scope 英文 → 中文口径映射；fact 冲突 → conflict；无来源种子 → needs_review
    fact_ok, fact_conflict = by_type["fact"]
    assert fact_ok["scope"] == "合并口径" and fact_ok["status"] == "verified"
    assert fact_conflict["status"] == "conflict"
    # metric：有公式+输入事实 → derived；无链路 → needs_review（不得作为支持）
    metric_ok, metric_bare = by_type["metric"]
    assert metric_ok["status"] == "derived" and metric_ok["source_fact_ids"] == ["f1"]
    assert metric_bare["status"] == "needs_review"
    # rule：阈值单位从标题恢复（1.5 倍）；规则结果值成记录（同样带单位线索）
    rule_units = {(record["value"], record["unit"]) for record in by_type["rule"]}
    assert ("1.5", "倍") in rule_units and ("1.71", "倍") in rule_units


def test_allow_set_layering():
    records = _records_for(
        metrics=[{
            "metric_key": "debt_ratio", "name": "资产负债率", "period": "2025", "value": 64.8,
            "unit": "%", "formula": "负债/资产", "input_fact_ids": ["f1"], "source_ids": ["s1"],
        }, {
            "metric_key": "current_ratio", "name": "流动比率", "period": "2025", "value": 1.58,
            "unit": "倍", "formula": "", "input_fact_ids": [],
        }],
        facts=[{"id": "fx", "fact_key": "revenue", "name": "营业收入", "period": "2025",
                "value": 100, "unit": "万元", "source_ids": [], "assertion_status": "asserted"}],
    )
    assert len(matchable_records(records)) == len(records)  # 章节匹配集含待核验
    supported = {record["metric_key"] if "metric_key" in record else record.get("fact_key")
                 for record in supportable_records(records)}
    assert "debt_ratio" in supported and "current_ratio" not in supported
    review_metrics = {record.get("metric_key") for record in review_records(records)}
    assert "current_ratio" in review_metrics  # 无链路指标只进待核验
    keys = evidence_number_keys(supportable_records(records))
    assert ("64.8", "%") in keys


# ------------------------------------------------ 3. 三层数字支持判定 --

def test_numeric_status_three_tiers():
    records = _records_for(
        metrics=[{
            "metric_key": "debt_ratio", "name": "资产负债率", "period": "2025", "value": 64.8,
            "unit": "%", "formula": "负债/资产", "input_fact_ids": ["f1"], "source_ids": ["s1"],
        }, {
            "metric_key": "current_ratio", "name": "流动比率", "period": "2025", "value": 1.58,
            "unit": "倍", "formula": "", "input_fact_ids": [],
        }],
    )
    from backend.app.chapter_guard import _number_mentions

    supported = _number_mentions("资产负债率为64.8%")
    assert _numeric_binding_status("p", records, "资产负债率为64.8%", supported, "numeric_fact") == "direct"
    # 命中待核验记录（无链路指标）→ 降级待核验，不得算支持
    review = _number_mentions("流动比率为1.58倍")
    assert _numeric_binding_status("p", records, "流动比率为1.58倍", review, "numeric_fact") == "needs_review"
    # 查无记录 → 不支持
    unknown = _number_mentions("短期借款为999万元")
    assert _numeric_binding_status("p", records, "短期借款为999万元", unknown, "numeric_fact") == "unsupported"


def test_support_failure_reason_codes_and_matched_records():
    """支持失败四分类原因码 + 匹配记录（收口方案 §三）：缺记录 / 字段歧义 / 明确错配 /
    基础设施错误；状态三层判定与原因码互不改变（旧字符串口径由兼容封装守护）。"""
    from backend.app.chapter_guard import _number_mentions
    from backend.app.p0 import _numeric_binding_outcome

    records = _records_for(
        metrics=[{
            "metric_key": "debt_ratio", "name": "资产负债率", "period": "2025", "value": 64.8,
            "unit": "%", "formula": "负债/资产", "input_fact_ids": ["f1"], "source_ids": ["s1"],
        }, {
            "metric_key": "current_ratio", "name": "流动比率", "period": "2025", "value": 1.58,
            "unit": "倍", "formula": "", "input_fact_ids": [],
        }],
    )

    def outcome(claim: str):
        return _numeric_binding_outcome("p", records, claim, _number_mentions(claim), "numeric_fact")

    # 通过：原因码为空，但仍回带匹配记录（供诊断/台账还原）
    status, code, matched = outcome("资产负债率为64.8%")
    assert (status, code) == ("direct", "")
    assert matched == ["metric:debt_ratio:2025"]
    # 字段歧义：只命中待核验记录（无链路指标）→ 待人工核验，不得算支持
    status, code, matched = outcome("流动比率为1.58倍")
    assert (status, code) == ("needs_review", "field_ambiguity")
    assert matched == ["metric:current_ratio:2025"]
    # 缺记录：数字在冻结记录里查无
    status, code, matched = outcome("短期借款为999万元")
    assert (status, code, matched) == ("unsupported", "missing_record", [])
    # 明确错配：同值记录存在，但科目/期间与所写冲突——匹配记录要能指回去
    status, code, matched = outcome("流动比率为64.8%")
    assert (status, code, matched) == ("unsupported", "explicit_mismatch", ["metric:debt_ratio:2025"])
    status, code, _matched = outcome("2024年资产负债率为64.8%")
    assert (status, code) == ("unsupported", "explicit_mismatch")
    # 基础设施错误：整章没有冻结记录（legacy_unfrozen）→ 先补/再生记录，不是改句子
    status, code, matched = _numeric_binding_outcome(
        "p", [], "短期借款为999万元", _number_mentions("短期借款为999万元"), "numeric_fact")
    assert (status, code, matched) == ("unsupported", "infrastructure_error", [])


def test_assessment_reports_position_reason_code_and_matches_per_claim():
    """评估返回的每条主张带正文位置；支持失败项带原因码与匹配记录（不新增数据库列）。"""
    project_id = _project()
    try:
        citation_id, source_id = _source_and_citation(project_id)
        records = _records_for(
            metrics=[{
                "metric_key": "debt_ratio", "name": "资产负债率", "period": "2025", "value": 64.8,
                "unit": "%", "formula": "负债/资产", "input_fact_ids": ["f1"], "source_ids": ["s1"],
            }],
        )
        content = "据募集说明书披露，资产负债率为64.8%。\n\n短期借款为999万元。"
        section_id = _section_row(project_id, content, citation_id, evidence_records=records,
                                  allowed_source_ids=[source_id])
        section = rows("SELECT * FROM report_sections WHERE id=?", (section_id,))[0]
        citation_map = {citation_id: rows("SELECT * FROM citations WHERE id=?", (citation_id,))[0]}
        assessment = assess_section_claims(project_id, section, citation_map, {source_id: "材料原文"}, {source_id})
        supported = next(item for item in assessment["claims"] if "资产负债率为64.8%" in item["claim_text"])
        assert supported["support_status"] in ("direct", "derived")
        assert supported["reason_code"] == ""
        assert supported["matched_record_ids"] == ["metric:debt_ratio:2025"]
        assert isinstance(supported["position"], int) and supported["position"] >= 0
        missing = next(item for item in assessment["claims"] if "999万元" in item["claim_text"])
        assert missing["support_status"] == "unsupported"
        assert missing["reason_code"] == "missing_record"
        assert missing["position"] == content.find("短期借款为999万元")
    finally:
        _cleanup(project_id)


def test_gap_statement_window_covers_enumeration_predicate():
    """缺口谓语在枚举末尾（"A、B、C，均因材料不足…"）时，前面每个标签都要按缺失陈述跳过。

    实测中邮 guarantee：「保证责任上限、保证范围的具体条款表述以及保证人代偿能力，
    均因材料不足而无法判断」——首个标签距谓语 25 字，旧 24 字后窗口差 1 字漏判，
    于是"保证责任上限"被当作存在性主张、整句被误记为无支持（missing_record）。
    """
    from backend.app.p0 import _claim_support_status

    claim = ("保证责任上限、保证范围的具体条款表述以及保证人代偿能力，"
             "均因材料不足而无法判断，不得以零值或模型推断替代。")
    assert _claim_support_status(
        "p", "guarantee", claim, "risk_judgment", ["s1"], {"s1": "材料原文"}, [], "model"
    ) == "derived"
    # 反例：没有缺口语境的同类枚举（标签确实不在证据里）仍按无支持处理。
    factual = "保证责任上限、保证范围以及其他条款均已明确约定并生效。"
    assert _claim_support_status(
        "p", "guarantee", factual, "risk_judgment", ["s1"], {"s1": "材料原文"}, [], "model"
    ) == "unsupported"


# -------------------------------------------- 4. 冻结读取与 legacy 迁移 --

def test_frozen_read_and_legacy_migration():
    project_id = _project()
    try:
        citation_id, source_id = _source_and_citation(project_id)
        # ① 统一记录：直接读取（真实库里每个 section_key 唯一，这里用不同 key 模拟三章）
        records = _records_for(metrics=[{
            "metric_key": "debt_ratio", "name": "资产负债率", "period": "2025", "value": 64.8,
            "unit": "%", "formula": "负债/资产", "input_fact_ids": ["f1"], "source_ids": [source_id],
        }])
        section_id = _section_row(project_id, "正文", citation_id, section_key="financial",
                                  evidence_records=records)
        frozen, version = _frozen_evidence_records(project_id, {"section_key": "financial", "id": section_id})
        assert version == RECORD_VERSION and len(frozen) == len(records)

        # ② 旧行只有 table_records_json：就地迁移为 record_type=table
        legacy_id = _section_row(project_id, "正文", citation_id, section_key="cashflow", table_records=[{
            "record_id": "t1", "value": "100.00", "unit": "万元", "label": "应付债券",
            "period": "2025", "source_ids": [source_id], "page": 9, "verification": "ok",
        }])
        frozen, version = _frozen_evidence_records(project_id, {"section_key": "cashflow", "id": legacy_id})
        assert version == "legacy:table-records-v1"
        assert frozen and frozen[0]["record_type"] == "table"

        # ③ 两者都空：legacy_unfrozen
        empty_id = _section_row(project_id, "正文", citation_id, section_key="debt")
        frozen, version = _frozen_evidence_records(project_id, {"section_key": "debt", "id": empty_id})
        assert frozen == [] and version == "legacy_unfrozen"
    finally:
        _cleanup(project_id)


# ---------------------------------------- 5/6. 后置评估端到端 --

def test_metric_and_rule_sentences_supported_with_chain():
    project_id = _project()
    try:
        citation_id, source_id = _source_and_citation(project_id)
        records = _records_for(
            facts=[{
                "id": "f1", "fact_key": "interest_expense", "name": "利息支出", "period": "2025",
                "scope": "consolidated", "value": 49400, "unit": "万元", "source_ids": [source_id],
                "assertion_status": "asserted", "subject": "统一记录测试企业",
            }],
            metrics=[{
                "metric_key": "debt_ratio", "name": "资产负债率", "period": "2025", "value": 64.8,
                "unit": "%", "formula": "负债/资产", "input_fact_ids": ["f1"], "source_ids": [source_id],
            }, {
                "metric_key": "current_ratio", "name": "流动比率", "period": "2025", "value": 1.58,
                "unit": "倍", "formula": "", "input_fact_ids": [],
            }],
            checks=[{
                "rule_key": "interest_coverage", "title": "利息保障倍数", "status": "passed",
                "thresholds": {"minimum": 1.5},
                "values": [{"label": "利息覆盖倍数", "value": 1.71}],
                "source_ids": [source_id], "fact_ids": ["f1"], "formula": "EBITDA/利息支出",
            }],
        )
        content = (
            "据募集说明书披露，资产负债率为64.8%。\n\n"
            "经营现金流对利息支出覆盖倍数为1.71倍，高于1.5倍阈值。\n\n"
            "流动比率为1.58倍。"
        )
        section_id = _section_row(project_id, content, citation_id, evidence_records=records,
                                  allowed_source_ids=[source_id])
        section = rows("SELECT * FROM report_sections WHERE id=?", (section_id,))[0]
        citation_map = {citation_id: rows("SELECT * FROM citations WHERE id=?", (citation_id,))[0]}
        source_texts = {source_id: "材料原文"}
        assessment = assess_section_claims(project_id, section, citation_map, source_texts, {source_id})
        statuses = {
            claim["claim_text"].rstrip("。").strip(): claim["support_status"]
            for claim in assessment["claims"]
        }
        # 有计算链路的指标与规则数值 → 支持
        assert statuses["据募集说明书披露，资产负债率为64.8%"] in ("direct", "derived")
        assert statuses["经营现金流对利息支出覆盖倍数为1.71倍，高于1.5倍阈值"] in ("direct", "derived")
        # 无链路指标 → 待核验（不得算支持）
        assert statuses["流动比率为1.58倍"] == "needs_review"
        assert assessment["needs_review_claims"] == 1
        assert assessment["support_record_version"] == RECORD_VERSION
        assert assessment["evidence_record_count"] == len(records)
    finally:
        _cleanup(project_id)


def test_conflicting_fact_record_is_not_support():
    """冲突事实（记录冲突）不得作为支持——数字查不到任何非冲突记录 → unsupported。"""
    records = _records_for(facts=[{
        "id": "fc", "fact_key": "revenue", "name": "营业收入", "period": "2025",
        "value": 100, "unit": "万元", "source_ids": ["s1"], "assertion_status": "conflict",
    }])
    from backend.app.chapter_guard import _number_mentions

    mentions = _number_mentions("营业收入为100万元")
    assert _numeric_binding_status("p", records, "营业收入为100万元", mentions, "numeric_fact") == "unsupported"
    # 章节匹配集也不含冲突记录
    assert matchable_records(records) == []


def test_deterministic_template_sentences_are_not_false_flagged():
    """确定性内容的模板句不再误报：无数字→not_applicable；带数字→system_derived（无记录可依）；
    模型内容无来源 → 仍按 unsupported（不放宽）。"""
    project_id = _project()
    try:
        citation_id, source_id = _source_and_citation(project_id)
        content = (
            "现有材料所载有息债务为材料不足，未来12个月到期债务为材料不足。\n\n"
            "本期债券拟发行规模为材料不足，品种一（3年期）兑付日期已披露。"
        )
        section_id = _section_row(project_id, content, citation_id, content_origin="deterministic",
                                  section_key="bond_overview")
        section = rows("SELECT * FROM report_sections WHERE id=?", (section_id,))[0]
        assessment = assess_section_claims(project_id, section, {}, {}, set())
        statuses = [claim["support_status"] for claim in assessment["claims"]]
        assert assessment["unsupported_claims"] == 0
        assert all(status in ("not_applicable", "system_derived") for status in statuses), statuses

        # 负例：模型内容、无来源、有数字 → 仍然 unsupported
        model_id = _section_row(project_id, "短期借款为999万元。", citation_id, content_origin="model")
        model_section = rows("SELECT * FROM report_sections WHERE id=?", (model_id,))[0]
        model_assessment = assess_section_claims(project_id, model_section, {}, {}, set())
        assert model_assessment["unsupported_claims"] == 1
    finally:
        _cleanup(project_id)


def test_gap_statement_labels_skip_presence_gate():
    """缺失陈述（"缺少X/材料不足"）不因科目不在证据里被拦；带数字的仍走数字判定。"""
    from backend.app.p0 import _claim_support_status

    gap_claim = "就关联方资金往来事项，规则核验结果为材料不足：缺少关联方往来或其他应收款及净资产，本项未作风险判断"
    status = _claim_support_status(
        "p", "governance", gap_claim, "risk_judgment", ["s1"], {"s1": "材料原文"}, [], "model")
    assert status == "derived"       # 无数字的缺失陈述：risk_judgment → derived，不是 unsupported
    negative = "本期其他应收款为100万元"
    assert _claim_support_status(
        "p", "financial", negative, "numeric_fact", ["s1"], {"s1": "材料原文"}, [], "model") == "unsupported"


def test_citation_aliases_are_not_business_numbers():
    """（E05、E06、E07）不是数字 5/6/7：引用别名在所有取数字检查里剔除（p0/workflow 同口径）。"""
    from backend.app.delivery_wording import strip_references
    from backend.app.p0 import _numbers

    assert "E05" not in strip_references("据募集说明书披露（E05、E06、E07）")
    assert _numbers("据募集说明书披露（E05、E06、E07）") == []


def test_gap_statement_after_label_and_deterministic_no_records():
    """① "X为材料不足"（记号在标签**之后**）也按缺失陈述处理；
    ② 风险矩阵由规则引擎拼装（"缺少0类/4项"类系统计数）→ system_derived；
    模型内容带数字则仍走数字判定（无记录 → unsupported，不放宽）。"""
    from backend.app.p0 import _claim_support_status

    gap_after = "据发行文件〔资料01〕披露，第一大客户占比为材料不足，前五大客户占比为材料不足"
    assert _claim_support_status(
        "p", "operations", gap_after, "risk_judgment", ["s1"], {"s1": "材料原文"}, [], "deterministic"
    ) == "derived"

    template_numbers = "【高】债券担保材料完整性：缺少0类债券担保材料，核心交易字段缺少4项"
    assert _claim_support_status(
        "p", "risk_matrix", template_numbers, "numeric_fact", ["s1"], {"s1": "x"}, [], "deterministic"
    ) == "system_derived"
    # 负例：非系统章节的模型内容同句仍走数字判定（无记录 → unsupported）
    assert _claim_support_status(
        "p", "debt", template_numbers, "numeric_fact", ["s1"], {"s1": "x"}, [], "model"
    ) == "unsupported"


def test_unsupported_numbers_exclude_citation_aliases_and_format_numbers():
    """诊断里的"无支持数字"必须与**判定用过的**数字同源。

    裸 ``re.findall`` 会把引用别名读成数字：``（E01）`` → ``01``。它经
    ``unsupported_numbers`` 送进修复提示，只会让修复模型跑去改引用，而真正该处理的
    数字被漏掉。走 `_strip_format_numbers`（唯一剔除点）后只剩业务数字。

    期限短语（"未来12至18个月"）按收口方案 §一**不再是业务数字**：该句不再产生
    无支持数字（原样例把它当业务数字断言的写法随之更新）。
    """
    project_id = _project()
    try:
        citation_id, source_id = _source_and_citation(project_id)
        content = (
            "评级机构认为，公司流动比率为1.5倍（E01）。"
            "公司信用水平在未来12至18个月内将保持稳定。"
        )
        section_id = _section_row(project_id, content, citation_id, content_origin="model")
        section = rows("SELECT * FROM report_sections WHERE id=?", (section_id,))[0]
        assessment = assess_section_claims(project_id, section, {}, {}, set())
        by_text = {
            item["claim_text"].rstrip("。").strip(): item for item in assessment["claims"]
        }
        first = by_text["评级机构认为，公司流动比率为1.5倍（E01）"]
        assert first["support_status"] == "unsupported"
        assert "01" not in first["unsupported_numbers"], first["unsupported_numbers"]
        assert "1.5倍" in first["unsupported_numbers"], first["unsupported_numbers"]
        # 期限短语句：不产生无支持数字（收口方案 §一）。
        second = by_text["公司信用水平在未来12至18个月内将保持稳定"]
        assert second["unsupported_numbers"] == [], second["unsupported_numbers"]
    finally:
        _cleanup(project_id)


# ------------------------------------------------- 披露数字记录（disclosure） --

def test_disclosure_records_extract_change_and_named_facts_from_quotes():
    """收口方案 §一：引文里的披露数字按证据片段冻结（变动句 / 命名值句）——正例。

    取自北投 0685da5d 的真实引文（p92 存货变动、p38 子公司）：PDF 回流的断行与空格
    必须被容忍（"101.71 ⏎ 亿元"），科目/期间/单位/来源随记录保留。
    """
    evidence = [{
        "id": "c-1", "source_id": "src-1", "page": 92, "document_kind": "募集说明书",
        "quote": ("2024 年末发行人存货较2023 年末增加101.71 \n亿元，增幅为13.98%。"
                  "2025 年末发行人存货较2024 年末增加15.15 亿元，增幅为 \n1.83%。"),
    }, {
        "id": "c-2", "source_id": "src-2", "page": 38, "document_kind": "募集说明书",
        "quote": ("京投发展2025 年度营业收入较2024 年同比增长303.48%，主要系项目交付收入增加所致。"
                  "京投发展2025 年度净利润为-15.36 亿元，主要系计提减值所致。"),
    }]
    records = [r for r in _records_for(evidence=evidence) if r["record_type"] == "disclosure"]
    got = {(r["label"], r["value"], r["unit"], r["period"]) for r in records}
    assert ("存货", "101.71", "亿元", "2024") in got
    assert ("存货增幅", "13.98", "%", "2024") in got
    assert ("存货", "15.15", "亿元", "2025") in got
    assert ("存货增幅", "1.83", "%", "2025") in got
    assert ("京投发展营业收入", "303.48", "%", "2025") in got
    assert ("京投发展净利润", "-15.36", "亿元", "2025") in got
    # 来源与定位随记录保留（支持侧要求可追溯）。
    assert all(r["source_ids"] and r["page"] for r in records)
    # 同一 (科目,值,单位,期间) 不重复登记。
    assert len(records) == len({(r["label"], r["value"], r["unit"], r["period"]) for r in records})


def test_disclosure_records_read_year_header_tables_with_periods():
    """评级报告财务指标表（年份表头管道行）→ 每格一条带期间的记录；非年度列待核验。"""
    evidence = [{
        "id": "c-3", "source_id": "src-3", "page": 21, "document_kind": "信用评级报告",
        "quote": ("财务指标 | 2023 | 2024 | 2025 | 2026.3/2026.1~3\n"
                  "营业毛利率（%） | -45.98 | -83.96 | -59.54 | -90.25\n"
                  "经营活动产生的现金流量净额利息保障倍数（X） | 0.24 | 0.55 | 1.17 | --\n"
                  "总债务/EBITDA（X） | 22.73 | 22.38 | 21.55 | --\n"),
    }]
    records = [r for r in _records_for(evidence=evidence) if r["record_type"] == "disclosure"]
    got = {(r["label"], r["value"], r["period"], r["status"]) for r in records}
    assert ("经营活动产生的现金流量净额利息保障倍数（X）", "0.24", "2023", "verified") in got
    assert ("经营活动产生的现金流量净额利息保障倍数（X）", "1.17", "2025", "verified") in got
    assert ("总债务/EBITDA（X）", "22.73", "2023", "verified") in got
    assert ("总债务/EBITDA（X）", "21.55", "2025", "verified") in got
    assert ("营业毛利率（%）", "-45.98", "2023", "verified") in got
    # "--" 格不登记；非年度列（2026.3）期间无法唯一绑定 → needs_review。
    assert ("营业毛利率（%）", "-90.25", "", "needs_review") in got
    assert not any(r["value"] == "--" for r in records)


def test_disclosure_records_do_not_harvest_unbound_numbers():
    """负例：没有上述结构的数字不得被"整页同值放行"——公司简介类引文不产生记录。"""
    evidence = [{
        "id": "c-4", "source_id": "src-4", "page": 5, "document_kind": "募集说明书",
        "quote": "发行人注册资本 100 亿元，注册地址位于北京市。本项目投资总额约 50 亿元。",
    }]
    records = [r for r in _records_for(evidence=evidence) if r["record_type"] == "disclosure"]
    assert records == []


def test_support_binding_turns_direct_with_disclosure_records():
    """披露记录接入支持判定：北投 #20 场景从 unsupported 转为 direct。"""
    from backend.app.chapter_guard import _number_mentions

    evidence = [{
        "id": "c-1", "source_id": "src-1", "page": 92, "document_kind": "募集说明书",
        "quote": "2024 年末发行人存货较2023 年末增加101.71 \n亿元，增幅为13.98%。",
    }]
    records = _records_for(evidence=evidence)
    claim = "募集说明书披露2024年末存货较2023年末增加101.71亿元，增幅为13.98%。"
    status = _numeric_binding_status("", records, claim, _number_mentions(claim), "numeric_fact")
    assert status in ("direct", "derived"), status
    # 负例：引文里没有的披露值仍判 unsupported（不得被"整页同值放行"）。
    other = "募集说明书披露2024年末存货较2023年末增加999.99亿元。"
    assert _numeric_binding_status(
        "", records, other, _number_mentions(other), "numeric_fact") == "unsupported"


def test_rule_value_records_carry_chinese_ratio_units():
    """规则结果值的中文科目名要能恢复单位（2026-10-15 回退章定向复核）。

    原实现只认英文键（pct/ratio）→ "到期集中度" 等单位线索为空 → 记录 unit=""，
    正文"66.1%"永远匹配不上"66.129…"（华辰 debt/governance 回退的判定侧根因）。
    """
    from backend.app.chapter_guard import _allowed_number_index, _mention_allowed
    from backend.app.evidence_records import _rule_records, number_unit_hint
    from backend.app.p0 import _number_mentions

    # 中文比率类词表（本次修复）+ 不误伤裸"率"（周转率=次、资产负债率=% 同一后缀）。
    assert number_unit_hint("到期集中度") == "%"
    assert number_unit_hint("其他应收/净资产") == "%"
    assert number_unit_hint("逾期应收占比") == "%"
    assert number_unit_hint("due_12m_pct") == "%"
    assert number_unit_hint("周转率") == ""

    check = {
        "rule_key": "debt_maturity", "title": "一年内到期债务集中度", "status": "warning",
        "result_value": 66.12903225806451,
        "values": [{"label": "到期集中度", "value": 66.12903225806451}],
        "thresholds": {"due_12m_pct": 50.0}, "formula": "", "source_ids": ["source-1"],
    }
    records = _rule_records([check])
    by_label = {item["label"]: item for item in records}
    assert by_label["到期集中度"]["unit"] == "%"
    index = _allowed_number_index(evidence_number_keys(supportable_records(records)))
    mentions = _number_mentions("一年内到期债务占有息负债的比例为66.1%，超过50%的阈值，构成到期集中度提示。")
    assert mentions and all(_mention_allowed(item, index) for item in mentions)


def test_disclosure_records_extract_series_and_colon_forms():
    """系列句（"分别为…"）与冒号式单值（2026-10-15 回退章复核新增）。

    实测锚点：北投 cashflow 投资现金流系列、中邮 debt 有息负债系列、华辰 guarantee
    反担保折后价值——均为"引文有、记录无"的记录覆盖缺口。
    """
    from backend.app.evidence_records import build_evidence_records

    evidence = [{
        "quote": "最近三年，发行人投资活动现金流净额分别为-497.09亿元、-444.38亿元和-331.00亿元。"
                 "反担保折后价值：16,000万元。",
        "source_id": "s-1",
    }]
    records = build_evidence_records(evidence=evidence)
    series = [item for item in records if item["label"] == "投资活动现金流净额"]
    # 值经 Decimal 归一（尾零去除："-331.00" → "-331"）；数值等价性由匹配层保证。
    assert {item["value"] for item in series} == {"-497.09", "-444.38", "-331"}
    assert all(item["unit"] == "亿元" for item in series)
    colon = [item for item in records if "反担保折后价值" in item["label"]]
    assert colon and colon[0]["value"] == "16000" and colon[0]["unit"] == "万元"


def test_disclosure_series_requires_label_and_at_least_two_values():
    """反例：无前缀科目名 / 单值 / 冒号后非数值——不得凭空造记录。"""
    from backend.app.evidence_records import build_evidence_records

    assert not [r for r in build_evidence_records(
        evidence=[{"quote": "分别为10亿元。", "source_id": "s-1"}]) if r.get("unit") == "亿元"]
    assert not [r for r in build_evidence_records(
        evidence=[{"quote": "注：材料不足，详见后文说明。", "source_id": "s-1"}])]


def test_disclosure_series_accepts_omitted_wei():
    """"分别X、Y、Z"（省略"为"）也是系列句（2026-10-15 北投 operations #7 实测引文）。"""
    from backend.app.evidence_records import build_evidence_records

    records = build_evidence_records(evidence=[{
        "quote": "最近三年，发行人经营性活动产生的现金流出分别506.61亿元、479.38亿元和375.19亿元。",
        "source_id": "s-1",
    }])
    hit = [item for item in records if item["label"] == "经营性活动产生的现金流出"]
    assert {item["value"] for item in hit} == {"506.61", "479.38", "375.19"}


def test_disclosure_series_accepts_ratio_trigger():
    """"…的比例为A、B和C"（触发词"比例为"而非"分别为"）也是系列句。

    正例：北投 operations #13 第二段（"占经营活动现金流出的比例为10.11%、19.26%和14.76%"）。
    负例：仅有"为"、无数值枚举，或触发词后是普通叙述——不得登记。
    """
    from backend.app.evidence_records import build_evidence_records

    records = build_evidence_records(evidence=[{
        "quote": "最近三年，发行人支付其他与经营活动有关的现金分别为51.22亿元、92.32亿元及"
                 "55.36亿元，占经营活动现金流出的比例为10.11%、19.26%和14.76%。",
        "source_id": "s-1",
    }])
    pct = [item for item in records if item["value"] in {"10.11", "19.26", "14.76"}]
    assert len(pct) == 3
    assert all(item["unit"] == "%" for item in pct)
    assert all("经营活动现金流出" in item["label"] for item in pct)
    # 负例：无枚举触发词不得登记（"净利润为5.8%"是单值，交命名值句处理）。
    only = build_evidence_records(evidence=[{
        "quote": "报告期内公司净利润为5.80%，较上年有所下降。", "source_id": "s-1"}])
    assert not [item for item in only if item["value"] == "5.80" and item["unit"] == "%"]


def test_disclosure_space_separated_table_rows():
    """⑥ 空格分列表格行（"科目 值1 值2 值3"，无 Markdown 管道）也抽成记录。

    正例：北投 operations 现金流量表（万元系列）；单位缺省置空、状态 needs_review。
    负例：单行"标签+数字"、或纯叙述句（数字带单位文本）——不得当表格。
    """
    from backend.app.evidence_records import build_evidence_records

    quote = (
        "经营活动现金流出小计 3,751,868.91 4,793,811.23 5,066,119.59\n"
        "经营活动产生的现金流量净额 1,749,719.14 903,091.39 433,985.81\n"
        "筹资活动产生的现金流量净额 1,929,972.88 3,600,228.38 5,605,248.30\n"
        "最近三年，公司经营性活动产生的现金流入分别为550.01 亿元、569.69 亿元和"
        "550.16 亿元。"
    )
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    net = [item for item in records if item["label"] == "经营活动产生的现金流量净额"]
    assert {item["value"] for item in net} == {"1749719.14", "903091.39", "433985.81"}
    assert all(item["unit"] == "" and item["status"] == "needs_review" for item in net)
    # 负例：只有一行"标签+数字串"不成表；纯叙述句（数字带"亿元"文本）不被当表格。
    single = build_evidence_records(evidence=[{
        "quote": "经营活动产生的现金流量净额 1,749,719.14 903,091.39 433,985.81",
        "source_id": "s-1"}])
    assert not [item for item in single if item["label"].startswith("经营活动产生的现金流量净额")]
    prose = build_evidence_records(evidence=[{
        "quote": "最近三年经营性活动产生的现金流入分别为550.01 亿元、569.69 亿元和550.16 亿元。",
        "source_id": "s-1"}])
    assert not [item for item in prose if item["status"] == "needs_review"]


def test_blank_unit_disclosure_registers_money_and_percent_keys():
    """空格表格记录（单位缺省）的同值要能为正文的**带单位**数字背书。

    `_mention_allowed` 按单位分桶，空白单位记录若不登记标准单位，"引文里有记录"却仍判
    missing_record（北投 operations #14 实测）。仅对 **disclosure**（来自引文）补登记；
    fact/metric 的裸数字不在此列（"配置/指标 JSON 裸数字不得为正文数值背书"）。
    """
    from backend.app.evidence_records import _record, evidence_number_keys

    disclosure = _record(
        record_id="disclosure:x", record_type="disclosure", value="1749719.14",
        unit="", label="经营活动产生的现金流量净额", status="needs_review")
    fact = _record(
        record_id="fact:y", record_type="fact", value="1749719.14",
        unit="", label="某事实", status="verified")
    d_keys = evidence_number_keys([disclosure])
    f_keys = evidence_number_keys([fact])
    assert ("1749719.14", "万元") in d_keys
    assert ("1749719.14", "%") in d_keys
    assert ("1749719.14", "万元") not in f_keys



def test_year_header_accepts_nianmo_and_section_labels():
    """表头年份列容"年末/年度/年底"；表内分节标签行（"资产：""负债："）跳过而非中断。

    原 `re.fullmatch(r"(20\\d{2})\\s*年?")` 不认"2025 年末"→整张表 0 记录；分节标签行
    使 `"|" not in row` 提前 break→同样 0 记录（北投 debt #21、华辰 financial #28 实测）。
    """
    from backend.app.evidence_records import build_evidence_records

    quote = (
        "合并资产负债表；单位：万元；项目 | 2025 年末 | 2024 年末 | 2023 年末\n"
        "资产：\n"
        "货币资金 | 6,820 | 6,206 | 5,592\n"
        "其中：受限资金 | 818 | 745 | 671\n"
        "负债：\n"
        "短期借款 | 11,160 | 10,156 | 9,151\n"
    )
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    money = [item for item in records if item["label"] == "货币资金"]
    assert {item["value"]: item["period"] for item in money} == {
        "6820": "2025", "6206": "2024", "5592": "2023"}
    assert all(item["status"] == "verified" for item in money)
    # 分节标签行之后的行仍被登记（"短期借款"在"负债："之后）。
    assert [item for item in records if item["label"] == "短期借款"]
    # 负例：含数字的"列"不跳（数据行）——非管道长句仍 break。
    broken = (
        "表头 | 2025 年末 | 2024 年末 | 2023 年末\n"
        "合计 1,234 5,678 9,012\n"
    )
    assert not build_evidence_records(evidence=[{"quote": broken, "source_id": "s-1"}])


def test_named_value_accepts_zhihe_connector():
    """③ 命名值句的"名词 + 之和/合计 + 为"形态（"…债券余额之和为20.12亿元"）。

    原连接词集只有 为/的/达/占，漏"之和为"（中邮 debt #4 的 20.12亿元 实测）。
    负例：无科目名结尾词的裸值句不登记。
    """
    from backend.app.evidence_records import build_evidence_records

    quote = ("银行借款与公司债券外其他公司信用类债券余额之和为20.12亿元，"
             "占有息负债余额的比例为14.00%。")
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    hit = [item for item in records if item["value"] == "20.12" and item["unit"] == "亿元"]
    assert hit, records
    assert "余额" in hit[0]["label"]
    # 负例：连接词之后没有科目名结尾词（"为20.12亿元"裸值）不得登记。
    bare = build_evidence_records(evidence=[{
        "quote": "该等款项之和为20.12亿元。", "source_id": "s-1"}])
    assert not [item for item in bare if item["value"] == "20.12"]


def test_actual_backref_registers_reported_value():
    """⑦ "年份+实际+值" 回指式单值：科目从同引文唯一命名值句回指，状态 needs_review。

    样本：华辰 operations #15（预测9,000万元 / 2025年实际6,500万元 并列对比）。
    负例：同引文有**多个**不同科目的命名值句时不启用（回指对象不唯一）。
    """
    from backend.app.evidence_records import build_evidence_records

    quote = ("2026—2029年（债券存续期）预测经营活动现金流量净额9,000万元（属前瞻性信息）；"
             "该口径与财务表所载2025年实际6,500万元不同期间、不同口径。")
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    hit = [item for item in records if item["value"] == "6500"]
    assert hit and hit[0]["period"] == "2025" and hit[0]["status"] == "needs_review", records
    # 负例：两个不同科目的命名值句 → 回指对象不唯一 → 不登记 6,500。
    ambiguous = ("预测经营活动现金流量净额9,000万元，预测净利润1,200万元；"
                 "2025年实际6,500万元。")
    recs2 = build_evidence_records(evidence=[{"quote": ambiguous, "source_id": "s-1"}])
    assert not [item for item in recs2 if item["value"] == "6500"]


def test_metric_label_aliases_frozen_and_bind():
    """指标记录冻结叙述别名（`label_aliases`），供"说明性短语"绑定。

    样本：华辰 financial #28 的 0.29倍（"可用资金对未来12个月到期债务的覆盖" vs 规范名
    "现金短债覆盖"）。别名须 ≤ SUBJECT_LABEL_WINDOW（否则永远配不上）。
    """
    from backend.app.chapter_guard import SUBJECT_LABEL_WINDOW
    from backend.app.evidence_records import _metric_records
    from backend.app.metrics import METRIC_LABEL_ALIASES

    recs = _metric_records([{
        "metric_key": "cash_short_debt_ratio", "name": "现金短债覆盖", "value": "0.2928",
        "unit": "倍", "period": "2025", "formula": "(货币资金-受限资金)/一年内到期债务",
        "input_fact_ids": ["f1", "f2", "f3"], "source_ids": ["s-1"], "status": "ok"}])
    assert recs and recs[0]["label_aliases"] == list(
        METRIC_LABEL_ALIASES["cash_short_debt_ratio"])
    # 所有登记的别名必须在窗口内可命中。
    for key, aliases in METRIC_LABEL_ALIASES.items():
        for alias in aliases:
            assert len(alias) <= SUBJECT_LABEL_WINDOW, (key, alias)


def test_disclosure_comparison_back_value_registers_after_value():
    """⑧ "由 A 提高为 B" 比较句的**后值**抽成记录（2026-10-16 中邮 debt #26 实测）。

    正例：后值 67.30% 落记录（needs_review：科目就近推断），前值 64.45% 由既有形态
    登记——整句两个数字都能绑定。
    负例：无比较形态不抽；跨单位（"64.45% → 2.85 个百分点"不是同口径比较）不抽。
    """
    from backend.app.evidence_records import _lookup_flat, _quote_comparison_back_facts, build_evidence_records

    quote = ("本期债券发行完成且上述募集资金运用计划予以执行后，发行人合并报表口径的"
             "资产负债率水平由原先的64.45%提高为67.30%。")
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    # 记录值经 `_number_text` 归一（"67.30" → "67.3"）。
    after = [item for item in records if item["value"] == "67.3" and item["unit"] == "%"]
    assert after, "比较句后值未落记录"
    assert "资产负债率" in after[0]["label"]
    assert after[0]["status"] == "needs_review"
    # 负例①：无比较形态。
    assert _quote_comparison_back_facts(_lookup_flat("资产负债率为64.45%。")) == []
    # 负例②：跨单位比较不登记（百分点不是 %，不视为同口径前值/后值）。
    cross = build_evidence_records(evidence=[{
        "quote": "资产负债率由64.45%上升为2.85个百分点。", "source_id": "s-1"}])
    assert not [item for item in cross if item["value"] == "2.85"]


def test_disclosure_semicolon_series_registers_items():
    """⑨ 分号分隔的"科目值单位；…"系列抽成记录（2026-10-16 华辰 debt #10 实测）。

    正例：货币资金6,820万元；受限资金818万元；… —— 各落一条 needs_review。
    负例：单项不构成系列；科目头以披露名词（营业收入…）结尾时交③处理、不重复登记。
    """
    from backend.app.evidence_records import build_evidence_records

    quote = "货币资金6,820万元；受限资金818万元；应收账款12,000万元；其他应收款2,880万元；"
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    picked = {item["label"]: item for item in records if item["status"] == "needs_review"}
    assert picked.get("货币资金", {}).get("value") == "6820"
    assert picked.get("受限资金", {}).get("value") == "818"
    # 负例①：单项不构成系列（末项无分号同样不登记）。
    single = build_evidence_records(evidence=[{"quote": "货币资金6,820万元；", "source_id": "s-1"}])
    assert not [item for item in single if item["label"] == "货币资金"]
    # 负例②：披露名词结尾的科目由 ③ 负责（verified），⑨ 不重复登记 needs_review。
    named = build_evidence_records(evidence=[{
        "quote": "营业收入48,000万元；净利润2,800万元；", "source_id": "s-1"}])
    assert not [item for item in named if item["label"] == "营业收入" and item["status"] == "needs_review"]


def test_disclosure_space_row_accepts_percent_marked_labels():
    """⑥ 空格行标签容「（%）」等百分比记号（2026-10-16 中邮 financial 实测）。

    "资产负债率（%） 44.35 51.33 55.21" —— 标签尾部带"（%）"时原字符类不含 %，
    整行不匹配、整张母公司财务表 0 记录（-118.17 亿/55.21% 等数字全判无支持）。
    """
    from backend.app.evidence_records import build_evidence_records

    quote = (
        "净资产收益率（%） 10.12 5.15 -6.55\n"
        "资产负债率（%） 44.35 51.33 55.21\n"
    )
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    rates = [item for item in records if item["label"] == "资产负债率"]
    assert {item["value"] for item in rates} >= {"44.35", "51.33", "55.21"}
    assert all(item["status"] == "needs_review" for item in rates)


def test_disclosure_named_fact_accepts_zhanbi_single_value():
    """③ 名词表补"占比"：单值占比句"…合计占比为81.61%"落记录（2026-10-16 中邮 financial 实测）。"""
    from backend.app.evidence_records import build_evidence_records

    quote = "2023－2025年末公司所有者权益呈增长趋势，实收资本和资本公积合计占比为81.61%，权益稳定性很好。"
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    ratio = [item for item in records if item["value"] == "81.61" and item["unit"] == "%"]
    assert ratio, "单值占比句未落记录"
    assert "占比" in ratio[0]["label"]


def test_disclosure_aggregate_and_coverage_value_forms():
    """⑩-① 汇总句与 ⑩-② 占比句落记录（2026-10-16 中邮 cashflow #20 实测）。

    正例（材料 p23/p122/p133 原文）：
      "…金融投资等高流动性资产，合计达169.02亿元"（科目与"合计"之间夹逗号，③ 漏抽）；
      "占总资产（扣除代理买卖证券款）的比重达73.55%"（"占…的比重"整段是科目）。
    负例：无科目（"合计达100万元"）或科目过短（"该公司合计达100万元"）不登记。
    """
    from backend.app.evidence_records import build_evidence_records

    quote = ("截至2025 年末，公司货币资金（扣除客户资金存款）、结算备付金（扣除客户备付金）、"
             "金融投资等高流动性资产，合计达169.02 亿元，占总资产（扣除代理买卖证券款）的"
             "比重达73.55%。")
    records = build_evidence_records(evidence=[{"quote": quote, "source_id": "s-1"}])
    labels = {item["label"]: item for item in records}
    assert labels.get("金融投资等高流动性资产", {}).get("value") == "169.02"
    assert labels.get("金融投资等高流动性资产", {}).get("unit") == "亿元"
    assert labels.get("占总资产（扣除代理买卖证券款）的比重", {}).get("value") == "73.55"
    assert labels["占总资产（扣除代理买卖证券款）的比重"]["unit"] == "%"
    # 负例①：没有科目短语的裸汇总值不登记。
    bare = build_evidence_records(evidence=[{"quote": "合计达100万元。", "source_id": "s-1"}])
    assert not [item for item in bare if item["value"] == "100"]
    # 负例②：科目短语 <4 字不登记（避免"该公司"类指代被当成科目）。
    short = build_evidence_records(evidence=[{"quote": "该公司合计达100万元。", "source_id": "s-1"}])
    assert not [item for item in short if item["value"] == "100"]
