"""表格核对接口（Phase 6）。

三条纪律在接口层面必须成立：

1. **同一份数据**：核对接口与报告成品读的是同一个 ``TableBlock``，不存在第二套数字；
2. **只追加不覆盖**：人工更正另存一列，原始值原样保留，历史可查；
3. **不触碰证据**：核对不改动 ``documents`` / ``sources`` 与上传文件。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.database import rows
from backend.app.main import app
from backend.app.report_tables import project_tables


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def project_id() -> str:
    return "demo-bond-huachen"


@pytest.fixture(scope="module")
def tables(client, project_id) -> list[dict]:
    client.get(f"/api/projects/{project_id}/bond-summary")
    response = client.get(f"/api/projects/{project_id}/tables")
    assert response.status_code == 200
    return response.json()


def test_listed_tables_are_the_ones_the_report_uses(client, project_id, tables) -> None:
    """核对接口与报告成品同源：表格 ID 与标题必须一致。"""
    declared = project_tables(project_id, subject="华辰精密制造有限公司")
    assert [item["table_id"] for item in tables] == [block.table_id for block in declared]
    assert [item["title"] for item in tables] == [block.title for block in declared]


def test_matrix_carries_cell_ids_and_traceability(client, project_id, tables) -> None:
    table_id = tables[0]["table_id"]
    matrix = client.get(f"/api/projects/{project_id}/tables/{table_id}").json()
    assert [column["label"] for column in matrix["columns"]][0] == "科目"
    located = 0
    for row_index, item in enumerate(matrix["rows"], start=1):
        for column_index, cell in enumerate(item["cells"]):
            assert cell["cell_id"] == f"{table_id}:r{row_index:02d}c{column_index:02d}"
            if cell["status"] in {"ok", "zero"}:
                located += 1
    assert located > 0, "业务数据格应当带来源或定位"


def test_correction_keeps_the_original_value(client, project_id, tables) -> None:
    table_id = tables[0]["table_id"]
    matrix = client.get(f"/api/projects/{project_id}/tables/{table_id}").json()
    cell = matrix["rows"][0]["cells"][1]
    original = cell["raw"]
    response = client.post(
        f"/api/projects/{project_id}/tables/{table_id}/cells/{cell['cell_id']}/review",
        json={"decision": "corrected", "corrected_raw": "71,999",
              "note": "与审计报告核对后更正", "reviewer": "复核员甲"},
    )
    assert response.status_code == 200
    review = response.json()["review"]
    assert review["original_raw"] == original, "原始值必须原样保留"
    assert review["corrected_raw"] == "71,999"
    assert review["decision"] == "corrected"


def test_history_is_append_only(client, project_id, tables) -> None:
    table_id = tables[1]["table_id"]
    matrix = client.get(f"/api/projects/{project_id}/tables/{table_id}").json()
    cell_id = matrix["rows"][0]["cells"][1]["cell_id"]
    before = len(rows(
        "SELECT id FROM table_cell_reviews WHERE project_id=? AND table_id=? AND cell_id=?",
        (project_id, table_id, cell_id),
    ))
    for decision in ("corrected", "confirmed"):
        payload = {"decision": decision, "note": "历史追加测试"}
        if decision == "corrected":
            payload["corrected_raw"] = "1,234"
        assert client.post(
            f"/api/projects/{project_id}/tables/{table_id}/cells/{cell_id}/review", json=payload,
        ).status_code == 200
    after = rows(
        "SELECT decision FROM table_cell_reviews WHERE project_id=? AND table_id=? AND cell_id=? "
        "ORDER BY created_at, rowid",
        (project_id, table_id, cell_id),
    )
    assert len(after) == before + 2, "每次核对都追加一条，不覆盖历史"
    assert after[-1]["decision"] == "confirmed", "读取时取最新一条"
    detail = client.get(f"/api/projects/{project_id}/tables/{table_id}").json()
    assert detail["cell_reviews"][cell_id]["decision"] == "confirmed"


def test_correction_requires_a_value(client, project_id, tables) -> None:
    table_id = tables[0]["table_id"]
    cell_id = client.get(f"/api/projects/{project_id}/tables/{table_id}").json()["rows"][0]["cells"][1]["cell_id"]
    response = client.post(
        f"/api/projects/{project_id}/tables/{table_id}/cells/{cell_id}/review",
        json={"decision": "corrected"},
    )
    assert response.status_code == 400


def test_unknown_table_and_cell_are_rejected(client, project_id, tables) -> None:
    assert client.get(f"/api/projects/{project_id}/tables/not-a-table").status_code == 404
    table_id = tables[0]["table_id"]
    assert client.post(
        f"/api/projects/{project_id}/tables/{table_id}/cells/nope/review",
        json={"decision": "confirmed"},
    ).status_code == 404


def test_review_does_not_touch_evidence(client, project_id, tables) -> None:
    """核对只写核对记录，不改证据存储与材料。"""
    before = rows(
        "SELECT id,sha256,page_count FROM documents WHERE project_id=? ORDER BY id", (project_id,)
    )
    before_sources = len(rows("SELECT id FROM sources WHERE project_id=?", (project_id,)))
    table_id = tables[0]["table_id"]
    cell_id = client.get(f"/api/projects/{project_id}/tables/{table_id}").json()["rows"][0]["cells"][1]["cell_id"]
    client.post(
        f"/api/projects/{project_id}/tables/{table_id}/cells/{cell_id}/review",
        json={"decision": "rejected", "note": "与原件不符，待重新取证"},
    )
    after = rows(
        "SELECT id,sha256,page_count FROM documents WHERE project_id=? ORDER BY id", (project_id,)
    )
    assert after == before
    assert len(rows("SELECT id FROM sources WHERE project_id=?", (project_id,))) == before_sources
