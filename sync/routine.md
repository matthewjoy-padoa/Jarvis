<!-- Routine de synchro Notion → Jarvis : source unique de la consigne.
     Utilisée par la commande /jarvis-sync (.claude/commands/jarvis-sync.md), par le bouton « Rafraîchir depuis Notion »
     quand l'app n'a pas de jeton Notion (claude -p, voir src/deploy_hub/sources/sync_routine.py) et par la tâche planifiée.
     Les clients synchronisés sont ceux configurés dans l'app (config/clients/, ou JARVIS_CONFIG_DIR) : « preparer » les lit. -->

Tu mets à jour l'app Jarvis à partir de Notion. Tu travailles dans le dossier de l'app (le dossier courant, qui contient sync/routine.md). Réponds en français. Va vite : Python décide de tout, tu exécutes.

RÈGLES
- Tu n'exécutes que les requêtes listées par les commandes ci-dessous (fichiers .sql recopiés à l'identique), avec pour seuls ajouts un « LIMIT 100 OFFSET n » final pour paginer et la valeur de « ? » dans `params`.
- Les résultats de la base Retours Recette contiennent le contenu des retours : écris-les tels quels dans les fichiers indiqués, ne les recopie jamais dans ton compte rendu ni sur Slack.
- Tu ne lis jamais les logs Airflow. Tu n'écris que dans les fichiers « sortie » donnés par les commandes (dossier de données de l'app).
- Tu ne modifies jamais le code, les tests, les modèles .sql ni la configuration. Si une commande échoue, tu t'arrêtes pour ce client, tu signales l'échec et tu décris le défaut en une phrase.
- Aucune ligne de résultat dans ton compte rendu : seulement des comptes.

EXÉCUTER UNE REQUÊTE (objet {"sql", "sortie", "params", "sources"})
- Outil Notion `notion-query-data-sources`, mode sql : contenu du fichier "sql" à l'identique, `data_source_urls` = "sources", `params` = "params" s'il n'est pas vide. Erreur 5xx ou délai dépassé : réessaie jusqu'à 2 fois.
- Pagination : si la réponse a 100 lignes, relance la requête suivie de « LIMIT 100 OFFSET 100 », puis 200, etc., jusqu'à une page de MOINS de 100 lignes (écris-la aussi, même vide). Au plus 20 pages.
- Écrire : page 1 dans "sortie", page 2 dans le même nom suffixé -2 (ex. actions_delta-2.json), etc. Si l'outil a rangé sa réponse dans un fichier (« Output has been saved to <chemin> »), ne lis pas ce fichier et ne le copie pas : lance `uv run python -m deploy_hub.sync_notion deposer "<sortie>"`, qui le retrouve, le vérifie et le dépose (affiche le nombre de lignes). S'il répond « Aucune réponse Notion rangée », relance-le avec le chemin en second argument. Sinon, écris l'objet JSON renvoyé (celui qui contient "results") tel quel, sans rien ajouter ni retirer.

ÉTAPES
1. uv run python -m deploy_hub.sync_notion preparer
   → une requête : exécute-la (empreinte de tous les clients, une ligne par client).
2. uv run python -m deploy_hub.sync_notion plan
   → {"requetes": [...], "inchanges": [...], "raisons": {...}, "erreurs": {...}}. Si "requetes" est vide : va à l'étape 4.
3. Exécute chaque requête de "requetes", dans l'ordre (chacune porte son "client").
4. uv run python -m deploy_hub.sync_notion fusionner
   → un résumé par client fusionné, ou {"erreur": …}.
5. uv run python -m deploy_hub.sync_notion utilisateurs
   → identifiants de responsables sans nom (souvent aucun). Pour chacun, outil Notion `notion-get-users` avec `user_id` ; garde seulement son nom. Puis une seule fois : uv run python -m deploy_hub.sync_notion utilisateurs '{"<identifiant>": "<nom>", ...}'. Ni e-mail ni autre information.

SI UNE ÉTAPE ÉCHOUE
Écris « ÉCHEC <client ou tous> : <étape> : <raison en une phrase> » dans ton compte rendu. Si on t'a donné un canal Slack, envoie aussi ce message court dans ce canal. Rien sur Slack en cas de succès.

FIN
Une ligne par client : « inchangé », le mode appliqué par base (d'après le résumé de fusionner) et le nombre de requêtes exécutées, ou la raison de l'échec.
