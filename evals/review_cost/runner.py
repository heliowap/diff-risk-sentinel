from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
import concurrent.futures
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

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
1. Read the precomputed Sentinel triage report at {sentinel_json} and the spec worksheet (produced by spec_worksheet.py) at {worksheet_md}.
2. For each top production target, inspect the new revision, read its contract, and fill the spec:
   - What changed
   - Contract & Invariants
   - Suspected defects / risks
   - Given/When/Then test cases
3. Conduct the explicit second pass:
   - Neighbours: small behavior edits in the same files as the top targets.
   - Consumers: untouched callers or readers of changed contracts, formats, or payloads.
   - Deploy transition: data formats, stale cache keys, or migrations mid-way.
4. The Sentinel report meta block contains a `treatment_id` and the worksheet ends with a `Treatment-ID:` line. Copy that marker verbatim into the `treatment_id` field of your output JSON.
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
  "report_markdown": "# Risk triage: ...\\n## Findings first\\n...",
  "treatment_id": "optional; ARM B must copy the Treatment-ID marker from the sentinel artifacts"
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


@dataclass(frozen=True)
class SentinelArtifacts:
    sentinel_path: Path
    worksheet_path: Path
    treatment_id: str
    jev_tokens: int = 0
    jev_cost_usd: float = 0.0


