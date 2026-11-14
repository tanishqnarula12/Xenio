"""What the CEO's real jobs of 2026-11-10 showed: a working Django app was reported untested, could "not start", and was then broken by reviews."""

import pytest

from app.engineering import verify
from app.foundry import preview
from app.foundry.engine import _QUESTION, for_what_was_asked


def test_djangos_own_tests_are_found_and_run(tmp_path):
    (tmp_path / "manage.py").write_text("")
    (tmp_path / "todos").mkdir()
    (tmp_path / "todos" / "tests.py").write_text("")
    found = verify.detect(tmp_path)
    assert [c.name for c in found] == ["django"] and found[0].argv[1:] == ["manage.py", "test", "--noinput"]


def test_a_start_script_of_several_lines_runs_as_a_whole(tmp_path):
    (tmp_path / ".xenio-run").write_text('#!/bin/bash\ncd "$(dirname "$0")"\npython manage.py migrate --no-input\npython manage.py runserver 127.0.0.1:${PORT:-8000}\n')
    assert preview.detect(tmp_path, "python").how == "bash .xenio-run"
    (tmp_path / ".xenio-run").write_text("uvicorn main:app --port $PORT\n")
    assert preview.detect(tmp_path, "python").how == "uvicorn main:app --port $PORT"


def live(goal, greenfield=True):
    return type("L", (), {"wf": {"goal": goal, "greenfield": greenfield, "constraints": []}})()


FINDINGS = [{"kind": "security", "severity": "high", "summary": "No authentication or authorization anywhere: every todo can be read by any client"},
            {"kind": "security", "severity": "high", "summary": "DEBUG defaults to True and ALLOWED_HOSTS is a wildcard"},
            {"kind": "review", "severity": "high", "summary": "The score counts deleted todos twice"}]


def test_a_personal_tool_is_not_made_to_grow_logins_nobody_asked_for():
    out = for_what_was_asked([dict(f) for f in FINDINGS], live("build a full stack web application to score my daily progress and to dos"))
    assert [f["severity"] for f in out] == ["low", "low", "high"] and out[0]["summary"].startswith("For when it goes online")


@pytest.mark.parametrize("goal, greenfield", [("build a todo app with user accounts and login", True), ("build a SaaS for gyms and deploy it", True), ("fix the bug", False)])
def test_when_you_ask_for_accounts_or_it_is_your_own_repository_security_findings_stand(goal, greenfield):
    out = for_what_was_asked([dict(f) for f in FINDINGS], live(goal, greenfield))
    assert [f["severity"] for f in out] == ["high", "high", "high"]


@pytest.mark.parametrize("text, question", [("is the work done whats the status!? :)", True), ("bro any progress", True), ("why is it slow?", True),
                                            ("make sure the pdf is professional and add my name", False), ("update the status page to show progress bars", False), ("use skills also", False)])
def test_asking_how_it_is_going_is_a_question_and_never_an_order(text, question):
    assert bool(_QUESTION.match(text)) is question


class TestOneAnswerForCode:
    """A real update resent 13,000 tokens on each of nine steps and ran out before finishing. Now the project goes out once and all the work comes back in one answer."""

    def task(self, paths=()):
        from app.engineering.graph import Task

        return Task("build", "Add skills", "backend", allowed_paths=list(paths))

    def test_new_files_and_exact_edits_are_saved(self, tmp_path):
        from app.foundry.engine import FoundryEngine

        (tmp_path / "app.py").write_text("def score():\n    return 1\n")
        reply = "=== FILE: skills.py ===\nSKILLS = []\n=== END ===\n=== EDIT: app.py ===\n<<<<<<< FIND\n    return 1\n=======\n    return 2\n>>>>>>> REPLACE\n=== END ===\nSUMMARY: Added skills."
        out = __import__("json").loads(FoundryEngine.apply_batch(self.task(), tmp_path, reply))
        assert (tmp_path / "skills.py").read_text() == "SKILLS = []\n" and "return 2" in (tmp_path / "app.py").read_text()
        assert out["status"] == "done" and out["changed_files"] == ["skills.py", "app.py"] and out["summary"] == "Added skills."

    def test_an_edit_that_does_not_match_changes_nothing_and_says_so(self, tmp_path):
        from app.foundry.engine import FoundryEngine

        (tmp_path / "app.py").write_text("x = 1\nx = 1\n")
        reply = "=== FILE: a.py ===\nA = 1\n=== END ===\n=== EDIT: app.py ===\n<<<<<<< FIND\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n=== END ==="
        out = __import__("json").loads(FoundryEngine.apply_batch(self.task(), tmp_path, reply))
        assert (tmp_path / "app.py").read_text() == "x = 1\nx = 1\n" and "more than once" in out["needs"][0]

    @pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", ".git/config", "other/x.py"])
    def test_nothing_is_written_outside_the_project_or_the_tasks_files(self, tmp_path, path):
        from app.foundry.engine import FoundryEngine
        from app.hermes.client import HermesError

        (tmp_path / "proj").mkdir()
        with pytest.raises(HermesError):
            FoundryEngine.apply_batch(self.task(["app"]), tmp_path / "proj", f"=== FILE: {path} ===\nx\n=== END ===")
        assert not (tmp_path / "outside.py").exists() and not (tmp_path / "proj" / "other").exists()

    def test_an_answer_in_no_form_is_an_error_so_the_agent_gets_its_tools_instead(self, tmp_path):
        from app.foundry.engine import FoundryEngine
        from app.hermes.client import HermesError

        with pytest.raises(HermesError):
            FoundryEngine.apply_batch(self.task(), tmp_path, "I would start by reading the files.")


def test_a_fix_round_edits_an_existing_file_and_never_rewrites_it_whole(tmp_path):
    """A real fix round rewrote main.py to change a few lines: the page broke and 19 tests failed."""
    import json

    from app.engineering.graph import Task
    from app.foundry.engine import FoundryEngine

    (tmp_path / "main.py").write_text("def page():\n    return HTMLResponse(PAGE)\n")
    reply = "=== FILE: main.py ===\ndef page():\n    return PAGE\n=== END ===\n=== FILE: new_test.py ===\nX = 1\n=== END ==="
    out = json.loads(FoundryEngine.apply_batch(Task("fix-1-backend", "Fix the review findings", "backend"), tmp_path, reply))
    assert "HTMLResponse" in (tmp_path / "main.py").read_text() and (tmp_path / "new_test.py").is_file()
    assert any("never written again whole" in n for n in out["needs"])


class TestASymlinkedProjectFolder:
    """On macOS /var is a link to /private/var. A real task ended with "README.md is not in the subpath of" because files
    were found resolved and the project folder was not."""

    def test_the_files_are_given_through_a_linked_folder(self, tmp_path):
        from pathlib import Path

        from app.engineering.graph import Task
        from app.foundry.engine import FoundryEngine

        real = tmp_path / "real"
        real.mkdir()
        (real / "README.md").write_text("# hello\n")
        (real / "app.py").write_text("print(1)\n")
        linked = tmp_path / "linked"
        linked.symlink_to(real)
        task = Task("implement", "Build it", "backend")
        shown = FoundryEngine.working_files(task, Path(linked), numbered=False)
        assert '<file path="README.md">' in shown and '<file path="app.py">' in shown
