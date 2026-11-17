"""Exemples de la page Agents en mode démo : agents, suivis de DAG, enchaînements et lancements fictifs.

Sans eux la page est vide tant que rien n'a été lancé pour de vrai. Tout est inventé (clients des fiches de demo/,
noms de DAG, chemins de fichiers, PR) : aucune donnée client. Rien n'est écrit dans l'historique : les exemples vivent en
mémoire, leurs journaux dans un dossier temporaire, et ils ne sont jamais montrés en mode live.
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from deploy_hub.sources.airflow import DagRun
from deploy_hub.models import Deployment, RetourRecette
from deploy_hub.sources.claude_runner import TOKEN_KINDS, AgentRun, read_tokens, retour_prompt
from deploy_hub.sources.dag_chain import GREEN, RUNNING, SKIPPED, WAITING, DagChain, Step
from deploy_hub.sources.dag_watch import DagWatch, report_name

FIRST_ID = 901  # loin des numéros de l'historique : un exemple ne prend jamais la place d'un vrai lancement
DEMO, DEMO_SLUG, OTHER, OTHER_SLUG = "Client Démo", "client-demo", "Client Exemple", "client-exemple"
PR = "https://github.com/exemple-org/orchestrator-demo/pull/"
STACK, INTEGRATION = "snap-recette", "integration"
SOFTWARES = {DEMO_SLUG: "Preventiel", OTHER_SLUG: "Kenora"}  # logiciels des fiches de demo/
# Retours de recette fictifs : (ressource, ligne du cahier, commentaire du client, exemple, analyse). Le brief d'un exemple est
# le vrai brief de Jarvis (retour_prompt) rempli avec ce contenu inventé.
RETOURS = {
    "R-01": ("firm", "Adhérents : un adhérent par SIRET", "Certains adhérents apparaissent deux fois dans Padoa après la reprise.",
             "Adhérent fictif « Entreprise Exemple », SIRET 000 000 000 00000 : deux fiches.",
             "Le doublon apparaît après le nettoyage : le SIRET est saisi avec et sans espaces dans la source."),
    "R-02": ("visit_histo", "Visites historiques : type de visite repris", "Des visites historiques arrivent sans type de visite.",
             "Dossier fictif n° 0042 : visite du 12/03/2021 sans type dans Padoa, « VP » dans le logiciel d'origine.",
             "Le code « VP » n'a sans doute pas de correspondance dans le mapping des types de visite."),
    "R-03": ("additional_exam", "Examens complémentaires : tous les examens repris", "Il manque des examens complémentaires dans la reprise.",
             "Dossier fictif n° 0108 : 3 examens dans le logiciel d'origine, 1 seul dans Padoa.",
             "Les examens manquants semblent être ceux saisis hors visite."),
    "R-04": ("vaccine_injection", "Vaccins : un rappel par injection", "Les rappels de vaccin semblent en double.",
             "Dossier fictif n° 0077 : deux rappels à trois semaines d'écart.", ""),
    "R-07": ("firm", "Adhérents : adresse complète", "Les adresses des adhérents sont tronquées.",
             "Adhérent fictif « Société Exemple » : l'adresse s'arrête au 38e caractère.", ""),
}
# Tokens fictifs annoncés à la fin de chaque agent d'exemple : (entrée, sortie, cache relu, cache écrit), dans l'ordre de TOKEN_KINDS
TOKENS = {"visit_histo": (1_840, 21_600, 3_420_000, 186_000), "firm": (920, 8_400, 1_180_000, 74_000), "vaccine": (410, 2_900, 402_000, 31_000)}


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _call(key: str, name: str, **args) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": key, "name": name, "input": args}]}}


def _result(key: str, text: str, error: bool = False) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": key, "content": text, "is_error": error}]}}


def _end(log: str, turns: int, cost: float) -> dict:
    """Dernier évènement du journal, comme Claude Code l'écrit : tours, coût et tokens."""
    return {"type": "result", "subtype": "success", "num_turns": turns, "total_cost_usd": cost,
            "usage": {key: n for (_, _, key), n in zip(TOKEN_KINDS, TOKENS[log])}}


