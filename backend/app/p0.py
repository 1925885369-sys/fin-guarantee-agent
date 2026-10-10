from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from typing import Any

from .database import connect, json_dump, json_load, now_iso, row, rows
from .facts import FACT_REGISTRY


POINT_IN_TIME_KEYS = {
    "accounts_receivable", "inventory", "prepayments", "other_receivables", "cash", "fixed_assets",
    "total_assets", "total_liabilities", "equity", "current_assets", "current_liabilities",
    "interest_bearing_debt", "short_term_debt", "long_term_debt", "debt_due_12m", "credit_limit",
    "utilized_credit", "overdue_debt", "external_guarantee", "contingent_liabilities",
    "collateral_appraised_value", "collateral_discounted_value", "prior_lien_amount",
}
SYSTEM_SECTIONS = {
    "disclaimer", "tasks", "evidence", "conclusion", "executive_summary", "guarantee_structure",
    # These bond chapters are deterministic presentation layers.  Their prose
    # includes scope, material-status and process-boundary statements that are
    # not expected to be repeated verbatim in a source excerpt.
    "scope", "proceeds", "investor_protection",
    "counter_guarantee", "mitigation",
}


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def default_scope(category: str) -> str:
    if category in {"主体", "主体治理"}:
        return "entity"
    if category in {"融资申请", "用途", "担保方案"}:
        return "transaction"
    return "standalone"


def period_type(fact_key: str, period: str) -> str:
    if not period:
        return "undated"
    return "point_in_time" if fact_key in POINT_IN_TIME_KEYS else "duration"


def borrower_entity(project_id: str) -> dict:
    project = row("SELECT id,company_name FROM projects WHERE id=?", (project_id,))
    if not project:
        raise ValueError("项目不存在")
    entity_id = f"entity-{project_id}-borrower"
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) "
            "VALUES (?,?,?,?,1,'confirmed','{}',?,?) ON CONFLICT(project_id,entity_type,name) DO UPDATE SET is_borrower=1,updated_at=excluded.updated_at",
            (entity_id, project_id, "enterprise", project["company_name"], now, now),
        )
    return row("SELECT * FROM project_entities WHERE project_id=? AND is_borrower=1", (project_id,)) or {}


def ensure_entity(project_id: str, name: str, entity_type: str = "other") -> dict:
    borrower = borrower_entity(project_id)
    if not name or name == borrower["name"]:
        return borrower
    entity_id = _stable_id("entity", project_id, entity_type, name)
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) VALUES (?,?,?,?,0,'unreviewed','{}',?,?)",
            (entity_id, project_id, entity_type, name, now_iso(), now_iso()),
        )
    return row("SELECT * FROM project_entities WHERE project_id=? AND entity_type=? AND name=?", (project_id, entity_type, name)) or {}


