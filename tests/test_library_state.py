"""Réconciliation base SQLite ↔ AzuraCast ↔ dossier média."""

import json
import os
import time
from pathlib import Path

import pytest

import library_state
from library_state import (
    LibraryStateError,
    ReconcileReport,
    count_media_files,
    reconcile,
)
from track_db import TrackDB


@pytest.fixture
def db(tmp_path):
    d = TrackDB(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def seuils_desarmes(monkeypatch):
    """Les scénarios de ce module tiennent en quelques titres.

    Le plancher de vraisemblance (50 fichiers) les refuserait tous. Les
    tests qui visent le garde-fou lui-même le réarment par `_armer`.
    """
    monkeypatch.setattr(library_state, "RECONCILE_MIN_FILES", 0)
    monkeypatch.setattr(library_state, "RECONCILE_MIN_RATIO", 0.0)


def _armer(monkeypatch):
    """Remettre les seuils de production."""
    monkeypatch.setattr(library_state, "RECONCILE_MIN_FILES", 50)
    monkeypatch.setattr(library_state, "RECONCILE_MIN_RATIO", 0.5)


def _file(file_id, artist, title, uploaded_at=1_700_000_000.0, path=None):
    return {
        "id": file_id,
        "artist": artist,
        "title": title,
        "uploaded_at": uploaded_at,
        "path": path if path is not None else f"{artist} - {title}.mp3",
    }


def test_ghost_row_is_cleared(db):
    """Une ligne active dont le file_id a disparu d'AzuraCast est retirée."""
    db.record_upload("otto - i am", "Otto", "(I am)", file_id=38)
    db.record_upload("vivant - titre", "Vivant", "Titre", file_id=40)

    report = reconcile([_file(40, "Vivant", "Titre")], db)

    assert report.ghosts_cleared == 1
    active_ids = {t["azuracast_file_id"] for t in db.get_active_tracks()}
    assert active_ids == {40}


def test_untracked_file_is_registered(db):
    report = reconcile([_file(51, "Inconnu", "Morceau")], db)

    assert report.untracked_registered == 1
    assert db.get_track_by_file_id(51)["track_key"] == "inconnu - morceau"


def test_untracked_file_keeps_its_real_age(db):
    """uploaded_at pilote le tiering et n'est écrit qu'une fois : pas de now() hâtif."""
    report = reconcile(
        [
            _file(1, "A", "B", uploaded_at=1_600_000_000),
            # À défaut d'uploaded_at, mtime porte l'âge réel du fichier.
            {"id": 2, "artist": "C", "title": "D", "mtime": 1_600_000_001},
            # Une date illisible ne fait pas échouer la réconciliation.
            {"id": 3, "artist": "E", "title": "F", "uploaded_at": "2026-07-26T12:00:00Z"},
        ],
        db,
    )

    assert report.untracked_registered == 3
    assert db.get_track_by_file_id(1)["uploaded_at"] == 1_600_000_000.0
    assert db.get_track_by_file_id(2)["uploaded_at"] == 1_600_000_001.0
    assert db.get_track_by_file_id(3)["uploaded_at"] > 1_700_000_000.0


def test_drifted_metadata_repairs_key_without_duplicating(db):
    """Incident 18/07 : AzuraCast a réécrit le titre, la clé doit suivre."""
    db.record_upload("artiste - titre original", "Artiste", "Titre Original", file_id=12)

    report = reconcile([_file(12, "Artiste", "Titre Sanitize")], db)

    assert report.keys_repaired == 1
    assert report.untracked_registered == 0
    assert report.ghosts_cleared == 0
    assert len(db.get_active_tracks()) == 1
    assert db.get_track_by_file_id(12)["track_key"] == "artiste - titre sanitize"


def test_library_keys_reflect_azuracast(db):
    report = reconcile([_file(1, "A", "B"), _file(2, "C", "D")], db)
    assert report.library_keys == {"a - b", "c - d"}


def test_files_without_metadata_are_ignored_not_registered(db):
    report = reconcile([{"id": 9, "artist": "", "title": ""}], db)
    assert report.untracked_registered == 0
    assert report.library_keys == set()


def test_present_file_without_metadata_is_never_a_ghost(db):
    """Un fichier qu'AzuraCast renvoie existe, même sans tags lisibles.

    Le déclarer fantôme annulerait son azuracast_file_id : la ligne
    serait orpheline pour toujours, son embedding CLAP purgé, et le
    morceau re-téléchargé en doublon alors qu'il passe encore à l'antenne.
    """
    db.record_upload("otto - i am", "Otto", "(I am)", file_id=77)

    report = reconcile([{"id": 77, "artist": "", "title": ""}], db)

    assert report.ghosts_cleared == 0
    active = db.get_active_tracks()
    assert len(active) == 1
    assert active[0]["azuracast_file_id"] == 77


def test_counts_are_reported(db):
    db.record_upload("fantome - un", "Fantome", "Un", file_id=90)
    report = reconcile([_file(1, "A", "B")], db)
    assert report.az_files == 1
    assert report.db_active_before == 1


def test_missing_media_dir_is_not_an_error(db):
    report = reconcile([_file(1, "A", "B")], db, media_dir=Path("/inexistant/xyz"))
    assert report.disk_files is None
    assert report.disk_drift is None


def test_disk_drift_is_reported(tmp_path, db):
    media = tmp_path / "media"
    media.mkdir()
    (media / "un.mp3").write_bytes(b"x")
    (media / "deux.mp3").write_bytes(b"x")
    (media / "trois.mp3").write_bytes(b"x")

    report = reconcile([_file(1, "A", "B")], db, media_dir=media)

    assert report.disk_files == 3
    assert report.disk_drift == 2


def test_disk_in_sync_reports_zero_drift(tmp_path, db):
    media = tmp_path / "media"
    (media / "sub").mkdir(parents=True)
    (media / "sub" / "un.mp3").write_bytes(b"x")

    report = reconcile([_file(1, "A", "B")], db, media_dir=media)

    assert report.disk_drift == 0


def test_non_mp3_uploads_do_not_create_phantom_drift(tmp_path, db):
    """Un jingle .wav à l'antenne n'est pas un .mp3 manquant sur disque."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "un.mp3").write_bytes(b"x")

    report = reconcile(
        [
            _file(1, "A", "B"),
            _file(2, "Jingle", "Station", path="jingles/station.wav"),
        ],
        db,
        media_dir=media,
    )

    assert report.az_files == 2
    assert report.az_mp3_files == 1
    assert report.disk_drift == 0


def test_drift_check_is_unavailable_when_api_hides_paths(tmp_path, db):
    media = tmp_path / "media"
    media.mkdir()
    (media / "un.mp3").write_bytes(b"x")

    report = reconcile([{"id": 1, "artist": "A", "title": "B"}], db, media_dir=media)

    assert report.az_mp3_files is None
    assert report.disk_drift is None


# ---------------------------------------------------------------------------
# Rapport de la nuit : les corrections des deux passes doivent s'additionner
# ---------------------------------------------------------------------------

def test_report_is_written_when_a_path_is_given(tmp_path, db):
    path = tmp_path / "last_reconcile.json"
    db.record_upload("fantome - x", "Fantome", "X", file_id=99)

    reconcile([_file(1, "A", "B")], db, report_path=path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ghosts_cleared"] == 1
    assert payload["az_files"] == 1


def test_no_report_is_written_without_a_path(tmp_path, db):
    reconcile([_file(1, "A", "B")], db)
    assert list(tmp_path.glob("*.json")) == []


def test_second_pass_of_the_night_adds_up_instead_of_erasing(tmp_path, db):
    """download.py corrige, classify.py ne voit plus rien : l'alerte doit survivre."""
    path = tmp_path / "last_reconcile.json"
    db.record_upload("fantome - x", "Fantome", "X", file_id=99)
    db.record_upload("vivant - y", "Vivant", "Y", file_id=1)

    reconcile([_file(1, "Vivant", "Y")], db, report_path=path)
    # Seconde passe, même nuit : idempotente, elle ne corrige plus rien.
    reconcile([_file(1, "Vivant", "Y")], db, report_path=path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ghosts_cleared"] == 1


def test_a_report_older_than_the_window_starts_a_new_run(tmp_path, db):
    path = tmp_path / "last_reconcile.json"
    path.write_text(json.dumps({"ghosts_cleared": 7}), encoding="utf-8")
    vieux = time.time() - library_state.RECONCILE_REPORT_MERGE_WINDOW_S - 60
    os.utime(path, (vieux, vieux))

    reconcile([_file(1, "A", "B")], db, report_path=path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ghosts_cleared"] == 0


def test_an_unreadable_previous_report_never_aborts_the_run(tmp_path, db):
    path = tmp_path / "last_reconcile.json"
    path.write_text("{ pas du json", encoding="utf-8")

    report = reconcile([_file(1, "A", "B")], db, report_path=path)

    assert report.untracked_registered == 1


def test_a_corrupt_previous_report_is_replaced_not_suppressed(tmp_path, db):
    """Un JSON illisible dans la fenêtre ne doit pas empêcher d'écrire la nuit.

    La lecture de fusion et l'écriture partageaient le même `try` : le
    `json.loads` levait avant `write_text`, donc les deux passes de la nuit
    laissaient le fichier corrompu en place et le récap ne voyait rien des
    corrections faites.
    """
    path = tmp_path / "last_reconcile.json"
    path.write_text("{ pas du json", encoding="utf-8")
    db.record_upload("fantome - x", "Fantome", "X", file_id=99)

    reconcile([_file(1, "A", "B")], db, report_path=path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ghosts_cleared"] == 1
    assert payload["untracked_registered"] == 1
    assert payload["az_files"] == 1


def test_an_unwritable_report_path_never_aborts_the_run(tmp_path, db):
    # Un dossier là où on attend un fichier : l'écriture échoue à coup sûr.
    path = tmp_path / "last_reconcile.json"
    path.mkdir()

    report = reconcile([_file(1, "A", "B")], db, report_path=path)

    assert report.untracked_registered == 1


def test_count_media_files_handles_none():
    assert count_media_files(None) is None


def test_report_defaults_are_zero():
    r = ReconcileReport()
    assert r.ghosts_cleared == 0
    assert r.library_keys == set()


# ---------------------------------------------------------------------------
# Un file_id réattribué ne doit pas défaire la réparation qui vient d'être faite
# ---------------------------------------------------------------------------

def test_reassigned_file_id_is_adopted_and_not_undone_as_a_ghost(db):
    """AzuraCast a réattribué l'id du morceau (retrait + ré-ajout, rescan média).

    La première boucle adopte le nouvel id sur la ligne existante ; la
    seconde boucle itère un instantané pris AVANT et y voit encore l'ancien
    id. Sans garde, elle appelle record_deletion sur LA MÊME clé et annule
    l'adoption : la ligne devient inactive, `valid_keys` la perd, et le
    prune du store CLAP détruit définitivement son embedding.
    """
    db.record_upload("artiste - titre", "Artiste", "Titre", file_id=10)

    report = reconcile([_file(11, "Artiste", "Titre")], db)

    assert report.ghosts_cleared == 0
    active = db.get_active_tracks()
    assert len(active) == 1
    assert active[0]["track_key"] == "artiste - titre"
    assert active[0]["azuracast_file_id"] == 11


def test_reassigned_file_id_still_clears_a_real_ghost(db):
    """La garde ne doit pas rendre les vrais fantômes intouchables."""
    db.record_upload("artiste - titre", "Artiste", "Titre", file_id=10)
    db.record_upload("parti - ailleurs", "Parti", "Ailleurs", file_id=20)

    report = reconcile([_file(11, "Artiste", "Titre")], db)

    assert report.ghosts_cleared == 1
    assert {t["track_key"] for t in db.get_active_tracks()} == {"artiste - titre"}


# ---------------------------------------------------------------------------
# Garde-fou : une liste AzuraCast invraisemblable n'efface pas la librairie
# ---------------------------------------------------------------------------

def _fill(db, n):
    for i in range(n):
        db.record_upload(f"a{i} - b", f"A{i}", "B", file_id=i + 1)


def test_empty_file_list_aborts_instead_of_deactivating_everything(db, monkeypatch):
    """Un 200 vide (glitch d'API) ne doit pas vider la base."""
    _armer(monkeypatch)
    _fill(db, 200)

    with pytest.raises(LibraryStateError):
        reconcile([], db)

    assert len(db.get_active_tracks()) == 200


def test_file_list_below_the_floor_aborts(db, monkeypatch):
    _armer(monkeypatch)
    _fill(db, 200)

    with pytest.raises(LibraryStateError):
        reconcile([_file(i + 1, f"A{i}", "B") for i in range(40)], db)

    assert len(db.get_active_tracks()) == 200


def test_file_list_below_the_ratio_aborts(db, monkeypatch):
    """80 fichiers pour 200 lignes actives : au-dessus du plancher, sous le ratio."""
    _armer(monkeypatch)
    _fill(db, 200)

    with pytest.raises(LibraryStateError):
        reconcile([_file(i + 1, f"A{i}", "B") for i in range(80)], db)

    assert len(db.get_active_tracks()) == 200


def test_a_normal_reconciliation_is_unaffected_by_the_guard(db, monkeypatch):
    _armer(monkeypatch)
    _fill(db, 200)

    report = reconcile([_file(i + 1, f"A{i}", "B") for i in range(199)], db)

    assert report.ghosts_cleared == 1
    assert len(db.get_active_tracks()) == 199


def test_guard_does_not_block_a_fresh_database(db, monkeypatch):
    """Base vide : rien à protéger, l'amorçage doit passer."""
    _armer(monkeypatch)
    report = reconcile([_file(1, "A", "B")], db)
    assert report.untracked_registered == 1


def test_a_small_library_seeing_all_its_files_is_not_blocked(db, monkeypatch):
    """30 lignes, 30 fichiers présents : le plancher ne doit pas s'appliquer.

    Sinon amorçage neuf et bibliothèque volontairement réduite lèvent chaque
    nuit — download.py sort en 1, rien ne se télécharge, et la bibliothèque
    ne peut plus repasser au-dessus du plancher toute seule.
    """
    _armer(monkeypatch)
    _fill(db, 30)

    report = reconcile([_file(i + 1, f"A{i}", "B") for i in range(30)], db)

    assert report.ghosts_cleared == 0
    assert len(db.get_active_tracks()) == 30


def test_a_small_library_losing_most_of_its_files_still_aborts(db, monkeypatch):
    """5 fichiers pour 30 lignes : sous le plancher ET sous le ratio."""
    _armer(monkeypatch)
    _fill(db, 30)

    with pytest.raises(LibraryStateError):
        reconcile([_file(i + 1, f"A{i}", "B") for i in range(5)], db)

    assert len(db.get_active_tracks()) == 30


def test_non_list_payload_is_rejected(db):
    """Un 200 portant {"error": ...} ferait len() sur des clés et f.get() sur str."""
    with pytest.raises(LibraryStateError):
        reconcile({"error": "boom"}, db)


def test_payload_of_non_dicts_is_rejected(db):
    with pytest.raises(LibraryStateError):
        reconcile(["boom"], db)


def test_healthy_is_false_when_something_was_corrected():
    assert ReconcileReport().healthy is True
    assert ReconcileReport(ghosts_cleared=1).healthy is False
    assert ReconcileReport(keys_repaired=1).healthy is False
    assert ReconcileReport(disk_files=10, disk_drift=2).healthy is False
    # Contrôle disque non effectué : ce n'est pas une anomalie.
    assert ReconcileReport(disk_files=None, disk_drift=None).healthy is True
