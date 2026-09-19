import os
import tempfile
import unittest

from diff_risk_sentinel.coverage import load_coverage


def write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class TestCoverage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        write(os.path.join(self.root, "packages/api/app/svc.py"))
        write(os.path.join(self.root, "packages/api/app/other.py"))

    def tearDown(self):
        self.tmp.cleanup()

    def _coverage_xml(self, sources, classes):
        src = "".join(f"<source>{s}</source>" for s in sources)
        cls = ""
        for fn, lines in classes:
            body = "".join(f'<line number="{n}" hits="{h}"/>' for n, h in lines)
            cls += f'<class filename="{fn}"><lines>{body}</lines></class>'
        return f'<?xml version="1.0"?><coverage><sources>{src}</sources><packages><package><classes>{cls}</classes></package></packages></coverage>'

    def test_paths_relative_to_sources_are_mapped_to_repo_paths(self):
        cov = os.path.join(self.root, "packages/api/coverage.xml")
        write(cov, self._coverage_xml([os.path.join(self.root, "packages/api")], [("app/svc.py", [(1, 1), (2, 0)])]))
        cmap = load_coverage([cov], repo_root=self.root)
        self.assertEqual(cmap.lookup("packages/api/app/svc.py"), {1: True, 2: False})

    def test_relative_paths_without_matching_source_resolve_against_report_dir(self):
        cov = os.path.join(self.root, "packages/api/coverage.xml")
        write(cov, self._coverage_xml(["/ci/build/somewhere"], [("app/svc.py", [(1, 1)])]))
        cmap = load_coverage([cov], repo_root=self.root)
        self.assertEqual(cmap.lookup("packages/api/app/svc.py"), {1: True})

    def test_unresolvable_paths_fall_back_to_unique_suffix_match(self):
        cov = os.path.join(self.root, "coverage.xml")
        write(cov, self._coverage_xml(["/ci/build"], [("api/app/svc.py", [(3, 2)])]))
        cmap = load_coverage([cov], repo_root=self.root)
        self.assertEqual(cmap.lookup("packages/api/app/svc.py"), {3: True})
        self.assertIsNone(cmap.lookup("packages/api/app/other.py"))

    def test_classes_sharing_a_file_are_merged(self):
        cov = os.path.join(self.root, "coverage.xml")
        write(cov, self._coverage_xml(
            [self.root],
            [("packages/api/app/svc.py", [(1, 0), (2, 1)]), ("packages/api/app/svc.py", [(1, 3), (5, 0)])],
        ))
        cmap = load_coverage([cov], repo_root=self.root)
        self.assertEqual(cmap.lookup("packages/api/app/svc.py"), {1: True, 2: True, 5: False})

    def test_multiple_reports_are_combined(self):
        a = os.path.join(self.root, "a.xml")
        b = os.path.join(self.root, "b.xml")
        write(a, self._coverage_xml([self.root], [("packages/api/app/svc.py", [(1, 1)])]))
        write(b, self._coverage_xml([self.root], [("packages/api/app/other.py", [(1, 0)])]))
        cmap = load_coverage([a, b], repo_root=self.root)
        self.assertEqual(cmap.lookup("packages/api/app/svc.py"), {1: True})
        self.assertEqual(cmap.lookup("packages/api/app/other.py"), {1: False})

    def test_same_relative_name_under_two_sources_is_not_guessed(self):
        write(os.path.join(self.root, "pkg_a/utils.py"))
        write(os.path.join(self.root, "pkg_b/utils.py"))
        cov = os.path.join(self.root, "coverage.xml")
        write(cov, self._coverage_xml(
            [os.path.join(self.root, "pkg_a"), os.path.join(self.root, "pkg_b")],
            [("utils.py", [(1, 0)]), ("utils.py", [(1, 5)])],
        ))
        cmap = load_coverage([cov], repo_root=self.root)
        self.assertIsNone(cmap.lookup("pkg_a/utils.py"))
        self.assertIsNone(cmap.lookup("pkg_b/utils.py"))
        self.assertTrue(any("ambiguous" in e for e in cmap.errors))

    def test_malformed_report_is_reported_not_swallowed(self):
        bad = os.path.join(self.root, "bad.xml")
        write(bad, "<coverage><not-closed>")
        cmap = load_coverage([bad], repo_root=self.root)
        self.assertEqual(len(cmap.errors), 1)


if __name__ == "__main__":
    unittest.main()
