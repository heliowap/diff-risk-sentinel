# Review-Cost Harness Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Correct the existing review-cost benchmark harness so future development and held-out runs have no future-fix leakage, symmetric treatments, complete telemetry, explicit unknown verifier outcomes, relevant consumer evidence, semantic grading seams, and paired case-level analysis.

**Architecture:** Keep the existing standard-library-only modules and make their public interfaces trustworthy. Dataset provenance is represented explicitly in `CaseConfig`; review execution produces an attested `RunResult`; Jev adapters return decisions plus real usage; post-processing operates per run; grading distinguishes a location proxy from semantic judgments; analysis aggregates paired outcomes by introducing commit. The Via 1 test synthesizer and sandbox are deliberately excluded from this correction plan.

**Tech Stack:** Python 3.9+, standard library, `unittest`/`pytest`, git CLI, Claude CLI, TypeSafe Jev.

**Spec:** `docs/proposals/counterfactual_test_reproducer.md`

## Global Constraints

- Preserve the project's zero-runtime-dependency default.
- Do not alter CRAP, complexity extraction, ranking formulas, or dead-code behavior.
- Do not expose private repository identifiers, paths, SHAs, commit titles, or code in tracked files or test fixtures.
- Keep raw evaluation artifacts under `evals/private/`; use only synthetic data in tracked tests.
- Review agents in baseline and Sentinel arms must run with identical permissions and safe mode.
- Future-fix SHA, message, diff, issue metadata, and descendants must never reach review prompts or hypothesis generation.
- Verifier errors and timeouts are `UNKNOWN`; they never reject a finding automatically.
- Record actual usage returned by Jev; never synthesize token counts or costs.
- Do not commit or push. Leave all changes for user review.

## Proposed Test Seams

The TDD cycles exercise these public seams after user confirmation:

1. **Dataset seam:** `CaseConfig.from_dict()`, `CaseConfig.to_dict()`, `stratify_and_select_cases()`, and `build_review_prompt()` preserve provenance and prevent fix leakage.
2. **Review-execution seam:** `run_case_review()` accepts command/artifact adapters and returns an attested `RunResult` with complete Claude and Jev telemetry.
3. **Verifier seam:** `verify_finding_with_jev()` and `verify_consumer_with_jev()` expose `VALID`/`FALSE_ALARM`/`UNKNOWN` or `PROBABLE_CONTRACT_BREAK`/`COMPATIBLE`/`UNKNOWN` without hiding usage.
4. **Post-processing seam:** `evaluate_stage6_on_runs()` applies validators to frozen findings with per-run accounting and fail-open unknowns.
5. **Evidence seam:** consumer-evaluation helpers return the relevant changed hunk and exact consumer-function body while excluding touched files.
6. **Grading seam:** `grade_case_findings()` reports location-only evidence as a proxy and accepts explicit semantic judgments for quality metrics.
7. **Analysis seam:** `compute_stratum_metrics()` and paired-bootstrap helpers aggregate by case, not by finding or repetition.

## Review Focus

- Legacy SZZ JSON containing only `subject` must treat it as a fix subject when `fix_commit` exists and must never leak it into a review prompt; Task 1 tests this.
- A Sentinel review whose artifact marker is absent from the output must be marked invalid even when the agent returns plausible findings; Task 2 tests this.
- Missing or malformed Jev usage must remain observable without fabricated zeros or estimated costs, while an API error remains `UNKNOWN`; Tasks 3 and 4 test this.
- Multiple repetitions and multiple findings from one introducing commit must contribute one paired case-level observation; Task 7 tests this.
- Consumer evidence must remain correct for functions below the first 5,000 file characters and hunks after the first 5,000 diff characters; Task 5 tests this.

---

### Task 1: Separate Introducing and Fix Provenance

**Files:**
- Modify: `evals/build_szz_dataset.py:61-100`
- Modify: `evals/review_cost/models.py:55-71`
- Modify: `evals/review_cost/select_cases.py:12-93`
- Modify: `evals/review_cost/runner.py:77-101`
- Modify: `tests/test_review_cost_models.py`
- Modify: `tests/test_select_cases.py`
- Modify: `tests/test_runner.py`

