import asyncio
import calendar
import dataclasses
import logging
import os
import re
from contextlib import asynccontextmanager
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from deploy_hub import client_config
from deploy_hub.config import PROCESS_KEYS, PROJECT_ROOT, clients_dir, data_dir, ensure_private_dirs, forget_secrets, load_settings, secret_problem
from deploy_hub.models import (ACTION_STATUSES, DONE, INJECTION_STATUSES, KO, MILESTONE_LABELS, RETOUR_STATUSES, Deployment,
                               kanban_columns, notion_link, status_group)
from deploy_hub import brief, console, env_file, onepassword, shell, sync_notion
from deploy_hub.search import Index
from deploy_hub.sources import ActionResult
from deploy_hub.sources import dags as dag_catalogue
from deploy_hub.sources.airflow import CONFIRM_PROD, PADOA_ENVS, AirflowSource
from deploy_hub.rendu import md, md_inline
from deploy_hub.sources.claude_runner import (TO_FILL, WAITING, ClaudeRunner, agent_questions, answer_text, chosen_answer,
                                              duration_text, read_log, retour_prompt, tokens_text, usage)
from deploy_hub.sources.dag_board import board
from deploy_hub.sources.dag_chain import MAX_STEPS, ChainRunner
from deploy_hub.sources.dag_source import DagSource
from deploy_hub.sources.dag_watch import DagWatcher
from deploy_hub.sources.client_directory import ClientDirectory, form_of, name_key
from deploy_hub.sources.sync_routine import SyncRoutine
from deploy_hub.sources import connections, flow_digger, notion_api, running_code
from deploy_hub.sources.external import external_statuses
from deploy_hub.sources.journal import changes_since, group_changes
from deploy_hub.sources import watchers
from deploy_hub.sources.notifications import Notification, NotificationStore
from deploy_hub.sources.personal import PersonalStore, remind_date
from deploy_hub.sources.pulls import PullRequests, names
from deploy_hub.sources import review as agent_review
from deploy_hub.sources.fiches import FicheSource
from deploy_hub.sources.web_push import WebPush

HERE = Path(__file__).parent

ensure_private_dirs()  # données et configuration des clients jamais dans un dossier que git pourrait pousser
settings = load_settings()
fiches = FicheSource(settings.deployments_dir, clients_dir())
airflow = AirflowSource.from_env()
notifications = NotificationStore(Path(os.getenv("DEPLOY_HUB_NOTIFICATIONS") or settings.deployments_dir.parent / "notifications.json"))
claude = ClaudeRunner.from_env(settings.orchestrator_dir, notifications, data_dir() / "agents")
# Synchro Notion sans jeton : le bouton « Rafraîchir depuis Notion » fait suivre sync/routine.md à Claude Code (claude -p).
sync_routine = SyncRoutine(notifications)
client_directory = ClientDirectory()
pulls = PullRequests(settings.orchestrator_dir)  # PR ouvertes de l'orchestrator (gh), gardées cinq minutes
# Suivi des runs lancés (air.py d'Arthur) ; les suivis en cours au démarrage reprennent si Airflow est configuré.
dag_watch = DagWatcher(data_dir() / "airflow", notifications, airflow.air_env())
# Catalogue des DAGs : celui du clone orchestrator local tel qu'il est sur le disque (comme l'Airflow local).
dag_source = DagSource(settings.orchestrator_dir, dag_catalogue.LOCAL)
# Enchaînements de DAG : le suivant part quand le suivi du précédent finit vert. Passe par le dag_watch du moment.
dag_chains = ChainRunner(data_dir() / "airflow" / "chaines.json",
                         lambda dag_id, padoa_env, slug, card: _launch_watched(dag_id, padoa_env, fiches.get_deployment(slug), card), notifications)
dag_watch.on_end = dag_chains.watch_ended
web_push = WebPush(data_dir() / "push")  # notifications du Mac même onglet fermé (voir sources/web_push.py)
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
        asyncio.to_thread(connections.notion_status, notion_api.load_token(), secret_problem("NOTION_TOKEN")),
        asyncio.to_thread(_check_claude_login, True))  # écrit sa propre ligne : avertissement s'il faut se reconnecter
    for line in connections.lines(*checks[:2]):
        log.info(line)
    await asyncio.to_thread(dag_chains.reconcile, lambda watch_id: dag_watch.get(watch_id))
    await asyncio.to_thread(dag_source.refresh)  # catalogue de la branche à jour (génération en tâche de fond)
    pusher = asyncio.create_task(_push_loop())
    yield
    pusher.cancel()
    dag_watch.shutdown()  # à l'arrêt (rechargement compris) : coupe les air.py, que le Jarvis suivant reprendra


PUSH_EVERY = 60  # secondes, comme le panneau des notifications


def _guarded(label: str, job) -> None:
    """Tâche de fond qui ne doit jamais empêcher les suivantes (ni l'envoi des notifications)."""
    try:
        job()
    except Exception:  # noqa: BLE001
        log.exception(f"{label} en échec")


CLAUDE_CHECK_S = 3600  # connexion de Claude Code : revérifiée toutes les heures tant que Jarvis tourne
_claude_checked = time.monotonic()
_claude_login: connections.ClaudeLogin | None = None  # dernier état connu : celui que montre la page État des systèmes


def _check_claude_login(startup: bool = False) -> None:
    """Claude Code est-il toujours connecté ? Au lancement de Jarvis (une ligne dans son terminal, comme Airflow et Notion),
    puis toutes les heures : Jarvis tourne des jours, et c'est pendant ce temps que la connexion expire. Déconnecté, ou
    connexion qui expire d'ici trois jours : avertissement dans le terminal et notification dans l'app, une par jour."""
    global _claude_checked, _claude_login
    if not startup and time.monotonic() - _claude_checked < CLAUDE_CHECK_S:
        return
    _claude_checked = time.monotonic()
    _claude_login = login = connections.claude_status(sync_routine.claude_bin)
    if login.alert:
        log.warning(login.line)
        notifications.add(f"claude-login:{login.word}:{date.today()}", "sync", "info" if login.ok else "urgent",
                          "Claude Code : connexion bientôt expirée" if login.ok else "Claude Code déconnecté", login.message, "/sources")
    elif startup:
        log.info(login.line)


_code_started = running_code.head()  # commit du code chargé par ce processus ("" : dossier hors de git)
_version_started = running_code.version()  # son numéro de version (pyproject.toml), en pied de page ; monté à chaque PR
_restart: tuple[str, ...] = ()  # Jarvis à relancer : (installée, en service), en numéros de version, en commits si le numéro n'a pas bougé


