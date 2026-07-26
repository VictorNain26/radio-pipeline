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


def test_quota_and_taste_rejections_coexist_without_merging():
    """Les deux motifs à la fois : une re-fusion afficherait « 8 » et une seule ligne."""
    msg = build_message(_stats(classify={"uploaded": 6, "rejected": 3, "quota": 5,
                                         "carryover": 24, "rotation_deleted": 0}))
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
    msg = build_message(_stats(classify={"uploaded": 6, "rejected": 3, "quota": 0,
                                         "carryover": 0, "rotation_deleted": 0}))
    assert "3 hors couleur" in msg
    assert "évincés" not in msg


def test_library_state_is_reported():
    msg = build_message(_stats())
    assert "666 titres" in msg
    assert "35 GOLD" in msg


def test_tier_line_is_not_orphaned_when_az_files_missing():
    """Sans « 📻 Radio », la répartition ne doit pas pendre sous la barre."""
    msg = build_message(_stats(reconcile={}))
    assert "35 GOLD" in msg
    tier_line = next(ligne for ligne in msg.split("\n") if "35 GOLD" in ligne)
    # Pas de parent émis : la ligne se tient seule, sans indentation orpheline.
    assert not tier_line.startswith(" ")
    assert "titres" not in msg


def test_tier_line_is_indented_under_its_parent():
    msg = build_message(_stats())
    lignes = msg.split("\n")
    parent = next(i for i, ligne in enumerate(lignes) if "666 titres" in ligne)
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