def sync_project_entities(project_id: str) -> list[dict]:
    borrower = borrower_entity(project_id)
    borrower_id, borrower_name = borrower["id"], borrower["name"]
    with connect() as conn:
        for table in ("facts", "fact_assertions", "canonical_facts"):
            items = conn.execute(
                f"SELECT id,category,fact_key,period,subject,subject_id,scope,period_type FROM {table} WHERE project_id=?",
                (project_id,),
            ).fetchall()
            for item in items:
                subject = item["subject"] or borrower_name
                subject_id = item["subject_id"]
                if not subject_id:
                    if subject == borrower_name:
                        subject_id = borrower_id
                    else:
                        subject_id = _stable_id("entity", project_id, "other", subject)
                        conn.execute(
                            "INSERT OR IGNORE INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) VALUES (?,?,?,?,0,'unreviewed','{}',?,?)",
                            (subject_id, project_id, "other", subject, now_iso(), now_iso()),
                        )
                conn.execute(
                    f"UPDATE {table} SET subject=?,subject_id=?,scope=?,period_type=? WHERE id=?",
                    (subject, subject_id, item["scope"] if item["scope"] != "unknown" else default_scope(item["category"]),
                     item["period_type"] if item["period_type"] != "unknown" else period_type(item["fact_key"], item["period"]), item["id"]),
                )

        controller = conn.execute(
            "SELECT text_value FROM canonical_facts WHERE project_id=? AND fact_key='controller' AND selected_assertion_id IS NOT NULL ORDER BY period DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if controller and controller["text_value"]:
            entity_id = _stable_id("entity", project_id, "person", controller["text_value"])
            conn.execute(
                "INSERT INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) VALUES (?,?,?,?,0,'unreviewed',?, ?,?) "
                "ON CONFLICT(project_id,entity_type,name) DO UPDATE SET attributes_json=excluded.attributes_json,updated_at=excluded.updated_at",
                (entity_id, project_id, "person", controller["text_value"], json_dump({"role": "controller"}), now_iso(), now_iso()),
            )
        related = conn.execute(
            "SELECT text_value FROM canonical_facts WHERE project_id=? AND fact_key='related_parties' AND selected_assertion_id IS NOT NULL",
            (project_id,),
        ).fetchall()
        for fact in related:
            for name in re.split(r"[、,，;；]", fact["text_value"] or ""):
                name = name.strip()
                if len(name) < 2:
                    continue
                entity_id = _stable_id("entity", project_id, "related_enterprise", name)
                conn.execute(
                    "INSERT OR IGNORE INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) VALUES (?,?,?,?,0,'unreviewed',?, ?,?)",
                    (entity_id, project_id, "related_enterprise", name, json_dump({"relation": "related_party"}), now_iso(), now_iso()),
                )
    return decode_entities(rows("SELECT * FROM project_entities WHERE project_id=? ORDER BY is_borrower DESC,entity_type,name", (project_id,)))


def decode_entities(items: list[dict]) -> list[dict]:
    for item in items:
        item["attributes_json"] = json_load(item.get("attributes_json", "{}"))
        item["is_borrower"] = bool(item["is_borrower"])
    return items


def _sources(facts: list[dict]) -> list[str]:
    output: list[str] = []
    for fact in facts:
        value = fact.get("source_ids", [])
        value = json_load(value) if isinstance(value, str) else value
        output.extend(value)
    return list(dict.fromkeys(output))


def _tie_out(
    project_id: str, subject_id: str, scope: str, period: str, check_key: str, title: str,
    formula: str, facts: list[dict], left: float | None, right: float | None, tolerance: float,
    policy_version: str,
) -> dict:
    if left is None or right is None:
        status, difference, rationale = "insufficient", None, "缺少勾稽所需科目，未以零值替代。"
    else:
        difference = round(left - right, 6)
        status = "passed" if abs(difference) <= tolerance else "warning"
        rationale = f"左侧{left:.2f}，右侧{right:.2f}，差额{difference:.2f}，容差{tolerance:.2f}。"
    return {
        "id": _stable_id("tie", project_id, subject_id, scope, period, check_key), "project_id": project_id,
        "subject_id": subject_id, "scope": scope, "period": period, "check_key": check_key, "title": title,
        "status": status, "formula": formula, "input_fact_ids": [item["id"] for item in facts],
        "source_ids": _sources(facts), "left_value": left, "right_value": right, "difference": difference,
        "tolerance": tolerance, "rationale": rationale, "policy_version": policy_version,
    }


def compute_financial_tie_outs(project_id: str, policy_version: str = "") -> list[dict]:
    from .professional import canonical_fact_rows

    facts = [item for item in canonical_fact_rows(project_id) if item.get("value") is not None]
    grouped: dict[tuple[str, str, str], dict[str, dict]] = defaultdict(dict)
    for fact in facts:
        if not fact.get("period") or fact.get("scope") in {"entity", "transaction"}:
            continue
        grouped[(fact["subject_id"], fact["scope"], fact["period"])][fact["fact_key"]] = fact
    output: list[dict] = []
    for (subject_id, scope, period), values in grouped.items():
        assets, liabilities, equity = values.get("total_assets"), values.get("total_liabilities"), values.get("equity")
        if not any((assets, liabilities, equity)):
            continue
        balance_facts = [item for item in (assets, liabilities, equity) if item]
        asset_value = assets["value"] if assets else None
        right_value = liabilities["value"] + equity["value"] if liabilities and equity else None
        output.append(_tie_out(project_id, subject_id, scope, period, "balance_equation", "资产负债表恒等式",
                               "资产总额=负债总额+所有者权益", balance_facts, asset_value, right_value,
                               max(abs(asset_value or 0) * 0.005, 1.0), policy_version))

        for key, child_key, parent_key, title in (
            ("current_assets_total", "current_assets", "total_assets", "流动资产不超过资产总额"),
            ("current_liabilities_total", "current_liabilities", "total_liabilities", "流动负债不超过负债总额"),
            ("interest_debt_total", "interest_bearing_debt", "total_liabilities", "有息负债不超过负债总额"),
        ):
            child, parent = values.get(child_key), values.get(parent_key)
            related = [item for item in (child, parent) if item]
            left = child["value"] if child else None
            right = parent["value"] if parent else None
            check = _tie_out(project_id, subject_id, scope, period, key, title, f"{FACT_REGISTRY[child_key].label}<={FACT_REGISTRY[parent_key].label}",
                             related, left, right, 0.0, policy_version)
            if left is not None and right is not None:
                check["status"] = "passed" if left <= right else "warning"
                check["difference"] = round(left - right, 6)
                check["rationale"] = f"{FACT_REGISTRY[child_key].label}{left:.2f}，{FACT_REGISTRY[parent_key].label}{right:.2f}。"
            output.append(check)
    with connect() as conn:
        conn.execute("DELETE FROM financial_tie_outs WHERE project_id=?", (project_id,))
        for item in output:
            conn.execute(
                "INSERT INTO financial_tie_outs (id,project_id,subject_id,scope,period,check_key,title,status,formula,input_fact_ids,source_ids,left_value,right_value,difference,tolerance,rationale,policy_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["id"], project_id, item["subject_id"], item["scope"], item["period"], item["check_key"], item["title"],
                 item["status"], item["formula"], json_dump(item["input_fact_ids"]), json_dump(item["source_ids"]),
                 item["left_value"], item["right_value"], item["difference"], item["tolerance"], item["rationale"], policy_version),
            )
    return decode_tie_outs(rows("SELECT * FROM financial_tie_outs WHERE project_id=? ORDER BY period DESC,check_key", (project_id,)))


