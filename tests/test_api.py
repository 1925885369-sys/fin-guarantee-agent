import hashlib
import time
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from docx import Document
from pydantic import ValidationError

from backend.app.config import settings
from backend.app.chapter_prompts import (
    BOND_CHAPTER_KEYS, BOND_CHAPTER_PART, BOND_CHAPTER_PARTS, BOND_CHAPTER_TITLES,
    chapter_default_prompt, chapter_prompt, load_chapter_config, recompile_structured_prompts,
)
from backend.app.chapter_guard import count_report_chars
from backend.app.database import connect, execute, now_iso, row
from backend.app.main import app
from backend.app.schemas import ProjectCreate


def wait_run(client: TestClient, run_id: str, headers=None):
    # 截止时间而非固定轮询次数：本机冷启动（jieba/检索模型首载）时演示运行可达 40–90 秒，
    # 原先 600×0.05s≈30s 的预算会把"还没跑完"误判成 `run timed out`。
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}", headers=headers)
        assert response.status_code == 200
        run = response.json()
        if run["status"] in {"completed", "failed", "cancelled", "model_required"}:
            return run
        time.sleep(0.05)
    raise AssertionError("run timed out")


def test_demo_workflow_and_exports():
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["policy"] == "bond-v1"
        assert len(client.get("/api/projects").json()) == 6
        inventory = client.get("/api/projects/demo-bond-huachen/document-inventory").json()
        assert {"documents", "gaps", "supplementary_gaps", "readiness", "parse_quality"} <= set(inventory)
        assert inventory["complete_due_diligence_ready"] is True
        assert inventory["gaps"] == []
        assert inventory["supplementary_gaps"]
        started = client.post("/api/projects/demo-bond-huachen/runs", json={"focus": "债券担保尽调"})
        run = wait_run(client, started.json()["id"])
        assert run["status"] == "completed"
        assert run["progress"] == 100
        latest = client.get("/api/projects/demo-bond-huachen/runs/latest").json()
        assert latest["run"]["id"] == run["id"]
        assert len(latest["nodes"]) == 11
        nodes = client.get(f"/api/runs/{run['id']}/nodes").json()
        assert len(nodes) == 11
        # 交付一致性修复后：存在无证据主张时 verify 如实降级为 warning（保留草稿待人工复核），
        # 不再通过替换/删除正文来伪造"全部完成"。
        node_status = {item["node_key"]: item["status"] for item in nodes}
        assert all(status == "completed" for key, status in node_status.items() if key != "verify")
        assert node_status["verify"] in {"completed", "warning"}
        artifacts = client.get(f"/api/runs/{run['id']}/artifacts").json()
        assert len(artifacts) == 11
        assert all(item["input_hash"] and item["artifact_type"] == item["node_key"] for item in artifacts)
        checks = client.get("/api/projects/demo-bond-huachen/cross-checks").json()
        risks = client.get("/api/projects/demo-bond-huachen/risks").json()
        report = client.get("/api/projects/demo-bond-huachen/report").json()
        assert len(checks) == 19 and risks and len(report) == 17
        assert {"bond_overview", "financial", "cashflow", "debt", "rating", "guarantee", "counter_guarantee", "evidence"} <= {item["section_key"] for item in report}
        report_text = "\n".join(item["content"] for item in report)
        assert "评分维度" not in report_text
        assert inventory["supplementary_gaps"]
        assert any(marker in report_text for marker in ("材料不足", "材料缺口", "无法判断", "无法核验"))
        assert all(term not in report_text for term in (
            "建议承保", "建议拒绝", "核定担保额度", "核定担保费率", "本报告构成投资建议",
        ))
        assert "monitoring" in {item["section_key"] for item in report}
        assert "贷后管理要求" not in report_text and "授信条件建议" not in report_text
        assert "建议暂缓担保" not in report_text and "/5" not in report_text
        assert {item["status"] for item in checks} <= {"passed", "warning", "insufficient"}
        citation = client.get(f"/api/citations/{report[0]['citation_ids'][0]}")
        assert citation.status_code == 200 and citation.json()["locator"]
        markdown = client.get("/api/projects/demo-bond-huachen/export?format=md")
        assert markdown.status_code == 200
        # 论文式标题层级：一级 一、二级 （一）三级 1.，且章与节不同级。
        assert "## 一、" in markdown.text and "### （一）" in markdown.text and "#### 1. " in markdown.text
        exported = client.get("/api/projects/demo-bond-huachen/export?format=docx")
        assert exported.status_code == 200 and len(exported.content) > 10000
        document = Document(BytesIO(exported.content))
        # 附录标题带论文式一级编号（如“七、附录：证据索引”）。
        assert any(paragraph.text.endswith("附录：证据索引") for paragraph in document.paragraphs)
        heading_text = {style: [item.text for item in document.paragraphs if item.style.name == style]
                        for style in ("Heading 1", "Heading 2", "Heading 3")}
        assert any(text.startswith("一、") for text in heading_text["Heading 1"])
        assert any(text.startswith("（一）") for text in heading_text["Heading 2"])
        assert any(text.startswith("1. ") for text in heading_text["Heading 3"])
        assert len(document.tables) >= 2
        # 引用块（Quote）只用于冲突披露/来源边界这类系统块，不该"满屏都是"。
        # 用**比例**而不是固定阈值：导出改为按 TableBlock 快照生成原生表格后，
        # 系统块会随章节数正常增长，原先固定 `< 20` 会把正常增长误判成失败。
        quote_count = sum(paragraph.style.name == "Quote" for paragraph in document.paragraphs)
        assert quote_count < max(20, len(document.paragraphs) // 4), (quote_count, len(document.paragraphs))


def test_private_project_requires_token_and_consent(monkeypatch):
    monkeypatch.setattr("backend.app.workflow.is_configured", lambda: False)
    with TestClient(app) as client:
        payload = {"company_name": "虚构测试企业", "amount": 500, "term_months": 12, "purpose": "补充流动资金", "data_consent": False}
        assert client.post("/api/projects", json=payload).status_code == 400
        payload["data_consent"] = True
        created = client.post("/api/projects", json=payload)
        assert created.status_code == 201
        project_id, token = created.json()["id"], created.json()["accessToken"]
        headers = {"X-Project-Token": token}
        assert client.get(f"/api/projects/{project_id}").status_code == 403
        assert client.get(f"/api/projects/{project_id}", headers=headers).status_code == 200
        started = client.post(f"/api/projects/{project_id}/runs", json={"focus": "债券担保尽调"}, headers=headers)
        assert started.status_code == 409
        assert started.json()["detail"]["code"] == "core_materials_not_ready"


def test_project_creation_accepts_twenty_billion_yuan_bond_scale():
    payload = {
        "company_name": "中邮债券建项规模测试",
        "amount": 200000,
        "term_months": 36,
        "purpose": "补充流动资金",
        "data_consent": True,
    }
    with TestClient(app) as client:
        created = client.post("/api/projects", json=payload)
        assert created.status_code == 201, created.text
        project_id, token = created.json()["id"], created.json()["accessToken"]
        project = client.get(f"/api/projects/{project_id}", headers={"X-Project-Token": token})
        assert project.status_code == 200
        assert project.json()["amount"] == 200000

        for amount in (0, -1):
            invalid = client.post("/api/projects", json={**payload, "amount": amount})
            assert invalid.status_code == 422
            assert any(item["loc"][-1] == "amount" for item in invalid.json()["detail"])

    for amount in (float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            ProjectCreate.model_validate({**payload, "amount": amount})


def test_project_report_prompt_overrides_are_persisted_and_reset():
    project_id = ""
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "company_name": "Report prompt test project", "amount": 500, "term_months": 12,
            "purpose": "working capital", "data_consent": True,
        })
        assert created.status_code == 201
        project_id, token = created.json()["id"], created.json()["accessToken"]
        headers = {"X-Project-Token": token}

        try:
            listed = client.get(f"/api/projects/{project_id}/report-prompts", headers=headers)
            assert listed.status_code == 200
            prompts = listed.json()
            assert [item["chapter_key"] for item in prompts] == list(BOND_CHAPTER_KEYS)
            # 未生成报告的项目也必须返回中文标题和报告分部（不再回退英文 key）。
            for item in prompts:
                assert item["title"] == BOND_CHAPTER_TITLES[item["chapter_key"]]
                assert item["part"] == BOND_CHAPTER_PART[item["chapter_key"]]
            assert {item["part"] for item in prompts} == {part for part, _ in BOND_CHAPTER_PARTS}
            financial = next(item for item in prompts if item["chapter_key"] == "financial")
            assert financial["llm"] is True
            assert financial["overridden"] is False
            original_effective_prompt = financial["prompt"]
            assert original_effective_prompt == chapter_prompt("financial", project_id)
            assert financial["source"] in {"system", "global"}
            assert financial["editable_config"]["role"]
            assert financial["constraints"]["forbidden"]

            custom_prompt = "Only use the approved evidence list and state material gaps explicitly."
            saved = client.patch(
                f"/api/projects/{project_id}/report-prompts/financial",
                headers=headers,
                json={"prompt": custom_prompt},
            )
            assert saved.status_code == 200
            assert saved.json()["title"] == BOND_CHAPTER_TITLES["financial"]
            assert saved.json()["part"] == BOND_CHAPTER_PART["financial"]
            assert saved.json()["overridden"] is True
            assert saved.json()["migration_mode"] == "preserved_legacy"
            assert saved.json()["editable_config"]["additional_requirements"] == custom_prompt
            assert custom_prompt in saved.json()["prompt"]
            assert "不可修改的风控约束" in saved.json()["prompt"]
            assert chapter_prompt("financial", project_id) == saved.json()["prompt"]

            structured = saved.json()["editable_config"]
            structured["goal"] = "基于已授权证据归纳财务结构，并披露资料边界。"
            structured["rules"] = ["不得将评级意见表述为经独立核验的事实。"]
            structured["narrative_contract"]["min_paragraphs"] = 2
            preview = client.post(
                f"/api/projects/{project_id}/report-prompts/financial/preview",
                headers=headers,
                json={"config": structured},
            )
            assert preview.status_code == 200
            assert structured["goal"] in preview.json()["prompt"]
            structured_saved = client.patch(
                f"/api/projects/{project_id}/report-prompts/financial",
                headers=headers,
                json={"config": structured},
            )
            assert structured_saved.status_code == 200
            assert structured_saved.json()["migration_mode"] == "structured"
            assert structured_saved.json()["editable_config"]["goal"] == structured["goal"]

            # 篇幅是写进 Prompt 的写作目标，就是用户设置的区间本身。
            # 系统已无独立字符硬顶，因此 3000 会被原样写入，不再下钳到 2600。
            structured["narrative_contract"]["preferred_min_chars"] = 1000
            structured["narrative_contract"]["preferred_max_chars"] = 3000
            sized = client.patch(
                f"/api/projects/{project_id}/report-prompts/financial",
                headers=headers,
                json={"config": structured},
            )
            assert sized.status_code == 200
            assert "max_output_chars" not in load_chapter_config("financial")["output_contract"]
            assert "正文篇幅目标为1000至3000字。" in sized.json()["prompt"]

            # 目标最小 > 目标最大 必须在保存时就被拒绝。
            invalid = dict(structured)
            invalid["narrative_contract"] = dict(
                structured["narrative_contract"], preferred_min_chars=4000, preferred_max_chars=1000,
            )
            assert client.patch(
                f"/api/projects/{project_id}/report-prompts/financial",
                headers=headers,
                json={"config": invalid},
            ).status_code == 422

            # 生成读的是存库的 Prompt 文本，所以编译器升级后要靠重编译迁移把
            # 已有结构化覆盖带上新措辞；legacy 文本是人工撰写的，不参与重编译。
            execute(
                "UPDATE report_prompt_overrides SET prompt='old-compiler-output' "
                "WHERE project_id=? AND chapter_key='financial'",
                (project_id,),
            )
            assert recompile_structured_prompts() >= 1
            assert chapter_prompt("financial", project_id) == sized.json()["prompt"]
            assert client.patch(
                f"/api/projects/{project_id}/report-prompts/scope",
                headers=headers,
                json={"config": structured},
            ).status_code == 422

            refreshed = client.get(f"/api/projects/{project_id}/report-prompts", headers=headers).json()
            financial = next(item for item in refreshed if item["chapter_key"] == "financial")
            assert financial["editable_config"]["goal"] == structured["goal"]
            assert financial["overridden"] is True
            assert row(
                "SELECT id FROM audit_events WHERE project_id=? AND action='report_prompt_update'",
                (project_id,),
            )

            unknown = client.patch(
                f"/api/projects/{project_id}/report-prompts/not-a-chapter",
                headers=headers,
                json={"prompt": custom_prompt},
            )
            assert unknown.status_code == 404

            reset = client.delete(f"/api/projects/{project_id}/report-prompts/financial", headers=headers)
            assert reset.status_code == 200
            assert reset.json()["overridden"] is False
            assert reset.json()["prompt"] == original_effective_prompt
            assert chapter_prompt("financial", project_id) == original_effective_prompt
            assert row(
                "SELECT id FROM audit_events WHERE project_id=? AND action='report_prompt_reset'",
                (project_id,),
            )
        finally:
            _delete_test_project(project_id)


