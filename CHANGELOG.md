# Historique des versions

Jarvis est numéroté `0.X.Y` et ne passe jamais en v1 : le premier chiffre reste 0. Chaque PR monte le numéro selon l'ampleur du changement, sauf une correction ou un détail :
- **changement majeur** → chiffre du milieu, le dernier revient à 0 (0.2.4 → 0.3.0) : une nouvelle fonctionnalité, une nouvelle page, un changement visible de comportement ;
- **changement mineur** → dernier chiffre (0.3.0 → 0.3.1) : une amélioration ou un ajout dans une fonctionnalité existante ;
- **correction ou changement trop petit** (un bug corrigé, un texte, un style, un détail) → **pas de nouveau numéro** : la ligne de la PR s'ajoute à la section de la version en cours, en tête de cet historique.

Pour une PR qui monte le numéro :
1. `version` dans `pyproject.toml`, puis `uv lock` (le numéro est aussi dans `uv.lock`) ;
2. une section `## [0.x.y] · AAAA-MM-JJ` en tête de cet historique, avec la ligne de la PR.

La version affichée en bas de chaque page change donc avec chaque fonctionnalité ; après une simple correction, le bandeau « Jarvis est à relancer » compare les commits. Deux PR ouvertes en même temps : la seconde fusionnée prend le numéro suivant (conflit à résoudre dans ces trois fichiers). Un test vérifie que les trois numéros concordent et que le premier chiffre reste 0 (`tests/test_versions.py`). Le tag `v0.x.y` sur `main` reste facultatif.

## [0.8.5] · 2026-11-27

- README allégé : ce qu'est Jarvis, installer, premiers pas, mises à jour, où lire la suite. Le détail de chaque fonction passe dans [`docs/guide.md`](docs/guide.md), les réglages et les secrets dans [`docs/reglages.md`](docs/reglages.md), sans rien retirer.
- Animations réglables à part, dans les deux vues : Réglages → Affichage → Animations (« Activées » ou « Désactivées »), pour ce navigateur. Désactivées, plus rien ne bouge ni ne glisse (logo, survols, ouvertures, booster InteCG), et la page Agents passe en compteurs et tableau, sans armures, avec les agents en attente de réponse listés en tête, quelle que soit la vue ; restent l'indicateur d'attente et l'avancement d'une synchro. La vue auparavant dite « sobre » ne coupe donc plus les animations d'elle-même : elle les retrouve, booster InteCG compris. Les deux vues changent de nom : « Originale » (ex-« animé ») et « Classique » (ex-« sobre ») ; le lien du pied de page devient « Vue classique » / « Vue originale ».

## [0.8.4] · 2026-11-27

