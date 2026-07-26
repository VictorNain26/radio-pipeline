# Source de vérité unique, filtrage amont et récap fiable — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Faire d'AzuraCast l'unique source de vérité réconciliée à chaque run, écarter les mauvais candidats avant de dépenser de la bande passante, et produire un récap WhatsApp dont chaque chiffre est exact.

**Architecture :** Un nouveau module `scripts/library_state.py` réconcilie la base SQLite avec la liste de fichiers AzuraCast au début de `download.py` et de `classify.py`. Une table `verdicts` mémorise chaque rejet pour qu'un morceau déjà jugé ne soit jamais retéléchargé. `download.py` passe à deux phases : un filtrage à froid sans réseau lourd, puis un téléchargement plafonné par un budget qui tient compte du carryover sur disque. `send_daily_recap.py` est réécrit à partir du rapport de réconciliation.

**Tech Stack :** Python 3, sqlite3, pytest. Aucune nouvelle dépendance.

## Global Constraints

- Aucune nouvelle dépendance externe (pas de nouveau paquet pip).
- Le récap ne doit jamais faire échouer le pipeline : `send_daily_recap.py` retourne 0 en toute circonstance.
- Le dossier média est lu, jamais écrit ni utilisé pour décider d'une suppression.
- Tous les messages utilisateur (récap, logs destinés à Victor) sont en français ; les docstrings et commentaires de code suivent la langue déjà dominante du fichier modifié.
- La suite existante — 167 tests — doit continuer de passer après chaque tâche.
- Chaque tâche se termine par un commit.

## Trois écarts assumés par rapport à la spec

1. **`reconcile` prend la liste de fichiers, pas le client.** La spec écrivait `reconcile(client, track_db)`. Signature retenue : `reconcile(files, track_db, media_dir=None)`. `AzuraCastClient.get_station_files()` et `ClassifyClient.get_all_files()` portent des noms différents ; passer la liste déjà récupérée évite de coupler le module aux deux, et rend les tests exécutables sans mock HTTP.
2. **La ligne en collision est supprimée, pas marquée supprimée.** `track_key` est la clé primaire de `tracks` : deux lignes ne peuvent pas la partager, donc réparer une clé impose de retirer la ligne concurrente. Celle qui disparaît est l'artefact en double, jamais celle qui porte le `file_id` vivant.
3. **Le tri se fait par source de découverte, pas par affinité de tags.** La spec parlait d'un tri « par affinité de tags ». Aucune donnée exploitable ne le permet : la base ne stocke pas les tags des morceaux déjà à l'antenne, et un score d'affinité inventé déciderait de la dépense du budget sans qu'on puisse en mesurer la justesse. La provenance, elle, est déjà portée par `Track["source"]` et dit quelque chose de vrai : un choix manuel de Victor vaut mieux qu'un chart de tag Last.fm. Le tri n'écarte toujours rien.

## Structure des fichiers

| Fichier | Responsabilité |
|---|---|
| `scripts/library_state.py` (créé) | Réconciliation base ↔ AzuraCast ↔ disque. Ne connaît ni HTTP ni le téléchargement. |
| `scripts/track_db.py` (modifié) | Ajoute la table `verdicts` et la réparation de clé. Reste la seule couche SQL. |
| `config.py` (modifié) | `download_margin`, `verdict_ttl_days`, priorité des sources. |
| `scripts/settings.py` (modifié) | Chemin du dossier média, facultatif. |
| `scripts/download.py` (modifié) | Passe à deux phases. Le filtrage à froid sort dans une fonction dédiée et testable. |
| `scripts/classify.py` (modifié) | Consomme la réconciliation, écrit les verdicts. |
| `scripts/send_daily_recap.py` (réécrit) | Compose le message. Aucune logique métier. |

---

### Task 1 : Table `verdicts` et réparation de clé dans `track_db.py`

**Files:**
- Modify: `scripts/track_db.py`
- Modify: `config.py:826` (après `min_profile_size`)
- Test: `tests/test_track_db.py`

**Interfaces:**
- Consumes : rien.
- Produces :
  - `TrackDB.record_verdict(track_key: str, verdict: str, reason: str | None = None, score: float | None = None) -> None`
  - `TrackDB.get_verdict(track_key: str) -> dict[str, Any] | None`
  - `TrackDB.has_active_verdict(track_key: str, taste_ttl_days: int) -> bool`
  - `TrackDB.repair_track_key(old_key: str, new_key: str) -> bool`
  - `TrackDB.PERISHABLE_VERDICTS: frozenset[str]`
  - `config.TASTE_FILTER.verdict_ttl_days: int`

- [ ] **Step 1 : Écrire les tests qui échouent**

Ajouter à la fin de `tests/test_track_db.py` :

```python
import time

from track_db import TrackDB


def _db(tmp_path):
    return TrackDB(tmp_path / "t.db")


def test_verdict_roundtrip(tmp_path):
    db = _db(tmp_path)
    db.record_verdict("a - b", "rejected_taste", reason="0.41 < 0.62", score=0.41)
    v = db.get_verdict("a - b")
    assert v["verdict"] == "rejected_taste"
    assert v["reason"] == "0.41 < 0.62"
    assert v["score"] == 0.41
    db.close()


def test_verdict_absent_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.get_verdict("jamais - vu") is None
    assert db.has_active_verdict("jamais - vu", 90) is False
    db.close()


def test_taste_verdict_expires_after_ttl(tmp_path):
    db = _db(tmp_path)
    db.record_verdict("a - b", "rejected_taste", score=0.41)
    assert db.has_active_verdict("a - b", 90) is True

    # Antidater de 91 jours : le profil de goût a pu être reconstruit,
    # le morceau doit pouvoir retenter sa chance.
    old = time.time() - 91 * 86400
    db.conn.execute("UPDATE verdicts SET decided_at = ? WHERE track_key = ?", (old, "a - b"))
    db.conn.commit()
    assert db.has_active_verdict("a - b", 90) is False
    db.close()


def test_hard_verdicts_never_expire(tmp_path):
    db = _db(tmp_path)
    old = time.time() - 3650 * 86400
    for verdict in ("rejected_speech", "rejected_multisignal", "blocked_genre", "filtered_duration"):
        key = f"a - {verdict}"
        db.record_verdict(key, verdict)
        db.conn.execute("UPDATE verdicts SET decided_at = ? WHERE track_key = ?", (old, key))
        db.conn.commit()
        assert db.has_active_verdict(key, 90) is True, verdict
    db.close()


def test_upload_clears_verdict(tmp_path):
    db = _db(tmp_path)
    db.record_verdict("a - b", "rejected_taste", score=0.41)
    db.record_upload("a - b", "A", "B", file_id=7)
    assert db.get_verdict("a - b") is None
    db.close()


def test_repair_track_key_moves_row_and_fingerprint(tmp_path):
    db = _db(tmp_path)
    db.record_upload("ancien - titre", "Ancien", "Titre", file_id=7)
    db.record_fingerprint("ancien - titre", "HASH", 200)

    assert db.repair_track_key("ancien - titre", "nouveau - titre") is True

    assert db.get_track_by_file_id(7)["track_key"] == "nouveau - titre"
    assert db.find_by_fingerprint("HASH")["track_key"] == "nouveau - titre"
    db.close()


def test_repair_track_key_collision_keeps_live_file(tmp_path):
    db = _db(tmp_path)
    # La ligne en double n'a pas de fichier vivant.
    db.record_upload("nouveau - titre", "Nouveau", "Titre", file_id=99)
    db.record_deletion("nouveau - titre")
    # Celle-ci porte le file_id réellement présent chez AzuraCast.
    db.record_upload("ancien - titre", "Ancien", "Titre", file_id=7)

    assert db.repair_track_key("ancien - titre", "nouveau - titre") is True

    rows = db.conn.execute(
        "SELECT track_key, azuracast_file_id FROM tracks WHERE track_key = 'nouveau - titre'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["azuracast_file_id"] == 7
    db.close()


def test_repair_track_key_noop_when_identical(tmp_path):
    db = _db(tmp_path)
    db.record_upload("a - b", "A", "B", file_id=1)
    assert db.repair_track_key("a - b", "a - b") is False
    db.close()
```

