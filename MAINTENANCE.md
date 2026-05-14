# Maintenance — AubeSonore Radio Pipeline

Procédures d'entretien et scripts d'audit. Le pipeline tourne sans
intervention sur cycle journalier (cron 03:00), mais certains audits
hebdo/mensuels gardent la bibliothèque saine.

> **AzuraCast server** — `116.203.46.203` (utilisé par `audit_server.py`
> et `reanalyze_server.py` qui requièrent un accès SSH direct ou
> `docker exec`).

---

## Inventaire des scripts maintenance

| Script | But | Fréquence | Côté |
|--------|-----|-----------|------|
| `scripts/audit_separation.py` | Compare `config.SEPARATION` à la config AzuraCast live | Mensuel ou après modif `config.py` | Pipeline (read-only API) |
| `scripts/audit_integrity.py` | Télécharge depuis AzuraCast et vérifie chaque fichier audio (ffprobe + tailles) | Hebdo si suspicion, sinon mensuel | Pipeline (lent : download) |
| `scripts/audit_server.py` | Idem mais directement sur les fichiers du serveur AzuraCast | Mensuel | Serveur AzuraCast (SSH) |
| `scripts/redownload_corrupted.py` | Re-télécharge la liste produite par `audit_server.py --fix` | Après audit_server | Pipeline |
| `scripts/reanalyze_server.py` | Ré-analyse les tracks où `mood IS NULL` dans `tracks.db` | Au besoin | Pipeline |
| `scripts/reanalyze.py` | **Ré-analyse globale** (lourd, ~600 tracks × ~30s) | One-shot historique | Pipeline |
| `scripts/redownload_corrupted.py` | Re-DL des fichiers corrompus identifiés | Après audit | Pipeline |
| `scripts/update-ytdlp.sh` | Met à jour `yt-dlp` (release ~toutes les 2 semaines) | Hebdo (cron séparé) | Pipeline |

`reanalyze.py` (ré-analyse globale) est essentiellement un outil
**one-shot historique** issu de la migration vers les modèles MTG
Arousal-Valence. Il peut être archivé sauf si tu changes encore une fois
de modèle. À mettre dans `scripts/legacy/` si tu veux faire du ménage,
ou laisser tel quel — il n'est plus appelé par `run.sh`.

Les autres scripts sont **toujours utiles** :
- `audit_integrity` / `audit_server` détectent les fichiers corrompus
- `reanalyze_server` répare les tracks classify échouées
- `redownload_corrupted` répare les fichiers cassés

---

## Audits réguliers

### 1. Séparation AzuraCast (mensuel ou après modification de `config.SEPARATION`)

```bash
cd /home/victormoi/radio-pipeline
set -a && source .env && set +a
python3 scripts/audit_separation.py
```

Sortie attendue (exemple `2026-05-13`) :
```
duplicate_prevention_time_range : 180 min
✓ [OK] backend_config.duplicate_prevention_time_range
       AzuraCast = 180min covers SEPARATION (artist=60, title=180).
· [INFO] config.SEPARATION (advanced rules)
       mood_min_separation=3, genre_min_separation=2, ... NOT enforceable
       by AzuraCast natively.
```

AzuraCast (Liquidsoap backend) **n'expose qu'un seul champ** pour la
prévention de doublons : `backend_config.duplicate_prevention_time_range`
en minutes. Il s'applique à artiste + titre en bloc. Les règles
plus fines (`mood_min_separation`, `genre_min_separation`,
`tempo_max_variance`) **ne sont pas applicables nativement** — leur
implémentation demanderait un script Liquidsoap custom, ce qui sort du
scope actuel. Les valeurs dans `config.SEPARATION` sont donc
**documentaires** sauf pour `title_min_minutes`.

### 2. Intégrité des fichiers serveur (mensuel)

À exécuter **sur le serveur AzuraCast** (plus rapide, pas de download) :

```bash
# Depuis la machine pipeline, déployer puis exécuter
scp scripts/audit_server.py victormoi@116.203.46.203:/tmp/
ssh victormoi@116.203.46.203 \
    "python3 /tmp/audit_server.py /var/azuracast/stations/radio/media --fix --output /tmp/audit_report.json"
scp victormoi@116.203.46.203:/tmp/audit_report.json data/
```

Si `--fix` est passé, les fichiers corrompus sont supprimés et la liste
écrite dans un JSON. Récupérer ce JSON, puis sur la machine pipeline :

```bash
python3 scripts/redownload_corrupted.py data/audit_report.json
./run.sh   # le pipeline re-DL via le flux normal
```

### 3. Réanalyse des tracks sans mood (au besoin)

