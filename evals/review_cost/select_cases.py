from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Sequence

from evals.review_cost.models import CaseConfig


def stratify_and_select_cases(
    cases: Sequence[Dict[str, Any]],
    clean_commits: Optional[Sequence[Dict[str, Any]]] = None,
    n_small: int = 8,
    n_medium: int = 8,
    n_large: int = 8,
    n_clean: int = 6,
    seed: int = 42,
    excluded_intros: Optional[Set[str]] = None,
) -> List[CaseConfig]:
    """
    Stratifies historical defect cases by touched-function count and samples
    the specified number of cases per bucket, plus clean noise commits.
    """
    rng = random.Random(seed)
    excluded = set(excluded_intros or set())

    def is_excluded(intro: str) -> bool:
        return any(intro.startswith(prefix) for prefix in excluded)

    # De-duplicate by intro commit
    seen_intros: Set[str] = set()
    small_cases: List[Dict[str, Any]] = []
    medium_cases: List[Dict[str, Any]] = []
    large_cases: List[Dict[str, Any]] = []

    for c in cases:
        intro = c.get("intro", "")
        if not intro or is_excluded(intro) or intro in seen_intros:
            continue

        touched = int(c.get("touched_production_functions", 0))
        seen_intros.add(intro)

        if touched < 30:
            small_cases.append(c)
        elif touched < 150:
            medium_cases.append(c)
        else:
            large_cases.append(c)

    def _sample_bucket(pool: List[Dict[str, Any]], k: int, category: str) -> List[CaseConfig]:
        sampled = rng.sample(pool, min(k, len(pool))) if len(pool) >= k else list(pool)
        results = []
        for idx, item in enumerate(sampled, 1):
            intro = item["intro"]
            base = item.get("base") or f"{intro}~1"
            cfg = CaseConfig(
                case_id=f"{category}_{idx:02d}_{intro[:8]}",
                intro_commit=intro,
                base_commit=base,
                category=category,
                touched_production_functions=int(item.get("touched_production_functions", 0)),
                fix_commit=item.get("fix"),
                fixed_functions=item.get("fixed_functions", {}),
                intro_subject=str(item.get("intro_subject", "")),
                fix_subject=str(item.get("fix_subject", item.get("subject", ""))),
            )
            results.append(cfg)
        return results

    selected_small = _sample_bucket(small_cases, n_small, "small")
    selected_medium = _sample_bucket(medium_cases, n_medium, "medium")
    selected_large = _sample_bucket(large_cases, n_large, "large")

    selected_clean: List[CaseConfig] = []
    if clean_commits and n_clean > 0:
        clean_pool = [c for c in clean_commits if not is_excluded(str(c.get("intro", "")))]
        sampled_clean = rng.sample(clean_pool, min(n_clean, len(clean_pool))) if len(clean_pool) >= n_clean else list(clean_pool)
        for idx, item in enumerate(sampled_clean, 1):
            intro = item["intro"]
            base = item.get("base") or f"{intro}~1"
            selected_clean.append(
                CaseConfig(
                    case_id=f"clean_{idx:02d}_{intro[:8]}",
                    intro_commit=intro,
                    base_commit=base,
                    category="clean",
                    touched_production_functions=int(item.get("touched_production_functions", 0)),
                    fix_commit=None,
                    fixed_functions=[],
                    intro_subject=str(item.get("intro_subject", item.get("subject", ""))),
                    fix_subject="",
                )
            )

    return selected_small + selected_medium + selected_large + selected_clean


def main() -> None:
    parser = argparse.ArgumentParser(description="Stratify and select evaluation cases from SZZ dataset.")
    parser.add_argument("--dataset", required=True, help="Path to SZZ dataset JSON")
    parser.add_argument("--output", required=True, help="Destination JSON path for selected cases")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for deterministic sampling")
    parser.add_argument("--small", type=int, default=8, help="Number of small diff cases")
    parser.add_argument("--medium", type=int, default=8, help="Number of medium diff cases")
    parser.add_argument("--large", type=int, default=8, help="Number of large diff cases")
    parser.add_argument("--clean", type=int, default=6, help="Number of clean commits")
    parser.add_argument("--clean-commits-file", default=None, help="Optional JSON file with clean commits")
    parser.add_argument(
        "--excluded",
        default="",
        help="Comma-separated commit SHAs or prefixes to exclude (design holds)",
    )

    args = parser.parse_args()

    with open(args.dataset, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw_cases = data.get("cases", [])
    clean_data = []
    if args.clean_commits_file and Path(args.clean_commits_file).exists():
        with open(args.clean_commits_file, "r", encoding="utf-8") as f:
            clean_data = json.load(f)

    excluded_set = set(p.strip() for p in args.excluded.split(",") if p.strip())

    selected = stratify_and_select_cases(
        cases=raw_cases,
        clean_commits=clean_data,
        n_small=args.small,
        n_medium=args.medium,
        n_large=args.large,
        n_clean=args.clean,
        seed=args.seed,
        excluded_intros=excluded_set,
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([c.to_dict() for c in selected], f, indent=2)

    print(f"Selected {len(selected)} cases -> saved to {out_path}")


if __name__ == "__main__":
    main()