- [ ] **Step 2 : Lancer les tests et vérifier qu'ils échouent**

Run : `python3 -m pytest tests/test_track_db.py -q`
Expected : FAIL — `AttributeError: 'TrackDB' object has no attribute 'record_verdict'`

- [ ] **Step 3 : Ajouter le réglage de péremption dans `config.py`**

Dans `TasteFilterConfig`, juste après la ligne `min_profile_size: int = 200` :

```python

    # Péremption du verdict `rejected_taste` au registre (track_db.verdicts).
    # Le profil de goût est reconstruit périodiquement : un morceau écarté
    # sous l'ancien profil doit pouvoir retenter sa chance, sinon un faux
    # négatif devient définitif. Les verdicts portant sur une propriété
    # stable de l'enregistrement (parole, genre, durée) ne périment jamais.
    verdict_ttl_days: int = 90
```

- [ ] **Step 4 : Créer la table `verdicts`**

Dans `scripts/track_db.py`, à l'intérieur du `executescript` de `_create_tables`, après le bloc `audio_fingerprints` et avant la fermeture `"""` :

```sql

            -- Registre des verdicts : tout morceau jugé et écarté, avec sa
            -- raison. Lu par download.py en phase à froid pour ne jamais
            -- retélécharger ce qui a déjà été jugé sur son audio réel.
            CREATE TABLE IF NOT EXISTS verdicts (
                track_key   TEXT PRIMARY KEY,
                verdict     TEXT NOT NULL,
                reason      TEXT,
                score       REAL,
                decided_at  REAL NOT NULL
            );
```

- [ ] **Step 5 : Implémenter les méthodes de verdict**

Dans `scripts/track_db.py`, ajouter en attribut de classe juste sous `class TrackDB:` et sa docstring :

```python
    # Seuls les verdicts portant sur le goût périment : le profil évolue.
    # Les autres jugent une propriété stable de l'enregistrement.
    PERISHABLE_VERDICTS = frozenset({"rejected_taste"})
```

Puis, après `find_by_fingerprint` :

```python
    # ------------------------------------------------------------------
    # Registre des verdicts
    # ------------------------------------------------------------------

    @_synchronized
    def record_verdict(
        self, track_key: str, verdict: str,
        reason: str | None = None, score: float | None = None,
    ) -> None:
        """Mémoriser le rejet d'un morceau (idempotent, dernier verdict gagne)."""
        self.conn.execute(
            """INSERT INTO verdicts (track_key, verdict, reason, score, decided_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(track_key) DO UPDATE SET
                   verdict = excluded.verdict,
                   reason = excluded.reason,
                   score = excluded.score,
                   decided_at = excluded.decided_at""",
            (track_key, verdict, reason, score, time.time()),
        )
        self.conn.commit()

    @_synchronized
    def get_verdict(self, track_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT track_key, verdict, reason, score, decided_at FROM verdicts WHERE track_key = ?",
            (track_key,),
        ).fetchone()
        return dict(row) if row else None

    @_synchronized
    def has_active_verdict(self, track_key: str, taste_ttl_days: int) -> bool:
        """Vrai si un verdict existe et n'a pas péri."""
        row = self.conn.execute(
            "SELECT verdict, decided_at FROM verdicts WHERE track_key = ?",
            (track_key,),
        ).fetchone()
        if not row:
            return False
        if row["verdict"] in self.PERISHABLE_VERDICTS:
            return (time.time() - row["decided_at"]) < taste_ttl_days * 86400
        return True

    @_synchronized
    def repair_track_key(self, old_key: str, new_key: str) -> bool:
        """
        Réécrire la clé d'un morceau après dérive des métadonnées AzuraCast.

        Si `new_key` est déjà pris, la ligne concurrente est retirée : la
        clé primaire interdit le doublon, et c'est la ligne portant le
        file_id vivant qui doit survivre.

        Returns:
            True si une ligne a été déplacée.
        """
        if old_key == new_key or not old_key or not new_key:
            return False
        if self.conn.execute(
            "SELECT 1 FROM tracks WHERE track_key = ?", (old_key,)
        ).fetchone() is None:
            return False

        self.conn.execute("DELETE FROM tracks WHERE track_key = ?", (new_key,))
        self.conn.execute(
            "UPDATE tracks SET track_key = ? WHERE track_key = ?", (new_key, old_key)
        )
        self.conn.execute("DELETE FROM audio_fingerprints WHERE track_key = ?", (new_key,))
        self.conn.execute(
            "UPDATE audio_fingerprints SET track_key = ? WHERE track_key = ?",
            (new_key, old_key),
        )
        self.conn.execute("DELETE FROM verdicts WHERE track_key = ?", (new_key,))
        self.conn.execute(
            "UPDATE verdicts SET track_key = ? WHERE track_key = ?", (new_key, old_key)
        )
        self.conn.commit()
        return True
```

- [ ] **Step 6 : Effacer le verdict à l'upload**

Dans `record_upload`, juste avant `self.conn.commit()` :

```python
        # Un morceau qui monte à l'antenne n'est plus un rejet.
        self.conn.execute("DELETE FROM verdicts WHERE track_key = ?", (track_key,))
```

- [ ] **Step 7 : Lancer les tests**

Run : `python3 -m pytest tests/test_track_db.py -q`
Expected : PASS

- [ ] **Step 8 : Lancer la suite complète**

Run : `python3 -m pytest tests/ -q`
Expected : PASS, aucun échec

- [ ] **Step 9 : Commit**

```bash
git add scripts/track_db.py config.py tests/test_track_db.py
git commit -m "feat(track_db): registre des verdicts et réparation de clé"
```

---

### Task 2 : Module `library_state.py`

**Files:**
- Create: `scripts/library_state.py`
- Create: `tests/test_library_state.py`
- Modify: `scripts/settings.py:77` (après `whatsapp_phone`)

**Interfaces:**
- Consumes : `TrackDB.get_active_tracks()`, `TrackDB.record_deletion()`, `TrackDB.register_untracked_file()`, `TrackDB.repair_track_key()` (Task 1), `track_db.normalize_track_key()`.
- Produces :
  - `library_state.ReconcileReport` — dataclass avec `az_files: int`, `db_active_before: int`, `ghosts_cleared: int`, `untracked_registered: int`, `keys_repaired: int`, `disk_files: int | None`, `disk_drift: int | None`, `library_keys: set[str]`
  - `library_state.reconcile(files: list[dict[str, Any]], track_db: TrackDB, media_dir: Path | None = None) -> ReconcileReport`
  - `library_state.count_media_files(media_dir: Path | None) -> int | None`
  - `settings.Settings.azuracast_media_dir: str | None`

- [ ] **Step 1 : Écrire les tests qui échouent**

Créer `tests/test_library_state.py` :

```python
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
```

- [ ] **Step 2 : Lancer les tests et vérifier qu'ils échouent**

Run : `python3 -m pytest tests/test_library_state.py -q`
Expected : FAIL — `ModuleNotFoundError: No module named 'library_state'`

- [ ] **Step 3 : Créer `scripts/library_state.py`**