def test_failed_document_can_be_reuploaded_and_reparsed(tmp_path, monkeypatch):
    from backend.app.parsers import parse_document as real_parse_document

    material = tmp_path / "prospectus.docx"
    document = Document()
    document.add_heading("Bond prospectus", level=1)
    document.add_paragraph("Issuance terms, repayment arrangements and guarantee scope.")
    document.save(material)

    attempts = 0

    def parse_after_initial_failure(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("temporary parser limit")
        return real_parse_document(path)

    monkeypatch.setattr("backend.app.main.parse_document", parse_after_initial_failure)
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "company_name": "Upload retry project", "amount": 500, "term_months": 12,
            "purpose": "working capital", "data_consent": True,
        })
        project_id, token = created.json()["id"], created.json()["accessToken"]
        headers = {"X-Project-Token": token}

        def upload():
            with material.open("rb") as stream:
                return client.post(
                    f"/api/projects/{project_id}/documents", headers=headers,
                    files={"file": (material.name, stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                )

        first = upload()
        assert first.status_code == 201
        assert first.json()["parseStatus"] == "failed"

        second = upload()
        assert second.status_code == 201
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["parseStatus"] == "parsed"

        assert upload().status_code == 409
        inventory = client.get(f"/api/projects/{project_id}/document-inventory", headers=headers).json()
        assert len(inventory["documents"]) == 1
        assert inventory["documents"][0]["locator_count"] > 0


def test_human_fact_review_uses_project_authorization():
    with TestClient(app) as client:
        fact = next(
            item for item in client.get("/api/projects/demo-bond-huachen/facts").json()
            if item["value"] is not None and len(item["period"]) >= 4
        )
        original_value = fact["value"]
        updated = client.patch(f"/api/projects/demo-bond-huachen/facts/{fact['id']}", json={
            "period": fact["period"], "value": original_value, "unit": fact["unit"], "text_value": fact["text_value"],
        })
        assert updated.status_code == 200
        refreshed = next(item for item in client.get("/api/projects/demo-bond-huachen/facts").json() if item["id"] == fact["id"])
        assert refreshed["review_status"] == "human_verified"
        group = next(item for item in client.get("/api/projects/demo-bond-huachen/fact-groups").json() if item["selected_assertion_id"] == fact["id"])
        assert group["status"] == "confirmed" and group["value"] == original_value


def test_private_project_upload_model_required_and_export_audit(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.workflow.is_configured", lambda: False)
    materials = []
    for filename, heading, paragraph in (
        ("募集说明书.docx", "虚构公司债券募集说明书", "本期发行规模500万元，期限一年，募集资金用于补充营运资金。"),
        ("信用评级报告.docx", "虚构公司信用评级报告", "评级机构对发行人主体和本期债项开展信用分析。"),
    ):
        material = tmp_path / filename
        document = Document()
        document.add_heading(heading, level=1)
        document.add_paragraph(paragraph)
        document.save(material)
        materials.append(material)

    project_id = ""
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "company_name": "上传闭环测试企业", "amount": 500, "term_months": 12,
            "purpose": "补充流动资金", "data_consent": True,
        })
        assert created.status_code == 201
        project_id, token = created.json()["id"], created.json()["accessToken"]
        headers = {"X-Project-Token": token}
        try:
            with materials[0].open("rb") as stream:
                files = {"file": (materials[0].name, stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
                assert client.post(f"/api/projects/{project_id}/documents", files=files).status_code == 403
            for material in materials:
                with material.open("rb") as stream:
                    files = {"file": (material.name, stream, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
                    uploaded = client.post(f"/api/projects/{project_id}/documents", files=files, headers=headers)
                assert uploaded.status_code == 201
                assert uploaded.json()["parseStatus"] == "parsed"

            inventory = client.get(f"/api/projects/{project_id}/document-inventory", headers=headers).json()
            assert len(inventory["documents"]) == 2
            assert {item["filename"] for item in inventory["documents"]} == {item.name for item in materials}
            assert all(item["locator_count"] >= 2 for item in inventory["documents"])
            assert inventory["core_materials_ready"] is True
            assert row("SELECT COUNT(*) count FROM sources WHERE project_id=?", (project_id,))["count"] >= 4

            started = client.post(
                f"/api/projects/{project_id}/runs", json={"focus": "全面尽调"}, headers=headers,
            )
            assert started.status_code == 202
            run = wait_run(client, started.json()["id"], headers)
            assert run["status"] == "model_required"
            assert "配置模型" in run["message"]

            assert client.get(f"/api/projects/{project_id}/export?format=md").status_code == 403
            exported = client.get(f"/api/projects/{project_id}/export?format=md", headers=headers)
            assert exported.status_code == 200
            assert "上传闭环测试企业" in exported.text
            assert row(
                "SELECT id FROM audit_events WHERE project_id=? AND action='export'", (project_id,),
            )
        finally:
            _delete_test_project(project_id)


def _delete_test_project(project_id: str) -> None:
    if not project_id:
        return
    tables = (
        "llm_attempts", "run_artifacts", "audit_events", "report_prompt_overrides", "evidence_edges", "report_claims", "gold_annotations",
        "candidate_findings", "review_tasks", "policy_snapshots", "financial_tie_outs", "financial_metrics",
        "project_entities", "canonical_facts", "fact_assertions", "run_nodes", "runs", "report_sections", "risks",
        "cross_checks", "facts", "citations", "sources", "documents",
    )
    with connect() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    upload_dir = settings.resolved_data_dir / "uploads" / project_id
    if upload_dir.is_dir():
        for path in upload_dir.iterdir():
            if path.is_file():
                path.unlink()
        upload_dir.rmdir()


def test_report_exposes_length_diagnosis_and_tolerates_legacy_rows():
    """篇幅字段的 API 契约：新行返回生成时的诊断，历史行按 not_checked 补算。"""
    project_id = ""
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "company_name": "Report length test project", "amount": 500, "term_months": 12,
            "purpose": "working capital", "data_consent": True,
        })
        assert created.status_code == 201
        project_id, token = created.json()["id"], created.json()["accessToken"]
        headers = {"X-Project-Token": token}
        try:
            # 不带篇幅列的插入 = 历史报告行
            execute(
                "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,review_status,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("section-length-legacy", project_id, "financial", "财务趋势与盈利质量",
                 "据募集说明书披露，材料不足。", "[]", "draft", now_iso()),
            )
            listed = client.get(f"/api/projects/{project_id}/report", headers=headers).json()
            section = next(item for item in listed if item["section_key"] == "financial")
            assert section["length_status"] == "not_checked"
            assert section["length_repair_attempted"] is False
            assert section["visible_content_chars"] == count_report_chars(section["content"])
            assert section["length_policy"]["unit"] == "visible_chars"
            assert section["length_policy"]["effective_max_chars"] > 0

            quality = client.get(f"/api/projects/{project_id}/report-quality", headers=headers).json()
            assert quality["length_quality"]["status"] == "not_checked"
            assert quality["length_quality"]["checked_sections"] == 0

            # 报告接口新增的生成模式 / Prompt 时效 / 修复诊断字段。
            # financial 是模型章节，按修订稿 §2/§3.2 使用直接撰写模式 → `generate`。
            assert section["generation_mode"] == "generate"
            assert section["prompt_outdated"] is False
            assert {"generation_status", "fallback_reason", "fallback_stage",
                    "validation_diagnostics"} <= set(section["repair"])
        finally:
            _delete_test_project(project_id)


def _insert_real_reference_project(project_id: str) -> None:
    execute(
        "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,"
        "created_at,updated_at,consent_at,data_class,evaluation_eligible) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (project_id, "token", "真实参考权限测试主体", 1000, 36, "补充流动资金", "draft",
         now_iso(), now_iso(), now_iso(), "real_reference", 0),
    )


def test_real_reference_prompt_edit_requires_local_direct_for_both_scopes():
    """真实参考项目**项目级** Prompt 仅本机直连且无代理转发头时允许保存/重置。

    ⚠️ 旧口径"全局默认一律禁止修改"已被新决定替代（`PLAN_new.md` §一 /
    `PLAN_new_p0_p1.md` P0-1 / `PLAN.md` §二 P0-A）：全局默认是**部署级**配置，
    允许非公开环境 + 本机直连 + 无转发头的请求修改，**与项目是否真实参考无关**。
    所以本用例验证的是：
    - 非环回客户端：项目级与全局级**都**拒绝；
    - 环回客户端：项目级可写；全局级**也可写**（且真实参考项目不构成额外限制）。
    """
    from uuid import uuid4

    payload = {"prompt": "仅使用已授权证据，并逐段披露材料边界，不新增主体与结论。"}

    # 1) 默认 TestClient 客户端（非环回）→ 项目级**与全局级**保存都被拒。
    with TestClient(app) as client:
        project_id = f"real-perm-{uuid4()}"
        _insert_real_reference_project(project_id)
        try:
            denied = client.patch(
                f"/api/projects/{project_id}/report-prompts/financial", json=payload,
            )
            assert denied.status_code == 403
            # 代理转发头存在 → 明确拒绝，且请求体被视为外部来源。
            forwarded = client.patch(
                f"/api/projects/{project_id}/report-prompts/financial", json=payload,
                headers={"x-forwarded-for": "203.0.113.9"},
            )
            assert forwarded.status_code == 403
            # 全局默认同样过不了"本机直连"这一关（非环回客户端）。
            assert client.patch(
                f"/api/projects/{project_id}/report-prompts/global/financial", json=payload,
            ).status_code == 403
            assert client.delete(
                f"/api/projects/{project_id}/report-prompts/global/financial",
            ).status_code == 403
        finally:
            _delete_test_project(project_id)

    # 2) 本机直连（环回 client）+ 无转发头 → 项目级保存/重置成功。
    with TestClient(app, client=("127.0.0.1", 42317)) as client:
        project_id = f"real-perm-{uuid4()}"
        _insert_real_reference_project(project_id)
        try:
            saved = client.patch(
                f"/api/projects/{project_id}/report-prompts/financial", json=payload,
            )
            assert saved.status_code == 200, saved.text
            body = saved.json()
            assert body["overridden"] is True
            assert body["editable"] is True
            # 新口径：本机直连下全局默认可编辑（全局是部署级配置，与项目类型无关）。
            assert body["global_editable"] is True

            listed = client.get(f"/api/projects/{project_id}/report-prompts").json()
            assert all(item["editable"] is True and item["global_editable"] is True
                       for item in listed)

            # 全局保存对真实参考项目同样可用（且不删除刚建的项目覆盖）。
            global_saved = client.patch(
                f"/api/projects/{project_id}/report-prompts/global/financial", json=payload,
            )
            assert global_saved.status_code == 200, global_saved.text
            assert global_saved.json()["global_overridden"] is True
            assert row("SELECT COUNT(*) c FROM report_prompt_overrides "
                       "WHERE project_id=? AND chapter_key='financial'", (project_id,))["c"] == 1

            assert client.delete(
                f"/api/projects/{project_id}/report-prompts/financial",
            ).status_code == 200
            assert client.delete(
                f"/api/projects/{project_id}/report-prompts/global/financial",
            ).status_code == 200
        finally:
            _purge_global("financial")
            _delete_test_project(project_id)


def test_prompt_outdated_compares_generated_hash_and_never_regenerates():
    """§四 line 41：`prompt_outdated` = 当前有效 Prompt 哈希 ≠ **生成时**哈希；保存 Prompt 不自动再生。

    旧口径比的是"库里的覆盖文本 vs 当前编译器对同一 structured_json 的重编译结果"，
    回答的是"覆盖文本该不该重编译"（编辑器侧的事，由 `recompile_structured_prompts` 在启动时
    自行处理）——它回答不了"**这份报告是不是用旧 Prompt 生成的**"。改为按生成时哈希比之后：
    保存覆盖 → 立刻转为 True，而**正文与生成时哈希都保持不变**（保存不触发再生）。
    """
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "company_name": "Prompt age test project", "amount": 500, "term_months": 12,
            "purpose": "working capital", "data_consent": True,
        })
        assert created.status_code == 201
        project_id, token = created.json()["id"], created.json()["accessToken"]
        headers = {"X-Project-Token": token}
        try:
            # 生成时哈希 = 正文生成那一刻的有效 Prompt 哈希（生产里由 build_chapter_context 冻结）
            generated = hashlib.sha256(
                chapter_prompt("financial", project_id).encode("utf-8")).hexdigest()
            execute(
                "INSERT INTO report_sections (id,project_id,section_key,title,content,citation_ids,"
                "review_status,updated_at,prompt_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
                ("section-prompt-age", project_id, "financial", "财务趋势与盈利质量",
                 "据募集说明书披露，材料不足。", "[]", "draft", now_iso(), generated),
            )
            listed = client.get(f"/api/projects/{project_id}/report", headers=headers).json()
            section = next(item for item in listed if item["section_key"] == "financial")
            assert section["prompt_outdated"] is False, "Prompt 未变时不得谎报过期"

            # 保存项目级 Prompt 覆盖 → 只写覆盖与审计，不碰 report_sections
            saved = client.patch(
                f"/api/projects/{project_id}/report-prompts/financial", headers=headers,
                json={"prompt": "Only use the approved evidence list and state material gaps explicitly."},
            )
            assert saved.status_code == 200

            listed = client.get(f"/api/projects/{project_id}/report", headers=headers).json()
            section = next(item for item in listed if item["section_key"] == "financial")
            assert section["prompt_outdated"] is True, "Prompt 改过之后报告须标记为用旧 Prompt 生成"
            # 「保存 Prompt 不自动再生」：正文与生成时哈希都不动
            assert section["content"] == "据募集说明书披露，材料不足。"
            assert section["prompt_sha256"] == generated
        finally:
            _delete_test_project(project_id)