MAPPING = "src/mappings/client/client_demo/mappings_visit_histo.py"
MAPPING_TEST = "tests/src/mappings/client/client_demo/test_mappings_visit_histo.py"
PYTEST = "uv run pytest tests/src/mappings/client/client_demo -q"
CLEANER, CLEANER_TEST = "src/lib/steps/clean/firm.py", "tests/src/lib/steps/clean/test_firm.py"

AGENT_LOGS = {
    "visit_histo": [
        _say("Je lis le retour R-02 : des visites historiques sortent sans type de visite."),
        _call("a1", "Bash", command="grep -rn 'visit_type' src/mappings/client/client_demo"), _result("a1", f"{MAPPING}:42: VISIT_TYPES = {{"),
        _say("Le code source « VP » n'est pas dans la table de correspondance : la visite garde un type vide. J'ajoute le code et un test."),
        _call("a2", "Edit", file_path=MAPPING), _result("a2", "ok"),
        _call("a3", "Write", file_path=MAPPING_TEST), _result("a3", "ok"),
        _call("a4", "Bash", command=PYTEST), _result("a4", "1 failed, 11 passed in 4.2s", error=True),
        _say("Le test échoue : le code arrive en minuscules dans l'extract. Je normalise la casse avant la correspondance."),
        _call("a5", "Edit", file_path=MAPPING), _result("a5", "ok"),
        _call("a6", "Bash", command=PYTEST), _result("a6", "12 passed in 4.0s"),
        _call("a7", "Bash", command="git commit -am '[FIX](client_demo) visit_histo : type de visite VP'"), _result("a7", "2 files changed"),
        _call("a8", "Bash", command="gh pr create --title '[FIX](client_demo) visit_histo : type de visite VP'"), _result("a8", PR + "412"),
        _say(f"PR ouverte : {PR}412. Le code « VP » est mappé, la casse est normalisée, 12 tests passent."),
        _end("visit_histo", 58, 2.38),
    ],
    "firm": [
        _say("Retour R-01 : des adhérents sortent en double après le nettoyage. Je regarde l'étape clean de firm."),
        _call("b1", "Bash", command="cat " + CLEANER), _result("b1", "def clean_firm(df): ..."),
        _call("b2", "Bash", command="docker ps"), _result("b2", "Commande refusée : Bash(docker ps) n'est pas dans les outils autorisés.", error=True),
        _say("Je n'ai pas accès à la base locale : je raisonne sur le code et les tests. Le dédoublonnage compare le SIRET sans retirer les espaces."),
        _call("b3", "Edit", file_path=CLEANER), _result("b3", "ok"),
        _call("b4", "Edit", file_path=CLEANER_TEST), _result("b4", "ok"),
        _call("b5", "Bash", command="uv run pytest tests/src/lib/steps/clean -q"), _result("b5", "34 passed in 9.8s"),
        _call("b6", "Bash", command="gh pr create --title '[FIX](client_demo) firm : dédoublonnage sur SIRET normalisé'"), _result("b6", PR + "398"),
        _say(f"PR ouverte : {PR}398. Attention : le correctif touche l'étape clean, commune à tous les clients."),
        _end("firm", 24, 0.84),
    ],
    "vaccine": [
        _say("Retour R-04 : « les rappels de vaccin semblent en double ». Je cherche d'où viennent les deux lignes."),
        _call("c1", "Bash", command="grep -rn 'booster' src/mappings/client/client_demo"),
        _result("c1", "src/mappings/client/client_demo/mappings_vaccine_injection.py:18: BOOSTER_CODES = ..."),
        _say("Les deux lignes ont des dates différentes dans l'extract : ce sont peut-être deux injections réelles. Le code ne dit pas "
             "laquelle garder : c'est une question métier, je ne tranche pas."),
        _say("Je n'ai rien modifié. Question pour le client : deux rappels à moins de 30 jours sont-ils une erreur de saisie ou deux injections ?"),
        _end("vaccine", 11, 0.31),
    ],
    "running": [
        _say("Je lis le retour R-03 : il manque des examens complémentaires dans la reprise."),
        _call("d1", "Bash", command="grep -rn 'exam' src/mappings/client/client_demo --include='*.sql' -l"),
        _result("d1", "src/mappings/client/client_demo/queries/additional_exam.sql"),
        _say("La requête filtre sur les examens rattachés à une visite : ceux saisis hors visite sont écartés. Je vérifie avec un test."),
        _call("d2", "Bash", command="uv run pytest tests/src/mappings/client/client_demo -q -k additional_exam"),
    ],
}

