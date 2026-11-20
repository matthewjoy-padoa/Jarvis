# Routine de synchro Notion → Jarvis

Jarvis lit Notion de deux façons :
- **avec un jeton d'intégration Notion** (`NOTION_TOKEN`, voir le README) : le bouton « Rafraîchir depuis Notion » appelle l'API ;
- **sans jeton, avec cette routine** : Claude Code exécute les requêtes SQL avec son connecteur Notion.

La consigne de la routine est dans le dépôt : [`sync/routine.md`](../sync/routine.md). C'est la seule source ; il n'y a rien à recopier ni à compléter (le dossier est celui où on la lance).

**Clients synchronisés** : ceux configurés dans l'app (`config/clients/`, ou `JARVIS_CONFIG_DIR`), lus par `sync_notion preparer`. Un client ajouté depuis « Clients → Ajouter un client » est pris au passage suivant ; un client archivé ou retiré ne l'est plus (désarchivé, il est repris). Le bouton de la fiche d'un client ne relit que ce client (`sync_notion preparer <slug>`) ; « Tout rafraîchir », `/jarvis-sync` et la tâche planifiée les relisent tous.

## Ce que fait un passage
Python décide de tout et récupère lui-même les réponses ; le modèle ne fait qu'exécuter les requêtes qu'on lui donne. Deux commandes : `sync_notion preparer` ouvre le passage, `sync_notion suite` se relance jusqu'à la fin.
- **Récolte** : `suite` lit les réponses de Notion dans le transcript de la session Claude Code qui les a reçues (rendues en ligne ou rangées dans un fichier) et les range dans le dossier de données, à l'octet près. Le modèle ne relit pas les requêtes, ne recopie ni ne dépose les réponses.
- **Vérification** : une réponse n'est prise que si la requête exécutée est celle demandée (aux blancs près) ; une requête modifiée, une erreur de Notion ou une réponse d'un passage précédent sont ignorées, et la requête est redemandée. À la deuxième demande, le modèle dépose la réponse lui-même (`deposer`, secours) ; à la troisième, le passage s'arrête sans rien fusionner.
- **Lots** : les requêtes sont données par lots d'environ 16 000 caractères ; une lecture complète demande d'emblée toutes ses pages (l'empreinte donne le nombre de lignes).
- Le format du transcript est interne à Claude Code : s'il change, la récolte ne trouve plus rien et le secours prend le relais (plus lent, mais la synchro aboutit).

Durées mesurées sur la VM le 2026-11-20 (un client de 1 130 cartes et 150 retours, modèle `haiku`) : rien de neuf 16 s ; petit delta 20 à 24 s ; trois clients 29 à 36 s ; lecture complète 108 s. Avant (modèle par défaut, qui relisait les requêtes et recopiait les réponses) : 109 s pour un petit delta sur trois clients, 4 min 02 pour une lecture complète.

1. **Une seule requête d'empreinte** (une ligne par client du passage) : nombre de lignes et dernière modification des actions et des retours, signature des règles et des dates de la fiche.
2. **Python fait le plan** : seulement ce qui a changé, base par base.
   - Lignes modifiées ou ajoutées : delta depuis la dernière modification connue du cache.
   - Lignes supprimées : liste compacte des identifiants (une ligne).
   - Colonne de métadonnées ajoutée à la recette : métadonnées seules, les textes restent ceux du cache.
   - Relecture complète : pas de cache, colonnes des actions modifiées, ou actions relues il y a 7 jours ou plus (leurs ressources et types viennent des cartes Bible).
3. Le modèle exécute ces requêtes ; `suite` récolte les réponses, met à jour les caches et les fiches, et rend le compte rendu (une ligne par client).

## Lancer la routine
**Toute seule** : Jarvis lance une synchro de tous les clients suivis dès que l'un d'eux n'a pas été vérifié dans Notion depuis une heure (réglage `JARVIS_SYNC_EVERY_MIN`, en minutes ; 0 : jamais), par l'API s'il a un jeton Notion, sinon par cette routine. Une synchro faite à la main compte (heure de dernière vérification de chaque client, `data/notion/verifications.json`) et un redémarrage de Jarvis ne remet rien à zéro (premier contrôle cinq minutes après le démarrage). Si elle ne part pas (une autre synchro tourne, `claude` introuvable), nouvel essai cinq minutes plus tard ; un client qui reste en échec est retenté une fois par intervalle. Un échec de synchro automatique n'est signalé qu'une fois par jour.

À la demande, trois façons, la même consigne :

1. **Bouton « Rafraîchir depuis Notion »** (fiche d'un client) ou **« Tout rafraîchir »** (page Clients), quand l'app n'a pas de jeton Notion.
   Jarvis ouvre le passage lui-même, puis lance `claude -p` dans son dossier, avec son propre environnement (donc ses clients), et affiche l'état sous le bouton ; la page se recharge à la fin.
   - Modèle : `haiku`, effort `low` (réglages `JARVIS_SYNC_MODEL` et `JARVIS_SYNC_EFFORT`, section Claude Code des Réglages). Il ne fait qu'exécuter des requêtes que Python vérifie : un modèle rapide suffit.
   - Il faut Claude Code en ligne de commande (`claude` dans le PATH, ou `CLAUDE_BIN` dans Réglages) et le connecteur Notion disponible en ligne de commande.
   - Outils permis : les commandes `sync_notion` (dont `deposer`, qui dépose les grosses réponses que Claude Code range dans `~/.claude/…/tool-results/` : Claude Code refuse un `cp` depuis ce dossier), la lecture et l'écriture du dossier de données, le connecteur Notion `mcp__claude_ai_Notion`, en `--permission-mode dontAsk`. Pour un connecteur au nom inhabituel : `JARVIS_SYNC_ALLOWED_TOOLS` (séparés par « ; »).
   - Une seule synchro à la fois ; compte rendu dans `data/notion/routine.log`, échec signalé dans les notifications.
2. **Commande `/jarvis-sync`** : dans Claude Code (app ou terminal) ouvert sur le dossier de Jarvis, taper `/jarvis-sync`, ou `/jarvis-sync #mon-canal` pour signaler un échec sur Slack.
3. **Tâche planifiée** : dans Claude Code, « Scheduled tasks », créer une tâche (par exemple `jarvis-sync`, `0 8-18/2 * * 1-5`) avec pour consigne :

   ```text
   Tu mets à jour l'app Jarvis à partir de Notion. Dossier de Jarvis : ~/Documents/Jarvis. Réponds en français.
   1. Lis ~/Documents/Jarvis/sync/routine.md et suis sa consigne à la lettre. Lance chaque commande depuis le dossier de Jarvis : préfixe-la par « cd ~/Documents/Jarvis && ».
   2. Canal Slack où signaler un échec : #mon-canal (id), ou aucun. Rien sur Slack en cas de succès.
   ```
   (Remplace le dossier s'il est ailleurs, et le canal.)
   Le connecteur Notion doit être activé pour la tâche, et le dossier autorisé. La consigne suit le dépôt : un `git pull` suffit à la mettre à jour.

