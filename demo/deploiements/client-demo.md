---
# Fiche fictive : aucune donnée client. Noms de DAG et personnes inventés.
client: Client Démo
software: Preventiel
status: en cours
notion_url: https://www.notion.so/
milestones:
  kickoff: 2026-09-01
  reception_donnees: 2026-09-15
  recette_1: 2026-10-20
  recette_2: 2026-11-17
  freeze: 2026-11-27
  formation: 2026-12-01
  deploiement: 2026-12-07
stacks:
  - {env: prod, label: Client Démo, url: "https://example.com/prod"}
  - {env: recette, label: Snap recette, url: "https://example.com/recette"}
links:
  - {label: Drive client, url: "https://drive.google.com/"}
  - {label: Catalogue RDD, url: "https://www.notion.so/"}
resources:
  - {name: firm, status: Ressource injectée en prod, dag_id: demo_client_firm}
  - {name: employer_user, status: Vérification OK recette, dag_id: demo_client_employer_user}
  - {name: visit_histo, status: Vérification KO recette, dag_id: demo_client_visit_histo, note: "Retour R-02 en cours"}
  - {name: vaccine_injection, status: En cours de dev, dag_id: demo_client_vaccine_injection}
  - {name: amt_task, status: A faire}
actions:
  - {title: Recevoir l'extraction complète, status: Terminé, owner: Inté, due: 2026-09-15}
  - {title: Ajouter le client dans keycloak-rights, status: Terminé, owner: Inté}
  - {title: Préparer la recette 2, status: En cours, owner: Inté, due: 2026-11-14}
  - {title: Mapping des codes AMT à valider par le client, status: À valider, owner: CDP, due: 2026-11-10, vigilance: true, comment: "Bloque l'injection amt_task"}
  - {title: Volume de documents CED à confirmer, status: À faire, owner: Inté, due: 2026-11-20, vigilance: true}
retours:
  - {id: R-01, status: INTE - Vérifié en INTEGRATION, data_type: Adhérents, resource: firm}
  - {id: R-02, status: À faire, data_type: Visites historiques, resource: visit_histo, due: 2026-11-12, created: 2026-10-21}
  - {id: R-03, status: OPS - Vérifié et pas OK, data_type: Examens complémentaires, due: 2026-11-15, ligne: "Examens complémentaires repris", commentaire_client: "Texte fictif : il manque des examens.", analyse: "Texte fictif : filtre trop strict."}
  - {id: R-04, status: "INTE - 🤔 Besoin clarification retour", data_type: Vaccins, resource: vaccine_injection}
regles:
  - {title: Clôturer les adhérents radiés avant 2020, resource_table: firm, type: Règle par défaut - logiciel, application: En flux - Transform, status: Active - Présentable}
  - {title: Ne pas reprendre les visites sans date, resource_table: visit_histo, type: Règle obligatoire - padoa, application: En flux - Cleaner, status: Active - A ne pas pousser}
  - {title: Remplacer les codes AMT par les étiquettes padoa, resource_table: amt_task, type: Règle par défaut à statuer - logiciel, application: Par requête, status: WIP}
remontees:
  - {title: Un médecin ne retrouve pas l'historique d'un salarié, contexte: Formation, status: En cours, posted: 2026-10-28, slack_url: "https://slack.com/"}
  - {title: Question sur la reprise des pièces jointes, contexte: Déploiement, status: À traiter, posted: 2026-11-02}
---

Notes libres du déploiement (non lues par l'app pour l'instant).
