# Jarvis

Le tableau de bord des déploiements clients de l'équipe Integration.
Ce qui est éparpillé entre Notion, Airflow et l'orchestrator, réuni au même endroit, jusqu'à la bascule.

Chacun installe son Jarvis et suit **ses propres clients** : configuration et données restent sur sa machine.

## Ce qu'on y trouve

| | |
|---|---|
| **Accueil** | Retards, échéances, injections KO, prochains jalons, ce qui a changé depuis ta dernière visite. |
| **Fiche client** | Synthèse, jalons, injections par ressource, actions par équipe, retours de recette, règles de reprise, CED, PR. |
| **BAU** | Les demandes de l'équipe, en liste ou en kanban, par squad. |
| **DAGs** | Lancer un DAG depuis la fiche, puis le suivre : relances automatiques et rapport. |
| **Agents Claude** | Sur un retour de recette ou une demande BAU, un agent prépare le correctif et ouvre la PR. |
| **Pour toi** | Brief du matin, épingles, notes, rappels, notifications sur le Mac. |

## Installer

Il te faut [uv](https://docs.astral.sh/uv/), un accès au Notion Padoa et, pour en profiter vraiment, [Claude Code](https://claude.com/claude-code) connecté à Notion.

```bash
git clone git@github.com:matthewjoy-padoa/Jarvis.git && cd Jarvis
uv sync
uv run uvicorn deploy_hub.main:app --port 8765
```

Ouvre http://localhost:8765. Aucun réglage n'est nécessaire pour démarrer : Jarvis crée tout seul son dossier `data/`.

## Premiers pas

1. **Ajoute un client** : **Clients → Ajouter un client**, puis choisis-le dans la liste. Tout se remplit (identifiant, clés orchestrator, liens Notion), tu relis et tu valides.
2. **Laisse-le se charger** : sa fiche s'ouvre et sa première synchro Notion part toute seule. Ensuite, **Rafraîchir depuis Notion** la relance quand tu veux.
3. **Branche le reste si tu en as besoin**, dans la page **Réglages** :
   - Airflow, pour lancer des DAGs ;
   - le clone de l'orchestrator (`ORCHESTRATOR_DIR`), pour les agents Claude et les PR ;
   - les notifications du Mac.

La page **État des systèmes** (lien en bas de page) dit ce qui marche et ce qui reste à régler.

## Mises à jour

Rien à faire à la main. Quand une nouvelle version sort, un bandeau l'annonce en haut de chaque page.
Le bouton **Mettre à jour et redémarrer** fait le reste : `git pull`, dépendances, redémarrage, puis la page se recharge.

Le bouton attend qu'aucun agent, aucune synchro ni aucune lecture de l'annuaire ne tourne. Ce qui change d'une version à l'autre : [CHANGELOG.md](CHANGELOG.md).

## Pour aller plus loin

| Sujet | Où lire |
|---|---|
| Tout ce que fait Jarvis, fonction par fonction | [docs/guide.md](docs/guide.md) |
| Tous les réglages, et les secrets avec 1Password | [docs/reglages.md](docs/reglages.md) |
| Synchro Notion : routine, tâche planifiée, journal | [docs/routine-synchro.md](docs/routine-synchro.md) |
| Faire tourner Jarvis sur une VM | [docs/vm.md](docs/vm.md) |

## Développer

```bash
uv run pytest
uv run uvicorn deploy_hub.main:app --port 8765 --reload --reload-dir src
```

- Routes dans `src/deploy_hub/main.py`, pages dans `templates/` et `static/`, sources (Notion, Airflow, agents, PR, perso, notifications) dans `sources/`.
- Synchro Notion dans `sync_notion.py` ; consigne de la routine dans `sync/routine.md`, lanceur dans `sources/sync_routine.py`.
- Chaque PR monte le numéro de version, sauf une correction : la règle est en tête du [CHANGELOG.md](CHANGELOG.md).
