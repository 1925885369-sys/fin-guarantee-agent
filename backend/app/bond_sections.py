"""Semantic section indexing and field whitelists for bond source documents.

The contract describes document *roles*, never a project name or fixed page
number.  A real document may use different pagination; only verified heading
boundaries are allowed to feed fact extraction.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .database import connect, json_dump, now_iso, rows
from .facts import FACT_REGISTRY
from .llm import LlmCallContext, LlmUsage, is_configured, locate_document_sections as llm_locate_document_sections


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "bond_extraction_contracts.yaml"


class SectionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_anchors: list[str] = Field(min_length=1)
    end_anchors: list[str] = Field(default_factory=list)
    allowed_fact_keys: list[str] = Field(default_factory=list)
    excluded_fact_keys: list[str] = Field(default_factory=list)
    subject_policy: str = "issuer"
    scope_policy: list[str] = Field(default_factory=lambda: ["transaction"])
    required: bool = False
    max_pages: int = Field(default=12, ge=1, le=100)

    @field_validator("allowed_fact_keys", "excluded_fact_keys")
    @classmethod
    def known_fact_keys(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(FACT_REGISTRY))
        if unknown:
            raise ValueError(f"章节抽取契约使用未知事实字段：{unknown}")
        return list(dict.fromkeys(value))

    @property
    def effective_fact_keys(self) -> tuple[str, ...]:
        excluded = set(self.excluded_fact_keys)
        return tuple(key for key in self.allowed_fact_keys if key not in excluded)


class DocumentContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_role: str
    sections: dict[str, SectionContract]


class ExtractionContractPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    documents: dict[str, DocumentContract]


@dataclass(frozen=True)
class LocatedSection:
    role: str
    title: str
    start_source_id: str
    end_source_id: str
    start_page: int | None
    end_page: int | None
    locator_method: str
    confidence: float
    status: str
    allowed_fact_keys: tuple[str, ...]
    subject_policy: str
    scope_policy: tuple[str, ...]


@dataclass(frozen=True)
class SectionExtractionPlan:
    section: LocatedSection
    sources: tuple[dict, ...]


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


@lru_cache(maxsize=1)
def load_extraction_contract() -> ExtractionContractPack:
    payload = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return ExtractionContractPack.model_validate(payload)


def contract_sha256() -> str:
    return hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()


def _document_contract(document_role: str) -> DocumentContract | None:
    for contract in load_extraction_contract().documents.values():
        if contract.document_role == document_role:
            return contract
    return None


# Some issuers use a stable semantic heading with a different final word
# (for example, ``增信机制`` instead of ``增信情况``). Keep these aliases in
# code so the contract remains reusable across issuers and page layouts.
_SECTION_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "credit_enhancement": ("\u7b2c\u4e03\u8282\u589e\u4fe1\u673a\u5236",),
    "rating_result": (
        "\u4e3b\u4f53\u8bc4\u7ea7\u7ed3\u679c",
        "\u503a\u9879\u8bc4\u7ea7\u7ed3\u679c",
        "\u4e3b\u4f53\u8bc4\u7ea7\u7ed3\u679c\u00a0\u503a\u9879\u8bc4\u7ea7\u7ed3\u679c\u00a0\u8bc4\u7ea7\u65f6\u95f4",
    ),
}
_SECTION_END_ALIASES: dict[str, tuple[str, ...]] = {
    "financial_statements": ("\u4e3b\u8981\u8d22\u52a1\u6307\u6807", "\u975e\u7ecf\u5e38\u6027\u635f\u76ca"),
}


def _source_sort_key(source: dict) -> tuple[int, str]:
    return (int(source.get("page") or 0), str(source.get("id") or ""))


def _toc_like_indices(source_rows: list[dict], contract: DocumentContract) -> set[int]:
    output: set[int] = set()
    for index, source in enumerate(source_rows):
        matches = 0
        for line in _heading_lines(str(source.get("text") or "")):
            if _looks_like_toc_line(line):
                matches += sum(
                    _heading_line_matches(line, anchor)
                    for item in contract.sections.values()
                    for anchor in item.title_anchors
                )
        if matches >= 3:
            output.add(index)
    return output


_HEADING_PREFIX = re.compile(r"^(?:第[一二三四五六七八九十百\d]+节|第[一二三四五六七八九十百\d]+[章节]|附[一二三四五六七八九十\d]+[：:]?|[一二三四五六七八九十]+、)")
_DECORATION_PREFIX = re.compile(r"^[\s\u3000\-—–•●◆◇▪▫■□◆☑\uf0a3\uf06c\(（\[]*\s*")


def _heading_lines(text: str) -> list[str]:
    """Return non-empty physical lines without collapsing document structure.

    PDF parsers commonly preserve headings as their own lines.  Keeping those
    boundaries is essential: a compact whole-page search cannot distinguish a
    real heading from a sentence such as “详见第二节发行条款”.
    """
    return [line.strip() for line in str(text or "").replace("\r", "\n").split("\n") if line.strip()]


# 目录引导符：半角点、全角点、间隔号，以及三种省略号（PDF 文本提取会把 U+2026
# 规范化成 U+22EF，只认一种写法会导致目录页识别失败）。
_TOC_LEADER = "[.．·…⋯‥⋮]"
_TOC_LEADER_TAIL = re.compile(rf"(?:{_TOC_LEADER}{{3,}}|\s{{2,}})\d{{1,4}}$")
_TOC_PAGE_SUFFIX = re.compile(r"(?:第\s*\d{1,4}\s*页|[(（]\s*第\s*\d{1,4}\s*页\s*[)）])$")


def _looks_like_toc_line(line: str) -> bool:
    """目录行识别：点线／空白引导页码，或「第N页」式页码后缀。

    目录页必须能被识别出来并忽略：一旦不被忽略，页面上每个章节标题都会成为锚点，
    把全部章节都定位到目录页——实测会让抽取按章节白名单拒绝掉正文里的事实。
    """
    compact = _compact(line)
    if _TOC_LEADER_TAIL.search(line) or _TOC_LEADER_TAIL.search(compact):
        return True
    return bool(_TOC_PAGE_SUFFIX.search(compact))


def _heading_line_matches(line: str, anchor: str) -> bool:
    """Match an anchor only where the line itself can reasonably be a title."""
    candidate = _DECORATION_PREFIX.sub("", line).strip()
    compact_line = _compact(candidate)
    compact_anchor = _compact(anchor)
    if not compact_anchor or not compact_line or _looks_like_toc_line(candidate):
        return False
    position = compact_line.find(compact_anchor)
    if position < 0:
        return False
    # Full section and appendix anchors must be a line prefix.  This rejects
    # prose references, quoted table-of-contents entries, and page headers.
    if _HEADING_PREFIX.match(compact_anchor):
        return position == 0
    if position == 0:
        # Short labels such as “外部支持” occur in body prose as
        # ``外部支持：……``.  They are headings only when the remaining text is
        # punctuation or a short qualifier, while longer anchors like the
        # rating-result table label remain valid on a single table line.
        suffix = compact_line[len(compact_anchor):]
        if len(compact_anchor) <= 5 and not _HEADING_PREFIX.match(compact_anchor) and len(suffix.strip("：:")) > 12:
            return False
        return True
    # A heading may be prefixed by “附一：发行人股权…”.  Do not admit arbitrary
    # body prose merely because it contains a short title phrase.
    prefix = compact_line[:position]
    return bool(_HEADING_PREFIX.match(prefix))


def _source_has_title(source: dict, title: str, anchors: Iterable[str]) -> bool:
    compact_title = _compact(title)
    if not compact_title:
        return False
    for line in _heading_lines(str(source.get("text") or "")):
        compact_line = _compact(_DECORATION_PREFIX.sub("", line))
        if compact_title in compact_line and not _looks_like_toc_line(line):
            return any(_heading_line_matches(line, anchor) for anchor in anchors)
    return False


def _first_heading_index(source_rows: list[dict], anchors: Iterable[str], ignored: set[int]) -> int | None:
    for index, source in enumerate(source_rows):
        if index in ignored:
            continue
        lines = _heading_lines(str(source.get("text") or ""))
        if any(_heading_line_matches(line, anchor) for line in lines for anchor in anchors):
            return index
    return None


def _first_heading_after(
    source_rows: list[dict], anchors: Iterable[str], ignored: set[int], start_index: int,
) -> int | None:
    """Locate a contract end heading after a known section start.

    End anchors are exclusive boundaries: the source containing the next
    heading belongs to the next section, not to the current section.  This is
    deliberately separate from title matching because a document can omit the
    next configured section while still retaining a reliable boundary heading.
    """
    for index in range(start_index + 1, len(source_rows)):
        if index in ignored:
            continue
        lines = _heading_lines(str(source_rows[index].get("text") or ""))
        if any(_heading_line_matches(line, anchor) for line in lines for anchor in anchors):
            return index
    return None


def _locate_deterministically(source_rows: list[dict], contract: DocumentContract) -> dict[str, int]:
    ignored = _toc_like_indices(source_rows, contract)
    return {
        role: index
        for role, item in contract.sections.items()
        if (index := _first_heading_index(
            source_rows, tuple(dict.fromkeys((*item.title_anchors, *_SECTION_TITLE_ALIASES.get(role, ())))), ignored,
        )) is not None
    }


def _locate_end_anchors(
    source_rows: list[dict], contract: DocumentContract, starts: dict[str, int],
) -> dict[str, int]:
    ignored = _toc_like_indices(source_rows, contract)
    return {
        role: index
        for role, item in contract.sections.items()
        if (item.end_anchors or _SECTION_END_ALIASES.get(role))
        and role in starts
        and (index := _first_heading_after(
            source_rows,
            tuple(dict.fromkeys((*item.end_anchors, *_SECTION_END_ALIASES.get(role, ())))),
            ignored,
            starts[role],
        )) is not None
    }


def _llm_locate_missing(
    source_rows: list[dict], contract: DocumentContract, missing_roles: list[str], context: LlmCallContext | None,
) -> tuple[dict[str, tuple[int, int | None]], set[str], LlmUsage]:
    if not missing_roles or not is_configured():
        return {}, set(), LlmUsage()
    candidates = []
    for source in source_rows:
        text = str(source.get("text") or "")
        compact = _compact(text)
        if re.search(r"第[一二三四五六七八九十\d]+节|评级|发行|募集|财务|增信|信用|债券", compact):
            candidates.append({**source, "text": text[:700]})
    candidates = candidates[:80] or [{**item, "text": str(item.get("text") or "")[:700]} for item in source_rows[:40]]
    if not candidates:
        return {}, set(), LlmUsage()
    requested = {
        role: {"title_anchors": contract.sections[role].title_anchors}
        for role in missing_roles
    }
    located, usage = llm_locate_document_sections(candidates, requested, context=context)
    source_index = {str(item.get("id")): index for index, item in enumerate(source_rows)}
    output: dict[str, tuple[int, int | None]] = {}
    ambiguous: set[str] = set()
    for item in located:
        role = str(item.get("section_role") or "")
        source_id = str(item.get("start_source_id") or "")
        end_source_id = str(item.get("end_source_id") or "")
        if role not in requested:
            continue
        if source_id not in source_index:
            ambiguous.add(role)
            continue
        start_index = source_index[source_id]
        end_index = source_index.get(end_source_id) if end_source_id else None
        if end_source_id and end_index is None:
            ambiguous.add(role)
            continue
        if end_index is not None and end_index < start_index:
            ambiguous.add(role)
            continue
        anchors = contract.sections[role].title_anchors
        if not _source_has_title(source_rows[start_index], str(item.get("title") or ""), anchors):
            ambiguous.add(role)
            continue
        output[role] = (start_index, end_index)
    return output, ambiguous, usage


def locate_document_sections(
    source_rows: list[dict], document_role: str, context: LlmCallContext | None = None,
) -> tuple[list[LocatedSection], LlmUsage]:
    contract = _document_contract(document_role)
    ordered = sorted(source_rows, key=_source_sort_key)
    if not contract or not ordered:
        return [], LlmUsage()
    starts = _locate_deterministically(ordered, contract)
    missing = [role for role, item in contract.sections.items() if item.required and role not in starts]
    llm_sections, ambiguous, usage = _llm_locate_missing(ordered, contract, missing, context)
    llm_starts = {role: location[0] for role, location in llm_sections.items()}
    llm_ends = {role: location[1] for role, location in llm_sections.items() if location[1] is not None}
    starts.update(llm_starts)
    deterministic_ends = _locate_end_anchors(ordered, contract, starts)
    ordered_starts = sorted(starts.items(), key=lambda item: item[1])
    next_start = {
        role: ordered_starts[index + 1][1] if index + 1 < len(ordered_starts) else len(ordered)
        for index, (role, _) in enumerate(ordered_starts)
    }
    result: list[LocatedSection] = []
    for role, item in contract.sections.items():
        start_index = starts.get(role)
        if start_index is None:
            result.append(LocatedSection(
                role, "", "", "", None, None, "", 0.0,
                "ambiguous" if role in ambiguous else "missing", item.effective_fact_keys,
                item.subject_policy, tuple(item.scope_policy),
            ))
            continue
        end_exclusive = min(next_start.get(role, len(ordered)), start_index + item.max_pages)
        if role in deterministic_ends:
            end_exclusive = min(end_exclusive, deterministic_ends[role])
        if role in llm_ends:
            end_exclusive = min(end_exclusive, llm_ends[role] + 1)
        end_index = max(start_index, end_exclusive - 1)
        start = ordered[start_index]
        end = ordered[end_index]
        result.append(LocatedSection(
            role, str(start.get("text") or "")[:80], str(start["id"]), str(end["id"]),
            start.get("page"), end.get("page"), "llm" if role in llm_starts else "heading",
            0.9 if role in llm_starts else 1.0, "located", item.effective_fact_keys,
            item.subject_policy, tuple(item.scope_policy),
        ))
    return result, usage


def persist_document_sections(project_id: str, document_id: str, sections: list[LocatedSection]) -> None:
    now = now_iso()
    with connect() as conn:
        conn.execute("DELETE FROM document_sections WHERE project_id=? AND document_id=?", (project_id, document_id))
        for item in sections:
            content_hash = hashlib.sha256(json.dumps({
                "role": item.role, "start": item.start_source_id, "end": item.end_source_id,
                "status": item.status, "contract": contract_sha256(),
            }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO document_sections (id,project_id,document_id,section_role,title,start_source_id,end_source_id,"
                "start_page,end_page,locator_method,confidence,status,contract_version,content_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (hashlib.sha256(f"{project_id}:{document_id}:{item.role}".encode()).hexdigest()[:32], project_id, document_id,
                 item.role, item.title, item.start_source_id, item.end_source_id, item.start_page, item.end_page,
                 item.locator_method, item.confidence, item.status, load_extraction_contract().version, content_hash, now, now),
            )


def build_section_extraction_plans(
    project_id: str, source_rows: list[dict], context: LlmCallContext | None = None,
) -> tuple[list[SectionExtractionPlan], list[dict], LlmUsage]:
    """Index bond documents and return only sources permitted by their section role."""
    by_document: dict[str, list[dict]] = {}
    for item in source_rows:
        role = str(item.get("document_role") or "")
        if _document_contract(role):
            by_document.setdefault(str(item["document_id"]), []).append(item)

    plans: list[SectionExtractionPlan] = []
    diagnostics: list[dict] = []
    total_usage = LlmUsage()
    for document_id, document_sources in by_document.items():
        document_role = str(document_sources[0].get("document_role") or "")
        sections, usage = locate_document_sections(document_sources, document_role, context)
        total_usage.input_tokens += usage.input_tokens
        total_usage.output_tokens += usage.output_tokens
        total_usage.cost += usage.cost
        total_usage.latency_ms += usage.latency_ms
        persist_document_sections(project_id, document_id, sections)
        ordered = sorted(document_sources, key=_source_sort_key)
        by_id = {str(item["id"]): index for index, item in enumerate(ordered)}
        for section in sections:
            if section.status != "located":
                if _document_contract(document_role).sections[section.role].required:
                    diagnostics.append({"document_id": document_id, "section_role": section.role, "reason": "required_section_missing"})
                continue
            if not section.allowed_fact_keys:
                continue
            start = by_id[section.start_source_id]
            end = by_id[section.end_source_id]
            sources = tuple({**item, "_section_role": section.role} for item in ordered[start:end + 1])
            plans.append(SectionExtractionPlan(section, sources))
    return plans, diagnostics, total_usage


def offering_term_sources(plans: list[SectionExtractionPlan]) -> list[dict]:
    output: list[dict] = []
    seen: set[str] = set()
    for plan in plans:
        if plan.section.role != "offering_terms":
            continue
        for source in plan.sources:
            if source["id"] not in seen:
                seen.add(source["id"])
                output.append(source)
    return output


def extraction_diagnostics(project_id: str) -> list[dict]:
    return rows(
        "SELECT document_id,section_role,status,start_page,end_page,locator_method FROM document_sections "
        "WHERE project_id=? AND status!='located' ORDER BY document_id,section_role",
        (project_id,),
    )


def section_location_tasks(project_id: str) -> list[dict]:
    grouped: dict[str, list[str]] = {}
    document_roles = {
        item["id"]: item["document_role"]
        for item in rows("SELECT id,document_role FROM documents WHERE project_id=?", (project_id,))
    }
    for item in extraction_diagnostics(project_id):
        contract = _document_contract(document_roles.get(item["document_id"], ""))
        section_contract = contract.sections.get(item["section_role"]) if contract else None
        if not section_contract or not section_contract.required:
            continue
        grouped.setdefault(item["document_id"], []).append(item["section_role"])
    return [
        {
            "task_type": "section_location", "priority": "high", "title": "定位债券材料章节边界",
            "description": f"文档 {document_id} 未能可靠定位必需章节：{'、'.join(sorted(roles))}。",
            "related_ids": [], "required_evidence": "可检索的募集说明书或评级报告正文", 
            "issue_key": f"section-location:{document_id}", "issue_category": "document_structure",
            "affected_fact_ids": [], "affected_rule_ids": [], "blocking_level": "blocking",
            "source_requirement": "可检索的募集说明书或评级报告正文",
        }
        for document_id, roles in grouped.items()
    ]
