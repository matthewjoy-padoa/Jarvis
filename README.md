# Jarvis

Tableau de bord des déploiements clients de l'équipe Integration : ce qui est éparpillé entre Notion, Airflow et l'orchestrator, au même endroit, jusqu'à la bascule.

- **Accueil** : retards, échéances, injections KO, prochains jalons, « Quoi de neuf » depuis ta dernière visite.
- **Une fiche par client** : synthèse et jalons, injections par ressource (avec lancement des DAGs), actions par équipe, retours de recette (avec brief pour un agent Claude), règles de reprise, CED.
- **Notifications** : agent terminé, changements urgents, échéances, avancement qui se dégrade, rappels arrivés à échéance. Une fois « Activer les notifications du Mac » cliqué (**Réglages → Notifications du Mac**), elles arrivent sur le Mac même onglet fermé (Web Push, [`sources/web_push.py`](src/deploy_hub/sources/web_push.py)) : le navigateur doit rester lancé ; le contenu part chiffré de bout en bout vers le service de push du navigateur ; clé et abonnements dans `data/push/`.
- **Pour moi seulement** : brief du matin à copier, tickets épinglés, notes, rappels (voir [Perso](#perso)).

Chacun configure **ses propres clients** : configuration et données restent sur sa machine.

## Démarrage

Prérequis : [uv](https://docs.astral.sh/uv/) et un accès au Notion Padoa.

```bash
git clone git@github.com:matthewjoy-padoa/Jarvis.git && cd Jarvis
uv sync
cp .env.example .env          # facultatif : tout se règle aussi dans la page Réglages
uv run uvicorn deploy_hub.main:app --port 8765
```

Ouvre http://localhost:8765, puis **Clients → Ajouter un client**, et choisis le client dans la liste : son identifiant (lettres et chiffres, comme dans l'orchestrator : « APST 26-07 » → `apst2607`), ses clés orchestrator, sa fiche « Clients en déploiement », son dashboard intégration et ses autres dashboards (déploiement, formation, gestion financière) se remplissent ; tu relis, puis tu ajoutes. Cette liste est l'annuaire des clients, que Jarvis fait relire en fond, à son démarrage puis chaque jour, par Claude Code (connecteur Notion, lecture seule, une vingtaine de secondes) : elle est prête quand tu ouvres la page. Il la lit depuis la page « Liste des dashboards pour le déploiement des nouveaux clients » et la base « Clients en déploiement » ; « Relire maintenant » le relit à la demande. Sans Claude Code, « Ou remplir à la main » : le lien de sa fiche (••• → Copier le lien), et au besoin son dashboard intégration. Facultatif : ses stacks. Autre liste de dashboards : `JARVIS_NOTION_DASHBOARDS` (id de la page). La configuration va dans `config/clients/<slug>.yaml` (modèle : [`config.example/clients/exemple.yaml`](config.example/clients/exemple.yaml)).

Le client apparaît alors comme « pas encore synchronisé » : lance une première synchro (section suivante) pour charger ses données.

## Synchroniser avec Notion

Synchro incrémentale : une requête d'empreinte pour tous les clients, puis seulement ce qui a changé. **Clients synchronisés : ceux configurés dans ton Jarvis** (`config/clients/`, ou `JARVIS_CONFIG_DIR`) ; un client ajouté est pris au passage suivant, un client retiré ne l'est plus.

Deux façons de lire Notion, au choix :

| | Sans jeton : routine Claude Code (recommandé) | Avec jeton : API Notion |
|---|---|---|
| Comment | Claude exécute les requêtes avec son connecteur Notion, en suivant [`sync/routine.md`](sync/routine.md) | Jarvis appelle l'API avec une intégration Notion interne en lecture seule |
| Prérequis | Connecteur Notion activé dans Claude, avec accès aux bases Notion du déploiement | `NOTION_TOKEN` dans `.env`, intégration connectée aux bases Clients en déploiement, Actions de déploiement, Retours Recette, Catalogue RDD, Bible déploiement et Resources |
| Bouton « Rafraîchir depuis Notion » | Lance la routine en arrière-plan (`claude -p`) ; il faut aussi Claude Code en ligne de commande (`claude` dans le PATH, ou `CLAUDE_BIN`) | Appelle l'API directement |

### Mettre en place la routine

1. **Connecteur Notion** : dans Claude (claude.ai → Réglages → Connecteurs), active Notion et donne-lui accès aux bases du déploiement.
2. **Première synchro, à la main** : ouvre le dossier de Jarvis dans Claude Code (app ou terminal) et tape `/jarvis-sync`. Le compte rendu donne une ligne par client ; les fiches apparaissent dans Jarvis.
3. **Synchro automatique** (conseillé) : dans Claude Code, **Scheduled tasks → nouvelle tâche** :
   - nom : `jarvis-sync` ; horaire : toutes les 2 h de 8 h à 18 h en semaine (`0 8-18/2 * * 1-5`) ;
   - consigne (remplace le dossier, et le canal ou « aucun ») :
     ```text
     Tu mets à jour l'app Jarvis à partir de Notion. Dossier de Jarvis : ~/Documents/Jarvis. Réponds en français.
     1. Lis ~/Documents/Jarvis/sync/routine.md et suis sa consigne à la lettre. Lance chaque commande depuis le dossier de Jarvis : préfixe-la par « cd ~/Documents/Jarvis && ».
     2. Canal Slack où signaler un échec : #mon-canal (id), ou aucun. Rien sur Slack en cas de succès.
     ```
   - active le connecteur Notion pour la tâche et autorise le dossier de Jarvis ; lance-la une fois (« Run now ») pour vérifier.
4. **Bouton dans Jarvis** (facultatif) : avec Claude Code en ligne de commande installé et connecté, « Rafraîchir depuis Notion » (fiche) et « Tout rafraîchir » (Clients) lancent la routine sans jeton ; l'état s'affiche sous le bouton et la page se recharge à la fin. Sans jeton ni `claude`, il est grisé et son infobulle dit pourquoi.

La consigne vit dans le dépôt : un `git pull` met à jour la tâche planifiée, la commande et le bouton. Détails (déroulé d'un passage, outils permis, journal) : [`docs/routine-synchro.md`](docs/routine-synchro.md).

## Lancer des DAGs

L'onglet Injections propose les DAGs de l'orchestrator qui traitent chaque ressource, sur integration, snap ou recette (la prod est désactivée).
- Réglages Airflow (page **Réglages** ou `.env`) : `AIRFLOW_BASE_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD` ; sans eux, aucun DAG n'est lancé et Jarvis dit quoi régler. **Sources** dit si Airflow est connecté.
- **Lancer** passe par [`air.py`](src/deploy_hub/air.py), comme `/air` : la confirmation montre son aperçu (DAG, stack, ressources, « ⚠️ run déjà en cours ») ; un DAG plus chargé par Airflow est refusé, un DAG en pause en est sorti, puis le run part avec `padoa_env`. Un aperçu en échec (Airflow injoignable, DAG absent, accès refusé) s'affiche et rien ne part.
- Après chaque lancement réel, `air.py` suit aussi le run : il relance les échecs passagers (3 fois au plus), envoie ses alertes en notifications et fait un rapport (**Agents → Suivis de DAG**). Suivis, rapports et journal sont gardés au redémarrage, et un suivi en cours reprend là où il en était ; relances au plus et intervalle de vérification dans **Réglages** (`JARVIS_DAG_MAX_CLEARS`, `JARVIS_DAG_INTERVAL_S`). Comme `/air`, il ne passe que par l'API REST d'Airflow : rien à régler en plus de `AIRFLOW_BASE_URL`, `AIRFLOW_USER` et `AIRFLOW_PASSWORD` (aucun accès aux fichiers de logs). Ce qu'il écrit reste dans `data/airflow/`.
- **Enchaînement** : plusieurs DAG cochés partent dans l'ordre ; le suivant ne part que si le suivi du précédent finit vert, sinon la chaîne s'arrête et le dit en notification (**Agents → Enchaînements**, bouton « Arrêter »). Jamais en prod.
- **Depuis Claude Code** : ouvre le dossier de Jarvis dans Claude Code et tape `/air` (skill [`.claude/skills/air/SKILL.md`](.claude/skills/air/SKILL.md)) : client par défaut, liste des DAGs et des runs, lancement après confirmation, suivi et rapport, avec les mêmes réglages Airflow que l'app (`uv run python -m deploy_hub.air_jarvis <commande>`). Ces runs n'apparaissent pas dans **Agents → Suivis de DAG** ; rapports et journal vont aussi dans `data/airflow/`.
- **DAGs proposés : ceux de ton clone local**, tel qu'il est sur le disque, comme le lit l'Airflow local (branche extraite dans `ORCHESTRATOR_DIR`, modifications non commitées comprises ; ce réglage est lu au démarrage, vide : `~/integration-orchestrator`). Pour changer de DAGs, change de branche dans le clone : Jarvis suit. [`sync/dags/generer_catalogue.py`](sync/dags/generer_catalogue.py) lit le clone sans le modifier et écrit le catalogue dans `data/dags/` (quelques secondes ; le premier passage télécharge Python 3.12 et les paquets du générateur). Régénéré au démarrage, quand le clone change (vérifié chaque minute) et sur « Régénérer » (onglet Injections). Sans clone : le catalogue versionné `sync/dags/catalogue.json` (copie figée de `main`), et le bandeau de l'onglet Injections le dit.

## Flow Digger

Le bouton **Flow Digger ↗** de la barre ouvre [integration-flow-digger](https://github.com/padoa/integration-flow-digger) dans sa propre fenêtre, et le démarre d'abord sur la VM s'il ne tourne pas (`uv run main.py --start --no-browser`). Son installation (outils, 1Password, Google Drive, configs client) se fait une fois, selon son README.

## Lancer des agents Claude

Sur un retour de recette, « Lancer un agent » envoie le brief à Claude Code (`claude -p`) :
- dans un **worktree** de l'orchestrator, sur la branche `fix-<client>-<retour>` : ton clone de travail n'est jamais modifié ;
- outils limités à `JARVIS_AGENT_ALLOWED_TOOLS` (lire, modifier, `uv run`, git sans reset, `gh pr create`), lecture hors worktree limitée à `JARVIS_AGENT_ADD_DIRS`, sans les jetons de Jarvis ;
- suivi dans **Agents** (journal, compte rendu, PR, bouton « Arrêter ») ;
- **questions** : un agent qui finit son compte rendu par des questions passe en « question en attente » ; « Répondre et relancer » reprend sa session Claude Code avec ta réponse ;
- **relecture guidée** sur la page d'un agent terminé : fichiers changés, tests lancés et leur résultat, appels d'outils refusés, contrôles (pas de PR, pas de test, étape commune touchée, fichiers non commités) ;
- **coût et durée** par agent et par type de ticket, en bas de la page Agents.

Prérequis : Claude Code et `gh` connectés, et le dossier `.claude` de l'orchestrator à ton utilisateur (sinon : `sudo chown -R $USER:$USER <ORCHESTRATOR_DIR>/.claude`, voir [`docs/vm.md`](docs/vm.md)). Les worktrees restent après la PR : `git worktree remove <chemin>`.

## Suivre les PR (GitHub)

L'onglet **PR** d'une fiche liste les PR ouvertes de l'orchestrator qui concernent le client, avec CI, relecture, jours depuis l'ouverture et jours sans mouvement.
- **Sans IA et sans jeton** : Jarvis lance la commande `gh` dans le clone de l'orchestrator (`ORCHESTRATOR_DIR`), avec la session `gh auth login` de la machine. Prérequis : `gh` installé et connecté, sur le Mac comme sur la VM.
- **En deux temps** : `gh pr list` pour toutes les PR ouvertes, puis `gh pr view` (CI et relecture) pour les seules PR du client. Tout lire d'un coup prenait 18 s sur 165 PR.
- **Rattachement au client** : scope du titre (`[FIX](client) …`), PR ouverte par un de ses agents, ou scope égal à son logiciel.
- **Lecture seule**, réponses gardées 5 minutes, bouton « Actualiser ». Code : [`sources/pulls.py`](src/deploy_hub/sources/pulls.py).

## Perso

Tout est dans `data/perso.json`, sur ta machine ; rien n'est écrit dans Notion.
- **Brief du matin** : bouton « Brief » de l'accueil, texte prêt à coller (retards, échéances, injections KO, jalons, rappels).
- **Épingles** : bouton « Épingler » d'un ticket ; il remonte sur l'accueil et sur la synthèse de la fiche de son client.
- **Notes** : bouton rond « Notes » en bas à droite (touche `n`) : une note par client, la même que « Mes notes » de sa fiche, et une note générale.
- **Rappels** : sur un ticket ou libres (demain, lundi, dans une semaine, une date) ; ils reviennent en notification et dans le brief.

## Sur une VM

Pour que les agents travaillent sans ton poste, avec en plus un shell de la VM dans la console : [`docs/vm.md`](docs/vm.md).

## Données et confidentialité

- `data/` et `config/` sont hors de git : rien de ce qui vient de Notion n'est versionné.
- La synchro de la recette n'accepte qu'une liste fixe de colonnes ; le contenu des retours est rangé à part (`data/notion/<client>/contenu-recette.json`).
- Le brief d'agent interdit toute donnée client dans le code, les tests ou la PR.

## Réglages (page Réglages, `.env`)

Tous facultatifs. La page **Réglages** (accessible seulement en local, par exemple par le transfert de port de la VM) les modifie et les enregistre dans `.env` ; modèle commenté : [`.env.example`](.env.example). Les réglages lus au démarrage (dossiers, `ORCHESTRATOR_DIR`) s'appliquent au prochain lancement de Jarvis, la page le dit.

| Variable | Rôle |
|---|---|
| `JARVIS_CONFIG_DIR`, `JARVIS_DATA_DIR` | Emplacement de la configuration et des données |
| `NOTION_TOKEN` | Synchro par l'API Notion ; vide : le bouton « Rafraîchir depuis Notion » lance la routine Claude Code |
| `CLAUDE_BIN`, `JARVIS_SYNC_ALLOWED_TOOLS` | Routine lancée par le bouton : binaire `claude`, outils permis (vide : liste par défaut) |
| `JARVIS_NOTION_WORKSPACE` | Espace de travail Notion des liens complets que l'annuaire pré-remplit (`https://app.notion.com/p/<espace>/<Titre>-<id>`, vide : `padoa`) |
| `JARVIS_NOTION_DASHBOARDS` | Page Notion qui liste les dashboards des clients, lue chaque jour pour l'annuaire de « Ajouter un client » (vide : « Liste des dashboards pour le déploiement des nouveaux clients ») |
| `AIRFLOW_BASE_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD` | Lancement réel des DAGs |
| `JARVIS_DAG_MAX_CLEARS`, `JARVIS_DAG_INTERVAL_S` | Suivi des runs : relances automatiques au plus (0 : aucune), secondes entre deux vérifications |
| `ORCHESTRATOR_DIR` | Clone local de l'orchestrator : DAGs proposés, agents Claude, PR (vide : `~/integration-orchestrator`) |
| `CLAUDE_BIN`, `JARVIS_AGENT_*` | Agents Claude |
| `FLOW_DIGGER_DIR`, `FLOW_DIGGER_PORT`, `FLOW_DIGGER_URL` | Bouton Flow Digger (dossier, port 4004, adresse si le transfert de port en change) |
| `PADOA_ENV_URL` | Bouton Environnements du dock (snap, snap-recette, integration) : modèle d'adresse avec `[stack]` et `[client]`, vide : `https://[stack]-[client].aodap-staging.fr/support` ; `[client]` = nom du client dans Jarvis (son slug) |

**Secrets** (`NOTION_TOKEN`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD`) : en clair, ou une référence 1Password `op://<coffre>/<élément>/<champ>` (« Copy Secret Reference »), lue par `op read` à la première demande et gardée en mémoire seulement. Pour une référence, 1Password doit être connecté : **Réglages → 1Password** (ton mot de passe sert une fois à `op signin` et n'est ni gardé ni écrit ; la page relit aussitôt les références), l'app 1Password, ou `eval $(op signin)` dans le terminal qui lance Jarvis. Sur une machine sans session 1Password (VM), **Sources** affiche « référence 1Password illisible ». `.env` est ramené à `600`, et Jarvis refuse de démarrer si ses données ou sa configuration sont dans un dépôt git sans y être ignorées.

## Développement

```bash
uv run pytest
uv run uvicorn deploy_hub.main:app --port 8765 --reload --reload-dir src
```

### Environnement local fictif

Pour voir un changement de l'interface sur le Mac sans pull ni VM :

```bash
uv run python demo/lancer.py          # http://localhost:8766 ; --neuf pour repartir des données d'origine
```

Seulement du faux : trois clients (`demo/clients/`, dont un jamais synchronisé), les fiches des tests (`demo/deploiements/`), un catalogue de DAGs (`demo/dags/`), un faux Airflow HTTP local que le vrai `air.py` appelle (`demo/faux_airflow.py` : un run passe en succès en 26 s), un faux `claude` pour les agents et le bouton « Rafraîchir depuis Notion » (`demo/faux_claude.py` : un agent sur deux pose une question). Ni `.env`, ni `config/`, ni `data/` ne sont lus ; l'état vit dans `demo/.etat/` (ignoré par git). Les tests n'utilisent pas ce lanceur : la validation reste `uv run pytest` et l'app réelle sur la VM.

Versions : numéro dans `pyproject.toml`, historique et règle de numérotation dans [`CHANGELOG.md`](CHANGELOG.md) (une ligne par PR sous « Non publié »).

Routes dans `src/deploy_hub/main.py`, pages dans `templates/` et `static/`, synchro Notion dans `sync_notion.py` (consigne de la routine : `sync/routine.md`, lanceur : `sources/sync_routine.py`), sources (Notion, Airflow, agents, PR GitHub, perso, notifications) dans `sources/`.

## Limites connues

- Après un changement du clone orchestrator, la liste des DAGs est à jour au plus une minute plus tard (ou tout de suite avec « Régénérer ») ; une page Injections déjà ouverte ne se recharge pas seule.
- Les runs lancés par `/air` n'apparaissent pas dans **Agents → Suivis de DAG** (leurs rapports sont dans `data/airflow/`).
- Un changement fait seulement sur une carte Bible n'est vu qu'à la synchro complète suivante (7 jours au plus).
