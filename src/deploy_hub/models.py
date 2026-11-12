"""Modèle d'un déploiement client, calqué sur le dashboard intégration Notion.

Les statuts reprennent les options des bases Notion « Actions de déploiement »
et « Retours Recette » ; les groupes (à faire / en cours / KO / terminé) servent à l'affichage.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote

TODO, IN_PROGRESS, KO, DONE = "todo", "in_progress", "ko", "done"

# Notion « Actions de déploiement » → « Statut intégration - Injections ».
INJECTION_STATUSES = {
    "A faire": TODO,
    "En cours d'analyse": IN_PROGRESS,
    "Analyse terminée": IN_PROGRESS,
    "En cours de dev": IN_PROGRESS,
    "Injecté et vérifié en intégration/snap": IN_PROGRESS,
    "Vérification KO inté/snap": KO,
    "Vérification OK inté/snap": IN_PROGRESS,
    "Injecté en snap recette": IN_PROGRESS,
    "Vérification KO recette": KO,
    "Vérification OK recette": IN_PROGRESS,
    "Vérification KO prod": KO,
    "Requête appliquée en prod": DONE,
    "Ressource injectée en prod": DONE,
    "Validé": DONE,
    "Non applicable pour ce client": DONE,
}

# Notion « Actions de déploiement » → « Statut ».
ACTION_STATUSES = {
    "À faire": TODO,
    "En cours": IN_PROGRESS,
    "À valider": IN_PROGRESS,
    "À suivre": DONE,
    "Terminé": DONE,
    "Non concerné": DONE,
}

# Notion « Retours Recette » → « Statut » (options génériques seulement).
RETOUR_STATUSES = {
    "À faire": TODO,
    "INTE - Prio déploiement": TODO,
    "INTE - En cours": IN_PROGRESS,
    "INTE - 🤔 Besoin clarification retour": IN_PROGRESS,
    "INTE - 🪪 Besoin matricule d'exemple": IN_PROGRESS,
    "INTE - En attente donnée client": IN_PROGRESS,
    "INTE - Fixed : à réinjecter": IN_PROGRESS,
    "INTE - En attente d'injection": IN_PROGRESS,
    "INTE - Post déploiement": TODO,
    "OPS - À vérifier avec le client": IN_PROGRESS,
    "OPS - Validation à faire en INTEGRATION": IN_PROGRESS,
    "OPS - Vérifié et pas OK": KO,
    "INTE - Vérifié en INTEGRATION": DONE,
    "OPS - Vérifié en INTEGRATION": DONE,
    "Validé": DONE,
    "Abandonné": DONE,
    "Doublon": DONE,
    "Non concerné": DONE,
}


GROUP_LABELS = {TODO: "À faire", IN_PROGRESS: "En cours", KO: "KO", DONE: "Terminé"}


def kanban_columns(cards: list[tuple], statuses: dict[str, str]) -> list[dict]:
    """Vue kanban : une colonne par statut présent, (fiche, élément) dedans. Ordre : à faire, en cours, KO, terminé,
    puis l'ordre des options Notion dans chaque groupe ; un statut hors liste va dans son groupe, en fin."""
    order = list(GROUP_LABELS)
    known = list(statuses)
    columns: dict[str, dict] = {}
    for d, item in cards:
        col = columns.setdefault(item.status, {"status": item.status, "group": item.group, "cards": []})
        col["cards"].append((d, item))
    rank = lambda c: (order.index(c["group"]) if c["group"] in order else 0,
                      known.index(c["status"]) if c["status"] in known else len(known), c["status"])
    return sorted(columns.values(), key=rank)


NOTION_PAGE = re.compile(r"^https://(?:app\.notion\.com|www\.notion\.so)/(?:p/)?([0-9a-f]{32})$")


def notion_link(url: str) -> str:
    """Lien ouvrable d'une page Notion. La synchro SQL renvoie « https://app.notion.com/<id> », sans le « /p/ »
    qu'attend Notion (lien en erreur) : on le remet. Les autres adresses sont laissées telles quelles."""
    match = NOTION_PAGE.match((url or "").replace("-", "") if url and "notion" in url else url or "")
    return f"https://app.notion.com/p/{match.group(1)}" if match else url


def status_group(status: str, statuses: dict[str, str]) -> str:
    return statuses.get(status, TODO)


