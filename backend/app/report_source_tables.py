"""Complete source statements and schedules for the financial report chapters."""
from __future__ import annotations

import re
from dataclasses import replace

from .database import rows
from .delivery_wording import document_series, material_numbers, normalize_report_text
from .table_block import TableBlock, TableCell, TableColumn, TableRow, block_from_extracted, validated
from .table_store import load_tables
from .table_extraction import ExtractedCell


# Classification selects whole tables, never a subset of their business rows.
TABLE_FAMILIES = (
    ("balance", "资产负债表", r"资产负债表|consolidated_balance_sheet|standalone_balance_sheet", ("financial",)),
    ("income", "利润表", r"利润表|利润项目|income_statement", ("financial",)),
    ("cashflow", "现金流量表及明细", r"现金流|现金流量|cash_flow_statement", ("financial", "cashflow")),
    ("cash", "货币资金及受限资产明细", r"货币资金|受限.*(?:资金|资产)|(?:资金|资产).*受限|cash_detail", ("financial", "cashflow")),
    ("aging", "应收款账龄结构", r"账龄|receivables_aging", ("financial",)),
    ("debtors", "应收款主要欠款方", r"欠款方|应收.*(?:前五|主要客户)|receivable_debtor", ("financial",)),
    ("receivables", "其他应收款明细", r"其他应收|other_receivable", ("financial",)),
    ("inventory", "存货明细", r"存货|inventory_detail", ("financial",)),
    ("nonrecurring", "非经常性损益明细", r"非经常性损益", ("financial",)),
    ("metrics", "财务及风险控制指标", r"财务指标|财务数据|损益关键指标|净资本|风险控制指标", ("financial",)),
    ("assets", "资产结构及主要资产明细", r"资产结构|融出资金|结算备付金|(?:交易性金融资产|债权投资|返售金融资产).*明细", ("financial",)),
    ("earnings", "收入、成本费用及收益明细", r"营业收入.*(?:结构|构成)|营业支出|投资收益.*明细|成本.*(?:结构|明细)|期间费用", ("financial",)),
    ("maturity", "债务到期及偿还计划", r"到期.*(?:债务|债券)|(?:债务|债券).*到期|债务期限|偿还计划|debt_maturity", ("debt",)),
    ("debt", "有息债务及融资明细", r"有息(?:债务|负债)|债务结构|短期借款|长期借款|应付债券|应付短期融资|拆入资金|卖出回购|interest_bearing_debt", ("debt",)),
    ("credit", "银行授信及使用情况", r"授信|credit_facilit", ("debt",)),
    ("rating", "评级财务数据及评价要素", r"评级|rating_financial", ("rating",)),
)

REQUIRED_FAMILIES = {
    "financial": ("balance", "income", "cashflow", "cash", "aging", "debtors", "receivables", "inventory", "nonrecurring", "metrics"),
    "cashflow": ("cashflow", "cash"),
    "debt": ("debt", "maturity", "credit"),
    "rating": ("rating",),
}

