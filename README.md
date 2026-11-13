# Jarvis

Tableau de bord des déploiements clients de l'équipe Integration : ce qui est éparpillé entre Notion, Airflow et l'orchestrator, au même endroit, jusqu'à la bascule.

- **Accueil** : retards, échéances, injections KO, prochains jalons, « Quoi de neuf » depuis ta dernière visite.
- **Une fiche par client** : synthèse et jalons, injections par ressource (avec lancement des DAGs), actions par équipe, retours de recette (avec brief pour un agent Claude), règles de reprise, CED.
- **Notifications** : agent terminé, changements urgents, échéances, avancement qui se dégrade.

Chacun configure **ses propres clients** : configuration et données restent sur sa machine.

## Démarrage

Prérequis : [uv](https://docs.astral.sh/uv/) et un accès au Notion Padoa.

```bash
git clone git@github.com:<organisation>/Jarvis.git && cd Jarvis
uv sync
cp .env.example .env          # facultatif
uv run uvicorn deploy_hub.main:app --port 8765
```

Ouvre http://localhost:8765, puis **Clients → Ajouter un client** : son nom et le lien de sa fiche dans la base Notion « Clients en déploiement » (••• → Copier le lien). Facultatif : son dashboard intégration, ses clés orchestrator (pour proposer les bons DAGs), ses stacks. La configuration va dans `config/clients/<slug>.yaml` (modèle : [`config.example/clients/exemple.yaml`](config.example/clients/exemple.yaml)).

## Synchroniser avec Notion

Synchro incrémentale (seulement ce qui a changé), au choix ou ensemble :
- **Routine Claude Code** avec le connecteur Notion, toutes les 2 h en semaine : [`docs/routine-synchro.md`](docs/routine-synchro.md).
- **Bouton « Rafraîchir depuis Notion »** : jeton d'une intégration Notion interne en lecture seule (`NOTION_TOKEN` dans `.env`), connectée aux bases Clients en déploiement, Actions de déploiement, Retours Recette, Catalogue RDD, Bible déploiement et Resources.

## Lancer des DAGs

L'onglet Injections propose les DAGs de l'orchestrator qui traitent chaque ressource, sur integration, snap ou recette (la prod est désactivée).
- Réglages Airflow dans `.env` (`AIRFLOW_BASE_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD`) ; sans eux, les lancements sont simulés.
- Après chaque lancement réel, [`air.py`](src/deploy_hub/air.py) (le script d'Arthur) suit le run : il relance les échecs passagers (3 fois au plus), envoie ses alertes en notifications et fait un rapport (**Agents → Suivis de DAG**). Il lit les logs des tâches qu'il suit ; ce qu'il écrit reste dans `data/airflow/`.
- Catalogue des DAGs : `sync/dags/catalogue.json`, à régénérer quand `dags/config_deployment.py` change ([`sync/dags/generer_catalogue.py`](sync/dags/generer_catalogue.py)).

## Flow Digger

Le bouton **Flow Digger ↗** de la barre ouvre [integration-flow-digger](https://github.com/padoa/integration-flow-digger) dans sa propre fenêtre, et le démarre d'abord sur la VM s'il ne tourne pas (`uv run main.py --start --no-browser`). Son installation (outils, 1Password, Google Drive, configs client) se fait une fois, selon son README.

## Lancer des agents Claude

Sur un retour de recette, « Lancer un agent » envoie le brief à Claude Code (`JARVIS_MODE=live`) :
- dans un **worktree** de l'orchestrator, sur la branche `fix-<client>-<retour>` : ton clone de travail n'est jamais modifié ;
- outils limités à `JARVIS_AGENT_ALLOWED_TOOLS` (lire, modifier, `uv run`, git sans reset, `gh pr create`), lecture hors worktree limitée à `JARVIS_AGENT_ADD_DIRS`, sans les jetons de Jarvis ;
- suivi dans **Agents** (journal, compte rendu, PR, bouton « Arrêter »).

Prérequis : Claude Code et `gh` connectés. Les worktrees restent après la PR : `git worktree remove <chemin>`.

## Sur une VM

Pour que les agents travaillent sans ton poste, avec en plus un shell de la VM dans la console : [`docs/vm.md`](docs/vm.md).

## Données et confidentialité

- `data/` et `config/` sont hors de git : rien de ce qui vient de Notion n'est versionné.
- La synchro de la recette n'accepte qu'une liste fixe de colonnes ; le contenu des retours est rangé à part (`data/notion/<client>/contenu-recette.json`).
- Le brief d'agent interdit toute donnée client dans le code, les tests ou la PR.

## Réglages (`.env`)

Tous facultatifs, voir [`.env.example`](.env.example) :

| Variable | Rôle |
|---|---|
| `JARVIS_CONFIG_DIR`, `JARVIS_DATA_DIR` | Emplacement de la configuration et des données |
| `NOTION_TOKEN` | Bouton « Rafraîchir depuis Notion » |
| `AIRFLOW_BASE_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD` | Lancement réel des DAGs |
| `ORCHESTRATOR_DIR`, `JARVIS_MODE`, `CLAUDE_BIN`, `JARVIS_AGENT_*` | Agents Claude |
| `FLOW_DIGGER_DIR`, `FLOW_DIGGER_PORT`, `FLOW_DIGGER_URL` | Bouton Flow Digger (dossier, port 4004, adresse si le transfert de port en change) |

**Secrets** (`NOTION_TOKEN`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD`) : de préférence une référence 1Password `op://<coffre>/<élément>/<champ>` (« Copy Secret Reference »), lue par `op read` au démarrage et gardée en mémoire seulement ; connecte-toi d'abord (app 1Password, ou `eval $(op signin)` dans le terminal qui lance Jarvis). `.env` est ramené à `600`, et Jarvis refuse de démarrer si ses données ou sa configuration sont dans un dépôt git sans y être ignorées.

## Développement

```bash
uv run pytest
uv run uvicorn deploy_hub.main:app --port 8765 --reload --reload-dir src
```

Routes dans `src/deploy_hub/main.py`, pages dans `templates/` et `static/`, synchro Notion dans `sync_notion.py`, sources (Notion, Airflow, agents, notifications) dans `sources/`.

## Limites connues

- L'historique des DAGs lancés n'est pas gardé au redémarrage (celui des agents l'est).
- Un changement fait seulement sur une carte Bible n'est vu qu'à la synchro complète suivante (7 jours au plus).