def decode_tie_outs(items: list[dict]) -> list[dict]:
    for item in items:
        item["input_fact_ids"] = json_load(item["input_fact_ids"])
        item["source_ids"] = json_load(item["source_ids"])
    return items


def _claim_lines(content: str) -> list[str]:
    lines: list[str] = []
    for raw in re.split(r"\n+", content):
        cleaned = re.sub(r"^[#>*\-\d.\s]+", "", raw).strip()
        if not cleaned or cleaned.startswith("|") or len(cleaned) < 4:
            continue
        lines.extend(item.strip() for item in re.split(r"(?<=[。！？])", cleaned) if len(item.strip()) >= 4)
    return lines


# 主张类型中**不参与证据支持统计**的类别：叙述（narrative）、审查要求/核验动作
# （requirement）、来源边界/使用限制声明（boundary）。三者都只表达"行文、应做什么、
# 效力边界"，不主张任何业务字段的取值；与 narrative 同处理：状态给 ``not_applicable``，
# 既不计入 factual 分母，也不产生 unsupported（修订稿 §3.1：审查要求、核验动作、来源边界
# 与业务事实分别判定）。**不允许**把这三类算成 factual，否则要求性/边界性表述会被当成
# "无证据事实"拉低支持率、甚至触发整章回退。
NON_FACTUAL_CLAIM_TYPES = {"narrative", "requirement", "boundary"}

# 来源边界 / 使用限制声明：说明披露材料的效力边界，不对业务字段取值
# （"本次披露未经独立核验""不构成…""仅作为披露信息""以原件为准"）。
_BOUNDARY_PHRASES = (
    "不等同于", "不构成", "不得替代", "不应替代", "仅反映", "不等于", "不代表",
    "未经独立核验", "未经核实", "未经审计核验", "仅作为披露", "仅供披露",
    "以原件为准", "以正式文件为准", "不作为", "供参考",
)

# 审查要求 / 建议核验动作：表述"应当做什么"，不主张业务事实取值。刻意用**多字词组**
# 而非单字"应/需"，避免把"对应收账款核对无误"这类含"应"的陈述误判为要求。
_REQUIREMENT_RE = re.compile(
    r"应当|建议|务必|须"
    r"|应与|应将|应在|应于|应由|应按|应予|应以|应对|应分|应单|应重"
    r"|应核对|应核实|应核查|应取得|应提供|应补充|应函证|应说明|应披露|应列示|应呈示|应验算|应复核"
    r"|需核对|需核实|需核查|需取得|需提供|需补充|需函证|需说明|需进一步|需重新"
    r"|请提供|请补充|请说明|要求提供|要求补充"
)


def _claim_type(text: str) -> str:
    """把句子分成 factual / requirement / boundary / risk_judgment / narrative。

    关键取舍：requirement 与 boundary **只在句子不含可核验数值时**才成立（``_numbers``
    为空时，年份与资料/页码引用已被 ``_numbers`` 自身过滤）。一旦句子带上业务数值
    （如"2025 年末有息负债为 1,234 万元"），就落回下面的数字分支按业务事实照常判定——
    这样才满足"带数值的业务陈述仍必须走原判定，无证据仍判 unsupported"，不会顺手把
    事实放过去。原实现把边界词无条件判成 narrative，等于让带数值的边界句逃过核验，
    这里一并收紧。
    """
    if not _numbers(text):
        if any(phrase in text for phrase in _BOUNDARY_PHRASES):
            return "boundary"
        if _REQUIREMENT_RE.search(text):
            return "requirement"
    if any(word in text for word in (
        "风险", "异常", "冲突", "不足", "覆盖", "偿债", "核验", "阈值", "同比", "增速",
        "差异率", "周转", "占比", "可能性", "影响程度", "判断依据",
    )):
        return "risk_judgment"
    if "可能原因包括" in text:
        return "narrative"
    if re.search(r"\d", text):
        return "numeric_fact"
    if any(definition.label in text for definition in FACT_REGISTRY.values()):
        return "fact"
    return "narrative"


