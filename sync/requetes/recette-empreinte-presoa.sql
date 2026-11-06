SELECT COUNT(*) AS n, MAX("Last edited time") AS modifie
FROM "collection://9e3440bc-941b-4c76-8b65-6fc15bbb02a7"
WHERE "🏕️ Clients en déploiement" LIKE '%3284976cdfac8157b91dcabc83e80b1c%'
   OR "Client" IN ('presoa_pulse', 'presoa-uegar')