def group_counts(groups: list[str]) -> dict[str, int]:
    """Nombre d'éléments par groupe de statut, dans l'ordre à faire, en cours, KO, terminé."""
    return {g: groups.count(g) for g in GROUP_LABELS}


@dataclass
class ResourceInjection:
    name: str
    status: str = "A faire"
    dag_id: str | None = None
    note: str = ""

    @property
    def group(self) -> str:
        return status_group(self.status, INJECTION_STATUSES)


CED_TITLE = re.compile(r"\b(CED|GED|fichiers?)\b", re.IGNORECASE)


@dataclass
class Action:
    title: str
    status: str = "À faire"
    injection_status: str = ""  # « Statut intégration - Injections »
    injection: bool = False  # carte d'injection : « INTE Injection » dans Actions à mener (manuel ou Bible)
    resources: list[str] = field(default_factory=list)  # ressources liées (noms orchestrator : firm, visit_histo…)
    teams: list[str] = field(default_factory=list)  # équipes (INTE, OPS, Formation…), d'après les types d'action
    owner: str = ""
    due: date | None = None
    comment: str = ""
    vigilance: bool = False
    event: bool = False  # « Événement » : dates clés
    notion_url: str = ""

    @property
    def group(self) -> str:
        return status_group(self.status, ACTION_STATUSES)

    @property
    def injection_group(self) -> str:
        return status_group(self.injection_status, INJECTION_STATUSES)

    @property
    def ref(self) -> str:
        """Référence courte de la carte (6 derniers caractères de l'id Notion), comme l'id d'un retour."""
        found = re.findall(r"[0-9a-f]{32}", (self.notion_url or "").replace("-", ""))
        return found[-1][-6:].upper() if found else ""

    @property
    def is_ced(self) -> bool:
        """Carte d'intégration sur les fichiers (CED) : « CED », « GED » ou « fichier(s) » dans l'intitulé d'une carte
        d'injection ou préfixée [INTE]. Notion n'a pas de champ dédié : règle sur l'intitulé, à revoir si besoin."""
        inte = self.injection or self.title.upper().startswith("[INTE]")
        return inte and bool(CED_TITLE.search(self.title))


@dataclass
class RetourRecette:
    """Retour de recette. Métadonnées dans la fiche (versionnée) ; contenu (commentaire, exemple, analyse…)
    lu à part depuis data/notion/<client>/contenu-recette.json, exclu de git (décision du 2026-11-05).
    """

    id: str
    status: str = "À faire"
    data_type: str = ""  # « Type de données » (Visites historiques, AMT…)
    software: str = ""  # « Logiciel »
    owners: list[str] = field(default_factory=list)  # « Responsable » : noms (uuids Notion dans la fiche, traduits à la lecture)
    resource: str | None = None  # ressource orchestrator, si connue
    due: date | None = None
    created: date | None = None
    notion_url: str = ""
    # contenu Notion (contenu-recette.json)
    ligne: str = ""  # « Ligne du cahier de recette »
    type_retour: str = ""
    commentaire_client: str = ""
    exemple: str = ""
    analyse: str = ""
    solutions: str = ""
    discussion: str = ""
    echanges: str = ""

    @property
    def content(self) -> list[tuple[str, str]]:
        """(libellé, texte) des champs de contenu renseignés, dans l'ordre de lecture d'un retour."""
        fields = [("Ligne du cahier de recette", self.ligne), ("Type de retour", self.type_retour),
                  ("Commentaire client", self.commentaire_client), ("Exemple salarié / adhérent", self.exemple),
                  ("Analyse padoa", self.analyse), ("Solutions possibles", self.solutions),
                  ("Discussion INTE/OPS", self.discussion), ("Échanges / suivi", self.echanges)]
        return [(label, text) for label, text in fields if text]

    @property
    def group(self) -> str:
        return status_group(self.status, RETOUR_STATUSES)

    @property
    def is_open(self) -> bool:
        return self.group != DONE

    @property
    def label(self) -> str:
        return f"Retour {self.id}" + (f" · {self.ligne or self.data_type}" if (self.ligne or self.data_type) else "")


@dataclass
class Regle:
    title: str
    resource_table: str = ""
    type: str = ""  # Règle obligatoire - padoa, Règle par défaut - logiciel…
    application: str = ""  # Par requête, En flux - Extract…
    status: str = ""


