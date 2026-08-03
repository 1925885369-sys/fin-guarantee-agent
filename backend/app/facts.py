from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .database import connect, json_dump, json_load, rows


@dataclass(frozen=True)
class FactDefinition:
    label: str
    category: str
    aliases: tuple[str, ...]
    unit_kind: str = "amount"


FACT_REGISTRY: dict[str, FactDefinition] = {
    "revenue": FactDefinition("营业收入", "财务", ("财务报表营业收入", "主营业务收入", "营业收入")),
    "tax_revenue": FactDefinition("纳税申报收入", "税务", ("增值税申报收入", "纳税申报收入", "开票收入")),
    "operating_inflow": FactDefinition("经营性流入", "流水", ("银行经营性流入", "经营性流入", "销售回款")),
    "net_profit": FactDefinition("净利润", "财务", ("净利润",)),
    "operating_cash_flow": FactDefinition("经营现金流净额", "财务", ("经营活动现金流量净额", "经营活动现金流净额", "经营现金流净额")),
    "accounts_receivable": FactDefinition("应收账款", "财务", ("应收账款余额", "应收账款")),
    "total_assets": FactDefinition("资产总额", "财务", ("资产总计", "资产总额")),
    "total_liabilities": FactDefinition("负债总额", "财务", ("负债合计", "负债总额")),
    "current_assets": FactDefinition("流动资产", "财务", ("流动资产合计", "流动资产")),
    "current_liabilities": FactDefinition("流动负债", "财务", ("流动负债合计", "流动负债")),
    "interest_bearing_debt": FactDefinition("有息负债", "债务", ("有息负债余额", "现有有息负债", "有息负债")),
    "debt_due_12m": FactDefinition("未来12个月到期债务", "债务", ("一年内到期债务", "未来12个月到期债务", "未来12个月需偿还或续作", "一年内集中到期合计")),
    "top_customer_share": FactDefinition("第一大客户占比", "经营", ("第一大客户占比", "第一大客户", "最大客户占比"), "percent"),
    "related_unsupported_amount": FactDefinition("无合同关联方往来", "关联方", ("无合同关联方往来", "未提供对应合同", "无商业背景的关联方往来")),
    "supporting_contract_amount": FactDefinition("已提供用途合同", "用途", ("用途合同合计", "合同金额合计", "已提供合同金额", "合计")),
    "collateral_discounted_value": FactDefinition("折后抵押价值", "抵押物", ("折后抵押价值", "折扣后价值", "折后覆盖额", "按70%折扣计算覆盖额")),
    "collateral_valuation_age_months": FactDefinition("抵押物估值账龄", "抵押物", ("评估报告已出具", "评估报告出具于", "估值报告距今"), "months"),
    "external_guarantee": FactDefinition("对外担保", "担保", ("对外担保余额", "连带责任保证", "对外担保")),
    "overdue_receivables": FactDefinition("一年以上应收账款", "经营", ("一年以上应收账款", "长期逾期应收")),
    "controller": FactDefinition("实际控制人", "主体", ("实际控制人为", "实际控制人"), "text"),
    "main_business": FactDefinition("主营业务", "经营", ("主营业务", "主营"), "text"),
}

NAME_TO_KEY = {definition.label: key for key, definition in FACT_REGISTRY.items()}
EXPECTED_MATERIAL_GROUPS = {
    "申请与主体": {"融资申请", "企业信息"},
    "财务报表": {"财务报表"},
    "流水与纳税": {"银行流水", "纳税资料"},
    "经营与应收": {"合同订单", "应收账款"},
    "债务与担保": {"负债担保"},
    "抵押与访谈": {"抵押物", "访谈纪要"},
}
MATERIAL_KEYWORDS = {
    "申请与主体": ("申请金额", "实际控制人", "营业执照"),
    "财务报表": ("营业收入", "资产总额", "资产总计"),
    "流水与纳税": ("经营性流入", "银行流水", "纳税申报", "增值税申报"),
    "经营与应收": ("客户", "供应商", "应收账款", "采购合同"),
    "债务与担保": ("有息负债", "到期债务", "对外担保", "连带责任保证"),
    "抵押与访谈": ("抵押", "评估报告", "现场访谈", "访谈"),
}


def canonical_key(name: str) -> str:
    if name in FACT_REGISTRY:
        return name
    if name in NAME_TO_KEY:
        return NAME_TO_KEY[name]
    compact = re.sub(r"\s+", "", name)
    for key, definition in FACT_REGISTRY.items():
        if any(alias in compact for alias in definition.aliases):
            return key
    return ""


