"""Passage de synchro piloté par « suite » : Python donne les requêtes, le modèle les exécute, Python récolte les réponses
dans le transcript de la session Claude Code (plus de relecture ni de recopie par le modèle)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import deploy_hub.sync_notion as sn
from deploy_hub.sources.sync_routine import SyncRoutine, routine_text

FIXTURES = Path(__file__).parent / "fixtures" / "notion" / "demo"
SESSION = "session-routine"
FINGERPRINT = {"cle": "demo", "actions": "4|2027-04-01 10:00:00Z", "recette": "1|2027-03-31 09:00:00Z", "regles": "1|40",
               "fiche": "Démo|2027-06-01"}


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    clients = tmp_path / "config" / "clients"
    clients.mkdir(parents=True)
    for slug, page in (("demo", "d" * 32), ("autre", "a" * 32)):
        (clients / f"{slug}.yaml").write_text(f"client: {slug.title()}\nsoftware: Pulse\nstatus: en cours\npage_id: {page}\n", encoding="utf-8")
    monkeypatch.setattr(sn, "CLAUDE_PROJECTS", tmp_path / "projects")
    monkeypatch.setenv(sn.SESSION_ENV, SESSION)
    monkeypatch.delenv(sn.CLIENT_ENV, raising=False)
    return tmp_path


def answer(sql: str, text: str, params=(), when: datetime | None = None, saved: bool = False, session: str = SESSION) -> None:
    """Ce que Claude Code écrit dans son transcript quand le modèle exécute une requête Notion : l'appel, puis la réponse
    (rendue en ligne, ou rangée dans un fichier quand elle est grosse)."""
    folder = sn.CLAUDE_PROJECTS / "projet"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (when or datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    call = f"toolu_{abs(hash((sql, tuple(params), stamp, text)))}"
    if saved:
        file = folder / session / "tool-results" / f"mcp-claude_ai_Notion-notion-query-data-sources-{call}.txt"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")
        text = f"Error: result (4,767 characters) exceeds maximum allowed tokens. Output has been saved to {file}.\nFormat: JSON"
    data = {"mode": "sql", "data_source_urls": [], "query": sql, **({"params": list(params)} if params else {})}
    lines = [{"type": "assistant", "timestamp": stamp, "message": {"content": [
                {"type": "tool_use", "id": call, "name": "mcp__claude_ai_Notion__notion-query-data-sources", "input": {"data": data}}]}},
             {"type": "user", "timestamp": stamp, "message": {"content": [{"type": "tool_result", "tool_use_id": call, "content": text}]}}]
    with (folder / f"{session}.jsonl").open("a", encoding="utf-8") as f:
        f.write("pas du JSON\n" + "".join(json.dumps(line) + "\n" for line in lines))


def rows(n: int) -> str:
    return json.dumps({"results": [{"url": f"u{i}"} for i in range(n)], "has_more": False})


def run(root: Path, asked: dict, **how) -> None:
    """Le modèle : exécute chaque requête donnée ; Notion répond avec le jeu d'essai de cette requête."""
    known = {p.read_text(encoding="utf-8").rstrip(): p.stem for p in (root / "data" / "notion" / "demo" / "requetes").glob("*.sql")}
    for q in asked["requetes"]:
        name = known[q["requete"]].replace("-", "_")
        answer(q["requete"], (FIXTURES / f"{name}.json").read_text(encoding="utf-8"), q.get("params", ()), **how)


def start(root: Path, only: str = "demo") -> dict:
    return sn.first_queries(root, only)


def test_one_client_from_its_fiche_and_everyone_from_the_clients_page(root):
    sn.prepare(root, "demo")
    sql = (root / "data" / "notion" / "empreintes.sql").read_text(encoding="utf-8")
    assert "d" * 32 in sql and "a" * 32 not in sql and "UNION ALL" not in sql  # ce client seulement
    clients = lambda: json.loads((root / "data" / "notion" / sn.PASSAGE).read_text(encoding="utf-8"))["clients"]  # noqa: E731
    assert clients() == ["demo"]
    sn.prepare(root, "")
    assert clients() == ["autre", "demo"] and "UNION ALL" in (root / "data" / "notion" / "empreintes.sql").read_text(encoding="utf-8")
    with pytest.raises(sn.SyncError, match="inconnu ou archivé"):
        sn.prepare(root, "parti")


