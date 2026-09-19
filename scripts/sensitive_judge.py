"""
TypeSafe Jev judge for text about to be published in a public repository: does it contain
personal data, or material from one of the maintainer's private projects (described locally in
~/.config/public-safety/private-context.txt, never committed)? Complements the regex checks
in public_safety_check.py (formats) with judgments about meaning (a name with a diagnosis, a
paragraph describing a private system). Measured in evals/sensitive_data_eval.py.
"""

import hashlib
import json
import os
import sys
from typing import Any, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

REPOSITORY = ("diff-risk-sentinel, a public open-source CLI that ranks the functions of a git diff by "
              "complexity and test coverage (CRAP), optionally with TypeSafe Jev, and scans repositories for dead "
              "code and orphan HTTP endpoints. Its evaluation write-ups report aggregate numbers measured on "
              "private codebases, which they mention only anonymously ('a private production monorepo').")
MAX_CHARS = 6000
# Chosen on the dev half, checked once on the held-out half (evals/sensitive_data_eval.py):
# with the regex checks and the local index, 100% precision / 73% recall.
PERSONAL_DATA_BLOCK = 0.7
PRIVATE_PROJECT_BLOCK = 0.8

JUDGE_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "personal_data": {
        "type": "noul",
        "instructions": (
            "`text` is about to be published in `repository` at `file`. Does it contain personal data about "
            "real-looking, identifiable people: a person's name together with contact details, documents (CPF, "
            "IDs), address, health, financial or family information, or messages written by or about a specific "
            "person? Obvious placeholders do not count (example.com addresses, John Doe, Alice and Bob, "
            "'Test User', 000.000.000-00, 'Fulano de Tal')."
        ),
        "criteria": {
            "true": "At least one real-looking person is identifiable together with personal information.",
            "false": "No identifiable person, or only obvious placeholders and generic roles.",
        },
    },
}

# Asked only when the maintainer describes their private projects locally (never committed).
PRIVATE_CONTEXT_FILE = os.path.expanduser("~/.config/public-safety/private-context.txt")
PRIVATE_PROJECT_QUESTION: Dict[str, Any] = {
    "type": "noul",
    "instructions": (
        "`private_projects` describes the maintainer's private projects. Does `text` come from, or reveal "
        "specifics of, any of them — their code, domain rules, operations, systems, clients, people or "
        "incidents? Mentions of software-engineering ideas shared by any project, and anonymous aggregate "
        "numbers ('a private production monorepo'), do not count."
    ),
    "criteria": {
        "true": "The text is recognizably material of one of the described private projects.",
        "false": "Nothing in the text is specific to the described private projects.",
    },
}


def private_context() -> str:
    if not os.path.exists(PRIVATE_CONTEXT_FILE):
        return ""
    with open(PRIVATE_CONTEXT_FILE, encoding="utf-8") as fh:
        return "\n".join(x for x in fh.read().split("\n") if not x.startswith("#")).strip()


def questions() -> Dict[str, Dict[str, Any]]:
    qs = dict(JUDGE_QUESTIONS)
    if private_context():
        qs["private_project"] = PRIVATE_PROJECT_QUESTION
    return qs


def cache_key(path: str, text: str) -> str:
    """Changes with the questions and context, so a new wording is never answered from an old cache."""
    spec = json.dumps([REPOSITORY, questions(), private_context()], sort_keys=True)
    return hashlib.sha256(f"{spec}\n{path}\n{text}".encode("utf-8")).hexdigest()


def state(path: str, text: str) -> Dict[str, Any]:
    st = {"repository": REPOSITORY, "file": path, "text": text[:MAX_CHARS]}
    if private_context():
        st["private_projects"] = private_context()
    return st


def judge(api_key: str, path: str, text: str, **kw) -> Dict[str, Any]:
    from diff_risk_sentinel.jev import ask_jev
    data = ask_jev(api_key, state(path, text), questions(), **kw)
    if "error" in data:
        return data
    try:
        a = data["answers"]
        pii = float(a["personal_data"]["noul"])
        priv = float(a["private_project"]["noul"]) if "private_project" in a else None
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"malformed answer: {exc}"}
    return {"p_personal_data": pii, "p_private_project": priv, "p_sensitive": max(pii, priv or 0.0)}


def blocks(answer: Dict[str, Any]) -> bool:
    return answer["p_personal_data"] >= PERSONAL_DATA_BLOCK or (answer.get("p_private_project") or 0.0) >= PRIVATE_PROJECT_BLOCK
