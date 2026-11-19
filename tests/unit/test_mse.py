from services.assembler.assemble import init_codec_string, split_fmp4


def _box(t, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + t + payload


def test_split_and_codec(tmp_path):
    avcc = b"\x01\x64\x00\x1f" + b"\x00" * 8  # profile 0x64 compat 0x00 level 0x1f
    init = _box(b"ftyp", b"iso5") + _box(b"moov", _box(b"avcC", avcc))
    media = _box(b"moof", b"\x00" * 100) + _box(b"mdat", b"\x00" * 1000)
    f = tmp_path / "seg.mp4"
    f.write_bytes(init + media + _box(b"mfra", b"\x00" * 10))
    i, m = split_fmp4(str(f))
    assert i == init
    assert m == media
    assert init_codec_string(i) == "avc1.64001F,mp4a.40.2"
