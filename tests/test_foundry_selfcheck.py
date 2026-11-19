"""A builder that answered in one go could not run anything: XENIO runs its tests at once, and failing output goes back
to it once, before any reviewer spends a turn on work whose own tests fail."""

import asyncio
from pathlib import Path

from tests.test_foundry_engine import FakeDeps, StubPreviews, build, covering, finished, result, until

BROKEN = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST = "from built.core import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"


def run(tmp_path, first_answer_works: bool):
    thinker = covering([{"complete": True, "missing": []}] * 4)
    thinker.topics = []
    thinker.plans = [{"summary": "x", "tasks": [{"id": "core", "title": "Core", "role": "backend", "allowed_paths": ["built"]}]}]
    builds = []

    def script(kind, prompt, cwd):
        if kind == "review":
            return '{"summary": "clean", "findings": []}'
        builds.append(prompt)
        if "answer ONCE with all of your work" in prompt:  # the first attempt: no tools, one answer
            body = FIXED if first_answer_works else BROKEN
            return f"=== FILE: built/__init__.py ===\n\n=== END ===\n=== FILE: built/core.py ===\n{body}=== END ===\n=== FILE: built/test_core.py ===\n{TEST}=== END ===\nSUMMARY: added add"
        (Path(cwd) / "built" / "core.py").write_text(FIXED)  # the second attempt, with tools, edits the file
        return result("fixed add")

    engine, _, _ = build(tmp_path, thinker, script=script, deps_factory=FakeDeps, previews=StubPreviews())

    async def go():
        wf = await engine.start("Build a calculator", autonomy="supervisor")
        await until(finished(engine, wf["id"]), 90)
        return wf["id"]

    return engine, asyncio.run(go()), builds


def test_failing_tests_after_a_first_answer_go_back_to_the_builder_once_with_the_output(tmp_path):
    engine, wid, builds = run(tmp_path, first_answer_works=False)
    checks = [e["data"] for e in engine.store.events(wid) if e["type"] == "agent.selfcheck"]
    assert checks and checks[0]["ok"] is False and checks[0]["failed"] >= 1
    first_retry = next((p for p in builds if "Your first answer is saved in the files" in p), "")
    assert "assert" in first_retry and "4" in first_retry  # the real pytest output, not a summary of it
    assert "answer ONCE with all of your work" not in first_retry  # the second try has tools
    assert any("Ran the tests on" in m.content and "straight away" in m.content for m in engine.bus.history(wid))
    assert (Path(engine.get(wid)["project"]) / "built" / "core.py").read_text() == FIXED


def test_a_first_answer_whose_tests_pass_is_left_alone(tmp_path):
    engine, wid, builds = run(tmp_path, first_answer_works=True)
    checks = [e["data"] for e in engine.store.events(wid) if e["type"] == "agent.selfcheck"]
    assert checks and checks[0]["ok"] is True and checks[0]["passed"] >= 1
    assert not any("Your first answer is saved in the files" in p for p in builds)
    assert not any("Ran the tests on" in m.content for m in engine.bus.history(wid))
