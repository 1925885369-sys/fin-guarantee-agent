from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from typing import Any

from .database import connect, json_dump, json_load, now_iso, row, rows
from .facts import FACT_REGISTRY

# "缺少/未提供X"类缺失陈述的语境记号：科目存在性闸门对它不生效（见 _claim_support_status）。
# 扩展（收口方案 §一："扩展缺口识别覆盖'未识别到、未见明确约定'"）：北投 financial #44 /
# cashflow #5/#23 实测——"未识别到原表""未见明确约定" 未在词表里，缺口陈述被当成
# "无证据事实"。
_GAP_STATEMENT_CONTEXT_RE = re.compile(
    r"(缺少|缺乏|缺失|材料不足|未提供|未获取|未取得|未随|未披露|未识别到|未识别|未见|未出现"
    r"|未包含|未在现有材料|没有|无法获取|无法识别|无法判断|无法形成结论|无法基于|暂无|暂缺"
    r"|待核验|待补充|待提供|待核实"
    # 否定性边界声明（"本章仅作事实转述，不据此推断…"/"不构成投资建议"）：与缺口陈述
    # 同属"无事实主张"的边界句，科目存在性闸门同样不应生效（2026-10-15 北投
    # operations #17 实测）。
    r"|不据此|不作结论|不作判断|不作风险判断|不代表|不构成|不得视为|不视为"
    # 2026-10-16 本轮试跑复核新增：① "…均需在补充…后方可核验"（北投 governance #18：
    # "本报告不对关联方资金往来的规模、占比或风险水平作出结论；…方可核验"——限定性
    # 边界句，与"不作结论"同族，只是措辞为"不对…作出结论/方可核验"）；
    # ② "无法形成确定性判断"（中邮 cashflow #38："…之前，均无法形成确定性判断"——
    # "无法形成结论"的等价措辞）。
    # 2026-10-16 第三轮试跑复核再扩：③ "无法区分/无法确认/无法核实"与"另行核验"
    # （中邮 governance：关联方事项"无法区分…情形、相关金额与占比均无法确认，需…另行核验"）；
    # ④ "未作（风险）判断/未作结论"（中邮 operations："缺少…基础数据，因此本项未作风险
    # 判断"）；⑤ "不对…作出（确定性）结论"的窗口版（完整句整句版见 `_CLAIM_DECLARATION_RE`）。
    r"|方可核验|另行核验|无法区分|无法确认|无法核实|未作(?:风险)?判断|未作结论"
    r"|不对[^。；]{0,24}(?:作出(?:确定性|结论性)?(?:结论|判断)|作(?:结论|判断))"
    r"|无法形成(?:结论|确定性判断|确定结论))"
)

# 强缺口词（首尾窗口专用）：**刻意不含**"没有/不代表"等通用否定词——业务事实句
# "没有诉讼、没有关联交易"不得借否定词豁免事实校验（执行方案 §P0-1）。
_GAP_EDGE_RE = re.compile(
    r"(缺少|缺乏|缺失|材料不足|未提供|未获取|未取得|未随|未披露|未识别|未见|未出现"
    r"|未包含|待核验|待补充|待提供|待核实|暂无|暂缺|无法获取|无法识别|无法判断"
    r"|无法形成结论|无法基于"
    # 2026-10-16：与上下文正则同步补"无法形成确定性判断"（中邮 cashflow #38 实测）与
    # "无法区分/无法确认/无法核实"（第三轮复核）。
    r"|无法形成(?:确定性判断|确定结论|结论)|无法区分|无法确认|无法核实)"
)

# 整句级"方法/过程声明"（2026-10-16 本轮试跑复核新增）："勾稽声明：本章未对任何现金流
# 数据、偿债指标或覆盖比例**进行自行计算或换算**…"（中邮 cashflow #32 实测）——缺口词
# （未对）与命中科目（偿债专户）相隔整句，邻域窗口与首尾窗口都装不下；过程声明不含业务
# 事实主张，整句按无事实主张处理。
# **动词白名单限定在 计算/换算/勾稽**：这三个动词只描述分析方法，不用于断言业务事实
# （事实句"未对商誉计提减值"用"计提"、"未对长期股权投资重分类"用"重分类"，均不在名单，
# 不受影响）；窗口 60 字且不跨句（不吞"；"后的内容）。
# 第三轮复核追加"不对…作出（确定性）结论"整句版：中邮 cashflow "在取得…之前，本章不对
# 本期债券兑付保障程度作出确定性结论"——句子短、但"不对"与"作出结论"间隔超出邻域窗口，
# 且"作出**确定性**结论"不在否定边界声明的既有措辞里。
_CLAIM_DECLARATION_RE = re.compile(
    r"未对[^。；]{0,60}(?:进行|作出)(?:自行)?(?:计算|换算|勾稽)"
    r"|不对[^。；]{0,30}作出(?:确定性|结论性)?(?:结论|判断)"
    # 2026-10-16 第四轮试跑复核：边界声明补"未对/未就 + 作出/形成/发表/提出 +
    # （任何|倾向性|确定性…）+ 判断/结论/意见"（中邮 financial #32"未对…作出任何
    # 倾向性判断，亦未就…形成意见"、cashflow #34"未对…发表意见"）。**必须带否定前缀**
    # （未/不），正面表述"对…作出判断"不受影响；带数字的句子仍走数字判定，
    # 本正则只跳过科目存在性闸门。
    r"|(?:未对|未就|不对|不就)[^。；]{0,50}(?:作出|形成|发表|提出)"
    r"(?:任何|相关|倾向性|确定性|结论性|明确的?)*(?:判断|结论|意见|评价|评论|倾向)"
)

