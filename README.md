# AubeSonore Radio Pipeline

Pipeline de découverte musicale automatique pour la webradio
[AubeSonore](https://radio.aubesonore.fr).

```
Discovery (multi-source) ──► yt-dlp (YouTube) ──► Essentia-TensorFlow ──► AzuraCast
       │                                            │
       │                                            │
       └─ MusicBrainz + Discogs + Last.fm (genre filter, multi-source)
```

## Architecture (mai 2026)

### 1. Discovery — multi-source

Le pipeline agrège plusieurs sources de découverte, déduplique sur
`(artist, title)` normalisé, et plafonne à `DISCOVER_MAX_TRACKS`
(120 par défaut). Chaque source est best-effort : si l'une tombe, les
autres continuent.

| Source                | Type     | Notes                                                              |
|-----------------------|----------|--------------------------------------------------------------------|
| HypeMachine `/popular`| JSON API | Source historique, gardée parmi d'autres                           |
| Gorilla vs. Bear      | RSS      | Indie / électro / ambient, parser em-dash                          |
| A Closer Listen       | RSS      | Ambient / experimental / modern classical, parser tilde            |
| Stereogum             | RSS      | Filtré sur `/music/`, parser dash + smart quotes                   |
| Pitchfork Track Reviews| RSS     | Artiste extrait de l'URL slug (parser dédié)                       |
| Last.fm `tag.gettoptracks` | API | 9 tags : indie, electronic, ambient, hip-hop, dream pop, downtempo, trip hop, indietronica, shoegaze |
| `data/manual_picks.json`| JSON   | Injection manuelle (existant)                                      |
| `data/custom_feeds.json`| JSON   | URLs RSS arbitraires (ex. flux générés par [rss.app](https://rss.app/)) |

Sources et seuils sont configurés dans `config.py` (`RSS_FEEDS`,
`LASTFM_TAGS`, `DISCOVER_MAX_TRACKS`).

### 2. Filtre genre — multi-source (MusicBrainz + Discogs + Last.fm)

Avant le téléchargement, chaque track passe par `genre_client.py` qui
interroge 3 sources et applique une politique hybride :

1. **blocklist hard** sur l'union des tags (rejet immédiat)
2. **allowlist soft** sur l'union des tags (accept, passe le filtre)
3. **aucun tag** → accept, le filtre audio Essentia (`AGGRESSIVE_FILTER`)
   reprend la main en aval

Les 3 sources sont complémentaires :

- **MusicBrainz** — taxonomie canonique genre/style, source primaire
- **Discogs** — couverture supérieure pour électronique + hip-hop (avec
  Personal Access Token : 60 req/min, sinon 25)
- **Last.fm** — crowd-sourced, sauve les artistes obscurs

Un cache disque (`data/genre_cache.json`, TTL 30 j) persiste les lookups
entre runs. Blocklist et allowlist sont configurées dans `config.py`
(`GENRE_FILTER.blocked_genres` et `ALLOWED_GENRES`).

### 3. Téléchargement — yt-dlp robuste

`scripts/download.py` :

- Probe via `ytsearch5` + scoring rapidfuzz pour choisir le bon candidat
- Téléchargement MP3 qualité maximale (`--audio-quality 0`)
- Sleep intervals (3–8 s) + 5 retries pour éviter le throttling YouTube
  (3 workers parallèles depuis une IP unique)
- `--extractor-args youtube:player_client=web_safari,web` : escape-hatch
  recommandée par yt-dlp en 2026 quand YouTube ship un player change
- Validation post-download via `ffprobe` + retry automatique si fichier
  corrompu
- Cover art HypeMachine embed + ID3 tags

### 3.5 Quality gates v4 (avant analyse)

Trois gates additionnels tournent post-yt-dlp pour économiser l'analyse
Essentia et garder la library propre :

- **AcoustID dedup** (`scripts/audio_fingerprint.py`) — Chromaprint
  fingerprint exact match contre `data/tracks.db`. Catches les
  re-uploads d'un même enregistrement sous métadonnées différentes
  (remasters, "feat." rewrites). Pas d'appel réseau.
- **Speech filter** (Essentia `voice_instrumental` head) — rejette les
  tracks > 60 % voice probability (interviews / podcasts qui sneak via
  les feeds RSS).
- **EBU R128 loudnorm** (ffmpeg, -16 LUFS / -1.5 dBTP) — normalisation
  broadcast pour cohérence sonore.

Tous sont feature-flaggés dans `config.py` (`ACOUSTID_DEDUP`,
`SPEECH_FILTER`, `LOUDNORM`), defaults `enabled=True`.

### 4. Analyse audio — Essentia-TensorFlow + MTG (v3)

`scripts/analyze.py` utilise un ensemble de modèles MTG :

- **Arousal-Valence ensemble** : DEAM + emoMusic + MuSe (MusiCNN,
  ~88 % accuracy)
- **mood_aggressive** : `mood_aggressive-discogs-effnet-1.pb` (~98 %)
- **Genre Discogs400** : `genre_discogs400-discogs-effnet-1.pb`
  (AUC 0.954)

Le mood final est dérivé de la position 2D (valence, arousal) via
`atan2`, projeté sur le cercle de Russell (8 catégories à 45°
d'intervalle).

### 5. Classification finale — filtre multi-signal

`scripts/classify.py` applique un filtre à 4 signaux indépendants pour
rejeter les tracks agressives qui auraient passé le filtre genre :

1. `mood_aggressive` (discogs-effnet)
2. Arousal-Valence (MusiCNN ensemble)
3. Genre ML (genre_discogs400)
4. Tags genre (multi-source : MusicBrainz + Discogs + Last.fm)

Rejet si **2 signaux ou plus concordants** OU `mood_aggressive > 0.85`.

### 6. 8 moods × 8 dayparts (modèle de Russell)

Les tracks sont routées vers des playlists AzuraCast selon le mood et le
créneau horaire :

| Mood        | Valence | Arousal | BPM       | Couleur |
|-------------|---------|---------|-----------|---------|
| Energetic   | +0.8    | +0.5    | 110–135   | #FFD700 |
| Excited     | +0.9    | +0.9    | 125–150   | #FF6B35 |
| Intense     |  0.0    | +1.0    | 120–160   | #DC143C |
| Angry       | −0.7    | +0.8    | 100–180   | #8B0000 |
| Melancholic | −0.5    |  0.0    |  70–110   | #4A0E4E |
| Sad         | −0.8    | −0.6    |  50–90    | #2F4F4F |
| Calm        |  0.0    | −1.0    |  50–85    | #87CEEB |
| Relaxed     | +0.6    | −0.5    |  70–105   | #98FB98 |

Dayparts (configurables dans `config.py`) :

| Daypart          | Horaire     | Moods cibles                                  |
|------------------|-------------|-----------------------------------------------|
| Early_Morning    | 05:00–07:00 | Calm, Relaxed                                 |
| Morning_Commute  | 07:00–09:00 | Energetic, Excited                            |
| Morning_Work     | 09:00–12:00 | Energetic, Relaxed                            |
| Lunch            | 12:00–14:00 | Relaxed, Energetic, Excited                   |
| Afternoon        | 14:00–17:00 | Energetic, Relaxed, Melancholic               |
| Evening_Commute  | 17:00–19:00 | Energetic, Relaxed                            |
| Evening          | 19:00–22:00 | Relaxed, Melancholic, Sad, Calm, Angry        |
| Night            | 22:00–05:00 | Calm, Sad, Melancholic, Intense, Angry        |

### 6.5 Smart sequencing — CLAP + FAISS (opt-in)

Quand `config.CLAP.enabled=True`, `analyze.py` calcule en plus un
embedding 512-dim L2-normalisé via le modèle LAION-CLAP HTSAT. Les
embeddings vivent dans `data/embeddings.npy` + `data/embeddings_index.json`.

`scripts/smart_queue.py` construit un index FAISS en mémoire (cosine
similarité = inner product sur vecteurs normalisés) et permet :

- **Nearest neighbour** : top-k tracks les plus similaires à une seed
- **Greedy walk** : séquence de longueur N partant d'une seed, chaque
  pas sélectionne le voisin le plus proche pas encore utilisé,
  optionnellement restreint à un set de candidats (utile par daypart)

C'est ce qui rendra émergentes les règles `tempo_max_variance`,
`mood_min_separation` et `genre_min_separation` documentées dans
`config.SEPARATION` mais non enforceables par AzuraCast natif :
deux tracks proches dans l'espace embedding ont *par construction*
des BPM, moods et timbres proches.

Voir `MAINTENANCE.md` section "CLAP smart sequencing" pour
l'activation (installation des deps, backfill de la library, etc.).
Activation impacte `+3-5 s/track` de temps CPU dans `analyze.py`.

### 7. Rotation 3-tiers + cooldown

`classify.enforce_tiered_rotation` sépare les tracks en 4 tiers basés
sur l'âge :

- **FRESH** (`<= fresh_days`, 10 j) — protection totale
- **CURRENT** (`<= current_days`, 30 j) — supprimable si library pleine
  ET `play_count >= min_plays_before_delete`
- **FADING** (`<= max_age_days`, 50 j) — plafonné à 20 % de la library,
  least-played évacuées en premier
- **EXPIRED** (`> max_age_days`) — force delete

Le `play_count` vient de l'historique AzuraCast (sync à chaque run).
Cooldown de 60 j après suppression : un track supprimé ne sera pas
re-téléchargé pendant cette fenêtre (`data/tracks.db`).

## Installation

```bash
git clone git@github.com:VictorNain26/radio-pipeline.git
cd radio-pipeline
./scripts/setup.sh
cp .env.example .env && nano .env       # remplir AzuraCast / Last.fm / Discogs
./scripts/download_models.sh            # ~600 Mo de modèles Essentia
./scripts/setup_playlists.sh            # crée les 8 playlists dans AzuraCast
sudo ./scripts/install_logrotate.sh     # rotation des logs (recommandé)
./scripts/setup_systemd.sh              # timers user-scoped : pipeline 03:00 + yt-dlp dimanche 02:00
```

## Variables d'environnement

Voir `.env.example` pour la liste complète. Clés notables :

- `AZURACAST_URL`, `AZURACAST_API_KEY`, `AZURACAST_STATION_ID` — requis
- `LASTFM_API_KEY` — fortement recommandé (alimente discovery + filtre genre)
- `DISCOGS_TOKEN` — optionnel, augmente le rate limit Discogs à 60 req/min
- `NTFY_TOPIC` — notifications push sur succès/échec
- `DEBUG`, `SSL_VERIFY` — flags de debug/sécurité

## Ajouter une source RSS sans toucher au code

Coller un objet dans `data/custom_feeds.json` (voir
`data/custom_feeds.json.example`) :

```json
[
  {
    "url": "https://rss.app/feeds/XXXXXXXX.xml",
    "parser": "dash",
    "label": "mon-flux",
    "limit": 25,
    "enabled": true
  }
]
```

Parsers disponibles : `dash`, `tilde`, `dash_quoted`, `pitchfork`.

## Structure

```
radio-pipeline/
├── run.sh                   # Orchestration (flock + trap + ntfy + stats)
├── config.py                # Moods, dayparts, sources, filtres
├── .env.example             # Documentation des variables
├── requirements.txt         # Dépendances Python
├── data/
│   ├── tracks.db            # SQLite (cooldown + play_count)
│   ├── manual_picks.json    # Injection manuelle
│   ├── custom_feeds.json    # RSS arbitraires (rss.app etc.)
│   ├── genre_cache.json     # Cache 30 j multi-source genre
│   ├── pipeline_stats.json  # 30 derniers runs + breakdown
│   ├── last_discover_stats.json
│   └── last_download_stats.json
├── scripts/
│   ├── discover.py          # Orchestrateur multi-source
│   ├── discovery_sources.py # HypeMachine + RSS + Last.fm tags
│   ├── discover_manual.py   # Manual picks (legacy, branché par run.sh)
│   ├── download.py          # yt-dlp + ffprobe + ID3 + checksum
│   ├── analyze.py           # Essentia-TF (arousal-valence + genre)
│   ├── classify.py          # Filtre multi-signal + rotation 3-tiers
│   ├── lastfm_client.py     # Backend Last.fm
│   ├── genre_client.py      # Agrégateur 3 sources + cache disque
│   ├── http_client.py       # Retry + circuit breaker + SHA-256
│   ├── settings.py          # Pydantic v2 settings
│   ├── track_db.py          # SQLite TrackDB
│   ├── audit_integrity.py   # Vérif local files (SHA + ffprobe)
│   ├── audit_server.py      # Vérif library AzuraCast
│   ├── reanalyze*.py        # Réanalyse forcée (maintenance)
│   ├── redownload_corrupted.py
│   ├── setup_playlists.py   # Crée les 8 playlists AzuraCast
│   ├── logrotate.conf       # Config logrotate
│   └── install_logrotate.sh # Installeur (sudo)
└── models/                  # Modèles Essentia pré-entraînés
```

## Maintenance et tests

Voir [`MAINTENANCE.md`](MAINTENANCE.md) — couvre :
- Audits réguliers (`audit_separation.py`, `audit_integrity.py`,
  `audit_server.py`)
- Procédure de redownload des fichiers corrompus
- Cron yt-dlp séparé
- Cache `genre_cache.json`
- Inventaire et fréquence recommandée de chaque script

Tests pytest dans `tests/` (~50 tests, ~1.5 s) :

```bash
python3 -m pytest tests/ -q
```

Cibles : parsers RSS, filtre genre multi-source, classification Russell
circumplex, cohérence de la configuration.

## Logs et observabilité

- `pipeline.log` : pipeline events (1 ligne par étape)
- `cron.log` : sortie complète stdout/stderr du cron
- `data/pipeline_stats.json` : 30 derniers runs avec breakdown discover
  + download (par source, par status)
- ntfy push (si `NTFY_TOPIC` configuré) sur succès/échec

Rotation hebdo via logrotate (8 semaines de rétention compressées).

## Scheduler (systemd user-scoped)

Le pipeline est piloté par 2 timers systemd dans `~/.config/systemd/user/` :

| Unit | Quand | Quoi |
|------|-------|------|
| `radio-pipeline.timer` | quotidien 03:00 | exécute `run.sh` (full pipeline) |
| `radio-pipeline-ytdlp.timer` | dimanche 02:00 | exécute `scripts/update-ytdlp.sh` (refresh yt-dlp avant le run hebdo) |

Les deux units sont versionnés sous `scripts/systemd/` avec des placeholders
`@HOME@` / `@PIPELINE_DIR@`, et installés via :

```bash
./scripts/setup_systemd.sh
```

`Persistent=true` garantit le catch-up après un reboot. `loginctl
enable-linger` est activé automatiquement par le script (1 prompt sudo)
pour que les timers tournent sans session shell ouverte.

Inspection :

```bash
systemctl --user list-timers 'radio-pipeline*'
journalctl --user -u radio-pipeline.service --since "1 day ago"
```

### Lancer un run à la main

```bash
systemctl --user start radio-pipeline.service    # full pipeline
systemctl --user start radio-pipeline-ytdlp.service   # juste yt-dlp update
```
