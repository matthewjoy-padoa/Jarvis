# Jarvis

Tableau de bord des déploiements clients pour l'équipe Integration : il rassemble au même endroit ce qui est éparpillé entre Notion, Airflow et l'orchestrator, et aide à piloter la reprise des données jusqu'à la bascule.

- **Accueil** : retards, échéances du jour et de la semaine, injections KO, prochains jalons, et « Quoi de neuf » depuis ta dernière visite.
- **Une fiche par client** : synthèse et frise des jalons, injections regroupées par ressource (avec lancement des DAGs), actions filtrables par équipe, retours de recette (avec brief prêt pour un agent Claude), règles de reprise, vue CED.
- **Notifications** : agent terminé, changements urgents après une synchro, échéances du jour, synchro en retard, avancement qui se dégrade, jalon proche.

Les données viennent du dashboard d'intégration Notion de chaque client (bases « Clients en déploiement », « Actions de déploiement », « Retours Recette », « Catalogue RDD »). Chacun configure **ses propres clients** : la configuration et les données restent sur sa machine.

## Démarrage rapide

Prérequis : [uv](https://docs.astral.sh/uv/) (installe Python 3.12 au besoin) et un accès au Notion Padoa.

```bash
git clone git@github.com:<organisation>/Jarvis.git && cd Jarvis
uv sync
cp .env.example .env          # facultatif : tout fonctionne sans réglage
uv run uvicorn deploy_hub.main:app --port 8765
```

Ouvre http://localhost:8765, puis **Clients → Ajouter un client**.

## Configurer ses clients

Pour chaque client, il faut :
- **son nom** ;
- **le lien de sa fiche** dans la base Notion « Clients en déploiement » (sur la fiche : ••• → Copier le lien). C'est elle qui relie les actions, la recette et les règles du client.

Facultatif :
- le lien de son **dashboard intégration**, affiché en tête de fiche ;
- des valeurs du champ **« Client »** de la base Retours Recette à inclure en plus des retours liés à la fiche ;
- ses **clés client de l'orchestrator** (`dags/config_deployment.py`), pour proposer les bons DAGs ;
- ses **stacks** Padoa, en liens rapides.

La page « Ajouter un client » écrit `config/clients/<slug>.yaml` et prépare les requêtes Notion du client. On peut aussi écrire ce fichier à la main : modèle dans [`config.example/clients/exemple.yaml`](config.example/clients/exemple.yaml). « Configurer » sur la carte d'un client permet de le modifier ou de le retirer du suivi (rien n'est supprimé).

Un client configuré apparaît « en attente de première synchro » jusqu'au premier passage de la synchro.

## Synchroniser avec Notion

Deux façons, au choix ou ensemble :

| | Routine Claude Code | Bouton « Rafraîchir depuis Notion » |
|---|---|---|
| Comment | Tâche planifiée de Claude Code qui exécute les requêtes SQL avec le connecteur Notion | L'app lit l'API Notion elle-même |
| Prérequis | Claude Code avec le connecteur Notion | Un jeton d'intégration Notion (`NOTION_TOKEN` dans `.env`) |
| Rythme | Toutes les 2 h en semaine, ou à la demande | À chaque clic |
| Mise en place | [`docs/routine-synchro.md`](docs/routine-synchro.md) | Section ci-dessous |

La synchro est incrémentale : un client inchangé est sauté, seules les lignes modifiées sont relues, et une synchro complète a lieu tous les 7 jours ou quand des lignes sont ajoutées ou supprimées.

### Jeton Notion (bouton « Rafraîchir »)
1. Sur https://www.notion.so/profile/integrations, créer une intégration **interne**, capacité « Lire le contenu » seulement (la création peut demander un admin du workspace).
2. La connecter (••• → Connexions) aux bases Clients en déploiement, Actions de déploiement, Retours Recette, Catalogue RDD, Bible déploiement et Resources.
3. Mettre le jeton dans `.env` (`NOTION_TOKEN=…`), puis relancer l'app.

## Lancer des DAGs

L'onglet Injections propose, pour chaque carte, les DAGs de l'orchestrator qui traitent ses ressources, et la stack cible (`padoa_env` : integration, snap ou recette ; la prod est désactivée par défaut).

- **Airflow** : renseigner `AIRFLOW_BASE_URL`, `AIRFLOW_USER` et `AIRFLOW_PASSWORD` dans `.env` (API REST v1). Sans eux, les lancements sont simulés et listés dans « Agents ».
- **Catalogue des DAGs** : `sync/dags/catalogue.json`, généré depuis un clone de l'orchestrator. À régénérer quand `dags/config_deployment.py` change (commande en tête de [`sync/dags/generer_catalogue.py`](sync/dags/generer_catalogue.py)).
- Pour réactiver la prod : ajouter `"prod"` à `PADOA_ENVS` dans `src/deploy_hub/sources/airflow.py` (il faut alors taper PROD pour confirmer chaque lancement).

