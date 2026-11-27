# Réglages de Jarvis

Tous facultatifs. La page **Réglages** (accessible seulement en local, par exemple par le transfert de port de la VM) les modifie et les enregistre dans `.env` ; modèle commenté : [`.env.example`](../.env.example). Les réglages lus au démarrage (dossiers, `ORCHESTRATOR_DIR`) s'appliquent au prochain lancement de Jarvis, la page le dit.

| Variable | Rôle |
|---|---|
| `JARVIS_CONFIG_DIR`, `JARVIS_DATA_DIR` | Emplacement de la configuration et des données |
| `NOTION_TOKEN` | Synchro par l'API Notion ; vide : le bouton « Rafraîchir depuis Notion » lance la routine Claude Code |
| `CLAUDE_BIN`, `JARVIS_SYNC_ALLOWED_TOOLS` | Routine lancée par le bouton : binaire `claude`, outils permis (vide : liste par défaut) |
| `JARVIS_SYNC_MODEL`, `JARVIS_SYNC_EFFORT` | Modèle et effort de la routine de synchro (vides : `haiku`, `low`) |
| `JARVIS_SYNC_EVERY_MIN` | Intervalle de la synchro Notion automatique, en minutes (vide : 60 ; 0 : jamais). Elle ne concerne que les clients dont le bouton « Synchro auto » de la fiche est activé |
| `JARVIS_NOTION_WORKSPACE` | Espace de travail Notion des liens complets que l'annuaire pré-remplit (`https://app.notion.com/p/<espace>/<Titre>-<id>`, vide : `padoa`) |
| `JARVIS_NOTION_DASHBOARDS` | Page Notion qui liste les dashboards des clients, lue chaque jour pour l'annuaire de « Ajouter un client » (vide : « Liste des dashboards pour le déploiement des nouveaux clients ») |
| `JARVIS_BAU_SQUADS` | Vues Notion des squads de la page BAU, « nom | lien de la vue » séparées par « ; » (vide : SPSTA et Plateform) |
| `JARVIS_NOTION_BAU` | Lien (ou id) de la page projet ou BAU dont Jarvis suit les demandes, page **BAU** (vide : « BAU équipe intégration - TECHNIQUE ») |
| `AIRFLOW_BASE_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD` | Lancement réel des DAGs |
| `JARVIS_DAG_MAX_CLEARS`, `JARVIS_DAG_INTERVAL_S` | Suivi des runs : relances automatiques au plus (0 : aucune), secondes entre deux vérifications |
| `ORCHESTRATOR_DIR` | Clone local de l'orchestrator : DAGs proposés, agents Claude, PR (vide : `~/integration-orchestrator`) |
| `CLAUDE_BIN`, `JARVIS_AGENT_*` | Agents Claude |
| `FLOW_DIGGER_DIR`, `FLOW_DIGGER_PORT`, `FLOW_DIGGER_URL` | Bouton Flow Digger (dossier, port 4004, adresse si le transfert de port en change) |
| `INTECG_DIR`, `INTECG_URL` | InteCG, le jeu de cartes de l'équipe : dossier de son dépôt (vide : cherché aux emplacements habituels, dont `~/Dev/Apps/intecg`) et son adresse (vide : `http://localhost:5173` si le dépôt est sur la machine, sinon pas de lien). Voir [InteCG](guide.md#intecg) |
| `PADOA_ENV_URL` | Bouton Environnements du dock (snap, snap-recette, integration) : modèle d'adresse avec `[stack]` et `[client]`, vide : `https://[stack]-[client].aodap-staging.fr/support` ; `[client]` = nom du client dans Jarvis (son slug) |

**Secrets** (`NOTION_TOKEN`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD`) : enregistrés dans `.env`, et jamais réaffichés par la page Réglages (le champ arrive vide : vide veut dire « inchangé »). `.env` est ramené à `600`, et Jarvis refuse de démarrer si ses données ou sa configuration sont dans un dépôt git sans y être ignorées.
