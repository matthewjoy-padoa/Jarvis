# Routine de synchro Notion → Jarvis

Jarvis lit Notion de deux façons :
- **le bouton « Rafraîchir depuis Notion »**, si un jeton d'intégration Notion est configuré (`NOTION_TOKEN`, voir le README) ;
- **cette routine**, une tâche planifiée de Claude Code qui exécute les requêtes SQL avec le connecteur Notion. Elle ne demande pas de jeton.

## Mise en place
1. Dans Claude Code, ouvrir les tâches planifiées (« Scheduled tasks ») et créer une tâche, par exemple `jarvis-sync`, toutes les 2 heures de 8h à 18h en semaine (`0 8-18/2 * * 1-5`).
2. Coller comme consigne le texte de la section suivante, en remplaçant :
   - `<DOSSIER_JARVIS>` par le chemin du dépôt (ex. `~/Documents/Jarvis`) ;
   - `<CANAL_SLACK>` par le canal où signaler un échec (nom et id), ou supprimer le paragraphe Slack.
3. Le connecteur Notion doit être activé pour la tâche, et le dossier du dépôt autorisé.

## Consigne de la tâche (à copier)

```text
Tu mets à jour l'app Jarvis à partir de Notion. Dossier de l'app : <DOSSIER_JARVIS> (appelé HUB ci-dessous). Réponds en français. Va vite : ne recopie que ce qui est demandé.

RÈGLES
- Tu n'exécutes que les requêtes écrites par la commande `requetes` (dossier HUB/data/notion/<slug>/requetes/), recopiées à l'identique, avec pour seuls ajouts possibles un « LIMIT 100 OFFSET n » final pour paginer et la valeur du paramètre « ? » dans `params`. Le script refuse toute colonne de recette non prévue.
- Les résultats de la base Retours Recette contiennent le contenu des retours (commentaire client, exemple, analyse) : écris-les tels quels dans les fichiers indiqués, ne les recopie jamais dans ton compte rendu ni sur Slack. Ces fichiers restent sur la machine (data/ est exclu de git).
- Tu ne lis jamais les logs Airflow. Tu n'écris que dans HUB/data/.
- Tu ne modifies jamais le code, les tests, les modèles .sql ni la configuration de HUB, même pour corriger un bug : si le script échoue ou si tu vois un défaut, tu t'arrêtes, tu signales l'échec et tu décris le défaut en une phrase dans ton compte rendu.
- Tu ne recopies aucune ligne de résultat dans ton compte rendu : seulement des comptes.

EXÉCUTER UNE REQUÊTE
Outil Notion `notion-query-data-sources`, mode sql, contenu du fichier .sql recopié à l'identique, `data_source_urls` = toutes les adresses collection://… citées dans la requête. Si la requête contient « ? », passe la date indiquée dans `params` (un seul élément). Erreur 5xx ou délai dépassé : réessaie jusqu'à 2 fois.
Pagination : la première page est la requête sans LIMIT. Si elle renvoie 100 lignes, relance-la suivie de « LIMIT 100 OFFSET 100 », puis « LIMIT 100 OFFSET 200 », etc. Arrête-toi à la première page qui renvoie MOINS de 100 lignes, et écris-la aussi, même vide ; ne te fie pas à "has_more". Au plus 20 pages, sinon échec.
Écrire un résultat : l'objet JSON renvoyé par l'outil (celui qui contient "results"), sans rien ajouter ni retirer ; page 1 dans <fichier>.json, page 2 dans <fichier>-2.json, page 3 dans <fichier>-3.json, etc.

POUR CHAQUE CLIENT
Liste des clients : cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion clients
Pour chaque <slug> (dossier des résultats : HUB/data/notion/<slug>/, appelé R ; dossier des requêtes : R/requetes/, appelé Q) :
0. Lance : cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion requetes <slug>
1. Exécute Q/empreinte-actions.sql et Q/empreinte-recette.sql ; écris-les dans R/empreinte_actions.json et R/empreinte_recette.json.
2. Lance : cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion verifier <slug>
   Il affiche un JSON avec "mode".
3. Selon le mode :
   - "inchangé" : rien d'autre pour ce client, passe au suivant.
   - Sinon, avant d'écrire le moindre résultat : rm -f R/actions*.json R/recette*.json (ne touche pas aux cache-*.json, contenu-*.json, empreinte_*.json, journal.json ni etat-synchro.json).
   - "delta" : exécute Q/client.sql et Q/regles.sql (écris R/client.json, R/regles.json). Puis, pour chaque base présente dans la réponse sous la forme "<base>_depuis" (actions, recette) : exécute Q/<base>-delta.sql avec params = [valeur de "<base>_depuis"] et écris le résultat dans R/<base>_delta.json (pages <base>_delta-2.json…). Une base absente de la réponse n'a pas changé.
   - "complet" : exécute Q/client.sql, Q/actions.sql, Q/recette.sql, Q/regles.sql et écris chacune dans R/<nom>.json (pages <nom>-2.json…).
4. Lance : cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion <slug>
   Le script fusionne dans le cache et n'écrit la fiche que si tout est valide.

APRÈS LES CLIENTS : NOMS DES RESPONSABLES
Lance : cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion utilisateurs
Il affiche la liste des identifiants de responsables sans nom (souvent vide). Pour chacun, outil Notion `notion-get-users` avec `user_id` = l'identifiant ; garde seulement son nom. Puis lance une seule fois : cd <DOSSIER_JARVIS> && uv run python -m deploy_hub.sync_notion utilisateurs '{"<identifiant>": "<nom>", ...}'. N'écris ni e-mail ni autre information. Un identifiant introuvable est laissé de côté.

SI UNE ÉTAPE ÉCHOUE
- Ne modifie pas la fiche existante ni les caches à la main.
- Envoie un message Slack court dans le canal <CANAL_SLACK> : « jarvis-sync ÉCHEC <slug> : <étape> : <raison en une phrase> ». En cas de succès ou de client inchangé, n'envoie rien.

FIN
Réponds en une ligne par client : « inchangé », ou le résumé JSON affiché par le script (il contient le mode appliqué), ou la raison de l'échec.
```
