# Source de vérité unique, filtrage amont et récap fiable

Date : 2026-07-26
État : en attente de relecture

## Problème

Trois défauts distincts, constatés sur l'état réel du 26/07.

### 1. La base SQLite dérive de la radio

| Source | Compte |
|---|---|
| Dossier `~/azuracast/stations/*/media` | 666 mp3 |
| API AzuraCast | 666 fichiers |
| `data/tracks.db`, lignes actives | 674 |

Huit lignes de la base pointent vers des `azuracast_file_id` qui n'existent
plus côté serveur (`Otto - (I am)` id=38, `Tholn - Lust Rites` id=559, …).
Rien ne les détecte : `enforce_tiered_rotation` itère sur les fichiers
AzuraCast et enregistre ceux qu'il ne connaît pas, mais ne fait jamais le
chemin inverse. Ces fantômes comptent dans le total qui pilote `max_tracks`,
maintiennent vivants des embeddings CLAP que le prune devrait retirer, et
faussent `get_stats()`.

Par ailleurs deux fetchs indépendants de la librairie coexistent :
`download.fetch_azuracast_library()` et `classify.enforce_tiered_rotation()`.
Aucun des deux ne fait autorité sur l'autre.

Un troisième stock existe sans réconciliation : `downloads/` contient 24 mp3
en carryover, invisible de la base.

### 2. Le filtre le plus sélectif agit après le téléchargement

Entonnoir du 26/07 : 354 candidats bruts → 30 retenus → 12 téléchargés →
6 uploadés, 24 mis en attente.

Avant le téléchargement, `download_track` ne vérifie que : clé artiste-titre
déjà en librairie, cooldown, genre Last.fm, durée. Le filtre de goût CLAP
(seuil 0.62), de loin le plus sélectif, n'intervient qu'après téléchargement,
normalisation loudnorm et analyse Essentia.

Pire, un morceau rejeté par ce filtre est simplement `unlink()` : aucune
trace. Redécouvert la nuit suivante, il est re-téléchargé intégralement,
puis retué par l'empreinte Chromaprint — qui est calculée après le
téléchargement. La boucle se répète indéfiniment.

### 3. Le récap WhatsApp annonce un fait faux

Le message du 26/07 affichait « 🚫 5 écartés (pas dans la couleur) ». Or
`build_message` additionne `rejected` et `quota` sous ce libellé, et cette
nuit-là `rejected` valait 0. Les cinq morceaux étaient en réalité **évincés
faute de place** : quota d'uploads atteint, pool de carryover plein, donc
supprimés et mis en cooldown. Ils étaient potentiellement excellents.

Trois défauts secondaires : le message ne dit rien de l'état de la radio, un
échec d'envoi est silencieux (`send_daily_recap.py` retourne toujours 0 et
run.sh n'a pas de repli), et `build_message` n'a aucun test.

## Conception

### Module `scripts/library_state.py`

Une seule fonction publique :

```python
def reconcile(client: AzuraCastClient, track_db: TrackDB) -> ReconcileReport
```

AzuraCast fait autorité sur ce qui existe. SQLite ne conserve que ce
qu'AzuraCast ignore : date d'upload propre au pipeline, compteur de lectures,
tier de rotation, mood, empreinte.

Étapes, dans l'ordre :

1. Un seul appel `client.get_all_files()` pour tout le run.
2. Fichier AzuraCast absent de la base → enregistré. La logique déménage
   depuis `classify.enforce_tiered_rotation`.
3. Ligne active dont le `file_id` a disparu des fichiers AzuraCast →
   `record_deletion()`. C'est le trou actuel.
4. Ligne dont le `file_id` existe mais dont l'artiste ou le titre a dérivé
   côté serveur → sa `track_key` est réécrite à partir des métadonnées
   AzuraCast, plutôt que de laisser créer une ligne concurrente. C'est le
   mode de panne de l'incident du 18/07, où la sanitization des noms de
   fichiers AzuraCast avait décalé les clés. Si la clé cible existe déjà,
   la ligne qui porte le `file_id` vivant gagne et l'autre est marquée
   supprimée : deux lignes ne peuvent pas revendiquer le même fichier.
5. Contrôle en lecture seule : nombre de `.mp3` sous le dossier média
   comparé au compte de l'API. Toute divergence est signalée, jamais
   corrigée — le disque ne décide de rien.

`ReconcileReport` porte : `az_files`, `db_active_before`, `ghosts_cleared`,
`untracked_registered`, `keys_repaired`, `disk_files`, `disk_drift`.

Le chemin du dossier média est configurable et facultatif. Absent ou
illisible, l'étape 5 est sautée et signalée comme non vérifiée : elle ne
peut jamais faire échouer un run.

`reconcile` est appelée au début de `download.py` et au début de
`classify.py`. `download.fetch_azuracast_library()` disparaît ; l'ensemble
des clés de dédup est dérivé du rapport. Un seul endroit sait ce qui existe.