# ------------------------------------------------- P0-A：全局默认 Prompt --

def _p0a_project(client: TestClient) -> tuple[str, dict]:
    created = client.post("/api/projects", json={
        "company_name": "Global prompt test", "amount": 500, "term_months": 12,
        "purpose": "working capital", "data_consent": True})
    assert created.status_code == 201
    return created.json()["id"], {"X-Project-Token": created.json()["accessToken"]}


def _global_row(chapter_key: str):
    return row("SELECT * FROM global_report_prompts WHERE chapter_key=?", (chapter_key,))


def _purge_global(chapter_key: str) -> None:
    execute("DELETE FROM global_report_prompts WHERE chapter_key=?", (chapter_key,))


def test_global_prompt_save_reset_scope_and_priority():
    """P0-A：全局默认可保存/重置/按范围读取；优先级 项目覆盖 > 全局 > YAML。

    且全局保存**不删除项目覆盖、不调用模型、不修改历史正文**（P0-1 明文）。
    """
    # 本机直连（环回）才允许写全局——TestClient 默认 client.host 是 "testclient"，
    # 显式指定环回地址与真实部署一致。
    with TestClient(app, client=("127.0.0.1", 42317)) as client:
        project_id, headers = _p0a_project(client)
        try:
            listed = client.get(f"/api/projects/{project_id}/report-prompts?scope=global",
                                headers=headers).json()
            fin = next(item for item in listed if item["chapter_key"] == "financial")
            assert fin["scope"] == "global"
            assert fin["global_editable"] is True
            assert fin["global_overridden"] is False
            assert fin["global_prompt"] == fin["default_prompt"]
            assert fin["effective_source"] == "system"
            assert fin["prompt_contract_version"], "响应必须带当前编译器版本"

            saved = client.patch(f"/api/projects/{project_id}/report-prompts/global/financial",
                                 headers=headers, json={"prompt": "全局默认：只用已批准证据。"})
            assert saved.status_code == 200, saved.text
            body = saved.json()
            assert body["global_overridden"] is True
            assert body["global_config_sha256"], "必须有并发基准哈希"

            # 无项目覆盖时，全局立刻生效
            fin = next(item for item in client.get(f"/api/projects/{project_id}/report-prompts",
                                                   headers=headers).json()
                       if item["chapter_key"] == "financial")
            assert fin["effective_source"] == "global"

            # 建一个项目覆盖 → 全局保存不再影响该项目（优先级：项目 > 全局）
            assert client.patch(f"/api/projects/{project_id}/report-prompts/financial",
                                headers=headers, json={"prompt": "项目级配置。"}).status_code == 200
            fin = next(item for item in client.get(f"/api/projects/{project_id}/report-prompts",
                                                   headers=headers).json()
                       if item["chapter_key"] == "financial")
            assert fin["effective_source"] == "project"

            # 保存不调用模型、不写正文
            assert row("SELECT COUNT(*) c FROM llm_attempts WHERE project_id=?",
                       (project_id,))["c"] == 0
            assert row("SELECT COUNT(*) c FROM report_sections WHERE project_id=?",
                       (project_id,))["c"] == 0

            # 重置全局：回落出厂，**项目覆盖仍在**
            reset = client.delete(f"/api/projects/{project_id}/report-prompts/global/financial",
                                  headers=headers)
            assert reset.status_code == 200, reset.text
            assert reset.json()["global_overridden"] is False
            fin = next(item for item in client.get(f"/api/projects/{project_id}/report-prompts",
                                                   headers=headers).json()
                       if item["chapter_key"] == "financial")
            assert fin["overridden"] is True, "重置全局不得连带删除项目覆盖"
            assert _global_row("financial") is None

            # 审计留痕
            with connect() as conn:
                actions = {dict(record)["action"] for record in conn.execute(
                    "SELECT action FROM audit_events WHERE project_id=?", (project_id,)).fetchall()}
            assert {"report_prompt_global_update", "report_prompt_global_reset"} <= actions
        finally:
            _purge_global("financial")
            _delete_test_project(project_id)


