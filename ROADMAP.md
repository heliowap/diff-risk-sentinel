# Diff Risk Sentinel — Project Roadmap & Architecture Memory

## Current Milestone (Completed & Validated)

- [x] **Core Structural Triage**: CRAP score, CCN, test coverage ingestion, and OtterWise risk categorization.
- [x] **Semantic Risk Triage**: TypeSafe Jev System One multi-dimensional evaluation (`--jev`).
- [x] **Stage 6 Finding Verifier (`finding_verifier.py`)**: Evidence-first validation of reviewer claims against cited code excerpts. Prunes false alarms, lifting precision from 28.6% to 66.7% at <$0.0005 USD per run.
- [x] **Consumer Contract Verifier (`consumer_verifier.py`)**: Identifies and verifies contract stability for callers outside the diff without expensive whole-repo LLM context dumps.
- [x] **Evidence-First Dead-Code Intent Classifiers (`deadcode_evidence.py`)**:
  - Classifies intentional protocol/abstract stubs and adapter fallbacks ($p_{\text{intent}} \ge 0.90$) vs abandoned dead code.
  - Classifies intentional testability seams (`reset_*`, DI setters) vs abandoned features with orphan tests.

---

## Under Exploration / Research Backlog

### 1. Dynamic Counterfactual Reproducer ("Inverted Funnel Review")
- **Document**: [`docs/proposals/counterfactual_test_reproducer.md`](docs/proposals/counterfactual_test_reproducer.md)
- **Concept**: Instead of broad 3–5 minute agentic code reviews that output speculative markdown text, construct a 30–45s inverted funnel:
  $$\text{Sentinel (90\% structural pruning)} \longrightarrow \text{Targeted Micro-Audit} \longrightarrow \text{Jev Stage 6 Veto} \longrightarrow \text{Executable Unit Test Reproducer}$$
- **Target Metrics**: ~30–45s turnaround, ~$0.03–$0.05 USD per PR, ~95% precision (alerts are backed by an undeniable failing test).
- **Status**: Formulated and archived in project memory for future prototyping.

### 2. Upstream Invariant Checker (Stage 6.2 Interprocedural Query)
- **Concept**: When a reviewer claims a function lacks defensive validation (e.g. `user_id is None`), query the callers (`RepoIndex`/`find_consumers`) to verify if the caller or routing schema guarantees the invariant before alerting the developer.
- **Goal**: Eliminate "unverifiable defensive check" false alarms without needing a live runtime.

### 3. Transitive Dead-Code Graph Cleaning
- **Concept**: Connect `orphan_endpoints` to downstream internal helper functions that have callers, but whose entire call graph only exists to serve the dead entrypoint.
