import asyncio
import calendar
import dataclasses
import logging
import os
import re
from contextlib import asynccontextmanager
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from deploy_hub import client_config
from deploy_hub.config import PROCESS_KEYS, PROJECT_ROOT, clients_dir, data_dir, ensure_private_dirs, forget_secrets, load_settings, secret_problem
from deploy_hub.models import (ACTION_STATUSES, DONE, INJECTION_STATUSES, MILESTONE_LABELS, RETOUR_STATUSES, Deployment,
                               kanban_columns, notion_link, status_group)
from deploy_hub import brief, console, env_file, onepassword, shell
from deploy_hub.search import Index
from deploy_hub.sources import ActionResult
from deploy_hub.sources import dags as dag_catalogue
from deploy_hub.sources.airflow import CONFIRM_PROD, PADOA_ENVS, AirflowSource
from deploy_hub.sources.claude_runner import TO_FILL, ClaudeRunner, duration_text, retour_prompt, usage
from deploy_hub.sources.dag_chain import MAX_STEPS, ChainRunner
from deploy_hub.sources.dag_watch import DagWatcher
from deploy_hub.sources import connections, flow_digger, notion_api
from deploy_hub.sources.external import external_statuses
from deploy_hub.sources.journal import changes_since, group_changes
from deploy_hub.sources import watchers
from deploy_hub.sources.notifications import NotificationStore
from deploy_hub.sources.personal import PersonalStore, remind_date
from deploy_hub.sources.pulls import PullRequests
from deploy_hub.sources import review as agent_review
from deploy_hub.sources.fiches import FicheSource

HERE = Path(__file__).parent

ensure_private_dirs()  # données et configuration des clients jamais dans un dossier que git pourrait pousser
settings = load_settings()
live = settings.mode == "live"
fiches = FicheSource(settings.deployments_dir, clients_dir())
airflow = AirflowSource.from_env()
notifications = NotificationStore(Path(os.getenv("DEPLOY_HUB_NOTIFICATIONS") or settings.deployments_dir.parent / "notifications.json"))
claude = ClaudeRunner.from_env(settings.orchestrator_dir, live, notifications, data_dir() / "agents")
pulls = PullRequests(settings.orchestrator_dir)  # PR ouvertes de l'orchestrator (gh), gardées cinq minutes
# Suivi des runs lancés (air.py d'Arthur) ; les suivis en cours au démarrage reprennent si Airflow est configuré.
dag_watch = DagWatcher(data_dir() / "airflow", notifications, {"AIRFLOW_URL": airflow.base_url, "AIRFLOW_USER": airflow.user,
                                                               "AIRFLOW_PASSWORD": airflow.password} if airflow.live else None)
# Enchaînements de DAG : le suivant part quand le suivi du précédent finit vert. Passe par le dag_watch du moment.
dag_chains = ChainRunner(data_dir() / "airflow" / "chaines.json",
                         lambda dag_id, padoa_env, slug, card: _launch_watched(dag_id, padoa_env, fiches.get_deployment(slug), card), notifications)
dag_watch.on_end = dag_chains.watch_ended
personal = PersonalStore(data_dir() / "perso.json")  # épingles, notes et rappels : à moi seul, jamais dans Notion
_refresh_lock = threading.Lock()  # un seul rafraîchissement Notion à la fois

log = logging.getLogger("uvicorn.error")  # mêmes lignes que celles de uvicorn


def _airflow_problem() -> str:
    """Raison pour laquelle les identifiants Airflow (op://) n'ont pas pu être lus ; "" sinon."""
    return " ; ".join(f"{n} : {e}" for n in ("AIRFLOW_USER", "AIRFLOW_PASSWORD") if (e := secret_problem(n)))


@asynccontextmanager
async def lifespan(_app):
    # Au lancement : Airflow et Notion sont-ils vraiment joignables ? (une lecture chacun, dans le terminal de Jarvis)
    checks = await asyncio.gather(
        asyncio.to_thread(connections.airflow_status, airflow, _airflow_problem()),
        asyncio.to_thread(connections.notion_status, notion_api.load_token(), secret_problem("NOTION_TOKEN")))
    for line in connections.lines(*checks):
        log.info(line)
    await asyncio.to_thread(dag_chains.reconcile, lambda watch_id: dag_watch.get(watch_id))
    yield
    dag_watch.shutdown()  # à l'arrêt (rechargement compris) : coupe les air.py, que le Jarvis suivant reprendra


