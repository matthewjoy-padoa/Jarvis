"""Console Jarvis : une ligne de commande → du texte de terminal (HTML échappé), sur le client en cours.

Les commandes lisent seulement. Celles qui agissent (lancer un DAG, rafraîchir depuis Notion) ne font rien elles-mêmes :
elles renvoient une confirmation « (o/N) » ; sur « o », le navigateur appelle la route d'action existante
(/actions/injection, /actions/rafraichir), avec ses garde-fous (catalogue, prod désactivée, suivi air.py).
"""

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from html import escape

from deploy_hub.models import DONE, KO, Deployment
from deploy_hub.search import fold

LIMIT = 15  # lignes affichées par liste, le reste est compté

COMMANDS = {  # nom : (arguments, description) — dans l'ordre de « aide »
    "aide": ("[commande]", "Liste des commandes, ou le détail de l'une d'elles"),
    "client": ("[nom]", "Affiche le client en cours, ou en change"),
    "etat": ("", "Avancement, échéance, retours ouverts, injections du client"),
    "coince": ("", "Les 5 points avec le plus de KO (blocs d'injection, types de retours)"),
    "retards": ("", "Actions et retours en retard"),
    "cherche": ("<mots>", "Cherche dans le client : retours, actions, injections, règles"),
    "dags": ("[mots]", "DAGs d'injection du client (catalogue de l'orchestrator)"),
    "lancer": ("<dag> <stack>", "Lance un DAG sur integration, snap ou recette, après confirmation"),
    "agent": ("<retour>", "Ouvre le retour avec le bloc « Lancer un agent » déplié"),
    "rafraichir": ("", "Relit le dashboard Notion du client, après confirmation"),
    "suivis": ("", "Suivis des runs lancés (air.py) : état et dernière nouvelle"),
    "ouvrir": ("<onglet>", "Ouvre un onglet de la fiche : synthese, injections, actions, recette…"),
    "historique": ("", "Les dernières commandes tapées"),
    "effacer": ("", "Vide l'écran (Ctrl+L)"),
    "quitter": ("", "Ferme la console (Échap)"),
}
ALIASES = {"help": "aide", "?": "aide", "status": "etat", "état": "etat", "clear": "effacer", "cls": "effacer",
           "exit": "quitter", "q": "quitter", "search": "cherche", "find": "cherche", "dag": "dags", "run": "lancer",
           "refresh": "rafraichir", "rafraîchir": "rafraichir", "open": "ouvrir", "history": "historique"}
CLIENT_FREE = {"aide", "client", "historique", "effacer", "quitter", "suivis"}  # utilisables sans client en cours


@dataclass
class Context:
    current: Deployment | None
    deployments: list[Deployment]
    today: date
    search: Callable  # Index.search
    deadlines: Callable  # main._deadlines
    dags: Callable  # dag_catalogue.dags_for
    watches: list
    envs: tuple[str, ...]
    ticket_url: Callable
    tabs: list[tuple[str, str]]


