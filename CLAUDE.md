# Working in this repository

This repository is **public**. Its evaluations were run on the maintainer's private repositories, so publishing rules matter more than usual.

## Never commit or push

- Personal data (PII): names with contact data, e-mails, phone numbers, CPF/CNPJ, addresses.
- Secrets: API keys, tokens, private keys, `.env` contents.
- Anything identifying a private project: its name, commit SHAs, file paths, function/class/component names, code excerpts, commit titles, issue or PR numbers, business terms, or local paths such as `/Users/...`.

## Evaluations on private repositories

- Keep raw outputs, datasets, caches and per-case files in `evals/private/` (gitignored) or outside the repository (`/tmp`).
- Publish only aggregates ("a private production monorepo", counts, rates, confidence intervals). Examples of code patterns must be rewritten with generic names.
- Eval scripts take the repository and dataset as arguments; never hard-code private paths, SHAs or expected function names.
- Data files under `evals/` must be synthetic and listed in `.public-safety/allowed-data.txt`.

## Guardrails (keep them on)

- `git config core.hooksPath .githooks` enables the pre-commit, commit-msg and pre-push checks (`scripts/public_safety_check.py`). Do not bypass them with `--no-verify`; fix the content instead.
- Private terms go into the hashed denylist: `python3 scripts/public_safety_check.py --add-term <term>` (the term itself is never written to the repository).
- Local private repositories listed in `~/.config/public-safety/private-repos.txt` are checked for leaked commit SHAs and distinctive identifiers.
- A genuinely public string that trips a check (a library API name) goes into `.public-safety/allowlist.txt`, never a private one.
- With `TYPESAFE_API_KEY` set, the pre-push hook adds a Jev judgment (personal data; material of the private projects described in `~/.config/public-safety/private-context.txt`, which stays local). Run `python3 scripts/public_safety_check.py --all --jev` for a full audit.
- CI (`.github/workflows/public-safety.yml`) runs the regex scan on every push and PR.
