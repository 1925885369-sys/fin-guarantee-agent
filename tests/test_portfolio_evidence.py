import hashlib
import json

import pytest

from scripts.archive_portfolio_evidence import archive, evaluation_summary


def test_archive_preserves_bytes_and_excludes_database_and_real_pdf(tmp_path):
    source = tmp_path / "docs/assets/bond-agent-evaluation-post-hardening-v2.json"
    source.parent.mkdir(parents=True)
    data = b'{"suite":"bond","details":[],"summary":{}}\n'
    source.write_bytes(data)
    private = tmp_path / ".data/demo_materials/demo-beitou"
    private.mkdir(parents=True)
    (private / "real.pdf").write_bytes(b"private pdf")
    (tmp_path / ".data/agent.db").write_bytes(b"database")
    target = tmp_path / ".release/portfolio-evidence/first"
    manifest = archive(tmp_path, target)
    assert source.read_bytes() == data
    assert (target / "files" / source.relative_to(tmp_path)).read_bytes() == data
    assert manifest["files"][0]["sha256"] == hashlib.sha256(data).hexdigest()
    assert not list(target.rglob("*.db"))
    assert not list(target.rglob("*.pdf"))
    assert (tmp_path / ".data/agent.db").read_bytes() == b"database"
    assert (target / "manifest.json").exists()
    with pytest.raises(FileExistsError):
        archive(tmp_path, target)


def test_archive_requires_private_output(tmp_path):
    with pytest.raises(ValueError):
        archive(tmp_path, tmp_path / "docs/archive")
    assert not (tmp_path / "docs/archive").exists()


def test_last_run_does_not_invent_test_count_or_exit_code(tmp_path):
    source = tmp_path / "frontend/test-results/.last-run.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"status": "passed", "failedTests": []}))
    manifest = archive(tmp_path, tmp_path / ".release/portfolio-evidence/e2e")
    assert manifest["e2e_status_file"]["payload"]["status"] == "passed"
    assert manifest["e2e_status_file"]["test_count"] is None
    assert manifest["e2e_status_file"]["exit_code"] is None


def test_summary_keeps_scope_and_failed_runs():
    summary = evaluation_summary({
        "suite": "bond", "mode": "baseline",
        "details": [
            {"case": "demo-bond-huachen", "status": "completed", "baseline_status": "valid"},
            {"case": "demo-bond-huachen", "status": "failed", "baseline_status": "invalid"},
        ],
    })
    assert summary["detail_count"] == 2
    assert summary["status_counts"] == {"completed": 1, "failed": 1}
    assert summary["baseline_status_counts"] == {"valid": 1, "invalid": 1}
    assert "not current code" in summary["scope"]
