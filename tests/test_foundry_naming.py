"""What the War Room makes gets a real name: a real repository was called build-a-landing-page-for-a-new-a-b69f."""

import subprocess

import pytest

from app.foundry import naming
from app.foundry.factory import read_brief
from app.foundry.github_ops import GitHubError, GitHubOps


@pytest.mark.parametrize("goal, name", [
    ("build a landing page for a new arts college normal page use skills we have already there", "arts-college-landing-page"),
    ("build a habit tracker web app: a FastAPI backend with a SQLite database, and a clean page", "habit-tracker-web-app"),
    ("make me a portfolio website", "portfolio-website"),
    ("a weather dashboard for Delhi using open-meteo", "delhi-weather-dashboard"),
    ("Add password reset", "password-reset"),
    ("!!!", "project"),
])
def test_a_name_says_what_it_is(goal, name):
    assert naming.from_goal(goal) == name


def test_a_name_is_short_and_safe_for_github():
    name = naming.from_goal("build " + " ".join(f"thing{i}" for i in range(40)))
    assert len(name) <= naming.MAX_CHARS and name.count("-") < naming.MAX_WORDS and naming.slug("Ünïcode / slashes ../..") == "n-code-slashes"


def test_the_plans_suggestion_is_used_when_it_is_a_name():
    assert naming.checked("Arts College Landing Page", "x") == "arts-college-landing-page"
    assert naming.checked("", "make me a portfolio website") == "portfolio-website"
    assert naming.checked("a b c d e f g h", "make me a portfolio website") == "portfolio-website"  # a sentence is not a name
    assert read_brief({"name": "Arts College Site!"})["name"] == "arts-college-site"


def test_titles_and_taken_folders(tmp_path):
    assert naming.title("gym-saas-api") == "Gym SaaS API" and naming.title("arts-college-landing-page") == "Arts College Landing Page"
    (tmp_path / "site").mkdir()
    (tmp_path / "site-2").mkdir()
    assert naming.unique(tmp_path, "site") == tmp_path / "site-3" and naming.unique(tmp_path, "other") == tmp_path / "other"


@pytest.fixture(autouse=True)
def never_the_real_github(monkeypatch):
    real = subprocess.run

    def guarded(argv, *a, **kw):
        if isinstance(argv, (list, tuple)) and argv and str(argv[0]).endswith("gh"):
            raise AssertionError(f"a test tried to run the real gh: {argv}")
        return real(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", guarded)


def on_github(tmp_path, url="https://github.com/me/build-a-landing-page-for-a-new-a-b69f.git"):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("1\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=T", "-c", "user.email=t@t", "commit", "-q", "-m", "first"], ["remote", "add", "origin", url]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def test_a_repository_on_github_is_renamed_and_pushes_go_to_the_new_name(tmp_path):
    root = on_github(tmp_path)
    asked = []

    def runner(argv, **kw):
        if argv[0] == "gh":
            asked.append(argv)
            return subprocess.CompletedProcess(argv, 0, "me\n" if argv[:3] == ["gh", "api", "user"] else "", "")
        return subprocess.run(argv, **kw)

    out = GitHubOps(runner).rename(root, "arts-college-landing-page")
    assert ["gh", "repo", "rename", "arts-college-landing-page", "--yes"] in asked
    assert out["remote"] == "https://github.com/me/arts-college-landing-page"
    assert subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True).stdout.strip() == "https://github.com/me/arts-college-landing-page.git"


def test_a_bad_name_or_a_project_not_on_github_is_refused_before_anything_runs(tmp_path):
    root = on_github(tmp_path)
    ops = GitHubOps(lambda argv, **kw: pytest.fail(f"ran {argv}") if argv[0] == "gh" else subprocess.run(argv, **kw))
    with pytest.raises(GitHubError):
        ops.rename(root, "arts college; rm -rf /")
    subprocess.run(["git", "remote", "remove", "origin"], cwd=root, check=True)
    with pytest.raises(GitHubError, match="not on GitHub yet"):
        ops.rename(root, "fine-name")


def test_a_job_gets_a_named_folder_a_titled_readme_and_the_plans_name_for_github(tmp_path):
    import asyncio
    from pathlib import Path

    from tests.test_foundry_engine import ANALYSIS, StubPreviews, TestYouAreTheCEO, covering, finished, until

    helper = TestYouAreTheCEO()
    thinker = covering([{"complete": True, "missing": []}] * 3)
    thinker.topics = []
    thinker.analysis = {**ANALYSIS, "name": "Arts College Site"}
    engine, _, _ = helper.start(tmp_path, thinker, [helper.PLAN1], previews=StubPreviews())
    goal = "build a landing page for a new arts college normal page use skills we have already there"

    async def go():
        wf = await engine.start(goal, autonomy="supervisor")
        await until(finished(engine, wf["id"]), 40)
        return engine.get(wf["id"])

    wf = asyncio.run(go())
    folder = Path(wf["project"])
    assert folder.name == "arts-college-landing-page"  # not build-a-landing-page-for-a-new-a-b69f
    assert (folder / "README.md").read_text().startswith(f"# Arts College Landing Page\n\n> {goal}\n")
    assert wf["name"] == "arts-college-site"  # what the plan chose: the default name on GitHub
