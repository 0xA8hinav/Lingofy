from services.dub_worker.diarize import classify_gender, gender_for_wav
from services.dub_worker.voices import voice_for


def test_gender_splits_male_female():
    assert gender_for_wav("tests/fixtures/voices/female_hi.wav") == "female"
    assert classify_gender(100.0) == "male"
    assert classify_gender(250.0) == "female"
    assert classify_gender(0.0) == "unknown"


def test_gender_nn_both_voices():
    from services.dub_worker.diarize import classify_gender_nn

    assert classify_gender_nn("tests/fixtures/voices/female_hi.wav") == "female"
    assert classify_gender_nn("tests/fixtures/voices/male_en.wav") == "male"


def test_gender_matched_voices():
    assert voice_for("9", "en", "male", 0) == "en_US-ryan-medium"
    assert voice_for("9", "en", "female", 0) == "en_US-lessac-medium"
    assert voice_for("9", "hi", "male", 0) == "hi_IN-pratham-medium"
    assert voice_for("9", "hi", "female", 0) == "hi_IN-priyamvada-medium"
    # unknown gender falls back to label map
    assert voice_for("1", "en", "unknown", 0) == voice_for("1", "en")
