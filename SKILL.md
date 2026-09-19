---
name: diff-risk-sentinel
description: Risk triage for large git diffs, PRs, branches or commit ranges — ranks every touched function by CRAP (complexity × missing coverage) and ΔCRAP, then writes a review-and-test spec (contract, invariants, suspected defects, Given/When/Then cases) for each high-risk function. Use whenever the user asks to review, audit or assess the risk of a big or complex change ("is this safe to merge", "where should I look first", "what should I test", epic/refactor branches, diffs too large to read or feed to an LLM in full), even if they never mention CRAP or the sentinel. Also use it when the user asks where the dead code, unused functions or orphan HTTP endpoints are, or wants to slim a codebase (Python and JS/TS). Skip for small single-function fixes and docs-only changes.
---

# Diff Risk Sentinel

`diff-risk-sentinel` is a CLI (bundled in this skill's directory) that reads both sides of a git diff, computes cyclomatic complexity (CCN) and CRAP for every touched function before and after, and ranks them. Your job is to turn that ranking into a review the user can act on: a short list of functions with a concrete spec for each — what changed, what must stay true, what looks wrong, and which tests would prove it.

Keep one fact in mind throughout: **the ranking is a reading order, not a filter — and not a bug detector.** Replayed against 563 real bug-introducing commits, reading the top 10% of functions reached the one that later needed a fix in ~63% of cases with CRAP + Jev (51% CRAP alone, 45% at random): a real head start, not a guarantee, and weakest on the very largest diffs (details in `references/methodology.md`). What found real, later-fixed defects was the review around it: running the reviewed revision's tests, reading the targets *and* the small behavior-changing edits next to them, following the consumers of every changed contract, and asking what happens at deploy time. Use the ranking to organize that work and to report structural risk; never conclude "safe" because a function ranked low.

## Workflow

### 1. Pin down the diff

Work out which two revisions the user means, and say it back to them in the report.

- A PR: `gh pr view <n> --json baseRefName,headRefName` → `--base origin/<base>...origin/<head>` (fetch first).
- The current branch against the default branch: omit `--base` — the tool uses `origin/HEAD`, else `main`/`master`.
- A single commit: `--base <sha>~1..<sha>`. A range: `a..b` (exact) or `a...b` (from the merge-base).

The tool reads git objects only. Uncommitted work is invisible to it — if the user wants that reviewed, tell them and review the working-tree diff by hand.

### 2. Make sure the tool runs

```bash
diff-risk-sentinel --help >/dev/null 2>&1 || pipx install -e <this skill's directory>
```

(`pip install -e <dir>` also works.) Exit code 2 means a git problem (bad ref, not a repository); the message says which.

### 3. Run the tests of the reviewed revision

Running the tests that exercise the changed modules, at the reviewed revision, pays twice: it tells you whether the change is green at all (a test already failing in the commit is a finding in itself), and it produces real coverage, without which CRAP collapses into a pure complexity ranking (at 0% coverage every method with CCN ≥ 8 is "high risk").

Work on an isolated copy so the user's checkout is never touched, and remove it afterwards:

```bash
git -C <repo> worktree add --detach /tmp/review-<sha> <new-rev>     # or: git archive <new-rev> | tar -x -C /tmp/review-<sha>
# run only the test files related to the changed modules, with coverage, e.g.
#   pytest <tests…> --cov=<package> --cov-report=xml:/tmp/review-<sha>.xml
#   vitest/jest: coverage reporter "cobertura"
git -C <repo> worktree remove --force /tmp/review-<sha>
```

Reuse the project's existing environment (virtualenv, `node_modules`) rather than installing anything. If the tests need services you don't have (a database, network), run what you can, and say what you could not. Existing reports (`find . -name 'coverage*.xml' -not -path '*/node_modules/*'`) work too, as long as they match the reviewed revision.

### 4. Run the structural triage

```bash
diff-risk-sentinel --base <range> [--coverage /tmp/review-<sha>.xml ...] --top 20 --output /tmp/sentinel.json
```

`--top 20` leaves room for dropping test files later. This mode is local and fast (seconds even for 20k-line diffs). Keep every artifact outside the user's repository (`/tmp` or wherever the user asked) — the default `--output` writes into the current directory. If `meta.unparsed_files` is non-empty, those files were skipped (syntax newer than the tool's Python, or invalid); review them by hand.

