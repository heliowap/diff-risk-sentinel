# Methodology reference

Read this when you need to explain a score or a badge to the user, or when a number in the report looks surprising.

## Formulas

- **CRAP** = CCN² × (1 − coverage)³ + CCN
- **Triage score** (with `--jev`) = mean of the function's percentile ranks, within the diff, on CRAP and on four Jev answers about it (`introduces_bug`, `edge_cases` — probabilities; `behavior_change`, `semantic_risk` — 0–3 scores). Jev sees the full new function, its previous version and the diff inside it. Without `--jev`, targets are ranked by CRAP.
- **Composite risk** = CRAP × (1 + Jev semantic risk) — kept for the badges below; it no longer orders the targets.
- **ΔCRAP** (per method) = CRAP(after) − CRAP(before). The method is matched by qualified name (`Class.method`, `outer.inner`, `Component.<anonymous>#2`) in the base revision; both sides use the same coverage, so ΔCRAP isolates the complexity change. A new method contributes its full CRAP.
- **Combined CRAP** = sum over touched methods, before → after. Removed methods count on the before side, scored with the file's current coverage (0% for deleted files), so renaming a tested function is neutral.
- **Average CRAP** = mean per method, before → after. Combined up + Average down means the PR added code but improved quality per method.

CCN counts: Python follows radon (if/elif, loops and their `else`, each `except`, `try … else`, comprehensions and their `if`s, boolean operators, ternaries, `assert`, non-wildcard `match` cases; `with` does not count). JS/TS counts `if`, loops, `case`, `catch`, `&&`, `||`, `??`, and ternaries, including inside template `${}` expressions. Expression-bodied arrows (`x => x + 1`) are not separate functions: their branches count toward the enclosing function. Block-bodied callbacks (`useEffect(() => { … })`) are separate functions named `Parent.<anonymous>`.

## Zones

| CRAP | Zone | Meaning |
|---|---|---|
| 0–30 | Acceptable | complexity and tests in balance |
| 30–60 | Needs attention | add tests to pull CRAP under 30 |
| 60+ | High risk | tests alone will not fix it; restructure |

Without coverage every method is at 0%, so CRAP = CCN² + CCN and CCN ≥ 8 already means "high risk". In that situation the ranking is effectively a complexity ranking.

## Badges (first match wins)

| # | Condition | Badge | Prescription |
|---|---|---|---|
| 1 | Jev ≥ 2.0 | `CRITICAL_SEMANTIC_AUDIT` | Audit business logic, unhandled edge cases, state mutations |
| 2 | CRAP ≥ 60 or CCN ≥ 20 | `HIGH_RISK_REFACTOR` | Extract methods, early returns, lookup tables |
| 3 | 30 ≤ CRAP < 60 | `NEEDS_ATTENTION_TESTS` | Add tests until CRAP < 30 |
| 4 | 1.0 ≤ Jev < 2.0 | `SEMANTIC_REVIEW` | Structurally fine; review the behavior change |
| 5 | ΔCRAP < 0 | `BENEFICIAL_REFACTOR` | Quality improved; fast-pass |
| 6 | otherwise | `ACCEPTABLE_LOW_RISK` | Low risk |

With Jev off, rows 1 and 4 never apply.

## JSON report (`--output`)

```json
{
  "meta": {
    "base": "origin/main", "old_rev": "<sha>", "new_rev": "<sha>",
    "files_in_diff": 26, "code_files_analyzed": 21, "unparsed_files": [],
    "coverage_reports": ["coverage.xml"],
    "thresholds": {"crap": 15.0, "ccn": 10, "delta_crap": 10.0},
    "jev_enabled": true, "jev_failures": 0, "ranking": "crap+jev"
  },
  "otterwise_summary": {
    "total_methods": 64, "new_methods": 12, "removed_methods": 3,
    "combined_crap_before": 900.0, "combined_crap": 1020.0, "combined_delta_crap": 120.0,
    "average_crap_before": 16.4, "average_crap": 13.8, "average_delta_crap": -2.6
  },
  "targets": [{
    "file": "src/svc.py", "function": "Service.pay", "lines": "40-95", "changed_lines": 12,
    "ccn": 14, "ccn_before": 9, "coverage": 0.0, "coverage_known": false,
    "crap": 210.0, "crap_before": 90.0, "delta_crap": 120.0,
    "diff_snippet": "...",
    "jev_introduces_bug": 0.62, "jev_edge_cases": 0.31, "jev_behavior_change": 2.4, "jev_semantic_risk": 2.1,
    "jev_confidence": 0.7, "triage_score": 0.91,
    "composite_risk": 651.0, "action": "CRITICAL_SEMANTIC_AUDIT", "strategy": "..."
  }],
  "consumers": [{
    "file": "src/scope.py", "function": "unit_scope", "lines": "4-6",
    "tokens": ["reminder_", "north"], "hits": 2
  }]
}
```

Without `--jev`, targets are the methods above a threshold (CRAP ≥ 15, CCN ≥ 10, or |ΔCRAP| ≥ 10), sorted by CRAP. With `--jev`, every touched method is ranked by `triage_score` (test files have no Jev answer and sink). Both are cut at `--top`. The summary covers every touched method. `consumers` are untouched functions that reference identifiers or literal fragments the diff changed or removed.

