"""误退扫描器五分类测试（三项遗留修复方案 §三）。

分类依据是**结构性证据**：被拒数字是否在冻结记录里同值存在（误退信号）、
是否存在明确错误（确证拒绝）、是否无法判定（待人工）、首稿被拒后修复是否被采用。
"""
from __future__ import annotations

import json

from scripts.stage6_acceptance import _classify_failure, _diagnose_fallbacks, _frozen_number_keys


def _attempt(status="failed", adopted=False, diagnostics=None):
    return {
        "status": status, "adopted": adopted,
        "diagnostics_json": json.dumps(diagnostics or {}, ensure_ascii=False),
    }


def _records():
    return [{
        "record_id": "table:t1", "record_type": "table", "value": "100", "unit": "万元",
        "label": "应付债券", "status": "verified",
    }]


def test_rejected_number_found_in_frozen_records_is_suspected_false():
    """被拒"新数字"在冻结记录里同值存在 → 疑似误退（单位继承/精度/来源类型/格式类）。"""
    records = _records()
    bucket, hits, _ = _classify_failure(
        _attempt(diagnostics={
            "safety_repair_reasons": ["章节包含确定性草稿和证据之外的新数字"],
            "unexpected_numbers": [{"normalized_value": "100", "normalized_unit": "万元"}],
        }),
        records, _frozen_number_keys(records),
    )
    assert bucket == "suspected_false_rejection"
    assert hits and hits[0]["record_id"] == "table:t1"


def test_rejected_number_without_record_is_confirmed_valid():
    records = _records()
    bucket, hits, _ = _classify_failure(
        _attempt(diagnostics={
            "safety_repair_reasons": ["章节包含确定性草稿和证据之外的新数字"],
            "unexpected_numbers": [{"normalized_value": "999", "normalized_unit": "万元"}],
        }),
        records, _frozen_number_keys(records),
    )
    assert bucket == "confirmed_valid_rejection" and hits == []


def test_entity_rejection_is_not_auto_confirmed():
    """实体类拒绝（"新主体"）归**待人工**：离线无法复核实体比对，不得自动确证。

    中邮 rating 实测：「联合资信评估股份有限公司」就在冻结引文里仍被判"新主体"，
    曾被本分类器归为确证拒绝——"疑似误退为零"不能建立在此类未经复核的归因上。
    """
    bucket, hits, _ = _classify_failure(
        _attempt(diagnostics={
            "safety_repair_reasons": [
                "章节包含确定性草稿和证据之外的新主体：评级结论根据联合资信评估股份有限公司"],
        }),
        [], set(),
    )
    assert bucket == "needs_human_review" and hits == []


def test_review_items_classify_as_needs_human_review():
    bucket, _, _ = _classify_failure(
        _attempt(diagnostics={
            "safety_repair_reasons": ["章节数字与所写科目不匹配"],
            "needs_review_items": [{"code": "generic_total_label"}],
        }),
        [], set(),
    )
    assert bucket == "needs_human_review"