def _numbers(text: str) -> list[float]:
    from .delivery_wording import strip_references

    # 资料编号与页码是引用定位，不是被主张的数据值。
    output = []
    for raw in re.findall(r"(?<![A-Za-z])-?\d+(?:\.\d+)?", strip_references(text).replace(",", "")):
        value = float(raw)
        if value.is_integer() and 1900 <= value <= 2100:
            continue
        output.append(value)
    return output


def _number_matches(value: float, candidates: list[float]) -> bool:
    return any(abs(value - candidate) <= max(0.51, abs(value) * 0.005) for candidate in candidates)


def _evidence_context(project_id: str, source_ids: list[str]) -> tuple[list[float], set[str]]:
    if not source_ids:
        return [], set()
    source_set = set(source_ids)
    values: list[float] = []
    labels: set[str] = set()
    for assertion in rows("SELECT fact_key,value,source_ids FROM fact_assertions WHERE project_id=?", (project_id,)):
        if source_set.intersection(json_load(assertion["source_ids"])):
            definition = FACT_REGISTRY.get(assertion["fact_key"])
            if definition:
                labels.add(definition.label)
            if assertion["value"] is not None:
                values.append(float(assertion["value"]))
    for item in rows("SELECT value,source_ids FROM financial_metrics WHERE project_id=? AND value IS NOT NULL", (project_id,)):
        if source_set.intersection(json_load(item["source_ids"])):
            values.append(float(item["value"]))
    for item in rows("SELECT result_value,values_json,thresholds,source_ids FROM cross_checks WHERE project_id=?", (project_id,)):
        if source_set.intersection(json_load(item["source_ids"])):
            if item["result_value"] is not None:
                values.append(float(item["result_value"]))
            values.extend(float(entry["value"]) for entry in json_load(item["values_json"]) if entry.get("value") is not None)
            values.extend(float(value) for value in json_load(item["thresholds"]).values() if isinstance(value, (int, float)))
    for item in rows("SELECT left_value,right_value,difference,tolerance,source_ids FROM financial_tie_outs WHERE project_id=?", (project_id,)):
        if source_set.intersection(json_load(item["source_ids"])):
            values.extend(float(item[key]) for key in ("left_value", "right_value", "difference", "tolerance") if item[key] is not None)
    return values, labels


def _frozen_table_evidence(section: dict) -> tuple[list[float], set[str]]:
    """该章**冻结**的结构化允许记录给出的（数值, 科目名）。

    表格单元格的数在材料页面原文里不一定以同样写法出现（单位写在表头、单元格是裸数字、
    千分位与精度不同），而科目名又常常只出现在表格行首；只靠 ``evidence_text`` 匹配会把
    正常引用判成"无证据声明"——**科目标签检查比数字检查更早触发**，所以两者都要并入
    （修订稿 §3.1："同时传入章节校验与后置支持校验"）。
    """
    records = json_load(section.get("table_records_json"))
    values: list[float] = []
    labels: set[str] = set()
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            values.append(float(str(item.get("value"))))
        except (TypeError, ValueError):
            pass
        label = str(item.get("label") or "").strip()
        if label:
            labels.add(label)
    return values, labels


def _claim_support_status(
    project_id: str, section_key: str, claim: str, claim_type: str,
    source_ids: list[str], source_texts: dict[str, str],
    table_values: list[float] | None = None, table_labels: set[str] | None = None,
) -> str:
    if section_key in SYSTEM_SECTIONS:
        return "system_derived"
    if section_key == "risks" and row("SELECT COUNT(*) count FROM risks WHERE project_id=?", (project_id,))["count"] == 0:
        return "system_derived"
    if not source_ids:
        return "unsupported"
    if claim_type == "risk_judgment":
        return "derived"
    evidence_text = "\n".join(source_texts.get(source_id, "") for source_id in source_ids)
    evidence_values, evidence_labels = _evidence_context(project_id, source_ids)
    labels = [definition.label for definition in FACT_REGISTRY.values() if definition.label in claim]
    if labels and not any(
        label in evidence_text or label in evidence_labels or label in (table_labels or set())
        for label in labels
    ):
        return "unsupported"
    claim_numbers = _numbers(claim)
    if claim_numbers:
        candidates = _numbers(evidence_text) + evidence_values + list(table_values or [])
        if not all(_number_matches(value, candidates) for value in claim_numbers):
            return "unsupported"
    return "direct"


