"""Brief du matin : le résumé de l'accueil en texte, prêt à coller dans Slack ou à lire au daily."""

from datetime import date, timedelta

from deploy_hub.sources.journal import DAYS, MONTHS

MAX_LINES = 8  # lignes par rubrique ; au-delà : « … et N autre(s) »


def _section(title: str, lines: list[str]) -> list[str]:
    if not lines:
        return []
    more = len(lines) - MAX_LINES
    return [f"{title} ({len(lines)})"] + [f"• {line}" for line in lines[:MAX_LINES]] + ([f"• … et {more} autre(s)"] if more > 0 else [])


def build(deployments: list, items: list[dict], reminders: list, today: date, in_period) -> str:
    """Texte du brief. items : échéances (main._deadlines) ; in_period : main._in_period (mêmes règles que l'accueil) ;
    reminders : rappels arrivés à échéance (sources/personal.py)."""
    out = [f"*Brief du {DAYS[today.weekday()]} {today.day} {MONTHS[today.month - 1]}*"]
    for d in deployments:
        mine = [i for i in items if i["d"] is d]
        head = f"*{d.client}*"
        if d.go_live:
            head += f" · déploiement le {d.go_live.strftime('%d/%m/%Y')} (J{-d.days_left:+d})"
        head += f" · {d.progress} % intégré"

        def lines(periode: str) -> list[str]:
            return [f"{i['date'].strftime('%d/%m')} · {i['kind']} · {i['label']}" for i in mine if in_period(i, periode, today)]

        ko = [f"{a.title}" + (f" ({', '.join(a.resources)})" if a.resources else "") for a in d.injection_cards if a.injection_group == "ko"]
        milestones = [f"{i['date'].strftime('%d/%m')} · {i['label']}" for i in mine
                      if i["kind"] == "Jalon" and today <= i["date"] <= today + timedelta(days=7)]
        body = (_section("🔴 En retard", lines("retard")) + _section("🟠 Aujourd'hui", lines("aujourdhui"))
                + _section("🟡 Dans les 7 jours", lines("semaine")) + _section("⚠️ Injections KO", ko)
                + _section("🏁 Jalons dans les 7 jours", milestones)
                + _section("🔔 Rappels", [r.label for r in reminders if r.slug == d.slug]))
        out += ["", head] + (body or ["Rien en retard ni à échéance cette semaine."])
    free = _section("🔔 Rappels", [r.label for r in reminders if not r.slug])
    if free:
        out += [""] + free
    return "\n".join(out)