def normalize_period(raw: str) -> str:
    match = re.search(r"20\d{2}", str(raw))
    return match.group(0) if match else ""


def normalize_numeric(value: float, unit: str, kind: str) -> tuple[float, str]:
    if kind == "percent":
        return float(value), "%"
    if kind == "months":
        return float(value), "个月"
    multiplier = 1.0
    if "亿元" in unit:
        multiplier = 10000
    elif "千元" in unit:
        multiplier = 0.1
    elif unit == "元" or ("元" in unit and "万元" not in unit):
        multiplier = 0.0001
    return round(float(value) * multiplier, 6), "万元"


def _numbers(text: str) -> list[float]:
    return [float(item.replace(",", "")) for item in re.findall(r"(?<!\d)(-?\d[\d,]*(?:\.\d+)?)(?!\d)", text)]


def _unit(text: str, kind: str) -> str:
    if kind == "percent":
        return "%"
    if kind == "months":
        return "个月"
    for unit in ("亿元", "万元", "千元", "元"):
        if unit in text:
            return unit
    return "万元"


def deterministic_extract(source_rows: list[dict]) -> list[dict]:
    """Best-effort extraction for labelled rows and common Chinese due-diligence prose."""
    headers: dict[tuple[str, str], list[str]] = {}
    for source in source_rows:
        if source.get("locator_type") != "xlsx_range":
            continue
        sheet = str(source.get("locator", "")).split("!", 1)[0]
        years = re.findall(r"20\d{2}", source["text"])
        if len(years) >= 2:
            headers[(source["document_id"], sheet)] = years

    output: list[dict] = []
    seen: set[tuple[str, str, float | None, str]] = set()
    for source in source_rows:
        text = source["text"]
        compact = re.sub(r"\s+", "", text)
        for fact_key, definition in FACT_REGISTRY.items():
            alias = next((item for item in definition.aliases if item in compact), None)
            if not alias:
                continue
            if definition.unit_kind == "text":
                match = re.search(re.escape(alias) + r"[：:为是]?([^，。；;|]{2,80})", compact)
                if not match:
                    continue
                item = _fact_dict(fact_key, "", None, "", match.group(1), match.group(1), source["id"], 0.78)
                key = (fact_key, "", None, item["text_value"])
                if key not in seen:
                    output.append(item)
                    seen.add(key)
                continue

            sheet = str(source.get("locator", "")).split("!", 1)[0]
            years = headers.get((source["document_id"], sheet), []) if source.get("locator_type") == "xlsx_range" else []
            numbers = _numbers(text)
            if not years:
                years_in_text = re.findall(r"20\d{2}", text)
                numbers = [number for number in numbers if int(number) not in {int(year) for year in years_in_text}]
            if not numbers:
                continue
            unit = _unit(text, definition.unit_kind)

            if definition.unit_kind == "percent":
                percent_match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", text)
                value = float(percent_match.group(1)) if percent_match else numbers[-1]
                periods_values = [(normalize_period(text), value)]
            elif definition.unit_kind == "months":
                month_match = re.search(r"(\d+(?:\.\d+)?)\s*个月", text)
                value = float(month_match.group(1)) if month_match else numbers[-1]
                periods_values = [(normalize_period(text) or str(date.today().year), value)]
            elif years and len(numbers) >= len(years):
                periods_values = list(zip(years, numbers[-len(years):]))
            else:
                value = numbers[-1]
                if fact_key == "supporting_contract_amount" and "合计" in compact:
                    value = numbers[-1]
                periods_values = [(normalize_period(text), value)]

            for period, raw_number in periods_values:
                value, normalized_unit = normalize_numeric(raw_number, unit, definition.unit_kind)
                item = _fact_dict(fact_key, period, value, normalized_unit, "", str(raw_number), source["id"], 0.86)
                key = (fact_key, period, value, "")
                if key not in seen:
                    output.append(item)
                    seen.add(key)

    if not any(item["fact_key"] == "overdue_receivables" for item in output):
        aging_groups: dict[tuple[str, str], list[tuple[float, str]]] = {}
        for source in source_rows:
            if source.get("locator_type") != "xlsx_range":
                continue
            label, separator, remainder = source["text"].partition("|")
            label = re.sub(r"\s+", "", label)
            if not separator or not (
                re.fullmatch(r"1[-–—~至]2年", label)
                or re.fullmatch(r"[2-9]\d*年以上", label)
            ):
                continue
            amount_cell = remainder.split("|", 1)[0]
            amounts = _numbers(amount_cell)
            if not amounts:
                continue
            sheet = str(source.get("locator", "")).split("!", 1)[0]
            aging_groups.setdefault((source["document_id"], sheet), []).append((amounts[0], source["id"]))

        for values in aging_groups.values():
            raw_total = sum(value for value, _ in values)
            total, unit = normalize_numeric(raw_total, "万元", "amount")
            item = _fact_dict(
                "overdue_receivables", "", total, unit, "", " + ".join(str(value) for value, _ in values),
                values[0][1], 0.95,
            )
            item["source_ids"] = [source_id for _, source_id in values]
            output.append(item)
    return output


