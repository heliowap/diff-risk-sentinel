import tempfile
import unittest

from diff_risk_sentinel.deadcode import is_stub, scan_repository
from tests.gitutil import GitRepo

SERVICE = '''\
import re


def used_helper(x):
    return x + 1


def unused_helper(x):
    return x * 2


def only_tested(x):
    return x - 1


def retired():
    raise NotImplementedError


@router.get("/health")
def health_route():
    return {"ok": True}


class Adapter:
    def dispatch(self, name):
        return getattr(self, f"_tool_{name}")()

    def _tool_lookup(self):
        return used_helper(1)

    def __repr__(self):
        return "Adapter"
'''

PROXY = '''\
from http.server import BaseHTTPRequestHandler


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)

    def log_message(self, fmt, *args):
        return
'''

WEB = '''\
export function renderBadge(label) {
  return `<b>${label}</b>`;
}

export function neverCalled() {
  return [];
}

export default function Page() {
  return renderBadge("x");
}
'''


class TestStub(unittest.TestCase):

    def test_python_and_ts_stubs(self):
        self.assertTrue(is_stub("a.py", "def f(x):\n    '''doc'''\n    return set()\n"))
        self.assertFalse(is_stub("a.py", "def f(x):\n    return x + 1\n"))
        self.assertTrue(is_stub("a.ts", "export function f(ids: string[]): Promise<X[]> {\n  // off\n  return [];\n}"))
        self.assertFalse(is_stub("a.ts", "function f(a) {\n  return a + 1;\n}"))


class TestScanRepository(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = GitRepo(self.tmp.name)
        self.repo.commit({
            "app/service.py": SERVICE,
            "app/main.py": "from app.service import used_helper\n\nprint(used_helper(2))\n",
            "scripts/proxy.py": PROXY,
            "web/page.ts": WEB,
            "web/setupTests.ts": "export function unobserve() {\n  return undefined;\n}\n",
            "web/errors.ts": ("export class ApiError extends Error {\n  constructor(msg) {\n    super(msg);\n  }\n}\n"
                              "throw new ApiError('x');\n"),
            "web/boundary.tsx": ("export class Boundary extends Component {\n"
                                 "  componentDidCatch(error) {\n    report(error);\n  }\n}\n"
                                 "export function startTour() {\n  return driver({\n"
                                 "    onDestroyed: () => {\n      done();\n    },\n  });\n}\n"),
            "tests/test_service.py": "from app.service import only_tested\n\ndef test_it():\n    assert only_tested(2) == 1\n",
        }, "init")
        self.found = {f["function"]: f for f in scan_repository(self.tmp.name, "HEAD")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_unreferenced_and_test_only_functions_are_reported(self):
        self.assertEqual(self.found["unused_helper"]["status"], "unreferenced")
        self.assertEqual(self.found["only_tested"]["status"], "tests_only")
        self.assertEqual(self.found["neverCalled"]["status"], "unreferenced")
        self.assertTrue(self.found["neverCalled"]["stub"])
        self.assertEqual(self.found["retired"]["status"], "unreferenced")
        self.assertTrue(self.found["retired"]["stub"])

    def test_used_functions_are_not_reported(self):
        for name in ("used_helper", "renderBadge"):
            self.assertNotIn(name, self.found)

    def test_framework_entry_points_and_dynamic_dispatch_are_excluded(self):
        for name in ("health_route", "Adapter._tool_lookup", "Adapter.__repr__", "Handler.do_GET",
                     "Handler.log_message", "Page", "unobserve", "Boundary.componentDidCatch",
                     "startTour.onDestroyed", "ApiError.constructor"):
            self.assertNotIn(name, self.found)

    def test_results_carry_location(self):
        f = self.found["unused_helper"]
        self.assertEqual((f["file"], f["lines"]), ("app/service.py", "8-9"))


class TestDeadCodeCommand(unittest.TestCase):

    def test_run_dead_code_writes_report(self):
        import io, json, os
        from contextlib import redirect_stdout
        from diff_risk_sentinel.cli import run_dead_code
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"app/a.py": "def lonely():\n    return None\n\n\ndef used():\n    return 1\n",
                         "app/b.py": "from app.a import used\nused()\n"}, "init")
            out = os.path.join(tmp, "dead.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run_dead_code(repo=tmp, rev="HEAD", output=out, top=10)
            self.assertEqual(code, 0)
            data = json.load(open(out))
            self.assertEqual([f["function"] for f in data["dead_code"]], ["lonely"])
            self.assertEqual(data["meta"]["rev"], repo.head())
            self.assertIn("lonely", buf.getvalue())

    def test_functions_used_only_by_tests_are_listed_apart(self):
        import io, json, os
        from contextlib import redirect_stdout
        from diff_risk_sentinel.cli import run_dead_code
        with tempfile.TemporaryDirectory() as tmp:
            GitRepo(tmp).commit({"app/a.py": "def lonely():\n    return None\n\n\ndef set_clock(c):\n    return c\n",
                                 "tests/test_a.py": "from app.a import set_clock\nset_clock(1)\n"}, "init")
            out = os.path.join(tmp, "dead.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                run_dead_code(repo=tmp, rev="HEAD", output=out, top=10)
            data = json.load(open(out))
        self.assertEqual([f["function"] for f in data["dead_code"]], ["lonely"])
        self.assertEqual([f["function"] for f in data["tests_only"]], ["set_clock"])
        self.assertIn("set_clock", buf.getvalue())

    def test_bad_revision_returns_2(self):
        import io
        from contextlib import redirect_stdout
        from diff_risk_sentinel.cli import run_dead_code
        with tempfile.TemporaryDirectory() as tmp:
            GitRepo(tmp).commit({"a.py": "x = 1\n"}, "init")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run_dead_code(repo=tmp, rev="nope", output=f"{tmp}/o.json", top=5), 2)


if __name__ == "__main__":
    unittest.main()
