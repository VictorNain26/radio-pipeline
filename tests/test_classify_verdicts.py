"""Un rejet doit laisser une trace exploitable par download.py."""

import pytest

from track_db import TrackDB


# ---------------------------------------------------------------------------
# Le point d'entrée unique
# ---------------------------------------------------------------------------

def test_taste_rejection_is_recorded_with_score(tmp_path):
    from classify import record_rejection

    db = TrackDB(tmp_path / "t.db")
    record_rejection(db, "artiste - titre", "rejected_taste", "0.41 < 0.62", 0.41)

    v = db.get_verdict("artiste - titre")
    assert v["verdict"] == "rejected_taste"
    assert v["score"] == 0.41
    assert db.has_active_verdict("artiste - titre", 90) is True
    db.close()


def test_speech_rejection_never_expires(tmp_path):
    from classify import record_rejection

    db = TrackDB(tmp_path / "t.db")
    record_rejection(db, "podcast - episode", "rejected_speech", "voix détectée")

    assert db.has_active_verdict("podcast - episode", 0) is True
    db.close()


def test_rejection_without_key_records_nothing(tmp_path):
    from classify import record_rejection

    db = TrackDB(tmp_path / "t.db")
    record_rejection(db, "", "rejected_taste", "pas de clé")
    assert db.get_verdict("") is None
    db.close()


def test_rejection_without_db_is_a_no_op():
    """process_track accepte track_db=None : le funnel doit l'absorber."""
    from classify import record_rejection

    record_rejection(None, "artiste - titre", "rejected_taste", "pas de base")


# ---------------------------------------------------------------------------
# Le câblage réel dans process_track
# ---------------------------------------------------------------------------

def _features(**overrides):
    features = {
        "artist": "Artiste",
        "title": "Titre",
        "bpm": 120,
        "mood": "Calm",
        "mood_confidence": 0.9,
        "energy_level": "medium",
        "duration": 200,
        "valence": 0.1,
        "arousal": 0.1,
        "mood_aggressive": 0.0,
        "genre_top": "",
        "genre_top_prob": 0.0,
        "lastfm_tags": "",
    }
    features.update(overrides)
    return features


@pytest.fixture
def track_file(tmp_path):
    """Un fichier bidon : les tags sont fournis par monkeypatch."""
    path = tmp_path / "artiste - titre.mp3"
    path.write_bytes(b"not really an mp3")
    return path


def _run_process_track(monkeypatch, filepath, db, features, taste_score=None):
    import classify

    monkeypatch.setattr(classify, "get_features_from_tags", lambda _p: features)
    monkeypatch.setattr(classify, "_track_key_of_file", lambda _p: "artiste - titre")
    monkeypatch.setattr(classify, "check_taste", lambda _k: taste_score)
    return classify.process_track(filepath, object(), {}, set(), db)


def test_taste_rejection_records_verdict_in_process_track(
    monkeypatch, tmp_path, track_file
):
    import classify

    db = TrackDB(tmp_path / "t.db")
    monkeypatch.setattr(classify.TASTE_FILTER, "log_only", False)

    status, _ = _run_process_track(
        monkeypatch, track_file, db, _features(), taste_score=0.10,
    )

    assert status == "rejected"
    v = db.get_verdict("artiste - titre")
    assert v["verdict"] == "rejected_taste"
    assert v["score"] == pytest.approx(0.10)
    db.close()


def test_multisignal_rejection_records_verdict_in_process_track(
    monkeypatch, tmp_path, track_file
):
    db = TrackDB(tmp_path / "t.db")

    status, _ = _run_process_track(
        monkeypatch, track_file, db,
        _features(mood_aggressive=0.99, genre_top="Rock---Hardcore"),
    )

    assert status == "rejected"
    assert db.get_verdict("artiste - titre")["verdict"] == "rejected_multisignal"
    db.close()


def test_duration_rejection_records_verdict_in_process_track(
    monkeypatch, tmp_path, track_file
):
    db = TrackDB(tmp_path / "t.db")

    status, _ = _run_process_track(
        monkeypatch, track_file, db, _features(duration=12),
    )

    assert status == "rejected"
    assert db.get_verdict("artiste - titre")["verdict"] == "filtered_duration"
    db.close()


def test_config_only_rejection_records_no_verdict(monkeypatch, tmp_path, track_file):
    """Un mood désactivé juge la config du moment, pas le morceau."""
    import classify

    monkeypatch.setattr(classify, "should_reject_track",
                        lambda _f: (True, "mood 'Calm' désactivé"))

    db = TrackDB(tmp_path / "t.db")
    status, _ = _run_process_track(monkeypatch, track_file, db, _features())

    assert status == "rejected"
    assert db.get_verdict("artiste - titre") is None
    db.close()


def test_unreadable_key_records_no_verdict(monkeypatch, tmp_path, track_file):
    """Sans clé, rien à mémoriser : le morceau repassera par le téléchargement."""
    import classify

    monkeypatch.setattr(classify, "get_features_from_tags", lambda _p: _features(duration=12))
    monkeypatch.setattr(classify, "_track_key_of_file", lambda _p: None)

    db = TrackDB(tmp_path / "t.db")
    status, _ = classify.process_track(track_file, object(), {}, set(), db)

    assert status == "rejected"
    assert db.get_verdict("artiste - titre") is None
    db.close()
