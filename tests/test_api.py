import time

from fastapi.testclient import TestClient

from backend.app.main import app


def wait_run(client: TestClient, run_id: str, headers=None):
    for _ in range(120):
        response = client.get(f"/api/runs/{run_id}", headers=headers)
        assert response.status_code == 200
        run = response.json()
        if run["status"] in {"completed", "failed", "cancelled", "model_required"}:
            return run
        time.sleep(0.05)
    raise AssertionError("run timed out")


def test_demo_workflow_and_exports():
    with TestClient(app) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert len(client.get("/api/projects").json()) == 3
        inventory = client.get("/api/projects/demo-huachen/document-inventory").json()
        assert set(inventory) == {"documents", "gaps", "readiness"}
        service_inventory = client.get("/api/projects/demo-xingqiao/document-inventory").json()
        assert "抵押与访谈" in {item["group"] for item in service_inventory["gaps"]}
        started = client.post("/api/projects/demo-huachen/runs", json={"focus": "全面尽调"})
        run = wait_run(client, started.json()["id"])
        assert run["status"] == "completed"
        assert run["progress"] == 100
        nodes = client.get(f"/api/runs/{run['id']}/nodes").json()
        assert len(nodes) == 8
        assert all(item["status"] == "completed" for item in nodes)
        checks = client.get("/api/projects/demo-huachen/cross-checks").json()
        risks = client.get("/api/projects/demo-huachen/risks").json()
        report = client.get("/api/projects/demo-huachen/report").json()
        assert len(checks) == 10 and len(risks) == 8 and len(report) == 10
        assert {item["status"] for item in checks} <= {"passed", "warning", "insufficient"}
        citation = client.get(f"/api/citations/{report[0]['citation_ids'][0]}")
        assert citation.status_code == 200 and citation.json()["locator"]
        assert client.get("/api/projects/demo-huachen/export?format=md").status_code == 200
        exported = client.get("/api/projects/demo-huachen/export?format=docx")
        assert exported.status_code == 200 and len(exported.content) > 10000


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
        started = client.post(f"/api/projects/{project_id}/runs", json={"focus": "全面尽调"}, headers=headers)
        run = wait_run(client, started.json()["id"], headers)
        assert run["status"] == "model_required"
        assert client.get(f"/api/runs/{run['id']}/nodes").status_code == 403
        nodes = client.get(f"/api/runs/{run['id']}/nodes", headers=headers).json()
        assert nodes[-1]["status"] == "model_required"
        retried = client.post(f"/api/runs/{run['id']}/retry", headers=headers)
        assert retried.status_code == 202


def test_human_fact_review_uses_project_authorization():
    with TestClient(app) as client:
        fact = client.get("/api/projects/demo-huachen/facts").json()[0]
        updated = client.patch(f"/api/projects/demo-huachen/facts/{fact['id']}", json={
            "period": fact["period"], "value": fact["value"], "unit": fact["unit"], "text_value": fact["text_value"],
        })
        assert updated.status_code == 200
        refreshed = next(item for item in client.get("/api/projects/demo-huachen/facts").json() if item["id"] == fact["id"])
        assert refreshed["review_status"] == "human_verified"
