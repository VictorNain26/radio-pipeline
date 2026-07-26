"""Filtrage à froid et budget : rien ne doit être téléchargé inutilement."""

import pytest

from track_db import TrackDB


@pytest.fixture
def db(tmp_path):
    d = TrackDB(tmp_path / "t.db")
    yield d
    d.close()


def _track(artist, title, source="rss"):
    return {"id": f"{artist}-{title}", "artist": artist, "title": title,
            "cover": None, "search": f"{artist} - {title}", "source": source}


def test_budget_is_zero_when_carryover_covers_quota():
    from download import compute_budget
    # 24 fichiers en attente pour un quota de 6 : rien à télécharger.
    assert compute_budget(24) == 0


def test_budget_is_full_when_nothing_on_disk():
    from config import ROTATION
    from download import compute_budget
    assert compute_budget(0) == int(ROTATION.max_uploads_per_night * ROTATION.download_margin)


def test_budget_is_partial():
    from config import ROTATION
    from download import compute_budget
    full = int(ROTATION.max_uploads_per_night * ROTATION.download_margin)
    assert compute_budget(full - 3) == 3


def test_budget_never_negative():
    from download import compute_budget
    assert compute_budget(10_000) == 0


def test_prefilter_drops_tracks_already_in_library(db):
    from download import prefilter_candidates

    survivors, counts = prefilter_candidates(
        [_track("Deja", "Vu"), _track("Nouveau", "Titre")],
        library_keys={"deja - vu"},
        track_db=db,
        genre_client=None,
    )

    assert [t["title"] for t in survivors] == ["Titre"]
    assert counts["already_in_library"] == 1


def test_prefilter_counts_intra_batch_duplicates_separately(db):
    """Listé deux fois ce soir n'est pas la même chose que déjà à l'antenne."""
    from download import prefilter_candidates

    survivors, counts = prefilter_candidates(
        [_track("Double", "Mise"), _track("Double", "Mise")],
        library_keys=set(),
        track_db=db,
        genre_client=None,
    )

    assert len(survivors) == 1
    assert counts["duplicate_in_batch"] == 1
    assert counts["already_in_library"] == 0


def test_prefilter_drops_tracks_with_active_verdict(db):
    from download import prefilter_candidates

    db.record_verdict("juge - deja", "rejected_taste", score=0.41)

    survivors, counts = prefilter_candidates(
        [_track("Juge", "Deja"), _track("Nouveau", "Titre")],
        library_keys=set(),
        track_db=db,
        genre_client=None,
    )

    assert [t["title"] for t in survivors] == ["Titre"]
    assert counts["known_verdict"] == 1


def test_prefilter_lets_expired_taste_verdict_through(db):
    import time

    from download import prefilter_candidates

    db.record_verdict("juge - jadis", "rejected_taste", score=0.41)
    db.conn.execute(
        "UPDATE verdicts SET decided_at = ? WHERE track_key = ?",
        (time.time() - 200 * 86400, "juge - jadis"),
    )
    db.conn.commit()

    survivors, counts = prefilter_candidates(
        [_track("Juge", "Jadis")], library_keys=set(), track_db=db, genre_client=None,
    )

    assert len(survivors) == 1
    assert counts["known_verdict"] == 0


def test_prefilter_drops_tracks_in_cooldown(db):
    from download import prefilter_candidates

    db.record_upload("recent - supprime", "Recent", "Supprime", file_id=5)
    db.record_deletion("recent - supprime")

    survivors, counts = prefilter_candidates(
        [_track("Recent", "Supprime")], library_keys=set(), track_db=db, genre_client=None,
    )

    assert survivors == []
    assert counts["cooldown"] == 1


def test_prefilter_orders_by_source_priority(db):
    """Les libellés sont ceux que discovery_sources écrit réellement."""
    from download import prefilter_candidates

    survivors, _ = prefilter_candidates(
        [
            _track("A", "Un", source="lastfm:post-punk"),
            _track("B", "Deux", source="manual"),
            _track("C", "Trois", source="personal"),
            _track("D", "Quatre", source="hypem"),
            _track("E", "Cinq", source="Le Grand Mix"),  # label RSS libre
        ],
        library_keys=set(), track_db=db, genre_client=None,
    )

    assert [t["title"] for t in survivors] == [
        "Deux",    # manual   0
        "Trois",   # personal 1
        "Cinq",    # RSS      3 (défaut)
        "Quatre",  # hypem    4
        "Un",      # lastfm:  5
    ]


def test_prefilter_is_stable_within_a_source(db):
    from download import prefilter_candidates

    tracks = [_track("A", str(i), source="rss") for i in range(5)]
    survivors, _ = prefilter_candidates(
        tracks, library_keys=set(), track_db=db, genre_client=None,
    )
    assert [t["title"] for t in survivors] == [str(i) for i in range(5)]


def test_prefilter_records_verdict_for_blocked_genre(db):
    from download import prefilter_candidates

    class _Blocking:
        def check_genre(self, artist, title):
            class R:
                tags = ["power metal"]
                top_tag = "power metal"
                is_blocked = True
                blocked_reason = "power metal"
            return R()

    survivors, counts = prefilter_candidates(
        [_track("Metal", "Band")], library_keys=set(), track_db=db,
        genre_client=_Blocking(),
    )

    assert survivors == []
    assert counts["blocked_genre"] == 1
    assert db.get_verdict("metal - band")["verdict"] == "blocked_genre"
