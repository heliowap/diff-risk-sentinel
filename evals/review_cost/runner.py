from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from evals.review_cost.models import CaseConfig, Finding, ReviewOutput, RunResult, parse_review_output
from evals.review_cost.prepare_isolated_repo import prepare_isolated_repo

ARM_A_INSTRUCTIONS = """You are conducting an unassisted code review (ARM A) of a commit range in an isolated git repository.
Your goal is to identify concrete functional defects, regressions, contract violations, or unhandled edge cases introduced by this change.

WORKFLOW:
1. Examine the git diff between {base_commit} and {target_commit} (e.g. `git diff {base_commit}..{target_commit}`).
2. Read the touched files and relevant surrounding code using read-only filesystem commands.
3. Assess the behavior changes and identify real defects. Do not run tests or modify code.
4. Output your findings strictly conforming to the required JSON schema at the end.
"""

ARM_B_INSTRUCTIONS = """You are conducting a sentinel-assisted code review (ARM B) using the diff-risk-sentinel skill workflow.
Your goal is to identify concrete functional defects, regressions, contract violations, or unhandled edge cases introduced by this change.

WORKFLOW:
1. Run triage: `diff-risk-sentinel --base {base_commit}..{target_commit} --top 20 --jev --output /tmp/sentinel.json`
2. Generate spec worksheet: `python3 {worksheet_script} /tmp/sentinel.json --repo . --max 8 > /tmp/worksheet.md`
3. For each top production target, inspect the new revision, read its contract, and fill the spec:
   - What changed
   - Contract & Invariants
   - Suspected defects / risks
   - Given/When/Then test cases
4. Conduct the explicit second pass:
   - Neighbours: small behavior edits in the same files as the top targets.
   - Consumers: untouched callers or readers of changed contracts, formats, or payloads.
   - Deploy transition: data formats, stale cache keys, or migrations mid-way.
5. Do not run tests or modify code.
6. Output your findings strictly conforming to the required JSON schema at the end.
"""

SCHEMA_INSTRUCTIONS = """
REQUIRED OUTPUT FORMAT:
You must provide your review output containing a JSON block with the following schema:
```json
{
  "findings": [
    {
      "file": "path/to/file.py",
      "line": 123,
      "function": "ClassName.method_name",
      "claim": "Specific description of the defect, invariant violation, or bug",
      "severity": "critical"
    }
  ],
  "report_markdown": "# Risk triage: ...\\n## Findings first\\n..."
}
```
Severities must be one of: "critical", "major", "minor".
If no concrete defects are found, return `"findings": []`.
"""


def _find_worksheet_script() -> str:
    claude_skill = Path.home() / ".claude" / "skills" / "diff-risk-sentinel" / "scripts" / "spec_worksheet.py"
    if claude_skill.exists():
        return str(claude_skill)
    repo_script = Path(__file__).resolve().parent.parent.parent / "scripts" / "spec_worksheet.py"
    return str(repo_script)


def build_review_prompt(case: CaseConfig, arm: str) -> str:
    instructions = ARM_A_INSTRUCTIONS if arm.upper() == "A" else ARM_B_INSTRUCTIONS
    formatted_instructions = instructions.format(
        base_commit=case.base_commit,
        target_commit=case.intro_commit,
        worksheet_script=_find_worksheet_script(),
    )
    prompt = f"""# Code Review Task ({case.case_id}) - ARM {arm.upper()}

Commit under review: {case.intro_commit}
Base commit: {case.base_commit}
Commit message: {case.subject}

{formatted_instructions}

{SCHEMA_INSTRUCTIONS}
"""
    return prompt


