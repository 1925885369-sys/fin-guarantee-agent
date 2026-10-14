"""报告里的系统生成表格。

与 ``conflict_block`` 同一套机制：由代码从已校验的数据确定性生成，进入模型上下文后
**必须逐字保留**，缺失由回退补回，且不计入模型分析正文的篇幅、不参与正文的数字白名单。

计划要求「报告财务章必定由代码生成规范化表格，不依赖模型是否选择输出 Markdown 表格」，
所以表格既不取自模型输出，也不受模型回退影响。

来源模式的用词沿用 ``TableBlock v1`` 契约：由**事实**汇总而成的一律是 ``profile_facts``，
并在表注里写明「不是从原始报表逐格提取」——只有真正解析上传 PDF 原表才是
``uploaded_table``，两者核对后才是 ``merged``。
"""
from __future__ import annotations

import uuid
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal

from .database import connect, now_iso, rows
from .table_block import (
    TableBlock, TableCell, annual_series_block, cell_from_text, conflict_comparison_block,
    table_block_from_dict, validate_table_block,
)


SYSTEM_TABLE_HEADING = "### 系统整理数据表（数值由系统生成，模型不得改动或复述）"

# 表后分析的章节职责。**走模型输入而不是章节 prompt**：prompt 的 ``rules`` 是可编辑
# 字段，用户在界面上存过覆盖后，YAML 的新规则不会回溯更新已存文本（实测：财务章的
# 全局默认里仍是旧的 4 条规则）。职责约束必须随上下文投递才一定到得了模型。
SYSTEM_TABLE_ROLE = (
    "本章数据表已由系统生成并原样附在确定性草稿中。正文只写分析，按"
    "「本章判断—主要变化分析—盈利与现金实现—财务结构与核验边界—冲突及待核验事项」组织；"
    "不得逐行复述表内数字，不得改动、重排或重算表内数值、年度与表注，"
    "不得自行输出表格，不得为变化编造原因，"
    "不得把冻结主体档案的事实汇总表述为已完成的独立核验。"
    "已经取得的确定性数据与缺少交叉核验材料的判断必须分开写：后者要写成"
    "具体核验动作（取得哪份原件、核对哪份台账、函证哪个对象），而不是笼统说“需进一步核实”。"
    "正文不得出现系统执行话术（规则状态、字段名、节点、校验结果等）。"
    "表格之后的结论必须与表内数值一致，不得给出表格支持不了的判断。"
)

# 已接入系统表格的章节。扩展时在此登记，并在 system_table_markdown 里分派。
TABLE_CHAPTERS = ("financial", "cashflow", "debt", "rating")
TABLE_RULE_VERSION = "complete-standard-v2"

# 三年主要资产负债项目表按披露习惯排列：先总量、后主要科目。
BALANCE_ITEMS = (
    ("total_assets", "资产总计"),
    ("total_liabilities", "负债合计"),
    ("equity", "所有者权益合计"),
    ("current_assets", "其中：流动资产合计"),
    ("current_liabilities", "其中：流动负债合计"),
    ("cash", "货币资金"),
    ("accounts_receivable", "应收账款"),
    ("other_receivables", "其他应收款"),
    ("inventory", "存货"),
    ("interest_bearing_debt", "有息负债"),
)
RECEIVABLE_ITEMS = (
    ("accounts_receivable", "应收账款"),
    ("total_overdue_receivables", "其中：逾期应收账款"),
)
INCOME_ITEMS = (
    ("revenue", "一、营业收入"),
    ("cost_of_goods_sold", "减：营业成本"),
    ("net_profit", "四、净利润"),
)
CASHFLOW_ITEMS = (
    ("cash_received_from_sales", "销售商品、提供劳务收到的现金"),
    ("operating_cash_flow", "经营活动产生的现金流量净额"),
    ("investing_cash_flow", "投资活动产生的现金流量净额"),
    ("financing_cash_flow", "筹资活动产生的现金流量净额"),
)
DEBT_ITEMS = (
    ("short_term_debt", "短期借款"),
    ("long_term_debt", "长期借款"),
    ("bonds_payable", "应付债券"),
    ("interest_bearing_debt", "有息负债合计"),
    ("debt_due_12m", "未来12个月到期债务"),
)


CASHFLOW_SOURCES = (
    ("operating_cash_flow", "经营活动现金流量净额（已审实际）"),
    ("cash_received_from_sales", "销售商品、提供劳务收到的现金（已审实际）"),
)
DEBT_STRUCTURE = (
    ("short_term_debt", "短期借款"),
    ("long_term_debt", "长期借款"),
    ("bonds_payable", "应付债券"),
)
DEBT_MATURITY = (("debt_due_12m", "未来12个月到期债务"),)
RATING_SOURCE_ITEMS = (
    ("revenue", "营业收入"),
    ("interest_bearing_debt", "有息负债"),
)


# 统一的科目显示名。
#
# 报告表、材料原表与核对表必须用**同一个名字**指同一个科目。实测踩过：
# ``FACT_REGISTRY["total_assets"].label`` 是「资产总额」，而材料与报告表用的是
# 「资产总计」——同一个值在同一份报告里出现两个名字。以报表科目名为准
# （那是披露用语），未登记的再回退到事实登记表的标签。
DISPLAY_LABELS = {
    "total_assets": "资产总计", "total_liabilities": "负债合计", "equity": "所有者权益合计",
    "current_assets": "流动资产合计", "current_liabilities": "流动负债合计",
    "cash": "货币资金", "accounts_receivable": "应收账款", "other_receivables": "其他应收款",
    "inventory": "存货", "interest_bearing_debt": "有息负债",
    "short_term_debt": "短期借款", "long_term_debt": "长期借款", "bonds_payable": "应付债券",
    "debt_due_12m": "未来12个月到期债务",
    "revenue": "营业收入", "cost_of_goods_sold": "营业成本", "net_profit": "净利润",
    "operating_cash_flow": "经营活动现金流量净额",
    "cash_received_from_sales": "销售商品、提供劳务收到的现金",
    "investing_cash_flow": "投资活动现金流量净额",
    "financing_cash_flow": "筹资活动现金流量净额",
    "total_overdue_receivables": "逾期应收账款",
    "rating_revenue": "评级口径营业收入", "rating_total_debt": "评级口径有息负债",
}


def _label(fact_key: str) -> str:
    """字段键 → 业务科目名。成品里不出现内部字段键。"""
    from .facts import FACT_REGISTRY

    if fact_key in DISPLAY_LABELS:
        return DISPLAY_LABELS[fact_key]
    definition = FACT_REGISTRY.get(fact_key)
    return definition.label if definition else fact_key


REPORT_SCOPE_PRIORITY = ("consolidated", "standalone")
# 口径的中文标签由本模块下方的 ``SCOPE_LABELS`` / ``_scope_label`` 统一提供（单一来源）——
# 这里不再另立一份，否则同名函数会被后定义者静默覆盖，标签口径分叉。
# 评级、债券条款类事实不属于财务报表口径：并存多条时按"主体 → 交易 → 其他"稳定排序，
# 避免 LIMIT 1 随机取到另一条（同一份材料两次生成可能得到不同评级）。
ENTITY_FACT_SCOPE_ORDER = (
    " ORDER BY CASE c.scope WHEN 'entity' THEN 0 WHEN 'transaction' THEN 1 ELSE 2 END, c.rowid"
)


