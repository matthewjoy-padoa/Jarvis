SELECT COUNT(*) AS n, MAX("Last edited time") AS modifie
FROM "collection://9e3440bc-941b-4c76-8b65-6fc15bbb02a7"
WHERE "🏕️ Clients en déploiement" LIKE '%{{page_id}}%'{{ou_clients_recette}}