GENERAL_STANDARD_ITEMS = {
    "balance": (
        ("货币资金", r"货币资金|现金及现金等价物", r"流动资产合计|资产总计"),
        ("应收账款", r"应收账款|应收票据及应收账款", r"流动资产合计|资产总计"),
        ("其他应收款", r"其他应收款|其他应收款项", r"流动资产合计|资产总计"),
        ("存货", r"存货", r"流动资产合计|资产总计"),
        ("固定资产", r"固定资产", r"非流动资产合计|资产总计"),
        ("在建工程", r"在建工程", r"非流动资产合计|资产总计"),
        ("无形资产", r"无形资产", r"非流动资产合计|资产总计"),
        ("短期借款", r"短期借款", r"流动负债合计|负债合计"),
        ("应付账款", r"应付账款|应付票据及应付账款", r"流动负债合计|负债合计"),
        ("长期借款", r"长期借款", r"非流动负债合计|负债合计"),
        ("应付债券", r"应付债券", r"非流动负债合计|负债合计"),
        ("实收资本", r"实收资本|股本", r"所有者权益合计|负债和所有者权益总计"),
        ("资本公积", r"资本公积", r"所有者权益合计|负债和所有者权益总计"),
        ("未分配利润", r"未分配利润", r"所有者权益合计|负债和所有者权益总计"),
    ),
    "income": (
        ("营业收入", r"营业收入|营业总收入", r"净利润"),
        ("营业成本", r"营业成本", r"净利润"),
        ("税金及附加", r"税金及附加", r"净利润"),
        ("销售费用", r"销售费用", r"净利润"),
        ("管理费用", r"管理费用", r"净利润"),
        ("研发费用", r"研发费用", r"净利润"),
        ("财务费用", r"财务费用", r"净利润"),
        ("投资收益", r"投资收益", r"净利润"),
        ("信用减值损失", r"信用减值损失", r"净利润"),
        ("营业利润", r"营业利润", r"净利润"),
        ("利润总额", r"利润总额", r"净利润"),
        ("净利润", r"净利润", r"$^"),
    ),
    "cashflow": (
        ("销售商品、提供劳务收到的现金", r"销售商品.*提供劳务收到的现金", r"经营活动现金流入小计|经营活动产生的现金流量净额"),
        ("购买商品、接受劳务支付的现金", r"购买商品.*接受劳务支付的现金", r"经营活动现金流出小计|经营活动产生的现金流量净额"),
        ("支付给职工以及为职工支付的现金", r"支付给职工", r"经营活动现金流出小计|经营活动产生的现金流量净额"),
        ("经营活动产生的现金流量净额", r"经营活动产生的现金流量净额", r"投资活动产生的现金流量净额|现金及现金等价物净增加额"),
        ("投资活动产生的现金流量净额", r"投资活动产生的现金流量净额", r"筹资活动产生的现金流量净额|现金及现金等价物净增加额"),
        ("筹资活动产生的现金流量净额", r"筹资活动产生的现金流量净额", r"现金及现金等价物净增加额|期末现金及现金等价物余额"),
        ("期末现金及现金等价物余额", r"期末现金及现金等价物余额", r"$^"),
    ),
}

SECURITIES_STANDARD_ITEMS = {
    "balance": (
        ("结算备付金", r"结算备付金", r"资产总计"), ("融出资金", r"融出资金", r"资产总计"),
        ("买入返售金融资产", r"买入返售金融资产", r"资产总计"), ("交易性金融资产", r"交易性金融资产", r"资产总计"),
        ("应付短期融资款", r"应付短期融资款", r"负债合计"), ("拆入资金", r"拆入资金", r"负债合计"),
        ("卖出回购金融资产款", r"卖出回购金融资产款", r"负债合计"), ("代理买卖证券款", r"代理买卖证券款", r"负债合计"),
    ),
    "income": (
        ("手续费及佣金净收入", r"手续费及佣金净收入", r"净利润"), ("利息净收入", r"利息净收入", r"净利润"),
        ("投资收益", r"投资收益", r"净利润"), ("公允价值变动收益", r"公允价值变动收益", r"净利润"),
    ),
}


def _family(table, document_role: str) -> str:
    caption = re.sub(r"\s+", "", table.caption)
    text = f"{caption} {table.table_key}"
    if document_role == "rating_report":
        labels = " ".join(row[0].text for row in table.rows if row)
        financial_labels = re.findall(r"资产总|负债|债务|营业收入|净利润|现金流|净资产收益|偿付能力|财务风险|所有者权益", labels)
        if len(financial_labels) >= 2 or re.search(r"评级|财务|信用|rating_financial", text):
            return "rating"
        return ""
    for family, _, pattern, _ in TABLE_FAMILIES:
        if re.search(pattern, text, re.IGNORECASE):
            return family
    return ""


