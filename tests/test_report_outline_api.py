"""报告版本化目录（report-outline）接口测试：spec §三/§四/§五。

覆盖读取链（builtin→global→project→回落）、保存规范化落库、422 校验、409 并发、
权限门禁、幂等删除、审计与"不改正文/不调模型"。
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.database import connect, execute, row
from backend.app.main import app
from backend.app.report_outline_store import builtin_outline


def _create_project(client: TestClient) -> tuple[str, dict]:
    created = client.post("/api/projects", json={
        "company_name": "Outline test project", "amount": 500, "term_months": 12,
        "purpose": "working capital", "data_consent": True})
    assert created.status_code == 201, created.text
    return created.json()["id"], {"X-Project-Token": created.json()["accessToken"]}


def _outline_url(project_id: str, scope: str = "project") -> str:
    return f"/api/projects/{project_id}/report-outline?scope={scope}"


def _purge_outline(scope: str, project_id: str) -> None:
    identity = project_id if scope == "project" else ""
    execute("DELETE FROM report_outlines WHERE scope=? AND project_id=?", (scope, identity))


def _purge_project(project_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM report_outlines WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM llm_attempts WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM report_sections WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM audit_events WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))


def test_report_outline_read_chain_builtin_global_project_and_fallback():
    with TestClient(app, client=("127.0.0.1", 42401)) as client:
        _purge_outline("global", "")
        project_id, headers = _create_project(client)
        try:
            body = client.get(_outline_url(project_id), headers=headers).json()
            assert body["scope"] == "project"
            assert body["source"] == "builtin" and body["effective_source"] == "builtin"
            assert body["outline_version"] == "" and body["config_sha256"] == ""
            assert body["has_project_override"] is False
            assert body["editable"] is True and body["global_editable"] is True
            assert body["outline"] == builtin_outline()

            # 保存全局覆盖
            global_outline = builtin_outline()
            global_outline["parts"][0]["chapters"][0]["title"] = "全局改名的第一章"
            saved_global = client.put(_outline_url(project_id, "global"), headers=headers,
                                      json={"outline": global_outline})
            assert saved_global.status_code == 200, saved_global.text
            g = saved_global.json()
            assert g["source"] == "global"
            assert g["outline_version"] and g["config_sha256"]
            assert g["outline"]["parts"][0]["chapters"][0]["title"] == "全局改名的第一章"
            # 保存响应与随后 GET 的并发基准一致
            again = client.get(_outline_url(project_id, "global"), headers=headers).json()
            assert again["outline_version"] == g["outline_version"]
            assert again["config_sha256"] == g["config_sha256"]

            # 项目 scope 读到全局覆盖，但 version/sha 仍是"项目自身覆盖"口径
            body = client.get(_outline_url(project_id), headers=headers).json()
            assert body["source"] == "global" and body["effective_source"] == "global"
            assert body["has_project_override"] is False
            assert body["outline_version"] == "" and body["config_sha256"] == ""
            assert body["outline"]["parts"][0]["chapters"][0]["title"] == "全局改名的第一章"

            # 项目覆盖优先于全局
            project_outline = builtin_outline()
            project_outline["parts"][0]["chapters"][0]["title"] = "项目改名的第一章"
            saved = client.put(_outline_url(project_id), headers=headers, json={"outline": project_outline})
            assert saved.status_code == 200, saved.text
            assert saved.json()["source"] == "project"
            body = client.get(_outline_url(project_id), headers=headers).json()
            assert body["source"] == "project" and body["has_project_override"] is True
            assert body["outline_version"] != "" and body["config_sha256"] != ""
            assert body["outline"]["parts"][0]["chapters"][0]["title"] == "项目改名的第一章"

            # 删项目覆盖 → 回落全局
            deleted = client.delete(_outline_url(project_id), headers=headers)
            assert deleted.status_code == 200
            body = deleted.json()
            assert body["source"] == "global" and body["has_project_override"] is False
            assert body["outline_version"] == "" and body["config_sha256"] == ""
            assert body["outline"]["parts"][0]["chapters"][0]["title"] == "全局改名的第一章"

            # 删全局覆盖 → 回落 builtin
            assert client.delete(_outline_url(project_id, "global"), headers=headers).status_code == 200
            body = client.get(_outline_url(project_id), headers=headers).json()
            assert body["source"] == "builtin" and body["effective_source"] == "builtin"
            assert body["outline"] == builtin_outline()
        finally:
            _purge_outline("global", "")
            _purge_project(project_id)


def test_report_outline_put_normalizes_derives_and_keeps_ids():
    with TestClient(app, client=("127.0.0.1", 42402)) as client:
        project_id, headers = _create_project(client)
        try:
            custom_id = f"chapter-{uuid.uuid4()}"
            outline = {"parts": [{"part_id": "part-1", "title": "报告摘要及审查范围", "order": 99, "chapters": [
                {"chapter_id": custom_id, "template_key": None, "title": "自定义章节", "order": 5,
                 "generation_mode": "template", "evidence_scope": "chapter_contract", "subsections": []},
                {"chapter_id": "chapter-scope", "template_key": "scope", "title": "重命名后的审查范围", "order": 2,
                 "generation_mode": "generate", "evidence_scope": "prompt_retrieval",
                 "subsections": [{"subsection_id": "sub-scope-1", "title": "审查范围",
                                  "order": 9, "prompt": "P", "enabled": False}]},
            ]}]}
            saved = client.put(_outline_url(project_id), headers=headers, json={"outline": outline})
            assert saved.status_code == 200, saved.text
            chapters = saved.json()["outline"]["parts"][0]["chapters"]
            assert [chapter["order"] for chapter in chapters] == [1, 2]
            custom, scope = chapters[0], chapters[1]

            # 内置章节：改名不换 ID；派生字段按 template_key 重写（scope 为确定性章节 → template）
            assert scope["chapter_id"] == "chapter-scope" and scope["template_key"] == "scope"
            assert scope["title"] == "重命名后的审查范围"
            assert scope["generation_mode"] == "template" and scope["evidence_scope"] == "chapter_contract"

            # 自定义章节：template_key=null，恒 generate + prompt_retrieval
            assert custom["chapter_id"] == custom_id and custom["template_key"] is None
            assert custom["generation_mode"] == "generate" and custom["evidence_scope"] == "prompt_retrieval"

            # 小节：order 重排、其余字段保留
            sub = scope["subsections"][0]
            assert sub["order"] == 1 and sub["subsection_id"] == "sub-scope-1"
            assert sub["prompt"] == "P" and sub["enabled"] is False

            # 落库内容与响应同构（GET 一致）
            listed = client.get(_outline_url(project_id), headers=headers).json()
            assert listed["outline"] == saved.json()["outline"]
        finally:
            _purge_project(project_id)


def test_report_outline_put_validation_returns_422():
    with TestClient(app, client=("127.0.0.1", 42403)) as client:
        project_id, headers = _create_project(client)
        try:
            empty = {"outline": {"parts": [{"part_id": "part-1", "title": "p", "chapters": []}]}}
            response = client.put(_outline_url(project_id), headers=headers, json=empty)
            assert response.status_code == 422
            assert "至少需要 1 个二级章节" in response.json()["detail"]

            duplicate = {"outline": {"parts": [{"part_id": "part-1", "title": "p", "chapters": [
                {"chapter_id": "chapter-scope", "template_key": "scope", "title": "A", "subsections": []},
                {"chapter_id": "chapter-scope", "template_key": "scope", "title": "B", "subsections": []},
            ]}]}}
            response = client.put(_outline_url(project_id), headers=headers, json=duplicate)
            assert response.status_code == 422
            assert "章节 ID 全局重复" in response.json()["detail"]

            bad_key = {"outline": {"parts": [{"part_id": "part-1", "title": "p", "chapters": [
                {"chapter_id": "chapter-unknown", "template_key": "unknown", "title": "A", "subsections": []},
            ]}]}}
            response = client.put(_outline_url(project_id), headers=headers, json=bad_key)
            assert response.status_code == 422
            assert "未知的内置章节 template_key" in response.json()["detail"]

            # 缺 outline 字段 → Pydantic 422（detail 为列表）
            assert client.put(_outline_url(project_id), headers=headers,
                              json={"expected_outline_version": "x"}).status_code == 422
            # 越权/非法 scope
            assert client.put(f"/api/projects/{project_id}/report-outline?scope=bogus", headers=headers,
                              json={"outline": builtin_outline()}).status_code == 422
        finally:
            _purge_project(project_id)


def test_report_outline_concurrency_conflicts_on_version_and_sha():
    with TestClient(app, client=("127.0.0.1", 42404)) as client:
        project_id, headers = _create_project(client)
        try:
            # 现状为空也拦（未保存过却声称有一版）
            ghost = client.put(_outline_url(project_id), headers=headers,
                               json={"outline": builtin_outline(), "expected_outline_version": "ghost"})
            assert ghost.status_code == 409
            assert "已被其他会话修改" in ghost.json()["detail"]

            first = client.put(_outline_url(project_id), headers=headers, json={"outline": builtin_outline()})
            assert first.status_code == 200, first.text
            version = first.json()["outline_version"]

            changed = builtin_outline()
            changed["parts"][0]["chapters"][0]["title"] = "改A"

            stale_version = client.put(_outline_url(project_id), headers=headers,
                                       json={"outline": changed, "expected_outline_version": "0" * 32})
            assert stale_version.status_code == 409
            stale_sha = client.put(_outline_url(project_id), headers=headers,
                                   json={"outline": changed, "expected_outline_sha256": "0" * 64})
            assert stale_sha.status_code == 409

            ok = client.put(_outline_url(project_id), headers=headers,
                            json={"outline": changed, "expected_outline_version": version})
            assert ok.status_code == 200, ok.text
            assert ok.json()["outline"]["parts"][0]["chapters"][0]["title"] == "改A"
            assert ok.json()["outline_version"] != version, "每次保存换新版本"
        finally:
            _purge_project(project_id)


def test_report_outline_write_requires_local_and_non_public(monkeypatch):
    with TestClient(app, client=("127.0.0.1", 42405)) as client:
        _purge_outline("global", "")
        project_id, headers = _create_project(client)
        try:
            from backend.app import main as main_module

            class _SettingsWithPublicDemo:
                def __init__(self, value: bool) -> None:
                    self._value = value

                def __getattr__(self, name: str):
                    return self._value if name == "public_demo" else getattr(settings, name)

            # 公开演示环境：全局写被拒；GET 仍可读且 global_editable=False
            monkeypatch.setattr(main_module, "settings", _SettingsWithPublicDemo(True))
            denied = client.put(_outline_url(project_id, "global"), headers=headers,
                                json={"outline": builtin_outline()})
            assert denied.status_code == 403
            assert "公开演示" in denied.json()["detail"]
            assert client.delete(_outline_url(project_id, "global"), headers=headers).status_code == 403
            listed = client.get(_outline_url(project_id, "global"), headers=headers).json()
            assert listed["global_editable"] is False
            monkeypatch.setattr(main_module, "settings", _SettingsWithPublicDemo(False))

            # 带代理转发头 → 无法确认本机直连，全局写被拒
            forwarded = client.put(_outline_url(project_id, "global"),
                                   headers={**headers, "X-Forwarded-For": "10.0.0.1"},
                                   json={"outline": builtin_outline()})
            assert forwarded.status_code == 403
            assert "代理转发头" in forwarded.json()["detail"]
            assert row("SELECT COUNT(*) c FROM report_outlines WHERE scope='global'")["c"] == 0
        finally:
            _purge_outline("global", "")
            _purge_project(project_id)


def test_report_outline_save_delete_isolated_and_audited():
    with TestClient(app, client=("127.0.0.1", 42406)) as client:
        project_id, headers = _create_project(client)
        try:
            saved = client.put(_outline_url(project_id), headers=headers, json={"outline": builtin_outline()})
            assert saved.status_code == 200, saved.text

            # 保存不调模型、不改报告正文
            assert row("SELECT COUNT(*) c FROM llm_attempts WHERE project_id=?", (project_id,))["c"] == 0
            assert row("SELECT COUNT(*) c FROM report_sections WHERE project_id=?", (project_id,))["c"] == 0

            # 幂等删除：删两次都成功
            assert client.delete(_outline_url(project_id), headers=headers).status_code == 200
            assert client.delete(_outline_url(project_id), headers=headers).status_code == 200
            assert row("SELECT COUNT(*) c FROM report_outlines WHERE scope='project' AND project_id=?",
                       (project_id,))["c"] == 0

            # 审计留痕
            with connect() as conn:
                actions = {dict(record)["action"] for record in conn.execute(
                    "SELECT action FROM audit_events WHERE project_id=?", (project_id,)).fetchall()}
            assert {"report_outline_update", "report_outline_reset"} <= actions
        finally:
            _purge_outline("global", "")
            _purge_project(project_id)