def test_many_clients_get_several_fingerprint_queries_each_small_enough_to_be_handed(root, monkeypatch):
    """Une vingtaine de clients : l'empreinte en une requête dépasserait ce qu'une commande peut rendre au modèle."""
    monkeypatch.setattr(sn, "BATCH_CHARS", 3000)  # un client par requête (son empreinte fait environ 1,5 Ko)
    asked = start(root, "")
    assert len(asked["requetes"]) == 1 and len(json.dumps(asked, ensure_ascii=False)) < 3000
    one = {**FINGERPRINT, "fiche": ""}
    answer(asked["requetes"][0]["requete"], json.dumps({"results": [{**one, "cle": "autre"}]}))
    second = sn.advance(root)  # le deuxième paquet de clients
    assert len(second["requetes"]) == 1 and second["requetes"][0]["requete"] != asked["requetes"][0]["requete"]
    answer(second["requetes"][0]["requete"], json.dumps({"results": [{**one, "cle": "demo"}]}))
    done = sn.advance(root)  # les deux réponses sont lues comme une seule empreinte
    assert done["termine"] and [line.split(" : ")[0] for line in done["compte_rendu"]] == ["ÉCHEC autre", "ÉCHEC demo"]
    assert all("fiche Notion introuvable" in line for line in done["compte_rendu"])
    sn.prepare(root, "demo")  # passage suivant, un client : la deuxième requête d'avant ne traîne pas
    assert [p.name for p in (root / "data" / "notion").glob("empreintes*.sql")] == ["empreintes.sql"]


def test_a_whole_passage_without_the_model_touching_an_answer(root):
    """Empreinte, plan, lecture complète (pas de cache), fusion : le modèle n'exécute que des requêtes, deux allers-retours."""
    asked = start(root)
    assert list(asked) == ["requetes", "ensuite"] and asked["ensuite"] == sn.NEXT
    assert set(asked["requetes"][0]) == {"requete", "sources"}  # ni fichier à lire, ni sortie à écrire
    answer(asked["requetes"][0]["requete"], json.dumps({"results": [FINGERPRINT]}))
    asked = sn.advance(root)
    assert len(asked["requetes"]) == 4 and all("sortie" not in q for q in asked["requetes"])  # client, règles, actions, recette
    run(root, asked, saved=True)  # grosses réponses : rangées dans des fichiers par Claude Code, Python va les y lire
    done = sn.advance(root)
    assert done["termine"] and done["utilisateurs"] == []
    assert len(done["compte_rendu"]) == 1 and done["compte_rendu"][0].startswith("demo : ") and "complet" in done["compte_rendu"][0]
    assert (root / "data" / "deploiements" / "demo.md").is_file()
    with pytest.raises(sn.SyncError, match="Aucun passage en cours"):
        sn.advance(root)


def test_nothing_changed_is_one_query_and_an_old_answer_is_never_reused(root):
    asked = start(root)
    answer(asked["requetes"][0]["requete"], json.dumps({"results": [FINGERPRINT]}))
    run(root, sn.advance(root))
    sn.advance(root)
    fiche = (root / "data" / "deploiements" / "demo.md").read_text(encoding="utf-8")
    # passage suivant, même requête d'empreinte : la réponse du passage d'avant, restée dans la session, ne compte pas
    again = start(root)
    state = json.loads((root / "data" / "notion" / sn.PASSAGE).read_text(encoding="utf-8"))
    state["debut"] = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds")
    (root / "data" / "notion" / sn.PASSAGE).write_text(json.dumps(state), encoding="utf-8")
    assert sn.advance(root)["requetes"] == again["requetes"]  # redemandée
    answer(again["requetes"][0]["requete"], json.dumps({"results": [FINGERPRINT]}), when=datetime.now(timezone.utc) + timedelta(seconds=10))
    done = sn.advance(root)
    assert done == {"termine": True, "compte_rendu": ["demo : inchangé"], "utilisateurs": []}
    assert (root / "data" / "deploiements" / "demo.md").read_text(encoding="utf-8") == fiche


