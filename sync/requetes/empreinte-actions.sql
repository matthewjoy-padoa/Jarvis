SELECT COUNT(*) AS n, MAX("Dernière modification le") AS modifie
FROM "collection://5055cf48-4d55-4606-8c83-92ae3dfaa8a2"
WHERE "Client" LIKE '%{{page_id}}%'
