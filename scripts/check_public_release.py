"""Check the proposed Git upload and reachable history without printing secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = {
    "provider_key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "huggingface_token": re.compile(rb"\bhf_[A-Za-z0-9]{30,}\b"),
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "credential_assignment": re.compile(
        rb"(?im)^[ \t]*(?:\$env:)?(?:LLM_API_KEY|OCR_API_KEY|OPENAI_API_KEY|DEEPSEEK_API_KEY|"
        rb"AWS_SECRET_ACCESS_KEY|GITHUB_TOKEN|GH_TOKEN)[ \t]*[:=][ \t]*[\"']?[A-Za-z0-9_+/=-]{20,}[\"']?[ \t\r]*$"
    ),
}
PRIVATE_PARTS = {".aws", ".ssh", ".venv", ".models", "node_modules", "__pycache__", ".release"}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pem", ".key", ".p12", ".pfx", ".pdf", ".docx", ".xlsx"}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def private_path(name: str) -> bool:
    path = Path(name)
    return (
        any(part in PRIVATE_PARTS or part == ".data" or part.startswith(".data-") for part in path.parts)
        or path.suffix.lower() in PRIVATE_SUFFIXES
        or path.name.startswith(".env") and path.name != ".env.example"
        or name.startswith(("frontend/dist/", "frontend/test-results/", "frontend/playwright-report/", "尽调报告word/"))
    )


def inspect_content(data: bytes, name: str, scope: str) -> list[dict]:
    findings = []
    for kind, pattern in SECRET_PATTERNS.items():
        match = pattern.search(data)
        if match:
            findings.append({"scope": scope, "path": name, "kind": kind,
                             "line": data[:match.start()].count(b"\n") + 1})
    return findings


def check() -> dict:
    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
    files, findings = [], []
    for name in sorted(set(filter(None, names))):
        path = ROOT / name
        if not path.is_file():
            continue
        if path.is_symlink():
            findings.append({"scope": "working_tree", "path": name, "kind": "symlink_requires_review"})
            continue
        if private_path(name):
            findings.append({"scope": "working_tree", "path": name, "kind": "private_or_generated_file"})
        data = path.read_bytes()
        if len(data) >= 50 * 1024 * 1024:
            findings.append({"scope": "working_tree", "path": name, "kind": "large_file_50mb"})
        findings.extend(inspect_content(data, name, "working_tree"))
        files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})

    objects = git("rev-list", "--objects", "--all").decode("utf-8").splitlines()
    history_blobs = 0
    for entry in objects:
        oid, _, name = entry.partition(" ")
        if git("cat-file", "-t", oid).strip() != b"blob":
            continue
        history_blobs += 1
        if private_path(name):
            findings.append({"scope": "history", "path": name, "kind": "private_or_generated_file", "object": oid})
        data = git("cat-file", "blob", oid)
        findings.extend({**item, "object": oid} for item in inspect_content(data, name, "history"))

    return {
        "status": "passed" if not findings else "blocked",
        "head": git("rev-parse", "HEAD").decode().strip(),
        "candidate_files": len(files), "candidate_bytes": sum(item["bytes"] for item in files),
        "history_blobs_checked": history_blobs,
        "findings": findings, "files": files,
        "limitations": "Pattern-based check; review documents and media manually. No network calls or Git staging performed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional local JSON audit path")
    args = parser.parse_args()
    result = check()
    if args.output:
        target = args.output.resolve()
        allowed = (ROOT / ".release").resolve()
        if allowed not in target.parents:
            parser.error("audit output must be inside the ignored .release directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "files"}, ensure_ascii=True, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
