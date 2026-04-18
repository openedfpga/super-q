"""GitHub Actions helpers.

Wraps `gh` CLI (not the raw REST API) so we get its auth handling,
rate-limit retries, and artifact-download logic for free. Everything
here returns plain dicts so the CLI can format them with rich or emit
JSON straight to stdout for agents.

Auto-detects the current repo from `git remote get-url origin` when
the caller doesn't pass `--repo`, so the common case (run in a core
repo) is one word: `superq gha watch`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GhaError(Exception):
    pass


# Matches https://github.com/owner/repo(.git), git@github.com:owner/repo(.git),
# and gh CLI's shortname `owner/repo`.
_REMOTE_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?/?$")


def detect_repo(cwd: Path | None = None) -> str | None:
    """Return `owner/repo` for the given repo, or None if we can't tell."""
    cwd = cwd or Path.cwd()
    if shutil.which("git") is None:
        return None
    try:
        url = subprocess.check_output(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None

    m = _REMOTE_RE.search(url)
    return f"{m.group('owner')}/{m.group('repo')}" if m else None


def _ensure_gh() -> None:
    if shutil.which("gh") is None:
        raise GhaError(
            "the `gh` CLI is required for super-q's GHA helpers — "
            "install from https://cli.github.com/ or `brew install gh`."
        )


def _gh_api(path: str, *, method: str = "GET", data: dict | None = None) -> Any:
    _ensure_gh()
    cmd = ["gh", "api", path]
    if method != "GET":
        cmd += ["--method", method]
    if data is not None:
        for k, v in data.items():
            cmd += ["-f", f"{k}={v}" if isinstance(v, str) else f"{k}={json.dumps(v)}"]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        raise GhaError(f"gh api {method} {path} failed: {e.stderr.strip()[:400]}") from e
    if not out:
        return None
    return json.loads(out)


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


@dataclass
class RunSummary:
    id: int
    name: str
    status: str           # 'queued' | 'in_progress' | 'completed'
    conclusion: str | None
    event: str
    branch: str
    commit_sha: str
    created_at: str
    updated_at: str
    url: str
    duration_s: float | None
    workflow_path: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def list_runs(repo: str, *, workflow: str | None = None, limit: int = 10) -> list[RunSummary]:
    path = f"repos/{repo}/actions/runs?per_page={limit}"
    if workflow:
        path = f"repos/{repo}/actions/workflows/{workflow}/runs?per_page={limit}"
    data = _gh_api(path)
    return [_run_from(r) for r in (data or {}).get("workflow_runs", [])]


def get_run(repo: str, run_id: int) -> RunSummary:
    return _run_from(_gh_api(f"repos/{repo}/actions/runs/{run_id}"))


def get_jobs(repo: str, run_id: int) -> list[dict]:
    data = _gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs") or {}
    return data.get("jobs", [])


def _run_from(raw: dict) -> RunSummary:
    # run_attempt_*_timing isn't returned on the summary endpoint; compute from timestamps.
    created = raw.get("created_at") or ""
    updated = raw.get("updated_at") or ""
    dur: float | None = None
    if raw.get("status") == "completed" and created and updated:
        from datetime import datetime
        try:
            dur = (datetime.fromisoformat(updated.replace("Z", "+00:00"))
                   - datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            dur = None
    return RunSummary(
        id=int(raw["id"]),
        name=raw.get("name") or raw.get("display_title") or "",
        status=raw["status"],
        conclusion=raw.get("conclusion"),
        event=raw.get("event", ""),
        branch=raw.get("head_branch", ""),
        commit_sha=raw.get("head_sha", ""),
        created_at=created,
        updated_at=updated,
        url=raw.get("html_url", ""),
        duration_s=dur,
        workflow_path=raw.get("path", ""),
    )


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


def watch_run(
    repo: str,
    run_id: int,
    *,
    poll_s: float = 5.0,
    timeout_s: float | None = None,
    on_update: Any = None,           # callable(status_dict) -> None
) -> RunSummary:
    """Block until the run completes (or we hit timeout), calling `on_update`
    whenever status/step state changes. Returns the final summary.
    """
    start = time.time()
    last_fingerprint: tuple | None = None
    while True:
        run = get_run(repo, run_id)
        jobs = get_jobs(repo, run_id) if run.status != "queued" else []

        fp = (
            run.status,
            run.conclusion,
            tuple((j.get("name"), j.get("status"), j.get("conclusion"),
                   _current_step_name(j)) for j in jobs),
        )
        if fp != last_fingerprint:
            last_fingerprint = fp
            if on_update:
                on_update({
                    "run": run.as_dict(),
                    "jobs": [
                        {
                            "name": j.get("name"),
                            "status": j.get("status"),
                            "conclusion": j.get("conclusion"),
                            "current_step": _current_step_name(j),
                            "started_at": j.get("started_at"),
                        }
                        for j in jobs
                    ],
                    "elapsed_s": round(time.time() - start, 1),
                })

        if run.status == "completed":
            return run
        if timeout_s is not None and time.time() - start > timeout_s:
            return run
        time.sleep(poll_s)


def _current_step_name(job: dict) -> str | None:
    """Return the name of the step currently running, or the last-completed one."""
    steps = job.get("steps") or []
    running = next((s for s in steps if s.get("status") == "in_progress"), None)
    if running:
        return running.get("name")
    completed = [s for s in steps if s.get("status") == "completed"]
    return completed[-1].get("name") if completed else None


# ---------------------------------------------------------------------------
# actions: trigger + download
# ---------------------------------------------------------------------------


def trigger_workflow(repo: str, workflow: str, *, ref: str = "main",
                     inputs: dict | None = None) -> str:
    """Fire `workflow_dispatch` on the given workflow. Returns the fresh run id
    once the API exposes it (polls up to 30 s)."""
    body: dict[str, Any] = {"ref": ref}
    if inputs:
        body["inputs"] = inputs
    _gh_api(
        f"repos/{repo}/actions/workflows/{workflow}/dispatches",
        method="POST", data=body,
    )

    # GitHub's dispatches API doesn't return the run id; poll the runs list
    # for a new entry that matches our ref + event.
    for _ in range(15):
        time.sleep(2)
        runs = list_runs(repo, workflow=workflow, limit=5)
        for r in runs:
            if r.event == "workflow_dispatch" and r.branch == ref and r.status != "completed":
                return str(r.id)
    raise GhaError("dispatched workflow but couldn't locate the new run id after 30 s")


def download_artifacts(repo: str, run_id: int, out_dir: Path,
                       *, name: str = "") -> list[Path]:
    """Pull a run's artifacts to `out_dir/`. Returns the paths written.

    `name` optionally filters to a single artifact name (the gh CLI's
    --name flag). Empty string means 'all artifacts'.
    """
    _ensure_gh()
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["gh", "run", "download", str(run_id), "-R", repo, "-D", str(out_dir)]
    if name:
        cmd += ["-n", name]
    subprocess.check_call(cmd)
    return sorted(out_dir.rglob("*"))
