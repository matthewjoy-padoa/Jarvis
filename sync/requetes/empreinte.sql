SELECT '{{slug}}' AS cle,
  (SELECT COUNT(*) || '|' || COALESCE(MAX("Dernière modification le"), '')
     FROM "collection://5055cf48-4d55-4606-8c83-92ae3dfaa8a2"
     WHERE "Client" LIKE '%{{page_id}}%') AS actions,
  (SELECT COUNT(*) || '|' || COALESCE(MAX("Last edited time"), '')
     FROM "collection://9e3440bc-941b-4c76-8b65-6fc15bbb02a7"
     WHERE ("🏕️ Clients en déploiement" LIKE '%{{page_id}}%'{{ou_clients_recette}})) AS recette,
  (SELECT COUNT(*) || '|' || COALESCE(SUM(length(COALESCE("Intitulé de la règle", '') || COALESCE("Type de règle", '')
                                               || COALESCE("Application de la règle", '') || COALESCE("Statut de la règle", ''))), 0)
     FROM "collection://3194976c-dfac-80e7-a241-000bf833be2c"
     WHERE "Clients padoa ayant la règle" LIKE '%{{page_id}}%') AS regles,
  (SELECT COALESCE("Nom", '') || '|' || COALESCE("date:Date de Kick-off:start", '') || '|' || COALESCE("date:Date réception des données:start", '')
          || '|' || COALESCE("date:Date recette 1:start", '') || '|' || COALESCE("date:Date recette 2:start", '') || '|' || COALESCE("date:Date recette 3:start", '')
          || '|' || COALESCE("date:Date du Freeze:start", '') || '|' || COALESCE("date:Date formation:start", '') || '|' || COALESCE("date:Date déploiement:start", '')
          || '|' || COALESCE("date:Date déploiement Lot 2:start", '')
     FROM "collection://28d3f790-e4ca-404c-84de-8ea4087bce14"
     WHERE url LIKE '%{{page_id}}%') AS fiche