def report_scopes(project_id: str) -> list[str]:
    """该项目实际存在哪些**报表口径**，按 合并 → 母公司 排序。

    口径在**表级**固定：先给这张表选定一个口径，再取该口径下的**全部科目**。
    不存在的口径不生成表；非报表口径（entity / transaction / unknown）不并入报表表。
    """
    present = {
        str(item["scope"] or "")
        for item in rows(
            "SELECT DISTINCT scope FROM canonical_facts WHERE project_id=? AND status!='conflicted'",
            (project_id,),
        )
    }
    return [scope for scope in REPORT_SCOPE_PRIORITY if scope in present]


def _facts(project_id: str, scope: str) -> dict[str, dict[str, dict]]:
    """**指定口径**下的事实：``fact_key → period → 事实``；该口径没有的科目/期间不出现。

    ``canonical_facts`` 本身不存来源，来源挂在被选中的断言上——表格的每个单元格
    都要能追溯到具体来源，所以这里必须把断言一并取出来。

    口径由调用方在**表级**固定后传入，不在这里按科目择优。按科目择优会让同一张表里
    不同科目来自不同口径（合并 / 母公司混用），整表再标成"合并口径"就是失真；修订稿
    明确禁止这种写法。该口径缺失的科目/期间由 ``_series_rows`` 渲染为"未披露"，
    **绝不用另一口径补位**——合并表缺某科目时不得取母公司值，母公司表同理。
    """
    grouped: dict[str, dict[str, dict]] = {}
    for item in rows(
        "SELECT c.fact_key,c.period,c.period_type,c.value,c.unit,c.text_value,c.scope,"
        "       a.source_ids AS source_ids "
        "FROM canonical_facts c "
        "LEFT JOIN fact_assertions a ON a.id=c.selected_assertion_id "
        "WHERE c.project_id=? AND c.status!='conflicted' AND c.scope=?",
        (project_id, scope),
    ):
        grouped.setdefault(str(item["fact_key"]), {})[str(item["period"])] = {
            **dict(item), "scope": scope, "source_ids": _decode(item["source_ids"]),
        }
    return grouped


REPORT_TABLE_ITEMS = tuple(
    item for group in (BALANCE_ITEMS, RECEIVABLE_ITEMS, INCOME_ITEMS, CASHFLOW_ITEMS, DEBT_ITEMS)
    for item in group
)


def _decode(value: object) -> tuple[str, ...]:
    import json

    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    try:
        parsed = json.loads(str(value or "[]"))
    except (ValueError, TypeError):
        return ()
    return tuple(str(item) for item in parsed) if isinstance(parsed, list) else ()


def _periods(grouped: dict[str, dict[str, dict]], items) -> list[str]:
    years: set[str] = set()
    for key, _ in items:
        years.update(period for period in grouped.get(key, {}) if period.isdigit())
    return sorted(years, reverse=True)


def _series_rows(
    grouped: dict[str, dict[str, dict]], items, periods: list[str], unit: str,
) -> list[tuple[str, tuple[TableCell, ...]]]:
    """科目 × 期间的取值行。**缺失显示"未披露"**：不跳行、也不由另一口径补位。

    修订稿要求"合并口径某科目或某期间缺失时显示'未披露'"。科目整行缺席会让人误以为
    该表不披露这个科目，因此对固定科目清单逐项出行，缺值写"未披露"；同一口径内确实
    没有该科目时也保留该行，使缺口可见而不是消失。
    """
    built = []
    for key, label in items:
        by_period = grouped.get(key, {})
        cells = []
        for period in periods:
            fact = by_period.get(period)
            if fact is None or fact.get("value") is None:
                cells.append(TableCell(raw="未披露", status="not_applicable"))
                continue
            cells.append(cell_from_text(
                _format(fact["value"]), unit=str(fact.get("unit") or unit),
                origin_type="profile_fact", source_ids=fact.get("source_ids") or (),
                fact_key=key,
            ))
        built.append((label, tuple(cells)))
    return built


def _format(value: object) -> str:
    try:
        formatted = f"{Decimal(str(value)):,f}"
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
    except Exception:
        return str(value)


def ensure_canonical_facts(project_id: str) -> None:
    """确保规范化事实已建立。

    ``canonical_facts`` 原本只在打开「事实核对」页时按需建立。报告生成不能依赖
    用户先去点那个页面——否则同一份材料，点过与没点过会生成两种报告，而且后者
    会静默丢掉表格。``sync_fact_assertions`` 本身会保留人工裁决过的取值。
    """
    if rows("SELECT id FROM canonical_facts WHERE project_id=? LIMIT 1", (project_id,)):
        return
    if not rows("SELECT id FROM facts WHERE project_id=? LIMIT 1", (project_id,)):
        return
    from .professional import sync_fact_assertions

    sync_fact_assertions(project_id)


def _for_each_report_scope(build, project_id: str, *, subject: str,
                           fallback_label: str = "口径待核验") -> list[TableBlock]:
    """按**表级口径**逐个生成表：合并与母公司各一套，不混用、不互相补值。

    口径必须先于取数固定：先定这张表属于哪个口径，再取该口径的**全部科目**。项目里
    存在哪些报表口径由 ``report_scopes`` 判定；一个口径都没有时不生成报表表——非报表
    口径（entity / transaction / unknown）的资料不并入报表口径的表。
    """
    scopes = report_scopes(project_id)
    if not scopes:
        return build(project_id, subject=subject, scope="", scope_label=fallback_label, suffix="")
    blocks: list[TableBlock] = []
    for index, scope in enumerate(scopes):
        blocks.extend(build(
            project_id, subject=subject, scope=scope, scope_label=_scope_label(scope),
            suffix="" if index == 0 else f"-{scope}",
        ))
    return blocks


def financial_table_blocks(project_id: str, *, subject: str, scope: str = "合并口径") -> list[TableBlock]:
    """财务章的系统表格：三年主要资产负债项目表、应收账款明细表、冲突对照表。

    **合并与母公司分别成表**：每个存在的报表口径各出一套表。``scope`` 只作兜底标签，
    用于项目中没有任何报表口径的情况。
    """
    ensure_canonical_facts(project_id)
    blocks = _for_each_report_scope(
        _financial_scope_blocks, project_id, subject=subject, fallback_label=scope,
    )
    conflicts = _conflicts(project_id)
    if conflicts:
        blocks.append(conflict_comparison_block(
            table_id="report-financial-conflicts", title="冲突对照表", subject=subject,
            conflicts=conflicts,
            notes=("注：同一事项的多个披露值并列保留，未自动择一；差异需取得原始材料后人工确认。",),
        ))
    return blocks


