#!/usr/bin/env python3
"""
Turns a diff-risk-sentinel JSON report into a Markdown review-spec worksheet.

For every ranked target (production code first, test files last) it prints the metrics,
the diff snippet and an empty spec skeleton (contract, invariants, risks, test cases) for
the reviewer to fill in after reading the code. Also lists changed files in languages the
sentinel may not have scored, so a quiet report is not mistaken for a safe diff.

Usage:
    python3 scripts/spec_worksheet.py llm_review_targets.json [--max 8] [--repo .] > review_specs.md
Standard library only.
"""

import argparse
import json
import os
import re
import subprocess
import sys

TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|specs?|e2e|fixtures?)/"
    r"|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$"
)
SCORED_BY_DEFAULT = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
OTHER_CODE = (
    ".go", ".java", ".kt", ".kts", ".rb", ".rs", ".scala", ".swift", ".php", ".cs", ".c", ".h",
    ".cc", ".cpp", ".cxx", ".hpp", ".lua", ".m", ".vue", ".svelte", ".dart", ".ex", ".exs", ".sql",
)
MAX_SNIPPET_LINES = 60


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def order_targets(targets):
    """Production code keeps its rank order; test files go last."""
    prod = [t for t in targets if not is_test_path(t["file"])]
    tests = [t for t in targets if is_test_path(t["file"])]
    return prod, tests


def unscored_files(repo, old_rev, new_rev):
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", old_rev, new_rev],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return [p for p in out.splitlines() if p.endswith(OTHER_CODE) and not p.endswith(SCORED_BY_DEFAULT)]


def fmt_ccn(t):
    return f"{t['ccn_before']} → {t['ccn']}" if t.get("ccn_before") is not None else f"{t['ccn']} (new)"


def fmt_cov(t):
    return f"{int(t['coverage'] * 100)}%" if t.get("coverage_known") else "no data"


def snippet(text):
    lines = (text or "").splitlines()
    if len(lines) > MAX_SNIPPET_LINES:
        hidden = len(lines) - MAX_SNIPPET_LINES
        lines = lines[:MAX_SNIPPET_LINES] + [f"... ({hidden} more diff lines — read the full function)"]
    return "\n".join(lines)


