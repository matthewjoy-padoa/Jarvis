# Jarvis (ex-Deploy Hub)

*Just A Rather Very Intelligent System* : le compagnon de l'équipe Integration pour suivre et piloter les déploiements clients. Dossier : `~/Documents/Jarvis` (sorti du vault le 2026-11-06). Le paquet Python garde son nom technique `deploy_hub`, la routine le sien, `deploy-hub-sync`.

Tableau de bord d'un déploiement client : injections par ressource, tâches, recettes, deadlines, et boutons d'action (DAG Airflow, Claude sur une demande).

**État :** fiches réelles synchronisées depuis Notion (Medetram) ; actions DAG et agents encore simulées.

## Synchro directe : bouton « 🔄 Rafraîchir depuis Notion » (sans Claude)
L'app lit Notion elle-même par l'API REST (`src/deploy_hub/sources/notion_api.py`). Le bouton est sur chaque fiche, et « Tout rafraîchir » sur la page Clients. En ligne de commande : `uv run python -m deploy_hub.sources.notion_api [slug]`.
- Chaque clic fait une synchro complète du client : client, actions, recette, règles. La fiche n'est réécrite que si toutes les lectures ont réussi.
- Même périmètre que les requêtes SQL : colonnes fixées dans `notion_api.py`, filtre par `page_id` (et `recette_clients`) du yaml du client.
- Recette : l'API ne renvoie que les colonnes listées (`filter_properties`), contenu compris ; le corps des pages n'est jamais lu, l'adresse est reconstruite depuis l'id. Le contenu va dans `contenu-recette.json`, exclu de git.

### Mise en place
1. Sur https://www.notion.so/profile/integrations, créer une intégration **interne**, capacité **Lire le contenu** seulement (pas d'insertion, de modification ni d'informations utilisateur). La création peut demander un admin du workspace Padoa.
2. La connecter (menu ••• → Connexions) aux 4 bases : Clients en déploiement, Actions de déploiement, Retours Recette, Catalogue RDD.
3. Copier le jeton dans `~/Documents/Jarvis/.env` (ignoré par git) : `NOTION_TOKEN=…`, puis redémarrer l'app.

### À savoir
- Le jeton donne accès à tout le contenu des 4 bases : ce sont l'app et ses tests qui limitent les colonnes lues.

## Synchro Notion (routine `deploy-hub-sync`, toutes les 2 h de 8h à 18h en semaine), incrémentale
1. Empreintes (`empreintes` du yaml) : nombre de lignes et dernière modification des bases Actions et Recette du client.
2. `uv run python -m deploy_hub.sync_notion verifier <slug>` donne le mode :
   - `inchangé` → client sauté ;
   - `delta` → requêtes `deltas` avec `?` = date de la dernière modification connue : seules les lignes modifiées sont relues ;
   - `complet` → toutes les lignes (premier passage, carte ou retour ajouté ou supprimé, ou dernière synchro complète il y a 7 jours).
3. Les requêtes `client` et `regles` (1 et ~30 lignes) sont relues à chaque passage non sauté.
4. `uv run python -m deploy_hub.sync_notion <slug>` fusionne dans `data/notion/<slug>/cache-*.json`, écrit `data/deploiements/<slug>.md`, `data/recette-pages.txt` et `etat-synchro.json`. Échec → message dans le canal Slack de la routine, fiche inchangée.

Pagination : pages de 100 lignes (`<nom>-2.json`…), jusqu'à une page de moins de 100 lignes ; le script refuse une dernière page pleine.

Ajouter un client : copier `sync/clients/medetram.yaml` et les requêtes `*-medetram.sql` en remplaçant l'id de la page « Clients en déploiement » du client, puis ajouter l'id de la vue Recette de son dashboard dans `sync/recette-bloquee.json`.

## Retours de recette : contenu gardé en local
Décision du 2026-11-05 : Notion est considéré comme sûr chez Padoa, donc l'app et Claude lisent le contenu utile des retours. Le hook `notion-recette-guard.sh` a été retiré des réglages (sauvegarde : `_setup/claude-settings.json.avant-recette-20261105`).
- Colonnes lues (`sync/requetes/recette-*.sql`, `RECETTE_PROPERTIES` dans `notion_api.py`) : ligne du cahier, type de retour, commentaire client, exemple salarié ou adhérent, analyse padoa, solutions possibles, discussion INTE/OPS, échanges et suivi, plus les métadonnées. Toute autre colonne est refusée par `sync_notion`.
- Métadonnées → `cache-recette.json` et la fiche, versionnés. Contenu → `data/notion/<client>/contenu-recette.json`, **exclu de git** (`.gitignore` du vault), comme les résultats bruts `recette*.json`. Il reste sur la machine et ne part pas sur GitHub.
- Onglet Recette : « Voir le retour » sur chaque carte qui a du contenu. Le bouton agent transmet ce contenu ; la consigne de l'utilisateur devient facultative.
- Le brief de l'agent interdit toute donnée client (matricule, nom, exemple) dans le code, les tests ou la PR.

