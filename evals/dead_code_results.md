# Dead-code detection: static references vs. TypeSafe Jev

**Summary (read first).** Sections are in the order the experiments ran; later ones revise earlier ones.

- Jev on a function's *body* detects stubs, not dead code (§1–2). Jev on the *reference evidence* works as a veto of framework hooks and adds a small probable tier (§4).
- The static scan's reliability, measured by what a team did with a real 183-item report: 97% of unreferenced functions removed, 62% of tests-only ones (§8).
- Recall against cleanup history: ~90% of leaf-dead TS/JS functions, ~50% of Python ones, tied with vulture; knip finds more unused TS exports (§6). The label-based comparison in §5 favoured this tool because the labels came from its own candidates; §6 supersedes it.
- Most of the Python dead code the function scan misses (19 of 35 in the history) is HTTP endpoints no client calls; `orphan_endpoints` finds 17/26 of those deletions at 42/47 blind-review precision (§7).
- Tried and rejected: "dead container" rules (§6), a code-first Jev state (§4).

Run on 2026-09-18 over every production function (10,201) of a private production monorepo at one revision (`evals/dead_code_experiment.py` + blind manual review of random samples).

## 1. Jev on each function in isolation (code only, no repository context)

| Question | Separates… | AUC |
|---|---|---|
| "Does this function do no real work (stub / no-op / disabled)?" | static stubs (136) from the rest | **0.99** (94% of stubs at p ≥ 0.5) |
| same | functions with no references anywhere | 0.64 |
| "Does it look like a leftover of a removed feature?" | static dead code | 0.59 |

Blind review of 30 random functions Jev flagged beyond the static labels: 4 removable, 17 intentional no-ops (fail-closed adapters, test doubles, interface defaults, simulators), 9 doing real work (mostly flagged by the "leftover" question). Jev recognizes *no-op code* well, but "does nothing" is not "dead": most no-ops are part of the design, and dead-by-contract code (e.g. an overlay waiting for a field no producer emits) scores near 0.

## 2. Static "no production caller" vs. Jev + static

Blind review (reviewers did not know the group), random samples:

| Set | Size | Removable |
|---|---|---|
| No production caller **and** Jev "no real work" ≥ 0.7 | 19 | 12/19 (63%) |
| No production caller, Jev says it does work | 213 | 14/20 (70%) |

Jev adds no precision; the reference analysis is what finds dead code. Its false positives were framework hooks (`do_GET`, `log_message`), dynamic dispatch (`getattr(self, f"_tool_{name}")`) and test support files outside test directories.

## 3. `diff-risk-sentinel --dead-code` (static, with those exclusions)

- Whole repository in ~3 s; 197 functions reported (131 unreferenced, 66 used only by tests).
- Held-out blind review of 15 random reported functions (not seen while designing the exclusions): **13/15 removable (87%)**. The two misses — a React class lifecycle method and an `onXxx` callback passed to a library — led to two more generic exclusions.
- Known limit: code reachable only by names built at runtime, registries or external callers (webhooks, cron, other services) can still appear; review before deleting.

## 4. Jev as a judge of the *evidence* (`--dead-code --jev`)

Section 1 asked Jev about the function body. Whether a function is dead depends on how the rest of the repository mentions it, so the judge now sees no body: its state is the signature/decorators, a sentence with the counts (production mentions in other files / same file, test mentions, same-named definitions) and every mentioning line tagged `PRODUCTION|TEST`, `SAME FILE|OTHER FILE`, `DEFINES ANOTHER FUNCTION WITH THE SAME NAME` (`deadcode_evidence.judge_state`). One Noul: "could it be deleted without changing what runs in production?".

Labels: 154 functions blind-reviewed in earlier rounds (83 dead), split by hash into a dev half (used to design state, questions and gate) and a test half evaluated once with the pre-registered rule.

| Dev half (76, 39 dead) | Precision | Recall |
|---|---|---|
| First design: body + nested reference list + "how is it reached?" choice, p ≥ 0.5 | 76% | 33% |
| Evidence-first state, p ≥ 0.5 | 89% | 79% |
| Static exclusions (gate) + evidence-first, p ≥ 0.5 | 97% | 77% |

| Test half (78, 44 dead), evaluated once | Precision | Recall |
|---|---|---|
| Static: gate + no production mention | 100% | 80% |
| Gate + Jev p ≥ 0.3 (pre-registered primary) | 87% | 93% |
| Gate + Jev p ≥ 0.5 | 95% | 82% |

The labels were drawn mostly from this tool's own candidates, so the recall figures favor it; the repository-wide run below is the fairer check.

**Repository-wide (5,863 candidate functions judged in ~2.5 min at 32 workers, 0 failures).** Blind review, reviewers not told the group:

| Group | Removable |
|---|---|
| Static findings Jev did not veto (random 20) | **20/20** |
| Findings only Jev proposes (production mentions exist, but only homonyms, export lists or the own file), p ≥ 0.5 | 27/34 (79%) |
| same, p ≥ 0.7 | 6/7 |

Jev vetoed 14 of 197 static findings, all correctly: 12 JS/TS `constructor`s (run by `new`, never named — now also a static exclusion) and two Vite plugin hooks. At high thresholds Jev reproduces the static rule; its value is (a) vetoing framework hooks the static rules do not know and (b) a smaller, less precise "probable" tier. Precision is preferred over coverage here (a false positive costs review time), so the default output is the static rule after the veto, and the probable tier (p ≥ 0.7) is listed separately.

