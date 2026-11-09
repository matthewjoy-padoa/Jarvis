import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from deploy_hub import client_config
from deploy_hub.config import PROJECT_ROOT, clients_dir, data_dir, load_settings
from deploy_hub.models import DONE, INJECTION_STATUSES, MILESTONE_LABELS, Deployment, status_group
from deploy_hub.sources import ActionResult
from deploy_hub.sources import dags as dag_catalogue
from deploy_hub.sources.airflow import CONFIRM_PROD, PADOA_ENVS, AirflowSource
from deploy_hub.sources.claude_runner import TO_FILL, ClaudeRunner, retour_prompt
from deploy_hub.sources import notion_api
from deploy_hub.sources.external import external_statuses
from deploy_hub.sources.journal import changes_since
from deploy_hub.sources import watchers
from deploy_hub.sources.notifications import NotificationStore
from deploy_hub.sources.fiches import FicheSource

HERE = Path(__file__).parent

settings = load_settings()
live = settings.mode == "live"
fiches = FicheSource(settings.deployments_dir, clients_dir())
airflow = AirflowSource.from_env()
notifications = NotificationStore(Path(os.getenv("DEPLOY_HUB_NOTIFICATIONS") or settings.deployments_dir.parent / "notifications.json"))
claude = ClaudeRunner.from_env(settings.orchestrator_dir, live, notifications, data_dir() / "agents")
_refresh_lock = threading.Lock()  # un seul rafraîchissement Notion à la fois

app = FastAPI(title="Jarvis")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["status_group"] = lambda s: status_group(s, INJECTION_STATUSES)

# Pages globales (barre du haut) et onglets d'une fiche, calqués sur les blocs du dashboard Notion.
PAGES = [
    ("/", "Accueil"),
    ("/clients", "Clients"),
    ("/deadlines", "Deadlines"),
    ("/agents", "Agents"),
]
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
    ("regles", "Règles"),
    ("accompagnement", "Accompagnement"),
]


def _deployments(request: Request) -> list[Deployment]:
    """Toutes les fiches, lues une seule fois par requête (menu et contenu de la page partagent la même liste)."""
    if not hasattr(request.state, "deployments"):
        request.state.deployments = fiches.list_deployments()
    return request.state.deployments