def test_global_prompt_write_requires_local_and_non_public(monkeypatch):
    """P0-A：全局写入门禁——公开演示环境或带代理转发头一律 403（项目级不受影响）。"""
    with TestClient(app, client=("127.0.0.1", 42318)) as client:
        project_id, headers = _p0a_project(client)
        try:
            # 公开演示环境：settings 是 frozen dataclass（不能直接 setattr），且整对象替换
            # 会波及无关字段 → 用只覆盖 public_demo、其余透传的代理替换 main 模块里的 settings。
            from backend.app import main as main_module

            class _SettingsWithPublicDemo:
                def __init__(self, value: bool) -> None:
                    self._value = value

                def __getattr__(self, name: str):
                    return self._value if name == "public_demo" else getattr(settings, name)

            monkeypatch.setattr(main_module, "settings", _SettingsWithPublicDemo(True))
            denied = client.patch(f"/api/projects/{project_id}/report-prompts/global/financial",
                                  headers=headers, json={"prompt": "x"})
            assert denied.status_code == 403
            assert "公开演示" in denied.json()["detail"]
            listed = client.get(f"/api/projects/{project_id}/report-prompts?scope=global",
                                headers=headers).json()
            assert next(i for i in listed if i["chapter_key"] == "financial")["global_editable"] is False
            monkeypatch.setattr(main_module, "settings", _SettingsWithPublicDemo(False))

            # 带代理转发头 → 无法确认本机直连
            forwarded = client.patch(f"/api/projects/{project_id}/report-prompts/global/financial",
                                     headers={**headers, "X-Forwarded-For": "10.0.0.1"},
                                     json={"prompt": "x"})
            assert forwarded.status_code == 403
            assert "代理转发头" in forwarded.json()["detail"]
            assert _global_row("financial") is None, "被拒的写入不得落库"
        finally:
            _purge_global("financial")
            _delete_test_project(project_id)