## 5. Versus knip and vulture (test half, each tool on its own scope) — superseded by §6

| Scope | Tool | Precision | Recall |
|---|---|---|---|
| TS (35 labels, 29 dead) | knip 5 | 94% | 59% |
| | sentinel static | 100% | 76% |
| | sentinel gate + Jev ≥ 0.5 | 96% | 79% |
| Python (34 labels, 13 dead) | vulture (`--min-confidence 60`, framework decorators ignored) | 76% | 100% |
| | sentinel static | 100% | 92% |
| | sentinel gate + Jev ≥ 0.5 | 92% | 92% |

Same caveat on label provenance: repository-wide, knip reports more dead exports than this tool (it follows re-exports and whole unused files), and vulture finds unused Python methods this tool misses when a homonym exists. One tool for both languages at ≥ 95% precision is what this mode offers; knip/vulture remain complementary for coverage.

## 6. Recall against the repositories' own history (`evals/dead_code_history_eval.py`)

Ground truth independent of this tool: production functions deleted by commits whose message says the code was dead/unused/orphaned (release squashes excluded; moves and renames dropped), classified at the parent revision as **direct** (nothing in production mentioned them), **transitive** (mentioned only by code the same commit deletes) or **live** (production still used them — a feature was retired). The scan is run at each parent. vulture 2.x ran on the same parents (`--min-confidence 60`, tests excluded) with framework decorators ignored, the configuration comparable to this tool's exclusions.

| Repository | Deleted functions (direct / transitive / live) | sentinel, direct | vulture, direct Python |
|---|---|---|---|
| private monorepo (61 commits) | 1,139 (140 / 560 / 439) | **98/140** (Python 31/66, TS/JS 67/74) | 31/66 |
| PrefectHQ/prefect, held out (16 commits) | 44 (10 / 8 / 26) | 6/10 (Python 3/6, TS 3/4) | 3/6 |

- On functions that were leaf-dead, recall is ~90% in TS/JS and ~50% in Python, tied with vulture in both repositories. Every Python miss in the private repo (35/35) and 2 of 3 in Prefect are decorated entry points (HTTP routes of routers no client called, pydantic validators of unused models, CLI commands) or dunders; vulture with decorators ignored misses the same ones. One Prefect miss is a function behind a dependency-injection decorator (`@inject_db`), which the "any decorator is an entry point" rule treats as registered.
- Neither tool finds transitive chains (1 of 568) or code retired together with its callers: most of what teams delete as "dead" was only dead once its caller went.
- Tried and rejected: reporting entry points whose class/module nothing else names ("dead container"). It recovered 1 more of the 140 direct deletions, and a blind review of its 8 repo-wide findings found 6 removable (75%), below the precision bar.
- Prefect's cleanup commits rarely say "unused", so its sample is small (n = 10); treat it as a sanity check, not an estimate.

## 7. Orphan HTTP endpoints (`--dead-code` → `orphan_endpoints`)

Most Python functions the history says were dead but no name-based tool finds are route handlers of endpoints no client called any more (section 6). `endpoints.py` searches each route's full path (same-file `...Router(prefix=...)` resolved) in every file that can call it; tests, docs, generated API specs, route decorators and the handler's own proxy call are not callers. It runs in ~7 s on the private monorepo.

| Check | Result |
|---|---|
| Recall, private monorepo history (route handlers deleted by cleanup commits, at the parent) | 17/26 (direct class 13/18) |
| Blind review, all 47 current findings (reviewers told nothing about the tool) | 42 orphan · 4 external (manual ops endpoints named only in runbooks) · 1 called |
| same, only samples drawn after the last rule change (held out) | 27/31 orphan · 30/31 have no caller in the repository |
| PrefectHQ/prefect | 0 of 193 judged routes reported: its SDK keeps a catalog of every server route. No false positives; recall not measurable there |

Rules came from the first review sample's misses (clients interpolating the last segment, handlers mounted on several paths, facades proxying the same path, router prefixes) and each is covered by a test. External callers — provider webhooks, OAuth callbacks, manual ops endpoints — cannot be seen; docs mentioning the path are listed with each finding (`documented_in`), which is where all 4 external ones were described.

## 8. Prospective: what a team did with a real report

The static scan (with the Jev veto) of the private monorepo was opened as an issue with 183 functions. Within a day the team (an engineer working with a coding agent) merged 7 PRs that verified every item and removed or kept it, with written reasons. Measured on the target branch afterwards (a removal counts only if the name is not defined anywhere else, i.e. not a rename or move):

| Finding | Removed |
|---|---|
| No reference anywhere | **115/119 (97%)** |
| Referenced only by tests | 41/66 (62%) |

Reasons given for the 29 kept: dependency-injection seams and test-isolation resets (`set_*`, `reset_*`, fakes), surfaces documented as awaiting a deploy or wiring, public API of a live module (state machine, provider factory), and one real miss — a method a library calls through its base class (a Presidio recognizer's `validate_result`), which neither the static rules nor the Jev veto caught.

Consequences in the tool: tests-only functions are now listed apart (`tests_only`, "verify") instead of in `dead_code`. Re-running on the cleaned branch found a second wave — 5 helpers and 7 HTTP endpoints left without their only caller by the removals (one of them had been noted by hand in a PR as a follow-up) — so the scan is meant to be re-run after each cleanup.