def _check_restart() -> None:
    """Jarvis se met à jour par « git pull » puis relance du serveur : tant que la relance n'est pas faite, bandeau sur
    toutes les pages (base.html), ligne dans le terminal et notification, une fois par commit déployé."""
    global _restart
    newer = running_code.newer_code(_code_started)[:7]
    if not newer:
        _restart = ()
        return
    disk = running_code.version()
    if disk and _version_started and disk != _version_started:
        _restart = (f"v{disk}", f"v{_version_started}")
    else:  # numéro pas monté (ou illisible) : les commits, faute de mieux
        _restart = (newer, _code_started[:7])
    detail = f"La version {_restart[0]} est installée, mais c'est encore la {_restart[1]} qui tourne : relance le serveur pour l'appliquer."
    if notifications.add(f"relance:{newer}", "sync", "urgent", "Jarvis est à relancer", detail, "/sources"):  # clé déjà vue : rien
        log.warning(f"Jarvis est à relancer. {detail}")


SYNC_EVERY_MIN = 60  # réglage JARVIS_SYNC_EVERY_MIN (minutes ; 0 : jamais)
SYNC_RETRY_S = 300  # synchro automatique non partie (une autre tourne, claude introuvable) : nouvel essai 5 minutes après
_auto_sync_next = datetime.now() + timedelta(seconds=SYNC_RETRY_S)  # pas avant ce moment ; au démarrage : on laisse Jarvis se lancer


def auto_sync_every() -> int:
    """Minutes entre deux synchros automatiques (0 : jamais). Relu à chaque fois : un changement dans Réglages s'applique."""
    value = os.getenv("JARVIS_SYNC_EVERY_MIN", "").strip()
    return int(value) if value.isdigit() else SYNC_EVERY_MIN


def auto_sync_plan() -> tuple[list[str], datetime | None]:
    """Synchro Notion automatique : les clients qui l'ont demandée (bouton « Synchro auto » de leur fiche ; aucun par
    défaut) et le moment où elle doit partir, None si rien n'est prévu (coupée dans Réglages, ou demandée par personne).
    Elle part dès que l'un d'eux n'a pas été vérifié depuis l'intervalle (une heure par défaut) : une synchro faite à la
    main compte, et un redémarrage de Jarvis ne remet rien à zéro. Jamais avant _auto_sync_next."""
    every = auto_sync_every()
    wanted = [c.slug for c in client_config.active_configs() if c.auto_sync] if every else []
    if not wanted:
        return [], None
    return wanted, max(sync_notion.next_check(every, wanted), _auto_sync_next)


def auto_sync_note(d: Deployment) -> str:
    """« prochaine vers 14:15 », à côté de la dernière synchro d'une fiche : l'heure prévue de la synchro automatique de
    ce client. Vide s'il ne l'a pas demandée, si rien n'est prévu, ou pendant qu'une synchro tourne (règle d'affichage :
    le bouton « Rafraîchir » porte alors l'avancement)."""
    if not d.auto_sync or sync_routine.running or _refresh_lock.locked():
        return ""
    wanted, due = auto_sync_plan()
    if due is None or d.slug not in wanted:
        return ""
    now = datetime.now()
    if due <= now + timedelta(seconds=PUSH_EVERY):  # la boucle de fond regarde une fois par minute
        return "prochaine dans la minute"
    return f"prochaine vers {due:%H:%M}" if due.date() == now.date() else f"prochaine le {due:%d/%m} vers {due:%H:%M}"


def _auto_sync() -> None:
    """Lance la synchro Notion automatique quand son heure est venue (auto_sync_plan), pour tous les clients qui l'ont
    demandée : par l'API avec un jeton Notion, sinon par la routine Claude Code. Partie : la suivante au plus tôt un
    intervalle après, même si un client reste en échec ; pas partie : nouvel essai cinq minutes plus tard."""
    global _auto_sync_next
    wanted, due = auto_sync_plan()
    if due is None or due > datetime.now():
        return
    if notion_api.load_token():
        threading.Thread(target=_refresh, args=(wanted,), name="jarvis-sync-auto", daemon=True).start()
        started, message = True, "lancée par l'API"
    else:
        r = sync_routine.start(wanted, auto=True)
        started, message = r.ok, r.message
    _auto_sync_next = datetime.now() + timedelta(seconds=auto_sync_every() * 60 if started else SYNC_RETRY_S)
    log.info(f"Synchro Notion automatique : {message}")


def _push_round() -> None:
    """Un tour : échéances du jour (créées d'habitude à l'ouverture d'une page), puis envoi des nouvelles notifications ;
    au passage, le catalogue des DAGs est régénéré si le clone orchestrator local a changé, et l'annuaire des clients est
    relu s'il a plus d'un jour (au démarrage, puis chaque jour) : « Ajouter un client » l'a déjà, sans attente ; enfin la
    synchro Notion part toute seule quand son intervalle est écoulé (une heure par défaut)."""
    _guarded("Catalogue des DAGs : relevé du clone", dag_source.refresh)
    _deadline_notes()
    web_push.pump(notifications)
    _guarded("Annuaire des clients : relecture", client_directory.refresh_if_stale)
    _guarded("Connexion de Claude Code", _check_claude_login)
    _guarded("Code à jour", _check_restart)
    _guarded("Synchro Notion automatique", _auto_sync)


async def _push_loop() -> None:
    """Toutes les minutes, même sans onglet ouvert : couvre aussi les notifications écrites par la synchro Notion (autre processus),
    et relit l'annuaire des clients quand il a plus d'un jour."""
    while True:
        try:
            await asyncio.to_thread(_push_round)
        except Exception:
            log.exception("Tour de Web Push en échec")
        await asyncio.sleep(PUSH_EVERY)