def _standard_statement_rows(table, family: str, *, securities: bool):
    if family not in {"balance", "income", "cashflow"}:
        return table
    if securities:
        items = ([item for item in GENERAL_STANDARD_ITEMS[family]
                  if item[0] in {"货币资金", "实收资本", "资本公积", "未分配利润",
                                 "营业收入", "净利润", "经营活动产生的现金流量净额",
                                 "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额"}]
                 + list(SECURITIES_STANDARD_ITEMS.get(family, ())))
    else:
        items = list(GENERAL_STANDARD_ITEMS[family])
    labels = [row[0].text.strip() for row in table.rows if row]
    compact = [re.sub(r"\s+", "", label).replace("：", "").replace(":", "") for label in labels]
    missing = [(label, pattern, anchor) for label, pattern, anchor in items
               if not any(re.search(pattern, item) for item in compact)]
    if not missing:
        return table
    rows = list(table.rows)
    next_index = max((cell.row for row in rows for cell in row), default=0) + 1
    pending: dict[int, list[str]] = {}
    for label, _pattern, anchor in missing:
        index = next((i for i, row in enumerate(rows) if row and re.search(anchor, row[0].text)), len(rows))
        pending.setdefault(index, []).append(label)
    for index in sorted(pending, reverse=True):
        synthetic = []
        for label in pending[index]:
            cells = [ExtractedCell(row=next_index, column=0, text=label, bbox=None, page=0)]
            cells.extend(ExtractedCell(row=next_index, column=column, text="未披露", bbox=None, page=0)
                         for column in range(1, len(table.columns)))
            synthetic.append(cells)
            next_index += 1
        rows[index:index] = synthetic
    return replace(table, rows=rows)


def source_scope(table, document_role: str) -> str:
    section_labels = " ".join(row[0].text for row in table.rows if row and re.search(r"合并口径|母公司口径", row[0].text))
    text = f"{table.scope} {table.caption} {' '.join(table.columns)} {section_labels}"
    standalone = bool(re.search(r"母公司|本部|standalone", text))
    consolidated = bool(re.search(r"合并|consolidated", text))
    if standalone and consolidated:
        label = "合并及母公司口径（按原表分别披露）"
    elif standalone:
        label = "母公司口径"
    elif consolidated:
        label = "合并口径"
    else:
        label = "原表口径（待核验）"
    return f"外部信用评价文件／{label}" if document_role == "rating_report" else label


def source_table_id(table, *, duplicate_key: bool = False, variant: str = "") -> str:
    suffix = f"-{table.document_id}" if duplicate_key else ""
    scope_suffix = f"-{variant}" if variant else ""
    return f"uploaded-{table.table_key}-{table.parser_version}{scope_suffix}{suffix}"


def _scope_variants(table, document_role: str):
    markers = []
    for index, row in enumerate(table.rows):
        label = row[0].text.strip() if row else ""
        if re.fullmatch(r"(?:合并|母公司|本部)(?:口径|报表|财务报表)?[：:]?", label):
            scope = "合并口径" if label.startswith("合并") else "母公司口径"
            markers.append((index, scope))
    if len({scope for _, scope in markers}) < 2:
        return [(table, source_scope(table, document_role), "")]
    output: dict[str, list] = {}
    for marker_index, (start, scope) in enumerate(markers):
        end = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(table.rows)
        scoped_rows = table.rows[start + 1:end]
        if scoped_rows:
            output.setdefault(scope, []).extend(scoped_rows)
    return [
        (replace(table, rows=scoped_rows, scope=scope), scope,
         "consolidated" if scope == "合并口径" else "standalone")
        for scope, scoped_rows in output.items()
    ]