Si `data/tracks.db` contient des tracks où `mood IS NULL` (échec de
`analyze.py` à un run antérieur) :

```bash
python3 scripts/reanalyze_server.py --dry-run    # voir ce qui serait fait
python3 scripts/reanalyze_server.py              # appliquer
```

Aucune ré-upload, juste re-classification + réassignation playlist.

### 4. CLAP smart sequencing (optionnel — opt-in)

CLAP (Contrastive Language-Audio Pretraining) calcule un embedding
512-dim par track. FAISS sert ensuite à retrouver des "nearest
neighbours" (smart_queue.py) et à construire des walks dans l'espace
sonore pour des transitions douces.

**Activation (étape par étape)** :

```bash
# 1. Installer les dépendances lourdes (~1 Go disque, modèle 1.7 Go)
pip install --user --break-system-packages -r requirements-clap.txt

# 2. Pre-warm + télécharger le modèle CLAP (une seule fois)
python3 -c "from scripts.audio_embeddings import _load_model; _load_model()"

# 3. Backfill de la bibliothèque AzuraCast (~30 min pour 600 tracks)
python3 scripts/backfill_embeddings.py
#   --dry-run pour voir ce qui serait fait
#   --limit N pour tester sur N tracks
# Reprend automatiquement si interrompu (idempotent).

# 4. Activer dans config.py
#   CLAP = CLAPConfig(enabled=True)
# Le prochain run cron calculera les embeddings des nouveaux tracks
# en plus du reste (+3-5 s/track sur CPU).
```

**Utilisation** :

```bash
# Trouver les 5 tracks similaires à une seed
python3 scripts/smart_queue.py similar "beach house - space song" -k 5

# Greedy walk de longueur 10 dans l'espace embedding
python3 scripts/smart_queue.py walk "beach house - space song" -n 10

# Stats du store
python3 scripts/smart_queue.py info
```

Le store est stocké dans `data/embeddings.npy` + `data/embeddings_index.json`
(gitignored). Pour rebuild from scratch : supprimer ces deux fichiers,
relancer le backfill.

### 5. Mise à jour `yt-dlp` (automatisée via systemd)

Géré par `radio-pipeline-ytdlp.timer` (dimanche 02:00, une heure avant
le run quotidien). Le script vérifie la dernière release GitHub et
ne télécharge que si la version a changé.

Manuel :
```bash
./scripts/update-ytdlp.sh
systemctl --user start radio-pipeline-ytdlp.service   # idem, via systemd
```

Inspection :
```bash
systemctl --user list-timers radio-pipeline-ytdlp.timer
tail -20 ytdlp-update.log
```

---

## Tests

Suite pytest dans `tests/`. ~50 tests couvrent les fonctions pures
critiques : parsers RSS, filtre genre (avec mocks des 3 backends),
sectoring Russell circumplex, validation de la config.

```bash
python3 -m pytest tests/ -q
```

À lancer après toute modification de :
- `scripts/discovery_sources.py` (parsers)
- `scripts/genre_client.py` (logique blocklist/allowlist)
- `scripts/analyze.py::classify_mood` (sectoring atan2)
- `config.py` (validation cohérence)

Pas de tests d'intégration (qui demanderaient un AzuraCast / YouTube
de test) — ceux-là sont couverts par le run cron quotidien réel.

---

## Logs et rotation

Logrotate configuré via `scripts/logrotate.conf` (rotation hebdo,
8 semaines de rétention compressées). Installation :

```bash
sudo ./scripts/install_logrotate.sh
```

Important : `copytruncate` est obligatoire car `run.sh` utilise
`tee -a` qui garde le file descriptor ouvert.

---

## Scheduler (référence)

2 timers systemd user-scoped (pas de cron) installés par `setup_systemd.sh` :

```
radio-pipeline.timer        OnCalendar=*-*-* 03:00:00       Persistent=true
radio-pipeline-ytdlp.timer  OnCalendar=Sun *-*-* 02:00:00   Persistent=true
```

Lingering activé via `loginctl enable-linger $USER` pour que les
timers tournent sans session shell ouverte.

Templates dans `scripts/systemd/`, installation idempotente :

```bash
./scripts/setup_systemd.sh
```

---

## Cache `genre_cache.json`

`data/genre_cache.json` stocke les tags genre (MusicBrainz + Discogs +
Last.fm) avec TTL 30 j. Il grossit progressivement (~1 ko / track).
Pas de purge nécessaire — au-delà de 30 j les entrées sont ignorées,
et la rotation `max_tracks=600` borne la taille du cache utile.

Pour forcer un refresh complet (ex. après modification des règles) :
```bash
rm data/genre_cache.json
```

Sera reconstruit au prochain run.
