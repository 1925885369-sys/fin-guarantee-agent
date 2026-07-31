from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .database import connect, execute, init_db, json_dump, json_load, now_iso, row, rows
from .exports import docx_report, markdown_report
from .facts import FACT_REGISTRY, decode_facts, material_inventory
from .llm import is_configured, rewrite_section
from .parsers import SUPPORTED, classify_document, parse_document, sha256
from .portfolio_cases import generate_portfolio_materials, is_demo_project, seed_portfolio_cases
from .sample_case import DEMO_PROJECT_ID, generate_demo_materials, seed_demo
from .schemas import FactUpdate, ProjectCreate, RunCreate, SectionUpdate
from .security import check_rate, cleanup_expired_projects, validate_magic
from .workflow import TERMINAL, _numbers_supported, retry_run, start_run


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        cleanup_expired_projects()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    cleanup_expired_projects()
    seed_demo()
    seed_portfolio_cases()
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()


app = FastAPI(title="研担 · 融资担保尽调 Agent", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def startup() -> None:
    init_db()
    cleanup_expired_projects()
    seed_demo()
    seed_portfolio_cases()


def _project(project_id: str) -> dict:
    item = row("SELECT * FROM projects WHERE id=?", (project_id,))
    if not item:
        raise HTTPException(404, "项目不存在")
    return item


def _authorize(project_id: str, token: Optional[str]) -> dict:
    item = _project(project_id)
    if not is_demo_project(project_id) and not secrets.compare_digest(token or "", item["access_token"]):
        raise HTTPException(403, "项目访问令牌无效")
    return item


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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "modelConfigured": is_configured(), "model": settings.llm_model, "version": "0.2.0"}


@app.get("/api/projects")
def list_projects() -> list[dict]:
    return rows("SELECT id,company_name,amount,term_months,purpose,status,created_at,updated_at FROM projects WHERE id LIKE 'demo-%' ORDER BY created_at,id")


@app.get("/api/projects/{project_id}")
def get_project(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    item = _authorize(project_id, x_project_token)
    item.pop("access_token", None)
    item["stats"] = {
        "documents": row("SELECT COUNT(*) count FROM documents WHERE project_id=?", (project_id,))["count"],
        "facts": row("SELECT COUNT(*) count FROM facts WHERE project_id=?", (project_id,))["count"],
        "checks": row("SELECT COUNT(*) count FROM cross_checks WHERE project_id=?", (project_id,))["count"],
        "risks": row("SELECT COUNT(*) count FROM risks WHERE project_id=?", (project_id,))["count"],
        "highRisks": row("SELECT COUNT(*) count FROM risks WHERE project_id=? AND severity='high'", (project_id,))["count"],
    }
    return item


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate, request: Request) -> dict:
    if not payload.data_consent:
        raise HTTPException(400, "请确认仅上传公开或虚构材料，并同意模型处理")
    check_rate(request.client.host if request.client else "unknown", "project", 5)
    project_id = str(uuid.uuid4())
    token = secrets.token_urlsafe(24)
    now = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (id,access_token,company_name,amount,term_months,purpose,status,created_at,updated_at,consent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (project_id, token, payload.company_name, payload.amount, payload.term_months,
             payload.purpose, "draft", now, now, now),
        )
    return {"id": project_id, "accessToken": token}


@app.get("/api/projects/{project_id}/document-inventory")
def inventory(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    return material_inventory(project_id)


@app.post("/api/projects/{project_id}/documents", status_code=201)
async def upload_document(
    project_id: str, request: Request, file: UploadFile = File(...),
    x_project_token: Optional[str] = Header(default=None),
) -> dict:
    _authorize(project_id, x_project_token)
    check_rate(request.client.host if request.client else "unknown", "upload", 20)
    count = row("SELECT COUNT(*) count FROM documents WHERE project_id=?", (project_id,))["count"]
    if count >= settings.max_files:
        raise HTTPException(400, f"每个项目最多上传 {settings.max_files} 个文件")
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
    safe_stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", original.stem)[:60] or "document"
    document_id = str(uuid.uuid4())
    directory = settings.resolved_data_dir / "uploads" / project_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{document_id}_{safe_stem}{suffix}"
    path.write_bytes(content)
    parse_status, quality, warning, fragments = "parsed", 0.0, "", []
    try:
        parsed = parse_document(path)
        quality, warning, fragments = parsed.quality, parsed.warning, parsed.fragments
        if warning and quality < 0.25:
            parse_status = "warning"
        existing_chars = row("SELECT COALESCE(SUM(LENGTH(text)),0) total FROM sources WHERE project_id=?", (project_id,))["total"]
        if existing_chars + sum(len(item.text) for item in fragments) > settings.max_total_chars:
            raise ValueError("项目材料文字总量超过处理限制")
    except ValueError as exc:
        parse_status, warning, fragments = "failed", str(exc), []
    kind = classify_document(original.name, " ".join(fragment.text for fragment in fragments[:8]))
    media_type = file.content_type or mimetypes.guess_type(original.name)[0] or "application/octet-stream"
    with connect() as conn:
        conn.execute(
            "INSERT INTO documents (id,project_id,filename,kind,media_type,sha256,size,parse_status,quality,locator_count,warning,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, project_id, original.name, kind, media_type, sha256(path), len(content),
             parse_status, quality, len(fragments), warning, now_iso()),
        )
        for fragment in fragments:
            conn.execute("INSERT INTO sources (id,project_id,document_id,locator_type,locator,text) VALUES (?,?,?,?,?,?)",
                         (str(uuid.uuid4()), project_id, document_id, fragment.locator_type, fragment.locator, fragment.text))
    return {"id": document_id, "kind": kind, "parseStatus": parse_status, "quality": quality, "warning": warning}


