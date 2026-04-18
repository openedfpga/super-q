import json
from pathlib import Path

import pytest

from super_q.init import InitError, InitOptions, scaffold, validate_identifier


def test_validate_identifier_rejects_dots():
    with pytest.raises(InitError):
        validate_identifier("bad.name", "author")


def test_full_scaffold(tmp_path: Path):
    target = tmp_path / "alice.my-core"
    opts = InitOptions(target=target, author="alice", name="my-core",
                      version="0.2.0", git_init=False)
    res = scaffold(opts)

    expected = {
        ".github/workflows/build.yml",
        ".github/workflows/release.yml",
        ".gitignore",
        ".dockerignore",
        "README.md",
        "dist/Cores/alice.my-core/core.json",
        "dist/Cores/alice.my-core/audio.json",
        "dist/Cores/alice.my-core/video.json",
        "dist/Cores/alice.my-core/input.json",
        "dist/Cores/alice.my-core/interact.json",
        "dist/Cores/alice.my-core/data.json",
        "dist/Cores/alice.my-core/variants.json",
        "dist/Platforms/my_core.json",
        "dist/Platforms/_images/.gitkeep",
        "src/fpga/README.md",
    }
    relatives = {str(p.relative_to(target)) for p in res.created}
    assert expected.issubset(relatives), expected - relatives


def test_core_json_is_valid(tmp_path: Path):
    opts = InitOptions(target=tmp_path / "a.b", author="a", name="b",
                      version="1.0.0", git_init=False)
    scaffold(opts)
    data = json.loads((tmp_path / "a.b/dist/Cores/a.b/core.json").read_text())
    assert data["core"]["metadata"]["author"] == "a"
    assert data["core"]["metadata"]["version"] == "1.0.0"
    assert data["core"]["metadata"]["shortname"] == "b"
    assert data["core"]["cores"][0]["filename"] == "bitstream.rbf_r"


def test_ci_only_leaves_dist_alone(tmp_path: Path):
    target = tmp_path / "existing-repo"
    target.mkdir()
    (target / "README.md").write_text("existing content")
    (target / "src").mkdir()

    opts = InitOptions(target=target, author="alice", name="x",
                      ci_only=True, git_init=False)
    res = scaffold(opts)

    # Only the two workflows should be written.
    created = [p.relative_to(target) for p in res.created]
    assert set(str(p) for p in created) == {
        ".github/workflows/build.yml",
        ".github/workflows/release.yml",
    }
    # README untouched.
    assert (target / "README.md").read_text() == "existing content"


def test_force_overwrites(tmp_path: Path):
    opts = InitOptions(target=tmp_path, author="a", name="b", git_init=False)
    scaffold(opts)
    (tmp_path / "README.md").write_text("stomped")
    res2 = scaffold(opts)
    assert (tmp_path / "README.md").read_text() == "stomped"  # no overwrite without --force
    assert str(tmp_path / "README.md") in [str(p) for p in res2.skipped]

    opts.force = True
    scaffold(opts)
    assert "alice" not in (tmp_path / "README.md").read_text()
    assert "**next**" not in (tmp_path / "README.md").read_text()
    assert (tmp_path / "README.md").read_text().startswith("# a.b")


def test_workflow_references_super_q_ref(tmp_path: Path):
    opts = InitOptions(target=tmp_path / "t", author="a", name="b",
                      super_q_ref="v0.2.0", git_init=False)
    scaffold(opts)
    build = (tmp_path / "t/.github/workflows/build.yml").read_text()
    release = (tmp_path / "t/.github/workflows/release.yml").read_text()
    assert "reusable-build.yml@v0.2.0" in build
    assert "reusable-release.yml@v0.2.0" in release


def test_workflow_respects_super_q_repo_override(tmp_path: Path):
    """When super-q lives at ericlewis/super-q instead of super-q/super-q,
    the generated `uses:` line follows the override."""
    opts = InitOptions(
        target=tmp_path / "t", author="a", name="b",
        super_q_repo="ericlewis/super-q",
        git_init=False,
    )
    scaffold(opts)
    build = (tmp_path / "t/.github/workflows/build.yml").read_text()
    release = (tmp_path / "t/.github/workflows/release.yml").read_text()
    assert "uses: ericlewis/super-q/.github/workflows/reusable-build.yml" in build
    assert "uses: ericlewis/super-q/.github/workflows/reusable-release.yml" in release
    assert "super-q/super-q" not in build
    assert "super-q/super-q" not in release


def test_multiple_platforms(tmp_path: Path):
    opts = InitOptions(
        target=tmp_path / "t", author="a", name="multi",
        platform_ids=["foo", "bar"], git_init=False,
    )
    scaffold(opts)
    core = json.loads((tmp_path / "t/dist/Cores/a.multi/core.json").read_text())
    assert core["core"]["metadata"]["platform_ids"] == ["foo", "bar"]
    assert (tmp_path / "t/dist/Platforms/foo.json").exists()
    assert (tmp_path / "t/dist/Platforms/bar.json").exists()