**Interfaces:**
- Consumes: legacy or new case JSON.
- Produces: `CaseConfig.intro_subject`, `CaseConfig.fix_subject`, safe serialization, prefix-aware exclusions, and prompts containing only `intro_subject`.

- [x] **Step 1: Add failing legacy-provenance tests**

Add to `tests/test_review_cost_models.py`:

```python
def test_legacy_fix_subject_is_not_treated_as_intro_subject(self):
    case = CaseConfig.from_dict({
        "case_id": "small_01",
        "intro_commit": "a" * 40,
        "base_commit": "a" * 40 + "~1",
        "category": "small",
        "touched_production_functions": 3,
        "fix_commit": "b" * 40,
        "fixed_functions": {"src/example.py": ["calculate"]},
        "subject": "fix: correct calculation",
    })
    self.assertEqual(case.intro_subject, "")
    self.assertEqual(case.fix_subject, "fix: correct calculation")
    self.assertNotIn("subject", case.to_dict())
```

Add a new runner test:

```python
def test_review_prompt_never_contains_fix_subject(self):
    case = CaseConfig(
        case_id="small_01",
        intro_commit="a" * 40,
        base_commit="a" * 40 + "~1",
        category="small",
        touched_production_functions=3,
        fix_commit="b" * 40,
        intro_subject="feat: add calculation",
        fix_subject="fix: correct calculation",
    )
    prompt = build_review_prompt(case, arm="A")
    self.assertIn("feat: add calculation", prompt)
    self.assertNotIn("fix: correct calculation", prompt)
```

- [x] **Step 2: Run the two tests and observe RED**

Run:

```bash
python3 -m pytest tests/test_review_cost_models.py::TestReviewCostModels::test_legacy_fix_subject_is_not_treated_as_intro_subject tests/test_runner.py::TestRunner::test_review_prompt_never_contains_fix_subject -q
```

Expected: failures because `CaseConfig` has only `subject` and the prompt includes it.

- [x] **Step 3: Implement explicit provenance**

Change `CaseConfig` to:

```python
@dataclass
class CaseConfig:
    case_id: str
    intro_commit: str
    base_commit: str
    category: str
    touched_production_functions: int
    fix_commit: Optional[str] = None
    fixed_functions: Any = field(default_factory=dict)
    intro_subject: str = ""
    fix_subject: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CaseConfig":
        values = dict(data)
        legacy_subject = str(values.pop("subject", ""))
        if legacy_subject:
            key = "fix_subject" if values.get("fix_commit") else "intro_subject"
            values.setdefault(key, legacy_subject)
        values.setdefault("intro_subject", "")
        values.setdefault("fix_subject", "")
        return cls(**values)
```

In `build_szz_dataset.analyze_fix()`, read the introducing subject after blame selects `intro` and return both fields:

```python
intro_subject = run_git(["show", "-s", "--format=%s", intro], repo).strip()
return {
    "fix": fix,
    "fix_subject": subject,
    "intro": intro,
    "intro_subject": intro_subject,
    "fixed_functions": fixed,
}
```

In `select_cases.py`, pass `intro_subject` and `fix_subject`; never copy legacy `subject` into `intro_subject` when a fix exists.

In `build_review_prompt()`, render this line only when non-empty:

```python
subject_line = f"Introducing commit subject: {case.intro_subject}\n" if case.intro_subject else ""
```

- [x] **Step 4: Add and run the prefix-exclusion RED/GREEN cycle**

Add to `tests/test_select_cases.py`:

```python
def test_excluded_intro_prefix_removes_case(self):
    cases = [{
        "intro": "abcdef1234567890",
        "fix": "fedcba0987654321",
        "intro_subject": "feat: add behavior",
        "fix_subject": "fix: correct behavior",
        "fixed_functions": {"src/example.py": ["calculate"]},
        "touched_production_functions": 3,
    }]
    selected = stratify_and_select_cases(
        cases,
        n_small=1,
        n_medium=0,
        n_large=0,
        n_clean=0,
        excluded_intros={"abcdef12"},
    )
    self.assertEqual(selected, [])
```

