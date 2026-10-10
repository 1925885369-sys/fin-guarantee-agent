"""Seed real public bond materials as an evaluation-isolated reference project."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .database import connect, json_dump, now_iso, row, rows
from .facts import FACT_REGISTRY
from .parsers import parse_document, sha256
from .table_extraction import TABLE_PARSER_VERSION
from .table_store import persist_tables

REAL_PROJECT_ID = "real-bond-zhongyou-2026-02"
REAL_COMPANY = "中邮证券有限责任公司"
REAL_BOND_NAME = "中邮证券有限责任公司2026年面向专业投资者公开发行公司债券（第二期）"
REAL_MATERIAL_DIR = "real-bond-zhongyou-2026-02"
REAL_LEGACY_DIR = "demo-中邮"

REAL_DOCUMENTS = (
    ("real-zy-prospectus", "中邮03募集说明书.pdf", "募集说明书", "prospectus"),
    ("real-zy-rating", "中邮03信用评级报告.pdf", "信用评级报告", "rating_report"),
)

BEITOU_PROJECT_ID = "real-bond-beitou-2026-01"
BEITOU_COMPANY = "北京市基础设施投资有限公司"
BEITOU_BOND_NAME = "北京市基础设施投资有限公司2026年面向专业投资者公开发行企业债券（多网融合）（第一期）"
BEITOU_MATERIAL_DIR = "demo-北投"
BEITOU_DOCUMENTS = (
    ("real-bt-prospectus", "募集说明书.pdf", "募集说明书", "prospectus"),
    ("real-bt-rating", "信用评级报告.pdf", "信用评级报告", "rating_report"),
)


@dataclass(frozen=True)
class RealFactSpec:
    fact_key: str
    value: float | None
    unit: str
    text_value: str
    raw_value: str
    document_id: str
    anchors: tuple[str, ...]


@dataclass(frozen=True)
class RealReferenceSpec:
    project_id: str
    company: str
    bond_name: str
    material_dirs: tuple[str, ...]
    documents: tuple[tuple[str, str, str, str], ...]
    facts: tuple[RealFactSpec, ...]
    amount: float
    term_months: int
    purpose: str
    counter_guarantee_status: str = "undetermined"


REAL_FACTS = (
    RealFactSpec("bond_name", None, "", REAL_BOND_NAME, REAL_BOND_NAME, "real-zy-prospectus", ("中邮证券有限责任公司2026年面向专业投资者公开发行公司债券（第二期）",)),
    RealFactSpec("bond_exchange", None, "", "上海证券交易所", "上海证券交易所", "real-zy-prospectus", ("上海证券交易所",)),
    RealFactSpec("registered_issue_amount", 300000.0, "万元", "", "不超过30亿元", "real-zy-prospectus", ("注册规模为不超过30亿元", "注册金额不超过人民币30亿元")),
    RealFactSpec("bond_issue_amount", 200000.0, "万元", "", "不超过20.00亿元", "real-zy-prospectus", ("本期债券发行总额为不超过20.00亿元", "本期发行金额不超过人民币20.00亿元")),
    RealFactSpec("bond_term_years", None, "年", "品种一3年/品种二5年", "品种一为3年期，品种二为5年期", "real-zy-prospectus", ("品种一为3年期，品种二为5年期",)),
    RealFactSpec("coupon_rate", None, "%", "网下询价簿记建档确定", "债券票面利率将根据网下询价簿记结果确定", "real-zy-prospectus", ("债券票面利率将根据网下询价簿记结果",)),
    RealFactSpec("interest_frequency", None, "", "按年付息", "付息频率为按年付息", "real-zy-prospectus", ("付息频率为按年付息",)),
    RealFactSpec("principal_repayment", None, "", "到期一次性偿还本金", "本期债券到期一次性偿还本金", "real-zy-prospectus", ("本期债券到期一次性偿还本金",)),
    RealFactSpec("proceeds_amount", 200000.0, "万元", "", "不超过20.00亿元", "real-zy-prospectus", ("本期债券发行总额为不超过20.00亿元",)),
    RealFactSpec("proceeds_purpose", None, "", "补充流动资金", "募集资金扣除发行费用后拟全部用于补充流动资金", "real-zy-prospectus", ("募集资金扣除发行费用后拟全部用于补充流动资金",)),
    RealFactSpec("guarantor_name", None, "", "中国邮政集团有限公司", "中国邮政集团有限公司", "real-zy-prospectus", ("本期债券由中国邮政集团有限公司提供",)),
    RealFactSpec("guarantee_type", None, "", "全额无条件不可撤销连带责任保证", "全额无条件不可撤销连带责任保证担保", "real-zy-prospectus", ("提供全额无条件不可撤销连带责任保证担保",)),
    RealFactSpec("guarantee_scope", None, "", "本金、利息、违约金、损害赔偿金和实现债权的全部费用", "担保范围包括本期债券的本金、利息、违约金、损害赔偿金和实现债权的全部费用", "real-zy-prospectus", ("担保范围包括本期债券的本金、利息、违约金", "保证担保的范围为本次债券的本金、利息、违约金")),
    RealFactSpec("guarantee_cap", 300000.0, "万元", "", "保证担保总额不超过30亿元", "real-zy-prospectus", ("实现债权的全部费用且总额不超过30亿元",)),
    RealFactSpec("guarantee_period", None, "", "债券存续期及到期日后两年", "本次债券的存续期及本次债券到期日后两年", "real-zy-prospectus", ("本次债券的存续期及本次债券到期日后两年",)),
    RealFactSpec("issuer_rating", None, "", "AA+", "主体长期信用等级为AA+", "real-zy-rating", ("主体长期信用等级为AA+",)),
    RealFactSpec("bond_rating", None, "", "AAA", "本期债券信用等级为AAA", "real-zy-rating", ("公司债券（第二期）信用等级为AAA", "本次发行债券的信用等级为AAA")),
    RealFactSpec("rating_outlook", None, "", "稳定", "评级展望为稳定", "real-zy-rating", ("评级展望为稳定",)),
    RealFactSpec("rating_agency", None, "", "联合资信评估股份有限公司", "联合资信评估股份有限公司", "real-zy-rating", ("联合资信评估股份有限公司通过对中邮证券",)),
)

BEITOU_FACTS = (
    RealFactSpec("bond_name", None, "", BEITOU_BOND_NAME, BEITOU_BOND_NAME, "real-bt-prospectus", ("北京市基础设施投资有限公司2026年面向专业投资者公开发行企业债券（多网融合）（第一期）",)),
    RealFactSpec("bond_exchange", None, "", "北京证券交易所", "北京证券交易所", "real-bt-prospectus", ("北京证券交易所债券簿记建档系统",)),
    RealFactSpec("registered_issue_amount", 500000.0, "万元", "", "注册规模为不超过50亿元", "real-bt-prospectus", ("注册规模为不超过50亿元",)),
    RealFactSpec("bond_issue_amount", 200000.0, "万元", "", "本期债券发行金额不超过人民币20亿元（含20亿元）", "real-bt-prospectus", ("本期债券发行金额不超过人民币20亿元（含20亿元）",)),
    RealFactSpec("bond_term_years", None, "年", "品种一3年/品种二10年", "品种一期限为3年期，品种二期限为10年期", "real-bt-prospectus", ("品种一期限为3年期，品种二期限为10年期",)),
    RealFactSpec("coupon_rate", None, "%", "固定利率，票面利率由网下询价簿记结果确定", "本期债券为固定利率债券，债券票面利率将根据网下询价簿记结果确定", "real-bt-prospectus", ("本期债券为固定利率债券，债券票面利率将根据网下询价簿记结果",)),
    RealFactSpec("interest_frequency", None, "", "按年付息", "本期债券采用单利计息，付息频率为按年付息", "real-bt-prospectus", ("付息频率为按年付息",)),
    RealFactSpec("principal_repayment", None, "", "到期一次性偿还本金", "本期债券到期一次性偿还本金", "real-bt-prospectus", ("本期债券到期一次性偿还本金",)),
    RealFactSpec("bond_maturity_date", None, "", "品种一2029-07-16/品种二2036-07-16", "品种一兑付日期为2029年7月16日，品种二兑付日期为2036年7月16日", "real-bt-prospectus", ("品种一的兑付日期为2029年7月16日",)),
    RealFactSpec("proceeds_amount", 200000.0, "万元", "", "本期募集资金不超过20亿元", "real-bt-prospectus", ("本期债券募集资金扣除发行费用后，拟全部用于补充流动资金",)),
    RealFactSpec("proceeds_purpose", None, "", "全部用于补充流动资金", "募集资金扣除发行费用后拟全部用于补充流动资金", "real-bt-prospectus", ("本期债券募集资金扣除发行费用后，拟全部用于补充流动资金",)),
    RealFactSpec("guarantee_type", None, "", "无增信安排", "本期债券不设定增信措施", "real-bt-prospectus", ("本期债券不设定增信措施", "本期债券无增信安排")),
    RealFactSpec("underwriter", None, "", "中信建投证券股份有限公司等", "牵头主承销商为中信建投证券股份有限公司", "real-bt-prospectus", ("牵头主承销商、簿记管理人", "主承销商：中信建投证券股份有限公司")),
    RealFactSpec("trustee", None, "", "中信建投证券股份有限公司", "债券受托管理人为中信建投证券股份有限公司", "real-bt-prospectus", ("债券受托管理人、簿记管理人：中信建投证券股份有限公司",)),
    RealFactSpec("investor_scope", None, "", "专业机构投资者", "本期债券面向专业机构投资者公开发行", "real-bt-prospectus", ("本期债券面向专业机构投资者公开发行",)),
    RealFactSpec("issuer_rating", None, "", "AAA", "发行人主体信用等级为AAA", "real-bt-rating", ("北京市基础设施投资有限公司AAA/稳定", "主体信用等级为AAA")),
    RealFactSpec("bond_rating", None, "", "AAA", "品种一和品种二信用等级均为AAA", "real-bt-rating", ("（第一期）（品种一）AAA", "评定“北京市基础设施投资有限公司2026年面向专业投资者公开发行企业债券")),
    RealFactSpec("rating_outlook", None, "", "稳定", "发行人评级展望为稳定", "real-bt-rating", ("北京市基础设施投资有限公司AAA/稳定", "评级展望为稳定")),
    RealFactSpec("rating_agency", None, "", "中诚信国际信用评级有限责任公司", "中诚信国际信用评级有限责任公司", "real-bt-rating", ("中诚信国际信用评级有限责任公司",)),
    RealFactSpec("controller", None, "", "北京市人民政府国有资产监督管理委员会", "北京市国资委为公司的唯一股东及实际控制人", "real-bt-rating", ("北京市国资委为公司的唯一股东及实际控制人",)),
    RealFactSpec("registered_capital", 20506600.0, "万元", "", "注册资本2,050.66亿元", "real-bt-rating", ("公司注册资本2,050.66亿元",)),
)

REAL_REFERENCE_SPECS = (
    RealReferenceSpec(REAL_PROJECT_ID, REAL_COMPANY, REAL_BOND_NAME, (REAL_MATERIAL_DIR, REAL_LEGACY_DIR), REAL_DOCUMENTS, REAL_FACTS, 200000.0, 36, "募集资金用于补充流动资金"),
    RealReferenceSpec(BEITOU_PROJECT_ID, BEITOU_COMPANY, BEITOU_BOND_NAME, (BEITOU_MATERIAL_DIR,), BEITOU_DOCUMENTS, BEITOU_FACTS, 200000.0, 36, "募集资金扣除发行费用后拟全部用于补充流动资金", "not_applicable"),
)

def is_real_reference(project_id: str) -> bool:
    item = row("SELECT data_class FROM projects WHERE id=?", (project_id,))
    return bool(item and item.get("data_class") == "real_reference")


def ensure_project_classification() -> int:
    """Backfill project classification without admitting real references to evaluations."""
    from .bond_documents import CASE_SPECS, evaluation_manifest

    eligible = set(evaluation_manifest())
    demo_ids = {case.id for case in CASE_SPECS}
    count = 0
    with connect() as conn:
        for item in conn.execute("SELECT id,data_class,evaluation_eligible FROM projects").fetchall():
            project_id = item["id"]
            if project_id == REAL_PROJECT_ID or project_id.startswith("real-"):
                data_class, eligible_flag = "real_reference", 0
            elif project_id in demo_ids:
                data_class, eligible_flag = "fictional_demo", int(project_id in eligible)
            elif project_id.startswith("demo-"):
                data_class, eligible_flag = "fictional_demo", 0
            else:
                data_class, eligible_flag = "user_project", 0
            if item["data_class"] != data_class or item["evaluation_eligible"] != eligible_flag:
                conn.execute(
                    "UPDATE projects SET data_class=?,evaluation_eligible=?,updated_at=? WHERE id=?",
                    (data_class, eligible_flag, now_iso(), project_id),
                )
                count += 1
    return count


def _material_root() -> Path | None:
    materials = settings.resolved_data_dir / "demo_materials"
    current = materials / REAL_MATERIAL_DIR
    legacy = materials / REAL_LEGACY_DIR
    if current.is_dir():
        return current
    return legacy if legacy.is_dir() else None


def _material_root_for(spec: RealReferenceSpec) -> Path | None:
    materials = settings.resolved_data_dir / "demo_materials"
    for directory in spec.material_dirs:
        candidate = materials / directory
        if candidate.is_dir():
            return candidate
    return None


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _source_for_anchor(conn, document_id: str, anchors: tuple[str, ...], project_id: str = REAL_PROJECT_ID) -> str | None:
    sources = conn.execute(
        "SELECT id,text FROM sources WHERE project_id=? AND document_id=? ORDER BY page,id",
        (project_id, document_id),
    ).fetchall()
    normalized = [(item["id"], _normalize(item["text"])) for item in sources]
    for anchor in anchors:
        target = _normalize(anchor)
        for source_id, text in normalized:
            if target and target in text:
                return source_id
    return None


def _extract_tranches(conn, project_id: str = REAL_PROJECT_ID, document_id: str = "real-zy-prospectus") -> list[dict]:
    """Build 0..N varieties only when the prospectus explicitly discloses them."""
    from .bond_tranches import deterministic_extract_tranches

    source_rows = [dict(item) for item in conn.execute(
        "SELECT id,text,page FROM sources WHERE project_id=? AND document_id=? ORDER BY page,id",
        (project_id, document_id),
    )]
    if not source_rows:
        return []

    # Variety disclosures are transaction terms. Use the deterministic
    # section index when the document has a recognizable offering-terms
    # heading so historical bond tables elsewhere in the prospectus cannot
    # become current tranches. Synthetic/minimal fixtures without headings
    # keep the compatibility fallback and are still validated by the parser.
    try:
        from .bond_sections import _document_contract, _locate_deterministically, _locate_end_anchors, _source_sort_key

        ordered = sorted(source_rows, key=_source_sort_key)
        contract = _document_contract("prospectus")
        starts = _locate_deterministically(ordered, contract) if contract else {}
        start = starts.get("offering_terms")
        if start is not None:
            ends = _locate_end_anchors(ordered, contract, starts)
            end = ends.get("offering_terms")
            if end is None:
                later = [index for role, index in starts.items() if index > start]
                end = min(later) if later else len(ordered)
            source_rows = ordered[start:max(start + 1, end)]
    except (ImportError, KeyError, TypeError):
        # The extraction helper remains usable with lightweight test schemas.
        pass
    return deterministic_extract_tranches(source_rows)


def _persist_seeded_tranches(conn, project_id: str, tranches: list[dict]) -> None:
    """Persist deterministic tranche facts discovered directly in the prospectus."""
    conn.execute(
        "DELETE FROM bond_tranches WHERE project_id=? AND extraction_method='legacy_reference_seed'",
        (project_id,),
    )
    for tranche in tranches:
        conn.execute(
            "INSERT INTO bond_tranches (id,project_id,tranche_key,tranche_name,term_years,issue_amount,"
            "repayment_method,maturity_date,coupon_rate,amount_scope,allocation_status,source_ids,created_at,"
            "extraction_method,confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), project_id, tranche["tranche_key"], tranche["tranche_name"],
                tranche["term_years"], tranche.get("issue_amount"), tranche.get("repayment_method", ""),
                tranche.get("maturity_date", ""), tranche.get("coupon_rate"),
                tranche.get("amount_scope", "aggregate_current_issue"),
                tranche.get("allocation_status", "not_disclosed"), json_dump(tranche.get("source_ids", [])),
                now_iso(), "reference_seed_deterministic", tranche.get("confidence"),
            ),
        )


def _upsert_document(conn, root: Path, spec: tuple[str, str, str, str], now: str, project_id: str = REAL_PROJECT_ID) -> bool:
    document_id, filename, kind, role = spec
    path = root / filename
    if not path.is_file():
        return False
    digest = sha256(path)
    existing = conn.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
    source_count = conn.execute("SELECT COUNT(*) count FROM sources WHERE document_id=?", (document_id,)).fetchone()["count"]
    stored_tables = conn.execute(
        "SELECT COUNT(*) count,MIN(parser_version) min_version,MAX(parser_version) max_version "
        "FROM document_tables WHERE project_id=? AND document_id=?", (project_id, document_id),
    ).fetchone()
    tables_current = bool(stored_tables["count"] and stored_tables["min_version"] == TABLE_PARSER_VERSION
                          and stored_tables["max_version"] == TABLE_PARSER_VERSION)
    complete = bool(existing and existing["sha256"] == digest and existing["page_count"] > 0
                    and source_count >= existing["page_count"] and tables_current)
    if complete:
        conn.execute("UPDATE documents SET document_role=? WHERE id=?", (role, document_id))
        return True

    parsed = parse_document(path, max_pages=600)
    persist_tables(conn, project_id, document_id, parsed.tables)
    conn.execute(
        "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,document_role,ocr_status,page_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET filename=excluded.filename,kind=excluded.kind,media_type=excluded.media_type,sha256=excluded.sha256,size=excluded.size,parse_status=excluded.parse_status,quality=excluded.quality,locator_count=excluded.locator_count,warning=excluded.warning,parser=excluded.parser,document_role=excluded.document_role,ocr_status=excluded.ocr_status,page_count=excluded.page_count",
        (document_id, project_id, filename, kind, "application/pdf", digest, path.stat().st_size,
         "parsed", parsed.quality, len(parsed.fragments), parsed.warning, now, parsed.parser, role,
         parsed.ocr_status, parsed.page_count),
    )
    for fragment in parsed.fragments:
        page = fragment.page or 0
        source_id = f"{document_id}-p{page:03d}"
        conn.execute(
            "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET locator_type=excluded.locator_type,locator=excluded.locator,text=excluded.text,page=excluded.page,bbox_json=excluded.bbox_json,extraction_method=excluded.extraction_method,content_hash=excluded.content_hash",
            (source_id, project_id, document_id, fragment.locator_type, fragment.locator, fragment.text, page,
             json_dump(fragment.bbox or []), fragment.extraction_method,
             hashlib.sha256(fragment.text.encode("utf-8")).hexdigest()),
        )
    return True


def _seed_reference(spec: RealReferenceSpec) -> bool:
    """Seed one public reference from its own materials and evidence anchors."""
    root = _material_root_for(spec)
    if not root:
        return False
    now = now_iso()
    with connect() as conn:
        existing = conn.execute("SELECT id FROM projects WHERE id=?", (spec.project_id,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,project_type,bond_name,consent_at,data_class,evaluation_eligible,counter_guarantee_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (spec.project_id, secrets.token_urlsafe(24), spec.company, spec.amount, spec.term_months,
                 spec.purpose, "ready", now, now, "bond_guarantee", spec.bond_name, now,
                 "real_reference", 0, spec.counter_guarantee_status),
            )
        else:
            conn.execute(
                "UPDATE projects SET company_name=?,amount=?,term_months=?,purpose=?,project_type='bond_guarantee',bond_name=?,data_class='real_reference',evaluation_eligible=0,counter_guarantee_status=?,analysis_target='full_due_diligence',effective_analysis_mode='full_due_diligence',updated_at=? WHERE id=?",
                (spec.company, spec.amount, spec.term_months, spec.purpose, spec.bond_name,
                 spec.counter_guarantee_status, now, spec.project_id),
            )

        for document in spec.documents:
            _upsert_document(conn, root, document, now, spec.project_id)
        # Reference fixtures are raw source projects, not a second fact
        # pipeline.  Older databases may contain hand-seeded expected values;
        # retain them for audit but make them ineligible for analysis until the
        # real chapter-scoped extraction runs.
        current_document_facts = conn.execute(
            "SELECT 1 FROM facts WHERE project_id=? AND extraction_method NOT IN ('seed','legacy_reference_seed') LIMIT 1",
            (spec.project_id,),
        ).fetchone()
        if not current_document_facts:
            conn.execute(
                "UPDATE facts SET extraction_method='legacy_reference_seed',review_status='stale',"
                "assertion_status='stale',origin_type='legacy_reference_seed',origin_ref='reference_seed' "
                "WHERE project_id=? AND extraction_method IN ('seed','legacy_reference_seed')",
                (spec.project_id,),
            )
            conn.execute(
                "UPDATE fact_assertions SET extraction_method='legacy_reference_seed',assertion_status='stale',"
                "origin_type='legacy_reference_seed',origin_ref='reference_seed' "
                "WHERE project_id=? AND extraction_method IN ('seed','legacy_reference_seed')",
                (spec.project_id,),
            )
            conn.execute(
                "UPDATE canonical_facts SET status='stale',selected_assertion_id=NULL,value=NULL,unit='',text_value='',"
                "resolved_by='system',resolution_note='历史参考种子，需按章节重新抽取',updated_at=? WHERE project_id=?",
                (now_iso(), spec.project_id),
            )
            conn.execute(
                "UPDATE bond_tranches SET extraction_method='legacy_reference_seed' WHERE project_id=?",
                (spec.project_id,),
            )
            conn.execute(
                "UPDATE projects SET analysis_stale=1,report_stale=1,report_stale_reasons=?,updated_at=? WHERE id=?",
                (json_dump(["reference_requires_chapter_extraction"]), now_iso(), spec.project_id),
            )

        # Make clean installs show source-backed varieties immediately. An
        # existing Agent result remains authoritative and is not overwritten.
        current_tranche = conn.execute(
            "SELECT 1 FROM bond_tranches WHERE project_id=? "
            "AND extraction_method!='legacy_reference_seed' LIMIT 1",
            (spec.project_id,),
        ).fetchone()
        prospectus_id = next((item[0] for item in spec.documents if item[3] == "prospectus"), None)
        if not current_tranche and prospectus_id:
            tranches = _extract_tranches(conn, spec.project_id, prospectus_id)
            _persist_seeded_tranches(conn, spec.project_id, tranches)
    return True


def seed_real_reference_projects() -> int:
    """Idempotently seed every local public reference and repair partial imports."""
    return sum(1 for spec in REAL_REFERENCE_SPECS if _seed_reference(spec))
