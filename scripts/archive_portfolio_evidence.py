"""Preserve existing portfolio evidence locally without running models or databases."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "demo-bond-huachen", "demo-bond-ancheng", "demo-bond-rongyi",
    "demo-bond-jinghe", "demo-bond-conflict", "demo-bond-qualitative",
)
PRIMARY_RESULTS = (
    "bond-agent-evaluation-post-hardening-v2.json",
    "bond-baseline-evaluation-v4.json",
    "bond-agent-evaluation-post-prompt.json",
)


def evidence_paths(root: Path) -> list[Path]:
    paths: set[Path] = set()
    patterns = (
        "docs/assets/*.json", "docs/assets/*.md", "docs/assets/*.webm",
        "docs/*EVALUATION*.md", "docs/RELEASE_CHECKLIST.md",
        "docs/table-evaluation.md", "backend/prompts/*.yaml",
        "backend/policies/*.yaml", "backend/policies/*.yml",
        "frontend/test-results/**/*.png", "frontend/test-results/**/*.webm",
        "frontend/test-results/**/*.zip", "frontend/test-results/**/*.json",
        "acceptance_output/*.json", "acceptance_output/*.md",
        "acceptance_output/*.docx",
    )
    for pattern in patterns:
        paths.update(root.glob(pattern))
    for name in (
        "scripts/evaluate.py", "scripts/compare_evaluations.py",
        "scripts/archive_portfolio_evidence.py", "tests/test_portfolio_evidence.py",
        "docs/PORTFOLIO_EVIDENCE.md", "docs/ARCHITECTURE.md", "docs/PRD.md", "README.md",
        "backend/app/bond_documents.py", "backend/app/facts.py",
        "requirements.txt", "frontend/package.json", "frontend/package-lock.json",
        "frontend/playwright.config.ts", "frontend/test-results/.last-run.json",
        ".release/public-release-audit.json",
    ):
        paths.add(root / name)
    for case in CASES:
        base = root / ".data/demo_materials" / case
        paths.add(base / "case-manifest.json")
        paths.update((base / "gold").glob("*.json"))
    # Do not follow links into credentials, real materials, or other workspaces.
    return sorted(
        (p for p in paths if p.is_file() and not p.is_symlink()
         and root.resolve() in p.resolve().parents),
        key=lambda p: p.relative_to(root).as_posix(),
    )


def git_context(root: Path) -> dict:
    def run(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=root, stderr=subprocess.PIPE,
        ).decode("utf-8").strip()
    try:
        status = run("status", "--porcelain", "--untracked-files=normal")
        return {
            "archive_head": run("rev-parse", "HEAD"),
            "dirty": bool(status), "status_at_archive": status,
            "historical_evaluation_commit": None,
            "note": "Archive HEAD is not proof of the commit used in historical evaluations.",
        }
    except (subprocess.CalledProcessError, OSError) as exc:
        return {"archive_head": None, "dirty": None, "error_type": type(exc).__name__}


def evaluation_summary(payload: dict) -> dict:
    details = payload.get("details") or []
    return {
        "suite": payload.get("suite"), "mode": payload.get("mode"),
        "model": payload.get("model"), "metadata": payload.get("metadata"),
        "summary": payload.get("summary"), "detail_count": len(details),
        "case_run_counts": dict(Counter(d.get("case", "unknown") for d in details)),
        "status_counts": dict(Counter(d.get("status", "unknown") for d in details)),
        "baseline_status_counts": dict(Counter(
            d.get("baseline_status", "not_recorded") for d in details
        )) if payload.get("mode") == "baseline" else {},
        "scope": "Historical fictional bond suite only; not current code or real-client accuracy.",
    }


def archive(root: Path, target: Path) -> dict:
    root, target = root.resolve(), target.resolve()
    allowed = root / ".release/portfolio-evidence"
    if allowed not in target.parents:
        raise ValueError("Archive must be a new directory under .release/portfolio-evidence")
    target.mkdir(parents=True, exist_ok=False)
    manifest: dict = {
        "schema_version": 1,
        "archived_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "visibility": "local_private_not_for_git",
        "git": git_context(root), "files": [], "evaluations": {},
        "missing_primary_results": [],
        "limits": [
            "No tests, builds, paid models, or database operations performed by this archive.",
            "Original evaluation time/commit is unknown unless present in the original result.",
            "Current prompts, policies and gold are snapshots, not proven historical inputs.",
            "Screenshots, traces, videos and real reports require privacy and copyright review.",
            "Databases and uploaded/real PDF materials are NOT backed up; do not delete them based on this archive.",
            "Missing historical logs cannot prove test counts, exit codes or a current full pass.",
        ],
    }
    for source in evidence_paths(root):
        name = source.relative_to(root).as_posix()
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        destination = target / "files" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Archive hash mismatch: {name}")
        manifest["files"].append({
            "path": name, "bytes": len(data), "sha256": digest,
            "source_mtime_ns": source.stat().st_mtime_ns,
        })
        if source.name in PRIMARY_RESULTS:
            manifest["evaluations"][source.name] = evaluation_summary(json.loads(data))
        if name == "frontend/test-results/.last-run.json":
            manifest["e2e_status_file"] = {
                "payload": json.loads(data), "test_count": None, "exit_code": None,
                "note": "Status file only; no count, full log, or commit attribution available.",
            }
    manifest["missing_primary_results"] = [
        name for name in PRIMARY_RESULTS if name not in manifest["evaluations"]
    ]
    manifest["retained_data_directories"] = [
        {"path": p.name, "action": "kept_in_place_not_backed_up"}
        for p in sorted(root.glob(".data*")) if p.is_dir()
    ]
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    lines = [
        "# 本地作品集证据归档", "",
        f"归档时间：{manifest['archived_at']}", "",
        "本目录为私有备份，不上传 GitHub。没有删除、迁移或打开任何数据库。",
        "`files/` 保存原始文件副本；`manifest.json` 保存 SHA-256、来源路径及版本信息。",
        "截图、视频、trace 和真实案例报告未通过公开脱敏审查，不可直接对外发送。", "",
        "## 历史债券评测", "",
    ]
    for name, result in manifest["evaluations"].items():
        lines.append(f"- `{name}`：{len(result['case_run_counts'])} 个案例，"
                     f"{result['detail_count']} 次运行；仅对应原结果版本。")
    lines.extend([
        "", "## 尚缺证据", "",
        "- 后端全量测试和前端构建的历史完整控制台日志、退出码及代码版本绑定。",
        "- E2E 完整结果日志/报告；`.last-run.json` 不足以证明通过项数量。",
        "- 历史模型评测对应的精确 Git 提交与原始材料版本绑定。",
        "- 当前版本的脱敏操作演示；旧视频只作为历史底稿。", "",
        "真实模型验收的 `completed` 只表示流程完成，不等于报告校验或交付通过。",
        "`acceptance_output/` 如有残留交付阻断，必须保留并如实说明。", "",
        "## 数据目录", "",
        "所有 `.data*` 目录保持原位；此归档不是数据库备份，也不构成删除授权。",
        "尤其保留真实案例验证目录与 `.data-accept` 的运行和决议审计记录。", "",
        "公开证据说明见项目 `docs/PORTFOLIO_EVIDENCE.md`。", "",
    ])
    (target / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-%f")
    target = args.output or ROOT / ".release/portfolio-evidence" / timestamp
    manifest = archive(ROOT, target)
    print(json.dumps({
        "archive": str(target.resolve()), "files": len(manifest["files"]),
        "missing_primary_results": manifest["missing_primary_results"],
        "source_files_modified": False, "databases_copied": False,
    }, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
