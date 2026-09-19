import hashlib
import importlib.util
import io
from contextlib import redirect_stderr
import os
import tempfile
import unittest

from tests.gitutil import GitRepo

_SPEC = importlib.util.spec_from_file_location(
    "public_safety_check", os.path.join(os.path.dirname(__file__), "..", "scripts", "public_safety_check.py"))
ps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ps)


# Fixtures are split so the test file itself holds no literal PII (the checker scans it too).
AT = "@"
CPF = "529.982" + ".247-25"
CPF_DIGITS = "529982" + "24725"
CNPJ = "11.222.333" + "/0001-81"
PERSON = "maria.silva" + AT + "gmail.com"
ANA = "ana" + AT + "corp.io"
JOAO = "joao" + AT + "corp.io"
ALLOWED = "it" + AT + "allowed.example.org"
USERS, HOME = "/Us" + "ers/", "/ho" + "me/"


def digest(term):
    return hashlib.sha256(term.lower().encode()).hexdigest()


class TestTextRules(unittest.TestCase):

    def setUp(self):
        self.policy = ps.Policy(denylist={digest("acmecorp"), digest("secretproject")},
                                allowlist=[ALLOWED])

    def kinds(self, text, path="src/x.py"):
        return sorted({f.kind for f in ps.scan_text(path, text, self.policy)})

    def test_clean_code_passes(self):
        self.assertEqual(self.kinds("def add(a, b):\n    return a + b\n"), [])

    def test_valid_cpf_and_cnpj_are_pii(self):
        self.assertEqual(self.kinds(f"cpf = '{CPF}'"), ["pii:cpf"])
        self.assertEqual(self.kinds(f"doc = {CPF_DIGITS}"), ["pii:cpf"])
        self.assertEqual(self.kinds(f"cnpj = '{CNPJ}'"), ["pii:cnpj"])

    def test_numbers_that_fail_the_check_digits_pass(self):
        self.assertEqual(self.kinds("ts = 12345678901\nsize = '111.222.333-44'"), [])

    def test_emails_outside_the_allowlist_are_pii(self):
        self.assertEqual(self.kinds(f"contact = '{PERSON}'"), ["pii:email"])
        for ok in ("test@example.com", "noreply@anthropic.com", ALLOWED,
                   "1234+bot@users.noreply.github.com"):
            self.assertEqual(self.kinds(f"x = '{ok}'"), [], ok)

    def test_escaped_newline_before_a_decorator_is_not_an_email(self):
        self.assertEqual(self.kinds("src = 'x = 1\\n@router.post(\"/a\")'"), [])

    def test_image_names_and_placeholder_addresses_are_not_emails(self):
        for text in ("![hero](live-NN" + AT + "2x.png)", "icon" + AT + "3x.webp", "email: your" + AT + "email.com",
                     "you" + AT + "yourdomain.com", "user" + AT + "domain.com"):
            self.assertEqual(self.kinds(text), [], text)

    def test_brazilian_phone_numbers_are_pii(self):
        self.assertEqual(self.kinds("tel = '+55 81 " + "99876-5432'"), ["pii:phone"])
        self.assertEqual(self.kinds("tel = '(81) " + "99876-5432'"), ["pii:phone"])

    def test_secrets(self):
        self.assertEqual(self.kinds("KEY = 'apikey_" + "a1b2c3d4e5f6g7h8i9'"), ["secret"])
        self.assertEqual(self.kinds("t = 'ghp_" + "A" * 36 + "'"), ["secret"])
        self.assertEqual(self.kinds("-----BEGIN RSA " + "PRIVATE KEY-----"), ["secret"])
        self.assertEqual(self.kinds("export TYPESAFE_API_KEY=\"apikey_...\""), [])  # placeholder

    def test_local_absolute_paths(self):
        self.assertEqual(self.kinds(f"repo = '{USERS}someone/work/app'"), ["local-path"])
        self.assertEqual(self.kinds(f"repo = '{HOME}someone/app'"), ["local-path"])
        self.assertEqual(self.kinds("--repo /path/to/repo"), [])
        self.assertEqual(self.kinds("local paths such as `/Users/...` or /home/...)"), [])
        for placeholder in ("yourname", "username", "you", "me", "<user>", "USER", "$USER"):
            self.assertEqual(self.kinds(f"cd {USERS}{placeholder}/project"), [], placeholder)
        self.assertEqual(self.kinds(f"flagged {USERS}yourname."), [])  # trailing punctuation

    def test_denylisted_terms_match_whole_words_and_word_parts(self):
        self.assertEqual(self.kinds("see acmecorp-platform"), ["private-term"])
        self.assertEqual(self.kinds("class SecretProject: pass"), ["private-term"])
        self.assertEqual(self.kinds("acmecorporate is fine"), [])

    def test_allowlisted_strings_are_removed_before_scanning(self):
        self.assertEqual(self.kinds(f"author = '{ALLOWED}'"), [])

    def test_findings_carry_line_numbers(self):
        found = ps.scan_text("a.py", f"ok = 1\nmail = '{ANA}'\n", self.policy)
        self.assertEqual([(f.path, f.line) for f in found], [("a.py", 2)])


