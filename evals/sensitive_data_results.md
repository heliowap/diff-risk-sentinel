# Can TypeSafe Jev keep personal data and private material out of a public repository?

`scripts/public_safety_check.py` blocks formats with regular expressions (CPF/CNPJ, e-mails, phone numbers, secrets, local paths, hashed private terms) and, on the maintainer's machine, commit SHAs and distinctive identifiers of their private repositories. Neither sees meaning: a person's name next to a diagnosis, a paragraph describing a private system's rules. `scripts/sensitive_judge.py` asks Jev about the text being published.

Run on 2026-09-19 with `evals/sensitive_data_eval.py` on a private labeled set (592 chunks, kept out of the repository), split by hash into a dev half (questions and thresholds chosen there) and a test half evaluated once with the pre-registered rule.

| Category | Label | Source |
|---|---|---|
| Personal data | must block | Synthetic, realistic records: patients, chat logs, CSV/JSON rows, notes; with and without regex-detectable formats |
| Private prose | must block | Paragraphs from the maintainer's private repositories' docs |
| Private code | must block | Functions from those repositories |
| Known leaks | must block | Text removed from this repository before it was published |
| This repository, didactic placeholders, an open-source project's docs and code | publishable | Current files; `example.com`/Alice-and-Bob style examples; PrefectHQ/prefect |

## Questions that did not work (dev half)

- "Would publishing this reveal non-public information about a private company?" — Jev has no way to know what is private: 17% of private prose flagged.
- "Does this describe a specific product other than this tool?" — raises recall on private prose to 59% but flags 45% of the open-source project's docs (they describe another product too).

What works is telling Jev what the private projects are: a short local description of their domains (`~/.config/public-safety/private-context.txt`, never committed), and asking whether the text is material of one of them.

## Result on the held-out half (292 chunks)

| Rule | Precision | Recall |
|---|---|---|
| Regular expressions (portable; what CI runs) | 100% | 41% |
| + local index of private repositories | 100% | 62% |
| Jev alone (personal data ≥ 0.7 or private project ≥ 0.8) | 100% | 48% |
| **All layers (deployed in the pre-push hook)** | **100%** | **74%** |

By category, all layers: personal data 64/64 (regex alone 49/64 — Jev catches names with health, family or complaint details that have no fixed format), private prose 29/60, private code 31/44, known leaks 14/21; 0 of 103 publishable chunks flagged. A full audit of this repository with Jev (`--all --jev`) reports nothing.

## Limits

- Recall on private prose and code is about half: generic-looking engineering text from a private project is indistinguishable from any other. Aggregates-only discipline (CLAUDE.md) remains the main protection; the checks catch slips.
- Jev sees the text being published (it goes to `api.typesafe.ai`); the layer is opt-in (`--jev`, or the pre-push hook when `TYPESAFE_API_KEY` is set). CI runs without it.
