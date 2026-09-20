"""
TypeSafe Jev as a second ranking signal.

Each touched production function is judged on its own (full new code, previous version and
the diff inside it) with four independent questions. The final triage order is the mean of
the function's percentile ranks on CRAP and on each Jev answer. On a forward-SZZ benchmark
of 563 bug-introducing commits this combination put later-fixed functions higher than CRAP
alone (evals/jev_szz_experiment.py); no single Jev answer beat CRAP on its own.
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

TYPESAFE_API_URL = "https://api.typesafe.ai/v1/systemone"
MAX_CODE_CHARS = 48_000   # ~12k tokens: state + longest question stay well under Jev's 32k window
MAX_DIFF_CHARS = 24_000

QUESTIONS: Dict[str, Dict[str, Any]] = {
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
    "semantic_risk": {
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
JEV_DIMENSIONS = tuple(QUESTIONS)


def _number(value: Any, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= hi:
        raise ValueError(f"unexpected value {value!r}")
    return float(value)


def function_state(file: str, function: str, old_code: str, new_code: str, diff: str) -> Dict[str, str]:
    return {
        "file": file,
        "function": function,
        "old_code": (old_code or "")[:MAX_CODE_CHARS],
        "new_code": (new_code or "")[:MAX_CODE_CHARS],
        "diff": (diff or "")[:MAX_DIFF_CHARS],
    }


def ask_jev(
    api_key: str,
    state: Any,
    questions: Dict[str, Dict[str, Any]],
    timeout: int = 60,
    api_url: Optional[str] = None,
    retries: int = 5,
    backoff: float = 1.0,
) -> Dict[str, Any]:
    """
    One Jev request. Returns {"answers": ..., "usage": ...}, or {"error": ...} after exhausting
    retries on rate limits/server errors, on client errors, or on an unreadable response.
    """
    body = json.dumps({"model": "jev-latest", "state": state, "questions": questions}).encode("utf-8")
    error = "no attempt"
    for attempt in range(retries + 1):
        req = urllib.request.Request(api_url or TYPESAFE_API_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data.get("answers"), dict):
                raise ValueError("response without answers")
            return data
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}"
            retry_after = exc.headers.get("retry-after") if exc.headers else None
            exc.close()
            if exc.code not in (429, 500, 502, 503, 504):
                break
            time.sleep(float(retry_after) if retry_after else backoff * 2 ** attempt)
        except (ValueError, KeyError, TypeError) as exc:
            error = f"malformed answer: {exc}"
            break
        except Exception as exc:  # network errors, timeouts
            error = f"{type(exc).__name__}: {exc}"
            time.sleep(backoff * 2 ** attempt)
    return {"error": error}


def query_jev_function(
    api_key: str,
    state: Dict[str, str],
    timeout: int = 60,
    api_url: Optional[str] = None,
    retries: int = 5,
    backoff: float = 1.0,
) -> Dict[str, Any]:
    """The four ranking questions about one function; {"error": ...} on failure."""
    data = ask_jev(api_key, state, QUESTIONS, timeout=timeout, api_url=api_url, retries=retries, backoff=backoff)
    if "error" in data:
        return data
    answers = data["answers"]
    try:
        result = {
            "introduces_bug": _number((answers.get("introduces_bug") or {}).get("noul"), 1.0),
            "edge_cases": _number((answers.get("edge_cases") or {}).get("noul"), 1.0),
            "behavior_change": _number((answers.get("behavior_change") or {}).get("score"), 3.0),
            "semantic_risk": _number((answers.get("semantic_risk") or {}).get("score"), 3.0),
        }
    except ValueError as exc:
        return {"error": f"malformed answer: {exc}"}
    result["confidence"] = round(float((answers.get("semantic_risk") or {}).get("confidence") or 0.0), 2)
    result["usage"] = data.get("usage", {})
    return result


def percentiles(scores: Sequence[float]) -> List[float]:
    """Tie-aware percentile rank (0 = lowest, 1 = highest) of each score."""
    n = len(scores)
    if n == 1:
        return [1.0]
    order = sorted(range(n), key=lambda i: scores[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = ((i + j) / 2) / (n - 1)
        i = j + 1
    return out


def triage_scores(crap: Sequence[float], jev: Sequence[Optional[Dict[str, Any]]]) -> List[float]:
    """
    Mean percentile rank over CRAP and the Jev dimensions. A function without a Jev answer
    (failure, or not sent) ranks lowest on the Jev dimensions.
    """
    columns = [percentiles(list(crap))]
    for dim in JEV_DIMENSIONS:
        values = [(a or {}).get(dim) for a in jev]
        columns.append(percentiles([v if isinstance(v, (int, float)) else -1.0 for v in values]))
    return [round(sum(col[i] for col in columns) / len(columns), 4) for i in range(len(crap))]
