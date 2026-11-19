"""An agent on a long task keeps its place: before older steps are trimmed, what they did is written down."""

import json

from app.foundry.runtime import LEDGER, MAX_CONTEXT, NativeSessions, _size


def call(i, name, args):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def result(i, text):
    return {"role": "tool", "tool_call_id": f"c{i}", "content": text}


def long_task():
    task = "YOUR TASK api: build the habit API"
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": task}]
    for i in range(0, 40, 4):
        messages += [call(i, "write_file", {"path": f"app/part{i}.py", "content": "x = 1\n" * 900}), result(i, "Wrote app/part%d.py" % i)]
        messages += [call(i + 1, "terminal", {"command": "python -m pytest -q"}), result(i + 1, ("exit code 1\n" if i < 32 else "exit code 0\n") + "F" * 6000)]
        messages += [call(i + 2, "patch", {"path": "app/main.py", "old_string": "a", "new_string": "b"}), result(i + 2, "Patched app/main.py")]
    messages += [call(99, "terminal", {"command": "curl https://example.com"}), result(99, "Blocked by XENIO policy: no network for agents.")]
    return task, messages


def test_before_older_steps_go_what_they_did_is_kept():
    task, messages = long_task()
    assert _size(messages[2:]) > MAX_CONTEXT
    NativeSessions._trim(messages)
    ledger = messages[2]["content"]
    assert messages[1]["content"] == task  # the task is never touched
    assert ledger.startswith(LEDGER)
    assert "app/part0.py" in ledger and "app/part36.py" in ledger and ledger.count("app/main.py") == 1  # every file, each once
    assert "`python -m pytest -q` -> exit code 0" in ledger.splitlines()[-3] or "exit code 0" in ledger  # how the latest run ended
    assert "Blocked by XENIO policy" in ledger
    assert _size(messages[2:]) <= MAX_CONTEXT + 3000


def test_the_note_is_refreshed_not_stacked_and_short_tasks_get_none():
    _, messages = long_task()
    NativeSessions._trim(messages)
    messages += [call(200, "write_file", {"path": "app/late.py", "content": "y = 2\n" * 3000}), result(200, "Wrote app/late.py"), call(201, "terminal", {"command": "npm test"}), result(201, "exit code 0\n" + "." * 30000)]
    NativeSessions._trim(messages)
    notes = [m for m in messages if m.get("role") == "user" and str(m.get("content")).startswith(LEDGER)]
    assert len(notes) == 1 and "app/late.py" in notes[0]["content"] and "`npm test` -> exit code 0" in notes[0]["content"]
    short = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}, call(1, "write_file", {"path": "a.py", "content": "1"}), result(1, "Wrote a.py")]
    NativeSessions._trim(short)
    assert not any(str(m.get("content")).startswith(LEDGER) for m in short)  # nothing was trimmed, so no note