app = FastAPI(title="Jarvis", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["duree"] = duration_text
templates.env.globals["status_group"] = lambda s: status_group(s, INJECTION_STATUSES)
templates.env.globals["kanban_columns"] = kanban_columns
templates.env.globals["ACTION_STATUSES"] = ACTION_STATUSES
templates.env.globals["RETOUR_STATUSES"] = RETOUR_STATUSES
templates.env.globals["console_commands"] = list(console.COMMANDS)  # complétion par Tab dans la console

# Pages globales et onglets d'une fiche, calqués sur les blocs du dashboard Notion.
# On travaille sur un client à la fois : la barre du haut montre le client en cours (sélecteur) et NAV_PAGES ;
# « Clients » (tous les clients, réglages) se rejoint depuis le sélecteur. PAGES reste la liste complète (palette ⌘K).
PAGES = [
    ("/", "Accueil"),
    ("/clients", "Clients"),
    ("/deadlines", "Deadlines"),
    ("/agents", "Agents"),
]
NAV_PAGES = [p for p in PAGES if p[0] != "/clients"]
CLIENT_COOKIE = "jarvis_client"  # client en cours : celui de la dernière fiche ouverte
# Vues tous clients, proposées sur la page Clients (et toujours joignables par leur URL).
CROSS_PAGES = [
    ("/injections", "Injections"),
    ("/actions", "Actions"),
    ("/recette", "Recette"),
]  # actives sous « Clients »
DEPLOYMENT_TABS = [
    ("synthese", "Synthèse"),
    ("injections", "Injections"),
    ("actions", "Actions"),
    ("recette", "Recette"),
    ("pr", "PR"),
    ("regles", "Règles"),
    ("accompagnement", "Accompagnement"),
]


def _deployments(request: Request) -> list[Deployment]:
    """Toutes les fiches, lues une seule fois par requête (menu et contenu de la page partagent la même liste)."""
    if not hasattr(request.state, "deployments"):
        request.state.deployments = fiches.list_deployments()
    return request.state.deployments


def _closest_go_live(d: Deployment) -> tuple:
    """Ordre par défaut : le déploiement à venir le plus proche d'abord, puis les autres."""
    upcoming = bool(d.go_live) and d.days_left >= 0
    return (not upcoming, d.days_left if upcoming else 0, d.client)


def _current(request: Request) -> Deployment | None:
    """Client en cours : celui du cookie (dernière fiche ouverte), sinon le déploiement le plus proche."""
    if not hasattr(request.state, "current"):
        deployments = _deployments(request)
        slug = request.cookies.get(CLIENT_COOKIE)
        request.state.current = next((d for d in deployments if d.slug == slug), None) or \
            (min(deployments, key=_closest_go_live) if deployments else None)
    return request.state.current


def _remember(response, slug: str):
    response.set_cookie(CLIENT_COOKIE, slug, max_age=365 * 24 * 3600, samesite="lax")
    return response


def _render(request: Request, template: str, active: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {
            "pages": NAV_PAGES,
            "current": _current(request),
            "shell_token": shell.TOKEN if shell.enabled() else "",  # onglet « Shell » de la console (JARVIS_SHELL=1, VM)
            "cross_pages": CROSS_PAGES,
            "clients": _deployments(request),
            "active": active,
            "mode": settings.mode,
            "airflow": airflow,
            "dags_for": dag_catalogue.dags_for,
            "ced_for": dag_catalogue.ced_for,
            "padoa_envs": PADOA_ENVS,
            "notion_ready": notion_api.load_token() is not None,
            "today": date.today(),
            **context,
        },
    )


def _get(slug: str) -> Deployment:
    deployment = fiches.get_deployment(slug)
    if deployment is None:
        raise HTTPException(404, f"Déploiement inconnu : {slug}")
    return deployment


def _deadlines(deployments: list[Deployment]) -> list[dict]:
    """Jalons, actions non terminées et retours de recette ouverts, triés par date."""
    items = []
    for d in deployments:
        items += [{"date": v, "kind": "Jalon", "label": MILESTONE_LABELS.get(k, k), "d": d, "url": f"/deploiements/{d.slug}/synthese"}
                  for k, v in d.milestones.items()]
        items += [{"date": a.due, "kind": "Action", "label": a.title, "d": d, "url": ticket_url(d.slug, "action", a.ref) if a.ref else a.notion_url, "action": a}
                  for a in d.actions if a.due and a.group != DONE]
        items += [{"date": r.due, "kind": "Retour", "label": r.label, "d": d, "url": ticket_url(d.slug, "retour", r.id), "retour": r}
                  for r in d.open_retours if r.due]
    return sorted(items, key=lambda i: i["date"])


def _result(request: Request, result: ActionResult) -> HTMLResponse:
    return templates.TemplateResponse(request, "_action_result.html", {"result": result})


PERIODS = {"retard": "En retard", "aujourdhui": "À échéance aujourd'hui", "semaine": "Dans les 7 jours"}


def _in_period(item: dict, periode: str, today: date) -> bool:
    """Périodes de l'accueil (jalons exclus) : mêmes règles pour ses compteurs et pour la liste qu'ils ouvrent."""
    if item["kind"] == "Jalon":
        return False
    return {"retard": item["date"] < today, "aujourdhui": item["date"] == today,
            "semaine": today < item["date"] <= today + timedelta(days=7)}[periode]


def _tile_link(items: list, list_url: str, ticket_of=lambda i: i.get("url")) -> dict | None:
    """Où mène un compteur : le ticket lui-même s'il n'y en a qu'un (ouvert dans le panneau), sinon la liste filtrée."""
    if not items:
        return None
    url = ticket_of(items[0]) if len(items) == 1 else None
    if url and "/ticket/" in url:
        return {"href": url, "ticket": True}
    return {"href": list_url, "ticket": False}


@app.get("/", response_class=HTMLResponse)
def today_page(request: Request):
    """Ce qui demande de l'attention aujourd'hui chez le client en cours : retards, échéances, KO, jalons, « Quoi de neuf »."""
    current = _current(request)
    deployments = [current] if current else []
    today = date.today()
    items = _deadlines(deployments)
    by_period = {p: [i for i in items if _in_period(i, p, today)] for p in PERIODS}
    ko = [(d, a) for d in deployments for a in d.injection_cards if a.injection_group == "ko"]
    ko_url = f"/deploiements/{current.slug}/injections?vue=liste#groupe=ko" if current else ""
    links = {p: _tile_link(found, f"/deadlines?periode={p}") for p, found in by_period.items()}
    links["ko"] = _tile_link(ko, ko_url, lambda da: ticket_url(da[0].slug, "action", da[1].ref) if da[1].ref else None)
    return _render(
        request,
        "today.html",
        "/",
        now=datetime.now(),
        deployments=deployments,
        late=by_period["retard"],
        due_today=by_period["aujourdhui"],
        due_week=by_period["semaine"],
        ko=ko,
        links=links,
        ko_url=ko_url,
        milestones=[i for i in items if i["kind"] == "Jalon" and today <= i["date"] <= today + timedelta(days=21)],
        pins=_pinned(current) if current else [],
        **_reminders_context(current.slug if current else ""),
    )


_index: tuple[tuple, Index] | None = None  # index de la palette, refait quand une fiche, le jour ou un agent change


def _search_index(deployments: list[Deployment]) -> Index:
    """Les fiches sont gardées en mémoire par FicheSource : tant que ce sont les mêmes objets, l'index reste valable.
    La clé compare des id() : sûr parce que l'Index garde ces fiches en vie (self.deployments), Python ne peut pas réutiliser leurs id."""
    global _index
    key = (tuple(id(d) for d in deployments), date.today(), tuple((r.id, r.status) for r in claude.runs))
    if _index is None or _index[0] != key:
        _index = (key, Index(deployments, claude.runs, date.today(), PAGES + CROSS_PAGES, DEPLOYMENT_TABS, ticket_url))
    return _index[1]


@app.get("/palette", response_class=HTMLResponse)
def palette(request: Request, q: str = ""):
    """Résultats de la palette de commandes (⌘K) : pages, clients, retours, cartes, règles et agents qui répondent à q."""
    return templates.TemplateResponse(request, "_palette.html", {"r": _search_index(_deployments(request)).search(q), "today": date.today()})


@app.post("/console")
def console_run(request: Request, ligne: str = Form("")):
    """Une ligne de la console : texte de terminal sur le client en cours ; une action passe par une confirmation (o/N)
    puis par sa route existante, appelée par le navigateur."""
    deployments = _deployments(request)
    ctx = console.Context(_current(request), deployments, date.today(), _search_index(deployments).search, _deadlines,
                          dag_catalogue.dags_for, dag_watch.watches, tuple(e for e in PADOA_ENVS if e != "prod"), ticket_url, DEPLOYMENT_TABS)
    out = console.run(ligne, ctx)
    slug = out.client or (ctx.current.slug if ctx.current else "")
    response = JSONResponse({"lines": out.lines, "prompt": f"jarvis:{slug or '~'}", "confirm": out.confirm, "navigate": out.navigate,
                             "ticket": out.ticket, "clear": out.clear, "close": out.close, "history": out.history})
    return _remember(response, out.client) if out.client else response


@app.websocket("/terminal/ws")
async def terminal_ws(websocket: WebSocket):
    """Shell de la VM dans la console (shell.py) : refusé sans JARVIS_SHELL=1, hors connexion locale, origine ou jeton."""
    await shell.bridge(websocket)


@app.get("/palette/dag/{slug}/{dag_id}", response_class=HTMLResponse)
def palette_dag(request: Request, slug: str, dag_id: str):
    """Formulaire de lancement ouvert par la commande « >dag » : stack de test au choix, confirmation par dag.js, envoi à /actions/injection."""
    d = _get(slug)
    choice = next((c for c in dag_catalogue.dags_for(d.orchestrator) if c.dag_id == dag_id), None)
    if choice is None:
        raise HTTPException(404, f"{dag_id} n'est pas un DAG de {d.client} dans le catalogue")
    return templates.TemplateResponse(request, "_palette_dag.html", {"d": d, "c": choice, "envs": [e for e in PADOA_ENVS if e != "prod"]})


@app.get("/quoi-de-neuf", response_class=HTMLResponse)
def whats_new(request: Request, depuis: str = "", client: str = ""):
    """Changements notés par la synchro depuis une date (par défaut 24 h), un client ou tous."""
    try:
        since = datetime.fromisoformat(depuis.replace("Z", "+00:00")).astimezone().replace(tzinfo=None) if depuis else None
    except ValueError:
        since = None
    since = since or datetime.now() - timedelta(days=1)
    deployments = [_get(client)] if client else _deployments(request)
    changes = sorted((c for d in deployments for c in changes_since(d, settings.deployments_dir, since)), key=lambda c: c.when, reverse=True)
    return templates.TemplateResponse(request, "_whats_new.html", {"changes": changes, "days": group_changes(changes), "since": since,
                                                                    "show_client": not client})


@app.get("/clients", response_class=HTMLResponse)
def index(request: Request, enregistre: str = "", retire: str = ""):
    deployments = _deployments(request)
    synced = {d.slug for d in deployments}
    pending = [c for slug in client_config.slugs() if slug not in synced and (c := client_config.load(slug))]
    return _render(request, "index.html", "/clients", deployments=deployments, pending=pending, saved=enregistre, retired=retire)


def _client_form(request: Request, cfg, error: str = "", new: bool = True, form: dict | None = None) -> HTMLResponse:
    suggestions = sorted(dag_catalogue.catalogue()["clients"])
    return _render(request, "client_form.html", "/clients", cfg=cfg, error=error, new=new, form=form or {}, suggestions=suggestions)


@app.get("/clients/nouveau", response_class=HTMLResponse)
def client_new(request: Request):
    return _client_form(request, None)


def _config_or_fiche(slug: str):
    """Configuration du client, ou, pour une fiche qui n'en a pas encore, une configuration de départ tirée de la fiche."""
    cfg = client_config.load(slug)
    if cfg is not None:
        return cfg, ""
    return client_config.ClientConfig.from_deployment(_get(slug)), "Ce client n'a pas encore de configuration : renseigne au moins sa fiche Notion."


@app.get("/clients/{slug}/configuration", response_class=HTMLResponse)
def client_edit(request: Request, slug: str):
    cfg, notice = _config_or_fiche(slug)
    return _client_form(request, cfg, notice, new=False)


def _local_path(value: str, default: str) -> str:
    """Page de retour : seulement un chemin de Jarvis (jamais une adresse externe ni « //hôte »)."""
    return value if value.startswith("/") and not value.startswith("//") else default


def _links_page(request: Request, cfg, vers: str, error: str = "", form: dict | None = None) -> HTMLResponse:
    return _render(request, "client_links.html", "/clients", cfg=cfg, vers=vers, error=error, form=form or {})


@app.get("/clients/{slug}/liens", response_class=HTMLResponse)
def client_links(request: Request, slug: str, vers: str = ""):
    """Seulement les liens du client (Drive, dashboard Notion, stacks) : ce qu'on ajoute le plus souvent."""
    cfg = client_config.load(slug)
    if cfg is None:  # pas encore de configuration : il faut d'abord sa fiche Notion
        return RedirectResponse(f"/clients/{slug}/configuration", status_code=303)
    return _links_page(request, cfg, _local_path(vers, f"/deploiements/{slug}/synthese"))


@app.post("/clients/{slug}/liens", response_class=HTMLResponse)
async def client_links_save(request: Request, slug: str):
    cfg = client_config.load(slug)
    if cfg is None:
        raise HTTPException(404, f"Client sans configuration : {slug}")
    form = dict(await request.form())
    vers = _local_path(str(form.get("vers") or ""), f"/deploiements/{slug}/synthese")
    try:
        dashboard = str(form.get("notion_url") or "").strip()
        if dashboard and not dashboard.startswith(("http://", "https://")):
            raise client_config.ConfigError("Le lien du dashboard Notion doit être une adresse https://")
        updated = dataclasses.replace(cfg, notion_url=dashboard, drive=client_config.parse_drive(str(form.get("drive") or "")),
                                      stacks=client_config.parse_stacks(str(form.get("stacks") or "")))
        client_config.save(updated)
    except client_config.ConfigError as e:
        return _links_page(request, cfg, vers, str(e), form)
    return RedirectResponse(vers, status_code=303)


@app.post("/clients/enregistrer", response_class=HTMLResponse)
async def client_save(request: Request):
    form = dict(await request.form())
    existing = (form.get("existing_slug") or "").strip() or None
    try:
        cfg = client_config.from_form(form, existing)
        client_config.save(cfg, new=existing is None)
    except client_config.ConfigError as e:
        return _client_form(request, _config_or_fiche(existing)[0] if existing else None, str(e), new=existing is None, form=form)
    return RedirectResponse(f"/clients?enregistre={cfg.slug}", status_code=303)


@app.post("/clients/{slug}/retirer")
def client_retire(slug: str):
    try:
        client_config.retire(slug)
    except client_config.ConfigError as e:
        raise HTTPException(404, str(e))
    return RedirectResponse(f"/clients?retire={slug}", status_code=303)


@app.get("/injections", response_class=HTMLResponse)
def injections(request: Request, vue: str = "ressources"):
    return _render(request, "list.html", "/injections", title="Injections", part="injections", deployments=_deployments(request), vue=vue)


@app.get("/actions", response_class=HTMLResponse)
def actions(request: Request, tous: bool = False, vue: str = "tableau"):
    return _render(
        request, "list.html", "/actions", title="Actions", part="actions", deployments=_deployments(request), show_all=tous, vue=vue
    )


@app.get("/recette", response_class=HTMLResponse)
def recette(request: Request, tous: bool = False, vue: str = "liste"):
    return _render(
        request, "list.html", "/recette", title="Retours de recette", part="recette", deployments=_deployments(request), show_all=tous,
        vue=vue,
    )


def _month(value: str, today: date) -> date:
    """Premier jour du mois demandé (AAAA-MM), sinon du mois en cours."""
    try:
        year, month = (int(x) for x in value.split("-"))
        return date(year, month, 1)
    except ValueError:
        return today.replace(day=1)


@app.get("/deadlines", response_class=HTMLResponse)
def deadlines(request: Request, tous: bool = False, client: str = "", periode: str = "", vue: str = "liste", mois: str = ""):
    """Échéances en liste ou en calendrier. client : un slug, ou « tous » (tous=true : idem) ; vide = client en cours.
    periode : retard, aujourdhui ou semaine (compteurs de l'accueil) ; mois (AAAA-MM) : mois affiché par le calendrier."""
    current = _current(request)
    everyone = _deployments(request)
    chosen = next((d for d in everyone if d.slug == client), None) if client and client != "tous" else None
    if chosen is None and (tous or client == "tous" or not current):
        deployments, selected = everyone, "tous"
    else:
        deployments, selected = [chosen or current], (chosen or current).slug
    items = _deadlines(deployments)
    today = date.today()
    if periode in PERIODS:
        items = [i for i in items if _in_period(i, periode, today)]
    vue = "calendrier" if vue == "calendrier" else "liste"
    first = _month(mois, today)
    by_day: dict[date, list] = {}
    for i in items:
        by_day.setdefault(i["date"], []).append(i)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    prev_month = (first - timedelta(days=1)).replace(day=1)
    next_month = (first + timedelta(days=32)).replace(day=1)
    return _render(request, "deadlines.html", "/deadlines", items=items, all_clients=selected == "tous", selected=selected,
                   clients=sorted(everyone, key=lambda d: d.client.lower()), periode=periode if periode in PERIODS else "",
                   periods=PERIODS, vue=vue, month=first, weeks=weeks, by_day=by_day, prev_month=prev_month, next_month=next_month)


@app.get("/agents", response_class=HTMLResponse)
def agents(request: Request):
    return _render(request, "agents.html", "/agents", runs=claude.runs, dag_runs=airflow.runs, watches=dag_watch.watches, chains=dag_chains.chains, live=claude.live,
                   usage=usage(claude.runs))


def _run_or_404(run_id: int):
    run = claude.get(run_id)
    if not run:
        raise HTTPException(404, f"Agent #{run_id} inconnu")
    return run


def _agent_context(run) -> dict:
    """Journal de l'agent, et sa relecture guidée dès qu'il a fini (un agent simulé n'a rien à relire)."""
    path = claude.log_path(run)
    return {"run": run, "log": claude.log(run), "review": None if run.running or run.simulated else agent_review.build(run, path)}


@app.get("/agents/{run_id}", response_class=HTMLResponse)
def agent_detail(request: Request, run_id: int):
    run = _run_or_404(run_id)
    return _render(request, "agent.html", "/agents", **_agent_context(run))


@app.get("/agents/{run_id}/journal", response_class=HTMLResponse)
def agent_journal(request: Request, run_id: int):
    """Rafraîchi toutes les 5 s par la page de l'agent tant qu'il travaille."""
    run = _run_or_404(run_id)
    return templates.TemplateResponse(request, "_agent_log.html", _agent_context(run))


@app.post("/agents/{run_id}/arreter")
def agent_stop(run_id: int):
    _run_or_404(run_id)
    claude.stop(run_id)
    return RedirectResponse(f"/agents/{run_id}", status_code=303)


def _watch_or_404(watch_id: int):
    w = dag_watch.get(watch_id)
    if not w:
        raise HTTPException(404, f"Suivi #{watch_id} inconnu")
    return w


def _watch_context(watch_id: int) -> dict:
    w = _watch_or_404(watch_id)
    return {"w": w, "log": dag_watch.log(w), "report": dag_watch.report(w)}


@app.get("/agents/suivis/{watch_id}", response_class=HTMLResponse)
def dag_watch_detail(request: Request, watch_id: int):
    """Suivi d'un run lancé depuis Jarvis : journal (relances, vagues, notifications) et rapport de fin d'air.py."""
    return _render(request, "dag_watch.html", "/agents", **_watch_context(watch_id))


@app.get("/agents/suivis/{watch_id}/journal", response_class=HTMLResponse)
def dag_watch_journal(request: Request, watch_id: int):
    """Rafraîchi toutes les 10 s par la page du suivi tant qu'il tourne."""
    return templates.TemplateResponse(request, "_dag_watch_log.html", _watch_context(watch_id))


@app.post("/agents/suivis/{watch_id}/arreter")
def dag_watch_stop(watch_id: int):
    _watch_or_404(watch_id)
    dag_watch.stop(watch_id)
    return RedirectResponse(f"/agents/suivis/{watch_id}", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    return _render(request, "sources.html", "/sources", externals=external_statuses(notion_api.load_token() is not None,
                                                                         secret_problem("NOTION_TOKEN")), settings=settings,
                   airflow_problem=_airflow_problem())


@app.get("/deploiements/{slug}")
def deployment_root(slug: str):
    return RedirectResponse(f"/deploiements/{slug}/synthese")


ICON_NAME = re.compile(r"icone\.(png|jpg|gif|webp)")


@app.get("/icones/{slug}")
def client_icon(slug: str):
    """Image d'icône du client (dashboard d'intégration Notion), téléchargée par la synchro dans data/notion/<slug>/."""
    d = fiches.get_deployment(slug)
    path = fiches.deployments_dir.parent / "notion" / slug / (d.icone if d else "")
    if not d or not ICON_NAME.fullmatch(d.icone) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "max-age=300", "X-Content-Type-Options": "nosniff"})


