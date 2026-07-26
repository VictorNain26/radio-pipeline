#!/usr/bin/env python3
"""
WhatsApp daily recap via CallMeBot — one message at the end of each
nightly run summarising what was discovered, downloaded, curated,
rejected and rotated.

Reads the per-step stats files (last_discover/download/classify_stats.json)
written by the pipeline. Best-effort by design: any failure logs a
warning and exits 0 so the pipeline never fails because of a recap.

Requires CALLMEBOT_APIKEY and WHATSAPP_PHONE in .env (skipped if unset).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

sys.path.insert(0, str(Path(__file__).parent.parent))

from settings import get_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
NTFY_URL = "https://ntfy.sh"
# Le texte part en paramètre d'une URL GET : au-delà, CallMeBot tronque
# ou refuse. On coupe nous-mêmes, proprement, sur une frontière de ligne.
MAX_MESSAGE_CHARS = 900


def _read(name: str) -> dict:
    """
    Lire un fichier de stats, ou rien.

    Dernier chemin restant vers une sortie non nulle du récap : un fichier
    tronqué en plein caractère lève UnicodeDecodeError (un ValueError, pas
    un JSONDecodeError), et un JSON valide mais non-objet ("null", une
    liste) traverserait la lecture pour casser plus loin sur `.get`.
    """
    try:
        data = json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
        # az_mp3_files, pas az_files : le disque n'est compté qu'en .mp3.
        vus = rec.get("az_mp3_files")
        if vus is None:
            vus = rec.get("az_files")
        out.append(
            f"⚠️ Dossier désynchronisé : {rec.get('disk_files')} .mp3 "
            f"sur disque vs {vus} vus par l'API"
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

    # Le total et sa répartition sortent de la MÊME requête, au même instant.
    # az_files datait de la réconciliation de classify, donc d'avant les
    # uploads et les suppressions de rotation de la nuit, tandis que les tiers
    # sont comptés en base à l'heure du récap : le parent et ses enfants
    # différaient de `uploaded − rotation_deleted` chaque nuit.
    # az_files ne sert plus que de repli quand la base est indisponible — il
    # n'y a alors aucune répartition en dessous pour le contredire.
    total = sum(tiers.values()) if tiers else (rec.get("az_files") or 0)
    if total:
        lines.append(f"📻 Radio : {total} titres")
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
        # Trois motifs distincts, jamais additionnés : le quota juge le
        # calendrier, le filtre de goût juge la couleur du morceau, et le
        # reste (mood désactivé, BPM, durée, signal multi-source) juge les
        # réglages du moment. « hors couleur » ne recouvre que le deuxième.
        if classify.get("quota"):
            lines.append(f"🔇 {classify['quota']} évincés (quota plein, pas un rejet)")
        if classify.get("rejected_taste"):
            lines.append(f"🚫 {classify['rejected_taste']} hors couleur")
        if classify.get("rejected_other"):
            lines.append(f"⚙️ {classify['rejected_other']} écartés "
                         f"(réglages, durée, signal)")

    if discover or download:
        lines.append("")
        lines.append(f"🔍 {discover.get('deduped_total', 0)} candidats → "
                     f"{download.get('downloaded', 0)} téléchargés")
        if download.get("prefiltered"):
            # Les six motifs réels de la phase à froid. La durée n'en est
            # pas : ce filtre-là tourne à chaud, sur le fichier téléchargé.
            lines.append(f"   {download['prefiltered']} écartés avant DL "
                         f"(à l'antenne, doublon du lot, cooldown, "
                         f"déjà jugé, genre, sans métadonnées)")
        # Le budget vaut 0 quand downloads/ est déjà plein : sans cette
        # ligne, plusieurs nuits à « 0 téléchargés » se lisent comme un
        # pipeline en panne. `== 0` et non `not` : une clé absente d'un
        # vieux fichier de stats vaut None, et ne doit rien afficher.
        if download.get("budget") == 0 and download.get("carryover_on_disk"):
            lines.append(f"💤 Stock suffisant : {download['carryover_on_disk']} "
                         f"en attente, aucun téléchargement")

    alerts = _alerts(stats)
    if alerts:
        lines.append("")
        lines.extend(alerts)

    if len(lines) <= 2:
        lines.append("Aucune activité cette nuit.")
    return truncate("\n".join(lines))


def send_whatsapp(text: str, phone: str, apikey: str) -> bool:
    url = CALLMEBOT_URL + "?" + urllib.parse.urlencode(
        {"phone": phone, "apikey": apikey, "text": text})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            body = r.read().decode(errors="replace")
            if r.status == 200:
                logger.info("Recap WhatsApp envoyé (%d car.)", len(text))
                return True
            # CallMeBot signale ses pannes en 207 avec l'explication dans
            # le HTML (ex. "Service is down (410)") — la logger, sinon
            # l'échec est indistinguable d'un problème de clé.
            plain = " ".join(re.sub("<[^>]+>", " ", body).split())
            logger.warning("Envoi WhatsApp refusé : HTTP %s — %s",
                           r.status, plain[:300])
            return False
    except Exception as e:
        logger.warning("Envoi WhatsApp échoué : %s", e)
        return False


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

    try:
        settings = get_settings()
    except Exception as e:
        # La contrainte du récap est absolue : il ne fait jamais échouer
        # la nuit. Un .env absent ou invalide ne doit pas transformer un
        # message manqué en code de sortie non nul.
        logger.warning("Réglages illisibles — envoi sauté : %s", e)
        return 0

    if not settings.callmebot_apikey or not settings.whatsapp_phone:
        # Le repli ntfy existe pour qu'un récap ne disparaisse jamais en
        # silence. Sortir avant lui le rendait inatteignable dans le cas
        # même qu'il devait couvrir : WhatsApp indisponible.
        logger.info("CallMeBot non configuré — bascule sur ntfy")
        if send_ntfy(message):
            logger.info("Récap envoyé via ntfy")
        return 0

    if not send_whatsapp(message, settings.whatsapp_phone, settings.callmebot_apikey):
        # Sans ceci, une panne CallMeBot (incident du 18/07) rend le récap
        # silencieux : on croit que la nuit s'est bien passée sans preuve.
        if send_ntfy(message):
            logger.info("Récap basculé sur ntfy")
    return 0  # best-effort : le récap ne fait jamais échouer la nuit


if __name__ == "__main__":
    sys.exit(main())
