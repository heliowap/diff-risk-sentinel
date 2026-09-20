# Proposal: Dynamic Counterfactual Reproducer ("Inverted Funnel Review")

## Status

**Research proposal only. Nothing described here has been implemented or validated end to end.**

The latency, cost, precision, and recall figures in this document are hypotheses or decision targets unless explicitly linked to an existing project result. The proposed system must not be described as a bug detector or as a replacement for code review until a preregistered evaluation supports those claims.

## 1. Purpose

The Dynamic Counterfactual Reproducer explores whether Diff Risk Sentinel can turn a small number of grounded review hypotheses into executable evidence cheaply enough for opt-in pre-push or CI use.

Its intended role is narrow:

> Given a diff and a small hypothesis budget, produce a minimal test that exhibits the predicted behavior, compare that behavior across revisions when possible, and surface only well-supported results.

This would complement the existing review workflow. It would not replace architectural review, domain reasoning, consumer analysis, deploy-transition analysis, or broad review of unread code.

## 2. What Existing Evidence Supports

The project already supports three premises behind this proposal:

1. **Risk ranking improves reading order, but cannot safely filter the diff.** In the 563-case forward-SZZ benchmark, reading the top 5% of touched functions reached a later-fixed function in 37% of cases with CRAP and 52% with CRAP + Jev. At 10%, CRAP + Jev reached 63%; at 20%, 73%. On diffs with 150 or more touched production functions, top-8 performance was approximately random ([results](../../evals/jev_szz_results.md)).
2. **Important defects can live in small functions.** In the 23,000-line case study, one defect was inside the top-ranked component, while two others were in functions ranked #49 and #388 of 454 structural targets ([case study](../../evals/case_study_epic_pr.md)).
3. **Evidence-first verification can remove review noise.** In a small pilot, the Stage 6 Finding Verifier preserved 2 validated findings while reducing 7 findings to 3, moving observed precision from 28.6% to 66.7% ([pilot results](../../evals/review_cost/RESULTS.md)). This sample is too small to establish the true precision or false-negative rate.

These results justify testing an executable-evidence stage. They do **not** justify the original claims of 90–95% precision, 30–45 second end-to-end latency, or safe 80–90% pruning.

## 3. Scope and Non-Goals

### In scope

- Functional logic regressions with locally constructible inputs.
- Boundary-value and state-transition defects.
- Contract violations for which the relevant consumer or invariant can be cited.
- Python and JavaScript/TypeScript projects whose existing unit-test runner can execute hermetically.
- New-code hypotheses when no compatible base-revision comparison exists, reported with weaker evidence.

### Out of scope

- Declaring unread functions safe.
- Architecture, maintainability, product intent, or domain-model review.
- Defects that require production traffic, real credentials, external services, long-lived state, or nondeterministic distributed behavior.
- Security testing against live systems.
- Automatically modifying the source checkout, committing generated tests, or applying repairs.

## 4. Revised Inverted Funnel

The ranking is a **candidate router**, not a correctness gate. Each stage records its inputs, outcomes, and reason for attrition.

```text
[Diff + existing tests + optional coverage]
                    |
                    v
Stage 1: Candidate Routing
  - Rank touched production functions with CRAP + Jev when policy allows;
    otherwise use CRAP and mark semantic ranking unavailable.
  - Use a budgeted audit set rather than a fixed top 1–2.
  - Include small behavior-changing neighbours and candidate consumers of
    changed contracts; do not infer safety from omission.
                    |
                    v
Stage 2: Grounded Hypothesis Generation
  - Read the target, its diff, relevant neighbouring edits, existing tests,
    and cited caller/consumer evidence.
  - Emit at most a small global budget of concrete hypotheses.
  - For each hypothesis, specify inputs, setup, predicted observation,
    expected contract, citations, and external dependencies.
                    |
                    v
Stage 3: Isolated Test Generation and Counterfactual Execution
  - Generate a minimal test in the project's existing framework.
  - Run it in a disposable sandbox against the head revision.
  - If it fails in the predicted way, run the same test against the base
    revision when the test and API are compatible.
  - Classify setup errors explicitly; never count them as a clean result.
                    |
                    v
Stage 4: Evidence-Based Verdict
  - Evaluate the claim together with cited code, test source, exact failure,
    and base/head outcomes.
  - In Via 1, use Jev after executable evidence exists, as a semantic verifier
    or ranker rather than as an irreversible pre-execution veto; retain the
    current static Stage 6 only as an explicit evaluation comparator.
  - Surface the evidence tier, not an "undeniable bug" claim.
```

### 4.1 Stage 1: Candidate Routing

