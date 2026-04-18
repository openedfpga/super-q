"""CI integration.

Goals:
  * Auto-detect the runner (GitHub Actions, GitLab CI, CircleCI, Buildkite).
  * Emit native annotations so timing failures show up next to the
    relevant file in PR reviews.
  * Write machine-parseable outputs to the runner's "outputs" channel
    (e.g. GITHUB_OUTPUT) for chained jobs.
  * Upload artifacts to the canonical location per runner.

The `superq ci build <path>` command wraps `sweep` with CI-friendly
defaults: non-interactive, JSON summary on stdout, annotations on
stderr (so agents piping JSON don't choke), exit codes tuned for PRs.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CiEnv:
    name: str                          # 'github', 'gitlab', 'circle', 'buildkite', 'local'
    pr_number: int | None = None
    repo: str = ""
    run_id: str = ""
    commit: str = ""
    actor: str = ""
    outputs_path: Path | None = None   # writes `key=value` here on GHA

    @property
    def is_ci(self) -> bool:
        return self.name != "local"


def detect() -> CiEnv:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return CiEnv(
            name="github",
            pr_number=_int(os.environ.get("GITHUB_PR_NUMBER")),
            repo=os.environ.get("GITHUB_REPOSITORY", ""),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            commit=os.environ.get("GITHUB_SHA", ""),
            actor=os.environ.get("GITHUB_ACTOR", ""),
            outputs_path=_opt_path(os.environ.get("GITHUB_OUTPUT")),
        )
    if os.environ.get("GITLAB_CI"):
        return CiEnv(
            name="gitlab",
            repo=os.environ.get("CI_PROJECT_PATH", ""),
            run_id=os.environ.get("CI_JOB_ID", ""),
            commit=os.environ.get("CI_COMMIT_SHA", ""),
            actor=os.environ.get("GITLAB_USER_LOGIN", ""),
        )
    if os.environ.get("CIRCLECI"):
        return CiEnv(
            name="circle",
            repo=f"{os.environ.get('CIRCLE_PROJECT_USERNAME','')}/{os.environ.get('CIRCLE_PROJECT_REPONAME','')}",
            run_id=os.environ.get("CIRCLE_WORKFLOW_ID", ""),
            commit=os.environ.get("CIRCLE_SHA1", ""),
            actor=os.environ.get("CIRCLE_USERNAME", ""),
        )
    if os.environ.get("BUILDKITE") == "true":
        return CiEnv(
            name="buildkite",
            repo=os.environ.get("BUILDKITE_REPO", ""),
            run_id=os.environ.get("BUILDKITE_BUILD_ID", ""),
            commit=os.environ.get("BUILDKITE_COMMIT", ""),
            actor=os.environ.get("BUILDKITE_BUILD_AUTHOR", ""),
        )
    return CiEnv(name="local")


# ---------------------------------------------------------------------------
# annotations
# ---------------------------------------------------------------------------


def annotate(env: CiEnv, level: str, message: str, *, file: Path | None = None,
             line: int | None = None, title: str | None = None) -> None:
    """Emit a runner-native annotation so the message surfaces in PR review UI.

    `level`: 'notice' | 'warning' | 'error'
    """
    if env.name == "github":
        params: list[str] = []
        if file:
            params.append(f"file={file}")
        if line is not None:
            params.append(f"line={line}")
        if title:
            params.append(f"title={title}")
        prefix = f"::{level} " + ",".join(params) + "::" if params else f"::{level}::"
        print(f"{prefix}{message}", file=sys.stderr)
    elif env.name == "gitlab":
        # GitLab doesn't have first-class annotations; put them in the job log.
        print(f"[{level.upper()}] {message}", file=sys.stderr)
    else:
        print(f"[{level}] {message}", file=sys.stderr)


def set_output(env: CiEnv, key: str, value: Any) -> None:
    """Write a key=value pair to the runner's outputs channel, if any.

    GHA jobs downstream can read `${{ steps.build.outputs.<key> }}`.
    """
    if isinstance(value, (dict, list)):
        value = json.dumps(value)
    if env.name == "github" and env.outputs_path:
        with open(env.outputs_path, "a") as f:
            f.write(f"{key}={value}\n")
    # For other CIs we just print; each has its own way and users can parse.
    print(f"::superq-output {key}={value}", file=sys.stderr)


def summary_markdown(env: CiEnv, md: str) -> None:
    """Append to the runner's job summary (GHA: $GITHUB_STEP_SUMMARY)."""
    if env.name == "github":
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a") as f:
                f.write(md + "\n")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _opt_path(v: str | None) -> Path | None:
    return Path(v) if v else None


# ---------------------------------------------------------------------------
# report rendering
# ---------------------------------------------------------------------------


def render_sweep_summary(outcome: Any) -> str:
    """Pretty markdown for GHA job summaries. Accepts SweepOutcome or dict."""
    if hasattr(outcome, "as_dict"):
        d = outcome.as_dict()
    else:
        d = outcome

    core = d.get("core", {})
    best = d.get("best")
    summary = d.get("summary", {})

    md = [f"### super-q · {core.get('core_name', '')}", ""]
    if best:
        md.append(f"**PASS** · seed {best['seed']} · slack "
                  f"`{best.get('slack_ns', 'n/a'):+.3f} ns` · "
                  f"Fmax `{best.get('fmax_mhz', 'n/a')} MHz`")
    else:
        md.append("**FAIL** · no passing seed")
    md.append("")
    md.append(f"ran={summary.get('ran', 0)} · "
              f"passed={summary.get('passed', 0)} · "
              f"failed={summary.get('failed', 0)}")
    md.append("")
    md.append("| seed | status | slack (ns) | Fmax (MHz) | duration |")
    md.append("|-----:|:-------|-----------:|-----------:|---------:|")
    for r in d.get("results", []):
        status = "pass" if r.get("passed") else "fail"
        slack = r.get("slack_ns")
        fmax = r.get("fmax_mhz")
        md.append(f"| {r['seed']} | {status} | "
                  f"{slack:+.3f} |".replace("None:+.3f", "—")
                  if slack is not None else
                  f"| {r['seed']} | {status} | — | "
                  f"{fmax if fmax is not None else '—'} | "
                  f"{r.get('duration_s', 0):.0f}s |")
    return "\n".join(md)