def test_a_modified_query_is_refused_then_deposited_by_hand_then_the_passage_stops(root):
    asked = start(root)
    sql = asked["requetes"][0]["requete"]
    answer(sql + " LIMIT 5", json.dumps({"results": [FINGERPRINT]}))  # le modèle a touché à la requête
    answer(sql, "Error: Notion 504")  # et Notion a répondu une erreur à la bonne
    assert sn.advance(root) == asked  # redemandée telle quelle : une erreur passagère se rattrape ainsi
    lost = sn.advance(root)
    assert lost["requetes"][0]["sortie"].endswith("empreintes.json") and "introuvables" in lost["note"]  # secours : le modèle dépose
    with pytest.raises(sn.SyncError, match="introuvables après 3 demandes"):
        sn.advance(root)


def test_spacing_does_not_matter_and_a_hand_deposit_counts(root):
    asked = start(root)
    sql = asked["requetes"][0]["requete"]
    answer("  " + sql.replace("\n", "\n   ") + "\n", json.dumps({"results": [FINGERPRINT]}))  # mêmes mots, autres blancs
    assert len(sn.advance(root)["requetes"]) == 4
    state = json.loads((root / "data" / "notion" / sn.PASSAGE).read_text(encoding="utf-8"))
    for q in state["attendues"]:  # secours : réponses déposées à la main, sans transcript lisible ; l'une avec son enveloppe
        text = (FIXTURES / f"{q['nom']}.json").read_text(encoding="utf-8")
        Path(q["sortie"]).write_text(json.dumps([{"type": "text", "text": text}]) if q["nom"] == "regles" else text, encoding="utf-8")
    assert sn.advance(root)["termine"]


def test_params_given_as_json_text_are_the_same_query(root):
    """Vu sur la VM le 2026-11-20 : le modèle a passé « params » en texte JSON au lieu d'une liste, la réponse n'était pas reconnue."""
    assert sn._sql_key("SELECT 1", '["2026-11-20 10:40:00Z"]') == sn._sql_key(" SELECT  1 ", ["2026-11-20 10:40:00Z"])
    assert sn._sql_key("SELECT 1", None) == sn._sql_key("SELECT 1", []) == sn._sql_key("SELECT 1", "")
    assert sn._sql_key("SELECT 1", "2026") == sn._sql_key("SELECT 1", ["2026"]) != sn._sql_key("SELECT 1", ["2027"])


def test_without_a_known_session_the_transcripts_written_since_the_passage_began_are_read(root, monkeypatch):
    monkeypatch.delenv(sn.SESSION_ENV)
    asked = start(root)
    answer(asked["requetes"][0]["requete"], json.dumps({"results": [FINGERPRINT]}), session="commande-jarvis-sync")
    assert len(sn.advance(root)["requetes"]) == 4
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "commande-jarvis-sync")  # Claude Code le pose dans les commandes qu'il lance
    assert [p.stem for p in sn._transcripts(datetime.now(timezone.utc) + timedelta(days=1))] == ["commande-jarvis-sync"]


def test_full_reads_ask_every_page_at_once_and_a_full_last_page_asks_for_more(root):
    asked = start(root)
    big = {**FINGERPRINT, "actions": "250|2027-04-01 10:00:00Z"}  # 250 cartes : 3 pages, demandées ensemble
    answer(asked["requetes"][0]["requete"], json.dumps({"results": [big]}))
    asked = sn.advance(root)
    actions = (root / "data" / "notion" / "demo" / "requetes" / "actions.sql").read_text(encoding="utf-8").rstrip()
    pages = [q["requete"] for q in asked["requetes"] if q["requete"].startswith(actions)]
    assert pages == [actions, actions + "\nLIMIT 100 OFFSET 100", actions + "\nLIMIT 100 OFFSET 200"]
    state = json.loads((root / "data" / "notion" / sn.PASSAGE).read_text(encoding="utf-8"))
    assert [Path(q["sortie"]).name for q in state["attendues"] if q["nom"] == "actions"] == ["actions.json", "actions-2.json", "actions-3.json"]
    for q in state["attendues"]:  # la dernière page prévue revient pleine : il en reste
        answer(q["requete"], rows(100) if q["nom"] == "actions" else (FIXTURES / f"{q['nom']}.json").read_text(encoding="utf-8"))
    more = sn.advance(root)
    assert [q["requete"] for q in more["requetes"]] == [actions + "\nLIMIT 100 OFFSET 300"]