def _support_context(project_id: str) -> tuple[dict, dict, set]:
    citation_map = {
        item["id"]: item for item in rows("SELECT * FROM citations WHERE project_id=?", (project_id,))
    }
    source_texts = {
        item["id"]: item["text"]
        for item in rows("SELECT id,text FROM analysis_sources WHERE project_id=?", (project_id,))
    }
    return citation_map, source_texts, set(source_texts)


def _as_list(value: object) -> list:
    if isinstance(value, str):
        return json_load(value) or []
    return list(value or [])


def assess_section_claims(project_id: str, section: dict, citation_map: dict,
                          source_texts: dict, valid_sources: set) -> dict:
    """评估单章的主张证据支持（**纯计算，不写库**）。

    计算与持久化拆开，后置回退才能"在内存中分别评估原正文与候选草稿"，
    选定最终正文后再统一落库——否则失败现场会被重建的 report_claims 覆盖。
    """
    citation_ids = _as_list(section.get("citation_ids"))
    # 正文只保留少量面向读者的引用；校验仍需要该章完整的证据白名单，
    # 否则一句简短来源说明会把本来有支持的段落误判成无支持。
    source_ids = [citation_map[item]["source_id"] for item in citation_ids if item in citation_map]
    source_ids.extend(_as_list(section.get("allowed_source_ids")))
    source_ids = list(dict.fromkeys(item for item in source_ids if item in valid_sources))
    # 系统生成的强制披露（冲突断言块、来源边界块）不是本章的事实主张：它们由系统
    # 撰写且被要求逐字保留，其中的断言编号/来源编号不可能从材料里找到支持。
    # 把它们计入支持率只会制造"无证据声明"并（旧实现里）触发破坏性回退。
    from .chapter_guard import SOURCE_ATTRIBUTION_BLOCK      # 延迟导入：chapter_guard → professional → p0 成环
    content = section.get("content") or ""
    for block in [*_as_list(section.get("required_disclosures")), SOURCE_ATTRIBUTION_BLOCK]:
        if block and block in content:
            content = content.replace(block, "")
    claims, factual, supported = [], 0, 0
    # 冻结的**结构化记录**一并作为候选证据（数值 + 科目名）：表格里的数在页面原文里常是
    # 另一种写法、科目名常只出现在行首，不并入就会把正常引用误判成"无证据声明"。
    table_values, table_labels = _frozen_table_evidence(section)
    for index, claim in enumerate(_claim_lines(content), start=1):
        claim_type = _claim_type(claim)
        if claim_type in NON_FACTUAL_CLAIM_TYPES:
            # 叙述、审查要求/核验动作、来源边界声明都不是业务事实主张：不参与证据支持
            # 统计，状态给 not_applicable（与 narrative 完全一致的处理）。
            status = "not_applicable"
        else:
            factual += 1
            status = _claim_support_status(
                project_id, section["section_key"], claim, claim_type, source_ids, source_texts,
                table_values, table_labels,
            )
            if status != "unsupported":
                supported += 1
        unsupported_numbers = re.findall(r"-?\d+(?:\.\d+)?", claim) if status == "unsupported" else []
        claims.append({
            "claim_index": index, "claim_text": claim, "claim_type": claim_type, "support_status": status,
            "source_ids": source_ids, "citation_ids": citation_ids, "unsupported_numbers": unsupported_numbers,
        })
    return {
        "claims": claims, "factual": factual, "supported": supported,
        "unsupported_claims": factual - supported,
        "invalid_citations": sum(1 for item in citation_ids if item not in citation_map),
        "support_rate": round(supported / factual, 4) if factual else 1.0,
    }


