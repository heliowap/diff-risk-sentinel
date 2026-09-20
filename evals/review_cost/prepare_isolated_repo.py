from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence


class IsolationError(Exception):
    """Raised when repository isolation is compromised or fails verification."""
    pass


@dataclass
class IsolationReport:
    is_isolated: bool
    violations: List[str] = field(default_factory=list)


@dataclass
class IsolatedRepoInfo:
    dest_dir: Path
    target_commit: str
    base_commit: Optional[str] = None
    branch_name: str = "main"


def _run_git(args: Sequence[str], cwd: Path | str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
    )


def verify_isolation(
    repo_path: Path | str,
    forbidden_commits: Sequence[str],
    raise_on_error: bool = True,
) -> IsolationReport:
    """
    Verifies that none of the forbidden commits are reachable, resolvable, or
    present as objects in the target repository.
    """
    repo = Path(repo_path)
    violations: List[str] = []

    # 1. Check if git log --all contains any forbidden commit
    log_res = _run_git(["log", "--all", "--format=%H"], cwd=repo, check=False)
    existing_commits = set(log_res.stdout.split()) if log_res.returncode == 0 else set()

    # 2. Check all ref targets
    ref_res = _run_git(["for-each-ref", "--format=%(objectname)"], cwd=repo, check=False)
    ref_targets = set(ref_res.stdout.split()) if ref_res.returncode == 0 else set()

    for commit in forbidden_commits:
        commit_clean = commit.strip()
        if not commit_clean:
            continue

        if commit_clean in existing_commits:
            violations.append(f"Commit {commit_clean} found in git log --all")

        if commit_clean in ref_targets:
            violations.append(f"Commit {commit_clean} referenced by a local ref")

        # 3. Check cat-file -e (object database existence)
        cat_res = _run_git(["cat-file", "-e", commit_clean], cwd=repo, check=False)
        if cat_res.returncode == 0:
            violations.append(f"Commit {commit_clean} object exists in git database (cat-file -e)")

        # 4. Check rev-parse --verify ^{commit}
        rev_res = _run_git(["rev-parse", "--verify", f"{commit_clean}^{{commit}}"], cwd=repo, check=False)
        if rev_res.returncode == 0:
            violations.append(f"Commit {commit_clean} resolves via rev-parse")

    is_isolated = len(violations) == 0
    if not is_isolated and raise_on_error:
        raise IsolationError(
            f"Isolation verification failed in {repo_path}: commit is reachable or present:\n"
            + "\n".join(violations)
        )

    return IsolationReport(is_isolated=is_isolated, violations=violations)


def prepare_isolated_repo(
    source_repo: Path | str,
    target_commit: str,
    dest_dir: Path | str,
    base_commit: Optional[str] = None,
    branch_name: str = "main",
    unreachable_commits: Optional[Sequence[str]] = None,
) -> IsolatedRepoInfo:
    """
    Creates an isolated clone of source_repo at dest_dir whose history strictly
    terminates at target_commit. Objects and commits subsequent to target_commit
    are completely omitted.
    """
    src = Path(source_repo).resolve()
    dst = Path(dest_dir).resolve()

    if not src.exists():
        raise FileNotFoundError(f"Source repository does not exist: {src}")

    dst.mkdir(parents=True, exist_ok=True)

    # Initialize empty git repository
    _run_git(["init", "-q"], cwd=dst)

    # Fetch only target_commit and its ancestors from file:// URL
    # Using file:// ensures git uses the transfer protocol rather than local hardlinks
    src_url = f"file://{src}"
    fetch_res = _run_git(
        ["fetch", "--no-tags", "--no-recurse-submodules", src_url, target_commit],
        cwd=dst,
        check=False,
    )
    if fetch_res.returncode != 0:
        raise RuntimeError(
            f"Failed to fetch {target_commit} from {src_url}: {fetch_res.stderr.strip()}"
        )

    # Checkout target_commit onto specified branch
    _run_git(["checkout", "-q", "-B", branch_name, "FETCH_HEAD"], cwd=dst)

    # If base_commit is specified, verify it is present and create a 'base' branch
    if base_commit:
        base_cat = _run_git(["cat-file", "-e", base_commit], cwd=dst, check=False)
        if base_cat.returncode != 0:
            raise IsolationError(
                f"base_commit {base_commit} is not reachable from target_commit {target_commit}"
            )
        _run_git(["branch", "-f", "base", base_commit], cwd=dst)

    # Ensure no remotes exist
    remotes_res = _run_git(["remote"], cwd=dst, check=False)
    if remotes_res.returncode == 0:
        for r in remotes_res.stdout.split():
            if r.strip():
                _run_git(["remote", "remove", r.strip()], cwd=dst, check=False)

    # Expire reflog and prune all unreachable objects
    _run_git(["reflog", "expire", "--expire=now", "--all"], cwd=dst, check=False)
    _run_git(["gc", "--prune=now", "-q"], cwd=dst, check=False)

    # Verify that forbidden commits are truly unreachable
    if unreachable_commits:
        verify_isolation(dst, unreachable_commits, raise_on_error=True)

    return IsolatedRepoInfo(
        dest_dir=dst,
        target_commit=target_commit,
        base_commit=base_commit,
        branch_name=branch_name,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare an isolated git repository terminating at a specific target commit."
    )
    parser.add_argument("--source", required=True, help="Path to source git repository")
    parser.add_argument("--target", required=True, help="Target commit SHA (commit to review)")
    parser.add_argument("--dest", required=True, help="Path to create isolated repository")
    parser.add_argument("--base", default=None, help="Optional base commit SHA (parent / base branch)")
    parser.add_argument("--branch", default="main", help="Branch name for target commit (default: main)")
    parser.add_argument(
        "--assert-unreachable",
        action="append",
        default=[],
        help="Assert that this commit SHA is completely absent and unreachable (can specify multiple times)",
    )

    args = parser.parse_args()

    info = prepare_isolated_repo(
        source_repo=args.source,
        target_commit=args.target,
        dest_dir=args.dest,
        base_commit=args.base,
        branch_name=args.branch,
        unreachable_commits=args.assert_unreachable,
    )
    print(f"Isolated repository prepared at {info.dest_dir} (HEAD at {info.target_commit})")


if __name__ == "__main__":
    main()
