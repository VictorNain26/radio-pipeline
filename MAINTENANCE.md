# Maintenance — AubeSonore Radio Pipeline

Procédures d'entretien et scripts d'audit. Le pipeline tourne sans
intervention sur cycle journalier (timer systemd 03:00), mais certains
audits hebdo/mensuels gardent la bibliothèque saine.

> **AzuraCast server** — `116.203.46.203`. Seuls `audit_server.py` et
> `backfill_embeddings.py` requièrent un accès SSH / `docker exec` ;
> les autres scripts (dont `reanalyze_server.py` et `backfill_covers.py`)
> passent par l'API AzuraCast.

---

## Inventaire des scripts maintenance

| Script | But | Fréquence | Côté |
|--------|-----|-----------|------|
| `scripts/audit_separation.py` | Compare `config.SEPARATION` à la config AzuraCast live | Mensuel ou après modif `config.py` | Pipeline (read-only API) |
| `scripts/audit_integrity.py` | Télécharge depuis AzuraCast et vérifie chaque fichier audio (ffprobe + tailles) | Hebdo si suspicion, sinon mensuel | Pipeline (lent : download) |
| `scripts/audit_server.py` | Idem mais directement sur les fichiers du serveur AzuraCast | Mensuel | Serveur AzuraCast (SSH) |
| `scripts/redownload_corrupted.py` | Re-télécharge la liste `tracks-to-redownload.json` produite par `audit_server.py --fix` | Après audit_server | Pipeline |
| `scripts/reanalyze_server.py` | Ré-analyse les tracks où `mood IS NULL` dans `tracks.db` (download via API AzuraCast) | Au besoin | Pipeline (API) |
| `scripts/setup_playlists.py` | Crée / vérifie les 4 playlists zones dans AzuraCast | Setup initial ou après modif zones | Pipeline (API) |
| `scripts/backfill_covers.py` | **One-shot** — ajoute une cover iTunes Search aux fichiers de la library qui n'en ont pas (via API) | one-shot / au besoin | Pipeline (API) |
| `scripts/backfill_embeddings.py` | **One-shot** — calcule les embeddings CLAP de la library existante (fichiers récupérés via SSH + `docker exec`) | one-shot / au besoin | Pipeline (SSH) |
| `scripts/update-ytdlp.sh` | Met à jour `yt-dlp` (release ~toutes les 2 semaines) | Hebdo (timer séparé) | Pipeline |

Les scripts d'audit/maintenance toujours utiles :
- `audit_integrity` / `audit_server` détectent les fichiers corrompus
- `reanalyze_server` répare les tracks classify échouées
- `redownload_corrupted` répare les fichiers cassés
- `audit_separation` vérifie cohérence config Python ↔ AzuraCast

---

## Audits réguliers

### 1. Séparation AzuraCast (mensuel ou après modification de `config.SEPARATION`)

```bash
cd /home/victormoi/radio/pipeline
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

À exécuter **sur le serveur AzuraCast** (plus rapide, pas de download).
Les fichiers média vivent dans le conteneur Docker `azuracast`, sous
`/var/azuracast/stations/aubesonore/media` :

```bash
# Depuis la machine pipeline : déployer le script dans le conteneur puis exécuter
scp scripts/audit_server.py root@116.203.46.203:/tmp/
ssh root@116.203.46.203 "docker cp /tmp/audit_server.py azuracast:/tmp/ && \
    docker exec azuracast python3 /tmp/audit_server.py \
        /var/azuracast/stations/aubesonore/media \
        --fix --output /tmp/audit_report.json \
        --redownload-file /tmp/tracks-to-redownload.json"

# Récupérer la liste de re-download écrite par --fix
ssh root@116.203.46.203 \
    "docker cp azuracast:/tmp/tracks-to-redownload.json /tmp/"
