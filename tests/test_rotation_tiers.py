"""
Tests for the rotation tier system in scripts/classify.py.

Verifies the actual semantics of the BBC A/B/C-style rotation:
  HEAVY  : grace period (new) OR proven (plays/day above expected)
  MEDIUM : post-grace average performance
  LIGHT  : below-average performance, fading out
"""
from __future__ import annotations

import time

import pytest

from classify import compute_rotation_tier, tier_filter_dayparts, TIER_RANK  # type: ignore[import-not-found]
from config import (  # type: ignore[import-not-found]
    DaypartSegment, ROTATION, ROTATION_CATEGORIES,
)


# --- compute_rotation_tier ---


def test_new_track_is_heavy_during_grace_period():
    """The whole point of a discovery webradio: new tracks get full exposure."""
    assert compute_rotation_tier(play_count=0, age_days=0) == "HEAVY"
    assert compute_rotation_tier(play_count=0, age_days=1) == "HEAVY"
    assert compute_rotation_tier(play_count=0, age_days=13.9) == "HEAVY"


def test_grace_period_ends_at_grace_period_days():
    g = ROTATION_CATEGORIES.grace_period_days
    # Just inside grace → HEAVY regardless of plays
    assert compute_rotation_tier(play_count=0, age_days=g - 0.01) == "HEAVY"
    # Just past grace, no plays accumulated → LIGHT (below-average performer)
    assert compute_rotation_tier(play_count=0, age_days=g + 0.01) == "LIGHT"


def test_post_grace_proven_hit_is_heavy():
    """A track playing well above library average stays HEAVY."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    ratio = ROTATION_CATEGORIES.heavy_above_average_ratio
    age = 30
    # Plays well above the heavy threshold
    plays = int(age * expected * ratio * 1.5)
    assert compute_rotation_tier(play_count=plays, age_days=age) == "HEAVY"


def test_post_grace_average_is_medium():
    """Tracks performing around the library mean sit in MEDIUM."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    light = ROTATION_CATEGORIES.light_below_average_ratio
    heavy = ROTATION_CATEGORIES.heavy_above_average_ratio
    age = 25
    # Plays right in the middle of the band
    target_rate = expected * (light + heavy) / 2
    plays = int(age * target_rate)
    assert compute_rotation_tier(play_count=plays, age_days=age) == "MEDIUM"