app = FastAPI(title="Jarvis", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
def when_text(moment: datetime | None) -> str:
    """« aujourd'hui à 14:04 », « hier à 16:00 », « le 12/11 à 09:03 » (« le 12/11/2025 à … » une autre année)."""
    if not moment:
        return ""
    days = (date.today() - moment.date()).days
    day = "aujourd'hui" if days == 0 else "hier" if days == 1 else \
        f"le {moment:%d/%m}" if moment.year == date.today().year else f"le {moment:%d/%m/%Y}"
    return f"{day} à {moment:%H:%M}"


templates.env.filters["quand"] = when_text
# Réglages cités dans un message (« renseigne ORCHESTRATOR_DIR dans Réglages ») → lien vers leur champ de la page Réglages ;
# « 1Password » → sa section de connexion. Le plus long d'abord : JARVIS_AGENT_MAX ne doit pas couper JARVIS_AGENT_MAX_TURNS.
SETTING_MENTION = re.compile(r"\b(" + "|".join(sorted(map(re.escape, env_file.BY_KEY), key=len, reverse=True)) + r")\b|\b1Password\b")


def settings_links(text) -> Markup:
    """Message en texte (échappé ici) → même texte, réglages et 1Password en liens vers la page Réglages."""
    def link(m: re.Match) -> str:
        href = f"/reglages#reglage-{m.group(1)}" if m.group(1) else "/reglages#1password"
        return f'<a href="{href}">{m.group(0)}</a>'
    return Markup(SETTING_MENTION.sub(link, str(escape(text or ""))))


templates.env.filters["reglages"] = settings_links
templates.env.filters["duree"] = duration_text
templates.env.filters["tokens"] = tokens_text
def when_iso(iso: str) -> str:
    """when_text d'une date ISO (fichier d'état) ; vide si elle manque ou est illisible."""
    try:
        return when_text(datetime.fromisoformat(iso))
    except (TypeError, ValueError):
        return ""


templates.env.filters["quand_iso"] = when_iso
templates.env.filters["md"] = md
templates.env.filters["md_inline"] = md_inline
templates.env.globals["status_group"] = lambda s: status_group(s, INJECTION_STATUSES)
templates.env.globals["kanban_columns"] = kanban_columns
templates.env.globals["auto_sync_every"] = auto_sync_every  # bouton « Synchro auto » d'une fiche
templates.env.globals["auto_sync_note"] = auto_sync_note
templates.env.tests["drive"] = client_config.is_drive  # {% if l.url is drive %} : logo Google Drive dans le dock
templates.env.tests["notion"] = client_config.is_notion  # {% if l.url is notion %} : logo Notion dans le dock
templates.env.globals["jarvis_version"] = _version_started  # pied de page ; historique dans CHANGELOG.md
templates.env.globals["jarvis_commit"] = _code_started[:7]
templates.env.globals["restart_for"] = lambda: _restart  # bandeau « Jarvis est à relancer » (base.html)
templates.env.globals["ACTION_STATUSES"] = ACTION_STATUSES
templates.env.globals["RETOUR_STATUSES"] = RETOUR_STATUSES
templates.env.globals["console_commands"] = list(console.COMMANDS)  # complétion par Tab dans la console


RUNS_SHOWN = 25  # runs d'une ligne de l'onglet Injections : le dernier s'affiche, « Voir plus » ajoute les suivants 3 par 3


def _runs_of(choices, limit: int = RUNS_SHOWN) -> list[tuple]:
    """Derniers runs suivis (air.py) des DAG d'une ligne de l'onglet Injections, le plus récent d'abord : (suivi, DAG).
    Au-delà de la limite, la ligne renvoie à Agents → DAG, qui les a tous."""
    by_id = {c.dag_id: c for c in choices}
    return [(w, by_id[w.dag_id]) for w in dag_watch.watches if w.dag_id in by_id][:limit]


templates.env.globals["runs_of"] = _runs_of
templates.env.globals["RUNS_SHOWN"] = RUNS_SHOWN
templates.env.globals["dag_source_status"] = lambda: dag_source.status(dag_catalogue.CATALOGUE)  # bandeau Injections

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
TABS_COOKIE = "jarvis_onglets"  # vue de la fiche, par navigateur : « simple:injections.recette.pr » ou « complete:… » (pas de virgule dans un cookie)
SIMPLE_TABS = ("injections", "recette", "pr")  # onglets que garde la vue simplifiée tant qu'on n'en a pas choisi d'autres


def _deployments(request: Request) -> list[Deployment]:
    """Fiches des clients suivis (les archivés n'y sont pas), lues une seule fois par requête (menu et contenu de la page
    partagent la même liste)."""
    if not hasattr(request.state, "deployments"):
        request.state.deployments = fiches.list_deployments()
    return request.state.deployments


def _closest_go_live(d: Deployment) -> tuple:
    """Ordre par défaut : le déploiement à venir le plus proche d'abord, puis les autres."""
    upcoming = bool(d.go_live) and d.days_left >= 0
    return (not upcoming, d.days_left if upcoming else 0, d.client)


def _current(request: Request) -> Deployment | None:
    """Client en cours : celui du cookie (dernière fiche ouverte), sinon le déploiement le plus proche. Jamais un client
    archivé : archiver le client en cours passe au suivant."""
    if not hasattr(request.state, "current"):
        deployments = _deployments(request)
        slug = request.cookies.get(CLIENT_COOKIE)
        request.state.current = next((d for d in deployments if d.slug == slug), None) or \
            (min(deployments, key=_closest_go_live) if deployments else None)
    return request.state.current


VIEWS_COOKIE = "jarvis_vues"  # choix de vue gardés : « recette.vue:kanban|recette.tous:true|deadlines.periode:semaine »


def _views(request: Request, page: str, *keys: str) -> dict[str, str]:
    """Choix de vue d'une page (vue, tous, periode… ; "" : le défaut). Si l'adresse en porte un, elle seule compte : une
    pastille ou un lien direct disent tout ce qu'ils veulent, le reste vaut le défaut. Sinon (on arrive par le menu), les
    derniers choix faits sur les pastilles : filters.js les note dans le cookie au clic, le serveur n'y écrit jamais."""
    if any(k in request.query_params for k in keys):
        return {k: request.query_params.get(k, "") for k in keys}
    kept = dict(p.split(":", 1) for p in request.cookies.get(VIEWS_COOKIE, "").split("|") if ":" in p)
    return {k: kept.get(f"{page}.{k}", "") for k in keys}


def _list_view(request: Request, page: str) -> dict:
    """Présentation (vue) et étendue (show_all : terminés compris) d'une liste, pour son gabarit."""
    views = _views(request, page, "vue", "tous")
    return {"vue": views["vue"], "show_all": views["tous"].lower() in ("true", "1")}


def _tab_view(request: Request) -> tuple[bool, list[str]]:
    """Vue de la fiche (cookie) : la vue simplifiée est-elle active, et les onglets qu'elle garde, dans l'ordre de la fiche."""
    mode, _, keys = request.cookies.get(TABS_COOKIE, "").partition(":")
    return mode == "simple", _known_tabs(keys.split(".")) or list(SIMPLE_TABS)


def _known_tabs(keys) -> list[str]:
    return [k for k, _ in DEPLOYMENT_TABS if k in keys]


def _fiche_url(request: Request, slug: str) -> str:
    """Onglet sur lequel s'ouvre une fiche : la synthèse, sauf si la vue simplifiée ne la garde pas. Les liens « ouvrir la
    fiche » visent /deploiements/<slug>, qui redirige ici."""
    simple, kept = _tab_view(request)
    return f"/deploiements/{slug}/{kept[0] if simple and 'synthese' not in kept else 'synthese'}"


def _keep(response, cookie: str, value: str):
    """Préférence gardée un an dans ce navigateur (client en cours, vue des fiches)."""
    response.set_cookie(cookie, value, max_age=365 * 24 * 3600, samesite="lax")
    return response


def _remember(response, slug: str):
    return _keep(response, CLIENT_COOKIE, slug)


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
            "claude_problem": claude.problem(),
            "airflow": airflow,
            "dags_for": dag_catalogue.dags_for,
            "ced_for": dag_catalogue.ced_for,
            "groups_for": dag_catalogue.groups_for,
            "padoa_envs": PADOA_ENVS,
            "notion_ready": notion_api.load_token() is not None or sync_routine.problem() is None,  # jeton, ou routine via Claude
            "sync_running": sync_routine.running,  # synchro lancée avant ce chargement : le bouton reprend son avancement
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
def index(request: Request, enregistre: str = "", retire: str = "", archive: str = "", desarchive: str = ""):
    everyone = _deployments(request)
    deployments = [d for d in everyone if not d.pending]  # cartes : clients synchronisés
    pending = [c for d in everyone if d.pending and (c := client_config.load(d.slug))]  # configurés, pas encore synchronisés
    archived = sorted(fiches.archived_deployments(), key=lambda d: d.client.lower())
    names = {d.slug: d.client for d in everyone + archived}  # les messages nomment le client, pas son identifiant
    return _render(request, "index.html", "/clients", deployments=deployments, pending=pending, archived=archived, saved=enregistre,
                   retired=retire, archived_now=names.get(archive, archive), restored=names.get(desarchive, desarchive))


def _client_form(request: Request, cfg, error: str = "", new: bool = True, form: dict | None = None, notice: str = "",
                 chosen: dict | None = None, directory: dict | None = None, keys=None) -> HTMLResponse:
    """directory, keys : annuaire et clés orchestrator déjà lus par l'appelant (sinon lus ici)."""
    keys = sorted(keys if keys is not None else dag_catalogue.catalogue()["clients"])
    chosen = chosen or {}
    directory = (directory if directory is not None else client_directory.directory()) if new else {}
    followed = {key for c in client_config.all_configs() for key in (name_key(c.client), name_key(c.slug))} if new else set()
    fiches = list({f["url"]: f for f in chosen.get("fiches", []) + directory.get("fiches", [])}.values())  # les siennes d'abord
    return _render(request, "client_form.html", "/clients", cfg=cfg, error=error, new=new, form=form or {}, suggestions=keys,
                   notice=notice, directory=directory, followed=followed, chosen=chosen, fiches=fiches,
                   annuaire=client_directory.state() if new else {},
                   archivable=not new and client_config.exists(cfg.slug))  # fiche sans configuration : rien à archiver


@app.get("/clients/nouveau", response_class=HTMLResponse)
def client_new(request: Request, client: str = ""):
    """Choisir le client dans l'annuaire Notion (relu en fond par Jarvis au démarrage, puis chaque jour) : le formulaire se
    remplit, à relire avant l'ajout. Sans annuaire (pas de Claude Code), le formulaire à la main."""
    directory = client_directory.refresh_if_stale()  # en secours : Jarvis lancé sans sa boucle de fond, ou annuaire effacé
    entry = client_directory.entry(directory, client) if client else None
    if not entry:
        return _client_form(request, None, directory=directory)
    keys = dag_catalogue.catalogue()["clients"]
    form = form_of(entry, keys)
    missing = [label for key, label in (("page_id", "sa fiche « Clients en déploiement » (choisis-la dans la liste du champ)"),
                                        ("notion_url", "son dashboard intégration")) if not form[key]]
    notice = f"{entry['client']} : relis, complète au besoin, puis ajoute." + (f" À compléter : {', '.join(missing)}." if missing else "")
    return _client_form(request, None, form=form, notice=notice, chosen=entry, directory=directory, keys=keys)


@app.post("/clients/annuaire", response_class=HTMLResponse)
def client_directory_refresh(request: Request, lu: str = Form("")):
    """« Relire » : la liste des clients est relue dans Notion (une vingtaine de secondes)."""
    result = client_directory.refresh()
    return _directory_status(request, lu, "" if result.ok else result.message)


@app.get("/clients/annuaire/etat", response_class=HTMLResponse)
def client_directory_status(request: Request, lu: str = ""):
    return _directory_status(request, lu)


def _directory_status(request: Request, lu: str, error: str = "") -> HTMLResponse:
    """Suivi de la lecture. Une première liste arrive : la page se recharge pour l'afficher. Une liste plus récente remplace
    celle affichée (lu) : un lien propose de l'afficher, sans recharger la page sous les doigts de qui remplit le formulaire."""
    directory = client_directory.directory()
    newer = not client_directory.running and directory.get("lu", "") != lu  # lecture finie, liste différente de celle affichée
    if not error and newer and not lu:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return templates.TemplateResponse(request, "_client_directory.html", {
        "annuaire": client_directory.state(), "message": error, "directory": directory, "lu": lu, "updated": newer and bool(lu)})


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
    return _links_page(request, cfg, _local_path(vers, _fiche_url(request, slug)))


@app.post("/clients/{slug}/liens", response_class=HTMLResponse)
async def client_links_save(request: Request, slug: str):
    cfg = client_config.load(slug)
    if cfg is None:
        raise HTTPException(404, f"Client sans configuration : {slug}")
    form = dict(await request.form())
    vers = _local_path(str(form.get("vers") or ""), _fiche_url(request, slug))
    try:
        dashboard = str(form.get("notion_url") or "").strip()
        if dashboard and not dashboard.startswith(("http://", "https://")):
            raise client_config.ConfigError("Le lien du dashboard Notion doit être une adresse https://")
        updated = dataclasses.replace(cfg, notion_url=dashboard, drive=client_config.parse_drive(str(form.get("drive") or "")),
                                      dashboards=client_config.parse_dashboards(str(form.get("dashboards") or "")),
                                      stacks=client_config.parse_stacks(str(form.get("stacks") or "")))
        client_config.save(updated)
    except client_config.ConfigError as e:
        return _links_page(request, cfg, vers, str(e), form)
    return RedirectResponse(vers, status_code=303)


@app.post("/clients/enregistrer", response_class=HTMLResponse)
async def client_save(request: Request, background: BackgroundTasks):
    form = dict(await request.form())
    existing = (form.get("existing_slug") or "").strip() or None
    try:
        cfg = client_config.from_form(form, existing)
        if existing and (previous := client_config.load(existing)):  # ce que le formulaire ne porte pas est gardé
            cfg = dataclasses.replace(cfg, archived=previous.archived, archived_at=previous.archived_at, auto_sync=previous.auto_sync)
        client_config.save(cfg, new=existing is None)
    except client_config.ConfigError as e:
        return _client_form(request, _config_or_fiche(existing)[0] if existing else None, str(e), new=existing is None, form=form)
    if existing:
        return RedirectResponse(f"/clients?enregistre={cfg.slug}", status_code=303)
    _sync_new_client(cfg.slug, background)  # nouveau client : ses données arrivent sans attendre la routine ni « Rafraîchir »
    return RedirectResponse(_fiche_url(request, cfg.slug), status_code=303)


def _sync_new_client(slug: str, background: BackgroundTasks) -> None:
    """Synchro lancée dès l'ajout d'un client, sans la faire attendre : par l'API avec un jeton Notion (juste après la réponse),
    sinon par la routine Claude Code, pour ce client seulement, dont sa fiche montre l'avancement. Une synchro déjà
    en cours : il sera pris à la suivante."""
    if notion_api.load_token():
        background.add_task(_refresh, [slug])
    else:
        sync_routine.start(slug)  # lui seul : les autres clients n'ont pas à être relus pour un ajout
        client_directory.refresh_icons()  # sans jeton : son emoji (icône de son dashboard) est lu à part, tout de suite


def _set_archived(slug: str, archived: bool) -> bool:
    try:
        return client_config.set_archived(slug, archived)
    except client_config.ConfigError as e:
        raise HTTPException(404, str(e))


@app.post("/clients/{slug}/synchro-auto", response_class=HTMLResponse)
def client_auto_sync(request: Request, slug: str, actif: str = Form("")):
    """Bouton « Synchro auto » de la fiche : Jarvis relit ou non Notion tout seul pour ce client (non par défaut)."""
    try:
        client_config.set_auto_sync(slug, actif == "1")
    except client_config.ConfigError as e:
        raise HTTPException(404, str(e))
    return templates.TemplateResponse(request, "_auto_sync.html", {"d": _get(slug)})


@app.post("/clients/{slug}/archiver")
def client_archive(slug: str):
    """On ne travaille plus sur ce client : il sort du menu, de l'accueil, des notifications et de la synchro. Sa fiche,
    ses données et son historique restent en place, consultables depuis Clients → Archivés."""
    _set_archived(slug, True)
    return RedirectResponse(f"/clients?archive={slug}", status_code=303)


@app.post("/clients/{slug}/desarchiver")
def client_unarchive(slug: str, background: BackgroundTasks):
    """Le client revient dans le suivi ; ses données, figées depuis l'archivage, sont resynchronisées comme à un ajout."""
    if _set_archived(slug, False):
        _sync_new_client(slug, background)
    return RedirectResponse(f"/clients?desarchive={slug}", status_code=303)


@app.post("/clients/{slug}/retirer")
def client_retire(slug: str):
    try:
        client_config.retire(slug)
    except client_config.ConfigError as e:
        raise HTTPException(404, str(e))
    return RedirectResponse(f"/clients?retire={slug}", status_code=303)


@app.get("/injections", response_class=HTMLResponse)
def injections(request: Request):
    return _render(request, "list.html", "/injections", title="Injections", part="injections", deployments=_deployments(request),
                   **_views(request, "injections", "vue"))


@app.get("/actions", response_class=HTMLResponse)
def actions(request: Request):
    return _render(request, "list.html", "/actions", title="Actions", part="actions", deployments=_deployments(request),
                   **_list_view(request, "actions"))


@app.get("/recette", response_class=HTMLResponse)
def recette(request: Request):
    return _render(request, "list.html", "/recette", title="Retours de recette", part="recette", deployments=_deployments(request),
                   **_list_view(request, "recette"))


def _month(value: str, today: date) -> date:
    """Premier jour du mois demandé (AAAA-MM), sinon du mois en cours."""
    try:
        year, month = (int(x) for x in value.split("-"))
        return date(year, month, 1)
    except ValueError:
        return today.replace(day=1)


@app.get("/deadlines", response_class=HTMLResponse)
def deadlines(request: Request, tous: bool = False, client: str = ""):
    """Échéances en liste ou en calendrier. client : un slug, ou « tous » (tous=true : idem) ; vide = client en cours.
    periode : retard, aujourdhui ou semaine (compteurs de l'accueil) ; mois (AAAA-MM) : mois affiché par le calendrier.
    Sans periode, vue ni mois dans l'adresse : la période et la vue du dernier choix fait sur les pastilles de la page."""
    periode, vue, mois = _views(request, "deadlines", "periode", "vue", "mois").values()
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
    """Historique des lancements : agents, et une carte par run de DAG (suivis, enchaînements, lancements sans suivi)."""
    for run in claude.runs:
        claude.refresh_context(run)  # agents en cours : où en est leur contexte
    return _render(request, "agents.html", "/agents", runs=claude.runs,
                   cards=board(dag_watch, dag_chains.chains, airflow.runs), usage=usage(claude.runs))


def _run_or_404(run_id: int):
    run = claude.get(run_id)
    if not run:
        raise HTTPException(404, f"Agent #{run_id} inconnu")
    return run


def _agent_context(run) -> dict:
    """Journal de l'agent, et sa relecture guidée dès qu'il a fini (un agent simulé, d'un ancien historique, n'a rien à relire)."""
    path = claude.log_path(run)
    claude.refresh_context(run)
    follow_up = None if run.running else claude.follow_up(run.id)
    relaunch = claude.relaunched(run.id) if run.group == KO else None  # seul un agent en erreur peut avoir été relancé
    waiting = claude.can_reply(run, follow_up)
    intro, questions = agent_questions(run.summary) if waiting else ("", [])  # contexte écrit par l'agent, puis ses questions
    return {"relaunch": relaunch, "relaunchable": run.group == KO and (relaunch is None or relaunch.group == KO),  # relance en échec : on peut réessayer
            "run": run, "log": read_log(path), "review": None if run.running or run.simulated else agent_review.build(run, path),
            "follow_up": follow_up, "waiting": waiting, "intro": intro, "questions": questions}


@app.get("/agents/{run_id}", response_class=HTMLResponse)
def agent_detail(request: Request, run_id: int):
    run = _run_or_404(run_id)
    return _render(request, "agent.html", "/agents", **_agent_context(run))


@app.get("/agents/{run_id}/journal", response_class=HTMLResponse)
def agent_journal(request: Request, run_id: int):
    """Rafraîchi toutes les 5 s par la page de l'agent tant qu'il travaille."""
    run = _run_or_404(run_id)
    return templates.TemplateResponse(request, "_agent_log.html", _agent_context(run))


def _open_new_agent(request: Request, result: ActionResult, new_agent) -> HTMLResponse:
    """Refus affiché sous le bouton ; sinon la page du nouvel agent s'ouvre."""
    if not result.ok:
        return _result(request, result)
    return HTMLResponse("", headers={"HX-Redirect": f"/agents/{new_agent().id}"})


@app.post("/agents/{run_id}/repondre")
async def agent_reply(request: Request, run_id: int):
    """« Répondre et relancer » : par question, l'option cochée (choix_1…) et une réponse libre (reponse_1…), plus une précision
    libre (reponse), assemblées
    question par question ; elles partent à un nouvel agent sur la même branche, ouvert dès son lancement."""
    run = _run_or_404(run_id)
    form = await request.form()
    _, questions = agent_questions(run.summary)
    answers = [chosen_answer(q, str(form.get(f"choix_{i}", "")), str(form.get(f"reponse_{i}", ""))) for i, q in enumerate(questions, 1)]
    answer = answer_text(questions, answers, str(form.get("reponse", "")))
    result = await asyncio.to_thread(claude.reply, run_id, answer)  # git (vérifs, worktree) : hors de la boucle
    return _open_new_agent(request, result, lambda: claude.follow_up(run_id))


@app.post("/agents/{run_id}/relancer")
def agent_relaunch(request: Request, run_id: int):
    """Relance d'un agent fini en erreur, quelle qu'elle soit : même branche, même brief."""
    _run_or_404(run_id)
    return _open_new_agent(request, claude.relaunch(run_id), lambda: claude.relaunched(run_id))


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
                   claude_login=_claude_login or connections.claude_status(sync_routine.claude_bin),  # dernier contrôle (une heure au plus)
                   airflow_message="" if airflow.live else connections.airflow_status(airflow, _airflow_problem())[1])


