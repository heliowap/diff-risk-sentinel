import importlib.util
import os
import tempfile
import unittest

from tests.gitutil import GitRepo

_spec = importlib.util.spec_from_file_location(
    "historical_eval", os.path.join(os.path.dirname(__file__), "..", "evals", "historical_eval.py")
)
historical_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(historical_eval)


class TestWilson(unittest.TestCase):

    def test_known_interval(self):
        # 13/13 successes: the upper bound is 100%, the lower bound is far from it
        self.assertEqual(historical_eval.wilson(13, 13), (77.2, 100.0))

    def test_empty(self):
        self.assertEqual(historical_eval.wilson(0, 0), (None, None))


class TestRandomBaseline(unittest.TestCase):

    def test_single_target(self):
        self.assertAlmostEqual(historical_eval.random_hit_probability(touched=10, fixed=1, top=5), 0.5)

    def test_several_fixed_functions_raise_the_chance(self):
        # 1 - C(8,5)/C(10,5) = 1 - 56/252
        self.assertAlmostEqual(historical_eval.random_hit_probability(touched=10, fixed=2, top=5), 1 - 56 / 252)

    def test_degenerate_cases(self):
        self.assertEqual(historical_eval.random_hit_probability(touched=0, fixed=1, top=5), 0.0)
        self.assertEqual(historical_eval.random_hit_probability(touched=3, fixed=1, top=5), 1.0)


class TestSzz(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = GitRepo(self.tmp.name)
        self.repo.commit({"svc.py": "def other():\n    return 0\n"}, "init")
        self.repo.commit({"svc.py": "def other():\n    return 0\n\n\ndef pay(x):\n    if x:\n        return x - 1\n    return 0\n"}, "introduce bug")
        self.introducing = self.repo.head()
        self.repo.commit({"README.md": "docs\n"}, "unrelated")
        self.repo.commit({"svc.py": "def other():\n    return 0\n\n\ndef pay(x):\n    if x:\n        return x\n    return 0\n"}, "fix")
        self.fix = self.repo.head()

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_introducing_commit_and_fixed_function(self):
        intro = historical_eval.szz_introducing(self.tmp.name, self.fix)
        self.assertEqual(intro["commit"], self.introducing)
        self.assertEqual(intro["fixed_functions"], {"svc.py": ["pay"]})

    def test_rank_of_fixed_function(self):
        payload = {"targets": [{"file": "svc.py", "function": "other"}, {"file": "svc.py", "function": "pay"}]}
        self.assertEqual(historical_eval.szz_rank(payload, {"svc.py": ["pay"]}), 2)


if __name__ == "__main__":
    unittest.main()
