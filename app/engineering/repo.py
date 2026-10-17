"""Repository preflight and isolation for engineering runs (V6).

The user's checkout is never modified by a run. XENIO:

1. **inspects** the repository: root, branch, HEAD, staged / modified / untracked / conflicted files, stack, remotes;
2. **snapshots** the user's current state into a commit object (tracked changes *and* untracked, non-ignored files)
   through a temporary index, so the user's index, working tree and stash stay exactly as they were. Agents build on
   what the user actually has, and the final diff separates the user's changes (in the snapshot) from the agents'
   (snapshot..result);
3. **isolates** work in git worktrees outside the repository (``~/Xenio/engineering/<run>/``): one integration
   worktree, plus one per writing task, branched from the integration state that includes the task's finished
   dependencies. A folder that is not a git repository is copied first and the copy becomes a private repository;
4. **integrates** finished task branches into the integration branch with ordinary merges; a conflict aborts that
   merge and is reported, nothing is forced;
5. **applies** the result to the user's checkout only when asked (a separate, confirmed step): the patch holds only
   the agents' changes and is checked against the user's current files first.

Every git call disables hooks, fsmonitor, pagers, external diff drivers and textconv: a repository's own
configuration must not make XENIO run programs (a cloned repo can ship such settings). No command here contacts a
remote, resets, cleans or force-anything.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

AGENT_NAME, AGENT_EMAIL = "XENIO engineering", "xenio-engineering@localhost"
BRANCH_PREFIX = "xenio"
_COPY_IGNORE = ("node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build", ".next", "target",
                ".mypy_cache", ".ruff_cache", ".tox", ".gradle", ".idea", ".DS_Store", "*.pyc")
MAX_COPY_BYTES = 500 * 1024 * 1024
# One lock per repository for git operations on shared state (worktree list, the integration worktree's index):
# parallel tasks finishing at the same moment must not merge at the same time (git's index.lock would refuse one).
_REPO_LOCKS: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


class RepoError(Exception):
    pass


def _safe_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "PAGER": "cat",
                "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"})
    env.update(extra or {})
    return env


def git(cwd: Path, *args: str, timeout: float = 60, env: dict | None = None, check: bool = True,
        binary: bool = False):
    """Run git with everything that could execute repository-supplied programs switched off."""
    cmd = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat",
           "-c", "credential.helper=", "-c", "protocol.allow=never", "-c", "diff.external=",
           "-c", f"user.name={AGENT_NAME}", "-c", f"user.email={AGENT_EMAIL}", *args]
    try:
        done = subprocess.run(cmd, cwd=str(cwd), capture_output=True, timeout=timeout, env=_safe_env(env),
                              text=not binary, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RepoError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise RepoError(f"git {args[0]} timed out after {timeout:.0f} s") from None
    if check and done.returncode != 0:
        err = done.stderr if isinstance(done.stderr, str) else done.stderr.decode("utf-8", "replace")
        raise RepoError(f"git {' '.join(args[:2])} failed: {err.strip()[-300:]}")
    return done.stdout if check else done


@dataclass
class RepoState:
    root: str
    is_git: bool
    has_commits: bool = False
    branch: str = ""
    head: str = ""
    detached: bool = False
    staged: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)
    remotes: list[str] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)
    in_progress: str = ""  # merge | rebase | cherry-pick | revert | bisect (the user is in the middle of something)

    @property
    def dirty(self) -> bool:
        return bool(self.staged or self.modified or self.untracked or self.conflicted)

    def summary(self) -> str:
        if not self.is_git:
            return f"{self.root} (not a git repository: work happens in a private copy)"
        parts = [f"{self.root} on {'detached HEAD' if self.detached else self.branch or '?'}"
                 + (f" at {self.head[:8]}" if self.head else " (no commits yet)")]
        user = [f"{len(v)} {k}" for k, v in (("staged", self.staged), ("modified", self.modified),
                                              ("untracked", self.untracked), ("conflicted", self.conflicted)) if v]
        parts.append("your uncommitted work: " + ", ".join(user) if user else "clean working tree")
        if self.in_progress:
            parts.append(f"a {self.in_progress} is in progress")
        return "; ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)


_STACK_MARKERS = {"python": ("pyproject.toml", "setup.py", "requirements.txt", "pytest.ini", "setup.cfg"),
                  "node": ("package.json",), "go": ("go.mod",), "rust": ("Cargo.toml",), "java": ("pom.xml", "build.gradle"),
                  "ruby": ("Gemfile",), "php": ("composer.json",), "dotnet": ("*.csproj", "*.sln")}


def detect_stack(root: Path) -> list[str]:
    names = {p.name for p in root.iterdir()} if root.is_dir() else set()
    return [stack for stack, markers in _STACK_MARKERS.items()
            if any(fnmatch.filter(names, m) if "*" in m else m in names for m in markers)]


def inspect(path: str | Path) -> RepoState:
    """Facts about a folder the user wants worked on (read-only)."""
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise RepoError(f"{p} is not a folder")
    probe = git(p, "rev-parse", "--show-toplevel", check=False)
    if probe.returncode != 0:
        return RepoState(str(p), False, stack=detect_stack(p))
    root = Path(probe.stdout.strip())
    state = RepoState(str(root), True, stack=detect_stack(root))
    head = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    state.has_commits = head.returncode == 0
    state.head = head.stdout.strip() if state.has_commits else ""
    branch = git(root, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    state.branch = branch.stdout.strip()
    state.detached = branch.returncode != 0
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    for entry in [e for e in status.split("\0") if e]:
        code, name = entry[:2], entry[3:]
        if code == "??":
            state.untracked.append(name)
        elif "U" in code or code in ("AA", "DD"):
            state.conflicted.append(name)
        else:
            if code[0] not in (" ", "?"):
                state.staged.append(name)
            if code[1] not in (" ", "?"):
                state.modified.append(name)
    state.remotes = [r for r in git(root, "remote").split() if r]
    gitdir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    for marker, label in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                          ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if (gitdir / marker).exists():
            state.in_progress = label
            break
    return state


def snapshot(root: Path, message: str = "XENIO snapshot of your working tree") -> str:
    """A commit holding the user's current files (tracked changes + untracked, non-ignored files), parented on HEAD
    when there is one. Built with a temporary index: the user's index, working tree and stash are untouched."""
    with tempfile.TemporaryDirectory(prefix="xenio-index-") as tmp:
        index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        has_head = git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
        if has_head:
            git(root, "read-tree", "HEAD", env=index)
        git(root, "add", "-A", "--", ".", env=index, timeout=300)
        tree = git(root, "write-tree", env=index).strip()
    parent = ["-p", "HEAD"] if has_head else []
    return git(root, "commit-tree", tree, *parent, "-m", message).strip()