@dataclass
class Remontee:
    title: str
    contexte: str = ""  # Formation, Déploiement, Quotidien
    status: str = "À traiter"
    posted: date | None = None
    slack_url: str = ""


@dataclass
class Link:
    label: str
    url: str


@dataclass
class Stack:
    """Une stack Padoa du client (prod, recette, formation…), saisie dans la configuration du client (config/clients/<slug>.yaml)."""
    label: str
    url: str
    env: str = ""


HEALTH_LABELS = {
    "ok": "Dans les temps",
    "warn": "En retard",
    "ko": "Très en retard",
    "done": "Intégration terminée",
    "none": "Avancement attendu non calculable (dates de réception ou de freeze manquantes)",
}

MILESTONE_LABELS = {
    "kickoff": "Kick-off",
    "reception_donnees": "Réception des données",
    "recette_1": "Recette 1",
    "recette_2": "Recette 2",
    "recette_3": "Recette 3",
    "freeze": "Freeze",
    "formation": "Formation",
    "deploiement": "Déploiement",
    "deploiement_lot2": "Déploiement lot 2",
}


@dataclass
class Deployment:
    slug: str
    client: str
    software: str
    status: str = "en cours"
    emoji: str = ""  # icône du dashboard d'intégration dans Notion
    orchestrator: list[str] = field(default_factory=list)  # clés client de l'orchestrator (configuration du client)
    notion_url: str = ""
    synced_at: str = ""
    injection_counts: dict[str, int] = field(default_factory=dict)  # synchro Notion : nb de cartes par statut
    milestones: dict[str, date] = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    stacks: list[Stack] = field(default_factory=list)
    resources: list[ResourceInjection] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    retours: list[RetourRecette] = field(default_factory=list)
    regles: list[Regle] = field(default_factory=list)
    remontees: list[Remontee] = field(default_factory=list)

    @property
    def go_live(self) -> date | None:
        return self.milestones.get("deploiement")

    @property
    def days_left(self) -> int | None:
        return (self.go_live - date.today()).days if self.go_live else None

    @property
    def progress(self) -> int:
        """Avancement intégration : part des cartes (synchro Notion) ou des ressources au statut terminé, en %."""
        if self.injection_counts:
            total = sum(self.injection_counts.values())
            done = sum(n for s, n in self.injection_counts.items() if status_group(s, INJECTION_STATUSES) == DONE)
            return round(100 * done / total) if total else 0
        if not self.resources:
            return 0
        return round(100 * sum(r.group == DONE for r in self.resources) / len(self.resources))

    @property
    def injection_blocks(self) -> list[dict]:
        """Cartes d'injection regroupées par ressources liées, les blocs à problème d'abord (KO, puis en cours…)."""
        blocks: dict[tuple[str, ...], list[Action]] = {}
        for a in self.injection_cards:
            blocks.setdefault(tuple(a.resources), []).append(a)
        rank = {KO: 0, IN_PROGRESS: 1, TODO: 2, DONE: 3}
        out = []
        for key, cards in blocks.items():
            counts = group_counts([a.injection_group for a in cards])
            worst = min((rank[g] for g, n in counts.items() if n), default=3)
            out.append({
                "resources": list(key),
                "id": "-".join(key) or "sans-ressource",
                "label": " + ".join(key) or "Sans ressource liée",
                "cards": sorted(cards, key=lambda a: (rank[a.injection_group], a.due or date.max, a.title)),
                "counts": counts,
                "worst": worst,
            })
        return sorted(out, key=lambda b: (not b["resources"], b["worst"], b["label"]))

    @property
    def blocking_points(self) -> list[dict]:
        """« Où ça coince » : blocs d'injection (cartes regroupées par ressources liées, comme l'onglet Injections) et types de
        retours de recette qui ont au moins un KO, les plus de KO d'abord, puis le plus d'éléments ouverts.
        Un KO d'injection et un KO de recette pèsent pareil. Chaque point mène à sa liste (bloc ou filtre « type »)."""
        points = [{"kind": "injection", "label": b["label"], "resources": b["resources"], "counts": b["counts"], "total": len(b["cards"]),
                   "href": f"/deploiements/{self.slug}/injections#{self.slug}-{b['id']}"} for b in self.injection_blocks]
        by_type: dict[str, list[str]] = {}
        for r in self.retours:
            by_type.setdefault(r.data_type, []).append(r.group)
        recette = f"/deploiements/{self.slug}/recette"
        points += [{"kind": "retour", "label": t or "Type non précisé", "resources": [], "counts": group_counts(groups), "total": len(groups),
                    "href": f"{recette}#type={quote(t)}" if t else recette}  # sans type : pas de filtre qui viderait la liste
                   for t, groups in by_type.items()]
        for p in points:
            p["open"] = p["total"] - p["counts"][DONE]
        return sorted((p for p in points if p["counts"][KO]), key=lambda p: (-p["counts"][KO], -p["open"], p["label"]))

    @property
    def ced_cards(self) -> list[Action]:
        return [a for a in self.actions if a.is_ced]

    @property
    def injection_cards(self) -> list[Action]:
        """Cartes Notion avec un statut d'injection (toutes, « A faire » compris, comme la vue TO DO du dashboard)."""
        return [a for a in self.actions if a.injection]

    @property
    def active_actions(self) -> list[Action]:
        """Actions à suivre : vigilance, date clé, échéance proche, ou travail en cours (action ou injection)."""
        today = date.today()
        return [
            a for a in self.actions
            if a.vigilance or a.event
            or (a.due and -7 <= (a.due - today).days <= 30)
            or a.group == IN_PROGRESS
            or a.injection_group in (IN_PROGRESS, KO)
        ]

    @property
    def expected_progress(self) -> int | None:
        """Avancement attendu : linéaire de 0 % à la réception des données à 100 % au freeze."""
        start, end = self.milestones.get("reception_donnees"), self.milestones.get("freeze")
        if not start or not end or end <= start:
            return None
        ratio = (date.today() - start).days / (end - start).days
        return round(100 * min(max(ratio, 0), 1))

    @property
    def health(self) -> str:
        """Même règle que « 📊 Inté - Statut » dans Notion."""
        if self.progress == 100:
            return "✅"
        if self.expected_progress is None:
            return "⚪"
        gap = self.progress - self.expected_progress
        return "🟢" if gap >= -5 else "🟠" if gap >= -15 else "🔴"

    @property
    def health_level(self) -> str:
        """ok, warn, ko, done ou none : classe CSS de la pastille de santé."""
        return {"🟢": "ok", "🟠": "warn", "🔴": "ko", "✅": "done"}.get(self.health, "none")

    @property
    def health_label(self) -> str:
        return HEALTH_LABELS[self.health_level]

    @property
    def days_to_freeze(self) -> int | None:
        freeze = self.milestones.get("freeze")
        return (freeze - date.today()).days if freeze else None

    @property
    def injection_groups(self) -> dict[str, int]:
        if self.injection_counts:
            counts = {g: 0 for g in GROUP_LABELS}
            for status, n in self.injection_counts.items():
                counts[status_group(status, INJECTION_STATUSES)] += n
            return counts
        return group_counts([r.group for r in self.resources])

    @property
    def retour_groups(self) -> dict[str, int]:
        return group_counts([r.group for r in self.retours])

    @property
    def open_retours_by_type(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for r in self.open_retours:
            key = r.data_type or "Non précisé"
            counts[key] = counts.get(key, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    @property
    def timeline(self) -> dict | None:
        """Position d'aujourd'hui et des jalons entre le kick-off (ou le premier jalon) et le déploiement, en %."""
        if not self.milestones or not self.go_live:
            return None
        start = self.milestones.get("kickoff") or min(self.milestones.values())
        span = (self.go_live - start).days
        if span <= 0:
            return None
        pos = lambda d: round(100 * min(max((d - start).days / span, 0), 1), 1)
        marks = [(MILESTONE_LABELS.get(k, k), v, pos(v)) for k, v in sorted(self.milestones.items(), key=lambda kv: kv[1])
                 if start <= v <= self.go_live]
        return {"start": start, "end": self.go_live, "today": pos(date.today()), "marks": marks}

    @property
    def vigilance(self) -> list[Action]:
        return [a for a in self.actions if a.vigilance and a.group != DONE]

    @property
    def open_retours(self) -> list[RetourRecette]:
        return [r for r in self.retours if r.is_open]