@app.get("/api/demo/materials/{document_id}")
def download_demo_material(document_id: str) -> FileResponse:
    document = row("SELECT * FROM documents WHERE id=? AND project_id LIKE 'demo-%'", (document_id,))
    if not document:
        raise HTTPException(404, "演示材料不存在")
    material = next((item for item in generate_demo_materials() + generate_portfolio_materials() if item["id"] == document_id), None)
    if not material:
        raise HTTPException(404, "演示材料不存在")
    return FileResponse(material["path"], filename=document["filename"], media_type=document["media_type"])


@app.post("/api/projects/{project_id}/runs", status_code=202)
def create_run(project_id: str, payload: RunCreate, request: Request, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    check_rate(request.client.host if request.client else "unknown", "run", 3)
    active = row("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running')", (project_id,))
    if active:
        return {"id": active["id"], "status": "running"}
    return {"id": start_run(project_id), "status": "queued", "focus": payload.focus}


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


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize_run(run_id, x_project_token)
    execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
    return {"status": "cancelling"}


@app.post("/api/runs/{run_id}/retry", status_code=202)
def retry(run_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize_run(run_id, x_project_token)
    try:
        new_id = retry_run(run_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"id": new_id, "status": "queued"}


@app.get("/api/projects/{project_id}/facts")
def facts(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return decode_facts(rows("SELECT * FROM facts WHERE project_id=? ORDER BY category,name,period", (project_id,)))


@app.patch("/api/projects/{project_id}/facts/{fact_id}")
def update_fact(project_id: str, fact_id: str, payload: FactUpdate, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    fact = row("SELECT * FROM facts WHERE id=? AND project_id=?", (fact_id, project_id))
    if not fact:
        raise HTTPException(404, "事实不存在")
    execute(
        "UPDATE facts SET period=?,value=?,unit=?,text_value=?,raw_value=?,confidence=1,extraction_method='human',review_status='human_verified' WHERE id=?",
        (payload.period, payload.value, payload.unit, payload.text_value,
         payload.text_value or (str(payload.value) if payload.value is not None else ""), fact_id),
    )
    return {"status": "human_verified"}


@app.get("/api/projects/{project_id}/cross-checks")
def cross_checks(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return _decode(rows("SELECT * FROM cross_checks WHERE project_id=? ORDER BY CASE status WHEN 'warning' THEN 1 WHEN 'passed' THEN 2 ELSE 3 END, CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END", (project_id,)), ("values_json", "source_ids", "fact_ids"))


@app.get("/api/projects/{project_id}/risks")
def risks(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return _decode(rows("SELECT * FROM risks WHERE project_id=? ORDER BY CASE severity WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END", (project_id,)), ("source_ids",))


@app.get("/api/projects/{project_id}/report")
def report(project_id: str, x_project_token: Optional[str] = Header(default=None)) -> list[dict]:
    _authorize(project_id, x_project_token)
    return _decode(rows("SELECT * FROM report_sections WHERE project_id=? ORDER BY rowid", (project_id,)), ("citation_ids",))


@app.patch("/api/projects/{project_id}/report/sections/{section_key}")
def update_section(project_id: str, section_key: str, payload: SectionUpdate, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    execute("UPDATE report_sections SET content=?,review_status=?,updated_at=? WHERE project_id=? AND section_key=?",
            (payload.content, payload.review_status, now_iso(), project_id, section_key))
    return {"status": "updated"}


@app.post("/api/projects/{project_id}/report/sections/{section_key}/regenerate")
def regenerate_section(project_id: str, section_key: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    _authorize(project_id, x_project_token)
    section = row("SELECT * FROM report_sections WHERE project_id=? AND section_key=?", (project_id, section_key))
    if not section:
        raise HTTPException(404, "报告章节不存在")
    evidence = [item for citation_id in json_load(section["citation_ids"])
                if (item := row("SELECT id,quote FROM citations WHERE id=?", (citation_id,)))]
    try:
        result, usage = rewrite_section(section["title"], section["content"], evidence)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    allowed = {item["id"] for item in evidence}
    citations = [citation_id for citation_id in result.citation_ids if citation_id in allowed]
    content = result.content if (not evidence or citations) and _numbers_supported(result.content, section["content"], evidence) else section["content"]
    execute("UPDATE report_sections SET content=?,citation_ids=?,review_status='draft',updated_at=? WHERE id=?",
            (content, json_dump(citations or list(allowed)), now_iso(), section["id"]))
    return {"content": content, "citationIds": citations or list(allowed), "usage": usage.__dict__}


@app.get("/api/citations/{citation_id}")
def citation(citation_id: str, x_project_token: Optional[str] = Header(default=None)) -> dict:
    item = row("SELECT * FROM citations WHERE id=?", (citation_id,))
    if not item:
        raise HTTPException(404, "引用不存在")
    _authorize(item["project_id"], x_project_token)
    return item


@app.get("/api/projects/{project_id}/export")
def export(project_id: str, format: str = "md", x_project_token: Optional[str] = Header(default=None)) -> Response:
    project = _authorize(project_id, x_project_token)
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", project["company_name"])
    encoded_name = quote(safe_name)
    if format == "md":
        return Response(markdown_report(project_id), media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}.md"})
    if format == "docx":
        return Response(docx_report(project_id), media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}.docx"})
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
