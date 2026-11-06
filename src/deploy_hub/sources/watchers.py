"""Veilles qui produisent des notifications à chaque relevé (cloche, toutes les 60 s) :
- échéance du jour (jalon, action non terminée, retour ouvert) ;
- synchro en retard : en semaine, de 10h à 19h, dernière synchro d'un client vieille de plus de SYNC_LATE_HOURS heures ;
- santé qui se dégrade : pastille 🟢 → 🟠/🔴, ou 🟠 → 🔴 (même règle que « 📊 Inté - Statut » de Notion) ;
- jalon important proche : freeze, recettes, déploiements, à J-7 (info) et J-1 (urgent), avec ce qui reste ouvert.

Fonctions pures : elles reçoivent les fiches, l'heure et l'état précédent (santé déjà vue), et renvoient des
notifications (dict pour NotificationStore.add_many) et le nouvel état. Une clé = une seule notification.
"""

from datetime import date, datetime, timedelta

from deploy_hub.models import DONE, KO, MILESTONE_LABELS, Deployment

SYNC_LATE_HOURS = 4  # la routine passe toutes les 2 h de 8h à 18h en semaine
WORK_HOURS = range(10, 20)  # pas d'alerte avant le premier passage du matin, ni le soir
KEY_MILESTONES = ("recette_1", "recette_2", "recette_3", "freeze", "deploiement", "deploiement_lot2")
REMINDERS = {7: "info", 1: "urgent"}  # jours avant le jalon → niveau
HEALTH_RANK = {"✅": 0, "🟢": 1, "⚪": 1, "🟠": 2, "🔴": 3}
HEALTH_WORDS = {"✅": "terminé", "🟢": "dans les temps", "⚪": "non calculable", "🟠": "en retard", "🔴": "très en retard"}


def _note(key, kind, level, title, detail, link, client) -> dict:
    return {"key": key, "kind": kind, "level": level, "title": title, "detail": detail, "link": link, "client": client}


def deadline_notes(items: list[dict], today: date) -> list[dict]:
    """items : échéances au format de main._deadlines (date, kind, label, d, url)."""
    return [
        _note(f"deadline:{i['d'].slug}:{i['kind']}:{i['label']}:{today}", "deadline", "urgent",
              f"{i['d'].client} · échéance aujourd'hui", f"{i['kind']} : {i['label']}",
              i.get("url") or f"/deploiements/{i['d'].slug}", i["d"].client)
        for i in items if i["date"] == today
    ]


def last_sync(d: Deployment) -> datetime | None:
    try:
        return datetime.strptime(d.synced_at, "%d/%m/%Y %H:%M") if d.synced_at else None
    except ValueError:
        return None


def sync_late_notes(deployments: list[Deployment], now: datetime) -> list[dict]:
    if now.weekday() >= 5 or now.hour not in WORK_HOURS:
        return []
    notes = []
    for d in deployments:
        last = last_sync(d)
        if last and now - last > timedelta(hours=SYNC_LATE_HOURS):
            notes.append(_note(
                f"sync-late:{d.slug}:{now.date()}", "sync", "urgent", f"{d.client} · synchro en retard",
                f"Jarvis n'a rien reçu de Notion depuis le {last.strftime('%d/%m à %H:%M')}. La routine deploy-hub-sync "
                "a-t-elle tourné (app Claude ouverte, Mac allumé) ?", f"/deploiements/{d.slug}", d.client))
    return notes


def health_notes(deployments: list[Deployment], seen: dict[str, str], now: datetime) -> tuple[list[dict], dict[str, str]]:
    """Notifie une dégradation de la pastille par rapport à la dernière vue ; la première vue ne notifie rien."""
    notes, state = [], dict(seen)
    for d in deployments:
        before, after = seen.get(d.slug), d.health
        state[d.slug] = after
        if before and HEALTH_RANK.get(after, 1) > HEALTH_RANK.get(before, 1):
            gap = f"{d.progress} % intégré, attendu {d.expected_progress} %" if d.expected_progress is not None else f"{d.progress} % intégré"
            notes.append(_note(f"health:{d.slug}:{before}>{after}:{now.date()}", "sync", "urgent" if after == "🔴" else "info",
                               f"{d.client} · avancement : {HEALTH_WORDS.get(before, before)} → {HEALTH_WORDS.get(after, after)}",
                               gap, f"/deploiements/{d.slug}/synthese", d.client))
    return notes, state


def milestone_notes(deployments: list[Deployment], today: date) -> list[dict]:
    notes = []
    for d in deployments:
        for key in KEY_MILESTONES:
            day = d.milestones.get(key)
            if not day:
                continue
            left = (day - today).days
            if left not in REMINDERS:
                continue
            ko = sum(1 for a in d.injection_cards if a.injection_group == KO)
            todo = sum(1 for a in d.injection_cards if a.injection_group != DONE)
            label = MILESTONE_LABELS.get(key, key)
            notes.append(_note(
                f"jalon:{d.slug}:{key}:{day}:J-{left}", "deadline", REMINDERS[left],
                f"{d.client} · {label} {'demain' if left == 1 else 'dans 7 jours'} ({day.strftime('%d/%m')})",
                f"Reste {todo} injection(s) non terminée(s) dont {ko} KO, et {len(d.open_retours)} retour(s) de recette ouvert(s).",
                f"/deploiements/{d.slug}/synthese", d.client))
    return notes
