from __future__ import annotations

from typing import List, Optional, Sequence

from evals.review_cost.models import CaseConfig, CaseGrading, Finding, FindingJudgment, VerdictType


def match_function_name(candidate: str, targets: Sequence[str]) -> bool:
    """
    Checks if candidate matches any target function name across qualified notations.
    e.g. 'checkout' matches 'OrderService.checkout' and 'pkg.OrderService.checkout'.
    """
    cand_parts = [p.strip() for p in candidate.replace("::", ".").split(".") if p.strip()]
    if not cand_parts:
        return False
    cand_leaf = cand_parts[-1].lower()

    for t in targets:
        t_parts = [p.strip() for p in t.replace("::", ".").split(".") if p.strip()]
        if not t_parts:
            continue
        t_leaf = t_parts[-1].lower()

        # Leaf function name match
        if cand_leaf == t_leaf:
            # If candidate specified a class, verify class match if target also has class
            if len(cand_parts) >= 2 and len(t_parts) >= 2:
                if cand_parts[-2].lower() == t_parts[-2].lower():
                    return True
            else:
                return True

        # Substring / full qualification match
        norm_cand = ".".join(cand_parts).lower()
        norm_t = ".".join(t_parts).lower()
        if norm_cand == norm_t or norm_cand.endswith(f".{norm_t}") or norm_t.endswith(f".{norm_cand}"):
            return True

    return False


def match_file_path(candidate: str, targets: Sequence[str]) -> bool:
    """Checks if candidate path matches any target path."""
    cand_norm = candidate.strip().lstrip("/").replace("\\", "/")
    for t in targets:
        t_norm = t.strip().lstrip("/").replace("\\", "/")
        if cand_norm == t_norm or cand_norm.endswith(t_norm) or t_norm.endswith(cand_norm):
            return True
    return False


def match_finding_to_targets(
    finding_file: str,
    finding_fn: str,
    fixed_targets: Any,
) -> Tuple[bool, bool]:
    """
    Evaluates whether a finding matches the ground-truth targets.
    Returns (file_matched, func_matched).
    """
    if not fixed_targets:
        return False, False

    if isinstance(fixed_targets, dict):
        file_matched = False
        func_matched = False

        # 1. Match against files in dictionary
        for target_file, target_fns in fixed_targets.items():
            if match_file_path(finding_file, [target_file]):
                file_matched = True
                if not target_fns or match_function_name(finding_fn, target_fns):
                    func_matched = True
                    break

        # 2. If not matched per-file, check function name across all files
        if not func_matched:
            all_target_fns = [fn for fns in fixed_targets.values() for fn in fns]
            if match_function_name(finding_fn, all_target_fns):
                func_matched = True

        return file_matched, func_matched

    if isinstance(fixed_targets, (list, set, tuple)):
        # List could contain file paths or function names
        file_matched = match_file_path(finding_file, [str(t) for t in fixed_targets])
        func_matched = match_function_name(finding_fn, [str(t) for t in fixed_targets])
        return file_matched, func_matched

    return False, False


def grade_case_findings(
    case: CaseConfig,
    findings: Sequence[Finding],
    fix_diff: str = "",
    fix_message: str = "",
    arm: str = "A",
    repetition: int = 1,
    semantic_judgments: Optional[Sequence[FindingJudgment]] = None,
) -> CaseGrading:
    """
    Grades a list of findings against ground truth (the subsequent fix commit).

    With `semantic_judgments` (from blind semantic grading), verdicts are copied
    verbatim and `known_defect_verdict` is "found" only when a "correct" judgment
    has `matches_known_defect=True`. Without them, grading is a location proxy:
    findings are never marked "correct", matches against fixed files/functions
    yield "near", and per-finding verdicts stay "unverifiable".
    """
    fixed_targets = case.fixed_functions
    is_clean = case.category == "clean" or not fixed_targets

    if semantic_judgments is not None:
        for j in semantic_judgments:
            if not 0 <= j.finding_index < len(findings):
                raise ValueError(
                    f"semantic judgment index {j.finding_index} out of range for {len(findings)} findings"
                )
        verdict: VerdictType = "missed"
        if not is_clean:
            if any(j.verdict == "correct" and j.matches_known_defect for j in semantic_judgments):
                verdict = "found"
            elif any(
                match_finding_to_targets(f.file, f.function, fixed_targets) != (False, False)
                for f in findings
            ):
                verdict = "near"
        return CaseGrading(
            case_id=case.case_id,
            arm=arm,
            repetition=repetition,
            known_defect_verdict=verdict,
            finding_judgments=list(semantic_judgments),
            grading_method="semantic",
        )

    judgments: List[FindingJudgment] = []
    matched_any_target = False

    if is_clean:
        for idx, f in enumerate(findings):
            judgments.append(
                FindingJudgment(
                    finding_index=idx,
                    verdict="unverifiable",
                    rationale="Location proxy cannot establish correctness on a clean commit.",
                )
            )
    else:
        for idx, f in enumerate(findings):
            file_matched, func_matched = match_finding_to_targets(f.file, f.function, fixed_targets)
            if func_matched:
                matched_any_target = True
                rationale = f"Matches fixed function {f.function}; correctness requires semantic judgment."
            elif file_matched:
                matched_any_target = True
                rationale = f"Matches fixed file {f.file} but different function."
            else:
                rationale = "Finding on function outside ground-truth fix commit."
            judgments.append(
                FindingJudgment(
                    finding_index=idx,
                    verdict="unverifiable",
                    rationale=rationale,
                )
            )

    return CaseGrading(
        case_id=case.case_id,
        arm=arm,
        repetition=repetition,
        known_defect_verdict="near" if (matched_any_target and not is_clean) else "missed",
        finding_judgments=judgments,
        grading_method="location_proxy",
    )


