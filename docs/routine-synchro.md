# Routine de synchro Notion → Jarvis

Jarvis lit Notion de deux façons :
- **le bouton « Rafraîchir depuis Notion »**, si un jeton d'intégration Notion est configuré (`NOTION_TOKEN`, voir le README) ;
- **cette routine**, une tâche planifiée de Claude Code qui exécute les requêtes SQL avec le connecteur Notion. Elle ne demande pas de jeton.

## Ce que fait un passage
1. **Une seule requête d'empreinte pour tous les clients** : nombre de lignes et dernière modification des actions et des retours, signature des règles et des dates de la fiche.
2. **Python fait le plan** (`sync_notion plan`) : seulement ce qui a changé, base par base.
   - Lignes modifiées ou ajoutées : delta depuis la dernière modification connue du cache.
   - Lignes supprimées : liste compacte des identifiants (une ligne).
   - Colonne de métadonnées ajoutée à la recette : métadonnées seules, les textes restent ceux du cache.
   - Relecture complète : pas de cache, colonnes des actions modifiées, ou actions relues il y a 7 jours ou plus (leurs ressources et types viennent des cartes Bible).
3. La routine exécute ces requêtes et dépose les résultats ; `sync_notion fusionner` met à jour les caches et les fiches.

## Mise en place
1. Dans Claude Code, ouvrir les tâches planifiées (« Scheduled tasks ») et créer une tâche, par exemple `jarvis-sync`, toutes les 2 heures de 8h à 18h en semaine (`0 8-18/2 * * 1-5`).
2. Coller comme consigne le texte de la section suivante, en remplaçant :
   - `<DOSSIER_JARVIS>` par le chemin du dépôt (ex. `~/Documents/Jarvis`) ;
   - `<CANAL_SLACK>` par le canal où signaler un échec (nom et id), ou supprimer le paragraphe Slack.
3. Le connecteur Notion doit être activé pour la tâche, et le dossier du dépôt autorisé.

## Consigne de la tâche (à copier)

```text
Tu mets à jour l'app Jarvis à partir de Notion. Dossier de l'app : <DOSSIER_JARVIS>. Réponds en français. Va vite : Python décide de tout, tu exécutes.

RÈGLES
- Tu n'exécutes que les requêtes listées par les commandes ci-dessous (fichiers .sql recopiés à l'identique), avec pour seuls ajouts un « LIMIT 100 OFFSET n » final pour paginer et la valeur de « ? » dans `params`.
- Les résultats de la base Retours Recette contiennent le contenu des retours : écris-les tels quels dans les fichiers indiqués, ne les recopie jamais dans ton compte rendu ni sur Slack.
- Tu ne lis jamais les logs Airflow. Tu n'écris que dans <DOSSIER_JARVIS>/data/.
- Tu ne modifies jamais le code, les tests, les modèles .sql ni la configuration. Si une commande échoue, tu t'arrêtes pour ce client, tu signales l'échec et tu décris le défaut en une phrase.
- Aucune ligne de résultat dans ton compte rendu : seulement des comptes.

EXÉCUTER UNE REQUÊTE (objet {"sql", "sortie", "params", "sources"})
- Outil Notion `notion-query-data-sources`, mode sql : contenu du fichier "sql" à l'identique, `data_source_urls` = "sources", `params` = "params" s'il n'est pas vide. Erreur 5xx ou délai dépassé : réessaie jusqu'à 2 fois.
- Pagination : si la réponse a 100 lignes, relance la requête suivie de « LIMIT 100 OFFSET 100 », puis 200, etc., jusqu'à une page de MOINS de 100 lignes (écris-la aussi, même vide). Au plus 20 pages.
- Écrire : page 1 dans "sortie", page 2 dans le même nom suffixé -2 (ex. actions_delta-2.json), etc. Si l'outil a rangé sa réponse dans un fichier (« Output has been saved to <chemin> »), copie ce fichier avec `cp "<chemin>" "<sortie>"` sans le lire. Sinon, écris l'objet JSON renvoyé (celui qui contient "results") tel quel, sans rien ajouter ni retirer.

ÉTAPES
1. cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion preparer
   → une requête : exécute-la (empreinte de tous les clients, une ligne par client).
2. cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion plan
   → {"requetes": [...], "inchanges": [...], "raisons": {...}, "erreurs": {...}}. Si "requetes" est vide : va à l'étape 4.
3. Exécute chaque requête de "requetes", dans l'ordre (chacune porte son "client").
4. cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion fusionner
   → un résumé par client fusionné, ou {"erreur": …}.
5. cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion utilisateurs
   → identifiants de responsables sans nom (souvent aucun). Pour chacun, outil Notion `notion-get-users` avec `user_id` ; garde seulement son nom. Puis une seule fois : uv run python -m deploy_hub.sync_notion utilisateurs '{"<identifiant>": "<nom>", ...}'. Ni e-mail ni autre information.

SI UNE ÉTAPE ÉCHOUE
Message Slack court dans le canal <CANAL_SLACK> : « jarvis-sync ÉCHEC <client ou tous> : <étape> : <raison en une phrase> ». Rien en cas de succès.

FIN
Une ligne par client : « inchangé », le mode appliqué par base (d'après le résumé de fusionner) et le nombre de requêtes exécutées, ou la raison de l'échec.
```