def validate_report_support(project_id: str) -> dict:
    # 延迟导入：report_delivery → chapter_guard → professional → p0 会形成环。
    from .report_delivery import assess_delivery, content_sha256

    sections = rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,))
    citation_map, source_texts, valid_sources = _support_context(project_id)
    total_factual = total_supported = invalid_citations = 0
    disclosure_missing = needs_review = 0
    with connect() as conn:
        conn.execute("DELETE FROM report_claims WHERE project_id=?", (project_id,))
        for section in sections:
            assessment = assess_section_claims(project_id, section, citation_map, source_texts, valid_sources)
            total_factual += assessment["factual"]
            total_supported += assessment["supported"]
            invalid_citations += assessment["invalid_citations"]
            for claim in assessment["claims"]:
                conn.execute(
                    "INSERT INTO report_claims (id,project_id,section_id,claim_index,claim_text,claim_type,support_status,source_ids,supporting_ids,unsupported_numbers,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (_stable_id("claim", section["id"], str(claim["claim_index"])), project_id, section["id"],
                     claim["claim_index"], claim["claim_text"], claim["claim_type"], claim["support_status"],
                     json_dump(claim["source_ids"]), json_dump(claim["citation_ids"]),
                     json_dump(claim["unsupported_numbers"]), now_iso()),
                )
            rate = assessment["support_rate"]
            status = "passed" if rate == 1 and assessment["invalid_citations"] == 0 else "warning"
            # 强制披露与交付状态独立于支持率：冲突块缺失时即便支持率 100% 也不可交付。
            disclosure_status, delivery_status, validated_hash = assess_delivery(
                content=section.get("content") or "",
                required_disclosures=_as_list(section.get("required_disclosures")),
                support_status=status,
                length_status=section.get("length_status") or "not_checked",
                validated_content_sha256=section.get("validated_content_sha256") or "",
            )
            disclosure_missing += 1 if disclosure_status != "passed" else 0
            needs_review += 1 if delivery_status == "needs_review" else 0
            # 证据支持状态只写它自己的字段（validation_status 为兼容字段）。
            # 绝不能覆盖 generation_status / fallback_reason——那会把"模型回退"
            # 伪装成"通过"，前端就再也看不出模型结果有没有真正进入报告。
            conn.execute(
                "UPDATE report_sections SET support_rate=?,support_validation_status=?,validation_status=?,"
                "disclosure_status=?,delivery_status=?,validated_content_sha256=?,content_sha256=? WHERE id=?",
                (rate, status, status, disclosure_status, delivery_status, validated_hash,
                 content_sha256(section.get("content") or ""), section["id"]),
            )
    unsupported = total_factual - total_supported
    return {
        "factual_claims": total_factual, "supported_claims": total_supported, "unsupported_claims": unsupported,
        "support_rate": round(total_supported / total_factual, 4) if total_factual else 1.0,
        "invalid_citations": invalid_citations,
        "disclosure_missing_sections": disclosure_missing,
        "needs_review_sections": needs_review,
        "status": "passed" if unsupported == 0 and invalid_citations == 0 and disclosure_missing == 0 else "warning",
    }


def migrate_report_generation_status() -> int:
    """历史报告行补 `generation_status` / `support_validation_status`。

    只补空值，不覆盖已有判断。此前 `validation_status` 同时承担"生成结果"和
    "证据支持"两种含义，被 `validate_report_support` 覆盖后无法再看出模型是否回退。
    """
    from .chapter_guard import NO_MODEL_REASON
    from .chapter_prompts import chapter_uses_llm

    pending = rows(
        "SELECT id,section_key,generation_status,fallback_reason,validation_status "
        "FROM report_sections WHERE generation_status='' OR generation_status IS NULL"
    )
    for item in pending:
        key = str(item.get("section_key") or "")
        reason = str(item.get("fallback_reason") or "")
        if not chapter_uses_llm(key):
            status = "deterministic"
        elif reason and reason != NO_MODEL_REASON:
            status = "fallback"
        else:
            status = "model_passed"
        with connect() as conn:
            conn.execute(
                "UPDATE report_sections SET generation_status=?,support_validation_status=? WHERE id=?",
                (status, str(item.get("validation_status") or "pending"), item["id"]),
            )
    return len(pending)