@app.get("/deploiements/{slug}")
def deployment_root(request: Request, slug: str):
    return RedirectResponse(_fiche_url(request, slug))


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
def open_flow_digger(request: Request, forcer: str = Form(""), relancer: str = Form(""), actuelle: str = Form("")):
    """Démarre Flow Digger sur la VM s'il ne tourne pas (mis à jour s'il y a du nouveau), puis y emmène ; déjà lancé
    mais en retard sur sa branche : propose de le mettre à jour et le relancer, ou d'ouvrir la version en service.
    forcer=1 (page d'échec) : lance la version actuelle sans mise à jour ; relancer=1 : arrête, met à jour et relance ;
    actuelle=1 : ouvre la version en service sans rien vérifier."""
    if not _from_jarvis(request):
        raise HTTPException(403, "Lancement refusé : la demande ne vient pas de Jarvis.")
    launch = flow_digger.start(force=forcer == "1", restart=relancer == "1", current=actuelle == "1")
    if launch.url and not launch.behind:
        return RedirectResponse(launch.url, status_code=303)
    return _render(request, "outil.html", "", tool="Flow Digger", launch=launch, action="/outils/flow-digger")


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
    dag_watch.airflow_env = fresh.air_env()


def _guard_settings(request: Request) -> None:
    """Réglages et mot de passe 1Password : seulement en local, et depuis une page de Jarvis."""
    if not request.client or request.client.host not in shell.LOCAL_HOSTS:
        raise HTTPException(403, "Réglages : connexions locales seulement (transfert de port).")
    if not _from_jarvis(request):
        raise HTTPException(403, "Réglages : la demande ne vient pas de Jarvis.")


