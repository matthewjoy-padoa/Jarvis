SELECT
  url,
  "Intitulé" AS intitule,
  "Statut" AS statut,
  "Statut intégration - Injections" AS statut_injection,
  "date:Deadline:start" AS deadline,
  "Point de vigilance" AS vigilance,
  "Événement" AS evenement,
  "Dernière modification le" AS modifie,
  -- carte d'injection : « INTE Injection » dans Actions à mener (manuel) ou dans la carte Bible liée
  CASE WHEN COALESCE(a."Actions à mener (manuel)", '') LIKE '%INTE Injection%'
         OR EXISTS (SELECT 1 FROM json_each(a."Carte Bible déploiement") j
                    JOIN "collection://0cdf1861-fb15-4fee-b792-e8dfd7f0491f" b ON b.url = j.value
                    WHERE b."Actions à mener" LIKE '%INTE Injection%')
       THEN 1 ELSE 0 END AS injection,
  -- ressources liées (noms orchestrator) : celles de la carte, sinon celles de sa carte Bible
  (SELECT group_concat(r."Integration name", ',')
     FROM json_each(CASE WHEN a."Ressources liées - Manuel" IS NOT NULL AND a."Ressources liées - Manuel" <> '[]'
                         THEN a."Ressources liées - Manuel"
                         ELSE COALESCE((SELECT b."ressources liées" FROM json_each(a."Carte Bible déploiement") jb
                                        JOIN "collection://0cdf1861-fb15-4fee-b792-e8dfd7f0491f" b ON b.url = jb.value), '[]') END) jr
     JOIN "collection://3274976c-dfac-808c-bb6c-000bc601f8c4" r ON r.url = jr.value) AS ressources,
  -- types d'action (« INTE Injection », « OPS Paramétrage »…) : ceux de la carte et ceux de sa carte Bible ; l'équipe en est le préfixe
  TRIM(COALESCE(a."Actions à mener (manuel)", '') || ' ' ||
       COALESCE((SELECT group_concat(b."Actions à mener", ' ') FROM json_each(a."Carte Bible déploiement") jt
                 JOIN "collection://0cdf1861-fb15-4fee-b792-e8dfd7f0491f" b ON b.url = jt.value), '')) AS types_action
FROM "collection://5055cf48-4d55-4606-8c83-92ae3dfaa8a2" a
WHERE "Client" LIKE '%3ec4976cdfac8113b6fbf0d50dcf0035%'
ORDER BY url
