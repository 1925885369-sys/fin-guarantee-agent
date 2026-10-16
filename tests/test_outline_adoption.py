"""P1-1 报告采用目录：冻结、二级结构门禁与报告接口状态。

口径：
- 「配置目录」（report_outlines 当前版本）与「报告采用目录」（整份生成开始时冻结、
  完成时写入 report_outline_adoptions）是两件事；
- 单章再生只更新**该章**的小节配置；二级结构（章节集合/顺序）变化 → 422
  ``full_report_regeneration_required``；
- 报告接口给出 ``outline_outdated`` 与 ``regeneration_requirement``
  （none/chapter/full_report/unknown）；旧报告优先历史快照、无快照标 unknown。
"""
from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from backend.app.database import connect, execute, now_iso
from backend.app.main import app
from backend.app.report_delivery import write_report_section
from backend.app.report_outline_adoption import (
    adoption_state, outline_adoption_payload, regeneration_status, save_adoption, update_chapter_adoption,
)
from backend.app.report_outline_store import builtin_outline


def _create_project(client: TestClient) -> tuple[str, dict]:
    created = client.post("/api/projects", json={
        "company_name": "目录冻结测试有限公司", "amount": 500, "term_months": 12,
        "purpose": "流动资金", "data_consent": True, "project_type": "bond_guarantee",
        "bond_name": "目录冻结测试有限公司2026年公司债券",
    })
    assert created.status_code == 201, created.text
    return created.json()["id"], {"X-Project-Token": created.json()["accessToken"]}


def _purge(project_id: str) -> None:
    with connect() as conn:
        for table in ("report_outlines", "report_outline_adoptions", "report_snapshots",
                      "report_sections", "chapter_generation_attempts", "audit_events",
                      "report_claims", "report_table_snapshots"):
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def _write_section(project_id: str) -> None:
    write_report_section(
        project_id=project_id, section_key="rating", title="评级报告复核及评级差异",
        content="本章分析。", content_origin="deterministic", generation_status="deterministic",
        length_policy={"effective_min_chars": 1, "effective_max_chars": 20000},
    )


def _outline_url(project_id: str, scope: str = "project") -> str:
    return f"/api/projects/{project_id}/report-outline?scope={scope}"


def _rating_chapter(outline: dict) -> dict:
    for part in outline["parts"]:
        for chapter in part["chapters"]:
            if chapter.get("template_key") == "rating":
                return chapter
    raise AssertionError("内置目录应含 rating 章")


def test_structure_signature_tracks_second_level_structure_only() -> None:
    """结构签名只覆盖二级结构：改标题不算结构变化，换顺序/增删才算。"""
    base = outline_adoption_payload(builtin_outline())
    renamed_outline = builtin_outline()
    _rating_chapter(renamed_outline)["title"] = "外部信用评级与差异分析"
    renamed = outline_adoption_payload(renamed_outline)
    assert renamed["structure_sha256"] == base["structure_sha256"]
    assert renamed["chapters"] != base["chapters"]

    reordered_outline = builtin_outline()
    first_part = reordered_outline["parts"][0]
    first_part["chapters"] = list(reversed(first_part["chapters"]))
    assert outline_adoption_payload(reordered_outline)["structure_sha256"] != base["structure_sha256"]

    trimmed_outline = builtin_outline()
    trimmed_outline["parts"][0]["chapters"] = trimmed_outline["parts"][0]["chapters"][:-1]
    assert outline_adoption_payload(trimmed_outline)["structure_sha256"] != base["structure_sha256"]


def test_status_unknown_without_adoption_or_snapshot() -> None:
    """独存取不到 adoption 与快照时必须标 unknown，不得冒充一致。"""
    with TestClient(app, client=("127.0.0.1", 42402)) as client:
        project_id, headers = _create_project(client)
        try:
            _write_section(project_id)
            status = regeneration_status(project_id)
            assert status["outline_outdated"] is False
            assert status["regeneration_requirement"] == "unknown"
            assert status["outline_evidence"] == "none"
            quality = client.get(f"/api/projects/{project_id}/report-quality", headers=headers).json()
            assert quality["regeneration_requirement"] == "unknown"
            assert "outline_outdated" in quality
        finally:
            _purge(project_id)


