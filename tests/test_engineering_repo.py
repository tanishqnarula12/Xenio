"""V6 repository safety: the user's checkout is never modified by a run, user changes are preserved and kept apart from
agent changes, task work is isolated in worktrees, conflicts are detected, and repository hooks never run.
All on disposable repositories in pytest's tmp_path."""

import subprocess
from pathlib import Path

import pytest

from app.engineering import repo as R


def sh(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=User", "-c", "user.email=user@example.com", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "api.py").write_text("def ping():\n    return 'pong'\n")
    (root / "src" / "ui.js").write_text("export const title = 'Home';\n")
    (root / "README.md").write_text("# Project\n")
    (root / ".gitignore").write_text("secret.env\nnode_modules/\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'proj'\n")
    sh(root, "init", "-q", "-b", "main")
    sh(root, "add", "-A")
    sh(root, "commit", "-q", "-m", "initial")
    return root


def state_of(root):
    """Everything about the user's checkout that must never change."""
    files = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    return files, sh(root, "status", "--porcelain=v1"), sh(root, "rev-parse", "HEAD"), sh(root, "stash", "list"), \
        (root / ".git" / "index").read_bytes()


def test_inspect_reports_user_changes_exactly(project):
    (project / "src" / "api.py").write_text("def ping():\n    return 'PONG'\n")  # modified
    (project / "notes.txt").write_text("my notes\n")  # untracked
    (project / "README.md").write_text("# Project\nmore\n")
    sh(project, "add", "README.md")  # staged
    (project / "secret.env").write_text("KEY=1\n")  # ignored: not reported
    s = R.inspect(project)
    assert s.is_git and s.has_commits and s.branch == "main" and s.dirty
    assert s.modified == ["src/api.py"] and s.untracked == ["notes.txt"] and s.staged == ["README.md"]
    assert "python" in s.stack and "your uncommitted work" in s.summary()


def test_a_run_never_touches_the_users_checkout_and_builds_on_their_work(project, tmp_path):
    (project / "src" / "api.py").write_text("def ping():\n    return 'PONG'\n")
    (project / "notes.txt").write_text("my notes\n")
    before = state_of(project)
    ws = R.Workspace.create(R.inspect(project), "run-0001", tmp_path / "runs")
    assert state_of(project) == before  # index, files, HEAD, stash: untouched
    wt = ws.task_worktree("api")
    assert (wt / "src" / "api.py").read_text().endswith("'PONG'\n") and (wt / "notes.txt").exists()  # user's work is the base
    assert not str(wt).startswith(str(project))  # outside the repository
    (wt / "src" / "api.py").write_text("def ping():\n    return 'PONG!'\n")
    assert ws.commit_task("api", "api change")
    ok, conflicts = ws.merge_task("api")
    assert ok and not conflicts
    assert ws.changed_files(ws.integration_branch) == [("M", "src/api.py")]  # only the agent's change
    assert "notes.txt" not in ws.patch()
    assert state_of(project) == before
    log = sh(project, "log", "--format=%an", ws.integration_branch, "-3")
    assert "XENIO engineering" in log  # agent commits are labelled as such


def test_disjoint_parallel_tasks_merge_and_overlapping_ones_conflict(project, tmp_path):
    ws = R.Workspace.create(R.inspect(project), "run-0002", tmp_path / "runs")
    api, ui = ws.task_worktree("api"), ws.task_worktree("ui")
    (api / "src" / "api.py").write_text("def ping():\n    return 'api'\n")
    (ui / "src" / "ui.js").write_text("export const title = 'Dashboard';\n")
    ws.commit_task("api", "api"), ws.commit_task("ui", "ui")
    assert ws.merge_task("api") == (True, []) and ws.merge_task("ui") == (True, [])
    assert {p for _s, p in ws.changed_files(ws.integration_branch)} == {"src/api.py", "src/ui.js"}
    # two tasks that changed the same lines: the second merge is aborted, integration stays as it was
    a, b = ws.task_worktree("a"), ws.task_worktree("b")
    (a / "README.md").write_text("# A\n")
    (b / "README.md").write_text("# B\n")
    ws.commit_task("a", "a"), ws.commit_task("b", "b")
    head = ws.head()
    assert ws.merge_task("a")[0]
    ok, conflicts = ws.merge_task("b")
    assert not ok and conflicts == ["README.md"]
    assert ws.head() == ws.head(ws.integration_branch) and ws.head() != head  # a merged, b not
    assert R.git(Path(ws.integration_dir), "status", "--porcelain").strip() == ""  # merge aborted cleanly


def test_applying_writes_only_the_agents_changes_and_keeps_the_users(project, tmp_path):
    (project / "notes.txt").write_text("my notes\n")
    (project / "src" / "api.py").write_text("def ping():\n    return 'USER'\n")
    ws = R.Workspace.create(R.inspect(project), "run-0003", tmp_path / "runs")
    wt = ws.task_worktree("ui")
    (wt / "src" / "ui.js").write_text("export const title = 'Agent';\n")
    ws.commit_task("ui", "ui"), ws.merge_task("ui")
    assert ws.apply_check() == (True, "")
    assert ws.apply_to_source() == ["src/ui.js"]
    assert (project / "src" / "ui.js").read_text() == "export const title = 'Agent';\n"
    assert (project / "src" / "api.py").read_text().endswith("'USER'\n") and (project / "notes.txt").exists()
    assert "src/ui.js" not in sh(project, "diff", "--cached", "--name-only")  # never staged or committed for the user


def test_apply_refuses_when_the_user_changed_the_same_lines_meanwhile(project, tmp_path):
    ws = R.Workspace.create(R.inspect(project), "run-0004", tmp_path / "runs")
    wt = ws.task_worktree("api")
    (wt / "src" / "api.py").write_text("def ping():\n    return 'agent'\n")
    ws.commit_task("api", "x"), ws.merge_task("api")
    (project / "src" / "api.py").write_text("def ping():\n    return 'user edited meanwhile'\n")
    ok, why = ws.apply_check()
    assert not ok and why
    with pytest.raises(R.RepoError, match="do not apply cleanly"):
        ws.apply_to_source()
    assert (project / "src" / "api.py").read_text().endswith("'user edited meanwhile'\n")


def test_repository_hooks_never_run(project, tmp_path):
    hook = project / ".git" / "hooks" / "pre-commit"
    marker = tmp_path / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    sh(project, "config", "core.fsmonitor", f"touch {marker}.fsmonitor; true")
    ws = R.Workspace.create(R.inspect(project), "run-0005", tmp_path / "runs")
    wt = ws.task_worktree("t")
    (wt / "x.txt").write_text("x")
    ws.commit_task("t", "x"), ws.merge_task("t")
    assert not marker.exists() and not Path(f"{marker}.fsmonitor").exists()


def test_a_merge_or_rebase_in_progress_is_left_alone(project, tmp_path):
    (project / ".git" / "MERGE_HEAD").write_text(sh(project, "rev-parse", "HEAD"))
    with pytest.raises(R.RepoError, match="merge is in progress"):
        R.Workspace.create(R.inspect(project), "run-0006", tmp_path / "runs")


def test_a_plain_folder_is_worked_on_in_a_private_copy(tmp_path):
    folder = tmp_path / "plain"
    (folder / "node_modules" / "big").mkdir(parents=True)
    (folder / "app.py").write_text("print(1)\n")
    (folder / "node_modules" / "big" / "x.js").write_text("x")
    ws = R.Workspace.create(R.inspect(folder), "run-0007", tmp_path / "runs")
    assert ws.copied and not (folder / ".git").exists()  # the user's folder stays as it was
    wt = ws.task_worktree("t")
    assert (wt / "app.py").exists() and not (wt / "node_modules").exists()
    with pytest.raises(R.RepoError, match="private copy"):
        ws.apply_to_source()


def test_a_repository_without_commits_still_gets_a_base(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    (root / "a.txt").write_text("a")
    sh(root, "init", "-q", "-b", "main")
    s = R.inspect(root)
    assert s.is_git and not s.has_commits and s.untracked == ["a.txt"]
    ws = R.Workspace.create(s, "run-0008", tmp_path / "runs")
    assert (Path(ws.task_worktree("t")) / "a.txt").exists()
    assert sh(root, "status", "--porcelain=v1") == "?? a.txt\n"  # still untouched


def test_cleanup_removes_only_this_runs_worktrees_and_branches(project, tmp_path):
    sh(project, "branch", "users-own-branch")
    ws = R.Workspace.create(R.inspect(project), "run-0009", tmp_path / "runs")
    ws.task_worktree("t")
    ws.cleanup(keep_branches=False)
    branches = sh(project, "branch", "--format=%(refname:short)").split()
    assert "users-own-branch" in branches and not any(b.startswith("xenio/") for b in branches)
    assert sh(project, "worktree", "list").count("\n") == 1