def build_sentinel_artifacts(
    case: CaseConfig,
    repo_path: Path,
    sentinel_path: Path,
    worksheet_path: Path,
) -> SentinelArtifacts:
    """
    Precomputes the Sentinel triage report and spec worksheet inside the
    isolated repo, embeds a random treatment marker in both artifacts, and
    returns real Jev usage read from the Sentinel metadata.
    """
    triage = subprocess.run(
        [
            "diff-risk-sentinel",
            "--base", f"{case.base_commit}..{case.intro_commit}",
            "--top", "20",
            "--jev",
            "--output", str(sentinel_path),
        ],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if triage.returncode != 0:
        raise RuntimeError(f"sentinel triage failed: {triage.stderr.strip()}")
    if not sentinel_path.exists():
        raise RuntimeError(f"sentinel output missing: {sentinel_path}")

    sentinel_data = json.loads(sentinel_path.read_text())
    meta = sentinel_data.setdefault("meta", {})
    jev_tokens = int(meta.get("jev_tokens", 0))
    jev_cost_usd = float(meta.get("jev_cost_usd", 0.0))
    treatment_id = secrets.token_hex(8)
    meta["treatment_id"] = treatment_id
    sentinel_path.write_text(json.dumps(sentinel_data, indent=2))

    with open(worksheet_path, "w", encoding="utf-8") as out:
        worksheet = subprocess.run(
            ["python3", _find_worksheet_script(), str(sentinel_path), "--repo", ".", "--max", "8"],
            cwd=str(repo_path),
            stdout=out,
            stderr=subprocess.PIPE,
            text=True,
        )
    if worksheet.returncode != 0:
        raise RuntimeError(f"worksheet generation failed: {worksheet.stderr.strip()}")
    if not worksheet_path.exists():
        raise RuntimeError(f"worksheet output missing: {worksheet_path}")
    with open(worksheet_path, "a", encoding="utf-8") as out:
        out.write(f"\nTreatment-ID: {treatment_id}\n")

    return SentinelArtifacts(
        sentinel_path=sentinel_path,
        worksheet_path=worksheet_path,
        treatment_id=treatment_id,
        jev_tokens=jev_tokens,
        jev_cost_usd=jev_cost_usd,
    )


def build_review_prompt(
    case: CaseConfig,
    arm: str,
    sentinel_out_path: str = "/tmp/sentinel.json",
    worksheet_path: str = "/tmp/worksheet.md",
) -> str:
    instructions = ARM_A_INSTRUCTIONS if arm.upper() == "A" else ARM_B_INSTRUCTIONS
    formatted_instructions = instructions.format(
        base_commit=case.base_commit,
        target_commit=case.intro_commit,
        worksheet_script=_find_worksheet_script(),
        sentinel_json=sentinel_out_path,
        worksheet_md=worksheet_path,
    )
    subject_line = f"Introducing commit subject: {case.intro_subject}\n" if case.intro_subject else ""
    prompt = f"""# Code Review Task ({case.case_id}) - ARM {arm.upper()}

Commit under review: {case.intro_commit}
Base commit: {case.base_commit}
{subject_line}
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
    sentinel_out_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Executes claude CLI non-interactively with structured JSON telemetry.
    Both arms run with --safe-mode (no skills or customizations).
    """
    cmd = [
        "claude",
        "--model", model,
        "-p",
        "--permission-mode", "auto",
        "--output-format", "json",
        "--safe-mode",
        prompt,
    ]

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
        sentinel_out = sentinel_out_path or Path("/tmp/sentinel.json")
        jev_tokens = 0
        jev_cost = 0.0
        if arm.upper() == "B":
            target_f = sentinel_out if (sentinel_out and sentinel_out.exists()) else Path("/tmp/sentinel.json")
            if target_f.exists():
                try:
                    sdata = json.loads(target_f.read_text())
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
    artifact_builder: Optional[
        Callable[[CaseConfig, Path, Path, Path], SentinelArtifacts]
    ] = None,
    work_base_dir: Optional[Path | str] = None,
    model: str = "sonnet",
) -> RunResult:
    """
    Executes a single review run for a case and arm in an isolated repository.
    Arm B precomputes Sentinel artifacts and requires the review output to echo
    the embedded treatment marker; a missing or mismatched marker aborts the run.
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

        sentinel_file = repo_dir / "sentinel.json"
        worksheet_file = repo_dir / "worksheet.md"

        artifacts: Optional[SentinelArtifacts] = None
        if arm.upper() == "B":
            builder = artifact_builder or build_sentinel_artifacts
            try:
                artifacts = builder(case, repo_dir, sentinel_file, worksheet_file)
            except Exception as e:
                return RunResult(
                    case_id=case.case_id,
                    arm=arm.upper(),
                    repetition=repetition,
                    target_commit=case.intro_commit,
                    base_commit=case.base_commit,
                    duration_seconds=time.time() - start_time,
                    error=f"sentinel artifact build failed: {e}",
                    aborted=True,
                )

        prompt = build_review_prompt(
            case,
            arm=arm,
            sentinel_out_path=str(sentinel_file),
            worksheet_path=str(worksheet_file),
        )

        if command_runner:
            exec_res = command_runner(prompt, repo_dir)
        else:
            # Default to real claude CLI runner with isolated sentinel path
            exec_res = claude_command_runner(
                prompt,
                repo_dir,
                arm=arm,
                model=model,
                sentinel_out_path=sentinel_file,
            )

        duration = time.time() - start_time
        review_out = parse_review_output(exec_res.get("output", ""))

        err = exec_res.get("error")
        aborted = False
        if artifacts is not None and review_out.treatment_id != artifacts.treatment_id:
            aborted = True
            err = err or (
                "treatment attestation failed: review output marker "
                f"{review_out.treatment_id!r} does not match artifact marker"
            )

        if artifacts is not None:
            jev_tokens = artifacts.jev_tokens
            jev_cost_usd = artifacts.jev_cost_usd
        else:
            jev_tokens = exec_res.get("jev_tokens", 0)
            jev_cost_usd = exec_res.get("jev_cost_usd", 0.0)

        return RunResult(
            case_id=case.case_id,
            arm=arm.upper(),
            repetition=repetition,
            target_commit=case.intro_commit,
            base_commit=case.base_commit,
            duration_seconds=duration,
            input_tokens=exec_res.get("input_tokens", 0),
            output_tokens=exec_res.get("output_tokens", 0),
            cache_read_tokens=exec_res.get("cache_read_tokens", 0),
            cache_creation_tokens=exec_res.get("cache_creation_tokens", 0),
            cost_usd=exec_res.get("cost_usd", 0.0),
            jev_tokens=jev_tokens,
            jev_cost_usd=jev_cost_usd,
            findings=review_out.findings,
            report_markdown=review_out.report_markdown,
            treatment_id=review_out.treatment_id,
            error=err,
            aborted=aborted,
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
    parser.add_argument("--workers", type=int, default=4, help="Number of concurrent workers (default: 4)")
    parser.add_argument("--force", action="store_true", help="Re-run existing cases")

    args = parser.parse_args()

    with open(args.cases, "r", encoding="utf-8") as f:
        cases = [CaseConfig.from_dict(c) for c in json.load(f)]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    runs_file = out_dir / "runs.json"
    results: List[RunResult] = []
    completed_keys: Set[Tuple[str, str, int]] = set()

    if runs_file.exists() and not args.force:
        try:
            with open(runs_file, "r", encoding="utf-8") as f:
                prev_runs = [RunResult.from_dict(r) for r in json.load(f)]
                results.extend(prev_runs)
                completed_keys = {(r.case_id, r.arm, r.repetition) for r in prev_runs}
                print(f"Loaded {len(results)} existing runs from {runs_file}")
        except Exception as e:
            print(f"Warning loading existing runs: {e}")

    tasks: List[Tuple[CaseConfig, str, int]] = []
    for case in cases:
        for arm in arms:
            for rep in range(1, args.repetitions + 1):
                if (case.case_id, arm, rep) in completed_keys:
                    print(f"Skipping completed: Case {case.case_id} | Arm {arm} | Rep {rep}")
                    continue
                tasks.append((case, arm, rep))

    lock = threading.Lock()
    total_tasks = len(tasks)
    finished_count = len(completed_keys)

    def _execute(task_info: Tuple[CaseConfig, str, int]) -> RunResult:
        c, a, r = task_info
        return run_case_review(
            case=c,
            source_repo=args.source_repo,
            arm=a,
            repetition=r,
            model=args.model,
        )

    if tasks:
        print(f"Executing {total_tasks} runs with {args.workers} workers...", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_task = {executor.submit(_execute, t): t for t in tasks}
            for future in concurrent.futures.as_completed(future_to_task):
                task_info = future_to_task[future]
                try:
                    res = future.result()
                    with lock:
                        finished_count += 1
                        results.append(res)
                        print(
                            f"[{finished_count}/{total_tasks + len(completed_keys)}] Done Case: {res.case_id} | "
                            f"Arm: {res.arm} | Rep: {res.repetition} | "
                            f"Time: {res.duration_seconds:.1f}s | Tokens: {res.total_tokens} | "
                            f"Cost: ${res.cost_usd:.4f} | Findings: {len(res.findings)}",
                            flush=True,
                        )
                        tmp_runs = out_dir / "runs.json.tmp"
                        with open(tmp_runs, "w", encoding="utf-8") as f:
                            json.dump([r.to_dict() for r in results], f, indent=2)
                        tmp_runs.replace(runs_file)
                except Exception as exc:
                    print(f"Error executing {task_info[0].case_id} Arm {task_info[1]}: {exc}", flush=True)

    print(f"Finished. Total runs: {len(results)}. Saved to {runs_file}")


if __name__ == "__main__":
    main()