def _from_jarvis(request: Request) -> bool:
    """Requête envoyée par une page de Jarvis (même origine) : un autre site ne peut pas déclencher un lancement."""
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    return bool(origin) and urlsplit(origin).netloc == request.headers.get("host", "")


@app.get("/outils/flow-digger")
def flow_digger_reloaded():
    """Onglet rechargé : rien n'est lancé par un GET, retour à l'accueil."""
    return RedirectResponse("/", status_code=303)


@app.post("/outils/flow-digger")
def open_flow_digger(request: Request, forcer: str = Form("")):
    """Démarre Flow Digger sur la VM s'il ne tourne pas (après un git pull de son dépôt), puis y emmène.
    forcer=1 (bouton de la page d'échec) : lance la version actuelle sans tenter le pull."""
    if not _from_jarvis(request):
        raise HTTPException(403, "Lancement refusé : la demande ne vient pas de Jarvis.")
    launch = flow_digger.start(force=forcer == "1")
    if launch.url:
        return RedirectResponse(launch.url, status_code=303)
    return _render(request, "outil.html", "", tool="Flow Digger", error=launch.error, can_force=launch.can_force,
                   action="/outils/flow-digger")


# ---- Réglages : .env, connexions et 1Password depuis l'app -------------------------------------------------------