WATCH_LOGS = {
    "running": "Suivi du run démarré.\nVague 1/4 : 12 tâches lancées.\nVague 1/4 : 12 vertes.\nVague 2/4 : 12 tâches lancées.\n"
               "transform_visit_histo_07 : délai dépassé (erreur passagère), relance 1/3.\nVague 2/4 : 11 vertes, 1 en cours.\n",
    "green": "Suivi du run démarré.\nextract : vert.\ntransform : vert.\nclean : mémoire insuffisante (erreur passagère), relance 1/3.\n"
             "clean : vert.\ncompare : vert.\nload : connexion perdue (erreur passagère), relance 1/3.\nload : vert.\nRun vert.\n",
    "ko": "Suivi du run démarré.\nextract : vert.\ntransform : vert.\nclean : vert.\ncompare : vert.\n"
          "load_firm : en échec (contrainte d'unicité), erreur non passagère : pas de relance.\nRun en échec.\n",
}
REPORT = ("# Rapport de fin (fictif)\n\n| Étape | Durée | Résultat |\n|---|---|---|\n| extract | 3 min | vert |\n| transform | 9 min | vert |\n"
          "| clean | 6 min | vert, 1 relance |\n| compare | 4 min | vert |\n| load | 19 min | vert, 1 relance |\n\n"
          "Loader : 1 240 lignes créées, 0 rejetée.\nCompare : 0 écart.\nMappings manquants : aucun.\n")