def build_grader_prompt(
    finding: Finding,
    case_summary: str,
    fix_diff: str,
    fix_message: str,
) -> str:
    """
    Constructs the prompt for a blind grading LLM or human reviewer.
    The finding contains no metadata about which arm (A or B) generated it.
    """
    return f"""You are a blind reviewer grading a static code review finding against the ground truth.
A subsequent fix commit was pushed to fix a merged defect introduced in this change.

GROUND TRUTH FIX COMMIT MESSAGE:
{fix_message}

GROUND TRUTH FIX DIFF:
{fix_diff}

ANONYMIZED REVIEW FINDING:
File: {finding.file}:{finding.line}
Function: {finding.function}
Severity: {finding.severity}
Claim: {finding.claim}

Answer strictly as a JSON object:
```json
{
  "verdict": "correct | incorrect | unverifiable",
  "matches_known_defect": true,
  "claim_group_id": "short stable identifier for the underlying defect",
  "rationale": "brief justification"
}
```
Rules:
- "correct" requires the claim to describe a genuine defect in the cited code; a matching file or function name alone is not enough.
- A novel, real defect that is NOT the one fixed by the ground-truth commit is still "correct" with "matches_known_defect": false.
- Claims describing the same underlying defect share the same non-empty "claim_group_id"; unrelated claims get distinct ids.
- Use "unverifiable" when the claim is speculative, stylistic, or cannot be confirmed from the evidence.
"""


def main() -> None:
    import argparse
    import json
    import subprocess
    from pathlib import Path

    from evals.review_cost.models import RunResult

    parser = argparse.ArgumentParser(description="Grade evaluation review findings against ground truth.")
    parser.add_argument("--runs", required=True, help="JSON file containing RunResult records")
    parser.add_argument("--cases", required=True, help="JSON file containing CaseConfig records")
    parser.add_argument("--source-repo", required=True, help="Path to source git repository (to inspect fix commits)")
    parser.add_argument("--output", required=True, help="Destination JSON file for CaseGrading records")

    args = parser.parse_args()

    with open(args.runs, "r", encoding="utf-8") as f:
        runs = [RunResult.from_dict(r) for r in json.load(f)]

    with open(args.cases, "r", encoding="utf-8") as f:
        cases_list = [CaseConfig.from_dict(c) for c in json.load(f)]

    cases_by_id = {c.case_id: c for c in cases_list}
    gradings: List[CaseGrading] = []

    for r in runs:
        case = cases_by_id.get(r.case_id)
        if not case:
            continue

        fix_diff = ""
        fix_msg = ""
        if case.fix_commit:
            msg_res = subprocess.run(
                ["git", "-C", args.source_repo, "log", "-1", "--format=%B", case.fix_commit],
                capture_output=True,
                text=True,
            )
            fix_msg = msg_res.stdout.strip() if msg_res.returncode == 0 else ""

            diff_res = subprocess.run(
                ["git", "-C", args.source_repo, "diff", f"{case.fix_commit}~1..{case.fix_commit}"],
                capture_output=True,
                text=True,
            )
            fix_diff = diff_res.stdout if diff_res.returncode == 0 else ""

        g = grade_case_findings(
            case=case,
            findings=r.findings,
            fix_diff=fix_diff,
            fix_message=fix_msg,
            arm=r.arm,
            repetition=r.repetition,
        )
        gradings.append(g)

    out_p = Path(args.output)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump([g.to_dict() for g in gradings], f, indent=2)

    print(f"Graded {len(gradings)} runs -> saved to {out_p}")


if __name__ == "__main__":
    main()

