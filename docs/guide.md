# Guide de Jarvis

Tout ce que fait Jarvis, fonction par fonction. Pour installer et démarrer : le [README](../README.md). Les réglages : [reglages.md](reglages.md).

- [Ajouter un client](#ajouter-un-client)
- [Fiche client](#fiche-client)
- [Synchroniser avec Notion](#synchroniser-avec-notion)
- [Demandes BAU](#demandes-bau)
- [Lancer des DAGs](#lancer-des-dags)
- [Lancer des agents Claude](#lancer-des-agents-claude)
- [Suivre les PR (GitHub)](#suivre-les-pr-github)
- [Notifications](#notifications)
- [Perso](#perso)
- [Flow Digger](#flow-digger)
- [InteCG](#intecg)
- [Mises à jour](#mises-à-jour)
- [Données et confidentialité](#données-et-confidentialité)
- [Limites connues](#limites-connues)

## Ajouter un client

**Clients → Ajouter un client**, et choisis le client dans la liste : son identifiant (lettres et chiffres, comme dans l'orchestrator : « APST 26-07 » → `apst2607`), ses clés orchestrator, sa fiche « Clients en déploiement », son dashboard intégration et ses autres dashboards (déploiement, formation, gestion financière) se remplissent ; tu relis, puis tu ajoutes. Cette liste est l'annuaire des clients, que Jarvis fait relire en fond, à son démarrage puis chaque jour, par Claude Code (connecteur Notion, lecture seule, une vingtaine de secondes) : elle est prête quand tu ouvres la page. Il la lit depuis la page « Liste des dashboards pour le déploiement des nouveaux clients » et la base « Clients en déploiement » ; « Relire maintenant » le relit à la demande. L'emoji affiché devant le nom d'un client est l'icône de son dashboard intégration : sans `NOTION_TOKEN` (la routine de synchro ne voit pas les icônes), une petite lecture à part (Haiku, une dizaine de secondes ; une image mal relue est reprise avec le modèle par défaut), en parallèle de l'annuaire, la récupère une fois pour chaque client qui n'en a pas encore (à son ajout, par exemple). Sans Claude Code, « Ou remplir à la main » : le lien de sa fiche (••• → Copier le lien), et au besoin son dashboard intégration. Facultatif : ses stacks. Autre liste de dashboards : `JARVIS_NOTION_DASHBOARDS` (id de la page). La configuration va dans `config/clients/<slug>.yaml` (modèle : [`config.example/clients/exemple.yaml`](../config.example/clients/exemple.yaml)).

Le client s'ouvre aussitôt, et sa première synchro Notion part toute seule (par l'API avec un jeton, sinon par la routine) ; sa fiche en montre l'avancement.

## Fiche client

Synthèse et jalons, injections par ressource (une liste : l'état de chaque ressource, ce qui bloque en haut, la stack choisie une fois et un bouton « Lancer » par ligne, pour chaque ressource et chaque groupe de ressources ; ouvrir une ligne montre ses cartes et ses derniers DAG lancés, qui mènent à leur suivi), actions par équipe, retours de recette (avec brief pour un agent Claude), règles de reprise, CED.

## Synchroniser avec Notion

Synchro incrémentale : une requête d'empreinte pour tous les clients, puis seulement ce qui a changé. **Clients synchronisés : ceux configurés dans ton Jarvis** (`config/clients/`, ou `JARVIS_CONFIG_DIR`) ; un client ajouté est pris au passage suivant, un client archivé ou retiré ne l'est plus. **Archiver** un client (sa carte de la page Clients, ou Configurer) quand on ne travaille plus dessus : il sort du menu, de l'accueil, des notifications et de la synchro ; sa fiche reste consultable dans « Archivés », en bas de la page Clients, et « Désarchiver » le remet dans le suivi (clé `archived` de son yaml).

Deux façons de lire Notion, au choix :

| | Sans jeton : routine Claude Code (recommandé) | Avec jeton : API Notion |
|---|---|---|
| Comment | Claude exécute les requêtes avec son connecteur Notion, en suivant [`sync/routine.md`](../sync/routine.md) | Jarvis appelle l'API avec une intégration Notion interne en lecture seule |
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

La consigne vit dans le dépôt : un `git pull` met à jour la tâche planifiée, la commande et le bouton. Détails (déroulé d'un passage, outils permis, journal) : [`docs/routine-synchro.md`](routine-synchro.md).

## Demandes BAU

Pour l'équipe qui traite des demandes plutôt qu'un déploiement : la page **BAU** affiche les actions de la base Notion « Equipes OPS - Actions internes » liées à une page projet ou BAU, par exemple « BAU équipe intégration - TECHNIQUE ».
- **Mise en place** : rien à régler. « BAU » est dans la barre du haut et suit « BAU équipe intégration - TECHNIQUE », la même page pour toute l'équipe ; « Rafraîchir depuis Notion » charge les demandes (une minute environ la première fois). Aucun client n'est nécessaire. Pour suivre une autre page projet ou BAU : son lien dans **Réglages → Notion** → `JARVIS_NOTION_BAU` (••• → Copier le lien, celui de la page elle-même, pas d'une vue).
- **Liens de l'équipe** : l'équipe n'a pas de client pour porter ses liens ; « + Ajouter des liens » (page BAU, ou le « + » des raccourcis à droite) enregistre ses dossiers Google Drive, ses pages Notion et tout autre lien (outils, canaux Slack, dépôts…). Ils s'affichent sous le titre de la page BAU et dans les raccourcis à droite, sur les pages BAU et partout quand aucun client n'est suivi. Gardés dans `config/bau.yaml`, sur ta machine ; jamais lus par Jarvis, seulement ouverts.
- **Une vue par squad, comme dans Notion** : « Toute l'équipe », « Squad SPSTA », « Squad Plateform ». Chaque squad montre exactement les demandes de sa vue « Action squad » dans Notion (ses membres, demandes à faire ou en cours), relue à chaque synchro : si la vue change dans Notion, Jarvis suit. Le choix est gardé d'une visite à l'autre. Autres vues : `JARVIS_BAU_SQUADS` (« nom | lien de la vue », séparées par « ; »).
- **Ce qu'on y voit** : les demandes en liste ou en kanban (une colonne par statut Notion), ouvertes ou toutes, les plus prioritaires d'abord (P0, P1, P2), puis les échéances les plus proches. Filtres : statut, priorité, responsable, référent inté ; la recherche trouve aussi un client lié ou une ressource ; tout est gardé d'une visite à l'autre. Une demande s'ouvre en panneau : responsables, référents, clients liés (avec le lien vers leur fiche s'ils sont suivis dans Jarvis), ressources, échéance, lien Notion. L'accueil résume où en est l'équipe (demandes ouvertes, en retard).
- **Comme un retour de recette** : « Lancer un agent » sur une demande ouverte (liste ou panneau) prépare un brief pour Claude Code, sur une branche `bau-<client>-<premiers mots de l'intitulé>` ; l'agent se suit dans **Agents** (journal, questions, PR) et apparaît dans le panneau de sa demande. Le brief reprend la description de la demande ; une carte sans description laisse « À COMPLÉTER », et l'agent ne part pas tant que tu ne l'as pas remplacé. Une demande s'épingle (page BAU et accueil) et se rappelle comme un ticket.
- **Synchro auto** : bouton « Synchro auto » de la page BAU, comme sur la fiche d'un client (non par défaut) : Jarvis relit les demandes tout seul, toutes les heures (`JARVIS_SYNC_EVERY_MIN`), et ne relit que ce qui a changé. Mesuré sur la VM le 2026-11-24 : premier passage 58 s (531 demandes lues entières et les 2 vues de squad).
- **Synchro** : par la routine Claude Code seulement (connecteur Notion, lecture seule), même avec un `NOTION_TOKEN` : le bouton de la page BAU ne relit que les demandes ; « Tout rafraîchir », `/jarvis-sync` et la tâche planifiée les relisent avec les clients. Une requête d'empreinte, puis seulement les demandes modifiées ; tout est relu quand des demandes ont disparu de la page, et tous les 7 jours.
- **Description d'une demande** : le corps de sa page Notion, que la synchro ne voit pas (elle ne lit que les colonnes). La synchro charge d'abord les demandes ; les descriptions se lisent ensuite en fond, par lots de 15 enchaînés, les demandes ouvertes les plus récemment modifiées d'abord, jusqu'à ce que toutes soient à jour ; ensuite, seules les demandes modifiées sont relues. Mesuré sur la VM le 2026-11-24 : 12 à 15 s par lot, les 249 demandes ouvertes en 4 minutes environ. La page BAU dit où en est la lecture (« Descriptions : 120 / 249 »), et une demande qu'on ouvre avant fait lire la sienne tout de suite (une dizaine de secondes). Elle s'affiche dans le panneau ; elle est gardée sur ta machine (`data/notion/_bau/descriptions/`) et relue quand la demande change dans Notion, ou sur « Relire dans Notion ». Les images ne sont pas reprises : la description renvoie à la page. Rien n'est écrit dans Notion ; les demandes restent sur ta machine (`data/notion/_bau/`).

## Lancer des DAGs

L'onglet Injections propose les DAGs de l'orchestrator qui traitent chaque ressource, sur integration, snap ou recette (la prod est désactivée).
- Réglages Airflow (page **Réglages** ou `.env`) : `AIRFLOW_BASE_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD` ; sans eux, aucun DAG n'est lancé et Jarvis dit quoi régler. **Sources** dit si Airflow est connecté.
- **Lancer** passe par [`air.py`](../src/deploy_hub/air.py), comme `/air` : la confirmation montre son aperçu (DAG, stack, ressources, « ⚠️ run déjà en cours ») ; un DAG plus chargé par Airflow est refusé, un DAG en pause en est sorti, puis le run part avec `padoa_env`. Un aperçu en échec (Airflow injoignable, DAG absent, accès refusé) s'affiche et rien ne part.
- Après chaque lancement réel, `air.py` suit aussi le run : il relance les échecs passagers (3 fois au plus), envoie ses alertes en notifications et fait un rapport (**Agents → DAG** : une carte par run, rangée par état (en cours, en échec ou arrêtés, terminés), avec son état, sa durée, ses relances automatiques et sa dernière nouvelle). Suivis, rapports et journal sont gardés au redémarrage, et un suivi en cours reprend là où il en était ; relances au plus et intervalle de vérification dans **Réglages** (`JARVIS_DAG_MAX_CLEARS`, `JARVIS_DAG_INTERVAL_S`). Comme `/air`, il ne passe que par l'API REST d'Airflow : rien à régler en plus de `AIRFLOW_BASE_URL`, `AIRFLOW_USER` et `AIRFLOW_PASSWORD` (aucun accès aux fichiers de logs). Ce qu'il écrit reste dans `data/airflow/`.
- **Enchaînement** : plusieurs DAG cochés partent dans l'ordre ; le suivant ne part que si le suivi du précédent finit vert, sinon la chaîne s'arrête et le dit en notification (**Agents → DAG** : une seule carte pour la chaîne, avec ses étapes et le bouton « Arrêter la chaîne »). Jamais en prod.
- **Depuis Claude Code** : ouvre le dossier de Jarvis dans Claude Code et tape `/air` (skill [`.claude/skills/air/SKILL.md`](../.claude/skills/air/SKILL.md)) : client par défaut, liste des DAGs et des runs, lancement après confirmation, suivi et rapport, avec les mêmes réglages Airflow que l'app (`uv run python -m deploy_hub.air_jarvis <commande>`). Ces runs n'apparaissent pas dans **Agents → Suivis de DAG** ; rapports et journal vont aussi dans `data/airflow/`.
- **DAGs proposés : ceux de ton clone local**, tel qu'il est sur le disque, comme le lit l'Airflow local (branche extraite dans `ORCHESTRATOR_DIR`, modifications non commitées comprises ; ce réglage est lu au démarrage, vide : `~/integration-orchestrator`). Pour changer de DAGs, change de branche dans le clone : Jarvis suit. [`sync/dags/generer_catalogue.py`](../sync/dags/generer_catalogue.py) lit le clone sans le modifier et écrit le catalogue dans `data/dags/` (quelques secondes ; le premier passage télécharge Python 3.12 et les paquets du générateur). Régénéré au démarrage, quand le clone change (vérifié chaque minute) et sur « Régénérer » (onglet Injections). Sans clone : le catalogue versionné `sync/dags/catalogue.json` (copie figée de `main`), et le bandeau de l'onglet Injections le dit.


## Lancer des agents Claude

Sur un retour de recette, « Lancer un agent » envoie le brief à Claude Code (`claude -p`) :
- dans un **worktree** de l'orchestrator, sur la branche `fix-<client>-<retour>` : ton clone de travail n'est jamais modifié ;
- outils limités à `JARVIS_AGENT_ALLOWED_TOOLS` (lire, modifier, `uv run`, git sans reset, `gh pr create`), lecture hors worktree limitée à `JARVIS_AGENT_ADD_DIRS`, sans les jetons de Jarvis ;
- suivi dans **Agents** (journal, compte rendu, PR, bouton « Arrêter ») ;
- **questions** : un agent qui finit son compte rendu par des questions passe en « question en attente » ; « Répondre et relancer » reprend sa session Claude Code avec ta réponse ;
- **relecture guidée** sur la page d'un agent terminé : fichiers changés, tests lancés et leur résultat, appels d'outils refusés, contrôles (pas de PR, pas de test, étape commune touchée, fichiers non commités) ;
- **coût et durée** par agent et par type de ticket, en bas de la page Agents.

Prérequis : Claude Code et `gh` connectés, et le dossier `.claude` de l'orchestrator à ton utilisateur (sinon : `sudo chown -R $USER:$USER <ORCHESTRATOR_DIR>/.claude`, voir [`docs/vm.md`](vm.md)). Les worktrees restent après la PR : `git worktree remove <chemin>`.

## Suivre les PR (GitHub)

L'onglet **PR** d'une fiche liste les PR ouvertes de l'orchestrator qui concernent le client, avec CI, relecture, jours depuis l'ouverture et jours sans mouvement.
- **Sans IA et sans jeton** : Jarvis lance la commande `gh` dans le clone de l'orchestrator (`ORCHESTRATOR_DIR`), avec la session `gh auth login` de la machine. Prérequis : `gh` installé et connecté, sur le Mac comme sur la VM.
- **En deux temps** : `gh pr list` pour toutes les PR ouvertes, puis `gh pr view` (CI et relecture) pour les seules PR du client. Tout lire d'un coup prenait 18 s sur 165 PR.
- **Rattachement au client** : scope du titre (`[FIX](client) …`), PR ouverte par un de ses agents, ou scope égal à son logiciel.
- **Lecture seule**, réponses gardées 5 minutes, bouton « Actualiser ». Code : [`sources/pulls.py`](../src/deploy_hub/sources/pulls.py).


## Notifications

agent terminé, changements urgents, échéances, avancement qui se dégrade, rappels arrivés à échéance. Une fois « Activer les notifications du Mac » cliqué (**Réglages → Notifications du Mac**), elles arrivent sur le Mac même onglet fermé (Web Push, [`sources/web_push.py`](../src/deploy_hub/sources/web_push.py)) : le navigateur doit rester lancé ; le contenu part chiffré de bout en bout vers le service de push du navigateur ; clé et abonnements dans `data/push/`.

## Perso

Tout est dans `data/perso.json`, sur ta machine ; rien n'est écrit dans Notion.
- **Brief du matin** : bouton « Brief » de l'accueil, texte prêt à coller (retards, échéances, injections KO, jalons, rappels).
- **Épingles** : bouton « Épingler » d'un ticket ; il remonte sur l'accueil et sur la synthèse de la fiche de son client.
- **Notes** : bouton rond « Notes » en bas à droite (touche `n`) : une note par client, la même que « Mes notes » de sa fiche, et une note générale.
- **Rappels** : sur un ticket ou libres (demain, lundi, dans une semaine, une date) ; ils reviennent en notification et dans le brief.


## Flow Digger

Le bouton **Flow Digger ↗** de la barre ouvre [integration-flow-digger](https://github.com/padoa/integration-flow-digger) dans sa propre fenêtre, et le démarre d'abord sur la VM s'il ne tourne pas (`uv run main.py --start --no-browser`). À chaque clic, Jarvis regarde si sa branche a des commits que le dossier n'a pas (`git fetch`) : arrêté, il ne le met à jour (avance rapide, jamais de fusion) que s'il y en a ; déjà lancé, il propose « Mettre à jour et relancer » (seulement un Flow Digger qu'il a lui-même lancé) ou « Ouvrir la version actuelle ». Son installation (outils, 1Password, Google Drive, configs client) se fait une fois, selon son README.

## InteCG

InteCG est le jeu de cartes à collectionner de l'équipe, dans son propre dépôt (`intecg`). Jarvis s'y relie sans rien à installer : il suffit de jouer à InteCG, où que le jeu tourne (ton Mac, la VM).
- **Une chance de booster à chaque lancement** : un DAG ou un agent lancé dans Jarvis a une chance de rapporter un booster bonus. Jarvis l'écrit lui-même dans la base du jeu (des tables CSV versionnées dans son dépôt, où chaque joueur pousse ses lignes), au nom du compte GitHub de sa machine : le jeu n'a pas à tourner, et le booster y apparaît à sa synchro suivante. Il garde pour ça son propre clone du jeu (`data/intecg/depot`, cloné au premier lancement).
- **Les règles sont celles du jeu** : la chance et le maximum du jour sont lus dans le dépôt du jeu (`JARVIS_DROP_CHANCE`, `JARVIS_DAILY_MAX`) ; un lancement n'est tiré qu'une fois, toujours avec le même résultat. Seul un joueur déjà inscrit dans le jeu est crédité.
- **Booster gagné** : le sachet du jeu tombe au milieu de l'écran, avec un bouton pour ouvrir le jeu, et une notification reste dans la cloche.
- **Lien « InteCG ↗ » en pied de page**, seulement si le dépôt du jeu est aussi sur la machine de Jarvis (par exemple `~/Dev/Apps/intecg`) : il ouvre le jeu dans sa propre fenêtre. S'il ne tourne pas, Jarvis met d'abord ce dépôt à jour (avance rapide seulement), installe les paquets npm s'ils manquent ou ont changé, puis le démarre (`npm run dev`).

Prérequis : `gh` connecté sur la machine de Jarvis, avec un accès en écriture au dépôt du jeu (celui qu'il faut déjà pour y jouer). Dépôt injoignable : le lancement se fait quand même, sans booster, et Jarvis réessaie au plus tôt une heure après. `INTECG_REPO=aucun` coupe les boosters. Code : [`sources/intecg.py`](../src/deploy_hub/sources/intecg.py), [`static/booster.js`](../src/deploy_hub/static/booster.js).


## Mises à jour

rien à faire à la main. Toutes les dix minutes, Jarvis regarde sur GitHub (`git fetch`) si sa branche a du code qu'il n'a pas : bandeau « Nouvelle version de Jarvis disponible » en haut de chaque page, et notification. Son bouton **Mettre à jour et redémarrer** fait lui-même le `git pull` (avance rapide seulement, jamais de fusion), synchronise les dépendances (`uv sync`) puis redémarre Jarvis, dans le même terminal (ou le même service), et la page se recharge sur la nouvelle version. C'est refusé tant qu'un agent Claude, une synchro Notion ou la lecture de l'annuaire tourne, puisque le redémarrage les couperait. Seul cas à part : si le dossier a été mis à jour en dehors de Jarvis (`git pull` dans un terminal, sans relancer), le bandeau dit « Jarvis est à relancer » et son bouton **Redémarrer** suffit.

## Données et confidentialité

- `data/` et `config/` sont hors de git : rien de ce qui vient de Notion n'est versionné.
- La synchro de la recette n'accepte qu'une liste fixe de colonnes ; le contenu des retours est rangé à part (`data/notion/<client>/contenu-recette.json`).
- Le brief d'agent interdit toute donnée client dans le code, les tests ou la PR.


## Limites connues

- Demandes BAU : la description n'est pas affichée dans la liste ni cherchable, et ne se lit qu'avec Claude Code en ligne de commande ; pas de synchro par jeton Notion ; elles ne sont pas dans la recherche ⌘K, la commande `>agent` ni « Quoi de neuf ».
- Après un changement du clone orchestrator, la liste des DAGs est à jour au plus une minute plus tard (ou tout de suite avec « Régénérer ») ; une page Injections déjà ouverte ne se recharge pas seule.
- Les runs lancés par `/air` n'apparaissent pas dans **Agents → DAG** (leurs rapports sont dans `data/airflow/`).
- Un changement fait seulement sur une carte Bible n'est vu qu'à la synchro complète suivante (7 jours au plus).
