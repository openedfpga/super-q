from pathlib import Path

from super_q.artifacts import reverse_rbf


def test_reverse_rbf_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "in.rbf"
    dst = tmp_path / "out.rbf_r"
    back = tmp_path / "roundtrip.rbf"
    # Every byte 0..255
    src.write_bytes(bytes(range(256)) * 17)
    n = reverse_rbf(src, dst)
    assert n == 256 * 17
    # Reversing twice should give the original.
    reverse_rbf(dst, back)
    assert back.read_bytes() == src.read_bytes()


def test_reverse_known_byte(tmp_path: Path) -> None:
    src = tmp_path / "a.rbf"
    src.write_bytes(b"\x01\x80\xff\x00")
    dst = tmp_path / "a.rbf_r"
    reverse_rbf(src, dst)
    # 0x01 -> 0x80, 0x80 -> 0x01, 0xff -> 0xff, 0x00 -> 0x00
    assert dst.read_bytes() == b"\x80\x01\xff\x00"
