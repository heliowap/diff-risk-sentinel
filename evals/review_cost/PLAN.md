# Evaluation Plan: Review Cost & Quality (Sentinel-Assisted vs. Full-Diff LLM)

## 1. Overview & Research Question

The central premise of `diff-risk-sentinel` is that structural risk triage (CRAP score + TypeSafe Jev semantic triage) enables faster, cheaper, and higher-focus code reviews than feeding an entire raw diff to an LLM, without sacrificing defect detection.

While the ranking capability of CRAP + Jev has been rigorously established on historical benchmarks (`evals/jev_szz_results.md`), **the core end-to-end efficiency claim has never been measured head-to-head**:
> *Does a sentinel-assisted review find at least as many real defects as an unassisted full-diff LLM review, with fewer tokens and less wall-clock time?*

Cost savings are only meaningful if defect detection quality is preserved: reducing tokens simply by overlooking real defects is a false economy. This evaluation measures cost, token usage, defect recall, and finding precision in a single paired experiment.

---

## 2. Experimental Arms

All arms use identical underlying models, effort levels, tool capabilities (read-only filesystem/git navigation), and output schemas.

| Arm | Description | Tooling / Workflow |
|---|---|---|
| **Arm A (Baseline)** | Unassisted Full-Diff Review | Standard agent with read-only git/code navigation tools. Reviews the target commit range directly without sentinel triage. |
| **Arm B (Sentinel-Assisted)** | Sentinel Workflow | Executes `diff-risk-sentinel --base <range> --top 20 --jev`, generates the specification worksheet (`spec_worksheet.py`), reviews top targets filling Given/When/Then test specs, and conducts an explicit second pass (neighbours in touched files, consumers of changed contracts, deploy transition). |
| **Arm C (Ablation, Optional)** | Sentinel Top-K Only | Reads and audits only the top-K functions prioritized by the sentinel ranking without the second pass (isolating the ranking effect from the full review workflow). |

### Output Schema

Every arm must output its review conforming to a strict JSON structure accompanied by a structured markdown report:

```json
{
  "findings": [
    {
      "file": "path/to/file.py",
      "line": 123,
      "function": "ClassName.method_name",
      "claim": "Specific description of the defect or contract violation",
      "severity": "critical | major | minor"
    }
  ],
  "report_markdown": "# Risk triage: ...\n## Findings first\n..."
}
```

---

## 3. Dataset & Stratification

Cases are sampled from the forward-SZZ dataset (`evals/private/szz_dataset.json`), linking historical bug-introducing commits to subsequent verified fix commits in a private production monorepo.

### Sampling Structure (30 Cases Total)

To assess scalability across diff sizes, cases are stratified by the number of touched production functions in the introducing revision:

- **Small diffs (< 30 functions):** 8 cases
- **Medium diffs (30–150 functions):** 8 cases
- **Large diffs (150+ functions):** 8 cases
- **Clean commits (Noise baseline):** 6 commits with no subsequent defect fixes (to measure false-alarm rate and hallucination under realistic conditions)

### Exclusions & Replications

- **Design holds:** Commits and cases used during the initial design, calibration, or exploratory evaluation of `diff-risk-sentinel` are excluded.
- **Replications:** 2 repetitions per arm per case to account for LLM generation variance.
- **Total Runs:**
  - 2 Arms (A vs. B): 30 cases × 2 arms × 2 repetitions = **120 runs**
  - If Arm C is included: 30 cases × 3 arms × 2 repetitions = **180 runs**

---

## 4. Experimental Controls

To eliminate confounding variables and information leakage, the evaluation enforces four strict controls:

1. **Strict Repository Isolation (Anti-Leakage):**
   Each run executes inside a dedicated, isolated temporary repository clone whose commit history strictly terminates at the reviewed commit. All subsequent commits, remote tracking branches, tags, and reflogs are scrubbed (`git reflog expire --expire=now --all && git gc --prune=now`).
   *Validation:* Automated tests verify that `git log --all`, `git show`, and `git rev-parse` cannot access or resolve the subsequent fix commit SHA.
2. **Fresh Contexts & Randomized Order:**
   Every trial runs in a fresh, isolated agent session with no state carrying over. The sequence of cases and arms is randomized to neutralize temporal or API caching biases.
3. **Symmetric Permissions:**
   Both arms are restricted to read-only analysis tools (reading git commits, diffs, file trees, and file contents). Neither arm is permitted to run tests, execute builds, or modify files during the review.
4. **Blind Grader:**
   The grading agent evaluates findings without knowing which arm produced them. Finding lists are anonymized, normalized, and evaluated solely against the known historical defect description and fix diff.

---

## 5. Evaluation Metrics

