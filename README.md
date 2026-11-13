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

La synchro est incrémentale et calculée par Jarvis : une seule requête d'empreinte pour tous les clients, puis seulement les lignes modifiées (ajouts compris), une liste compacte d'identifiants quand des lignes disparaissent, et une relecture complète des actions tous les 7 jours (leurs ressources viennent des cartes Bible). Détail dans [`docs/routine-synchro.md`](docs/routine-synchro.md).

### Jeton Notion (bouton « Rafraîchir »)
1. Sur https://www.notion.so/profile/integrations, créer une intégration **interne**, capacité « Lire le contenu » seulement (la création peut demander un admin du workspace).
2. La connecter (••• → Connexions) aux bases Clients en déploiement, Actions de déploiement, Retours Recette, Catalogue RDD, Bible déploiement et Resources.
3. Mettre le jeton dans `.env` (`NOTION_TOKEN=…`), puis relancer l'app.

## Lancer des DAGs

L'onglet Injections propose, pour chaque carte, les DAGs de l'orchestrator qui traitent ses ressources, et la stack cible (`padoa_env` : integration, snap ou recette ; la prod est désactivée par défaut).

- **Airflow** : renseigner `AIRFLOW_BASE_URL`, `AIRFLOW_USER` et `AIRFLOW_PASSWORD` dans `.env` (API REST v1). Sans eux, les lancements sont simulés et listés dans « Agents ».
- **Catalogue des DAGs** : `sync/dags/catalogue.json`, généré depuis un clone de l'orchestrator. À régénérer quand `dags/config_deployment.py` change (commande en tête de [`sync/dags/generer_catalogue.py`](sync/dags/generer_catalogue.py)).
- Pour réactiver la prod : ajouter `"prod"` à `PADOA_ENVS` dans `src/deploy_hub/sources/airflow.py` (il faut alors taper PROD pour confirmer chaque lancement).
- **Un DAG en pause** est sorti de pause avant le lancement (sinon le run attendrait) ; le message le dit.

### Suivi automatique des runs (`air.py`)
Après chaque lancement réel (onglets Injections et CED, console `>dag`), Jarvis lance en tâche de fond `python -m deploy_hub.air watch <dag> <run> --client <client>` : le script d'Arthur ([`src/deploy_hub/air.py`](src/deploy_hub/air.py)), repris tel quel à trois adaptations près (repérées « Jarvis »).
- Toutes les 60 s, il relit le run. Une tâche en échec **passager** (disque plein, délai, connexion, tâche tuée par manque de mémoire) est relancée avec ses suites, 3 fois au plus ; les lots d'une tâche « mappée » le sont par vagues adaptatives. Une erreur SQL ou Python n'est jamais relancée.
- Ses alertes deviennent des **notifications** de Jarvis ; à la fin, un **rapport** (durée, volumes du loader, compare, cleaner, mappings manquants) s'affiche sur la page du suivi (**Agents → Suivis de DAG**), avec le journal des relances et un bouton « Arrêter le suivi » (le run, lui, continue).
- Pour classer les erreurs et faire le rapport, le suivi **lit les logs des tâches qu'il suit**. État, rapports et journal vont dans `data/airflow/` (hors de git) : ils peuvent contenir des valeurs client.
- Un redémarrage de Jarvis (rechargement compris) coupe les suivis puis les **reprend** : `air.py` repart de son état (relances déjà faites, notifications déjà envoyées). Sans Airflow configuré au redémarrage, ils passent « interrompu » et le run continue sans relances.

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

## Faire tourner Jarvis sur une VM

Utile pour que les agents travaillent sans ton poste : Jarvis tourne en service systemd utilisateur sur la VM, tu l'ouvres depuis ton poste par un transfert de port.

1. **Cloner et installer**, sur la VM, dans le dossier de ton choix :
   ```bash
   gh repo clone <organisation>/Jarvis && cd Jarvis && uv sync
   ```