def _financial_scope_blocks(project_id: str, *, subject: str, scope: str,
                            scope_label: str, suffix: str) -> list[TableBlock]:
    """**单一口径**下的一套财务表。该口径缺失的科目/期间写"未披露"，不由另一口径补位。"""
    grouped = _facts(project_id, scope) if scope else {}
    blocks: list[TableBlock] = []

    periods = _periods(grouped, BALANCE_ITEMS)
    if periods:
        blocks.append(annual_series_block(
            table_id=f"report-financial-balance{suffix}", title="三年主要资产负债项目表",
            subject=subject, scope=scope_label, unit="万元",
            period_labels=[f"{year} 年末" for year in periods],
            rows=_series_rows(grouped, BALANCE_ITEMS, periods, "万元"),
            source_refs=_sources(grouped, BALANCE_ITEMS),
            notes=("注：本表为系统依据已核验事实生成的口径汇总，不是从原始报表逐格提取。",),
        ))

    receivable_periods = _periods(grouped, RECEIVABLE_ITEMS)
    if receivable_periods:
        blocks.append(annual_series_block(
            table_id=f"report-financial-receivables{suffix}", title="应收账款规模及明细",
            subject=subject, scope=scope_label, unit="万元",
            period_labels=[f"{year} 年末" for year in receivable_periods],
            rows=_series_rows(grouped, RECEIVABLE_ITEMS, receivable_periods, "万元"),
            source_refs=_sources(grouped, RECEIVABLE_ITEMS),
            notes=("注：逾期应收账款为应收账款的其中项，不参与加总；账龄与主要欠款方明细见材料原表。",),
        ))

    for items, table_id, title, notes in (
        (INCOME_ITEMS, "report-financial-income", "主要利润项目表",
         "注：本表为系统依据已核验事实生成的口径汇总，不是从原始报表逐格提取。"),
        (CASHFLOW_ITEMS, "report-financial-cashflow", "主要现金流量项目表",
         "注：经营活动现金流量净额为已审期间的实际数；预测值与实际数期间不同、口径不同，不得混同。"),
        (DEBT_ITEMS, "report-financial-debt", "债务结构表",
         "注：有息负债为短期借款、长期借款与应付债券之和；到期债务为披露口径，不计入有息负债加总。"),
    ):
        periods_for = _periods(grouped, items)
        if not periods_for:
            continue
        blocks.append(annual_series_block(
            table_id=f"{table_id}{suffix}", title=title, subject=subject,
            scope=scope_label, unit="万元",
            period_labels=[f"{year} {'年度' if items in (INCOME_ITEMS, CASHFLOW_ITEMS) else '年末'}" for year in periods_for],
            rows=_series_rows(grouped, items, periods_for, "万元"),
            source_refs=_sources(grouped, items), notes=(notes,),
        ))
    return blocks


def _sources(grouped: dict[str, dict[str, dict]], items) -> tuple[str, ...]:
    collected: list[str] = []
    for key, _ in items:
        for fact in grouped.get(key, {}).values():
            for source in fact.get("source_ids") or ():
                if source not in collected:
                    collected.append(source)
    return tuple(collected)


def _conflicts(project_id: str) -> list[dict]:
    """构造冲突对照表的输入：同一字段同一期间的全部断言。"""
    output: list[dict] = []
    for item in rows(
        "SELECT fact_key,period,scope,value,unit,text_value,status,subject "
        "FROM canonical_facts WHERE project_id=? AND status='conflicted' ORDER BY fact_key,period",
        (project_id,),
    ):
        assertions = []
        seen: set[tuple[str, str]] = set()
        for assertion in rows(
            "SELECT id,value,unit,raw_value,text_value,source_ids,origin_type "
            "FROM fact_assertions WHERE project_id=? AND fact_key=? AND period=? ORDER BY created_at",
            (project_id, item["fact_key"], item["period"]),
        ):
            # 同一取值可能来自多条断言（字段键不同、书写格式不同）。披露值列只列
            # 真正不同的取值，否则同一数字会重复出现，读者会以为是三个不同来源。
            raw = _render_value(assertion)
            dedupe_key = (raw, str(assertion.get("unit") or ""))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            assertions.append({
                "id": assertion["id"],
                "raw": raw,
                "value": assertion.get("value"),
                "text_value": assertion.get("text_value") or "",
                "source_ids": _decode(assertion.get("source_ids")),
                "locator": {"document_id": _document_for(assertion.get("source_ids"))},
            })
        if len(assertions) < 2:
            continue
        output.append({
            "fact_key": item["fact_key"],
            "label": _label_for(item["fact_key"]),
            "period": item["period"], "scope": _scope_label(item["scope"]),
            "unit": item["unit"], "assertions": assertions,
        })
    return output


SCOPE_LABELS = {"consolidated": "合并口径", "standalone": "母公司口径"}


def _scope_label(scope: object) -> str:
    """口径写中文——``standalone`` 这类内部枚举不得进入成品。"""
    text = str(scope or "").strip()
    return SCOPE_LABELS.get(text, text if text and text != "unknown" else "未注明口径")


def _render_value(assertion: dict) -> str:
    """把断言渲染成披露口径的字符串。

    优先用规范化后的数值，避免把 ``36000.0万元`` 这种原始串直接摆进成品。
    """
    value = assertion.get("value")
    if value is not None:
        rendered = _format(value)
        unit = str(assertion.get("unit") or "")
        return f"{rendered}{unit}" if unit else rendered
    return str(assertion.get("text_value") or assertion.get("raw_value") or "").strip()


def _label_for(fact_key: str) -> str:
    from .facts import FACT_REGISTRY

    definition = FACT_REGISTRY.get(fact_key)
    return definition.label if definition else fact_key


_SOURCE_CACHE: dict[str, str] = {}

MATERIAL_LABELS = {"prospectus": "发行文件", "rating_report": "外部信用评价文件"}


def _document_for(source_ids: object) -> str:
    """把来源 ID 映射成材料类型，报告成品里不出现内部 UUID。"""
    ids = _decode(source_ids)
    return source_reference(ids[0]).split("〔")[0] if ids else ""


def source_reference(source_id: str) -> str:
    """来源 ID → 成品口径的定位，例如「发行文件〔资料01〕第 29 页」。

    成品里不出现内部来源 ID 与 UUID；工作台的证据详情仍显示真实文件名——
    那是另一条读取路径，本函数不改动任何证据存储。
    """
    from .delivery_wording import document_series

    key = str(source_id or "")
    if not key:
        return ""
    if key in _SOURCE_CACHE:
        return _SOURCE_CACHE[key]
    record = rows(
        "SELECT s.page, s.document_id, d.document_role, d.kind, d.project_id "
        "FROM sources s JOIN documents d ON d.id=s.document_id WHERE s.id=?",
        (key,),
    )
    if not record:
        _SOURCE_CACHE[key] = ""
        return ""
    item = record[0]
    label = MATERIAL_LABELS.get(str(item.get("document_role") or ""), str(item.get("kind") or "材料"))
    series = document_series(str(item.get("project_id") or "")).get(str(item.get("document_id")), "")
    page = item.get("page")
    reference = f"{label}〔{series}〕" if series else label
    if page:
        reference = f"{reference}第 {page} 页"
    _SOURCE_CACHE[key] = reference
    return reference