### Resource & Cost Efficiency
- **Input Tokens:** Total prompt tokens consumed (including prompt cache hits and misses).
- **Output Tokens:** Total completion tokens generated.
- **Cache Efficiency:** Ratio of cached prompt tokens to total tokens.
- **Financial Cost:** Total cost in USD per run based on published model pricing (including TypeSafe Jev API costs for Arm B, tracked separately).
- **Wall-Clock Duration:** Total elapsed time from invocation to completion (including sentinel CLI + Jev latency).
- **Completion Rate:** Percentage of runs completed successfully vs. aborted or exceeding context limits.

### Review Quality & Defect Detection
- **Known-Defect Recall:**
  - `Found`: The review accurately identifies the defect at the correct function/location with a correct description of the underlying problem.
  - `Near`: The review flags the correct function or file, but cites an unrelated, benign, or incorrect issue.
  - `Missed`: The known defect is not identified.
- **Finding Precision:** Each finding generated across all runs is categorized as:
  - `Correct`: Valid bug, semantic defect, unhandled boundary condition, or regression.
  - `Incorrect`: Hallucinated defect, incorrect code interpretation, or false alarm.
  - `Unverifiable`: Plausible concern that cannot be definitively validated or refuted from the repository state.
- **Tokens per Correct Finding:** Total input + output tokens divided by the number of validated correct findings.
- **False Alarm Rate:** Mean number of critical/major findings generated on clean diffs.

### Quality Assurance
- **Human Spot-Check:** A random sample of 20% of all graded findings is audited by a human reviewer to verify grader consistency and rubric adherence.
- **Dataset Dual-Use (Stage 6):** Graded findings and precision labels serve directly as the benchmark evaluation dataset for "Stage 6" (TypeSafe Jev verification of LLM review findings).

---

## 6. Pre-Registered Hypotheses

All hypotheses and evaluation criteria are fixed prior to execution:

- **Hypothesis 1 (Token Efficiency - H1):**
  Arm B (Sentinel-assisted) consumes **≥ 30% fewer total tokens** than Arm A on medium (30–150) and large (150+) diffs.
- **Hypothesis 2 (Non-Inferior Recall - H2):**
  Arm B known-defect recall is **non-inferior** to Arm A (difference in recall rate ≤ 5 percentage points, within a 95% bootstrap confidence interval).
- **Hypothesis 3 (Finding Precision - H3):**
  Arm B finding precision is **equal to or higher** than Arm A ($Precision_B \ge Precision_A$).

### Statistical Decision Rules
- Pairwise comparisons by case using cluster bootstrap confidence intervals (resampling over commit cases) within each diff-size stratum.
- **Failure Clause:** If H1 holds (fewer tokens) but H2 fails (lower recall by > 5 pp), the outcome must be explicitly reported as:
  > *"The sentinel-assisted review is cheaper solely because it reviews less code, sacrificing defect detection."*

---

## 7. Implementation & Rollout Architecture

Scripts reside in `evals/review_cost/`:

- `prepare_isolated_repo.py`: Creates an isolated repository ending at the reviewed commit, removes future history/reflogs, runs `git gc`, and asserts the fix commit is unreachable.
- `run_review_eval.py`: Orchestrates agent reviews (`claude -p --output-format json` or agent runner), configuring isolated environments and recording per-run tokens, costs, durations, and outputs.
- `grade_findings.py`: Blind grading harness matching review findings against known bug descriptions and fix diffs.
- `analyze_results.py`: Computes paired bootstrap confidence intervals, cost breakdowns, recall/precision rates, and generates public aggregate summary tables.

### Phased Execution Strategy

1. **Phase 1: Tooling & Isolation Verification (TDD)**
   - Implement `prepare_isolated_repo.py` with comprehensive unit tests (`tests/test_prepare_isolated_repo.py`) verifying that future commits, blame references, and reflogs cannot be queried.
   - Implement data structures and schemas for findings, grader prompts, and logging.
2. **Phase 2: Pilot Run (3 Cases × 2 Arms)**
   - Run 1 small, 1 medium, and 1 large diff across Arm A and Arm B (6 runs total).
   - Objectives:
     - Calibrate grading rubrics and prompt clarity.
     - Verify isolation mechanics and skill loading configuration.
     - Obtain empirical token/cost benchmarks to project the full 120-run budget.
3. **Phase 3: Execution of the Full Benchmark**
   - Execute the remaining 27 cases (114 runs) with random ordering.
   - Run the blind grading pipeline and human spot-check sample (20%).
4. **Phase 4: Synthesis & Reporting**
   - Publish aggregate statistics to `evals/review_cost_results.md`.
   - Export labeled findings dataset to `evals/private/` for the Stage 6 Jev verifier experiment.

---

## 8. Public Safety & Data Privacy Constraints

In accordance with repository guidelines (`CLAUDE.md`):
- All raw transcripts, execution traces, finding dumps, private repo paths, commit SHAs, and developer identifiers must remain in `evals/private/` or temporary storage.
- No private repository names, function names, internal identifiers, or commit hashes may be committed to public repository tracking.
- Only aggregated metrics, statistical intervals, and sanitized/generic pattern illustrations may be committed to version control.
