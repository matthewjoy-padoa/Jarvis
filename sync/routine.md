<!-- Routine de synchro Notion → Jarvis : source unique de la consigne.
     Utilisée par la commande /jarvis-sync (.claude/commands/jarvis-sync.md), par le bouton « Rafraîchir depuis Notion »
     quand l'app n'a pas de jeton Notion (claude -p, voir src/deploy_hub/sources/sync_routine.py) et par la tâche planifiée.
     Les clients synchronisés sont ceux configurés dans l'app (config/clients/, ou JARVIS_CONFIG_DIR), sauf les archivés : « preparer » les lit.
     Les demandes BAU (réglage JARVIS_NOTION_BAU) sont du même passage : rien de plus à faire, Python donne leurs requêtes avec les autres. -->

Tu mets à jour l'app Jarvis à partir de Notion. Tu travailles dans le dossier de l'app (le dossier courant, qui contient sync/routine.md). Réponds en français. Va vite : Python décide de tout et récupère lui-même les réponses de Notion ; toi, tu exécutes les requêtes qu'il te donne, sans rien commenter entre deux étapes.

RÈGLES
- Tu n'exécutes que les requêtes données par les commandes ci-dessous, recopiées à l'identique : Python compare, une requête modifiée est refusée puis redemandée.
- Tu ne lis, ne recopies et ne résumes jamais les réponses de Notion (les retours de recette sont du contenu client) : ni dans ton compte rendu, ni sur Slack.
- Tu ne lis jamais les logs Airflow. Tu ne modifies jamais le code, les tests, les modèles .sql ni la configuration.
- Si une commande échoue (« ÉCHEC synchro : … »), tu t'arrêtes et tu le signales.

ÉTAPES
1. uv run python -m deploy_hub.sync_notion preparer
   → {"requetes": [...], "ensuite": "<commande>"}.
2. Exécute TOUTES les requêtes de "requetes" dans le même message (plusieurs appels d'outil à la fois) : outil Notion `notion-query-data-sources`, mode sql, `query` = "requete" à l'identique, `data_source_urls` = "sources", `params` = "params" s'il est donné. Une entrée qui porte "vue" au lieu de "requete" : même outil, mode view, `view_url` = "vue" à l'identique, `page_size` = 100. Une entrée qui porte "page" : outil Notion `notion-fetch`, `id` = "page" à l'identique. Tu ne fais RIEN des réponses, quelles qu'elles soient : rendues en ligne, rangées dans un fichier (« Output has been saved to … » : c'est normal, Python ira les y lire, ne lance aucune commande pour ça), en erreur ou en délai dépassé (ne réessaie pas à la main, l'étape 3 redemandera la requête). Dès que tous les appels ont répondu, passe à l'étape 3.
3. uv run python -m deploy_hub.sync_notion suite
   → soit de nouvelles "requetes" : retourne à l'étape 2 ;
   → soit {"termine": true, "compte_rendu": [...], "utilisateurs": [...]} : va à l'étape 4.
4. Seulement si "utilisateurs" n'est pas vide : pour chaque identifiant, outil Notion `notion-get-users` avec `user_id` ; garde seulement son nom. Puis une seule fois : uv run python -m deploy_hub.sync_notion utilisateurs '{"<identifiant>": "<nom>", ...}'. Ni e-mail ni autre information.

SECOURS (rare) : SEULEMENT SI UNE REQUÊTE DONNÉE PAR « suite » PORTE UNE CLÉ "sortie"
Python n'a pas retrouvé sa réponse : pour ces requêtes-là seulement, exécute-les une à la fois et dépose chaque réponse. Si l'outil a rangé la réponse dans un fichier, lance `uv run python -m deploy_hub.sync_notion deposer "<valeur de sortie>"` (le chemin donné dans "sortie", jamais celui du fichier rangé ; ne lis ni ne copie ce fichier). Sinon, écris dans "sortie" l'objet JSON renvoyé (celui qui contient "results"), tel quel. Sans clé "sortie", cette section ne s'applique pas : `deposer` ne se lance jamais.

SI UNE ÉTAPE ÉCHOUE
Écris « ÉCHEC <client ou tous> : <étape> : <raison en une phrase> » dans ton compte rendu. Si on t'a donné un canal Slack, envoie aussi ce message court dans ce canal. Rien sur Slack en cas de succès.

FIN
Recopie les lignes de "compte_rendu" (une par client, plus une pour les demandes BAU s'il y en a), et rien d'autre.