def test_post_grace_underperformer_is_light():
    """Tracks well below average fade out."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    ratio = ROTATION_CATEGORIES.light_below_average_ratio
    age = 40
    # Plays well below the LIGHT floor
    plays = int(age * expected * ratio * 0.4)
    assert compute_rotation_tier(play_count=plays, age_days=age) == "LIGHT"


def test_zero_plays_post_grace_is_light():
    """A track that has been around for a long time but no one plays it → LIGHT."""
    assert compute_rotation_tier(play_count=0, age_days=50) == "LIGHT"


@pytest.mark.parametrize("plays,age,expected_tier", [
    # (plays, age_days, expected_tier)
    (0,    0,    "HEAVY"),     # just uploaded
    (5,    7,    "HEAVY"),     # 1 week in, grace
    (15,   14,   "HEAVY"),     # just past grace, plays/day = 1.07 > 0.78
    (5,    14.01, "LIGHT"),    # just past grace, plays/day = 0.36 < 0.39
    (12,   20,   "MEDIUM"),    # plays/day = 0.6, in band
    (30,   20,   "HEAVY"),     # plays/day = 1.5, well above
    (3,    30,   "LIGHT"),     # plays/day = 0.1, well below
])
def test_tier_table(plays, age, expected_tier):
    assert compute_rotation_tier(plays, age) == expected_tier


# --- tier_filter_dayparts ---


def test_heavy_keeps_all_dayparts():
    all_dps = [
        DaypartSegment.DAWN,
        DaypartSegment.DAY,
        DaypartSegment.DUSK,
        DaypartSegment.NIGHT,
    ]
    assert tier_filter_dayparts(all_dps, "HEAVY") == all_dps


def test_medium_keeps_first_n():
    all_dps = [
        DaypartSegment.DAWN,
        DaypartSegment.DAY,
        DaypartSegment.DUSK,
        DaypartSegment.NIGHT,
    ]
    n = ROTATION_CATEGORIES.medium_daypart_count
    assert tier_filter_dayparts(all_dps, "MEDIUM") == all_dps[:n]


def test_light_keeps_first_one_by_default():
    all_dps = [
        DaypartSegment.DAY,
        DaypartSegment.DUSK,
        DaypartSegment.NIGHT,
    ]
    n = ROTATION_CATEGORIES.light_daypart_count
    result = tier_filter_dayparts(all_dps, "LIGHT")
    assert len(result) == n
    assert result == all_dps[:n]


def test_legacy_discovery_label_treated_as_light():
    """Legacy DBs may have 'DISCOVERY' as tier value — treat as LIGHT."""
    all_dps = [DaypartSegment.DAY, DaypartSegment.DUSK, DaypartSegment.NIGHT]
    assert tier_filter_dayparts(all_dps, "DISCOVERY") == all_dps[: ROTATION_CATEGORIES.light_daypart_count]


def test_tier_rank_ordering():
    """LIGHT < MEDIUM < HEAVY, and legacy DISCOVERY aliases LIGHT (rank 0)."""
    assert TIER_RANK["LIGHT"] < TIER_RANK["MEDIUM"] < TIER_RANK["HEAVY"]
    assert TIER_RANK.get("DISCOVERY") == 0


# --- math sanity: the system actually creates differentiation ---


def test_average_track_after_2_months_is_medium_at_least():
    """A track at the library mean rate should land in MEDIUM or better."""
    expected = ROTATION_CATEGORIES.expected_plays_per_day
    age = 60
    plays = int(age * expected)  # exactly average
    result = compute_rotation_tier(plays, age)
    assert result in ("MEDIUM", "HEAVY")


def test_disabled_config_returns_all_dayparts():
    """Sanity: if ROTATION_CATEGORIES is disabled, every tier keeps all dayparts."""
    import importlib
    cfg = importlib.import_module("config")
    original = cfg.ROTATION_CATEGORIES.enabled
    try:
        cfg.ROTATION_CATEGORIES.enabled = False
        dps = [DaypartSegment.DAY, DaypartSegment.DUSK]
        assert tier_filter_dayparts(dps, "LIGHT") == dps
        assert tier_filter_dayparts(dps, "MEDIUM") == dps
    finally:
        cfg.ROTATION_CATEGORIES.enabled = original


# --- Discovery/Library weighted playlist variants (2026-07) ---


class TestPlaylistNameForTier:
    """HEAVY (grace + proven) plays from the high-weight Discovery variant;
    MEDIUM/LIGHT/GOLD play from the low-weight Library (base) playlist."""

    def test_heavy_maps_to_discovery_variant(self):
        from config import playlist_name_for_tier
        assert playlist_name_for_tier(DaypartSegment.DAWN, "HEAVY") == "Dawn-Discovery"

    def test_non_heavy_tiers_map_to_base_playlist(self):
        from config import playlist_name_for_tier
        for tier in ("MEDIUM", "LIGHT", "GOLD", "DISCOVERY"):
            assert playlist_name_for_tier(DaypartSegment.NIGHT, tier) == "Night"

    def test_all_playlist_names_includes_both_variants(self):
        from config import get_all_playlist_names, get_enabled_dayparts
        names = get_all_playlist_names()
        for dp in get_enabled_dayparts():
            assert dp.value in names
            assert f"{dp.value}-Discovery" in names
        assert len(names) == 2 * len(get_enabled_dayparts())

    def test_discovery_weight_higher_than_library(self):
        assert ROTATION_CATEGORIES.discovery_weight > ROTATION_CATEGORIES.library_weight


class TestTargetPlaylistNames:
    """target_playlist_names is the single source of truth for
    mood+tier → AzuraCast playlist names (upload, re-tier, GOLD,
    zero-play remediation, reanalysis all delegate to it)."""

    def test_heavy_gets_discovery_variants_of_all_mood_dayparts(self):
        from classify import target_playlist_names
        from config import get_dayparts_for_mood
        names = target_playlist_names("Calm", "HEAVY")
        expected = [f"{dp.value}-Discovery" for dp in get_dayparts_for_mood("Calm")]
        assert names == expected

    def test_medium_gets_base_playlists_capped_at_medium_count(self):
        from classify import target_playlist_names
        names = target_playlist_names("Calm", "MEDIUM")
        assert len(names) <= ROTATION_CATEGORIES.medium_daypart_count
        assert all("-Discovery" not in n for n in names)

    def test_legacy_discovery_tier_behaves_like_light(self):
        from classify import target_playlist_names
        assert target_playlist_names("Calm", "DISCOVERY") == \
               target_playlist_names("Calm", "LIGHT")

    def test_gold_behaves_like_light_on_base_playlists(self):
        from classify import target_playlist_names
        names = target_playlist_names("Sad", "GOLD")
        assert len(names) <= ROTATION_CATEGORIES.light_daypart_count
        assert all("-Discovery" not in n for n in names)



def test_rotation_clears_ghost_rows(tmp_path, monkeypatch):
    """Une ligne active absente d'AzuraCast ne survit pas à la rotation.

    Verrouille le câblage de reconcile() DANS enforce_tiered_rotation :
    sans l'appel, ce test échoue.
    """
    import audio_embeddings
    import classify
    import library_state
    from classify import enforce_tiered_rotation
    from track_db import TrackDB

    class _FakeClient:
        """Station minimale : un seul fichier vivant, aucun historique."""

        def get_all_files(self):
            return [{"id": 1, "artist": "Vivant", "title": "Y",
                     "uploaded_at": time.time()}]

        def get_history_since(self, since):
            return []

        def get_playlists_map(self):
            return {}

        def assign_playlists(self, file_id, playlist_ids):
            return True

        def delete_file(self, file_id):
            return True

    # enforce_tiered_rotation vise le data/ du dépôt pour deux effets de bord
    # sans rapport avec ce qu'on teste : le rapport de réconciliation, et le
    # prune du store CLAP. Ce dernier est destructif — il ne garderait que les
    # clés de la base jouet et réécrirait embeddings.npy. On neutralise.
    monkeypatch.setattr(classify, "RECONCILE_REPORT_PATH",
                        tmp_path / "last_reconcile.json")
    # Station jouet à deux lignes : le plancher de vraisemblance de la
    # réconciliation (50 fichiers) refuserait le scénario.
    monkeypatch.setattr(library_state, "RECONCILE_MIN_FILES", 0)
    monkeypatch.setattr(library_state, "RECONCILE_MIN_RATIO", 0.0)

    class _NoopStore:
        def __init__(self, data_dir):
            pass

        def prune(self, valid_keys):
            return 0

    monkeypatch.setattr(audio_embeddings, "EmbeddingStore", _NoopStore)

    db = TrackDB(tmp_path / "t.db")
    db.record_upload("fantome - x", "Fantome", "X", file_id=999)
    db.record_upload("vivant - y", "Vivant", "Y", file_id=1)

    enforce_tiered_rotation(_FakeClient(), db, new_tracks_count=0)

    assert [t["track_key"] for t in db.get_active_tracks()] == ["vivant - y"]
    db.close()


# --- Famine : gel réseau et plafond de suppressions ---------------------------
# Ajoutés après l'incident du 1er-18 août 2026. Une coupure DNS à 03:00
# (rafraîchissement du snap Docker) a privé le pipeline d'acquisition 18 nuits
# de suite pendant que la rotation continuait de purger : 626 morceaux tombés à
# 333 en une nuit de rattrapage, dont 31 likés par Victor.
#
# Ces deux tests appellent réellement enforce_tiered_rotation et comptent les
# suppressions : retirer un garde-fou les fait échouer.


def _station_expiree(nb):
    """Monte une station jouet dont tous les morceaux sont EXPIRED."""
    vieux = time.time() - (ROTATION.max_age_days + 50) * 86400
    return [{"id": i, "artist": f"A{i}", "title": f"T{i}", "uploaded_at": vieux}
            for i in range(1, nb + 1)]


def _rotation_jouet(tmp_path, monkeypatch, nb_morceaux):
    """Câblage commun : client factice traçant, base jouet, effets de bord neutralisés."""
    import audio_embeddings
    import classify
    import library_state
    from track_db import TrackDB

    supprimes = []
    fichiers = _station_expiree(nb_morceaux)

    class _FakeClient:
        def get_all_files(self):
            return fichiers

        def get_history_since(self, since):
            return []

        def get_playlists_map(self):
            return {}

        def assign_playlists(self, file_id, playlist_ids):
            return True

        def delete_file(self, file_id):
            supprimes.append(file_id)
            return True

    monkeypatch.setattr(classify, "RECONCILE_REPORT_PATH", tmp_path / "r.json")
    monkeypatch.setattr(library_state, "RECONCILE_MIN_FILES", 0)
    monkeypatch.setattr(library_state, "RECONCILE_MIN_RATIO", 0.0)

    class _NoopStore:
        def __init__(self, data_dir):
            pass

        def prune(self, valid_keys):
            return 0

    monkeypatch.setattr(audio_embeddings, "EmbeddingStore", _NoopStore)

    db = TrackDB(tmp_path / "t.db")
    vieux = time.time() - (ROTATION.max_age_days + 50) * 86400
    for f in fichiers:
        cle = f"{f['artist'].lower()} - {f['title'].lower()}"
        db.record_upload(cle, f["artist"], f["title"], file_id=f["id"])
        # record_upload horodate à maintenant : on vieillit la ligne pour
        # qu'elle tombe réellement dans EXPIRED.
        db.conn.execute("UPDATE tracks SET uploaded_at=? WHERE track_key=?", (vieux, cle))
    db.conn.commit()
    return _FakeClient(), db, supprimes


def test_gel_reseau_ne_supprime_rien(tmp_path, monkeypatch):
    """Sans réseau, aucune suppression — même avec une station 100 % expirée."""
    from classify import enforce_tiered_rotation

    monkeypatch.setenv("PIPELINE_NETWORK_DOWN", "1")
    client, db, supprimes = _rotation_jouet(tmp_path, monkeypatch, 20)
    enforce_tiered_rotation(client, db, new_tracks_count=0)
    assert supprimes == []
    db.close()


def test_plafond_limite_la_purge_de_rattrapage(tmp_path, monkeypatch):
    """Réseau OK : la purge ne peut pas dépasser max_deletions_per_night.

    C'est ce qui empêche une famine de se solder par un massacre à la reprise.
    """
    from classify import enforce_tiered_rotation

    monkeypatch.delenv("PIPELINE_NETWORK_DOWN", raising=False)
    client, db, supprimes = _rotation_jouet(tmp_path, monkeypatch, 60)
    enforce_tiered_rotation(client, db, new_tracks_count=0)
    assert len(supprimes) <= ROTATION.max_deletions_per_night
    assert len(supprimes) > 0, "sans gel, la rotation doit tout de même travailler"
    db.close()
