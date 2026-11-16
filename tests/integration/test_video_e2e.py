from scripts.demo import run_demo


def test_10min_e2e_first_playable_under_10s(tmp_path):
    result = run_demo("tests/fixtures/tone_16s.wav", source="en", target="hi", workdir=str(tmp_path))
    assert result["first_playable_s"] <= 10
    assert result["dlq_empty"] is True
    assert result["drift_p95_ms"] <= 250