scp root@116.203.46.203:/tmp/tracks-to-redownload.json .
```

Avec `--fix`, les fichiers corrompus sont supprimés et la liste des
tracks à re-télécharger est écrite dans le fichier passé à
`--redownload-file` (défaut : `tracks-to-redownload.json` — c'est **ce
fichier**, pas `audit_report.json`, qu'attend le script de redownload).
Puis, sur la machine pipeline :

```bash
python3 scripts/redownload_corrupted.py tracks-to-redownload.json
./run.sh   # le pipeline re-DL via le flux normal
```

### 3. Réanalyse des tracks sans mood (au besoin)

Si `data/tracks.db` contient des tracks où `mood IS NULL` (échec de
`analyze.py` à un run antérieur) :

```bash
python3 scripts/reanalyze_server.py --dry-run    # voir ce qui serait fait
python3 scripts/reanalyze_server.py              # appliquer
python3 scripts/reanalyze_server.py --limit 5    # tester sur 5 tracks
```

Tourne entièrement depuis la machine pipeline via l'**API AzuraCast**
(download des fichiers avec `http_client.download_file_to`) — aucun
accès SSH / `docker exec` requis. Aucun ré-upload, juste
re-classification + réassignation playlist.

### 4. CLAP smart sequencing (activé en production)

CLAP (Contrastive Language-Audio Pretraining) calcule un embedding
512-dim par track. FAISS sert ensuite à retrouver des "nearest
neighbours" (smart_queue.py), à construire des walks dans l'espace
sonore pour des transitions douces, et à faire de la recherche en
langage naturel (`smart_queue.py text "..."` via le text encoder CLAP).

`CLAP.enabled=True` dans `config.py` et le backfill de la library a
déjà été fait. Procédure de référence pour une (ré)installation :

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

## Source de vérité et réconciliation

AzuraCast fait autorité sur ce qui existe à l'antenne. `data/tracks.db` est
un cache : il conserve seulement ce qu'AzuraCast ignore (date d'upload
propre au pipeline, compteur de lectures, tier, mood, empreinte).

`scripts/library_state.reconcile()` aligne les deux au début de `download.py`
et de `classify.py`. Il désactive les lignes dont le fichier a disparu du
serveur (« fantômes ») en posant `deleted_at` et en annulant
`azuracast_file_id` — **sans les supprimer** : `play_count`, `mood`, `tier` et
l'empreinte survivent, et la ligne peut être réadoptée si le fichier
réapparaît. Il enregistre aussi les fichiers inconnus, et répare les clés
dont les métadonnées ont dérivé côté AzuraCast (sanitization — cf. incident
du 18/07). L'opération est idempotente : relancée aussitôt, elle ne corrige
plus rien.

Le dossier média (`AZURACAST_MEDIA_DIR`, facultatif) est lu pour comparer
son nombre de `.mp3` à celui de l'API. Une divergence remonte dans le récap.
Il n'est jamais utilisé pour supprimer quoi que ce soit. Non renseigné dans
`.env`, le contrôle est simplement sauté (`disk_files = None`) : le run
n'échoue pas.

Réconcilier à la main.

> ⚠️ **Cette commande écrit dans `data/tracks.db`.** `reconcile()` désactive
> *toute* ligne active dont le `file_id` est absent de la liste renvoyée par
> l'API. Si AzuraCast répond 200 avec une liste tronquée ou vide (instance
> dégradée, `AZURACAST_STATION_ID` erroné, station renommée), c'est la
> bibliothèque entière qui part. Or c'est précisément en situation
> d'incident, quand l'API n'est pas fiable, qu'on est tenté de lancer ce bloc.
>
> Les lignes restent réadoptables, mais la perte n'est pas entièrement
> réversible : le prochain `classify.py` purge le store CLAP contre les
> morceaux actifs, donc les embeddings des lignes désactivées en masse sont
> détruits et ne se recalculent qu'à 3-5 s/morceau, modèle et fichiers
> sources en main.
>
> D'où le garde-fou **intégré à `reconcile()`** depuis juillet 2026, et donc
> actif aussi bien ici que sur le chemin de nuit : en dessous de
> `RECONCILE_MIN_RATIO` (0,5) fois le nombre de lignes actives — ou, si la
> base compte déjà au moins `RECONCILE_MIN_FILES` (50) lignes actives, en
> dessous de `RECONCILE_MIN_FILES` fichiers — il lève `LibraryStateError`
> **avant toute écriture**. `download.py` en fait un `exit 1` ; `classify.py`
> saute la rotation et laisse les uploads se faire.
>
> Le plancher est conditionné à la taille de la base à dessein : une
> bibliothèque de moins de 50 morceaux qui voit bien tous ses fichiers doit
> se réconcilier normalement, sinon elle lèverait chaque nuit et ne pourrait
> jamais grandir. C'est le ratio qui la protège.
>
> Une suppression massive volontaire est donc refusée elle aussi : c'est
> voulu (rare, et rattrapable en desserrant `RECONCILE_MIN_RATIO` le temps
> d'un run). Un aléa réseau, lui, est fréquent et ce qu'il détruit ne se
> rattrape pas.
>
> Le `health_check()` et le `assert` ci-dessous restent utiles : ils
> échouent plus tôt et plus lisiblement. **Ne pas les retirer.**

```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from pathlib import Path
from settings import get_settings
from http_client import AzuraCastClient
from track_db import TrackDB
from library_state import reconcile
s = get_settings()
c = AzuraCastClient(base_url=s.azuracast_url, api_key=s.azuracast_api_key,
                    station_id=s.azuracast_station_id, timeout=s.http_timeout)
assert c.health_check(), 'AzuraCast injoignable — ne pas réconcilier'
files = c.get_station_files()
print(f'{len(files)} fichiers vus par l\'API')
assert len(files) > 500, 'liste AzuraCast suspecte — ne pas réconcilier'
db = TrackDB('data/tracks.db')
print(reconcile(files, db,
                media_dir=Path(s.azuracast_media_dir) if s.azuracast_media_dir else None))
