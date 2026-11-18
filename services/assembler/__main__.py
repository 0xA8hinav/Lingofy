"""Assembler poll loop (container entrypoint).

Watches data/dubbed/<session>/ for finished per-chunk wavs and (re)builds
the per-language manifest + muxed video. The gateway's inline path already
calls assemble_session directly; this loop is the K8s-native equivalent that
keeps running when chunks arrive from any producer.
"""

import time
from pathlib import Path

from services.assembler.assemble import assemble_session

DATA = Path("data/dubbed")
OUT = Path("output")
POLL_S = int(__import__("os").environ.get("ASSEMBLE_POLL_S", "10"))


def main() -> None:
    print(f"[assembler] watching {DATA} every {POLL_S}s", flush=True)
    while True:
        try:
            if DATA.exists():
                for sess in sorted(p for p in DATA.iterdir() if p.is_dir()):
                    wavs = sorted(sess.glob("*.wav"))
                    if not wavs:
                        continue
                    # language from parent convention data/dubbed/<sid>; default hi
                    try:
                        assemble_session(
                            sess.name, "hi", dubbed_dir=str(sess), out_dir=str(OUT / sess.name), chunks_total=len(wavs)
                        )
                    except Exception as e:  # never crash the loop on one session
                        print(f"[assembler] {sess.name}: {e}", flush=True)
        except Exception as e:
            print(f"[assembler] loop: {e}", flush=True)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
