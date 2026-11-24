SELECT '{{cle}}' AS cle,
  COUNT(*) || '|' || COALESCE(MAX("Dernière modif"), '') AS demandes
FROM "collection://1ac4976c-dfac-81d3-a334-000bcc82d24e"
WHERE "Projet ou équipe liée" LIKE '%{{page_id}}%'
