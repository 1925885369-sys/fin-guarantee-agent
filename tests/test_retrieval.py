from __future__ import annotations

from types import SimpleNamespace

import fitz
import pytest
from fastapi.testclient import TestClient

from backend.app import main, retrieval
from backend.app.database import rows
from backend.app.main import app
from backend.app.parsers import parse_pdf
from backend.app.retrieval import _split_spans, _table_text_chunks_from_rows


def test_overlong_token_counts_do_not_warn_or_truncate(tmp_path, caplog):
    transformers = pytest.importorskip("transformers")
    vocab = tmp_path / "vocab.txt"
    vocab.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\n偿\n", encoding="utf-8")
    tokenizer = transformers.BertTokenizer(vocab_file=str(vocab), model_max_length=512)
    count = retrieval._token_counter(SimpleNamespace(tokenizer=tokenizer))
    text = "偿" * 1394

    spans = _split_spans(text, "偿债安排", count)

    assert count("偿" * 697) == 697
    assert "Token indices sequence length" not in caplog.text
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    assert all(
        len(tokenizer.encode("偿债安排\n" + text[start:end])) <= 386
        for start, end in spans
    )
    assert all(right_start <= left_end for (_, left_end), (right_start, _) in zip(spans, spans[1:]))


def test_token_bounded_chunks_keep_offsets_and_overlap():
    text = "偿债安排。" * 160
    spans = _split_spans(text, "偿债安排", lambda value: len(value))

    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    assert all(end > start for start, end in spans)
    assert all(len("偿债安排\n" + text[start:end]) <= 384 for start, end in spans)
    assert any(left_end > right_start for (_, left_end), (right_start, _) in zip(spans, spans[1:]))


def test_fallback_chunks_retain_overlong_page_tail():
    text = "募集说明书披露。" * 150 + "尾部证据：到期兑付安排由偿债专户支付。"
    spans = _split_spans(text, "发行条款", None)

    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    assert "尾部证据" in text[slice(*spans[-1])]
    assert all(end - start <= 700 for start, end in spans)


def test_table_chunks_keep_caption_unit_headers_and_page_source():
    tables = [{
        "id": "table-1", "document_id": "document-1", "table_key": "p007-t01",
        "caption": "合并资产负债表", "unit": "万元", "currency": "CNY", "scope": "consolidated",
        "start_page": 7, "headers_json": '["项目", "2025年末", "2024年末"]',
        "rows_json": '[ [{"text":"货币资金","page":7},{"text":"120","page":7},{"text":"95","page":7}] ]',
    }]
    source_by_page = {("document-1", 7): {
        "id": "source-7", "document_id": "document-1", "kind": "募集说明书",
        "document_role": "prospectus", "filename": "prospectus.pdf", "section_role": "financial_statements",
    }}

    chunks = _table_text_chunks_from_rows(tables, source_by_page, None)

    assert len(chunks) == 1
    assert chunks[0]["source_id"] == "source-7"
    assert chunks[0]["page"] == 7
    assert chunks[0]["chunk_type"] == "table"
    assert all(value in chunks[0]["text"] for value in ("合并资产负债表", "单位：万元", "2025年末", "货币资金", "120"))


def test_pdf_page_longer_than_old_cap_keeps_its_tail(tmp_path):
    path = tmp_path / "long-page.pdf"
    page_text = "A" * 13000 + "TAIL-EVIDENCE-991827"
    with fitz.open() as document:
        page = document.new_page(width=30000, height=200)
        page.insert_text((10, 50), page_text, fontsize=1)
        document.save(path)

    parsed = parse_pdf(path)

    assert parsed.page_count == 1
    assert len(parsed.fragments) == 1
    assert len(parsed.fragments[0].text) > 12000
    assert "TAIL-EVIDENCE-991827" in parsed.fragments[0].text


def test_material_qa_returns_page_evidence_without_cloud_model(monkeypatch):
    monkeypatch.setattr(main, "is_configured", lambda: False)
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/demo-bond-qualitative/material-qa",
            json={"question": "哪些合同缺少客户盖章？"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "retrieved_only"
    assert payload["citations"]
    assert any("客户盖章" in item["quote"] for item in payload["citations"])
    assert all(item["source_id"] and item["page"] for item in payload["citations"])


def test_material_qa_enforces_project_access_before_retrieval():
    with TestClient(app) as client:
        response = client.post(
            "/api/projects/nonexistent-private-project/material-qa",
            json={"question": "查询合同"},
        )

    assert response.status_code == 404


def test_hybrid_index_keeps_vector_positions_aligned_after_document_filter(monkeypatch):
    np = pytest.importorskip("numpy")
    from backend.app.main import startup

    startup()
    source = rows(
        "SELECT s.id,s.document_id FROM sources s WHERE s.project_id=? AND s.text LIKE ? LIMIT 1",
        ("demo-bond-qualitative", "%缺少客户盖章%"),
    )[0]

    class FakeTokenizer:
        @staticmethod
        def encode(text, **_kwargs):
            return list(text)

    class FakeEncoder:
        tokenizer = FakeTokenizer()

    monkeypatch.setattr(retrieval, "_model_manifest", lambda: {"model_id": retrieval.MODEL_ID, "revision": "test-revision"})
    monkeypatch.setattr(retrieval, "_model_files_ready", lambda: True)
    monkeypatch.setattr(retrieval, "_load_encoder", lambda: FakeEncoder())

    def fake_embed(_encoder, texts):
        vectors = np.zeros((len(texts), 768), dtype=np.float32)
        for index, text in enumerate(texts):
            if "缺少客户盖章" in text or "签章不足" in text:
                vectors[index, 0] = 1
            else:
                vectors[index, 1] = 1
        return vectors

    monkeypatch.setattr(retrieval, "_embed_texts", fake_embed)
    hits = retrieval.retrieve_chunks(
        "demo-bond-qualitative", "交易的签章不足", top_k=5,
        document_ids={source["document_id"]}, mode="vector",
    )

    assert hits
    assert hits[0]["source_id"] == source["id"]
    assert all(item["document_id"] == source["document_id"] for item in hits)
    assert all(item["retrieval_mode"] == "hybrid" for item in hits)
