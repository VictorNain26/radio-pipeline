# CLAUDE.md — pipeline

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Ce que fait ce dépôt

Découverte musicale automatique : trouver des morceaux, les télécharger, les
analyser, les classer, les envoyer à AzuraCast et gérer leur rotation. **Ce
dépôt possède le catalogue** — c'est le seul à créer et supprimer des morceaux à
l'antenne.

Il n'a aucun lien avec l'application web ; les deux partagent une station, pas du
code. `README.md` détaille l'architecture par étapes, `MAINTENANCE.md` les
procédures d'audit.

## Commandes

```bash
python3 -m pytest tests/ -q                           # suite complète, quelques secondes
python3 -m pytest tests/test_<fichier>.py::test_nom -q # un seul test
python3 -m pytest -k "mot-clé" -q

set -a && source .env && set +a    # les scripts isolés en ont besoin ; run.sh le fait lui-même
python3 scripts/<étape>.py         # jouer une étape seule
```

Python système, sans venv — voir `scripts/setup.sh` pour la façon dont les
dépendances ont été installées.

## Ce qui compte vraiment

**AzuraCast fait autorité, la base SQLite est un cache.** Elle est réalignée sur
l'API à chaque run. Un écart ne se corrige jamais en éditant la base pour coller
au disque : le disque ne décide de rien. Un dossier média absent ou non monté doit
rendre un contrôle indisponible, jamais faire échouer un run.

**`config.py` est la source de vérité unique** pour les humeurs, les créneaux, les
sources, les seuils et tous les drapeaux de fonctionnalité. On règle là, jamais
dans les scripts d'étape. Un test garde sa cohérence interne.

**Un repli silencieux est pire qu'une panne.** Une normalisation qui échoue sans
bruit a déjà envoyé des dizaines de morceaux non normalisés à l'antenne : tout
allait « bien », les compteurs étaient verts. Toute étape qui sait se rabattre
doit donc compter ses échecs et les remonter. En ajouter une sans compteur, c'est
reconstruire la même panne.

**Les compteurs viennent des fichiers de stats, pas du système de fichiers.**
Compter les `.mp3` d'un dossier mélange les nouveautés et les reprises de runs
précédents. Chaque étape écrit ce qu'elle a fait ; c'est cette écriture qui fait foi.

**Le meilleur effort est un choix, pas une négligence.** Sources, enrichissements
et recherches échouent indépendamment sans interrompre le run — une source morte
ne doit jamais coûter la fournée. Les entrées optionnelles (profil de goût, disque
externe) se dégradent en avertissement, jamais en exception.

## Exécuter le pipeline

`run.sh` prend un verrou exclusif et **écrit sur la station en production**. Pour
un run manuel, passer par le service systemd : il partage le même verrou et la
même journalisation. Le verrou tient sur un descripteur ouvert et le fichier n'est
jamais supprimé — l'effacer laisserait un troisième processus verrouiller un
nouvel inode pendant que le premier tient encore l'ancien, soit deux pipelines
concurrents sur la même station.

Les units systemd sont des **gabarits** dans `scripts/systemd/` : les chemins
absolus sont substitués à l'installation. Éditer les fichiers installés est sans
effet durable — modifier le gabarit et réinstaller.

## Conventions

Documentation, commits et commentaires en français. Les messages de log et les
clés des fichiers de stats restent en anglais : ils sont lus par des scripts.

`README.md` et `MAINTENANCE.md` décrivent parfois un état antérieur (AzuraCast a
tourné sur une machine distante). Ne jamais faire confiance à un hôte, un port ou
un chemin absolu lu dans un `.md` — vérifier avec `docker ps` et le `.env` avant
d'agir dessus.