def _reload_connections() -> None:
    """Après un changement de réglages ou une connexion à 1Password : secrets relus, Airflow reconstruit (historique gardé),
    réglages des agents Claude appliqués aux prochains lancements."""
    global airflow
    forget_secrets()
    claude.reconfigure()
    fresh = AirflowSource.from_env()
    fresh.runs = airflow.runs
    airflow = fresh
    dag_watch.airflow_env = {"AIRFLOW_URL": fresh.base_url, "AIRFLOW_USER": fresh.user, "AIRFLOW_PASSWORD": fresh.password} if fresh.live else {}


def _guard_settings(request: Request) -> None:
    """Réglages et mot de passe 1Password : seulement en local, et depuis une page de Jarvis."""
    if not request.client or request.client.host not in shell.LOCAL_HOSTS:
        raise HTTPException(403, "Réglages : connexions locales seulement (transfert de port).")
    if not _from_jarvis(request):
        raise HTTPException(403, "Réglages : la demande ne vient pas de Jarvis.")


def _settings_page(request: Request, message: str = "", error: str = "", op_error: str = "") -> HTMLResponse:
    op = onepassword.status()
    return _render(request, "reglages.html", "/reglages", fields=env_file.view(), env_path=env_file.path(), op=op,
                   message=message, error=error, op_error=op_error or op.get("error", ""))


