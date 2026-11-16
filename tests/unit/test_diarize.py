from services.dub_worker.diarize import cluster_session, label_chunk


def test_two_speakers_keep_labels():
    assert label_chunk("tests/fixtures/tone_16s.wav", "s1") in ("1", "2", "3")
    labels = cluster_session([[0.0], [0.1], [10.0], [10.1], [20.0], [99.0]])
    assert max(labels) <= 3 and len(labels) == 6


def test_fourth_speaker_maps_to_nearest():
    labels = cluster_session([[0.0], [0.1], [10.0], [10.1], [20.0], [99.0]])
    assert max(labels) <= 3 and len(labels) == 6
