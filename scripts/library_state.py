"""
Réconciliation AzuraCast ↔ base SQLite ↔ dossier média.

AzuraCast fait autorité sur ce qui existe réellement à l'antenne. La base
SQLite n'est qu'un cache enrichi (dates d'upload, compteurs de rotation,
mood, empreinte audio) : à chaque run elle est réalignée sur la liste de
fichiers renvoyée par l'API.

Trois écarts sont corrigés :
  - fantôme : une ligne active pointe vers un file_id absent d'AzuraCast
    (fichier supprimé à la main, ou perdu lors d'une mise à jour) ;
  - inconnu : un fichier existe côté serveur sans ligne correspondante ;
  - dérive de clé : AzuraCast a réécrit artiste/titre (sanitization), la
    clé normalisée doit suivre plutôt que de créer un doublon.

Le jeu de clés normalisées effectivement présentes (`library_keys`) est
consommé en amont par download.py et classify.py pour ne pas retélécharger
ce qui est déjà à l'antenne.

Le dossier média n'est lu que pour un contrôle de cohérence : un écart
entre le nombre de .mp3 sur disque et le nombre de fichiers vus par l'API
est une alerte. Il ne déclenche jamais de suppression : le disque ne
décide de rien. Un dossier absent, non monté ou illisible rend le contrôle
indisponible, jamais le run.

Ce module ne fait aucun appel réseau : l'appelant lui passe la liste de
fichiers déjà récupérée.
"""

import logging
import time
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
        """Vrai si rien d'anormal constaté."""
        return (
            self.ghosts_cleared == 0
            and self.keys_repaired == 0
            and not self.disk_drift
        )


def count_media_files(media_dir: Path | None) -> int | None:
    """
    Nombre de .mp3 sous le dossier média, ou None si non vérifiable.

    Jamais d'exception : un dossier non configuré, absent, non monté ou
    illisible rend le contrôle indisponible, pas le run.
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
        media_dir: Dossier média, pour le contrôle de cohérence en lecture seule.

    Returns:
        Rapport chiffré, dont `library_keys` : les clés normalisées
        réellement présentes à l'antenne, pour déduplication en amont.
    """
    report = ReconcileReport(az_files=len(files))

    active = track_db.get_active_tracks()
    report.db_active_before = len(active)
    by_file_id = {t["azuracast_file_id"]: t for t in active}

    seen_ids: set[int] = set()

    for f in files:
        file_id = f.get("id")

        # Marquer le fichier comme vu AVANT tout garde : sa présence est
        # établie par son id, indépendamment de la lisibilité de ses tags.
        # Sortir plus tôt le ferait passer pour un fantôme dans la boucle
        # finale, qui annulerait son azuracast_file_id — la ligne serait
        # alors définitivement orpheline, son embedding CLAP purgé, et le
        # morceau re-téléchargé en doublon alors qu'il est toujours à
        # l'antenne. C'est exactement le scénario de l'incident du 18/07.
        if file_id is not None:
            seen_ids.add(file_id)

        artist = (f.get("artist") or "").strip()
        title = (f.get("title") or "").strip()

        if not (artist and title):
            # Sans métadonnées on ne peut ni construire de clé ni dédupliquer.
            # Le fichier reste à l'antenne, il est simplement invisible ici —
            # mais il n'est pas déclaré disparu.
            logger.warning(
                "Fichier AzuraCast sans artiste/titre (id=%s) — ignoré", file_id
            )
            continue

        key = normalize_track_key(artist, title)
        report.library_keys.add(key)

        if file_id is None:
            continue

        known = by_file_id.get(file_id)
        if known is None:
            # Fichier présent côté serveur sans ligne locale : upload
            # manuel, suppression accidentelle de la ligne, ou base repartie
            # de zéro. On l'adopte pour ne pas le retélécharger.
            # uploaded_at n'est écrit qu'une fois (register_untracked_file ne
            # le rafraîchit pas) et pilote l'âge, donc le tiering : on prend
            # la date la plus fidèle disponible avant de se rabattre sur
            # maintenant.
            raw_uploaded_at = f.get("uploaded_at") or f.get("mtime")
            try:
                uploaded_at = float(raw_uploaded_at) if raw_uploaded_at else time.time()
            except (TypeError, ValueError):
                # Une date non numérique renvoyée par l'API ne doit pas faire
                # échouer la réconciliation.
                logger.warning(
                    "uploaded_at illisible pour file_id=%s : %r",
                    file_id, raw_uploaded_at,
                )
                uploaded_at = time.time()
            track_db.register_untracked_file(
                key,
                artist,
                title,
                uploaded_at,
                file_id,
            )
            report.untracked_registered += 1
        elif known["track_key"] != key:
            # AzuraCast a réécrit les métadonnées : on suit la clé plutôt
            # que de laisser un doublon s'installer.
            if track_db.repair_track_key(known["track_key"], key):
                logger.info(
                    "Clé réparée : %s -> %s (file_id=%s)",
                    known["track_key"], key, file_id,
                )
                report.keys_repaired += 1

    for file_id, track in by_file_id.items():
        if file_id in seen_ids:
            continue
        # Le fichier a disparu d'AzuraCast (suppression manuelle, incident
        # de mise à jour) : la ligne ne doit plus compter dans la rotation.
        track_db.record_deletion(track["track_key"])
        logger.info(
            "Fantôme retiré : %s - %s (file_id=%s absent d'AzuraCast)",
            track["artist"], track["title"], file_id,
        )
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