def _settings_page(request: Request, message: str = "", error: str = "", op_error: str = "") -> HTMLResponse:
    op = onepassword.status()
    return _render(request, "reglages.html", "/reglages", fields=env_file.view(), sections=env_file.SECTIONS, env_path=env_file.path(), op=op,
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
def switch_client(request: Request, slug: str, vers: str = "/"):
    """Change de client en cours en restant sur la même page : même onglet de fiche, ou même page globale."""
    _get(slug)
    path, _, query = vers.partition("?")
    if m := re.match(r"^/deploiements/[^/]+(/[a-z]+)?$", path):
        target = f"/deploiements/{slug}{m[1]}" if m[1] else _fiche_url(request, slug)
    elif path in CLIENT_FILTERED_PAGES:  # pages globales : leurs filtres suivent le client en cours
        dropped = ("client", "tous") if path == "/deadlines" else ("client",)  # tous=true : tous les clients sur Deadlines seulement
        kept = urlencode([(k, v) for k, v in parse_qsl(query) if k not in dropped])
        target = path + (f"?{kept}" if kept else "")
    else:
        target = _fiche_url(request, slug)
    return _remember(RedirectResponse(target, status_code=303), slug)


@app.post("/fiche/vue")
def fiche_view(request: Request, vue: str = Form("complete"), onglets: list[str] = Form([]), vers: str = Form("/")):
    """Vue complète ou simplifiée des fiches, et les onglets que garde la vue simplifiée : un choix par navigateur (cookie),
    le même pour tous les clients. Sans onglet coché, la vue simplifiée garde ceux d'avant."""
    simple, kept = vue == "simple", _known_tabs(onglets) or _tab_view(request)[1]
    target = _local_path(vers, "/")
    if simple and (m := re.match(r"^(/deploiements/[^/]+)/([a-z]+)$", target.partition("?")[0])) and m[2] not in kept:
        target = f"{m[1]}/{kept[0]}"  # l'onglet ouvert sort de la vue : on passe au premier gardé
    return _keep(RedirectResponse(target, status_code=303), TABS_COOKIE, f"{'simple' if simple else 'complete'}:{'.'.join(kept)}")


def _softwares(d) -> set[str]:
    """Logiciels du client, pour rattacher les PR de son logiciel : ceux de sa fiche et ceux du catalogue des DAGs."""
    return names(d.software) | dag_catalogue.softwares_of(d.orchestrator)


def _agent_pr_urls(slug: str) -> set[str]:
    return {r.pr_url for r in claude.runs if r.slug == slug and r.pr_url}


@app.get("/deploiements/{slug}/pr/liste", response_class=HTMLResponse)
def deployment_pulls(request: Request, slug: str, frais: bool = False):
    """PR ouvertes de l'orchestrator qui concernent ce client ; chargé par l'onglet PR (GitHub peut mettre quelques secondes)."""
    d = _get(slug)
    prs, error = pulls.for_client(d.slug, d.orchestrator, _softwares(d), _agent_pr_urls(slug), frais)
    return templates.TemplateResponse(request, "_pr_list.html", {"d": d, "prs": prs, "error": error, "today": date.today(), "read_at": pulls.read_at})


@app.get("/deploiements/{slug}/pr/historique", response_class=HTMLResponse)
def deployment_pull_history(request: Request, slug: str, logiciel: bool = False, tous: bool = False, frais: bool = False):
    """PR fusionnées ou fermées du client ; options d'affichage, depuis la réception de ses données : PR de son logiciel
    (logiciel) et PR communes à tous les clients (tous)."""
    d = _get(slug)
    since, softwares = d.milestones.get("reception_donnees"), _softwares(d)
    prs, error, updating = pulls.history(d.slug, d.orchestrator, _agent_pr_urls(slug), since, softwares if logiciel else set(), tous,
                                         frais)
    return templates.TemplateResponse(request, "_pr_history.html", {"d": d, "prs": prs, "error": error, "since": since,
                                                                    "softwares": sorted(softwares), "logiciel": logiciel, "tous": tous,
                                                                    "updating": updating})


@app.get("/deploiements/{slug}/{tab}", response_class=HTMLResponse)
def deployment_tab(request: Request, slug: str, tab: str):
    if tab not in dict(DEPLOYMENT_TABS):
        raise HTTPException(404, f"Onglet inconnu : {tab}")
    d = _get(slug)
    request.state.current = d  # la fiche ouverte devient le client en cours
    simple_view, kept_tabs = _tab_view(request)
    page = _render(
        request,
        "deployment.html",
        f"/deploiements/{slug}",
        d=d,
        deployments=[d],
        tabs=DEPLOYMENT_TABS,
        tab=tab,
        simple_view=simple_view,
        kept_tabs=kept_tabs,
        **_list_view(request, tab),  # mêmes choix gardés que la page tous clients de l'onglet
        milestones=[(MILESTONE_LABELS.get(k, k), v) for k, v in sorted(d.milestones.items(), key=lambda kv: kv[1])],
    )
    return page if d.archived else _remember(page, slug)  # une fiche archivée se consulte sans devenir le client en cours


def _launch(dag_id: str, padoa_env: str, d: Deployment | None = None, card: str = "", prod_authorized: bool = False) -> ActionResult:
    return _launch_watched(dag_id, padoa_env, d, card, prod_authorized)[0]


def _launch_watched(dag_id: str, padoa_env: str, d: Deployment | None = None, card: str = "", prod_authorized: bool = False):
    """Lance le DAG ; si le lancement est réel, air.py suit le run (relances, notifications, rapport).
    prod_authorized : seulement quand la personne a tapé PROD. Renvoie le résultat et le suivi (None si rien n'est suivi)."""
    result = airflow.trigger_dag(dag_id, padoa_env, d.client if d else "", card, prod_authorized)
    w = None
    if result.ok and result.run_id:
        customers = d.orchestrator if d else []
        customer = next((c.customer for c in dag_catalogue.dags_for(customers) if c.dag_id == dag_id), "") or \
            next((c["customer"] for c in dag_catalogue.ced_for(customers) if c["dag_id"] == dag_id), "")  # client du catalogue
        w = dag_watch.start(d.slug if d else "", d.client if d else "", customer, dag_id, result.run_id, padoa_env, card)
        result.message += f" · suivi #{w.id} lancé (relances automatiques, rapport à la fin) : Agents → DAG"
    return result, w


@app.post("/actions/dag/{dag_id}", response_class=HTMLResponse)
def trigger_dag(request: Request, dag_id: str, padoa_env: str = Form(...)):
    """DAG d'une ressource de la fiche, sur la stack choisie : jamais de stack par défaut (Airflow prendrait la sienne)."""
    return _result(request, _launch(dag_id, padoa_env))


@app.get("/dags/source", response_class=HTMLResponse)
def dag_source_banner(request: Request):
    """Bandeau de la source des DAGs, relu toutes les 3 s pendant une génération (comme _sync_status) ; dès que rien ne
    tourne, la page se recharge : les listes de DAGs suivent la branche."""
    response = templates.TemplateResponse(request, "_dag_source.html", {"s": dag_source.status(dag_catalogue.CATALOGUE)})
    if not dag_source.running:
        response.headers["HX-Refresh"] = "true"
    return response


@app.post("/dags/regenerer", response_class=HTMLResponse)
def dag_source_regenerate(request: Request):
    """« Régénérer » : catalogue des DAGs refait depuis le clone local, même si sa version est déjà générée ou en échec."""
    if not _from_jarvis(request):
        raise HTTPException(403, "Source des DAGs : la demande ne vient pas de Jarvis.")
    dag_source.refresh(force=True)
    return dag_source_banner(request)


@app.post("/actions/apercu")
def dag_preview(dag_id: str = Form(...), padoa_env: str = Form(...)):
    """Aperçu d'air.py (DAG, stack, ressources, avertissements) montré par dag.js dans la confirmation : ne lance rien."""
    result = airflow.preview(dag_id, padoa_env)
    return {"ok": result.ok, "text": result.message}


@app.post("/actions/injection/{slug}", response_class=HTMLResponse)
def trigger_injection(request: Request, slug: str, dag_id: str = Form(...), padoa_env: str = Form(...), card: str = Form("")):
    """Lance le DAG choisi pour une carte d'injection, sur la stack choisie (padoa_env)."""
    d = _get(slug)
    if not dag_catalogue.known_dag(d.orchestrator, dag_id):
        return _result(request, ActionResult(False, f"{dag_id} n'est pas un DAG de {d.client} dans le catalogue.", simulated=False))
    typed_prod = request.headers.get("HX-Prompt", "").strip() == CONFIRM_PROD
    if padoa_env == "prod" and "prod" in PADOA_ENVS and not typed_prod:
        return _result(request, ActionResult(False, f"Lancement en prod annulé : tape {CONFIRM_PROD} pour confirmer.", simulated=False))
    return _result(request, _launch(dag_id, padoa_env, d, card, prod_authorized=padoa_env == "prod" and typed_prod))


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
    return _result(request, ActionResult(True, f"Enchaînement #{chain.id} lancé sur {padoa_env} : chaque DAG part quand le précédent finit vert"
                                               f" · {' → '.join(dag_ids)} · à suivre dans Agents → DAG", simulated=False))


@app.post("/agents/chaines/{chain_id}/arreter")
def chain_stop(chain_id: int):
    if not dag_chains.get(chain_id):
        raise HTTPException(404, f"Enchaînement #{chain_id} inconnu")
    dag_chains.stop(chain_id)
    return RedirectResponse(f"/agents#chaine-{chain_id}", status_code=303)


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
    # Ce qui empêcherait le lancement, et l'agent déjà lancé sur ce retour : dits avant le clic, pas après.
    branch = claude.retour_branch(d, r)
    previous, problem = claude.latest_on(branch), claude.problem()
    blocker = problem or (f"Un agent travaille déjà sur la branche {branch} : attends sa fin ou arrête-le." if previous and previous.running else "")
    return templates.TemplateResponse(request, "_agent_form.html", {
        "d": d, "r": r, "brief": retour_prompt(d, r, branch=branch), "to_fill": TO_FILL, "branch": branch, "problem": problem, "blocker": blocker,
        "previous": previous, "waiting": bool(previous) and previous.status == WAITING})


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
    if not token:  # sans jeton : routine Claude Code, voir refresh_by_routine
        return False, "Jeton Notion absent : renseigne NOTION_TOKEN dans Réglages."
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


def _sync_status(request: Request, running: bool, ok: bool, message: str, simulated: bool = False, reload: bool = False) -> HTMLResponse:
    """État de la routine sous le bouton : un cercle d'avancement qui se met à jour seul tant qu'elle tourne. À la fin, le
    message reste affiché ; après une vraie réussite (reload), la page se recharge 3 s plus tard pour montrer les données."""
    return templates.TemplateResponse(request, "_sync_status.html", {"running": running, "ok": ok, "message": message,
                                                                     "simulated": simulated, "reload": reload,
                                                                     "progress": sync_routine.progress() if running else None})


def refresh_by_routine(request: Request, d: Deployment | None = None) -> HTMLResponse | None:
    """Sans jeton Notion : la routine, pour ce client seulement ou pour tous ceux de l'app ; None si un jeton permet l'API."""
    if notion_api.load_token():
        return None
    r = sync_routine.start(d.slug, d.client) if d else sync_routine.start()
    return _sync_status(request, r.ok and not r.simulated, r.ok, r.message, r.simulated)


@app.get("/actions/rafraichir/etat", response_class=HTMLResponse)
def refresh_status(request: Request):
    if sync_routine.running:
        return _sync_status(request, True, True, "Synchro Notion en cours…")
    state = sync_routine.state()
    if state.get("running"):  # lancée avant un redémarrage de Jarvis : plus suivie par ce processus
        return _sync_status(request, False, False, "Synchro interrompue : Jarvis a redémarré pendant qu'elle tournait. Relance-la.")
    ok = bool(state.get("ok"))
    if ok:
        return _sync_status(request, False, True, "Synchro terminée : la page se recharge…", reload=True)
    return _sync_status(request, False, False, "Synchro en échec · " + (state.get("summary") or "voir data/notion/routine.log"))


@app.post("/actions/rafraichir", response_class=HTMLResponse)
def refresh_all(request: Request):
    if (routine := refresh_by_routine(request)) is not None:
        return routine
    ok, message = _refresh(notion_api.clients())
    response = _result(request, ActionResult(ok, message, simulated=False))
    if ok:
        response.headers["HX-Refresh"] = "true"
    return response


@app.post("/actions/rafraichir/{slug}", response_class=HTMLResponse)
def refresh_one(request: Request, slug: str):
    d = _get(slug)
    if d.archived:  # la routine ne le lirait pas ; par l'API, il serait resynchronisé alors qu'on ne le suit plus
        return _result(request, ActionResult(False, f"{d.client} est archivé : désarchive-le pour le rafraîchir.", simulated=False))
    if (routine := refresh_by_routine(request, d)) is not None:  # la fiche d'un client ne relit que lui
        return routine
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


templates.env.globals["pins_of"] = _pinned  # encart « Épinglés » de la synthèse d'une fiche


PADOA_STACKS = ("snap", "snap-recette", "integration")
PADOA_ENV_URL = "https://[stack]-[client].aodap-staging.fr/support"  # modèle par défaut ; réglage PADOA_ENV_URL pour le changer


def _env_links(d) -> list[tuple[str, str]]:
    """(environnement, adresse) des environnements Padoa d'un client, pour le bouton Environnements du dock.
    Modèle : réglage PADOA_ENV_URL, où [stack] est l'environnement et [client] le nom du client dans Jarvis (son slug). Vide : modèle par défaut ; pas en http(s) : aucun lien."""
    template = os.environ.get("PADOA_ENV_URL", "").strip() or PADOA_ENV_URL
    if not template.startswith(("https://", "http://")):
        return []
    key = quote(d.slug, safe="")
    return [(stack, template.replace("[stack]", stack).replace("[client]", key)) for stack in PADOA_STACKS]


templates.env.globals["env_links"] = _env_links


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


@app.post("/perso/note", status_code=204)
def general_note_save(texte: str = Form("")) -> Response:
    """Notes générales (sans client) : rangées sous la clé vide de data/perso.json. Réponse vide : l'état « Enregistré »
    est écrit par la page (notes.js), commun aux notes et aux to-do lists."""
    personal.set_note("", texte)
    return Response(status_code=204)


@app.post("/perso/note/{slug}", status_code=204)
def note_save(slug: str, texte: str = Form("")) -> Response:
    _get(slug)
    personal.set_note(slug, texte)
    return Response(status_code=204)


def _todo_list(request: Request, slug: str | None, added: bool = False) -> HTMLResponse:
    """La to-do list de ce client ("" : générale), qui remplace celle où l'on vient de cliquer ; tâche inconnue : 404.
    added : le champ garde le curseur, pour enchaîner les tâches."""
    if slug is None:
        raise HTTPException(404, "Tâche introuvable")
    return templates.TemplateResponse(request, "_todo.html", {"todo_slug": slug, "todo_added": added})


@app.post("/perso/todo", response_class=HTMLResponse)
def todo_add(request: Request, slug: str = Form(""), label: str = Form("")):
    """Ajoute une tâche à la to-do list d'un client (slug), ou à la générale (slug vide)."""
    if slug:
        _get(slug)
    personal.add_todo(slug, label)
    return _todo_list(request, slug, added=True)


@app.post("/perso/todo/{todo_id}/cocher", response_class=HTMLResponse)
def todo_toggle(request: Request, todo_id: int):
    return _todo_list(request, personal.toggle_todo(todo_id))


@app.post("/perso/todo/{todo_id}/supprimer", response_class=HTMLResponse)
def todo_remove(request: Request, todo_id: int):
    return _todo_list(request, personal.remove_todo(todo_id))


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
            for r in personal.due(today) if not r.slug or r.slug in names]  # rappel libre, ou d'un client suivi (pas archivé)


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


