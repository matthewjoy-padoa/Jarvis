# Historique des versions

Jarvis suit une numérotation `0.MINEUR.CORRECTIF` tant qu'il n'est pas stable :
- **mineur** (0.2.0) : une nouvelle fonctionnalité ou un changement visible de comportement ;
- **correctif** (0.1.1) : uniquement des corrections.

À chaque PR, ajouter une ligne sous « Non publié ». Pour publier : renommer cette section en `## [0.x.y] · AAAA-MM-JJ`, reporter le numéro dans `pyproject.toml` (`version`), fusionner, puis poser le tag `v0.x.y` sur `main`. Le numéro s'affiche en bas de chaque page.

## Non publié

## [0.1.0] · 2026-11-17

Première version suivie : tout ce qui a été fusionné jusqu'à la PR #46.

### Suivi des clients
- Fiches de déploiement par client, accueil, synthèse (« Où ça coince »), Deadlines en liste et en calendrier (#1, #10, #11, #27).
- Synchro Notion des retours de recette et des actions, filtres, vue kanban, détail d'un ticket (#3, #5, #6, #41).
- Fiche client : icône, liens Google Drive, dock de raccourcis, tickets épinglés (#16, #28 à #31, #36, #38).
- Graphiques lisibles et cliquables (#25, #39).

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

### Corrections
- Agents : un lien de PR injecté par les hooks n'est plus pris pour la PR de l'agent (#46).
