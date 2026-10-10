from __future__ import annotations

import argparse
import json
from time import perf_counter
from pathlib import Path

from backend.app.database import init_db
from backend.app.retrieval import retrieve_chunks


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate retrieval against a private, locally stored gold set.")
    parser.add_argument("project_id")
    parser.add_argument("--gold", type=Path, default=Path(".data/retrieval-gold.json"))
    args = parser.parse_args()
    if not args.gold.is_file():
        raise SystemExit(f"Gold set not found: {args.gold}. Keep real-material annotations under .data.")

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    if not isinstance(gold, list) or not gold:
        raise SystemExit("Gold set must be a non-empty JSON array")
    init_db()
    eligible = [item for item in gold if item.get("answerable", True) and item.get("gold_pages")]
    unanswerable = [item for item in gold if item.get("answerable") is False]
    if not eligible:
        raise SystemExit("Gold set has no answerable questions with gold_pages")

    modes = ("bm25", "vector", "hybrid")
    summary: dict[str, dict] = {}
    for mode in modes:
        hits = 0
        critical_hits = 0
        critical_total = 0
        unanswerable_hits = 0
        query_times: list[float] = []
        for item in eligible:
            started = perf_counter()
            results = retrieve_chunks(
                args.project_id,
                str(item["question"]),
                top_k=10,
                document_ids=set(item["document_ids"]) if item.get("document_ids") else None,
                mode=mode,
            )
            query_times.append((perf_counter() - started) * 1000)
            gold_pages = {(str(page[0]), int(page[1])) for page in item["gold_pages"]}
            found = any((str(result["document_id"]), int(result.get("page") or 0)) in gold_pages for result in results)
            hits += int(found)
            if item.get("critical"):
                critical_total += 1
                critical_hits += int(found)
        for item in unanswerable:
            started = perf_counter()
            results = retrieve_chunks(
                args.project_id,
                str(item["question"]),
                top_k=10,
                document_ids=set(item["document_ids"]) if item.get("document_ids") else None,
                mode=mode,
            )
            query_times.append((perf_counter() - started) * 1000)
            unanswerable_hits += int(bool(results))
        summary[mode] = {
            "questions": len(eligible),
            "recall_at_10": round(hits / len(eligible), 4),
            "critical_questions": critical_total,
            "critical_recall_at_10": round(critical_hits / critical_total, 4) if critical_total else None,
            "unanswerable_questions": len(unanswerable),
            "unanswerable_retrieval_rate": round(unanswerable_hits / len(unanswerable), 4) if unanswerable else None,
            "mean_query_ms": round(sum(query_times) / len(query_times), 2),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
