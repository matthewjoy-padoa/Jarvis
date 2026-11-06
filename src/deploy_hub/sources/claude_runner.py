"""Lance Claude Code en headless sur une demande, dans un worktree dédié de l'orchestrator."""

import itertools
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from deploy_hub.models import Deployment, RetourRecette
from deploy_hub.sources import ActionResult


@dataclass
class AgentRun:
    id: int
    slug: str
    client: str
    subject: str
    branch: str
    prompt: str
    started_at: datetime
    status: str


TO_FILL = "À COMPLÉTER"  # marqueur laissé dans le brief quand une info manque ; le lancement est refusé tant qu'il reste


def _tag(name: str, text: str) -> str:
    return f"<{name}>\n{text.strip()}\n</{name}>"


def retour_prompt(d: Deployment, r: RetourRecette, description: str = "") -> str:
    """Brief pré-rempli de l'agent sur un retour de recette, pensé pour Claude Code dans integration-orchestrator.

    Contexte et contenu du retour balisés, démarche tirée de kb/anatomie-orchestrator et des patterns
    correctif-au-niveau-logiciel / requete-client-doublon-logiciel, contrat en cinq lignes, compte rendu attendu.
    La personne qui lance l'agent n'a qu'à relire, et à compléter <precisions> si besoin.
    """
    software = r.software or d.software
    branch = f"fix-{d.slug}-{r.id.lower()}"
    context = [
        f"Client : {d.client} (slug Jarvis : {d.slug}) · logiciel du retour : {software} · logiciel(s) du client : {d.software}",
        f"Retour de recette : {r.id} · statut Notion : {r.status} · type de données : {r.data_type or 'non précisé'}"
        + (f" · type de retour : {r.type_retour}" if r.type_retour else ""),
        f"Ressource orchestrator : {r.resource}" if r.resource
        else f"Ressource orchestrator : à identifier à partir du type de données « {r.data_type or '?'} »",
    ]
    if r.due:
        context.append(f"Deadline : {r.due.strftime('%d/%m/%Y')}" + (f" · déploiement le {d.go_live.strftime('%d/%m/%Y')}" if d.go_live else ""))
    if r.notion_url:
        context.append(f"Page Notion du retour : {r.notion_url}")

    tags = {"Ligne du cahier de recette": "ligne_cahier", "Type de retour": None, "Commentaire client": "commentaire_client",
            "Exemple salarié / adhérent": "exemple", "Analyse padoa": "analyse_padoa", "Solutions possibles": "solutions_possibles",
            "Discussion INTE/OPS": "discussion_inte_ops", "Échanges / suivi": "echanges_suivi"}
    content = [_tag(tags[label], text) for label, text in r.content if tags.get(label)]
    if not content:
        content = [_tag("description", f"{TO_FILL} : ce retour n'a pas de contenu dans Notion. Décris l'écart constaté, "
                                       "l'attendu et un exemple (identifiant ou cas), puis retire cette consigne.")]

    parts = [
        "Tu corriges un retour de recette d'une reprise de données Padoa, dans le dépôt integration-orchestrator. "
        "Le client a constaté un écart entre ses données d'origine et ce qui a été injecté dans Padoa. "
        "Lis tout le brief avant d'agir.",
        _tag("contexte", "\n".join(context)),
        _tag("retour", "\n".join(content)),
        _tag("precisions", description.strip() or "(rien à ajouter)"),
        _tag("demarche", "\n".join([
            "1. Reformule en deux lignes l'écart : ce qui est attendu, ce qui est constaté. Si le retour ne permet pas de le dire, "
            "arrête-toi et liste tes questions au lieu de deviner.",
            "2. Trouve les fichiers qui interviennent pour ce client et cette ressource à chaque étape (extract SQL, mapping, settings, "
            "composant, transform, tests). Le code existe à deux niveaux : logiciel (`src/extract/softwares/<logiciel>/`, "
            "`src/mappings/software/<logiciel>/`, `src/lib/steps/transform/software/<logiciel>/`) et client en surcharge "
            "(`src/extract/customers/<client>/`, `src/lib/components/<logiciel ou version>/<client>/`). La clé du client et de sa "
            "version de logiciel sont dans `src/shared/constants/customers.py`.",
            "3. Avant de modifier une requête logiciel, vérifie qu'aucun client n'en a une copie : "
            "`git ls-files 'src/extract/customers/*/queries/<chemin relatif>.sql'`. Toutes les copies sont à traiter ensemble.",
            "4. Choisis le niveau du correctif : client si le problème est propre à ce client, logiciel s'il vient du logiciel "
            "(liste alors les autres clients concernés). Ne modifie `clean`, `compare` ou `load` que si c'est indispensable : "
            "ces étapes sont communes à tous les clients.",
            "5. Corrige au plus petit endroit, puis ajoute ou adapte un test qui échoue avant le correctif et passe après, "
            "avec des valeurs fictives.",
        ])),
        _tag("contrat", "\n".join([
            "- Entrée : ce brief, le code du client et de son logiciel dans integration-orchestrator, à jour de main.",
            f"- Sortie : un correctif sur la branche `{branch}` et une PR intitulée `[FIX]({d.slug}) <résumé>` "
            "(scope = le logiciel si le correctif est au niveau logiciel).",
            "- Validation : `uv run pytest tests/src/lib/steps/extract tests/src/lib/components/<logiciel>` vert, "
            "plus les tests que tu as ajoutés ; cite la commande et son résultat.",
            "- Limites : ne lis pas les logs des DAGs Airflow ; ne pousse pas sur main et ne force pas de push ; aucune donnée client "
            "(matricule, nom, exemple du retour) dans le code, les tests, les commits ou la PR ; pas de modification hors du périmètre du retour.",
            "- Résultat : la PR ouverte, ou un rapport qui dit précisément ce qui manque pour corriger.",
        ])),
        _tag("compte_rendu", "Termine par : la cause (fichier:ligne), le correctif et son niveau (client ou logiciel), les tests "
                             "(commande et résultat), le lien de la PR, et ce qu'il faudra vérifier en recette."),
    ]
    return "\n\n".join(parts)


