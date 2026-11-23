"""Cartes à collectionner : catalogue, tirage d'un booster, collection, boosters gagnés, page Cartes (fiches fictives de demo/)."""

import json
import random
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from markupsafe import escape
from fastapi.testclient import TestClient

from deploy_hub import main
from deploy_hub.config import PROJECT_ROOT
from deploy_hub.sources import cards
from deploy_hub.sources.cards import Card, CardStore, Event
from deploy_hub.sources.fiches import FicheSource
from deploy_hub.sources.notifications import Notification
from deploy_hub.sources.pulls import PullRequests

NOW = datetime(2026, 11, 23, 10, 0)
HTMX = {"HX-Request": "true"}
STATIC = PROJECT_ROOT / "src" / "deploy_hub" / "static" / "cartes"


def card(name: str, rarity: str = "commune", family: str = "equipe") -> Card:
    return Card(id=f"{family}:{name}", family=family, name=name.title(), rarity=rarity)


POOL = [card("a"), card("b"), card("c"), card("d"), card("e", "rare"), card("f", "epique"), card("g", "legendaire")]


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "cartes.json")


@pytest.fixture
def client(monkeypatch):
    """Chaque test part sans collection, sans notification ni agent (ceux des autres tests rapporteraient boosters et cartes)."""
    monkeypatch.setattr(main.claude, "runs", [])
    monkeypatch.setattr(main, "_journals_seen", {})
    for path in (main.card_store.path, main.notifications.path):
        path.unlink(missing_ok=True)
    yield TestClient(main.app)
    for path in (main.card_store.path, main.notifications.path):
        path.unlink(missing_ok=True)


# ------------------------------------------------------------ Catalogue
def test_catalogue_du_depot_est_lisible_et_ses_photos_existent():
    found, problem = cards.catalogue()
    assert not problem and {c.family for c in found} == {"equipe", "logiciel"}
    assert len({c.id for c in found}) == len(found)
    assert all(len(c.stats) <= 3 for c in found)
    assert [c.photo for c in found if c.photo and not (STATIC / c.photo).is_file()] == []


@pytest.mark.parametrize("text, said", [
    ("equipe:\n  - {id: a, nom: A, rarete: mythique}", "rareté inconnue « mythique »"),
    ("client:\n  - {id: a, nom: A}", "famille inconnue « client »"),
    ("equipe:\n  - {nom: Sans id}", "a un id et un nom"),
    ("equipe:\n  - {id: a, nom: A}\n  - {id: a, nom: B}", "id en double : equipe:a"),
    ("equipe:\n  - {id: jean/paul, nom: A}", "lettres, chiffres et tirets"),
    ("equipe:\n  - {id: a, nom: A, stats: [Force, 80]}", "les stats de a"),
    ("equipe: {id: a, nom: A}", "est une liste de cartes"),
])
def test_carte_mal_ecrite_dit_ce_qui_ne_va_pas(text, said):
    with pytest.raises(ValueError, match=re.escape(said)):
        cards.parse_catalogue(text)


def test_stats_un_entier_fait_une_jauge_un_texte_reste_tel_quel():
    [c] = cards.parse_catalogue("equipe:\n  - {id: a, nom: Ada Lovelace, stats: {Force: 140, Devise: carpe diem, Zen: 30, Trop: 1}}")
    assert [(s.label, s.value, s.pct) for s in c.stats] == [("Force", "140", 100), ("Devise", "carpe diem", None), ("Zen", "30", 30)]
    assert c.initials == "AL" and c.id == "equipe:a"


def test_fichier_local_ajoute_des_cartes_sans_remplacer_celles_du_depot(tmp_path):
    extra = tmp_path / "cartes.yaml"
    extra.write_text("equipe:\n  - {id: jarvis, nom: Imposteur}\n  - {id: collegue, nom: Collègue, rarete: rare}", encoding="utf-8")
    found, problem = cards.catalogue(extra)
    names = {c.id: c.name for c in found}
    assert not problem and names["equipe:collegue"] == "Collègue" and names["equipe:jarvis"] == "Jarvis"


