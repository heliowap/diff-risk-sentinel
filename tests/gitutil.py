import os
import subprocess


class GitRepo:
    """Minimal throwaway git repository for integration tests."""

    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.path, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, files, message):
        for rel, content in files.items():
            full = os.path.join(self.path, rel)
            if content is None:
                self.git("rm", "-q", rel)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
            self.git("add", rel)
        self.git("commit", "-q", "-m", message)

    def head(self):
        return self.git("rev-parse", "HEAD")