2. **Copier ta configuration et tes données**, depuis ton poste (l'historique des agents reste propre à chaque machine) :
   ```bash
   rsync -a --exclude agents/ config data <vm>:<dossier de Jarvis sur la VM>/
   ```
3. **Installer le service**, sur la VM, depuis le dossier de Jarvis. Le dossier de Jarvis (`$PWD`) et les réglages non secrets vont dans un complément du service ; remplace les `<…>` :
   ```bash
   mkdir -p ~/.config/systemd/user/jarvis.service.d && cp deploy/jarvis.service ~/.config/systemd/user/
   printf '[Service]\nWorkingDirectory=%s\nEnvironment=JARVIS_MODE=live\nEnvironment=ORCHESTRATOR_DIR=<dossier de integration-orchestrator>\nEnvironment=CLAUDE_BIN=%s/deploy/claude-vscode.sh\nEnvironment=JARVIS_AGENT_ADD_DIRS=<dossier de notes>\nEnvironment=AIRFLOW_BASE_URL=http://localhost:8888\n' "$PWD" "$PWD" > ~/.config/systemd/user/jarvis.service.d/vm.conf
   systemctl --user daemon-reload && systemctl --user enable --now jarvis
   ```
   Adapte l'adresse d'Airflow à ta VM. `JARVIS_AGENT_ADD_DIRS` est facultatif : c'est le dossier de notes que l'agent peut lire en plus de son worktree.
4. **Garder le service actif** quand tu te déconnectes (sinon il s'arrête avec ta session SSH) :
   ```bash
   loginctl enable-linger $USER
   ```
5. **Secrets** : mets `AIRFLOW_USER`, `AIRFLOW_PASSWORD` et, si besoin, `NOTION_TOKEN` dans le `.env` à la racine du dossier de Jarvis, puis `systemctl --user restart jarvis`.
6. **Ouvrir Jarvis depuis ton poste** : transfert du port 8765, par l'onglet Ports de VS Code (Remote-SSH) ou `ssh -L 8765:localhost:8765 <vm>`, puis http://localhost:8765.

Bon à savoir :
- **Binaire Claude** : sans installation de Claude Code sur la VM, `deploy/claude-vscode.sh` utilise celui de l'extension VS Code Remote-SSH, en prenant toujours la version la plus récente (son chemin change à chaque mise à jour). Claude doit être connecté sur la VM (`~/.claude/.credentials.json`).
- **Ce que l'agent charge** : tes `~/.claude/CLAUDE.md` et hooks de la VM, comme une session normale. Un hook de fin de session (synchro de notes, par exemple) s'exécute donc aussi à la fin de chaque agent.
- **Synchro Notion** : la routine Claude Code de ton poste écrit dans le `data/` de ton poste, pas dans celui de la VM. Sur la VM, utilise le jeton Notion (« Rafraîchir depuis Notion ») ou recopie `data/` (étape 2).
- **Suivi** : `systemctl --user status jarvis` et `journalctl --user -u jarvis -f`.
- **Mise à jour** : `git pull && uv sync && systemctl --user restart jarvis`.

### Shell de la VM dans la console (`JARVIS_SHELL=1`)
La console (bouton en bas à droite, touche `>`) a alors un onglet **Shell VM** : un vrai terminal (xterm.js) relié au shell de l'utilisateur du service, sur la VM. Couleurs, Ctrl+C, programmes interactifs (`htop`, `vim`, `less`) fonctionnent.
- **Désactivé par défaut.** Pour l'activer sur la VM, et seulement là : ajouter `Environment=JARVIS_SHELL=1` au complément du service (étape 3 ci-dessus), puis `systemctl --user restart jarvis`.
- **Garde-fous** (`src/deploy_hub/shell.py`) : connexion locale seulement (le service écoute sur `127.0.0.1`, joint par transfert de port) ; la page doit venir de Jarvis (origine vérifiée : un autre site ouvert dans le navigateur ne peut pas s'y connecter) ; jeton tiré à chaque démarrage de Jarvis, glissé dans la page. Les secrets de Jarvis (`NOTION_TOKEN`, `AIRFLOW_*`) ne sont pas dans l'environnement du shell.
- **Session** : elle vit tant que la page est ouverte ; changer de page ou fermer l'onglet la termine. Pour garder un travail long, lance `tmux` sur la VM et rattache-toi.
- xterm.js est chargé depuis `cdn.jsdelivr.net` à la première ouverture de l'onglet.
- **Shell et thème** : c'est le shell de connexion de l'utilisateur (`/etc/passwd`) qui s'ouvre, avec sa configuration. Pour retrouver Oh My Zsh comme sur le Mac (thème `robbyrussell`, plugin `git`), sur la VM :
  ```bash
  sudo apt install -y zsh git
  # installe Oh My Zsh avec son installateur officiel (https://ohmyz.sh), puis dans ~/.zshrc :
  #   ZSH_THEME="robbyrussell"
  #   plugins=(git)
  chsh -s "$(command -v zsh)"          # zsh devient le shell de connexion
  systemctl --user restart jarvis      # les nouvelles sessions s'ouvrent dans zsh
  ```
  La palette du terminal est accordée à `robbyrussell`. Pour un thème à glyphes (agnoster, powerlevel10k), installe une police Nerd Font sur ton poste (`MesloLGS NF`, `JetBrainsMono Nerd Font` ou `Hack Nerd Font`) : le terminal la prend en premier.

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
