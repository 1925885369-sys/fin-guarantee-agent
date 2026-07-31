from __future__ import annotations

import secrets
from pathlib import Path

import fitz
from docx import Document as WordDocument
from openpyxl import Workbook

from .config import settings
from .database import connect, json_dump, now_iso
from .facts import canonical_key
from .parsers import sha256


DEMO_PROJECT_ID = "demo-huachen"


def _make_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    lines = [
        "华辰精密制造有限公司融资担保申请书",
        "申请金额：800 万元",
        "融资期限：12 个月",
        "资金用途：采购数控加工设备及补充原材料采购资金",
        "还款来源：主营业务销售回款",
        "企业声明：申请材料均为虚构演示数据，不对应任何真实企业。",
    ]
    y = 90
    for index, line in enumerate(lines):
        page.insert_text((70, y), line, fontsize=18 if index == 0 else 12, fontname="china-s")
        y += 48 if index == 0 else 32
    doc.save(path)


def _make_company_docx(path: Path) -> None:
    doc = WordDocument()
    doc.add_heading("华辰精密制造有限公司基本情况", level=1)
    for text in [
        "企业成立于2017年，主营汽车零部件精密加工，注册资本2000万元。",
        "实际控制人为周明远，持股72%；关联企业华辰贸易由其配偶控制。",
        "2025年第一大客户远航汽车贡献销售收入2432万元，占营业收入38%。",
        "主要供应商前三名采购占比46%，原材料以特种钢材和铝合金为主。",
        "企业申请800万元流动资金贷款担保，期限12个月，用于采购数控设备和原材料。",
    ]:
        doc.add_paragraph(text)
    doc.save(path)


def _make_finance_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "三年财务数据"
    ws.append(["指标（万元）", 2023, 2024, 2025])
    rows = [
        ["营业收入", 4200, 5100, 6400],
        ["净利润", 220, 280, 310],
        ["经营活动现金流净额", 80, -120, -260],
        ["应收账款", 700, 980, 1650],
        ["存货", 880, 1020, 1300],
        ["资产总额", 6000, 7100, 8200],
        ["负债总额", 3200, 4300, 5600],
        ["流动资产", 3600, 4100, 4550],
        ["流动负债", 2500, 3100, 3900],
        ["有息负债", 1400, 1900, 2500],
    ]
    for item in rows:
        ws.append(item)
    ws2 = wb.create_sheet("债务明细")
    ws2.append(["债权机构", "余额（万元）", "到期时间", "性质"])
    ws2.append(["城商银行A", 900, "2026-09", "流动资金贷款"])
    ws2.append(["租赁公司B", 500, "2026-11", "融资租赁"])
    ws2.append(["银行C", 300, "2027-04", "设备贷款"])
    ws2.append(["一年内集中到期合计", 1700, "未来12个月", "含拟申请融资"])
    wb.save(path)


def _make_operations_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "收入交叉核验"
    ws.append(["口径", "2025金额（万元）", "说明"])
    ws.append(["财务报表营业收入", 6400, "企业财务报表"])
    ws.append(["增值税申报收入", 6200, "纳税申报汇总"])
    ws.append(["银行经营性流入", 5600, "剔除借款及关联方往来后"])
    ws2 = wb.create_sheet("应收账款账龄")
    ws2.append(["账龄", "金额（万元）", "占比"])
    ws2.append(["1年以内", 1100, "66.7%"])
    ws2.append(["1-2年", 350, "21.2%"])
    ws2.append(["2年以上", 200, "12.1%"])
    ws3 = wb.create_sheet("异常流水")
    ws3.append(["日期", "对手方", "金额（万元）", "摘要"])
    ws3.append(["2025-12-18", "华辰贸易", 420, "往来款，未提供对应合同"])
    wb.save(path)


def _make_risk_docx(path: Path) -> None:
    doc = WordDocument()
    doc.add_heading("合同、负债、抵押物及现场访谈摘要", level=1)
    paragraphs = [
        "已提供设备采购合同金额450万元、原材料采购合同金额200万元，合计650万元，低于800万元融资申请。",
        "企业现有有息负债2500万元，未来12个月需偿还或续作1700万元。",
        "企业为关联企业华辰贸易提供500万元连带责任保证，尚未解除。",
        "拟抵押工业设备评估价值1000万元，评估报告出具于26个月前；按70%折扣计算覆盖额为700万元。",
        "现场访谈确认部分主要客户回款周期由90天延长至150天。",
        "企业未能解释向华辰贸易支付420万元往来款的商业背景，也未提供对应合同。",
    ]
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(path)


