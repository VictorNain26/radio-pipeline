# Refonte programmation/rotation — curation, débit, tier GOLD (design)

**Date** : 2026-07-17
**Validé avec Victor** : volume 6 uploads/nuit, tier GOLD activé.

## Problème (chiffres mesurés)

- ~26-30 uploads/nuit (~200/sem.) pour ~2 700 passages antenne/sem. :
  chaque nouveauté passe ~4 fois puis disparaît — l'exposition minimale
  d'une découverte en radio (15-25 passages/sem.) est mathématiquement
  impossible à ce débit.
- La rotation supprime TOUT à 60 jours, y compris les morceaux les plus
  joués — l'inverse de la pratique radio (hits prouvés → catalogue).
- `expected_plays_per_day=0.65` hardcodé pour 597 tracks : dérive dès que
  la taille de library change.

## Décisions

1. **Curation par le goût, 6 uploads/nuit** (`ROTATION.max_uploads_per_night`).
   classify trie les fichiers analysés par score de goût décroissant et
   n'uploade que les 6 meilleurs qui passent les filtres. Les non-retenus
   partent en cooldown 60 j (pas de re-téléchargement le lendemain).
2. **Découverte 60 → 30 candidats** (`DISCOVER_MAX_TRACKS`) : assez pour
   choisir (ratio sélection ~2-3:1), moitié moins d'API/bande passante.
3. **Taux de passages mesuré** : `expected_plays_per_day` calculé à chaque
   rotation depuis les données réelles (Σ play_count / Σ age_days des
   tracks actives, clamp [0.2, 3.0], fallback config si < 30 jours-tracks
   de signal). Utilisé par le re-tiering HEAVY/MEDIUM/LIGHT.
4. **Tier GOLD (catalogue)** : à l'expiration (60 j), un morceau **prouvé**
   (rate ≥ expected × heavy_ratio) **et dans la couleur** (taste ≥ 0.70)
   passe GOLD au lieu d'être supprimé — rotation douce (1 daypart, comme
   LIGHT), plafonné à 40 % de la library (`gold_max_pct`). GOLD est
   immunisé contre l'expiration et le re-tiering ; le surplus au-delà du
   plafond suit la suppression normale.

## Équilibre attendu

42 ajouts/sem. ; library d'équilibre ≈ 6×60 j ≈ 360 découvertes actives
+ catalogue GOLD (≤ 40 %) → ~400-500 tracks, chaque nouveauté ~15-20
passages/sem. en HEAVY pendant sa grace period.

## Non-objectifs

Séquencement fin des enchaînements (chantier CLAP séparé). Pondérations
de playlists AzuraCast (géré côté serveur).
