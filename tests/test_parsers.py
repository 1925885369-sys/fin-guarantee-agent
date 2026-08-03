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


def test_excel_amount_that_looks_like_a_year_uses_header_periods():
    sources = [
        {"id": "h", "document_id": "d", "locator_type": "xlsx_range", "locator": "财务!A1:C1", "text": "指标（万元） | 2024 | 2025"},
        {"id": "r", "document_id": "d", "locator_type": "xlsx_range", "locator": "财务!A2:C2", "text": "流动资产 | 1700 | 2000"},
    ]

    facts = deterministic_extract(sources)

    assert [(item["period"], item["value"]) for item in facts if item["fact_key"] == "current_assets"] == [
        ("2024", 1700.0),
        ("2025", 2000.0),
    ]


def test_overdue_receivables_are_derived_from_aging_buckets():
    sources = [
        {"id": "one", "document_id": "d", "locator_type": "xlsx_range", "locator": "账龄!A2:C2", "text": "1年以内 | 1100 | 66.7%"},
        {"id": "two", "document_id": "d", "locator_type": "xlsx_range", "locator": "账龄!A3:C3", "text": "1-2年 | 350 | 21.2%"},
        {"id": "three", "document_id": "d", "locator_type": "xlsx_range", "locator": "账龄!A4:C4", "text": "2年以上 | 200 | 12.1%"},
    ]

    facts = deterministic_extract(sources)
    overdue = next(item for item in facts if item["fact_key"] == "overdue_receivables")

    assert overdue["value"] == 550
    assert overdue["period"] == ""
    assert overdue["source_ids"] == ["two", "three"]
