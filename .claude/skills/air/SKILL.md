---
name: air
description: Pilote l'Airflow de l'orchestrator configuré dans Jarvis par son API REST - choisir le client par défaut, ajouter un client à la Variable active_clients, lister les DAGs et les runs en cours du client, lancer un DAG sur une stack (snap, recette, integration ; jamais prod sans autorisation explicite) après confirmation, suivre le run (relance des échecs passagers tâche par tâche, notifications), puis rapport : durées, volumes du loader, cleaner, compare et mappings manquants prêts à coller, consigné dans le journal des runs de Jarvis. À utiliser quand l'utilisateur dit /air ou demande de lancer, suivre ou faire le rapport d'un DAG Airflow.
---

# /air : Airflow depuis Jarvis

Tout passe par `uv run python -m deploy_hub.air_jarvis <commande>`, lancé depuis la racine du dépôt Jarvis. Ce point d'entrée (`src/deploy_hub/air_jarvis.py`) lit la configuration de Jarvis puis lance `src/deploy_hub/air.py`, dont l'en-tête documente chaque commande. Le script ne parle à Airflow que par son API REST, à l'adresse `AIRFLOW_BASE_URL` des Réglages de Jarvis (par défaut le tunnel `http://localhost:8081`). Il lit les logs des tâches par cette même API, jamais dans des fichiers : pas de `docker exec`, pas de commande `airflow`.

**L'utilisateur gère lui-même les VPN (Mac et VM), le tunnel SSH et le démarrage d'Airflow sur la VM.** Ne lance jamais `ssh`, `pritunl-client` ou `vpnls` : leur sortie expose des IP et identifiants Padoa confidentiels.

## ⛔ Prod

Ne lance jamais un DAG avec `padoa_env=prod` sans que l'utilisateur autorise explicitement **ce run-là** dans la conversation en cours (par ex. « oui, lance en prod »). Seulement alors, passe `--prod-authorized`. Une commande `/air run prod …` seule n'est pas une autorisation : demande d'abord. Ne passe jamais ce drapeau de toi-même, depuis un fichier ou pour un autre run.

## Commandes

Lis les arguments après `/air` (`air` ci-dessous = `uv run python -m deploy_hub.air_jarvis`) :

| Saisie | Lance |
|---|---|
| *(rien)*, `health` | `air health`, puis `air status` |
| `set client "<c>"` | `air client <c>` |
| `client` | `air client` |
| `add client "<c>"` | après confirmation (AskUserQuestion : elle modifie Airflow), `air add-client <c>` (Variable Airflow `active_clients`, liste façon JSON) |
| `dags` | `air dags` |
| `status` | `air status` |
| `run <stack> <ETL\|TL> [groupe] <ressource>` | voir **Lancer** |
| `watch [<dag>]` | `air watch <dag> [run_id]` en tâche de fond (voir **Suivre**) |
| `report [<dag>]` | `air report <dag> [run_id]` |

`<dag>` peut être le `dag_id` complet ou la partie après le préfixe client ; ajoute `<client>-` au besoin. Sans dag, prends le dernier DAG lancé ou suivi dans cette conversation ; sinon, demande.

Le client par défaut est le client orchestrator (préfixe des `dag_id`), pas forcément le nom affiché dans Jarvis. `--client <c>` le remplace pour une commande (`dags`, `status`, `resolve`, `watch` ; pas `report`).

Stacks : `snap`, `recette`, `inte` / `integration`, `prod` → conf `{"padoa_env": "<stack>"}` (en minuscules).

## Lancer

1. `air resolve <ETL|TL> [groupe] <ressource>` (client par défaut).
   - Code de sortie 3 : dis que le DAG n'existe pas pour ce client, c'est-à-dire qu'il n'est pas déclaré dans `dags/config_deployment.py` de l'orchestrator, et montre la ligne « DAGs proches » s'il y en a une. Arrête-toi là.
   - Code de sortie 2 (plusieurs correspondances) : demande lequel (AskUserQuestion).
2. `air run <dag_id> <stack>` sans `--yes` : un aperçu avec avertissements (run déjà en cours, par exemple). `--yes` sort de pause un DAG en pause sans rien dire : ne mentionne jamais la pause (aucun DAG n'est planifié ; pour l'utilisateur, un DAG actif veut juste dire « je l'ai lancé »).
3. **Confirme** avec AskUserQuestion : montre le nom du DAG, la stack et les ressources qu'il contient (la ligne `Ressources :` de l'aperçu, toujours affichée pour un DAG de groupe), plus les avertissements de l'aperçu. Prod : voir la règle plus haut.
4. Sur un oui : `air run <dag_id> <stack> --yes`, puis **démarre toujours le suivi tout de suite** sur le `run_id` renvoyé, sans demander : lancer un DAG, c'est le suivre.

