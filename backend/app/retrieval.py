from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import re
import threading
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterator

import jieba
from rank_bm25 import BM25Okapi

from .config import settings
from .database import connect, json_load, now_iso, row, rows


MODEL_ID = "BAAI/bge-base-zh-v1.5"
INDEX_VERSION = "hybrid-rag-v1"
RRF_K = 60
DEFAULT_TOP_K = 30
DENSE_MIN_SIMILARITY = 0.20
FALLBACK_CHUNK_CHARS = 700
FALLBACK_OVERLAP_CHARS = 100
MODEL_MANIFEST = "retrieval-model.json"
TOKENIZER_BOUNDARIES = re.compile(r"[。！？!?；;\n]")
ALIASES: dict[str, tuple[str, ...]] = {
    "还债": ("偿债", "还本付息", "兑付"),
    "偿债": ("还债", "还本付息", "兑付", "偿付"),
    "还款": ("偿债", "兑付", "偿付"),
    "还本": ("本金兑付", "到期本金", "本金偿还"),
    "现金够不够": ("货币资金", "可用现金", "流动性", "短期偿债"),
    "现金流": ("经营活动现金流", "经营现金流", "偿债来源"),
    "欠款": ("有息负债", "债务", "应付账款"),
    "借款": ("有息负债", "短期借款", "长期借款", "债务"),
    "担保": ("保证责任", "保证范围", "反担保", "增信"),
    "抵押": ("反担保", "抵押物", "权属", "登记"),
    "钱怎么用": ("募集资金用途", "募集资金运用", "专户"),
    "资金用途": ("募集资金用途", "募集资金运用", "专户"),
}

_encoder = None
_encoder_lock = threading.Lock()
_encoder_error = ""
_project_locks: dict[str, threading.Lock] = {}
_project_locks_guard = threading.Lock()


def tokenize(text: str) -> list[str]:
    return [token.strip().lower() for token in jieba.cut_for_search(text) if token.strip()]