def test_fichier_local_illisible_est_signale_et_le_jeu_reste_entier(tmp_path):
    extra = tmp_path / "cartes.yaml"
    extra.write_text("equipe:\n  - {id: x, nom: X, rarete: mythique}", encoding="utf-8")
    found, problem = cards.catalogue(extra)
    assert "rareté inconnue" in problem and found == cards.catalogue()[0]


def test_carte_client_ne_porte_que_des_comptes():
    d = FicheSource(PROJECT_ROOT / "demo" / "deploiements").get_deployment("client-demo")
    c = cards.client_card(d)
    assert (c.id, c.name, c.family, c.role) == ("client:client-demo", "Client Démo", "client", d.software)
    assert [(s.label, s.value, s.pct) for s in c.stats[:2]] == [("Intégré", "20 %", 20), ("Retours ouverts", "3", None)]
    assert c.stats[2].label == "Déploiement" and re.fullmatch(r"J[-+]\d+|aujourd'hui", c.stats[2].value)
    assert cards.pool([card("a")], [d])[-1] == c


# ------------------------------------------------------------ Tirage
def test_booster_cinq_cartes_differentes_la_derniere_jamais_commune():
    for seed in range(200):
        pulled = cards.draw(POOL, random.Random(seed))
        assert len(pulled) == cards.BOOSTER_SIZE and len({c.id for c, _ in pulled}) == cards.BOOSTER_SIZE
        assert pulled[-1][0].rarity != "commune" and all(finish in cards.FINISHES for _, finish in pulled)
    assert cards.draw(POOL, random.Random(7)) == cards.draw(POOL, random.Random(7))


def test_booster_d_un_petit_jeu_accepte_les_doubles_et_d_un_jeu_vide_ne_donne_rien():
    assert [c.id for c, _ in cards.draw([card("a")], random.Random(1))] == ["equipe:a"] * cards.BOOSTER_SIZE
    assert cards.draw([], random.Random(1)) == []


def test_ajouter_des_communes_ne_rarefie_pas_les_legendaires():
    def legendaries(pool):
        rng = random.Random(42)
        return sum(cards.draw(pool, rng)[0][0].rarity == "legendaire" for _ in range(2000))  # première carte : tirage ordinaire
    few, many = legendaries(POOL), legendaries(POOL + [card(f"commune-{i}") for i in range(40)])
    assert 15 < few < 90 and 15 < many < 90  # 2 % par tirage environ, quel que soit le nombre de communes


# ------------------------------------------------------------ Collection et boosters
def test_premiere_visite_bienvenue_puis_un_booster_par_jour(store):
    assert store.boosters == 0 and not store.path.exists()
    assert store.visit(NOW)["boosters"] == cards.WELCOME + 1
    assert store.visit(NOW + timedelta(hours=5))["boosters"] == cards.WELCOME + 1
    data = CardStore(store.path).visit(NOW + timedelta(days=1))
    assert data["boosters"] == cards.WELCOME + 2 == store.boosters
    assert [g["quoi"] for g in data["journal"]] == ["Bienvenue", "Booster du jour", "Booster du jour"]


def test_ouvrir_un_booster_range_les_cartes_et_vide_la_reserve(store):
    before = store.visit(NOW)["boosters"]
    pulled = store.open_booster(POOL, random.Random(3), NOW)
    data = store.visit(NOW)
    assert len(pulled) == cards.BOOSTER_SIZE and data["boosters"] == before - 1 and all(p.new for p in pulled)
    assert sum(n for owned in data["cartes"].values() for n in owned.values()) == cards.BOOSTER_SIZE
    assert data["noms"][pulled[0].card.id] == pulled[0].card.name
    again = store.open_booster([pulled[0].card], random.Random(3), NOW)
    assert [p.new for p in again if p.finish == pulled[0].finish] and not any(p.new for p in again if p.finish == pulled[0].finish)
    for _ in range(before):
        store.open_booster(POOL, random.Random(1), NOW)
    assert store.boosters == 0 and store.open_booster(POOL, random.Random(1), NOW) == []