**Use `--jev` whenever the user allows sending code to TypeSafe** (`api.typesafe.ai`; it sends each touched production function's full source and diff). It is the measured-best ranking, fast (~20 s for a 21k-line diff) and cheap. Ask once per repository if you don't know; if you cannot ask, run without it. With `--jev` every touched function is ranked (thresholds no longer gate the list), so use `--top 20` as usual and check `meta.jev_failures`.

### 5. Generate the spec worksheet

```bash
python3 <this skill's directory>/scripts/spec_worksheet.py /tmp/sentinel.json --repo . --max 8 > /tmp/review_specs.md
```

It orders production code first (test files go to a short deprioritized list at the end), prints each target's metrics (and the Jev answers when present), the exact `git show` command to read it, its diff, and an empty spec skeleton. It lists the **consumers outside the diff** the tool found — untouched functions still referencing an identifier, key format or hard-coded value the diff changed — and changed files in languages the default install does not score (Go, Java, Vue, SQL…): those were **not** triaged, so review them by hand or say so explicitly.

### 6. Fill in each spec by reading the code

For each target (typically the top 5–8 production functions), read the whole function at the new revision and enough of its callers to know its contract, then fill the skeleton:

- **What changed** — one to three bullets, in behavioral terms ("now dedups by confirmation code before dispatch"), not "lines 40–60 modified".
- **Contract** — inputs, outputs, state read or mutated, who calls it.
- **Invariants** — what must stay true (idempotency, ordering, units/timezones, nullability, bounds).
- **Risks / suspected defects** — concrete and located (`L123: falsy check drops explicit None`). "None found" is a valid answer; vague worry is not.
- **Test spec** — Given/When/Then rows that would expose each risk and pin each invariant. Search the test suite and mark whether each case already exists; the missing ones are the actionable output.
- **Prescription** — adapt the badge's strategy to this function (which branch to extract, which conditional becomes a table).

Complexity metrics don't see most semantic defects, so check each target and its neighbours for them directly. Useful questions: can a falsy-but-valid value (`0`, `""`, `None`, empty list) take the wrong branch? Does initial state collide with a legitimate value? Is a collection validated and then transformed so the validation no longer holds? Do two places that should agree on a rule (validator vs. consumer, writer vs. reader, frontend vs. backend) implement it differently? What does the new code assume about its inputs that the old code did not?

### 7. Second pass: neighbours, consumers, and the deploy transition

The ranking only sees what the diff touched, and only in proportion to complexity. Three sweeps cover what it misses:

- **Neighbours.** `git diff <old>..<new> -- <file>` for the files holding the top targets; skim the *other* changed functions there, especially small ones with changed conditionals, validation, defaults or state initialization.
- **Consumers of changed contracts.** Start from the worksheet's consumer list, then extend it by hand: for every contract the diff changes — a function signature or return shape, a key or identifier format, a persisted column or payload, an enum value, a config name — search the code *at the new revision*, including files the diff did not touch, for everything that produces or reads it (`git grep -n <name> <new-rev>`). A change that is consistent inside the diff but not with an untouched reader is a classic merged bug.
- **The deploy transition.** Ask what happens to data and in-flight work created by the old code once the new code runs: rows or cache entries in the old format, keys computed the old way, new columns left empty by a migration, jobs mid-way. Tests almost never cover this, because fixtures are created by the new code.

Add anything real as extra findings and specs.

For large or high-stakes diffs, if you can run a subagent, a second, independent review pays off: independent passes over the same diff repeatedly find different real bugs. Give it the same range and a different entry point (e.g. "start from the tests and migrations" vs. "start from the ranking"), then merge the findings.

### 8. Report

Write the report (to a file if the user wants one, otherwise in the reply) using this structure:

```markdown
# Risk triage: <old>..<new> (<what it is>)

**Scope:** N files, M code files scored, K touched methods · coverage: <reports | none> · Jev: <off | on, F failures>
**Trend:** Combined CRAP a → b (Δ), Average CRAP c → d (Δ) — one sentence on what that means here
**Not triaged:** <unscored files, uncommitted work, anything skipped>

## Findings first
<numbered list of concrete suspected defects across all specs, most severe first, each with file:line — or "no concrete defects found">

## Specs
<one section per target, from the worksheet, filled in>

## Tests to add
<grouped by test file; one table per file, only the missing cases:>
| # | Given | When | Then | Covers |
|---|---|---|---|---|
```

Keep the Given/When/Then table form in the final report, not just in the worksheet: it is what makes each case directly implementable and checkable.

Lead with findings: the user reads the top of the report. Keep metrics as supporting context, not the headline — a function being complex is not news; a function being wrong is.

## Related: dead-code cleanup

If the user asks where the dead code is, wants to shrink the codebase, or a review keeps tripping over code nothing calls, run:

```bash
diff-risk-sentinel --dead-code [--jev] --rev <branch> --output /tmp/dead_code.json
```

(`--jev` under the same data rule as above; it drops framework hooks from the findings and adds `probable_dead`.) Present the lists in order of reliability, and say which is which:

- `dead_code` — nothing references the function; a team removed 97% of these. Still confirm: runtime-built names, registries, methods a library calls through a base class, public library API.
- `tests_only` — only tests use it; about a third were kept as intentional (DI seams, test resets, documented seams awaiting wiring). Present as "verify".
- `orphan_endpoints` — HTTP routes no file in the repo requests. Check `documented_in`: a runbook step or a webhook means an outside caller.
- `probable_dead` — leads, not findings.

Removing code orphans its helpers and endpoints, so suggest one PR per package and a re-run after each. Intentional no-ops (fail-closed adapters, test doubles, interface defaults) are not dead.

## Interpreting numbers

- ΔCRAP compares the function with its own previous version; "(new)" functions contribute their full CRAP, so big new code always ranks high.
- `BENEFICIAL_REFACTOR` only appears for methods already in the acceptable zone; a refactor that shrinks a 300-CRAP method still reads `HIGH_RISK_REFACTOR`.
- Combined CRAP up with Average CRAP down usually means "more code, simpler per function".
- Badge rules, formulas, CCN counting rules, the JSON schema and the evaluation evidence are in `references/methodology.md` — read it when you need to justify a number.
