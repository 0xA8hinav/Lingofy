"""Cost ledger: track paid API usage in INR. Append-only JSONL."""

import json
import time
from pathlib import Path

LEDGER = Path("data/cost_ledger.jsonl")
USDINR = 94.0
# Sarvam list price for bulbul:v4-flash, pay-as-you-go. Shown as an ESTIMATE
# (dashboard labels it accordingly) since plan discounts/credits vary.
SARVAM_INR_PER_1K_CHARS = 1.0


def record(provider: str, audio_s: float, meta: str = "") -> dict:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "provider": provider,
        "audio_s": round(audio_s, 2),
        "usd": 0.0,
        "inr": 0.0,
        "meta": meta,
    }
    with open(LEDGER, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def totals() -> dict:
    sarvam_chars = 0
    m = 0
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("provider") == "sarvam":
                try:
                    sarvam_chars += int(str(e.get("meta", "0")).split()[0])
                except Exception:
                    pass
                m += 1
    sarvam_inr = sarvam_chars / 1000 * SARVAM_INR_PER_1K_CHARS
    return {
        "rate_usdinr": USDINR,
        "piper_inr": 0.0,
        "translate_inr": 0.0,
        "sarvam_calls": m,
        "sarvam_chars": sarvam_chars,
        "sarvam_inr_est": round(sarvam_inr, 2),
    }
