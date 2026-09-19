# CRAP vs. TypeSafe Jev on a forward-SZZ benchmark

Pre-registered experiment (`evals/build_szz_dataset.py` + `evals/jev_szz_experiment.py`), run on 2026-09-18 against the history of a private production monorepo.

- **Ground truth:** 1,066 `fix` commits → `git blame` of the production lines each removed → the commit that introduced most of them → the functions (touched by that commit) that the fix changed. 649 cases from 337 introducing commits; 563 held out from earlier exploration (the metrics below).
- **Rankings compared (fixed before running):** CRAP; each Jev question alone; CRAP + all Jev questions (mean percentile rank); random ordering.
- **Jev:** every touched production function (13,951) with full new code, previous version and diff; four questions per request; 25,184,397 input tokens, 1 failed request.
- **Caveat:** SZZ is noisy — not every `fix` fixes a bug and the blamed commit is not always where the bug was born. "Hit" means a later-fixed function is among those read, not that a reviewer would spot the defect.

## Share of cases where a later-fixed function is read, by fraction of the diff's functions read

| Read | Random | CRAP | CRAP + Jev |
|---|---|---|---|
| 5% | 35% | 37% | **52%** |
| 10% | 45% | 51% | **63%** |
| 20% | 57% | 67% | **73%** |
| 30% | 67% | 77% | **79%** |
| 50% | 80% | 87% | **90%** |

## By diff size (hit@8 = a later-fixed function in the top 8)

| Touched production functions | Cases | Random | CRAP | CRAP + Jev |
|---|---|---|---|---|
| <30 | 269 | 88% | 93% | 94% |
| 30-150 | 249 | 51% | 59% | 72% |
| >=150 | 45 | 30% | 29% | 31% |

## Mean best-percentile of a later-fixed function (1.0 = ranked first), with 95% cluster-bootstrap intervals

| Ranking | Mean percentile | Difference vs random | Difference vs CRAP |
|---|---|---|---|
| Random | 0.70 | — | — |
| crap | 0.81 | +0.080 … +0.123 | — |
| introduces_bug | 0.79 | +0.061 … +0.104 | -0.040 … +0.002 |
| edge_cases | 0.78 | +0.058 … +0.097 | -0.047 … -0.002 |
| behavior_change | 0.83 | +0.100 … +0.143 | -0.005 … +0.044 |
| semantic_risk | 0.81 | +0.083 … +0.127 | -0.017 … +0.025 |
| crap+jev | 0.84 | +0.116 … +0.158 | +0.017 … +0.054 |

CRAP + Jev vs CRAP on hit@8: +2.6 … +9.4 percentage points.

## Conclusions

- CRAP alone beats a random ordering (an earlier README claim of "no better than random" came from 11 cases and was wrong).
- No single Jev question beats CRAP; the combination of CRAP with all Jev answers does, by a small but significant margin. This is what `--jev` ranks by.
- The gain is largest on medium diffs (30–150 functions). On the largest diffs (150+, only 45 cases) the top 8 finds a later-fixed function about as often as chance; triage there still leaves most defects unread.
- Reading 10–20% of the functions covers roughly 60–75% of cases: useful to order a review, not enough to replace reading the rest.
