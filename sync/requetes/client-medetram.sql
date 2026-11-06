SELECT
  url,
  "Nom" AS nom,
  "date:Date de Kick-off:start" AS kickoff,
  "date:Date réception des données:start" AS reception_donnees,
  "date:Date recette 1:start" AS recette_1,
  "date:Date recette 2:start" AS recette_2,
  "date:Date recette 3:start" AS recette_3,
  "date:Date du Freeze:start" AS freeze,
  "date:Date formation:start" AS formation,
  "date:Date déploiement:start" AS deploiement,
  "date:Date déploiement Lot 2:start" AS deploiement_lot2
FROM "collection://28d3f790-e4ca-404c-84de-8ea4087bce14"
WHERE url LIKE '%3ec4976cdfac8113b6fbf0d50dcf0035%'