def render(report, max_targets, repo, max_consumers=8):
    meta = report.get("meta", {})
    summary = report.get("otterwise_summary", {})
    prod, tests = order_targets(report.get("targets", []))
    out = []
    w = out.append

    w(f"# Review specs — `{meta.get('base', '?')}`")
    w("")
    w(f"- Revisions: `{str(meta.get('old_rev', '?'))[:12]}` → `{str(meta.get('new_rev', '?'))[:12]}`")
    w(f"- Files in diff: {meta.get('files_in_diff', '?')} · code files scored: {meta.get('code_files_analyzed', '?')}")
    w(f"- Touched methods: {summary.get('total_methods', '?')} (new {summary.get('new_methods', '?')}, "
      f"removed {summary.get('removed_methods', '?')})")
    w(f"- Combined CRAP: {summary.get('combined_crap_before', '?')} → {summary.get('combined_crap', '?')} "
      f"(Δ {summary.get('combined_delta_crap', '?')}) · Average CRAP: {summary.get('average_crap_before', '?')} → "
      f"{summary.get('average_crap', '?')} (Δ {summary.get('average_delta_crap', '?')})")
    if meta.get("coverage_reports"):
        w(f"- Coverage: {', '.join(meta['coverage_reports'])}")
    else:
        w("- Coverage: **none** — CRAP ≈ CCN² + CCN, so the ranking is a complexity ranking")
    if meta.get("jev_enabled"):
        w(f"- Ranking: CRAP + Jev (mean percentile rank; {meta.get('jev_failures', 0)} failed Jev calls)")
    else:
        w("- Ranking: CRAP only (Jev off)")

    if repo and meta.get("old_rev") and meta.get("new_rev"):
        missing = unscored_files(repo, meta["old_rev"], meta["new_rev"])
        if missing:
            w(f"- ⚠️ Changed files the default install does not score ({len(missing)}): "
              + ", ".join(f"`{p}`" for p in missing[:15]) + (" …" if len(missing) > 15 else ""))
            w("  Review these by hand, or install the `polyglot` extra (lizard) and re-run.")
    w("")

    for idx, t in enumerate(prod[:max_targets], 1):
        start, _, end = t["lines"].partition("-")
        w(f"## T{idx} · `{t['file']}::{t['function']}` (L{t['lines']}) — `{t['action']}`")
        w("")
        rank = f"triage {t['triage_score']:.2f}" if t.get("triage_score") is not None else f"composite {t['composite_risk']}"
        w(f"CCN {fmt_ccn(t)} · coverage {fmt_cov(t)} · CRAP {t['crap']} (Δ {t['delta_crap']:+}) · {rank}")
        if t.get("jev_semantic_risk") is not None:
            w(f"Jev: bug {int(t['jev_introduces_bug'] * 100)}% · edge cases {int(t['jev_edge_cases'] * 100)}% · "
              f"behavior change {t['jev_behavior_change']:.1f}/3 · risk {t['jev_semantic_risk']:.1f}/3")
        w("")
        w(f"Read: `git show {str(meta.get('new_rev', 'HEAD'))[:12]}:{t['file']} | sed -n '{start},{end}p'`")
        w("")
        w("<details><summary>Diff inside this function</summary>")
        w("")
        w("```diff")
        w(snippet(t.get("diff_snippet")))
        w("```")
        w("</details>")
        w("")
        w("**What changed:** TODO")
        w("")
        w("**Contract:** inputs · outputs · state it reads/mutates · callers affected — TODO")
        w("")
        w("**Invariants:** TODO")
        w("")
        w("**Risks / suspected defects:** TODO (cite lines; write \"none found\" if none)")
        w("")
        w("**Test spec:**")
        w("")
        w("| # | Given | When | Then | Exists? |")
        w("|---|---|---|---|---|")
        w("| 1 | TODO | TODO | TODO | TODO |")
        w("")
        w(f"**Prescription:** {t.get('strategy', '')}")
        w("")

    if len(prod) > max_targets:
        w(f"_{len(prod) - max_targets} more production targets in the report were not expanded._")
        w("")
    consumers = report.get("consumers", [])[:max_consumers]
    if consumers:
        w("## Consumers outside the diff")
        w("")
        w("Untouched functions that reference identifiers or literal fragments this diff changed or "
          "removed. Check each still agrees with the new contract (formats, keys, hard-coded values).")
        w("")
        rev = str(meta.get("new_rev", "HEAD"))[:12]
        for c in consumers:
            start, _, end = c["lines"].partition("-")
            w(f"- `{c['file']}::{c['function']}` (L{c['lines']}) — uses {', '.join(f'`{t}`' for t in c['tokens'][:5])}"
              f" · `git show {rev}:{c['file']} | sed -n '{start},{end}p'`")
        w("")

    if tests:
        w("## Test files in the ranking (deprioritized)")
        w("")
        for t in tests:
            w(f"- `{t['file']}::{t['function']}` — CCN {fmt_ccn(t)}, CRAP {t['crap']}")
        w("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", help="JSON written by diff-risk-sentinel --output")
    parser.add_argument("--max", type=int, default=8, help="Production targets to expand (default: 8)")
    parser.add_argument("--repo", default=".", help="Repository used to list unscored files (default: .)")
    parser.add_argument("--consumers", type=int, default=8, help="Consumers outside the diff to list (default: 8)")
    args = parser.parse_args()
    with open(args.report, encoding="utf-8") as fh:
        report = json.load(fh)
    repo = args.repo if os.path.isdir(args.repo) else None
    sys.stdout.write(render(report, args.max, repo, max_consumers=args.consumers) + "\n")


if __name__ == "__main__":
    main()