Implement:

```python
def is_excluded(intro: str) -> bool:
    return any(intro.startswith(prefix) for prefix in excluded)
```

Use it for positive and clean pools.

- [x] **Step 5: Run the focused task tests**

```bash
python3 -m pytest tests/test_review_cost_models.py tests/test_select_cases.py tests/test_runner.py -q
```

Expected: PASS.

---

### Task 2: Make Review Treatment Symmetric and Attested

**Files:**
- Modify: `evals/review_cost/models.py`
- Modify: `evals/review_cost/runner.py`
- Modify: `tests/test_review_cost_models.py`
- Modify: `tests/test_runner.py`

**Interfaces:**
- Consumes: the public `run_case_review` interface with injected `command_runner` and `artifact_builder` callables.
- Produces: `ReviewOutput.treatment_id`, `RunResult.treatment_id`, `RunResult.aborted`, complete token fields, and a verified Sentinel treatment for arm B.

- [x] **Step 1: Write failing token-accounting tests**

Update `test_run_result_serialization` to assert:

```python
self.assertEqual(d["total_tokens"], 1500 + 350 + 500 + 100 + 4000)
```

Extend the mock runner result in `test_run_case_review_end_to_end`:

```python
"cache_read_tokens": 500,
"cache_creation_tokens": 100,
```

Assert those values survive `run_case_review()`.

- [x] **Step 2: Run focused tests and observe RED**

```bash
python3 -m pytest tests/test_review_cost_models.py::TestReviewCostModels::test_run_result_serialization tests/test_runner.py::TestRunner::test_run_case_review_end_to_end -q
```

Expected: `total_tokens` excludes cache/Jev and `run_case_review()` drops cache fields.

- [x] **Step 3: Implement complete token accounting**

Change `RunResult.total_tokens` to:

```python
return (
    self.input_tokens
    + self.output_tokens
    + self.cache_read_tokens
    + self.cache_creation_tokens
    + self.jev_tokens
)
```

Read and pass cache fields in both command-runner branches of `run_case_review()`.

- [x] **Step 4: Write failing safe-mode and treatment-attestation tests**

Add a `subprocess.run` patch test asserting `--safe-mode` appears for both arms.

Extend `ReviewOutput` parsing tests with:

```python
raw = '{"findings": [], "report_markdown": "ok", "treatment_id": "marker-1"}'
out = parse_review_output(raw)
self.assertEqual(out.treatment_id, "marker-1")
```

Add two arm-B tests using this adapter interface:

```python
def artifact_builder(case, repo_path, sentinel_path, worksheet_path):
    sentinel_path.write_text('{"meta": {"treatment_id": "marker-1"}, "targets": []}')
    worksheet_path.write_text("Treatment-ID: marker-1\n")
    return SentinelArtifacts(
        sentinel_path=sentinel_path,
        worksheet_path=worksheet_path,
        treatment_id="marker-1",
        jev_tokens=321,
        jev_cost_usd=0.001,
    )
```

One command adapter returns `treatment_id="marker-1"` and must succeed; another returns no marker and must produce `aborted=True` with a treatment error.

- [x] **Step 5: Implement treatment artifacts and attestation**

Add to `runner.py`:

```python
@dataclass(frozen=True)
class SentinelArtifacts:
    sentinel_path: Path
    worksheet_path: Path
    treatment_id: str
    jev_tokens: int = 0
    jev_cost_usd: float = 0.0
```

Define:

```python
def build_sentinel_artifacts(
    case: CaseConfig,
    repo_path: Path,
    sentinel_path: Path,
    worksheet_path: Path,
) -> SentinelArtifacts:
```

The implementation runs the Sentinel CLI and worksheet script before Claude, checks both return codes, checks both files, adds a random `treatment_id` to Sentinel `meta`, appends the marker to the worksheet, and returns usage from Sentinel metadata. Do not place the marker value in the review prompt; instruct arm B to copy it from the artifact into the output JSON.

