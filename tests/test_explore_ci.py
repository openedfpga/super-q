from pathlib import Path

import pytest

from super_q.ci import detect, render_sweep_summary
from super_q.explore import default_ladder
from super_q.pool_config import PoolSpec, describe, load, write_example


def test_ladder_rungs_escalate():
    ladder = default_ladder(parallel=4)
    assert [r.name for r in ladder][:2] == ["quick-range", "wider-range"]
    # rungs later in the ladder should request more work or higher effort
    assert len(ladder[2].plan_factory().seeds) >= len(ladder[0].plan_factory().seeds)


def test_pool_spec_defaults():
    p = PoolSpec(name="modal", kind="modal", raw={"max_parallel": 12, "cpu": 8})
    assert p.max_parallel == 12
    assert p.opt("cpu") == 8
    assert p.opt("missing", 99) == 99


def test_ci_detect_defaults_to_local(monkeypatch):
    for k in ("GITHUB_ACTIONS", "GITLAB_CI", "CIRCLECI", "BUILDKITE"):
        monkeypatch.delenv(k, raising=False)
    env = detect()
    assert env.name == "local"
    assert env.is_ci is False


def test_ci_detect_github(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    env = detect()
    assert env.name == "github"
    assert env.repo == "owner/repo"
    assert env.run_id == "12345"


def test_write_example(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERQ_CONFIG", str(tmp_path / "config.toml"))
    p = write_example()
    assert p.exists()
    data = p.read_bytes()
    assert b"pool.modal" in data
    # Idempotent
    p2 = write_example()
    assert p2 == p


def test_describe_with_pools(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("""
[pool.modal]
kind = "modal"
max_parallel = 16
""")
    monkeypatch.setenv("SUPERQ_CONFIG", str(cfg))
    pools = load()
    assert "modal" in pools
    assert pools["modal"].max_parallel == 16
    d = describe(pools)
    assert d["pools"][0]["name"] == "modal"


def test_render_sweep_summary():
    md = render_sweep_summary({
        "core": {"core_name": "foo"},
        "best": {"seed": 5, "slack_ns": 0.2, "fmax_mhz": 75.0},
        "summary": {"ran": 3, "passed": 1, "failed": 2},
        "results": [
            {"seed": 5, "passed": True, "slack_ns": 0.2, "fmax_mhz": 75.0, "duration_s": 120},
            {"seed": 6, "passed": False, "slack_ns": -0.1, "fmax_mhz": None, "duration_s": 200},
        ],
    })
    assert "PASS" in md
    assert "seed 5" in md
