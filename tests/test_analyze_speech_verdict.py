"""Un rejet pour parole doit laisser une trace, sinon le podcast revient."""

from track_db import TrackDB, normalize_track_key


def test_speech_rejection_is_recorded(tmp_path):
    from analyze import record_speech_rejection

    db = TrackDB(tmp_path / "t.db")
    record_speech_rejection(db, "Le Podcast", "Épisode 12", 0.91)

    key = normalize_track_key("Le Podcast", "Épisode 12")
    v = db.get_verdict(key)
    assert v["verdict"] == "rejected_speech"
    assert v["score"] == 0.91
    db.close()


def test_speech_verdict_is_permanent(tmp_path):
    from analyze import record_speech_rejection

    db = TrackDB(tmp_path / "t.db")
    record_speech_rejection(db, "Le Podcast", "Épisode 12", 0.91)

    key = normalize_track_key("Le Podcast", "Épisode 12")
    # ttl à 0 : seuls les verdicts périssables tomberaient.
    assert db.has_active_verdict(key, 0) is True
    db.close()


def test_missing_identity_records_nothing(tmp_path):
    from analyze import record_speech_rejection

    db = TrackDB(tmp_path / "t.db")
    record_speech_rejection(db, "", "", 0.91)
    assert db.get_verdict(" - ") is None
    db.close()


def test_no_db_is_a_noop(tmp_path):
    from analyze import record_speech_rejection

    # Ne doit pas lever : analyze.py doit rester utilisable sans base.
    record_speech_rejection(None, "Le Podcast", "Épisode 12", 0.91)