Extend `ReviewOutput` and `RunResult` with `treatment_id: Optional[str] = None`. Extend the output schema with an optional `treatment_id` field. In `run_case_review()`, arm B calls the artifact adapter before building the prompt and marks the result aborted when the returned marker does not match the review output. Arm A never builds artifacts.

Always append `--safe-mode` in `claude_command_runner()`.

- [x] **Step 6: Run focused task tests**

```bash
python3 -m pytest tests/test_review_cost_models.py tests/test_runner.py -q
```

Expected: PASS.

---

### Task 3: Expose Real Jev Usage and Unknown Decisions

**Files:**
- Modify: `src/diff_risk_sentinel/jev.py`
- Modify: `src/diff_risk_sentinel/cli.py`
- Modify: `src/diff_risk_sentinel/finding_verifier.py`
- Modify: `src/diff_risk_sentinel/consumer_verifier.py`
- Modify: `tests/test_jev.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_finding_verifier.py`
- Modify: `tests/test_consumer_verifier.py`

**Interfaces:**
- Consumes: a TypeSafe response containing `answers` and optional numeric `usage` fields, or an error response containing `error` and optional `usage`.
- Produces: verifier results with tri-state `is_valid` or `is_broken`, `verdict`, `usage`, and explicit `UNKNOWN`; Sentinel JSON `meta.jev_usage`, `meta.jev_tokens`, and `meta.jev_cost_usd` when reported.

- [x] **Step 1: Write failing verifier-error tests**

Add to both verifier test files:

```python
@patch("diff_risk_sentinel.finding_verifier.ask_jev")
def test_error_is_unknown_and_preserves_usage(self, mock_ask):
    mock_ask.return_value = {"error": "timeout", "usage": {"input_tokens": 12}}
    finding = Finding("src/example.py", 10, "calculate", "wrong result", "major")
    result = verify_finding_with_jev("fake-key", finding, "def calculate(): pass")
    self.assertIsNone(result["is_valid"])
    self.assertEqual(result["verdict"], "UNKNOWN")
    self.assertEqual(result["usage"], {"input_tokens": 12})
```

Use the equivalent `is_broken is None` assertion for consumers.

- [x] **Step 2: Run error tests and observe RED**

```bash
python3 -m pytest tests/test_finding_verifier.py tests/test_consumer_verifier.py -q
```

Expected: current errors return `False` and omit usage.

- [x] **Step 3: Implement tri-state verifier results**

Return on error:

```python
{
    "is_valid": None,
    "support_probability": None,
    "calibrated_severity": "unknown",
    "verdict": "UNKNOWN",
    "usage": resp.get("usage", {}),
    "error": resp["error"],
}
```

Use the corresponding consumer fields. Include `usage` in successful results as well.

- [x] **Step 4: Write failing Jev usage propagation tests**

Add a `query_jev_function()` test where `ask_jev` returns numeric answers plus:

```python
"usage": {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.004}
```

Assert the result retains the usage. Add a CLI test with two mocked answers and assert output metadata contains totals 200, 40, and 0.008.

- [x] **Step 5: Implement generic numeric usage aggregation**

In `query_jev_function()`, copy `data.get("usage", {})` into the result.

Change `_score_with_jev()` to return:

```python
{
    "failures": len(failures),
    "usage": aggregate_usage,
}
```

Sum only finite numeric values with matching keys. In `run_sentinel()`, write:

```python
meta["jev_failures"] = stats["failures"]
meta["jev_usage"] = stats["usage"]
meta["jev_tokens"] = int(stats["usage"].get("input_tokens", 0) + stats["usage"].get("output_tokens", 0))
meta["jev_cost_usd"] = float(stats["usage"].get("cost_usd", 0.0))
```

Do not estimate missing fields.

- [x] **Step 6: Run focused tests**

```bash
python3 -m pytest tests/test_jev.py tests/test_cli.py tests/test_finding_verifier.py tests/test_consumer_verifier.py -q
```

Expected: PASS.

---

### Task 4: Correct Stage 6 Filtering and Per-Run Telemetry

**Files:**
- Modify: `evals/review_cost/eval_new_architecture.py`
- Create: `tests/test_new_architecture.py`

