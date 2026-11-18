"""Cost ledger: track paid API usage in INR. Append-only JSONL."""

import json
import time
from pathlib import Path

LEDGER = Path("data/cost_ledger.jsonl")
USDINR = 94.0
CHIRP_USD_PER_MIN = 0.016


def record(provider: str, audio_s: float, meta: str = "") -> dict:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    usd = (audio_s / 60) * CHIRP_USD_PER_MIN if provider == "chirp" else 0.0
    entry = {
        "ts": time.time(),
        "provider": provider,
        "audio_s": round(audio_s, 2),
        "usd": round(usd, 6),
        "inr": round(usd * USDINR, 4),
        "meta": meta,
    }
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def totals() -> dict:
    chirp_s = 0.0
    n = 0
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("provider") == "chirp":
                chirp_s += e.get("audio_s", 0)
                n += 1
    usd = (chirp_s / 60) * CHIRP_USD_PER_MIN
    return {
        "chirp_calls": n,
        "chirp_audio_s": round(chirp_s, 1),
        "chirp_audio_min": round(chirp_s / 60, 2),
        "chirp_usd": round(usd, 4),
        "chirp_inr": round(usd * USDINR, 2),
        "rate_usdinr": USDINR,
        "piper_inr": 0.0,
        "translate_inr": 0.0,
    }
