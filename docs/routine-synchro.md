# Routine de synchro Notion → Jarvis

Jarvis lit Notion de deux façons :
- **avec un jeton d'intégration Notion** (`NOTION_TOKEN`, voir le README) : le bouton « Rafraîchir depuis Notion » appelle l'API ;
- **sans jeton, avec cette routine** : Claude Code exécute les requêtes SQL avec son connecteur Notion.

La consigne de la routine est dans le dépôt : [`sync/routine.md`](../sync/routine.md). C'est la seule source ; il n'y a rien à recopier ni à compléter (le dossier est celui où on la lance).

**Clients synchronisés** : ceux configurés dans l'app (`config/clients/`, ou `JARVIS_CONFIG_DIR`), lus par `sync_notion preparer`. Un client ajouté depuis « Clients → Ajouter un client » est pris au passage suivant ; un client archivé ou retiré ne l'est plus (désarchivé, il est repris).

## Ce que fait un passage
1. **Une seule requête d'empreinte pour tous les clients** : nombre de lignes et dernière modification des actions et des retours, signature des règles et des dates de la fiche.
2. **Python fait le plan** (`sync_notion plan`) : seulement ce qui a changé, base par base.
   - Lignes modifiées ou ajoutées : delta depuis la dernière modification connue du cache.
   - Lignes supprimées : liste compacte des identifiants (une ligne).
   - Colonne de métadonnées ajoutée à la recette : métadonnées seules, les textes restent ceux du cache.
   - Relecture complète : pas de cache, colonnes des actions modifiées, ou actions relues il y a 7 jours ou plus (leurs ressources et types viennent des cartes Bible).
3. La routine exécute ces requêtes et dépose les résultats ; `sync_notion fusionner` met à jour les caches et les fiches.

## Lancer la routine
Trois façons, la même consigne :

1. **Bouton « Rafraîchir depuis Notion »** (fiche d'un client) ou **« Tout rafraîchir »** (page Clients), quand l'app n'a pas de jeton Notion.
   Jarvis lance `claude -p` dans son dossier, avec son propre environnement (donc ses clients), et affiche l'état sous le bouton ; la page se recharge à la fin.
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