class TestPathRules(unittest.TestCase):

    def test_eval_data_files_need_an_explicit_allowance(self):
        policy = ps.Policy(allowed_data=["evals/dataset.example.json"])
        self.assertEqual([f.kind for f in ps.scan_path("evals/results.json", policy)], ["eval-data"])
        self.assertEqual([f.kind for f in ps.scan_path("evals/private/x.md", policy)], ["eval-data"])
        self.assertEqual(ps.scan_path("evals/dataset.example.json", policy), [])
        self.assertEqual(ps.scan_path("evals/historical_eval.py", policy), [])


class TestPrivateRepositories(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.private)
        repo = GitRepo(self.private)
        repo.commit({"svc/billing.py": "class InvoiceReconciliationService:\n    def run(self):\n        return 1\n"}, "init")
        self.sha = repo.head()
        self.public = os.path.join(self.tmp.name, "public")
        os.makedirs(self.public)
        GitRepo(self.public).commit({"src/tool.py": "def extract_functions():\n    return []\n"}, "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_private_commit_shas_and_identifiers_are_flagged(self):
        index = ps.PrivateIndex([self.private], own_repo=self.public)
        text = f"regression from {self.sha[:9]} in InvoiceReconciliationService\n"
        kinds = sorted({f.kind for f in ps.scan_text("evals/notes.md", text, ps.Policy(), index)})
        self.assertEqual(kinds, ["private-commit", "private-identifier"])

    def test_plain_words_and_names_common_to_several_private_repos_are_not_flagged(self):
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        GitRepo(other).commit({"cli.py": "def parse_arguments():\n    pass\n"}, "init")
        GitRepo(self.private).commit({"cli.py": "def parse_arguments():\n    pass\n", "ui.ts": "const description = 1;\n"}, "more")
        index = ps.PrivateIndex([self.private, other], own_repo=self.public)
        text = "parse_arguments and a description of InvoiceReconciliationService"
        self.assertEqual([f.detail for f in ps.scan_text("a.md", text, ps.Policy(), index)], ["InvoiceReconciliationService"])

    def test_two_part_names_and_dunders_are_too_generic(self):
        GitRepo(self.private).commit({"q.ts": "const queryClient = new QueryClient();\n",
                                      "m.py": "class Rec:\n    def __post_init__(self):\n        pass\n"}, "generic")
        index = ps.PrivateIndex([self.private], own_repo=self.public)
        self.assertEqual(ps.scan_text("a.ts", "queryClient.invalidate(); obj.__post_init__()", ps.Policy(), index), [])

    def test_identifiers_also_defined_in_this_repo_are_not_flagged(self):
        GitRepo(self.private).commit({"svc/util.py": "def extract_functions():\n    return 0\n"}, "more")
        index = ps.PrivateIndex([self.private], own_repo=self.public)
        self.assertEqual(ps.scan_text("a.md", "uses extract_functions here", ps.Policy(), index), [])


class TestGitModes(unittest.TestCase):

    def test_staged_mode_scans_the_index_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"a.py": "x = 1\n"}, "init")
            with open(os.path.join(tmp, "a.py"), "w") as fh:
                fh.write(f"mail = '{ANA}'\n")
            repo.git("add", "a.py")
            with open(os.path.join(tmp, "a.py"), "w") as fh:
                fh.write("x = 2\n")  # unstaged fix does not hide what is being committed
            found = ps.check(tmp, mode="staged", policy=ps.Policy())
        self.assertEqual([f.kind for f in found], ["pii:email"])

    def test_range_mode_scans_changed_files_and_commit_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"a.py": "x = 1\n"}, "init")
            base = repo.head()
            repo.commit({"b.py": "y = 2\n"}, f"fix for {PERSON}")
            found = ps.check(tmp, mode="range", rev_range=f"{base}..HEAD", policy=ps.Policy())
        self.assertEqual([(f.kind, f.path) for f in found], [("pii:email", "commit message")])

    def test_new_branch_range_scans_the_whole_tree_and_unpushed_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"a.py": f"mail = '{ANA}'\n"}, f"init by {JOAO}")
            found = ps.check(tmp, mode="range", rev_range="..HEAD", policy=ps.Policy())
        self.assertEqual(sorted((f.kind, f.path) for f in found),
                         [("pii:email", "a.py"), ("pii:email", "commit message")])



