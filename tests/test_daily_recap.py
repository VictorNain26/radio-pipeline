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
