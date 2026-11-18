from pathlib import Path

from services.assembler.assemble import assemble_session


def test_assemble_real_mux(tmp_path):
    from services.dub_worker.voices import synthesize

    d = tmp_path / "dub"
    o1 = synthesize("नमस्ते", "hi", "1", str(d))
    o2 = synthesize("आप कैसे हैं", "hi", "2", str(d))
    assert Path(o1["wav_path"]).stat().st_size > 1000 and Path(o2["wav_path"]).stat().st_size > 1000
    m = assemble_session(
        "sess_mux",
        "hi",
        dubbed_dir=str(d),
        out_dir=str(tmp_path / "out"),
        src_video="samples/trimmed-demo-10min.mp4",
    )
    assert Path(m["video_path"]).stat().st_size > 10000
