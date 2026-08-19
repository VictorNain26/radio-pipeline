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

Parce qu'elle désactive tout ce qu'AzuraCast ne lui montre pas, la
réconciliation refuse de tourner sur une liste invraisemblable : voir
RECONCILE_MIN_FILES / RECONCILE_MIN_RATIO et LibraryStateError.

Ce module ne fait aucun appel réseau : l'appelant lui passe la liste de
fichiers déjà récupérée. Il persiste en revanche le rapport de la nuit
(`report_path`), en cumulant les deux passes du run — voir _persist_report.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from track_db import TrackDB, normalize_track_key

logger = logging.getLogger(__name__)


class LibraryStateError(Exception):
    """Réconciliation refusée : l'état source est invraisemblable."""


# Garde-fous sur la liste reçue d'AzuraCast. reconcile() désactive TOUTE ligne
# active dont le file_id manque à cette liste : un 200 tronqué ou vide (glitch
# d'API, proxy, mise à jour en cours) désactiverait la librairie entière, et
# enforce_tiered_rotation purgerait dans la foulée le store CLAP contre un
# valid_keys vide — 40 à 55 minutes de vecteurs perdus, irrécupérables sans
# les fichiers audio.
#
# Le compromis est asymétrique, d'où les seuils : une suppression massive
# volontaire est rare et se rattrape (relancer en desserrant le seuil) ; un
# aléa réseau est fréquent et ce qu'il détruit ne se rattrape pas.
#
# Le plancher ne s'applique qu'aux bases qui comptent déjà au moins
# RECONCILE_MIN_FILES lignes actives : en dessous, une liste courte est la
# réalité, pas un symptôme. L'appliquer inconditionnellement condamnerait
# une petite bibliothèque à lever chaque nuit — download.py sortirait en 1,
# classify.py sauterait la rotation, plus rien ne serait téléchargé, et elle
# ne pourrait donc jamais repasser au-dessus du plancher. Le ratio, lui,
# reste armé à toute taille : c'est lui qui rattrape l'effondrement soudain
# d'une petite bibliothèque.
RECONCILE_MIN_FILES = 50
RECONCILE_MIN_RATIO = 0.5

# Fenêtre pendant laquelle deux réconciliations sont réputées appartenir à la
# même nuit (download.py au début, classify.py plus tard). Voir _persist_report.
RECONCILE_REPORT_MERGE_WINDOW_S = 6 * 3600


@dataclass
class ReconcileReport:
    """Ce que la réconciliation a constaté et corrigé."""

    az_files: int = 0
    # Sous-ensemble .mp3 de az_files, seule grandeur comparable au disque.
    # None = l'API n'expose pas les chemins : comparaison impossible.
    az_mp3_files: int | None = None
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


def _count_az_mp3(files: list[dict[str, Any]]) -> int | None:
    """
    Nombre de .mp3 parmi les fichiers vus par l'API, ou None si indécidable.

    Comparer le total API au nombre de .mp3 sur disque ferait passer le
    moindre upload non-mp3 (jingle wav, ogg) pour une désynchro permanente.
    On compare ce qui est comparable. Si aucun fichier n'expose de chemin,
    le contrôle est indisponible — jamais faussement alarmant.
    """
    paths = [str(f.get("path") or f.get("filename") or "") for f in files]
    if not any(paths):
        return None
    return sum(1 for p in paths if p.lower().endswith(".mp3"))


def _persist_report(report: ReconcileReport, path: Path) -> None:
    """
    Écrire (ou fusionner) le rapport de la nuit, pour le récap quotidien.

    Une nuit réconcilie deux fois : download.py au démarrage, puis classify.py
    avant la rotation. La première passe corrige tout ; la seconde ne voit
    plus rien à corriger. Écraser donnerait un rapport à zéro et l'alerte
    « N fantômes corrigés » ne partirait jamais. On somme donc les corrections
    sur la fenêtre d'un run.

    Appartenance au même run : la mtime du fichier précédent. Un run est une
    chaîne d'une à deux heures lancée une fois par jour ; deux runs distincts
    sont à 24 h d'écart, très au-delà de la fenêtre. Et une relance manuelle
    dans la fenêtre n'invente rien : reconcile est idempotente, la seconde
    passe ajoute 0 à chaque compteur.

    Les grandeurs d'état (fichiers vus, disque, lignes actives) ne se somment
    pas : le dernier écrivain fait foi, c'est la photo la plus récente.

    Jamais fatale : un récap muet vaut mieux qu'une nuit interrompue.
    """
    payload = {
        "az_files": report.az_files,
        "az_mp3_files": report.az_mp3_files,
        "db_active_before": report.db_active_before,
        "ghosts_cleared": report.ghosts_cleared,
        "untracked_registered": report.untracked_registered,
        "keys_repaired": report.keys_repaired,
        "disk_files": report.disk_files,
        "disk_drift": report.disk_drift,
    }
    # La lecture de fusion a son propre garde : un fichier précédent illisible
    # ou corrompu fait perdre le cumul, pas le rapport de la nuit. Les deux
    # passes échoueraient sinon à écrire et le récap lirait le fichier
    # corrompu — les corrections de la nuit ne seraient jamais rapportées.
    cumulables = ("ghosts_cleared", "untracked_registered", "keys_repaired")
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < RECONCILE_REPORT_MERGE_WINDOW_S:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                # Tout convertir avant d'ajouter quoi que ce soit : un champ
                # illisible ne doit pas laisser un cumul à moitié appliqué.
                report_precedent = {
                    c: int(previous.get(c) or 0) for c in cumulables
                }
                for c in cumulables:
                    payload[c] += report_precedent[c]
    except (OSError, ValueError, TypeError) as e:
        logger.warning("Rapport précédent %s illisible, cumul abandonné : %s", path, e)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError) as e:
        logger.warning("Écriture de %s impossible : %s", path, e)