def test_un_evenement_ne_rapporte_qu_une_fois_et_le_passe_ne_compte_pas(store):
    start = store.visit(NOW)["boosters"]
    old, new = Event("dag:/agents/suivis/1", "DAG vert", NOW - timedelta(days=1)), Event("dag:/agents/suivis/2", "DAG vert · etl", NOW + timedelta(minutes=5))
    assert store.earn([old, new], NOW + timedelta(minutes=6)) == 1
    assert store.earn([old, new], NOW + timedelta(minutes=7)) == 0
    data = store.visit(NOW + timedelta(minutes=8))
    assert data["boosters"] == start + 1 and data["journal"][-1]["quoi"] == "DAG vert · etl"


def test_activite_plafonnee_par_jour_le_surplus_est_perdu(store):
    start = store.visit(NOW)["boosters"]
    events = [Event(f"agent:{i}", f"Agent #{i}", NOW + timedelta(minutes=i + 1)) for i in range(cards.DAILY_ACTIVITY_MAX + 3)]
    assert store.earn(events, NOW + timedelta(hours=1)) == cards.DAILY_ACTIVITY_MAX
    tomorrow = NOW + timedelta(days=1)
    assert store.earn(events, tomorrow) == 0  # déjà vus : ils ne reviennent pas le lendemain
    assert store.earn([Event("agent:autre", "Agent", tomorrow)], tomorrow) == 1
    assert store.visit(tomorrow)["boosters"] == start + cards.DAILY_ACTIVITY_MAX + 2  # + le booster du lendemain


def test_vieil_evenement_ne_rapporte_rien_meme_si_sa_cle_n_est_plus_gardee(store):
    store.visit(NOW)
    later = NOW + timedelta(days=cards.EVENT_DAYS + 3)
    assert store.earn([Event("pr:1", "PR fusionnée · #1", NOW + timedelta(hours=1))], later) == 0
    assert store.earn([Event("pr:2", "PR fusionnée · #2", later - timedelta(days=1))], later) == 1


def test_chaque_agent_lance_incarne_une_carte_d_equipe_qui_entre_dans_la_collection(store):
    store.visit(NOW)
    team = [card("a"), card("b", "rare")]
    runs = [(1, NOW - timedelta(days=2)), (2, NOW + timedelta(minutes=1)), (3, NOW + timedelta(minutes=2))]
    linked = store.agent_cards(runs, team, random.Random(5), NOW + timedelta(minutes=3))
    assert linked[1] == "" and {linked[2], linked[3]} <= {"equipe:a", "equipe:b"}  # l'agent d'avant la collection n'a pas de carte
    data = store.visit(NOW + timedelta(minutes=4))
    assert sum(owned["standard"] for owned in data["cartes"].values()) == 2
    assert CardStore(store.path).agent_cards(runs, team, random.Random(9), NOW + timedelta(minutes=5)) == linked  # tirée une fois pour toutes
    assert store.visit(NOW + timedelta(minutes=6))["cartes"] == data["cartes"]
    assert store.agent_cards([(4, NOW + timedelta(minutes=7))], [], now=NOW + timedelta(minutes=8)) == linked  # pas de carte d'équipe : rien