# 通用时间窗口（"未来12个月""未来3年"）不是业务数值：核验动作/建议句里出现它，
# 不应把句子从 requirement 拉回数字分支（中邮 financial #28"核对未来12个月到期债务规模"
# 实测：仅因"12"被读成业务数值，整句建议清单落进 risk_judgment 并撞科目存在性闸门）。
# 只在本判定（`_claim_type` 的"是否含可核验数值"）里剔除；数字判定链（`_number_mentions`）
# 不受影响——"未来12个月到期债务为500万元"仍会核对 500。
_HORIZON_WINDOW_RE = re.compile(r"未来\s*\d{1,3}\s*个?月|未来\s*\d{1,2}\s*年")

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
    # 风险矩阵由规则引擎拼装（"缺少0类/4项"这类系统计数），与 risks 同一性质：
    # 不是模型事实主张，判 system_derived（北投 risk_matrix#4 实测误报）。
    "risk_matrix",
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
    name = project["company_name"]
    now = now_iso()
    with connect() as conn:
        # 冲突目标必须是 borrower 的确定 id，而不是 (project_id,entity_type,name)：
        # 项目基础信息可以修改发行人名称，旧行与新名不同名时按名字做 upsert 不会命中，
        # 插入会直接撞主键抛 UNIQUE constraint failed（改一次名就再也打不开实体列表）。
        duplicate = conn.execute(
            "SELECT id FROM project_entities WHERE project_id=? AND entity_type='enterprise' AND name=? AND id<>?",
            (project_id, name, entity_id),
        ).fetchone()
        if duplicate:
            # 目标名已被本项目另一个企业实体占用：把指向它的记录改挂到 borrower 再删除重复行。
            # 只挪 subject_id 指向，不改任何事实内容。
            for table in ("facts", "fact_assertions", "canonical_facts"):
                conn.execute(
                    f"UPDATE {table} SET subject_id=? WHERE project_id=? AND subject_id=?",
                    (entity_id, project_id, duplicate["id"]),
                )
            conn.execute("DELETE FROM project_entities WHERE id=?", (duplicate["id"],))
        conn.execute(
            "INSERT INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) "
            "VALUES (?,?,?,?,1,'confirmed','{}',?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,is_borrower=1,updated_at=excluded.updated_at",
            (entity_id, project_id, "enterprise", name, now, now),
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

# 计入「已获支持」的状态。``needs_review``（归属不明，待人工核验）**不在其中**：
# 修订稿 §二.2「归属不明不得自动算作支持」，且待核验事实须计入独立指标并阻止最终交付。
SUPPORTED_STATUSES = {"direct", "derived", "system_derived"}

# 来源边界 / 使用限制声明：说明披露材料的效力边界，不对业务字段取值
# （"本次披露未经独立核验""不构成…""仅作为披露信息""以原件为准"）。
_BOUNDARY_PHRASES = (
    "不等同于", "不构成", "不得替代", "不应替代", "仅反映", "不等于", "不代表",
    "未经独立核验", "未经核实", "未经审计核验", "仅作为披露", "仅供披露",
    "以原件为准", "以正式文件为准", "不作为", "供参考",
    # 取值口径/方法边界声明（收口方案 §一："来源说明与具体业务事实分开检查"）。
    # 北投 financial/debt 实测："均只能引用确定性值…未推算换算"、"本章不自行计算"。
    "只能引用", "不自行计算", "不自动计算",
)

# 审查要求 / 建议核验动作：表述"应当做什么"，不主张业务事实取值。刻意用**多字词组**
# 而非单字"应/需"，避免把"对应收账款核对无误"这类含"应"的陈述误判为要求。
_REQUIREMENT_RE = re.compile(
    r"应当|建议|务必|须"
    r"|应与|应将|应在|应于|应由|应按|应予|应以|应对|应分|应单|应重"
    r"|应核对|应核实|应核查|应取得|应提供|应补充|应函证|应说明|应披露|应列示|应呈示|应验算|应复核"
    r"|需核对|需核实|需核查|需取得|需提供|需补充|需函证|需说明|需进一步|需重新"
    r"|请提供|请补充|请说明|要求提供|要求补充"
    # 后续核验动作清单（"后续核验动作包括：取得…原件，核对…"）：北投 debt #30 实测。
    r"|后续核验|核验动作"
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
    if not _numbers(_HORIZON_WINDOW_RE.sub("", text)):
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


def _number_mentions(claim: str) -> list[dict]:
    """按 ``chapter_guard`` 的口径提取（归一化数值, 归一化单位, 局部语境）。

    延迟导入：``chapter_guard → professional → p0`` 成环，不能在模块顶层导入。
    """
    from .chapter_guard import _number_mentions as _impl

    return _impl(claim)


def _frozen_evidence_records(project_id: str, section: dict) -> tuple[list[dict], str]:
    """该章**冻结的统一记录**与记录版本（``evidence_records_json``；旧行兼容迁移）。

    后置复查只读冻结输入（第三点细化方案 §二.2 / §四 + 三项遗留修复方案 §一）：优先取
    调用方随 ``section`` 传入的记录；未携带时按 ``(project_id, section_key)`` 读已存的
    ``evidence_records_json``——旧章节只有 ``table_records_json`` 时就地迁移为
    ``record_type=table``；两者都为空标 ``legacy_unfrozen``（不自动补证据，需人工再生）。
    **禁止**重新查询当前表格或页面数值池。
    """
    from .evidence_records import RECORD_VERSION, records_from_legacy

    def _decode(raw) -> list[dict]:
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
        if isinstance(raw, str) and raw.strip():
            decoded = json_load(raw)
            if isinstance(decoded, list):
                return [item for item in decoded if isinstance(item, dict)]
        return []

    for key in ("evidence_records_json", "evidence_records"):
        records = _decode(section.get(key))
        if records:
            return records, RECORD_VERSION

    section_key = str(section.get("section_key") or "")
    stored = None
    if project_id and section_key:
        stored = row(
            "SELECT evidence_records_json, table_records_json FROM report_sections "
            "WHERE project_id=? AND section_key=?",
            (project_id, section_key),
        )
    if stored:
        records = _decode(stored.get("evidence_records_json"))
        if records:
            return records, RECORD_VERSION
        legacy = _decode(stored.get("table_records_json"))
        if legacy:
            return records_from_legacy(legacy), "legacy:table-records-v1"
    legacy = _decode(section.get("table_records_json")) or _decode(section.get("table_records"))
    if legacy:
        return records_from_legacy(legacy), "legacy:table-records-v1"
    return [], "legacy_unfrozen"


def _frozen_records(project_id: str, section: dict) -> list[dict]:
    """兼容封装：仅返回记录列表（旧调用方沿用）。"""
    return _frozen_evidence_records(project_id, section)[0]


def _record_identity(record: dict) -> tuple[str, str, str, str, str]:
    """记录的归属身份：科目/期间/主体/口径/单位。同数值多条记录身份相同才算"归属唯一"。"""
    from .chapter_guard import _canonical_measurement

    _value, unit, _ = _canonical_measurement(record.get("value"), str(record.get("unit") or ""))
    return (
        str(record.get("label") or "").strip(), str(record.get("period") or "").strip(),
        str(record.get("subject") or "").strip(), str(record.get("scope") or "").strip(), unit,
    )


def _mentions_ambiguous_attribution(item: dict, records: list[dict]) -> bool:
    """某数字是否**归属不明**：命中多条归属不同的记录，而主张紧邻语境没写出任何线索。

    线索 = 该章科目词表里出现的科目名 / 数字前的年份（非比较基准）/ 口径声明。任意一条
    线索存在时，'跨记录/无记录拼接'的判定交给 ``_record_binding``（更严格），这里不重复。
    """
    from .chapter_guard import (
        PERIOD_COMPARATIVE_MARKERS, PERIOD_TOKEN_RE, SCOPE_TERMS, SUBJECT_BINDING_WINDOW, _canonical_measurement,
    )

    value, unit = str(item.get("normalized_value")), item.get("normalized_unit") or ""
    has_unit = bool(item.get("has_unit"))
    candidates = []
    for record in records:
        record_value, record_unit, _ = _canonical_measurement(record.get("value"), str(record.get("unit") or ""))
        if record_value != value or (has_unit and record_unit and record_unit != unit):
            continue
        candidates.append(record)
    if len({_record_identity(record) for record in candidates}) <= 1:
        return False
    prefix = str(item.get("context") or "").split(str(item["raw"]), 1)[0]
    vocabulary = {str(record.get("label") or "").strip() for record in records}
    vocabulary.discard("")
    if any(label in prefix[-SUBJECT_BINDING_WINDOW:] for label in vocabulary):
        return False
    years = list(PERIOD_TOKEN_RE.finditer(prefix))
    if len(years) == 1:
        found = years[0]
        around = prefix[max(0, found.start() - 3):found.end() + 6]
        if not any(marker in around for marker in PERIOD_COMPARATIVE_MARKERS):
            return False
    if any(term in prefix for term in SCOPE_TERMS):
        return False
    return True


def _same_value_record_ids(mentions: list[dict], records: list[dict], limit: int = 5) -> list[str]:
    """与主张数字**同值**的记录 id（支持失败诊断里的"匹配记录"，收口方案 §三）。

    只回记录 id、不回正文；按记录顺序去重取前 ``limit`` 条。裸数字（主张没写单位）
    按数值匹配、单位不参与；主张写了单位则须同单位。
    """
    from .chapter_guard import _canonical_measurement

    wanted: set[tuple[str, str]] = set()
    for item in mentions or []:
        value = str(item.get("normalized_value") or "")
        if value:
            wanted.add((value, str(item.get("normalized_unit") or "")))
    if not wanted:
        return []
    found: list[str] = []
    for record in records or []:
        record_value, record_unit, _ = _canonical_measurement(
            record.get("value"), str(record.get("unit") or ""))
        if not record_value:
            continue
        for wanted_value, wanted_unit in wanted:
            if record_value == wanted_value and (not wanted_unit or not record_unit or record_unit == wanted_unit):
                record_id = str(record.get("record_id") or "")
                if record_id and record_id not in found:
                    found.append(record_id)
                break
        if len(found) >= limit:
            break
    return found


def _numeric_binding_outcome(project_id: str, records: list[dict], claim: str,
                             mentions: list[dict], claim_type: str) -> tuple[str, str, list[str]]:
    """数字支持判定（统一冻结记录，三层；三项遗留修复方案 §一）+ **失败原因码与匹配记录**。

    返回 ``(status, reason_code, matched_record_ids)``；``reason_code`` 仅在失败时有值
    （收口方案 §三"每条支持失败返回主张位置、匹配记录和原因码"）：

    - ``missing_record``：该章有记录体系，但数字/科目在冻结记录里查无（缺记录）；
    - ``field_ambiguity``：命中记录但归属不唯一，或仅命中待核验记录（待人工核验）；
    - ``explicit_mismatch``：命中同值记录，但科目/期间/主体/口径与所写**冲突**（明确错配）；
    - ``infrastructure_error``：该章没有冻结记录可核对（legacy_unfrozen）——先补/再生
      记录，不是改句子（三项遗留修复方案 §一）。

    状态口径（direct/needs_review/unsupported）与旧 ``_numeric_binding_status`` 完全一致，
    后者是它的纯状态封装。复用 ``chapter_guard._record_binding`` 这一"共同校验器"
    （细化方案 §二.2），与章节级校验**同口径**，后置复查不另起一套判定。
    """
    from .chapter_guard import (
        _allowed_number_index, _mention_allowed, _project_entity_registry, _record_binding,
    )
    from .evidence_records import (
        evidence_number_keys, matchable_records, records_from_legacy, review_records,
        supportable_records,
    )

    if not records:
        return "unsupported", "infrastructure_error", []
    if any(not item.get("record_type") for item in records):
        # 旧冻结数据（纯表格记录）就地升级为统一记录。
        records = records_from_legacy(records)
    supported_index = _allowed_number_index(evidence_number_keys(supportable_records(records)))
    review_index = _allowed_number_index(evidence_number_keys(review_records(records)))
    missing_supported = [item for item in mentions if not _mention_allowed(item, supported_index)]
    if missing_supported:
        matched = _same_value_record_ids(mentions, records)
        if all(
            _mention_allowed(item, supported_index) or _mention_allowed(item, review_index)
            for item in mentions
        ):
            # 每个数字都能被记录接住，但至少一个只命中待核验记录 → 待人工核验。
            return "needs_review", "field_ambiguity", matched
        return "unsupported", "missing_record", matched
    binding_records = matchable_records(records)
    allowed_entries = evidence_number_keys(binding_records)
    registry = _project_entity_registry(project_id)
    conflicts, review_items = _record_binding(mentions, binding_records, registry, claim, allowed_entries)
    matched = _same_value_record_ids(mentions, binding_records)
    if conflicts:
        return "unsupported", "explicit_mismatch", matched
    if review_items:
        return "needs_review", "field_ambiguity", matched
    if any(_mentions_ambiguous_attribution(item, binding_records) for item in mentions):
        return "needs_review", "field_ambiguity", matched
    return ("derived" if claim_type == "risk_judgment" else "direct"), "", matched


def _numeric_binding_status(project_id: str, records: list[dict], claim: str,
                            mentions: list[dict], claim_type: str) -> str:
    """兼容封装：仅返回支持状态（旧调用方沿用字符串口径）。"""
    return _numeric_binding_outcome(project_id, records, claim, mentions, claim_type)[0]


def _lookup_normalize(text: str) -> str:
    """存在性检索用的空白归一：去掉**汉字之间**的空白与换行。

    引文与页文本是 PDF 回流（"本期债券受托管⏎理人有权查询专户内的资金"），不归一会把材料里
    明明有的科目判成"不存在"（北投 cashflow #3 实测："债券受托管理人"被断行拆成"受托管/理人"）。
    只用于检索，不改写正文。
    """
    return re.sub(r"(?<=[\u4e00-\u9fff])[\s\u3000]+(?=[\u4e00-\u9fff])", "", str(text or ""))


def _label_present_in_sources(names, evidence_lookup: str, evidence_labels: set,
                              record_labels: set) -> bool:
    """科目标签在章节证据里是否存在：**登记别名一并检索** + PDF 断行归一。

    只认"所写科目的登记名与别名"（北投实测："债券受托管理人" 的登记别名含"受托管理人"、
    "评级机构" 别名含"中诚信"）：材料用别名写法时按存在处理，别名也不在才判不存在。
    """
    for name in names:
        if not name:
            continue
        if _lookup_normalize(name) in evidence_lookup or name in evidence_labels or name in record_labels:
            return True
    return False


# ------------------------------------------------------- 引文已有数字的补建记录 --
# 方案 §二 line 23：「引文已有数字但尚未建记录时，先补建可追溯记录，不能直接认定模型捏造」。
#
# ⚠️ 落点在**支持层**，不在章节校验：章节校验的数字允许集第一项就是 evidence 的 quotes
# （`chapter_guard._allowed_texts`），这类数字根本进不了 `unexpected_numbers`；真正把它
# 判成"编造"的是后置支持层的 `missing_record`（无冻结记录可核对 → 计无证据声明）。
_BACKFILL_COMPACT_RE = re.compile(r"[\s\u3000,，]")


def _locate_mention_in_sources(mention: dict, source_ids: list[str],
                               source_texts: dict[str, str]) -> str:
    """数字是否**逐字**出现在本章已实际发送的原文里；返回命中的 source_id（空=没找到）。

    比较前去掉空白与千分位：引文与页文本是 PDF 回流（"169.02 亿元"可能被空格/换行拆开）。
    只做检索，不改写任何文本。
    """
    raw = str(mention.get("raw") or "").strip()
    if not raw:
        return ""
    target = _BACKFILL_COMPACT_RE.sub("", _lookup_normalize(raw))
    if not target:
        return ""
    for source_id in source_ids or []:
        text = str(source_texts.get(source_id) or "")
        if not text:
            continue
        if target in _BACKFILL_COMPACT_RE.sub("", _lookup_normalize(text)):
            return str(source_id)
    return ""


def _backfill_disclosure_records(section_key: str, claim: str, mentions: list[dict],
                                 source_ids: list[str],
                                 source_texts: dict[str, str],
                                 records: list[dict] | None = None) -> list[dict]:
    """为"引文里有、记录里没有"的数字补建 `disclosure` 记录。

    三条自我约束（否则补建会变成"给所有找不到的数字开后门"）：

    1. **必须逐字命中**本章已发送原文，且每个数字都要命中——有一个没命中就整句不补建；
    2. 状态一律 `needs_review`：补建只把"疑似捏造"降为"待核验"，**不升为可支持**，
       所以它仍然计入未决项、仍然阻断最终交付；
    3. `origin_type="citation_backfill"` + `extra.backfilled_from_citation=True`：
       验收时能单独统计"有多少数字是靠补建救回来的"，而不是混进正常记录。
    """
    from .evidence_records import _record

    # 已被冻结记录接住的数字**不需要**再证明它在原文里：它已经有据可查
    # （2026-10-18 北投 financial 实测：`3,689,393.72万元` 来自系统表格、原文是
    # 另一写法，要求它逐字命中会让整句放弃补建，而同句的 `368.48亿元` 本可补建）。
    covered = {
        str(record.get("value")).replace(",", "").strip()
        for record in (records or [])
        if str(record.get("value") or "").strip()
    }
    located: list[tuple[dict, str]] = []
    for mention in mentions:
        if str(mention.get("normalized_value") or "") in covered:
            continue
        source_id = _locate_mention_in_sources(mention, source_ids, source_texts)
        if not source_id:
            return []
        located.append((mention, source_id))

    records: list[dict] = []
    for mention, source_id in located:
        value = str(mention.get("normalized_value") or "")
        unit = str(mention.get("normalized_unit") or "")
        digest = hashlib.sha1(
            f"{section_key}|{source_id}|{value}|{unit}".encode("utf-8")
        ).hexdigest()[:16]
        records.append(_record(
            record_id=f"disclosure:backfill:{digest}",
            record_type="disclosure",
            value=value,
            unit=unit,
            raw=str(mention.get("raw") or ""),
            # 补建记录**不带科目/期间/主体**：原文里能看到数字不等于是这个科目的值，
            # 填了反而会给出错误的归属（正是"字段歧义"该由人工裁的地方）。
            status="needs_review",
            source_ids=[source_id],
            origin_type="citation_backfill",
            extra={
                "backfilled_from_citation": True,
                "claim_excerpt": (claim or "")[:120],
            },
        ))
    return records


def _claim_support_outcome(
    project_id: str, section_key: str, claim: str, claim_type: str,
    source_ids: list[str], source_texts: dict[str, str],
    records: list[dict] | None = None, content_origin: str = "",
    supplemental: list[dict] | None = None,
) -> tuple[str, str, list[str]]:
    """单条主张的**支持状态 + 失败原因码 + 匹配记录**（收口方案 §三）。

    数字支持来自该章**统一冻结记录**（``evidence_records``：fact/table/metric/rule），
    按同一条记录核对科目/期间/主体/口径；``evidence_text`` 仅用于**无数字主张**的直接引用
    支持与科目存在性判断，不再充当"整页数值池"（第三点细化方案 §二.2）。

    确定性内容分流（三项遗留修复方案 §一）：确定性模板的普通说明文字不自动视为事实支持——
    无数字的说明/缺口句标 ``not_applicable``；带数字的仍按业务事实校验（无冻结记录可依时
    记 ``system_derived``，不再因为没有原文逐字匹配而误报）；核验要求、边界声明和叙述类
    句子沿用既有分类。

    原因码仅在失败（``unsupported`` / ``needs_review``）时有值，四类口径见
    ``_numeric_binding_outcome``；无来源的模型内容与存在性闸门失败记为
    ``missing_record``（缺记录/来源）。``_claim_support_status`` 是仅回状态的兼容封装。
    """
    if section_key in SYSTEM_SECTIONS:
        return "system_derived", "", []
    if section_key == "risks" and row("SELECT COUNT(*) count FROM risks WHERE project_id=?", (project_id,))["count"] == 0:
        return "system_derived", "", []
    if not source_ids:
        if content_origin == "deterministic":
            mentions = _number_mentions(claim)
            if not mentions:
                # 模板说明文字（"…为材料不足""…待核验"等）：不是事实主张，也不是"无证据声明"。
                return "not_applicable", "", []
            if records:
                return _numeric_binding_outcome(project_id, records, claim, mentions, claim_type)
            # 确定性内容且整章没有冻结记录（非受控表格章节）：无结构化输入可核对，
            # 按系统生成内容记录，不判"无证据"。
            return "system_derived", "", []
        # 模型内容无来源仍按不支持处理（不放宽）；失败原因是还没有可核对来源/记录。
        return "unsupported", "missing_record", []
    evidence_text = "\n".join(source_texts.get(source_id, "") for source_id in source_ids)
    # 存在性检索前做 PDF 断行/空白归一（"受托管⏎理人"）。
    evidence_lookup = _lookup_normalize(evidence_text)
    _evidence_values, evidence_labels = _evidence_context(project_id, source_ids)
    record_labels = {str(item.get("label") or "").strip() for item in (records or [])}
    # "缺少/未提供X"类**缺失陈述**：主张的是"该科目材料缺失"，不是"有该科目的某数值"——
    # 科目存在性闸门对它不应生效（北投 governance 实测："缺少关联方往来或其他应收款及
    # 净资产"因"其他应收款"不在证据里被误判无支持）。**任一出现**处于缺口语境即按缺失
    # 陈述处理（"关于主体与债项评级…核验，材料不足：缺少主体评级和债项评级"里，科目
    # 第一次出现在主题短语、第二次才带"缺少"）。带数字的句子仍走数字判定，不受影响。
    # 语境窗口：前 16 字 / 后 24 字——缺口短语常是整段枚举的**谓语**（"A、B、C…等，
    # 材料不足/未披露"），后窗口太窄会漏（北投 financial #44 "…到期债务未披露" 实测）。
    claim_labels: list[tuple[str, tuple[str, ...]]] = []
    # 强缺口词的**首尾窗口**（整句首 16 / 尾 16 字）：句首/句尾宣告"材料不足声明：…"
    # "…均未随本核心材料包提供，材料不足"时，枚举科目与谓语相隔极远（长句），邻域窗口
    # 装不下——首尾出现强缺口词即整句按缺失陈述处理（2026-10-15 华辰 governance #28
    # 实测）。**刻意不含通用否定词**（"没有/不代表"等）：业务事实句"没有诉讼、没有关联
    # 交易"不得借否定词豁免事实校验（执行方案 §P0-1）。
    # 2026-10-16：追加**整句级过程声明**（`_CLAIM_DECLARATION_RE`）——"未对…进行(自行)
    # 计算/换算/勾稽"是方法声明而非事实主张，缺口词与科目相隔整句（中邮 cashflow #32
    # 实测：未对…在句首、命中科目"偿债专户"在第二个分句）。
    edge_gap = bool(
        _GAP_EDGE_RE.search(claim[:16]) or _GAP_EDGE_RE.search(claim[-16:])
        or _CLAIM_DECLARATION_RE.search(claim)
    )
    for definition in FACT_REGISTRY.values():
        positions = [match.start() for match in re.finditer(re.escape(definition.label), claim)]
        if not positions:
            continue
        # 缺口语境可前可后："缺少X" vs "X为材料不足/未披露"——两者都按缺失陈述处理。
        # 窗口 2026-10-15 复核放宽（前 16→32 / 后 32→48）：缺口谓语常与**枚举列表**的
        # 各科目相隔较远（"材料未披露 A、B、C…"/"…的一致性，材料不足，无法判断"），
        # 原窗口差 1-20 字就漏判、整句被误记无支持并拖垮回退。claim 本身是**单句**
        # （`_claim_lines` 按句切分），窗口放大的代价仅为"同句无数字说明句"被漏放；
        # **带数字的主张仍走数字判定**，不受本闸门影响（实测：北投 operations #18/#32、
        # 华辰 governance #9）。
        gap = False
        for index in positions:
            before = claim[max(0, index - 32):index]
            after = claim[index + len(definition.label): index + len(definition.label) + 48]
            if _GAP_STATEMENT_CONTEXT_RE.search(before) or _GAP_STATEMENT_CONTEXT_RE.search(after):
                gap = True
                break
        if gap:
            continue
        # 注意：展平成 (登记名, *别名) 一维序列——`_label_present_in_sources` 逐名检索，
        # 嵌套元组会让别名静默失效（北投 financial #36/#41 "评级机构"→别名"中诚信" 实测）。
        claim_labels.append((definition.label, *tuple(definition.aliases or ())))
    if claim_labels and not edge_gap and not any(
        _label_present_in_sources(names, evidence_lookup, evidence_labels, record_labels)
        for names in claim_labels
    ):
        # 科目在证据与记录里都查无 → 缺记录（不是"数字错"，是"记录没建/没进证据"）。
        #
        # 但要区分两种情况（2026-10-17 北投 cashflow 实测）：
        # - 句子**带合法引用**：多半是写法差异——模型写"经营现金流净额"，材料写
        #   "经营活动产生的现金流量净额"，登记名逐字比对必然落空。此时降为待核验：
        #   仍计入未决、仍阻断最终交付，但不再当成"无证据声明"把整章打回模板
        #   （两章因此回退，属正确披露被误退）；
        # - 句子没有引用：维持无支持（"凭空写了个材料里没有的科目"必须硬拦）。
        from .chapter_guard import CITATION_ALIAS_RE

        if CITATION_ALIAS_RE.search(claim):
            return "needs_review", "label_variant_unverified", []
        return "unsupported", "missing_record", []
    mentions = _number_mentions(claim)
    if mentions:
        # 确定性内容**带数字**时仍按业务事实校验（方案 §一"带数字的…仍按业务事实校验"）；
        # 数字无对账输入的问题由来源空分支与 SYSTEM_SECTIONS 两道口径处理，不在此放宽。
        outcome = _numeric_binding_outcome(project_id, records or [], claim, mentions, claim_type)
        if outcome[1] == "missing_record":
            # 方案 §二 line 23：材料里明明写着、只是没抽成记录 —— 先补建可追溯记录，
            # 不能直接当"捏造"。补建只降到待核验（仍阻断最终交付），不放宽为可支持。
            backfilled = _backfill_disclosure_records(
                section_key, claim, mentions, source_ids, source_texts, records)
            if backfilled:
                if supplemental is not None:
                    supplemental.extend(backfilled)
                return "needs_review", "quote_number_without_record", [
                    str(item["record_id"]) for item in backfilled]
        return outcome
    if claim_type == "risk_judgment":
        return "derived", "", []
    return "direct", "", []


def _claim_support_status(
    project_id: str, section_key: str, claim: str, claim_type: str,
    source_ids: list[str], source_texts: dict[str, str],
    records: list[dict] | None = None, content_origin: str = "",
) -> str:
    """兼容封装：仅返回支持状态（旧调用方与测试沿用字符串口径）。"""
    return _claim_support_outcome(
        project_id, section_key, claim, claim_type, source_ids, source_texts,
        records, content_origin,
    )[0]


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
    claims, factual, supported, needs_review = [], 0, 0, 0
    # 方案 §二 line 23：本句"引文有、记录无"的数字补建记录（纯计算，**不写库**——
    # 本函数要保持可在内存里对比原正文与候选稿）。
    supplemental_records: list[dict] = []
    # **统一冻结记录**（fact/table/metric/rule）是该章数字支持的唯一来源（第三点细化方案
    # §二.2 + 三项遗留修复方案 §一）：只读这一份冻结输入，不重取页面数值池；旧章节
    # 兼容迁移，缺记录标 legacy_unfrozen。
    records, record_version = _frozen_evidence_records(project_id, section)
    content_origin = str(section.get("content_origin") or "")
    position_cursor = 0
    for index, claim in enumerate(_claim_lines(content), start=1):
        claim_type = _claim_type(claim)
        # 主张在**评估文本**（系统披露块剥离后）中的字符位置：收口方案 §三要求诊断带
        # "正文位置"。按顺序向后检索，重复句不会永远锚在第一次出现处。
        position = content.find(claim, position_cursor)
        if position < 0:
            position = position_cursor
        position_cursor = position + len(claim)
        if claim_type in NON_FACTUAL_CLAIM_TYPES:
            # 叙述、审查要求/核验动作、来源边界声明都不是业务事实主张：不参与证据支持
            # 统计，状态给 not_applicable（与 narrative 完全一致的处理）。
            status, reason_code, matched_record_ids = "not_applicable", "", []
        else:
            factual += 1
            status, reason_code, matched_record_ids = _claim_support_outcome(
                project_id, section["section_key"], claim, claim_type, source_ids, source_texts,
                records, content_origin, supplemental=supplemental_records,
            )
            if status in SUPPORTED_STATUSES:
                supported += 1
            elif status == "needs_review":
                needs_review += 1
        # 诊断里的"无支持数字"必须与**判定用过的**那份数字同源。裸 `re.findall` 会把
        # 引用别名（`（E01）`→`01`）、`〔资料03〕`、页码、日期一起读成数字——这些正是
        # `_strip_format_numbers` 的唯一剔除点要清掉的东西。它们经 `unsupported_numbers`
        # 送到修复提示里，只会让模型跑去"改引用"，而真正该删的数字被漏掉。
        unsupported_numbers = (
            list(dict.fromkeys(item["raw"] for item in _number_mentions(claim)))
            if status == "unsupported" else []
        )
        claims.append({
            "claim_index": index, "claim_text": claim, "claim_type": claim_type, "support_status": status,
            # 支持失败诊断三件套（收口方案 §三）：位置 / 原因码 / 匹配记录。
            "position": position, "reason_code": reason_code, "matched_record_ids": matched_record_ids,
            "source_ids": source_ids, "citation_ids": citation_ids, "unsupported_numbers": unsupported_numbers,
        })
    needs_review_items = [
        {"claim_index": item["claim_index"], "claim_text": item["claim_text"], "claim_type": item["claim_type"],
         "position": item["position"], "reason_code": item["reason_code"],
         "matched_record_ids": item["matched_record_ids"]}
        for item in claims if item["support_status"] == "needs_review"
    ]
    # 无证据声明数与 claims 状态**同源**：确定性模板句（"…为材料不足"）在
    # `_claim_support_status` 里按 not_applicable 处理，不再能从差额法
    # （factual - supported - needs_review）里漏出来被计成"无证据声明"
    # （收口方案 §一"材料不足句 … 不作为独立取值核验"；北投模板句实测）。
    return {
        "claims": claims, "factual": factual, "supported": supported,
        "unsupported_claims": sum(
            1 for item in claims if item["support_status"] == "unsupported"
        ),
        "needs_review_claims": needs_review,
        "needs_review_claim_items": needs_review_items,
        "invalid_citations": sum(1 for item in citation_ids if item not in citation_map),
        "support_rate": round(supported / factual, 4) if factual else 1.0,
        # 三项遗留修复方案 §一：记录规模与版本随评估结果输出（接口/验收可见）。
        "evidence_record_count": len(records),
        "support_record_version": record_version,
        # 待落库的补建记录（`disclosure`/`needs_review`）；由 `validate_report_support`
        # 与章节记录合并后写回，接口/导出据此显示"已补建但待核验"。
        "supplemental_records": supplemental_records,
    }


def _merge_supplemental_records(project_id: str, conn, section: dict,
                                records: list[dict]) -> int:
    """把补建记录并入该章已冻结记录（按 `record_id` **幂等**，重复运行不叠加）。

    写回 `evidence_records_json` 而不是另开存储：补建记录与其它记录必须走**同一套**
    允许集/绑定/支持判定，分表存会让后置复查读不到它、下次又判一次 `missing_record`。
    `input_hash` 不变——它是模型输入侧的摘要，补建属于**输出后的证据补齐**，
    与已冻结的计算记录同理。
    """
    existing, _ = _frozen_evidence_records(project_id, section)
    known = {str(item.get("record_id")) for item in existing if item.get("record_id")}
    fresh = [item for item in records if str(item.get("record_id") or "") not in known]
    if not fresh:
        return 0
    conn.execute(
        "UPDATE report_sections SET evidence_records_json=? WHERE id=?",
        (json_dump([*existing, *fresh]), section["id"]),
    )
    return len(fresh)


def validate_report_support(project_id: str) -> dict:
    # 延迟导入：report_delivery → chapter_guard → professional → p0 会形成环。
    from .report_delivery import assess_delivery, content_sha256

    sections = rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,))
    citation_map, source_texts, valid_sources = _support_context(project_id)
    total_factual = total_supported = invalid_citations = 0
    total_needs_review = 0
    disclosure_missing = needs_review_sections = 0
    backfilled_records = 0
    needs_review_items: list[dict] = []
    with connect() as conn:
        conn.execute("DELETE FROM report_claims WHERE project_id=?", (project_id,))
        for section in sections:
            assessment = assess_section_claims(project_id, section, citation_map, source_texts, valid_sources)
            total_factual += assessment["factual"]
            total_supported += assessment["supported"]
            total_needs_review += assessment["needs_review_claims"]
            invalid_citations += assessment["invalid_citations"]
            # 引文有数字但没建记录 → 补建 `needs_review` 记录并写回本章记录集
            # （方案 §二 line 23：不直接认定模型捏造，但也**不**升为可支持）。
            if assessment["supplemental_records"]:
                backfilled_records += _merge_supplemental_records(
                    project_id, conn, section, assessment["supplemental_records"])
            for item in assessment["needs_review_claim_items"]:
                needs_review_items.append({
                    "section_id": section["id"], "section_key": section["section_key"], **item,
                })
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
            if str(section.get("generation_status") or "") == "model_needs_review":
                # 计划 §四 line 38：修复后**仅剩未决项**的章节不得报 `passed`。它的支持率
                # 可能刚好 100%，但章节本身还有未决项、不计入合格采用率——支持状态强制
                # `warning`，交付状态于是保持 `needs_review`，等人工裁决。
                status = "warning"
            # 强制披露与交付状态独立于支持率：冲突块缺失时即便支持率 100% 也不可交付。
            disclosure_status, delivery_status, validated_hash = assess_delivery(
                content=section.get("content") or "",
                required_disclosures=_as_list(section.get("required_disclosures")),
                support_status=status,
                length_status=section.get("length_status") or "not_checked",
                validated_content_sha256=section.get("validated_content_sha256") or "",
            )
            disclosure_missing += 1 if disclosure_status != "passed" else 0
            needs_review_sections += 1 if delivery_status == "needs_review" else 0
            # 证据支持状态只写它自己的字段（validation_status 为兼容字段）。
            # 绝不能覆盖 generation_status / fallback_reason——那会把"模型回退"
            # 伪装成"通过"，前端就再也看不出模型结果有没有真正进入报告。
            conn.execute(
                "UPDATE report_sections SET support_rate=?,support_validation_status=?,validation_status=?,"
                "disclosure_status=?,delivery_status=?,validated_content_sha256=?,content_sha256=? WHERE id=?",
                (rate, status, status, disclosure_status, delivery_status, validated_hash,
                 content_sha256(section.get("content") or ""), section["id"]),
            )
    unsupported = total_factual - total_supported - total_needs_review
    return {
        "factual_claims": total_factual, "supported_claims": total_supported, "unsupported_claims": unsupported,
        # 待核验事实计入独立指标（第三点细化方案 §二.2）：不得自动算支持，供交付阻断消费。
        "needs_review_claims": total_needs_review,
        "needs_review_claim_items": needs_review_items,
        "support_rate": round(total_supported / total_factual, 4) if total_factual else 1.0,
        "invalid_citations": invalid_citations,
        # 补建记录数（验收口径：「有多少数字是靠补建从疑似捏造降为待核验的」）。
        "backfilled_records": backfilled_records,
        "disclosure_missing_sections": disclosure_missing,
        "needs_review_sections": needs_review_sections,
        "status": (
            "passed" if unsupported == 0 and total_needs_review == 0
            and invalid_citations == 0 and disclosure_missing == 0 else "warning"
        ),
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
            # 只覆盖"历史空值行"：`WHERE generation_status='' OR IS NULL` 保证**不会**
            # 覆盖已写入的 `model_needs_review`。老行无法回溯区分"全部通过"与"仅剩未决项"
            # （当时的实现把两者都记成 model_passed，行里没有留下区分信息），
            # 因此这里只能给 model_passed；新运行一律走 `run_chapter_model_attempt` 的判定。
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
    claims = rows(
        "SELECT section_id,claim_index,claim_text,claim_type,support_status,unsupported_numbers "
        "FROM report_claims WHERE project_id=? ORDER BY section_id,claim_index",
        (project_id,),
    )
    factual = [item for item in claims if item["claim_type"] not in NON_FACTUAL_CLAIM_TYPES]
    # needs_review（归属不明）不得自动算支持：单列一个指标供交付阻断，且不计入 supported。
    supported = [item for item in factual if item["support_status"] in SUPPORTED_STATUSES]
    needs_review_claim_items = [
        {"section_id": item["section_id"], "claim_index": item["claim_index"],
         "claim_text": item["claim_text"], "claim_type": item["claim_type"]}
        for item in factual if item["support_status"] == "needs_review"
    ]
    valid_citations = {item["id"] for item in rows("SELECT id FROM citations WHERE project_id=?", (project_id,))}
    invalid_citations = sum(
        citation_id not in valid_citations
        for section in sections
        for citation_id in json_load(section["citation_ids"])
    )
    unsupported = len([item for item in factual if item["support_status"] == "unsupported"])
    needs_review = len(needs_review_claim_items)
    evidence_factual = [item for item in factual if item["support_status"] != "system_derived"]
    factual_section_ids = {item["section_id"] for item in evidence_factual}
    cited_factual_sections = sum(
        section["id"] in factual_section_ids
        and any(citation_id in valid_citations for citation_id in json_load(section["citation_ids"]))
        for section in sections
    )
    quality = {
        "factual_claims": len(factual), "supported_claims": len(supported), "unsupported_claims": unsupported,
        "needs_review_claims": needs_review,
        "needs_review_claim_items": needs_review_claim_items,
        "support_rate": round(len(supported) / len(factual), 4) if factual else 0,
        "factual_section_citation_coverage": round(cited_factual_sections / len(factual_section_ids), 4) if factual_section_ids else 1.0,
        "invalid_citations": invalid_citations,
        "unsupported_numeric_claims": sum(bool(json_load(item["unsupported_numbers"])) for item in claims),
        "status": (
            "passed" if unsupported == 0 and needs_review == 0 and invalid_citations == 0 else "warning"
        ) if claims else "not_generated",
    }
    quality["tie_outs"] = {
        status: row("SELECT COUNT(*) count FROM financial_tie_outs WHERE project_id=? AND status=?", (project_id, status))["count"]
        for status in ("passed", "warning", "insufficient")
    }
    quality["entities"] = row("SELECT COUNT(*) count FROM project_entities WHERE project_id=?", (project_id,))["count"]
    # 方案 §二 line 23 的验收口径：有多少个数字是靠"引文补建记录"从疑似捏造降为待核验的。
    # 单独统计而不是混进记录总数——否则无法回答"补建到底救回了多少"，也无法发现补建过滥。
    quality["backfilled_records"] = sum(
        1
        for section in rows("SELECT evidence_records_json FROM report_sections WHERE project_id=?",
                            (project_id,))
        for record in json_load(section.get("evidence_records_json") or "[]")
        if isinstance(record, dict) and record.get("origin_type") == "citation_backfill"
    )
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
    # `model_needs_review`（修复后仅剩未决项）也算"尝试过模型"——它占分母、不占分子，
    # 于是"不计入合格采用率"（计划 §四 line 38）在统计上自然成立。
    attempted = [
        item for item in delivery_rows
        if chapter_uses_llm(item["section_key"])
        and item["generation_status"] in {"model_passed", "model_needs_review", "fallback"}
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
