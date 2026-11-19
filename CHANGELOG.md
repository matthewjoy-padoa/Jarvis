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
- DAGs proposés depuis le clone orchestrator local, tel qu'il est sur le disque (branche extraite, modifications non commitées comprises, comme l'Airflow local) : catalogue dans `data/dags/`, régénéré quand le clone change et sur « Régénérer » (onglet Injections) ; repli sur le catalogue versionné sans clone.
- Page Agents : les petits robots du schéma deviennent des armures, une par état : rouge et or en vol (en cours), argent à l'arrêt (terminé), tout or, répulseur levé (PR ouverte), ferraille grise sur le dos (arrêté ou en échec).
- Page Agents : l'agent qui attend une réponse apparaît dans le schéma (il n'y était pas), parmi les « en cours » : posé au sol, armure bleu et argent, main levée, « ? » au-dessus de la tête.
- README relu et mis à jour : historique des DAGs gardé au redémarrage (la limite annoncée n'existait plus), page Réglages et connexion 1Password, rôle de `ORCHESTRATOR_DIR`, questions des agents, réglages du suivi des runs, adresse du dépôt.
- Heure française partout dans l'app, même sur un serveur en UTC comme la VM ; réglage `JARVIS_TZ` (#59).
- Agents Claude : refus clair avant lancement si le dossier `.claude` de l'orchestrator n'est pas à l'utilisateur (commande `chown` donnée) ou si `ORCHESTRATOR_DIR` ne pointe pas vers un clone de integration-orchestrator ; `ORCHESTRATOR_DIR` vide reprend la valeur par défaut ; page d'un agent sans erreur 500 (#64).
- Agents Claude : état « question en attente » quand l'agent s'arrête sur des questions, « Répondre et relancer » (reprise de sa session Claude dans le même worktree), et « Relancer l'agent » sur tout agent fini en erreur (#66).
- Agents Claude : une carte et un champ de réponse par question, réponses envoyées question par question (#69).
- Agents Claude : le bloc agent d'un retour dit avant le clic pourquoi l'agent ne partirait pas (bouton grisé, lien Réglages) et signale l'agent déjà lancé sur ce retour (#70).
- Filtres : plusieurs valeurs par filtre (cases à cocher), « a|b » dans une ancre ; Échap ou un clic ailleurs ferme le menu (#73).
- Agents Claude : « Ce que l'agent a constaté » au-dessus de ses questions, contexte, options et recommandation par question, texte de l'agent mis en forme (Markdown, HTML jamais interprété) (#74).
- Agents Claude : questions rendues en JSON par l'agent, options à cocher et recommandation pré-cochée : on répond en un clic (#75).
- Ajouter un client : choisi dans une liste, tout se remplit (identifiant comme dans l'orchestrator, « APST 26-07 » → `apst2607`, clés orchestrator, fiche « Clients en déploiement », dashboard intégration et autres dashboards). La liste est l'annuaire des clients, relu chaque jour par Claude Code (lecture seule) depuis la page Notion des dashboards ; réglage `JARVIS_NOTION_DASHBOARDS` ; autres dashboards dans les raccourcis du client (#76).
- Ajouter un client sans attendre : l'annuaire est relu en fond par Jarvis (à son démarrage, puis chaque jour) ; une relecture en cours ne bloque ni ne recharge la page, la liste affichée reste utilisable.
- Ajouter un client : liens complets pré-remplis, comme « Copier le lien » de Notion (`https://app.notion.com/p/padoa/<Titre>-<id>`) pour la fiche, le dashboard intégration et les autres dashboards ; l'annuaire déjà lu est relu aussitôt. Le dashboard d'un client enregistré sans « /p/ » s'ouvre de nouveau (réglage `JARVIS_NOTION_WORKSPACE`).
- Ajouter un client lance aussitôt sa synchro Notion (API avec un jeton, sinon la routine) et ouvre sa fiche, qui en montre l'avancement ; un client pas encore synchronisé s'ouvre depuis la page Clients.
- Un problème de réglage mène au réglage : dans les messages (Sources, onglet PR, lancement d'un DAG ou d'un agent, Tester les connexions, synchro), chaque réglage cité (`ORCHESTRATOR_DIR`, `AIRFLOW_USER`…) est un lien vers son champ dans Réglages, centré et encadré, et « 1Password » mène à sa connexion. L'onglet PR distingue enfin « clone introuvable » (`ORCHESTRATOR_DIR`) de « `gh` introuvable ».
- Onglet PR : toute la ligne d'une PR ouvre la PR dans GitHub (nouvel onglet), plus seulement le petit lien ; le numéro « #123 » s'affiche comme le reste de la ligne.
- Emoji des clients sans jeton Notion : une petite lecture à part (l'annuaire n'en est pas ralenti) prend l'icône du dashboard intégration de chaque client suivi qui n'en a pas encore (emoji ou image) ; elle s'affiche aussitôt et la synchro la reporte dans la fiche. Un client ajouté a la sienne dans la minute.

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