class ClaudeRunner:
    def __init__(self, orchestrator_dir: Path, live: bool = False, notifications=None):
        self.orchestrator_dir = orchestrator_dir
        self.live = live
        self.notifications = notifications  # NotificationStore : « agent terminé »
        self.runs: list[AgentRun] = []
        self._ids = itertools.count(1)

    def build_command(self, prompt: str, branch: str) -> list[str]:
        # Option --worktree à vérifier avec `claude --help` sur la VM.
        return ["claude", "-p", prompt, "--worktree", branch]

    def _launch(self, d: Deployment, subject: str, branch: str, prompt: str) -> ActionResult:
        if self.live:
            # À brancher : subprocess.Popen(cmd, cwd=self.orchestrator_dir), suivi du process et de la PR.
            raise NotImplementedError("Lancement Claude réel non branché")
        run = AgentRun(next(self._ids), d.slug, d.client, subject, branch, prompt, datetime.now(), "simulé")
        self.runs.insert(0, run)
        self.finished(run, "terminé (simulé)")
        return ActionResult(True, f"Simulation : agent #{run.id} sur {subject}, branche {branch}, dans {self.orchestrator_dir}")

    def finished(self, run: AgentRun, status: str, detail: str = "") -> None:
        """Fin d'un agent : met à jour son état et notifie. À appeler aussi par le suivi du process réel."""
        run.status = status
        if self.notifications:
            self.notifications.add(f"agent:{run.id}:{run.started_at.isoformat()}", "agent", "info",
                                   f"Agent #{run.id} {status} · {run.client}", detail or f"{run.subject} · branche {run.branch}", "/agents", run.client)

    def launch(self, d: Deployment, resource: str) -> ActionResult:
        prompt = f"Ressource {resource} du client {d.client} (logiciel {d.software}) : lis la fiche de déploiement, code, teste, ouvre la PR."
        return self._launch(d, resource, f"{d.slug}-{resource}", prompt)

    def launch_retour(self, d: Deployment, r: RetourRecette, description: str = "", prompt: str = "") -> ActionResult:
        """prompt : le brief relu (et éventuellement modifié) par la personne qui lance l'agent ; sinon il est construit ici."""
        return self._launch(d, f"retour {r.id}", f"fix-{d.slug}-{r.id.lower()}", prompt.strip() or retour_prompt(d, r, description))