def retrieval_signature() -> str:
    manifest = _model_manifest()
    payload = {
        "index_version": INDEX_VERSION,
        "model_id": MODEL_ID,
        "model_revision": str(manifest.get("revision") or "missing"),
        "max_tokens": settings.retrieval_chunk_max_tokens,
        "overlap_tokens": settings.retrieval_chunk_overlap_tokens,
        "query_prefix": settings.retrieval_query_prefix,
        "dimension": 768,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _model_manifest() -> dict:
    path = settings.resolved_retrieval_model_dir / MODEL_MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if manifest.get("model_id") != MODEL_ID or not manifest.get("revision"):
        return {}
    return manifest


def _model_files_ready() -> bool:
    model_dir = settings.resolved_retrieval_model_dir
    return bool(
        _model_manifest()
        and (model_dir / "config.json").is_file()
        and (model_dir / "modules.json").is_file()
        and (model_dir / "1_Pooling" / "config.json").is_file()
    )


def retrieval_status() -> dict:
    dependencies_ready = all(
        importlib.util.find_spec(name) is not None
        for name in ("sentence_transformers", "numpy")
    )
    model_files_ready = _model_files_ready()
    model_ready = model_files_ready and dependencies_ready
    error_type = _encoder_error
    if not model_files_ready:
        error_type = "model_not_prepared"
    elif not dependencies_ready:
        error_type = "dependencies_missing"
    return {
        "mode": "hybrid" if model_ready and not _encoder_error else "bm25_only",
        "model": MODEL_ID,
        "modelRevision": str(_model_manifest().get("revision") or ""),
        "modelReady": model_ready,
        "dependenciesReady": dependencies_ready,
        "indexVersion": INDEX_VERSION,
        "errorType": error_type,
    }


def _load_encoder():
    global _encoder, _encoder_error
    if _encoder is not None:
        return _encoder
    if _encoder_error:
        return None
    with _encoder_lock:
        if _encoder is not None:
            return _encoder
        if _encoder_error:
            return None
        try:
            from sentence_transformers import SentenceTransformer

            if not _model_files_ready():
                raise FileNotFoundError("Retrieval model has not been prepared")
            _encoder = SentenceTransformer(
                str(settings.resolved_retrieval_model_dir),
                device="cpu",
                local_files_only=True,
            )
        except Exception as exc:
            _encoder_error = type(exc).__name__
            return None
    return _encoder


def _token_counter(encoder) -> Callable[[str], int]:
    tokenizer = encoder.tokenizer

    def count(text: str) -> int:
        # Split candidates can exceed the model limit; count them without truncating.
        return len(tokenizer.encode(text, add_special_tokens=False, truncation=False, verbose=False))

    return count


def _split_spans(text: str, heading: str, count_tokens: Callable[[str], int] | None) -> list[tuple[int, int]]:
    if not text.strip():
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    if count_tokens is None:
        max_size, overlap = FALLBACK_CHUNK_CHARS, FALLBACK_OVERLAP_CHARS
        while start < len(text):
            end = min(start + max_size, len(text))
            if end < len(text):
                boundary = max(
                    (match.end() for match in TOKENIZER_BOUNDARIES.finditer(text, start, end)),
                    default=0,
                )
                if boundary >= start + max_size // 2:
                    end = boundary
            spans.append((start, end))
            if end >= len(text):
                break
            start = max(start + 1, end - overlap)
        return spans

    maximum = max(64, settings.retrieval_chunk_max_tokens)
    overlap = max(0, min(settings.retrieval_chunk_overlap_tokens, maximum // 2))

    def size(left: int, right: int) -> int:
        body = text[left:right]
        return count_tokens(f"{heading}\n{body}" if heading else body)

    while start < len(text):
        low, high = start + 1, len(text)
        best = start
        while low <= high:
            middle = (low + high) // 2
            if size(start, middle) <= maximum:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best <= start:
            raise ValueError("Embedding tokenizer cannot fit a single source character")
        end = best
        if end < len(text):
            boundary = max(
                (match.end() for match in TOKENIZER_BOUNDARIES.finditer(text, start, end)),
                default=0,
            )
            if boundary >= start + (end - start) * 0.55:
                end = boundary
        spans.append((start, end))
        if end >= len(text):
            break
        if overlap:
            low, high, overlap_start = start + 1, end, end
            while low <= high:
                middle = (low + high) // 2
                if size(middle, end) <= overlap:
                    overlap_start = middle
                    high = middle - 1
                else:
                    low = middle + 1
            start = max(start + 1, overlap_start)
        else:
            start = end
    return spans


def _section_map(source_rows: list[dict]) -> dict[str, tuple[str, str]]:
    by_document: dict[str, list[dict]] = {}
    for item in source_rows:
        by_document.setdefault(str(item["document_id"]), []).append(item)
    project_id = str(source_rows[0].get("project_id") or "") if source_rows else ""
    section_rows = rows(
        "SELECT document_id,section_role,title,start_source_id,end_source_id,status "
        "FROM document_sections WHERE project_id=? AND status='located' ORDER BY document_id,section_role",
        (project_id,),
    ) if project_id else []
    sections_by_document: dict[str, list[dict]] = {}
    for section in section_rows:
        sections_by_document.setdefault(str(section["document_id"]), []).append(section)
    output: dict[str, tuple[str, str]] = {}
    for document_id, sources in by_document.items():
        ordered = sorted(sources, key=lambda item: (int(item.get("page") or 0), str(item["id"])))
        positions = {str(item["id"]): index for index, item in enumerate(ordered)}
        for section in sections_by_document.get(document_id, []):
            start = positions.get(str(section.get("start_source_id") or ""))
            end = positions.get(str(section.get("end_source_id") or ""))
            if start is None:
                continue
            if end is None or end < start:
                end = start
            for source in ordered[start:end + 1]:
                output.setdefault(
                    str(source["id"]),
                    (str(section.get("section_role") or ""), str(section.get("title") or "")),
                )
    return output


def _load_project_sources(project_id: str) -> list[dict]:
    return rows(
        "SELECT s.id,s.project_id,s.document_id,s.locator_type,s.locator,s.text,s.page,s.content_hash,"
        "s.origin_type,s.snapshot_id,s.profile_id,s.profile_version,s.entity_role,"
        "d.filename,d.kind,d.document_role,d.sha256,d.parse_status "
        "FROM analysis_sources s JOIN analysis_documents d "
        "ON d.id=s.document_id AND d.project_id=s.project_id "
        "WHERE s.project_id=? AND d.parse_status IN ('parsed','warning') "
        "ORDER BY d.filename,s.page,s.id",
        (project_id,),
    )


def _source_chunks(source: dict, heading: str, section_role: str, count_tokens) -> list[dict]:
    text = str(source.get("text") or "")
    output = []
    for start, end in _split_spans(text, heading, count_tokens):
        quote = text[start:end].strip()
        if not quote:
            continue
        chunk_id = hashlib.sha256(
            f"{source['id']}|text|{start}|{end}|{hashlib.sha256(quote.encode('utf-8')).hexdigest()}".encode("utf-8")
        ).hexdigest()[:32]
        output.append({
            "chunk_id": chunk_id,
            "source_id": str(source["id"]),
            "document_id": str(source["document_id"]),
            "page": source.get("page"),
            "kind": str(source.get("kind") or ""),
            "document_role": str(source.get("document_role") or ""),
            "section_role": section_role,
            "filename": str(source.get("filename") or ""),
            "heading": heading,
            "chunk_type": "text",
            "table_key": "",
            "start_offset": start,
            "end_offset": end,
            "content_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            "text": quote,
        })
    return output


def _project_fingerprint(project_id: str, source_rows: list[dict], table_rows: list[dict]) -> str:
    snapshot = row(
        "SELECT s.id,s.version,s.content_hash FROM projects p "
        "LEFT JOIN project_profile_snapshots s ON s.id=p.profile_snapshot_id WHERE p.id=?",
        (project_id,),
    ) or {}
    digest = hashlib.sha256()
    digest.update(retrieval_signature().encode("ascii"))
    digest.update(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    for source in source_rows:
        digest.update(json.dumps([
            source.get("id"), source.get("document_id"), source.get("page"), source.get("kind"),
            source.get("document_role"), source.get("origin_type"), source.get("snapshot_id"),
            source.get("profile_version"), hashlib.sha256(str(source.get("text") or "").encode("utf-8")).hexdigest(),
        ], ensure_ascii=False, default=str).encode("utf-8"))
    for table in table_rows:
        digest.update(json.dumps(table, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def _load_table_rows(project_id: str) -> list[dict]:
    return rows(
        "SELECT id,document_id,table_key,caption,unit,currency,scope,start_page,headers_json,rows_json,notes_json "
        "FROM document_tables WHERE project_id=? ORDER BY document_id,start_page,id",
        (project_id,),
    )


@contextlib.contextmanager
def _project_lock(project_id: str) -> Iterator[None]:
    key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()
    with _project_locks_guard:
        lock = _project_locks.setdefault(key, threading.Lock())
    with lock:
        lock_dir = settings.resolved_data_dir / "retrieval" / key
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / "index.lock").open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _safe_project_dir(project_id: str) -> tuple[Path, str]:
    key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:32]
    base = settings.resolved_data_dir / "retrieval" / key
    base.mkdir(parents=True, exist_ok=True)
    return base, key


def delete_project_index(project_id: str) -> None:
    key = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:32]
    base = settings.resolved_data_dir / "retrieval" / key
    if base.exists() and not base.is_symlink():
        import shutil

        shutil.rmtree(base)


def _current_index(project_id: str) -> dict | None:
    meta = row("SELECT * FROM retrieval_indexes WHERE project_id=?", (project_id,))
    if not meta:
        return None
    relative = str(meta.get("vector_path") or "")
    if relative:
        path = (settings.resolved_data_dir / relative).resolve()
        root = settings.resolved_data_dir.resolve()
        if root not in path.parents or not path.is_file():
            return None
        meta["resolved_vector_path"] = path
    return meta


def _embed_texts(encoder, texts: list[str]):
    import numpy as np

    embedded = encoder.encode(
        texts,
        batch_size=max(1, settings.retrieval_index_batch_size),
        convert_to_numpy=True,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    vectors = np.asarray(embedded, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[1] != 768 or len(vectors) != len(texts):
        raise ValueError("Embedding output does not match the expected 768-dimensional BGE vectors")
    if not np.isfinite(vectors).all():
        raise ValueError("Embedding output contains a non-finite value")
    return vectors


def _table_text_chunks_from_rows(table_rows: list[dict], source_by_page: dict, count_tokens) -> list[dict]:
    if not table_rows:
        return []
    # The table renderer is shared with indexing so table content participates in
    # the fingerprint that decides whether an existing vector file can be reused.
    chunks: list[dict] = []
    for table in table_rows:
        cells_by_page: dict[int, list[tuple[int, list[str]]]] = {}
        for row_index, row_cells in enumerate(json_load(table.get("rows_json") or "[]")):
            cells = [str(cell.get("text") or "").strip() for cell in row_cells]
            pages = [int(cell.get("page") or table.get("start_page") or 0) for cell in row_cells]
            page = next((item for item in pages if item > 0), int(table.get("start_page") or 0))
            if page:
                cells_by_page.setdefault(page, []).append((row_index, cells))
        header = " | ".join(str(item) for item in json_load(table.get("headers_json") or "[]"))
        table_title = "；".join(item for item in (
            str(table.get("caption") or ""),
            f"单位：{table['unit']}" if table.get("unit") else "",
            f"币种：{table['currency']}" if table.get("currency") and table["currency"] != "CNY" else "",
            f"口径：{table['scope']}" if table.get("scope") else "",
            header,
        ) if item)
        for page, table_page_rows in sorted(cells_by_page.items()):
            source = source_by_page.get((str(table["document_id"]), page))
            if not source:
                continue
            lines: list[str] = []
            offsets: list[tuple[int, int, int]] = []
            position = len(table_title) + 1
            for row_index, values in table_page_rows:
                line = " | ".join(value for value in values if value)
                if not line:
                    continue
                lines.append(line)
                offsets.append((row_index, position, position + len(line)))
                position += len(line) + 1
            body = "\n".join(lines)
            if not body:
                continue
            text = f"{table_title}\n{body}" if table_title else body
            for start, end in _split_spans(text, table_title, count_tokens):
                quote = text[start:end].strip()
                if not quote:
                    continue
                selected_rows = [item[0] for item in offsets if item[1] < end and item[2] > start]
                row_start = min(selected_rows, default=0)
                row_end = max(selected_rows, default=row_start) + 1
                chunk_id = hashlib.sha256(
                    f"{source['id']}|{table['id']}|{page}|{row_start}|{row_end}|{quote}".encode("utf-8")
                ).hexdigest()[:32]
                chunks.append({
                    "chunk_id": chunk_id,
                    "source_id": str(source["id"]),
                    "document_id": str(source["document_id"]),
                    "page": page,
                    "kind": str(source.get("kind") or ""),
                    "document_role": str(source.get("document_role") or ""),
                    "section_role": str(source.get("section_role") or ""),
                    "filename": str(source.get("filename") or ""),
                    "heading": table_title,
                    "chunk_type": "table",
                    "table_key": str(table.get("table_key") or ""),
                    "start_offset": row_start,
                    "end_offset": row_end,
                    "content_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                    "text": quote,
                })
    return chunks


def _write_vectors(base: Path, fingerprint: str, vectors) -> str:
    import numpy as np

    filename = f"vectors-{fingerprint[:20]}.npy"
    final_path = base / filename
    temporary_path = base / f".{filename}.{os.getpid()}.tmp"
    try:
        with temporary_path.open("wb") as handle:
            np.save(handle, vectors, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, final_path)
    finally:
        with contextlib.suppress(OSError):
            temporary_path.unlink()
    return str(final_path.relative_to(settings.resolved_data_dir))


def _persist_index(
    project_id: str,
    fingerprint: str,
    chunks: list[dict],
    vectors,
    error_type: str,
    build_duration_ms: int,
) -> dict:
    base, _ = _safe_project_dir(project_id)
    vector_path = _write_vectors(base, fingerprint, vectors) if vectors is not None else ""
    manifest = _model_manifest()
    mode = "hybrid" if vectors is not None else "bm25_only"
    with connect() as conn:
        conn.execute("DELETE FROM retrieval_chunks WHERE project_id=?", (project_id,))
        conn.executemany(
            "INSERT INTO retrieval_chunks (project_id,chunk_id,source_id,document_id,page,kind,document_role,"
            "section_role,filename,heading,chunk_type,table_key,vector_position,start_offset,end_offset,content_hash,text) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(
                project_id, item["chunk_id"], item["source_id"], item["document_id"], item.get("page"),
                item.get("kind", ""), item.get("document_role", ""), item.get("section_role", ""),
                item.get("filename", ""), item.get("heading", ""), item.get("chunk_type", "text"),
                item.get("table_key", ""), index if vectors is not None else -1,
                item.get("start_offset", 0), item.get("end_offset", 0),
                item["content_hash"], item["text"],
            ) for index, item in enumerate(chunks)],
        )
        conn.execute(
            "INSERT INTO retrieval_indexes (project_id,fingerprint,model_id,model_revision,vector_path,dimension,"
            "chunk_count,build_duration_ms,retrieval_mode,error_type,indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET fingerprint=excluded.fingerprint,model_id=excluded.model_id,"
            "model_revision=excluded.model_revision,vector_path=excluded.vector_path,dimension=excluded.dimension,"
            "chunk_count=excluded.chunk_count,build_duration_ms=excluded.build_duration_ms,"
            "retrieval_mode=excluded.retrieval_mode,error_type=excluded.error_type,"
            "indexed_at=excluded.indexed_at",
            (project_id, fingerprint, MODEL_ID, str(manifest.get("revision") or ""), vector_path,
             768 if vectors is not None else 0, len(chunks), build_duration_ms, mode, error_type, now_iso()),
        )
    active_files = {Path(vector_path).name} if vector_path else set()
    for candidate in base.glob("vectors-*.npy"):
        if candidate.name not in active_files:
            with contextlib.suppress(OSError):
                candidate.unlink()
    return {
        "fingerprint": fingerprint, "retrieval_mode": mode, "chunk_count": len(chunks),
        "error_type": error_type, "build_duration_ms": build_duration_ms,
    }


def ensure_project_index(project_id: str, *, force: bool = False) -> dict:
    started = perf_counter()
    with _project_lock(project_id):
        source_rows = _load_project_sources(project_id)
        table_rows = _load_table_rows(project_id)
        fingerprint = _project_fingerprint(project_id, source_rows, table_rows)
        current = _current_index(project_id)
        if not force and current and current.get("fingerprint") == fingerprint:
            if current.get("retrieval_mode") == "hybrid":
                return current
            if not _model_files_ready():
                return current
            if current.get("error_type") and _encoder_error:
                return current
        encoder = _load_encoder() if _model_files_ready() else None
        sections = _section_map(source_rows)
        source_by_page: dict[tuple[str, int], dict] = {}
        chunks: list[dict] = []
        count_tokens = _token_counter(encoder) if encoder is not None else None
        for source in source_rows:
            source_id = str(source["id"])
            role, title = sections.get(source_id, ("", ""))
            source["section_role"] = role
            source_by_page.setdefault((str(source["document_id"]), int(source.get("page") or 0)), source)
            chunks.extend(_source_chunks(source, title or role, role, count_tokens))
        chunks.extend(_table_text_chunks_from_rows(table_rows, source_by_page, count_tokens))
        if not chunks:
            duration = round((perf_counter() - started) * 1000)
            return _persist_index(project_id, fingerprint, [], None, "", duration)
        vectors = None
        error_type = _encoder_error if encoder is None else ""
        if encoder is not None:
            try:
                embedding_inputs = [
                    f"{item['heading']}\n{item['text']}" if item.get("heading") else item["text"]
                    for item in chunks
                ]
                vectors = _embed_texts(encoder, embedding_inputs)
            except Exception as exc:
                error_type = type(exc).__name__
                vectors = None
        duration = round((perf_counter() - started) * 1000)
        return _persist_index(project_id, fingerprint, chunks, vectors, error_type, duration)


def index_diagnostics(project_id: str) -> dict:
    meta = row(
        "SELECT chunk_count,build_duration_ms,retrieval_mode,indexed_at,error_type "
        "FROM retrieval_indexes WHERE project_id=?",
        (project_id,),
    )
    return {
        "chunk_count": int(meta["chunk_count"]) if meta else 0,
        "index_build_ms": int(meta["build_duration_ms"]) if meta else 0,
        "retrieval_mode": str(meta["retrieval_mode"]) if meta else retrieval_status()["mode"],
        "indexed_at": str(meta["indexed_at"]) if meta else "",
        "error_type": str(meta["error_type"]) if meta else "",
    }


def _query_aliases(query: str) -> str:
    terms = [alias for key, values in ALIASES.items() if key in query for alias in values]
    return " ".join(dict.fromkeys([query, *terms]))


def _vector_path(meta: dict):
    relative = str(meta.get("vector_path") or "")
    if not relative:
        return None
    path = (settings.resolved_data_dir / relative).resolve()
    root = settings.resolved_data_dir.resolve()
    return path if root in path.parents and path.is_file() else None


def retrieve_chunks(
    project_id: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    document_ids: set[str] | None = None,
    allowed_kinds: set[str] | None = None,
    allowed_roles: set[str] | None = None,
    source_ids: set[str] | None = None,
    priority_source_ids: list[str] | None = None,
    mode: str = "hybrid",
) -> list[dict]:
    query = str(query or "").strip()[:settings.retrieval_max_query_chars]
    if not query or top_k <= 0:
        return []
    ensure_project_index(project_id)
    filters = ["project_id=?"]
    params: list[object] = [project_id]
    for column, values in (
        ("document_id", document_ids), ("kind", allowed_kinds),
        ("document_role", allowed_roles), ("source_id", source_ids),
    ):
        if values is None:
            continue
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        filters.append(f"{column} IN ({placeholders})")
        params.extend(sorted(values))
    candidates = rows(
        "SELECT chunk_id,source_id,document_id,page,kind,document_role,section_role,filename,heading,"
        "chunk_type,table_key,vector_position,start_offset,end_offset,content_hash,text FROM retrieval_chunks WHERE "
        + " AND ".join(filters) + " ORDER BY document_id,page,chunk_id",
        params,
    )
    if not candidates:
        return []
    chunks = len(candidates)
    bm25_scores: dict[str, float] = {}
    query_tokens = tokenize(_query_aliases(query))
    if mode in {"hybrid", "bm25"} and query_tokens:
        corpus = [tokenize(f"{item.get('heading') or ''} {item['text']}") for item in candidates]
        if any(corpus):
            scores = BM25Okapi(corpus).get_scores(query_tokens)
            bm25_scores = {item["chunk_id"]: float(score) for item, score in zip(candidates, scores)}
    vector_scores: dict[str, float] = {}
    meta = _current_index(project_id) or {}
    vector_file = _vector_path(meta)
    encoder = _load_encoder() if vector_file and mode in {"hybrid", "vector"} else None
    if vector_file and encoder is not None:
        try:
            import numpy as np

            vectors = np.load(vector_file, mmap_mode="r", allow_pickle=False)
            if vectors.ndim == 2 and vectors.shape == (int(meta.get("chunk_count") or 0), 768):
                vector_candidates = [item for item in candidates if int(item.get("vector_position") or -1) >= 0]
                selected_positions = [int(item["vector_position"]) for item in vector_candidates]
                if selected_positions:
                    query_vector = _embed_texts(encoder, [settings.retrieval_query_prefix + query])[0]
                    matrix = np.asarray(vectors[selected_positions], dtype=np.float32)
                    similarities = matrix @ query_vector
                    vector_scores = {
                        item["chunk_id"]: float(score)
                        for item, score in zip(vector_candidates, similarities)
                    }
        except Exception:
            vector_scores = {}
    if mode == "vector" and not vector_scores:
        return []

    limit = min(chunks, max(top_k, settings.retrieval_max_chunks))
    lexical_rank = sorted(
        (item for item in candidates if bm25_scores.get(item["chunk_id"], 0) > 0),
        key=lambda item: (-bm25_scores[item["chunk_id"]], str(item["document_id"]), int(item.get("page") or 0), item["chunk_id"]),
    )[:limit]
    dense_rank = sorted(
        (item for item in candidates if vector_scores.get(item["chunk_id"], -2) >= DENSE_MIN_SIMILARITY),
        key=lambda item: (-vector_scores.get(item["chunk_id"], -2), str(item["document_id"]), int(item.get("page") or 0), item["chunk_id"]),
    )[:limit] if vector_scores else []
    fused: dict[str, float] = {}
    for ranked in (lexical_rank if mode != "vector" else [], dense_rank if mode != "bm25" else []):
        for rank, item in enumerate(ranked, 1):
            fused[item["chunk_id"]] = fused.get(item["chunk_id"], 0.0) + 1.0 / (RRF_K + rank)
    by_id = {item["chunk_id"]: item for item in candidates}
    selected: list[dict] = []
    selected_ids: set[str] = set()
    priority_source_ids = list(dict.fromkeys(priority_source_ids or []))[: min(12, top_k)]
    for source_id in priority_source_ids:
        matching = [item for item in candidates if item["source_id"] == source_id]
        if matching:
            best = max(matching, key=lambda item: fused.get(item["chunk_id"], 0.0))
            selected.append({
                **best, "score": fused.get(best["chunk_id"], 0.0),
                "bm25_score": bm25_scores.get(best["chunk_id"], 0.0),
                "vector_score": vector_scores.get(best["chunk_id"]), "retrieval_mode": meta.get("retrieval_mode", "bm25_only"),
            })
            selected_ids.add(best["chunk_id"])
    ranked_ids = sorted(fused, key=lambda chunk_id: (-fused[chunk_id], chunk_id))
    for chunk_id in ranked_ids:
        if len(selected) >= top_k:
            break
        if chunk_id in selected_ids:
            continue
        item = by_id[chunk_id]
        selected.append({
            **item, "score": fused[chunk_id], "bm25_score": bm25_scores.get(chunk_id, 0.0),
            "vector_score": vector_scores.get(chunk_id), "retrieval_mode": meta.get("retrieval_mode", "bm25_only"),
        })
        selected_ids.add(chunk_id)
    return selected[:top_k]


def retrieve(project_id: str, query: str, top_k: int = 5) -> list[dict]:
    return [
        {**item, "id": item["source_id"]}
        for item in retrieve_chunks(project_id, query, top_k)
    ]


def rebuild_index(project_id: str, *, force: bool = True) -> dict:
    return ensure_project_index(project_id, force=force)

