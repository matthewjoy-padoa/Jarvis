SELECT
  url,
  "Nom" AS intitule,
  "Statut de l'action" AS statut,
  "Priorité" AS priorite,
  "date:Echéance de l'action:start" AS echeance,
  "Responsable action" AS responsables,
  "Référent inté" AS referents,
  createdTime AS cree_le,
  "Dernière modif" AS modifie,
  -- clients liés : leur nom de domaine Padoa
  (SELECT group_concat(c."Nom de domaine padoa", ',') FROM json_each(a."Client lié à l'action") jc
     JOIN "collection://98866091-8a6b-48d4-8215-e8ae5f90ee41" c ON c.url = jc.value) AS clients,
  -- ressources liées (noms orchestrator)
  (SELECT group_concat(r."Integration name", ',') FROM json_each(a."💶 Resources") jr
     JOIN "collection://3274976c-dfac-808c-bb6c-000bc601f8c4" r ON r.url = jr.value) AS ressources
FROM "collection://1ac4976c-dfac-81d3-a334-000bcc82d24e" a
WHERE "Projet ou équipe liée" LIKE '%{{page_id}}%'
  AND "Dernière modif" >= ?
ORDER BY url
