from services.gateway.chunker import chunk_video_to_wav


def test_chunker_20s_basic(tmp_path):
    chunks = chunk_video_to_wav("tests/fixtures/tone_16s.wav", str(tmp_path), "sess_1")
    assert 1 <= len(chunks) <= 3
    assert chunks == sorted(chunks, key=lambda c: c["seq"])
    assert all(c["end_ms"] - c["start_ms"] <= 21000 for c in chunks)
    assert all(c["chunk_id"].startswith("sess_1_") for c in chunks)
