#!/usr/bin/env python3
"""Drive Arthur's Airflow on the VM through its REST API (backend of the /air skill).

The Airflow UI/API is reached through the SSH tunnel Arthur opens himself (alias `vm`):
http://localhost:8081. VPNs, the tunnel and `runair` are his job; this script never touches the VM
any other way (no ssh: `pritunl-client list` exposes confidential IPs and ids).

  air.py health                                  tunnel + scheduler status
  air.py client [NAME]                           show / set the default client
  air.py add-client NAME                         append NAME to the Airflow Variable active_clients
  air.py dags [--client C]                       list the client's DAGs
  air.py status [--client C]                     running / queued runs of the client
  air.py resolve KIND [group] RESOURCE           find the DAG (KIND: ETL | TL), exit 3 if absent
  air.py run DAG_ID STACK [--yes] [--prod-authorized]
                                                 unpause if paused, trigger with conf {"padoa_env": STACK}; preview without --yes
  air.py watch DAG_ID [RUN_ID]                   follow a run: clear transient failures one by one,
                                                 macOS notifications, report + journal at the end
  air.py report DAG_ID [RUN_ID] [--journal]      durations, loader volumes, cleaner, compare, mappings

STACK: snap | recette | integration (alias inte) | prod. prod is refused without --prod-authorized,
which the skill passes only on Arthur's explicit permission for that run (CLAUDE.md hard rule).

Credentials: AIRFLOW_USER / AIRFLOW_PASSWORD (UI login), from the environment or .claude/air.env
(git-ignored, KEY=VALUE lines). AIRFLOW_URL defaults to http://localhost:8081.
State: .claude/state/air.json (default client) and .claude/state/air/ (watch state, reports).
Journal of finished runs: journal/airflow-runs.md.

Jarvis : script d'Arthur repris tel quel, sauf trois adaptations repérées « Jarvis » (2026-11-12).
Jarvis lance `python -m deploy_hub.air watch DAG_ID RUN_ID --client C` après chaque « Lancer » réel
(sources/dag_watch.py) avec AIRFLOW_URL / AIRFLOW_USER / AIRFLOW_PASSWORD, AIR_STATE_DIR (data/airflow : état,
rapports, journal, jamais le vault : ils contiennent des valeurs client) et AIR_NOTIFY=print (notifications de Jarvis).
Ce suivi lit les logs des tâches qu'il suit (classement des erreurs, rapport) : dérogation décidée par Matthew le 2026-11-12.
"""
import argparse, ast, base64, html, http.client, http.cookiejar, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

VAULT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Jarvis : état, rapports et journal dans AIR_STATE_DIR (data/airflow) quand il est donné
STATE = os.environ.get("AIR_STATE_DIR") or os.path.join(VAULT, ".claude", "state")
CONF = os.path.join(STATE, "air.json")
RUNS = os.path.join(STATE, "air")
JOURNAL = os.path.join(STATE, "airflow-runs.md") if os.environ.get("AIR_STATE_DIR") else os.path.join(VAULT, "journal", "airflow-runs.md")
# Short names Arthur uses for a resource or group → the name in the dag_id
ALIASES = {"fe": "fe_module"}   # FE = fiche d'entreprise
STACKS = {"snap": "snap", "recette": "recette", "integration": "integration", "inte": "integration", "prod": "prod"}

# Failures worth a clear: infrastructure hiccups (Arthur's list); tasks killed with -9 too (KILLED, his choice 2026-10-15)
TRANSIENT = re.compile(r"No space left on device|Read timed? ?out|ReadTimeout|timed out|connection to server|"
                       r"could not connect to server|server closed the connection|Connection refused|"
                       r"ConnectionError|ConnectionResetError|SYSCALL|EOF", re.I)
# Code or data errors: never cleared, notified at once
FATAL = re.compile(r"syntax error|SyntaxError|ProgrammingError|UndefinedColumn|UndefinedTable|\b(KeyError|ValueError|"
                   r"TypeError|AttributeError|IndexError|NameError|ZeroDivisionError|AssertionError|ImportError|"
                   r"ModuleNotFoundError|NotImplementedError|UnboundLocalError|RecursionError)\b")
# The task process was killed from outside (return code -9 = SIGKILL, usually out of memory)
KILLED = re.compile(r"Task exited with return code -9|return code -9|SIGKILL|Killed\b")
# The worker that ran the task is gone (tunnel or containers restarted): its log is lost, worth a clear (Arthur, 2026-10-21)
LOGS_LOST = re.compile(r"Could not read served logs.*(Invalid URL|No host supplied)", re.S)
ACTIVE = (None, "none", "scheduled", "queued", "running", "up_for_retry", "up_for_reschedule", "restarting", "deferred")
LOG_PREFIX = re.compile(r"^\[[^\]]+\] \{[^}]+\} [A-Z]+ - ")
TL_RESOURCE = re.compile(r"transform_and_load_([^.]+)\.")  # task group -> resource


# ---------- plumbing ----------

