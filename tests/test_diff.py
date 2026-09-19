import os
import subprocess
import tempfile
import unittest

from diff_risk_sentinel.diff import (
    GitError,
    default_base,
    get_diff,
    parse_unified_diff,
    read_blobs,
    resolve_revisions,
)
from tests.gitutil import GitRepo


SAMPLE = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -10,4 +10,5 @@ def f():
     a = 1
-    b = 2
+    b = 3
+    c = 4
     return a
diff --git a/old name.py b/new name.py
similarity index 90%
rename from old name.py
rename to new name.py
--- a/old name.py
+++ b/new name.py
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def g():
-    pass
diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"
--- "a/caf\\303\\251.py"
+++ "b/caf\\303\\251.py"
@@ -3,2 +2,0 @@
-y = 1
-z = 2
"""


class TestParseUnifiedDiff(unittest.TestCase):

    def setUp(self):
        self.files = {(f.old_path, f.new_path): f for f in parse_unified_diff(SAMPLE)}

    def test_changed_lines_exclude_context(self):
        f = self.files[("src/app.py", "src/app.py")]
        self.assertEqual(f.added_lines, {11, 12})
        # a replaced line is anchored to its replacement
        self.assertEqual(f.touched_lines, {11, 12})

    def test_pure_deletion_is_anchored_to_preceding_line(self):
        f = self.files[("gone.py", None)]
        self.assertEqual(f.touched_lines, set())
        self.assertEqual(f.removed_count, 2)

    def test_rename_keeps_both_paths(self):
        self.assertIn(("old name.py", "new name.py"), self.files)

    def test_deleted_file_has_no_new_path(self):
        self.assertIn(("gone.py", None), self.files)

    def test_quoted_paths_are_unescaped(self):
        f = self.files[("café.py", "café.py")]
        # a pure deletion sits between new lines 2 and 3
        self.assertEqual(f.touched_lines, {2.5})

    def test_snippet_for_range_only_includes_lines_in_range(self):
        f = self.files[("src/app.py", "src/app.py")]
        self.assertEqual(f.snippet_for(11, 11).splitlines(), ["-    b = 2", "+    b = 3"])


class TestLineSeparators(unittest.TestCase):

    def test_only_newline_ends_a_diff_line(self):
        for odd in ("\x0c", "\u2028", "\x85", "\x1c"):
            with self.subTest(sep=repr(odd)):
                text = ("diff --git a/x.js b/x.js\n--- a/x.js\n+++ b/x.js\n@@ -1,3 +1,3 @@\n"
                        f" const s = \"a{odd}b\";\n-x\n+y\n z\n")
                self.assertEqual(parse_unified_diff(text)[0].added_lines, {2})


class TestGitIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = GitRepo(self.tmp.name)
        self.repo.commit({"a.py": "x = 1\n"}, "base")
        self.base_sha = self.repo.head()
        self.repo.commit({"a.py": "x = 2\n", "b.py": "y = 1\n"}, "change")
        self.head_sha = self.repo.head()

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_bare_base_uses_merge_base_and_head(self):
        self.assertEqual(
            resolve_revisions("HEAD~1", repo=self.tmp.name),
            (self.base_sha, self.head_sha),
        )

    def test_resolve_two_dot_range(self):
        self.assertEqual(
            resolve_revisions(f"{self.base_sha}..{self.head_sha}", repo=self.tmp.name),
            (self.base_sha, self.head_sha),
        )

    def test_get_diff_ignores_user_prefix_and_color_config(self):
        self.repo.git("config", "diff.noprefix", "true")
        self.repo.git("config", "color.ui", "always")
        old, new, files = get_diff("HEAD~1", repo=self.tmp.name)
        self.assertEqual(sorted(f.new_path for f in files), ["a.py", "b.py"])

    def test_invalid_ref_raises_git_error_naming_the_ref(self):
        with self.assertRaises(GitError) as ctx:
            get_diff("does-not-exist", repo=self.tmp.name)
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_default_base_falls_back_to_local_main(self):
        self.assertEqual(default_base(self.tmp.name), "main")

    def test_default_base_prefers_origin_head(self):
        self.repo.git("update-ref", "refs/remotes/origin/trunk", self.base_sha)
        self.repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        self.assertEqual(default_base(self.tmp.name), "origin/trunk")

    def test_read_blobs_stays_aligned_after_a_non_blob(self):
        self.repo.commit({"pkg/x.py": "z = 1\n"}, "dir")
        head = self.repo.head()
        blobs = read_blobs(self.tmp.name, [(head, "pkg"), (head, "a.py")])
        self.assertIsNone(blobs[(head, "pkg")])
        self.assertEqual(blobs[(head, "a.py")], "x = 2\n")

    def test_lone_carriage_return_is_not_a_line_break(self):
        self.repo.commit({"cr.py": "a = 1\nb = '\r'\nc = 1\n"}, "cr")
        self.repo.commit({"cr.py": "a = 1\nb = '\r'\nc = 2\n"}, "cr2")
        _, _, files = get_diff("HEAD~1", repo=self.tmp.name)
        self.assertEqual(files[0].added_lines, {3})

    def test_read_blobs_reads_revision_not_working_tree(self):
        with open(os.path.join(self.tmp.name, "a.py"), "w") as fh:
            fh.write("dirty working tree\n")
        blobs = read_blobs(self.tmp.name, [(self.base_sha, "a.py"), (self.head_sha, "a.py"), (self.base_sha, "b.py")])
        self.assertEqual(blobs[(self.base_sha, "a.py")], "x = 1\n")
        self.assertEqual(blobs[(self.head_sha, "a.py")], "x = 2\n")
        self.assertIsNone(blobs[(self.base_sha, "b.py")])


if __name__ == "__main__":
    unittest.main()