db.close()
"
```

Si un `assert` casse, **c'est l'API qu'il faut réparer, pas le seuil.** Pour
seulement regarder sans rien écrire, exécuter les lignes jusqu'au `print` du
nombre de fichiers et s'arrêter là : rien n'est modifié avant l'appel à
`reconcile()`.

Compter les lignes actives, avant et après :

```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from track_db import TrackDB
db = TrackDB('data/tracks.db')
print('DB active :', len(db.get_active_tracks()))
db.close()
"
```

Après réconciliation, ce nombre doit être **égal** au `az_files` du rapport.
Vérifié le 26/07/2026 : 674 lignes actives pour 666 fichiers AzuraCast,
8 fantômes retirés, base ramenée à 666.

Comparer l'API et le disque à la main (le dossier média vit sur la machine
qui héberge AzuraCast). Vérifier d'abord que le glob désigne bien **une seule**
station — sinon `find` ne parcourt rien et renvoie `0`, ce qui se lit comme une
dérive catastrophique alors que le chemin est simplement faux (station
renommée, dossier déplacé) :

```bash
ls -d ~/radio/azuracast/stations/*/media
find ~/radio/azuracast/stations/*/media -name '*.mp3' | wc -l
```

---

## Registre des verdicts

Tout rejet portant sur un morceau **identifiable** est inscrit dans la table
`verdicts` de `data/tracks.db`, avec son motif. `download.py` la consulte en
phase à froid : un morceau déjà jugé n'est jamais retéléchargé.

Le registre n'est délibérément pas exhaustif — deux familles de rejets n'y
sont pas écrites, pour que le morceau puisse retenter sa chance :

- **morceau non identifiable** : sans artiste/titre exploitables il n'y a pas
  de clé, donc rien à mémoriser (`classify.record_rejection`, `analyze.py`).
  On ne condamne pas ce qu'on n'a pas su identifier ;
- **absence de tags** quand `GENRE_FILTER.require_tags` est actif
  (`download.py`) : l'absence est transitoire (morceau trop récent, sources
  muettes). L'inscrire bannirait à vie un morceau pour un silence du réseau,
  il n'est donc écarté que pour la nuit.

Conséquence pratique : **un morceau peut être rejeté nuit après nuit sans
jamais apparaître dans `verdicts`.** Une table qui ne mentionne pas un morceau
récurrent n'est pas cassée — c'est l'un de ces deux cas. Chercher plutôt le
motif dans les logs du run.

Les verdicts `rejected_taste` périment après `TASTE_FILTER.verdict_ttl_days`
(90 jours) — le profil de goût évolue, un morceau écarté sous l'ancien
profil doit pouvoir retenter sa chance. Les autres verdicts portent sur une
propriété stable de l'enregistrement et ne périment pas.

La table part vide : elle ne se remplit qu'à partir du premier run qui
enregistre un rejet. Tant qu'elle est vide, le filtrage amont n'écarte rien
— c'est normal, pas une panne.

Inspecter les verdicts (`sqlite3` n'est pas installé sur la machine
pipeline, d'où le passage par `python3`) :

```bash
python3 -c "
import sqlite3
con = sqlite3.connect('data/tracks.db')
print('total :', con.execute('SELECT COUNT(*) FROM verdicts').fetchone()[0])
for r in con.execute('SELECT track_key, verdict, reason, score FROM verdicts'
                     ' ORDER BY decided_at DESC LIMIT 20'):
    print(r)
con.close()
"
```

Effacer un verdict à la main pour forcer un nouvel essai :

```bash
python3 -c "
import sqlite3
con = sqlite3.connect('data/tracks.db')
n = con.execute('DELETE FROM verdicts WHERE track_key = ?', ('artiste - titre',)).rowcount
con.commit(); print(f'{n} verdict(s) effacé(s)')
con.close()
"
```

---

## Budget de téléchargement

`download.py` ne télécharge que `ROTATION.max_uploads_per_night ×
ROTATION.download_margin` moins le nombre de `.mp3` déjà présents dans
`downloads/`. Avec 24 fichiers
en carryover pour un quota de 6 et une marge de 2.0 (soit 12), le budget
vaut zéro et aucune nuit de téléchargement n'a lieu : le stock suffit.
C'est le comportement attendu, pas une panne.

```bash
python3 -c "
import sys; sys.path.insert(0,'scripts')
from pathlib import Path
from download import compute_budget
n = len(list(Path('downloads').glob('*.mp3')))
print(f'{n} fichiers en attente → budget {compute_budget(n)}')
"
```

Pour relancer les téléchargements, il faut vider le carryover — c'est-à-dire
laisser le pipeline consommer `downloads/` sur les nuits suivantes, pas
supprimer les fichiers à la main : ce sont des morceaux déjà validés.

---

## Tests

Suite pytest dans `tests/`. 120+ tests couvrent les fonctions pures
critiques : parsers RSS, filtre genre (avec mocks des 3 backends),
scoring multi-source, sectoring Russell circumplex, rotation tiers,
embeddings CLAP / smart queue, client HTTP (`test_http_client.py`),
TrackDB (`test_track_db.py`), validation de la config.

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
et la rotation `max_tracks=700` borne la taille du cache utile.

Pour forcer un refresh complet (ex. après modification des règles) :
```bash
rm data/genre_cache.json
```

Sera reconstruit au prochain run.
