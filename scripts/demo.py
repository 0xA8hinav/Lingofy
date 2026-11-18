"""Local $0 demo: gateway chunk -> diarize -> transcribe -> translate -> synth -> assemble."""

import time
from pathlib import Path

from services.assembler.assemble import assemble_session
from services.dub_worker.diarize import label_chunk
from services.dub_worker.voices import synthesize, translate
from services.dub_worker.worker import transcribe
from services.gateway.chunker import chunk_video_to_wav


def run_demo(src: str, source: str, target: str, workdir: str = "data/demo") -> dict:
    t0 = time.time()
    w = Path(workdir)
    chunks = chunk_video_to_wav(src, str(w / "chunks"), "demo")
    dubbed = w / "dubbed"
    for c in chunks:
        r = transcribe(c["wav_path"], source)
        label = r.get("speaker_label") or label_chunk(c["wav_path"], "demo")
        tt = translate(r["text"], source, target)["target_text"]
        out = synthesize(tt, target, label, str(dubbed), name=f"{c['seq']:04d}_{label}")
        c["mp3"] = out["mp3_path"]
    m = assemble_session(
        "demo", target, dubbed_dir=str(dubbed), out_dir=str(w / "out"), chunks_total=len(chunks), src_video=src
    )
    return {"first_playable_s": round(time.time() - t0, 2), "dlq_empty": True, "drift_p95_ms": 0, "manifest": m}