## Lancer
```bash
uv sync
uv run uvicorn deploy_hub.main:app --port 8765
uv run pytest
```

## Pages
Calquées sur le dashboard intégration Notion (« Dashboard intégration - <client> »).

Barre du haut : **Clients**, puis un lien direct par client, **Deadlines**, **Agents** et le thème. Le lien « État des sources » est en pied de page.

| Page | URL | Bloc Notion |
|---|---|---|
| Clients | `/` | Clients en déploiement (statut 🟢🟠🔴, avancement vs attendu) ; liens vers les vues tous clients |
| Injections, Actions, Recette (tous clients) | `/injections`, `/actions`, `/recette` (`?tous=true` pour les clos) | Statut intégration, Actions du moment, Retours Recette |
| Deadlines | `/deadlines` | Jalons, actions et retours datés |
| Agents | `/agents` | Agents lancés depuis l'app |
| Sources | `/sources` | État des connexions |
| Fiche client | `/deploiements/<client>/synthese`, `…/injections`, `…/actions`, `…/recette`, `…/regles`, `…/accompagnement` | Un onglet par bloc. En tête : échéance, avancement, liens Notion et stacks |

Page d'accueil « Aujourd'hui » (`/`, tous clients) : retards, échéances du jour et des 7 jours, injections KO (avec un lien vers le bloc de la ressource à relancer), jalons des 3 semaines, et « Quoi de neuf ».
- « Quoi de neuf » : à chaque synchro, `sync_notion.publish` compare l'ancien et le nouveau cache et note dans `data/notion/<client>/journal.json` (exclu de git, 60 jours) les retours et cartes nouveaux, supprimés ou modifiés : statut, deadline, statut d'injection, point de vigilance, et pour la recette commentaire client, exemple, analyse, solutions, discussion, échanges (avant / après). Période : depuis la dernière visite (mémorisée dans le navigateur par « Tout marquer comme vu »), 24 h ou 7 jours. Le journal démarre à la première synchro après sa mise en place ; on ne voit que l'état avant et après chaque passage.
- La vue d'ensemble des clients est passée à `/clients`.

Notifications (cloche 🔔 de la barre du haut, page `/notifications`) :
- 🤖 agent terminé (`ClaudeRunner.finished`, à appeler aussi par le suivi des agents réels) ;
- 🔄 après une synchro (`sync_notion.sync_notes`) : urgentes = nouveau retour ouvert, retour passé en KO, injection passée en KO, point de vigilance levé, deadline amenée à moins de 2 jours ; les autres changements en un résumé ;
- 📅 échéance du jour : jalon, action non terminée ou retour ouvert dont la date est aujourd'hui, une seule fois par jour.
- veilles relevées avec la cloche (`sources/watchers.py`) : synchro en retard (en semaine de 10h à 19h, dernière synchro d'un client vieille de plus de 4 h) ; avancement qui se dégrade (pastille 🟢 → 🟠/🔴 ou 🟠 → 🔴, état mémorisé dans `data/etat-veilles.json`, exclu de git) ; jalon important (recettes, freeze, déploiements) à J-7 (info) et J-1 (urgent), avec les injections et retours encore ouverts ; échec du bouton « Rafraîchir depuis Notion ».
- Stockées dans `data/notifications.json` (exclu de git, 500 dernières, une clé = une seule notification). Le compteur est rouge s'il y a de l'urgent. « Activer les notifications du Mac » dans le panneau : notifications du navigateur tant qu'un onglet de l'app est ouvert (relevé toutes les 60 s).

Injections par ressource (vue par défaut de l'onglet Injections) : un bloc par ensemble de ressources liées, les blocs avec un KO d'abord (ouverts, bord rouge), puis en cours, à faire, terminés ; un formulaire DAG par bloc ; recherche, filtre par état, tout déplier. « Liste » garde le tableau filtrable, « CED (fichiers) » la vue CED.

Onglet Injections : seulement les cartes d'injection, c'est-à-dire celles qui ont « INTE Injection » dans Actions à mener (champ manuel ou carte Bible liée ; colonne `injection` de `actions-*.sql`). Les comptes, l'avancement et les barres des pages Synthèse et Clients portent sur ces cartes.

Lancer un DAG depuis une carte d'injection (onglet Injections) :
- Ressources de la carte : « Ressources liées » de la carte, sinon de sa carte Bible (catalogue Notion « Resources », nom orchestrator).
- DAGs proposés : ceux des clés `orchestrator` du yaml du client qui traitent ces ressources, d'après `sync/dags/catalogue.json` (ETL de la ressource d'abord, puis groupe). Régénérer le catalogue quand `dags/config_deployment.py` change : `sync/dags/generer_catalogue.py <dépôt orchestrator>` (commande en tête du fichier).
- Stack cible : paramètre `padoa_env` du DAG : integration, snap ou recette. La prod est désactivée (2026-11-05) ; pour la réactiver, l'ajouter à `PADOA_ENVS` dans `sources/airflow.py` (il faudra alors taper PROD pour confirmer).
- Airflow appelé : `AIRFLOW_BASE_URL` avec `AIRFLOW_USER` / `AIRFLOW_PASSWORD` dans `.env` ; sans eux, lancement simulé. Lancements listés dans `/agents`.
- Airflow de la VM : écoute sur le port 8888 de la VM, qui n'est pas ouvert sur Internet. Sur le Mac, il est joignable sur `localhost:8081` via le transfert de port SSH de la session VS Code Remote-SSH (sinon : `ssh -N -L 8081:localhost:8888 vm-inte-matthew`) : `AIRFLOW_BASE_URL=http://localhost:8081`.

Graphiques cliquables : un segment, une ligne de légende ou une barre applique le filtre correspondant au tableau ou à la liste de la page (re-clic : retire ; « Effacer les filtres » remet tout). Depuis la Synthèse, ils ouvrent l'onglet déjà filtré (`#groupe=todo` dans l'adresse), et les retours terminés passent en vue « Tous ».