def _fact_dict(
    fact_key: str, period: str, value: float | None, unit: str, text_value: str,
    raw_value: str, source_id: str, confidence: float,
) -> dict:
    definition = FACT_REGISTRY[fact_key]
    return {
        "id": str(uuid.uuid4()), "category": definition.category, "fact_key": fact_key,
        "name": definition.label, "period": period, "value": value, "unit": unit,
        "text_value": text_value, "raw_value": raw_value, "source_ids": [source_id],
        "confidence": confidence, "extraction_method": "deterministic", "review_status": "unreviewed",
    }


def merge_facts(*fact_groups: Iterable[dict]) -> list[dict]:
    chosen: dict[tuple[str, str], dict] = {}
    for group in fact_groups:
        for fact in group:
            key = (fact["fact_key"], fact.get("period", ""))
            current = chosen.get(key)
            rank = (fact.get("review_status") == "human_verified", fact.get("confidence", 0))
            current_rank = ((current or {}).get("review_status") == "human_verified", (current or {}).get("confidence", 0))
            if current is None or rank > current_rank:
                chosen[key] = fact
    return list(chosen.values())


def replace_extracted_facts(project_id: str, extracted: list[dict]) -> None:
    existing = rows("SELECT * FROM facts WHERE project_id=?", (project_id,))
    human = [item for item in existing if item.get("review_status") == "human_verified"]
    merged = merge_facts(extracted, human)
    with connect() as conn:
        conn.execute("DELETE FROM facts WHERE project_id=? AND review_status!='human_verified'", (project_id,))
        for fact in merged:
            if fact.get("review_status") == "human_verified":
                continue
            conn.execute(
                "INSERT INTO facts (id,project_id,category,name,period,value,unit,text_value,source_ids,confidence,fact_key,raw_value,extraction_method,review_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fact.get("id", str(uuid.uuid4())), project_id, fact["category"], fact["name"], fact.get("period", ""),
                 fact.get("value"), fact.get("unit", ""), fact.get("text_value", ""), json_dump(fact.get("source_ids", [])),
                 fact.get("confidence", 0.8), fact["fact_key"], fact.get("raw_value", ""),
                 fact.get("extraction_method", "model"), fact.get("review_status", "unreviewed")),
            )


def decode_facts(items: list[dict]) -> list[dict]:
    for item in items:
        item["source_ids"] = json_load(item["source_ids"])
    return items


def material_inventory(project_id: str) -> dict:
    documents = rows("SELECT * FROM documents WHERE project_id=? ORDER BY created_at", (project_id,))
    kinds = {item["kind"] for item in documents if item["parse_status"] == "parsed"}
    source_text = " ".join(item["text"] for item in rows("SELECT text FROM sources WHERE project_id=?", (project_id,)))
    def covered(group: str, expected: set[str]) -> bool:
        if kinds & expected:
            return True
        if group == "抵押与访谈" and any(phrase in source_text for phrase in ("未提供抵押", "无抵押物", "缺少抵押")):
            return any(phrase in source_text for phrase in ("折后抵押价值", "评估价值", "现场访谈", "访谈确认"))
        return any(keyword in source_text for keyword in MATERIAL_KEYWORDS[group])

    gaps = [
        {"group": group, "expectedKinds": sorted(expected), "status": "missing"}
        for group, expected in EXPECTED_MATERIAL_GROUPS.items()
        if not covered(group, expected)
    ]
    failed = sum(item["parse_status"] != "parsed" for item in documents)
    readiness = "ready" if documents and not gaps and not failed else "partial" if documents else "empty"
    return {"documents": documents, "gaps": gaps, "readiness": readiness}