def build_evidence_graph(project_id: str) -> dict:
    with connect() as conn:
        conn.execute("DELETE FROM evidence_edges WHERE project_id=?", (project_id,))

        def edge(from_type: str, from_id: str, relation: str, to_type: str, to_id: str, metadata: dict | None = None) -> None:
            if not from_id or not to_id:
                return
            conn.execute(
                "INSERT OR IGNORE INTO evidence_edges (id,project_id,from_type,from_id,relation,to_type,to_id,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (_stable_id("edge", project_id, from_type, from_id, relation, to_type, to_id), project_id, from_type, from_id,
                 relation, to_type, to_id, json_dump(metadata or {}), now_iso()),
            )

        for source in conn.execute("SELECT id,document_id FROM analysis_sources WHERE project_id=?", (project_id,)).fetchall():
            edge("document", source["document_id"], "contains", "source", source["id"])
        for assertion in conn.execute("SELECT id,source_ids FROM fact_assertions WHERE project_id=?", (project_id,)).fetchall():
            for source_id in json_load(assertion["source_ids"]):
                edge("source", source_id, "supports", "assertion", assertion["id"])
        for canonical in conn.execute("SELECT id,assertion_ids,selected_assertion_id FROM canonical_facts WHERE project_id=?", (project_id,)).fetchall():
            for assertion_id in json_load(canonical["assertion_ids"]):
                edge("assertion", assertion_id, "canonicalizes", "canonical_fact", canonical["id"], {"selected": assertion_id == canonical["selected_assertion_id"]})
        for metric in conn.execute("SELECT id,input_fact_ids FROM financial_metrics WHERE project_id=?", (project_id,)).fetchall():
            for fact_id in json_load(metric["input_fact_ids"]):
                edge("canonical_fact", fact_id, "input_to", "metric", metric["id"])
        for check in conn.execute("SELECT id,fact_ids FROM cross_checks WHERE project_id=?", (project_id,)).fetchall():
            for fact_id in json_load(check["fact_ids"]):
                edge("canonical_fact", fact_id, "input_to", "cross_check", check["id"])
        for tie in conn.execute("SELECT id,input_fact_ids FROM financial_tie_outs WHERE project_id=?", (project_id,)).fetchall():
            for fact_id in json_load(tie["input_fact_ids"]):
                edge("canonical_fact", fact_id, "input_to", "tie_out", tie["id"])
        checks = conn.execute("SELECT id,title FROM cross_checks WHERE project_id=? AND status='warning'", (project_id,)).fetchall()
        for risk in conn.execute("SELECT id,title,candidate_id FROM risks WHERE project_id=?", (project_id,)).fetchall():
            if risk["candidate_id"]:
                edge("candidate_finding", risk["candidate_id"], "confirmed_as", "risk", risk["id"])
            else:
                check = next((item for item in checks if item["title"] == risk["title"]), None)
                if check:
                    edge("cross_check", check["id"], "triggers", "risk", risk["id"])
        for claim in conn.execute("SELECT id,source_ids FROM report_claims WHERE project_id=?", (project_id,)).fetchall():
            for source_id in json_load(claim["source_ids"]):
                edge("source", source_id, "cited_by", "report_claim", claim["id"])

    entities = rows("SELECT id,entity_type type,name label FROM project_entities WHERE project_id=?", (project_id,))
    counts = rows("SELECT from_type,relation,to_type,COUNT(*) count FROM evidence_edges WHERE project_id=? GROUP BY from_type,relation,to_type", (project_id,))
    return {"entities": entities, "edge_summary": counts, "edge_count": sum(item["count"] for item in counts)}


def evidence_graph(project_id: str, limit: int = 2000) -> dict:
    summary = build_evidence_graph(project_id)
    edges = rows("SELECT from_type,from_id,relation,to_type,to_id,metadata_json FROM evidence_edges WHERE project_id=? ORDER BY rowid LIMIT ?", (project_id, limit))
    for item in edges:
        item["metadata_json"] = json_load(item["metadata_json"])
    summary["edges"] = edges
    summary["truncated"] = summary["edge_count"] > len(edges)
    return summary


def calculation_trace(project_id: str, metric_id: str) -> dict:
    metric = row("SELECT * FROM financial_metrics WHERE id=? AND project_id=?", (metric_id, project_id))
    if not metric:
        raise ValueError("财务指标不存在")
    fact_ids = json_load(metric["input_fact_ids"])
    facts = []
    for fact_id in fact_ids:
        fact = row("SELECT * FROM canonical_facts WHERE id=? AND project_id=?", (fact_id, project_id))
        if not fact:
            continue
        assertion = row("SELECT * FROM fact_assertions WHERE id=?", (fact.get("selected_assertion_id"),)) or {}
        source_rows = []
        for source_id in json_load(assertion.get("source_ids", "[]")):
            source = row(
                "SELECT id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,"
                "origin_type,snapshot_id,profile_id,profile_version,entity_role FROM analysis_sources WHERE id=?",
                (source_id,),
            )
            if source:
                source["bbox_json"] = json_load(source["bbox_json"])
                source_rows.append(source)
        facts.append({"canonical_fact": fact, "assertion": assertion, "sources": source_rows})
    metric["input_fact_ids"] = fact_ids
    metric["source_ids"] = json_load(metric["source_ids"])
    metric["trace_json"] = json_load(metric["trace_json"])
    return {"metric": metric, "inputs": facts}