Ergonomie : onglets collants ; formulaire d'agent replié sous « 🤖 Lancer un agent » ; colonnes vides masquées (DAG et dernier run sans ressource, Responsable sans valeur).

## Bouton « Lancer un agent » (retours de recette)
- Visible sur chaque retour ouvert (statut hors groupe terminé, voir `RETOUR_STATUSES` dans `models.py`).
- Le brief est construit depuis le retour (`retour_prompt` dans `sources/claude_runner.py`) : ligne du cahier, type de données, commentaire client, exemple, analyse, solutions, puis le contrat (Entrée, Sortie, Validation, Limites, Résultat). Il est visible avant le clic.
- Branche `fix-<client>-<id du retour>`, PR `[FIX](<client>) …`. En mode démo, le lancement est simulé et listé dans `/agents`.

## Structure
| Fichier | Rôle |
|---|---|
| `src/deploy_hub/main.py` | Routes : pages globales, onglets d'une fiche, actions |
| `src/deploy_hub/templates/` | Une page par partie ; les blocs `_*.html` (`_injections`, `_actions`, `_recette`, `_regles`, `_accompagnement`, `_synthese`). Les tableaux `_*.html` sont partagés entre la page globale (tous les clients) et l'onglet d'une fiche |
| `src/deploy_hub/models.py` | Déploiement, ressource, action, retour de recette, règle, remontée ; statuts Notion et calcul 🟢🟠🔴 |
| `src/deploy_hub/sources/vault.py` | Lit les fiches `*.md` (frontmatter YAML), une par client |
| `src/deploy_hub/sources/airflow.py` | Déclenchement des DAG et état du dernier run. Jamais les logs |
| `src/deploy_hub/sources/claude_runner.py` | `claude -p` dans un worktree dédié de l'orchestrator, brief des retours, liste des lancements |
| `src/deploy_hub/sources/external.py` | Notion et Slack, non connectés |
| `src/deploy_hub/sync_notion.py` | Résultats Notion bruts → fiche, liste des pages bloquées |
| `sync/` | Clients suivis, requêtes SQL approuvées, ids bloqués |
| `data/` | Fiches réelles et résultats bruts (métadonnées, aucun contenu de recette) |
| `demo/deploiements/` | Fiches fictives (tests) |

## Reste à décider ou à brancher
- Groupes de statuts (à faire / en cours / KO / terminé) : mon classement des options Notion, à valider (`models.py`).
- Quels retours ouvrent le bouton agent : tous les retours non terminés pour l'instant.
- Lecture Notion réelle (bases Clients en déploiement, Actions de déploiement, Retours Recette, Catalogue RDD, Remontées Opsbot) avec `NOTION_TOKEN` ; Slack via les remontées Opsbot déjà dans Notion.
- Correspondance « Type de données » Notion → ressource orchestrator (champ `resource` saisi à la main pour l'instant).
- Mode live : appels REST Airflow, lancement réel de Claude (vérifier l'option `--worktree`).
- Emplacement des fiches dans le vault (`DEPLOY_HUB_DEPLOYMENTS_DIR`), puis déploiement sur la VM.
