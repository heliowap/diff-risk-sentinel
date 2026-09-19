# Case study: three real bugs in a 23,000-line epic PR

An integration PR in a private production monorepo (Python backend + React/TypeScript frontend; 198 files, 76 commits, +21,681 / −2,383 lines) had passed several rounds of human and AI review. Full-diff LLM review was not an option: the GitHub API refuses diffs above 20,000 lines (`HTTP 406`), and a 20k-line prompt dilutes attention across boilerplate.

An early version of Diff Risk Sentinel ranked the touched functions, the reviewers read the top targets and their files, and found three real defects. Identifiers below are renamed; the code shapes are the real ones.

## The three bugs

### 1. Initial state that collides with a legitimate value (React)

```typescript
const resolvedUnitRef = useRef<number | null>(null);

useEffect(() => {
  const unitId = selectedUnitId;            // null when no unit is selected
  if (unitId === resolvedUnitRef.current) return;   // null === null → never loads
  void reloadTemplates().then(() => {
    if (selectedUnitIdRef.current === unitId) resolvedUnitRef.current = unitId;
  });
}, [selectedUnitId, reloadTemplates]);
```

"Not resolved yet" and "resolved for no unit" were both `null`, so the first load never ran for users without a selected unit. Fix: start from `undefined`.

### 2. A truthiness check that drops an explicit clear (Python)

```python
def apply_schedule(rule, effective):
    rule.time_of_day = effective["time_of_day"]
    if effective["quiet_hours_start"]:          # None means "clear it" — silently ignored
        rule.quiet_hours_start = effective["quiet_hours_start"]
```

A request to clear a field (`None`) left the old value in the database. Fix: test for the key's presence in the update, not the value's truthiness.

### 3. Validation on one collection, work on a filtered one (Python)

```python
if len(buttons) not in {2, 3}:
    unsupported.append("button_count")
else:
    rows = [normalize(b) for b in buttons if isinstance(b, dict)]   # may drop malformed items
    errors = check_button_mix(rows)                                  # assumes ≥ 2 rows
```

Two items with one malformed passed the count check and reached a validator that assumes at least two valid rows.

## What the ranking actually did

The original run came from v0.1.0, which read function bodies from the working tree instead of the diffed revisions. Reproduced with v0.2.0 at the original revisions (CRAP only, no coverage; 817 touched methods, 454 above thresholds, 1.4 s):

| Bug | Function with the defect | CCN | Rank (of 454) | Enclosing target |
|---|---|---|---|---|
| 1 | effect callback inside the inbox component | 2 | #453 | the component itself (CCN 153): **#1** |
| 2 | schedule applier | 3 | #388 | none (v0.1.0 had flagged a neighbouring function) |
| 3 | button parser | 13 | #49 | none (v0.1.0 had flagged the calling parser, now #36) |

- **What holds up:** the component containing bug 1 was the PR's top structural offender, so a reviewer following the ranking opened the right file first.
- **What does not:** bugs 2 and 3 sat in small functions *next to* the targets. The tool narrowed the search to the right files; it did not isolate the defects, and no multiplier on CRAP can lift a CCN-3 function at 0% coverage (CRAP 12) into a top list.
- **The lesson carried into the skill:** read the targets *and* the small behavior-changing edits around them, and ask the three questions these bugs answer. Does initial state collide with a legitimate value? Can a falsy-but-valid value take the wrong branch? Is a collection validated and then transformed so that the validation no longer holds?

The systematic version of this check, whether the ranking points at the function that later needed a fix, is the forward-SZZ benchmark in [jev_szz_results.md](jev_szz_results.md).