def test_status_prefers_adoption_and_falls_back_to_snapshot() -> None:
    with TestClient(app, client=("127.0.0.1", 42403)) as client:
        project_id, headers = _create_project(client)
        try:
            _write_section(project_id)
            # adoption = 当前内置目录 → 一致。
            save_adoption(project_id, outline_adoption_payload(builtin_outline()), run_id="run-1")
            status = regeneration_status(project_id)
            assert status["regeneration_requirement"] == "none"
            assert status["outline_evidence"] == "adoption"
            assert adoption_state(project_id)["outline_version"] == "builtin-v1"

            # 改一章标题（结构不变）→ chapter 级；changed 指到该章。
            renamed = builtin_outline()
            _rating_chapter(renamed)["title"] = "外部信用评级与差异分析"
            response = client.put(_outline_url(project_id), headers=headers, json={"outline": renamed})
            assert response.status_code == 200, response.text
            status = regeneration_status(project_id)
            assert status["outline_outdated"] is True
            assert status["regeneration_requirement"] == "chapter"
            assert status["outline_changed_chapters"] == ["rating"]

            # 无 adoption 时回退历史快照：快照目录 = 改名版 → 与当前一致；标 snapshot 来源。
            execute("DELETE FROM report_outline_adoptions WHERE project_id=?", (project_id,))
            execute(
                "INSERT INTO report_snapshots(id,project_id,run_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                (f"snap-{project_id}", project_id, f"run-{project_id}",
                 json.dumps({"outline": renamed}, ensure_ascii=False), now_iso()),
            )
            status = regeneration_status(project_id)
            assert status["outline_evidence"] == "snapshot"
            assert status["regeneration_requirement"] == "none"

            # 快照目录缺一章 → full_report（结构变化）。
            trimmed = builtin_outline()
            trimmed["parts"] = trimmed["parts"][:5]
            execute(
                "UPDATE report_snapshots SET payload_json=? WHERE project_id=?",
                (json.dumps({"outline": trimmed}, ensure_ascii=False), project_id),
            )
            status = regeneration_status(project_id)
            assert status["regeneration_requirement"] == "full_report"
        finally:
            _purge(project_id)


def test_regenerate_422_when_second_level_structure_changed() -> None:
    """二级结构变化：单章再生返回 422 full_report_regeneration_required。"""
    with TestClient(app, client=("127.0.0.1", 42404)) as client:
        project_id, headers = _create_project(client)
        try:
            _write_section(project_id)
            save_adoption(project_id, outline_adoption_payload(builtin_outline()), run_id="run-1")
            reordered = builtin_outline()
            reordered["parts"][0]["chapters"] = list(reversed(reordered["parts"][0]["chapters"]))
            assert client.put(_outline_url(project_id), headers=headers, json={"outline": reordered}).status_code == 200
            response = client.post(f"/api/projects/{project_id}/report/sections/rating/regenerate",
                                   headers=headers)
            assert response.status_code == 422, response.text
            assert "full_report_regeneration_required" in response.text
        finally:
            _purge(project_id)


def test_regenerate_refreshes_only_that_chapter_adoption(monkeypatch) -> None:
    """单章再生成功后，报告采用目录里只刷新该章条目。"""
    with TestClient(app, client=("127.0.0.1", 42405)) as client:
        project_id, headers = _create_project(client)
        try:
            _write_section(project_id)
            save_adoption(project_id, outline_adoption_payload(builtin_outline()), run_id="run-1")
            renamed = builtin_outline()
            _rating_chapter(renamed)["title"] = "外部信用评级与差异分析"
            assert client.put(_outline_url(project_id), headers=headers, json={"outline": renamed}).status_code == 200
            assert regeneration_status(project_id)["regeneration_requirement"] == "chapter"

            from backend.app import bond_report
            from backend.app import main as main_module

            # regenerate 里是函数内延迟导入，必须打在源模块上。
            monkeypatch.setattr(bond_report, "generate_bond_chapters", lambda *args, **kwargs: [])
            # 并发校验（Prompt/材料版本）读的是真实值：让它与上下文一致。
            real_prompt_sha = hashlib.sha256(main_module.chapter_prompt("rating", project_id).encode("utf-8")).hexdigest()
            monkeypatch.setattr(main_module, "build_chapter_context", lambda *args, **kwargs: {
                "evidence": [], "allowed_citation_ids": [], "prompt_sha256": real_prompt_sha,
                "prompt_version": "v", "materials_version": main_module._materials_fingerprint(project_id),
                "input_hash": "h", "draft": "草稿", "length_policy": {},
            })
            monkeypatch.setattr(main_module, "run_chapter_model_attempt", lambda *args, **kwargs: {
                "usage": None, "validation": {"status": "passed", "reasons": []},
                "fallback_reason": "", "fallback_stage": "none", "repair_attempted": False,
                "generation_status": "model_passed", "attempt_id": "", "content": "模型正文",
                "citation_ids": [],
            })
            response = client.post(f"/api/projects/{project_id}/report/sections/rating/regenerate",
                                   headers=headers)
            assert response.status_code == 200, response.text
            status = regeneration_status(project_id)
            assert status["regeneration_requirement"] == "none", status
            assert status["outline_outdated"] is False
        finally:
            _purge(project_id)


def test_update_chapter_adoption_keeps_other_chapters() -> None:
    """条目级更新只替换目标章；结构不一致或章不存在时拒绝更新。"""
    with TestClient(app, client=("127.0.0.1", 42406)) as client:
        project_id, headers = _create_project(client)
        try:
            _write_section(project_id)
            payload = outline_adoption_payload(builtin_outline())
            save_adoption(project_id, payload, run_id="run-1")
            before = {item["key"]: item for item in adoption_state(project_id)["chapters"]}
            assert update_chapter_adoption(project_id, "not-a-chapter") is False
            assert update_chapter_adoption(project_id, "rating") is True
            after = {item["key"]: item for item in adoption_state(project_id)["chapters"]}
            assert set(before) == set(after)
            assert after["rating"] == before["rating"]
            assert after["financial"] == before["financial"]
        finally:
            _purge(project_id)