def test_global_prompt_concurrency_hash_and_contract_version():
    """P0-A / `PLAN.md` §四.2：写入前检查配置哈希与**编译器版本**，不符一律 409。"""
    from backend.app.chapter_prompts import PROMPT_CONTRACT_VERSION

    with TestClient(app, client=("127.0.0.1", 42319)) as client:
        project_id, headers = _p0a_project(client)
        try:
            first = client.patch(f"/api/projects/{project_id}/report-prompts/global/financial",
                                 headers=headers, json={"prompt": "第一版。"})
            assert first.status_code == 200
            base = first.json()["global_config_sha256"]

            stale = client.patch(f"/api/projects/{project_id}/report-prompts/global/financial",
                                 headers=headers,
                                 json={"prompt": "第二版。", "expected_config_sha256": "0" * 64})
            assert stale.status_code == 409
            assert "已被其他会话修改" in stale.json()["detail"]

            ok = client.patch(f"/api/projects/{project_id}/report-prompts/global/financial",
                              headers=headers,
                              json={"prompt": "第二版。", "expected_config_sha256": base})
            assert ok.status_code == 200

            bad_version = client.patch(
                f"/api/projects/{project_id}/report-prompts/global/financial", headers=headers,
                json={"prompt": "第三版。", "expected_prompt_contract_version": "contract-0-old"})
            assert bad_version.status_code == 409
            assert "编译器版本" in bad_version.json()["detail"]

            right_version = client.patch(
                f"/api/projects/{project_id}/report-prompts/global/financial", headers=headers,
                json={"prompt": "第三版。", "expected_prompt_contract_version": PROMPT_CONTRACT_VERSION})
            assert right_version.status_code == 200
            # 被拒的写入不得落库：库里应是"第三版。"
            assert _global_row("financial")["prompt"] == right_version.json()["global_prompt"]
        finally:
            _purge_global("financial")
            _delete_test_project(project_id)