def test_diagnose_fallbacks_five_buckets_and_unclassified():
    records = _records()
    sections = [
        {
            "section_key": "debt", "generation_status": "fallback", "content_origin": "deterministic",
            "fallback_reason": "章节包含确定性草稿和证据之外的新数字", "fallback_stage": "chapter_validation",
            "support_validation_status": "", "evidence_records_json": json.dumps(records),
        },
        {
            "section_key": "financial", "generation_status": "model_passed", "content_origin": "model",
            "fallback_reason": "", "fallback_stage": "none", "support_validation_status": "",
            "evidence_records_json": json.dumps(records),
        },
        {
            # 基础设施失败（输入预算/模型调用）：不是误退，但也必须被解释——不得计未分类。
            "section_key": "cashflow", "generation_status": "fallback", "content_origin": "deterministic",
            "fallback_reason": "章节模型调用或结构校验失败：input_budget_exceeded",
            "fallback_stage": "model_call", "support_validation_status": "",
            "evidence_records_json": json.dumps(records),
        },
        {
            # 非回退终态（确定性章）：不参与诊断，也不计未分类。
            "section_key": "operations", "generation_status": "deterministic", "content_origin": "deterministic",
            "fallback_reason": "", "fallback_stage": "none", "support_validation_status": "",
            "evidence_records_json": "[]",
        },
        {
            "section_key": "rating", "generation_status": "fallback", "content_origin": "deterministic",
            "fallback_reason": "某种全新的未知原因", "fallback_stage": "chapter_validation",
            "support_validation_status": "", "evidence_records_json": "[]",
        },
    ]
    attempts = {
        # 数字 100 有记录却被拒 → 疑似误退
        "debt": [_attempt(diagnostics={
            "safety_repair_reasons": ["章节包含确定性草稿和证据之外的新数字"],
            "unexpected_numbers": [{"normalized_value": "100", "normalized_unit": "万元"}],
        })],
        # 首稿被拒 → 修复采用
        "financial": [
            _attempt(diagnostics={"safety_repair_reasons": ["章节包含禁止的承保或审批结论"]}),
            _attempt(status="passed", adopted=True),
        ],
        # 模型调用失败（无安全原因）
        "cashflow": [_attempt(status="failed")],
    }
    out = _diagnose_fallbacks(sections, attempts)
    diagnosis = out["fallback_diagnosis"]
    assert diagnosis["suspected_false_rejection"] == 1
    assert diagnosis["repaired_and_adopted"] == 1
    assert diagnosis["final_failure"] == 2          # debt + cashflow（终态维度）
    assert out["unclassified_fallbacks"] == 1       # rating 的未知原因必须计未分类
    items = out["fallback_diagnosis_items"]
    assert items["suspected_false_rejection"][0]["matched_records"][0]["record_id"] == "table:t1"
    assert items["suspected_false_rejection"][0]["blocks_final_delivery"] is True
    assert items["repaired_and_adopted"][0]["blocks_final_delivery"] is False
    # 基础设施失败被解释（终态里带 classification），且不在疑似误退里
    infra = [item for item in items["final_failure"] if item["section"] == "cashflow"]
    assert infra and infra[0]["classification"] == "infrastructure_failure"
    assert all(item["section"] != "cashflow" for item in items["suspected_false_rejection"])
    # 确定性章不参与诊断
    assert all(item["section"] != "operations" for item in items["final_failure"])


def test_anchor_number_normalization():
    """锚点按数值等价比对：去尾零与省略 % 符不算缺失（三项遗留修复方案 §五）。"""
    from scripts.stage6_acceptance import _number_present

    assert _number_present("合并资产总计96,717,233.2万元。", "96,717,233.20")
    assert _number_present("占其他应收款总额的59.22为高占比", "59.22%")
    assert not _number_present("合并资产总计96,717,233.3万元。", "96,717,233.20")


def test_support_failure_with_only_support_reasons_is_classified():
    """支持检查并入同一候选评估后（计划 §四 line 33）的回归防线。

    这类尝试**没有** ``safety_repair_reasons``（安全修复没触发），原因文本只在
    ``support_reasons`` 里。若不回填，``reason_text`` 为空 → 落到"未分类"，
    而验收硬性要求 ``unclassified_fallbacks == 0``。
    """
    bucket, hits, reason_text = _classify_failure(
        _attempt(diagnostics={
            "support_failed": True,
            "support_reasons": ["模型正文未通过前置证据支持检查（2 条主张无证据支持）"],
            "support_unsupported_claims": 2,
        }),
        [], set(),
    )
    assert bucket == "needs_human_review", "支持未过的章节必须归入明确分类"
    assert "前置证据支持" in reason_text
    assert hits == []


def test_support_failure_is_not_counted_as_unclassified_in_the_sweep():
    """端到端：只有 support_reasons 的回退章节不得计入未分类。"""
    sections = [{
        "section_key": "governance", "generation_status": "fallback",
        "content_origin": "deterministic",
        "fallback_reason": "模型正文未通过前置证据支持检查（2 条主张无证据支持）（已尝试一次修复仍未通过）",
        "fallback_stage": "chapter_validation", "support_validation_status": "",
        "evidence_records_json": "[]",
    }]
    attempts = {"governance": [_attempt(diagnostics={
        "support_failed": True,
        "support_reasons": ["模型正文未通过前置证据支持检查（2 条主张无证据支持）"],
        "support_unsupported_claims": 2,
    })]}
    diagnosis = _diagnose_fallbacks(sections, attempts)
    assert diagnosis["unclassified_fallbacks"] == 0
    assert diagnosis["fallback_diagnosis"]["needs_human_review"] == 1