def test_a_long_plan_is_handed_in_batches_the_model_can_read(root, monkeypatch):
    """Vu sur la VM le 2026-11-20 : 12 pages d'une lecture complète en une fois, 31,7 Ko, sortie rangée dans un fichier
    que le modèle ne pouvait pas lire. Le plan part par lots ; rien n'est fusionné avant la dernière réponse."""
    asked = start(root)
    big = {**FINGERPRINT, "actions": "1130|2027-04-01 10:00:00Z"}  # 12 pages
    answer(asked["requetes"][0]["requete"], json.dumps({"results": [big]}))
    total, batches = 0, 0
    asked = sn.advance(root)
    while "requetes" in asked and batches < 10:
        assert len(json.dumps(asked, ensure_ascii=False)) < 30000 and asked["requetes"]
        batches += 1
        total += len(asked["requetes"])
        for q in asked["requetes"]:
            answer(q["requete"], rows(0), q.get("params", ()))
        try:
            asked = sn.advance(root)
        except sn.SyncError:  # réponses vides de ce test : la fusion refuse, mais toutes les requêtes ont été données
            break
    assert batches >= 2 and total == 3 + 12  # client, règles, recette, et les 12 pages des actions


def test_the_button_of_a_fiche_runs_a_fast_model_on_that_client_only(tmp_path, monkeypatch):
    seen = tmp_path / "vu.txt"
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat > /dev/null\necho \"$JARVIS_SYNC_CLIENT|$@\" > {seen}\necho 'acme : inchangé'\n", encoding="utf-8")
    script.chmod(0o755)
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(script))
    r = routine.start("acme", "Acme")
    assert r.ok and "pour Acme" in r.message
    for _ in range(100):
        if seen.is_file() and not routine.running:
            break
        __import__("time").sleep(0.05)
    client, args = seen.read_text(encoding="utf-8").strip().split("|", 1)
    assert client == "acme" and "--model haiku --effort low --tools Bash,Write,ToolSearch --session-id" in args
    monkeypatch.setenv("JARVIS_SYNC_MODEL", "sonnet")
    assert routine.command()[routine.command().index("--model") + 1] == "sonnet"
    assert "pour tous les clients" in SyncRoutine(folder=tmp_path / "n2", claude_bin=str(script)).start().message


def test_the_routine_tells_the_model_to_run_queries_together_and_leave_the_answers_alone():
    text = routine_text()
    assert "dans le même message" in text and "Tu ne lis, ne recopies et ne résumes jamais les réponses" in text
    assert 'Recopie les lignes de "compte_rendu"' in text and "SECOURS" in text


def test_jarvis_opens_the_passage_itself_so_the_model_starts_with_the_first_query(tmp_path, monkeypatch):
    """Bouton de l'app : « preparer » est fait par Jarvis avant de lancer Claude, sa sortie part avec la consigne."""
    from deploy_hub.sources import sync_routine
    received = tmp_path / "consigne.txt"
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat > {received}\necho 'acme : inchangé'\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(sync_routine, "data_dir", lambda: tmp_path)
    asked = []
    monkeypatch.setattr(sync_routine, "first_queries", lambda only: asked.append(only) or {"requetes": [{"requete": "SELECT 'é'", "sources": []}], "ensuite": sn.NEXT})
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(script))
    assert routine.start("acme").ok and asked == ["acme"]
    for _ in range(100):
        if received.is_file() and not routine.running and "finished" in routine.state():
            break
        __import__("time").sleep(0.05)
    text = received.read_text(encoding="utf-8")
    assert text.startswith(routine_text()) and "L'ÉTAPE 1 EST DÉJÀ FAITE" in text and text.rstrip().endswith('"ensuite": "' + sn.NEXT + '"}')
    assert "SELECT 'é'" in text  # la requête telle quelle, accents compris

    def refuse(only):
        raise sn.SyncError("acme : client inconnu ou archivé, rien à synchroniser")
    monkeypatch.setattr(sync_routine, "first_queries", refuse)
    r = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(script)).start("acme")
    assert not r.ok and "client inconnu ou archivé" in r.message


