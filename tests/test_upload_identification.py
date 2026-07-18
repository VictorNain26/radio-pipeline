"""
Tests for the 2026-07-18 upload-identification incident, where an
auto-updated AzuraCast started (a) returning upload responses without a
usable file id and (b) sanitizing stored filenames (lowercase,
underscores), so the exact-basename fallback failed 12/12.

Two mechanisms are covered:
  - the post-upload identification fallback matches on a normalized
    filename skeleton (_normalize_filename) — the path is available
    instantly, before the server has parsed metadata;
  - the duplicate check matches on identity (artist - title) via
    normalize_track_key — the signal the server preserves verbatim, with
    no collision across distinct non-Latin titles.
"""

from classify import _normalize_filename
from track_db import normalize_track_key


# ---------------------------------------------------------------------------
# _normalize_filename — post-upload fallback against sanitized paths
# ---------------------------------------------------------------------------

def test_matches_sanitized_names_observed_in_production():
    # Real pairs from the 2026-07-18 night run (local file → stored path).
    pairs = [
        ("St. Vincent - Marry Me.mp3", "st._vincent_-_marry_me.mp3"),
        ("Alvvays - Archie, Marry Me.mp3", "alvvays_-_archie,_marry_me.mp3"),
        ("My Bloody Valentine - Soon (The Andrew Weatherall Remix).mp3",
         "my_bloody_valentine_-_soon_(the_andrew_weatherall_remix).mp3"),
        ("The Sundays - Here's Where The Story Ends.mp3",
         "the_sundays_-_heres_where_the_story_ends.mp3"),
    ]
    for local, stored in pairs:
        assert _normalize_filename(local) == _normalize_filename(stored)

def test_combining_marks_and_composed_forms_match():
    # AzuraCast stores accents as combining marks; the local name may be in
    # composed (NFC) form. NFKD folding reconciles both to the same skeleton.
    import unicodedata
    assert (_normalize_filename(unicodedata.normalize("NFC", "Gideön - Angelas Dream.mp3"))
            == _normalize_filename(unicodedata.normalize("NFD", "gideön_-_angelas_dream.mp3")))
    assert (_normalize_filename("Sébastien Tellier - Naïf de Coeur.mp3")
            == _normalize_filename("sébastien_tellier_-_naïf_de_coeur.mp3"))

def test_non_latin_scripts_are_preserved_by_server_and_match():
    # The server keeps non-Latin scripts verbatim in the path (час, μ, CJK),
    # so normalizing both sides drops the same chars and still matches.
    pairs = [
        ("polyμ - Pulse.mp3", "polyμ_-_pulse.mp3"),
        ("Dirtbag Loris - час.mp3", "dirtbag_loris_-_час.mp3"),
        ("illasoul - 天地玄黃 the genesis.mp3", "illasoul_-_天地玄黃_the_genesis.mp3"),
    ]
    for local, stored in pairs:
        assert _normalize_filename(local) == _normalize_filename(stored)

def test_distinct_latin_tracks_stay_distinct():
    names = [
        "The Olivia Tremor Control - Garden of Light.mp3",
        "The Olivia Tremor Control - The Same Place.mp3",
        "Alvvays - Archie, Marry Me.mp3",
        "St. Vincent - Marry Me.mp3",
    ]
    skeletons = [_normalize_filename(n) for n in names]
    assert len(set(skeletons)) == len(names)

def test_empty_and_extension_only():
    assert _normalize_filename("") == ""
    assert _normalize_filename(".mp3") == "mp3"


# ---------------------------------------------------------------------------
# Duplicate check uses metadata, avoiding the filename-skeleton collision
# ---------------------------------------------------------------------------

def test_metadata_key_avoids_non_latin_title_collision():
    # Filename skeletons collide (the non-Latin title is stripped, leaving
    # only the shared artist), which is why the duplicate check must key on
    # metadata instead — normalize_track_key keeps the full titles distinct.
    a = "maykretch - 骨董品.mp3"
    b = "maykretch - 天地玄黃.mp3"
    assert _normalize_filename(a) == _normalize_filename(b)      # would collide
    assert normalize_track_key("maykretch", "骨董品") != \
           normalize_track_key("maykretch", "天地玄黃")            # but identity does not