def generate_demo_materials() -> list[dict]:
    directory = settings.resolved_data_dir / "demo_materials"
    directory.mkdir(parents=True, exist_ok=True)
    specs = [
        ("demo-application", "融资担保申请书.pdf", "融资申请", _make_pdf),
        ("demo-company", "企业及实控人信息.docx", "企业信息", _make_company_docx),
        ("demo-finance", "三年财务及债务明细.xlsx", "财务报表", _make_finance_xlsx),
        ("demo-operations", "流水纳税与应收账款.xlsx", "银行流水", _make_operations_xlsx),
        ("demo-risk", "合同抵押物与访谈纪要.docx", "访谈纪要", _make_risk_docx),
    ]
    materials = []
    for doc_id, filename, kind, writer in specs:
        path = directory / filename
        if not path.exists():
            writer(path)
        materials.append({"id": doc_id, "filename": filename, "kind": kind, "path": path})
    return materials


SOURCES = [
    ("s-app", "demo-application", "pdf_page", "第 1 页", "申请金额800万元，期限12个月，用于采购数控设备及补充原材料采购资金。"),
    ("s-owner", "demo-company", "docx_paragraph", "第 3 段", "实际控制人为周明远，持股72%；关联企业华辰贸易由其配偶控制。"),
    ("s-customer", "demo-company", "docx_paragraph", "第 4 段", "2025年第一大客户远航汽车贡献销售收入2432万元，占营业收入38%。"),
    ("s-revenue", "demo-finance", "xlsx_range", "三年财务数据!A2:D2", "营业收入 | 4200 | 5100 | 6400"),
    ("s-profit", "demo-finance", "xlsx_range", "三年财务数据!A3:D3", "净利润 | 220 | 280 | 310"),
    ("s-cashflow", "demo-finance", "xlsx_range", "三年财务数据!A4:D4", "经营活动现金流净额 | 80 | -120 | -260"),
    ("s-ar", "demo-finance", "xlsx_range", "三年财务数据!A5:D5", "应收账款 | 700 | 980 | 1650"),
    ("s-balance", "demo-finance", "xlsx_range", "三年财务数据!A7:D10", "资产总额8200万元；负债总额5600万元；流动资产4550万元；流动负债3900万元。"),
    ("s-debt", "demo-finance", "xlsx_range", "债务明细!A2:D5", "有息负债2500万元，未来12个月需偿还或续作1700万元。"),
    ("s-tax-bank", "demo-operations", "xlsx_range", "收入交叉核验!A2:C4", "财务报表营业收入6400万元；增值税申报收入6200万元；银行经营性流入5600万元。"),
    ("s-aging", "demo-operations", "xlsx_range", "应收账款账龄!A2:C4", "应收账款1650万元，其中1-2年350万元、2年以上200万元。"),
    ("s-related", "demo-operations", "xlsx_range", "异常流水!A2:D2", "2025年12月向关联企业华辰贸易支付往来款420万元，未提供对应合同。"),
    ("s-contract", "demo-risk", "docx_paragraph", "第 2 段", "设备和原材料合同合计650万元，低于800万元融资申请。"),
    ("s-guarantee", "demo-risk", "docx_paragraph", "第 4 段", "企业为关联企业华辰贸易提供500万元连带责任保证，尚未解除。"),
    ("s-collateral", "demo-risk", "docx_paragraph", "第 5 段", "设备评估价值1000万元，评估报告已出具26个月；按70%折扣覆盖额为700万元。"),
    ("s-visit", "demo-risk", "docx_paragraph", "第 6 段", "主要客户回款周期由90天延长至150天。"),
]


