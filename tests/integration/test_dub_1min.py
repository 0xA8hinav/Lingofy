from services.dub_worker.voices import synthesize, translate


def test_dub_1min_mock(tmp_path):
    t = translate("hello world", "en", "hi")
    assert t["target_text"] and t["chars_billed"] >= 0
    out = synthesize(t["target_text"], "hi", "2", str(tmp_path))
    assert out["provider"] in ("piper", "sarvam")