def reconcile(
    files: list[dict[str, Any]],
    track_db: TrackDB,
    media_dir: Path | None = None,
    report_path: Path | None = None,
) -> ReconcileReport:
    """
    Aligner la base sur la liste de fichiers AzuraCast.

    Args:
        files: Fichiers tels que renvoyés par l'API AzuraCast.
        track_db: Base persistante à réconcilier.
        media_dir: Dossier média, pour le contrôle de cohérence en lecture seule.
        report_path: Où persister le rapport pour le récap. None = ne rien
            écrire (défaut : aucun appel ne touche le disque par accident,
            les tests notamment).

    Returns:
        Rapport chiffré, dont `library_keys` : les clés normalisées
        réellement présentes à l'antenne, pour déduplication en amont.

    Raises:
        LibraryStateError: La liste reçue est inexploitable ou trop courte
            pour être crue. Rien n'a été modifié.
    """
    if not isinstance(files, list) or any(not isinstance(f, dict) for f in files):
        # Un 200 portant {"error": ...} ferait len() sur des clés et f.get()
        # sur une chaîne : mieux vaut refuser que réconcilier contre du bruit.
        raise LibraryStateError(
            f"réponse AzuraCast inexploitable : {type(files).__name__} "
            "au lieu d'une liste d'objets — réconciliation abandonnée"
        )

    report = ReconcileReport(az_files=len(files))

    active = track_db.get_active_tracks()
    report.db_active_before = len(active)

    if report.db_active_before:
        # Plancher : seulement si la base est elle-même au-dessus du plancher,
        # sinon une petite bibliothèque saine serait refusée à chaque run.
        sous_le_plancher = (
            report.db_active_before >= RECONCILE_MIN_FILES
            and len(files) < RECONCILE_MIN_FILES
        )
        sous_le_ratio = len(files) < RECONCILE_MIN_RATIO * report.db_active_before
        if sous_le_plancher or sous_le_ratio:
            raise LibraryStateError(
                f"liste AzuraCast suspecte : {len(files)} fichiers pour "
                f"{report.db_active_before} lignes actives — réconciliation abandonnée"
            )

    by_file_id = {t["azuracast_file_id"]: t for t in active}

    seen_ids: set[int] = set()
    # Clés que la boucle des fichiers a (ré)activées. L'instantané by_file_id
    # date d'AVANT : si AzuraCast a réattribué l'id d'un morceau, il y reste
    # sous son ancien id et la boucle des fantômes désactiverait la ligne
    # qu'on vient de réparer — même clé, décision inverse.
    claimed_keys: set[str] = set()

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
            claimed_keys.add(key)
        elif known["track_key"] != key:
            # AzuraCast a réécrit les métadonnées : on suit la clé plutôt
            # que de laisser un doublon s'installer.
            if track_db.repair_track_key(known["track_key"], key):
                logger.info(
                    "Clé réparée : %s -> %s (file_id=%s)",
                    known["track_key"], key, file_id,
                )
                report.keys_repaired += 1
                claimed_keys.add(key)

    for file_id, track in by_file_id.items():
        if file_id in seen_ids or track["track_key"] in claimed_keys:
            continue
        # Le fichier a disparu d'AzuraCast (suppression manuelle, incident
        # de mise à jour) : la ligne ne doit plus compter dans la rotation.
        track_db.record_deletion(track["track_key"])
        logger.info(
            "Fantôme retiré : %s - %s (file_id=%s absent d'AzuraCast)",
            track["artist"], track["title"], file_id,
        )
        report.ghosts_cleared += 1

    report.az_mp3_files = _count_az_mp3(files)
    report.disk_files = count_media_files(media_dir)
    if report.disk_files is not None and report.az_mp3_files is not None:
        report.disk_drift = abs(report.disk_files - report.az_mp3_files)
        if report.disk_drift:
            logger.warning(
                "Désynchro dossier/API : %d .mp3 sur disque, %d .mp3 vus par l'API",
                report.disk_files, report.az_mp3_files,
            )

    logger.info(
        "Réconciliation : %d fichiers AzuraCast | %d fantômes retirés | "
        "%d inconnus enregistrés | %d clés réparées",
        report.az_files, report.ghosts_cleared,
        report.untracked_registered, report.keys_repaired,
    )

    if report_path is not None:
        _persist_report(report, report_path)
    return report