def render_table_block(block: TableBlock, *, index: int, total: int) -> str:
    """把 ``TableBlock`` 渲染成 Markdown。成品里不出现内部 UUID 与字段键。"""
    meta = [f"单位：{block.unit}"] if block.unit else []
    # 冲突对照表不是事实汇总——它的口径就是「多来源并列」，写成汇总会误导读者。
    if block.table_type != "conflict_comparison":
        meta.append(f"口径：{block.scope}")
    meta.append(f"来源：{_origin_label(block)}")
    # 不写「表 N/M」形式：``N/M`` 与「模型采用率 x/y」的形状相同，成品校验里
    # 「不得泄漏采用率」的守卫会把它一并拦下（实测撞过）。读者也不需要分母。
    lines = [f"**表 {index}：{block.title}**（{'｜'.join(meta)}）", ""]
    lines.append("| " + " | ".join(column.label for column in block.columns) + " |")
    lines.append("| " + " | ".join("---" for _ in block.columns) + " |")
    for row in block.rows:
        lines.append("| " + " | ".join(_cell_text(cell) for cell in row.cells) + " |")
    if block.notes:
        lines.extend(["", *block.notes])
    if block.validation_status != "passed":
        lines.extend(["", f"> 本表校验状态：{block.validation_status}，需人工确认后方可作为数据使用。"])
    return "\n".join(lines)


def _origin_label(block: TableBlock) -> str:
    if block.table_type == "conflict_comparison":
        return "多来源披露并列，未择一采信"
    return {
        "profile_facts": "已核验事实汇总",
        "uploaded_table": "上传材料原表",
        "merged": "档案与上传材料核对",
    }.get(block.origin_mode, block.origin_mode)


def _cell_text(cell: TableCell) -> str:
    """单元格渲染：原文优先，无数据写作 ``—``。"""
    if cell.status in {"blank", "not_applicable"} and cell.raw != "未披露":
        return "—"
    return cell.raw or ("—" if cell.normalized in {None, ""} else cell.normalized)


def _current_chapter_table_blocks(project_id: str, chapter_key: str, *, subject: str) -> list[TableBlock]:
    """按章分派。用函数分派而不是模块级字典：字典会在构造器定义之前求值，
    导入即 NameError（实测踩过）。"""
    builder = {
        "financial": financial_table_blocks,
        "cashflow": cashflow_table_blocks,
        "debt": debt_table_blocks,
        "rating": rating_table_blocks,
    }.get(chapter_key)
    if not builder:
        return []
    from .report_source_tables import chapter_source_table_blocks

    return [*builder(project_id, subject=subject),
            *chapter_source_table_blocks(project_id, chapter_key, subject=subject)]


