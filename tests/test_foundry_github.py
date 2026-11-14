"""A War Room project on the CEO's GitHub: only on their word, never forced."""

import asyncio
import json
import subprocess

import pytest

from app.foundry.github_ops import GitHubError, GitHubOps, _web


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(autouse=True)
def never_the_real_github(monkeypatch):
    """No test here may reach the CEO's GitHub: any real gh call fails the test instead of running."""
    real = subprocess.run

    def guarded(argv, *a, **kw):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]).endswith("gh"):
            raise AssertionError(f"a test tried to run the real gh: {argv}")
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("print('hi')\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


@pytest.fixture
def remote(tmp_path, project):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    git(project, "remote", "add", "origin", str(bare))
    return bare


def test_publishing_creates_a_private_repository_from_the_project_and_pushes_it(project, tmp_path):
    calls = []
    bare = tmp_path / "on-github.git"

    def runner(argv, **kw):
        calls.append(argv)
        if argv[0] == "gh":  # never the real gh: a test must not create anything on anyone's GitHub
            if argv[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(argv, 0, "someone\n", "")
            if argv[:3] == ["gh", "repo", "create"]:  # what gh would do, with a folder standing in for GitHub
                git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
                git(project, "remote", "add", "origin", str(bare))
                git(project, "push", "-q", "-u", "origin", "main")
                return subprocess.CompletedProcess(argv, 0, "", "")
            raise AssertionError(f"unexpected gh call: {argv}")
        assert argv[0] == "git"
        return subprocess.run(argv, **kw)

    state = GitHubOps(runner).publish(project, "notes-app")
    create = next(c for c in calls if c[:3] == ["gh", "repo", "create"])
    assert create[3] == "notes-app" and "--private" in create and "--push" in create and create[create.index("--source") + 1] == str(project)
    assert state["ahead"] == 0 and git(bare, "log", "--oneline", "main").endswith("first")


@pytest.mark.parametrize("name", ["", "has space", "x/y", "a" * 101])
def test_a_bad_repository_name_is_refused(project, name):
    with pytest.raises(GitHubError, match="repository name"):
        GitHubOps().publish(project, name)


def test_a_project_already_on_github_is_pushed_and_not_created_again(project, remote):
    with pytest.raises(GitHubError, match="already on GitHub"):
        GitHubOps().publish(project, "again")


def test_push_and_pull_work_against_a_real_remote_and_never_force(project, remote, tmp_path):
    ops = GitHubOps()
    state = ops.push(project)
    assert state["ahead"] == 0 and git(remote, "log", "--oneline", "main").endswith("first")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "new.py").write_text("x = 1\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "from elsewhere")
    git(other, "push", "-q", "origin", "main")
    ops.pull(project)
    assert (project / "new.py").is_file()


def test_a_pull_that_would_merge_over_your_work_is_refused(project, remote, tmp_path):
    ops = GitHubOps()
    ops.push(project)
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "a.py").write_text("theirs\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "theirs")
    git(other, "push", "-q", "origin", "main")
    (project / "b.py").write_text("mine\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "mine")
    with pytest.raises(GitHubError):
        ops.pull(project)  # fast-forward only: nothing is merged over the CEO's work


def test_no_command_ever_forces():
    import inspect

    from app.foundry import github_ops

    assert "--force" not in inspect.getsource(github_ops).replace("never --force", "") and "-f\"" not in inspect.getsource(github_ops)


def test_a_web_address_never_carries_credentials():
    assert _web("https://user:token123@github.com/me/app.git") == "https://github.com/me/app"
    assert _web("git@github.com:me/app.git") == "https://github.com/me/app"


class TestAgents:
    ANALYSIS = {"kind": "software", "size": "small", "summary": "x", "needs": [{"name": "Backend engineer", "work": "build", "why": "x"}]}

    def scopes(self, tmp_path, goal):
        from tests.test_foundry_engine import Thinker, build, until

        scopes = []

        async def go():
            engine, _, _ = build(tmp_path, Thinker(analysis=self.ANALYSIS), script=lambda kind, prompt, cwd: json.dumps({"summary": "fine", "findings": []}) if kind == "review" else json.dumps({"summary": "x", "status": "done"}))
            real = engine.session_registry.add
            engine.session_registry.add = lambda session, scope: (scopes.append(dict(scope)), real(session, scope))[1]
            wf = await engine.start(goal, autonomy="supervisor")
            await until(lambda: engine.get(wf["id"])["status"] in ("done", "failed", "stopped"), 30)

        asyncio.run(go())
        return [s for s in scopes if not str(s.get("task", "")).endswith("-review")]

    def test_github_is_offered_only_when_you_asked_for_it_in_the_job(self, tmp_path):
        assert all(not s.get("github") for s in self.scopes(tmp_path / "a", "build a notes app"))
        assert all(s.get("github") for s in self.scopes(tmp_path / "b", "build a notes app and push it to github with a CI pipeline"))

    def test_an_agent_asks_you_and_it_happens_only_after_the_work_is_finished(self, tmp_path):
        from app.foundry.engine import TeamTools

        asked, saved = [], {}

        class Toolbox:
            def owns(self, tool):
                return False

            def schemas(self, scope):
                return []

            def _name(self, scope):
                return "Backend engineer"

            async def _confirm(self, scope, decision):
                asked.append(decision.summary)
                return True

        live = type("L", (), {"id": "w1", "wf": {"project": str(tmp_path / "notes-app")}})()
        engine = type("E", (), {"flows": {"w1": live}, "_save": lambda self, l, **kw: saved.update(kw)})()
        tools = TeamTools(engine, None, Toolbox())
        text, ok = asyncio.run(tools.call("github", {"action": "publish", "name": "notes app"}, {"workflow": "w1", "github": True}))
        assert ok and asked and "private" in asked[0] and saved["github_wanted"] == {"action": "publish", "name": "notes-app", "private": True} and "as soon as the finished work" in text


def test_only_you_can_publish_push_or_pull(tmp_path):
    from app.foundry.engine import FoundryError
    from app.foundry.service import FoundryService

    service = FoundryService.__new__(FoundryService)
    service.engine = service.skills = service.registry = None
    for command in ("github_publish", "github_push", "github_pull"):
        with pytest.raises(FoundryError, match="only you"):
            asyncio.run(service.dispatch(command, {"workflow": "w1"}, confirmed=False))
