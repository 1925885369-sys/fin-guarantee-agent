from __future__ import annotations

import jieba
from rank_bm25 import BM25Okapi

from .database import rows


def _tokens(text: str) -> list[str]:
    return [token.strip().lower() for token in jieba.cut_for_search(text) if token.strip()]


def retrieve(project_id: str, query: str, top_k: int = 5) -> list[dict]:
    sources = rows("SELECT * FROM sources WHERE project_id=?", (project_id,))
    if not sources:
        return []
    corpus = [_tokens(source["text"]) for source in sources]
    model = BM25Okapi(corpus)
    scores = model.get_scores(_tokens(query))
    ranked = sorted(zip(sources, scores), key=lambda item: item[1], reverse=True)
    return [{**source, "score": float(score)} for source, score in ranked[:top_k] if score > 0]