def test_global_prompt_guards_for_deterministic_and_unknown_chapters():
    """P0-A：确定性章节 422、未知章节 404、归档项目不可写（沿用既有门禁）。"""
    with TestClient(app, client=("127.0.0.1", 42320)) as client:
        project_id, headers = _p0a_project(client)
        try:
            assert client.patch(f"/api/projects/{project_id}/report-prompts/global/tasks",
                                headers=headers, json={"prompt": "x"}).status_code == 422
            assert client.patch(f"/api/projects/{project_id}/report-prompts/global/not-a-chapter",
                                headers=headers, json={"prompt": "x"}).status_code == 404
            assert client.delete(f"/api/projects/{project_id}/report-prompts/global/not-a-chapter",
                                 headers=headers).status_code == 404
        finally:
            _delete_test_project(project_id)


def test_project_prompt_reset_honours_concurrency_basis():
    """P0-A / `PLAN.md` §四.2：项目级**重置**与保存共用同一条并发基准（重置同样会覆盖别人的改动）。

    只对非空基准生效；不带基准时保持历史行为（旧客户端兼容）。
    """
    with TestClient(app, client=("127.0.0.1", 42321)) as client:
        project_id, headers = _p0a_project(client)
        try:
            saved = client.patch(f"/api/projects/{project_id}/report-prompts/financial",
                                 headers=headers, json={"prompt": "项目级第一版。"})
            assert saved.status_code == 200
            basis = saved.json()["config_sha256"]
            assert basis, "保存后应返回项目级并发基准"

            stale = client.delete(
                f"/api/projects/{project_id}/report-prompts/financial"
                "?expected_config_sha256=" + "0" * 64, headers=headers)
            assert stale.status_code == 409
            # 被拒的重置不得落库
            assert row("SELECT COUNT(*) c FROM report_prompt_overrides "
                       "WHERE project_id=? AND chapter_key='financial'", (project_id,))["c"] == 1

            ok = client.delete(
                f"/api/projects/{project_id}/report-prompts/financial?expected_config_sha256={basis}",
                headers=headers)
            assert ok.status_code == 200
            assert row("SELECT COUNT(*) c FROM report_prompt_overrides "
                       "WHERE project_id=? AND chapter_key='financial'", (project_id,))["c"] == 0

            # 不带基准 → 兼容旧客户端，照常重置（此处已无覆盖，幂等返回 200）
            assert client.delete(f"/api/projects/{project_id}/report-prompts/financial",
                                 headers=headers).status_code == 200
        finally:
            _purge_global("financial")
            _delete_test_project(project_id)
