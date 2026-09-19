import sys
import unittest
from diff_risk_sentinel.complexity import analyze_python_functions, analyze_js_ts_fallback, extract_functions


def by_name(functions):
    return {f["name"]: f for f in functions}


class TestPythonComplexity(unittest.TestCase):

    def test_python_complexity_visitor(self):
        py_code = """
def simple_fn(x):
    return x + 1

def complex_fn(x, y):
    if x > 0:
        if y > 0:
            return x + y
        else:
            return x - y
    for i in range(10):
        print(i)
    return 0
"""
        functions = analyze_python_functions("sample.py", py_code)
        self.assertEqual(len(functions), 2)
        self.assertEqual(functions[0]["name"], "simple_fn")
        self.assertEqual(functions[0]["ccn"], 1)

        self.assertEqual(functions[1]["name"], "complex_fn")
        self.assertEqual(functions[1]["ccn"], 4)

    def test_nested_function_branches_are_not_counted_in_outer(self):
        code = """
def outer(x):
    def inner(y):
        if y: return 1
        if y > 2: return 2
        return 0
    return inner(x)
"""
        fns = by_name(analyze_python_functions("a.py", code))
        self.assertEqual(fns["outer"]["ccn"], 1)
        self.assertEqual(fns["outer.inner"]["ccn"], 3)

    def test_methods_are_qualified_with_class_name(self):
        code = """
class Service:
    def run(self):
        return 1
"""
        fns = by_name(analyze_python_functions("a.py", code))
        self.assertIn("Service.run", fns)

    def test_comprehension_conditions_count_as_branches(self):
        code = """
def f(xs):
    return [x for x in xs if x > 0]
"""
        self.assertEqual(analyze_python_functions("a.py", code)[0]["ccn"], 3)

    def assertCcn(self, body, expected):
        code = "def f(xs, lock):\n" + "".join("    " + ln + "\n" for ln in body.strip("\n").splitlines())
        self.assertEqual(analyze_python_functions("a.py", code)[0]["ccn"], expected)

    def test_with_does_not_count(self):  # radon rule
        self.assertCcn("with lock:\n    pass", 1)

    def test_loop_else_counts(self):
        self.assertCcn("for x in xs:\n    pass\nelse:\n    pass", 3)

    def test_try_else_counts(self):
        self.assertCcn("try:\n    pass\nexcept ValueError:\n    pass\nelse:\n    pass", 3)

    @unittest.skipIf(sys.version_info < (3, 10), "match syntax needs a Python >= 3.10 parser")
    def test_match_wildcard_does_not_count(self):
        self.assertCcn("match xs:\n    case [1]:\n        pass\n    case _:\n        pass", 2)

    def test_unparseable_file_returns_none(self):
        self.assertIsNone(analyze_python_functions("a.py", "def broken(:\n    pass\n"))

    def test_decorator_lines_belong_to_function(self):
        code = """
@decorator
def f():
    return 1
"""
        fn = analyze_python_functions("a.py", code)[0]
        self.assertEqual(fn["start_line"], 2)
        self.assertEqual(fn["end_line"], 4)


