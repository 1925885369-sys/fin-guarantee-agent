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

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .chapter_guard import (
    NO_MODEL_REASON, _materials_fingerprint, assess_chapter_length, build_chapter_context, count_report_chars,
)
from .chapter_prompts import (
    BOND_CHAPTER_KEYS, BOND_CHAPTER_PART, BOND_CHAPTER_TITLES, PROMPT_CONTRACT_VERSION,
    chapter_default_prompt, chapter_prompt,
    chapter_uses_llm, compile_structured_prompt, editable_config_from_factory, effective_chapter_config,
    load_chapter_config,
    global_prompt_edit_state, legacy_prompt_config, migrate_legacy_prompt_overrides,
    prompt_config_payload, prompt_edit_state,
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
from .llm import LlmCallContext, StructuredLlmError, answer_material_question, is_configured, rewrite_section
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
from .report_outline import section_labels, outline_for_grouping
from .report_delivery import (
    content_sha256, delivery_blockers, latest_attempt, record_chapter_attempt,
    reconcile_historical_sections, review_notice, set_section_attempt, write_report_section,
)
from .review import decode_candidates, decode_tasks, decide_candidate, review_summary, unresolved_items, update_task
from .review_lifecycle import mark_report_reviewed, report_state
from .bond_case import PROJECT_ID as BOND_PROJECT_ID, _materials as bond_materials, seed_bond_case
from .bond_portfolio_cases import generate_bond_portfolio_materials, seed_bond_portfolio_cases
from .real_reference import ensure_project_classification, is_real_reference, seed_real_reference_projects
from .report_outline_store import (
    delete_outline, has_project_override, outline_effective_source, outline_scope_state,
    payload_sha256, save_outline, validate_outline,
)
from .schemas import (
    CandidateDecision, DocumentUpdate, EntityProfileCreate, EntityProfileUpdate, ReportOutlineUpdate, ReportTableRefresh,
    FactGroupResolve, FactUpdate, MaterialQARequest, ProfileImportCommit, ProjectCreate, ProjectUpdate, PromptEditableConfig, ReportPromptPreview, ReportPromptUpdate, ReviewTaskUpdate, RunCreate, SectionRegenerateRequest, SectionUpdate, TableCellReview,
)
from .security import check_rate, cleanup_expired_projects, validate_magic
from .retrieval import index_diagnostics, retrieval_status, retrieve_chunks
from .workflow import (
    TERMINAL, _numbers_supported, apply_review_decisions, chapter_diagnostics,
    invalidate_project_analysis, is_model_content_status, retry_run, run_chapter_model_attempt,
    start_run,
)


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

    规则：

    - **项目级**：非真实参考项目可编辑（受既有项目令牌与归档门禁约束）；真实参考项目
      （``data_class == 'real_reference'``）仅当**非公开环境**、请求来自**本机直连**
      （``request.client.host`` 为环回地址）且**不带任何代理转发头**时才能编辑；
    - **全局默认**：允许非公开环境、本机直连且无代理转发头的请求修改
      （`PLAN_new.md` §一 / `PLAN_new_p0_p1.md` P0-1）。旧口径"一律不可编辑"已被新决定
      替代——全局是**部署级**配置，与项目是否真实参考无关，所以只判环境与本机直连，
      不再叠加项目类型条件。

    两个权限各自带原因串：`reason`（项目级）与 `global_reason`（全局级），
    接口原样透出，前端据此给出"为什么不能改"。
    """
    local = _is_loopback_client(request)
    forwarded = _has_proxy_forwarding_headers(request)
    # "本机可写"是**环境级**判据，项目级与全局级共用；`editable` 再叠加"是否真实参考项目"。
    local_writable = (not settings.public_demo) and local and not forwarded
    if settings.public_demo:
        global_reason = "公开演示环境不允许修改全局默认 Prompt"
    elif forwarded:
        global_reason = "请求带有代理转发头，无法确认本机直连，拒绝修改全局默认 Prompt"
    elif not local:
        global_reason = "全局默认 Prompt 仅允许本机直连（环回地址）修改"
    else:
        global_reason = ""

    real_reference = is_real_reference(project_id)
    reason = ""
    if real_reference:
        if settings.public_demo:
            reason = "公开演示环境不允许修改真实参考项目的 Prompt"
        elif forwarded:
            reason = "请求带有代理转发头，无法确认本机直连，拒绝修改真实参考项目 Prompt"
        elif not local:
            reason = "真实参考项目的项目级 Prompt 仅允许本机直连（环回地址）修改"
    return {
        # 非真实参考项目保持历史口径（不受本机判据限制）；真实参考项目要求本机直连。
        "editable": local_writable or not real_reference,
        "global_editable": local_writable, "real_reference": real_reference,
        "reason": reason, "global_reason": global_reason,
        "environment": "public_demo" if settings.public_demo else "local",
    }


def _prompt_permission_fields(permissions: Optional[dict]) -> dict:
    permissions = permissions or {}
    return {
        "editable": bool(permissions.get("editable", True)),
        "global_editable": bool(permissions.get("global_editable", False)),
        "edit_reason": str(permissions.get("reason") or ""),
        "global_edit_reason": str(permissions.get("global_reason") or ""),
        "real_reference": bool(permissions.get("real_reference", False)),
    }


def _require_global_prompt_editable(project_id: str, request: Request) -> dict:
    """全局默认 Prompt 保存/重置的门禁。

    与项目级的区别：全局是**部署级**配置，判据只看"非公开环境 + 本机直连 + 无转发头"，
    不叠加真实参考项目条件（`PLAN_new_p0_p1.md` P0-1）。项目令牌与归档门禁仍由调用方
    先行执行——全局接口挂在 `/api/projects/{id}/` 下，仍需证明对该项目有访问权。
    """
    permissions = prompt_edit_permissions(project_id, request)
    if not permissions["global_editable"]:
        raise HTTPException(403, permissions.get("global_reason") or "当前环境不允许修改全局默认 Prompt")
    return permissions


def _prompt_config_sha256(prompt: str, structured_json: str) -> str:
    """保存/重置的并发基准哈希：覆盖**即将被改写的那份状态**。

    取「生效 prompt 文本 + 结构化配置 JSON」两者的组合，而不是只取其一：
    - 只比 prompt 文本时，结构化配置改了但编译结果恰好相同的情况会被漏判；
    - 只比 structured_json 时，`legacy` 覆盖（没有结构化配置）无值可比。
    空配置与文本都为空 → 返回空串，调用方据此跳过并发检查（"无内容可覆盖"）。
    """
    if not str(prompt or "").strip() and not str(structured_json or "").strip():
        return ""
    return hashlib.sha256(f"{prompt or ''}\n{structured_json or ''}".encode("utf-8")).hexdigest()


def _require_prompt_editable(project_id: str, request: Request) -> dict:
    """项目级 Prompt 保存/重置的门禁，返回权限字典供响应体复用。"""
    permissions = prompt_edit_permissions(project_id, request)
    if not permissions["editable"]:
        raise HTTPException(403, permissions["reason"] or "当前环境不允许修改项目 Prompt")
    return permissions


def _prompt_outdated(project_id: str, chapter_key: str, stored_sha256: str) -> bool:
    """正文生成时的 Prompt 哈希是否与**当前有效 Prompt** 不同（计划 §四 line 41）。

    计划原文：「``prompt_outdated`` 改为比较当前有效 Prompt 哈希与生成时哈希；
    **保存 Prompt 不自动再生**」。所以这里只**报告差异**，绝不触发再生——改 Prompt 的人
    自己决定要不要重跑那几章（`update_report_prompt` 只写覆盖与审计，不碰 `report_sections`，
    因此保存后本函数自然转为 True，而正文保持原样）。

    与旧实现的区别：旧口径比的是"库里的覆盖文本 vs 当前编译器对同一 ``structured_json`` 的
    重编译结果"，回答的是"**覆盖文本该不该重编译**"——那是编辑器侧的事，由
    ``recompile_structured_prompts`` 在启动时自行处理，与本函数无关。字段名与计划要求指向的
    是"**这份报告是不是用旧 Prompt 生成的**"（交付侧的事）。同一函数名下两种语义会互相误导，
    故按计划改为后者。

    生成时哈希取 ``report_sections.prompt_sha256``（由 ``build_chapter_context`` 与正文
    同事务冻结）；**历史行该列为空 → 无法判断，返回 False**，不制造"全部过期"的假警报。
    哈希口径与再生接口的 409 并发检查同源（同一个 `chapter_prompt` + sha256）。
    """
    if not stored_sha256:
        return False
    try:
        current = hashlib.sha256(chapter_prompt(chapter_key, project_id).encode("utf-8")).hexdigest()
    except Exception:
        # 取不到当前 Prompt（章节不存在/配置损坏）时不宣称过期：宁可漏报，不误报。
        return False
    return current != stored_sha256


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


# 项目基础信息（发行人／标的名称／发行规模）被修改时的失效原因码。这三个字段是
# 分析口径的一部分，改了就必须重跑；沿用 report_stale_reasons 既有的字符串约定。
PROJECT_INFO_STALE_REASON = "project_info_changed"


def _project_info_fields(project: dict) -> dict:
    """项目基础信息三字段的审计口径（不含 access_token 等敏感列）。"""
    return {
        "company_name": str(project.get("company_name") or ""),
        "bond_name": str(project.get("bond_name") or ""),
        "amount": project.get("amount"),
    }


@app.patch("/api/projects/{project_id}")
def patch_project(project_id: str, payload: ProjectUpdate,
                  x_project_token: Optional[str] = Header(default=None)) -> dict:
    """修改项目基础信息：发行人名称 / 标的名称 / 发行规模。

    只动这三个字段——材料、报告、风险与运行历史一律保留，但把分析口径标记为待更新
    （``analysis_stale`` + ``report_stale`` + 撤销已复核修订），旧成果因此不能再被标记为
    最新最终报告：`report_state` 的 ``final_ready`` 要求二者皆非 stale，且
    ``report_reviewed_revision == report_revision``。

    「检查活动任务 + 写库」整段放在 ``BEGIN IMMEDIATE`` 事务里，与 ``start_run`` 争夺同一把
    写锁，避免修改基础信息与尽调任务启动交错。无实际变化时不写库、不审计、不置失效。
    """
    project = _authorize(project_id, x_project_token)
    if project.get("data_class", "user_project") != "user_project":
        raise HTTPException(403, "仅用户创建的项目可以修改基础信息")
    if project.get("project_type") != "bond_guarantee":
        raise HTTPException(403, "仅债券担保项目可以修改基础信息")
    _ensure_project_writable(project)

    submitted = payload.model_dump(exclude_unset=True)
    now = now_iso()
    changed = False
    before: dict = {}
    after: dict = {}
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not current:
            raise HTTPException(404, "项目不存在")
        active = conn.execute(
            "SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running') LIMIT 1",
            (project_id,),
        ).fetchone()
        if active:
            raise HTTPException(409, f"项目正在分析，完成或取消任务后再修改基础信息：{active['id']}")
        if current["archived_at"]:
            raise HTTPException(409, "项目已归档，请先恢复项目后再修改")

        before = _project_info_fields(dict(current))
        after = dict(before)
        for field in ("company_name", "bond_name"):
            value = submitted.get(field)
            if value is not None:
                after[field] = value
        if submitted.get("amount") is not None:
            after["amount"] = float(submitted["amount"])
        changed = after != before
        if changed:
            reasons = json_load(current["report_stale_reasons"] or "[]")
            if PROJECT_INFO_STALE_REASON not in reasons:
                reasons.append(PROJECT_INFO_STALE_REASON)
            conn.execute(
                "UPDATE projects SET company_name=?,bond_name=?,amount=?,updated_at=?,"
                "analysis_stale=1,report_stale=1,report_stale_reasons=?,"
                "report_reviewed_revision=0,report_reviewed_at=NULL WHERE id=?",
                (after["company_name"], after["bond_name"], after["amount"], now,
                 json_dump(reasons), project_id),
            )
    if changed:
        # 审计写在事务之外：audit_event 自建连接，放事务里会自己再抢一次写锁。
        audit_event(project_id, "project_update", "project", project_id, before, after, "human")
    detail = get_project(project_id, x_project_token)
    detail["changed"] = changed
    return detail


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
    material_issue_amount = value("bond_issue_amount")
    material_bond_name = value("bond_name")
    issue_value = None if "bond_issue_amount" in conflicted_fields else (material_issue_amount or project["amount"])
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
        # 取值来源必须显式给出：这两个字段在材料没有披露时会回落到项目录入值，
        # 前端只看「非空」就会把项目录入的数字标成「募集说明书」。与上面的 or 语义逐字对齐。
        "bondNameSource": "project" if project.get("bond_name") else ("material" if material_bond_name else "none"),
        "issueAmountSource": ("conflict" if "bond_issue_amount" in conflicted_fields
                              else "material" if material_issue_amount else "project"),
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
    # P1-1：报告接口同时给出目录口径——`outline_outdated` 表示"报告采用的目录"与当前
    # 配置不一致；`regeneration_requirement` ∈ none/chapter/full_report/unknown
    # （旧报告无 adoption 优先历史快照，无快照标 unknown，不得冒充一致）。
    from .report_outline_adoption import regeneration_status

    return {**report_quality(project_id), **regeneration_status(project_id)}


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
    from .report_outline_adoption import regeneration_status

    return {**review_summary(project_id), **report_state(project_id), **regeneration_status(project_id)}


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
    # P1-4：展示分组跟随生效目录（章节改名/排序立即生效；被删章节未再生前仍显示）。
    # 章节标题的成品用词规范化（旧计划剩余项 P0-2）：新写入的 `title` 已规范化，但**历史
    # 行**仍是原材料名（"评级报告复核及评级差异"）——读取侧再规范一次，保证网页目录/标题栏
    # 与 Word 导出（`exports.py` 已规范化）逐字一致；幂等，不改变已规范的值。
    # 必须在 `section_labels` **之前**执行：章标题（chapter_label）与分部标题都由
    # `section["title"]` 派生，先规范才能让目录一起正确。
    from .delivery_wording import material_numbers as _material_numbers
    from .delivery_wording import normalize_report_text as _normalize_report_text
    _title_numbers = _material_numbers(project_id)
    for section in sections:
        section["title"] = _normalize_report_text(str(section.get("title") or ""), _title_numbers)
    labels = section_labels(sections, outline=outline_for_grouping(project_id))
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
        # 最新一次生成尝试的摘要（状态/阶段/修复类型/是否采用/未决数量/诊断）：前端据此
        # 区分"现正文来自哪次成功"与"最新尝试是否失败"（修订稿 §四）。
        section["latest_attempt"] = latest_attempt(project_id, section["section_key"])
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
        # 生成模式与 Prompt 时效：前端据此提示"该章用哪套 Prompt 生成、是否需要再生"。
        # ``generation_mode`` 目前尚未写入章节契约，必须用 ``.get`` 容错（默认 rewrite）。
        section_key = section["section_key"]
        section["generation_mode"] = load_chapter_config(section_key).get("generation_mode", "rewrite")
        # 计划 §四 line 41：比"当前有效 Prompt 哈希 vs **生成时**哈希"（生成时哈希随正文冻结
        # 在 `prompt_sha256` 列）。为真只表示"Prompt 改过了、报告还是旧版"，**不自动再生**。
        section["prompt_outdated"] = _prompt_outdated(
            project_id, section_key, str(section.get("prompt_sha256") or ""))
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
        # 统一冻结记录（三项遗留修复方案 §一）：记录规模与版本对接口可见；旧章节兼容
        # table_records_json（legacy 迁移），两者都空标 legacy_unfrozen。
        frozen_records = json_load(section.get("evidence_records_json") or "[]") or []
        if frozen_records:
            section["evidence_record_count"] = len(frozen_records)
            section["support_record_version"] = "evidence-records-v1"
        else:
            legacy_records = json_load(section.get("table_records_json") or "[]") or []
            section["evidence_record_count"] = len(legacy_records)
            section["support_record_version"] = (
                "legacy:table-records-v1" if legacy_records else "legacy_unfrozen"
            )
        section["needs_review_claims"] = row(
            "SELECT COUNT(*) count FROM report_claims WHERE section_id=? AND support_status='needs_review'",
            (section["id"],),
        )["count"]
        # Prompt 契约编译版本（三项遗留修复方案 §二）：接口返回当前契约版本；
        # `prompt_outdated` 由 _prompt_outdated 另给（结构化覆盖早于当前编译器）。
        section["prompt_contract_version"] = PROMPT_CONTRACT_VERSION
    return sections


@app.get("/api/projects/{project_id}/report-history")
def report_history(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    """已完成任务的历史报告摘要（倒序），按任务留存、每个完成任务最多一份。

    默认不补造上线前旧任务的快照，因此全新项目首次打开会是空列表（前端显示
    「暂无已留存报告」），这是预期行为。
    """
    from .report_snapshots import list_snapshots

    _authorize(project_id, x_project_token)
    return list_snapshots(project_id)


@app.get("/api/projects/{project_id}/report-history/{snapshot_id}")
def report_history_detail(project_id: str, snapshot_id: str,
                          x_project_token: Optional[str] = Header(default=None)) -> dict:
    """完整快照。沿用项目令牌鉴权，并校验快照确属本项目（跨项目一律 404）。"""
    from .report_snapshots import get_snapshot

    _authorize(project_id, x_project_token)
    item = get_snapshot(project_id, snapshot_id)
    if not item:
        raise HTTPException(404, "历史报告不存在")
    return item


def _table_refresh_validation(project_id: str, chapter: str, content: str, draft: str,
                              original: dict) -> dict:
    """表格刷新后用**共同校验器**复核刷新稿（不调用模型）。返回 {status, reasons}。

    尽力而为：上下文构建或校验异常一律降级为"未复核"（status=''），绝不让表格刷新失败。
    """
    try:
        from .chapter_guard import validate_chapter_output

        source_ids = json_load(original.get("allowed_source_ids") or "[]")
        ctx = build_chapter_context(project_id, chapter, draft or content, list(source_ids))
        result = validate_chapter_output(content, json_load(original.get("citation_ids") or "[]"), ctx)
        reasons = [str(item) for item in (result.get("reasons") or [])][:5]
        return {"status": result.get("status") or "", "reasons": reasons}
    except Exception:
        return {"status": "", "reasons": []}


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
            # 表格刷新同步更新证据记录并调用**共同校验器**（修订稿 §四）：不调用模型，只对
            # 刷新后的正文重跑章节校验；失配即标注待复核——这正是"正文中的主张需重新核验"的判据。
            refresh_validation = _table_refresh_validation(project_id, chapter, content, draft, original)
            if refresh_validation.get("status") not in ("", "passed"):
                delivery = "needs_review"
            updates.append((chapter, content, draft, disclosures, policy, length, disclosure, delivery,
                            validated_hash, new_digest, next_input_hash, blocks, original, refresh_validation))

        now = now_iso()
        for (chapter, content, draft, disclosures, _policy, length, disclosure, delivery,
             validated_hash, digest, input_hash, blocks, original, refresh_validation) in updates:
            diagnostics = {"system_tables_refreshed": True, "table_rule_version": TABLE_RULE_VERSION}
            if refresh_validation.get("status") not in ("", "passed"):
                diagnostics["table_refresh_validation"] = refresh_validation.get("status")
                diagnostics["table_refresh_needs_review"] = True
                diagnostics["table_refresh_reasons"] = refresh_validation.get("reasons") or []
            conn.execute(
                "UPDATE report_sections SET content=?,deterministic_draft=?,content_sha256=?,"
                "validated_content_sha256=?,input_hash=?,required_disclosures=?,review_status='draft',"
                "support_validation_status='pending',validation_status='pending',disclosure_status=?,"
                "delivery_status=?,validation_diagnostics=?,raw_output_chars=?,visible_content_chars=?,"
                "length_status=?,updated_at=? WHERE project_id=? AND section_key=?",
                (content, draft, digest, validated_hash, input_hash, json_dump(disclosures), disclosure,
                 delivery, json_dump(diagnostics),
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
                              "input_hash": input_hash, "table_hash": table_blocks_hash(blocks),
                              "validation": refresh_validation.get("status") or "passed"})

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


def _prompt_row_state(project_id: str, chapter_key: str, permissions: Optional[dict] = None,
                      scope: str = "project") -> dict:
    """Effective prompt row for a chapter: project override > global default > YAML.

    ``permissions`` 为 ``prompt_edit_permissions`` 的结果；缺失时按"项目级可编辑、
    全局不可编辑"处理（与 GET 未显式传入时保持一致的保守默认）。

    ``scope`` 决定**可变的那一份**是哪个范围（`PLAN_new_p0_p1.md` P0-1 的"项目配置／全局默认"
    切换）：``project`` 用 项目覆盖 > 全局 > YAML 链；``global`` 只用 全局覆盖 > YAML 链
    （不掺项目覆盖，否则在"全局默认"范围里会看到并保存项目配置）。
    两个范围的并发基准哈希都返回，前端切范围时用对应那一个。
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
    effective_config, source, migration_mode = prompt_edit_state(chapter_key, project_id)
    global_config, global_source, global_migration_mode = global_prompt_edit_state(chapter_key)
    global_prompt = (str(global_override["prompt"])
                     if global_override and str(global_override.get("prompt") or "").strip()
                     else default_prompt)
    if scope == "global":
        editable_config, migration_mode = global_config, global_migration_mode
        prompt = global_prompt
    else:
        editable_config = effective_config
        prompt = chapter_prompt(chapter_key, project_id)
    # 并发基准：项目级保存/重置改写的是项目覆盖行；尚无覆盖时为空串
    # （`_check_prompt_concurrency` 对空值同样拦截，避免"以为在改一份不存在的覆盖"）。
    project_config_sha = _prompt_config_sha256(
        (project_override or {}).get("prompt") or "", (project_override or {}).get("structured_json") or "")
    global_config_sha = _prompt_config_sha256(
        (global_override or {}).get("prompt") or "", (global_override or {}).get("structured_json") or "")
    return {
        "title": BOND_CHAPTER_TITLES.get(chapter_key, chapter_key),
        "part": BOND_CHAPTER_PART.get(chapter_key, ""),
        "prompt": prompt,
        "default_prompt": default_prompt,
        "overridden": bool(project_override and str(project_override.get("prompt") or "").strip()),
        "updated_at": project_override.get("updated_at") if project_override else None,
        "global_overridden": bool(global_override and str(global_override.get("prompt") or "").strip()),
        "global_updated_at": global_override.get("updated_at") if global_override else None,
        # 全局视图的"当前全局生效 Prompt"（全局覆盖，否则出厂 YAML）：前端"全局默认"范围
        # 在没有覆盖时也要能看到并编辑出厂配置。
        "global_prompt": global_prompt,
        "scope": scope,
        # `PLAN_new_p0_p1.md` P0-1：项目/全局两个范围各自的并发基准哈希。
        "config_sha256": project_config_sha,
        "global_config_sha256": global_config_sha,
        # 当前编译器版本（`PLAN.md` §四.2）：前端读取后原样回传，版本不符时保存被 409。
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        # 当前**生效**来源：项目覆盖 > 全局默认 > YAML。前端据此提示"有项目覆盖时项目仍优先"。
        "effective_source": source,
        "global_source": global_source,
        "editable_config": editable_config,
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
        # 权限：项目级是否可编辑 + 全局是否可编辑（全局 = 非公开且本机直连无转发头）。
        **_prompt_permission_fields(permissions),
    }


@app.get("/api/projects/{project_id}/report-prompts")
def report_prompts(project_id: str, request: Request,
                   x_project_token: Optional[str] = Header(default=None),
                   scope: str = Query(default="project", pattern="^(project|global)$")) -> list[dict]:
    """Return every report chapter's effective prompt and its contract metadata.

    ``scope=global`` 返回**全局默认**范围：每章的 `prompt`/`editable_config` 取
    全局覆盖 > 出厂 YAML 链（不含项目覆盖），供前端"全局默认"页加载与回填。
    两种范围返回同一套字段，前端切换时可原样复用渲染与保存逻辑。
    """
    _authorize(project_id, x_project_token)
    permissions = prompt_edit_permissions(project_id, request)
    # P1-4：章节列表跟随生效目录——内置章节（template_key）+ 自定义章节（chapter_id）。
    # 自定义章节可编辑章级 Prompt（覆盖链以 chapter_id 为键），前端据此渲染同一编辑器。
    chapters: list[tuple[str, str, str]] = []  # (chapter_key, part_title, chapter_title)
    try:
        from .report_outline_store import effective_outline

        for part in effective_outline(project_id).get("parts") or []:
            part_title = str(part.get("title") or "")
            for chapter in part.get("chapters") or []:
                key = str(chapter.get("template_key") or chapter.get("chapter_id") or "")
                if key:
                    chapters.append((key, part_title, str(chapter.get("title") or "")))
    except Exception:
        chapters = []
    if not chapters:
        chapters = [(key, BOND_CHAPTER_PART.get(key, ""), BOND_CHAPTER_TITLES.get(key, key)) for key in BOND_CHAPTER_KEYS]
    output = []
    for chapter_key, part_title, chapter_title in chapters:
        config = load_chapter_config(chapter_key)
        output.append({
            "chapter_key": chapter_key,
            # 目录分部级标题与章节标题覆盖内置映射（自定义章节也能拿到所在分部名与改名标题）。
            **(_prompt_row_state(project_id, chapter_key, permissions, scope)),
            "title": chapter_title or BOND_CHAPTER_TITLES.get(chapter_key, chapter_key),
            "part": part_title or BOND_CHAPTER_PART.get(chapter_key, ""),
            "llm": config.get("llm", False),
            "required_sections": config.get("required_sections", []),
        })
    return output


def _compile_prompt_payload(chapter_key: str, payload: "ReportPromptUpdate") -> dict:
    """把请求体编译成落库用的四元组（项目级与全局级共用，避免两处逻辑漂移）。

    返回 ``{prompt, structured_json, migration_mode, legacy_prompt}``：
    - 提交 ``config``（新客户端）→ 结构化配置，由服务端用固定编译器产出生效文本；
    - 提交 ``prompt``（旧客户端）→ 走 legacy 解析，尽力还原为结构化配置后再编译，
      这样编辑器打开时看到的仍是一份可编辑结构，而不是一堆自由文本。
    """
    config = payload.config
    if config is not None:
        dumped = config.model_dump(mode="json")
        return {
            "prompt": compile_structured_prompt(chapter_key, dumped),
            "structured_json": json_dump(dumped),
            "migration_mode": "structured",
            "legacy_prompt": "",
        }
    legacy_prompt = str(payload.prompt).strip()
    legacy_config, migration_mode = legacy_prompt_config(chapter_key, legacy_prompt)
    return {
        "prompt": compile_structured_prompt(chapter_key, legacy_config),
        "structured_json": json_dump(legacy_config),
        "migration_mode": migration_mode,
        "legacy_prompt": legacy_prompt,
    }


def _prompt_write_guards(chapter_key: str) -> None:
    """项目级与全局级写入口共用的章节判据。"""
    from .chapter_prompts import is_custom_chapter_key

    if chapter_key not in BOND_CHAPTER_KEYS and not is_custom_chapter_key(chapter_key):
        raise HTTPException(404, "报告章节不存在")
    if not chapter_uses_llm(chapter_key):
        raise HTTPException(422, "确定性章节由系统模板生成，不支持修改")


def _check_prompt_concurrency(expected: Optional[str], current: str, scope_label: str) -> None:
    """乐观并发：请求带的预期哈希与库中现状不一致 → 409。

    只对**非空**的预期哈希生效，且现状为空（尚无覆盖）时同样拦截——那说明别人刚在你
    之前保存过，或你以为自己在改一份并不存在的覆盖。客户端不传预期哈希时不检查
    （兼容旧客户端）。
    """
    if not str(expected or "").strip():
        return
    if str(expected) != str(current or ""):
        raise HTTPException(409, f"{scope_label}已被其他会话修改，请重新加载后再保存")


def _check_prompt_contract_version(expected: Optional[str]) -> None:
    """编译契约版本检查（`PLAN.md` §四.2：写入前检查"当前编译器版本"）。

    结构化配置由**编译器**渲染成生效 Prompt 文本；编译器升级后同一份配置会渲染出不同文本。
    若客户端是按旧编译器读到的内容在做编辑，直接保存会覆盖掉新编译器刚渲染的结果
    （或写出与预览不一致的文本）。因此版本不符一律 409，让客户端重读一次。
    不传则跳过（兼容旧客户端）。
    """
    if not str(expected or "").strip():
        return
    if str(expected) != str(PROMPT_CONTRACT_VERSION or ""):
        raise HTTPException(
            409, f"Prompt 编译器版本已更新（{expected} → {PROMPT_CONTRACT_VERSION}），请重新加载后再保存")


@app.patch("/api/projects/{project_id}/report-prompts/{chapter_key}")
def update_report_prompt(project_id: str, chapter_key: str, payload: ReportPromptUpdate, request: Request,
                         x_project_token: Optional[str] = Header(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    _prompt_write_guards(chapter_key)
    # 项目级保存门禁：真实参考项目仅本机直连（非公开、无转发头）时允许；否则 403。
    permissions = _require_prompt_editable(project_id, request)
    compiled = _compile_prompt_payload(chapter_key, payload)
    now = now_iso()
    before = row("SELECT * FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?", (project_id, chapter_key))
    _check_prompt_contract_version(payload.expected_prompt_contract_version)
    _check_prompt_concurrency(
        payload.expected_config_sha256,
        _prompt_config_sha256((before or {}).get("prompt") or "", (before or {}).get("structured_json") or ""),
        "项目 Prompt 配置",
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO report_prompt_overrides (id,project_id,chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id,chapter_key) DO UPDATE SET prompt=excluded.prompt,updated_at=excluded.updated_at,structured_json=excluded.structured_json,legacy_prompt=excluded.legacy_prompt,migration_mode=excluded.migration_mode",
            (str(uuid.uuid4()), project_id, chapter_key, compiled["prompt"], now, compiled["structured_json"],
             compiled["legacy_prompt"], compiled["migration_mode"]),
        )
    # 保存只动配置与审计：**不删除项目覆盖、不调用模型、不修改历史正文**。
    audit_event(project_id, "report_prompt_update", "report_prompt", chapter_key, before,
                {"prompt": compiled["prompt"], "scope": "project",
                 "config": payload.config.model_dump(mode="json") if payload.config else None}, "human")
    return {"chapter_key": chapter_key, **_prompt_row_state(project_id, chapter_key, permissions)}


@app.post("/api/projects/{project_id}/report-prompts/{chapter_key}/preview")
def preview_report_prompt(project_id: str, chapter_key: str, payload: ReportPromptPreview, request: Request,
                          x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    from .chapter_prompts import is_custom_chapter_key

    if chapter_key not in BOND_CHAPTER_KEYS and not is_custom_chapter_key(chapter_key):
        raise HTTPException(404, "报告章节不存在")
    if not chapter_uses_llm(chapter_key):
        raise HTTPException(422, "确定性章节不支持结构化预览")
    # 预览不落库、不调模型；仅回带当前环境的编辑权限供前端展示。
    permissions = prompt_edit_permissions(project_id, request)
    return {"chapter_key": chapter_key, **_prompt_permission_fields(permissions),
            "prompt": compile_structured_prompt(chapter_key, payload.config.model_dump(mode="json"))}


@app.delete("/api/projects/{project_id}/report-prompts/{chapter_key}")
def reset_report_prompt(project_id: str, chapter_key: str, request: Request,
                        x_project_token: Optional[str] = Header(default=None),
                        expected_config_sha256: Optional[str] = Query(default=None)) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    # P1-2：重置同样支持**自定义章节**（chapter-<uuid>）——删除项目覆盖后按
    # 全局覆盖 > 出厂配置回落（与 GET 的覆盖链同源）。未知章节仍 404。
    from .chapter_prompts import is_custom_chapter_key

    if chapter_key not in BOND_CHAPTER_KEYS and not is_custom_chapter_key(chapter_key):
        raise HTTPException(404, "报告章节不存在")
    # 项目级重置与保存同一门禁：真实参考项目仅本机直连（非公开、无转发头）时允许。
    permissions = _require_prompt_editable(project_id, request)
    before = row("SELECT * FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?", (project_id, chapter_key))
    # 与保存同一条并发基准（`PLAN.md` §四.2）：重置同样会覆盖别人刚写的内容。
    _check_prompt_concurrency(
        expected_config_sha256,
        _prompt_config_sha256((before or {}).get("prompt") or "", (before or {}).get("structured_json") or ""),
        "项目 Prompt 配置",
    )
    execute("DELETE FROM report_prompt_overrides WHERE project_id=? AND chapter_key=?", (project_id, chapter_key))
    audit_event(project_id, "report_prompt_reset", "report_prompt", chapter_key, before, {}, "human")
    state = _prompt_row_state(project_id, chapter_key, permissions)
    part_title, chapter_title = _outline_chapter_meta(project_id, chapter_key)
    return {
        "chapter_key": chapter_key, **state,
        # 与 GET /report-prompts 同一口径：标题/分部取生效目录，自定义章节显示目录改名。
        "title": chapter_title or BOND_CHAPTER_TITLES.get(chapter_key, chapter_key),
        "part": part_title or BOND_CHAPTER_PART.get(chapter_key, ""),
    }


def _outline_chapter_meta(project_id: str, chapter_key: str) -> tuple[str, str]:
    """生效目录里的 (分部标题, 章节标题)；目录不可用或未收录时返回空串。"""
    try:
        from .report_outline_store import effective_outline

        for part in effective_outline(project_id).get("parts") or []:
            for chapter in part.get("chapters") or []:
                key = str(chapter.get("template_key") or chapter.get("chapter_id") or "")
                if key == chapter_key:
                    return str(part.get("title") or ""), str(chapter.get("title") or "")
    except Exception:
        return "", ""
    return "", ""


@app.patch("/api/projects/{project_id}/report-prompts/global/{chapter_key}")
def set_global_report_prompt(project_id: str, chapter_key: str, payload: ReportPromptUpdate, request: Request,
                             x_project_token: Optional[str] = Header(default=None)) -> dict:
    """保存**全局默认** Prompt（`PLAN_new_p0_p1.md` P0-1）。

    旧口径"全局一律禁止写入"（修订稿 §4）已被新决定替代：全局默认允许**非公开环境 +
    本机直连 + 无代理转发头**的请求修改。接口挂在项目路径下只是为了复用项目令牌与归档
    门禁——写入的是**部署级**配置，因此不叠加"是否真实参考项目"这一项目级条件。

    保存语义（P0-1 明文）：**不删除项目覆盖、不调用模型、不修改历史正文**，只更新配置、
    审计与 Prompt 哈希。生效优先级保持 项目覆盖 > 全局默认 > YAML，所以已有项目覆盖的
    项目在保存全局后**仍然使用项目配置**——响应里的 `effective_source` 会如实说明。
    """
    _ensure_project_writable(_authorize(project_id, x_project_token))
    _prompt_write_guards(chapter_key)
    permissions = _require_global_prompt_editable(project_id, request)
    compiled = _compile_prompt_payload(chapter_key, payload)
    now = now_iso()
    before = row("SELECT * FROM global_report_prompts WHERE chapter_key=?", (chapter_key,))
    _check_prompt_contract_version(payload.expected_prompt_contract_version)
    _check_prompt_concurrency(
        payload.expected_config_sha256,
        _prompt_config_sha256((before or {}).get("prompt") or "", (before or {}).get("structured_json") or ""),
        "全局 Prompt 配置",
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO global_report_prompts (chapter_key,prompt,updated_at,structured_json,legacy_prompt,migration_mode) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(chapter_key) DO UPDATE SET prompt=excluded.prompt,updated_at=excluded.updated_at,structured_json=excluded.structured_json,legacy_prompt=excluded.legacy_prompt,migration_mode=excluded.migration_mode",
            (chapter_key, compiled["prompt"], now, compiled["structured_json"],
             compiled["legacy_prompt"], compiled["migration_mode"]),
        )
    audit_event(project_id, "report_prompt_global_update", "report_prompt", chapter_key, before,
                {"prompt": compiled["prompt"], "scope": "global",
                 "config": payload.config.model_dump(mode="json") if payload.config else None}, "human")
    return {"chapter_key": chapter_key, **_prompt_row_state(project_id, chapter_key, permissions)}


@app.delete("/api/projects/{project_id}/report-prompts/global/{chapter_key}")
def reset_global_report_prompt(project_id: str, chapter_key: str, request: Request,
                               x_project_token: Optional[str] = Header(default=None),
                               expected_config_sha256: Optional[str] = Query(default=None)) -> dict:
    """重置**全局默认** Prompt：删除全局覆盖，回落 YAML 出厂配置（P0-1）。

    只删 `global_report_prompts` 那一行；**项目覆盖原样保留**，因此有项目覆盖的项目
    在重置后仍然使用项目配置。幂等：没有全局覆盖时同样返回成功（已是出厂状态）。
    """
    _ensure_project_writable(_authorize(project_id, x_project_token))
    _prompt_write_guards(chapter_key)
    permissions = _require_global_prompt_editable(project_id, request)
    before = row("SELECT * FROM global_report_prompts WHERE chapter_key=?", (chapter_key,))
    _check_prompt_concurrency(
        expected_config_sha256,
        _prompt_config_sha256((before or {}).get("prompt") or "", (before or {}).get("structured_json") or ""),
        "全局 Prompt 配置",
    )
    execute("DELETE FROM global_report_prompts WHERE chapter_key=?", (chapter_key,))
    audit_event(project_id, "report_prompt_global_reset", "report_prompt", chapter_key, before,
                {"scope": "global"}, "human")
    return {"chapter_key": chapter_key, **_prompt_row_state(project_id, chapter_key, permissions)}


# ------------------------------------------------ P1-1：报告版本化目录（report-outline）--

def _outline_scope_label(scope: str) -> str:
    return "全局报告目录" if scope == "global" else "项目报告目录"


def _outline_concurrency_conflict(scope: str, expected_version: Optional[str], expected_sha: Optional[str],
                                  before: dict | None) -> None:
    """乐观并发：请求带的版本/哈希与库中现状不一致 → 409。

    与 P0-A ``_check_prompt_concurrency`` 同口径：只对**非空**预期值生效，且现状为空
    （该 scope 尚无覆盖）时同样拦截——那说明别人刚在你之前保存过，或你以为在改一份并
    不存在的覆盖。两值都传则都严格比对。
    """
    current_version = str((before or {}).get("outline_version") or "")
    current_sha = payload_sha256((before or {}).get("payload_json") or "") if before else ""
    if str(expected_version or "").strip() and str(expected_version) != current_version:
        raise HTTPException(409, f"{_outline_scope_label(scope)}已被其他会话修改，请重新加载后再保存")
    if str(expected_sha or "").strip() and str(expected_sha) != current_sha:
        raise HTTPException(409, f"{_outline_scope_label(scope)}已被其他会话修改，请重新加载后再保存")


def _report_outline_view(project_id: str, scope: str, permissions: dict) -> dict:
    """GET/PUT/DELETE 的统一响应体（spec §四）。"""
    state = outline_scope_state(scope, project_id)
    return {
        "scope": scope,
        "outline": state["outline"],
        "source": state["source"],
        "outline_version": state["outline_version"],
        "config_sha256": state["config_sha256"],
        # 项目链生效来源与项目覆盖存在性（scope=global 时同样返回，供前端统一渲染）。
        "effective_source": outline_effective_source(project_id),
        "has_project_override": has_project_override(project_id),
        **_prompt_permission_fields(permissions),
    }


@app.get("/api/projects/{project_id}/report-outline")
def get_report_outline(project_id: str, request: Request,
                       x_project_token: Optional[str] = Header(default=None),
                       scope: str = Query(default="project", pattern="^(project|global)$")) -> dict:
    """读取报告目录（项目链或全局链）。GET 一律可读，权限字段仅作展示。"""
    _authorize(project_id, x_project_token)
    permissions = prompt_edit_permissions(project_id, request)
    return _report_outline_view(project_id, scope, permissions)


@app.put("/api/projects/{project_id}/report-outline")
def update_report_outline(project_id: str, payload: ReportOutlineUpdate, request: Request,
                          x_project_token: Optional[str] = Header(default=None),
                          scope: str = Query(default="project", pattern="^(project|global)$")) -> dict:
    """保存报告目录：校验（422）→ 并发（409）→ UPSERT + 审计。响应 = 保存后的 GET 同构状态。

    **不调用模型、不修改报告正文、不删除项目其他配置**（spec §四/§五）。
    """
    _ensure_project_writable(_authorize(project_id, x_project_token))
    if scope == "global":
        permissions = _require_global_prompt_editable(project_id, request)
        identity = ""
    else:
        permissions = _require_prompt_editable(project_id, request)
        identity = project_id
    before = row("SELECT * FROM report_outlines WHERE scope=? AND project_id=?", (scope, identity))
    try:
        validate_outline(payload.outline)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    _outline_concurrency_conflict(scope, payload.expected_outline_version, payload.expected_outline_sha256, before)
    save_outline(scope, project_id, payload.outline)
    return _report_outline_view(project_id, scope, permissions)


@app.delete("/api/projects/{project_id}/report-outline")
def reset_report_outline(project_id: str, request: Request,
                         x_project_token: Optional[str] = Header(default=None),
                         scope: str = Query(default="project", pattern="^(project|global)$"),
                         expected_outline_version: Optional[str] = Query(default=None),
                         expected_outline_sha256: Optional[str] = Query(default=None)) -> dict:
    """重置报告目录（幂等）：删该 scope 覆盖行 + 审计。项目 scope 删除后回落全局/内置。"""
    _ensure_project_writable(_authorize(project_id, x_project_token))
    if scope == "global":
        permissions = _require_global_prompt_editable(project_id, request)
        identity = ""
    else:
        permissions = _require_prompt_editable(project_id, request)
        identity = project_id
    before = row("SELECT * FROM report_outlines WHERE scope=? AND project_id=?", (scope, identity))
    _outline_concurrency_conflict(scope, expected_outline_version, expected_outline_sha256, before)
    delete_outline(scope, project_id)
    return _report_outline_view(project_id, scope, permissions)


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


def _regenerate_concurrency_conflict(project_id: str, section_key: str, expected_revision: int,
                                     expected_content_sha256: str, expected_prompt_sha256: str,
                                     expected_materials_version: str) -> str:
    """写库前的并发校验（修订稿 §四）。返回空串表示无冲突，否则返回中文冲突原因。

    比较报告版本、章节正文哈希、章节 Prompt 哈希与材料版本；任一与预期不一致即视为并发
    修改，调用方据此返回 409，不覆盖并发修改、保留本次尝试诊断。
    """
    state = report_state(project_id)
    if int(state.get("report_revision") or 0) != int(expected_revision or 0):
        return "报告版本已变化"
    current = row(
        "SELECT content,content_sha256 FROM report_sections WHERE project_id=? AND section_key=?",
        (project_id, section_key),
    )
    if not current:
        return "章节已不存在"
    current_sha = str(current.get("content_sha256") or content_sha256(current.get("content") or ""))
    if expected_content_sha256 and current_sha != expected_content_sha256:
        return "章节正文已被修改"
    if expected_prompt_sha256:
        fresh_prompt = hashlib.sha256(chapter_prompt(section_key, project_id).encode("utf-8")).hexdigest()
        if fresh_prompt != expected_prompt_sha256:
            return "章节 Prompt 已变化"
    if expected_materials_version and _materials_fingerprint(project_id) != expected_materials_version:
        return "材料版本已变化"
    return ""


@app.post("/api/projects/{project_id}/report/sections/{section_key}/regenerate")
def regenerate_section(project_id: str, section_key: str, x_project_token: Optional[str] = Header(default=None),
                       payload: SectionRegenerateRequest | None = None) -> dict:
    _ensure_project_writable(_authorize(project_id, x_project_token))
    section = row("SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, section_key))
    if not section:
        raise HTTPException(404, "报告章节不存在")
    # P1-1：单章再生只覆盖"该章的小节配置"。若目录的**二级结构**（章节集合/顺序）已
    # 相对"报告采用的目录"变化，单章再生无法让报告与目录一致——必须整份重新生成。
    # 旧报告无 adoption 记录时优先历史快照；两者皆无（unknown）不拦，保持旧行为。
    from .report_outline_adoption import regeneration_status

    outline_status = regeneration_status(project_id)
    if outline_status["regeneration_requirement"] == "full_report":
        raise HTTPException(
            422,
            "报告目录的二级结构已变更（章节增删或排序），单章再生无法使报告与目录一致，"
            "请整份重新生成（full_report_regeneration_required）",
        )
    stored_citations = json_load(section.get("citation_ids") or "[]")
    # 起始版本：新前端随请求体发送预期版本与正文哈希；旧调用省略请求体，由服务端捕获起始值。
    start_revision = int(report_state(project_id).get("report_revision") or 0)
    start_content_sha256 = str(section.get("content_sha256") or content_sha256(section.get("content") or ""))
    expected_revision = (
        payload.expected_revision if payload and payload.expected_revision is not None else start_revision
    )
    expected_content_sha256 = (
        payload.expected_content_sha256 if payload and payload.expected_content_sha256
        else start_content_sha256
    )
    # Rebuild from the frozen deterministic draft. A previous model response may
    # contain rejected numbers or wording and must not become the next prompt's
    # factual boundary.
    from .bond_report import generate_bond_chapters

    current_chapter = next(
        (item for item in generate_bond_chapters(project_id, False) if item.key == section_key),
        None,
    )
    deterministic_draft = str(section.get("deterministic_draft") or "").strip()
    if current_chapter is not None:
        draft_seed = (
            current_chapter.content if current_chapter.content.strip()
            else deterministic_draft or str(section.get("content") or "")
        )
        source_ids = current_chapter.source_ids
    else:
        # 自定义章节（P1-4）：无确定性模板草稿——以空草稿全新撰写；旧模型正文不得
        # 成为下一次生成的边界（与修订稿"previous model response must not become the
        # next prompt's factual boundary"同一原则）。
        draft_seed = ""
        source_ids = []
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
    fallback_stage = "none"
    usage = None
    repair_attempted = False
    attempt_id = ""
    expected = {
        "expected_revision": expected_revision,
        "expected_content_sha256": expected_content_sha256,
        "expected_prompt_sha256": chapter_context.get("prompt_sha256", ""),
        "expected_materials_version": chapter_context.get("materials_version", ""),
    }
    generation_status = "deterministic" if not chapter_uses_llm(section_key) else "model_passed"
    if not chapter_uses_llm(section_key):
        fallback_reason = NO_MODEL_REASON
        validation = {"status": "skipped", "reasons": [fallback_reason]}
    else:
        # 与完整生成共用同一"生成 → 章节校验 → 一次修复（安全优先）→ 全部重检"流程
        # （修订稿 §3.3）：单章入口不再只校验不执行完整判定。安全被拒 / 调用失败都在
        # `run_chapter_model_attempt` 内一次性处理，每章最多一次额外模型调用。
        attempt = run_chapter_model_attempt(
            section["title"], content, evidence,
            chapter_key=section_key, chapter_context=chapter_context,
            call_context=LlmCallContext(project_id, "", "draft", "report"),
            expected=expected,
        )
        usage = attempt["usage"]
        validation = attempt["validation"]
        fallback_reason = attempt["fallback_reason"]
        fallback_stage = attempt["fallback_stage"]
        repair_attempted = attempt["repair_attempted"]
        generation_status = attempt["generation_status"]
        attempt_id = attempt.get("attempt_id") or ""
        # model_needs_review：修复后仅剩未决项，正文仍是模型稿，同样要采用（计划 §四 line 38）。
        if is_model_content_status(generation_status):
            content = attempt["content"]
            citations = list(attempt["citation_ids"])
    if generation_status == "fallback":
        # 模型未产出可用正文（安全被拒且一次修复仍未通过 / 模型调用失败）→ **不写入新正文**，
        # 保留已有正文为旧版本，记录本次失败并返回失败响应（修订稿 §3.3）。形态与下方后置
        # 支持失败分支一致，失败现场不会因本次再生而丢失。
        failure = fallback_reason or "章节模型未能产出可用正文"
        execute(
            "UPDATE report_sections SET validation_status=?, fallback_reason=?, fallback_stage=?,"
            " validation_diagnostics=? WHERE project_id=? AND section_key=?",
            ("failed", failure, fallback_stage or "chapter_validation",
             json.dumps(chapter_diagnostics(validation), ensure_ascii=False), project_id, section_key),
        )
        if attempt_id:
            set_section_attempt(project_id, section_key, attempt_id, adopted=False)
        audit_event(
            project_id, "report_section_regenerate_failed", "report_section", section_key, section,
            {"failure": failure, "fallback_stage": fallback_stage, "validation": validation,
             "usage": usage.__dict__ if usage else None}, "human",
        )
        return {
            "content": str(section.get("content") or ""), "citationIds": stored_citations,
            "validation": {"status": "failed", "reasons": [failure]},
            "fallbackReason": failure, "generationStatus": "failed",
            "contentOrigin": str(section.get("content_origin") or ""),
            "support": {},
            "latestAttempt": latest_attempt(project_id, section_key),
            "usage": usage.__dict__ if usage else None,
        }
    if not citations:
        citations = list(allowed) or stored_citations
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
        # 模型尝试本身通过、但后置支持不通过 → 正文未落库，本次尝试不算采用。
        if attempt_id:
            set_section_attempt(project_id, section_key, attempt_id, adopted=False)
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
            "latestAttempt": latest_attempt(project_id, section_key),
            "usage": usage.__dict__ if usage else None,
        }
    # 写库前的并发校验（修订稿 §四）：报告版本、正文哈希、Prompt/材料版本任一变化即返回 409，
    # 保留本次诊断（尝试已落台账）、不覆盖并发修改。
    conflict = _regenerate_concurrency_conflict(
        project_id, section_key, expected_revision, expected_content_sha256,
        expected.get("expected_prompt_sha256", ""), expected.get("expected_materials_version", ""),
    )
    if conflict:
        if attempt_id:
            set_section_attempt(project_id, section_key, attempt_id, adopted=False)
        audit_event(
            project_id, "report_section_regenerate_conflict", "report_section", section_key, section,
            {"conflict": conflict, "expected_revision": expected_revision,
             "expected_content_sha256": expected_content_sha256}, "human",
        )
        raise HTTPException(409, f"{conflict}，请刷新报告后重试")
    # 单章重新生成同样走统一写入入口：来源/状态/阶段/篇幅/哈希/冻结披露一次对齐。
    write_report_section(
        project_id=project_id, section_key=section_key, title=section["title"],
        content=content, citation_ids=citations,
        allowed_source_ids=sorted({item["source_id"] for item in chapter_context.get("evidence", [])}),
        content_origin="model" if is_model_content_status(generation_status) else "deterministic",
        generation_status=generation_status, fallback_reason=fallback_reason,
        fallback_stage=fallback_stage,
        # 系统表格块必须与生成路径一致地冻结。漏了它会有两个后果：正文里的表格
        # 被当成模型事实主张去要证据（支持率掉成 warning），并且「表格逐字保留」
        # 的检查失效。实测：单章重生成后 rating 章的 support 变成 warning。
        required_disclosures=[block for block in (
            chapter_context.get("conflict_block"), chapter_context.get("system_table_block"),
            chapter_context.get("source_attribution_block"),
        ) if block],
        deterministic_draft=chapter_context.get("draft") or draft_seed,
        table_records=chapter_context.get("table_records") or [],
        # 统一冻结记录（fact/table/metric/rule）：单章再生成与完整生成同口径冻结。
        evidence_records=chapter_context.get("evidence_records") or None,
        table_blocks=chapter_context.get("table_blocks") or None,
        length_policy=chapter_context.get("length_policy") or {},
        diagnostics=chapter_diagnostics(validation),
        prompt_version=chapter_context.get("prompt_version", ""),
        prompt_sha256=chapter_context.get("prompt_sha256", ""),
        input_hash=chapter_context.get("input_hash", ""),
        # 与完整生成同口径（计划 §四 line 38）：仅剩未决项的模型稿支持状态记 warning。
        support_status="warning" if generation_status == "model_needs_review" else "pending",
    )
    # 正文真正落库后，本次尝试才算采用（accepted=latest=本次）。
    if attempt_id:
        set_section_attempt(project_id, section_key, attempt_id, adopted=True)
    # P1-1：单章再生成功后，刷新**该章**在"报告采用目录"里的标题与小节条目；
    # 结构已变化（返回 False）时不更新——那时本来就走不到这里（入口已 422）。
    from .report_outline_adoption import update_chapter_adoption

    update_chapter_adoption(project_id, section_key)
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
            "fallbackReason": fallback_reason,
            "latestAttempt": latest_attempt(project_id, section_key),
            "usage": usage.__dict__ if usage else None}


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
    from .exports import ExportWordingError

    try:
        if format == "md":
            body = markdown_report(project_id, variant)
        elif format == "docx":
            body = docx_report(project_id, variant)
        else:
            raise HTTPException(400, "仅支持 md 或 docx")
    except ExportWordingError as exc:
        raise HTTPException(409, str(exc)) from exc
    if format == "md":
        return Response(body, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}{suffix}.md", **warning_headers})
    return Response(body, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}{suffix}.docx", **warning_headers})


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
