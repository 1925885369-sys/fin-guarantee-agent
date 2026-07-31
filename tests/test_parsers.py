from pathlib import Path

import fitz
import pytest

from backend.app.facts import deterministic_extract, normalize_numeric
from backend.app.parsers import parse_document
from backend.app.sample_case import generate_demo_materials


def test_generated_materials_are_parseable():
    materials = generate_demo_materials()
    assert {item["path"].suffix for item in materials} == {".pdf", ".docx", ".xlsx"}
    for material in materials:
        parsed = parse_document(material["path"])
        assert parsed.fragments
        assert parsed.quality > 0.2


def test_scanned_pdf_is_flagged(tmp_path: Path):
    path = tmp_path / "scan.pdf"
    document = fitz.open(); document.new_page(); document.save(path)
    parsed = parse_document(path)
    assert parsed.quality <= 0.2
    assert "扫描型" in parsed.warning


def test_encrypted_and_corrupt_files_fail_cleanly(tmp_path: Path):
    encrypted = tmp_path / "encrypted.pdf"
    document = fitz.open(); page = document.new_page(); page.insert_text((50, 50), "secret")
    document.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    with pytest.raises(ValueError, match="加密"):
        parse_document(encrypted)
    corrupt = tmp_path / "broken.xlsx"; corrupt.write_bytes(b"PK broken")
    with pytest.raises(ValueError, match="无法打开"):
        parse_document(corrupt)


def test_unit_normalization_and_labelled_excel_extraction():
    assert normalize_numeric(1.2, "亿元", "amount") == (12000.0, "万元")
    assert normalize_numeric(50000, "元", "amount") == (5.0, "万元")
    sources = [
        {"id": "h", "document_id": "d", "locator_type": "xlsx_range", "locator": "财务!A1:C1", "text": "指标（万元） | 2024 | 2025"},
        {"id": "r", "document_id": "d", "locator_type": "xlsx_range", "locator": "财务!A2:C2", "text": "营业收入 | 1000 | 1200"},
    ]
    facts = deterministic_extract(sources)
    assert [(item["period"], item["value"]) for item in facts if item["fact_key"] == "revenue"] == [("2024", 1000.0), ("2025", 1200.0)]