A fixed top-1 or top-2 funnel would impose an end-to-end recall ceiling unsupported by the project's ranking benchmark. Candidate-budget policies such as top 8 and top 10–20% must be compared only on the development set; the selected policy is then frozen before the held-out evaluation.

The audit set should combine:

- CRAP + Jev ranking when sending code to TypeSafe is permitted;
- small changed functions in files containing top targets, especially changes to conditions, validation, defaults, or initial state;
- callers or consumers that may rely on a changed contract; and
- existing tests near the changed behavior.

Only a small number of hypotheses need proceed to test generation. The audit set and the test-generation budget are separate controls.

### 4.2 Stage 2: Grounded Hypotheses

Each hypothesis must be falsifiable and carry an evidence packet:

- target file, function, and revision;
- precise defect claim;
- minimum input and setup;
- predicted head-revision behavior;
- expected behavior and its source (existing test, caller contract, schema, documentation, or clearly stated inference);
- cited code supporting reachability;
- relevant neighbour or consumer context; and
- known test-runner, fixture, database, network, or service requirements.

A hypothesis without a defensible expected-behavior source may still be explored, but it cannot later be labeled strong regression evidence solely because a generated assertion fails.

### 4.3 Stage 3: Counterfactual Execution

A failing generated test proves that the implementation disagrees with the generated oracle. It does not by itself prove that the implementation is wrong. The base/head comparison strengthens the evidence by asking whether the diff introduced the demonstrated behavior.

| Head outcome | Base outcome | Classification |
|---|---|---|
| Test passes | Not run | Not reproduced |
| Setup/import/fixture failure | Not run | Inconclusive: runner/setup |
| Fails, but not as predicted | Any | Hypothesis mismatch |
| Predicted failure | Passes | Strong regression evidence |
| Predicted failure | Same failure | Pre-existing behavior or invalid regression oracle |
| Predicted failure | Test/API incompatible | Head-only executable hypothesis |
| Predicted failure in new code | No base target | Head-only executable hypothesis |

The same generated test should be used on both revisions. If it must be rewritten for the base revision, the result is no longer a strict counterfactual and must be reported separately.

The source checkout remains untouched. Tests execute in a disposable workspace. For surfaced results, the report returns the generated test source, exact command, and relevant output so a developer can inspect or adopt the reproducer; it never commits the test automatically.

### 4.4 Stage 4: Evidence-Based Verdict

The semantic verifier receives evidence unavailable to the current function-only judgments:

- the original claim and expected-behavior source;
- cited target, neighbour, caller, and consumer code;
- generated test source;
- normalized head failure;
- base result or reason it could not run; and
- diff context indicating whether the behavior could be intentional.

The default user-facing policy should surface:

1. **Strong regression evidence:** predicted failure on head, pass on base, and a supported semantic claim.
2. **Executable hypothesis:** predicted failure on head but no valid base comparison; clearly labeled for human review.
3. **No alert:** not reproduced, pre-existing behavior, hypothesis mismatch, or verifier rejection.
4. **Inconclusive telemetry:** runner or environment failure. This is not shown as a bug, but it is retained in aggregate diagnostics so funnel reliability can be measured.

## 5. Why This Is Not an Executable Proof of Defect

Generated tests have an oracle problem: the model may encode a false assumption about intended behavior. Base-pass/head-fail evidence demonstrates a behavioral regression, but that regression may be intentional. New functionality often has no compatible base behavior at all.

Therefore:

- use **"strong regression evidence"**, not "undeniable bug";
- cite the source of the expected behavior;
- distinguish deterministic execution evidence from semantic judgment;
- require human confirmation before treating a surfaced result as a defect; and
- measure both precision and end-to-end recall.

The value proposition is still meaningful: execution can eliminate claims whose predicted path is unreachable, whose imports or inputs are invalid, or whose alleged failure does not occur. It also produces an actionable reproducer when the hypothesis is valid.

## 6. Safety and Isolation Requirements

Directory cleanup alone is not a sandbox. Generated tests and repository test hooks must be treated as untrusted code.

A viable runner requires:

- a disposable filesystem that never writes to the source checkout (for example, a temporary worktree or clone);
- process isolation from the host, such as a restricted container or VM; a worktree alone is not isolation;
- no production credentials or inherited developer secrets;
- network disabled by default;
- explicit CPU, memory, process, and wall-clock limits;
- an allowlisted existing test command rather than arbitrary shell generation;
- no dependency installation during the review run;
- no access to production databases or services; and
- clear handling of test processes that outlive the timeout.

