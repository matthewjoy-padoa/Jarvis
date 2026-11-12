SELECT
  (SELECT group_concat(substr(url, -16), ',') FROM "collection://5055cf48-4d55-4606-8c83-92ae3dfaa8a2"
     WHERE "Client" LIKE '%{{page_id}}%') AS actions,
  (SELECT group_concat(substr(url, -16), ',') FROM "collection://9e3440bc-941b-4c76-8b65-6fc15bbb02a7"
     WHERE ("🏕️ Clients en déploiement" LIKE '%{{page_id}}%'{{ou_clients_recette}})) AS recette