**Interfaces:**
- Consumes: `evaluate_stage6_on_runs(runs, cases, repo_path, api_key, verifier=verify_finding_with_jev)` with frozen `RunResult` records and an injectable verifier adapter.
- Produces: filtered runs where valid findings are retained, false alarms are removed, unknown findings are retained and counted, and each run contains only its own Jev time/usage/cost.

- [x] **Step 1: Write the failing per-run accounting test**

Create two synthetic runs with one finding each. Patch `verify_finding_with_jev` so the first returns usage `{input_tokens: 10, output_tokens: 2, cost_usd: 0.001}` and the second `{input_tokens: 20, output_tokens: 3, cost_usd: 0.002}`. Assert:

```python
self.assertEqual(filtered_runs[0].jev_tokens, 12)
self.assertEqual(filtered_runs[1].jev_tokens, 23)
self.assertEqual(filtered_runs[0].jev_cost_usd, 0.001)
self.assertEqual(filtered_runs[1].jev_cost_usd, 0.002)
```

Do not accept cumulative 35 tokens on the second run.

- [x] **Step 2: Write the failing unknown-retention test**

Make the verifier return `verdict="UNKNOWN"`, `is_valid=None`, and an error. Assert the original finding remains in `runs_after`, `unknown_findings == 1`, and `pruned_findings == 0`.

- [x] **Step 3: Run the new file and observe RED**

```bash
python3 -m pytest tests/test_new_architecture.py -q
```

Expected: current code fabricates 430 tokens per finding, accumulates across runs, and drops unknowns.

- [x] **Step 4: Implement local accounting**

Add the optional `verifier` callable parameter with `verify_finding_with_jev` as its default, and call that adapter for each frozen finding. Inside each run loop, initialize:

```python
run_jev_tokens = 0
run_jev_cost = 0.0
run_jev_time = 0.0
run_unknowns = 0
```

For each result, sum actual `usage.input_tokens` and `usage.output_tokens`; use `usage.cost_usd` only when supplied. Retain unknown findings without severity recalibration. Build each `RunResult` with original Claude token fields unchanged, per-run Jev fields, and `duration_seconds=r.duration_seconds + run_jev_time`.

Aggregate report totals from completed per-run counters after the loop. Rename console output from “false alarms pruned” to “findings rejected by verifier”.

- [x] **Step 5: Run focused tests**

```bash
python3 -m pytest tests/test_new_architecture.py tests/test_finding_verifier.py -q
```

Expected: PASS.

---

### Task 5: Supply Relevant Consumer Evidence

**Files:**
- Modify: `evals/review_cost/eval_new_architecture.py`
- Modify: `tests/test_new_architecture.py`

**Interfaces:**
- Consumes: a unified git diff, a revision, and consumer records from `find_consumers()`.
- Produces: touched-file exclusions, the exact consumer function body, the changed hunk containing the token, and explicit scored/unscored counts.

- [x] **Step 1: Write failing pure evidence tests**

Add tests for these public helpers:

```python
changed_files_from_diff(diff: str) -> set[str]
changed_hunk_for_token(diff: str, token: str) -> Optional[Tuple[str, str]]
consumer_function_code(path: str, code: str, function: str) -> str
```

Use a synthetic diff longer than 5,000 characters where the relevant hunk is at the end and a source file where the consumer function begins after character 5,000. Assert the returned hunk and body contain the target token and exclude unrelated prefixes.

- [x] **Step 2: Run the evidence tests and observe RED**

```bash
python3 -m pytest tests/test_new_architecture.py -q
```

Expected: helpers do not exist; current implementation truncates the first 5,000 characters.

- [x] **Step 3: Implement evidence extraction**

Parse `diff --git`, `+++ b/`, and `@@` sections without external dependencies. Return the file and complete hunk containing the exact token. Use `extract_functions()` to locate the named function and slice source lines from `start_line` through `end_line`.

Call:

```python
find_consumers(
    repo_path,
    c.intro_commit,
    tokens,
    exclude_files=changed_files,
)
```

