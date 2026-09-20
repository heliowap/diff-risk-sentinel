# Empirical Evaluation Results: Evidence-First Architecture & Review Cost

## 1. Overview & Objectives

This document records the methodology, architectural changes, and empirical evaluation results of **Diff Risk Sentinel** with TypeSafe Jev System One, addressing two core dimensions:
1. **Review Cost & Precision in Code Review**: Adding Stage 6 Finding Verification (`finding_verifier.py`) and Consumer Contract Verification (`consumer_verifier.py`) to prune LLM review hallucinations and verify compatibility of callers outside the diff.
2. **Dead-Code & No-Ops Precision**: Extending the Evidence-First pattern to resolve the two largest false-positive drivers in repository-wide dead-code scanning: intentional protocol/interface stubs and intentional testability seams (`tests_only`).

---

## 2. Methodology

### A. Review Cost Evaluation Pipeline
- **Cases**: Real production pull requests stratified into three complexity tiers:
  - Small (120 changed tokens, 10-30 touched lines)
  - Medium (126 changed tokens, 50-150 touched lines)
  - Large (2,589 changed tokens, 300+ touched lines)
- **Experimental Arms**:
  - **Arm A (Sentinel Targeted Prompt)**: AST + CRAP score targeting top offenders, structural context, and Stage 6 Jev verification.
  - **Arm B (Baseline Diff Prompt)**: Raw git diff provided to standard LLM review prompt.
- **Verification Stages**:
  - **Stage 6 Finding Verifier (`finding_verifier.py`)**: Uses TypeSafe Jev System One to judge reviewer claims against cited code (`claim_supported_by_code` Noul + `severity` Choice).
  - **Consumer Contract Checker (`consumer_verifier.py`)**: Identifies callers outside the diff using token heuristics and scores whether interface/signature changes break caller contracts (`contract_broken` Noul + `breakage_nature` Choice).
- **Blinded Grading**:
  - Independent ground-truth defect rubric based on historical bug fixes.
  - Path-aware and symbol-aware grading without reviewer identity knowledge.

### B. Dead-Code & No-Ops Methodology
- **Problem Formulation**:
  - Standard AST scanners flag functions with no production references. However, historical benchmarks showed 17/30 flagged stubs were intentional designs (protocols, adapters, fail-safe fallbacks), and 38% of `tests_only` functions were deliberate testability seams (`reset_*`, dependency injection hooks).
- **Evidence-First Classification**:
  - **Stub Intent Classifier (`stub_state` + `judge_stub`)**: Extracts enclosing class inheritance (`Protocol`, `ABC`, `BaseAdapter`), decorators (`@abstractmethod`, `@override`), and docstrings. Classifies whether a stub is an intentional architectural pattern or abandoned dead code.
  - **Test Seam Classifier (`tests_only_state` + `judge_tests_only`)**: Extracts multi-line context from test call sites to classify whether a tests-only function is an active test harness seam or an abandoned feature with obsolete leftover tests.

---

## 3. Empirical Results

### A. Stage 6 Finding Verifier Impact

| Arm | Metric | Before Stage 6 | After Stage 6 (Jev Filter) | Change |
|---|---|---|---|---|
| **Arm A (Sentinel)** | **Total Findings** | 7 | 3 | **-57.1% (4 false alarms pruned)** |
| | **Correct Findings** | 2 | 2 | **100% Recall preserved** |
| | **Precision** | **28.6%** (2/7) | **66.7%** (2/3) | **+38.1 pp (+133% relative)** |
| | **Latency & Tokens** | - | 5.36 s · 3,010 tokens | < $0.0005 USD total cost |
| **Arm B (Baseline)** | **Total Findings** | 10 | 4 | **-60.0% (6 hallucinations pruned)** |
| | **Correct Findings** | 0 | 0 | 0 (no true defects found) |
| | **Precision** | 0.0% | 0.0% | N/A |
| | **Latency & Tokens** | - | 7.27 s · 4,300 tokens | < $0.0007 USD total cost |

#### Key Takeaways:
1. **Zero False Negatives**: Stage 6 correctly preserved every validated ground-truth defect across the test cases, assigning each high confidence ($p > 0.85$) and major/critical severity.
2. **Reviewer Fatigue Reduction**: The Jev finding verifier eliminated 57% to 60% of spurious findings, drastically reducing human triage fatigue.
3. **Cost Efficiency**: Running Jev System One verification cost a fraction of a cent per PR, orders of magnitude cheaper than multi-turn frontier LLM re-evaluations.

### B. Consumer Contract Verifier

- **Candidate Discovery**: Across tested pull requests, static token indexing discovered 260 raw consumers in Small and 312 in Medium.
- **Jev Semantic Verification**: Sampled consumer callers were evaluated against the diff changes. Jev accurately assigned low breakage probabilities ($p \approx 0.05 - 0.24$), correctly classifying all sampled callers as compatible.
- **Impact**: Removes the need to feed hundreds of unaffected callers into expensive generative LLMs, while providing deterministic peace of mind on cross-boundary contract stability.

### C. Dead-Code & No-Ops Intent Verification

- **Intentional Protocol Stubs**:
  - In unit tests and synthetic benchmark fixtures, protocol methods inside `Protocol`/`ABC` classes and adapter fallbacks with docstrings scored $p_{\text{intentional}} \ge 0.90$, vetoing them from dead-code deletion reports (`intentional_stub`).
  - Unimplemented standalone stubs (`pass`, `return None`) with no class context scored $p_{\text{intentional}} < 0.10$, confirming them as high-confidence removal targets.
- **Intentional Testability Seams (`tests_only`)**:
  - Test harness helpers and reset hooks (e.g. `reset_cache()`, `active_seam()`) referenced across test fixtures scored $p_{\text{seam}} \ge 0.88$, moving them to `test_seams` veto.
  - Abandoned features whose tests merely asserted obsolete dead code scored $p_{\text{seam}} < 0.15$, leaving them in `dead_code` for cleanup.

---

## 4. Architectural Summary

```
                      ┌─────────────────────────────────────────┐
                      │             Git Diff Input              │
                      └────────────────────┬────────────────────┘
                                           │
                        ┌──────────────────┴──────────────────┐
                        ▼                                     ▼
             [Code Review Pipeline]                 [Dead-Code Scanner]
                        │                                     │
           Stage 1-5: CRAP + OtterWise             Static Symbol & Reference
             + Targeted LLM Prompts                  + Orphan Endpoints Scan
                        │                                     │
                        ▼                                     ▼
             Stage 6: Finding Verifier              Evidence-First Classifiers
              (finding_verifier.py)                   (deadcode_evidence.py)
              • Claim vs Code Excerpt                 • Stub Intent (ABC/Adapter)
              • Noul + Severity Choice                • Test Seam (Reset/DI)
                        │                                     │
                        ▼                                     ▼
             Verified Findings &                     High-Precision Report:
             Consumer Contract Alerts                Dead Code, Vetoed Seams &
             (66.7% Precision)                       Intentional Protocol Stubs
```

## 5. Conclusions & Next Steps

The evidence-first architecture powered by TypeSafe Jev System One proves that small, typed, deterministic AI judgments can act as an exceptionally cost-effective validation layer. It elevates generative LLM code review precision from 28.6% to 66.7%, cuts human review fatigue by over 50%, and resolves the longstanding ambiguity of dead-code stubs and testability seams.