def test_the_button_follows_notion_answers_as_they_come(root, tmp_path):
    """Vu le 2026-11-20 : l'avancement restait à 20 % puis passait à « terminé ». Les réponses ne sont rangées qu'à « suite »,
    toutes ensemble : le bouton les compte donc dans la session de la routine, dès qu'elles arrivent."""
    folder = root / "data" / "notion"
    routine = SyncRoutine(folder=folder)
    asked = start(root)
    routine._save(running=True, started=datetime.now().isoformat(timespec="seconds"), session=SESSION)
    assert routine.progress() == {"pct": 8, "label": "Empreinte…"}
    answer(asked["requetes"][0]["requete"], json.dumps({"results": [FINGERPRINT]}))
    assert routine.progress() == {"pct": 15, "label": "Plan des requêtes…"}
    asked = sn.advance(root)
    seen = [routine.progress()]
    for q in asked["requetes"]:
        answer(q["requete"], (FIXTURES / f"{known_name(root, q)}.json").read_text(encoding="utf-8"), q.get("params", ()))
        seen.append(routine.progress())
    assert [p["label"] for p in seen] == ["Requêtes : 0 / 4", "Requêtes : 1 / 4", "Requêtes : 2 / 4", "Requêtes : 3 / 4", "Fusion…"]
    assert [p["pct"] for p in seen] == [20, 36, 52, 68, 90]  # il monte à chaque réponse
    sn.advance(root)
    assert routine.progress()["pct"] == 97
    # passage d'une synchro précédente, ou session inconnue : rien à compter, la synchro démarre
    old = datetime.now().timestamp() - 3600
    __import__("os").utime(folder / sn.PASSAGE, (old, old))
    assert routine.progress() == {"pct": 3, "label": "Démarrage…"}
    assert SyncRoutine(folder=tmp_path / "vide").progress() == {"pct": 3, "label": "Démarrage…"}


def known_name(root: Path, q: dict) -> str:
    known = {p.read_text(encoding="utf-8").rstrip(): p.stem for p in (root / "data" / "notion" / "demo" / "requetes").glob("*.sql")}
    return known[q["requete"]].replace("-", "_")


def test_jarvis_syncs_by_itself_the_clients_that_asked_for_it(monkeypatch):
    from types import SimpleNamespace as Config
    from deploy_hub import main
    started, answer_ok, asked, due = [], [True], [], [True]
    on = {"acme": False, "bolt": False, "calme": False}  # synchro automatique : à personne par défaut
    monkeypatch.setattr(main.client_config, "active_configs", lambda *a: [Config(slug=s, auto_sync=v) for s, v in on.items()])
    monkeypatch.setattr(main.sync_routine, "start", lambda slug="", client="", auto=False: started.append((slug, auto)) or main.ActionResult(answer_ok[0], "…", False))
    monkeypatch.setattr(main.notion_api, "load_token", lambda: None)
    monkeypatch.setattr(main.sync_notion, "stale_clients", lambda minutes: asked.append(minutes) or (["acme", "calme"] if due[0] else []))
    monkeypatch.delenv("JARVIS_SYNC_EVERY_MIN", raising=False)
    clock = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(main, "_auto_sync_next", clock[0] + main.SYNC_RETRY_S)  # Jarvis vient de démarrer
    main._auto_sync()
    assert started == []  # pas pendant le démarrage
    clock[0] += main.SYNC_RETRY_S
    main._auto_sync()
    assert started == []  # aucun client ne l'a demandée : rien ne part, même s'ils n'ont pas été vérifiés
    on.update(acme=True, bolt=True)
    main._auto_sync()
    main._auto_sync()
    assert started == [(["acme", "bolt"], True)] and asked[-1] == 60  # l'un d'eux est à revérifier : ceux qui l'ont demandée, une fois
    clock[0] += 59 * 60
    main._auto_sync()
    assert len(started) == 1  # la suivante au plus tôt une heure après, même si un client reste en échec
    clock[0] += 60
    due[0] = False  # tout a été vérifié entre-temps (bouton d'une fiche, « Tout rafraîchir ») : rien à faire
    main._auto_sync()
    assert len(started) == 1
    due[0], answer_ok[0] = True, False  # une synchro manuelle tourne : nouvel essai cinq minutes après, pas une heure
    main._auto_sync()
    clock[0] += main.SYNC_RETRY_S - 1
    main._auto_sync()
    assert len(started) == 2
    clock[0] += 1
    answer_ok[0] = True
    main._auto_sync()
    assert len(started) == 3
    clock[0] += 7200
    monkeypatch.setenv("JARVIS_SYNC_EVERY_MIN", "0")  # réglage : jamais
    main._auto_sync()
    assert len(started) == 3
    assert "_auto_sync" in __import__("inspect").getsource(main._push_round)  # vérifié à chaque tour de la boucle de fond


