"""收口方案 §五 · 验收归档：pytest JUnit + 前端交互报告 + 构建日志，绑定代码版本。

背景（收口方案 §五）：「归档 pytest JUnit、前端交互报告和构建日志，绑定对应代码版本；
重启服务确认加载版本后验证，不能用旧进程结果宣称新代码通过。」

用法（用项目 .venv 解释器运行，pytest 在其中）：
  .venv/Scripts/python.exe scripts/archive_verification_artifacts.py --run-tests
  .venv/Scripts/python.exe scripts/archive_verification_artifacts.py --junit <x.xml> \
      --frontend frontend/test-results --build-log <build.log> --label 阶段四

默认输出：`acceptance_output/verification-<时间戳>/`（manifest.json + 归档产物）。
退出码：0=归档完成且（如跑了测试）全部通过；1=有测试失败或请求的产物缺失
（**日志仍已归档**——失败证据本身就是要保留的东西）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 标准回归子集（收口方案 §五 / MEMORY 约定：15 文件、单进程串行）。
REGRESSION_FILES = [
    "tests/test_chapter_guard.py", "tests/test_metrics.py", "tests/test_table_evidence.py",
    "tests/test_table_evidence_freeze.py", "tests/test_evidence_records.py", "tests/test_p0.py",
    "tests/test_p0_records.py", "tests/test_validator_states.py", "tests/test_delivery_wording.py",
    "tests/test_report_delivery.py", "tests/test_failure_flow.py", "tests/test_report_boundary.py",
    "tests/test_llm_budget.py", "tests/test_prompt_length_policy.py", "tests/test_acceptance_diagnosis.py",
]

# 代码版本绑定的关键文件：校验/抽取/渲染链路 + 两份 Prompt 契约。
KEY_FILES = [
    "backend/app/chapter_guard.py", "backend/app/p0.py", "backend/app/evidence_records.py",
    "backend/app/table_extraction.py", "backend/app/facts.py", "backend/app/workflow.py",
    "backend/app/llm.py", "backend/app/exports.py", "backend/app/report_tables.py",
    "backend/prompts/bond_chapters.yaml", "backend/prompts/bond_extraction_contracts.yaml",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _record_artifact(manifest: dict, path: Path, *, directory: bool = False) -> None:
    if directory:
        files = [item for item in path.rglob("*") if item.is_file()]
        manifest["artifacts"].append({
            "name": path.name + "/", "files": len(files),
            "bytes": sum(item.stat().st_size for item in files),
        })
    else:
        manifest["artifacts"].append({
            "name": path.name, "bytes": path.stat().st_size, "sha256": _sha(path)[:16],
        })


def main() -> int:
    parser = argparse.ArgumentParser(
        description="收口方案 §五 验收归档（JUnit/前端报告/构建日志 + 代码版本绑定）")
    parser.add_argument("--label", default="", help="归档标签（如：阶段四免费回归）")
    parser.add_argument("--run-tests", action="store_true",
                        help="先跑标准回归子集（15 文件、单进程，约 12 分钟）并归档 JUnit")
    parser.add_argument("--junit", default="", help="现成 JUnit XML 路径（与 --run-tests 二选一）")
    parser.add_argument("--frontend", default="", help="前端交互报告目录（如 frontend/test-results）")
    parser.add_argument("--build-log", default="", help="前端构建日志文件路径")
    parser.add_argument("--out", default="", help="输出目录（默认 acceptance_output/verification-<时间戳>）")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else ROOT / "acceptance_output" / f"verification-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": args.label,
        "pytest_exit_code": None,
        "artifacts": [],
        "warnings": [],
    }
    dirty = [line for line in _git("status", "--porcelain").splitlines() if line.strip()]
    manifest["code"] = {
        "git_head": _git("rev-parse", "HEAD"),
        "git_dirty_files": len(dirty),
        "git_dirty_sample": dirty[:12],
        "files": {path: _sha(ROOT / path)[:16] for path in KEY_FILES if (ROOT / path).is_file()},
    }

    failed = False

    # 1) pytest JUnit（跑或收）
    if args.run_tests:
        junit_path = out_dir / "pytest-junit.xml"
        log_path = out_dir / "pytest.log"
        command = [sys.executable, "-u", "-m", "pytest", *REGRESSION_FILES, "-q", "--tb=short",
                   f"--junitxml={junit_path}"]
        print(f"[归档] 运行回归子集（{len(REGRESSION_FILES)} 文件，单进程）…")
        with open(log_path, "wb") as log_handle:
            proc = subprocess.run(command, cwd=ROOT, stdout=log_handle, stderr=subprocess.STDOUT)
        manifest["pytest_exit_code"] = proc.returncode
        if proc.returncode != 0:
            failed = True
            manifest["warnings"].append(f"pytest 退出码 {proc.returncode}（失败证据已归档，不得宣称通过）")
        for name in ("pytest-junit.xml", "pytest.log"):
            target = out_dir / name
            if target.is_file():
                _record_artifact(manifest, target)
    elif args.junit:
        source = Path(args.junit)
        if source.is_file():
            destination = out_dir / source.name
            shutil.copy2(source, destination)
            _record_artifact(manifest, destination)
        else:
            failed = True
            manifest["warnings"].append(f"JUnit 不存在：{source}")

    # 2) 前端交互报告（目录整体复制）
    if args.frontend:
        source = Path(args.frontend)
        if source.is_dir():
            destination = out_dir / source.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            _record_artifact(manifest, destination, directory=True)
        else:
            failed = True
            manifest["warnings"].append(f"前端报告目录不存在：{source}")

    # 3) 构建日志
    if args.build_log:
        source = Path(args.build_log)
        if source.is_file():
            destination = out_dir / source.name
            shutil.copy2(source, destination)
            _record_artifact(manifest, destination)
        else:
            failed = True
            manifest["warnings"].append(f"构建日志不存在：{source}")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[归档] 输出目录：{out_dir}")
    print(f"[归档] git HEAD={manifest['code']['git_head'][:12] or '-'} "
          f"未提交文件={manifest['code']['git_dirty_files']} "
          f"关键文件指纹={len(manifest['code']['files'])} 项")
    for item in manifest["artifacts"]:
        print(f"  - {item['name']}（{item.get('bytes', 0)} B）")
    for warning in manifest["warnings"]:
        print(f"  ! {warning}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
