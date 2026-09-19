#!/usr/bin/env python3
"""
Benchmarks the JS/TS function extractors (built-in scanner and, if installed, lizard)
against the TypeScript compiler AST as ground truth.

Ground truth = every block-bodied function-like node (function declarations/expressions,
methods, accessors, constructors, arrow functions with `{}` bodies). Expression-bodied
arrows are not separate functions in the built-in scanner: their branches are counted
in the enclosing function, and the reference CCN follows the same convention.

Usage:
    python3 evals/js_parser_benchmark.py --repo /path/to/repo \
        --typescript /path/to/node_modules/typescript [--glob 'packages/*/src/**/*.ts*']
Requires `node` on PATH.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from diff_risk_sentinel.complexity import analyze_js_ts_fallback  # noqa: E402

TS_TRUTH_JS = r"""
const ts = require(process.argv[2]);
const fs = require('fs');
const files = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
const isBranch = (n) =>
  ts.isIfStatement(n) || ts.isForStatement(n) || ts.isForInStatement(n) || ts.isForOfStatement(n) ||
  ts.isWhileStatement(n) || ts.isDoStatement(n) || ts.isCaseClause(n) || ts.isCatchClause(n) ||
  ts.isConditionalExpression(n) ||
  (ts.isBinaryExpression(n) && [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken,
    ts.SyntaxKind.QuestionQuestionToken].includes(n.operatorToken.kind));
const isBlockFn = (n) => ts.isFunctionLike(n) && n.body && ts.isBlock(n.body);
for (const f of files) {
  const src = ts.createSourceFile(f, fs.readFileSync(f, 'utf8'), ts.ScriptTarget.Latest, true,
    /x$/.test(f) ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const res = [];
  const countCcn = (body) => {
    let ccn = 1;
    const walk = (n) => {
      if (isBlockFn(n)) return;           // nested block-bodied functions are scored separately
      if (isBranch(n)) ccn += 1;
      ts.forEachChild(n, walk);
    };
    ts.forEachChild(body, walk);
    return ccn;
  };
  const visit = (n) => {
    if (isBlockFn(n)) {
      res.push([
        src.getLineAndCharacterOfPosition(n.getStart()).line + 1,
        src.getLineAndCharacterOfPosition(n.body.getEnd()).line + 1,
        countCcn(n.body),
      ]);
    }
    ts.forEachChild(n, visit);
  };
  visit(src);
  out[f] = res;
}
process.stdout.write(JSON.stringify(out));
"""


def load_truth(files, typescript_path):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(TS_TRUTH_JS)
        script = fh.name
    try:
        res = subprocess.run(["node", script, typescript_path], input=json.dumps(files),
                             capture_output=True, text=True, check=True)
    finally:
        os.unlink(script)
    return json.loads(res.stdout)


def evaluate(name, files, truth, extract):
    tp = fp = fn = 0
    ccn_exact = ccn_close = 0
    for path in files:
        code = open(path, encoding="utf-8", errors="replace").read()
        pred = {(s, e): c for s, e, c in extract(path, code)}
        ref = {(s, e): c for s, e, c in truth[path]}
        matched = set(pred) & set(ref)
        tp += len(matched)
        fp += len(set(pred) - set(ref))
        fn += len(set(ref) - set(pred))
        for key in matched:
            diff = abs(pred[key] - ref[key])
            ccn_exact += diff == 0
            ccn_close += diff <= 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    result = {
        "extractor": name,
        "range_precision": round(precision, 4),
        "range_recall": round(recall, 4),
        "ccn_exact_on_matched": round(ccn_exact / tp, 4) if tp else 0.0,
        "ccn_within_1_on_matched": round(ccn_close / tp, 4) if tp else 0.0,
        "true_positives": tp, "false_positives": fp, "false_negatives": fn,
    }
    print(f"{name:10} ranges P={precision:.1%} R={recall:.1%} | CCN exact={result['ccn_exact_on_matched']:.1%} "
          f"±1={result['ccn_within_1_on_matched']:.1%} (tp={tp} fp={fp} fn={fn})")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--typescript", required=True, help="Path to a `typescript` package directory")
    parser.add_argument("--glob", default="**/*.ts*")
    args = parser.parse_args()

    files = sorted(
        f for f in glob.glob(os.path.join(args.repo, args.glob), recursive=True)
        if "node_modules" not in f and f.endswith((".ts", ".tsx")) and not f.endswith(".d.ts")
    )
    print(f"{len(files)} files")
    truth = load_truth(files, os.path.abspath(args.typescript))

    def builtin(path, code):
        return [(f["start_line"], f["end_line"], f["ccn"]) for f in analyze_js_ts_fallback(path, code)]

    evaluate("builtin", files, truth, builtin)
    try:
        import lizard
    except ImportError:
        return

    def via_lizard(path, code):
        return [(f.start_line, f.end_line, f.cyclomatic_complexity)
                for f in lizard.analyze_file.analyze_source_code(path, code).function_list]

    evaluate("lizard", files, truth, via_lizard)


if __name__ == "__main__":
    main()