FACTS = [
    ("f-rev-23", "财务", "营业收入", "2023", 4200, "万元", "", ["s-revenue"]),
    ("f-rev-24", "财务", "营业收入", "2024", 5100, "万元", "", ["s-revenue"]),
    ("f-rev-25", "财务", "营业收入", "2025", 6400, "万元", "", ["s-revenue"]),
    ("f-tax-25", "税务", "纳税申报收入", "2025", 6200, "万元", "", ["s-tax-bank"]),
    ("f-bank-25", "流水", "经营性流入", "2025", 5600, "万元", "", ["s-tax-bank"]),
    ("f-profit-25", "财务", "净利润", "2025", 310, "万元", "", ["s-profit"]),
    ("f-ocf-25", "财务", "经营现金流净额", "2025", -260, "万元", "", ["s-cashflow"]),
    ("f-ar-24", "财务", "应收账款", "2024", 980, "万元", "", ["s-ar"]),
    ("f-ar-25", "财务", "应收账款", "2025", 1650, "万元", "", ["s-ar"]),
    ("f-assets", "财务", "资产总额", "2025", 8200, "万元", "", ["s-balance"]),
    ("f-liabilities", "财务", "负债总额", "2025", 5600, "万元", "", ["s-balance"]),
    ("f-current-assets", "财务", "流动资产", "2025", 4550, "万元", "", ["s-balance"]),
    ("f-current-liabilities", "财务", "流动负债", "2025", 3900, "万元", "", ["s-balance"]),
    ("f-debt", "债务", "有息负债", "2025", 2500, "万元", "", ["s-debt"]),
    ("f-due", "债务", "未来12个月到期债务", "2026", 1700, "万元", "", ["s-debt"]),
    ("f-top-customer", "经营", "第一大客户占比", "2025", 38, "%", "", ["s-customer"]),
    ("f-overdue-ar", "经营", "一年以上应收账款", "2025", 550, "万元", "", ["s-aging"]),
    ("f-related", "关联方", "无合同关联方往来", "2025", 420, "万元", "", ["s-related"]),
    ("f-contract", "用途", "已提供用途合同", "2026", 650, "万元", "", ["s-contract"]),
    ("f-collateral", "抵押物", "折后抵押价值", "2026", 700, "万元", "", ["s-collateral"]),
    ("f-collateral-age", "抵押物", "抵押物估值账龄", "2026", 26, "个月", "", ["s-collateral"]),
]


def seed_demo() -> None:
    materials = generate_demo_materials()
    now = now_iso()
    with connect() as conn:
        if conn.execute("SELECT 1 FROM projects WHERE id=?", (DEMO_PROJECT_ID,)).fetchone():
            for fact_id, _, name, _, _, _, _, _ in FACTS:
                conn.execute("UPDATE facts SET fact_key=?,raw_value=CAST(value AS TEXT),extraction_method='seed' WHERE id=? AND (fact_key='' OR fact_key IS NULL)",
                             (canonical_key(name), fact_id))
            if not conn.execute("SELECT 1 FROM facts WHERE id='f-collateral-age'").fetchone():
                item = FACTS[-1]
                conn.execute(
                    "INSERT INTO facts (id,project_id,category,name,period,value,unit,text_value,source_ids,confidence,fact_key,raw_value,extraction_method,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item[0], DEMO_PROJECT_ID, item[1], item[2], item[3], item[4], item[5], item[6], json_dump(item[7]), 0.99,
                     canonical_key(item[2]), str(item[4]), "seed", "unreviewed"),
                )
            return
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (DEMO_PROJECT_ID, secrets.token_urlsafe(24), "华辰精密制造有限公司", 800, 12,
             "采购数控加工设备及补充原材料采购资金", "ready", now, now, now),
        )
        for material in materials:
            path: Path = material["path"]
            media = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[path.suffix]
            count = sum(1 for source in SOURCES if source[1] == material["id"])
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (material["id"], DEMO_PROJECT_ID, material["filename"], material["kind"], media,
                 sha256(path), path.stat().st_size, "parsed", 0.98, count, "", now),
            )
        conn.executemany(
            "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text) VALUES (?,?,?,?,?,?)",
            [(source_id, DEMO_PROJECT_ID, doc_id, kind, locator, text) for source_id, doc_id, kind, locator, text in SOURCES],
        )
        conn.executemany(
            "INSERT INTO facts (id,project_id,category,name,period,value,unit,text_value,source_ids,confidence,fact_key,raw_value,extraction_method,review_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(fact_id, DEMO_PROJECT_ID, category, name, period, value, unit, text_value, json_dump(source_ids), 0.99,
              canonical_key(name), str(value) if value is not None else text_value, "seed", "unreviewed")
             for fact_id, category, name, period, value, unit, text_value, source_ids in FACTS],
        )