Skip and count candidates whose exact producer hunk or consumer function cannot be recovered. Pass the real producer file, relevant hunk, and exact body to the verifier. Add `raw_candidates`, `scored_candidates`, and `unscored_candidates` to each result; do not describe unscored candidates as compatible.

- [x] **Step 4: Add an integration-style synthetic test**

Patch `find_consumers()` to return one touched-file function and one untouched function. Assert only the untouched function reaches `verify_consumer_with_jev` and receives exact evidence.

- [x] **Step 5: Run focused tests**

```bash
python3 -m pytest tests/test_new_architecture.py tests/test_consumer_verifier.py -q
```

Expected: PASS.

---

### Task 6: Make Grading Method Explicit and Conservative

**Files:**
- Modify: `evals/review_cost/models.py`
- Modify: `evals/review_cost/grader.py`
- Modify: `tests/test_review_cost_models.py`
- Modify: `tests/test_grader.py`

**Interfaces:**
- Consumes: findings plus optional explicit semantic judgments.
- Produces: `CaseGrading.grading_method` (`location_proxy` or `semantic`), semantic verdicts only when supplied, and location-only `near`/`unverifiable` outcomes otherwise.

- [x] **Step 1: Write failing conservative-grading tests**

Add:

```python
def test_matching_function_without_semantic_judgment_is_near(self):
    case = CaseConfig(
        case_id="medium_01",
        intro_commit="a" * 40,
        base_commit="a" * 40 + "~1",
        category="medium",
        touched_production_functions=40,
        fix_commit="b" * 40,
        fixed_functions={"src/order.py": ["Order.checkout"]},
    )
    finding = Finding("src/order.py", 30, "Order.checkout", "Unrelated cache concern", "major")
    grading = grade_case_findings(case, [finding], fix_diff="cache checkout", fix_message="fix checkout")
    self.assertEqual(grading.grading_method, "location_proxy")
    self.assertEqual(grading.known_defect_verdict, "near")
    self.assertEqual(grading.finding_judgments[0].verdict, "unverifiable")
```

Add a semantic-path test with `FindingJudgment(finding_index=0, verdict="correct", rationale="Matches the validated defect", matches_known_defect=True, claim_group_id="defect-1")`; assert `known_defect_verdict="found"` and `grading_method="semantic"`.

- [x] **Step 2: Run grader tests and observe RED**

```bash
python3 -m pytest tests/test_grader.py tests/test_review_cost_models.py -q
```

Expected: token overlap marks the unrelated claim correct and models lack grading metadata.

- [x] **Step 3: Extend grading models**

Add:

```python
@dataclass
class FindingJudgment:
    finding_index: int
    verdict: PrecisionType
    rationale: str
    matches_known_defect: bool = False
    claim_group_id: Optional[str] = None

@dataclass
class CaseGrading:
    case_id: str
    arm: str
    repetition: int
    known_defect_verdict: VerdictType
    finding_judgments: List[FindingJudgment] = field(default_factory=list)
    grading_method: str = "location_proxy"
    human_spot_checked: bool = False
    human_notes: str = ""
```

Preserve backward-compatible deserialization.

- [x] **Step 4: Remove lexical correctness inference**

Change `grade_case_findings()` to accept:

```python
semantic_judgments: Optional[Sequence[FindingJudgment]] = None
```

Without semantic judgments, exact function or file location yields `near`; every finding verdict is `unverifiable`. With semantic judgments, validate indices, copy judgments, and set `found` only when a `correct` judgment has `matches_known_defect=True`. Clean cases do not make findings automatically incorrect.

Update `build_grader_prompt()` to require JSON containing `verdict`, `matches_known_defect`, `claim_group_id`, and rationale, and to state that a novel valid defect may be `correct` without matching the known defect. Claims that describe the same underlying defect receive the same non-empty `claim_group_id`; unrelated claims receive distinct IDs.

- [x] **Step 5: Run focused tests**

```bash
python3 -m pytest tests/test_grader.py tests/test_review_cost_models.py -q
```

Expected: PASS.

---

### Task 7: Analyze Paired Cases Instead of Independent Findings

**Files:**
- Modify: `evals/review_cost/analyze_results.py`
- Modify: `tests/test_analyze_results.py`