@app.get("/reglages", response_class=HTMLResponse)
def settings_page(request: Request):
    return _settings_page(request)


@app.post("/reglages", response_class=HTMLResponse)
async def settings_save(request: Request):
    _guard_settings(request)
    form = await request.form()
    values = {f.key: str(form.get(f.key, "")) for f in env_file.FIELDS if f.key in form}
    clear = {f.key for f in env_file.FIELDS if form.get(f"effacer_{f.key}")}
    process_keys_before = set(PROCESS_KEYS)
    try:
        changed = env_file.update(values, clear)
    except env_file.EnvError as e:
        return _settings_page(request, error=str(e))
    if not changed:
        return _settings_page(request, message="Rien n'a changé.")
    await asyncio.to_thread(_reload_connections)
    overridden = [k for k in changed if k in process_keys_before]
    later = [env_file.BY_KEY[k].label for k in changed if not env_file.BY_KEY[k].live]
    note = f" Au prochain démarrage de Jarvis : {', '.join(later)}." if later else ""
    if overridden:
        note += f" Attention : {', '.join(overridden)} est aussi défini hors .env (vm.conf…), cette valeur-là reviendra au redémarrage."
    return _settings_page(request, message=f"Enregistré dans .env : {', '.join(changed)}.{note} Lance « Tester » pour vérifier.")


@app.post("/reglages/tester", response_class=HTMLResponse)
async def settings_test(request: Request):
    _guard_settings(request)
    checks = await asyncio.gather(
        asyncio.to_thread(connections.airflow_status, airflow, _airflow_problem()),
        asyncio.to_thread(connections.notion_status, notion_api.load_token(), secret_problem("NOTION_TOKEN")))
    return templates.TemplateResponse(request, "_reglages_tests.html", {"checks": zip(("Airflow", "Notion"), checks)})


@app.post("/reglages/1password/connexion", response_class=HTMLResponse)
async def settings_op_signin(request: Request, password: str = Form("")):
    _guard_settings(request)
    status = await asyncio.to_thread(onepassword.signin, password)
    del password  # jamais gardé ni journalisé
    if status.get("signed_in"):
        await asyncio.to_thread(_reload_connections)
        return _settings_page(request, message="Connecté à 1Password : les références op:// sont relues. Lance « Tester ».")
    return _settings_page(request, op_error=status.get("error") or "Connexion refusée.")


@app.post("/reglages/1password/deconnexion", response_class=HTMLResponse)
async def settings_op_signout(request: Request):
    _guard_settings(request)
    await asyncio.to_thread(onepassword.signout)
    return _settings_page(request, message="Déconnecté de 1Password. Les secrets déjà lus restent en mémoire jusqu'au prochain changement ou redémarrage.")


# Pages globales qui restent ouvertes quand on change de client dans le menu : leur client par défaut est celui du menu.
CLIENT_FILTERED_PAGES = ("/", "/brief", "/deadlines", "/actions", "/recette", "/injections", "/agents", "/notifications")