## Lancer des agents Claude

Sur un retour de recette, « Lancer un agent » envoie le brief (relu et modifiable) à Claude Code en mode headless. Avec `JARVIS_MODE=live` :

1. Jarvis crée un **worktree** de l'orchestrator depuis `origin/main`, sur la branche `fix-<client>-<retour>` (sous `<ORCHESTRATOR_DIR>/.claude/worktrees/jarvis/`). Ton clone de travail n'est jamais modifié. Relancé sur la même branche, l'agent reprend le même worktree.
2. Il lance `claude -p` dans ce worktree, brief sur l'entrée standard, sortie dans `data/agents/<id>.jsonl`.
3. La page **Agents** suit chaque agent : état, journal mis à jour toutes les 5 s, compte rendu, lien de la PR détecté, bouton « Arrêter ». Une notification arrive à la fin.

Ce que l'agent peut faire :
- il charge comme une session normale ton `~/.claude/CLAUDE.md`, tes hooks et le `CLAUDE.md` du dépôt (pas d'option `--bare`) ;
- il ne lit hors de son worktree que les dossiers de `JARVIS_AGENT_ADD_DIRS` ;
- ses outils sont limités à `JARVIS_AGENT_ALLOWED_TOOLS` (par défaut : lire, modifier, `uv run`, git sans reset ni checkout, `gh pr create`). Tout autre appel est refusé, sans question, puisqu'il n'y a personne pour répondre. Ces règles filtrent le texte des commandes : ce n'est pas une barrière de sécurité, d'où l'intérêt d'un hook qui bloque le push sur main ;
- les jetons de Jarvis (`NOTION_TOKEN`, `AIRFLOW_*`) ne lui sont pas transmis.

Prérequis : Claude Code installé et connecté (`claude` une fois en interactif), `gh` connecté pour ouvrir la PR. Les worktrees ne sont pas supprimés : `git worktree remove <chemin>` quand la PR est mergée.

## Données et confidentialité

- **Rien de ce qui vient de Notion n'est versionné** : `data/` (fiches, caches, journal des changements, notifications) et `config/` sont exclus de git.
- Retours de recette : l'app lit les métadonnées et le contenu utile à l'analyse (commentaire client, exemple, analyse, solutions, échanges). Le contenu est rangé à part dans `data/notion/<client>/contenu-recette.json`. La synchro refuse toute autre colonne.
- Le brief d'agent interdit toute donnée client (matricule, nom, exemple) dans le code, les tests ou la PR.

## Réglages (`.env`)

Tous facultatifs, voir [`.env.example`](.env.example) :

| Variable | Rôle |
|---|---|
| `JARVIS_CONFIG_DIR`, `JARVIS_DATA_DIR` | Emplacement de la configuration et des données (par défaut `./config` et `./data`) |
| `NOTION_TOKEN` | Active « Rafraîchir depuis Notion » |
| `AIRFLOW_BASE_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD` | Lancement réel des DAGs |
| `ORCHESTRATOR_DIR`, `JARVIS_MODE`, `CLAUDE_BIN`, `JARVIS_AGENT_*` | Lancement réel des agents Claude (section ci-dessus) |

## Développement

```bash
uv run pytest            # tests (aucun appel réseau)
uv run uvicorn deploy_hub.main:app --port 8765 --reload --reload-dir src
```

| Dossier | Contenu |
|---|---|
| `src/deploy_hub/main.py` | Routes de l'app |
| `src/deploy_hub/templates/`, `static/` | Pages (Jinja + htmx), styles et scripts |
| `src/deploy_hub/sync_notion.py` | Synchro : fusion incrémentale, fiche, journal, notifications |
| `src/deploy_hub/queries.py`, `sync/requetes/` | Modèles de requêtes Notion communs à tous les clients |
| `src/deploy_hub/client_config.py` | Configuration des clients |
| `src/deploy_hub/sources/` | Notion (API), Airflow, catalogue des DAGs, agents Claude, notifications, veilles |
| `demo/`, `tests/` | Fiches de démonstration et tests |

## Limites connues

- L'historique des DAGs lancés n'est pas conservé au redémarrage (celui des agents l'est).
- En synchro incrémentale, un changement fait uniquement sur une carte Bible n'est vu qu'à la synchro complète suivante (7 jours au plus).
- Les notifications du navigateur ne s'affichent que tant qu'un onglet de l'app est ouvert.
