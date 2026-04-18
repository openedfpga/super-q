from pathlib import Path

from super_q.init import InitOptions, scaffold


def test_inline_workflow_has_no_uses_reference(tmp_path: Path):
    """--inline must produce self-contained workflows agents can use in
    private repos without cross-repo access grants."""
    opts = InitOptions(
        target=tmp_path / "a.b", author="a", name="b",
        ci_only=True, inline=True, git_init=False,
    )
    scaffold(opts)
    build = (tmp_path / "a.b/.github/workflows/build.yml").read_text()
    release = (tmp_path / "a.b/.github/workflows/release.yml").read_text()

    # The telltale cross-repo reference must not appear.
    assert "uses: openedfpga/super-q" not in build
    assert "uses: openedfpga/super-q" not in release
    assert "raw.githubusercontent.com" not in build
    assert "raw.githubusercontent.com" not in release

    # But the build pieces must still be present.
    for needed in ("actions/checkout", "actions/cache", "superq install-quartus",
                   "superq ci build", "actions/upload-artifact"):
        assert needed in build, f"missing: {needed}"

    for needed in ("softprops/action-gh-release", "superq release pack"):
        assert needed in release, f"missing: {needed}"


def test_inline_custom_pip_target(tmp_path: Path):
    """--super-q-pip lets users point at a private git URL or a PyPI pin."""
    opts = InitOptions(
        target=tmp_path / "a.b", author="a", name="b",
        ci_only=True, inline=True, git_init=False,
        super_q_pip="super-q @ git+https://private.example/my/super-q@v1.2.3",
    )
    scaffold(opts)
    build = (tmp_path / "a.b/.github/workflows/build.yml").read_text()
    assert "private.example/my/super-q@v1.2.3" in build


def test_inline_respects_seeds(tmp_path: Path):
    opts = InitOptions(
        target=tmp_path / "a.b", author="a", name="b",
        ci_only=True, inline=True, git_init=False,
        default_seeds_build="1-4", default_seeds_release="1-64",
    )
    scaffold(opts)
    build = (tmp_path / "a.b/.github/workflows/build.yml").read_text()
    release = (tmp_path / "a.b/.github/workflows/release.yml").read_text()
    assert "1-4" in build
    assert "1-64" in release


def test_reusable_and_inline_produce_different_outputs(tmp_path: Path):
    r = tmp_path / "reusable"
    i = tmp_path / "inline"
    for target, inline in ((r, False), (i, True)):
        scaffold(InitOptions(
            target=target, author="a", name="b",
            ci_only=True, inline=inline, git_init=False,
        ))
    reusable = (r / ".github/workflows/build.yml").read_text()
    inline = (i / ".github/workflows/build.yml").read_text()
    assert "uses: openedfpga/super-q" in reusable
    assert "uses: openedfpga/super-q" not in inline
    # Inline is necessarily larger — it expands everything.
    assert len(inline) > len(reusable) * 3