def test_a_client_is_stale_when_never_checked_or_checked_too_long_ago(root):
    assert sn.stale_clients(60, root) == ["autre", "demo"]  # jamais vérifiés
    sn.mark_checked(["demo"], root)
    assert sn.stale_clients(60, root) == ["autre"]
    path = root / "data" / "notion" / sn.VERIFICATIONS
    path.write_text(json.dumps({"demo": (datetime.now() - timedelta(minutes=61)).strftime(sn.CHECKED_FORMAT), "autre": "illisible"}), encoding="utf-8")
    assert sn.stale_clients(60, root) == ["autre", "demo"] and sn.stale_clients(120, root) == ["autre"]


def test_a_failing_automatic_sync_is_notified_once_a_day(tmp_path):
    from deploy_hub.sources.notifications import NotificationStore
    script = tmp_path / "claude"
    script.write_text("#!/bin/sh\ncat > /dev/null\necho 'ÉCHEC tous : étape 2 : connecteur Notion absent'\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    notes = NotificationStore(tmp_path / "notifications.json")
    for n in range(2):
        routine = SyncRoutine(notifications=notes, folder=tmp_path / f"notion{n}", claude_bin=str(script))
        assert routine.start(auto=True).ok
        for _ in range(100):
            if not routine.running and "finished" in routine.state():
                break
            __import__("time").sleep(0.05)
    assert len(notes.all()) == 1 and "auto:" in notes.all()[0].key


def test_several_chosen_clients_in_one_passage(root):
    (root / "config" / "clients" / "trois.yaml").write_text("client: Trois\nsoftware: Pulse\nstatus: en cours\npage_id: " + "c" * 32 + "\n", encoding="utf-8")
    sn.prepare(root, "demo, trois")
    sql = (root / "data" / "notion" / "empreintes.sql").read_text(encoding="utf-8")
    assert "d" * 32 in sql and "c" * 32 in sql and "a" * 32 not in sql
    assert json.loads((root / "data" / "notion" / sn.PASSAGE).read_text(encoding="utf-8"))["clients"] == ["demo", "trois"]


def test_the_automatic_sync_hands_its_clients_to_the_routine_as_one_variable(tmp_path):
    seen = tmp_path / "vu.txt"
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat > /dev/null\necho \"$JARVIS_SYNC_CLIENT\" > {seen}\necho 'acme : inchangé'\n", encoding="utf-8")
    script.chmod(0o755)
    routine = SyncRoutine(folder=tmp_path / "notion", claude_bin=str(script))
    r = routine.start(["acme", "bolt"], auto=True)
    assert r.ok and "pour acme, bolt" in r.message
    for _ in range(100):
        if seen.is_file() and not routine.running:
            break
        __import__("time").sleep(0.05)
    assert seen.read_text(encoding="utf-8").strip() == "acme,bolt"  # ce que « preparer » relit
