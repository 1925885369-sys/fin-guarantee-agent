from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

from backend.app.config import settings
from backend.app.retrieval import MODEL_ID, MODEL_MANIFEST


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and pin the local BGE retrieval model.")
    parser.add_argument("--revision", default="main", help="Model branch, tag, or commit SHA to download")
    parser.add_argument("--output", type=Path, default=settings.resolved_retrieval_model_dir)
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    resolved_revision = HfApi().model_info(MODEL_ID, revision=args.revision).sha
    snapshot_download(
        repo_id=MODEL_ID,
        revision=resolved_revision,
        local_dir=str(output),
    )
    if not (output / "config.json").is_file() or not (output / "modules.json").is_file():
        raise RuntimeError("Downloaded model is missing the expected Sentence Transformers files")
    manifest = {
        "model_id": MODEL_ID,
        "revision": resolved_revision,
        "requested_revision": args.revision,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    fd, temporary = tempfile.mkstemp(prefix=".retrieval-model-", suffix=".json", dir=output)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output / MODEL_MANIFEST)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Prepared {MODEL_ID} at revision {resolved_revision}")
    print(f"Local model directory: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