def report_quality(project_id: str) -> dict:
    sections = rows("SELECT id,citation_ids FROM report_sections WHERE project_id=?", (project_id,))
    claims = rows("SELECT section_id,claim_type,support_status,unsupported_numbers FROM report_claims WHERE project_id=?", (project_id,))
    factual = [item for item in claims if item["claim_type"] not in NON_FACTUAL_CLAIM_TYPES]
    supported = [item for item in factual if item["support_status"] != "unsupported"]
    valid_citations = {item["id"] for item in rows("SELECT id FROM citations WHERE project_id=?", (project_id,))}
    invalid_citations = sum(
        citation_id not in valid_citations
        for section in sections
        for citation_id in json_load(section["citation_ids"])
    )
    unsupported = len(factual) - len(supported)
    evidence_factual = [item for item in factual if item["support_status"] != "system_derived"]
    factual_section_ids = {item["section_id"] for item in evidence_factual}
    cited_factual_sections = sum(
        section["id"] in factual_section_ids
        and any(citation_id in valid_citations for citation_id in json_load(section["citation_ids"]))
        for section in sections
    )
    quality = {
        "factual_claims": len(factual), "supported_claims": len(supported), "unsupported_claims": unsupported,
        "support_rate": round(len(supported) / len(factual), 4) if factual else 0,
        "factual_section_citation_coverage": round(cited_factual_sections / len(factual_section_ids), 4) if factual_section_ids else 1.0,
        "invalid_citations": invalid_citations,
        "unsupported_numeric_claims": sum(bool(json_load(item["unsupported_numbers"])) for item in claims),
        "status": ("passed" if unsupported == 0 and invalid_citations == 0 else "warning") if claims else "not_generated",
    }
    quality["tie_outs"] = {
        status: row("SELECT COUNT(*) count FROM financial_tie_outs WHERE project_id=? AND status=?", (project_id, status))["count"]
        for status in ("passed", "warning", "insufficient")
    }
    quality["entities"] = row("SELECT COUNT(*) count FROM project_entities WHERE project_id=?", (project_id,))["count"]
    # 交付一致性统计：独立于支持率，专门暴露"模型被采用/被回退/待人工复核/披露缺失/
    # 正文与校验哈希不一致"。旧指标里这些问题会被"支持率 100%"掩盖。
    from .chapter_prompts import chapter_uses_llm

    delivery_rows = rows(
        "SELECT section_key,generation_status,content_origin,fallback_stage,delivery_status,disclosure_status,"
        "content_sha256,validated_content_sha256 FROM report_sections WHERE project_id=?",
        (project_id,),
    )
    # 采用率的分母只算**启用模型的章节**：确定性章节即使发生后置回退，
    # 也不是"模型未被采用"，计入分母会低估采用率。
    attempted = [
        item for item in delivery_rows
        if chapter_uses_llm(item["section_key"]) and item["generation_status"] in {"model_passed", "fallback"}
    ]
    quality["delivery_quality"] = {
        "model_attempted_sections": len(attempted),          # 本次实际尝试模型生成的章节数
        "model_adopted_sections": len([item for item in attempted
                                       if item["generation_status"] == "model_passed"
                                       and item["content_origin"] == "model"
                                       and item["delivery_status"] == "eligible"]),
        "chapter_fallbacks": len([item for item in attempted if item["generation_status"] == "fallback"]),
        "support_fallbacks": len([item for item in delivery_rows if item["fallback_stage"] == "report_support"]),
        "needs_review_sections": len([item for item in delivery_rows if item["delivery_status"] == "needs_review"]),
        "disclosure_missing_sections": len([item for item in delivery_rows if item["disclosure_status"] == "warning"]),
        "content_diagnosis_mismatch_sections": len([
            item for item in delivery_rows
            if item["validated_content_sha256"] and item["content_sha256"] != item["validated_content_sha256"]
        ]),
        "model_adoption_rate": (
            round(len([item for item in attempted
                       if item["generation_status"] == "model_passed"
                       and item["content_origin"] == "model"
                       and item["delivery_status"] == "eligible"]) / len(attempted), 4)
            if attempted else 0
        ),
    }
    # 篇幅质量与事实支持率分开报告：篇幅偏离不改变事实验证结论，也不单独阻断导出。
    length_rows = rows(
        "SELECT section_key,length_status,visible_content_chars,length_repair_attempted "
        "FROM report_sections WHERE project_id=? ORDER BY rowid",
        (project_id,),
    )
    checked = [item for item in length_rows if item["length_status"] != "not_checked"]
    in_range = [item for item in checked if item["length_status"] == "passed"]
    quality["length_quality"] = {
        "status": ("passed" if len(in_range) == len(checked) else "warning") if checked else "not_checked",
        "checked_sections": len(checked),
        "in_range_sections": len(in_range),
        "short_sections": sum(1 for item in checked if item["length_status"] == "short"),
        "long_sections": sum(1 for item in checked if item["length_status"] == "long"),
        # 历史库里仍有 hard_limit_exceeded（系统硬顶已移除），保留计数以免旧报告看起来"少了一类"。
        "hard_limit_sections": sum(1 for item in checked if item["length_status"] == "hard_limit_exceeded"),
        "repair_count": sum(1 for item in length_rows if item["length_repair_attempted"]),
        "average_visible_chars": round(sum(item["visible_content_chars"] for item in checked) / len(checked)) if checked else 0,
        "details": [
            {"section_key": item["section_key"], "status": item["length_status"],
             "visible_content_chars": item["visible_content_chars"],
             "repair_attempted": bool(item["length_repair_attempted"])}
            for item in length_rows
        ],
    }
    quality["gold_annotations"] = row("SELECT COUNT(*) count FROM gold_annotations WHERE project_id=?", (project_id,))["count"]
    return quality