class TestJsTsFallback(unittest.TestCase):

    def test_js_ts_fallback_complexity(self):
        js_code = """
function processOrder(order) {
    if (!order) return null;
    if (order.status === 'pending') {
        for (let item of order.items) {
            if (item.valid) {
                console.log(item);
            }
        }
    }
    return order;
}
"""
        functions = analyze_js_ts_fallback("sample.ts", js_code)
        self.assertEqual(len(functions), 1)
        self.assertEqual(functions[0]["name"], "processOrder")
        self.assertEqual(functions[0]["ccn"], 5)
        self.assertEqual((functions[0]["start_line"], functions[0]["end_line"]), (2, 12))

    def test_single_line_arrow_does_not_swallow_next_function(self):
        code = """const f = (x) => x + 1;
function g(a) {
  if (a) { return 1; }
  return 2;
}
function h(b) {
  return b;
}
"""
        fns = by_name(analyze_js_ts_fallback("a.ts", code))
        self.assertEqual((fns["g"]["start_line"], fns["g"]["end_line"]), (2, 5))
        self.assertEqual(fns["g"]["ccn"], 2)
        self.assertEqual((fns["h"]["start_line"], fns["h"]["end_line"]), (6, 8))

    def test_top_level_control_flow_is_not_a_function(self):
        code = """function outer() {
  return 1;
}
if (ready) {
  start();
}
for (const x of xs) {
  run(x);
}
"""
        names = [f["name"] for f in analyze_js_ts_fallback("a.ts", code)]
        self.assertEqual(names, ["outer"])

    def test_braces_in_strings_and_optional_chaining_are_ignored(self):
        code = """function k(o) {
  const a = o?.x ?? 0;
  const s = "}";
  const t = `${a} }`;
  return a;
}
"""
        fn = analyze_js_ts_fallback("a.ts", code)[0]
        self.assertEqual(fn["end_line"], 6)
        self.assertEqual(fn["ccn"], 2)  # 1 + '??'

    def test_branches_inside_template_expressions_are_counted(self):
        code = """function label(r) {
  return `${r.a ?? 0} · ${r.b ? 'x' : `${r.c || '}'}`}`;
}
"""
        fn = analyze_js_ts_fallback("a.ts", code)[0]
        self.assertEqual((fn["end_line"], fn["ccn"]), (3, 4))

    def test_string_right_after_template_placeholder(self):
        code = """function f() {
  return `tasks${':'}view`;
}
function g() {
  return 1;
}
"""
        names = [(x["name"], x["end_line"]) for x in analyze_js_ts_fallback("a.ts", code)]
        self.assertEqual(names, [("f", 3), ("g", 6)])

    def test_object_type_in_generic_constraint(self):
        code = """export function match<T extends { route: string }>(
  items: T[],
): T | null {
  return items[0] ?? null;
}
"""
        fn = analyze_js_ts_fallback("a.ts", code)[0]
        self.assertEqual((fn["name"], fn["start_line"], fn["end_line"]), ("match", 1, 5))

    def test_multiline_block_comments_are_ignored(self):
        code = """function k(o) {
  /*
   if (x) { for (;;) {}
  */
  return o;
}
"""
        fn = analyze_js_ts_fallback("a.ts", code)[0]
        self.assertEqual((fn["end_line"], fn["ccn"]), (6, 1))

    def test_react_component_with_destructured_props_and_nested_callbacks(self):
        code = """export function Inbox({ unitId, items }: Props): JSX.Element {
  const label = unitId ? 'on' : 'off';
  useEffect(() => {
    if (unitId === null) return;
    reload();
  }, [unitId]);
  const onClick = useCallback((id: string) => {
    if (id && items.length) select(id);
  }, [items]);
  return <p className="x">Don't {label}</p>;
}
"""
        fns = analyze_js_ts_fallback("a.tsx", code)
        names = by_name(fns)
        self.assertEqual((names["Inbox"]["start_line"], names["Inbox"]["end_line"]), (1, 11))
        self.assertEqual(names["Inbox"]["ccn"], 2)  # 1 + ternary; callbacks counted separately
        self.assertEqual(names["Inbox.<anonymous>"]["ccn"], 2)
        self.assertEqual(names["Inbox.onClick"]["ccn"], 3)

    def test_class_methods_are_detected_and_qualified(self):
        code = """class Store {
  private items: Map<string, number> = new Map();
  async load(id: string, opts?: Options): Promise<Map<string, number>> {
    if (!id) throw new Error('x');
    return this.items;
  }
  get size() {
    return this.items.size;
  }
}
"""
        fns = by_name(analyze_js_ts_fallback("a.ts", code))
        self.assertEqual(fns["Store.load"]["ccn"], 2)
        self.assertEqual((fns["Store.load"]["start_line"], fns["Store.load"]["end_line"]), (3, 6))
        self.assertIn("Store.size", fns)

    def test_private_hash_methods(self):
        code = """class Game {
  async #probe(): Promise<void> {
    if (this.x) return;
  }
}
"""
        fns = by_name(analyze_js_ts_fallback("a.ts", code))
        self.assertEqual((fns["Game.#probe"]["end_line"], fns["Game.#probe"]["ccn"]), (4, 2))

    def test_object_literal_is_not_a_function(self):
        code = """const cfg = {
  a: 1,
  b: { c: 2 },
};
function f() {
  return { x: 1 };
}
"""
        names = [f["name"] for f in analyze_js_ts_fallback("a.ts", code)]
        self.assertEqual(names, ["f"])

    def test_object_type_return_annotation_is_not_the_body(self):
        code = """function stats(x: number): { a: number; b?: string } {
  if (x) return { a: 1 };
  return { a: 2 };
}
class Api {
  async get(): Promise<{ session: string | null }> {
    return { session: null };
  }
}
"""
        fns = by_name(analyze_js_ts_fallback("a.ts", code))
        self.assertEqual((fns["stats"]["end_line"], fns["stats"]["ccn"]), (4, 2))
        self.assertEqual((fns["Api.get"]["start_line"], fns["Api.get"]["end_line"]), (6, 8))

    def test_bare_block_statement_is_a_block(self):
        code = """function f(x) {
  const a = 1;
  {
    const r = g(a);
    if (r) return r;
  }
  const h = (t: number): boolean => {
    return t > 0;
  };
  return h(x);
}
"""
        fns = by_name(analyze_js_ts_fallback("a.ts", code))
        self.assertEqual((fns["f.h"]["start_line"], fns["f.h"]["end_line"]), (7, 9))
        self.assertEqual(fns["f"]["ccn"], 2)

    def test_less_than_in_control_flow_header_does_not_hide_functions(self):
        code = """if (a < b) { init(); }
function f1() {
  return 1;
}
function f2() {
  for (let i = 0; i < n; i++) { step(i); }
  const g = () => {
    return 2;
  };
}
"""
        fns = {f["name"]: f for f in analyze_js_ts_fallback("a.ts", code)}
        self.assertEqual((fns["f1"]["start_line"], fns["f1"]["end_line"]), (2, 4))
        self.assertEqual((fns["f2.g"]["start_line"], fns["f2.g"]["end_line"]), (7, 9))
        self.assertEqual(fns["f2"]["ccn"], 2)

    def test_arrow_returned_from_if_is_a_function(self):
        code = """function f(xs) {
  if (xs.length === 0) return () => {
    cancelled = true;
  };
}
"""
        names = {x["name"]: (x["start_line"], x["end_line"]) for x in analyze_js_ts_fallback("a.ts", code)}
        self.assertEqual(names["f.<anonymous>"], (2, 4))

    def test_no_semicolon_code(self):
        code = """import { x } from 'y'
const limit = 10
export const handler = async (req) => {
  doSomething()
  if (req) { a() }
  const value = compute(req)
  const g = (y) => {
    return y
  }
}
"""
        fns = {f["name"]: f for f in analyze_js_ts_fallback("a.ts", code)}
        self.assertEqual(sorted(fns), ["handler", "handler.g"])
        self.assertEqual((fns["handler"]["start_line"], fns["handler"]["ccn"]), (3, 2))
        self.assertEqual(fns["handler.g"]["start_line"], 7)

    def test_multiline_signatures_and_chains_are_not_split(self):
        code = """export async function load(
  id: string,
): Promise<number> {
  return api
    .get(id)
    .then((r) => {
      return r.n
    })
}
"""
        fns = {f["name"]: f for f in analyze_js_ts_fallback("a.ts", code)}
        self.assertEqual((fns["load"]["start_line"], fns["load"]["end_line"]), (1, 9))
        self.assertIn("load.<anonymous>", fns)

    def test_literals_after_arrow_and_type_operators(self):
        for first_line in (
            "const quote = (s) => /'/.test(s);",
            "const brace = (s) => /\\{/.test(s);",
            "type K = T extends 'a{' ? 1 : 2;",
            "const v = x as '}';",
            "const w = x satisfies '{';",
        ):
            with self.subTest(first_line=first_line):
                code = first_line + "\nfunction later() {\n  return 1;\n}\n"
                fns = analyze_js_ts_fallback("a.ts", code)
                self.assertEqual([(f["name"], f["start_line"], f["end_line"]) for f in fns], [("later", 2, 4)])

    def test_deeply_nested_names_are_not_duplicated(self):
        code = """function top() {
  function mid() {
    function low() {
      return 1;
    }
  }
}
class S {
  run() {
    const inner = () => {
      return 1;
    };
  }
}
"""
        names = [f["name"] for f in analyze_js_ts_fallback("a.ts", code)]
        self.assertEqual(names, ["top", "top.mid", "top.mid.low", "S.run", "S.run.inner"])

    def test_callback_is_not_named_after_its_parameter(self):
        code = """class C {
  private handle = async (e) => {
    return e;
  };
  readonly other = () => {
    xs.forEach(item => {
      use(item);
    });
  };
}
"""
        names = [f["name"] for f in analyze_js_ts_fallback("a.ts", code)]
        self.assertEqual(names, ["C.handle", "C.other", "C.other.<anonymous>"])

    def test_same_line_decorators(self):
        code = """@Controller('u')
export class UsersController {
  @Get(':id') async find(@Param('id') id: string) {
    if (!id) throw new E();
    return id;
  }
}
"""
        fns = {x["name"]: x for x in analyze_js_ts_fallback("a.ts", code)}
        self.assertEqual((fns["UsersController.find"]["start_line"], fns["UsersController.find"]["ccn"]), (3, 2))

    def test_postfix_increment_before_division_is_not_a_regex(self):
        fn = analyze_js_ts_fallback("a.ts", "function f(a) {\n  let c = a++ / 2; if (a) { g(); }\n}\n")[0]
        self.assertEqual(fn["ccn"], 2)

    def test_duplicate_names_get_ordinal_suffix(self):
        code = """function f() {
  run(() => {
    a();
  });
  run(() => {
    b();
  });
}
"""
        names = [f["name"] for f in analyze_js_ts_fallback("a.ts", code)]
        self.assertEqual(names, ["f", "f.<anonymous>", "f.<anonymous>#2"])


if __name__ == "__main__":
    unittest.main()


class TestExtractFunctionsRouting(unittest.TestCase):

    def test_ts_uses_builtin_scanner_even_when_lizard_is_installed(self):
        # lizard runs past the end of this function because of the regex literals
        code = """export function slugLabel(slug: string): string {
  const label = slug
    .replace(/[-_]/g, ' ')
    .replace(/\\b\\w/g, (letter) => letter.toUpperCase());
  return `Recepcao - ${label}`;
}

export const OPTIONS = [
  { value: 1, label: 'x' },
];
"""
        fn = extract_functions("a.ts", code)[0]
        self.assertEqual((fn["name"], fn["start_line"], fn["end_line"]), ("slugLabel", 1, 6))
