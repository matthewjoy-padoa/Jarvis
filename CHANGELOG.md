# Historique des versions

Jarvis suit une numérotation `0.MINEUR.CORRECTIF` tant qu'il n'est pas stable :
- **mineur** (0.2.0) : une nouvelle fonctionnalité ou un changement visible de comportement ;
- **correctif** (0.1.1) : uniquement des corrections.

À chaque PR, ajouter une ligne sous « Non publié ». Pour publier : renommer cette section en `## [0.x.y] · AAAA-MM-JJ`, reporter le numéro dans `pyproject.toml` (`version`), fusionner, puis poser le tag `v0.x.y` sur `main`. Le numéro s'affiche en bas de chaque page.

## Non publié

- Plus de mode démo : `JARVIS_MODE` disparaît ; agents Claude et synchro Notion toujours réels, DAG lancés seulement avec Airflow configuré ; ce qui manque est dit (Claude Code, orchestrator, Airflow) au lieu d'être simulé ; plus d'agents fictifs sur la page Agents (#51).
- Bouton « Rafraîchir depuis Notion » : la synchro peut enfin écrire ses résultats dans le dossier de données (règle `Edit(…)` au lieu de `Write(…)`, que Claude Code ignore), au lieu de s'arrêter sur « Résultat manquant » (#53).
- Un client ajouté apparaît tout de suite dans le sélecteur, avec une fiche vide et le bouton « Rafraîchir depuis Notion » en attendant sa première synchro (#52).
- Synchro Notion : cercle d'avancement sous le bouton (empreinte, plan, requêtes n / N, fusion) ; le bouton ne bouge plus au clic (#53).
- Dernière synchro d'un client (« aujourd'hui à 14:04 ») à côté du bouton et sur sa carte, juste même quand rien n'a changé ; l'alerte « synchro en retard » suit la même date (#54).
- Suivi des DAG : plus de « dérogation » ni de mention qui laisse croire à un réglage des logs Airflow ; comme `/air`, tout passe par l'API REST avec les trois variables du lancement (page Sources, README, docstrings).
- Skill `/air` dans le dépôt (`.claude/skills/air/`), adapté à Jarvis : réglages Airflow de l'app (`.env`, références 1Password comprises) via `python -m deploy_hub.air_jarvis`, état, rapports et journal dans `data/airflow/`.
- Bouton « Lancer » (Injections, CED, ressource de la fiche, console, enchaînements) : lancement par `air.py run`, comme `/air`. La confirmation des formulaires montre l'aperçu (ressources, run déjà en cours ; la commande `lancer` du terminal garde sa question) ; un DAG plus chargé par Airflow est refusé ; le message ne dit plus « DAG sorti de pause ». Tests contre un faux Airflow HTTP local que le vrai `air.py` appelle.
- DAGs proposés depuis une branche locale du clone orchestrator (par défaut sa branche courante, telle que commitée) : choix et « Régénérer » dans l'onglet Injections ; catalogue généré dans `data/dags/` par `git archive`, sans checkout ; repli sur le catalogue versionné sans clone.

## [0.1.0] · 2026-11-17

Première version suivie : tout ce qui a été fusionné jusqu'à la PR #50.

### Suivi des clients
- Fiches de déploiement par client, accueil, synthèse (« Où ça coince »), Deadlines en liste et en calendrier (#1, #10, #11, #27).
- Synchro Notion des retours de recette et des actions, filtres, vue kanban, détail d'un ticket (#3, #5, #6, #41).
- Fiche client : icône, liens Google Drive, dock de raccourcis, tickets épinglés (#16, #28 à #31, #36, #38).
- Graphiques lisibles et cliquables (#25, #39).
- Sans aucun client, bouton « + Ajouter un client » dans la barre du haut et sur l'accueil (#49).

### Synchro Notion
- Routine de synchro embarquée dans le dépôt (`sync/routine.md`) : commande `/jarvis-sync`, bouton « Rafraîchir depuis Notion » sans jeton (`claude -p`), tâche planifiée ; clients pris dans la configuration de l'app (#50).

### Airflow
- Lancement de DAGs, suivi automatique des runs, enchaînement de DAGs (#8, #33).

### Agents Claude
- Lancement d'un agent sur un retour de recette, dans un worktree dédié (#2).
- Coût, durée et relecture guidée ; suivi des PR GitHub par client (#34).
- Réglages des agents depuis l'app ; exemples fictifs en mode démo (#26, #43, #45).

### Perso et notifications
- Brief du matin, notes, épingles, rappels, bouton Notes flottant (#32, #35).
- Notifications du Mac par Web Push, activables dans les Réglages (#40, #42).

### Outils
- Palette ⌘K, console Jarvis et shell de la VM (#7, #12, #13).
- Bouton Flow Digger, mis à jour avant chaque démarrage (#18, #24).

### Réglages et sécurité
- Secrets dans 1Password, données jamais poussables, `.env` en 600 (#20).
- Connexions Airflow et Notion vérifiées au lancement ; page Réglages en parties (#22, #23, #44).

### Interface
- Logo, couleurs, audit du style et des contrastes (#4, #9, #14, #15, #17).
- Pages client plus légères sans rien retirer (contenu secondaire replié, filtres derrière un bouton), typo Inter ; accueil en rangées alignées, « À traiter » par période (#48).

### Corrections
- Agents : un lien de PR injecté par les hooks n'est plus pris pour la PR de l'agent (#46).
- Jauge d'avancement en retard sans pastille isolée ; séparateur en double dans les barres de la vue tous clients ; texte du bouton de lancement en prod lisible (#48).