def claude_command_runner(
    prompt: str,
    repo_path: Path,
    arm: str,
    model: str = "sonnet",
    timeout_seconds: int = 600,
) -> Dict[str, Any]:
    """
    Executes claude CLI non-interactively with structured JSON telemetry.
    Arm A enforces --safe-mode (no skills or customizations).
    """
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--permission-mode", "auto",
        "--output-format", "json",
    ]
    if arm.upper() == "A":
        cmd.append("--safe-mode")

    cmd.append(prompt)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=dict(os.environ),
        )
        if proc.returncode != 0 and not proc.stdout:
            return {"error": f"Process exited with {proc.returncode}: {proc.stderr}", "output": ""}

        try:
            data = json.loads(proc.stdout)
        except Exception:
            return {"error": "Non-JSON output from claude CLI", "output": proc.stdout}

        usage = data.get("usage", {})
        result = data.get("result", "")
        cost = float(data.get("total_cost_usd", 0.0))

        # Check for Jev tokens if sentinel output was generated
        sentinel_out = Path("/tmp/sentinel.json")
        jev_tokens = 0
        jev_cost = 0.0
        if arm.upper() == "B" and sentinel_out.exists():
            try:
                sdata = json.loads(sentinel_out.read_text())
                jev_tokens = int(sdata.get("meta", {}).get("jev_tokens", 0))
                jev_cost = float(sdata.get("meta", {}).get("jev_cost_usd", 0.0))
            except Exception:
                pass

        return {
            "output": result,
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
            "cost_usd": cost,
            "jev_tokens": jev_tokens,
            "jev_cost_usd": jev_cost,
            "error": data.get("api_error_status") or (proc.stderr if proc.returncode != 0 else None),
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Timeout expired after {timeout_seconds}s", "output": ""}
    except Exception as e:
        return {"error": str(e), "output": ""}


def run_case_review(
    case: CaseConfig,
    source_repo: Path | str,
    arm: str,
    repetition: int = 1,
    command_runner: Optional[Callable[[str, Path], Dict[str, Any]]] = None,
    work_base_dir: Optional[Path | str] = None,
) -> RunResult:
    """
    Executes a single review run for a case and arm in an isolated repository.
    """
    start_time = time.time()
    work_dir = tempfile.mkdtemp(prefix=f"review_{case.case_id}_{arm}_", dir=str(work_base_dir) if work_base_dir else None)
    repo_dir = Path(work_dir) / "repo"

    unreachable = [case.fix_commit] if case.fix_commit else None

    try:
        # 1. Prepare isolated repository
        prepare_isolated_repo(
            source_repo=source_repo,
            target_commit=case.intro_commit,
            dest_dir=repo_dir,
            base_commit=case.base_commit,
            unreachable_commits=unreachable,
        )

        prompt = build_review_prompt(case, arm=arm)

        if command_runner:
            exec_res = command_runner(prompt, repo_dir)
            raw_output = exec_res.get("output", "")
            input_tokens = exec_res.get("input_tokens", 0)
            output_tokens = exec_res.get("output_tokens", 0)
            cost_usd = exec_res.get("cost_usd", 0.0)
            jev_tokens = exec_res.get("jev_tokens", 0)
            jev_cost_usd = exec_res.get("jev_cost_usd", 0.0)
            err = exec_res.get("error")
        else:
            # Default to real claude CLI runner
            exec_res = claude_command_runner(prompt, repo_dir, arm=arm)
            raw_output = exec_res.get("output", "")
            input_tokens = exec_res.get("input_tokens", 0)
            output_tokens = exec_res.get("output_tokens", 0)
            cost_usd = exec_res.get("cost_usd", 0.0)
            jev_tokens = exec_res.get("jev_tokens", 0)
            jev_cost_usd = exec_res.get("jev_cost_usd", 0.0)
            err = exec_res.get("error")

        duration = time.time() - start_time
        review_out = parse_review_output(raw_output)

        return RunResult(
            case_id=case.case_id,
            arm=arm.upper(),
            repetition=repetition,
            target_commit=case.intro_commit,
            base_commit=case.base_commit,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            jev_tokens=jev_tokens,
            jev_cost_usd=jev_cost_usd,
            findings=review_out.findings,
            report_markdown=review_out.report_markdown,
            error=err,
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run evaluation reviews across arms.")
    parser.add_argument("--cases", required=True, help="JSON file containing CaseConfig items")
    parser.add_argument("--source-repo", required=True, help="Path to source git repository")
    parser.add_argument("--output-dir", required=True, help="Directory to save run results JSON")
    parser.add_argument("--arms", default="A,B", help="Comma-separated arms to run (e.g. A,B)")
    parser.add_argument("--repetitions", type=int, default=1, help="Repetitions per case")
    parser.add_argument("--model", default="sonnet", help="Claude model name (default: sonnet)")

    args = parser.parse_args()

    with open(args.cases, "r", encoding="utf-8") as f:
        cases = [CaseConfig.from_dict(c) for c in json.load(f)]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    results: List[RunResult] = []

    for case in cases:
        for arm in arms:
            for rep in range(1, args.repetitions + 1):
                print(f"Running Case: {case.case_id} | Arm: {arm} | Rep: {rep} ...", flush=True)
                res = run_case_review(
                    case=case,
                    source_repo=args.source_repo,
                    arm=arm,
                    repetition=rep,
                    command_runner=lambda p, r, _a=arm: claude_command_runner(p, r, arm=_a, model=args.model),
                )
                results.append(res)
                print(f"  Done in {res.duration_seconds:.1f}s | Tokens: {res.total_tokens} | Cost: ${res.cost_usd:.4f} | Findings: {len(res.findings)}", flush=True)

                # Save incremental results
                runs_file = out_dir / "runs.json"
                with open(runs_file, "w", encoding="utf-8") as f:
                    json.dump([r.to_dict() for r in results], f, indent=2)

    print(f"Finished {len(results)} runs. Saved to {out_dir / 'runs.json'}")


if __name__ == "__main__":
    main()