```python
"""
Réconciliation de l'état de la bibliothèque musicale.

AzuraCast fait autorité sur ce qui existe. SQLite ne conserve que ce
qu'AzuraCast ignore : date d'upload propre au pipeline, compteur de
lectures, tier de rotation, mood, empreinte audio.

Ce module est le seul endroit qui décide « ce morceau existe-t-il ». Il
est appelé au début de download.py et de classify.py, à partir d'une
liste de fichiers déjà récupérée — il ne fait aucun appel réseau.

Le dossier média est lu pour vérifier la cohérence et lever une alerte.
Il ne déclenche jamais de suppression : le disque ne décide de rien.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from track_db import TrackDB, normalize_track_key

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """Ce que la réconciliation a constaté et corrigé."""

    az_files: int = 0
    db_active_before: int = 0
    ghosts_cleared: int = 0
    untracked_registered: int = 0
    keys_repaired: int = 0
    # None = dossier média non configuré ou illisible : contrôle non effectué.
    disk_files: int | None = None
    disk_drift: int | None = None
    library_keys: set[str] = field(default_factory=set)

    @property
    def healthy(self) -> bool:
        """Vrai si rien d'anormal n'a été constaté."""
        return (
            self.ghosts_cleared == 0
            and self.keys_repaired == 0
            and not self.disk_drift
        )


def count_media_files(media_dir: Path | None) -> int | None:
    """
    Nombre de .mp3 sous le dossier média, ou None si non vérifiable.

    Jamais d'exception : un dossier absent, non monté ou illisible rend
    le contrôle indisponible, pas le run.
    """
    if media_dir is None:
        return None
    try:
        path = Path(media_dir)
        if not path.is_dir():
            return None
        return sum(1 for _ in path.rglob("*.mp3"))
    except OSError as e:
        logger.warning("Dossier média illisible (%s) : %s", media_dir, e)
        return None


def reconcile(
    files: list[dict[str, Any]],
    track_db: TrackDB,
    media_dir: Path | None = None,
) -> ReconcileReport:
    """
    Aligner la base sur la liste de fichiers AzuraCast.

    Args:
        files: Fichiers tels que renvoyés par l'API AzuraCast.
        track_db: Base persistante à réconcilier.
        media_dir: Dossier média, pour le contrôle de cohérence seul.

    Returns:
        Rapport chiffré, dont `library_keys` : les clés normalisées
        réellement présentes à l'antenne, à utiliser pour la déduplication.
    """
    report = ReconcileReport(az_files=len(files))

    active = track_db.get_active_tracks()
    report.db_active_before = len(active)
    by_file_id = {t["azuracast_file_id"]: t for t in active}

    seen_ids: set[int] = set()

    for f in files:
        file_id = f.get("id")
        artist = f.get("artist", "") or ""
        title = f.get("title", "") or ""

        if not (artist and title):
            # Sans métadonnées on ne peut ni construire de clé ni dédupliquer.
            # Le fichier reste à l'antenne, il est simplement invisible ici.
            logger.warning("Fichier AzuraCast sans artiste/titre (id=%s) — ignoré", file_id)
            continue

        key = normalize_track_key(artist, title)
        report.library_keys.add(key)

        if file_id is None:
            continue
        seen_ids.add(file_id)

        known = by_file_id.get(file_id)
        if known is None:
            track_db.register_untracked_file(
                key, artist, title,
                f.get("uploaded_at") or f.get("mtime") or 0.0,
                file_id,
            )
            report.untracked_registered += 1
        elif known["track_key"] != key:
            # Les métadonnées ont dérivé côté serveur (sanitization,
            # réécriture manuelle). La clé suit le fichier vivant.
            if track_db.repair_track_key(known["track_key"], key):
                logger.info("Clé réparée : %r → %r (file_id=%s)",
                            known["track_key"], key, file_id)
                report.keys_repaired += 1

    for file_id, track in by_file_id.items():
        if file_id in seen_ids:
            continue
        # Le fichier a disparu d'AzuraCast (purge manuelle, suppression
        # serveur). Sans ceci la ligne compte à vie dans max_tracks et
        # maintient son embedding CLAP en vie.
        logger.info("Fantôme retiré : %s - %s (file_id=%s absent d'AzuraCast)",
                    track["artist"], track["title"], file_id)
        track_db.record_deletion(track["track_key"])
        report.ghosts_cleared += 1

    report.disk_files = count_media_files(media_dir)
    if report.disk_files is not None:
        report.disk_drift = abs(report.disk_files - report.az_files)
        if report.disk_drift:
            logger.warning(
                "Désynchro dossier/API : %d fichiers sur disque, %d vus par l'API",
                report.disk_files, report.az_files,
            )

    logger.info(
        "Réconciliation : %d fichiers AzuraCast | %d fantômes retirés | "
        "%d inconnus enregistrés | %d clés réparées",
        report.az_files, report.ghosts_cleared,
        report.untracked_registered, report.keys_repaired,
    )
    return report
```

- [ ] **Step 4 : Lancer les tests**

Run : `python3 -m pytest tests/test_library_state.py -q`
Expected : PASS

- [ ] **Step 5 : Ajouter le chemin média dans `settings.py`**

Dans `scripts/settings.py`, après le champ `whatsapp_phone` :

```python

    # Dossier média AzuraCast, pour le contrôle de cohérence en lecture
    # seule (library_state.count_media_files). Facultatif : absent, le
    # contrôle est simplement sauté.
    azuracast_media_dir: str | None = Field(
        default=None,
        description="Chemin du dossier média AzuraCast (ex. ~/azuracast/stations/aubesonore/media)",
    )
```

- [ ] **Step 6 : Documenter le réglage dans `.env.example`**

Ajouter à `.env.example`, sous la section AzuraCast :

```bash
# Contrôle de cohérence en lecture seule entre le dossier média et l'API.
# Facultatif : laissé vide, le contrôle est sauté.
# AZURACAST_MEDIA_DIR=/home/victormoi/azuracast/stations/aubesonore/media
```

- [ ] **Step 7 : Lancer la suite complète**

Run : `python3 -m pytest tests/ -q`
Expected : PASS

- [ ] **Step 8 : Commit**

```bash
git add scripts/library_state.py tests/test_library_state.py scripts/settings.py .env.example
git commit -m "feat(library_state): réconciliation base/AzuraCast/disque"
```

---

### Task 3 : Brancher la réconciliation dans `classify.py`