def _render(request: Request, template: str, active: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {
            "pages": PAGES,
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
        items += [{"date": a.due, "kind": "Action", "label": a.title, "d": d, "url": a.notion_url, "action": a}
                  for a in d.actions if a.due and a.group != DONE]
        items += [{"date": r.due, "kind": "Retour", "label": r.label, "d": d, "url": f"/deploiements/{d.slug}/recette#{r.id}", "retour": r}
                  for r in d.open_retours if r.due]
    return sorted(items, key=lambda i: i["date"])


def _result(request: Request, result: ActionResult) -> HTMLResponse:
    return templates.TemplateResponse(request, "_action_result.html", {"result": result})


@app.get("/", response_class=HTMLResponse)
def today_page(request: Request):
    """Ce qui demande de l'attention aujourd'hui, tous clients : retards, échéances, KO, jalons, et « Quoi de neuf »."""
    deployments = _deployments(request)
    today = date.today()
    items = [i for i in _deadlines(deployments) if i["kind"] != "Jalon"]
    return _render(
        request,
        "today.html",
        "/",
        now=datetime.now(),
        deployments=deployments,
        late=[i for i in items if i["date"] < today],
        due_today=[i for i in items if i["date"] == today],
        due_week=[i for i in items if today < i["date"] <= today + timedelta(days=7)],
        ko=[(d, a) for d in deployments for a in d.injection_cards if a.injection_group == "ko"],
        milestones=[i for i in _deadlines(deployments) if i["kind"] == "Jalon" and today <= i["date"] <= today + timedelta(days=21)],
    )


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
    return templates.TemplateResponse(request, "_whats_new.html", {"changes": changes, "since": since, "show_client": not client})


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
def actions(request: Request, tous: bool = False):
    return _render(
        request, "list.html", "/actions", title="Actions", part="actions", deployments=_deployments(request), show_all=tous
    )


@app.get("/recette", response_class=HTMLResponse)
def recette(request: Request, tous: bool = False):
    return _render(
        request, "list.html", "/recette", title="Retours de recette", part="recette", deployments=_deployments(request), show_all=tous
    )


@app.get("/deadlines", response_class=HTMLResponse)
def deadlines(request: Request):
    return _render(request, "deadlines.html", "/deadlines", items=_deadlines(_deployments(request)))


@app.get("/agents", response_class=HTMLResponse)
def agents(request: Request):
    return _render(request, "agents.html", "/agents", runs=claude.runs, dag_runs=airflow.runs, live=claude.live)


def _run_or_404(run_id: int):
    run = claude.get(run_id)
    if not run:
        raise HTTPException(404, f"Agent #{run_id} inconnu")
    return run


@app.get("/agents/{run_id}", response_class=HTMLResponse)
def agent_detail(request: Request, run_id: int):
    run = _run_or_404(run_id)
    return _render(request, "agent.html", "/agents", run=run, log=claude.log(run))


@app.get("/agents/{run_id}/journal", response_class=HTMLResponse)
def agent_journal(request: Request, run_id: int):
    """Rafraîchi toutes les 5 s par la page de l'agent tant qu'il travaille."""
    run = _run_or_404(run_id)
    return templates.TemplateResponse(request, "_agent_log.html", {"run": run, "log": claude.log(run)})


@app.post("/agents/{run_id}/arreter")
def agent_stop(run_id: int):
    _run_or_404(run_id)
    claude.stop(run_id)
    return RedirectResponse(f"/agents/{run_id}", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
def sources(request: Request):
    return _render(request, "sources.html", "/sources", externals=external_statuses(notion_api.load_token() is not None), settings=settings)


@app.get("/deploiements/{slug}")
def deployment_root(slug: str):
    return RedirectResponse(f"/deploiements/{slug}/synthese")


@app.get("/deploiements/{slug}/{tab}", response_class=HTMLResponse)
def deployment_tab(request: Request, slug: str, tab: str, tous: bool = False, vue: str = "ressources"):
    if tab not in dict(DEPLOYMENT_TABS):
        raise HTTPException(404, f"Onglet inconnu : {tab}")
    d = _get(slug)
    return _render(
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
    )


@app.post("/actions/dag/{dag_id}", response_class=HTMLResponse)
def trigger_dag(request: Request, dag_id: str):
    """DAG d'une ressource de la fiche (démo)."""
    return _result(request, airflow.trigger_dag(dag_id))


@app.post("/actions/injection/{slug}", response_class=HTMLResponse)
def trigger_injection(request: Request, slug: str, dag_id: str = Form(...), padoa_env: str = Form(...), card: str = Form("")):
    """Lance le DAG choisi pour une carte d'injection, sur la stack choisie (padoa_env)."""
    d = _get(slug)
    if not dag_catalogue.known_dag(d.orchestrator, dag_id):
        return _result(request, ActionResult(False, f"{dag_id} n'est pas un DAG de {d.client} dans le catalogue.", simulated=False))
    if padoa_env == "prod" and "prod" in PADOA_ENVS and request.headers.get("HX-Prompt", "").strip() != CONFIRM_PROD:
        return _result(request, ActionResult(False, f"Lancement en prod annulé : tape {CONFIRM_PROD} pour confirmer.", simulated=False))
    return _result(request, airflow.trigger_dag(dag_id, padoa_env, d.client, card))


@app.post("/actions/claude/{slug}/{resource}", response_class=HTMLResponse)
def launch_claude(request: Request, slug: str, resource: str):
    return _result(request, claude.launch(_get(slug), resource))


def _retour(slug: str, retour_id: str):
    d = _get(slug)
    retour = next((r for r in d.retours if r.id == retour_id), None)
    if retour is None:
        raise HTTPException(404, f"Retour inconnu : {retour_id}")
    return d, retour


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
