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
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import ROTATION  # noqa: E402
from settings import get_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"


def _read(name: str) -> dict:
    try:
        return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build_message() -> str:
    discover = _read("last_discover_stats.json")
    download = _read("last_download_stats.json")
    classify = _read("last_classify_stats.json")

    lines = [f"🎵 AubeSonore — {datetime.now().strftime('%d/%m')}"]

    if discover:
        lines.append(f"🔍 {discover.get('deduped_total', 0)} candidats "
                     f"({discover.get('raw_total', 0)} bruts)")
    if download:
        dl = download.get("downloaded", 0)
        parts = []
        for key, label in (("blocked", "genre bloqué"), ("filtered", "filtré"),
                           ("skipped", "doublon"), ("failed", "échec")):
            if download.get(key):
                parts.append(f"{download[key]} {label}")
        lines.append(f"⬇️ {dl} téléchargés"
                     + (f" ({', '.join(parts)})" if parts else ""))
    if classify:
        lines.append(f"✅ {classify.get('uploaded', 0)}/"
                     f"{ROTATION.max_uploads_per_night} ajoutés à la radio")
        # One plain number for everything set aside, whatever the filter.
        ecartes = classify.get("rejected", 0) + classify.get("quota", 0)
        if ecartes:
            lines.append(f"🚫 {ecartes} écartés (pas dans la couleur)")
        if classify.get("carryover"):
            lines.append(f"💎 {classify['carryover']} bons candidats gardés pour demain")
        if classify.get("rotation_deleted"):
            lines.append(f"🗑️ {classify['rotation_deleted']} anciens retirés (fin de vie)")

    if len(lines) == 1:
        lines.append("Aucune activité cette nuit.")
    return "\n".join(lines)


def send_whatsapp(text: str, phone: str, apikey: str) -> bool:
    url = CALLMEBOT_URL + "?" + urllib.parse.urlencode(
        {"phone": phone, "apikey": apikey, "text": text})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            logger.info("Recap WhatsApp envoyé (%d car.) → HTTP %s", len(text), r.status)
            return r.status == 200
    except Exception as e:
        logger.warning("Envoi WhatsApp échoué : %s", e)
        return False


def main() -> int:
    settings = get_settings()
    if not settings.callmebot_apikey or not settings.whatsapp_phone:
        logger.info("CallMeBot non configuré — recap sauté")
        return 0
    message = build_message()
    logger.info("%s", message)
    send_whatsapp(message, settings.whatsapp_phone, settings.callmebot_apikey)
    return 0  # best-effort: never fail the pipeline


if __name__ == "__main__":
    sys.exit(main())
