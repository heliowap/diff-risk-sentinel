import os
import sys
import json
import argparse
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Union

from . import __version__
from .diff import FileDiff, GitError, default_base, get_diff, read_blobs, run_git
from .complexity import extract_functions, supported_extensions
from .coverage import load_coverage
from .deadcode import scan_repository
from .deadcode_evidence import review_with_jev
from .endpoints import orphan_endpoints
from .crap import (
    calculate_crap,
    calculate_crap_trend,
    compute_aggregate_metrics,
    classify_otterwise_risk,
)
from .jev import JEV_DIMENSIONS, function_state, query_jev_function, triage_scores
from .signals import changed_tokens, find_consumers, is_test_path


def _innermost_function_lines(functions: List[Dict[str, Any]], lines: Sequence[int]) -> Dict[int, List[int]]:
    """Maps function index -> touched lines, attributing each line to the innermost function only."""
    owned: Dict[int, List[int]] = {}
    for ln in lines:
        best = None
        for idx, fn in enumerate(functions):
            if fn["start_line"] <= ln <= fn["end_line"]:
                if best is None or (fn["end_line"] - fn["start_line"]) < (
                    functions[best]["end_line"] - functions[best]["start_line"]
                ):
                    best = idx
        if best is not None:
            owned.setdefault(best, []).append(ln)
    return owned


def _analyze_file(
    fdiff: FileDiff,
    old_code: Optional[str],
    new_code: Optional[str],
    file_cov: Optional[Dict[int, bool]],
) -> tuple:
    """
    Returns (touched method items, removed method items) for one file, or None when
    either side cannot be parsed (e.g. syntax newer than the running interpreter):
    scoring it anyway would report every function as removed — a fake improvement.
    """
    old_funcs = extract_functions(fdiff.old_path, old_code) if fdiff.old_path and old_code is not None else []
    new_funcs = extract_functions(fdiff.new_path, new_code) if fdiff.new_path and new_code is not None else []
    if old_funcs is None or new_funcs is None:
        return None
    old_by_name = {fn["name"]: fn for fn in old_funcs}
    new_names = {fn["name"] for fn in new_funcs}

    # The old version's coverage is unknown; the file's current coverage is the closest
    # estimate. A fixed 0% would make renaming a well-tested function look like a big
    # improvement (its "before" CRAP would be inflated). Deleted files fall back to 0%.
    instrumented_file = list((file_cov or {}).values())
    file_ratio = sum(instrumented_file) / len(instrumented_file) if instrumented_file else 0.0
    removed = [
        {
            "file": fdiff.old_path,
            "function": fn["name"],
            "ccn_before": fn["ccn"],
            "crap_before": round(calculate_crap(fn["ccn"], file_ratio), 1),
        }
        for fn in old_funcs if fn["name"] not in new_names
    ]

    new_lines = (new_code or "").split("\n")
    old_lines = (old_code or "").split("\n")
    touched = []
    owned = _innermost_function_lines(new_funcs, sorted(fdiff.touched_lines))
    for idx, lines in owned.items():
        fn = new_funcs[idx]
        start, end = fn["start_line"], fn["end_line"]
        cov_lines = file_cov or {}
        instrumented = [ln for ln in range(start, end + 1) if ln in cov_lines]
        covered = sum(1 for ln in instrumented if cov_lines[ln])
        cov_ratio = covered / len(instrumented) if instrumented else 0.0

        crap = round(calculate_crap(fn["ccn"], cov_ratio), 1)
        old_fn = old_by_name.get(fn["name"])
        # Same coverage for both sides isolates the complexity change introduced by the diff.
        crap_before = round(calculate_crap(old_fn["ccn"], cov_ratio), 1) if old_fn else None

        touched.append({
            "file": fdiff.new_path,
            "function": fn["name"],
            "lines": f"{start}-{end}",
            "changed_lines": len(lines),
            "ccn": fn["ccn"],
            "ccn_before": old_fn["ccn"] if old_fn else None,
            "coverage": round(cov_ratio, 2),
            "coverage_known": bool(instrumented),
            "crap": crap,
            "crap_before": crap_before,
            "delta_crap": calculate_crap_trend(crap_before, crap),
            "diff_snippet": fdiff.snippet_for(start, end),
            # private: full source for the Jev judgment, never written to the report
            "_new_code": "\n".join(new_lines[start - 1:end]),
            "_old_code": "\n".join(old_lines[old_fn["start_line"] - 1:old_fn["end_line"]]) if old_fn else "",
        })
    return touched, removed


