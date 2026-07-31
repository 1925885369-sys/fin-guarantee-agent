from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import fitz
from docx import Document as WordDocument
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .config import settings


SUPPORTED = {".pdf", ".docx", ".xlsx"}


@dataclass
class SourceFragment:
    locator_type: str
    locator: str
    text: str


@dataclass
class ParsedDocument:
    fragments: list[SourceFragment]
    quality: float
    warning: str = ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_document(filename: str, content: str = "") -> str:
    haystack = f"{filename} {content[:2000]}".lower()
    categories = [
        ("融资申请", ["申请", "贷款", "融资用途"]),
        ("财务报表", ["资产负债", "利润表", "现金流量"]),
        ("银行流水", ["银行流水", "账户流入", "回款"]),
        ("纳税资料", ["纳税", "增值税", "开票"]),
        ("合同订单", ["合同", "订单", "客户", "供应商"]),
        ("应收账款", ["应收账款", "账龄", "逾期"]),
        ("负债担保", ["借款", "对外担保", "有息负债"]),
        ("抵押物", ["抵押", "估值", "反担保"]),
        ("访谈纪要", ["访谈", "现场", "走访"]),
        ("企业信息", ["营业执照", "实控人", "关联企业"]),
    ]
    for category, keywords in categories:
        if any(keyword in haystack for keyword in keywords):
            return category
    return "其他材料"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_pdf(path: Path) -> ParsedDocument:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ValueError(f"PDF 无法打开：{exc}") from exc
    if doc.needs_pass:
        raise ValueError("PDF 已加密，暂不支持")
    if len(doc) > settings.max_pages:
        raise ValueError(f"PDF 超过 {settings.max_pages} 页限制")
    fragments: list[SourceFragment] = []
    total_chars = 0
    for page_no, page in enumerate(doc, start=1):
        blocks = page.get_text("blocks")
        page_texts = [_clean(str(block[4])) for block in blocks if _clean(str(block[4]))]
        text = "\n".join(page_texts)
        total_chars += len(text)
        if total_chars > settings.max_total_chars:
            raise ValueError("文档文字总量超过处理限制")
        if text:
            fragments.append(SourceFragment("pdf_page", f"第 {page_no} 页", text[:12000]))
    pages = max(len(doc), 1)
    quality = min(1.0, total_chars / (pages * 500))
    warning = ""
    if total_chars / pages < 50:
        warning = "疑似扫描型或空白 PDF，文字层不足"
        quality = min(quality, 0.2)
    return ParsedDocument(fragments, quality, warning)


def parse_docx(path: Path) -> ParsedDocument:
    try:
        doc = WordDocument(path)
    except Exception as exc:
        raise ValueError(f"Word 文档无法打开：{exc}") from exc
    fragments = [
        SourceFragment("docx_paragraph", f"第 {index} 段", _clean(paragraph.text))
        for index, paragraph in enumerate(doc.paragraphs, start=1)
        if _clean(paragraph.text)
    ]
    for table_index, table in enumerate(doc.tables, start=1):
        for row_index, table_row in enumerate(table.rows, start=1):
            values = [_clean(cell.text) for cell in table_row.cells if _clean(cell.text)]
            if values:
                fragments.append(SourceFragment("docx_paragraph", f"表格 {table_index} 第 {row_index} 行", " | ".join(values)))
    if sum(len(item.text) for item in fragments) > settings.max_total_chars:
        raise ValueError("文档文字总量超过处理限制")
    return ParsedDocument(fragments, min(1.0, len(fragments) / 8), "" if fragments else "Word 文档没有正文")


def parse_xlsx(path: Path) -> ParsedDocument:
    try:
        workbook = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError(f"Excel 文档无法打开：{exc}") from exc
    fragments: list[SourceFragment] = []
    total_chars = 0
    for sheet in workbook.worksheets:
        for row_index, row_values in enumerate(sheet.iter_rows(values_only=True), start=1):
            values = [str(value).strip() for value in row_values if value is not None and str(value).strip()]
            if values:
                text = " | ".join(values)
                total_chars += len(text)
                if total_chars > settings.max_total_chars:
                    raise ValueError("工作簿文字总量超过处理限制")
                last_col = get_column_letter(max(len(row_values), 1))
                fragments.append(SourceFragment("xlsx_range", f"{sheet.title}!A{row_index}:{last_col}{row_index}", text))
    return ParsedDocument(fragments, min(1.0, len(fragments) / 15), "" if fragments else "Excel 工作簿没有有效单元格")


def parse_document(path: Path) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".xlsx":
        return parse_xlsx(path)
    raise ValueError("仅支持 PDF、DOCX、XLSX")
