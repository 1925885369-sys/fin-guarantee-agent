from __future__ import annotations

import argparse

from backend.app.database import init_db
from backend.app.retrieval import rebuild_index, retrieval_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or refresh a project's local retrieval index.")
    parser.add_argument("project_id")
    parser.add_argument("--reuse", action="store_true", help="Reuse a matching index when available")
    args = parser.parse_args()

    init_db()
    status = rebuild_index(args.project_id, force=not args.reuse)
    print(f"project={args.project_id}")
    print(f"mode={status.get('retrieval_mode', 'bm25_only')}")
    print(f"chunks={status.get('chunk_count', 0)}")
    print(f"index_ms={status.get('build_duration_ms', 0)}")
    print(f"embedding={retrieval_status()['mode']}")
    if status.get("error_type"):
        print(f"embedding_error={status['error_type']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
