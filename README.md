# Diff Risk Sentinel 🛡️⚡

**High-Throughput Risk Triage for Pull Requests — CRAP Score, OtterWise PR Methodology and TypeSafe Jev System One — plus a repository-wide dead-code scan.**

[![Tests](https://github.com/heliowap/diff-risk-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/heliowap/diff-risk-sentinel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

---

## 🎯 The Problem

Large pull requests (>300 lines, refactors, or epic branches) break traditional code reviews:
- **Generative LLMs** suffer from attention dilution, hallucinate on massive diffs, or hit hard token/diff limits (e.g. GitHub API 406 on >20k lines).
- **CI Pipelines** waste compute running expensive, 20-minute Playwright/e2e suites on changes that pose zero semantic risk.
- **Static Analysis / Linters** catch syntax and types, but are blind to subtle logical regressions and transactional edge cases.

---

## 💡 The Solution

**Diff Risk Sentinel** is a fast triage radar: it ranks every method touched by a diff by structural risk (and, optionally, semantic risk) so review effort goes to a short list first. On a 198-file, +21,681 / −2,383 diff it takes **~1.4 s** in CRAP-only mode and **~22 s** with `--jev` (373 production functions judged by TypeSafe Jev).

It is a **prioritization aid, not a bug detector** — see [Evals](#-testing--evals) for what it does and does not measure.

A second mode, **`--dead-code`**, scans a whole repository at one revision for code nothing uses: production functions nothing references (Python and JS/TS), functions used only by tests, and HTTP endpoints no client in the repository requests. On a production monorepo a team acting on its report removed 97% of the unreferenced functions it listed ([Dead-code scan](#dead-code-scan)).

```
[Git Diff (300 to 20,000+ lines)]
       │
       ▼
 1. Structural Analysis (CRAP + OtterWise)
    • Reads both sides of the diff straight from git (base and head revisions)
    • Computes Cyclomatic Complexity (CCN) for every touched method, before and after
    • Ingests Cobertura XML test coverage (paths resolved against <sources>)
    • Calculates CRAP = CCN² × (1 - coverage)³ + CCN
    • Computes real ΔCRAP per method + Combined & Average CRAP (before → after) per PR
       │
       ▼
 2. Semantic Evaluation (TypeSafe Jev System One) — opt-in with --jev
    • Sends every touched production function (new code, previous version, diff) to Jev in parallel
    • Four judgments: introduces a bug · mishandles edge cases · behavior change · regression risk
       │
       ▼
 3. Triage & OtterWise Prescription
    • Ranking: mean percentile rank of CRAP and the four Jev answers (CRAP alone without --jev)
    • Consumers outside the diff that still reference a changed contract (identifiers, key formats,
      removed hard-coded values)
    • Generates `llm_review_targets.json` with targeted prescriptions:
        - CRITICAL_SEMANTIC_AUDIT (Audit state mutations & callers)
        - HIGH_RISK_REFACTOR (Extract seams, early returns, lookup tables)
        - NEEDS_ATTENTION_TESTS (Write parameterized unit tests)
        - SEMANTIC_REVIEW (Structurally fine, behavior change worth a look)
        - BENEFICIAL_REFACTOR (Fast-pass approved!)
```

---

## 📊 OtterWise PR Methodology & Matrix

$$\text{CRAP} = \text{CCN}^2 \times (1 - \text{coverage})^3 + \text{CCN}$$
$$\text{Triage (with --jev)} = \text{mean}\big(\text{pct}(\text{CRAP}),\ \text{pct}(\text{Jev}_\text{bug}),\ \text{pct}(\text{Jev}_\text{edge}),\ \text{pct}(\text{Jev}_\text{behavior}),\ \text{pct}(\text{Jev}_\text{risk})\big)$$

where pct is the function's percentile rank within the diff. This combination was chosen before, and validated on, a 563-case historical benchmark ([results](evals/jev_szz_results.md)); no single Jev answer beat CRAP alone.

### The Thresholds
- **0 – 30 (Acceptable):** Healthy balance between complexity and tests.
- **30 – 60 (Needs Attention):** Yellow zone — prioritize adding tests to pull CRAP below 30.
- **60+ (High Risk):** Red zone — exponential risk penalty; tests alone are not enough, requires structural refactoring.

> Without a coverage report every method is scored at 0% coverage, so CRAP reduces to CCN² + CCN and any method with CCN ≥ 8 lands in the red zone. Feed `--coverage` for meaningful CRAP.

### Combined vs. Average CRAP
- **Combined CRAP:** Sum of CRAP across all touched methods, before and after the diff (removed methods count on the *before* side, new methods on the *after* side).
- **Average CRAP:** Mean CRAP per method, before and after.
- **Key Trend Insight:** A PR that increases Combined CRAP but lowers Average CRAP is actively improving codebase quality per method.
- Aggregates cover **every touched method**, not only the ones above the alert thresholds.

### ΔCRAP
Each touched method is matched by qualified name (`Class.method`, `outer.inner`) with its version in the base revision. ΔCRAP compares both CCNs under the same coverage; a method that did not exist before contributes its full CRAP.

### Risk Matrix
Rules are evaluated top to bottom; the first match wins. An improving ΔCRAP never fast-passes a method that is still in the yellow or red zone.

| # | Condition | Badge | Prescribed Strategy |
|---|---|---|---|
| 1 | Jev $\ge 2.0$ | `CRITICAL_SEMANTIC_AUDIT` | Audit business logic, unhandled edge cases, and state mutations before merging. |
| 2 | $\text{CRAP} \ge 60$ or $\text{CCN} \ge 20$ | `HIGH_RISK_REFACTOR` | Refactor: extract methods (seams), use early returns, or replace conditionals with lookup tables. |
| 3 | $\text{CRAP} \in [30, 60)$ | `NEEDS_ATTENTION_TESTS` | Add unit tests to increase coverage and pull CRAP score below 30. |
| 4 | $1.0 \le$ Jev $< 2.0$ | `SEMANTIC_REVIEW` | Structurally healthy, but review the behavior change before merging. |
| 5 | $\Delta\text{CRAP} < 0$ | `BENEFICIAL_REFACTOR` | Fast-pass approved. PR improved code quality. |
| 6 | otherwise | `ACCEPTABLE_LOW_RISK` | Low risk. Safe to merge. |

When Jev is disabled or a Jev call fails, the semantic risk is *unknown* and only rules 2, 3, 5 and 6 apply.

---

## 🚀 Installation & Quick Start

### Standard Installation (Zero External Dependencies)
Diff Risk Sentinel uses only Python's standard library (`ast`, `urllib`, `xml`, `concurrent.futures`) and the `git` CLI:

```bash
git clone https://github.com/heliowap/diff-risk-sentinel.git
cd diff-risk-sentinel
pip install -e .
```

Out of the box it analyzes **Python** (radon-compatible CCN) and **JavaScript / TypeScript** (`.js .jsx .mjs .cjs .ts .tsx .mts .cts`, built-in scanner).

### Polyglot Installation
To also analyze C/C++, C#, Go, Java, Kotlin, Ruby, Rust, Scala, Swift, PHP, Lua and Objective-C via [lizard](https://github.com/terryyin/lizard):
```bash
pip install -e ".[polyglot]"
```
JS/TS keeps using the built-in scanner even with lizard installed — it is markedly more accurate on real TypeScript (see [parser benchmark](#parser-benchmark)).

---

## 💻 CLI Usage

Run from anywhere inside the target git repository (or pass `--repo`):
```bash
diff-risk-sentinel                    # current branch vs. the default branch (origin/HEAD, else main/master)
diff-risk-sentinel --base origin/dev
```

`--base` accepts the same forms as git:

| `--base` | Compares |
|---|---|
| `origin/dev` | merge-base(origin/dev, HEAD) → HEAD (same as `origin/dev...HEAD`) |
| `a...b` | merge-base(a, b) → b |
| `a..b` | a → b |

Both sides are read from git objects, so uncommitted changes in the working tree are ignored. Python files that the running interpreter cannot parse (syntax errors, or syntax newer than it) are skipped and listed in `meta.unparsed_files` rather than scored — otherwise every function in them would look "removed".

### With Coverage XML
```bash
diff-risk-sentinel --base origin/dev --coverage coverage.xml
# monorepo: repeat the flag
diff-risk-sentinel --base origin/dev --coverage packages/api/coverage.xml --coverage packages/web/coverage.xml
```
Without `--coverage`, a `coverage.xml` in the current directory or at the repository root is picked up automatically (from Python, `run_sentinel(coverage=[])` disables that).

### Target Top 3 Offenders
```bash
diff-risk-sentinel --base origin/dev --top 3 --output llm_targets.json
```

### With TypeSafe Jev (opt-in, recommended when allowed)
```bash
export TYPESAFE_API_KEY="apikey_..."
diff-risk-sentinel --base origin/dev --jev
```
With `--jev`, every touched production function is ranked (the CRAP thresholds no longer gate the list) by the combined CRAP + Jev triage score, which measurably outranks CRAP alone ([benchmark](evals/jev_szz_results.md)). Cost is small: ~25M input tokens for ~14k functions in the benchmark.

> **Data:** `--jev` sends each touched production function's full source (new and previous version, up to ~48k characters each) and its diff, with file path and function name, to `api.typesafe.ai`. It is never enabled implicitly, even when the API key is set; check TypeSafe's data-handling terms (zero data retention is available) against your code's policy. Failed calls are retried on rate limits, reported in `meta.jev_failures`, and those functions rank last on the Jev dimensions.

### Dead-code scan
```bash
diff-risk-sentinel --dead-code [--jev] [--rev origin/main] [--output dead_code.json] [--top 30]
```
Scans every file at `--rev` (read from git, like the diff mode) and writes four lists, most reliable first:

| JSON key | What it lists | Measured on a production monorepo |
|---|---|---|
| `dead_code` | Production functions whose name appears nowhere else in production code or tests; stubs flagged | Team acting on the report removed **115/119 (97%)** |
| `tests_only` | Production functions only tests use — verify: dependency-injection seams and test-isolation resets are often intentional | Team removed 41/66 (62%) |
| `orphan_endpoints` | Python HTTP route handlers (FastAPI-style decorators) whose full path no file requests — frontend, proxy, script or config — with the docs that mention the path (`documented_in`) | Blind review: 42/47 removable, 4 called only from outside (runbook steps), 1 called |
| `probable_dead` (`--jev`) | Functions production mentions only through homonyms, export lists or their own file | Blind review: 6/7 at the default p ≥ 0.7 |

Framework entry points are excluded from the function lists: decorated Python functions (routes, validators, tasks), `export default`, dunders, JS/TS `constructor`s, http.server / unittest / React lifecycle hooks, `onXxx` callbacks and `getattr`-dispatched name prefixes. Runs in ~10 s on a 10k-function monorepo, endpoints included.

**Before deleting,** check what the analysis cannot see: names built at runtime, registries, methods a library calls through its base class (the one real miss in the team's review was a Presidio recognizer's `validate_result`), public APIs of libraries, and callers outside the repository (webhooks, OAuth callbacks, manual operations). **Re-run after each cleanup:** removing code leaves its helpers and endpoints orphaned (the second pass on the cleaned branch found 5 functions and 7 endpoints).

**With `--jev`**, TypeSafe Jev judges each finding from the *evidence*: the signature plus every line that mentions the name, tagged production/test/homonym, not the function body. Static findings it scores below 0.5 are dropped into `vetoed_by_jev` (framework hooks the rules do not know; every veto checked in the monorepo was correct, and the most common one — JS/TS constructors — became a static exclusion), and `probable_dead` is added. It sends signatures and mentioning lines to `api.typesafe.ai`; ~2.5 min for ~5.9k candidate functions.

**Compared with knip and vulture.** Against the repositories' own cleanup history (functions later deleted as dead, scanned at the revision before), the static scan found ~90% of leaf-dead TS/JS functions and ~50% of Python ones, tied with vulture (`--ignore-decorators` for framework decorators) on both a private monorepo and PrefectHQ/prefect. The Python misses are decorated entry points of routers or models that died, which is what `orphan_endpoints` addresses. knip reports more unused exports in TS (it follows re-exports and whole unused files). Use this tool for one precise pass over both languages, and knip/vulture when coverage matters more than precision. Details: [evals/dead_code_results.md](evals/dead_code_results.md).

### Other options
| Flag | Default | Meaning |
|---|---|---|
| `--threshold-crap` | 15.0 | Flag methods with CRAP ≥ value |
| `--threshold-ccn` | 10 | …or CCN ≥ value |
| `--threshold-delta` | 10.0 | …or \|ΔCRAP\| ≥ value |
| `--workers` | 16 | Parallel Jev requests |
| `--top-consumers` | 8 | Untouched functions to list that reference a changed identifier or literal |
| `--dead-code` / `--rev` | off / `HEAD` | Repository-wide dead-code scan instead of diff triage |
| `--top` / `--output` with `--dead-code` | 30 / `dead_code.json` | Items printed per list / report path |

Exit codes: `0` success (the output JSON is always rewritten, even with no targets), `2` git error (bad ref, not a repository).

---

## 🤖 Example Output

A 198-file, +21,681 / −2,383 epic branch, CRAP-only mode:

```text
🔍 1. Fatiando git diff contra origin/dev...origin/epic...
ℹ️  Nenhum coverage.xml encontrado. Assumindo cobertura base 0% (CRAP conservador).
📐 2. Analisando CCN e CRAP dos métodos modificados (169 arquivo(s) de código)...

────────────────────────────────────────────────────────────────────────────────
📈 MÉTRICAS AGREGADAS DO PR (OTTERWISE)
   Métodos Tocados: 817 (novos: 624, removidos: 16)
   Combined CRAP: 76698.0 → 82384.0 (Δ +5686.0)
   Average CRAP:  367.0 → 100.8 (Δ -266.1)
   🌟 Tendência: Melhoria da qualidade média por método (Average CRAP em queda).
────────────────────────────────────────────────────────────────────────────────

⚡ 3. Identificados 454 métodos acima dos limites estruturais.

================================================================================
🚨 TOP 2 OFENSORES DE RISCO NO DIFF (CRAP)
================================================================================

#1 [HIGH_RISK_REFACTOR] packages/web/src/modules/inbox/components/Inbox.tsx::Inbox (L:3495-5887)
   Complexity (CCN): 152 → 153 | Cobertura: sem dados | CRAP: 23562.0 (Δ +306.0) [High Risk (60+)]
   => SCORE COMPOSTO FINAL: 23562.0
   🛠️  Estratégia Recomendada: High complexity/CRAP (OtterWise 60+). Refactor: extract methods (seams), use early returns, or replace conditionals with lookup tables.

#2 [HIGH_RISK_REFACTOR] packages/web/src/modules/templates/components/TemplateCatalogPanel.tsx::TemplateCatalogPanel (L:259-1667)
   Complexity (CCN): 141 → 150 | Cobertura: sem dados | CRAP: 22650.0 (Δ +2628.0) [High Risk (60+)]
   => SCORE COMPOSTO FINAL: 22650.0
   🛠️  Estratégia Recomendada: High complexity/CRAP (OtterWise 60+). Refactor: extract methods (seams), use early returns, or replace conditionals with lookup tables.

📁 Metas de auditoria salvas em 'llm_review_targets.json'.
💡 Você pode acionar o LLM diretamente para tratar estes ofensores.
```

The JSON output contains `meta` (revisions, thresholds, coverage reports, `ranking` = `crap` or `crap+jev`, Jev failures, unparsed files), `otterwise_summary`, the ranked `targets` (with diff snippets, and with `--jev` the four Jev answers and `triage_score`), and `consumers` outside the diff.

---

## 🧪 Testing & Evals

### Unit Tests
```bash
pip install -e ".[test]"
python3 -m pytest
# or, with the standard library only, from the repository root:
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

### Parser benchmark
[`evals/js_parser_benchmark.py`](evals/js_parser_benchmark.py) scores the JS/TS function extractor against the **TypeScript compiler AST** (block-bodied functions; exact start/end line match):

| Codebase (files) | Built-in ranges P / R | Built-in CCN exact | lizard ranges P / R | lizard CCN exact |
|---|---|---|---|---|
| private monorepo `packages/*/src` (714) — *tuning set* | 100.0% / 100.0% | 99.8% | 45.0% / 61.5% | 88.0% |
| TS codebase A (1,336) | 100.0% / 100.0% | 100.0% | 48.3% / 49.7% | 86.9% |
| TS codebase B (197) | 100.0% / 99.8% | 100.0% | 71.8% / 81.0% | 93.1% |
| TS codebase C (343) | 99.9% / 99.9% | 100.0% | 46.3% / 75.5% | 82.6% |
| TS codebase D (187) | 100.0% / 99.5% | 98.4% | 71.7% / 80.6% | 91.8% |

Codebases A–D are unrelated projects that were not used while tuning the scanner. lizard's precision is also penalized because it reports expression-bodied arrows as functions, which the reference excludes; its recall gap is real (it frequently runs past functions containing regex or template literals). Python CCN matches `radon` exactly on 10,736/10,736 functions of the tuning codebase.

### Ranking benchmark (forward SZZ, 563 cases)
[`evals/build_szz_dataset.py`](evals/build_szz_dataset.py) turns a repository's `fix` history into ground truth (the commit that introduced the lines each fix changed, and the functions involved); [`evals/jev_szz_experiment.py`](evals/jev_szz_experiment.py) compares pre-registered rankings on it. On 1,066 fixes of a production monorepo ([full results](evals/jev_szz_results.md)):

| Share of the diff's functions read | Random | CRAP | CRAP + Jev |
|---|---|---|---|
| 5% | 35% | 37% | **52%** |
| 10% | 45% | 51% | **63%** |
| 20% | 57% | 67% | **73%** |

(share of cases where a later-fixed function is among those read). CRAP beats random; CRAP + Jev beats CRAP (95% CI of the mean-percentile gain +0.017…+0.054; hit@8 +2.6…+9.4 pp). The gain is concentrated in medium diffs (30–150 functions: top 8 finds a later-fixed function in 72% of cases vs 59% CRAP, 51% random); on the largest diffs (150+, 45 cases) the top 8 is no better than chance.

### Historical Backtesting Harness
```bash
python3 evals/historical_eval.py --repo /path/to/target-repo [--szz] [--jev]
```
Replays labeled commits (`--dataset`, format in [`evals/dataset.example.json`](evals/dataset.example.json)) with the CLI defaults (top 5, thresholds 15 / 10 / 10, no coverage) and reports every rate with a Wilson 95% confidence interval:

- **Fix-commit flag rate** (strict = `CRITICAL_SEMANTIC_AUDIT`/`HIGH_RISK_REFACTOR`; lenient adds `NEEDS_ATTENTION_TESTS`/`SEMANTIC_REVIEW`), next to a **naive baseline** that flags every commit touching code.
- **SZZ bug-introduction hit rate** (`--szz`): blames the lines each fix removed to find the commit that introduced them, replays that commit, and checks whether the later-fixed function is in the top 5 — next to what a **random ranking** of the touched methods would achieve.
- **Refactor ΔCRAP accuracy** and **false-positive rate on safe commits** (docs-only commits are reported separately: they pass trivially through the extension filter).

**Baseline (N=25 commits of a private production monorepo, CRAP-only; the dataset is not published):**

| Metric | Result | Reference |
|---|---|---|
| Fix commits flagged (strict) | 92.3% (12/13, IC95% 66.7–98.6%) | naive baseline: 100.0% (13/13) |
| **SZZ: fixed function in top 5 when introduced** | **27.3% (3/11, IC95% 9.7–56.6%)** | random ranking: 26.8% (2.95 expected hits) |
| Refactors with ΔCRAP < 0 | 75.0% (3/4, IC95% 30.1–95.4%) | — |
| Safe commits flagged | 12.5% (1/8) — the only code-bearing safe commit (test code) was flagged; the 7 docs-only commits pass trivially | — |
| Time | ~12 s for 25 commits + 11 SZZ replays | — |

With `--jev` (0 API failures) the fix-commit flag rate reaches 13/13 — the same as the naive baseline, so it shows no discriminative lift on this dataset.

**How to read this:** flagging fix commits is a weak proxy — nearly every commit touching code is flagged, especially without coverage. The SZZ row on these 11 cases is indistinguishable from random (3 hits vs 2.95 expected) — too few cases to say anything; the 563-case [ranking benchmark](#ranking-benchmark-forward-szz-563-cases) above is the reliable measurement (earlier versions of this README first claimed "about 3× random" and then "no better than random"; both came from this 11-case sample). The dataset has a single code-bearing negative, so the false-positive rate on real code changes is essentially unmeasured.

What *did* hold up in practice is the review wrapped around the ranking: in agent-driven reviews of real commits (see `SKILL.md`), running the tests at the reviewed revision, following the consumers of changed contracts and checking the deploy transition found defects that were merged and fixed later; the ranking mostly decided reading order.

> Earlier versions of this README reported "96% accuracy / 100% bug recall". Those numbers were produced by a version that read files from the working tree instead of the replayed commits and counted any `NEEDS_ATTENTION_TESTS` as a detection, and are superseded by the table above.

### Dead-code evals
- [`evals/dead_code_history_eval.py`](evals/dead_code_history_eval.py) — recall against a repository's history: functions deleted by commits that call them dead/unused/orphaned, classified at the parent revision (nothing used them / only code deleted with them did / production still did) and scanned there.
- [`evals/dead_code_experiment.py`](evals/dead_code_experiment.py) — Jev on function bodies ("does no real work?"): detects stubs (AUC 0.99), not dead code.
- [`evals/dead_code_results.md`](evals/dead_code_results.md) — all results: blind reviews, dev/test-split tuning of the Jev judge, knip/vulture comparisons, history recall, orphan endpoints, and the team's outcomes on a real report.

### Case study: a 23,000-line epic PR
See [evals/case_study_epic_pr.md](evals/case_study_epic_pr.md). A reproduction with the corrected tool at the original revisions confirms that the component containing bug #1 is the #1 structural offender, but bugs #2 and #3 sit in small functions ranked #388 and #49 of 454 (CRAP-only) — the original targets were neighbouring functions in the same files. The tool narrowed the search to the right files; it did not isolate the defects. The case study documents the details.

---

## 📄 License

MIT © Helio