def _autodetect_coverage(repo_root: str) -> List[str]:
    found = []
    for directory in (os.getcwd(), repo_root):
        candidate = os.path.join(directory, "coverage.xml")
        if os.path.isfile(candidate) and os.path.realpath(candidate) not in map(os.path.realpath, found):
            found.append(candidate)
    return found


def _write_output(output: str, payload: Dict[str, Any]):
    with open(output, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _load_coverage_map(coverage: Union[None, str, Sequence[str]], repo_root: str):
    """None autodetects coverage.xml; an empty list explicitly means "no coverage"."""
    if coverage is None:
        coverage_paths = _autodetect_coverage(repo_root)
    else:
        coverage_paths = [coverage] if isinstance(coverage, str) else list(coverage)
    cov_map = load_coverage(coverage_paths, repo_root)
    for err in cov_map.errors:
        print(f"⚠️  Relatório de cobertura ignorado: {err}")
    if cov_map.reports:
        print(f"📊 Relatório(s) de cobertura carregado(s): {', '.join(cov_map.reports)}")
    else:
        print("ℹ️  Nenhum coverage.xml encontrado. Assumindo cobertura base 0% (CRAP conservador).")
    return cov_map


def _collect_methods(repo_root: str, old_rev: str, new_rev: str, code_files: List[FileDiff], cov_map) -> tuple:
    """Returns (touched methods, removed methods, unparsed file paths) across the diff's code files."""
    blobs = read_blobs(repo_root, [(new_rev, f.new_path) for f in code_files if f.new_path]
                       + [(old_rev, f.old_path) for f in code_files if f.old_path])
    touched: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    unparsed: List[str] = []
    for fdiff in code_files:
        result = _analyze_file(
            fdiff,
            blobs.get((old_rev, fdiff.old_path)) if fdiff.old_path else None,
            blobs.get((new_rev, fdiff.new_path)) if fdiff.new_path else None,
            cov_map.lookup(fdiff.new_path) if fdiff.new_path else None,
        )
        if result is None:
            unparsed.append(fdiff.new_path or fdiff.old_path)
            continue
        touched.extend(result[0])
        removed.extend(result[1])
    return touched, removed, unparsed


def _print_summary(agg: Dict[str, Any]):
    print("\n" + "─" * 80)
    print("📈 MÉTRICAS AGREGADAS DO PR (OTTERWISE)")
    print(f"   Métodos Tocados: {agg['total_methods']} "
          f"(novos: {agg['new_methods']}, removidos: {agg['removed_methods']})")
    print(f"   Combined CRAP: {agg['combined_crap_before']} → {agg['combined_crap']} "
          f"(Δ {agg['combined_delta_crap']:+})")
    print(f"   Average CRAP:  {agg['average_crap_before']} → {agg['average_crap']} "
          f"(Δ {agg['average_delta_crap']:+})")
    if agg['average_delta_crap'] < 0:
        print("   🌟 Tendência: Melhoria da qualidade média por método (Average CRAP em queda).")
    elif agg['combined_delta_crap'] > 0:
        print("   ⚠️  Tendência: Risco aumentado (Combined CRAP em ascensão).")
    else:
        print("   ℹ️  Tendência: Estável.")
    print("─" * 80)


def _score_with_jev(touched: List[Dict[str, Any]], api_key: str, workers: int) -> int:
    """
    Asks Jev about every touched production function (not only those above the CRAP
    thresholds) and sets `triage_score`: the mean percentile rank over CRAP and the Jev
    answers. Returns the number of failed Jev calls.
    """
    production = [i for i in touched if not is_test_path(i["file"])]
    print(f"🤖 4. Avaliando {len(production)} funções de produção com TypeSafe Jev...")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        answers = list(executor.map(
            lambda it: query_jev_function(api_key, function_state(
                it["file"], it["function"], it["_old_code"], it["_new_code"], it["diff_snippet"])),
            production))
    by_item = {id(it): a for it, a in zip(production, answers)}
    failures = []
    for item in touched:
        answer = by_item.get(id(item))
        ok = bool(answer) and "error" not in answer
        for dim in JEV_DIMENSIONS:
            item[f"jev_{dim}"] = answer[dim] if ok else None
        item["jev_confidence"] = answer.get("confidence") if ok else None
        if answer and not ok:
            item["jev_error"] = answer["error"]
            failures.append(item)
    scores = triage_scores([i["crap"] for i in touched],
                           [{d: i[f"jev_{d}"] for d in JEV_DIMENSIONS} if i["jev_semantic_risk"] is not None else None
                            for i in touched])
    for item, score in zip(touched, scores):
        item["triage_score"] = score
    if failures:
        print(f"⚠️  {len(failures)}/{len(production)} chamadas ao Jev falharam; essas funções ficam no fim da "
              f"ordem do Jev. Exemplo: {failures[0]['jev_error']}")
    return len(failures)


def _classify(items: List[Dict[str, Any]]):
    for item in items:
        jev = item.get("jev_semantic_risk")
        item.setdefault("jev_semantic_risk", None)
        item.setdefault("triage_score", None)
        item["composite_risk"] = round(item["crap"] * (1.0 + (jev or 0.0)), 1)
        item["action"], item["strategy"] = classify_otterwise_risk(
            ccn=item["ccn"], crap=item["crap"], delta_crap=item["delta_crap"], jev_semantic_risk=jev)


def _public(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


def _consumers(repo_root: str, new_rev: str, code_files: List[FileDiff],
               touched: List[Dict[str, Any]], top: int) -> List[Dict[str, Any]]:
    """Functions outside the touched set that reference identifiers/literals the diff changed."""
    tokens = set()
    private: Dict[str, set] = {}
    for fdiff in code_files:
        path = fdiff.new_path or fdiff.old_path or ""
        if is_test_path(path):
            continue
        file_tokens = changed_tokens("\n".join(f"{kind}{text}" for kind, _, text in fdiff.lines))
        tokens |= file_tokens
        for tok in file_tokens:
            if tok.startswith("_") and not tok.startswith("__"):
                private.setdefault(tok, set()).add(path)
    exclude = {(i["file"], i["function"]) for i in touched}
    return find_consumers(repo_root, new_rev, tokens, exclude_functions=exclude, token_files=private)[:top]


def _print_consumers(consumers: List[Dict[str, Any]]):
    if consumers:
        print("\n🔗 Consumidores fora do diff que usam contratos alterados:")
        for c in consumers:
            print(f"   {c['file']}::{c['function']} (L:{c['lines']}) — {', '.join(c['tokens'][:4])}")


def _crap_zone(crap: float) -> str:
    if crap >= 60:
        return "High Risk (60+)"
    return "Needs Attention (30-60)" if crap >= 30 else "Acceptable (0-30)"


def _print_target(idx: int, item: Dict[str, Any]):
    cov_label = f"{int(item['coverage'] * 100)}%" if item["coverage_known"] else "sem dados"
    ccn_label = f"{item['ccn_before']} → {item['ccn']}" if item["ccn_before"] is not None else f"{item['ccn']} (novo)"
    print(f"\n#{idx} [{item['action']}] {item['file']}::{item['function']} (L:{item['lines']})")
    print(f"   Complexity (CCN): {ccn_label} | Cobertura: {cov_label} | CRAP: {item['crap']} "
          f"(Δ {item['delta_crap']:+}) [{_crap_zone(item['crap'])}]")
    if item.get("jev_semantic_risk") is not None:
        print(f"   Jev: bug {int(item['jev_introduces_bug'] * 100)}% · casos de borda {int(item['jev_edge_cases'] * 100)}% · "
              f"mudança de comportamento {item['jev_behavior_change']:.1f}/3 · risco {item['jev_semantic_risk']:.1f}/3")
    if item.get("triage_score") is not None:
        print(f"   => TRIAGEM (CRAP + Jev, percentil médio): {item['triage_score']:.2f}")
    else:
        print(f"   => SCORE COMPOSTO FINAL: {item['composite_risk']}")
    print(f"   🛠️  Estratégia Recomendada: {item.get('strategy', '')}")


def run_sentinel(
    base: Optional[str] = None,
    coverage: Union[None, str, Sequence[str]] = None,
    threshold_crap: float = 15.0,
    threshold_ccn: int = 10,
    threshold_delta: float = 10.0,
    top: int = 5,
    output: str = "llm_review_targets.json",
    jev: bool = False,
    workers: int = 16,
    repo: Optional[str] = None,
    top_consumers: int = 8,
) -> int:
    try:
        repo_root = run_git(["rev-parse", "--show-toplevel"], repo).strip()
        base = base or default_base(repo_root)
        print(f"🔍 1. Fatiando git diff contra {base}...")
        old_rev, new_rev, files = get_diff(base, repo_root)
    except GitError as exc:
        print(f"❌ Erro do git: {exc}")
        return 2

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if jev and not api_key:
        print("⚠️  --jev solicitado, mas TYPESAFE_API_KEY não está definida. Rodando em modo CRAP-only.")
        jev = False

    exts = supported_extensions()
    code_files = [f for f in files if (f.new_path or f.old_path or "").endswith(exts)]
    cov_map = _load_coverage_map(coverage, repo_root)

    print(f"📐 2. Analisando CCN e CRAP dos métodos modificados ({len(code_files)} arquivo(s) de código)...")
    try:
        touched, removed, unparsed = _collect_methods(repo_root, old_rev, new_rev, code_files, cov_map)
    except GitError as exc:
        print(f"❌ Erro do git: {exc}")
        return 2
    for path in unparsed:
        print(f"⚠️  {path}: não foi possível fazer o parse (sintaxe inválida ou mais nova que este Python); arquivo ignorado.")

    try:
        consumers = _consumers(repo_root, new_rev, code_files, touched, top_consumers)
    except GitError as exc:
        print(f"⚠️  Busca de consumidores falhou: {exc}")
        consumers = []

    # Aggregates describe every touched method, not only the ones above the alert thresholds.
    agg_metrics = compute_aggregate_metrics(touched, removed)
    meta = {
        "version": __version__,
        "base": base,
        "old_rev": old_rev,
        "new_rev": new_rev,
        "files_in_diff": len(files),
        "code_files_analyzed": len(code_files) - len(unparsed),
        "unparsed_files": unparsed,
        "coverage_reports": cov_map.reports,
        "thresholds": {"crap": threshold_crap, "ccn": threshold_ccn, "delta_crap": threshold_delta},
        "jev_enabled": jev,
        "jev_failures": 0,
        "ranking": "crap+jev" if jev else "crap",
    }
    _print_summary(agg_metrics)

    if jev:
        # Measured ranking: every touched function, ordered by the mean rank of CRAP and Jev.
        meta["jev_failures"] = _score_with_jev(touched, api_key, workers)
        candidates = sorted(touched, key=lambda x: x["triage_score"], reverse=True)
    else:
        candidates = [
            item for item in touched
            if item["crap"] >= threshold_crap or item["ccn"] >= threshold_ccn or abs(item["delta_crap"]) >= threshold_delta
        ]
        candidates.sort(key=lambda x: x["crap"], reverse=True)

    if not candidates:
        print(f"✅ Nenhum método ultrapassou os limites de alerta "
              f"(CRAP < {threshold_crap}, CCN < {threshold_ccn}, |ΔCRAP| < {threshold_delta}).")
        _print_consumers(consumers)
        _write_output(output, {"meta": meta, "otterwise_summary": agg_metrics, "targets": [], "consumers": consumers})
        return 0

    top_offenders = candidates[:top]
    _classify(top_offenders)
    if not jev:
        print(f"\n⚡ 3. Identificados {len(candidates)} métodos acima dos limites estruturais.")
    print("\n" + "=" * 80)
    print(f"🚨 TOP {len(top_offenders)} OFENSORES DE RISCO NO DIFF (CRAP{' + JEV' if jev else ''})")
    print("=" * 80)
    for idx, item in enumerate(top_offenders, 1):
        _print_target(idx, item)

    _print_consumers(consumers)
    _write_output(output, {"meta": meta, "otterwise_summary": agg_metrics, "targets": _public(top_offenders),
                           "consumers": consumers})
    print(f"\n📁 Metas de auditoria salvas em '{output}'.")
    print("💡 Você pode acionar o LLM diretamente para tratar estes ofensores.")
    return 0


def run_dead_code(repo: Optional[str] = None, rev: str = "HEAD", output: str = "dead_code.json", top: int = 30,
                  jev: bool = False, workers: int = 16) -> int:
    """Repository-wide dead-code scan at `rev` (deadcode.py), optionally reviewed by Jev (deadcode_evidence.py)."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if jev and not api_key:
        print("⚠️  --jev solicitado, mas TYPESAFE_API_KEY não está definida. Rodando só a análise estática.")
        jev = False
    try:
        repo_root = run_git(["rev-parse", "--show-toplevel"], repo).strip()
        sha = run_git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], repo_root).strip()
        if not sha:
            raise GitError(f"unknown revision '{rev}'")
        print(f"🧹 Procurando código morto em {rev} ({sha[:12]})...")
        found = scan_repository(repo_root, sha)
        endpoints = orphan_endpoints(repo_root, sha)
    except GitError as exc:
        print(f"❌ Erro do git: {exc}")
        return 2
    review = {}
    if jev:
        print("🤖 Revisando com Jev (veto de achados estáticos + candidatos prováveis)...")
        review = review_with_jev(repo_root, sha, found, api_key, workers=workers)
        found = review["dead_code"]
    # A team acting on a real report removed 97% of `unreferenced` findings but only 62% of the
    # tests-only ones (DI seams, test-isolation resets, documented seams), so they are listed apart.
    tests_only = [f for f in found if f["status"] == "tests_only"]
    found = [f for f in found if f["status"] != "tests_only"]
    print(f"   {len(found)} função(ões) de produção sem nenhuma referência.")
    print("   Revise antes de remover: chamadas dinâmicas não detectadas e ferramentas de dev podem aparecer aqui.")
    for f in found[:top]:
        print(f"   {f['file']}::{f['function']} (L:{f['lines']}){' — stub' if f['stub'] else ''}")
    if tests_only:
        print(f"\n   Só usadas por testes: {len(tests_only)} (verificar — seams de injeção e resets de teste são intencionais)")
        for f in tests_only[:top]:
            print(f"   {f['file']}::{f['function']} (L:{f['lines']}) — {f['test_references']} referência(s) em testes")
    if endpoints:
        print(f"\n   Endpoints HTTP que nada no repositório chama: {len(endpoints)} "
              f"(confira chamadores externos — webhooks, callbacks, operação manual — antes de remover)")
        for e in endpoints[:top]:
            docs = f" · citado em {', '.join(e['documented_in'])}" if e["documented_in"] else ""
            print(f"   {e['method'].upper()} {e['path']} — {e['file']}::{e['function']}{docs}")
    meta = {"version": __version__, "rev": sha, "functions_reported": len(found), "jev_enabled": jev}
    payload = {"meta": meta, "dead_code": found, "tests_only": tests_only, "orphan_endpoints": endpoints}
    if jev:
        meta.update(jev_judged=review["jev_judged"], jev_failures=review["jev_failures"])
        payload.update(vetoed_by_jev=review["vetoed_by_jev"], probable_dead=review["probable_dead"])
        print(f"   Jev descartou {len(review['vetoed_by_jev'])} achado(s) estático(s) (hooks de framework, callbacks).")
        if review["jev_failures"]:
            print(f"   ⚠️  {review['jev_failures']} julgamento(s) do Jev falharam; achados estáticos mantidos.")
        if review["probable_dead"]:
            print(f"\n   Prováveis (menos precisos — só homônimos, listas de export ou o próprio arquivo os citam): "
                  f"{len(review['probable_dead'])}")
            for f in review["probable_dead"][:top]:
                print(f"   {f['file']}::{f['function']} (L:{f['lines']}) — Jev {f['jev_p_removable']:.2f}")
    _write_output(output, payload)
    print(f"\n📁 Relatório salvo em '{output}'.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Diff Risk Sentinel: CRAP Score + OtterWise PR Methodology + TypeSafe Jev Risk Triage"
    )
    parser.add_argument("--base", default=None,
                        help="Diff target: 'base' (= base...HEAD), 'a...b' or 'a..b' "
                             "(default: origin/HEAD, else main/master)")
    parser.add_argument("--repo", default=None, help="Path inside the target git repository (default: cwd)")
    parser.add_argument("--coverage", action="append", default=None,
                        help="Cobertura XML report; repeat for monorepos (default: autodetect coverage.xml)")
    parser.add_argument("--threshold-crap", type=float, default=15.0, help="CRAP threshold to evaluate (default: 15.0)")
    parser.add_argument("--threshold-ccn", type=int, default=10, help="CCN threshold to evaluate (default: 10)")
    parser.add_argument("--threshold-delta", type=float, default=10.0, help="|ΔCRAP| threshold to evaluate (default: 10.0)")
    parser.add_argument("--top", type=int, default=5, help="Number of top offenders to output (default: 5)")
    parser.add_argument("--output", default="llm_review_targets.json", help="Output JSON path for LLM prompt")
    parser.add_argument("--jev", action="store_true",
                        help="Rank every touched production function by CRAP + TypeSafe Jev (sends each function's "
                             "code and diff to the Jev API; needs TYPESAFE_API_KEY). With --dead-code: Jev "
                             "reviews the findings (sends signatures and the lines that mention each name)")
    parser.add_argument("--no-jev", action="store_true", help=argparse.SUPPRESS)  # kept for compatibility; CRAP-only is now the default
    parser.add_argument("--top-consumers", type=int, default=8,
                        help="Untouched functions to list that use changed identifiers/literals (default: 8)")
    parser.add_argument("--dead-code", action="store_true",
                        help="Scan the whole repository at --rev for production functions nothing in production calls")
    parser.add_argument("--rev", default="HEAD", help="Revision for --dead-code (default: HEAD)")
    parser.add_argument("--workers", type=int, default=16, help="Parallel Jev requests (default: 16)")
    args = parser.parse_args()

    if args.dead_code:
        output = args.output if args.output != "llm_review_targets.json" else "dead_code.json"
        sys.exit(run_dead_code(repo=args.repo, rev=args.rev, output=output, top=args.top if args.top != 5 else 30,
                               jev=args.jev, workers=args.workers))

    sys.exit(run_sentinel(
        base=args.base,
        coverage=args.coverage,
        threshold_crap=args.threshold_crap,
        threshold_ccn=args.threshold_ccn,
        threshold_delta=args.threshold_delta,
        top=args.top,
        output=args.output,
        jev=args.jev and not args.no_jev,
        workers=args.workers,
        repo=args.repo,
        top_consumers=args.top_consumers,
    ))


if __name__ == "__main__":
    main()