@app.get("/client/{slug}")
def switch_client(slug: str, vers: str = "/"):
    """Change de client en cours en restant sur la même page : même onglet de fiche, ou même page globale."""
    _get(slug)
    path, _, query = vers.partition("?")
    if m := re.match(r"^/deploiements/[^/]+(/[a-z]+)?$", path):
        target = f"/deploiements/{slug}{m[1] or '/synthese'}"
    elif path in CLIENT_FILTERED_PAGES:  # pages globales : leurs filtres suivent le client en cours
        dropped = ("client", "tous") if path == "/deadlines" else ("client",)  # tous=true : tous les clients sur Deadlines seulement
        kept = urlencode([(k, v) for k, v in parse_qsl(query) if k not in dropped])
        target = path + (f"?{kept}" if kept else "")
    else:
        target = f"/deploiements/{slug}/synthese"
    return _remember(RedirectResponse(target, status_code=303), slug)


@app.get("/deploiements/{slug}/pr/liste", response_class=HTMLResponse)
def deployment_pulls(request: Request, slug: str, frais: bool = False):
    """PR ouvertes de l'orchestrator qui concernent ce client ; chargé par l'onglet PR (GitHub peut mettre quelques secondes)."""
    d = _get(slug)
    prs, error = pulls.for_client(d.slug, d.orchestrator, d.software, {r.pr_url for r in claude.runs if r.slug == slug and r.pr_url}, frais)
    return templates.TemplateResponse(request, "_pr_list.html", {"d": d, "prs": prs, "error": error, "today": date.today(), "read_at": pulls.read_at})


@app.get("/deploiements/{slug}/{tab}", response_class=HTMLResponse)
def deployment_tab(request: Request, slug: str, tab: str, tous: bool = False, vue: str = "ressources"):
    if tab not in dict(DEPLOYMENT_TABS):
        raise HTTPException(404, f"Onglet inconnu : {tab}")
    d = _get(slug)
    request.state.current = d  # la fiche ouverte devient le client en cours
    return _remember(_render(
        request,
        "deployment.html",
        f"/deploiements/{slug}",
        d=d,
        deployments=[d],
        tabs=DEPLOYMENT_TABS,
        tab=tab,
        show_all=tous,
        vue=vue,
        milestones=[(MILESTONE_LABELS.get(k, k), v) for k, v in sorted(d.milestones.items(), key=lambda kv: kv[1])],
    ), slug)


def _launch(dag_id: str, padoa_env: str, d: Deployment | None = None, card: str = "") -> ActionResult:
    return _launch_watched(dag_id, padoa_env, d, card)[0]


def _launch_watched(dag_id: str, padoa_env: str, d: Deployment | None = None, card: str = ""):
    """Lance le DAG ; si le lancement est réel, air.py suit le run (relances, notifications, rapport).
    Renvoie le résultat et le suivi (None si rien n'est suivi : échec ou simulation)."""
    result = airflow.trigger_dag(dag_id, padoa_env, d.client if d else "", card)
    w = None
    if result.ok and result.run_id:
        customers = d.orchestrator if d else []
        customer = next((c.customer for c in dag_catalogue.dags_for(customers) if c.dag_id == dag_id), "") or \
            next((c["customer"] for c in dag_catalogue.ced_for(customers) if c["dag_id"] == dag_id), "")  # client du catalogue
        w = dag_watch.start(d.slug if d else "", d.client if d else "", customer, dag_id, result.run_id, padoa_env, card)
        result.message += f" · suivi #{w.id} lancé (relances automatiques, rapport à la fin) : Agents → Suivis de DAG"
    return result, w


@app.post("/actions/dag/{dag_id}", response_class=HTMLResponse)
def trigger_dag(request: Request, dag_id: str, padoa_env: str = Form(...)):
    """DAG d'une ressource de la fiche, sur la stack choisie : jamais de stack par défaut (Airflow prendrait la sienne)."""
    return _result(request, _launch(dag_id, padoa_env))


@app.post("/actions/injection/{slug}", response_class=HTMLResponse)
def trigger_injection(request: Request, slug: str, dag_id: str = Form(...), padoa_env: str = Form(...), card: str = Form("")):
    """Lance le DAG choisi pour une carte d'injection, sur la stack choisie (padoa_env)."""
    d = _get(slug)
    if not dag_catalogue.known_dag(d.orchestrator, dag_id):
        return _result(request, ActionResult(False, f"{dag_id} n'est pas un DAG de {d.client} dans le catalogue.", simulated=False))
    if padoa_env == "prod" and "prod" in PADOA_ENVS and request.headers.get("HX-Prompt", "").strip() != CONFIRM_PROD:
        return _result(request, ActionResult(False, f"Lancement en prod annulé : tape {CONFIRM_PROD} pour confirmer.", simulated=False))
    return _result(request, _launch(dag_id, padoa_env, d, card))


@app.post("/actions/chaine/{slug}", response_class=HTMLResponse)
def chain_start(request: Request, slug: str, padoa_env: str = Form(...), dag_id: list[str] = Form([]), ordre: str = Form("")):
    """Enchaînement : les DAG cochés, dans l'ordre des clics (« ordre »), sinon dans celui de la liste. Jamais en prod."""
    d = _get(slug)
    wanted = [x for x in ordre.split(",") if x]
    dag_ids = wanted if sorted(wanted) == sorted(set(dag_id)) else list(dict.fromkeys(dag_id))
    problem = ("Coche au moins deux DAG à enchaîner." if len(dag_ids) < 2
               else f"Pas plus de {MAX_STEPS} DAG par enchaînement." if len(dag_ids) > MAX_STEPS
               else "Un enchaînement ne part jamais en prod." if padoa_env == "prod"
               else f"Stack inconnue : {padoa_env}" if padoa_env not in PADOA_ENVS
               else next((f"{x} n'est pas un DAG de {d.client} dans le catalogue." for x in dag_ids if not dag_catalogue.known_dag(d.orchestrator, x)), ""))
    if problem:
        return _result(request, ActionResult(False, problem, simulated=False))
    chain = dag_chains.start(d.slug, d.client, padoa_env, dag_ids)
    first = chain.steps[0]
    if chain.group == "ko":
        return _result(request, ActionResult(False, f"Enchaînement #{chain.id} {chain.status} · {first.dag_id} : {first.message}", simulated=False))
    how = "simulé de bout en bout (Airflow non configuré)" if chain.simulated else f"lancé sur {padoa_env} : chaque DAG part quand le précédent finit vert"
    return _result(request, ActionResult(True, f"Enchaînement #{chain.id} {how} · {' → '.join(dag_ids)} · à suivre dans Agents → Enchaînements",
                                         simulated=chain.simulated))