def test_classeur_numerote_garde_les_cartes_retirees_et_compte_les_finitions(store):
    store.visit(NOW)
    data = {"cartes": {"equipe:a": {"standard": 2, "or": 1}, "client:parti": {"brillante": 1}}, "noms": {"client:parti": "Client Parti"}}
    groups = cards.binder([card("a"), card("b"), card("k", family="logiciel")], data)
    assert [(family, [(s.number, s.card.name, s.best, s.copies) for s in slots]) for family, slots in groups] == [
        ("equipe", [(1, "A", "or", 3), (2, "B", "", 0)]), ("logiciel", [(3, "K", "", 0)]), ("client", [(4, "Client Parti", "brillante", 1)])]
    assert cards.progress(groups) == (3, 12)


# ------------------------------------------------------------ Ce qui vaut un booster
def note(key, kind, level, detail="", link="", title="etl_demo · snap") -> Notification:
    return Notification(id=1, key=key, kind=kind, level=level, title=title, detail=detail, link=link, created=NOW.isoformat())


def test_dag_vert_et_agent_termine_valent_un_booster_pas_les_echecs():
    notes = [note("suivi:4:2026-11-23T09:00:00:✅ fini", "dag", "info", "✅ fini", "/agents/suivis/4"),
             note("suivi:4:2026-11-23T09:00:00:✅ rapport", "dag", "info", "✅ rapport prêt", "/agents/suivis/4"),
             note("suivi:5:2026-11-23T09:30:00:❌ échec", "dag", "urgent", "❌ échec", "/agents/suivis/5"),
             note("chaine:1:2026-11-23T09:00:00", "dag", "info", "✅ chaîne finie", "/agents"),
             note("agent:7:2026-11-23T09:00:00", "agent", "info", title="Agent #7 terminé (simulé) · Client Démo"),
             note("agent:8:2026-11-23T09:10:00", "agent", "warning", title="Agent #8 échec · Client Démo"),
             note("deadline:x", "deadline", "urgent")]
    events = cards.note_events(notes)
    assert [(e.key, e.label) for e in events] == [("dag:/agents/suivis/4", "DAG vert · etl_demo · snap")] * 2 + [
        ("agent:7:2026-11-23T09:00:00", "Agent #7 terminé")]
    assert all(e.when == NOW for e in events)


def test_retour_passe_a_termine_vaut_un_booster_sans_son_contenu():
    d = SimpleNamespace(slug="acme", client="Acme")

    def change(before, after, field="statut", kind="retour", type="modifie"):
        return SimpleNamespace(deployment=d, kind=kind, type=type, field=field, before=before, after=after, when=NOW, notion_url="https://n/1",
                               label="Retour 01 · contenu du client")
    events = cards.retour_events([change("À faire", "Validé"), change("Validé", "terminé"), change("À faire", "INTE - En cours"),
                                  change("À faire", "Validé", kind="carte"), change(None, "Validé", type="nouveau"),
                                  change("a", "b", field="analyse")])
    assert [(e.key, e.label) for e in events] == [("retour:acme:https://n/1", "Retour de recette terminé · Acme")]


def test_pr_fusionnees_gardees_dans_l_historique_sans_appel_a_github(tmp_path):
    def raw(number, title, state="MERGED"):
        return {"number": number, "title": title, "url": f"https://github.com/o/r/pull/{number}", "createdAt": "2026-11-20T08:00:00Z",
                "state": state, "mergedAt": "2026-11-23T09:00:00Z" if state == "MERGED" else None, "closedAt": "2026-11-23T09:00:00Z"}
    history = tmp_path / "historique-pr.json"
    history.write_text(json.dumps({
        "acme in:title": {"lu": NOW.isoformat(), "prs": {"1": raw(1, "[FIX](acme) a"), "2": raw(2, "[FIX](acme) b", "CLOSED")}},
        "preventiel in:title is:merged": {"lu": NOW.isoformat(), "prs": {"3": raw(3, "[FEAT](preventiel) c"), "1": raw(1, "[FIX](acme) a")}},
        "agent https://github.com/o/r/pull/4": {"lu": NOW.isoformat(), "prs": {"4": raw(4, "Correction d'un retour")}}}), encoding="utf-8")

    def no_gh(*_):
        raise AssertionError("aucun appel à gh attendu")
    merged = PullRequests(tmp_path, run=no_gh, store=history, background=False).merged({"acme"})
    assert sorted(p.number for p in merged) == [1, 4]
    assert sorted(e.key for e in cards.pull_events(merged)) == ["pr:https://github.com/o/r/pull/1", "pr:https://github.com/o/r/pull/4"]


