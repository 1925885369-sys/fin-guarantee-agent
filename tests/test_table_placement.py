"""系统表格的**位置**：按正文锚点分散到各处，而不是全部堆在章末。

2026-10-28 用户反馈："主要会计科目分析章节的表格出现失效"——其实是财务章 24 张表全挤在
章末的「系统整理数据表」块里，正文讲固定资产/存货/应收时旁边没有表，表和正文脱节。
`report_tables.place_blocks_by_anchor` 把表按业务锚点插到**讲它的那一段之后**。
"""
from __future__ import annotations

from backend.app.report_outline import content_blocks
from backend.app.report_tables import place_blocks_by_anchor
from backend.app.table_block import table_block_from_dict


def _block(table_id: str, title: str):
    return table_block_from_dict({
        "table_id": table_id, "title": title, "table_type": "annual_series",
        "display_mode": "report", "origin_mode": "profile_facts", "subject": "发行人",
        "scope": "合并口径", "currency": "CNY", "unit": "万元",
        "columns": [{"key": "c1", "label": "2025 年末", "role": "period_value", "unit": "万元"}],
        "rows": [{"row_key": "r1", "kind": "normal",
                  "cells": [{"raw": "1", "normalized": "1", "unit": "万元", "status": "ok"}]}],
        "source_refs": [], "notes": [], "validation_status": "passed",
    })


FINANCIAL = (
    "### 1. 财务概览\n\n"
    "本章财务分析以合并口径三个完整年度的资产负债表、利润表与现金流量表为基础。\n\n"
    "### 2. 主要会计科目分析\n\n"
    "资产端最显著的特征是长期资产占比高。\n\n"
    "应收类科目方面，应收账款为100万元。\n\n"
    "负债端，长期借款为200万元。\n"
)


def test_tables_follow_the_paragraph_that_discusses_them():
    parsed = content_blocks(FINANCIAL)
    blocks = [
        _block("report-financial-balance", "三年主要资产负债项目表"),
        _block("report-financial-receivables", "应收账款规模及明细"),
        _block("report-financial-debt", "债务结构表"),
    ]
    placed, loose = place_blocks_by_anchor("financial", blocks, parsed)

    assert not loose, "三张表都应找到归属段落"
    # 每张表各自落在**不同**段落之后，而不是挤在同一处
    assert len({index for index, items in placed.items() if items}) == 3
    for index, items in placed.items():
        text = str(parsed[index].get("text") or "")
        expected = {
            "report-financial-balance": "资产端",
            "report-financial-receivables": "应收类",
            "report-financial-debt": "负债端",
        }[items[0].table_id]
        assert expected in text, f"{items[0].title} 落到了不相关的段落：{text[:30]}"


def test_overview_paragraph_does_not_swallow_tables():
    """总述段会同时提到"资产负债表/利润表/现金流量表"，不能把所有表都吸过去。"""
    parsed = content_blocks(FINANCIAL)
    blocks = [
        _block("report-financial-balance", "三年主要资产负债项目表"),
        _block("report-financial-cashflow", "主要现金流量项目表"),
    ]
    placed, loose = place_blocks_by_anchor("financial", blocks, parsed)

    overview_index = 1  # "本章财务分析以…资产负债表、利润表与现金流量表为基础。"
    assert "资产负债表" in str(parsed[overview_index].get("text"))
    # 现金流量表在本文里没有对应段落（正文只写了资产端/应收类/负债端）→ 回落章末，
    # 绝不能挂到总述段后面。
    assert overview_index not in placed


def test_heading_wins_over_a_later_paragraph_mention():
    """小节标题是权威归属：后文再次提到该词不该把表拖走。"""
    from backend.app.report_tables import _anchor_position

    parsed = content_blocks(
        "### 1. 评级结论\n\n主体级别AA+。\n\n"
        "### 2. 评级关注因素\n\n关注资本支出压力。\n\n"
        "### 3. 使用边界\n\n本节的评级结论仅作参考。\n"
    )
    position = _anchor_position(parsed, ("评级结论",))
    # 落在第 1 节（标题 0、段落 1），而不是第 3 节那句"本节的评级结论仅作参考"
    assert position == 1


def test_unknown_tables_fall_back_to_chapter_end():
    parsed = content_blocks(FINANCIAL)
    blocks = [_block("report-financial-mystery", "未知表")]
    placed, loose = place_blocks_by_anchor("financial", blocks, parsed)
    assert not placed
    assert [item.table_id for item in loose] == ["report-financial-mystery"]