@app.post("/agents/chaines/{chain_id}/arreter")
def chain_stop(chain_id: int):
    if not dag_chains.get(chain_id):
        raise HTTPException(404, f"Enchaînement #{chain_id} inconnu")
    dag_chains.stop(chain_id)
    return RedirectResponse("/agents#chaines", status_code=303)


@app.post("/actions/claude/{slug}/{resource}", response_class=HTMLResponse)
def launch_claude(request: Request, slug: str, resource: str):
    return _result(request, claude.launch(_get(slug), resource))


def _retour(slug: str, retour_id: str):
    d = _get(slug)
    retour = next((r for r in d.retours if r.id == retour_id), None)
    if retour is None:
        raise HTTPException(404, f"Retour inconnu : {retour_id}")
    return d, retour


TICKET_KINDS = ("retour", "action")


def ticket_url(slug: str, kind: str, ref: str) -> str:
    return f"/deploiements/{slug}/ticket/{kind}/{ref}"


templates.env.globals["ticket_url"] = ticket_url
templates.env.globals["perso"] = personal


def _find_ticket(d: Deployment, kind: str, ref: str):
    items = d.retours if kind == "retour" else d.actions
    return next((i for i in items if (i.id if kind == "retour" else i.ref) == ref.upper()), None)


def _ticket_item(slug: str, kind: str, ref: str):
    """(fiche, retour ou carte) ; 404 si le type, le client ou le ticket est inconnu."""
    if kind not in TICKET_KINDS:
        raise HTTPException(404, f"Type inconnu : {kind}")
    d = _get(slug)
    item = _find_ticket(d, kind, ref)
    if item is None:
        raise HTTPException(404, f"{'Retour' if kind == 'retour' else 'Carte'} {ref} introuvable chez {d.client}")
    return d, item


def _ticket_label(kind: str, item) -> str:
    return item.label if kind == "retour" else item.title


@app.get("/deploiements/{slug}/ticket/{kind}/{ref}", response_class=HTMLResponse)
def ticket(request: Request, slug: str, kind: str, ref: str, agent: bool = False):
    """Tout ce qu'on sait d'un retour ou d'une carte d'action : fiche, contenu, agents, historique.
    Panneau latéral quand la page l'appelle (htmx), page entière sinon (lien ouvert dans un nouvel onglet).
    agent=1 (commande « >agent » de la palette) : le bloc « Lancer un agent » arrive déplié, brief chargé, et prend le focus."""
    d, item = _ticket_item(slug, kind, ref)
    history = [c for c in changes_since(d, settings.deployments_dir, datetime.min) if c.notion_url == notion_link(item.notion_url)]
    runs = [r for r in claude.runs if r.slug == slug and r.subject == f"retour {ref.upper()}"] if kind == "retour" else []
    context = {"d": d, "kind": kind, "item": item, "history": history, "runs": runs, "today": date.today(), "open_agent": agent}
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_ticket.html", context)
    return _render(request, "ticket.html", f"/deploiements/{slug}", **context)


@app.get("/deploiements/{slug}/recette/{retour_id}/brief", response_class=HTMLResponse)
def agent_brief(request: Request, slug: str, retour_id: str):
    """Formulaire de l'agent, brief pré-rempli : chargé à l'ouverture du bloc pour ne pas alourdir la page."""
    d, r = _retour(slug, retour_id)
    return templates.TemplateResponse(request, "_agent_form.html", {"d": d, "r": r, "brief": retour_prompt(d, r), "to_fill": TO_FILL})


@app.post("/actions/agent-retour/{slug}/{retour_id}", response_class=HTMLResponse)
def launch_agent_on_retour(request: Request, slug: str, retour_id: str, description: str = Form(""), prompt: str = Form("")):
    d, retour = _retour(slug, retour_id)
    if not retour.is_open:
        return _result(request, ActionResult(False, f"Retour {retour_id} déjà clos ({retour.status})", simulated=False))
    if TO_FILL in prompt:
        return _result(request, ActionResult(False, f"Le brief contient encore « {TO_FILL} » : complète-le avant de lancer.", simulated=False))
    if not prompt.strip() and not description.strip() and not retour.content:
        return _result(request, ActionResult(False, "Ce retour n'a pas de contenu dans Notion : écris le descriptif du bug.", simulated=False))
    return _result(request, claude.launch_retour(d, retour, description, prompt))


def _refresh(slugs: list[str]) -> tuple[bool, str]:
    token = notion_api.load_token()
    if not token:
        return False, "Jeton Notion absent : ajoute NOTION_TOKEN dans le fichier .env de l'app (voir README)."
    if not _refresh_lock.acquire(blocking=False):
        return False, "Un rafraîchissement est déjà en cours."
    try:
        notion = notion_api.NotionClient(token)
        done, errors = [], []
        for slug in slugs:
            try:
                r = notion_api.refresh(slug, notion)
                done.append(f"{r['client']} : {r['cartes']} cartes, {r['retours']} retours ({r['secondes']} s)")
            except Exception as e:  # le message est affiché ; la fiche existante reste en place
                errors.append(f"{slug} : {e}")
                notifications.add(f"refresh-fail:{slug}:{datetime.now().isoformat(timespec='minutes')}", "sync", "urgent",
                                  f"{slug} · rafraîchissement Notion en échec", str(e)[:300], f"/deploiements/{slug}", slug)
        return not errors, " · ".join(done + [f"ÉCHEC {e}" for e in errors])
    finally:
        _refresh_lock.release()


@app.post("/actions/rafraichir", response_class=HTMLResponse)
def refresh_all(request: Request):
    ok, message = _refresh(notion_api.clients())
    response = _result(request, ActionResult(ok, message, simulated=False))
    if ok:
        response.headers["HX-Refresh"] = "true"
    return response


@app.post("/actions/rafraichir/{slug}", response_class=HTMLResponse)
def refresh_one(request: Request, slug: str):
    _get(slug)
    ok, message = _refresh([slug])
    response = _result(request, ActionResult(ok, message, simulated=False))
    if ok:
        response.headers["HX-Refresh"] = "true"
    return response


