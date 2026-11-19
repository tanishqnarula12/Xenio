"""An agent's own task list: the CEO watches it live, and it costs one short line per update."""

import asyncio

from app.foundry.engine import TODO_TOOL, TeamTools
from tests.test_foundry_engine import build

SCOPE = {"workflow": "w1", "role": "backend", "task": "api", "tools": ["read_file", "patch"]}


def tools(tmp_path):
    engine, _, _ = build(tmp_path)
    return engine, TeamTools(engine, None)


def names(schemas):
    return [s["function"]["name"] for s in schemas]


def test_anyone_working_with_tools_is_offered_it_and_a_reviewer_is_not(tmp_path):
    _, box = tools(tmp_path)
    assert TODO_TOOL in names(box.schemas(SCOPE)) and box.owns(TODO_TOOL)
    assert TODO_TOOL not in names(box.schemas({**SCOPE, "tools": []}))  # a reviewer only reads
    assert TODO_TOOL not in names(box.schemas({**SCOPE, "workflow": ""}))


def test_the_list_is_recorded_for_the_ceo_and_the_answer_is_one_short_line(tmp_path):
    engine, box = tools(tmp_path)
    items = [{"text": "Read the brief", "status": "done"}, {"text": "Add  the\\n route", "status": "in_progress"}, {"text": "Tests", "status": "pending"}]
    text, ok = asyncio.run(box.call(TODO_TOOL, {"items": items}, SCOPE))
    assert ok and text == "Saved: 1 done, 1 doing, 1 to do. Now: Add the\\n route" and len(text) < 120
    saved = [e for e in engine.store.events("w1") if e["type"] == "agent.todo"][-1]
    assert saved["agent"] == "backend" and saved["task"] == "api"
    assert [i["status"] for i in saved["data"]["items"]] == ["done", "doing", "pending"]


def test_a_bad_list_is_refused_with_how_to_fix_it(tmp_path):
    engine, box = tools(tmp_path)
    two = [{"text": "a", "status": "doing"}, {"text": "b", "status": "doing"}]
    assert asyncio.run(box.call(TODO_TOOL, {"items": two}, SCOPE)) == ("Only one item can be doing at a time; you marked 2. Finish one first.", False)
    assert asyncio.run(box.call(TODO_TOOL, {"items": "do things"}, SCOPE))[1] is False
    assert asyncio.run(box.call(TODO_TOOL, {"items": [{"text": "", "status": "done"}, {"text": "x", "status": "maybe"}]}, SCOPE))[1] is False
    assert not [e for e in engine.store.events("w1") if e["type"] == "agent.todo"]  # nothing recorded from a refused list


def test_a_long_list_is_kept_short(tmp_path):
    engine, box = tools(tmp_path)
    many = [{"text": f"step {i} " + "x" * 300, "status": "pending"} for i in range(50)]
    assert asyncio.run(box.call(TODO_TOOL, {"items": many}, SCOPE))[1]
    saved = [e for e in engine.store.events("w1") if e["type"] == "agent.todo"][-1]["data"]["items"]
    assert len(saved) == 20 and all(len(i["text"]) <= 140 for i in saved)