**Interfaces:**
- Consumes: runs and gradings with case, arm, and repetition identifiers.
- Produces: case-level arm summaries, paired arm differences, case-clustered bootstrap intervals, and precision only for semantic gradings.

- [x] **Step 1: Write failing case-clustering tests**

Create two cases, two arms, and two repetitions per arm. Give one case ten semantic judgments sharing one `claim_group_id` and the other one judgment with a different group ID. Assert recall averages the two cases equally, precision counts each claim group once per run, and repetitions are averaged within each case/arm before comparison.

Add a paired-bootstrap test:

```python
pairs = {
    "case_1": {"A": 0.0, "B": 1.0},
    "case_2": {"A": 1.0, "B": 1.0},
}
mean, lo, hi = paired_cluster_bootstrap(pairs, arm_a="A", arm_b="B", n_resamples=500, seed=42)
self.assertEqual(mean, 0.5)
self.assertLessEqual(lo, mean)
self.assertGreaterEqual(hi, mean)
```

- [x] **Step 2: Run analysis tests and observe RED**

```bash
python3 -m pytest tests/test_analyze_results.py -q
```

Expected: current implementation treats each repetition and finding as independent and has no paired helper.

- [x] **Step 3: Implement case-level aggregation**

Add:

```python
def paired_cluster_bootstrap(
    values: Dict[str, Dict[str, float]],
    arm_a: str,
    arm_b: str,
    n_resamples: int = 5000,
    seed: int = 42,
) -> Tuple[float, float, float]:
```

Resample case IDs, preserve both arms, compute `B - A`, and return the observed mean plus percentile interval. Aggregate repetitions within a case/arm first.

`compute_stratum_metrics()` must:

- group by `(case_id, arm)`;
- average repetition-level cost, tokens, and duration within the group;
- calculate recall from one case-level outcome per arm;
- return `precision_rate=None` unless all contributing gradings use `grading_method="semantic"`;
- deduplicate semantic judgments by non-empty `claim_group_id` within each run before calculating its precision;
- include paired differences and intervals when both arms exist.

- [x] **Step 4: Correct the report language**

Remove unconditional statements that H1–H3 were checked. Report each hypothesis as `not evaluated`, `descriptive only`, or with an actual paired interval and decision rule. Include raw counts beside percentages.

- [x] **Step 5: Run focused tests**

```bash
python3 -m pytest tests/test_analyze_results.py -q
```

Expected: PASS.

---

### Task 8: Full Verification and Safety Audit

**Files:**
- Verify all modified files.
- Do not create benchmark outputs or modify `evals/private/`.

**Interfaces:**
- Consumes: completed Tasks 1–7.
- Produces: a green standard-library test suite and public-safety report.

- [x] **Step 1: Run the review-cost and verifier tests**

```bash
python3 -m pytest tests/test_review_cost_models.py tests/test_select_cases.py tests/test_runner.py tests/test_jev.py tests/test_cli.py tests/test_finding_verifier.py tests/test_consumer_verifier.py tests/test_new_architecture.py tests/test_grader.py tests/test_analyze_results.py -q
```

Expected: PASS.

- [x] **Step 2: Run the full standard-library suite**

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Expected: all tests pass.

- [x] **Step 3: Run the public-safety audit**

```bash
python3 scripts/public_safety_check.py --all
```

Expected: zero findings. If `TYPESAFE_API_KEY` is set and network policy permits, run the existing `--jev` audit separately; do not bypass hooks or allowlist private material.

- [x] **Step 4: Inspect the final diff**

Confirm:

- no private data or raw artifacts are tracked;
- no future-fix field reaches `build_review_prompt()`;
- both review arms use safe mode;
- no fabricated token constants remain in review-cost evaluation;
- no verifier error maps to a false decision;
- no report claims a hypothesis was statistically tested without paired output;
- the proposal remains the only pre-existing user change outside this implementation.

- [x] **Step 5: Leave changes uncommitted for user review**

Report changed files, focused/full test results, public-safety result, and any remaining limitation. Do not commit or push.