# ============================================================ Perso : brief, épingles, notes, rappels
def _pinned(d: Deployment) -> list[dict]:
    """Tickets épinglés du client ; « item » vaut None quand le ticket a disparu de la fiche (reste à désépingler)."""
    return [{**p, "item": (item := _find_ticket(d, p["kind"], p["ref"])), "label": _ticket_label(p["kind"], item) if item else p["ref"]}
            for p in personal.pins(d.slug)]


def _reminders_context(slug: str, error: str = "") -> dict:
    return {"reminders": personal.reminders(slug or None), "remind_slug": slug, "remind_error": error, "today": date.today()}


@app.get("/brief", response_class=HTMLResponse)
def brief_page(request: Request, tous: bool = False):
    """Brief du matin : le résumé de l'accueil en texte à copier (client en cours, ou tous les clients)."""
    current = _current(request)
    deployments = [current] if current and not tous else sorted(_deployments(request), key=_closest_go_live)
    today = date.today()
    return _render(request, "brief.html", "/", all_clients=tous or not current,
                   text=brief.build(deployments, _deadlines(deployments), personal.due(today), today, _in_period))


@app.post("/perso/epingle/{slug}/{kind}/{ref}", response_class=HTMLResponse)
def pin_toggle(request: Request, slug: str, kind: str, ref: str):
    d, _item = _ticket_item(slug, kind, ref)
    pinned = personal.toggle_pin(slug, kind, ref.upper())
    return templates.TemplateResponse(request, "_pin_button.html", {"d": d, "kind": kind, "ref": ref.upper(), "pinned": pinned})


@app.get("/perso/notes", response_class=HTMLResponse)
def notes_panel(request: Request):
    """Contenu du bloc-notes flottant (bouton « Notes ») : notes du client en cours et notes générales."""
    return templates.TemplateResponse(request, "_notes_panel.html", {"current": _current(request)})


@app.post("/perso/note", response_class=HTMLResponse)
def general_note_save(request: Request, texte: str = Form("")):
    """Notes générales (sans client) : rangées sous la clé vide de data/perso.json."""
    personal.set_note("", texte)
    return _result(request, ActionResult(True, f"Enregistré à {datetime.now():%H:%M}", simulated=False))


@app.post("/perso/note/{slug}", response_class=HTMLResponse)
def note_save(request: Request, slug: str, texte: str = Form("")):
    _get(slug)
    personal.set_note(slug, texte)
    return _result(request, ActionResult(True, f"Enregistré à {datetime.now():%H:%M}", simulated=False))


@app.post("/perso/rappel", response_class=HTMLResponse)
def reminder_add(request: Request, quand: str = Form(""), jour: str = Form(""), slug: str = Form(""), kind: str = Form(""),
                 ref: str = Form(""), label: str = Form("")):
    """Rappel sur un ticket (kind et ref : le libellé est celui du ticket) ou rappel libre de l'accueil (label)."""
    d = item = None
    if kind:
        d, item = _ticket_item(slug, kind, ref)
        ref, label = ref.upper(), _ticket_label(kind, item)
    elif slug:
        _get(slug)
    due = remind_date(quand, jour)
    error = "Choisis une date à venir." if due is None else "" if label.strip() else "Écris ce qu'il faut te rappeler."
    if not error:
        personal.add_reminder(due, label, slug, kind, ref)
    if kind:
        return templates.TemplateResponse(request, "_ticket_remind.html", {"d": d, "kind": kind, "ref": ref, "today": date.today(), "remind_error": error})
    return templates.TemplateResponse(request, "_reminders.html", _reminders_context(slug, error))


@app.post("/perso/rappel/{reminder_id}/fait", response_class=HTMLResponse)
def reminder_done(reminder_id: int):
    personal.remove_reminder(reminder_id)
    return HTMLResponse("")


def _reminder_notes(deployments: list[Deployment], today: date) -> list[dict]:
    """Rappels arrivés à échéance : une notification chacun, qui mène au ticket (ou à l'accueil pour un rappel libre)."""
    names = {d.slug: d.client for d in deployments}
    return [{"key": f"rappel:{r.id}:{r.due.isoformat()}", "kind": "rappel", "level": "info", "title": f"Rappel : {r.label}",
             "detail": names.get(r.slug, ""), "link": ticket_url(r.slug, r.kind, r.ref) if r.kind else "/", "client": names.get(r.slug, "")}
            for r in personal.due(today)]


# ============================================================ Notifications
def _deadline_notes() -> None:
    """Veilles (sources/watchers.py) : échéance du jour, synchro en retard, santé qui se dégrade, jalon proche."""
    now = datetime.now()
    deployments = fiches.list_deployments()
    state = notifications.load_state()
    health, seen = watchers.health_notes(deployments, state.get("sante", {}), now)
    notifications.add_many(
        watchers.deadline_notes(_deadlines(deployments), now.date())
        + watchers.sync_late_notes(deployments, now)
        + health
        + watchers.milestone_notes(deployments, now.date())
        + _reminder_notes(deployments, now.date())
    )
    if seen != state.get("sante"):
        notifications.save_state({**state, "sante": seen})


def _notes_context() -> dict:
    _deadline_notes()
    items = notifications.all()
    unread = [n for n in items if not n.read]
    return {"notes": items, "unread": unread, "urgent": [n for n in unread if n.level == "urgent"]}


@app.get("/notifications/compte", response_class=HTMLResponse)
def notifications_count(request: Request):
    return templates.TemplateResponse(request, "_notif_count.html", _notes_context())


@app.get("/notifications/panneau", response_class=HTMLResponse)
def notifications_panel(request: Request):
    ctx = _notes_context()
    return templates.TemplateResponse(request, "_notif_panel.html", {**ctx, "notes": ctx["notes"][:15]})


@app.get("/notifications/nouvelles")
def notifications_new(apres: int = 0):
    """Notifications plus récentes que l'id donné, pour les notifications du navigateur."""
    _deadline_notes()
    return JSONResponse([{"id": n.id, "key": n.key, "title": n.title, "detail": n.detail, "link": n.link, "level": n.level}
                         for n in notifications.after(apres)])


@app.post("/notifications/lu", response_class=HTMLResponse)
def notifications_read(request: Request, id: int | None = Form(None)):
    notifications.mark_read([id] if id else None)
    ctx = _notes_context()
    response = templates.TemplateResponse(request, "_notif_panel.html", {**ctx, "notes": ctx["notes"][:15]})
    response.headers["HX-Trigger"] = "notifs-changed"
    return response


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    return _render(request, "notifications.html", "/notifications", **_notes_context())
