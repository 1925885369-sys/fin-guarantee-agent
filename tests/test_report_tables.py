"""报告表格（Phase 5）：系统生成表格、模型只写分析。

两条职责：

1. **表格由代码生成**，不依赖模型是否愿意输出 Markdown 表格，也不受模型回退影响；
2. **模型只写分析**——不得逐行复述、不得改动数值、不得自行输出表格。

第 2 条的职责约束走**模型输入**而不是章节 prompt：prompt 的 ``rules`` 是可编辑字段，
用户在界面上存过覆盖后 YAML 的新规则不会回溯更新已存文本（实测财务章的全局默认里
仍是旧的 4 条规则），所以职责必须随上下文投递才一定到得了模型。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.chapter_guard import build_chapter_context, count_report_chars, validate_chapter_output
from backend.app.main import app
from backend.app.report_tables import (
    SYSTEM_TABLE_HEADING, SYSTEM_TABLE_ROLE, financial_table_blocks, system_table_markdown,
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _context(client, project_id: str, chapter_key: str) -> dict:
    client.get(f"/api/projects/{project_id}/bond-summary")
    return build_chapter_context(project_id, chapter_key, "确定性草稿。")


def test_financial_chapter_gets_system_tables(client) -> None:
    block = _context(client, "demo-bond-huachen", "financial")["system_table_block"]
    assert block.startswith(SYSTEM_TABLE_HEADING)
    assert "三年主要资产负债项目表" in block
    assert "应收账款规模及明细" in block
    assert "| 科目 |" in block


def test_system_table_values_match_the_model(client) -> None:
    """表内数值必须来自已核验事实，不能是抽取噪声。"""
    block = _context(client, "demo-bond-huachen", "financial")["system_table_block"]
    assert "| 资产总计 | 72,000 | 65,520 | 59,040 |" in block
    assert "| 负债合计 | 50,400 | 45,864 | 41,328 |" in block
    # 实测过的缺陷：表格一行多个裸数值被拼成一个 15 位数，写进了报告。
    assert "504,004,586,441,328" not in block


def test_non_table_chapters_have_no_system_table(client) -> None:
    """首批只在财务章输出表格——扩展时要在这里同步登记。"""
    context = _context(client, "demo-bond-huachen", "guarantee")
    assert context["system_table_block"] == ""
    assert context["system_table_role"] == ""


def test_chapter_duties_travel_with_the_context(client) -> None:
    """职责约束必须出现在模型输入里，而不是只在可被覆盖的 prompt 中。"""
    context = _context(client, "demo-bond-huachen", "financial")
    assert context["system_table_role"] == SYSTEM_TABLE_ROLE
    assert "不得逐行复述" in context["system_table_role"]
    assert "不得自行输出表格" in context["system_table_role"]


def test_system_table_is_a_required_disclosure(client) -> None:
    """系统表格与冲突块同类：生成时冻结、必须逐字保留、不计入篇幅。"""
    context = _context(client, "demo-bond-huachen", "financial")
    assert context["system_table_block"] in context["required_disclosures"]
    content = f"本章分析正文。\n\n{context['system_table_block']}"
    assert count_report_chars(
        content, system_table_block=context["system_table_block"],
    ) == count_report_chars("本章分析正文。")


def test_model_authored_table_is_rejected(client) -> None:
    """模型自行画表属于越权，且会与系统表形成重复披露。"""
    context = _context(client, "demo-bond-huachen", "financial")
    content = (
        f"资产负债结构变化显著。\n\n{context['system_table_block']}\n\n"
        "| 科目 | 2025 |\n| --- | --- |\n| 资产总计 | 72,000 |"
    )
    reasons = validate_chapter_output(content, [], context)["reasons"]
    assert any("自行输出了表格" in reason for reason in reasons), reasons


def test_system_table_itself_is_not_flagged_as_model_output(client) -> None:
    """判定前先摘掉系统块，否则会把系统表误判成模型输出。"""
    context = _context(client, "demo-bond-huachen", "financial")
    content = f"资产负债结构变化显著，详见下表。\n\n{context['system_table_block']}"
    reasons = validate_chapter_output(content, [], context)["reasons"]
    assert not any("自行输出了表格" in reason for reason in reasons), reasons


def test_missing_system_table_is_rejected(client) -> None:
    context = _context(client, "demo-bond-huachen", "financial")
    reasons = validate_chapter_output("只有分析正文，没有表格。", [], context)["reasons"]
    assert any("系统整理数据表未原样保留" in reason for reason in reasons), reasons


def test_conflict_chapter_emits_a_comparison_table(client) -> None:
    """有来源冲突时必须输出冲突对照表，且不设「采用值」列。"""
    block = system_table_markdown("demo-bond-conflict", "financial", subject="瀚川绿色装备有限公司")
    assert "冲突对照表" in block
    assert "多来源披露并列，未择一采信" in block
    assert "采用值" not in block
    # 成品里不得出现内部字段键与内部枚举。
    assert "bond_issue_amount" not in block
    assert "standalone" not in block


def test_financial_table_blocks_are_validated() -> None:
    """每张表都要通过 TableBlock 校验——有不可追溯的格就不能算通过。"""
    for block in financial_table_blocks("demo-bond-huachen", subject="华辰精密制造有限公司"):
        assert block.validation_status == "passed", (block.title, block.validation_summary)