def table_blocks_hash(blocks: list[TableBlock]) -> str:
    payload = json.dumps([block.to_dict() for block in blocks], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def save_table_snapshot(project_id: str, chapter_key: str, *, subject: str,
                        blocks: list[TableBlock] | None = None, conn=None) -> dict:
    """冻结某章表格快照。传入 ``conn`` 时并入调用方事务——写入端要求快照与正文**同一事务**
    （第三点细化方案 §四：禁止提交正文后另行保存表格快照）。"""
    current = blocks if blocks is not None else _current_chapter_table_blocks(project_id, chapter_key, subject=subject)
    digest = table_blocks_hash(current)
    statement = (
        "INSERT INTO report_table_snapshots(project_id,chapter_key,blocks_json,input_hash,rule_version,updated_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,chapter_key) DO UPDATE SET "
        "blocks_json=excluded.blocks_json,input_hash=excluded.input_hash,rule_version=excluded.rule_version,updated_at=excluded.updated_at"
    )
    params = (project_id, chapter_key, json.dumps([item.to_dict() for item in current], ensure_ascii=False),
              digest, TABLE_RULE_VERSION, now_iso())
    if conn is not None:
        conn.execute(statement, params)
    else:
        with connect() as owned:
            owned.execute(statement, params)
    return {"input_hash": digest, "rule_version": TABLE_RULE_VERSION, "blocks": current}


def table_snapshot_status(project_id: str, chapter_key: str, *, subject: str) -> dict:
    snapshot = rows(
        "SELECT input_hash,rule_version,updated_at FROM report_table_snapshots WHERE project_id=? AND chapter_key=?",
        (project_id, chapter_key),
    )
    current = _current_chapter_table_blocks(project_id, chapter_key, subject=subject)
    current_hash = table_blocks_hash(current)
    frozen_hash = str(snapshot[0]["input_hash"]) if snapshot else ""
    return {
        "snapshot_hash": frozen_hash, "current_hash": current_hash,
        "rule_version": str(snapshot[0]["rule_version"]) if snapshot else "",
        "updated_at": snapshot[0]["updated_at"] if snapshot else None,
        "outdated": (not snapshot or frozen_hash != current_hash
                     or str(snapshot[0]["rule_version"]) != TABLE_RULE_VERSION),
    }


def chapter_table_blocks(project_id: str, chapter_key: str, *, subject: str) -> list[TableBlock]:
    snapshot = rows(
        "SELECT blocks_json FROM report_table_snapshots WHERE project_id=? AND chapter_key=?",
        (project_id, chapter_key),
    )
    if snapshot:
        try:
            return [table_block_from_dict(item) for item in json.loads(snapshot[0]["blocks_json"] or "[]")]
        except (ValueError, TypeError, KeyError):
            pass
    return _current_chapter_table_blocks(project_id, chapter_key, subject=subject)


def render_system_table_markdown(chapter_key: str, blocks: list[TableBlock]) -> str:
    """把**同一份** ``blocks`` 渲染成系统表格 Markdown 正文（三件套之一）。

    与 :func:`chapter_guard._system_table_block` 现在的拼装方式保持一致：标题
    ``SYSTEM_TABLE_HEADING`` + 逐表 :func:`render_table_block`，以空行分隔。
    非表格章或空表返回空串。
    """
    if chapter_key not in TABLE_CHAPTERS or not blocks:
        return ""
    rendered = [
        render_table_block(block, index=index, total=len(blocks))
        for index, block in enumerate(blocks, 1)
    ]
    return "\n\n".join([SYSTEM_TABLE_HEADING, *rendered])


@dataclass(frozen=True)
class ChapterTables:
    """一次构建产出的**三件套**：表格正文 + 结构化记录 + 表格快照。

    三个出口共用同一份 ``blocks``，禁止分别重新查询当前表格（第三点细化方案 §二.1）。
    """

    project_id: str
    chapter_key: str
    subject: str
    blocks: tuple[TableBlock, ...]
    markdown: str
    records: tuple[dict, ...]

    @property
    def snapshot(self) -> dict:
        """``save_table_snapshot`` 落库所需的快照 payload（blocks 列表 + 哈希 + 规则版本）。"""
        data = list(self.blocks)
        return {
            "blocks": data,
            "input_hash": table_blocks_hash(data),
            "rule_version": TABLE_RULE_VERSION,
        }


def build_chapter_tables(project_id: str, chapter_key: str, *, subject: str,
                         current: bool = False) -> ChapterTables:
    """**一次**取表，同时产出表格正文、结构化记录与表格快照（三件套单一来源）。

    同一份 ``blocks`` 同时喂给 :func:`render_system_table_markdown`、
    :func:`table_evidence.records_from_blocks` 与 :attr:`ChapterTables.snapshot`，
    杜绝「三个出口各自重查」导致的不一致（第三点细化方案 §二.1）。

    ``current=True``：生成期口径——取**当前抽取表**（与表格正文的既有行为一致），随后由
    写入端在同一事务内冻结快照；``False``：快照优先（读取/导出复用冻结版本）。
    """
    from .table_evidence import records_from_blocks

    blocks = (_current_chapter_table_blocks(project_id, chapter_key, subject=subject) if current
              else chapter_table_blocks(project_id, chapter_key, subject=subject))
    markdown = render_system_table_markdown(chapter_key, blocks)
    records = records_from_blocks(blocks, project_id=project_id)
    return ChapterTables(
        project_id=project_id, chapter_key=chapter_key, subject=subject,
        blocks=tuple(blocks), markdown=markdown, records=tuple(records),
    )


def system_table_markdown(project_id: str, chapter_key: str, *, subject: str,
                          current: bool = False) -> str:
    """某一章的系统表格块。返回空串表示该章不输出系统表格。"""
    if chapter_key not in TABLE_CHAPTERS:
        return ""
    blocks = (_current_chapter_table_blocks(project_id, chapter_key, subject=subject) if current
              else chapter_table_blocks(project_id, chapter_key, subject=subject))
    return render_system_table_markdown(chapter_key, blocks)


# ---------------------------------------------------------------------------
# 上传原表：uploaded_table 与双来源核对
# ---------------------------------------------------------------------------


def _cell_fact_key(table_key: str, row_label: str) -> str | None:
    from .bond_documents import row_fact_key

    return row_fact_key(table_key, row_label)


def _year_of(columns: list[str], index: int) -> str:
    """行内位置 → 年度。年度列形如「2025 年末」「2025 年度」。

    注意索引口径：行里第 0 格是科目名，与 ``columns[0]`` 对齐，所以**第 index 格
    对应的是 ``columns[index]``**。写成 ``columns[index-1]`` 会让整表错位一年，
    核对表会把每一行都判成冲突（实测踩过）。
    """
    if index <= 0 or index >= len(columns):
        return ""
    head = columns[index].strip().split(" ")[0]
    return head if head.isdigit() else ""


def uploaded_table_blocks(project_id: str, *, subject: str) -> list[TableBlock]:
    """材料原表 → ``uploaded_table`` 契约。

    只取「能稳定恢复」的表：抽取置信度不足或表头缺失的整表标记待确认，
    不让下游把它当可信数据用。
    """
    from .table_block import block_from_extracted
    from .table_store import load_tables
    from .report_source_tables import _scope_variants, source_table_id

    blocks: list[TableBlock] = []
    tables = load_tables(project_id)
    documents = {str(item["id"]): item for item in rows(
        "SELECT id,document_role FROM documents WHERE project_id=?", (project_id,),
    )}
    for table in tables:
        if not table.columns or table.row_count == 0:
            continue
        role = str(documents.get(table.document_id, {}).get("document_role") or "")
        duplicate = sum(item.table_key == table.table_key for item in tables) > 1
        for scoped_table, scope, variant in _scope_variants(table, role):
            block = block_from_extracted(
                scoped_table,
                table_id=source_table_id(table, duplicate_key=duplicate, variant=variant),
                subject=subject, scope=scope, document_id=table.document_id, source_refs=(),
            )
            blocks.append(block)
    return blocks


def reconciliation_tables(project_id: str, *, subject: str) -> list[TableBlock]:
    """档案事实与上传原表的逐科目核对。

    匹配键固定为 **主体＋报表口径＋单位＋科目＋年度**——计划里就是这么定的。
    只按「科目」匹配会把不同口径、不同单位的数字摆到一起，那是制造假冲突。
    """
    from .table_block import reconciliation_block
    profile = _profile_cells(project_id)
    if not profile:
        return []
    uploaded, period_labels = _uploaded_cells(project_id)
    if not uploaded:
        return []

    blocks: list[TableBlock] = []
    shared_dimensions = {(scope, unit) for _key, scope, unit, _period in profile} & {
        (scope, unit) for _key, scope, unit, _period in uploaded
    }
    combinations = sorted(
        {(period, scope, unit) for _key, scope, unit, period in profile if (scope, unit) in shared_dimensions}
        | {(period, scope, unit) for _key, scope, unit, period in uploaded if (scope, unit) in shared_dimensions},
        reverse=True,
    )
    for period, scope, unit in combinations:
        label = period_labels.get(period, f"{period} 年末")
        profile_cells = {
            key: cell for (key, scope_, unit_, period_), cell in profile.items()
            if period_ == period and scope_ == scope and unit_ == unit
        }
        uploaded_cells = {
            key: cell for (key, scope_, unit_, period_), cell in uploaded.items()
            if period_ == period and scope_ == scope and unit_ == unit
        }
        if not profile_cells and not uploaded_cells:
            continue
        # 键里的口径与单位是匹配条件，不是给人看的。行键换成**业务科目名**：
        # 直接截出 ``fact_key`` 会把 ``accounts_receivable`` 这类内部字段键摆进表里，
        # 成品用词校验（``wording_violations``）会判为违规，一旦这张表接进出口，
        # 整份报告会被「成品用词不合规」阻断交付。
        def _relabel(cells: dict[str, object]) -> dict[str, object]:
            return {_label(fact_key): value for fact_key, value in
                    ((key.split("|")[0], value) for key, value in cells.items())}

        blocks.append(reconciliation_block(
            table_id=f"reconcile-{period}-{hashlib.sha1(f'{scope}|{unit}'.encode()).hexdigest()[:8]}",
            title=f"档案与上传材料核对（{label}，{scope}，{unit}）",
            subject=subject, scope=scope, period=label,
            profile_cells=_relabel(profile_cells), uploaded_cells=_relabel(uploaded_cells),
            notes=(
                "注：匹配条件为同一主体、同一报表口径、同一单位、同一科目、同一年度；"
                "同口径不同值时采用值留空并进入冲突，不自动择一。",
            ),
        ))
    return blocks


def _profile_cells(project_id: str) -> dict[tuple[str, str, str, str], TableCell]:
    """档案侧：科目 → 单元格，键为 (字段, 口径, 单位, 年度)。"""
    ensure_canonical_facts(project_id)
    output: dict[tuple[str, str, str, str], TableCell] = {}
    for item in rows(
        "SELECT c.fact_key,c.period,c.value,c.unit,c.scope,c.text_value,a.source_ids AS source_ids "
        "FROM canonical_facts c LEFT JOIN fact_assertions a ON a.id=c.selected_assertion_id "
        "WHERE c.project_id=? AND c.status!='conflicted'",
        (project_id,),
    ):
        if not str(item["period"]).isdigit() or item["value"] is None:
            continue
        scope = str(item["scope"] or "unknown")
        scope = {"consolidated": "合并口径", "standalone": "母公司口径"}.get(scope, scope)
        key = (str(item["fact_key"]), scope, str(item["unit"] or ""), str(item["period"]))
        output[key] = cell_from_text(
            _format(item["value"]), unit=str(item["unit"] or ""),
            origin_type="profile_fact", source_ids=_decode(item["source_ids"]), fact_key=str(item["fact_key"]),
        )
    return output


def _uploaded_cells(project_id: str) -> tuple[dict[tuple[str, str, str, str], TableCell], dict[str, str]]:
    """上传侧：从材料原表逐格取值，键与档案侧一致，便于同键比对。"""
    from .report_source_tables import source_scope
    from .table_store import load_tables

    output: dict[tuple[str, str, str, str], TableCell] = {}
    periods: dict[str, str] = {}
    roles = {str(item["id"]): str(item["document_role"] or "") for item in rows(
        "SELECT id,document_role FROM documents WHERE project_id=?", (project_id,),
    )}
    for table in load_tables(project_id):
        if not table.columns:
            continue
        role = roles.get(table.document_id, "")
        scope = source_scope(table, role)
        subheader = next((row for row in table.rows[:3] if row and not row[0].text.strip()), [])
        monetary_units = re.findall(r"亿元|万元|元", table.unit)
        default_money_unit = monetary_units[0] if len(set(monetary_units)) == 1 else ""
        for row in table.rows:
            if len(row) < 2:
                continue
            label = row[0].text.replace("　", "").strip()
            fact_key = _cell_fact_key(table.table_key, label)
            if not fact_key:
                continue
            for index in range(1, len(row)):
                year = _year_of(list(table.columns), index)
                if not year:
                    continue
                periods.setdefault(year, f"{year} 年末")
                cell = row[index]
                if not cell.text.strip():
                    continue
                row_units = re.findall(r"亿元|万元|元|%|％|倍", label)
                header_unit = re.findall(r"亿元|万元|元|%|％|倍", table.columns[index])
                subheader_text = subheader[index].text if index < len(subheader) else ""
                if not header_unit and "占比" in subheader_text:
                    header_unit = ["%"]
                elif not header_unit and "金额" in subheader_text:
                    header_unit = [default_money_unit] if default_money_unit else []
                source_unit = (row_units[0] if len(set(row_units)) == 1 else
                               header_unit[0] if len(set(header_unit)) == 1 else
                               default_money_unit)
                if source_unit not in {"亿元", "万元", "元"}:
                    continue
                shown, shown_unit = cell.text, source_unit
                try:
                    amount = Decimal(cell.text.replace(",", "").replace("，", ""))
                    factor = {"元": Decimal("0.0001"), "万元": Decimal(1), "亿元": Decimal("10000")}[source_unit]
                    shown = _format(amount * factor)
                    shown_unit = "万元"
                except Exception:
                    pass
                key = (fact_key, scope, shown_unit, year)
                output.setdefault(key, cell_from_text(
                    shown, unit=shown_unit, origin_type="uploaded_cell",
                    locator={"page": cell.page, "bbox": list(cell.bbox) if cell.bbox else None,
                             "document_id": table.document_id, "original_raw": cell.text,
                             "original_unit": source_unit},
                    fact_key=fact_key,
                ))
    return output, periods


# ---------------------------------------------------------------------------
# 核对：表格列表、单元格矩阵、人工确认
# ---------------------------------------------------------------------------


def project_tables(project_id: str, *, subject: str) -> list[TableBlock]:
    """核对接口可见的全部表格。

    三类都列出来，缺任何一类都会让这条链路"生成了却看不见"：
    - 报告用的系统表格（``profile_facts`` 汇总）；
    - 材料原表（``uploaded_table``，逐格带页码与坐标）；
    - 档案与上传材料的核对表（``merged``，档案值／上传值／采用值并列）。

    实测踩过：前两类能被生成、能被校验，但核对接口只暴露第一类，
    于是"上传原表"与"双来源核对"在界面上根本不存在。
    """
    return [
        *financial_table_blocks(project_id, subject=subject),
        *uploaded_table_blocks(project_id, subject=subject),
        *reconciliation_tables(project_id, subject=subject),
    ]


def find_table(project_id: str, table_id: str, *, subject: str) -> TableBlock | None:
    return next(
        (block for block in project_tables(project_id, subject=subject) if block.table_id == table_id),
        None,
    )


def latest_reviews(project_id: str, table_id: str) -> dict[str, dict]:
    """单元格 → 最新一条人工核对记录。只取最新，历史仍留在表里。

    **重抽隔离靠 ``table_id`` 本身**：``source_table_id`` 把 ``parser_version`` 编进了 id
    （``uploaded-{table_key}-{parser_version}…``），升级解析版本会改变单元格身份，旧意见
    因此不会再命中新表——不需要在这里按版本再过滤一遍。详见
    ``tests/test_complete_report_tables.py::test_reparse_isolates_history_reviews_by_parser_version``。
    """
    records = rows(
        "SELECT * FROM table_cell_reviews WHERE project_id=? AND table_id=? ORDER BY created_at, rowid",
        (project_id, table_id),
    )
    latest: dict[str, dict] = {}
    for record in records:
        latest[str(record["cell_id"])] = dict(record)
    return latest


def record_review(
    project_id: str, table_id: str, cell_id: str, *, decision: str,
    corrected_raw: str = "", note: str = "", reviewer: str = "", original_raw: str = "",
) -> dict:
    """写入一条单元格核对记录。

    **只追加**：人工更正值另存一列，原始值原样保留在 ``original_raw``；
    读取时取最新一条。任何一步都不覆盖历史（计划要求「人工更正保留原始提取值
    并创建新版本，不覆盖历史」）。重抽时旧记录同样只归档不删除
    （``table_store.persist_tables`` 先写 ``document_table_history`` 再清旧行），
    且因 ``table_id`` 内含 ``parser_version``，旧意见不会按新行号套到新表上。
    """
    if decision not in {"confirmed", "corrected", "rejected"}:
        raise ValueError(f"未知的核对结论：{decision}")
    if decision == "corrected" and not str(corrected_raw).strip():
        raise ValueError("更正必须给出更正值")
    now = now_iso()
    record_id = f"tcr-{uuid.uuid4().hex[:16]}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO table_cell_reviews (id,project_id,table_id,cell_id,original_raw,corrected_raw,"
            "decision,note,reviewer,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record_id, project_id, table_id, cell_id, original_raw,
             corrected_raw if decision == "corrected" else "", decision, note, reviewer, now),
        )
    return {
        "id": record_id, "project_id": project_id, "table_id": table_id, "cell_id": cell_id,
        "original_raw": original_raw, "corrected_raw": corrected_raw if decision == "corrected" else "",
        "decision": decision, "note": note, "reviewer": reviewer, "created_at": now,
    }


