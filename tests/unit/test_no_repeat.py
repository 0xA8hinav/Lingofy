import json
from pathlib import Path

from services.dub_worker import gemini_diarize as g


def _seed(path, segs):
    p = g.translated_segs_path(path, "hi")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(segs))
    return path


def test_no_repeat_across_boundary(tmp_path):
    """A 10s sentence spanning an 8s chunk edge must be SPLIT, never repeated whole."""
    wav = str(tmp_path / "x.wav")
    Path(wav).write_bytes(b"RIFF....")  # exists; hash of content is the cache key
    long_turn = " ".join(f"shabd{i}" for i in range(20))
    _seed(wav, [{"text": long_turn, "start": 29.7, "end": 39.4, "speaker": "2"}])
    t1, _ = g.chunk_translated(wav, "hi", 24.0, 32.0)
    t2, _ = g.chunk_translated(wav, "hi", 32.0, 40.0)
    w1, w2 = t1.split(), t2.split()
    assert len(w1) > 0 and len(w2) > 0
    assert len(set(w1) & set(w2)) == 0, f"repeated words across boundary: {set(w1) & set(w2)}"
    assert len(w1) + len(w2) <= 20


def test_short_segments_pass_through_whole(tmp_path):
    wav = str(tmp_path / "y.wav")
    Path(wav).write_bytes(b"RIFF....")
    _seed(wav, [{"text": "namaste doston", "start": 1.0, "end": 2.0, "speaker": "1"}])
    t, s = g.chunk_translated(wav, "hi", 0.0, 8.0)
    assert t == "namaste doston" and s == "1"
