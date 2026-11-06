SELECT
  url,
  CASE WHEN "Statut" IN ('À faire', 'INTE - Prio déploiement', 'INTE - En cours', 'INTE - Post déploiement', 'INTE - En attente donnée client', 'INTE - Fixed : à réinjecter', 'INTE - En attente d''injection', 'INTE - Vérifié en INTEGRATION', 'INTE - Ancien retour - OK', 'OPS - À vérifier avec le client', 'OPS - En attente décision client', 'OPS - À faire directement en front Padoa', 'OPS - Validation à faire en INTEGRATION', 'OPS - Validation à faire en SNAP', 'OPS - Vérifié en INTEGRATION', 'OPS - Vérifié en SNAP', 'OPS - Vérifié et pas OK', 'OPS - Paramétrage Admin Panel', 'PRODUIT - Demande amélioration', 'Non vérifié', 'Non vérifié par le client', 'Non vérifié par le client sans motif', 'A revoir', 'Action identifiée', 'Validé', 'Validé avec commentaire', 'terminé', 'Abandonné', 'Doublon', 'Non concerné') THEN "Statut" ELSE 'Autre' END AS statut,
  CASE WHEN length("Type de données") <= 40 THEN "Type de données" ELSE 'Autre' END AS type_donnees,
  "Logiciel" AS logiciel,
  "date:Deadline:start" AS deadline,
  createdTime AS cree_le,
  "Last edited time" AS modifie,
  "Ligne du cahier de recette" AS ligne,
  "Type de retour" AS type_retour,
  "Commentaire client (NE PAS MODIFIER)" AS commentaire_client,
  "Exemple Salarié / adhérents (NE PAS MODIFIER)" AS exemple,
  "Analyse padoa" AS analyse,
  "Solutions possibles" AS solutions,
  "Discussion INTE/OPS" AS discussion,
  "Échanges / Suivi Déploiement-Inté" AS echanges
FROM "collection://9e3440bc-941b-4c76-8b65-6fc15bbb02a7"
WHERE "🏕️ Clients en déploiement" LIKE '%{{page_id}}%'{{ou_clients_recette}}
ORDER BY createdTime
