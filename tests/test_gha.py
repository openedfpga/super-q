from pathlib import Path

import pytest

from super_q.gha import _run_from, detect_repo


def test_detect_repo_from_https_remote(tmp_path: Path, monkeypatch):
    import subprocess
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    subprocess.check_call(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "https://github.com/openedfpga/super-q.git"],
    )
    assert detect_repo(tmp_path) == "openedfpga/super-q"


def test_detect_repo_from_ssh_remote(tmp_path: Path):
    import subprocess
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    subprocess.check_call(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:openedfpga/openFPGA-Popeye.git"],
    )
    assert detect_repo(tmp_path) == "openedfpga/openFPGA-Popeye"


def test_detect_repo_returns_none_without_remote(tmp_path: Path):
    import subprocess
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    assert detect_repo(tmp_path) is None


def test_detect_repo_rejects_non_github(tmp_path: Path):
    import subprocess
    subprocess.check_call(["git", "init", "-q", str(tmp_path)])
    subprocess.check_call(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "https://gitlab.example.com/me/my-core.git"],
    )
    assert detect_repo(tmp_path) is None


def test_run_summary_parsing():
    raw = {
        "id": 123,
        "name": "build",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "head_sha": "abc123",
        "created_at": "2026-04-18T10:00:00Z",
        "updated_at": "2026-04-18T10:05:00Z",
        "html_url": "https://github.com/owner/repo/actions/runs/123",
        "path": ".github/workflows/build.yml",
    }
    r = _run_from(raw)
    assert r.id == 123
    assert r.conclusion == "success"
    assert r.duration_s == 300.0
    assert r.workflow_path == ".github/workflows/build.yml"


def test_run_summary_in_progress_has_no_duration():
    raw = {
        "id": 1, "name": "build",
        "status": "in_progress", "conclusion": None,
        "event": "push", "head_branch": "main", "head_sha": "x",
        "created_at": "2026-04-18T10:00:00Z", "updated_at": "2026-04-18T10:01:00Z",
        "html_url": "", "path": ".github/workflows/build.yml",
    }
    assert _run_from(raw).duration_s is None