### Filtrage amont dans `download.py`

`main()` passe d'une boucle unique à deux phases.

**Phase à froid** — aucun octet téléchargé. Pour chaque candidat :
déjà présent dans la librairie réconciliée, en cooldown, ou présent au
registre des verdicts → écarté. Les genres Last.fm sont pré-résolus en
parallèle, en s'appuyant sur `data/genre_cache.json` déjà en place ; les
genres bloqués sont écartés ici et non plus au milieu du téléchargement.
Les survivants sont **triés** par affinité de tags. Ce tri n'écarte rien.

**Phase chaude** — téléchargement dans l'ordre du tri, jusqu'au budget :

```
budget = max_uploads_per_night × marge_téléchargement − carryover_sur_disque
```

Avec 24 fichiers en attente pour un quota de 6, le budget est nul et aucun
téléchargement n'a lieu. Aujourd'hui, douze le seraient. Les candidats non
téléchargés ne sont pas condamnés : ils n'entrent simplement pas au registre
et reviendront par la découverte.

`marge_téléchargement` est un réglage de `config.ROTATION`, valeur initiale 2,
qui absorbe les rejets et les échecs yt-dlp.

### Registre des verdicts

Nouvelle table dans `track_db.py` :

```sql
CREATE TABLE IF NOT EXISTS verdicts (
    track_key   TEXT PRIMARY KEY,
    verdict     TEXT NOT NULL,
    reason      TEXT,
    score       REAL,
    decided_at  REAL NOT NULL
);
```

Écrite par `classify.py` à chaque rejet et par `download.py` pour les genres
bloqués et les durées hors bornes. Lue en phase à froid.

La péremption dépend de ce sur quoi porte le jugement :

| verdict | péremption | raison |
|---|---|---|
| `rejected_taste` | 90 jours | le profil de goût est reconstruit ; un morceau doit pouvoir retenter sa chance |
| `rejected_speech` | permanent | propriété stable de l'enregistrement |
| `rejected_multisignal` | permanent | idem |
| `blocked_genre` | permanent | idem |
| `filtered_duration` | permanent | idem |

Cette péremption est ce qui empêche un faux négatif de devenir définitif.

Un verdict ne remplace jamais une décision d'upload : `record_upload` sur une
clé présente au registre efface son verdict.

### Écarté volontairement

Le pré-filtre de goût par encodeur texte CLAP. L'alignement texte-audio de
CLAP est faible sur les noms propres d'artistes et de titres ; le taux de
faux négatifs serait invérifiable, et un morceau écarté sans avoir été
écouté ne laisse aucune trace exploitable.

### Récap `send_daily_recap.py`

Format cible :

```
🎵 AubeSonore — 26/07
────────────────
📻 Radio : 666 titres
   35 GOLD · 217 heavy · 224 medium · 198 light

➕ 6 ajoutés · 🗑 0 retirés
💎 24 en attente pour demain
🔇 5 évincés (quota plein, pas un rejet)
🚫 0 hors couleur

🔍 30 candidats → 12 téléchargés
   18 écartés avant DL (déjà vus, genre, durée)
```

`quota` et `rejected` deviennent deux lignes distinctes, chacune affichée
seulement si non nulle. L'état de la radio et la répartition des tiers
viennent du rapport de réconciliation et de `track_db.get_stats()`.

Un bloc d'alertes n'apparaît que si quelque chose cloche : fantômes corrigés,
divergence dossier/API, `loudnorm_failed`, `fingerprint_failed`, échecs de
téléchargement anormaux.

Fiabilité de l'envoi : un échec WhatsApp bascule sur ntfy plutôt que de
rester silencieux. Le message est écrit dans `data/last_recap.txt`. Le texte
part en paramètre d'une URL GET, il est donc tronqué à une longueur sûre en
coupant sur une frontière de ligne.

Le script continue de retourner 0 en toute circonstance : le récap ne doit
jamais faire échouer la nuit.

## Vérification

Nouveaux tests :

- `library_state` : fantôme retiré, fichier inconnu enregistré, clé réparée
  après dérive de métadonnées, dossier absent sans échec, divergence
  dossier/API signalée.
- Registre des verdicts : écriture, lecture, péremption à 90 jours pour
  `rejected_taste`, permanence des autres, effacement à l'upload.
- Budget de téléchargement : budget nul quand le carryover couvre le quota,
  budget partiel, budget plein quand `downloads/` est vide.
- `build_message` : un test par compteur, dont la distinction entre évincés
  et hors couleur ; bloc d'alertes absent quand tout va bien ; troncature.

Non-régression : les 167 tests existants doivent continuer de passer.

Vérification sur données réelles, avant et après : les huit fantômes
disparaissent, `db_active` rejoint 666, un second run consécutif ne
retélécharge aucun morceau déjà jugé.
