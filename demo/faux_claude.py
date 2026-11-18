"""Faux binaire `claude` de l'environnement local (demo/lancer.py) : ni modèle, ni Notion, ni réseau.

- Agent (`--output-format stream-json`) : quelques étapes espacées de 2 s, puis un compte rendu. Un agent sur deux s'arrête
  sur une question (« en attente d'une réponse ») ; la reprise après réponse finit normalement.
- Routine de synchro Notion (`--output-format text`) : dépose ses fichiers dans data/notion/ au rythme d'une vraie
  synchro (empreinte, plan, requêtes, fusion) pour faire avancer le cercle d'avancement, met à jour l'heure de dernière
  synchro de chaque client et répond « inchangé ». Les fiches ne changent pas.
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PAUSE = 2  # secondes entre deux étapes


def say(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


def agent(resume: bool) -> None:
    session = "demo-" + Path.cwd().name
    say({"type": "system", "subtype": "init", "session_id": session})
    steps = [("Je lis le brief et le mapping concerné.", "Read", {"file_path": "src/mappings/demo.py"}, "(contenu fictif)"),
             ("Je corrige le filtre et je lance les tests.", "Bash", {"command": "uv run pytest tests/demo"}, "3 passed"),
             ("J'ouvre la PR.", "Bash", {"command": "gh pr create --draft"}, "https://github.com/exemple/orchestrator-demo/pull/1")]
    counter = Path(os.environ["FAUX_CLAUDE_ETAT"]) / "faux-claude-agents.txt"  # exporté par le lanceur (demo/.etat/claude)
    n = int(counter.read_text() or 0) + 1 if counter.is_file() else 1
    counter.write_text(str(n))
    asks = not resume and n % 2 == 0  # un agent sur deux pose une question
    for text, tool, args, out in steps[:1] if asks else steps:
        time.sleep(PAUSE)
        say({"type": "assistant", "message": {"content": [{"type": "text", "text": text}, {"type": "tool_use", "name": tool, "input": args}]}})
        say({"type": "user", "message": {"content": [{"type": "tool_result", "content": out}]}})
    time.sleep(PAUSE)
    result = ("Je n'ai rien modifié.\n\n**Questions :**\n1. Correctif au niveau du client ou du logiciel ?\n   - client\n   - logiciel"
              if asks else "Cause : filtre trop strict dans src/mappings/demo.py:12 (fictif). Tests : 3 passed.\n"
                           "PR : https://github.com/exemple/orchestrator-demo/pull/1")
    say({"type": "result", "subtype": "success", "num_turns": 2 if asks else 4, "session_id": session,
         "total_cost_usd": 0.05 if asks else 0.12, "result": result})


def routine() -> None:
    data = Path(os.environ["JARVIS_DATA_DIR"])
    notion = data / "notion"
    slugs = sorted(p.stem for p in (data / "deploiements").glob("*.md"))
    time.sleep(PAUSE)
    (notion / "empreintes.json").write_text(json.dumps({"results": [{"cle": s} for s in slugs]}), encoding="utf-8")
    time.sleep(PAUSE)
    outputs = ["client", "actions_delta", "recette_delta"]
    for slug in slugs:
        raw = notion / slug
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "plan.json").write_text(json.dumps({"sorties": outputs}), encoding="utf-8")
    for slug in slugs:
        for name in outputs:
            time.sleep(PAUSE / 2)
            (notion / slug / f"{name}.json").write_text('{"results": []}', encoding="utf-8")
    time.sleep(PAUSE)
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    for slug in slugs:
        raw = notion / slug
        for name in ("plan", *outputs):
            (raw / f"{name}.json").unlink(missing_ok=True)
        (raw / "etat-synchro.json").write_text("{}", encoding="utf-8")
    checks = notion / "verifications.json"
    known = json.loads(checks.read_text(encoding="utf-8")) if checks.is_file() else {}
    checks.write_text(json.dumps(known | dict.fromkeys(slugs, now)), encoding="utf-8")
    time.sleep(PAUSE)
    print("\n".join(f"{slug} : inchangé (faux claude, 3 requêtes fictives)" for slug in slugs))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--version" in args:
        print("0.0.0 (faux claude de l'environnement local)")
    elif "stream-json" in args:
        sys.stdin.read()
        agent(resume="--resume" in args)
    else:
        sys.stdin.read()
        routine()