Using Jev remains opt-in because function source and evidence are sent to `api.typesafe.ai`. Data-handling policy must be checked before enabling it.

## 7. Layered Evaluation Design

The reproducer must be evaluated offline before any pre-push or CI integration is proposed. The experiment is divided into layers so that candidate generation, semantic validation, and dynamic execution can be measured without changing several variables at once.

### 7.1 Experimental Controls and Units

The protocol must satisfy these controls before any benchmark run:

- Split cases by introducing commit into a **development set** and a sealed **held-out set**. Cases from earlier pilots, benchmarks, case studies, prompt design, or threshold tuning are development-only.
- Keep the future fix commit, its SHA, message, diff, issue metadata, and descendants unavailable to reviewers and hypothesis generators. A reviewer may see only the actual introducing-commit subject obtained from the isolated repository.
- Pin the model version, prompts, tool versions, permissions, effort, time limit, and token budget. Randomize or counterbalance arm order and run at least two independent repetitions per generation arm.
- Precompute Sentinel reports and worksheets outside the review agent. A Sentinel-assisted run is invalid if the expected artifact is absent or was not consumed.
- Keep review agents read-only in both generation arms. The presence of the frozen Sentinel artifact is the only tooling difference.
- Normalize every review output into an arm-blind hypothesis record containing a stable ID, location, claim, severity, expected behavior, and cited evidence. Freeze these records before validation begins.
- Treat the introducing commit as the primary statistical unit. Findings, tests, repetitions, and validator outcomes from the same commit remain clustered in resampling and inference.

### 7.2 Experiment A: Hypothesis Generation

Experiment A asks whether Sentinel changes the quality or cost of the hypotheses produced before any finding filter or generated test is applied.

| Arm | Inputs and workflow | Output |
|---|---|---|
| **G0 — Baseline Review** | Raw diff plus read-only repository navigation under the standard review prompt | Normalized raw hypotheses |
| **G1 — Sentinel-Assisted Review** | The same review environment plus a precomputed Sentinel report and worksheet using the frozen routing/context policy | Normalized raw hypotheses |

Both arms receive the same review objective and resource budget. Neither arm receives Stage 6 decisions, generated tests, future fixes, or historical defect descriptions. Although G1 artifacts are precomputed for treatment fidelity, their Sentinel and Jev usage remains part of G1's end-to-end cost and latency accounting.

Compare G0 and G1 on:

- case-level known-defect recall after blinded semantic grading;
- unique-defect precision after duplicate claims are clustered;
- candidate coverage and hypothesis yield for G1;
- false alerts per negative diff;
- completion rate, model tokens, cost, and latency.

This comparison estimates the effect of the complete Sentinel-assisted review workflow. The routing and context components inside G1 are selected separately on the development set.

### 7.3 Experiment B: Paired Finding Validation

Experiment B freezes the hypotheses from Experiment A and applies every validator to the same claims. Validators do not trigger a new code review.

| Condition | Treatment applied to the frozen hypothesis |
|---|---|
| **V0 — Raw** | No validation; retain the original hypothesis |
| **V1 — Static Jev** | Judge the claim against the cited static code evidence |
| **V2 — Dynamic Execution** | Generate one test, run it on `head`, then run the unchanged test on `base` when compatible |
| **V3 — Dynamic + Post-Execution Jev** | Reuse the V2 test and outcomes, then judge the claim with the test source, logs, cited contract, and base/head evidence |

V2 and V3 share the same generated test, repair attempts, execution logs, and resource limits. V2 records the head-only decision before adding the base result, so H4 does not require a second generation run. V3 adds only the post-execution semantic judgment. V1, V2, and V3 therefore measure whether each validation layer retains correct hypotheses and rejects incorrect ones; they do not measure differences caused by a second stochastic review.

For each condition, report:

- retention of correct, incorrect, and unverifiable hypotheses;
- unique-defect precision and case-level recall after validation;
- false alerts per negative diff;
- validator errors and timeouts as `unknown`, never as automatic rejection;
- incremental tokens, cost, and latency.

### 7.4 Counterfactual Semantics

`base` and `fix` are not interchangeable counterfactuals:

- **Product-time comparison (`base` → `head`):** asks whether the reviewed diff introduced the demonstrated behavior. A predicted failure on `head` with a pass on `base` is change-specific execution evidence, not proof that the change is unintended.
- **Historical evaluation (`intro` → `fix`):** asks whether the unchanged test captures behavior removed by the later fix. The fix remains hidden until generation and execution against `intro` are complete.

The same generated test must be transplanted unchanged. A rewrite for another revision is recorded as incompatible rather than counted as a pass.

