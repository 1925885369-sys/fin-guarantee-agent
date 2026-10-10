from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz
from docx import Document as WordDocument
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .config import settings
from .ocr import OcrProvider, configured_ocr_provider
from .table_extraction import ExtractedTable, extract_tables


SUPPORTED = {".pdf", ".docx", ".xlsx"}


@dataclass
class SourceFragment:
    locator_type: str
    locator: str
    text: str
    page: int | None = None
    bbox: list[float] | None = None
    extraction_method: str = "native"


@dataclass
class ParsedDocument:
    fragments: list[SourceFragment]
    quality: float
    warning: str = ""
    page_count: int = 0
    parser: str = "native"
    ocr_status: str = "not_required"
    # 表格与文本片段并列输出。扫描件走 OCR 时不产表格，由 ocr_status 说明原因。
    tables: list["ExtractedTable"] = field(default_factory=list)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def classify_document(filename: str, content: str = "") -> str:
    haystack = f"{filename} {content[:2000]}".lower()
    categories = [
        ("募集说明书", ["募集说明书", "发行条款", "本期债券"]),
        ("信用评级报告", ["信用评级报告", "评级结果", "评级观点"]),
        ("担保合同", ["保证合同", "担保合同", "保证责任范围"]),
        ("反担保", ["反担保合同", "反担保措施", "抵押顺位", "质押登记"]),
        ("债务明细", ["债务明细", "债券余额", "到期债务", "交叉违约"]),
        ("募集资金用途", ["募集资金用途", "专户管理", "资金使用计划"]),
        ("偿债安排", ["偿债安排", "偿债现金流", "偿债专户"]),
        ("法律意见", ["法律意见书", "律师事务所", "合法合规"]),
        ("发行决议", ["董事会决议", "股东会决议", "发行决议"]),
        ("承销协议", ["承销协议", "承销商"]),
        ("受托管理", ["受托管理协议", "持有人会议规则"]),
        ("治理材料", ["股权结构", "治理结构", "授权矩阵"]),
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


def parse_pdf(path: Path, ocr_provider: OcrProvider | None = None, *, max_pages: int | None = None) -> ParsedDocument:
    try:
        doc = fitz.open(path)
    except Exception as exc:
        raise ValueError(f"PDF 无法打开：{exc}") from exc
    if doc.needs_pass:
        raise ValueError("PDF 已加密，暂不支持")
    page_limit = settings.max_pages if max_pages is None else max_pages
    if len(doc) > page_limit:
        raise ValueError(f"PDF 超过 {page_limit} 页限制")
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
            fragments.append(SourceFragment("pdf_page", f"第 {page_no} 页", text, page_no, [0, 0, page.rect.width, page.rect.height]))
    pages = max(len(doc), 1)
    quality = min(1.0, total_chars / (pages * 500))
    warning = ""
    if total_chars / pages < 50:
        warning = "疑似扫描型或空白 PDF，文字层不足"
        quality = min(quality, 0.2)
        provider = ocr_provider or configured_ocr_provider()
        if provider.available:
            try:
                ocr_fragments = provider.extract_pdf(path, min(settings.ocr_max_pages, settings.max_pages))
                if ocr_fragments:
                    fragments = [
                        SourceFragment("pdf_ocr_block", f"第 {item.page} 页 OCR区域", item.text, item.page, item.bbox, "ocr")
                        for item in ocr_fragments
                    ]
                    quality = sum(item.confidence for item in ocr_fragments) / len(ocr_fragments)
                    # 扫描件没有可用的矢量线框，表格须标记待人工确认，不让模型补齐。
                    return ParsedDocument(fragments, quality, "", pages, provider.name, "completed")
                return ParsedDocument(fragments, quality, warning + "；OCR未识别到文字", pages, provider.name, "failed")
            except Exception as exc:
                return ParsedDocument(fragments, quality, warning + f"；OCR失败：{type(exc).__name__}", pages, provider.name, "failed")
        return ParsedDocument(fragments, quality, warning + "；需要配置OCR模型", pages, "native", "required")
    return ParsedDocument(fragments, quality, warning, pages, "native", "not_required", _safe_tables(path, doc))


def _safe_tables(path: Path, doc: fitz.Document) -> list[ExtractedTable]:
    """抽取表格。表格抽取失败不应让整份材料解析失败——退回空列表并留待人工确认。"""
    try:
        return extract_tables(path)
    except Exception:
        return []


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
    return ParsedDocument(fragments, min(1.0, len(fragments) / 8), "" if fragments else "Word 文档没有正文", 0, "python-docx")


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
    return ParsedDocument(fragments, min(1.0, len(fragments) / 15), "" if fragments else "Excel 工作簿没有有效单元格", 0, "openpyxl")


def parse_document(path: Path, ocr_provider: OcrProvider | None = None, *, max_pages: int | None = None) -> ParsedDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(path, ocr_provider, max_pages=max_pages)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix == ".xlsx":
        return parse_xlsx(path)
    raise ValueError("仅支持 PDF、DOCX、XLSX")
