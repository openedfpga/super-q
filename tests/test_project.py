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


def test_author_name_from_dist_cores(tmp_path: Path):
    """Repo dirname doesn't follow Author.Name but dist/Cores/ does."""
    root = tmp_path / "openFPGA-Popeye"
    (root / "src" / "fpga").mkdir(parents=True)
    (root / "src" / "fpga" / "ap_core.qpf").write_text("")
    (root / "dist" / "Cores" / "ericlewis.Popeye").mkdir(parents=True)

    core = detect_core(root)
    assert core.author == "ericlewis"
    assert core.core_name == "Popeye"
    assert core.full_name == "ericlewis.Popeye"
    assert core.project_name == "ap_core"


def test_sdc_walks_subdirs(tmp_path: Path):
    """SDC files in common Pocket subdirs (apf/, core/) should be found."""
    root = tmp_path / "proj"
    fpga = root / "src" / "fpga"
    (fpga / "apf").mkdir(parents=True)
    (fpga / "core").mkdir()
    (fpga / "ap_core.qpf").write_text("")
    (fpga / "apf" / "apf_constraints.sdc").write_text("")
    (fpga / "core" / "core_constraints.sdc").write_text("")
    core = detect_core(root)
    names = sorted(p.name for p in core.sdc_files)
    assert names == ["apf_constraints.sdc", "core_constraints.sdc"]
