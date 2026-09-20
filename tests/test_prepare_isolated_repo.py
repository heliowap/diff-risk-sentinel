import subprocess
import tempfile
import unittest
from pathlib import Path
from tests.gitutil import GitRepo
from evals.review_cost.prepare_isolated_repo import (
    prepare_isolated_repo,
    verify_isolation,
    IsolationError,
    IsolatedRepoInfo,
)


def _cat_file_exists(repo_path: Path, sha: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo_path), "cat-file", "-e", sha],
        capture_output=True,
    )
    return res.returncode == 0


def _rev_parse_commit(repo_path: Path, sha: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", f"{sha}^{{commit}}"],
        capture_output=True,
    )
    return res.returncode == 0


class TestPrepareIsolatedRepo(unittest.TestCase):

    def test_prepare_isolated_repo_excludes_future_commits(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            src_dir = tmp_path / "src"
            dst_dir = tmp_path / "dst"
            src_dir.mkdir(parents=True, exist_ok=True)
            src = GitRepo(str(src_dir))

            src.commit({"file.py": "def foo():\n    return 0\n"}, "init")
            c0 = src.head()

            src.commit({"file.py": "def foo():\n    return 1\n"}, "base commit")
            c1 = src.head()

            src.commit({"file.py": "def foo():\n    return 1 / 0\n"}, "target with defect")
            c2 = src.head()

            src.commit({"file.py": "def foo():\n    return 42\n"}, "future fix commit")
            c3 = src.head()

            # Isolate at c2 (target), ensuring c3 (fix) is unreachable
            info = prepare_isolated_repo(
                source_repo=src_dir,
                target_commit=c2,
                dest_dir=dst_dir,
                base_commit=c1,
                unreachable_commits=[c3],
            )

            self.assertIsInstance(info, IsolatedRepoInfo)
            self.assertEqual(info.target_commit, c2)
            self.assertEqual(info.dest_dir, dst_dir.resolve())

            # 1. Target commit is present and checked out
            current_head = subprocess.run(
                ["git", "-C", str(dst_dir), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(current_head, c2)

            # 2. Base commit is present and diff matches
            diff_res = subprocess.run(
                ["git", "-C", str(dst_dir), "diff", f"{c1}..{c2}"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("return 1 / 0", diff_res.stdout)

            # 3. Future commit (c3) must be completely absent from objects and logs
            self.assertFalse(_cat_file_exists(dst_dir, c3))
            self.assertFalse(_rev_parse_commit(dst_dir, c3))

            log_all = subprocess.run(
                ["git", "-C", str(dst_dir), "log", "--all", "--format=%H"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            self.assertNotIn(c3, log_all)
            self.assertIn(c2, log_all)
            self.assertIn(c1, log_all)

            # 4. No remotes exist pointing to source
            remotes = subprocess.run(
                ["git", "-C", str(dst_dir), "remote"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(remotes, "")

    def test_verify_isolation_raises_when_forbidden_commit_leaked(self):
        with tempfile.TemporaryDirectory() as td:
            src_dir = Path(td) / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            src = GitRepo(str(src_dir))
            src.commit({"f.py": "v1\n"}, "c0")
            c0 = src.head()
            src.commit({"f.py": "v2\n"}, "c1")
            c1 = src.head()

            # Target c0, but source contains c1. If we verify c0 against forbidden c0, it must fail.
            with self.assertRaises(IsolationError) as ctx:
                verify_isolation(src_dir, forbidden_commits=[c0])
            self.assertIn("reachable or present", str(ctx.exception))

            # Verifying an actually absent SHA should pass
            fake_sha = "0123456789abcdef0123456789abcdef01234567"
            report = verify_isolation(src_dir, forbidden_commits=[fake_sha])
            self.assertTrue(report.is_isolated)
            self.assertEqual(report.violations, [])

    def test_prepare_isolated_repo_cli(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            src_dir = tmp_path / "src_cli"
            dst_dir = tmp_path / "dst_cli"
            src_dir.mkdir(parents=True, exist_ok=True)
            src = GitRepo(str(src_dir))

            src.commit({"mod.py": "a = 1\n"}, "initial")
            c0 = src.head()
            src.commit({"mod.py": "a = 2\n"}, "target")
            c1 = src.head()
            src.commit({"mod.py": "a = 3\n"}, "fix")
            c2 = src.head()

            cmd = [
                "python3",
                "-m",
                "evals.review_cost.prepare_isolated_repo",
                "--source",
                str(src_dir),
                "--target",
                c1,
                "--dest",
                str(dst_dir),
                "--assert-unreachable",
                c2,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"CLI failed: {res.stderr}")

            self.assertTrue(_rev_parse_commit(dst_dir, c1))
            self.assertFalse(_rev_parse_commit(dst_dir, c2))

    def test_invalid_base_commit_raises(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            src_dir = tmp_path / "src_bad_base"
            dst_dir = tmp_path / "dst_bad_base"
            src_dir.mkdir(parents=True, exist_ok=True)
            src = GitRepo(str(src_dir))
            src.commit({"a.py": "1\n"}, "init")
            c0 = src.head()

            non_existent_base = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
            with self.assertRaises(IsolationError) as ctx:
                prepare_isolated_repo(
                    source_repo=src_dir,
                    target_commit=c0,
                    dest_dir=dst_dir,
                    base_commit=non_existent_base,
                )
            self.assertIn("not reachable", str(ctx.exception))

    def test_custom_branch_name(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            src_dir = tmp_path / "src_branch"
            dst_dir = tmp_path / "dst_branch"
            src_dir.mkdir(parents=True, exist_ok=True)
            src = GitRepo(str(src_dir))
            src.commit({"a.py": "1\n"}, "init")
            c0 = src.head()

            info = prepare_isolated_repo(
                source_repo=src_dir,
                target_commit=c0,
                dest_dir=dst_dir,
                branch_name="review-target",
            )
            branch = subprocess.run(
                ["git", "-C", str(dst_dir), "branch", "--show-current"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(branch, "review-target")
            self.assertEqual(info.branch_name, "review-target")