# ------------------------------------------------------------ Page
def test_page_cartes_classeur_ferme_puis_booster_ouvert(client):
    page = client.get("/cartes")
    total = len(cards.catalogue()[0]) + 2  # + les deux clients de demo/
    assert page.status_code == 200 and f"{total} cartes" in page.text and page.text.count("À trouver") == total
    assert f'<span class="tcg-open-n">{cards.WELCOME + 1}</span>' in page.text and "data-ticket" not in page.text
    opened = client.post("/cartes/booster", headers=HTMX)
    assert opened.status_code == 200 and opened.text.count('class="tcg-flip"') == cards.BOOSTER_SIZE
    assert f'<span class="tcg-open-n">{cards.WELCOME}</span>' in opened.text and opened.text.count("À trouver") == total - cards.BOOSTER_SIZE
    assert f">{cards.WELCOME}</span>" in re.search(r'<span class="nav-count" id="nav-boosters"[^>]*>\d+</span>', opened.text).group(0)


def test_detail_d_une_carte_seulement_si_on_la_possede(client):
    assert client.get("/cartes/equipe:jarvis").status_code == 404
    opened = client.post("/cartes/booster", headers=HTMX)
    url = re.search(r'href="(/cartes/[^"]+)" data-ticket', opened.text).group(1)
    detail = client.get(url)
    assert detail.status_code == 200 and "tcg-detail" in detail.text and 'aria-current="true"' in detail.text
    assert client.get("/cartes/inconnue").status_code == 404


def test_sans_booster_le_bouton_est_grise_et_rien_n_est_tire(client):
    for _ in range(cards.WELCOME + 1):
        client.post("/cartes/booster", headers=HTMX)
    owned = main.card_store.visit()["cartes"]
    page = client.post("/cartes/booster", headers=HTMX)
    assert "Plus de booster" in page.text and "disabled" in page.text and 'class="tcg-flip"' not in page.text
    assert main.card_store.visit()["cartes"] == owned
    assert re.search(r'id="nav-boosters" hidden', client.get("/").text)


def test_sans_htmx_ouvrir_un_booster_redirige_vers_le_classeur(client):
    r = client.post("/cartes/booster", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/cartes")
    assert main.card_store.boosters == cards.WELCOME


def test_page_agents_montre_la_carte_de_chaque_agent(client, monkeypatch):
    client.get("/cartes")
    r = main.claude.launch(main.fiches.get_deployment("client-demo"), "visit_histo")
    assert r.ok
    [run] = main.claude.runs
    monkeypatch.setattr(run, "simulated", False)
    linked = main._agent_cards()
    assert list(linked) == [run.id] and linked[run.id].family == "equipe"
    name = escape(linked[run.id].name)
    assert f'title="Carte débloquée par cet agent">{name}</a>' in client.get("/agents").text
    assert "<dt>Carte</dt>" in client.get(f"/agents/{run.id}").text
    assert main.card_store.visit()["cartes"][linked[run.id].id]["standard"] == 1


def test_un_agent_termine_rapporte_un_booster_a_l_ouverture_de_la_page(client):
    client.get("/cartes")
    main.notifications.add("agent:99:2099-01-01T00:00:00", "agent", "info", "Agent #99 terminé · Client Démo")
    page = client.get("/cartes")
    assert f'<span class="tcg-open-n">{cards.WELCOME + 2}</span>' in page.text and "Agent #99 terminé" in page.text