def table_matrix(project_id: str, table_id: str, *, subject: str) -> dict | None:
    """核对工作区用的矩阵：列、行、单元格（含来源、定位与最新核对结论）。"""
    block = find_table(project_id, table_id, subject=subject)
    if block is None:
        return None
    reviews = latest_reviews(project_id, table_id)
    payload = block.to_dict()
    # 单元格地址 ``{table_id}:rRRcCC``（行从 1 起、列从 0 起，与渲染器的记法一致）。
    # 核对接口与更正接口都用它，前端不再自己拼坐标。
    #
    # 每格同时给出三个值，缺一个都会让消费方出错：
    # ``raw`` 原始提取值（永不改写）、``review`` 最新核对记录、``display`` 有效展示值。
    # 只给 raw + 一张 reviews 表，等于把 join 的责任推给每个消费方——漏了就显示错值。
    for row_index, row_item in enumerate(payload["rows"], start=1):
        for column_index, cell in enumerate(row_item["cells"]):
            cell_id = f"{table_id}:r{row_index:02d}c{column_index:02d}"
            cell["cell_id"] = cell_id
            review = reviews.get(cell_id)
            cell["review"] = review
            cell["display"] = (
                review["corrected_raw"] if review and review["decision"] == "corrected" and review["corrected_raw"]
                else cell["raw"]
            )
    payload["cell_reviews"] = reviews
    payload["review_summary"] = {
        "reviewed": len(reviews),
        "corrected": sum(1 for item in reviews.values() if item["decision"] == "corrected"),
        "rejected": sum(1 for item in reviews.values() if item["decision"] == "rejected"),
    }
    return payload


