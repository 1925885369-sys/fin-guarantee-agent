from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import mimetypes
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .chapter_guard import (
    NO_MODEL_REASON, assess_chapter_length, build_chapter_context, count_report_chars,
    validate_chapter_output, validation_diagnostics,
)
from .chapter_prompts import (
    BOND_CHAPTER_KEYS, BOND_CHAPTER_PART, BOND_CHAPTER_TITLES, chapter_default_prompt, chapter_uses_llm,
    compile_structured_prompt, editable_config_from_factory, effective_chapter_config,
    load_chapter_config,
    legacy_prompt_config, migrate_legacy_prompt_overrides, prompt_config_payload, prompt_edit_state,
    recompile_structured_prompts,
)
# 注意：chapter_prompts.seed_global_prompts_from_latest_overrides（把项目 Prompt 自动
# 提升为全局默认）已按修订稿要求从所有启动入口移除，这里也不得再导入/调用。
from .database import connect, execute, init_db, json_dump, json_load, now_iso, recover_interrupted_runs, row, rows
from .exports import docx_report, markdown_report
from .facts import FACT_REGISTRY, decode_facts, material_inventory
from .entity_profiles import (
    bind_project_profiles, create_profile, create_project_snapshot, get_profile,
    get_project_snapshot, list_profile_versions, list_profiles, profile_readiness,
    profile_version_diff, project_profile_evidence, project_snapshot_status,
    seed_entity_profiles, snapshot_readiness, update_profile,
)
from .profile_imports import commit_import_job, create_import_job, get_import_job
from .profile_evidence import materialize_snapshot_evidence
from .llm import LlmCallContext, StructuredLlmError, answer_material_question, combine_usage, is_configured, rewrite_section
from .metrics import decode_metrics
from .parsers import SUPPORTED, classify_document, parse_document
from .p0 import (
    calculation_trace, decode_entities, decode_tie_outs, evidence_graph,
    migrate_report_generation_status, report_quality,
    sync_project_entities, validate_report_support,
)
from .policy import load_policy
from .professional import audit_event, canonical_fact_rows, fact_groups, resolve_fact_group, sync_fact_assertions
from .report_blocks import section_blocks
from .report_outline import section_labels
from .report_delivery import (
    delivery_blockers, reconcile_historical_sections, review_notice, write_report_section,
)
from .review import decode_candidates, decode_tasks, decide_candidate, review_summary, unresolved_items, update_task
from .review_lifecycle import mark_report_reviewed, report_state
from .bond_case import PROJECT_ID as BOND_PROJECT_ID, _materials as bond_materials, seed_bond_case
from .bond_portfolio_cases import generate_bond_portfolio_materials, seed_bond_portfolio_cases
from .real_reference import ensure_project_classification, is_real_reference, seed_real_reference_projects
from .schemas import (
    CandidateDecision, DocumentUpdate, EntityProfileCreate, EntityProfileUpdate, ReportTableRefresh,
    FactGroupResolve, FactUpdate, MaterialQARequest, ProfileImportCommit, ProjectCreate, PromptEditableConfig, ReportPromptPreview, ReportPromptUpdate, ReviewTaskUpdate, RunCreate, SectionUpdate, TableCellReview,
)
from .security import check_rate, cleanup_expired_projects, validate_magic
from .retrieval import index_diagnostics, retrieval_status, retrieve_chunks
from .workflow import TERMINAL, _attribution_block_missing, _numbers_supported, _preserve_chapter_boundaries, _report_boundary_safe, apply_review_decisions, invalidate_project_analysis, repair_chapter_length, retry_run, start_run


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        cleanup_expired_projects()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    migrate_legacy_prompt_overrides()
    recompile_structured_prompts()
    migrate_report_generation_status()
    reconcile_historical_sections()
    recover_interrupted_runs()
    cleanup_expired_projects()
    seed_bond_case()
    seed_bond_portfolio_cases()
    ensure_project_classification()
    seed_real_reference_projects()
    seed_entity_profiles()
    task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="研担 · 融资担保尽调 Agent", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def startup() -> None:
    init_db()
    migrate_legacy_prompt_overrides()
    recompile_structured_prompts()
    migrate_report_generation_status()
    reconcile_historical_sections()
    recover_interrupted_runs()
    cleanup_expired_projects()
    seed_bond_case()
    seed_bond_portfolio_cases()
    ensure_project_classification()
    seed_real_reference_projects()
    seed_entity_profiles()
    # 修订稿要求：启动入口不得把项目 Prompt 自动提升为全局默认。
    # 原 startup() 曾调用 seed_global_prompts_from_latest_overrides()，此处已移除；
    # 既有全局配置与历史覆盖保持不动（仅不再自动新增/扩散）。


def is_demo_project(project_id: str) -> bool:
    """演示/虚构项目对任何访问方公开（产品只做债券担保）。"""
    return project_id.startswith("demo-")


def _project(project_id: str) -> dict:
    item = row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not item:
        raise HTTPException(404, "项目不存在")
    return item


def _authorize(project_id: str, token: Optional[str]) -> dict:
    item = _project(project_id)
    public = is_demo_project(project_id) or is_real_reference(project_id)
    if not public and not secrets.compare_digest(token or "", item["access_token"]):
        raise HTTPException(403, "项目访问令牌无效")
    return item


def _ensure_project_writable(project: dict) -> None:
    if project.get("archived_at"):
        raise HTTPException(409, "项目已归档，请先恢复项目后再修改")


# 代理转发头：任何一个存在都说明请求可能经代理/平台转发，不能视为本机直连。
_PROMPT_PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded")


def _is_loopback_client(request: Request) -> bool:
    """request.client.host 是否为本机环回地址。"""
    client = request.client
    if not client or not client.host:
        return False
    host = str(client.host)
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() in {"localhost"}


def _has_proxy_forwarding_headers(request: Request) -> bool:
    return any(str(request.headers.get(name) or "").strip() for name in _PROMPT_PROXY_HEADERS)


def prompt_edit_permissions(project_id: str, request: Request) -> dict:
    """报告 Prompt 的"项目级 vs 全局级"可编辑权限。

    规则（修订稿 §4）：

    - 非真实参考项目：项目级可编辑（受既有项目令牌与归档门禁约束）；
    - 真实参考项目（``data_class == 'real_reference'``）：仅当**非公开环境**、
      请求来自**本机直连**（``request.client.host`` 为环回地址）且**不带任何代理转发头**
      （``x-forwarded-for`` / ``x-real-ip`` / ``forwarded`` 均缺失）时，项目级才可编辑；
    - 全局默认：任何环境、任何项目一律**不可编辑**。
    """
    if not is_real_reference(project_id):
        return {
            "editable": True, "global_editable": False, "real_reference": False,
            "reason": "", "environment": "public_demo" if settings.public_demo else "local",
        }
    local = _is_loopback_client(request)
    forwarded = _has_proxy_forwarding_headers(request)
    if settings.public_demo:
        reason = "公开演示环境不允许修改真实参考项目的 Prompt"
    elif forwarded:
        reason = "请求带有代理转发头，无法确认本机直连，拒绝修改真实参考项目 Prompt"
    elif not local:
        reason = "真实参考项目的项目级 Prompt 仅允许本机直连（环回地址）修改"
    else:
        reason = ""
    return {
        "editable": (not settings.public_demo) and local and not forwarded,
        "global_editable": False, "real_reference": True, "reason": reason,
        "environment": "public_demo" if settings.public_demo else "local",
    }


def _prompt_permission_fields(permissions: Optional[dict]) -> dict:
    permissions = permissions or {}
    return {
        "editable": bool(permissions.get("editable", True)),
        "global_editable": bool(permissions.get("global_editable", False)),
        "edit_reason": str(permissions.get("reason") or ""),
        "real_reference": bool(permissions.get("real_reference", False)),
    }


def _require_prompt_editable(project_id: str, request: Request) -> dict:
    """项目级 Prompt 保存/重置的门禁，返回权限字典供响应体复用。"""
    permissions = prompt_edit_permissions(project_id, request)
    if not permissions["editable"]:
        raise HTTPException(403, permissions["reason"] or "当前环境不允许修改项目 Prompt")
    return permissions


def _fetch_effective_prompt_override(project_id: str, chapter_key: str) -> tuple[Optional[dict], str]:
    """返回生效的 Prompt 覆盖行（项目覆盖 > 全局默认）及其来源。"""
    project_override = row(
        "SELECT prompt,structured_json,migration_mode FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?",
        (project_id, chapter_key),
    )
    if project_override and str(project_override.get("prompt") or "").strip():
        return project_override, "project"
    global_override = row(
        "SELECT prompt,structured_json,migration_mode FROM global_report_prompts WHERE chapter_key=?",
        (chapter_key,),
    )
    if global_override and str(global_override.get("prompt") or "").strip():
        return global_override, "global"
    return None, "system"


def _prompt_outdated(project_id: str, chapter_key: str) -> bool:
    """章节 Prompt 覆盖是否**早于当前编译器版本**。

    ``recompile_structured_prompts`` 的判定口径：结构化覆盖存库的 ``prompt`` 文本是
    由 ``structured_json`` 经**当前编译器**生成的派生值。若库里文本与当前编译器对同一
    ``structured_json`` 的重编译结果不一致，说明该覆盖生成于旧编译器，需要重新编译（过期）。
    人工撰写的 legacy/preserved 覆盖不参与重编译，恒判为不过期。
    """
    override, _source = _fetch_effective_prompt_override(project_id, chapter_key)
    if not override:
        return False
    if str(override.get("migration_mode") or "") != "structured":
        return False
    try:
        config = json_load(override.get("structured_json") or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(config, dict) or not config:
        return False
    try:
        recompiled = compile_structured_prompt(chapter_key, config)
    except Exception:
        return False
    return recompiled != str(override.get("prompt") or "")


def _normalize_bond_mode(project: dict) -> dict:
    """Expose the unified mode for both new and historical bond projects."""
    if project.get("project_type") == "bond_guarantee":
        project["analysis_target"] = "full_due_diligence"
        if project.get("effective_analysis_mode") == "core_pre_review":
            project["effective_analysis_mode"] = "full_due_diligence"
    return project


def _enforce_profile_snapshot_readiness(project: dict) -> None:
    """Apply the profile gate only to projects created with a frozen snapshot."""
    if not project.get("profile_snapshot_id"):
        return
    readiness = snapshot_readiness(project["id"])
    if readiness.get("ready_for_agent"):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "profile_snapshot_not_ready",
            "message": "冻结的发行人主体档案未通过 Agent 启动门禁",
            "snapshot_id": readiness.get("snapshot_id") or project.get("profile_snapshot_id"),
            "snapshot_version": readiness.get("version"),
            "hard_vetoes": readiness.get("hard_vetoes", []),
            "blocking_reasons": readiness.get("blocking_reasons", []),
        },
    )


def _enforce_core_materials_readiness(project: dict) -> None:
    if project.get("project_type") != "bond_guarantee":
        return
    inventory = material_inventory(project["id"])
    if inventory.get("core_materials_ready"):
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "core_materials_not_ready",
            "message": "募集说明书和信用评级报告尚未齐备，不能启动债券尽调 Agent",
            "blocking_items": inventory.get("blocking_items", []),
            "analysis_mode": inventory.get("analysis_mode", "insufficient"),
        },
    )