class Examples:
    """Jeu d'exemples construit au démarrage ; les heures sont calées sur « now » pour que la page paraisse vivante."""

    def __init__(self, now: datetime | None = None, folder: Path | None = None):
        now = (now or datetime.now()).replace(microsecond=0)
        self.folder = folder or Path(tempfile.mkdtemp(prefix="jarvis-exemples-"))

        def ago(**delta) -> datetime:
            return now - timedelta(**delta)

        def agent(n, slug, client, ref, subject, category, log, start, minutes=None, status="terminé", **more) -> AgentRun:
            resource, line, comment, example, analysis = RETOURS[ref]
            retour = RetourRecette(ref, "En cours", category, SOFTWARES[slug], resource=resource, ligne=line, type_retour="Écart de données",
                                   commentaire_client=comment, exemple=example, analyse=analysis)
            brief = retour_prompt(Deployment(slug, client, SOFTWARES[slug]), retour)
            run = AgentRun(FIRST_ID + n, slug, client, f"{ref} · {subject}", f"fix-{slug}-{ref.lower()}", brief, start, status,
                           ended_at=start + timedelta(minutes=minutes) if minutes else None, category=category, **more)
            if log in TOKENS:  # relevés comme pour un vrai agent, dans le dernier évènement de son journal
                read_tokens(run, AGENT_LOGS[log][-1])
            if log:
                self.agent_log(run).write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in AGENT_LOGS[log]), encoding="utf-8")
            return run

        self.runs = [
            agent(4, DEMO_SLUG, DEMO, "R-03", "il manque des examens complémentaires", "Examens complémentaires", "running", ago(minutes=6),
                  status="en cours"),
            agent(3, OTHER_SLUG, OTHER, "R-07", "adresses d'adhérents tronquées", "Adhérents", "", ago(hours=1, minutes=10), 3, status="arrêté",
                  summary="Arrêté à la main : le brief visait la mauvaise ressource."),
            agent(2, DEMO_SLUG, DEMO, "R-04", "rappels de vaccin en double", "Vaccins", "vaccine", ago(hours=2, minutes=5), 7, cost_usd=0.31, num_turns=11,
                  summary="Rien modifié. Question pour le client : deux rappels à moins de 30 jours sont-ils une erreur de saisie ou deux injections ?"),
            agent(1, DEMO_SLUG, DEMO, "R-02", "visites historiques sans type de visite", "Visites historiques", "visit_histo", ago(hours=20),
                  27, pr_url=PR + "412", cost_usd=2.38, num_turns=58,
                  summary=f"PR ouverte : {PR}412. Le code « VP » est mappé, la casse est normalisée, 12 tests passent."),
            agent(0, DEMO_SLUG, DEMO, "R-01", "adhérents en double après nettoyage", "Adhérents", "firm", ago(hours=23), 9,
                  pr_url=PR + "398", cost_usd=0.84, num_turns=24,
                  summary=f"PR ouverte : {PR}398. Attention : le correctif touche l'étape clean, commune à tous les clients."),
        ]

        def watch(n, dag, env, card, start, status, summary, log, minutes=None, report="") -> DagWatch:
            w = DagWatch(FIRST_ID + n, DEMO_SLUG, DEMO, "client_demo", f"demo_client_{dag}", f"manual__{start.isoformat()}", env, card, start,
                         status, start + timedelta(minutes=minutes) if minutes else None, summary)
            self.watch_log(w).write_text(WATCH_LOGS[log], encoding="utf-8")
            if report:
                self.watch_report(w).write_text(report, encoding="utf-8")
            return w

        self.watches = [
            watch(3, "visit_histo", STACK, "Visites historiques", ago(minutes=18), "suivi en cours",
                  "Vague 2/4 : 11 vertes, 1 relance en cours (délai dépassé).", "running"),
            watch(2, "employer_user", STACK, "Utilisateurs employeur", ago(hours=1, minutes=2), "vert",
                  "Run vert en 41 min : 2 relances automatiques, aucune tâche en échec.", "green", 41, REPORT),
            watch(1, "firm", STACK, "Adhérents", ago(hours=1, minutes=40), "vert", "Run vert en 36 min, sans relance.", "green", 36, REPORT),
            watch(0, "firm", INTEGRATION, "Adhérents", ago(hours=22), "en échec ou incomplet",
                  "load_firm en échec : contrainte d'unicité, erreur non passagère (pas de relance).", "ko", 22),
        ]
        running, user, firm, failed = self.watches
        self.chains = [
            DagChain(FIRST_ID + 1, DEMO_SLUG, DEMO, STACK, [
                Step(firm.dag_id, GREEN, firm.id), Step(user.dag_id, GREEN, user.id), Step(running.dag_id, RUNNING, running.id),
                Step("demo_client_vaccine_injection", WAITING)], firm.started_at),
            DagChain(FIRST_ID, DEMO_SLUG, DEMO, INTEGRATION, [
                Step(failed.dag_id, failed.status, failed.id, failed.summary), Step("demo_client_employer_user", SKIPPED)],
                failed.started_at, "arrêtée à l'étape 1", failed.ended_at),
        ]
        self.dag_runs = [DagRun(w.dag_id, w.padoa_env, w.client, w.card, w.started_at, f"Run créé : {w.run_id}", False) for w in self.watches]
        self._ids = {id(o) for o in (*self.runs, *self.watches, *self.chains, *self.dag_runs)}

    def __contains__(self, item) -> bool:
        return id(item) in self._ids

    def run(self, run_id: int) -> AgentRun | None:
        return next((r for r in self.runs if r.id == run_id), None)

    def watch(self, watch_id: int) -> DagWatch | None:
        return next((w for w in self.watches if w.id == watch_id), None)

    def agent_log(self, run: AgentRun) -> Path:
        return self.folder / f"{run.id}.jsonl"

    def watch_log(self, w: DagWatch) -> Path:
        return self.folder / f"suivi-{w.id}.log"

    def watch_report(self, w: DagWatch) -> Path:
        return self.folder / report_name(w.dag_id, w.run_id)
