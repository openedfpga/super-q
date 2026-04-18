"""Smoke: every GHA workflow we ship must parse as valid YAML.

We caught one "multi-line `python -c` inside `run: |` broke block
indentation" bug in reusable-build.yml only after GitHub Actions
accepted the push and failed the run with zero logs. This test runs
every .yml under `.github/workflows/` and under `examples/**/workflows/`
through PyYAML, so that mistake can't ship again.

Also renders the init templates with a dummy set of variables and
parses the result — inline mode had the same shape of bug.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _all_workflow_files() -> list[Path]:
    """Every GHA workflow under .github/workflows/ — both ours and examples.

    Deliberately scoped to GitHub Actions: GitLab `.gitlab-ci.yml` and
    other CI files live alongside but have different schemas.
    """
    out: list[Path] = []
    out.extend((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    out.extend((_REPO_ROOT / ".github" / "workflows").glob("*.yaml"))
    for ex in (_REPO_ROOT / "examples").rglob(".github/workflows/*.yml"):
        out.append(ex)
    return sorted(out)


@pytest.mark.parametrize("path", _all_workflow_files(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_workflow_yaml_parses(path: Path) -> None:
    """Any syntax-level regression here fails the test suite instead of CI."""
    with open(path) as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as e:
            pytest.fail(f"{path.relative_to(_REPO_ROOT)}: {e}")
    # Non-empty top-level mapping — every workflow has at least name+jobs.
    assert isinstance(data, dict), f"{path}: empty or non-mapping root"
    # YAML coerces the literal `on` key to Python True. Accept either form.
    keys = set(data.keys())
    assert ("on" in keys) or (True in keys), f"{path}: no `on:` trigger"
    assert "jobs" in keys, f"{path}: no `jobs:` section"


def test_init_reusable_templates_parse(tmp_path: Path) -> None:
    """`superq init` reusable-mode templates must parse once rendered."""
    from super_q.init import InitOptions, scaffold

    scaffold(InitOptions(
        target=tmp_path / "a.b", author="a", name="b", git_init=False,
    ))
    for f in (tmp_path / "a.b/.github/workflows").rglob("*.yml"):
        with open(f) as fh:
            yaml.safe_load(fh)


def test_init_inline_templates_parse(tmp_path: Path) -> None:
    """`superq init --inline` templates must parse once rendered."""
    from super_q.init import InitOptions, scaffold

    scaffold(InitOptions(
        target=tmp_path / "a.b", author="a", name="b",
        ci_only=True, inline=True, git_init=False,
    ))
    for f in (tmp_path / "a.b/.github/workflows").rglob("*.yml"):
        with open(f) as fh:
            data = yaml.safe_load(fh)
        # Inline mode shouldn't rely on any `uses:` cross-repo call.
        for job in (data.get("jobs") or {}).values():
            assert "uses" not in job, f"{f.name}: inline job has a `uses:` reference"
