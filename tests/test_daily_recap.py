"""Chaque chiffre du récap doit dire ce qu'il dit."""

import pytest

import send_daily_recap
from send_daily_recap import MAX_MESSAGE_CHARS, build_message, truncate


def _stats(**over):
    base = {
        "reconcile": {"az_files": 666, "ghosts_cleared": 0, "keys_repaired": 0,
                      "disk_files": 666, "disk_drift": 0},
        "tiers": {"GOLD": 35, "HEAVY": 217, "MEDIUM": 224, "LIGHT": 198},
        "discover": {"raw_total": 354, "deduped_total": 30},
        "download": {"downloaded": 12, "prefiltered": 18, "failed": 0,
                     "loudnorm_failed": 0, "fingerprint_failed": 0,
                     "budget": 12, "carryover_on_disk": 0},
        "classify": {"uploaded": 6, "rejected_taste": 0, "rejected_other": 0,
                     "rejected": 0, "quota": 5, "carryover": 24,
                     "rotation_deleted": 0},
    }
    base.update(over)
    return base


def _classify(**over):
    base = {"uploaded": 6, "rejected_taste": 0, "rejected_other": 0,
            "quota": 5, "carryover": 24, "rotation_deleted": 0}
    base.update(over)
    return base


def test_quota_and_taste_rejections_are_separate_lines():
    """Le bug du 26/07 : 5 évincés annoncés comme hors couleur."""
    msg = build_message(_stats())
    assert "5 évincés" in msg
    assert "quota" in msg
    # rejected_taste == 0 : aucune ligne « hors couleur » ne doit apparaître.
    assert "hors couleur" not in msg


def test_quota_and_taste_rejections_coexist_without_merging():
    """Les deux motifs à la fois : une re-fusion afficherait « 8 » et une seule ligne."""
    msg = build_message(_stats(classify=_classify(rejected_taste=3)))
    assert "5 évincés" in msg
    assert "3 hors couleur" in msg
    # Une re-fusion des deux compteurs afficherait leur somme sous un seul
    # libellé — exactement le bug du 26/07. On vise la somme sous chacun des
    # libellés, et le libellé fautif d'origine. (« 8 » nu matcherait le tier
    # 198 et « 18 écartés avant DL » : trop lâche pour assurer quoi que ce soit.)
    assert "8 évincés" not in msg
    assert "8 hors couleur" not in msg
    assert "écartés (pas dans la couleur)" not in msg


def test_taste_rejection_line_appears_when_nonzero():
    msg = build_message(_stats(classify=_classify(rejected_taste=3, quota=0,
                                                  carryover=0)))
    assert "3 hors couleur" in msg
    assert "évincés" not in msg


def test_other_rejections_are_not_announced_as_taste():
    """Mood désactivé, BPM, durée, multi-signal : ce n'est pas « hors couleur »."""
    msg = build_message(_stats(classify=_classify(rejected_other=4)))
    assert "hors couleur" not in msg
    assert "4 écartés" in msg


def test_taste_and_other_rejections_are_two_lines():
    msg = build_message(_stats(classify=_classify(rejected_taste=3,
                                                  rejected_other=4)))
    assert "3 hors couleur" in msg
    assert "4 écartés" in msg
    assert "7 hors couleur" not in msg


def test_library_state_is_reported():
    msg = build_message(_stats())
    assert "35 GOLD" in msg


def test_headline_total_matches_the_tier_breakdown():
    """Parent et enfants doivent sortir du même instant : leur somme est le total.

    az_files (666) est la photo prise par la réconciliation de classify,
    AVANT les uploads et les suppressions de rotation de la nuit ; les tiers
    (674) sont comptés à l'heure du récap. Publier l'un au-dessus des autres
    affichait deux états de la radio à quelques minutes d'écart.
    """
    stats = _stats()
    msg = build_message(stats)
    total = sum(stats["tiers"].values())
    assert f"{total} titres" in msg
    assert "666 titres" not in msg


def test_headline_falls_back_to_az_files_without_tiers():
    """Base indisponible : il ne reste que le compte de l'API, sans enfants à contredire."""
    msg = build_message(_stats(tiers={}))
    assert "666 titres" in msg
    assert "GOLD" not in msg


def test_tier_line_is_indented_under_its_parent():
    msg = build_message(_stats())
    lignes = msg.split("\n")
    parent = next(i for i, ligne in enumerate(lignes) if "titres" in ligne)
    assert lignes[parent + 1].startswith("   ")
    assert "35 GOLD" in lignes[parent + 1]


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


