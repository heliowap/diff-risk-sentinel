import os
import tempfile
import unittest

from diff_risk_sentinel.signals import changed_tokens, find_consumers, is_test_path
from tests.gitutil import GitRepo


class TestTestPaths(unittest.TestCase):

    def test_test_paths(self):
        self.assertTrue(is_test_path("pkg/tests/test_x.py"))
        self.assertTrue(is_test_path("web/src/a.spec.ts"))
        self.assertFalse(is_test_path("src/contest.py"))


class TestChangedTokens(unittest.TestCase):

    def test_extracts_changed_literals_constants_and_signatures(self):
        snippet = "\n".join([
            "-UNIT_MAPS_URLS = {'north': 'https://maps.app.goo.gl/abc'}",
            "-def template_keys(slug):",
            "+def template_keys(code, kind):",
            "-    return f'reminder_{kind}_{slug}_v1'",
            "+    return f'reminder_{kind}_{code}_v1'",
            " unchanged = 'context_literal_value'",
        ])
        tokens = changed_tokens(snippet)
        self.assertIn("UNIT_MAPS_URLS", tokens)
        self.assertIn("template_keys", tokens)
        self.assertIn("reminder_", tokens)
        self.assertIn("north", tokens)
        self.assertNotIn("context_literal_value", tokens)

    def test_common_words_and_short_fragments_are_ignored(self):
        tokens = changed_tokens("+    x = 'id'\n+    y = 'name'\n+    z = 'a'\n+    if x == 'true':")
        self.assertEqual(tokens, set())

    def test_literal_present_on_both_sides_is_not_a_change(self):
        tokens = changed_tokens("-    key = 'agenda_confirmation'\n+    key = 'agenda_confirmation' + suffix")
        self.assertNotIn("agenda_confirmation", tokens)


class TestFindConsumers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = GitRepo(self.tmp.name)
        self.repo.commit({
            "svc/keys.py": "def template_keys(slug):\n    return f'reminder_{slug}_v1'\n",
            "svc/scope.py": ("import re\n\n\ndef unit_scope(key):\n"
                             "    return re.fullmatch(r'reminder_(north|south)_v1', key)\n\n\n"
                             "def unrelated():\n    return 1\n"),
            "svc/caller.py": "from svc.keys import template_keys\n\n\ndef build(u):\n    return template_keys(u)\n",
            "tests/test_scope.py": "def test_it():\n    assert 'reminder_north_v1'\n",
        }, "base")
        self.head = self.repo.head()

    def tearDown(self):
        self.tmp.cleanup()

    def test_maps_hits_to_enclosing_functions_outside_changed_files(self):
        consumers = find_consumers(self.tmp.name, self.head, {"reminder_", "template_keys"},
                                   exclude_files={"svc/keys.py"})
        found = {(c["file"], c["function"]): c["tokens"] for c in consumers}
        self.assertEqual(found[("svc/scope.py", "unit_scope")], ["reminder_"])
        self.assertEqual(found[("svc/caller.py", "build")], ["template_keys"])
        self.assertNotIn(("svc/scope.py", "unrelated"), found)
        self.assertFalse(any(f.startswith("tests/") for f, _ in found))

    def test_private_names_only_count_in_their_own_file(self):
        self.repo.commit({
            "svc/a.py": "def _as_utc(x):\n    return x\n\n\ndef user(x):\n    return _as_utc(x)\n",
            "svc/b.py": "def _as_utc(y):\n    return y\n\n\ndef other(y):\n    return _as_utc(y)\n",
        }, "private")
        consumers = find_consumers(self.tmp.name, self.repo.head(), {"_as_utc"},
                                   exclude_functions={("svc/a.py", "_as_utc")}, token_files={"_as_utc": {"svc/a.py"}})
        found = {(c["file"], c["function"]) for c in consumers}
        self.assertIn(("svc/a.py", "user"), found)
        self.assertNotIn(("svc/b.py", "other"), found)

    def test_migration_history_is_not_a_consumer(self):
        self.repo.commit({"db/migrations/versions/0001_init.py": "def upgrade():\n    op.add('reminder_x')\n"}, "mig")
        consumers = find_consumers(self.tmp.name, self.repo.head(), {"reminder_"}, exclude_files=set())
        self.assertFalse(any("migrations" in c["file"] for c in consumers))

    def test_overly_common_tokens_are_dropped(self):
        consumers = find_consumers(self.tmp.name, self.head, {"return"}, exclude_files=set(), max_files_per_token=2)
        self.assertEqual(consumers, [])


if __name__ == "__main__":
    unittest.main()