A high-confidence historical reproduction requires all three conditions:

1. the test fails as predicted on the introducing revision;
2. the unchanged test passes on the fix revision; and
3. blinded semantic grading confirms that the claim and test match the historical defect rather than unrelated behavior.

Base incompatibility, fix incompatibility, setup failure, and hypothesis mismatch are separate outcomes with separate denominators. The protocol must never substitute `fix` for `base`, or vice versa, after observing the result.

### 7.5 Development-Set Routing and Context Ablation

The routing and context policy for G1 is selected by a 2×2 factorial ablation on the development set:

| Cell | Ranking | Generator context |
|---|---|---|
| **R0C0** | CRAP | Target plus existing nearby tests |
| **R1C0** | CRAP + Jev ranking | Target plus existing nearby tests |
| **R0C1** | CRAP | Target, existing tests, changed neighbours, and candidate consumers |
| **R1C1** | CRAP + Jev ranking | Target, existing tests, changed neighbours, and candidate consumers |

All four cells use the same candidate budget, maximum context tokens, model, maximum hypothesis count, and generation attempts. Evaluate:

- known-defect candidate recall at the frozen budget;
- correct-hypothesis yield per audited candidate;
- end-to-end hypothesis recall;
- tokens, cost, and latency.

Candidate budgets such as top 8 and top 10–20% may also be compared on the development set. Select one routing/context/budget policy before opening the held-out set; do not choose the best cell from held-out results.

### 7.6 Confirmatory End-to-End Arms

After the development ablations select a policy, freeze the complete pipelines for held-out confirmation:

| Arm | Frozen pipeline | User-facing deliverable |
|---|---|---|
| **E0 — Baseline** | G0 raw review | Normalized textual findings |
| **E1 — Sentinel Static** | G1 hypotheses followed by V1 with the frozen Stage 6 threshold | Validated textual findings |
| **E2 — Via 1** | The same G1 hypothesis set followed by V2 and V3 with frozen generation, repair, execution, and verdict policies | Findings with executable evidence and explicit evidence tiers |

E1 and E2 branch from the same frozen G1 hypotheses within each repetition. This makes their difference attributable to validation rather than hypothesis-generation variance. Experiment A remains the source of the causal comparison between G0 and G1; E0–E2 compare the complete products a developer would receive.

### 7.7 Positive and Negative Cases

Historical positive cases come from forward-SZZ links between an introducing revision and a later fix, but each selected case requires human confirmation that:

- the later commit fixes a functional defect;
- the introducing commit plausibly introduced it;
- the labeled function contains the relevant behavior; and
- the case was not used during system design.

Prepare an isolated repository ending at the introducing revision before generation. The fix is used only after outputs are frozen.

Negative cases must be code-bearing commits reviewed by humans as having no target defect under the rubric. "No subsequent fix" alone is insufficient. Novel findings on either positive or negative cases receive semantic adjudication rather than being declared false merely because they do not match the historical fix.

Predetermine test-runner eligibility before generation. The primary end-to-end comparison retains every assigned case and counts setup failures against Via 1's operational result. Report the eligible hermetic subset separately as a diagnostic; do not remove cases after seeing outcomes.

### 7.8 Metrics

#### Co-primary quality metrics

- **End-to-end known-defect recall:** known defects correctly identified or reproduced, divided by all human-validated positive cases assigned to the arm, measured per case. Via 1 setup failures remain in this denominator; the predeclared hermetic subset is reported separately.
- **Unique-defect precision:** human-confirmed defects divided by distinct surfaced defect claims after duplicate findings are clustered.

#### Funnel diagnostics

- candidate recall at the frozen budget;
- correct-hypothesis yield per candidate;
- hypothesis recall conditional on candidate inclusion and end to end;
- runnable-test rate;
- predicted-failure reproduction rate on `head`;
- change-specific evidence rate (`head` fails as predicted and `base` passes);
- historical reproduction rate (`intro` fails as predicted and `fix` passes);
- base/fix incompatibility, setup failure, hypothesis mismatch, validator rejection, and validator error rates;
- retention of correct and incorrect hypotheses at V1–V3;
- false alerts per negative diff.

#### Operational metrics

- uncached input, cache-read, cache-creation, and output tokens reported separately;
- actual Jev usage and cost rather than fixed token estimates;
- test-runner compute and repair-loop cost;
- p50 and p95 latency by stage and end to end;
- completion rate and cost per confirmed defect.

Every metric must include raw counts, its denominator, and a confidence interval. Small-stratum percentages must not be presented without their counts.

### 7.9 Blinded Grading and Statistical Analysis