**Files:**
- Modify: `scripts/classify.py:846-875` (bloc d'auto-enregistrement dans `enforce_tiered_rotation`)
- Test: `tests/test_rotation_tiers.py`

**Interfaces:**
- Consumes : `library_state.reconcile` (Task 2).
- Produces : `enforce_tiered_rotation` accepte `reconcile_report: ReconcileReport | None = None` et écrit `data/last_reconcile.json`.

- [ ] **Step 1 : Écrire le test qui échoue**

Ajouter à `tests/test_rotation_tiers.py` :

```python
def test_rotation_clears_ghost_rows(tmp_path, monkeypatch):
    """Une ligne active absente d'AzuraCast ne survit pas à la rotation."""
    from library_state import reconcile
    from track_db import TrackDB

    db = TrackDB(tmp_path / "t.db")
    db.record_upload("fantome - x", "Fantome", "X", file_id=999)
    db.record_upload("vivant - y", "Vivant", "Y", file_id=1)

    report = reconcile([{"id": 1, "artist": "Vivant", "title": "Y"}], db)

    assert report.ghosts_cleared == 1
    assert [t["track_key"] for t in db.get_active_tracks()] == ["vivant - y"]
    db.close()
```

- [ ] **Step 2 : Lancer le test**

Run : `python3 -m pytest tests/test_rotation_tiers.py::test_rotation_clears_ghost_rows -q`
Expected : PASS déjà (le module existe depuis Task 2) — ce test verrouille le comportement avant le refactor de l'étape 3.

- [ ] **Step 3 : Appeler `reconcile` dans `enforce_tiered_rotation`**

Dans `scripts/classify.py`, ajouter l'import en tête, juste après `from track_db import TrackDB, normalize_track_key` (ligne 30) :

```python
from library_state import ReconcileReport, reconcile
```

`json`, `Path`, `get_settings` et `TrackDB` sont déjà importés dans ce fichier — rien d'autre à ajouter.

Remplacer, dans `enforce_tiered_rotation`, la ligne `files = client.get_all_files()` et son commentaire de phase par :

```python
    # --- Phase 2 : réconcilier puis classer par tier ---
    # AzuraCast fait autorité. La réconciliation retire les fantômes,
    # enregistre les fichiers inconnus et répare les clés ayant dérivé,
    # avant tout calcul de tier — sinon les compteurs mentent.
    files = client.get_all_files()
    settings = get_settings()
    media_dir = Path(settings.azuracast_media_dir) if settings.azuracast_media_dir else None
    report = reconcile(files, track_db, media_dir=media_dir)
    _write_reconcile_report(report)
    current_count = len(files)
    now = time.time()
```

- [ ] **Step 4 : Retirer l'auto-enregistrement devenu redondant**

Toujours dans `enforce_tiered_rotation`, dans la boucle `for f in files:`, remplacer le bloc `else:` de l'auto-enregistrement (celui qui appelle `track_db.register_untracked_file`) par :

```python
        else:
            # reconcile() vient d'enregistrer tout fichier inconnu : ne
            # rester ici que le cas des fichiers sans métadonnées, qu'on
            # ne peut pas cléer. Ils traversent la rotation en DISCOVERY.
            uploaded_at = f.get("uploaded_at") or f.get("mtime") or now
            play_count = 0
            track_key = normalize_track_key(artist, title) if artist and title else ""
            mood = None
            tier_stored = "DISCOVERY"
```

- [ ] **Step 5 : Écrire le rapport pour le récap**

Dans `scripts/classify.py`, ajouter avant `enforce_tiered_rotation` :

```python
def _write_reconcile_report(report: "ReconcileReport") -> None:
    """Persister le rapport de réconciliation pour le récap quotidien."""
    path = Path(__file__).parent.parent / "data" / "last_reconcile.json"
    payload = {
        "az_files": report.az_files,
        "db_active_before": report.db_active_before,
        "ghosts_cleared": report.ghosts_cleared,
        "untracked_registered": report.untracked_registered,
        "keys_repaired": report.keys_repaired,
        "disk_files": report.disk_files,
        "disk_drift": report.disk_drift,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Écriture de last_reconcile.json impossible")
```

- [ ] **Step 6 : Lancer la suite complète**

Run : `python3 -m pytest tests/ -q`
Expected : PASS

- [ ] **Step 7 : Commit**

```bash
git add scripts/classify.py tests/test_rotation_tiers.py
git commit -m "refactor(classify): la rotation part de l'état réconcilié"
```

---

### Task 4 : `classify.py` écrit les verdicts

**Files:**
- Modify: `scripts/classify.py:714-795` (les `return "rejected"` de `process_track`)
- Test: `tests/test_classify_verdicts.py` (créé)

**Interfaces:**
- Consumes : `TrackDB.record_verdict` (Task 1).
- Produces : après un rejet, `track_db.get_verdict(track_key)` renvoie le motif.

- [ ] **Step 1 : Écrire le test qui échoue**

Créer `tests/test_classify_verdicts.py` :

```python
"""Un rejet doit laisser une trace exploitable par download.py."""

from track_db import TrackDB


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


def test_rejection_without_key_is_a_noop(tmp_path):
    from classify import record_rejection

    db = TrackDB(tmp_path / "t.db")
    record_rejection(db, "", "rejected_taste", "pas de clé")
    assert db.get_verdict("") is None
    db.close()
```

- [ ] **Step 2 : Lancer le test et vérifier qu'il échoue**

Run : `python3 -m pytest tests/test_classify_verdicts.py -q`
Expected : FAIL — `ImportError: cannot import name 'record_rejection'`

- [ ] **Step 3 : Implémenter le point d'entrée unique**

Dans `scripts/classify.py`, ajouter avant `process_track` :

```python
def record_rejection(
    track_db: TrackDB,
    track_key: str | None,
    verdict: str,
    reason: str,
    score: float | None = None,
) -> None:
    """
    Inscrire un rejet au registre pour ne jamais retélécharger ce morceau.

    Sans clé (métadonnées illisibles) il n'y a rien à mémoriser : le
    morceau repassera par le téléchargement, ce qui est le comportement
    voulu — on ne condamne pas ce qu'on n'a pas su identifier.
    """
    if not track_key:
        return
    track_db.record_verdict(track_key, verdict, reason=reason, score=score)
```

- [ ] **Step 4 : Appeler `record_rejection` à chaque rejet de `process_track`**

Dans `process_track`, avant chacun des `filepath.unlink()` suivis de `return "rejected", []`, insérer l'appel correspondant. Les cinq points de rejet et leur verdict :

| Motif du rejet dans `process_track` | Verdict à inscrire |
|---|---|
| filtre parole (speech) | `rejected_speech` |
| `should_reject_multisignal` | `rejected_multisignal` |
| filtre de goût sous le seuil | `rejected_taste` |
| durée hors bornes | `filtered_duration` |
| genre bloqué | `blocked_genre` |

Exemple, pour le filtre de goût :

```python
            record_rejection(
                track_db, track_key, "rejected_taste",
                f"{taste_score:.2f} < {TASTE_FILTER.threshold:.2f}",
                taste_score,
            )
            filepath.unlink()
            return "rejected", []
```

Localiser les points de rejet avec :

```bash
grep -n "return \"rejected\"" scripts/classify.py
```

Vérifier avant d'éditer que `track_key` est bien en portée à chaque point ; sinon le calculer par `_track_key_of_file(filepath)`, déjà défini dans le fichier.

- [ ] **Step 5 : Inscrire aussi l'éviction par quota**

Dans `_main_inner`, à l'endroit où un fichier non retenu part en cooldown (`track_db.record_deletion(track_key)` suivi de `results["quota"] += 1`), **ne rien inscrire au registre**. L'éviction par quota n'est pas un jugement sur le morceau : le cooldown existant suffit, et un verdict le condamnerait à tort.

Ajouter le commentaire suivant à cet endroit pour que l'intention reste lisible :

```python
                # Pas de verdict ici : l'éviction par quota juge le calendrier,
                # pas le morceau. Le cooldown suffit à éviter le rebond.
```

- [ ] **Step 6 : Lancer les tests**

Run : `python3 -m pytest tests/test_classify_verdicts.py tests/ -q`
Expected : PASS

- [ ] **Step 7 : Commit**

```bash
git add scripts/classify.py tests/test_classify_verdicts.py
git commit -m "feat(classify): tout rejet est inscrit au registre des verdicts"
```

---

### Task 5 : `download.py` en deux phases

**Files:**
- Modify: `scripts/download.py:1258-1359`
- Modify: `config.py:243` (fin de `RotationConfig`)
- Test: `tests/test_download_prefilter.py` (créé)

**Interfaces:**
- Consumes : `library_state.reconcile` (Task 2), `TrackDB.has_active_verdict` (Task 1), `TrackDB.record_verdict` (Task 1).
- Produces :
  - `download.prefilter_candidates(tracks, library_keys, track_db, genre_client) -> tuple[list[Track], dict[str, int]]`
  - `download.compute_budget(carryover_files: int) -> int`
  - `config.ROTATION.download_margin: float`
  - `config.SOURCE_PRIORITY: dict[str, int]`
  - `data/last_download_stats.json` gagne les clés `budget`, `carryover_on_disk`, `prefiltered`

- [ ] **Step 1 : Écrire les tests qui échouent**

Créer `tests/test_download_prefilter.py` :

```python
"""Filtrage à froid et budget : rien ne doit être téléchargé inutilement."""

import pytest

from track_db import TrackDB


@pytest.fixture
def db(tmp_path):
    d = TrackDB(tmp_path / "t.db")
    yield d
    d.close()


def _track(artist, title, source="RSSSource"):
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
    from download import prefilter_candidates

    survivors, _ = prefilter_candidates(
        [
            _track("A", "Un", source="LastFMTagSource"),
            _track("B", "Deux", source="ManualPicksSource"),
            _track("C", "Trois", source="PersonalArtistsSource"),
        ],
        library_keys=set(), track_db=db, genre_client=None,
    )

    assert [t["title"] for t in survivors] == ["Deux", "Trois", "Un"]


def test_prefilter_is_stable_within_a_source(db):
    from download import prefilter_candidates

    tracks = [_track("A", str(i), source="RSSSource") for i in range(5)]
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
```

- [ ] **Step 2 : Lancer les tests et vérifier qu'ils échouent**

Run : `python3 -m pytest tests/test_download_prefilter.py -q`
Expected : FAIL — `ImportError: cannot import name 'compute_budget'`

- [ ] **Step 3 : Ajouter les réglages dans `config.py`**

Dans `RotationConfig`, après `gold_max_pct: float = 40.0` :

```python

    # --- Budget de téléchargement (2026-07) ------------------------------
    # On ne télécharge que ce qu'on peut espérer diffuser. Le budget d'une
    # nuit vaut max_uploads_per_night × download_margin, moins les fichiers
    # déjà en attente dans downloads/. La marge absorbe les rejets du filtre
    # de goût et les échecs yt-dlp. Avec 24 en carryover pour un quota de 6,
    # le budget tombe à zéro : c'est voulu, le stock suffit.
    download_margin: float = 2.0
```

Après la ligne `TASTE_DISCOVERY_TRACKS_PER_ARTIST: int = 2` :

```python

# Ordre de dépense du budget de téléchargement. Un choix explicite de
# Victor passe avant une piste dérivée de son profil, qui passe avant une
# découverte éditoriale, qui passe avant un chart de tag. Cet ordre ne
# rejette rien : il décide seulement qui est servi en premier quand le
# budget est plus court que la liste de candidats.
SOURCE_PRIORITY: dict[str, int] = {
    "ManualPicksSource": 0,
    "PersonalArtistsSource": 1,
    "CustomFeedsSource": 2,
    "RSSSource": 3,
    "HypeMachineSource": 4,
    "LastFMTagSource": 5,
}
SOURCE_PRIORITY_DEFAULT: int = 9
```

- [ ] **Step 4 : Implémenter `compute_budget` et `prefilter_candidates`**

Dans `scripts/download.py`, les imports de `config` vivent dans un `try/except ImportError` (lignes 39-46). Ajouter les trois noms **à l'intérieur** de ce bloc, en gardant l'ordre alphabétique :

```python
try:
    from config import (
        ACOUSTID_DEDUP,
        AUDIO_FILTERS,
        GENRE_FILTER,
        LOUDNORM,
        ROTATION,
        SOURCE_PRIORITY,
        SOURCE_PRIORITY_DEFAULT,
        TASTE_FILTER,
        format_duration,
    )
except ImportError as e:
    print(f"Error: config.py not found or invalid in pipeline root: {e}")
    sys.exit(1)
```

Puis, avant `def main()` :

```python
def compute_budget(carryover_files: int) -> int:
    """
    Nombre de morceaux qu'il est utile de télécharger cette nuit.

    On ne télécharge que ce qu'on peut espérer diffuser : le quota de la
    nuit, majoré d'une marge qui absorbe les rejets et les échecs, moins
    ce qui dort déjà dans downloads/.
    """
    full = int(ROTATION.max_uploads_per_night * ROTATION.download_margin)
    return max(0, full - carryover_files)


def prefilter_candidates(
    tracks: list[Track],
    library_keys: set[str],
    track_db: "TrackDB",
    genre_client: GenreClient | None,
) -> tuple[list[Track], dict[str, int]]:
    """
    Phase à froid : écarter tout ce qui est décidable sans télécharger.

    Aucun octet d'audio n'est transféré ici. Les seuls appels réseau
    possibles sont les recherches de genre, servies par data/genre_cache.json
    dans la grande majorité des cas.

    Le tri final n'écarte rien : il décide de l'ordre dans lequel le budget
    sera dépensé.

    Returns:
        (candidats retenus et ordonnés, compteurs par motif d'exclusion).
    """
    counts = {
        "already_in_library": 0,
        "cooldown": 0,
        "known_verdict": 0,
        "blocked_genre": 0,
        "no_metadata": 0,
    }
    survivors: list[Track] = []
    seen: set[str] = set()

    for track in tracks:
        artist = track.get("artist") or ""
        title = track.get("title") or ""
        if not (artist and title):
            counts["no_metadata"] += 1
            continue

        key = normalize_track_key(artist, title)

        if key in library_keys or key in seen:
            counts["already_in_library"] += 1
            continue
        if track_db.is_in_cooldown(key, ROTATION.cooldown_days):
            counts["cooldown"] += 1
            continue
        if track_db.has_active_verdict(key, TASTE_FILTER.verdict_ttl_days):
            counts["known_verdict"] += 1
            continue

        if genre_client is not None and GENRE_FILTER.enabled:
            result = genre_client.check_genre(artist, title)
            if result.is_blocked:
                logger.info("  Bloqué [%s - %s] : %s", artist, title, result.blocked_reason)
                track_db.record_verdict(key, "blocked_genre", reason=result.blocked_reason)
                counts["blocked_genre"] += 1
                continue
            if GENRE_FILTER.require_tags and not result.tags:
                track_db.record_verdict(key, "blocked_genre", reason="aucun tag de genre")
                counts["blocked_genre"] += 1
                continue

        seen.add(key)
        survivors.append(track)

    # Tri stable : à priorité égale, l'ordre de découverte est conservé.
    survivors.sort(
        key=lambda t: SOURCE_PRIORITY.get(t.get("source", ""), SOURCE_PRIORITY_DEFAULT)
    )
    return survivors, counts
```

- [ ] **Step 5 : Lancer les tests du filtrage**

Run : `python3 -m pytest tests/test_download_prefilter.py -q`
Expected : PASS

- [ ] **Step 6 : Câbler les deux phases dans `main()`**

Dans `scripts/download.py`, remplacer la ligne `existing_library = fetch_azuracast_library(client)` par :

```python
    # AzuraCast fait autorité : on réconcilie avant tout, et les clés de
    # déduplication sortent du rapport plutôt que d'un fetch parallèle.
    from library_state import reconcile
    from track_db import TrackDB

    db_path = Path(__file__).parent.parent / "data" / "tracks.db"
    track_db = TrackDB(db_path)
    media_dir = Path(settings.azuracast_media_dir) if settings.azuracast_media_dir else None
    report = reconcile(client.get_station_files(), track_db, media_dir=media_dir)
    existing_library = report.library_keys
```

Supprimer le bloc plus bas qui réinstanciait `TrackDB` (`from track_db import TrackDB` / `db_path = ...` / `track_db = TrackDB(db_path)`) : la base est désormais ouverte une seule fois, plus haut.

Supprimer aussi la fonction `fetch_azuracast_library` (lignes 300-332) et son import `ClientError, ServerError, HTTPConnectionError` s'il devient inutilisé. Elle n'a plus d'appelant : les clés de déduplication viennent du rapport de réconciliation. Vérifier avec :

```bash
grep -rn "fetch_azuracast_library" scripts/ tests/
```
Expected : aucun résultat après suppression.

Après la construction du `genre_client` et de `stats`, insérer les deux phases :

```python
    # --- Phase à froid : rien n'est téléchargé ---
    candidates, prefilter_counts = prefilter_candidates(
        tracks, existing_library, track_db, genre_client
    )
    stats["prefiltered"] = sum(prefilter_counts.values())
    stats["skipped"] = prefilter_counts["already_in_library"] + prefilter_counts["cooldown"]
    stats["known_verdict"] = prefilter_counts["known_verdict"]
    stats["blocked"] = prefilter_counts["blocked_genre"]
    logger.info(
        "Filtrage à froid : %d candidats → %d retenus (%d déjà en librairie, "
        "%d en cooldown, %d déjà jugés, %d genre bloqué)",
        len(tracks), len(candidates),
        prefilter_counts["already_in_library"], prefilter_counts["cooldown"],
        prefilter_counts["known_verdict"], prefilter_counts["blocked_genre"],
    )

    # --- Budget : on ne télécharge que ce qu'on peut espérer diffuser ---
    carryover_on_disk = len(list(DOWNLOAD_DIR.glob("*.mp3")))
    budget = compute_budget(carryover_on_disk)
    stats["carryover_on_disk"] = carryover_on_disk
    stats["budget"] = budget
    logger.info(
        "Budget de la nuit : %d (quota %d × marge %.1f − %d en attente sur disque)",
        budget, ROTATION.max_uploads_per_night, ROTATION.download_margin,
        carryover_on_disk,
    )
    if budget == 0:
        logger.info("Stock suffisant : aucun téléchargement cette nuit.")
    tracks_to_download = candidates[:budget]
```

Ajouter les trois compteurs neufs à l'initialisation de `stats` :

```python
        # Phase à froid (2026-07) : ce qui a été écarté sans rien télécharger.
        "prefiltered": 0,
        "known_verdict": 0,
        "budget": 0,
        "carryover_on_disk": 0,
```

- [ ] **Step 7 : Faire porter la boucle sur `tracks_to_download`**

Remplacer, dans `_process_track`, `len(tracks)` par `len(tracks_to_download)`, et dans la construction des futures, `for i, track in enumerate(tracks, 1)` par `for i, track in enumerate(tracks_to_download, 1)`.

- [ ] **Step 8 : Alléger `download_track`**

Les vérifications désormais faites en phase à froid sont retirées de `download_track` : le bloc de filtrage par genre (`if genre_client and GENRE_FILTER.enabled:` et tout son corps) et le bloc de cooldown (`if track_db and track_db.is_in_cooldown(...)`).

Les tags Last.fm restent nécessaires à l'écriture ID3. Les récupérer depuis le cache, sans nouvel appel réseau coûteux :

```python
    # Tags pour l'ID3 : la phase à froid a déjà rempli le cache de genres,
    # cet appel est servi localement.
    has_lastfm_tags = False
    lastfm_tags_str = ""
    if genre_client and GENRE_FILTER.enabled:
        genre_result = genre_client.check_genre(artist, title)
        has_lastfm_tags = bool(genre_result.tags)
        lastfm_tags_str = ", ".join(genre_result.tags) if genre_result.tags else ""
```

Conserver intacte la déduplication par clé sous `_download_lock` : elle protège contre deux workers visant la même clé, ce que la phase à froid ne couvre pas.

- [ ] **Step 9 : Inscrire au registre les durées hors bornes**

Dans `download_track`, aux deux `return DownloadOutcome('filtered', match_source)` du contrôle de durée, insérer juste avant :

```python
            if track_db is not None:
                track_db.record_verdict(
                    track_key, "filtered_duration",
                    reason=f"{int(match['duration'])}s hors bornes",
                )
```

- [ ] **Step 10 : Mettre à jour le résumé de fin**

Remplacer le bloc de `logger.info("\n=== Results ===")` par :

```python
    logger.info("\n=== Results ===")
    logger.info("Candidats : %d → retenus %d → budget %d",
                len(tracks), len(candidates), budget)
    logger.info("Écartés avant téléchargement : %d", stats['prefiltered'])
    logger.info("  → déjà en librairie ou cooldown : %d", stats['skipped'])
    logger.info("  → déjà jugés (registre)         : %d", stats['known_verdict'])
    logger.info("  → genre bloqué                  : %d", stats['blocked'])
    logger.info("Téléchargés : %d", stats['downloaded'])
    logger.info("  → depuis YouTube    : %d", stats['source_youtube'])
    logger.info("  → depuis SoundCloud : %d", stats['source_soundcloud'])
    if stats['source_other']:
        logger.info("  → depuis autre      : %d", stats['source_other'])
    logger.info("Doublon audio (empreinte) : %d", stats['duplicate'])
    logger.info("Filtré (durée) : %d", stats['filtered'])
    logger.info("Échecs : %d", stats['failed'])
    if stats['loudnorm_failed']:
        logger.warning("Loudnorm en échec (uploads non normalisés) : %d", stats['loudnorm_failed'])
    if stats['fingerprint_failed']:
        logger.warning("Empreinte en échec (dédup sautée) : %d", stats['fingerprint_failed'])
```

- [ ] **Step 11 : Lancer la suite complète**

Run : `python3 -m pytest tests/ -q`
Expected : PASS

- [ ] **Step 12 : Vérifier que le module se charge et que le budget est correct**

Run :
```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from download import compute_budget
from config import ROTATION
print('quota', ROTATION.max_uploads_per_night, 'marge', ROTATION.download_margin)
for c in (0, 6, 12, 24):
    print(f'carryover {c:>3} -> budget {compute_budget(c)}')
"
```
Expected : `carryover  24 -> budget 0`, `carryover   0 -> budget 12`

- [ ] **Step 13 : Commit**

```bash
git add scripts/download.py config.py tests/test_download_prefilter.py
git commit -m "feat(download): filtrage à froid et budget adossé au stock"
```

---

### Task 6 : Récap WhatsApp fiable

**Files:**
- Modify: `scripts/send_daily_recap.py`
- Test: `tests/test_daily_recap.py` (créé)

**Interfaces:**
- Consumes : `data/last_reconcile.json` (Task 3), les compteurs neufs de `data/last_download_stats.json` (Task 5).
- Produces : `build_message(stats: dict[str, dict]) -> str`, `send_ntfy(text: str) -> bool`, `data/last_recap.txt`.

- [ ] **Step 1 : Écrire les tests qui échouent**

Créer `tests/test_daily_recap.py` :

```python
"""Chaque chiffre du récap doit dire ce qu'il dit."""

from send_daily_recap import MAX_MESSAGE_CHARS, build_message, truncate


def _stats(**over):
    base = {
        "reconcile": {"az_files": 666, "ghosts_cleared": 0, "keys_repaired": 0,
                      "disk_files": 666, "disk_drift": 0},
        "tiers": {"GOLD": 35, "HEAVY": 217, "MEDIUM": 224, "LIGHT": 198},
        "discover": {"raw_total": 354, "deduped_total": 30},
        "download": {"downloaded": 12, "prefiltered": 18, "failed": 0,
                     "loudnorm_failed": 0, "fingerprint_failed": 0},
        "classify": {"uploaded": 6, "rejected": 0, "quota": 5, "carryover": 24,
                     "rotation_deleted": 0},
    }
    base.update(over)
    return base


def test_quota_and_taste_rejections_are_separate_lines():
    """Le bug du 26/07 : 5 évincés annoncés comme hors couleur."""
    msg = build_message(_stats())
    assert "5 évincés" in msg
    assert "quota" in msg
    # rejected == 0 : aucune ligne « hors couleur » ne doit apparaître.
    assert "hors couleur" not in msg


def test_taste_rejection_line_appears_when_nonzero():
    msg = build_message(_stats(classify={"uploaded": 6, "rejected": 3, "quota": 0,
                                          "carryover": 0, "rotation_deleted": 0}))
    assert "3 hors couleur" in msg
    assert "évincés" not in msg


def test_library_state_is_reported():
    msg = build_message(_stats())
    assert "666 titres" in msg
    assert "35 GOLD" in msg


def test_no_alert_block_when_healthy():
    msg = build_message(_stats())
    assert "⚠" not in msg


def test_ghosts_raise_an_alert():
    msg = build_message(_stats(reconcile={"az_files": 666, "ghosts_cleared": 8,
                                          "keys_repaired": 0, "disk_files": 666,
                                          "disk_drift": 0}))
    assert "⚠" in msg
    assert "8" in msg


def test_disk_drift_raises_an_alert():
    msg = build_message(_stats(reconcile={"az_files": 666, "ghosts_cleared": 0,
                                          "keys_repaired": 0, "disk_files": 670,
                                          "disk_drift": 4}))
    assert "⚠" in msg
    assert "dossier" in msg.lower()


def test_loudnorm_failure_raises_an_alert():
    msg = build_message(_stats(download={"downloaded": 12, "prefiltered": 18,
                                         "failed": 0, "loudnorm_failed": 3,
                                         "fingerprint_failed": 0}))
    assert "⚠" in msg
    assert "loudnorm" in msg.lower()


def test_empty_stats_do_not_crash():
    msg = build_message({})
    assert "AubeSonore" in msg
    assert msg.strip()


def test_message_stays_within_url_budget():
    msg = build_message(_stats())
    assert len(msg) <= MAX_MESSAGE_CHARS


def test_truncate_cuts_on_a_line_boundary():
    text = "ligne un\nligne deux\nligne trois"
    out = truncate(text, 20)
    assert out == "ligne un…"


def test_truncate_leaves_short_text_alone():
    assert truncate("court", 20) == "court"
```

- [ ] **Step 2 : Lancer les tests et vérifier qu'ils échouent**

Run : `python3 -m pytest tests/test_daily_recap.py -q`
Expected : FAIL — `ImportError: cannot import name 'MAX_MESSAGE_CHARS'`

- [ ] **Step 3 : Réécrire `build_message` et ses annexes**

Dans `scripts/send_daily_recap.py`, remplacer `build_message` et ajouter les fonctions voisines :

```python
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
NTFY_URL = "https://ntfy.sh"
# Le texte part en paramètre d'une URL GET : au-delà, CallMeBot tronque
# ou refuse. On coupe nous-mêmes, proprement, sur une frontière de ligne.
MAX_MESSAGE_CHARS = 900


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Couper sur une frontière de ligne, en signalant la coupe."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if "\n" in cut:
        cut = cut[: cut.rindex("\n")]
    return cut + "…"


def collect_stats() -> dict[str, dict]:
    """Rassembler les fichiers de stats écrits par les étapes du pipeline."""
    stats = {
        "reconcile": _read("last_reconcile.json"),
        "discover": _read("last_discover_stats.json"),
        "download": _read("last_download_stats.json"),
        "classify": _read("last_classify_stats.json"),
        "tiers": {},
    }
    try:
        from track_db import TrackDB

        db = TrackDB(DATA_DIR / "tracks.db")
        try:
            rows = db.conn.execute(
                """SELECT tier, COUNT(*) AS n FROM tracks
                   WHERE azuracast_file_id IS NOT NULL AND deleted_at IS NULL
                   GROUP BY tier"""
            ).fetchall()
            stats["tiers"] = {r["tier"] or "DISCOVERY": r["n"] for r in rows}
        finally:
            db.close()
    except Exception as e:  # base absente ou verrouillée : le récap continue
        logger.warning("Répartition par tier indisponible : %s", e)
    return stats


def _alerts(stats: dict[str, dict]) -> list[str]:
    """Les lignes qui ne doivent apparaître que si quelque chose cloche."""
    rec = stats.get("reconcile") or {}
    dl = stats.get("download") or {}
    out = []

    if rec.get("ghosts_cleared"):
        out.append(f"⚠️ {rec['ghosts_cleared']} fantômes en base corrigés")
    if rec.get("keys_repaired"):
        out.append(f"⚠️ {rec['keys_repaired']} clés réparées (métadonnées modifiées)")
    if rec.get("disk_drift"):
        out.append(
            f"⚠️ Dossier désynchronisé : {rec.get('disk_files')} fichiers "
            f"sur disque vs {rec.get('az_files')} vus par l'API"
        )
    if dl.get("loudnorm_failed"):
        out.append(f"⚠️ {dl['loudnorm_failed']} titres non normalisés (loudnorm)")
    if dl.get("fingerprint_failed", 0) > 2:
        out.append(f"⚠️ {dl['fingerprint_failed']} empreintes en échec (dédup partielle)")
    if dl.get("failed", 0) > 5:
        out.append(f"⚠️ {dl['failed']} téléchargements en échec")
    return out


def build_message(stats: dict[str, dict]) -> str:
    """
    Composer le récap.

    Trois blocs : l'état de la radio, ce qui a bougé cette nuit, et — s'il
    y a lieu seulement — les alertes. Chaque compteur dit exactement ce
    qu'il mesure : une éviction par quota n'est pas un rejet de goût.
    """
    rec = stats.get("reconcile") or {}
    tiers = stats.get("tiers") or {}
    discover = stats.get("discover") or {}
    download = stats.get("download") or {}
    classify = stats.get("classify") or {}

    lines = [f"🎵 AubeSonore — {datetime.now().strftime('%d/%m')}", "─" * 16]

    if rec.get("az_files"):
        lines.append(f"📻 Radio : {rec['az_files']} titres")
    if tiers:
        parts = []
        for label, key in (("GOLD", "GOLD"), ("heavy", "HEAVY"),
                           ("medium", "MEDIUM"), ("light", "LIGHT")):
            if tiers.get(key):
                parts.append(f"{tiers[key]} {label}")
        if parts:
            lines.append("   " + " · ".join(parts))

    if classify:
        lines.append("")
        lines.append(f"➕ {classify.get('uploaded', 0)} ajoutés · "
                     f"🗑 {classify.get('rotation_deleted', 0)} retirés")
        if classify.get("carryover"):
            lines.append(f"💎 {classify['carryover']} en attente pour demain")
        # Deux motifs distincts, jamais additionnés : le quota juge le
        # calendrier, le filtre de goût juge le morceau.
        if classify.get("quota"):
            lines.append(f"🔇 {classify['quota']} évincés (quota plein, pas un rejet)")
        if classify.get("rejected"):
            lines.append(f"🚫 {classify['rejected']} hors couleur")

    if discover or download:
        lines.append("")
        lines.append(f"🔍 {discover.get('deduped_total', 0)} candidats → "
                     f"{download.get('downloaded', 0)} téléchargés")
        if download.get("prefiltered"):
            lines.append(f"   {download['prefiltered']} écartés avant DL "
                         f"(déjà vus, genre, durée)")

    alerts = _alerts(stats)
    if alerts:
        lines.append("")
        lines.extend(alerts)

    if len(lines) <= 2:
        lines.append("Aucune activité cette nuit.")
    return truncate("\n".join(lines))
```

- [ ] **Step 4 : Ajouter le repli ntfy et la trace sur disque**

Remplacer `main()` par :

```python
def send_ntfy(text: str) -> bool:
    """Repli quand WhatsApp ne passe pas : un récap manqué doit se voir."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    try:
        req = urllib.request.Request(
            f"{NTFY_URL}/{topic}",
            data=text.encode("utf-8"),
            headers={"Title": "AubeSonore — récap (repli WhatsApp)",
                     "Priority": "default"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except Exception as e:
        logger.warning("Repli ntfy échoué : %s", e)
        return False


def main() -> int:
    message = build_message(collect_stats())
    logger.info("%s", message)

    # Trace consultable même quand aucun envoi n'aboutit.
    try:
        (DATA_DIR / "last_recap.txt").write_text(message, encoding="utf-8")
    except OSError:
        logger.warning("Écriture de last_recap.txt impossible")

    settings = get_settings()
    if not settings.callmebot_apikey or not settings.whatsapp_phone:
        logger.info("CallMeBot non configuré — envoi WhatsApp sauté")
        return 0

    if not send_whatsapp(message, settings.whatsapp_phone, settings.callmebot_apikey):
        # Sans ceci, une panne CallMeBot (incident du 18/07) rend le récap
        # silencieux : on croit que la nuit s'est bien passée sans preuve.
        if send_ntfy(message):
            logger.info("Récap basculé sur ntfy")
    return 0  # best-effort : le récap ne fait jamais échouer la nuit
```

Ajouter `import os` en tête du fichier, et **supprimer** `from config import ROTATION` (ligne 29) : le nouveau message n'affiche plus le quota comme dénominateur, cet import devient mort. Vérifier avec :

```bash
grep -n "ROTATION" scripts/send_daily_recap.py
```
Expected : aucun résultat.

- [ ] **Step 5 : Lancer les tests du récap**

Run : `python3 -m pytest tests/test_daily_recap.py -q`
Expected : PASS

- [ ] **Step 6 : Vérifier le rendu sur les données réelles**

Run : `python3 scripts/send_daily_recap.py`
Expected : le message s'affiche, avec « 5 évincés (quota plein, pas un rejet) » et aucune ligne « hors couleur » — la correction du bug constaté le 26/07.

- [ ] **Step 7 : Lancer la suite complète**

Run : `python3 -m pytest tests/ -q`
Expected : PASS

- [ ] **Step 8 : Commit**

```bash
git add scripts/send_daily_recap.py tests/test_daily_recap.py
git commit -m "fix(recap): distinguer éviction et rejet, état de la radio, repli ntfy"
```

---

### Task 7 : Vérification sur données réelles

**Files:**
- Modify: `MAINTENANCE.md`

**Interfaces:**
- Consumes : tout ce qui précède.
- Produces : aucune interface de code.

- [ ] **Step 1 : Mesurer l'état avant réconciliation**

Run :
```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from track_db import TrackDB
db = TrackDB('data/tracks.db')
print('DB active avant :', len(db.get_active_tracks()))
db.close()
"
```
Noter le chiffre. Attendu : 674.

- [ ] **Step 2 : Réconcilier pour de vrai**

Run :
```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from pathlib import Path
from settings import get_settings
from http_client import AzuraCastClient
from track_db import TrackDB
from library_state import reconcile
s = get_settings()
c = AzuraCastClient(base_url=s.azuracast_url, api_key=s.azuracast_api_key,
                    station_id=s.azuracast_station_id, timeout=s.http_timeout)
db = TrackDB('data/tracks.db')
media = Path(s.azuracast_media_dir) if s.azuracast_media_dir else None
r = reconcile(c.get_station_files(), db, media_dir=media)
print(r)
print('DB active après :', len(db.get_active_tracks()))
db.close()
"
```
Expected : `ghosts_cleared=8`, et `DB active après` égal au nombre de fichiers AzuraCast.

- [ ] **Step 3 : Vérifier qu'une seconde réconciliation ne corrige plus rien**

Relancer exactement la commande de l'étape 2.
Expected : `ghosts_cleared=0`, `untracked_registered=0`, `keys_repaired=0`. La réconciliation est idempotente.

- [ ] **Step 4 : Vérifier le budget sur l'état réel du disque**

Run :
```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from pathlib import Path
from download import compute_budget
n = len(list(Path('downloads').glob('*.mp3')))
print(f'{n} fichiers en attente → budget {compute_budget(n)}')
"
```
Expected : avec 24 fichiers en attente, budget 0.

- [ ] **Step 5 : Vérifier le récap**

Run : `cat data/last_recap.txt`
Expected : le fichier existe et son contenu correspond au message affiché à la Task 6.

- [ ] **Step 6 : Documenter dans `MAINTENANCE.md`**

Ajouter une section :

```markdown
## Source de vérité et réconciliation

AzuraCast fait autorité sur ce qui existe à l'antenne. `data/tracks.db` est
un cache : il conserve seulement ce qu'AzuraCast ignore (date d'upload
propre au pipeline, compteur de lectures, tier, mood, empreinte).

`scripts/library_state.reconcile()` aligne les deux au début de `download.py`
et de `classify.py`. Il retire les lignes dont le fichier a disparu du
serveur, enregistre les fichiers inconnus, et répare les clés dont les
métadonnées ont dérivé côté AzuraCast.

Le dossier média (`AZURACAST_MEDIA_DIR`, facultatif) est lu pour comparer
son nombre de `.mp3` à celui de l'API. Une divergence remonte dans le récap.
Il n'est jamais utilisé pour supprimer quoi que ce soit.

Réconcilier à la main :

```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from pathlib import Path
from settings import get_settings
from http_client import AzuraCastClient
from track_db import TrackDB
from library_state import reconcile
s = get_settings()
c = AzuraCastClient(base_url=s.azuracast_url, api_key=s.azuracast_api_key,
                    station_id=s.azuracast_station_id, timeout=s.http_timeout)
db = TrackDB('data/tracks.db')
print(reconcile(c.get_station_files(), db,
                media_dir=Path(s.azuracast_media_dir) if s.azuracast_media_dir else None))
db.close()
"
```

## Registre des verdicts

Tout rejet est inscrit dans la table `verdicts` de `data/tracks.db`, avec
son motif. `download.py` la consulte en phase à froid : un morceau déjà
jugé n'est jamais retéléchargé.

Les verdicts `rejected_taste` périment après `TASTE_FILTER.verdict_ttl_days`
(90 jours) — le profil de goût évolue, un morceau écarté sous l'ancien
profil doit pouvoir retenter sa chance. Les autres verdicts portent sur une
propriété stable de l'enregistrement et ne périment pas.

Effacer un verdict à la main pour forcer un nouvel essai :

```bash
sqlite3 data/tracks.db "DELETE FROM verdicts WHERE track_key = 'artiste - titre';"
```

## Budget de téléchargement

`download.py` ne télécharge que `max_uploads_per_night × download_margin`
moins le nombre de `.mp3` déjà présents dans `downloads/`. Avec 24 fichiers
en carryover pour un quota de 6, le budget vaut zéro et aucune nuit de
téléchargement n'a lieu : le stock suffit. C'est le comportement attendu,
pas une panne.
```

- [ ] **Step 7 : Commit**

```bash
git add MAINTENANCE.md
git commit -m "docs: réconciliation, registre des verdicts et budget de téléchargement"
```

---

## Ordre d'exécution

Les tâches 1 et 2 sont indépendantes. Les tâches 3 et 4 dépendent de 1 et 2.
La tâche 5 dépend de 1, 2 et 4. La tâche 6 dépend de 3 et 5. La tâche 7
vient en dernier.

Séquence recommandée : 1 → 2 → 3 → 4 → 5 → 6 → 7.