def original_cell_raw(block: TableBlock, cell_id: str) -> str | None:
    """按 ``table_id:rRRcCC`` 找到单元格原文；不存在返回 None。"""
    for row_index, item in enumerate(block.rows, start=1):
        for column_index, cell in enumerate(item.cells):
            if f"{block.table_id}:r{row_index:02d}c{column_index:02d}" == cell_id:
                return cell.raw
    return None


def cashflow_table_blocks(project_id: str, *, subject: str, scope: str = "合并口径") -> list[TableBlock]:
    """现金流章：已审实际的偿债来源，以及预测值与实际数的口径边界。

    预测值**单独成表**而不是与历史数并排——并排就会有人把它们当同一口径比较，
    这正是 6,500 万元与 9,000 万元那类误读的来源。历史数按报表口径**分别成表**。
    """
    ensure_canonical_facts(project_id)
    blocks = _for_each_report_scope(
        _cashflow_scope_blocks, project_id, subject=subject, fallback_label=scope,
    )
    forecast = _forecast_rows(project_id)
    if forecast:
        blocks.append(annual_series_block(
            table_id="report-cashflow-forecast", title="预测经营现金流（与上表口径不同）",
            subject=subject, scope="债券存续期预测", unit="万元",
            period_labels=["预测值"], rows=forecast, source_refs=(),
            notes=("注：预测值为前瞻性信息、未经审计，且期间与上表不同；"
                   "两者并列展示不等于可以互相替代。",),
        ))
    return blocks


def _cashflow_scope_blocks(project_id: str, *, subject: str, scope: str,
                           scope_label: str, suffix: str) -> list[TableBlock]:
    """单一口径下的偿债来源表。该口径缺失的科目/期间写"未披露"。"""
    grouped = _facts(project_id, scope) if scope else {}
    blocks: list[TableBlock] = []
    periods = _periods(grouped, CASHFLOW_SOURCES)
    if periods:
        blocks.append(annual_series_block(
            table_id=f"report-cashflow-sources{suffix}", title="偿债来源及核验表",
            subject=subject, scope=scope_label, unit="万元",
            period_labels=[f"{year} 年度" for year in periods],
            rows=_series_rows(grouped, CASHFLOW_SOURCES, periods, "万元"),
            source_refs=_sources(grouped, CASHFLOW_SOURCES),
            notes=("注：本表为已审期间的实际数。预测经营现金流为债券存续期的前瞻性信息，"
                   "期间与口径均不同，不得与本表并作同一口径比较。",),
        ))
    return blocks


def _forecast_rows(project_id: str) -> list[tuple[str, tuple[TableCell, ...]]]:
    records = rows(
        "SELECT c.value,a.source_ids AS source_ids FROM canonical_facts c "
        "LEFT JOIN fact_assertions a ON a.id=c.selected_assertion_id "
        "WHERE c.project_id=? AND c.fact_key='operating_cashflow_forecast'",
        (project_id,),
    )
    built = []
    for item in records:
        if item["value"] is None:
            continue
        built.append((
            "预测经营活动现金流量净额",
            (cell_from_text(_format(item["value"]), unit="万元", origin_type="profile_fact",
                            source_ids=_decode(item["source_ids"]),
                            fact_key="operating_cashflow_forecast"),),
        ))
    return built


def debt_table_blocks(project_id: str, *, subject: str, scope: str = "合并口径") -> list[TableBlock]:
    """债务章：分类结构表与到期结构表。

    发行规模与存量债务**分表**：混在一张表里会让人把"本期发行 2 亿"读成"债务增加 2 亿"。
    存量债务按报表口径**分别成表**。
    """
    ensure_canonical_facts(project_id)
    return _for_each_report_scope(
        _debt_scope_blocks, project_id, subject=subject, fallback_label=scope,
    )


def _debt_scope_blocks(project_id: str, *, subject: str, scope: str,
                       scope_label: str, suffix: str) -> list[TableBlock]:
    """单一口径下的债务结构表。该口径缺失的科目/期间写"未披露"。"""
    grouped = _facts(project_id, scope) if scope else {}
    blocks: list[TableBlock] = []
    periods = _periods(grouped, DEBT_STRUCTURE)
    if periods:
        blocks.append(annual_series_block(
            table_id=f"report-debt-structure{suffix}", title="有息负债分类表",
            subject=subject, scope=scope_label, unit="万元",
            period_labels=[f"{year} 年末" for year in periods],
            rows=_series_rows(grouped, DEBT_STRUCTURE, periods, "万元"),
            source_refs=_sources(grouped, DEBT_STRUCTURE),
            notes=("注：本表只列存量债务，不含本期拟发行规模；两者口径不同，不得相加。",),
        ))
    maturity_periods = _periods(grouped, DEBT_MATURITY)
    if maturity_periods:
        blocks.append(annual_series_block(
            table_id=f"report-debt-maturity{suffix}", title="到期结构表",
            subject=subject, scope=scope_label, unit="万元",
            period_labels=[f"{year} 年末" for year in maturity_periods],
            rows=_series_rows(grouped, DEBT_MATURITY, maturity_periods, "万元"),
            source_refs=_sources(grouped, DEBT_MATURITY),
            notes=("注：到期债务为披露口径，不计入有息负债加总；明细台账与还款计划未随材料提供。",),
        ))
    return blocks