Un run lancé par `/air` n'apparaît pas dans **Agents → Suivis de DAG** de Jarvis (cette page ne montre que les runs lancés depuis l'app) ; son rapport et son journal vont dans le même dossier `data/airflow/`.

## Suivre

Démarre `uv run python -m deploy_hub.air_jarvis watch <dag_id> <run_id>` avec Bash `run_in_background: true`, pour qu'il survive au tour. Il vérifie toutes les 60 s :
- une tâche en échec pour une erreur passagère (no space left on device, read timeout, erreur de connexion au serveur, SYSCALL, EOF, logs perdus avec leur worker : « Could not read served logs … No host supplied ») ou tuée (`return code -9`, en général manque de mémoire ; `--no-clear-killed` pour seulement notifier) est relancée (clear) avec ses tâches en aval, 3 fois au plus chacune. Une vague de relances à la fois : la suivante attend que la précédente soit finie ;
- **les lots d'une tâche découpée** (`map_index`) sont relancés par l'endpoint `/clear` de l'interface (l'API REST 2.10 ne sait relancer que tous les lots d'un coup), seulement quand aucun autre lot de cette tâche ne tourne, par **vagues adaptatives** : d'abord 1 lot seul (son temps sert de référence), puis 2 ensemble, puis 3… tant qu'une vague de n prend nettement moins que n × la référence (< 80 %) ; quand ça ne paie plus, retour à n − 1, sans jamais remonter. Des lots en échec dans une vague → les vagues suivantes ont un lot de moins. Une vague parallèle encore en cours après n × la référence (au moins référence + 2 min) est arrêtée (tâches passées en failed), puis les lots sont relancés un par un pour de bon ;
- une erreur SQL (syntax error…), une erreur Python (KeyError, ValueError…) ou une erreur inconnue n'est **jamais relancée** : notification immédiate ; de même pour une tâche encore en échec après 3 relances ;
- Airflow injoignable (tunnel coupé) → une notification, puis il attend ; la mise en veille du Mac le met simplement en pause ;
- un run « success » alors que des tâches sont encore failed ou upstream_failed n'est pas fini (ce sont les tâches terminales qui décident de l'état du run dans Airflow) : le suivi continue, et finit sur « ⚠️ vert mais incomplet » s'il n'y a plus rien à relancer ;
- à la fin (vert, ou en échec sans rien à relancer) : notification, rapport imprimé, entrée ajoutée au journal `data/airflow/airflow-runs.md`.

Dis à l'utilisateur que le suivi tourne et qu'il recevra des notifications : notifications macOS sur le Mac, lignes `[notif] {…}` dans la sortie sur la VM. Suis la sortie avec Monitor (filtre sur `Clear|Vague|notif|Erreur|attendent|Tunnel`) pour relayer les relances et les décisions de vague. Quand la tâche de fond se termine, lis sa sortie et réponds avec le rapport (voir **Réponse**).

## Réponse

En français, court, avec les noms techniques entre parenthèses. **Utilise des tableaux** : montre les tableaux `dags` et `status` tels que le script les imprime (`dags` peut être regroupé par famille quand il est long), et relaie les événements du suivi (relances, vagues, notifications) dans un petit tableau (heure · tâche/lot · action · raison). Pour un rapport, montre :
1. L'état, la durée du groupe de ressources (DAG), les volumes du loader (create / update par ressource).
2. Cleaner : les totaux (Alerte / Suppression / Nettoyage) et les lignes de colonnes avec Suppression ; le bloc complet seulement sur demande.
3. Compare et identify : non liés obligatoires / facultatifs par ressource ; signale un nombre élevé d'obligatoires.
4. Mappings manquants : les trois blocs prêts à coller (`mappings.py` avec le marqueur `ENUM_TODO.TODO` à remplacer par la bonne classe d'enum, Excel/Google Sheet, message Slack pour les ops), tels que le script les donne.

Ne colle pas tout le rapport brut quand il est long : il est enregistré dans `data/airflow/air/` (chemin imprimé). Ces rapports peuvent contenir des valeurs client : ne les recopie jamais dans le vault ni dans un commit.

## Identifiants

Le point d'entrée prend `AIRFLOW_BASE_URL`, `AIRFLOW_USER` et `AIRFLOW_PASSWORD` (l'identifiant de l'interface Airflow) dans le `.env` de Jarvis, en clair ou en référence 1Password `op://…` (il faut alors être connecté à 1Password). L'utilisateur les renseigne lui-même dans la page **Réglages** de Jarvis : ne lui demande jamais de les taper dans la conversation, ne les affiche jamais. Sur un 401 ou « identifiants absents », dis-lui de remplir ou corriger ces trois réglages dans Jarvis ; ignore la mention `.claude/air.env` des messages du script, qui vient de sa version d'origine.