Randomize and anonymize findings before grading. At least two independent graders judge semantic correctness against the historical defect and repository evidence, with human adjudication of disagreements and a preregistered spot-check. The grader must not infer correctness from function-name or token overlap alone.

Cluster duplicate findings that describe the same defect before precision is calculated. Preserve genuinely novel validated defects even when they differ from the historical fix.

Use paired, case-clustered bootstrap intervals for arm differences, resampling introducing commits and carrying all corresponding arms, repetitions, findings, and tests together. Determine the held-out sample size before execution with a preregistered power analysis or simulation over paired case-level outcomes; ten-case pilots are not confirmatory. Non-inferiority, superiority, and operational targets must be defined before held-out execution. Multiple primary comparisons require a preregistered correction or hierarchy.

## 8. Research Hypotheses and Decision Rules

The layered design tests these hypotheses:

- **H1 — Sentinel generation efficiency:** On medium and large diffs, G1 uses at least 30% fewer reviewer tokens than G0 while its case-level known-defect recall is non-inferior within a 5 percentage-point margin.
- **H2 — Static verification:** V1 improves unique-defect precision over V0 without reducing case-level known-defect recall by more than 5 percentage points.
- **H3 — Dynamic evidence:** V3 improves unique-defect precision over V1 without reducing case-level known-defect recall by more than 5 percentage points.
- **H4 — Counterfactual value:** Base/head execution improves precision over a head-only verdict when both use the same generated tests and hypothesis set.

The original performance figures remain **aspirational operational targets**, not established facts:

- strong-evidence precision of approximately 90–95%;
- approximately 30–45 seconds on compatible hermetic unit-test cases;
- approximately $0.03–$0.05 variable AI cost per diff, excluding environment provisioning and reporting test-runner compute separately.

Apply these decision rules:

1. A precision gain does not compensate for an undisclosed or out-of-margin recall loss.
2. H1 fails if token savings arise only because G1 audits less code and misses more defects.
3. Stage 6 is not adopted if H2 fails, even if it reduces the number of findings.
4. Via 1 is not promoted over static validation if H3 fails; runnable-test and setup-failure rates remain operational constraints, not exclusions.
5. Base/head and intro/fix results remain separate. Success on one cannot substitute for failure or incompatibility on the other.
6. Results must be stratified by diff size, language, test framework, and compatibility, but confirmatory claims come from preregistered aggregate comparisons unless strata are adequately powered.
7. The system cannot be called a replacement for review unless E2 demonstrates non-inferior recall against E0 within the preregistered margin and a precision or cost benefit.

## 9. Phased Research Sequence

1. **Preregister and split:** human-validate cases, freeze eligibility rules, create development and held-out splits by introducing commit, and register prompts, budgets, evidence tiers, metrics, retry limits, grading, and analysis.
2. **Establish runner feasibility:** on development cases only, measure test discovery, runnable-test rate, failure taxonomy, revision compatibility, sandbox behavior, latency, and repair-loop cost.
3. **Select the development policy:** run the routing/context/budget ablations, calibrate validators and thresholds, and freeze the G1, E1, and E2 policies.
4. **Validate the layers on development data:** run Experiments A and B to verify treatment fidelity, telemetry, grading, and statistical code before confirmation.
5. **Run held-out confirmation once:** execute E0–E2 in randomized order with independent repetitions, blind grading, and case-clustered analysis. Do not tune after seeing held-out outcomes.
6. **Only if supported, build an opt-in integration:** begin in CI or an isolated local command; do not install it as a blocking pre-push gate by default.

## 10. Open Questions

- What minimum repository metadata is needed to discover a safe test command and fixture pattern?
- How often can one unchanged test run against both base and head revisions?
- What proportion of useful hypotheses require external services or integration fixtures?
- Can existing tests provide a reliable oracle more often than generated assertions?
- Should head-only executable hypotheses ever block a change, or only annotate it?
- Which candidate budget maximizes reproduced defects per dollar without collapsing recall on large diffs?
- Does post-execution semantic verification improve precision beyond the deterministic outcome table?

## 11. Conceptual Verdict

The Dynamic Counterfactual Reproducer is worth evaluating as a **high-precision, executable-evidence layer for selected functional regressions**. Its strongest form is not "AI writes a failing test, therefore the code is wrong," but:

> A grounded hypothesis predicts a specific failure; the failure occurs on the changed revision; the same test passes on a meaningful counterfactual revision; and the semantic claim remains supported when code, contract, test, and execution evidence are considered together.

That result would be more actionable than a speculative review comment. Whether it is frequent, fast, cheap, and accurate enough to justify integration remains an empirical question.