def _enforce_analysis_readiness(project: dict) -> None:
    _enforce_profile_snapshot_readiness(project)
    _enforce_core_materials_readiness(project)


def _authorize_run(run_id: str, token: Optional[str]) -> dict:
    item = row("SELECT * FROM runs WHERE id=?", (run_id,))
    if not item:
        raise HTTPException(404, "任务不存在")
    _authorize(item["project_id"], token)
    return item


def _decode(items: list[dict], fields: tuple[str, ...]) -> list[dict]:
    for item in items:
        for field in fields:
            if field in item:
                item[field] = json_load(item[field])
    return items


DOCUMENT_KIND_ROLES = {
    "募集说明书": "prospectus",
    "信用评级报告": "rating_report",
    "其他材料": "other",
}


def _document_role(kind: str) -> str:
    return DOCUMENT_KIND_ROLES.get(kind, "other")


def _ensure_document_mutable(project: dict) -> None:
    _ensure_project_writable(project)
    if project.get("data_class", "user_project") != "user_project":
        raise HTTPException(403, "仅用户创建的项目可以修改材料分类")
    active = row("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (project["id"],))
    if active:
        raise HTTPException(409, f"项目正在分析，完成或取消任务后再修改材料：{active['id']}")


def _ensure_profile_api_available() -> None:
    if settings.public_demo:
        raise HTTPException(403, "公开演示环境不开放私有主体档案")


@app.get("/api/health")
def health() -> dict:
    policy = load_policy("bond-v1")
    return {"status": "ok", "modelConfigured": is_configured(), "model": settings.llm_model, "version": "0.4.0", "policy": policy.profile, "policyVersion": policy.version,
            "ocrConfigured": bool(settings.ocr_enabled and settings.ocr_api_key and settings.ocr_model), "ocrModel": settings.ocr_model,
            "retrieval": retrieval_status(),
            # 上传弹窗要显示格式与大小限制。这些限制来自环境变量，界面读一份即可，
            # 不要在前端再写一份常量，否则调整限制后提示与实际校验会不一致。
            "uploadLimits": {"maxFileMb": settings.max_file_mb, "maxFiles": settings.max_files, "formats": sorted(SUPPORTED)}}


@app.get("/api/projects")
def list_projects(data_class: Optional[str] = None) -> list[dict]:
    columns = (
        "id,company_name,amount,term_months,purpose,status,project_type,bond_name,data_class,evaluation_eligible,"
        "issuer_profile_id,profile_snapshot_id,analysis_target,effective_analysis_mode,analysis_stale,"
        "review_revision,report_revision,report_reviewed_revision,report_reviewed_at,report_stale,report_stale_reasons,"
        "counter_guarantee_status,created_at,updated_at"
        ",archived_at"
    )
    if data_class:
        if data_class not in {"fictional_demo", "real_reference", "user_project"}:
            raise HTTPException(400, "无效的项目数据分类")
        if data_class == "user_project" and settings.public_demo:
            raise HTTPException(403, "公开演示环境不提供私有项目列表")
        return [
            _normalize_bond_mode(item)
            for item in rows(
            f"SELECT {columns} FROM projects WHERE data_class=? AND project_type='bond_guarantee' "
            "ORDER BY CASE WHEN id='demo-bond-huachen' THEN 0 ELSE 1 END, created_at,id",
            (data_class,),
            )
        ]
    # 默认返回债券演示评测案例（保持既有 6 套虚构案例页面不变）；
    # 真实参考项目通过 ?data_class=real_reference 单独查询，前端据此分组。
    return [
        _normalize_bond_mode(item)
        for item in rows(
            f"SELECT {columns} FROM projects WHERE data_class='fictional_demo' AND project_type='bond_guarantee' ORDER BY CASE WHEN id='demo-bond-huachen' THEN 0 ELSE 1 END, created_at,id"
        )
    ]


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    item = _normalize_bond_mode(_authorize(project_id, x_project_token))
    inventory_state = material_inventory(project_id)
    item["effective_analysis_mode"] = inventory_state["effective_analysis_mode"]
    item["analysis_stale"] = bool(item.get("analysis_stale", 0))
    item.pop("access_token", None)
    review_state = review_summary(project_id)
    item.update(report_state(project_id))
    item["stats"] = {
        "documents": row("SELECT COUNT(*) count FROM documents WHERE project_id=?", (project_id,))["count"],
        "facts": row("SELECT COUNT(*) count FROM facts WHERE project_id=?", (project_id,))["count"],
        "checks": row("SELECT COUNT(*) count FROM cross_checks WHERE project_id=?", (project_id,))["count"],
        "risks": row("SELECT COUNT(*) count FROM risks WHERE project_id=?", (project_id,))["count"],
        "highRisks": row("SELECT COUNT(*) count FROM risks WHERE project_id=? AND severity='high'", (project_id,))["count"],
        "conflicts": row("SELECT COUNT(*) count FROM canonical_facts WHERE project_id=? AND status='conflicted'", (project_id,))["count"],
        "openTasks": review_state["total_unique_issues"],
        "candidates": row("SELECT COUNT(*) count FROM candidate_findings WHERE project_id=? AND status='proposed'", (project_id,))["count"],
    }
    return item


@app.get("/api/projects/{project_id}/runs")
def project_runs(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return rows("SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC,rowid DESC", (project_id,))


@app.post("/api/projects/{project_id}/archive")
def archive_project(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize(project_id, x_project_token)
    if project.get("data_class", "user_project") != "user_project":
        raise HTTPException(403, "仅用户创建的项目可以归档")
    now = now_iso()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not current:
            raise HTTPException(404, "项目不存在")
        active = conn.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running') LIMIT 1", (project_id,)).fetchone()
        if active:
            raise HTTPException(409, f"项目已有活动任务：{active['id']}，完成或取消后再归档")
        if current["archived_at"]:
            return {"id": project_id, "archived": True, "archived_at": current["archived_at"], "changed": False}
        conn.execute("UPDATE projects SET archived_at=?,updated_at=? WHERE id=?", (now, now, project_id))
    audit_event(project_id, "project_archive", "project", project_id, dict(project), {"archived_at": now}, "human")
    return {"id": project_id, "archived": True, "archived_at": now, "changed": True}


@app.post("/api/projects/{project_id}/restore")
def restore_project(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize(project_id, x_project_token)
    if project.get("data_class", "user_project") != "user_project":
        raise HTTPException(403, "仅用户创建的项目可以恢复")
    now = now_iso()
    if not project.get("archived_at"):
        return {"id": project_id, "archived": False, "archived_at": None, "changed": False}
    with connect() as conn:
        conn.execute("UPDATE projects SET archived_at=NULL,updated_at=? WHERE id=?", (now, project_id))
    audit_event(project_id, "project_restore", "project", project_id, {"archived_at": project["archived_at"]}, {"archived_at": None}, "human")
    return {"id": project_id, "archived": False, "archived_at": None, "changed": True}


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate, request: Request) -> dict:
    if not payload.data_consent:
        raise HTTPException(400, "请确认仅上传公开或虚构材料，并同意模型处理")
    check_rate(request.client.host if request.client else "unknown", "project", 5)
    issuer_profile = None
    uses_project_subject_names = bool(payload.guarantor_name.strip() or payload.counter_guarantor_names)
    uses_legacy_profile_roles = bool(payload.guarantor_profile_id or payload.counter_guarantor_profile_ids)
    if uses_project_subject_names and uses_legacy_profile_roles:
        raise HTTPException(422, "保证人名称与旧版主体档案绑定字段不能同时提交")
    if payload.issuer_profile_id:
        _ensure_profile_api_available()
        issuer_profile = get_profile(payload.issuer_profile_id)
        if not issuer_profile:
            raise HTTPException(422, "发行人主体档案不存在")
        if uses_legacy_profile_roles:
            related_profile_ids = [payload.guarantor_profile_id, *payload.counter_guarantor_profile_ids]
            if any(profile_id and not get_profile(profile_id) for profile_id in related_profile_ids):
                raise HTTPException(422, "保证人或反担保主体档案不存在")
    project_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(24)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,project_type,bond_name,consent_at,issuer_profile_id,analysis_target,effective_analysis_mode,analysis_stale,counter_guarantee_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, token, issuer_profile["name"] if issuer_profile else payload.company_name,
             payload.amount, payload.term_months, payload.purpose, "draft", now, now, payload.project_type,
             payload.bond_name, now, payload.issuer_profile_id, "full_due_diligence", "insufficient", 0,
             payload.counter_guarantee_status),
        )
    if payload.issuer_profile_id:
        bind_project_profiles(
            project_id, payload.issuer_profile_id,
            payload.guarantor_profile_id if uses_legacy_profile_roles else None,
            payload.counter_guarantor_profile_ids if uses_legacy_profile_roles else [],
        )
        create_project_snapshot(project_id)
        execute("UPDATE projects SET analysis_stale=0 WHERE id=?", (project_id,))
        try:
            materialize_snapshot_evidence(project_id)
        except Exception:
            # The projection function records the failed evidence status. A
            # recoverable projection failure must not roll back project intake.
            pass
    project_subjects = []
    if payload.guarantor_name.strip():
        project_subjects.append(("guarantor", payload.guarantor_name.strip()))
    project_subjects.extend(
        ("counter_guarantor", name.strip()) for name in payload.counter_guarantor_names if name.strip()
    )
    with connect() as conn:
        for entity_type, name in dict.fromkeys(project_subjects):
            conn.execute(
                "INSERT OR IGNORE INTO project_entities (id,project_id,entity_type,name,is_borrower,status,attributes_json,created_at,updated_at) "
                "VALUES (?,?,?,?,0,'unreviewed',?,?,?)",
                (str(uuid.uuid4()), project_id, entity_type, name,
                 json_dump({"role": entity_type, "source": "project_intake"}), now, now),
            )
    project_state = row("SELECT profile_snapshot_id FROM projects WHERE id=?", (project_id,)) or {}
    return {
        "id": project_id, "accessToken": token, "projectType": payload.project_type,
        "profile_snapshot_id": project_state.get("profile_snapshot_id"),
    }


@app.get("/api/entity-profiles")
def entity_profiles(search: str = "", status: str = "") -> list[dict]:
    _ensure_profile_api_available()
    if status and status not in {"ready", "stale", "conflicted", "incomplete"}:
        raise HTTPException(400, "无效的主体档案状态")
    return list_profiles(search=search, status=status)


@app.post("/api/entity-profiles", status_code=201)
def post_entity_profile(payload: EntityProfileCreate) -> dict:
    _ensure_profile_api_available()
    try:
        return create_profile(payload.model_dump())
    except ValueError as exc:
        status_code = 409 if "统一社会信用代码已存在" in str(exc) else 422
        raise HTTPException(status_code, str(exc)) from exc


@app.get("/api/entity-profiles/{profile_id}")
def entity_profile(profile_id: str) -> dict:
    _ensure_profile_api_available()
    item = get_profile(profile_id)
    if not item:
        raise HTTPException(404, "主体档案不存在")
    return item


@app.patch("/api/entity-profiles/{profile_id}")
def patch_entity_profile(profile_id: str, payload: EntityProfileUpdate) -> dict:
    _ensure_profile_api_available()
    try:
        return update_profile(profile_id, payload.model_dump(exclude_unset=True))
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/entity-profiles/{profile_id}/readiness")
def entity_profile_readiness(profile_id: str) -> dict:
    _ensure_profile_api_available()
    try:
        return profile_readiness(profile_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/entity-profiles/{profile_id}/versions")
def entity_profile_versions(profile_id: str) -> list[dict]:
    _ensure_profile_api_available()
    try:
        return list_profile_versions(profile_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/entity-profiles/{profile_id}/versions/{version}")
def entity_profile_version(profile_id: str, version: int, compare_to: Optional[int] = None) -> dict:
    _ensure_profile_api_available()
    item = get_profile(profile_id, version)
    if not item:
        raise HTTPException(404, "主体档案版本不存在")
    try:
        item["diff"] = profile_version_diff(profile_id, version, compare_to)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return item


@app.post("/api/entity-profile-imports", status_code=202)
async def create_entity_profile_import(
    structured_file: UploadFile = File(...), profile_id: Optional[str] = Form(default=None),
    target_profile_id: Optional[str] = Form(default=None),
    package_kind: str = Form(default=""), baseline_date: Optional[str] = Form(default=None),
    period: Optional[str] = Form(default=None), scope: str = Form(default="entity"),
    evidence_files: list[UploadFile] = File(default=[]),
) -> dict:
    _ensure_profile_api_available()
    structured_content = await structured_file.read(settings.max_file_mb * 1024 * 1024 + 1)
    if not structured_content:
        raise HTTPException(400, "结构化导入文件为空")
    if len(structured_content) > settings.max_file_mb * 1024 * 1024:
        raise HTTPException(413, f"结构化导入文件不能超过 {settings.max_file_mb} MB")
    suffix = Path(structured_file.filename or "").suffix.lower()
    if suffix not in {".json", ".xlsx"}:
        raise HTTPException(415, "结构化导入仅支持 JSON 或 XLSX")
    if suffix == ".xlsx":
        validate_magic(structured_file.filename or "import.xlsx", structured_content)
    evidence_payload: list[tuple[str, str, bytes]] = []
    for evidence in evidence_files:
        content = await evidence.read(settings.max_file_mb * 1024 * 1024 + 1)
        if not content:
            continue
        if len(content) > settings.max_file_mb * 1024 * 1024:
            raise HTTPException(413, f"证据文件不能超过 {settings.max_file_mb} MB")
        evidence_name = evidence.filename or "evidence"
        if Path(evidence_name).suffix.lower() not in SUPPORTED:
            raise HTTPException(415, "证据文件仅支持 PDF、DOCX 或 XLSX")
        validate_magic(evidence_name, content)
        evidence_payload.append((evidence_name, evidence.content_type or "application/octet-stream", content))
    try:
        return create_import_job(
            structured_file.filename or f"import{suffix}", structured_content,
            profile_id=target_profile_id or profile_id, evidence_files=evidence_payload,
            package_kind=package_kind, baseline_date=baseline_date, period=period, scope=scope,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/entity-profile-imports/{job_id}")
def entity_profile_import(job_id: str) -> dict:
    _ensure_profile_api_available()
    item = get_import_job(job_id)
    if not item:
        raise HTTPException(404, "导入任务不存在")
    return item


@app.post("/api/entity-profile-imports/{job_id}/commit")
def commit_entity_profile_import(job_id: str, payload: ProfileImportCommit) -> dict:
    _ensure_profile_api_available()
    try:
        return commit_import_job(job_id, payload)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/projects/{project_id}/profile-snapshot")
def project_profile_snapshot(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    snapshot = get_project_snapshot(project_id)
    if not snapshot:
        raise HTTPException(404, "项目尚未绑定主体档案")
    return snapshot


@app.get("/api/projects/{project_id}/profile-snapshot/status")
def profile_snapshot_status(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    try:
        return project_snapshot_status(project_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/profile-evidence")
def profile_evidence(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    try:
        snapshot = get_project_snapshot(project_id)
        if snapshot and snapshot.get("evidence_status") == "pending":
            try:
                materialize_snapshot_evidence(project_id, snapshot["id"])
            except Exception:
                pass
        return project_profile_evidence(project_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/projects/{project_id}/profile-snapshot/refresh")
def refresh_project_profile_snapshot(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize(project_id, x_project_token)
    _ensure_document_mutable(project)
    try:
        snapshot = create_project_snapshot(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    invalidate_project_analysis(project_id, "extract")
    try:
        materialize_snapshot_evidence(project_id, snapshot["id"])
        snapshot = get_project_snapshot(project_id) or snapshot
    except Exception:
        snapshot = get_project_snapshot(project_id) or snapshot
    audit_event(
        project_id, "refresh", "profile_snapshot", snapshot["id"],
        after={"version": snapshot["version"]}, actor="user",
    )
    return snapshot


@app.get("/api/projects/{project_id}/document-inventory")
def inventory(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    return material_inventory(project_id)


@app.get("/api/projects/{project_id}/bond-summary")
def bond_summary(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize(project_id, x_project_token)
    if project.get("project_type") != "bond_guarantee":
        raise HTTPException(409, "该项目不是债券发行担保项目")
    fact_rows = decode_facts(rows("SELECT * FROM facts WHERE project_id=?", (project_id,)))
    canonical_rows = rows("SELECT * FROM canonical_facts WHERE project_id=?", (project_id,))
    conflicted_fields = sorted({item["fact_key"] for item in canonical_rows if item.get("status") == "conflicted"})
    by_key: dict[str, dict] = {}
    if canonical_rows:
        for item in canonical_rows:
            key = item.get("fact_key", "")
            if key and item.get("status") not in {"conflicted", "stale", "insufficient"} and (key not in by_key or str(item.get("period", "")) >= str(by_key[key].get("period", ""))):
                by_key[key] = item
    else:
        for item in fact_rows:
            key = item.get("fact_key", "")
            if key and (key not in by_key or str(item.get("period", "")) >= str(by_key[key].get("period", ""))):
                by_key[key] = item
    def value(key: str):
        item = by_key.get(key)
        return None if not item else item.get("value") if item.get("value") is not None else item.get("text_value")
    def rating_value(key: str):
        raw = value(key)
        return re.sub(r"^(?:rating|评级)\s*[:：]\s*", "", str(raw), flags=re.IGNORECASE) if raw is not None else None
    inv = material_inventory(project_id)
    report_level = "完整尽调" if inv["complete_due_diligence_ready"] else "材料不足"
    issue_value = None if "bond_issue_amount" in conflicted_fields else (value("bond_issue_amount") or project["amount"])
    tranches = rows(
        "SELECT tranche_key,tranche_name,term_years,issue_amount,repayment_method,maturity_date,coupon_rate,amount_scope,allocation_status,source_ids,extraction_method,confidence "
        "FROM bond_tranches WHERE project_id=? AND extraction_method!='legacy_reference_seed' ORDER BY term_years",
        (project_id,),
    )
    for tranche in tranches:
        tranche["source_ids"] = json_load(tranche["source_ids"])
    single_term = (
        int(tranches[0]["term_years"]) if len(tranches) == 1 and tranches[0].get("term_years") is not None
        else value("bond_term_years") if len(tranches) == 0 else None
    )
    guarantee_type = value("guarantee_type")
    guarantee_applicable = bool(inv.get("guarantee_applicable", True))
    return {
        "projectId": project_id, "bondName": project.get("bond_name") or value("bond_name"),
        "issuer": project["company_name"], "issueAmount": issue_value,
        "currentIssueAmount": issue_value, "registeredAmount": value("registered_issue_amount"),
        "termYears": single_term, "couponRate": value("coupon_rate"),
        "repayment": value("principal_repayment"), "issuerRating": rating_value("issuer_rating"),
        "bondRating": rating_value("bond_rating"), "outlook": value("rating_outlook"),
        "guarantor": value("guarantor_name"), "guaranteeScope": value("guarantee_scope"),
        "guaranteePeriod": value("guarantee_period"), "guaranteeCap": value("guarantee_cap"),
        "guaranteeType": guarantee_type, "guaranteeApplicable": guarantee_applicable,
        "reportLevel": report_level,
        "dataClass": project.get("data_class", "user_project"),
        "evaluationEligible": bool(project.get("evaluation_eligible", 0)),
        "conflictedFields": conflicted_fields,
        "coreMaterialsReady": inv["core_materials_ready"],
        "completeDueDiligenceReady": inv["complete_due_diligence_ready"],
        "analysisMode": inv["analysis_mode"],
        "materialGaps": inv["gaps"],
        "supplementaryGaps": inv["supplementary_gaps"],
        "tranches": [dict(item) for item in tranches],
    }


@app.post("/api/projects/{project_id}/documents", status_code=201)
async def upload_document(
    project_id: str, request: Request, file: UploadFile = File(...),
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    project = _authorize(project_id, x_project_token)
    _ensure_document_mutable(project)
    check_rate(request.client.host if request.client else "unknown", "upload", 20)
    original = Path(file.filename or "document")
    suffix = original.suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(415, "仅支持文本型 PDF、DOCX、XLSX")
    content = await file.read(settings.max_file_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_file_mb * 1024 * 1024:
        raise HTTPException(413, f"单个文件不能超过 {settings.max_file_mb} MB")
    if not content:
        raise HTTPException(400, "文件为空")
    validate_magic(original.name, content)
    content_sha256 = hashlib.sha256(content).hexdigest()
    duplicate = row(
        "SELECT id,filename,parse_status FROM documents WHERE project_id=? AND sha256=?",
        (project_id, content_sha256),
    )
    if duplicate and duplicate["parse_status"] != "failed":
        raise HTTPException(
            409,
            detail={
                "code": "duplicate_document",
                "message": f"该文件已上传：{duplicate['filename']}",
                "document_id": duplicate["id"],
            },
        )
    retry_failed_document = bool(duplicate)
    count = row("SELECT COUNT(*) count FROM documents WHERE project_id=?", (project_id,))["count"]
    if not retry_failed_document and count >= settings.max_files:
        raise HTTPException(400, f"每个项目最多上传 {settings.max_files} 个文件")
    safe_stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", original.stem)[:60] or "document"
    document_id = duplicate["id"] if retry_failed_document else str(uuid.uuid4())
    directory = settings.resolved_data_dir / "uploads" / project_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document_id}_{safe_stem}{suffix}"
    path.write_bytes(content)
    parse_status, quality, warning, fragments = "parsed", 0.0, "", []
    parsed_result = None
    try:
        parsed_result = parse_document(path)
        quality, warning, fragments = parsed_result.quality, parsed_result.warning, parsed_result.fragments
        if parsed_result.ocr_status == "required":
            parse_status = "ocr_required"
        elif parsed_result.ocr_status == "failed" and not fragments:
            parse_status = "failed"
        elif warning and quality < 0.25:
            parse_status = "warning"
        existing_chars = row(
            "SELECT COALESCE(SUM(LENGTH(text)),0) total FROM sources WHERE project_id=? AND document_id<>?",
            (project_id, document_id),
        )["total"]
        if existing_chars + sum(len(item.text) for item in fragments) > settings.max_total_chars:
            raise ValueError("项目材料文字总量超过处理限制")
    except ValueError as exc:
        parse_status, warning, fragments = "failed", str(exc), []
    kind = classify_document(original.name, " ".join(fragment.text for fragment in fragments[:8]))
    document_role = _document_role(kind)
    media_type = file.content_type or mimetypes.guess_type(original.name)[0] or "application/octet-stream"
    with connect() as conn:
        if retry_failed_document:
            conn.execute("DELETE FROM sources WHERE project_id=? AND document_id=?", (project_id, document_id))
            conn.execute(
                "UPDATE documents SET filename=?,kind=?,media_type=?,sha256=?,size=?,parse_status=?,quality=?,locator_count=?,warning=?,parser=?,document_role=?,ocr_status=?,page_count=? WHERE id=? AND project_id=?",
                (original.name, kind, media_type, content_sha256, len(content), parse_status, quality, len(fragments), warning,
                 parsed_result.parser if parsed_result else "native", document_role,
                 parsed_result.ocr_status if parsed_result else "failed", parsed_result.page_count if parsed_result else 0,
                 document_id, project_id),
            )
        else:
            conn.execute(
                "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at,parser,document_role,ocr_status,page_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (document_id, project_id, original.name, kind, media_type, content_sha256, len(content),
                 parse_status, quality, len(fragments), warning, now_iso(), parsed_result.parser if parsed_result else "native",
                 document_role, parsed_result.ocr_status if parsed_result else "failed", parsed_result.page_count if parsed_result else 0),
            )
        if parsed_result is not None and parsed_result.tables:
            from .table_store import persist_tables

            persist_tables(conn, project_id, document_id, parsed_result.tables)
        for fragment in fragments:
            conn.execute(
                "INSERT INTO sources (id,project_id,document_id,locator_type,locator,text,page,bbox_json,extraction_method,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), project_id, document_id, fragment.locator_type, fragment.locator, fragment.text, fragment.page,
                 json_dump(fragment.bbox or []), fragment.extraction_method, hashlib.sha256(fragment.text.encode("utf-8")).hexdigest()),
            )
    invalidate_project_analysis(project_id, "extract")
    audit_event(
        project_id, "reupload" if retry_failed_document else "upload", "document", document_id,
        after={"filename": original.name, "kind": kind, "document_role": document_role, "sha256": content_sha256},
        actor="user",
    )
    return {"id": document_id, "kind": kind, "documentRole": document_role, "parseStatus": parse_status, "quality": quality, "warning": warning}


@app.patch("/api/projects/{project_id}/documents/{document_id}")
def update_document_classification(
    project_id: str, document_id: str, payload: DocumentUpdate,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    project = _authorize(project_id, x_project_token)
    _ensure_document_mutable(project)
    document = row("SELECT * FROM documents WHERE id=? AND project_id=?", (document_id, project_id))
    if not document:
        raise HTTPException(404, "材料不存在")
    expected_role = _document_role(payload.kind)
    if payload.document_role != expected_role:
        raise HTTPException(422, "材料分类与文档角色不一致")
    before = {"kind": document["kind"], "document_role": document.get("document_role", "")}
    after = {"kind": payload.kind, "document_role": payload.document_role}
    if before == after:
        return {"id": document_id, **after, "changed": False}
    with connect() as conn:
        conn.execute(
            "UPDATE documents SET kind=?,document_role=? WHERE id=? AND project_id=?",
            (payload.kind, payload.document_role, document_id, project_id),
        )
    invalidate_project_analysis(project_id, "extract")
    audit_event(project_id, "classify", "document", document_id, before=before, after=after, actor="user")
    return {"id": document_id, **after, "changed": True}


@app.get("/api/demo/materials/{document_id}")
def download_demo_material(document_id: str) -> FileResponse:
    document = row("SELECT * FROM documents WHERE id=? AND project_id LIKE 'demo-%'", (document_id,))
    if not document:
        raise HTTPException(404, "演示材料不存在")
    material = next((item for item in bond_materials(settings.resolved_data_dir / "demo_materials" / BOND_PROJECT_ID) + generate_bond_portfolio_materials() if item["id"] == document_id), None)
    if not material:
        raise HTTPException(404, "演示材料不存在")
    return FileResponse(material["path"], filename=document["filename"], media_type=document["media_type"])


@app.post("/api/projects/{project_id}/runs", status_code=202)
def create_run(project_id: str, payload: RunCreate, request: Request, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize(project_id, x_project_token)
    _ensure_project_writable(project)
    _enforce_analysis_readiness(project)
    check_rate(request.client.host if request.client else "unknown", "run", 3)
    active = row("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,))
    if active:
        raise HTTPException(409, f"项目已有活动任务：{active['id']}")
    try:
        run_id = start_run(project_id, payload.policy_profile)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": run_id, "status": "queued", "focus": payload.focus}


@app.get("/api/projects/{project_id}/runs/latest")
def latest_project_run(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    latest = row("SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1", (project_id,))
    if not latest:
        return {"run": None, "nodes": []}
    return {
        "run": latest,
        "nodes": rows("SELECT * FROM run_nodes WHERE run_id=? ORDER BY rowid", (latest["id"],)),
    }


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    return _authorize_run(run_id, x_project_token)


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> StreamingResponse:
    _authorize_run(run_id, x_project_token)

    async def stream():
        previous = None
        while True:
            item = row("SELECT * FROM runs WHERE id=?", (run_id,))
            if not item:
                yield "event: error\ndata: {\"message\":\"任务不存在\"}\n\n"
                return
            encoded = json.dumps(item, ensure_ascii=False)
            if encoded != previous:
                yield f"data: {encoded}\n\n"
                previous = encoded
            if item["status"] in TERMINAL:
                return
            await asyncio.sleep(0.4)
    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}/nodes")
def run_nodes(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize_run(run_id, x_project_token)
    return rows("SELECT * FROM run_nodes WHERE run_id=? ORDER BY rowid", (run_id,))


@app.get("/api/runs/{run_id}/artifacts")
def run_artifacts(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize_run(run_id, x_project_token)
    return _decode(rows("SELECT * FROM run_artifacts WHERE run_id=? ORDER BY rowid", (run_id,)), ("artifact_ids", "details_json"))


@app.get("/api/runs/{run_id}/attempts")
def run_attempts(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize_run(run_id, x_project_token)
    return rows(
        "SELECT node_key,task_type,attempt,status,input_tokens,output_tokens,cost,latency_ms,error_type,error_message,created_at "
        "FROM llm_attempts WHERE run_id=? ORDER BY created_at,rowid",
        (run_id,),
    )


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize_run(run_id, x_project_token)
    _ensure_project_writable(_project(project["project_id"]))
    execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
    return {"status": "cancelling"}


@app.post("/api/runs/{run_id}/retry", status_code=202)
def retry(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize_run(run_id, x_project_token)
    _ensure_project_writable(_project(project["project_id"]))
    try:
        new_id = retry_run(run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": new_id, "status": "queued"}


@app.post("/api/runs/{run_id}/nodes/{node_key}/retry", status_code=202)
def retry_node(run_id: str, node_key: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize_run(run_id, x_project_token)
    _ensure_project_writable(_project(project["project_id"]))
    try:
        new_id = retry_run(run_id, node_key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": new_id, "status": "queued", "resumedFromRunId": run_id, "resumeNode": node_key}


@app.get("/api/projects/{project_id}/facts")
def facts(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return decode_facts(rows("SELECT * FROM facts WHERE project_id=? ORDER BY category,name,period", (project_id,)))


@app.patch("/api/projects/{project_id}/facts/{fact_id}")
def update_fact(project_id: str, fact_id: str, payload: FactUpdate, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    fact = row("SELECT * FROM facts WHERE id=? AND project_id=?", (fact_id, project_id))
    if not fact:
        raise HTTPException(404, "事实不存在")
    execute(
        "UPDATE facts SET period=?,value=?,unit=?,text_value=?,raw_value=?,confidence=1,extraction_method='human',review_status='human_verified' WHERE id=?",
        (payload.period, payload.value, payload.unit, payload.text_value,
         payload.text_value or (str(payload.value) if payload.value is not None else ""), fact_id),
    )
    execute("DELETE FROM fact_assertions WHERE id=? AND project_id=?", (fact_id, project_id))
    sync_fact_assertions(project_id)
    audit_event(project_id, "legacy_fact_update", "fact", fact_id, fact, payload.model_dump(), "human")
    from .review_lifecycle import mark_review_changed
    mark_review_changed(project_id, f"fact:{fact.get('fact_key', '')}")
    return {"status": "human_verified"}


@app.get("/api/projects/{project_id}/tables")
def project_tables(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    """系统表格清单。与报告成品读同一份 TableBlock，不存在第二套数字。"""
    _authorize(project_id, x_project_token)
    from .report_tables import latest_reviews, project_tables as build_tables

    subject = _project_subject(project_id)
    output = []
    for block in build_tables(project_id, subject=subject):
        reviews = latest_reviews(project_id, block.table_id)
        output.append({
            "table_id": block.table_id, "table_type": block.table_type, "title": block.title,
            "unit": block.unit, "scope": block.scope, "origin_mode": block.origin_mode,
            "display_mode": block.display_mode,
            "columns": [{"key": c.key, "label": c.label, "role": c.role} for c in block.columns],
            "row_count": len(block.rows),
            "validation_status": block.validation_status,
            "validation_summary": block.validation_summary,
            "notes": list(block.notes),
            "cell_reviews": len(reviews),
        })
    return output


@app.get("/api/projects/{project_id}/tables/{table_id}")
def project_table_detail(project_id: str, table_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    """单张表的核对矩阵：列、行、单元格（含来源、定位与最新核对结论）。"""
    _authorize(project_id, x_project_token)
    from .report_tables import table_matrix

    matrix = table_matrix(project_id, table_id, subject=_project_subject(project_id))
    if matrix is None:
        raise HTTPException(status_code=404, detail="表格不存在")
    return matrix


@app.post("/api/projects/{project_id}/tables/{table_id}/cells/{cell_id}/review")
def review_table_cell(
    project_id: str, table_id: str, cell_id: str, payload: TableCellReview,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    """人工确认或更正单元格。

    只追加、不覆盖：原始值原样保留，更正另存一列并形成新版本；不触碰证据存储与上传文件。
    """
    _ensure_project_writable(_authorize(project_id, x_project_token))
    from .report_tables import find_table, latest_reviews, original_cell_raw, record_review

    block = find_table(project_id, table_id, subject=_project_subject(project_id))
    if block is None:
        raise HTTPException(status_code=404, detail="表格不存在")
    original = original_cell_raw(block, cell_id)
    if original is None:
        raise HTTPException(status_code=404, detail="单元格不存在")
    try:
        review = record_review(
            project_id, table_id, cell_id, decision=payload.decision,
            corrected_raw=payload.corrected_raw or "", note=payload.note or "",
            reviewer=payload.reviewer or "", original_raw=original,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"review": review, "cell_reviews": latest_reviews(project_id, table_id)}


def _project_subject(project_id: str) -> str:
    item = row("SELECT company_name FROM projects WHERE id=?", (project_id,))
    return str((item or {}).get("company_name") or "")


@app.get("/api/projects/{project_id}/fact-groups")
def grouped_facts(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    if not row("SELECT id FROM canonical_facts WHERE project_id=? LIMIT 1", (project_id,)):
        sync_fact_assertions(project_id)
    return fact_groups(project_id)


@app.patch("/api/projects/{project_id}/fact-groups/{canonical_id}/resolve")
def resolve_group(
    project_id: str, canonical_id: str, payload: FactGroupResolve,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    try:
        return resolve_fact_group(project_id, canonical_id, payload.assertion_id, payload.manual, payload.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{project_id}/metrics")
def metrics(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return decode_metrics(rows("SELECT * FROM financial_metrics WHERE project_id=? ORDER BY metric_key,period", (project_id,)))


@app.get("/api/projects/{project_id}/metrics/{metric_id}/trace")
def metric_trace(project_id: str, metric_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    try:
        return calculation_trace(project_id, metric_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/entities")
def entities(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return sync_project_entities(project_id)


@app.get("/api/projects/{project_id}/financial-tie-outs")
def financial_tie_outs(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return decode_tie_outs(rows("SELECT * FROM financial_tie_outs WHERE project_id=? ORDER BY period DESC,check_key", (project_id,)))


@app.get("/api/projects/{project_id}/evidence-graph")
def project_evidence_graph(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    return evidence_graph(project_id)


@app.get("/api/projects/{project_id}/report-quality")
def project_report_quality(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    return report_quality(project_id)


@app.get("/api/projects/{project_id}/gold-standard")
def gold_standard(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    items = rows("SELECT annotation_type,annotation_key,expected_json,source_ids,version FROM gold_annotations WHERE project_id=? ORDER BY annotation_type,annotation_key", (project_id,))
    return _decode(items, ("expected_json", "source_ids"))


@app.get("/api/projects/{project_id}/policy")
def policy(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    project = _authorize(project_id, x_project_token)
    latest = row("SELECT profile,version,sha256,created_at FROM policy_snapshots WHERE project_id=? ORDER BY created_at DESC LIMIT 1", (project_id,))
    active = load_policy("bond-v1" if project.get("project_type") == "bond_guarantee" else None).public_dict()
    return {"active": active, "lastRun": latest}


@app.get("/api/projects/{project_id}/review-tasks")
def review_tasks(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return decode_tasks(rows("SELECT * FROM review_tasks WHERE project_id=? ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,created_at", (project_id,)))


@app.get("/api/projects/{project_id}/review-summary")
def get_review_summary(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    return {**review_summary(project_id), **report_state(project_id)}


@app.post("/api/projects/{project_id}/review/apply")
def apply_review(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    try:
        result = apply_review_decisions(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    # 复核更新后的影响面：补上受影响章节及其"再生入口所需字段"，供前端直接渲染再生按钮。
    # ``apply_review_decisions`` 已返回 affected_section_keys（确定性章节键），这里不重复计算
    # 影响面，只用 chapter 元数据把它补全为可操作的条目。
    affected_keys = [key for key in result.get("affected_section_keys", []) if key in BOND_CHAPTER_KEYS]
    result["affected_sections"] = [
        {
            "section_key": key,
            "title": BOND_CHAPTER_TITLES.get(key, key),
            "llm": chapter_uses_llm(key),
            # 只有调用模型的章节才有"手动再生"入口；确定性章节由系统重算。
            "regenerable": chapter_uses_llm(key),
            "regenerate_endpoint": f"/api/projects/{project_id}/report/sections/{key}/regenerate",
        }
        for key in affected_keys
    ]
    result["affected_model_section_keys"] = [item["section_key"] for item in result["affected_sections"] if item["llm"]]
    result["regenerable_section_keys"] = [item["section_key"] for item in result["affected_sections"] if item["regenerable"]]
    return result


@app.post("/api/projects/{project_id}/report/mark-reviewed")
def mark_report_as_reviewed(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    quality = report_quality(project_id)
    unresolved = unresolved_items(project_id)
    if quality["status"] != "passed":
        raise HTTPException(409, "报告引用或数字校验尚未通过")
    if unresolved["has_open_major_items"]:
        raise HTTPException(409, "仍存在未决重大事项，不能标记为已复核")
    blockers = delivery_blockers(project_id)
    if blockers:
        # 支持率通过不能掩盖回退、披露缺失或正文被改动。
        raise HTTPException(409, "报告未通过交付一致性校验：" + "；".join(blockers))
    try:
        return mark_report_reviewed(project_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.patch("/api/projects/{project_id}/review-tasks/{task_id}")
def patch_review_task(
    project_id: str, task_id: str, payload: ReviewTaskUpdate,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    try:
        return update_task(project_id, task_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/candidate-findings")
def candidate_findings(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return decode_candidates(rows("SELECT * FROM candidate_findings WHERE project_id=? ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,created_at", (project_id,)))


@app.patch("/api/projects/{project_id}/candidate-findings/{finding_id}")
def patch_candidate(
    project_id: str, finding_id: str, payload: CandidateDecision,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    try:
        return decide_candidate(project_id, finding_id, payload.status, payload.decision_note)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/projects/{project_id}/audit-events")
def audit_events(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return _decode(rows("SELECT * FROM audit_events WHERE project_id=? ORDER BY created_at DESC LIMIT 300", (project_id,)), ("before_json", "after_json"))


@app.post("/api/projects/{project_id}/report/generate", status_code=202)
def generate_report(
    project_id: str, payload: RunCreate, request: Request,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    project = _authorize(project_id, x_project_token)
    _ensure_project_writable(project)
    _enforce_analysis_readiness(project)
    check_rate(request.client.host if request.client else "unknown", "run", 3)
    try:
        run_id = start_run(project_id, payload.policy_profile)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": run_id, "status": "queued"}


@app.get("/api/projects/{project_id}/cross-checks")
def cross_checks(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return _decode(rows("SELECT * FROM cross_checks WHERE project_id=? ORDER BY CASE status WHEN 'warning' THEN 1 WHEN 'passed' THEN 2 ELSE 3 END, CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END", (project_id,)), ("values_json", "source_ids", "fact_ids", "thresholds", "trace_json"))


@app.get("/api/projects/{project_id}/risks")
def risks(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return _decode(rows("SELECT * FROM risks WHERE project_id=? ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END", (project_id,)), ("source_ids",))


@app.get("/api/projects/{project_id}/report")
def report(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    sections = _decode(
        rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,)),
        ("citation_ids", "allowed_source_ids", "validation_diagnostics", "required_disclosures"),
    )
    # 论文式标题层级：编号在此计算（不写入正文），前端按 part_label/chapter_label 分组渲染；
    # blocks 为解析后的章内分块（含三级标题编号）＋该章的系统表格块（数值直接来自
    # TableBlock，不从正文 Markdown 反解），前端据此渲染标题层级与表格。
    labels = section_labels(sections)
    subject = _project_subject(project_id)
    project_revision = report_state(project_id)
    for section in sections:
        info = labels.get(section["section_key"], {})
        section["part"] = info.get("part", "")
        section["part_number"] = info.get("part_number", "")
        section["part_label"] = info.get("part_label", "")
        section["chapter_number"] = info.get("chapter_number", "")
        section["chapter_label"] = info.get("chapter_label", "")
        section["blocks"] = section_blocks(project_id, section, subject=subject)
        if section["section_key"] in {"financial", "cashflow", "debt", "rating"}:
            from .report_tables import table_snapshot_status

            section["system_tables"] = table_snapshot_status(project_id, section["section_key"], subject=subject)
        section["report_revision"] = project_revision["report_revision"]
        # 篇幅诊断：历史报告没有这些列时按 not_checked 返回，并在读取时补算字符数。
        policy = effective_chapter_config(section["section_key"], project_id).get("length_policy") or {}
        if not section.get("length_status") or section["length_status"] == "not_checked":
            # 口径必须与写入口径一致：按**冻结披露**剔除系统块（冲突块、系统表格块、
            # 来源边界块）。旧写法取 ``section["conflict_block"]``——那一列并不存在，
            # 于是重算把系统表格算进篇幅，和写入时存的数字对不上，同一章出现两个字数。
            section["visible_content_chars"] = count_report_chars(
                section.get("content") or "",
                required_disclosures=json_load(section.get("required_disclosures") or "[]"),
            )
        section["length_policy"] = {
            "effective_min_chars": section.get("effective_min_chars") or policy.get("effective_min_chars", 0),
            "effective_max_chars": section.get("effective_max_chars") or policy.get("effective_max_chars", 0),
            "unit": "visible_chars",
        }
        section["length_status"] = section.get("length_status") or "not_checked"
        section["length_repair_attempted"] = bool(section.get("length_repair_attempted"))
        # 生成模式与 Prompt 时效：前端据此提示"该章用哪套 Prompt 生成、是否需要重新编译/再生"。
        # ``generation_mode`` 目前尚未写入章节契约，必须用 ``.get`` 容错（默认 rewrite）。
        section_key = section["section_key"]
        section["generation_mode"] = load_chapter_config(section_key).get("generation_mode", "rewrite")
        section["prompt_outdated"] = _prompt_outdated(project_id, section_key)
        # 修复状态与失败诊断：fallback_reason / fallback_stage / validation_diagnostics 已是
        # report_sections 的列，这里归一为一个显式的 repair 块，缺失字段补空值。
        section["repair"] = {
            "generation_status": section.get("generation_status") or "",
            "content_origin": section.get("content_origin") or "",
            "fallback_reason": section.get("fallback_reason") or "",
            "fallback_stage": section.get("fallback_stage") or "none",
            "support_validation_status": section.get("support_validation_status") or "",
            "delivery_status": section.get("delivery_status") or "not_checked",
            "validation_diagnostics": section.get("validation_diagnostics") or {},
        }
    return sections


@app.post("/api/projects/{project_id}/report/tables/refresh")
def refresh_report_tables(
    project_id: str, payload: ReportTableRefresh, request: Request,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    project = _authorize(project_id, x_project_token)
    _ensure_project_writable(project)
    if is_real_reference(project_id):
        try:
            local = bool(request.client and ipaddress.ip_address(request.client.host).is_loopback)
        except ValueError:
            local = False
        if settings.public_demo or not local:
            raise HTTPException(403, "真实参考项目仅允许非公开环境的本机请求更新表格")

    from .report_tables import (
        SYSTEM_TABLE_HEADING, TABLE_RULE_VERSION, _current_chapter_table_blocks,
        render_table_block, table_blocks_hash,
    )
    from .delivery_wording import material_numbers, normalize_report_text
    from .report_delivery import assess_delivery, assess_length, content_sha256

    state = report_state(project_id)
    if state["report_revision"] != payload.expected_report_revision:
        raise HTTPException(409, "报告版本已变化，请刷新报告后重试")
    if set(payload.section_hashes) != set(payload.chapters):
        raise HTTPException(422, "每个待更新章节都必须提供正文哈希")
    from .table_store import reparse_stale_tables

    try:
        reparsed_documents = reparse_stale_tables(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(409, str(exc)) from exc

    refreshed: list[dict] = []
    with connect() as conn:
        current = conn.execute(
            "SELECT report_revision,review_revision FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if int(current["report_revision"] or 0) != payload.expected_report_revision:
            raise HTTPException(409, "报告版本已变化，请刷新报告后重试")
        updates = []
        for chapter in payload.chapters:
            section = conn.execute(
                "SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, chapter),
            ).fetchone()
            if not section:
                raise HTTPException(404, f"报告章节不存在：{chapter}")
            original = dict(section)
            content = str(original.get("content") or "")
            digest = content_sha256(content)
            if digest != payload.section_hashes.get(chapter):
                raise HTTPException(409, f"{chapter} 正文已修改，请刷新报告后重试")
            disclosures = json_load(original.get("required_disclosures") or "[]")
            previous = next((item for item in disclosures if str(item).startswith(SYSTEM_TABLE_HEADING)), "")
            if previous and previous not in content:
                raise HTTPException(409, f"{chapter} 的原系统表格已被编辑，无法安全自动替换")
            if not previous and SYSTEM_TABLE_HEADING in content:
                raise HTTPException(409, f"{chapter} 的系统表格披露无法与当前保存记录对应")

            blocks = _current_chapter_table_blocks(project_id, chapter, subject=project["company_name"])
            rendered = [render_table_block(block, index=index, total=len(blocks))
                        for index, block in enumerate(blocks, 1)]
            table_text = "\n\n".join([SYSTEM_TABLE_HEADING, *rendered]) if rendered else ""
            table_text = normalize_report_text(table_text, material_numbers(project_id))
            if previous:
                content = content.replace(previous, table_text, 1)
            elif table_text:
                content = content.rstrip() + "\n\n" + table_text
            disclosures = [table_text if item == previous and previous else item for item in disclosures]
            if not previous and table_text:
                disclosures.append(table_text)

            draft = str(original.get("deterministic_draft") or "")
            if previous and previous in draft:
                draft = draft.replace(previous, table_text, 1)
            if not draft:
                draft = content
            policy = {
                "effective_min_chars": original.get("effective_min_chars") or 0,
                "effective_max_chars": original.get("effective_max_chars") or 0,
            }
            length = assess_length(content, policy, disclosures)
            disclosure, delivery, validated_hash = assess_delivery(
                content=content, required_disclosures=disclosures, support_status="pending",
                length_status=length["length_status"],
            )
            new_digest = content_sha256(content)
            next_input_hash = hashlib.sha256(
                f"{original.get('input_hash') or ''}:{table_blocks_hash(blocks)}".encode("utf-8")
            ).hexdigest()
            updates.append((chapter, content, draft, disclosures, policy, length, disclosure, delivery,
                            validated_hash, new_digest, next_input_hash, blocks, original))

        now = now_iso()
        for (chapter, content, draft, disclosures, _policy, length, disclosure, delivery,
             validated_hash, digest, input_hash, blocks, original) in updates:
            conn.execute(
                "UPDATE report_sections SET content=?,deterministic_draft=?,content_sha256=?,"
                "validated_content_sha256=?,input_hash=?,required_disclosures=?,review_status='draft',"
                "support_validation_status='pending',validation_status='pending',disclosure_status=?,"
                "delivery_status=?,validation_diagnostics=?,raw_output_chars=?,visible_content_chars=?,"
                "length_status=?,updated_at=? WHERE project_id=? AND section_key=?",
                (content, draft, digest, validated_hash, input_hash, json_dump(disclosures), disclosure,
                 delivery, json_dump({"system_tables_refreshed": True, "table_rule_version": TABLE_RULE_VERSION}),
                 length["raw_output_chars"], length["visible_content_chars"], length["length_status"], now,
                 project_id, chapter),
            )
            conn.execute(
                "INSERT INTO report_table_snapshots(project_id,chapter_key,blocks_json,input_hash,rule_version,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,chapter_key) DO UPDATE SET "
                "blocks_json=excluded.blocks_json,input_hash=excluded.input_hash,rule_version=excluded.rule_version,updated_at=excluded.updated_at",
                (project_id, chapter, json_dump([block.to_dict() for block in blocks]),
                 table_blocks_hash(blocks), TABLE_RULE_VERSION, now),
            )
            refreshed.append({"chapter_key": chapter, "content_sha256": digest,
                              "input_hash": input_hash, "table_hash": table_blocks_hash(blocks)})

        conn.execute(
            "UPDATE projects SET review_revision=review_revision+1,report_revision=report_revision+1,"
            "report_reviewed_revision=0,report_reviewed_at=NULL,report_stale=0,report_stale_reasons='[]',updated_at=? "
            "WHERE id=?",
            (now, project_id),
        )

    quality = validate_report_support(project_id)
    audit_event(
        project_id, "report_system_tables_refresh", "report", ",".join(payload.chapters),
        {"report_revision": payload.expected_report_revision},
        {"chapters": refreshed, "support_status": quality.get("status")}, "human",
    )
    # 表格刷新只替换章节里的**系统表格块**，分析正文（人工/模型撰写的主体）原样保留；
    # 因此这里明确区分"表格已刷新"与"报告是否完整更新"，并标注正文主张需重新核验，
    # 避免界面把表格刷新的成功误读成整份报告已更新。
    return {
        "status": "tables_refreshed",
        "chapters": refreshed,
        "support_status": quality.get("status"),
        "reparsed_documents": reparsed_documents,
        "analysis_content_preserved": True,
        "content_claims_status": "pending_reverification",
        "report_fully_updated": False,
        "notice": "系统表格已刷新，分析正文保留；正文中的主张需重新核验后方可视为报告完整更新。",
        **report_state(project_id),
    }


def _prompt_row_state(project_id: str, chapter_key: str, permissions: Optional[dict] = None) -> dict:
    """Effective prompt row for a chapter: project override > global default > YAML.

    ``permissions`` 为 ``prompt_edit_permissions`` 的结果；缺失时按"项目级可编辑、
    全局不可编辑"处理（与 GET 未显式传入时保持一致的保守默认）。
    """
    project_override = row(
        "SELECT prompt,updated_at,structured_json,migration_mode FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?",
        (project_id, chapter_key),
    )
    global_override = row(
        "SELECT prompt,updated_at,structured_json,migration_mode FROM global_report_prompts WHERE chapter_key=?",
        (chapter_key,),
    )
    default_prompt = chapter_default_prompt(chapter_key)
    editable, source, migration_mode = prompt_edit_state(chapter_key, project_id)
    if project_override and str(project_override.get("prompt") or "").strip():
        prompt = str(project_override["prompt"])
    elif global_override and str(global_override.get("prompt") or "").strip():
        prompt = str(global_override["prompt"])
    else:
        prompt = default_prompt
    return {
        "title": BOND_CHAPTER_TITLES.get(chapter_key, chapter_key),
        "part": BOND_CHAPTER_PART.get(chapter_key, ""),
        "prompt": prompt,
        "default_prompt": default_prompt,
        "overridden": bool(project_override and str(project_override.get("prompt") or "").strip()),
        "updated_at": project_override.get("updated_at") if project_override else None,
        "global_overridden": bool(global_override and str(global_override.get("prompt") or "").strip()),
        "global_updated_at": global_override.get("updated_at") if global_override else None,
        "editable_config": editable,
        "factory_config": editable_config_from_factory(chapter_key),
        # 篇幅的运行时口径由后端给出（= 保存校验/生成校验用的那一份），前端只展示不推算。
        "length_policy": effective_chapter_config(chapter_key, project_id).get("length_policy", {}),
        "constraints": {
            "required_inputs": load_chapter_config(chapter_key).get("required_inputs", []),
            "input_contract": load_chapter_config(chapter_key).get("input_contract", {}),
            "preserve_blocks": load_chapter_config(chapter_key).get("preserve_blocks", []),
            "forbidden": load_chapter_config(chapter_key).get("forbidden", []),
            "output_contract": load_chapter_config(chapter_key).get("output_contract", {}),
            "global_rules": prompt_config_payload(bond=True).get("global_rules", {}),
        },
        "source": source,
        "migration_mode": migration_mode,
        # 权限：项目级是否可编辑 + 全局是否可编辑（全局恒 false）。
        **_prompt_permission_fields(permissions),
    }


@app.get("/api/projects/{project_id}/report-prompts")
def report_prompts(project_id: str, request: Request,
                   x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    """Return every report chapter's effective prompt and its contract metadata."""
    _authorize(project_id, x_project_token)
    permissions = prompt_edit_permissions(project_id, request)
    output = []
    for chapter_key in BOND_CHAPTER_KEYS:
        config = load_chapter_config(chapter_key)
        output.append({
            "chapter_key": chapter_key,
            # 中文标题与报告分部以 chapter_prompts 元数据为单一来源，
            # 不再依赖 report_sections——未生成报告的项目同样返回中文。
            **(_prompt_row_state(project_id, chapter_key, permissions)),
            "llm": config.get("llm", False),
            "required_sections": config.get("required_sections", []),
        })
    return output


@app.patch("/api/projects/{project_id}/report-prompts/{chapter_key}")
def update_report_prompt(project_id: str, chapter_key: str, payload: ReportPromptUpdate, request: Request,
                         x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    if chapter_key not in BOND_CHAPTER_KEYS:
        raise HTTPException(404, "报告章节不存在")
    # 项目级保存门禁：真实参考项目仅本机直连（非公开、无转发头）时允许；否则 403。
    permissions = _require_prompt_editable(project_id, request)
    config = payload.config
    if not chapter_uses_llm(chapter_key):
        raise HTTPException(422, "确定性章节由系统模板生成，不支持修改")
    if config is not None:
        prompt = compile_structured_prompt(chapter_key, config.model_dump(mode="json"))
        structured_json = json_dump(config.model_dump(mode="json"))
        migration_mode = "structured"
        legacy_prompt = ""
    else:
        legacy_prompt = str(payload.prompt).strip()
        legacy_config, migration_mode = legacy_prompt_config(chapter_key, legacy_prompt)
        prompt = compile_structured_prompt(chapter_key, legacy_config)
        structured_json = json_dump(legacy_config)
    now = now_iso()
    before = row("SELECT * FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?", (project_id, chapter_key))
    with connect() as conn:
        conn.execute(
            "INSERT INTO report_prompt_overrides (id,project_id,chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id,chapter_key) DO UPDATE SET prompt=excluded.prompt,updated_at=excluded.updated_at,structured_json=excluded.structured_json,legacy_prompt=excluded.legacy_prompt,migration_mode=excluded.migration_mode",
            (str(uuid.uuid4()), project_id, chapter_key, prompt, now, structured_json, legacy_prompt, migration_mode),
        )
    audit_event(project_id, "report_prompt_update", "report_prompt", chapter_key, before, {"prompt": prompt, "config": config.model_dump(mode="json") if config else None}, "human")
    return {"chapter_key": chapter_key, **_prompt_row_state(project_id, chapter_key, permissions)}


@app.post("/api/projects/{project_id}/report-prompts/{chapter_key}/preview")
def preview_report_prompt(project_id: str, chapter_key: str, payload: ReportPromptPreview, request: Request,
                          x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    if chapter_key not in BOND_CHAPTER_KEYS:
        raise HTTPException(404, "报告章节不存在")
    if not chapter_uses_llm(chapter_key):
        raise HTTPException(422, "确定性章节不支持结构化预览")
    # 预览不落库、不调模型；仅回带当前环境的编辑权限供前端展示。
    permissions = prompt_edit_permissions(project_id, request)
    return {"chapter_key": chapter_key, **_prompt_permission_fields(permissions),
            "prompt": compile_structured_prompt(chapter_key, payload.config.model_dump(mode="json"))}


@app.delete("/api/projects/{project_id}/report-prompts/{chapter_key}")
def reset_report_prompt(project_id: str, chapter_key: str, request: Request,
                        x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    if chapter_key not in BOND_CHAPTER_KEYS:
        raise HTTPException(404, "报告章节不存在")
    # 项目级重置与保存同一门禁：真实参考项目仅本机直连（非公开、无转发头）时允许。
    permissions = _require_prompt_editable(project_id, request)
    before = row("SELECT * FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?", (project_id, chapter_key))
    execute("DELETE FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?", (project_id, chapter_key))
    audit_event(project_id, "report_prompt_reset", "report_prompt", chapter_key, before, {}, "human")
    return {"chapter_key": chapter_key, **_prompt_row_state(project_id, chapter_key, permissions)}


@app.patch("/api/projects/{project_id}/report-prompts/global/{chapter_key}")
def set_global_report_prompt(project_id: str, chapter_key: str, payload: ReportPromptUpdate, request: Request,
                             x_project_token: Optional[str] = Header(default=None)) -> dict:
    """全局默认 Prompt 一律禁止通过接口修改（修订稿 §4）。

    既有的全局配置与历史覆盖继续生效（``global_chapter_prompt`` 仍会读取），
    但不再允许任何调用方写入或提升为全局默认——包括真实参考项目的本机直连。
    """
    _ensure_project_writable(_authorize(project_id, x_project_token))
    if chapter_key not in BOND_CHAPTER_KEYS:
        raise HTTPException(404, "报告章节不存在")
    raise HTTPException(403, "全局默认 Prompt 不允许通过接口修改，仅支持项目级覆盖")


@app.delete("/api/projects/{project_id}/report-prompts/global/{chapter_key}")
def reset_global_report_prompt(project_id: str, chapter_key: str, request: Request,
                               x_project_token: Optional[str] = Header(default=None)) -> dict:
    """全局默认 Prompt 一律禁止通过接口重置（既有全局覆盖保持不变）。"""
    _ensure_project_writable(_authorize(project_id, x_project_token))
    if chapter_key not in BOND_CHAPTER_KEYS:
        raise HTTPException(404, "报告章节不存在")
    raise HTTPException(403, "全局默认 Prompt 不允许通过接口重置，仅支持项目级覆盖")


@app.patch("/api/projects/{project_id}/report/sections/{section_key}")
def update_section(project_id: str, section_key: str, payload: SectionUpdate, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    before = row("SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, section_key))
    if not before:
        raise HTTPException(404, "报告章节不存在")
    # 人工编辑同样走统一写入入口：正文来源改为 human、重算篇幅与哈希、
    # 不沿用旧模型诊断（旧诊断描述的是那份已被替换的正文）。
    write_report_section(
        project_id=project_id, section_key=section_key, content=payload.content,
        citation_ids=json_load(before.get("citation_ids") or "[]"),
        allowed_source_ids=json_load(before.get("allowed_source_ids") or "[]"),
        content_origin="human", generation_status="human_edit",
        fallback_reason="", fallback_stage="none",
        required_disclosures=json_load(before.get("required_disclosures") or "[]"),
        length_policy={"effective_min_chars": before.get("effective_min_chars") or 0,
                       "effective_max_chars": before.get("effective_max_chars") or 0},
        diagnostics={},
        prompt_version=before.get("prompt_version") or "",
        prompt_sha256=before.get("prompt_sha256") or "",
        input_hash=before.get("input_hash") or "",
        review_status=payload.review_status, support_status="pending",
    )
    quality = validate_report_support(project_id)
    if payload.review_status == "reviewed" and quality["status"] != "passed":
        execute("UPDATE report_sections SET review_status='draft' WHERE project_id=? AND section_key=?", (project_id, section_key))
        raise HTTPException(409, "章节存在无证据声明或无效引用，已保存为草稿但不能标记已复核")
    audit_event(project_id, "report_section_update", "report_section", section_key, before, payload.model_dump(), "human")
    return {"status": "updated"}


@app.post("/api/projects/{project_id}/report/sections/{section_key}/regenerate")
def regenerate_section(project_id: str, section_key: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    section = row("SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, section_key))
    if not section:
        raise HTTPException(404, "报告章节不存在")
    stored_citations = json_load(section.get("citation_ids") or "[]")
    # Rebuild from the frozen deterministic draft. A previous model response may
    # contain rejected numbers or wording and must not become the next prompt's
    # factual boundary.
    from .bond_report import generate_bond_chapters

    current_chapter = next(
        (item for item in generate_bond_chapters(project_id, False) if item.key == section_key),
        None,
    )
    deterministic_draft = str(section.get("deterministic_draft") or "").strip()
    draft_seed = (
        current_chapter.content if current_chapter and current_chapter.content.strip()
        else deterministic_draft or str(section.get("content") or "")
    )
    source_ids = current_chapter.source_ids if current_chapter else []
    chapter_context = build_chapter_context(project_id, section_key, draft_seed, source_ids)
    evidence = [
        {"id": item["id"], "quote": item["quote"], "locator": item.get("locator", ""),
         "locator_type": item.get("locator_type", "")}
        for item in chapter_context.get("evidence", [])
    ]
    allowed = set(chapter_context.get("allowed_citation_ids", []))
    validation = {"status": "passed", "reasons": []}
    content = chapter_context.get("draft") or draft_seed
    citations = list(chapter_context.get("allowed_citation_ids") or [])
    fallback_reason = ""
    usage = None
    repair_attempted = False
    if not chapter_uses_llm(section_key):
        fallback_reason = NO_MODEL_REASON
        validation = {"status": "skipped", "reasons": [fallback_reason]}
    else:
        try:
            result, usage = rewrite_section(
                section["title"], content, evidence,
                chapter_key=section_key, chapter_context=chapter_context,
                strict_citations=True,
            )
            attribution_restored = _attribution_block_missing(result.content, chapter_context)
            result.content = _preserve_chapter_boundaries(result.content, chapter_context)
            validation = validate_chapter_output(result.content, result.citation_ids, chapter_context)
            validation["source_attribution_restored"] = attribution_restored
            if not _report_boundary_safe(result.content):
                # 与完整生成同一道边界检查：单章重生成此前漏了这一步，
                # 「同意担保」这类禁止结论在重生成路径上不会被拒绝。
                validation["reasons"].append("章节包含禁止的承保或审批结论")
                validation["status"] = "rejected"
                validation["length_only_failure"] = False
            if validation.get("length_only_failure"):
                # 安全校验全过、仅篇幅超上限 → 一次定向压缩，复用同一上下文与别名表。
                # 与完整生成共用 `repair_chapter_length`，两侧口径不会分叉。
                repair_attempted = True
                result, validation, repair_usage = repair_chapter_length(
                    section["title"], content, evidence,
                    chapter_key=section_key, chapter_context=chapter_context,
                    call_context=LlmCallContext(project_id, "", "draft", "report"),
                    original_result=result, original_validation=validation,
                )
                usage = repair_usage if usage is None else combine_usage(usage, repair_usage)
            if validation["status"] == "passed":
                content = result.content
                citations = list(result.citation_ids)
            else:
                fallback_reason = "；".join(validation["reasons"])
        except RuntimeError as exc:
            fallback_reason = str(exc)[:240]
            validation = {"status": "fallback", "reasons": [fallback_reason]}
        except Exception:
            fallback_reason = "章节模型调用或结构校验失败"
            validation = {"status": "fallback", "reasons": [fallback_reason]}
    if not citations:
        citations = list(allowed) or stored_citations
    if not fallback_reason and validation["status"] == "passed":
        fallback_reason = ""
    # 生成状态与证据支持状态分开写：证据侧由随后的 validate_report_support 计算。
    generation_status = (
        "deterministic" if not chapter_uses_llm(section_key)
        else "fallback" if fallback_reason and validation.get("status") != "skipped"
        else "model_passed" if validation.get("status") == "passed"
        else "skipped"
    )
    # 单章再生也走**后置支持校验**，与完整生成同一判定（修订稿 §3.3：完整生成与单章再生
    # 共用"生成 → 章节及后置校验 → 一次修复 → 全量重检 → 写入"，禁止单章入口只校验不执行
    # 完整判定）。放在**写库之前**评估候选正文：有主张未获证据支持时**不覆盖已有正文**，
    # 保留旧版本并明确显示本次失败——否则失败现场一旦落库就再也回不到旧稿。
    from .p0 import _support_context, assess_section_claims

    pre_support = assess_section_claims(project_id, {
        "section_key": section_key,
        "content": content,
        "citation_ids": citations,
        "allowed_source_ids": sorted({item["source_id"] for item in chapter_context.get("evidence", [])}),
        "required_disclosures": [block for block in (
            chapter_context.get("conflict_block"), chapter_context.get("system_table_block"),
            chapter_context.get("source_attribution_block"),
        ) if block],
    }, *_support_context(project_id))
    if pre_support["unsupported_claims"]:
        failure = (
            f"模型正文有 {pre_support['unsupported_claims']} 条主张未获证据支持，"
            "已保留已有正文为旧版本（本次未采用）"
        )
        execute(
            "UPDATE report_sections SET validation_status=?, fallback_reason=?, fallback_stage=?,"
            " validation_diagnostics=? WHERE project_id=? AND section_key=?",
            ("failed", failure, "report_support", json.dumps(pre_support, ensure_ascii=False),
             project_id, section_key),
        )
        audit_event(
            project_id, "report_section_regenerate_failed", "report_section", section_key, section,
            {"failure": failure, "unsupported_claims": pre_support["unsupported_claims"],
             "support_rate": pre_support["support_rate"]}, "human",
        )
        return {
            "content": str(section.get("content") or ""), "citationIds": stored_citations,
            "validation": {"status": "failed", "reasons": [failure]},
            "fallbackReason": failure, "generationStatus": "failed",
            "contentOrigin": str(section.get("content_origin") or ""),
            "support": {"unsupported_claims": pre_support["unsupported_claims"],
                        "support_rate": pre_support["support_rate"]},
            "usage": usage.__dict__ if usage else None,
        }
    # 单章重新生成同样走统一写入入口：来源/状态/阶段/篇幅/哈希/冻结披露一次对齐。
    write_report_section(
        project_id=project_id, section_key=section_key, title=section["title"],
        content=content, citation_ids=citations,
        allowed_source_ids=sorted({item["source_id"] for item in chapter_context.get("evidence", [])}),
        content_origin="model" if generation_status == "model_passed" else "deterministic",
        generation_status=generation_status, fallback_reason=fallback_reason,
        fallback_stage="chapter_validation" if fallback_reason else "none",
        # 系统表格块必须与生成路径一致地冻结。漏了它会有两个后果：正文里的表格
        # 被当成模型事实主张去要证据（支持率掉成 warning），并且「表格逐字保留」
        # 的检查失效。实测：单章重生成后 rating 章的 support 变成 warning。
        required_disclosures=[block for block in (
            chapter_context.get("conflict_block"), chapter_context.get("system_table_block"),
            chapter_context.get("source_attribution_block"),
        ) if block],
        deterministic_draft=chapter_context.get("draft") or draft_seed,
        table_records=chapter_context.get("table_records") or [],
        length_policy=chapter_context.get("length_policy") or {},
        diagnostics=validation_diagnostics(validation),
        prompt_version=chapter_context.get("prompt_version", ""),
        prompt_sha256=chapter_context.get("prompt_sha256", ""),
        input_hash=chapter_context.get("input_hash", ""),
        support_status="pending",
    )
    if repair_attempted:
        execute("UPDATE report_sections SET length_repair_attempted=1 WHERE project_id=? AND section_key=?",
                (project_id, section_key))
    audit_event(
        project_id, "report_section_regenerate", "report_section", section_key, section,
        {"content": content, "citation_ids": citations, "validation": validation,
         "fallback_reason": fallback_reason, "prompt_version": chapter_context.get("prompt_version", ""),
         "prompt_sha256": chapter_context.get("prompt_sha256", ""), "input_hash": chapter_context.get("input_hash", "")}, "human",
    )
    validate_report_support(project_id)
    return {"content": content, "citationIds": citations, "validation": validation,
            "fallbackReason": fallback_reason, "usage": usage.__dict__ if usage else None}


@app.post("/api/projects/{project_id}/material-qa")
def material_qa(
    project_id: str,
    payload: MaterialQARequest,
    request: Request,
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    _authorize(project_id, x_project_token)
    check_rate(request.client.host if request.client else "unknown", "material_qa", 30)
    question = payload.question.strip()
    if len(question) < 2:
        raise HTTPException(422, "问题至少需要2个字符")

    document_ids = set(payload.document_ids)
    if document_ids:
        available_ids = {
            str(item["id"])
            for item in rows("SELECT id FROM analysis_documents WHERE project_id=?", (project_id,))
        }
        if not document_ids <= available_ids:
            raise HTTPException(404, "指定材料不存在")

    query_started = time.perf_counter()
    try:
        hits = retrieve_chunks(project_id, question, top_k=30, document_ids=document_ids or None)
    except Exception as exc:
        return {
            "status": "retrieval_unavailable",
            "answer": "本地材料检索暂不可用，请稍后重试。",
            "claims": [], "citations": [], "retrieval_mode": "unavailable",
            "retrieval_metrics": {"query_ms": round((time.perf_counter() - query_started) * 1000)},
            "error_type": type(exc).__name__,
        }
    retrieval_metrics = {
        **index_diagnostics(project_id),
        "query_ms": round((time.perf_counter() - query_started) * 1000),
    }

    selected: list[dict] = []
    selected_chars = 0
    seen_chunks: set[str] = set()
    for hit in hits:
        chunk_id = str(hit["chunk_id"])
        quote = str(hit.get("text") or "").strip()
        if not quote or chunk_id in seen_chunks or len(selected) >= 8:
            continue
        if selected and selected_chars + len(quote) > 12000:
            continue
        selected.append({
            "id": f"c-{hit['source_id']}",
            "chunk_id": chunk_id,
            "source_id": str(hit["source_id"]),
            "document_id": str(hit["document_id"]),
            "filename": str(hit.get("filename") or ""),
            "page": hit.get("page"),
            "locator": f"第 {hit['page']} 页" if hit.get("page") else "材料片段",
            "quote": quote,
            "chunk_type": str(hit.get("chunk_type") or "text"),
            "table_key": str(hit.get("table_key") or ""),
            "score": round(float(hit.get("score") or 0), 6),
        })
        selected_chars += len(quote)
        seen_chunks.add(chunk_id)

    retrieval_mode = str(hits[0].get("retrieval_mode") or retrieval_status()["mode"]) if hits else retrieval_status()["mode"]
    if not selected:
        return {
            "status": "insufficient",
            "answer": "本次检索没有找到足以回答该问题的材料片段；这不代表材料未披露。",
            "claims": [], "citations": [], "retrieval_mode": retrieval_mode,
            "conflict_detected": False,
            "retrieval_metrics": retrieval_metrics,
        }

    source_ids = {str(item["source_id"]) for item in selected}
    conflicted_source_ids: set[str] = set()
    for conflict in rows(
        "SELECT assertion_ids FROM canonical_facts WHERE project_id=? AND status='conflicted'",
        (project_id,),
    ):
        assertion_ids = json_load(conflict.get("assertion_ids") or "[]")
        if not assertion_ids:
            continue
        placeholders = ",".join("?" for _ in assertion_ids)
        assertions = rows(
            f"SELECT source_ids FROM fact_assertions WHERE project_id=? AND id IN ({placeholders})",
            (project_id, *assertion_ids),
        )
        for assertion in assertions:
            conflicted_source_ids.update(json_load(assertion.get("source_ids") or "[]"))
    conflict_detected = bool(source_ids & conflicted_source_ids)

    if not is_configured():
        return {
            "status": "retrieved_only",
            "answer": "已找到相关材料片段；当前未配置回答模型，请核对下方原文。",
            "claims": [], "citations": selected, "retrieval_mode": retrieval_mode,
            "conflict_detected": conflict_detected,
            "retrieval_metrics": retrieval_metrics,
        }

    try:
        result, usage = answer_material_question(
            question,
            selected,
            LlmCallContext(project_id, "", "material_qa", "material_qa"),
        )
    except Exception as exc:
        error_type = exc.error_type if isinstance(exc, StructuredLlmError) else type(exc).__name__
        return {
            "status": "model_unavailable",
            "answer": "已找到相关材料，但当前模型未能生成回答；请核对下方原文。",
            "claims": [], "citations": selected, "retrieval_mode": retrieval_mode,
            "conflict_detected": conflict_detected, "error_type": error_type,
            "retrieval_metrics": retrieval_metrics,
        }

    cited_ids = {source_id for claim in result["claims"] for source_id in claim["source_ids"]}
    return {
        "status": "answered" if result["claims"] else "insufficient",
        "answer": result["answer"],
        "claims": result["claims"],
        "citations": [item for item in selected if item["source_id"] in cited_ids] or selected,
        "retrieval_mode": retrieval_mode,
        "conflict_detected": conflict_detected,
        "retrieval_metrics": retrieval_metrics,
        "usage": usage.__dict__,
    }


@app.get("/api/citations/{citation_id}")
def citation(citation_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    item = row("SELECT * FROM citations WHERE id=?", (citation_id,))
    source_id = item.get("source_id") if item else citation_id[2:] if citation_id.startswith("c-") else ""
    source = row(
        "SELECT s.*,d.filename,d.kind AS document_kind,d.document_role "
        "FROM analysis_sources s JOIN analysis_documents d "
        "ON d.id=s.document_id AND d.project_id=s.project_id WHERE s.id=?",
        (source_id,),
    ) if source_id else None
    if source:
        _authorize(source["project_id"], x_project_token)
        return {
            "id": item["id"] if item else citation_id,
            "project_id": source["project_id"],
            "source_id": source["id"],
            "document_id": source["document_id"],
            "label": item.get("label") if item and item.get("label") else source["filename"],
            "quote": source["text"],
            "locator_type": source["locator_type"],
            "locator": source["locator"],
            "page": source.get("page"),
            "bbox": json_load(source.get("bbox_json", "[]")),
            "extraction_method": source.get("extraction_method", "native"),
            "origin_type": source.get("origin_type", "document"),
            "snapshot_id": source.get("snapshot_id", ""),
            "profile_id": source.get("profile_id", ""),
            "profile_version": int(source.get("profile_version") or 0),
            "entity_role": source.get("entity_role", ""),
            "origin_source_id": source.get("origin_source_id", source["id"]),
            "document_kind": source.get("document_kind", ""),
            "document_role": source.get("document_role", ""),
        }
    if not item:
        raise HTTPException(404, "引用不存在")
    _authorize(item["project_id"], x_project_token)
    item["page"] = None
    item["bbox"] = []
    item["extraction_method"] = "native"
    item["origin_type"] = "document"
    item["snapshot_id"] = ""
    item["profile_id"] = ""
    item["profile_version"] = 0
    item["entity_role"] = ""
    return item


@app.get("/api/projects/{project_id}/export")
def export(project_id: str, format: str = "md", variant: str = "draft", x_project_token: Optional[str] = Header(default=None)) -> Response:
    project = _authorize(project_id, x_project_token)
    if variant not in {"draft", "final"}:
        raise HTTPException(400, "导出版本仅支持 draft 或 final")
    if project.get("data_class") == "real_reference" and not settings.allow_real_reference_export:
        raise HTTPException(403, "真实参考项目报告默认禁止导出；确认材料使用权限后可通过配置显式开启")
    if format == "docx" and project.get("data_class", "user_project") == "user_project":
        state = report_state(project_id)
        quality = report_quality(project_id)
        unresolved = unresolved_items(project_id)
        if variant == "final" and (state["report_stale"] or quality["status"] != "passed" or unresolved["has_open_major_items"] or state["report_reviewed_revision"] != state["report_revision"]):
            raise HTTPException(409, "报告仍有未应用复核、校验失败或未决重大事项，不能导出最终 Word；可先导出草稿")
        if not rows("SELECT id FROM report_sections WHERE project_id=? LIMIT 1", (project_id,)):
            raise HTTPException(409, "报告尚未生成或证据校验未通过，暂不能导出 Word")
    # 最终版本（Word 与 Markdown）共用同一交付一致性门槛：支持率通过不能掩盖回退、
    # 强制披露缺失或正文被改动。草稿仍可导出，但会带上"待人工复核"警示。
    if variant == "final":
        blockers = delivery_blockers(project_id)
        if blockers:
            raise HTTPException(409, "报告未通过交付一致性校验，不能导出最终版本：" + "；".join(blockers))
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", project["company_name"])
    encoded_name = quote(safe_name)
    unresolved = unresolved_items(project_id)
    audit_event(project_id, "export", "report", format, {}, {"unresolved": unresolved["has_open_major_items"]}, "human")
    warning_headers = {"X-Report-Open-Items": "true" if unresolved["has_open_major_items"] else "false"}
    suffix = quote("_草稿" if variant == "draft" else "_最终")
    if format == "md":
        return Response(markdown_report(project_id, variant), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}{suffix}.md", **warning_headers})
    if format == "docx":
        return Response(docx_report(project_id, variant), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}{suffix}.docx", **warning_headers})
    raise HTTPException(400, "仅支持 md 或 docx")


frontend_dist = settings.root_dir / "frontend" / "dist"
if frontend_dist.exists():
    assets = frontend_dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        candidate = frontend_dist / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend_dist / "index.html")
