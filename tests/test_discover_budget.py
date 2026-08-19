"""
Répartition du budget de découverte entre sources.

Ajouté le 2026-08-19 après avoir mesuré que le cap était saturé par la
première source ajoutée : les 30 candidats de tracks-to-download.json
étaient hypem à 100 %. Les blogs RSS, PersonalArtists et les picks manuels
de Victor étaient calculés chaque nuit puis jetés, et SOURCE_PRIORITY —
appliqué plus loin dans download.py — opérait sur une liste déjà homogène.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from discover import _dedupe_and_cap  # type: ignore[import-not-found]


def _lot(source: str, n: int, prefixe: str | None = None):
    p = prefixe or source
    return [{"id": f"{p}{i}", "artist": f"{p}-A{i}", "title": f"T{i}",
             "cover": "", "search": "", "source": source} for i in range(n)]


def test_une_source_riche_ne_monopolise_plus_le_cap():
    """50 hypem contre 5 RSS : hypem ne doit pas rafler les 30 places."""
    out = _dedupe_and_cap(_lot("hypem", 50) + _lot("gorillavsbear", 5), 30)
    parts = collections.Counter(t["source"] for t in out)
    assert parts["gorillavsbear"] == 5, "les 5 candidats RSS doivent tous passer"
    assert parts["hypem"] == 25


def test_les_picks_manuels_passent_en_entier():
    """Un choix explicite de Victor ne peut pas être évincé par le volume."""
    out = _dedupe_and_cap(_lot("hypem", 100) + _lot("manual", 4), 10)
    assert collections.Counter(t["source"] for t in out)["manual"] == 4


def test_toutes_les_sources_sont_representees():
    """Le tourniquet sert chaque source, aucune n'est laissée à zéro."""
    tracks = (_lot("hypem", 40) + _lot("personal", 40)
              + _lot("gorillavsbear", 40) + _lot("lastfm:indie", 40))
    parts = collections.Counter(t["source"] for t in _dedupe_and_cap(tracks, 20))
    assert len(parts) == 4
    assert min(parts.values()) >= 4


def test_la_source_prioritaire_gagne_le_doublon():
    """Un même morceau proposé par deux sources est crédité à la mieux classée."""
    doublon = {"id": "x", "artist": "Même", "title": "Morceau",
               "cover": "", "search": "", "source": "hypem"}
    prioritaire = dict(doublon, id="y", source="manual")
    out = _dedupe_and_cap([doublon, prioritaire], 10)
    assert len(out) == 1
    assert out[0]["source"] == "manual"


def test_le_cap_reste_respecte():
    assert len(_dedupe_and_cap(_lot("hypem", 500), 30)) == 30


def test_moins_de_candidats_que_le_cap_ne_boucle_pas():
    """Garde-fou : le tourniquet doit s'arrêter quand les sources sont vides."""
    assert len(_dedupe_and_cap(_lot("hypem", 3), 30)) == 3
