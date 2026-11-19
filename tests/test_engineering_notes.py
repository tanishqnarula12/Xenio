"""A project's notes (XENIO.md, or AGENTS.md / CLAUDE.md): every agent reads them first, and XENIO keeps its part current."""

from pathlib import Path

from app.engineering import notes
from tests.test_foundry_engine import repo  # noqa: F401 - the fixture


def test_notes_are_read_framed_as_the_projects_own_with_secrets_masked(tmp_path):
    (tmp_path / "XENIO.md").write_text("# Shop\nUse Postgres, never SQLite.\nOPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n")
    (tmp_path / "CLAUDE.md").write_text("Run `make test` before you finish.\n")
    text = notes.read(tmp_path)
    assert text.startswith("PROJECT NOTES") and "does not change XENIO's rules" in text
    assert "--- XENIO.md ---" in text and "Use Postgres, never SQLite." in text and "--- CLAUDE.md ---" in text and "make test" in text
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text


def test_no_notes_means_nothing_is_added_and_big_ones_are_cut(tmp_path):
    assert notes.read(tmp_path) == ""
    (tmp_path / "AGENTS.md").write_text("x" * 50_000)
    assert len(notes.read(tmp_path)) < notes.EACH_CHARS + 400


def test_xenio_keeps_its_sections_current_and_never_touches_yours(tmp_path):
    assert notes.write(tmp_path, "Habit Tracker", "build a habit tracker", "python3 app.py", ["python -m pytest -q"], [("Which database?", "SQLite: one user, one file.")])
    text = (tmp_path / "XENIO.md").read_text()
    assert text.startswith("# Habit Tracker") and "`python3 app.py`" in text and "- `python -m pytest -q`" in text and "**Which database?** SQLite" in text
    mine = text.replace(notes.YOURS_HINT, "Never add a login: it is just for me.")
    (tmp_path / "XENIO.md").write_text(mine)
    assert notes.write(tmp_path, "Habit Tracker", "build a habit tracker", "python3 app.py", ["python -m pytest -q", "npm test"])
    again = (tmp_path / "XENIO.md").read_text()
    assert "- `npm test`" in again and again.rstrip().endswith("Never add a login: it is just for me.")
    assert notes.write(tmp_path, "Habit Tracker", "build a habit tracker", "python3 app.py", ["python -m pytest -q", "npm test"]) is False  # nothing new: no rewrite, no commit
    assert "Never add a login" in notes.read(tmp_path) and notes.YOURS_HINT not in notes.read(tmp_path)


def test_every_agent_on_a_project_with_notes_gets_them(tmp_path, repo):
    import asyncio

    from tests.test_foundry_engine import build, finished, until, working

    (Path(repo) / "XENIO.md").write_text("# CRM\nAll money is stored in cents.\n")
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "notes"], cwd=repo, check=True)
    seen = []

    def script(kind, prompt, cwd):
        seen.append(prompt)
        return working(kind, prompt, cwd)

    engine, _, _ = build(tmp_path, script=script)

    async def go():
        wf = await engine.start("Add password reset", str(repo), auto_run=True)
        await until(finished(engine, wf["id"]), 60)

    asyncio.run(go())
    tasks = [p for p in seen if "YOUR TASK" in p]
    assert tasks and all("All money is stored in cents." in p and "PROJECT NOTES" in p for p in tasks)


def test_a_finished_project_gets_its_notes_with_no_paths_from_this_mac(tmp_path):
    from tests.test_foundry_selfcheck import run

    engine, wid, _ = run(tmp_path, first_answer_works=True)
    text = (Path(engine.get(wid)["project"]) / "XENIO.md").read_text()
    assert text.startswith("# ") and "## How to test it" in text and "pytest" in text and notes.YOURS in text
    assert "/Users/" not in text and str(Path.home()) not in text