@app.get("/sw.js")
def service_worker():
    """Service worker des notifications du Mac, servi à la racine pour couvrir tout le site."""
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/notifications/push/cle")
def push_key():
    return JSONResponse({"key": web_push.public_key()})


@app.post("/notifications/push/abonnement")
async def push_subscribe(request: Request):
    """Abonne un navigateur. Même origine exigée : sinon un autre site pourrait abonner sa propre adresse et recevoir les notifications."""
    if not _from_jarvis(request):
        return JSONResponse({"error": "requête d'une autre origine"}, status_code=403)
    try:
        web_push.subscribe(await request.json())
    except (ValueError, AttributeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "abonnes": len(web_push.subscriptions())})


@app.post("/notifications/push/desabonnement")
async def push_unsubscribe(request: Request):
    if not _from_jarvis(request):
        return JSONResponse({"error": "requête d'une autre origine"}, status_code=403)
    web_push.unsubscribe(str((await request.json()).get("endpoint", "")))
    return JSONResponse({"ok": True})


@app.post("/notifications/push/test")
def push_test(request: Request):
    """Envoie une notification d'essai à chaque navigateur abonné, sans l'enregistrer."""
    if not _from_jarvis(request):
        return JSONResponse({"error": "requête d'une autre origine"}, status_code=403)
    note = Notification(id=0, key="push-test", kind="rappel", level="info", title="Jarvis",
                        detail="Les notifications du Mac fonctionnent, même onglet fermé.", link="/notifications")
    return JSONResponse({"envoyees": web_push.send(note), "abonnes": len(web_push.subscriptions())})


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
