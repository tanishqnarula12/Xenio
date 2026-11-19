"""A War Room project on the CEO's GitHub: create its repository, push, pull. Through the `gh` sign-in already on this Mac.

Every one of these leaves the Mac, so each happens only on the CEO's word: their click, or their yes to an agent that asks.
Nothing is ever forced: no force push, no history rewritten, a pull only when it fast-forwards. No token passes through
XENIO: `gh` and git use the CEO's own sign-in, and git hooks in the project do not run.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
GIT = ["git", "-c", "core.hooksPath=/dev/null"]


class GitHubError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class GitHubOps:
    def __init__(self, runner=None):
        self._run = runner or (lambda *a, **kw: subprocess.run(*a, **kw))  # looked up when used, so a test can stand in for it

    def _do(self, argv: list[str], cwd: Path, timeout: float = 180.0) -> str:
        try:
            done = self._run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise GitHubError(f"{argv[0]} is not installed on this Mac", 503) from None
        except subprocess.TimeoutExpired:
            raise GitHubError(f"{argv[0]} took too long", 504) from None
        if done.returncode != 0:
            said = " ".join((done.stderr or done.stdout or "").split())[-300:]
            raise GitHubError(said or f"{argv[0]} failed")
        return (done.stdout or "").strip()

    def _project(self, project: str | Path) -> Path:
        root = Path(project)
        if not (root / ".git").exists():
            raise GitHubError("this job has no project folder under version control yet", 422)
        return root

    def signed_in(self) -> str:
        """The GitHub account `gh` is signed in as, or ""."""
        try:
            return self._do(["gh", "api", "user", "--jq", ".login"], Path.home(), timeout=20)
        except GitHubError:
            return ""

    def status(self, project: str | Path) -> dict:
        root = self._project(project)
        branch = self._do([*GIT, "rev-parse", "--abbrev-ref", "HEAD"], root)
        try:
            remote = self._do([*GIT, "remote", "get-url", "origin"], root)
        except GitHubError:
            remote = ""
        ahead = behind = 0
        if remote:
            try:
                counts = self._do([*GIT, "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"], root).split()
                behind, ahead = int(counts[0]), int(counts[1])
            except (GitHubError, ValueError, IndexError):
                ahead = -1  # never pushed
        return {"branch": branch, "remote": _web(remote), "ahead": ahead, "behind": behind}

    def publish(self, project: str | Path, name: str, private: bool = True, description: str = "") -> dict:
        """A new repository under the CEO's account, with this project pushed to it."""
        root = self._project(project)
        name = str(name or "").strip()
        if not REPO_NAME.match(name):
            raise GitHubError("a repository name is letters, numbers, dots, dashes and underscores", 422)
        if self.status(root)["remote"]:
            raise GitHubError("this project is already on GitHub: push to it instead", 409)
        if not self.signed_in():
            raise GitHubError("GitHub is not signed in on this Mac: run gh auth login once", 401)
        argv = ["gh", "repo", "create", name, "--private" if private else "--public", "--source", str(root), "--remote", "origin", "--push"]
        if description:
            argv += ["--description", " ".join(description.split())[:300]]
        self._do(argv, root, timeout=240)
        return self.status(root)

    def rename(self, project: str | Path, name: str) -> dict:
        """Gives the project's repository a new name on GitHub; GitHub sends the old address on to the new one."""
        root = self._project(project)
        name = str(name or "").strip()
        if not REPO_NAME.match(name):
            raise GitHubError("a repository name is letters, numbers, dots, dashes and underscores", 422)
        state = self.status(root)
        if not state["remote"]:
            raise GitHubError("this project is not on GitHub yet: name it when you put it there", 409)
        if state["remote"].rstrip("/").rsplit("/", 1)[-1] == name:
            return state
        if not self.signed_in():
            raise GitHubError("GitHub is not signed in on this Mac: run gh auth login once", 401)
        before = self._do([*GIT, "remote", "get-url", "origin"], root)
        self._do(["gh", "repo", "rename", name, "--yes"], root, timeout=60)  # the repository this folder's origin points at
        after = re.sub(r"[^/:]+?(\.git)?/?$", lambda m: name + (m.group(1) or ""), before.strip(), count=1)
        self._do([*GIT, "remote", "set-url", "origin", after], root)  # so pushes go to the new name, whether or not gh already moved it
        return self.status(root)

    def push(self, project: str | Path) -> dict:
        root = self._project(project)
        state = self.status(root)
        if not state["remote"]:
            raise GitHubError("this project is not on GitHub yet: put it there first", 409)
        self._do([*GIT, "push", "-u", "origin", state["branch"]], root)  # never --force
        return self.status(root)

    def pull(self, project: str | Path) -> dict:
        root = self._project(project)
        state = self.status(root)
        if not state["remote"]:
            raise GitHubError("this project is not on GitHub yet", 409)
        self._do([*GIT, "pull", "--ff-only", "origin", state["branch"]], root)  # only when it fast-forwards: nothing of yours is merged over
        return self.status(root)


def _web(remote: str) -> str:
    """The repository's web address, never a URL with credentials in it."""
    remote = re.sub(r"https://[^@/]+@", "https://", remote.strip())
    found = re.match(r"^(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?$", remote)
    return f"https://github.com/{found.group(1)}" if found else remote