## What the evidence says

Measured on 25 labeled commits of a production monorepo (`evals/`):

- Flagging bug-*fix* commits does not discriminate: a baseline that flags every commit touching code does as well.
- Ranking benchmark (forward SZZ, 563 held-out cases from 1,066 fixes; `evals/jev_szz_results.md`): reading the top 10% of a diff's functions reaches a later-fixed function in 45% of cases at random, 51% with CRAP, **63% with CRAP + Jev**. CRAP beats random; CRAP + Jev beats CRAP (small but significant); no single Jev answer beats CRAP. Gains concentrate in medium diffs (30–150 functions); in the largest diffs (150+) the top 8 is no better than chance.
- The 11-case SZZ row of the older harness (3/11 vs 2.95 expected at random) is too small to conclude anything.
- Agent reviews of two real commits whose defects were merged and fixed later found 3 of the 6 later-fixed defects — with or without this skill. The finds came from reading, running the revision's tests, and tracing consumers of changed contracts, not from the ranking.
- On a 23k-line epic PR, the component containing one of three real bugs ranked #1, but the other two bugs sat in small functions (CCN 3 and 13) ranked #388 and #49, in the same files as higher-ranked targets.

Hence the workflow: use the ranking to decide reading order — it beats chance, especially with Jev — but never to declare the unread part safe; then check consumers, neighbours and the deploy transition before concluding.

## Dead-code scan (`--dead-code`)

Scans every file at `--rev` (default `HEAD`) and writes `dead_code.json`:

```json
{
  "meta": {"version": "0.3.0", "rev": "<sha>", "functions_reported": 119, "jev_enabled": true,
           "jev_judged": 5863, "jev_failures": 0},
  "dead_code": [{"file": "app/a.py", "function": "helper", "lines": "8-9", "status": "unreferenced",
                 "test_references": 0, "stub": false, "jev_p_removable": 0.93}],
  "tests_only": [{"...": "same fields, status tests_only, test_references > 0"}],
  "orphan_endpoints": [{"file": "api/routes.py", "function": "rebuild", "lines": "5-7", "method": "post",
                        "path": "/reports/rebuild", "documented_in": ["docs/runbook.md"]}],
  "vetoed_by_jev": ["with --jev: static findings Jev scored < 0.5"],
  "probable_dead": [{"file": "...", "function": "...", "lines": "...", "jev_p_removable": 0.86,
                     "reference_counts": {"production_other_files": 1, "production_same_file": 0,
                                          "tests": 2, "other_definitions_same_name": 1}}]
}
```

- **`dead_code`** — the name appears in no other production or test file, and nowhere else in its own file. Word-level matching: a homonym used elsewhere hides a dead function (false negative), never the reverse.
- **`tests_only`** — only tests mention it. Weaker: dependency-injection seams (`set_*`), test-isolation resets (`reset_*`) and fakes are often deliberate.
- **Excluded entry points** — decorated Python functions, `export default`, dunders, JS/TS `constructor`, http.server/unittest/alembic/React lifecycle hooks, `main`, `onXxx` callbacks, names matching a `getattr(obj, f"prefix{…}")` dispatch prefix, test-support files (`conftest`, `setupTests`, `test-utils`), migrations.
- **`orphan_endpoints`** — route decorators `@<router>.get/post/put/patch/delete/api_route/websocket/route("path")`, with the same-file `...Router(prefix=...)`. The full path is searched in every file that could request it; not callers: tests, docs (`docs/`, `*.md`, `*.txt`…), OpenAPI/Swagger specs, route decorators, and the handler's own body (a facade proxying the same path downstream). Path parameters match anything a client interpolates; the last segment may be interpolated too (`/catalog/{id}/{action}`) when another literal anchors it; the client URL must end where the route ends. Paths without a distinctive literal (`/`, `/{id}`) are skipped. Prefixes given only at `include_router(..., prefix=)` are not resolved, which makes matching looser (fewer findings), not stricter.
- **`--jev`** — Jev receives, per function, the first lines of its signature (decorators included), a sentence with the counts, and every mentioning line tagged `PRODUCTION|TEST`, `SAME FILE|OTHER FILE`, `DEFINES ANOTHER FUNCTION WITH THE SAME NAME`; one Noul: "could it be deleted without changing what runs in production?". Static findings with p < 0.5 are vetoed; candidates outside the static lists (≤ 3 production mentions in other files, not entry points) with p ≥ 0.7 become `probable_dead`. Failed judgments keep the static finding.

### What the evidence says (details in `evals/dead_code_results.md`)

- A team acting on a 183-item report removed 97% of `unreferenced` and 62% of `tests_only` functions; the one real miss was a method a library calls through its base class.
- Recall against cleanup history: ~90% of leaf-dead TS/JS functions, ~50% of Python ones (decorated entry points of dead routers/models), tied with vulture on the same ground truth; transitive chains (code dead only because its caller was deleted in the same commit) are not found — re-run after each cleanup.
- Orphan endpoints: 42/47 removable in a blind review, 4 external (runbook steps), 1 called; 17/26 of the route handlers later deleted as dead would have been flagged.
