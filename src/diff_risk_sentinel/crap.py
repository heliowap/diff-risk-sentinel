from typing import Dict, List, Any, Optional, Tuple


def calculate_crap(ccn: int, coverage: float) -> float:
    """
    CRAP (Change Risk Anti-Patterns) Score formula:
    CRAP = comp^2 * (1 - cov)^3 + comp
    """
    return (ccn ** 2) * ((1.0 - coverage) ** 3) + ccn


def calculate_crap_trend(initial_crap: Optional[float], final_crap: float) -> float:
    """
    Calculates delta CRAP (trend) according to OtterWise methodology:
    positive delta means risk increased; negative means refactoring improvement.
    A method that did not exist before (initial_crap=None) contributes its full CRAP.
    """
    if initial_crap is None:
        return round(final_crap, 2)
    return round(final_crap - initial_crap, 2)


def classify_otterwise_risk(
    ccn: int,
    crap: float,
    delta_crap: float,
    jev_semantic_risk: Optional[float]
) -> Tuple[str, str]:
    """
    Classifies risk based on OtterWise thresholds (0-30 acceptable, 30-60 needs attention, 60+ high risk)
    and TypeSafe Jev semantic risk judgment. Rules are ordered by severity, so an improving
    delta can never fast-pass a method that is still in the yellow or red zone.
    jev_semantic_risk=None means "unknown" (Jev disabled or failed): only structural rules apply.
    """
    jev = jev_semantic_risk

    if jev is not None and jev >= 2.0:
        return (
            "CRITICAL_SEMANTIC_AUDIT",
            "Semantic risk high (Jev >= 2.0). Audit business logic, unhandled edge cases, and state mutations before merging."
        )

    if crap >= 60.0 or ccn >= 20:
        return (
            "HIGH_RISK_REFACTOR",
            "High complexity/CRAP (OtterWise 60+). Refactor: extract methods (seams), use early returns, or replace conditionals with lookup tables."
        )

    if crap >= 30.0:
        return (
            "NEEDS_ATTENTION_TESTS",
            "Needs attention (OtterWise 30-60). Add unit tests to increase coverage and pull CRAP score below 30."
        )

    if jev is not None and jev >= 1.0:
        return (
            "SEMANTIC_REVIEW",
            "Moderate semantic risk (1.0 <= Jev < 2.0). Structurally healthy, but review the behavior change before merging."
        )

    if delta_crap < 0:
        return (
            "BENEFICIAL_REFACTOR",
            "Code quality improved (delta CRAP < 0) and method is in the acceptable zone. Fast-pass approved."
        )

    return (
        "ACCEPTABLE_LOW_RISK",
        "Low risk (CRAP < 30). Safe to merge."
    )


def compute_aggregate_metrics(
    items: List[Dict[str, Any]],
    removed_items: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, float]:
    """
    Computes Combined CRAP and Average CRAP according to the OtterWise PR methodology.

    items: every method touched by the diff that exists after the change, with
        "crap" (after) and "crap_before" (None when the method is new).
    removed_items: methods that existed before and were deleted, with "crap_before".

    Deltas compare the before/after populations, so "average_delta_crap" is the change
    in average CRAP per method, not the mean of per-method deltas.
    """
    removed_items = removed_items or []

    after = [item["crap"] for item in items]
    before = [item["crap_before"] for item in items if item.get("crap_before") is not None]
    before += [item["crap_before"] for item in removed_items]

    combined_after = sum(after)
    combined_before = sum(before)
    avg_after = combined_after / len(after) if after else 0.0
    avg_before = combined_before / len(before) if before else 0.0

    return {
        "total_methods": len(items),
        "new_methods": sum(1 for item in items if item.get("crap_before") is None),
        "removed_methods": len(removed_items),
        "combined_crap": round(combined_after, 1),
        "combined_crap_before": round(combined_before, 1),
        "combined_delta_crap": round(combined_after - combined_before, 1),
        "average_crap": round(avg_after, 1),
        "average_crap_before": round(avg_before, 1),
        "average_delta_crap": round(avg_after - avg_before, 1),
    }
