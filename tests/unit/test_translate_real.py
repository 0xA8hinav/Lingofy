import pytest

from services.dub_worker.voices import translate

pytest.importorskip("argostranslate")


def _is_devanagari(s: str) -> bool:
    return any("\u0900" <= ch <= "\u097f" for ch in s)


def test_translate_real_en_hi():
    r = translate("Hello, how are you?", "en", "hi")
    assert _is_devanagari(r["target_text"])
    # colloquial + length-matched: short input -> short output, no essay
    assert len(r["target_text"].split()) <= 8
    assert r["chars_billed"] >= 0
