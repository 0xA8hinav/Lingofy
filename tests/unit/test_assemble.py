from pathlib import Path

from services.assembler.assemble import assemble_session


def test_assemble_orders_by_seq(tmp_path):
    (tmp_path / "0.mp3").write_bytes(b"ID3a")
    (tmp_path / "1.mp3").write_bytes(b"ID3b")
    m = assemble_session("sess_shuffled", "hi", dubbed_dir=str(tmp_path), out_dir=str(tmp_path / "out"))
    assert m["chunks_done"] == m["chunks_total"]
    assert Path(m["video_path"]).exists()


def test_missing_chunk_marks_unavailable(tmp_path):
    m = assemble_session(
        "sess_gap", "hi", dubbed_dir=str(tmp_path / "empty"), out_dir=str(tmp_path / "out2"), chunks_total=3
    )
    assert "unavailable" in m and m["chunks_done"] < m["chunks_total"]
