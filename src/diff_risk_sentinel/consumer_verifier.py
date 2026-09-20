"""
Consumer Contract Verifier via TypeSafe Jev.

Evaluates untouched functions in the repository that reference tokens whose contract
was modified in the diff. Takes the producer change (diff hunk) and consumer usage
(function body) and poses an atomic, typed question to Jev:
does the consumer rely on a contract or format that the diff broke?
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .jev import ask_jev

CONSUMER_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "contract_broken": {
        "type": "noul",
        "instructions": (
            "A code change (`producer_diff`) in `producer_file` modified or redefined how `changed_token` "
            "is structured, typed, returned, or handled.\n"
            "`consumer_code` in `consumer_file` is an untouched function that references `changed_token`.\n"
            "Looking at both: does the consumer function make an assumption about `changed_token` "
            "(such as expected argument count/types, return shape, key format, nullability, or error behavior) "
            "that is broken or violated by the producer diff, leading to a defect, exception, or incorrect behavior at runtime?"
        ),
        "criteria": {
            "true": "The consumer's usage of the token is incompatible with the new change and will fail, crash, or behave incorrectly.",
            "false": "The consumer's usage is compatible with the new change, does not rely on the modified parts of the contract, or is unaffected.",
        },
    },
}


def consumer_state(
    token: str,
    producer_file: str,
    producer_diff: str,
    consumer_file: str,
    consumer_function: str,
    consumer_code: str,
) -> Dict[str, Any]:
    """Packages producer diff and consumer code into an evidence-first state."""
    return {
        "changed_token": token,
        "producer_file": producer_file,
        "producer_diff": producer_diff[:24_000],
        "consumer_file": consumer_file,
        "consumer_function": consumer_function,
        "consumer_code": consumer_code[:48_000],
    }


def verify_consumer_with_jev(
    api_key: str,
    token: str,
    producer_file: str,
    producer_diff: str,
    consumer_file: str,
    consumer_function: str,
    consumer_code: str,
    threshold: float = 0.6,
    timeout: int = 45,
) -> Dict[str, Any]:
    """
    Asks Jev whether a consumer outside the diff is broken by the producer diff.
    """
    state = consumer_state(
        token=token,
        producer_file=producer_file,
        producer_diff=producer_diff,
        consumer_file=consumer_file,
        consumer_function=consumer_function,
        consumer_code=consumer_code,
    )

    resp = ask_jev(api_key, state, CONSUMER_QUESTIONS, timeout=timeout)

    if "error" in resp:
        return {
            "is_broken": None,
            "broken_probability": None,
            "verdict": "UNKNOWN",
            "usage": resp.get("usage", {}),
            "error": resp["error"],
        }

    answers = resp.get("answers", {})
    broken_prob = float((answers.get("contract_broken") or {}).get("noul", 0.0))
    is_broken = broken_prob >= threshold

    return {
        "is_broken": is_broken,
        "broken_probability": broken_prob,
        "verdict": "PROBABLE_CONTRACT_BREAK" if is_broken else "COMPATIBLE",
        "usage": resp.get("usage", {}),
    }
