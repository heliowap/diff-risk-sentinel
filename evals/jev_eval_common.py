#!/usr/bin/env python3
"""
Shared pieces of the Jev ranking experiments: the questions asked about each changed
function, the Jev call, and the list of touched production functions of a range with their
new and previous code. Used by jev_szz_experiment.py.
"""

import contextlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from diff_risk_sentinel.cli import run_sentinel  # noqa: E402
from diff_risk_sentinel.complexity import extract_functions  # noqa: E402
from diff_risk_sentinel.diff import read_blobs, resolve_revisions  # noqa: E402
from diff_risk_sentinel.signals import is_test_path  # noqa: E402
import historical_eval as h  # noqa: E402

API_URL = "https://api.typesafe.ai/v1/systemone"
MAX_CODE_CHARS = 48_000   # ~12k tokens: keeps state + longest question well under 32k tokens
MAX_DIFF_CHARS = 24_000

QUESTIONS = {
    "introduces_bug": {
        "type": "noul",
        "instructions": (
            "`new_code` is a function after a code change; `old_code` is the same function before it "
            "(empty when the function is new) and `diff` shows the changed lines. Does the change most "
            "likely introduce a bug, i.e. code that will behave incorrectly for some realistic input, "
            "state or call order?"
        ),
        "criteria": {
            "true": "A concrete input, state or sequence exists for which the changed code gives a wrong result, "
                    "crashes, loses data or skips required work.",
            "false": "The changed code behaves correctly for realistic inputs; the change is a refactor, a "
                     "correct feature addition, logging, naming or formatting.",
        },
    },
    "edge_cases": {
        "type": "noul",
        "instructions": (
            "Looking only at the lines changed in `diff` (in the context of `new_code`): do they mishandle a "
            "plausible edge case such as an empty collection, a null/None/undefined value, zero, a falsy but "
            "valid value, a missing key, a boundary size, or an initial state equal to a legitimate value?"
        ),
        "criteria": {
            "true": "At least one such edge case reaches the changed lines and is handled incorrectly.",
            "false": "Edge cases reaching the changed lines are handled or cannot occur.",
        },
    },
    "inconsistent_assumption": {
        "type": "noul",
        "instructions": (
            "Does the change alter or rely on a format, unit, time zone, date window, identifier/key shape, "
            "validation rule or default that other code (callers, readers of stored data, other services) is "
            "likely to assume differently?"
        ),
        "criteria": {
            "true": "The change introduces or depends on such an assumption and a mismatch with other code is plausible.",
            "false": "No such cross-code assumption is changed or relied upon.",
        },
    },
    "behavior_change": {
        "type": "score",
        "instructions": "How much does the change alter the observable behavior of this function?",
        "criteria": [
            "None: pure refactor, rename, formatting, comments, logging or types only",
            "Small: same results for all realistic inputs except a narrow, intended case",
            "Moderate: new or changed branches, conditions, queries or outputs for some inputs",
            "Large: different results, side effects or error behavior for many inputs",
        ],
    },
    "semantic_risk": {  # the question diff-risk-sentinel --jev asks today, for comparison
        "type": "score",
        "instructions": (
            "Rate the risk of regression, race condition, broken contract or unexpected side effect "
            "introduced by this change to the function."
        ),
        "criteria": [
            "Harmless: rename, logging, formatting or visual styling",
            "Low: simple, safe logical extension with strict typing",
            "Medium: new conditional branches, extra queries or filters",
            "Critical: transactional change, sensitive business rule change, or missing exception handling",
        ],
    },
}


def jev(api_key: str, state: Dict) -> Dict:
    body = json.dumps({"model": "jev-latest", "state": state, "questions": QUESTIONS}).encode()
    for attempt in range(6):
        req = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            a = data["answers"]
            return {
                "introduces_bug": a["introduces_bug"]["noul"],
                "edge_cases": a["edge_cases"]["noul"],
                "inconsistent_assumption": a["inconsistent_assumption"]["noul"],
                "behavior_change": a["behavior_change"]["score"],
                "semantic_risk": a["semantic_risk"]["score"],
                "input_tokens": data.get("usage", {}).get("input_tokens"),
            }
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(float(exc.headers.get("retry-after") or 2 ** attempt))
                continue
            return {"error": f"HTTP {exc.code}: {exc.read()[:200]!r}"}
        except Exception as exc:  # network hiccup
            last_error = str(exc)
            time.sleep(2 ** attempt)
            continue
    return {"error": "retries exhausted"}


def touched_functions(repo: str, rng: str) -> List[Dict]:
    out = "/tmp/_jev_exp.json"
    with contextlib.redirect_stdout(io.StringIO()):
        run_sentinel(base=rng, repo=repo, coverage=[], output=out, top=10 ** 6, top_consumers=0,
                     threshold_crap=0, threshold_ccn=0, threshold_delta=0)
    with open(out) as fh:
        data = json.load(fh)
    os.remove(out)
    items = [t for t in data["targets"] if not is_test_path(t["file"])]
    old_rev, new_rev = resolve_revisions(rng, repo)
    files = {t["file"] for t in items}
    blobs = read_blobs(repo, [(new_rev, f) for f in files] + [(old_rev, f) for f in files])
    old_funcs = {}
    for f in files:
        src = blobs.get((old_rev, f))
        old_funcs[f] = {fn["name"]: fn for fn in (extract_functions(f, src) or [])} if src else {}
    for t in items:
        a, b = (int(x) for x in t["lines"].split("-"))
        new_src = (blobs.get((new_rev, t["file"])) or "").split("\n")
        t["new_code"] = "\n".join(new_src[a - 1:b])[:MAX_CODE_CHARS]
        old = old_funcs.get(t["file"], {}).get(t["function"])
        old_src = (blobs.get((old_rev, t["file"])) or "").split("\n")
        t["old_code"] = "\n".join(old_src[old["start_line"] - 1:old["end_line"]])[:MAX_CODE_CHARS] if old else ""
    return items
