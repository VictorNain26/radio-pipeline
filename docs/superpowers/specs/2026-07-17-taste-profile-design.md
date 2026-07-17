# Profil de goût personnel — filtre + découverte (design)

**Date** : 2026-07-17
**Objectif** : que les morceaux téléchargés et programmés collent à la couleur musicale de Victor (référence : bibliothèque perso `/media/plex/Musique`, 661 artistes / ~14 000 fichiers FLAC). Douleur n°1 identifiée : des morceaux hors-goût entrent dans la radio.

## Décisions de cadrage (validées avec Victor)

- Référence de goût : la bibliothèque Plex personnelle.
- Double usage : **filtre** des candidats + **moteur de découverte** (seeds → artistes similaires).
- Sévérité du filtre : **équilibrée** (rejet du clairement éloigné, marge pour l'adjacent).
- Périmètre : goût + téléchargement. Le séquencement fin à l'antenne (CLAP walk) = chantier séparé ultérieur.
- Fiabilité : uniquement des briques éprouvées déjà présentes (LAION-CLAP, FAISS, API Last.fm). Seuil de rejet **calibré empiriquement**, filtre livré d'abord en `log_only` puis armé après audit concluant.

## Architecture

Trois composants greffés sur l'existant :

### 1. `scripts/build_taste_profile.py` (one-shot, relançable)

- Scanne `/media/plex/Musique` (structure Artiste/Album/titres).
- Échantillonne 2-3 morceaux par artiste (déterministe, répartis sur les albums) → ~1 700 fichiers.
- Calcule les embeddings CLAP (réutilise `audio_embeddings.py`) et écrit :
  - `data/taste_profile.npy` — matrice N×512 normalisée ;
  - `data/taste_profile_index.json` — méta (artiste, fichier, date) par ligne + liste des artistes seeds.
- Incrémental : les fichiers déjà embarqués ne sont pas recalculés ; relançable après ajout d'albums.

### 2. Filtre de goût dans `classify.py`

- Après l'analyse Essentia/CLAP, avant l'upload AzuraCast : score de goût = **similarité cosine moyenne avec les k=5 plus proches voisins** du profil.
- `score < seuil` → rejet (même traitement que les autres rejets : stats, rapport ntfy, cooldown).
- Config `TASTE_FILTER` dans `config.py` : `enabled`, `log_only`, `k`, `threshold`.
- Phase 1 : `log_only=True` — le verdict est loggé sans bloquer. Armement après audit.

### 3. `PersonalArtistsSource` dans `discovery_sources.py`

- Chaque nuit : ~15 artistes seeds par roulement (curseur persistant), Last.fm `artist.getSimilar` → artistes similaires absents de la radio → leurs top tracks entrent dans la découverte.
- Complète (ne remplace pas) les blogs RSS et tags Last.fm existants.

## Calibration & audit (avant armement)

1. **Positifs** : morceaux de la bibliothèque perso **tenus hors profil** (held-out) — doivent passer.
2. **Négatifs** : morceaux de genres bloqués / clairement hors-goût — doivent être rejetés.
3. Distribution des scores de la library AzuraCast actuelle pour situer le seuil « équilibré ».
4. Seuil choisi = meilleur compromis (haute acceptation des positifs, rejet net des négatifs). Si la séparation n'est pas nette, le filtre reste en `log_only` et on le signale.

## Erreurs & robustesse

- Profil absent/corrompu → filtre inactif avec warning (la pipeline ne casse jamais pour ça).
- Last.fm indisponible → la source perso est sautée, les autres sources tournent (pattern existant).
- SSD `/media/plex` non monté → `build_taste_profile.py` refuse de tourner ; la pipeline nocturne n'en dépend pas (le profil est copié dans `data/`).

## Résultat de calibration (2026-07-17)

Profil construit : **1 305 vecteurs / 654 artistes, 0 échec**. Audit sur
120 positifs held-out, 941 tracks de la library radio, 7 négatifs réels
(genres bloqués téléchargés pour l'occasion).

- Le centrage par la moyenne **dégrade** la séparation (98 % d'overlap) → cosine brut conservé, k=5.
- **Seuil retenu : 0.62** — rejette 0,8 % des positifs (un interlude parlé), attrape le power metal (0.605) et les 11 tracks les plus hors-couleur de la library actuelle.
- **Limite documentée** : le metal/hardcore proche du cluster punk de la bibliothèque (Jinjer, Soul Glo, Idles…) score 0.77-0.87 et n'est pas séparable par l'audio seul. Première ligne de défense pour ces cas : la blocklist de genres par tags (inchangée). Le filtre de goût est le second filet, pour les candidats sans tags.
- Filtre **armé** (`log_only=False`) après ces vérifications ; smoke test réel : track à 0.247 → reject, track médian (0.825) → ok.

## Tests

- Unitaires : échantillonnage déterministe, scoring k-NN (cas limites : profil vide, k > N), rotation du curseur de seeds, parsing getSimilar.
- Intégration : filtre en `log_only` sur données réelles, audit de calibration scripté.