@dataclass
class Output:
    lines: list[str] = field(default_factory=list)  # HTML, une entrée par ligne de terminal
    client: str = ""  # nouveau client en cours (slug)
    confirm: dict | None = None  # {"question", "url", "data"} : action sur « o »
    navigate: str = ""
    ticket: bool = False  # navigate s'ouvre dans la fenêtre de ticket
    clear: bool = False
    close: bool = False
    history: bool = False  # la liste vient du navigateur

    def say(self, text: str = "", cls: str = "", href: str = "", ticket: bool = False) -> "Output":
        body = escape(text)
        if href:
            body = f'<a href="{escape(href)}"{" data-ticket" if ticket else ""}>{body}</a>'
        self.lines.append(f'<div class="t-line {cls}">{body or "&nbsp;"}</div>')
        return self

    def table(self, headers: list[str], rows: list[tuple[list[str], str, str, bool]], total: int | None = None) -> "Output":
        """rows : (cellules, classe, lien, ticket). Colonnes alignées comme dans un terminal, chaque ligne cliquable."""
        widths = [max(len(h), *(len(r[0][i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
        cut = lambda cells: "  ".join(c[:60].ljust(min(w, 60)) for c, w in zip(cells, widths)).rstrip()
        self.say(cut(headers), "t-head")
        for cells, cls, href, ticket in rows:
            self.say(cut(cells), cls, href, ticket)
        if total is not None and total > len(rows):
            self.say(f"… et {total - len(rows)} de plus", "t-dim")
        return self


def _prefix_match(word: str, names: list[str]) -> list[str]:
    word = fold(word)
    exact = [n for n in names if fold(n) == word]
    return exact or [n for n in names if fold(n).startswith(word)]


def run(line: str, ctx: Context) -> Output:
    out = Output()
    words = line.strip().split()
    if not words:
        return out
    name = ALIASES.get(fold(words[0]), fold(words[0]))
    args = words[1:]
    if name not in COMMANDS:
        close = difflib.get_close_matches(name, list(COMMANDS), n=1)
        return out.say(f"{words[0]} : commande inconnue." + (f" Tu voulais dire « {close[0]} » ?" if close else ""), "t-ko") \
                  .say("Tape « aide » pour la liste des commandes.", "t-dim")
    if name not in CLIENT_FREE and not ctx.current:
        return out.say("Aucun client en cours : « client <nom> » pour en choisir un.", "t-ko")
    return HANDLERS[name](args, ctx, out)


# ---------------------------------------------------------------- commandes
def _aide(args, ctx, out):
    if args:
        name = ALIASES.get(fold(args[0]), fold(args[0]))
        if name not in COMMANDS:
            return out.say(f"{args[0]} : commande inconnue.", "t-ko")
        syntax, about = COMMANDS[name]
        return out.say(f"{name} {syntax}".strip(), "t-accent").say(about)
    out.say("Commandes (Tab complète, ↑↓ rappelle, Ctrl+L efface, Ctrl+C annule, Échap ferme) :", "t-dim")
    for name, (syntax, about) in COMMANDS.items():
        out.say(f"  {(name + ' ' + syntax).strip():<26}{about}")
    return out


def _client(args, ctx, out):
    if not args:
        if ctx.current:
            d = ctx.current
            left = f" · J-{d.days_left} · déploiement le {d.go_live:%d/%m/%Y}" if d.go_live and d.days_left >= 0 else ""
            out.say(f"Client en cours : {d.client} ({d.software}){left}", "t-accent")
        others = [d for d in ctx.deployments if d is not ctx.current]
        for d in others:
            out.say(f"  {d.slug:<20}{d.client}" + (f" · J-{d.days_left}" if d.go_live and d.days_left >= 0 else ""), "t-dim")
        return out.say("« client <nom> » pour changer.", "t-dim") if others else out
    wanted = " ".join(args)
    found = [d for d in ctx.deployments if fold(wanted) in (fold(d.slug), fold(d.client))] or \
        [d for d in ctx.deployments if fold(d.slug).startswith(fold(wanted)) or fold(d.client).startswith(fold(wanted))]
    if len(found) != 1:
        return out.say(f"{wanted} : {'aucun client' if not found else 'plusieurs clients'} ne répond.", "t-ko")
    out.client = found[0].slug
    return out.say(f"Client en cours : {found[0].client}", "t-ok")


def _etat(args, ctx, out):
    d = ctx.current
    out.say(f"{d.client} · {d.software} · {d.health_label}", "t-accent")
    if d.go_live:
        out.say(f"Déploiement   {d.go_live:%d/%m/%Y}" + (f"  (J-{d.days_left})" if d.days_left >= 0 else "  (passé)"))
    expected = f"  (attendu {d.expected_progress} %)" if d.expected_progress is not None else ""
    out.say(f"Avancement    {d.progress} % intégré{expected}")
    out.say(f"Recette       {len(d.open_retours)} retour(s) ouvert(s) sur {len(d.retours)}")
    groups = d.injection_groups
    out.say(f"Injections    {groups.get('todo', 0)} à faire · {groups.get('in_progress', 0)} en cours · "
            f"{groups.get(KO, 0)} KO · {groups.get(DONE, 0)} terminée(s)", "t-ko" if groups.get(KO) else "")
    late = [i for i in ctx.deadlines([d]) if i["kind"] != "Jalon" and i["date"] < ctx.today]
    return out.say(f"En retard     {len(late)} action(s) ou retour(s)", "t-ko" if late else "t-ok")


def _coince(args, ctx, out):
    points = ctx.current.blocking_points
    if not points:
        return out.say("Aucun point bloquant : aucun KO en injection ni en recette.", "t-ok")
    rows = [([str(p["counts"][KO]), f"{p['open']}/{p['total']}", "injection" if p["kind"] == "injection" else "recette", p["label"]],
             "t-ko", p["href"], False) for p in points[:5]]
    return out.table(["KO", "ouverts", "type", "point"], rows, len(points))


def _retards(args, ctx, out):
    late = [i for i in ctx.deadlines([ctx.current]) if i["kind"] != "Jalon" and i["date"] < ctx.today]
    if not late:
        return out.say("Rien en retard.", "t-ok")
    late.sort(key=lambda i: i["date"])
    rows = [([i["date"].strftime("%d/%m"), f"J+{(ctx.today - i['date']).days}", i["kind"].lower(), i["label"]],
             "", i["url"] or "", "/ticket/" in (i["url"] or "")) for i in late[:LIMIT]]
    return out.table(["date", "retard", "type", "quoi"], rows, len(late))


def _cherche(args, ctx, out):
    if not args:
        return out.say("Usage : cherche <mots>", "t-ko")
    results = ctx.search(f"{ctx.current.slug} {' '.join(args)}")
    hits = [h.item for _, _, group in results.groups for h in group if h.item.kind not in ("page", "client")]
    if not hits:
        return out.say(f"Rien pour « {' '.join(args)} » chez {ctx.current.client}.", "t-dim")
    rows = [([i.kind, i.badge or "", i.title], "t-ko" if i.group == KO else "", i.url, i.ticket) for i in hits[:LIMIT]]
    return out.table(["type", "statut", "titre"], rows, results.total)


def _client_dags(ctx, words):
    choices = ctx.dags(ctx.current.orchestrator)
    wanted = [fold(w) for w in words]
    return [c for c in choices if all(w in fold(f"{c.dag_id} {c.label} {' '.join(c.resources)}") for w in wanted)]


def _dags(args, ctx, out):
    choices = _client_dags(ctx, args)
    if not choices:
        return out.say("Aucun DAG ne répond" + (f" à « {' '.join(args)} »" if args else " (clés orchestrator du client ?)") + ".", "t-dim")
    rows = [([c.dag_id, ", ".join(c.resources)], "", "", False) for c in choices[:LIMIT * 2]]
    out.table(["dag", "ressources"], rows, len(choices))
    return out.say("« lancer <dag> <stack> » pour en lancer un.", "t-dim")


def _lancer(args, ctx, out):
    if len(args) != 2:
        return out.say("Usage : lancer <dag> <stack>   (stack : " + ", ".join(ctx.envs) + ")", "t-ko")
    target, stack = args[0], fold(args[1])
    if stack not in ctx.envs:
        why = "la prod est désactivée dans Jarvis" if stack == "prod" else f"stacks possibles : {', '.join(ctx.envs)}"
        return out.say(f"{args[1]} : stack refusée ({why}).", "t-ko")
    choices = ctx.dags(ctx.current.orchestrator)
    found = [c for c in choices if c.dag_id == target] or [c for c in choices if fold(target) in fold(c.dag_id)]
    if len(found) != 1:
        out.say(f"{target} : {'aucun DAG' if not found else f'{len(found)} DAGs'} ne répond{'ent' if len(found) > 1 else ''}.", "t-ko")
        for c in found[:8]:
            out.say(f"  {c.dag_id}", "t-dim")
        return out
    dag = found[0]
    out.say(f"{dag.dag_id}", "t-accent").say(f"  ressources : {', '.join(dag.resources)}", "t-dim")
    out.confirm = {"question": f"Lancer {dag.dag_id} sur {stack} ? (o/N)",
                   "url": f"/actions/injection/{ctx.current.slug}", "data": {"dag_id": dag.dag_id, "padoa_env": stack, "card": "Console Jarvis"}}
    return out


def _agent(args, ctx, out):
    if not args:
        return out.say("Usage : agent <retour>", "t-ko")
    ref = args[0].upper()
    retour = next((r for r in ctx.current.retours if r.id.upper() == ref), None)
    if retour is None:
        return out.say(f"{args[0]} : retour introuvable chez {ctx.current.client}.", "t-ko")
    if not retour.is_open:
        return out.say(f"{retour.id} est clos ({retour.status}) : pas d'agent.", "t-ko")
    out.navigate, out.ticket = ctx.ticket_url(ctx.current.slug, "retour", retour.id) + "?agent=1", True
    return out.say(f"Ouverture de {retour.id} · bloc « Lancer un agent » déplié.", "t-ok")


def _rafraichir(args, ctx, out):
    out.confirm = {"question": f"Relire le dashboard Notion de {ctx.current.client} ? (o/N)",
                   "url": f"/actions/rafraichir/{ctx.current.slug}", "data": {}}
    return out


def _suivis(args, ctx, out):
    if not ctx.watches:
        return out.say("Aucun suivi : ils démarrent avec les lancements réels.", "t-dim")
    cls = {"vert": "t-ok", "suivi en cours": "t-accent"}
    rows = [([f"#{w.id}", w.status, w.padoa_env, w.dag_id, (w.summary or "")[:60]], cls.get(w.status, "t-ko"), f"/agents/suivis/{w.id}", False)
            for w in ctx.watches[:LIMIT]]
    return out.table(["suivi", "état", "stack", "dag", "dernière nouvelle"], rows, len(ctx.watches))


def _ouvrir(args, ctx, out):
    tabs = dict(ctx.tabs)
    if not args:
        return out.say("Usage : ouvrir <onglet>   (" + ", ".join(tabs) + ")", "t-ko")
    found = _prefix_match(args[0], list(tabs))
    if len(found) != 1:
        return out.say(f"{args[0]} : onglet inconnu ({', '.join(tabs)}).", "t-ko")
    out.navigate = f"/deploiements/{ctx.current.slug}/{found[0]}"
    return out.say(f"Ouverture de {ctx.current.client} · {tabs[found[0]]}.", "t-ok")


def _historique(args, ctx, out):
    out.history = True
    return out


def _effacer(args, ctx, out):
    out.clear = True
    return out


def _quitter(args, ctx, out):
    out.close = True
    return out


HANDLERS = {"aide": _aide, "client": _client, "etat": _etat, "coince": _coince, "retards": _retards, "cherche": _cherche,
            "dags": _dags, "lancer": _lancer, "agent": _agent, "rafraichir": _rafraichir, "suivis": _suivis, "ouvrir": _ouvrir,
            "historique": _historique, "effacer": _effacer, "quitter": _quitter}