- Affichage sobre, pour qui préfère un outil de travail plus neutre : à choisir dans Réglages → Affichage ou par le lien en pied de page, pour ce navigateur seulement ; l'affichage animé reste celui par défaut. Même menu en haut, même dock des liens et mêmes boutons Notes et Console, mais tout est plus carré : gris neutres et un seul bleu, en clair comme en nuit ; angles peu arrondis, traits fins, pas d'ombre, rien qui bouge (logo fixe, environnements Padoa en liste et non en éventail, libellés Notes et Console toujours écrits) ; onglets soulignés, compteurs en bande, tableaux plus denses. Accueil en « Tableau de bord », sans salutation. Page Agents en compteurs et tableau, sans schéma ni armures (un agent s'archive depuis sa page) ; les agents qui attendent une réponse sont listés en tête, avec leur question et un bouton « Répondre » qui arrive sur le formulaire. Pas de thème Noël, ni au bouton de thème, ni imposé en décembre. Les informations et les fonctions sont les mêmes.
## [0.8.3] · 2026-11-27

- InteCG, le jeu de cartes de l'équipe (dépôt `intecg`, à part), relié à Jarvis dès que son dépôt est sur la machine (`~/Dev/Apps/intecg`, ou `INTECG_DIR`). Lien « InteCG ↗ » en pied de page : met le dépôt du jeu à jour, installe ses paquets au besoin, le démarre s'il ne tourne pas et l'ouvre dans sa propre fenêtre. Un DAG ou un agent lancé dans Jarvis demande un booster bonus au jeu (`POST /api/credits/jarvis`), en tâche de fond : c'est InteCG qui décide, par tirage au sort à chaque lancement (chance faible, réglée dans le jeu), sans compter un lancement deux fois et avec un maximum par jour. Booster gagné : un sachet tombe au milieu de l'écran avec un bouton « Ouvrir InteCG », et une notification reste dans la cloche. Jeu arrêté ou injoignable : le lancement se fait quand même, sans booster. Réglages `INTECG_DIR` et `INTECG_URL` (**Réglages → Dossiers et liens**).

## [0.8.2] · 2026-11-24

- Mises à jour depuis l'app, plus de `git pull` à la main : toutes les dix minutes, Jarvis regarde sur GitHub (`git fetch`) si une nouvelle version est publiée ; bandeau « Nouvelle version de Jarvis disponible » sur toutes les pages, et notification. Le bouton **Mettre à jour et redémarrer** fait lui-même le `git pull` (avance rapide), `uv sync`, puis relance Jarvis (même terminal, même service) ; la page se recharge sur la nouvelle version. Refusé tant qu'un agent, une synchro Notion ou la lecture de l'annuaire tourne.
- Pour passer à cette version-ci, une dernière fois à la main : `git pull`, puis relancer le serveur (le Jarvis d'avant n'a pas encore le bouton). Les versions suivantes passent par le bouton.

## [0.8.1] · 2026-11-24

- Agents : toute la carte d'un agent l'ouvre. Ligne du tableau de la page Agents et des archives, ligne d'un agent sur la page d'un retour : un clic n'importe où (hors autre lien ou bouton) ouvre l'agent, Cmd/Ctrl-clic dans un nouvel onglet. Sur le schéma, la bulle de détail ne disparaît plus quand la souris la rejoint et s'ouvre au clic.
## [0.8.0] · 2026-11-24

- Page BAU sans réglage : « BAU » est toujours dans la barre du haut et suit « BAU équipe intégration - TECHNIQUE », la même page pour toute l'équipe ; `JARVIS_NOTION_BAU` ne sert plus qu'à suivre une autre page. Tout passage complet de la synchro (« Tout rafraîchir », tâche planifiée) relit donc aussi les demandes BAU.
- Page « BAU » : Jarvis suit aussi les demandes d'une page projet ou BAU de Notion (base « Equipes OPS - Actions internes »), pour l'équipe qui les traite. Le lien de la page se colle dans Réglages (`JARVIS_NOTION_BAU`) ; les demandes s'affichent en liste ou en kanban, par priorité puis échéance, avec filtres (statut, priorité, responsable, référent inté) et panneau de détail, et l'accueil dit combien sont ouvertes et en retard. Elles suivent la synchro existante (routine Claude Code, lecture seule) : relues avec les clients par « Tout rafraîchir », seules par le bouton de la page. Aucun client n'est nécessaire pour s'en servir.
- Demande BAU : sa description (corps de la page Notion) est affichée dans le panneau et reprise dans le brief de l'agent. La synchro charge d'abord les demandes ; les descriptions se lisent ensuite en fond (lots de 15 enchaînés), puis seulement celles des demandes modifiées ; une demande pas encore à jour la fait lire à son ouverture.
- Page BAU : bouton « Synchro auto », comme sur la fiche d'un client : les demandes se relisent toutes seules, seulement ce qui a changé.
- Page BAU : liens de l'équipe (Drive, pages Notion, autres adresses) à ajouter soi-même, affichés sous le titre et dans les raccourcis à droite, comme ceux d'un client.
- Page BAU : une vue par squad (« Squad SPSTA », « Squad Plateform »), lue telle quelle dans les vues « Action squad » de Notion à chaque synchro.
- Demandes BAU traitées comme des retours de recette : « Lancer un agent » sur une demande ouverte (brief pré-rempli, à compléter du détail de la demande, que Jarvis ne lit pas dans Notion), suivi dans Agents et dans le panneau de la demande, épingle et rappels.

## [0.7.2] · 2026-11-24

- Page Agents : attraper un agent le fait s'envoler comme une armure d'Iron Man. Il garde son armure et ses couleurs, réacteurs allumés et halo qui pulse, suit la souris (ou le doigt) avec un léger retard, se penche dans le sens du vol et laisse une traînée de fumée. Lâché ailleurs dans sa colonne, il atterrit à la nouvelle place (ses voisins glissent pour lui faire place, ordre gardé) ; lâché sur « Archiver », qui s'éveille pendant le vol, il plonge dedans en vrille ; refusé (agent en cours ou en attente) ou lâché ailleurs, il rentre se poser chez lui et la zone secoue la tête. Échap (ou quitter la fenêtre) le renvoie chez lui ; un nouveau vol peut partir pendant qu'un autre atterrit. Un simple clic ouvre toujours l'agent ; rien ne bouge si le système demande moins d'animations.

## [0.7.1] · 2026-11-24

- Agents : on peut renommer un agent (son sujet « retour 1B7052 » reste en dessous et sert toujours de lien au retour) et l'épingler. Un agent épinglé passe en tête de sa colonne, porte une épingle sur le schéma et n'est jamais archivé par le rangement automatique. Nom et épingle se règlent depuis la page de l'agent, aussi pour un agent archivé.

## [0.7.0] · 2026-11-24

- Agents : le suivi ne grandit plus sans fin. Un rangement automatique, une fois par jour, archive les agents finis depuis plus de 30 jours (leur worktree est retiré s'il est propre, la branche reste) et efface après 90 jours le journal détaillé (la fiche garde compte rendu, coût, tokens et PR). Seuils dans Réglages (JARVIS_AGENT_ARCHIVE_DAYS, JARVIS_AGENT_LOG_DAYS). Un agent qui travaille ou attend ta réponse n'est jamais archivé.
- Page Agents : glisser un agent dans sa colonne change sa place (ordre gardé) ; le déposer sur « Archiver » le range. Bouton « Archiver » sur la page d'un agent fini, page « Archivés » d'où l'on peut le ressortir ; le bilan des tokens compte aussi les archivés.

## [0.6.0] · 2026-11-24

- Agents : répondre aux questions d'un agent ou le relancer après une erreur fait repartir la même fiche, au lieu d'en créer une nouvelle qui laissait la première « en attente ». La fiche garde son numéro, son journal (un repère « réponse » ou « relance » marque chaque reprise), et additionne tours, coût et tokens ; sa durée compte le travail, pas l'attente de ta réponse. Les passages précédents (état, durée, compte rendu, ta réponse) se déplient sur la page de l'agent. Les anciennes fiches (une par réponse) restent lisibles telles quelles.

## [0.5.7] · 2026-11-23

- Thèmes « Noël » : deux choix de plus au bouton de thème (Auto → Nuit → Clair → Noël nuit → Noël clair) et dans la commande « Changer de thème » de la palette. Nuit de sapin à l'accent or, ou matin de neige à l'accent sapin ; statuts gardés lisibles dans les deux (KO en rouge). Neige derrière le contenu, guirlande qui scintille sous la barre, bonnet sur le logo, « Joyeuses fêtes » en pied de page. Décor sans animation si le Mac demande moins de mouvement. À partir du 1er décembre 2026, Noël s'impose une fois à chaque navigateur, quel que soit le thème choisi avant (nuit si on était en Nuit, clair si on était en Clair, selon le Mac en Auto) ; on peut ensuite l'enlever au bouton : tout choix est retenu, Auto compris, et Noël ne revient pas.

## [0.5.6] · 2026-11-23

- Règle de version : une correction ou un changement trop petit ne monte plus le numéro ; sa ligne s'ajoute à la version en cours.
- Flow Digger : à chaque clic, Jarvis regarde s'il y a des commits à récupérer (`git fetch`). Arrêté, il ne met à jour que s'il y a du nouveau (des modifications locales ne bloquent plus le lancement quand rien n'a changé) ; déjà lancé mais en retard, il propose « Mettre à jour et relancer » ou « Ouvrir la version actuelle » au lieu d'ouvrir une vieille version sans rien dire. Il ne coupe jamais un Flow Digger qu'il n'a pas lancé lui-même.
- Page Agents : les armures du schéma portent une tête en photo, détourée, et leur casque en suit le contour dans la couleur de l'état ; en échec, la tête est grise.
- Page Agents : une seconde tête pour les armures. Chaque agent tire la sienne au hasard à partir de son numéro et la garde ; chaque tête a son casque.

## [0.5.5] · 2026-11-23

- Agents sur un retour de recette : la branche dit le sujet au lieu du numéro du retour (`fix-presoa-ced-ged-factures-multi-version` plutôt que `fix-presoa-23aa58`) : client, ressource ou type de données, premiers mots de la ligne du cahier de recette, chiffres retirés. Une reprise ou une relance garde la branche déjà lancée ; le numéro ne s'ajoute que si le retour n'a ni ligne ni type, ou qu'une autre branche d'agent porte déjà ce nom.

## [0.5.4] · 2026-11-23

- Fiche d'un client : plus de rangée de liens en haut (Notion, stacks, Drive, « + Stacks », « + Drive »), en doublon avec le dock à droite de l'écran ; le dock reprend aussi les autres dashboards Notion du client et ses autres liens, qui n'étaient que dans cette rangée. Les liens Google Drive (drive.google.com, docs.google.com) y ont le logo Google Drive, les liens Notion le logo Notion.

## [0.5.3] · 2026-11-23

- Action qui n'a pas abouti (Jarvis injoignable ou en erreur) annoncée partout au lieu d'échouer en silence : note tapée, rappel, épingle, tâche, « Tout marquer comme lu », lancements, agents, Rafraîchir, tests des Réglages. Le message « Non pris en compte : … » s'affiche dans la zone de résultat de l'action, sinon juste après le bouton ou le formulaire, et disparaît au prochain essai (`static/errors.js`). Notes et to-do lists : un seul état d'enregistrement par zone, discret (« Enregistré » en gris qui s'efface ; « Non enregistré » en rouge, la raison au survol), dans la barre du panneau Notes et en bas du bloc « Mes notes » de la fiche, au lieu d'un message sous chaque note.

## [0.5.2] · 2026-11-23

- To-do list dans les notes : sous les notes d'un client (bloc-notes flottant et « Mes notes » de sa fiche, avec le nombre de tâches à faire dans son titre replié) et sous les notes générales ; ajouter, cocher (la tâche faite passe en bas, barrée), supprimer. Gardée dans `data/perso.json` avec les notes, jamais envoyée dans Notion.

## [0.5.1] · 2026-11-23

- Questions d'un agent : l'intitulé ne montre plus le Markdown du compte rendu (« ## 3. Blocage n°2 : quel niveau de correctif ? » s'affiche « Blocage n°2 : quel niveau de correctif ? »). Une question posée en titre emporte sa section comme détail, au lieu de la laisser dans le contexte.

## [0.5.0] · 2026-11-23

- Agents → DAG : une carte par run lancé depuis Jarvis, à la place des trois tableaux (suivis, enchaînements, lancements) qui racontaient le même run trois fois. Les cartes sont rangées par état (en cours, en échec ou arrêtés, terminés, simulations) ; chaque carte donne le client, le DAG en clair (« administratif · ETL groupe »), la stack, l'état et la durée, la dernière nouvelle du suivi, le nombre de relances automatiques et le lien vers le journal ou le rapport de fin. Un enchaînement tient sur une seule carte, avec ses étapes numérotées. Recherche et filtres par client, stack et état.

## [0.4.0] · 2026-11-20

- Onglet Injections : chaque ligne (ressource ou groupe) montre ses derniers DAG lancés depuis Jarvis. Sur la ligne, une pastille donne le dernier (stack et état) ; en ouvrant la ligne, le dernier avec sa date, sa stack, son état, sa durée et la dernière nouvelle d'`air.py`, puis « Voir plus » ajoute les précédents trois par trois. Chacun mène à la page de son suivi (journal, rapport de fin).

## [0.3.0] · 2026-11-20

- Onglet Injections, vue par ressource : une liste simple à la place des blocs à déplier. Une ligne par ressource, avec un point de couleur, l'état de sa carte la plus en retard et le nom de la carte en KO ; ce qui bloque est en haut. La stack se choisit une fois en haut (gardée dans le navigateur, sauf la prod), puis « Lancer » en face de la ressource lance le DAG affiché dans la liste à côté (« ETL » par défaut, « Transform & load » ou le groupe au choix) ; un clic sur la ligne montre ses cartes. Sous les ressources, une ligne par groupe de ressources du catalogue, avec son bouton « Lancer » (l'ETL du groupe) ; une carte liée à plusieurs ressources apparaît sous chacune d'elles, et un groupe ne se lance que depuis sa ligne. Le graphique de répartition reste dans la vue Liste et sur la page de tous les clients.

## [0.2.0] · 2026-11-20

- Fichiers de Jarvis (notifications, annuaire, états et caches des synchros, fiches, configuration des clients, `.env`, agents, suivis et enchaînements de DAG, perso, Web Push) écrits d'un bloc par une seule fonction (`safe_write`) : fichier temporaire forcé sur le disque puis remplacement, un arrêt brutal (Jarvis tué, VM coupée) laisse l'ancien fichier intact au lieu d'un fichier à moitié écrit relu comme vide ; fichiers en 600.
- Onglet PR : historique des PR fusionnées ou fermées du client (scope du titre, ou ouvertes par un de ses agents), et en options d'affichage les PR fusionnées depuis la réception de ses données de son logiciel et celles communes à tous les clients (scope `all`). Historique gardé sur disque (`data/github/`, métadonnées GitHub), affiché aussitôt et complété en arrière-plan par les seules PR fermées depuis la dernière lecture ; chargé en même temps que les PR ouvertes.
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
- Emoji des clients sans jeton Notion : une petite lecture à part, sur Haiku (une dizaine de secondes, l'annuaire n'en est pas ralenti) prend l'icône du dashboard intégration de chaque client suivi qui n'en a pas encore (emoji ou image) ; elle s'affiche aussitôt et la synchro la reporte dans la fiche. Un client ajouté a la sienne dans la minute.
- Archiver un client quand on ne travaille plus dessus (sa carte de la page Clients, ou Configurer) : il sort du menu, de l'accueil, de la recherche, des notifications et de la synchro ; sa fiche reste consultable dans « Archivés », en bas de la page Clients ; « Désarchiver » le remet dans le suivi et relance sa synchro. Rien ne bouge sur le disque : clé `archived` de son yaml.
- Fiche d'un client : bouton « Vue simplifiée » à droite des onglets, qui ne garde que Injections, Recette et PR ; la roue à côté choisit les onglets gardés. Le choix vaut pour tous les clients, dans ce navigateur ; la fiche s'ouvre alors sur le premier onglet gardé, et un onglet masqué reste joignable par la recherche et les liens.
- Filtres mémorisés : la recherche, les filtres et le tri d'une liste (retours, actions, injections, règles, agents…) sont gardés dans le navigateur, page par page, et remis au retour sur la page, tout comme la recherche et l'état choisi (Avec KO, En cours…) de la vue « Par ressource » des injections ; la liste et le kanban d'un même onglet les partagent. Le filtre « Client » continue de suivre le client du menu, un lien venant d'un graphique l'emporte, « Effacer les filtres » vide la mémoire.
- Choix de vue mémorisés : Ouverts / Tous et Liste / Kanban des retours, Actives / Toutes et Tableau / Kanban des actions, vue des injections (Par ressource, Liste, CED), période et Liste / Calendrier des deadlines reviennent tels qu'on les a laissés, pour tous les clients, dans ce navigateur. Un lien direct qui porte un choix de vue (notification, graphique, compteur de l'accueil, bloc de ressource) s'affiche comme il le demande, sans changer le choix gardé ; le graphique des injections de la synthèse ouvre la liste, seule vue qui sait filtrer par groupe.
- Synchro Notion sans jeton beaucoup plus rapide : le bouton d'une fiche ne relit que ce client ; Python récolte lui-même les réponses de Notion dans la session Claude Code (le modèle ne relit plus les requêtes, ne recopie plus les réponses) et refuse une requête modifiée ; modèle rapide (`haiku`, réglages `JARVIS_SYNC_MODEL` et `JARVIS_SYNC_EFFORT`). Sur la VM : petit delta d'un client en 20 à 24 s et rien de neuf en 16 s (109 s avant), lecture complète en 108 s (4 min 02 avant). Les réponses sont rangées à l'octet près : la recopie par le modèle remplaçait par exemple une espace insécable par une espace.
- Synchro Notion automatique, client par client : bouton « Synchro auto » sur la fiche (non par défaut). Jarvis relit Notion tout seul pour les clients qui l'ont activée, dès que l'un d'eux n'a pas été vérifié depuis une heure (réglage `JARVIS_SYNC_EVERY_MIN`, 0 : jamais) : une synchro faite à la main compte, un redémarrage ne remet rien à zéro ; un échec n'est signalé qu'une fois par jour. Bouton « Rafraîchir depuis Notion » : l'avancement monte à chaque réponse de Notion, au lieu de rester à 20 % puis de passer à « terminé ».
- Connexion de Claude Code surveillée : au lancement de Jarvis (ligne « Jarvis · Claude Code » dans son terminal, comme Airflow et Notion), puis toutes les heures. Déconnecté, ou connexion qui expire dans les trois jours : avertissement dans le terminal, notification dans l'app (une par jour) et ligne « Claude Code » sur la page État des systèmes, avec la commande à lancer (`claude auth login`).
- Fiche d'un client en synchro automatique : l'heure prévue de la prochaine synchro (« · prochaine vers 14:15 ») à la suite de « Dernière synchro » ; le lancement et l'affichage partent du même calcul.
- Bandeau « Jarvis est à relancer » sur toutes les pages, avec une notification, quand le dossier a été mis à jour (`git pull`) et que le serveur tourne encore sur l'ancien code, en numéros de version (« la version v0.3.0 est installée, mais c'est encore la v0.2.0 qui tourne ») ; le commit seulement si le numéro n'a pas bougé.
- Version en bas de page lue dans `pyproject.toml` (et non plus dans le paquet installé, figé jusqu'au `uv sync`), montée à chaque PR : elle change à chaque fusion.

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