class TestJevLayer(unittest.TestCase):

    def fake_judge(self, flag_word):
        calls = []

        def judge(api_key, path, text):
            calls.append((path, text))
            hit = flag_word in text
            return {"p_personal_data": 0.95 if hit else 0.01, "p_private_project": 0.1, "p_sensitive": 0.95 if hit else 0.1}
        return judge, calls

    def test_added_hunks_and_messages_are_judged_and_flagged(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"notes.md": "intro\n\nold text stays\n"}, "init")
            base = repo.head()
            repo.commit({"notes.md": "intro\n\nold text stays\nplaceholder person FLAGME (fictitious)\n"}, "add note")
            judge, calls = self.fake_judge("FLAGME")
            with mock.patch.object(ps.sensitive_judge, "judge", side_effect=judge):
                found = ps.check(tmp, mode="range", rev_range=f"{base}..HEAD", policy=ps.Policy(), jev_key="k")
        self.assertEqual([(f.kind, f.path) for f in found], [("jev:personal-data", "notes.md")])
        judged = dict(calls)
        self.assertEqual(judged["notes.md"], "placeholder person FLAGME (fictitious)")  # only the added lines
        self.assertIn("commit message", judged)

    def test_allowlisted_strings_are_removed_before_judging(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"a.toml": "x = 1\n"}, "init")
            base = repo.head()
            repo.commit({"a.toml": f"x = 1\nauthors = ['Maintainer <{ALLOWED}>']\n"}, "author")
            judge, calls = self.fake_judge("never")
            with mock.patch.object(ps.sensitive_judge, "judge", side_effect=judge):
                ps.check(tmp, mode="range", rev_range=f"{base}..HEAD", policy=ps.Policy(allowlist=[ALLOWED]), jev_key="k")
        self.assertNotIn(ALLOWED, dict(calls)["a.toml"])

    def test_non_utf8_text_does_not_crash_the_scan(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"a.txt": "x\n"}, "init")
            base = repo.head()
            with open(os.path.join(tmp, "legacy.txt"), "wb") as fh:
                fh.write("caf\u00e9 com a\u00e7\u00facar\n".encode("latin-1"))
            repo.git("add", "legacy.txt")
            repo.git("commit", "-q", "-m", "latin-1 file")
            judge, calls = self.fake_judge("never")
            with mock.patch.object(ps.sensitive_judge, "judge", side_effect=judge):
                found = ps.check(tmp, mode="range", rev_range=f"{base}..HEAD", policy=ps.Policy(), jev_key="k")
        self.assertEqual(found, [])
        self.assertIn("legacy.txt", dict(calls))

    def test_private_project_threshold_and_api_failures(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            repo = GitRepo(tmp)
            repo.commit({"a.md": "x\n"}, "init")
            base = repo.head()
            repo.commit({"a.md": "x\nour clinic's reminder rules\n", "b.md": "unrelated text\n"}, "more")

            def judge(api_key, path, text):
                if path == "b.md":
                    return {"error": "HTTP 503"}
                p = 0.85 if path == "a.md" else 0.1
                return {"p_personal_data": 0.0, "p_private_project": p, "p_sensitive": p}
            out = io.StringIO()
            with mock.patch.object(ps.sensitive_judge, "judge", side_effect=judge), redirect_stderr(out):
                found = ps.check(tmp, mode="range", rev_range=f"{base}..HEAD", policy=ps.Policy(), jev_key="k")
        self.assertEqual([(f.kind, f.path) for f in found], [("jev:private-project", "a.md")])
        self.assertIn("could not be judged", out.getvalue())


if __name__ == "__main__":
    unittest.main()
