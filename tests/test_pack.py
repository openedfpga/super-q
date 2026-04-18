import json
import zipfile
from pathlib import Path

import pytest

from super_q.pack import PackError, detect_full_name, infer_version, pack


@pytest.fixture
def fake_core(tmp_path: Path) -> Path:
    """A minimal Pocket core: dist/Cores/<Author>.<Name>/ + core.json."""
    root = tmp_path / "alice.tile-matcher"
    cores = root / "dist" / "Cores" / "alice.tile-matcher"
    cores.mkdir(parents=True)
    (cores / "core.json").write_text(json.dumps({
        "core": {"metadata": {"version": "0.0.0", "date_release": "2024-01-01"}}
    }))
    (cores / "audio.json").write_text("{}")
    (cores / "video.json").write_text("{}")
    platforms = root / "dist" / "Platforms"
    platforms.mkdir()
    (platforms / "tilemaster.json").write_text("{}")
    return root


def test_detect_full_name_from_dist(fake_core: Path):
    assert detect_full_name(fake_core) == "alice.tile-matcher"


def test_detect_full_name_override_rejects_bad(fake_core: Path):
    with pytest.raises(PackError):
        detect_full_name(fake_core, override="bogus")


def test_infer_version_falls_back_to_core_json(fake_core: Path):
    # No git tag, no git — should fall through to core.json.
    assert infer_version(fake_core) in {"0.0.0", "dev"}  # "dev" if git isn't present


def test_pack_produces_valid_zip(fake_core: Path, tmp_path: Path):
    rbf_r = tmp_path / "bitstream.rbf_r"
    rbf_r.write_bytes(b"\x00" * 1024)      # fake bitstream

    result = pack(fake_core, rbf_r, out_dir=tmp_path / "release", version="0.3.0")

    assert result.zip_path.exists()
    assert result.version == "0.3.0"
    assert result.full_name == "alice.tile-matcher"
    assert result.bytes > 0

    with zipfile.ZipFile(result.zip_path) as zf:
        names = zf.namelist()
        # Every path should live under <full_name>/.
        assert all(n.startswith("alice.tile-matcher/") for n in names)
        # The bitstream made it in.
        assert "alice.tile-matcher/Cores/alice.tile-matcher/bitstream.rbf_r" in names
        # core.json was stamped with the new version.
        with zf.open("alice.tile-matcher/Cores/alice.tile-matcher/core.json") as fh:
            data = json.loads(fh.read())
            assert data["core"]["metadata"]["version"] == "0.3.0"


def test_pack_errors_without_dist(tmp_path: Path):
    core = tmp_path / "empty"
    core.mkdir()
    with pytest.raises(PackError):
        pack(core, tmp_path / "missing.rbf_r", out_dir=tmp_path / "out", name_override="a.b")


def test_pack_zip_filename(fake_core: Path, tmp_path: Path):
    rbf_r = tmp_path / "x.rbf_r"
    rbf_r.write_bytes(b"xx")
    result = pack(fake_core, rbf_r, out_dir=tmp_path / "rel", version="1.2.3")
    assert result.zip_path.name == "alice.tile-matcher_1.2.3.zip"