def chapter_source_table_blocks(project_id: str, chapter_key: str, *, subject: str) -> list[TableBlock]:
    if chapter_key not in REQUIRED_FAMILIES:
        return []
    tables = load_tables(project_id)
    documents = {str(item["id"]): item for item in rows(
        "SELECT id,document_role FROM documents WHERE project_id=?", (project_id,),
    )}
    sources = rows("SELECT id,document_id,page FROM sources WHERE project_id=?", (project_id,))
    numbers = material_numbers(project_id)
    series = document_series(project_id)
    output, coverage = [], {}
    for table in tables:
        role = str(documents.get(table.document_id, {}).get("document_role") or "")
        family = _family(table, role)
        definition = next((item for item in TABLE_FAMILIES if item[0] == family), None)
        if not definition or chapter_key not in definition[3]:
            continue
        coverage.setdefault(family, []).append(table)
        if not table.columns or not table.rows:
            continue
        table_labels = " ".join(row[0].text for row in table.rows if row)
        securities = bool(re.search(r"结算备付金|融出资金|代理买卖证券款|净资本|手续费及佣金", table_labels))
        table = _standard_statement_rows(table, family, securities=securities)
        refs = tuple(str(item["id"]) for item in sources if item["document_id"] == table.document_id
                     and table.page <= int(item.get("page") or 0) <= max(table.pages or [table.page]))
        duplicate = sum(item.table_key == table.table_key for item in tables) > 1
        label = "外部信用评价文件" if role == "rating_report" else "发行文件" if role == "prospectus" else "材料"
        material = f"{label}〔{series[table.document_id]}〕" if table.document_id in series else label
        end_page = max(table.pages or [table.page])
        pages = f"第 {table.page} 页" if end_page == table.page else f"第 {table.page} 至 {end_page} 页"
        for scoped_table, table_scope, variant in _scope_variants(table, role):
            block = block_from_extracted(
                scoped_table,
                table_id=source_table_id(table, duplicate_key=duplicate, variant=variant),
                subject=subject, scope=table_scope, document_id=table.document_id, source_refs=refs,
                page_source_refs={page: tuple(str(item["id"]) for item in sources
                                             if item["document_id"] == table.document_id and item.get("page") == page)
                                  for page in {cell.page for row in scoped_table.rows for cell in row}},
            )
            if "待核验" in block.scope or not table.caption or not table.unit:
                summary = dict(block.validation_summary)
                summary["scope_or_metadata_needs_review"] = True
                block = replace(block, validation_status="needs_review", validation_summary=summary)
            title = table.caption or f"{definition[1]}原表（{pages}，待核验）"
            if variant:
                title = f"{title}（{table_scope}）"
            original_unit = f"；原表单位为{table.unit}" if table.unit else ""
            notes = [*block.notes, f"注：来源为{material}{pages}{original_unit}；保留全部已抽取业务行、原始数值及披露期间，金额等值换算为万元，未自行补数或重算。"]
            if not table.caption or not table.unit:
                notes.append("注：原表标题或单位未稳定恢复，数值及口径须对照原件人工核验。")
            output.append(replace(
                block, title=normalize_report_text(title, numbers),
                notes=tuple(normalize_report_text(note, numbers) for note in notes),
            ))
    output.append(_coverage_block(chapter_key, subject, coverage, output))
    return output


def _coverage_block(chapter_key: str, subject: str, coverage: dict, blocks: list[TableBlock]) -> TableBlock:
    table_rows = []
    for family in REQUIRED_FAMILIES[chapter_key]:
        label = next(item[1] for item in TABLE_FAMILIES if item[0] == family)
        found = coverage.get(family, [])
        if not found:
            status, detail = "未识别到原表", "需取得或核验对应材料，事实汇总不替代完整原表。"
        else:
            ids = {source_table_id(table, duplicate_key=False) for table in found}
            failed = any(not table.rows or not table.columns or table.status == "needs_review" for table in found)
            failed = failed or any(block.validation_status != "passed" for block in blocks
                                   if any(block.table_id.startswith(table_id) for table_id in ids))
            status = "待人工核验" if failed else "已接入材料原表"
            detail = "；".join(f"{table.caption or label}（第 {table.page} 页）" for table in found)
        table_rows.append(TableRow(row_key=family, cells=(
            TableCell(raw=label, status="text"), TableCell(raw=status, status="text"),
            TableCell(raw=detail, status="text"),
        )))
    return validated(TableBlock(
        table_id=f"report-{chapter_key}-source-coverage", table_type="annual_series",
        title="报表及明细资料覆盖情况", subject=subject, scope="材料完整性核验", currency="CNY", unit="",
        columns=(TableColumn(key="item", label="报表或明细", role="subject"),
                 TableColumn(key="status", label="材料状态", role="status"),
                 TableColumn(key="reference", label="原表定位或缺失说明", role="reference")),
        rows=tuple(table_rows), origin_mode="uploaded_table",
        notes=("注：未识别到原表不代表科目余额为零；未提供或抽取不可靠的部分须人工核验，系统不生成替代金额。",),
    ))
