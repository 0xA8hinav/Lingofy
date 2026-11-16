from services.dub_worker.voices import translate


def test_translate_real_en_hi():
    r = translate("Hello, how are you?", "en", "hi")
    assert "नमस्ते" in r["target_text"] or "आप" in r["target_text"]
    assert r["chars_billed"] > 0
