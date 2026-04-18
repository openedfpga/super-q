from pathlib import Path

import pytest

from super_q.project import CoreDetectionError, detect_core, find_cores


@pytest.fixture
def fake_core(tmp_path: Path) -> Path:
    """Minimal Pocket-shaped directory with a src/fpga/.qpf."""
    root = tmp_path / "author.myname"
    fpga = root / "src" / "fpga"
    fpga.mkdir(parents=True)
    (fpga / "pocket.qpf").write_text("QUARTUS_VERSION = 24.1\nPROJECT_REVISION = pocket\n")
    (fpga / "pocket.qsf").write_text(
        "set_global_assignment -name DEVICE 5CEBA4F23C8\n"
        "set_global_assignment -name TOP_LEVEL_ENTITY top\n"
    )
    (fpga / "pocket.sdc").write_text("create_clock -name clk -period 13.468 [get_ports clk]\n")
    (root / "dist").mkdir()
    return root


def test_detect_simple(fake_core: Path):
    core = detect_core(fake_core)
    assert core.project_name == "pocket"
    assert core.device == "5CEBA4F23C8"
    assert core.qsf and core.qsf.exists()
    assert core.sdc_files
    assert core.full_name == "author.myname"


def test_detect_missing(tmp_path: Path):
    with pytest.raises(CoreDetectionError):
        detect_core(tmp_path)


def test_find_cores_over_parent(fake_core: Path):
    cores = find_cores([fake_core.parent])
    assert len(cores) == 1
    assert cores[0].project_name == "pocket"