def private_copy(source: Path, dest: Path) -> Path:
    """For a folder that is not a git repository: copy it (no dependencies/build output) and make the copy a private
    repository, so the same isolation applies. The original folder is never written to."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if not any(fnmatch.fnmatch(d, pat) for pat in _COPY_IGNORE)]
        for f in filenames:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
        if total > MAX_COPY_BYTES:
            raise RepoError(f"{source} is larger than {MAX_COPY_BYTES // 2**20} MB without dependencies; put it under "
                            "git (git init) so XENIO can use worktrees instead of a copy")
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*_COPY_IGNORE), symlinks=True)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "--allow-empty", "-m", "XENIO private copy of your folder")
    return dest


@dataclass
class Workspace:
    """Where one engineering run works: the source repository, its snapshot base, and the run's worktrees."""

    run_id: str
    source: str  # the user's folder
    repo: str  # the repository the worktrees belong to (the user's repo, or the private copy)
    base: str  # commit the run started from (the snapshot)
    run_dir: str
    integration_branch: str
    integration_dir: str
    copied: bool = False
    task_dirs: dict[str, str] = field(default_factory=dict)
    task_branches: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Workspace":
        return cls(**data)

    # ---- creating ---------------------------------------------------------------------------------------------

    @classmethod
    def create(cls, state: RepoState, run_id: str, runs_root: Path) -> "Workspace":
        if not re.fullmatch(r"[a-z0-9-]{4,40}", run_id):
            raise RepoError("invalid run id")
        run_dir = runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        source = Path(state.root)
        if state.is_git:
            if state.in_progress:
                raise RepoError(f"a {state.in_progress} is in progress in {source}; finish or abort it first (XENIO "
                                "does not touch a repository in that state)")
            repo, copied = source, False
            base = snapshot(source) if state.dirty or not state.has_commits else state.head
        else:
            repo, copied = private_copy(source, run_dir / "copy"), True
            base = git(repo, "rev-parse", "HEAD").strip()
        branch = f"{BRANCH_PREFIX}/{run_id}/integration"
        integration = run_dir / "integration"
        git(repo, "worktree", "add", "-q", "-b", branch, str(integration), base)
        return cls(run_id, str(source), str(repo), base, str(run_dir), branch, str(integration), copied)

    def task_worktree(self, task_id: str) -> Path:
        """A fresh worktree for a writing task, branched from the current integration state."""
        if task_id in self.task_dirs:
            return Path(self.task_dirs[task_id])
        branch = f"{BRANCH_PREFIX}/{self.run_id}/{task_id}"
        path = Path(self.run_dir) / "tasks" / task_id
        path.parent.mkdir(parents=True, exist_ok=True)
        with _REPO_LOCKS[self.repo]:
            git(Path(self.repo), "worktree", "add", "-q", "-b", branch, str(path), self.integration_branch)
        self.task_dirs[task_id], self.task_branches[task_id] = str(path), branch
        return path

    # ---- recording and integrating ------------------------------------------------------------------------------

    def commit_task(self, task_id: str, message: str) -> str:
        """Commit whatever the task changed in its worktree (as XENIO engineering). '' when nothing changed."""
        path = Path(self.task_dirs[task_id])
        git(path, "add", "-A")
        if git(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return ""
        git(path, "commit", "-q", "-m", message)
        return git(path, "rev-parse", "HEAD").strip()

    def changed_files(self, ref: str, since: str | None = None) -> list[tuple[str, str]]:
        """[(status, path)] between ``since`` (default: the run's base) and ``ref``."""
        out = git(Path(self.repo), "diff", "--no-ext-diff", "--no-textconv", "--name-status", "--no-renames", "-z",
                  f"{since or self.base}..{ref}")
        items = [x for x in out.split("\0") if x]
        return [(items[i], items[i + 1]) for i in range(0, len(items) - 1, 2)]

    def diff(self, ref: str, since: str | None = None, limit: int = 200_000) -> str:
        text = git(Path(self.repo), "diff", "--no-ext-diff", "--no-textconv", "--no-color", f"{since or self.base}..{ref}")
        return text if len(text) <= limit else text[:limit] + f"\n[... diff cut at {limit} characters]"

    def diff_stat(self, ref: str, since: str | None = None) -> str:
        return git(Path(self.repo), "diff", "--no-ext-diff", "--stat", "--no-color", f"{since or self.base}..{ref}").strip()

    def merge_task(self, task_id: str) -> tuple[bool, list[str]]:
        """Merge a finished task branch into the integration branch. (True, []) or (False, conflicting files) after
        aborting the merge (the integration worktree is left exactly as before)."""
        branch = self.task_branches.get(task_id)
        if not branch:
            return True, []
        integration = Path(self.integration_dir)
        with _REPO_LOCKS[self.repo]:
            done = git(integration, "merge", "--no-ff", "--no-edit", "-m", f"XENIO: integrate {task_id}", branch,
                       check=False, timeout=120)
            if done.returncode == 0:
                return True, []
            conflicted = [f for f in git(integration, "diff", "--name-only", "--diff-filter=U").split("\n") if f]
            git(integration, "merge", "--abort", check=False)
        if not conflicted:  # not a conflict: say what git really said
            raise RepoError(f"merging {task_id} failed: {(done.stderr or done.stdout).strip()[-300:]}")
        return False, conflicted

    def head(self, ref: str | None = None) -> str:
        return git(Path(self.repo), "rev-parse", ref or self.integration_branch).strip()

    def remove_task_worktree(self, task_id: str) -> None:
        path = self.task_dirs.get(task_id)
        if path and Path(path).exists():
            with _REPO_LOCKS[self.repo]:
                git(Path(self.repo), "worktree", "remove", "--force", path, check=False)

    def cleanup(self, keep_branches: bool = True) -> None:
        """Remove this run's worktrees (only the ones it created). Branches stay unless asked."""
        for tid in list(self.task_dirs):
            self.remove_task_worktree(tid)
        if Path(self.integration_dir).exists():
            git(Path(self.repo), "worktree", "remove", "--force", self.integration_dir, check=False)
        git(Path(self.repo), "worktree", "prune", check=False)
        if not keep_branches:
            for branch in [*self.task_branches.values(), self.integration_branch]:
                if branch.startswith(f"{BRANCH_PREFIX}/{self.run_id}/"):  # only branches this run created
                    git(Path(self.repo), "branch", "-D", branch, check=False)

    # ---- applying to the user's checkout (a separate, confirmed step) -------------------------------------------

    def patch(self) -> str:
        """The agents' changes only (snapshot..integration), as a binary-safe patch."""
        return git(Path(self.repo), "diff", "--no-ext-diff", "--no-textconv", "--binary", f"{self.base}..{self.integration_branch}")

    def apply_check(self) -> tuple[bool, str]:
        """Would the patch apply cleanly to the user's files as they are now? Nothing is changed."""
        patch = self.patch()
        if not patch.strip():
            return False, "there is nothing to apply"
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as fh:
            fh.write(patch)
        try:
            done = git(Path(self.source), "apply", "--check", "--whitespace=nowarn", fh.name, check=False)
            return done.returncode == 0, (done.stderr or "").strip()[-400:]
        finally:
            os.unlink(fh.name)

    def apply_to_source(self) -> list[str]:
        """Write the agents' changes into the user's working tree (never commits, never touches the index or other
        files). Only call after the user confirmed. Returns the changed paths."""
        if self.copied:
            raise RepoError("this run worked on a private copy; copy the changed files back yourself, or put the "
                            "folder under git so XENIO can apply changes")
        ok, why = self.apply_check()
        if not ok:
            raise RepoError(f"the changes do not apply cleanly to your current files: {why}")
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as fh:
            fh.write(self.patch())
        try:
            git(Path(self.source), "apply", "--whitespace=nowarn", fh.name)
        finally:
            os.unlink(fh.name)
        return [p for _s, p in self.changed_files(self.integration_branch)]


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)
