"""Réconciliation base SQLite ↔ AzuraCast ↔ dossier média."""

from pathlib import Path

import pytest

from library_state import ReconcileReport, count_media_files, reconcile
from track_db import TrackDB


@pytest.fixture
def db(tmp_path):
    d = TrackDB(tmp_path / "t.db")
    yield d
    d.close()


def _file(file_id, artist, title, uploaded_at=1_700_000_000.0):
    return {"id": file_id, "artist": artist, "title": title, "uploaded_at": uploaded_at}


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


def test_count_media_files_handles_none():
    assert count_media_files(None) is None


def test_report_defaults_are_zero():
    r = ReconcileReport()
    assert r.ghosts_cleared == 0
    assert r.library_keys == set()


def test_healthy_is_false_when_something_was_corrected():
    assert ReconcileReport().healthy is True
    assert ReconcileReport(ghosts_cleared=1).healthy is False
    assert ReconcileReport(keys_repaired=1).healthy is False
    assert ReconcileReport(disk_files=10, disk_drift=2).healthy is False
    # Contrôle disque non effectué : ce n'est pas une anomalie.
    assert ReconcileReport(disk_files=None, disk_drift=None).healthy is True