def env(key, default=None):
    if os.environ.get(key):
        return os.environ[key]
    path = os.path.join(VAULT, ".claude", "air.env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k.strip() == key and v:
                return v.strip().strip("'\"")
    return default


BASE = env("AIRFLOW_URL", "http://localhost:8081").rstrip("/")


class Down(Exception):
    """The tunnel (or Airflow's webserver) doesn't answer."""


def call(method, path, body=None, text=False, timeout=60):
    user, pwd = env("AIRFLOW_USER"), env("AIRFLOW_PASSWORD")
    headers = {"Accept": "text/plain" if text else "application/json"}
    if user and pwd:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            sys.exit("401: identifiants refusés ou absents. Renseigne AIRFLOW_USER / AIRFLOW_PASSWORD dans .claude/air.env")
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {detail}")
    except http.client.IncompleteRead as e:     # a huge log (200+ MB) cut mid-transfer: not a tunnel outage
        raise RuntimeError(f"réponse tronquée {method} {path}: {len(e.partial)} octets reçus")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise Down(str(e))
    return raw if text else (json.loads(raw) if raw else {})


def q(s):
    return urllib.parse.quote(str(s), safe="")


def paged(path, key, limit=100):
    out, offset = [], 0
    sep = "&" if "?" in path else "?"
    while True:
        page = call("GET", f"{path}{sep}limit={limit}&offset={offset}")
        out += page.get(key, [])
        offset += limit
        if offset >= page.get("total_entries", 0):
            return out


def load_conf():
    try:
        return json.load(open(CONF))
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(data, open(tmp, "w"), indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def client_of(args):
    c = getattr(args, "client", None) or load_conf().get("client")
    if not c:
        sys.exit("Aucun client par défaut : /air set client \"<client>\"")
    return c


def notify(title, msg, open_url=None):
    """macOS notification. With terminal-notifier (brew install terminal-notifier) a click opens the run journal in
    Obsidian; with the osascript fallback a click opens Script Editor on an empty document (macOS behaviour).
    Jarvis : AIR_NOTIFY=print, ou pas de macOS (VM) → une ligne « [notif] {json} » seule, que Jarvis change en notification."""
    if os.environ.get("AIR_NOTIFY") == "print" or not (shutil.which("terminal-notifier") or shutil.which("osascript")):
        print("[notif] " + json.dumps({"title": title, "msg": msg}, ensure_ascii=False), flush=True)  # lu par dag_watch.py
        return
    open_url = open_url or "obsidian://open?vault=" + q(os.path.basename(VAULT)) + "&file=" + q("journal/airflow-runs")
    tn = shutil.which("terminal-notifier") or next((p for p in ("/opt/homebrew/bin/terminal-notifier",
                                                                "/usr/local/bin/terminal-notifier") if os.path.exists(p)), None)
    if tn:
        subprocess.run([tn, "-title", title, "-message", msg, "-sound", "Glass", "-open", open_url],
                       capture_output=True)
    else:
        esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", f'display notification "{esc(msg)}" with title "{esc(title)}" sound name "Glass"'],
                       capture_output=True)
    print(f"[notif] {title} · {msg}", flush=True)


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def fmt_dur(sec):
    if sec is None:
        return "?"
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min {sec % 60:02d} s"


def fmt_n(n):
    return f"{n:,}".replace(",", " ")


# ---------- DAGs ----------

STATE_ICON = {"success": "✅ success", "failed": "❌ failed", "running": "🔄 running", "queued": "⏳ queued"}


def short_id(dag_id, client):
    return dag_id[len(client) + 1:] if dag_id.startswith(client + "-") else dag_id


def fmt_date(s):
    d = parse_ts(s)
    return f"{d:%Y-%m-%d %H:%M}" if d else "–"


def last_runs(dag_ids):
    """Latest run of each DAG, in one batch call."""
    out = {}
    if not dag_ids:
        return out
    runs = call("POST", "/api/v1/dags/~/dagRuns/list",
                {"dag_ids": dag_ids, "order_by": "-execution_date", "page_limit": 10000}).get("dag_runs", [])
    for r in runs:
        out.setdefault(r["dag_id"], r)
    return out



def client_dags(client):
    dags = paged(f"/api/v1/dags?dag_id_pattern={q(client)}", "dags")
    return sorted((d for d in dags if d["dag_id"].startswith(client + "-")), key=lambda d: d["dag_id"])


def resolve(client, kind, group, resource):
    kind, resource = kind.upper(), resource.lower().replace("-", "_")
    resource = ALIASES.get(resource, resource)
    hits = []
    for d in client_dags(client):
        rest = d["dag_id"][len(client) + 1:].lower().replace("-", "_")
        toks = rest.split("_")
        is_etl = "etl" in toks
        is_tl = not is_etl and any(t in ("transform", "tranform") for t in toks) and "load" in toks
        if (kind == "ETL" and not is_etl) or (kind == "TL" and not is_tl):
            continue
        if ("group" in toks) != group:
            continue
        if rest.endswith("_" + resource):
            hits.append(d)
    return hits


def latest_run(dag_id):
    runs = call("GET", f"/api/v1/dags/{q(dag_id)}/dagRuns?order_by=-execution_date&limit=1").get("dag_runs", [])
    if not runs:
        sys.exit(f"Aucun run pour {dag_id}")
    return runs[0]


# The 2.10 REST API clears by task_id only (every failed batch of a mapped task at once, which runs out of memory).
# The UI's own /clear endpoint takes a map_index: used to clear one batch at a time, with a logged-in UI session.
_ui = None


def ui_session():
    global _ui
    if _ui:
        return _ui
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    token = lambda page: (re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
                          or re.search(r'name="csrf_token" content="([^"]+)"', page)
                          or re.search(r'csrf_token[^>]*content="([^"]+)"', page))
    try:
        page = opener.open(BASE + "/login/", timeout=30).read().decode("utf-8", "replace")
        m = token(page)
        form = urllib.parse.urlencode({"username": env("AIRFLOW_USER"), "password": env("AIRFLOW_PASSWORD"),
                                       "csrf_token": m.group(1) if m else ""}).encode()
        page = opener.open(urllib.request.Request(BASE + "/login/", data=form), timeout=30).read().decode("utf-8", "replace")
        if 'name="password"' in page and "/logout" not in page:
            raise RuntimeError("connexion à l'interface Airflow refusée (identifiants ?)")
        page = opener.open(BASE + "/home", timeout=30).read().decode("utf-8", "replace")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise Down(str(e))
    m = token(page)
    if not m:
        raise RuntimeError("jeton CSRF introuvable dans l'interface Airflow")
    _ui = (opener, html.unescape(m.group(1)))
    return _ui


def ui_clear(dag_id, run_id, task_id, map_index, dry=False):
    """Clear one batch (map_index) of a mapped task and its failed downstream tasks, like the UI's Clear button."""
    global _ui
    run = call("GET", f"/api/v1/dags/{q(dag_id)}/dagRuns/{q(run_id)}")
    logical = run.get("logical_date") or run.get("execution_date")
    for attempt in (1, 2):
        opener, csrf = ui_session()
        form = urllib.parse.urlencode([("dag_id", dag_id), ("dag_run_id", run_id), ("execution_date", logical), ("task_id", task_id),
                                       ("map_index", str(map_index)), ("confirmed", "false" if dry else "true"),
                                       ("upstream", "false"), ("downstream", "true"), ("future", "false"),
                                       ("past", "false"), ("recursive", "true"), ("only_failed", "true")]).encode()
        req = urllib.request.Request(BASE + "/clear", data=form, method="POST",
                                     headers={"X-CSRFToken": csrf, "Accept": "application/json",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with opener.open(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if attempt == 1 and e.code in (400, 401, 403):
                _ui = None          # expired session or CSRF token: log in again once
                continue
            raise RuntimeError(f"HTTP {e.code} POST /clear: {e.read().decode('utf-8', 'replace')[:300]}")
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise Down(str(e))


# ---------- logs ----------

def tis(dag_id, run_id):
    return paged(f"/api/v1/dags/{q(dag_id)}/dagRuns/{q(run_id)}/taskInstances", "task_instances")


def ti_now(dag_id, run_id, ti):
    """Fresh copy of a task instance (its state may have changed since the list was read, e.g. cleared in the UI)."""
    mi = ti.get("map_index", -1)
    path = f"/api/v1/dags/{q(dag_id)}/dagRuns/{q(run_id)}/taskInstances/{q(ti['task_id'])}" + (f"/{mi}" if mi >= 0 else "")
    return call("GET", path)


def ti_label(ti):
    return ti["task_id"] + (f"[{ti['map_index']}]" if ti.get("map_index", -1) >= 0 else "")


def ti_log(dag_id, run_id, ti, try_number=None):
    n = try_number or ti.get("try_number") or 0
    if n < 1:
        return ""
    mi = ti.get("map_index", -1)
    path = (f"/api/v1/dags/{q(dag_id)}/dagRuns/{q(run_id)}/taskInstances/{q(ti['task_id'])}/logs/{n}"
            f"?full_content=true" + (f"&map_index={mi}" if mi >= 0 else ""))
    try:
        return call("GET", path, text=True, timeout=120)
    except (RuntimeError, Down) as e:
        return f"<log indisponible: {e}>"


def error_part(log):
    """The end of the log from the last traceback / ERROR record / exit line, where the failure is (DEBUG lines dropped)."""
    lines = [l for l in log.splitlines() if " DEBUG - " not in l]
    for marker in ("Traceback (most recent call last)", " ERROR - ", "Task exited with return code"):
        for i in range(len(lines) - 1, -1, -1):
            if marker in lines[i]:
                return "\n".join(lines[max(0, i - 3):])
    return "\n".join(lines[-80:])


def last_error_line(text):
    lines = [LOG_PREFIX.sub("", l).strip() for l in text.splitlines()]
    for pat in (r"Task exited with return code", r"^\w[\w.]*(Error|Exception)\b.*:", r"(Error|Exception|EOF|timed out)"):
        hits = [l for l in lines if re.search(pat, l)]
        if hits:
            return re.sub(r"(password|pwd|token|secret)=\S+", r"\1=***", hits[-1])[:200]
    return (lines[-1] if lines else "?")[:200]


def classify(log):
    part = error_part(log)
    if FATAL.search(part):
        return "fatal", part
    if KILLED.search(part):
        return "killed", part
    if TRANSIENT.search(part) or LOGS_LOST.search(log):
        return "transient", part
    return "unknown", part


# ---------- report ----------

def json_records(log):
    for line in log.splitlines():
        i = line.find('{"client_name"')
        if i >= 0:
            try:
                yield json.loads(line[i:])
            except ValueError:
                pass


def cleaner_blocks(log):
    raw = log.splitlines()
    lines = [LOG_PREFIX.sub("", l).rstrip() for l in raw]
    dash = lambda s: len(s.strip()) > 3 and set(s.strip()) == {"-"}
    blocks, i = [], 0
    while i < len(lines):
        if lines[i].strip() != "Types d'incohérences":
            i += 1
            continue
        start = i - 1 if i and dash(lines[i - 1]) else i
        j, seen_cols, underline = i + 1, False, False
        while j < len(lines):
            if LOG_PREFIX.match(raw[j]) and not dash(lines[j]):
                j -= 1                       # a new log record: the block ended without its closing line
                break
            if lines[j].startswith("Éléments incohérents"):
                seen_cols = True
            elif seen_cols and dash(lines[j]):
                if underline:
                    break                    # closing line
                underline = True
            j += 1
        blocks.append("\n".join(lines[start:j + 1]))
        i = j + 1
    return blocks


MAPPING_RE = re.compile(r"Les valeurs (.+?) ne sont pas présentes dans le mapping (.+?)\.\s*$")
VALUE_RE = re.compile(r'"((?:[^"\\]|\\.)*)" \((\d+)\)')


def build_report(dag_id, run_id, journal=False):
    run = call("GET", f"/api/v1/dags/{q(dag_id)}/dagRuns/{q(run_id)}")
    stack = (run.get("conf") or {}).get("padoa_env", "?")
    all_tis = tis(dag_id, run_id)
    # The run's start_date is reset by every clear: take the first task start and the last task end
    starts = [parse_ts(t["start_date"]) for t in all_tis if t.get("start_date")]
    ends = [parse_ts(t["end_date"]) for t in all_tis if t.get("end_date")]
    start = min(starts) if starts else parse_ts(run.get("start_date"))
    end = max(ends) if ends else parse_ts(run.get("end_date"))
    run_sec = (end - start).total_seconds() if start and end else None

    per_task = defaultdict(lambda: {"n": 0, "sum": 0.0, "states": defaultdict(int)})
    for t in all_tis:
        pt = per_task[t["task_id"]]
        pt["n"] += 1
        pt["sum"] += t.get("duration") or 0
        pt["states"][t.get("state") or "none"] += 1

    logs = {}
    with_log = [t for t in all_tis if (t.get("try_number") or 0) > 0 and t.get("state") in ("success", "failed")]
    with ThreadPoolExecutor(8) as ex:
        for t, log in zip(with_log, ex.map(lambda t: ti_log(dag_id, run_id, t), with_log)):
            logs[ti_label(t)] = log

    loader = defaultdict(lambda: defaultdict(int))
    compare = defaultdict(dict)
    cleaner = []
    mappings = defaultdict(lambda: defaultdict(int))
    for label, log in sorted(logs.items()):
        m = TL_RESOURCE.search(label)  # fallback when the log never names its resource
        last_res = m.group(1) if m else None
        for rec in json_records(log):
            res = rec.get("resource") or last_res
            last_res = res or last_res
            tv, val = rec.get("type_value"), rec.get("value")
            if (rec.get("step") or "").lower() == "loader" and isinstance(val, (int, float)):
                loader[res][tv] += val
            elif rec.get("step") == "db_comparison" and tv in ("not_linked_mandatory", "not_linked_optional"):
                compare[res][tv] = val
            for outcome, n in re.findall(r"DbComparisonOutcome\.(\w+): \((\d+),", rec.get("message") or ""):
                compare[res][outcome] = int(n)
                if outcome in ("NOT_LINKED_MANDATORY", "NOT_LINKED_OPTIONAL"):  # no db_comparison line for some resources
                    compare[res].setdefault(outcome.lower(), int(n))
        for b in cleaner_blocks(log):
            cleaner.append((label, last_res, b))
        for line in log.splitlines():
            m = MAPPING_RE.search(line)
            if m:
                for v, n in VALUE_RE.findall(m.group(1)):
                    mappings[m.group(2)][v] = max(mappings[m.group(2)][v], int(n))

    icon = {"success": "✅", "failed": "❌"}.get(run["state"], "⏳")
    if run["state"] == "success" and any(t.get("state") in ("failed", "upstream_failed") for t in all_tis):
        icon = "⚠️"
    out = [f"## {icon} {dag_id} · {stack} · {run['state']}",
           f"- Run `{run_id}` · début {start:%Y-%m-%d %H:%M} UTC · durée **{fmt_dur(run_sec)}**" if start else f"- Run `{run_id}`"]
    # The worker that ran the task is gone (e.g. runair restarted): its served logs died with it.
    lost = [l for l, log in logs.items() if log.startswith("<log indisponible") or "Could not read served logs" in log]
    if lost:
        out += ["", f"⚠️ Logs introuvables pour {len(lost)}/{len(logs)} tâches (worker disparu, p. ex. runair redémarré) : "
                    "volumétrie, cleaner, compare et mappings manquants sont incomplets" + (" ou absents." if len(lost) == len(logs) else ".")]
    tot_c = sum(v.get("create", 0) for v in loader.values())
    tot_u = sum(v.get("update", 0) for v in loader.values())
    if loader:
        out += ["", f"### Volumétrie injectée (loader) · {fmt_n(tot_c)} créations · {fmt_n(tot_u)} mises à jour",
                "| resource | create | update |", "|---|---:|---:|"]
        out += [f"| {r} | {fmt_n(v.get('create', 0))} | {fmt_n(v.get('update', 0))} |" for r, v in sorted(loader.items(), key=lambda x: str(x[0]))]
    if compare:
        out += ["", "### Compare and identify", "| resource | in flow only | flow ≠ db | not linked mandatory | not linked optional |",
                "|---|---:|---:|---:|---:|"]
        for r, v in sorted(compare.items(), key=lambda x: str(x[0])):
            g = lambda k: fmt_n(v[k]) if k in v else "–"
            out.append(f"| {r} | {g('IN_FLOW_ONLY')} | {g('IN_FLOW_AND_DB_DIFFERENT')} | {g('not_linked_mandatory')} | {g('not_linked_optional')} |")
    if cleaner:
        out += ["", "### Cleaner"]
        for label, res, b in cleaner:
            out += [f"`{label}`" + (f" · {res}" if res else ""), "```", b, "```"]
    longest = sorted(per_task.items(), key=lambda x: -x[1]["sum"])[:5]
    out += ["", "### Tâches les plus longues", "| task | instances | durée cumulée |", "|---|---:|---:|"]
    out += [f"| {k} | {v['n']} | {fmt_dur(v['sum'])} |" for k, v in longest]
    failed = [k for k, v in per_task.items() if v["states"].get("failed")]
    if failed:
        out += ["", "⚠️ Tâches en échec : " + ", ".join(f"`{k}`" for k in failed)]
    n_values = sum(len(v) for v in mappings.values())
    if mappings:
        out += ["", f"### Mappings manquants ({n_values} valeurs)"]
        for name, vals in mappings.items():
            vol = sum(vals.values())
            ordered = sorted(vals.items(), key=lambda x: -x[1])
            out += [f"**{name}** · {len(vals)} valeurs · volumétrie {fmt_n(vol)}", "", "`mappings.py` :", "```python"]
            out += [f"    {v!r}: ENUM_TODO.TODO,  # {n}" for v, n in ordered]
            out += ["```", "Excel / Google Sheet (mapping · valeur · volumétrie) :", "```"]
            out += [f"{name}\t{v}\t{n}" for v, n in ordered]
            out += ["```", "Slack (ops) :", "```",
                    f'{len(vals)} nouvelles valeurs de mapping à valider "{name}" (volumétrie : {fmt_n(vol)}).', "```"]
    text = "\n".join(out)

    os.makedirs(RUNS, exist_ok=True)
    path = os.path.join(RUNS, re.sub(r"[^\w.-]", "_", f"{dag_id}__{run_id}") + ".md")
    open(path, "w", encoding="utf-8").write(text + "\n")
    if journal:
        append_journal(dag_id, run_id, run, stack, start, run_sec, loader, tot_c, tot_u, compare, cleaner, mappings)
    summary = {"state": run["state"], "duration": fmt_dur(run_sec), "create": tot_c, "update": tot_u,
               "mapping_values": n_values, "logs_lost": len(lost), "report": os.path.relpath(path, VAULT)}
    return text, summary


def append_journal(dag_id, run_id, run, stack, start, run_sec, loader, tot_c, tot_u, compare, cleaner, mappings):
    if not os.path.exists(JOURNAL):
        open(JOURNAL, "w", encoding="utf-8").write(
            "# Airflow runs\n\nFinished DAG runs followed by `/air` (newest last): injection date, volumes, duration, "
            "data problems and missing mappings to report. Appended by `.claude/scripts/air.py`; not a wiki source.\n")
    icon = {"success": "✅", "failed": "❌"}.get(run["state"], "⏳")
    lines = ["", f"## {start:%Y-%m-%d} · {dag_id} · {stack} {icon}" if start else f"## {dag_id} · {stack} {icon}",
             f"- Run `{run_id}` · {start:%H:%M} UTC · durée {fmt_dur(run_sec)}" if start else f"- Run `{run_id}`"]
    if loader:
        lines.append(f"- Loader : {fmt_n(tot_c)} create · {fmt_n(tot_u)} update — " +
                     ", ".join(f"{r} {fmt_n(v.get('create', 0))}/{fmt_n(v.get('update', 0))}" for r, v in sorted(loader.items(), key=lambda x: str(x[0]))))
    for r, v in sorted(compare.items(), key=lambda x: str(x[0])):
        if v.get("not_linked_mandatory"):
            lines.append(f"- Compare {r} : {fmt_n(v['not_linked_mandatory'])} not linked mandatory, "
                         f"{fmt_n(v.get('not_linked_optional', 0))} optional")
    for label, res, b in cleaner:
        kinds = re.findall(r"^\s*(\d+)\s*> ([^:\n]+?)\s*$", b, re.M)
        if kinds:
            lines.append(f"- Cleaner {res or label} : " + " · ".join(f"{k} {fmt_n(int(n))}" for n, k in kinds))
    for name, vals in mappings.items():
        lines.append(f"- Mapping « {name} » : {len(vals)} valeurs manquantes (volumétrie {fmt_n(sum(vals.values()))}) — "
                     + ", ".join(f'"{v}"' for v, _ in sorted(vals.items(), key=lambda x: -x[1])))
        lines += ["  - [ ] signalé aux ops (Slack)", "  - [ ] ajouté au Google Sheet du client", "  - [ ] ajouté dans `mappings.py`"]
    open(JOURNAL, "a", encoding="utf-8").write("\n".join(lines) + "\n")


# ---------- watch ----------

def ti_times(c):
    return parse_ts(c.get("start_date")), parse_ts(c.get("end_date"))


def kill_slow_wave(dag_id, run_id, st, wave, running):
    """Parallel wave slower than running its batches one by one would be: stop it, go mono for good (Arthur's rule)."""
    ad = st.get("adapt", {}).get(wave["task_id"])
    if wave.get("single") or wave.get("killed") or wave["par"] < 2 or not ad or not ad.get("base"):
        return
    starts = [ti_times(c)[0] for c in running if c.get("start_date")]
    if not starts:
        return
    elapsed = (datetime.now(timezone.utc) - min(starts)).total_seconds()
    limit = max(wave["par"] * ad["base"], ad["base"] + 120)
    if elapsed <= limit:
        return
    for c in running:
        path = f"/api/v1/dags/{q(dag_id)}/dagRuns/{q(run_id)}/taskInstances/{q(c['task_id'])}/{c['map_index']}"
        call("PATCH", path, {"dry_run": False, "new_state": "failed"})
        st.setdefault("requeue", []).append(ti_label(c))
    wave["killed"] = True
    ad.update(par=1, cap=1)
    print(f"Vague de {wave['par']} batchs arrêtée après {fmt_dur(elapsed)} (un batch seul : {fmt_dur(ad['base'])}) : "
          f"relance 1 par 1 désormais, sans réaugmenter", flush=True)


def adapt_after_wave(st, wave, cur):
    """Choose the next wave size from the one that just ended (Arthur's method, 2026-10-15):
    1 batch alone → its time is the reference; then 2 together, 3… while a wave of n takes clearly less time than
    n batches one after the other; back down (and never above again) when it doesn't pay or batches fail."""
    if wave.get("single") or wave.get("killed"):
        return
    ad = st.setdefault("adapt", {}).setdefault(wave["task_id"], {"par": 1, "base": None, "cap": None})
    n = wave["par"]
    ok = [c for c in cur if c and c.get("state") == "success"]
    if len(ok) < len(cur):
        if n > 1:
            ad["par"] = ad["cap"] = n - 1
            print(f"Vague de {n} batchs : {len(cur) - len(ok)} en échec → prochaines vagues de {n - 1}", flush=True)
        return
    durs = [c["duration"] for c in ok if c.get("duration")]
    times = [ti_times(c) for c in ok]
    if not durs or not all(s and e for s, e in times):
        return
    avg = sum(durs) / len(durs)
    wall = (max(e for _, e in times) - min(s for s, _ in times)).total_seconds()
    if n == 1:
        ad["base"] = avg if not ad["base"] else min(ad["base"], avg)
        nxt, why = 2, f"batch seul en {fmt_dur(avg)}"
    elif wall < 0.8 * n * ad["base"]:
        nxt, why = n + 1, f"{n} batchs en {fmt_dur(wall)} (vs {fmt_dur(n * ad['base'])} un par un) : rentable"
    else:
        nxt, why = n - 1, f"{n} batchs en {fmt_dur(wall)} (vs {fmt_dur(n * ad['base'])} un par un) : pas rentable"
        ad["cap"] = nxt
    if ad.get("cap"):
        nxt = min(nxt, ad["cap"])
    ad["par"] = max(1, nxt)
    print(f"Vague terminée : {why} → prochaine vague : {ad['par']} batch(s)", flush=True)


def watch(dag_id, run_id, max_clears, interval, client, clear_killed=True):
    path = os.path.join(RUNS, re.sub(r"[^\w.-]", "_", f"watch__{dag_id}__{run_id}") + ".json")
    st = {"clears": {}, "notified": {}}
    if os.path.exists(path):
        st.update(json.load(open(path)))
    title = f"Airflow · {client}"
    short = dag_id[len(client) + 1:] if dag_id.startswith(client + "-") else dag_id
    down_since, last_tick = None, time.time()
    print(f"Suivi de {dag_id} / {run_id} (relances max {max_clears} par tâche, toutes les {interval} s)", flush=True)
    while True:
        now = time.time()
        if now - last_tick > 3 * interval + 60:
            print(f"Reprise après une pause de {fmt_dur(now - last_tick)} (veille ?)", flush=True)
        last_tick = now
        try:
            run = call("GET", f"/api/v1/dags/{q(dag_id)}/dagRuns/{q(run_id)}")
            if down_since:
                print(f"Tunnel rétabli après {fmt_dur(now - down_since)}", flush=True)
                down_since = None
            state = run["state"]
            all_tis = tis(dag_id, run_id)
            by_key = {(t["task_id"], t.get("map_index", -1)): t for t in all_tis}
            # ---- the wave in flight (batches cleared together), see adapt_after_wave ----
            wave = st.get("wave")
            if not wave and st.get("inflight"):          # state left by an older version: a wave of one
                wave = st["wave"] = {"task_id": st["inflight"][0], "batches": [st["inflight"][1]], "par": 1}
                st["inflight"] = None
            busy = False
            if wave:
                cur = [by_key.get((wave["task_id"], mi)) for mi in wave["batches"]]
                running = [c for c in cur if c and c.get("state") in ACTIVE]
                if running:
                    busy = True
                    kill_slow_wave(dag_id, run_id, st, wave, running)
                else:
                    adapt_after_wave(st, wave, cur)
                    st["wave"] = wave = None
            # ---- failed tasks: classify (cached per try), notify what must not be cleared ----
            eligible = []
            for t in (t for t in all_tis if t.get("state") == "failed"):
                lab = ti_label(t)
                key = f"{lab}#{t.get('try_number')}"
                if st["notified"].get(key):
                    continue
                if lab in st.setdefault("requeue", []):    # killed by the watch itself: back in the queue, no log check
                    eligible.append((t, "arrêtée par le suivi (vague trop lente)", False))
                    continue
                if key not in st.setdefault("kinds", {}):
                    log = ti_log(dag_id, run_id, t)
                    fresh = ti_now(dag_id, run_id, t)
                    if fresh.get("state") != "failed" or fresh.get("try_number") != t.get("try_number"):
                        continue  # cleared or retried meanwhile (e.g. from the UI): not a failure any more
                    kind, part = classify(log)
                    st["kinds"][key] = [kind, last_error_line(part)]
                kind, err = st["kinds"][key]
                retry = kind == "transient" or (kind == "killed" and clear_killed)
                n = st["clears"].get(lab, 0)
                if retry and n < max_clears:
                    eligible.append((t, err, True))
                else:
                    why = {"fatal": "erreur de code/données, pas de relance",
                           "killed": "tuée (return code -9, mémoire ?), pas de relance",
                           "unknown": "erreur non reconnue, pas de relance"}.get(kind)
                    if retry:
                        why = f"échoue encore après {n} relances"
                    notify(title, f"❌ {short} · {lab} : {why}. Va voir : {err}")
                    st["notified"][key] = kind
            # ---- launch the next wave: one task, or up to `par` batches of one mapped task ----
            cleared = False
            if eligible and not busy:
                first = eligible[0][0]
                tid = first["task_id"]
                mapped = first.get("map_index", -1) >= 0
                if mapped and any(x["task_id"] == tid and x.get("state") in ACTIVE for x in all_tis):
                    pass  # batches of this task still running from an earlier attempt: wait for them
                elif mapped:
                    ad = st.setdefault("adapt", {}).setdefault(tid, {"par": 1, "base": None, "cap": None})
                    picked = [e for e in eligible if e[0]["task_id"] == tid][:ad["par"]]
                    for t, err, counted in picked:
                        ui_clear(dag_id, run_id, tid, t["map_index"])   # this batch only (+ its failed downstream)
                        lab = ti_label(t)
                        if counted:
                            st["clears"][lab] = st["clears"].get(lab, 0) + 1
                        else:
                            st["requeue"].remove(lab)
                        print(f"Clear {lab} ({st['clears'].get(lab, 0)}/{max_clears}) : {err}", flush=True)
                    st["wave"] = {"task_id": tid, "batches": [t["map_index"] for t, _, _ in picked], "par": len(picked)}
                    if len(picked) > 1:
                        print(f"Vague de {len(picked)} batchs en parallèle", flush=True)
                    cleared = True
                else:
                    t, err, counted = eligible[0]
                    call("POST", f"/api/v1/dags/{q(dag_id)}/clearTaskInstances",
                         {"dry_run": False, "dag_run_id": run_id, "task_ids": [tid], "only_failed": True,
                          "include_downstream": True, "include_upstream": False, "reset_dag_runs": True})
                    st["clears"][tid] = st["clears"].get(tid, 0) + 1
                    st["wave"] = {"task_id": tid, "batches": [-1], "par": 1, "single": True}
                    cleared = True
                    print(f"Clear {tid} ({st['clears'][tid]}/{max_clears}) : {err}", flush=True)
            waiting = len(eligible) - (len(st["wave"]["batches"]) if cleared else 0)
            if waiting and waiting != st.get("waiting"):
                print(f"{waiting} tâche(s)/batch(s) en échec attendent leur tour", flush=True)
            st["waiting"] = waiting
            save_json(path, st)
            # A run can be "success" with failed batches (its leaf tasks decide): finished only when nothing is left to clear
            if state in ("success", "failed") and not (cleared or busy or waiting):
                now_tis = tis(dag_id, run_id)
                if not any(t.get("state") in ACTIVE[2:] for t in now_tis):
                    bad = [t for t in now_tis if t.get("state") in ("failed", "upstream_failed")]
                    text, s = build_report(dag_id, run_id, journal=True)
                    if state == "success" and not bad:
                        notify(title, f"✅ {short} vert · {s['duration']} · {fmt_n(s['create'])} créés, {fmt_n(s['update'])} maj"
                               + (f" · {s['mapping_values']} mappings manquants" if s["mapping_values"] else ""))
                    elif state == "success":
                        notify(title, f"⚠️ {short} vert mais incomplet : {len(bad)} tâche(s) en échec ou bloquée(s) · voir le rapport")
                    else:
                        notify(title, f"❌ {short} en échec · voir le rapport")
                    print(text, flush=True)
                    return 0 if state == "success" and not bad else 1
            if state == "queued" and run.get("start_date") is None and not st["notified"].get("queued"):
                dag = call("GET", f"/api/v1/dags/{q(dag_id)}")
                if not dag.get("is_active", True):
                    notify(title, f"⛔ {short} n'est plus chargé par Airflow : le run restera en attente")
                    st["notified"]["queued"] = True
                elif dag.get("is_paused"):
                    notify(title, f"⏸ {short} est en pause : le run attend")
                    st["notified"]["queued"] = True
        except Down:
            if not down_since:
                down_since = now
                notify(title, "🔌 Tunnel Airflow coupé : reconnecte-toi (vm), vérifie les VPN ou relance les containers")
            elif now - down_since > 12 * 3600:
                print("Tunnel coupé depuis plus de 12 h : fin du suivi", flush=True)
                return 2
        except RuntimeError as e:
            print(f"Erreur API : {e}", flush=True)
        time.sleep(interval)


# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    s = sub.add_parser("client"); s.add_argument("name", nargs="?")
    s = sub.add_parser("add-client"); s.add_argument("name")
    for name in ("dags", "status"):
        s = sub.add_parser(name); s.add_argument("--client")
    s = sub.add_parser("resolve"); s.add_argument("kind", choices=["ETL", "TL", "etl", "tl"]); s.add_argument("words", nargs="+"); s.add_argument("--client")
    s = sub.add_parser("run"); s.add_argument("dag_id"); s.add_argument("stack"); s.add_argument("--yes", action="store_true"); s.add_argument("--prod-authorized", action="store_true")
    s = sub.add_parser("watch"); s.add_argument("dag_id"); s.add_argument("run_id", nargs="?"); s.add_argument("--max-clears", type=int, default=3); s.add_argument("--interval", type=int, default=60); s.add_argument("--clear-killed", action=argparse.BooleanOptionalAction, default=True, help="also clear tasks killed with return code -9 (default: yes)")
    s.add_argument("--client")  # Jarvis : le client vient de Jarvis, pas de air.json
    s = sub.add_parser("report"); s.add_argument("dag_id"); s.add_argument("run_id", nargs="?"); s.add_argument("--journal", action="store_true")
    a = p.parse_args()

    try:
        if a.cmd == "health":
            h = call("GET", "/health")
            print(f"Tunnel OK · {BASE} · scheduler {h['scheduler']['status']} · metadatabase {h['metadatabase']['status']}")
            print("Identifiants : " + ("OK" if env("AIRFLOW_USER") and env("AIRFLOW_PASSWORD") else "absents (.claude/air.env)"))
        elif a.cmd == "client":
            if a.name:
                save_json(CONF, {**load_conf(), "client": a.name})
                if not (env("AIRFLOW_USER") and env("AIRFLOW_PASSWORD")):
                    print(f"Client par défaut : {a.name} (DAGs non vérifiés : identifiants absents dans .claude/air.env)")
                    return
                n = len(client_dags(a.name))
                print(f"Client par défaut : {a.name} ({n} DAGs)" + ("  ⚠️ aucun DAG trouvé pour ce préfixe" if not n else ""))
            else:
                print(load_conf().get("client") or "Aucun client par défaut")
        elif a.cmd == "add-client":
            var = call("GET", "/api/v1/variables/active_clients")
            raw = var["value"]
            try:
                clients = json.loads(raw)
            except ValueError:
                clients = ast.literal_eval(raw)
            if a.name in clients:
                print(f"{a.name} est déjà dans active_clients : {raw}")
                return
            clients.append(a.name)
            new = "[" + ", ".join(repr(c) for c in clients) + "]" if "'" in raw else json.dumps(clients)
            call("PATCH", "/api/v1/variables/active_clients?update_mask=value", {"key": "active_clients", "value": new})
            print(f"active_clients : {raw} → {new}")
        elif a.cmd == "dags":
            c = client_of(a)
            dags = client_dags(c)
            last = last_runs([d["dag_id"] for d in dags])
            print(f"{len(dags)} DAGs pour {c} (▶ = activé, donc déjà lancé)\n")
            print("| DAG | | Dernier run | Stack | Date (UTC) |\n|---|---|---|---|---|")
            for d in dags:
                r = last.get(d["dag_id"])
                print(f"| {short_id(d['dag_id'], c)} | {'' if d.get('is_paused') else '▶'} | "
                      + (f"{STATE_ICON.get(r['state'], r['state'])} | {(r.get('conf') or {}).get('padoa_env', '?')} | "
                         f"{fmt_date(r.get('start_date') or r.get('logical_date'))} |" if r else "– | | |"))
        elif a.cmd == "status":
            c = client_of(a)
            runs = [r for st in ("running", "queued") for r in paged(f"/api/v1/dags/~/dagRuns?state={st}", "dag_runs")
                    if r["dag_id"].startswith(c + "-")]
            if not runs:
                print(f"Aucun run en cours pour {c}")
            else:
                print("| DAG | Stack | État | Début (UTC) | Écoulé | ✅ | 🔄 | ❌ | ⏳ |\n|---|---|---|---|---|---:|---:|---:|---:|")
                now = datetime.now(timezone.utc)
                for r in runs:
                    cnt = defaultdict(int)
                    for t in tis(r["dag_id"], r["dag_run_id"]):
                        st = t.get("state")
                        cnt["ok" if st in ("success", "skipped") else "run" if st in ("running", "queued", "scheduled", "up_for_retry")
                            else "ko" if st in ("failed", "upstream_failed") else "todo"] += 1
                    start = parse_ts(r.get("start_date"))
                    print(f"| {short_id(r['dag_id'], c)} | {(r.get('conf') or {}).get('padoa_env', '?')} | "
                          f"{STATE_ICON.get(r['state'], r['state'])} | {fmt_date(r.get('start_date'))} | "
                          f"{fmt_dur((now - start).total_seconds()) if start else '–'} | "
                          f"{cnt['ok']} | {cnt['run']} | {cnt['ko']} | {cnt['todo']} |")
        elif a.cmd == "resolve":
            c = client_of(a)
            group = a.words[0].lower() == "group"
            words = a.words[1:] if group else a.words
            if len(words) != 1:
                sys.exit("Usage : resolve ETL|TL [group] <ressource>")
            hits = resolve(c, a.kind, group, words[0])
            if not hits:
                print(f"DAG absent pour {c} : {a.kind.upper()} {'group ' if group else ''}{words[0]}. "
                      f"Il n'est pas déclaré dans dags/config_deployment.py pour ce client.")
                near = [d["dag_id"] for d in client_dags(c) if words[0].lower() in d["dag_id"].lower()]
                if near:
                    print("DAGs proches : " + ", ".join(near))
                sys.exit(3)
            for d in hits:
                print(d["dag_id"] + ("  (en pause)" if d.get("is_paused") else ""))
            sys.exit(0 if len(hits) == 1 else 2)
        elif a.cmd == "run":
            stack = STACKS.get(a.stack.lower())
            if not stack:
                sys.exit(f"Stack inconnue : {a.stack} (snap, recette, integration, prod)")
            if stack == "prod" and not a.prod_authorized:
                sys.exit("⛔ Refusé : injection en prod sans autorisation explicite d'Arthur pour ce run.")
            dag = call("GET", f"/api/v1/dags/{q(a.dag_id)}")
            if not dag.get("is_active", True):
                sys.exit(f"⛔ {a.dag_id} n'est plus chargé par Airflow (is_active=false) : le run resterait en attente. "
                         "Vérifie dags/config_deployment.py et la branche d'integration-orchestrator sur la VM.")
            running = call("GET", f"/api/v1/dags/{q(a.dag_id)}/dagRuns?state=running&state=queued").get("dag_runs", [])
            print(f"DAG   : {a.dag_id}\nStack : {stack}  (conf {{\"padoa_env\": \"{stack}\"}})")
            tasks = call("GET", f"/api/v1/dags/{q(a.dag_id)}/tasks").get("tasks", [])
            res = sorted({m.group(1) for t in tasks for m in [TL_RESOURCE.search(t["task_id"])] if m})
            if res:
                print("Ressources : " + ", ".join(res))
            if running:
                print(f"⚠️ {len(running)} run(s) déjà en cours pour ce DAG.")
            if not a.yes:
                print("(aperçu : relancer avec --yes pour déclencher)")
                return
            if dag.get("is_paused"):
                call("PATCH", f"/api/v1/dags/{q(a.dag_id)}?update_mask=is_paused", {"is_paused": False})
            r = call("POST", f"/api/v1/dags/{q(a.dag_id)}/dagRuns", {"conf": {"padoa_env": stack}})
            print(f"Lancé : run_id {r['dag_run_id']}")
        elif a.cmd == "watch":
            run_id = a.run_id or latest_run(a.dag_id)["dag_run_id"]
            sys.exit(watch(a.dag_id, run_id, a.max_clears, a.interval, a.client or load_conf().get("client", ""), a.clear_killed))
        elif a.cmd == "report":
            run_id = a.run_id or latest_run(a.dag_id)["dag_run_id"]
            text, s = build_report(a.dag_id, run_id, journal=a.journal)
            print(text)
            print(f"\n(rapport : {s['report']})")
    except Down as e:
        sys.exit(f"🔌 Airflow injoignable sur {BASE} ({e}) : tunnel coupé ? Lance `vm`, vérifie les VPN ou les containers.")
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
