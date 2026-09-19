# Do atomic facts about a change, or narrow bug-pattern checks, locate later-fixed code?

`evals/review_signals_experiment.py`, run on 2026-09-19 on the dev half (306 cases, 180 introducing commits, 5,929 touched production functions) of the forward-SZZ dataset of a private production monorepo. For every function, TypeSafe Jev answered over its diff and new code: one Choice (kind of change), seven Nouls about observable facts (condition, default/initial value, validation, persisted format, error handling, concurrency, external contract) and three Nouls for bug patterns seen in real merged defects (falsy-but-valid value, initial state equal to a legitimate value, a check that no longer holds after a transform). 143 s for 5,929 calls, no failures.

| Ranking (best percentile of a later-fixed function; hit@8) | pct | hit@8 |
|---|---|---|
| random | 0.704 | 0.65 |
| each fact, alone | 0.725–0.742 | 0.58–0.63 |
| each pattern check, alone | 0.742–0.775 | 0.59–0.68 |
| CRAP | 0.803 | 0.71 |
| Jev `behavior_change` alone (existing question) | 0.836 | 0.74 |
| **CRAP + Jev (current triage)** | **0.848** | **0.78** |
| current + max pattern / + sum of facts / + "not cosmetic" | 0.837–0.841 | 0.75–0.76 |

- Facts barely beat random: they describe what kind of change it is, and defects occur in every kind. As alarms their lift over the 23.7% base rate is 0.75–1.13.
- Pattern checks are conservative: at p ≥ 0.7 they fire on 10, 2 and 2 of 5,929 functions — too rare to help or to measure.
- Adding either to the current triage lowers it slightly (95% CIs of the difference at or below zero), so no composition was carried to the test half.

Conclusion: per-function judgments are saturated as a way to *locate* defects; the current CRAP + Jev ranking stays. Jev's remaining room in review is in judgments that bring evidence the function alone does not have — a consumer outside the diff that relies on the old format, the deploy transition, whether a reviewer's claim matches the cited code — and in routing, which this benchmark cannot score.
