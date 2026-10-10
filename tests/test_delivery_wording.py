"""成品用词规范化与校验（Phase 5.3）。

规格见 ``PLAN3.md`` §3：报告成品里不出现原材料种类名，也不出现内部标识；
**规范化必须在保存前完成、再做校验与哈希**；工作台证据详情仍显示原件名称。
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from backend.app.database import row
from backend.app.delivery_wording import (
    FORBIDDEN_TERMS, document_series, material_numbers, normalize_report_text, wording_violations,
)
from backend.app.main import app
from backend.app.report_delivery import delivery_blockers, write_report_section


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_alias_table_maps_material_names_to_product_wording() -> None:
    numbers = {"发行文件": "资料01", "外部信用评价文件": "资料02"}
    assert normalize_report_text("募集说明书", numbers) == "发行文件〔资料01〕"
    assert normalize_report_text("信用评级报告", numbers) == "外部信用评价文件〔资料02〕"
    assert normalize_report_text("评级报告", numbers) == "外部信用评价文件〔资料02〕"


def test_specific_alias_wins_over_the_shorter_one() -> None:
    """「评级报告复核及评级差异」必须先于「评级报告」替换，否则会留下残句。"""
    out = normalize_report_text("评级报告复核及评级差异", {})
    assert out == "外部信用评级与差异分析"
    assert "外部信用评价文件" not in out


def test_normalization_is_idempotent() -> None:
    """重复规范化不得叠加编号后缀——正文与哈希要求结果稳定。"""
    numbers = {"发行文件": "资料01"}
    once = normalize_report_text("据募集说明书披露", numbers)
    assert normalize_report_text(once, numbers) == once


def test_normalization_without_numbers_still_removes_forbidden_terms() -> None:
    assert normalize_report_text("募集说明书", {}) == "发行文件"


def test_violations_detect_terms_uuids_and_field_keys() -> None:
    text = (
        "据募集说明书披露，bond_issue_amount 为 20000，"
        "来源 0e8d2e49-71b9-42ac-ba31-9669570ddb58。"
    )
    kinds = {item["kind"] for item in wording_violations(text)}
    assert kinds == {"forbidden_term", "internal_uuid", "internal_field_key"}


def test_clean_text_has_no_violations() -> None:
    assert wording_violations("发行文件〔资料01〕第 9 页披露本期发行规模 20,000 万元。") == []


def test_document_series_is_stable_and_ordered(client) -> None:
    """资料编号写进成品并被引用，不能随查询顺序漂移。"""
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    first = document_series("demo-bond-huachen")
    assert document_series("demo-bond-huachen") == first
    assert sorted(first.values()) == ["资料01", "资料02"]
    numbers = material_numbers("demo-bond-huachen")
    assert numbers == {"发行文件": "资料01", "外部信用评价文件": "资料02"}


def test_write_normalizes_before_hashing(client) -> None:
    """正文与冻结披露一起规范化，且哈希在规范化之后计算。

    冻结披露要参与「逐字保留」比对：只改正文不改它，校验会立刻失败。
    """
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    block = "### 来源\n据募集说明书与信用评级报告披露。"
    content = f"本章分析。\n\n{block}"
    write_report_section(
        project_id="demo-bond-huachen", section_key="wording_probe", title="用词探针",
        content=content, required_disclosures=[block], deterministic_draft=content,
        content_origin="deterministic",
    )
    stored = row(
        "SELECT content,required_disclosures,content_sha256 FROM report_sections "
        "WHERE project_id=? AND section_key=?",
        ("demo-bond-huachen", "wording_probe"),
    )
    assert "募集说明书" not in stored["content"]
    assert "发行文件〔资料01〕" in stored["content"]
    # 冻结披露必须与正文同样规范化，否则「逐字保留」比对会判失败。
    assert "募集说明书" not in stored["required_disclosures"]
    assert stored["content_sha256"] == hashlib.sha256(stored["content"].encode("utf-8")).hexdigest()


def test_delivery_blockers_flag_wording_violations(client) -> None:
    """禁用词与内部标识出现时，最终导出被阻止。"""
    client.get("/api/projects/demo-bond-huachen/bond-summary")
    polluted = "据募集说明书披露，字段 bond_name 指向 0e8d2e49-71b9-42ac-ba31-9669570ddb58。"
    write_report_section(
        project_id="demo-bond-huachen", section_key="wording_probe", title="用词探针",
        content=polluted, content_origin="deterministic",
    )
    try:
        # 直接写入会被规范化，所以这里绕过写入入口，模拟历史遗留的未规范化正文。
        from backend.app.database import execute
        execute(
            "UPDATE report_sections SET content=? WHERE project_id=? AND section_key=?",
            (polluted, "demo-bond-huachen", "wording_probe"),
        )
        blockers = delivery_blockers("demo-bond-huachen")
        assert any("成品用词不合规" in item for item in blockers), blockers
    finally:
        from backend.app.database import execute
        execute(
            "DELETE FROM report_sections WHERE project_id=? AND section_key=?",
            ("demo-bond-huachen", "wording_probe"),
        )


def test_reference_numbers_are_not_treated_as_data() -> None:
    """「发行文件〔资料01〕第 29 页」里的 01 与 29 不是被主张的数据值。

    规范化会把资料编号写成 ``〔资料01〕``，而对正文取数字的检查若先不剔除引用定位，
    就会把 ``01`` 当成一个新数字去证据里找——找不到，于是**正常引用被判成
    「无证据声明」**，交付哈希也随之失配。实测踩过：写入后重新校验，
    ``validated_content_sha256`` 再也绑不回正文。
    """
    from backend.app.delivery_wording import strip_references
    from backend.app.p0 import _numbers
    from backend.app.workflow import _numbers_supported

    assert strip_references("据发行文件〔资料01〕第 29 页披露") == "据发行文件披露"
    assert _numbers("据发行文件〔资料01〕第 29 页披露") == []
    assert _numbers("发行文件〔资料01〕披露营业收入 12,000 万元") == [12000.0]
    assert _numbers_supported(
        "据发行文件〔资料01〕第 9 页披露营业收入 100 万元。",
        "营业收入100万元",
        [{"quote": "营业收入100万元"}],
    ) is True


def test_conflict_block_is_product_grade(client) -> None:
    """冲突块既进上下文又被逐字保留在成品里，因此不能带内部标识。"""
    from backend.app.database import rows

    client.get("/api/projects/demo-bond-huachen/bond-summary")
    content = rows(
        "SELECT content FROM report_sections WHERE project_id=? AND section_key=?",
        ("demo-bond-huachen", "bond_overview"),
    )
    if not content:
        pytest.skip("该演示项目尚未生成报告，跳过成品抽查")
    body = content[0]["content"]
    assert wording_violations(body) == [], wording_violations(body)
    assert "断言 " not in body
    assert "字段 " not in body