def rating_table_blocks(project_id: str, *, subject: str, scope: str = "合并口径") -> list[TableBlock]:
    """评级章：评级结论对照表与评级口径差异表。

    评级口径与主体档案口径**分列**，不用一个数覆盖另一个——差异本身就是需核验的信息。
    """
    from .table_block import reconciliation_block

    ensure_canonical_facts(project_id)
    # 评级章的"主体档案口径"一侧取**项目实际存在的报表口径**（合并优先），不按科目
    # 择优；项目没有报表口径时该侧留空，不并入非报表口径的资料。
    scopes = report_scopes(project_id)
    profile_scope = scopes[0] if scopes else ""
    grouped = _facts(project_id, profile_scope) if profile_scope else {}
    blocks: list[TableBlock] = []
    conclusions = _rating_conclusions(project_id)
    if conclusions:
        blocks.append(annual_series_block(
            table_id="report-rating-conclusion", title="评级结论对照表",
            subject=subject, scope="评级口径", unit="", period_labels=["评级结果"],
            rows=conclusions, source_refs=(),
            notes=("注：评级结论为外部信用评价文件的意见，不等于担保机构的独立判断。",),
        ))
    rating_cells = _rating_cells(project_id)
    latest = (_periods(grouped, RATING_SOURCE_ITEMS) or ["2025"])[0]
    profile_cells = {
        label: cell_from_text(
            _format(grouped[key][latest]["value"]), unit="万元", origin_type="profile_fact",
            source_ids=grouped[key][latest].get("source_ids") or (), fact_key=key,
        )
        for key, label in RATING_SOURCE_ITEMS
        if grouped.get(key, {}).get(latest, {}).get("value") is not None
    }
    if profile_cells and rating_cells:
        blocks.append(reconciliation_block(
            table_id="report-rating-reconciliation", title="评级口径与主体档案口径对照",
            subject=subject, scope=_scope_label(profile_scope) if profile_scope else scope,
            period=f"{latest} 年度",
            profile_cells={label: profile_cells.get(label, TableCell(raw="—", status="not_applicable"))
                           for _, label in RATING_SOURCE_ITEMS},
            uploaded_cells={label: rating_cells.get(label, TableCell(raw="—", status="not_applicable"))
                            for _, label in RATING_SOURCE_ITEMS},
            left_label="主体档案口径", right_label="评级口径",
            notes=("注：两列口径不同，差异本身即为需核验事项；系统不自动择一采信。",),
        ))
    return blocks


def _rating_conclusions(project_id: str) -> list[tuple[str, tuple[TableCell, ...]]]:
    from .facts import strip_canonical_prefix

    output = []
    # 主体评级、债项评级、展望、**评级机构与评级时间**同属一张结论表（修订稿 §2.4 要求
    # 机构与日期也分别绑定对象、原文和来源）。日期缺失就留空，绝不用发行日/报表日替代。
    for key, label in (
        ("issuer_rating", "主体信用等级"), ("bond_rating", "债项信用等级"),
        ("rating_outlook", "评级展望"), ("rating_agency", "评级机构"),
        ("rating_date", "评级时间"),
    ):
        record = rows(
            "SELECT c.text_value,a.source_ids AS source_ids FROM canonical_facts c "
            "LEFT JOIN fact_assertions a ON a.id=c.selected_assertion_id "
            "WHERE c.project_id=? AND c.fact_key=?" + ENTITY_FACT_SCOPE_ORDER + " LIMIT 1",
            (project_id, key),
        )
        if not record or not str(record[0]["text_value"] or "").strip():
            continue
        # 内部签名前缀（text:/rating:/outlook:/agency:/date:）一律剥掉——它们只用于让不同
        # 写法的同一个值比较相等，**不得印进成品**（实测漏出去过「主体评级为rating:AA-」）。
        text = strip_canonical_prefix(record[0]["text_value"])
        output.append((label, (cell_from_text(text, source_ids=_decode(record[0]["source_ids"]), fact_key=key),)))
    output.extend(_tranche_rating_rows(project_id))
    return output


def _tranche_rating_rows(project_id: str) -> list[tuple[str, tuple[TableCell, ...]]]:
    """各品种债项评级行：**只填有原文支撑的品种**，其余显示"未披露"。

    修订稿 §2.4：品种评级必须有对应原文，**禁止把一个整体 ``bond_rating`` 复制到所有品种**。
    原文来源与页码写进单元格的来源与定位；单元格沿用已有的来源类型，不新增表级来源模式。
    """
    from .bond_tranches import tranche_ratings_from_text

    names = [
        str(item["tranche_name"] or "").strip()
        for item in rows(
            "SELECT tranche_name FROM bond_tranches WHERE project_id=? ORDER BY tranche_key",
            (project_id,),
        )
        if str(item["tranche_name"] or "").strip()
    ]
    if not names:
        return []
    # 逐份评级材料找，命中的那一份连同页码一起记为来源——这样"绑定原文和来源"是可回查的，
    # 而不是笼统挂一整份文件。
    found: dict[str, tuple[str, str, int]] = {}
    for item in rows(
        "SELECT s.id,s.page,s.text FROM sources s "
        "JOIN documents d ON d.id=s.document_id AND d.project_id=s.project_id "
        "WHERE s.project_id=? AND d.document_role='rating_report' ORDER BY s.page,s.id",
        (project_id,),
    ):
        text = str(item["text"] or "")
        if not text:
            continue
        hits = tranche_ratings_from_text(text, [name for name in names if name not in found])
        for name, value in hits.items():
            found[name] = (value, str(item["id"]), int(item["page"] or 0))
    output: list[tuple[str, tuple[TableCell, ...]]] = []
    for name in names:
        label = f"债项信用等级（{name}）"
        if name in found:
            value, source_id, page = found[name]
            output.append((label, (cell_from_text(
                value, origin_type="uploaded_cell", source_ids=(source_id,),
                locator={"page": page}, fact_key="bond_rating",
            ),)))
        else:
            # 没有对应原文就写"未披露"，不用整体债项评级补位。
            output.append((label, (TableCell(raw="未披露", status="not_applicable"),)))
    return output


def _rating_cells(project_id: str) -> dict[str, TableCell]:
    output: dict[str, TableCell] = {}
    for key, label in (("rating_revenue", "营业收入"), ("rating_total_debt", "有息负债")):
        record = rows(
            "SELECT c.value,c.unit,a.source_ids AS source_ids FROM canonical_facts c "
            "LEFT JOIN fact_assertions a ON a.id=c.selected_assertion_id "
            "WHERE c.project_id=? AND c.fact_key=?" + ENTITY_FACT_SCOPE_ORDER + " LIMIT 1",
            (project_id, key),
        )
        if record and record[0]["value"] is not None:
            output[label] = cell_from_text(
                _format(record[0]["value"]), unit=str(record[0]["unit"] or "万元"),
                origin_type="profile_fact", source_ids=_decode(record[0]["source_ids"]), fact_key=key,
            )
    return output


def blocked_reason(project_id: str, *, subject: str) -> str:
    """Any frozen system table that is not traceable blocks final delivery."""
    for chapter in TABLE_CHAPTERS:
        for block in chapter_table_blocks(project_id, chapter, subject=subject):
            status, _ = validate_table_block(block)
            if block.validation_status != "passed" or status != "passed":
                return f"{block.title}：{block.validation_status if block.validation_status != 'passed' else status}"
    return ""