# ---------------------------------------------------------------------------
# Budget : « 0 téléchargés » doit se distinguer d'un pipeline en panne
# ---------------------------------------------------------------------------

def test_zero_budget_is_explained():
    msg = build_message(_stats(download={"downloaded": 0, "prefiltered": 18,
                                         "failed": 0, "loudnorm_failed": 0,
                                         "fingerprint_failed": 0,
                                         "budget": 0, "carryover_on_disk": 24}))
    assert "24 en attente" in msg
    assert "aucun téléchargement" in msg


def test_nonzero_budget_says_nothing_about_stock():
    msg = build_message(_stats())
    assert "en attente, aucun téléchargement" not in msg


def test_missing_budget_key_says_nothing():
    """Un vieux fichier de stats sans budget ne doit rien inventer."""
    msg = build_message(_stats(download={"downloaded": 12, "prefiltered": 18,
                                         "failed": 0, "loudnorm_failed": 0,
                                         "fingerprint_failed": 0}))
    assert "Stock suffisant" not in msg


def test_cold_phase_reasons_are_the_real_ones():
    """La durée n'est pas un motif de la phase à froid : elle juge à chaud."""
    msg = build_message(_stats())
    ligne = next(l for l in msg.split("\n") if "écartés avant DL" in l)
    assert "durée" not in ligne
    for motif in ("antenne", "doublon", "cooldown", "jugé", "genre", "métadonnées"):
        assert motif in ligne


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


def test_unreadable_stats_files_never_break_the_recap(tmp_path, monkeypatch):
    """Dernier chemin restant vers une sortie non nulle : la lecture des stats."""
    monkeypatch.setattr(send_daily_recap, "DATA_DIR", tmp_path)
    # Tronqué en plein caractère multi-octets : UnicodeDecodeError, pas
    # JSONDecodeError.
    (tmp_path / "last_reconcile.json").write_bytes(b'{"az_files": 666, "x": "\xc3')
    # JSON valide mais non-objet : traverserait la lecture pour casser
    # ensuite sur `.get`.
    (tmp_path / "last_download_stats.json").write_text("null", encoding="utf-8")
    (tmp_path / "last_classify_stats.json").write_text("[1, 2, 3]", encoding="utf-8")

    msg = send_daily_recap.build_message(send_daily_recap.collect_stats())
    assert "AubeSonore" in msg


def test_ntfy_fallback_fires_when_callmebot_is_unconfigured(tmp_path, monkeypatch):
    """Sans CallMeBot, le récap doit passer par ntfy plutôt que disparaître."""
    monkeypatch.setattr(send_daily_recap, "DATA_DIR", tmp_path)

    class _Reglages:
        callmebot_apikey = ""
        whatsapp_phone = ""

    monkeypatch.setattr(send_daily_recap, "get_settings", lambda: _Reglages())
    monkeypatch.setattr(send_daily_recap, "send_whatsapp",
                        lambda *a, **k: pytest.fail("CallMeBot n'est pas configuré"))
    envoyes = []
    monkeypatch.setattr(send_daily_recap, "send_ntfy",
                        lambda text: envoyes.append(text) or True)

    assert send_daily_recap.main() == 0
    assert len(envoyes) == 1
    assert "AubeSonore" in envoyes[0]


def test_main_returns_zero_when_settings_are_unreadable(tmp_path, monkeypatch):
    """Un .env absent ou invalide ne doit pas faire échouer la nuit."""
    # DATA_DIR détourné : ni lecture ni écriture dans le data/ du dépôt.
    monkeypatch.setattr(send_daily_recap, "DATA_DIR", tmp_path)

    def _settings_illisibles():
        raise RuntimeError("ValidationError simulée : azuracast_url manquant")

    monkeypatch.setattr(send_daily_recap, "get_settings", _settings_illisibles)

    # Aucun chemin réseau ne doit rester atteignable, même si une régression
    # future laissait main() poursuivre après l'échec des réglages.
    def _interdit(*a, **k):
        pytest.fail("aucun envoi ne doit être tenté sans réglages lisibles")

    monkeypatch.setattr(send_daily_recap, "send_whatsapp", _interdit)
    monkeypatch.setattr(send_daily_recap, "send_ntfy", _interdit)

    assert send_daily_recap.main() == 0
