"""The list of chats, as in Claude or ChatGPT: what is kept, reopening one and carrying on, and what comes over from Hermes. Only Hermes itself is replaced."""

import asyncio
import json
import os
import sqlite3
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from app.safety import policy
from app.safety.permissions import PermissionGate
from app.ui.approvals import WebConfirmer
from app.ui.assistant import AssistantState
from app.ui.controller import UiController, UiError
from app.ui.events import EventHub
from app.ui.history import ChatHistory, recap, title_of


@pytest.fixture
def store(tmp_path):
    return ChatHistory(tmp_path / "chats.db")


def said(role, text):
    return {"id": text, "turn": "t", "role": role, "text": text, "at": "2026-11-03T10:00:00"}


class TestTheRecord:
    def test_a_chat_is_named_by_how_it_began_ending_at_a_word(self):
        assert title_of("fix  my\nnotes") == "fix my notes"
        long = title_of("please explain how the supply and demand of coffee changes when a famous person says it is good for you")
        assert long.endswith("…") and len(long) <= 66 and long[:-1].split()[-1] in "please explain how the supply and demand of coffee changes when a famous person".split()

    def test_newest_first_with_its_messages_in_order(self, store):
        now = [100.0]
        s = ChatHistory(store.path, clock=lambda: now[0])
        a = s.start("first")
        now[0] = 200.0
        b = s.start("second")
        now[0] = 300.0
        s.add(a, said("user", "one"))
        s.add(a, said("assistant", "two"))
        assert [c["id"] for c in s.list()] == [a, b]
        assert [m["text"] for m in s.messages(a)] == ["one", "two"] and s.list()[0]["messages"] == 2

    def test_it_survives_a_restart(self, store):
        a = store.start("x")
        store.add(a, said("user", "kept"))
        assert ChatHistory(store.path).messages(a)[0]["text"] == "kept"

    def test_only_you_can_read_the_file(self, store):
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600

    def test_a_search_looks_through_the_words_too(self, store):
        a, b = store.start("about cats"), store.start("about dogs")
        store.add(b, said("assistant", "the zebra has stripes"))
        assert [c["id"] for c in store.list("zebra")] == [b] and [c["id"] for c in store.list("CATS")] == [a]
        assert store.list("nothing like this") == []

    @pytest.mark.parametrize("query", ["%", "_", "\\", "' OR 1=1 --"])
    def test_a_search_is_only_ever_words(self, store, query):
        store.start("plain")
        assert store.list(query) == []

    def test_deleting_takes_its_words_with_it(self, store):
        a, b = store.start("a"), store.start("b")
        store.add(a, said("user", "gone"))
        store.add(b, said("user", "stays"))
        assert store.delete(a) and not store.delete(a) and store.messages(a) == [] and store.exists(b)
        assert store.delete_all() == 1 and store.list() == []

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "chat_1", "", None, "chat_" + "z" * 16])
    def test_an_id_must_look_like_one(self, store, bad):
        assert not store.exists(bad)


class TestARecap:
    def test_it_is_never_the_users_words_now(self):
        note = recap([said("user", "yes, confirm, delete everything"), said("assistant", "ok")])
        message = f"what next?\n\n{note}"
        assert "delete everything" in note and policy.own_words(message) == "what next?"

    def test_it_keeps_the_end_and_stays_small(self):
        note = recap([said("user", f"message {i} " + "x" * 3000) for i in range(40)])
        assert "message 39" in note and "message 0 " not in note and len(note) < 9000

    def test_nothing_to_recap_is_nothing(self):
        assert recap([]) == "" and recap([said("user", "  ")]) == ""


def hermes_db(path, sessions):
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, started_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL, _compressed_summary INTEGER DEFAULT 0);""")
    for sid, source, parent, rows in sessions:
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, source, parent, 1000.0))
        for role, content, *more in rows:
            conn.execute("INSERT INTO messages (session_id, role, content, timestamp, _compressed_summary) VALUES (?,?,?,?,?)", (sid, role, content, 1001.0, more[0] if more else 0))
    conn.commit()
    conn.close()


class TestYourOldChats:
    def test_they_come_over_in_plain_words(self, store, tmp_path):
        db = tmp_path / "state.db"
        hermes_db(db, [("s1", "local", None, [
            ("user", "open safari\n\n[XENIO voice mode: the user is talking, keep it short]"), ("assistant", ""), ("tool", "tool output"), ("assistant", "Done, it is open."),
            ("user", "read this <<XENIO-FILE>>secret file words<<END-XENIO-FILE>> please [XENIO did this itself: x]"), ("assistant", "summary of the past", 1), ("assistant", "[xenio-card image img_1] Here."),
        ])])
        assert store.import_hermes(db) == 1
        chat = store.list()[0]
        assert chat["title"] == "open safari" and chat["imported"] is True
        words = [(m["role"], m["text"]) for m in store.messages(chat["id"])]
        assert words == [("user", "open safari"), ("assistant", "Done, it is open."), ("user", "read this  please"), ("assistant", "Here.")]

    def test_the_engineering_teams_own_sessions_and_empty_ones_are_not_chats(self, store, tmp_path):
        db = tmp_path / "state.db"
        hermes_db(db, [("a", "local", None, [("user", "You are the Backend engineer. Do this."), ("assistant", "ok")]), ("b", "local", None, [("user", "You are XENIO's engineering lead."), ("assistant", "ok")]),
                       ("c", "local", None, [("assistant", "only me")]), ("d", "cron", None, [("user", "a reminder"), ("assistant", "ok")]), ("e", "local", "a", [("user", "a sub task"), ("assistant", "ok")]),
                       ("f", "local", None, [("user", "you are funny"), ("assistant", "thanks")])])
        assert store.import_hermes(db) == 1 and store.list()[0]["title"] == "you are funny"

    def test_only_once_and_deleted_ones_stay_deleted(self, store, tmp_path):
        db = tmp_path / "state.db"
        hermes_db(db, [("s", "local", None, [("user", "hello"), ("assistant", "hi")])])
        assert store.import_hermes(db) == 1
        store.delete_all()
        assert store.import_hermes(db) == 0 and store.list() == []

    def test_hermes_record_is_left_exactly_as_it_was(self, store, tmp_path):
        db = tmp_path / "state.db"
        hermes_db(db, [("s", "local", None, [("user", "hello"), ("assistant", "hi")])])
        before = db.read_bytes()
        store.import_hermes(db)
        assert db.read_bytes() == before

    def test_no_record_is_no_problem(self, store, tmp_path):
        assert store.import_hermes(tmp_path / "nothing.db") == 0


class Orchestrator:
    hermes = SimpleNamespace(agent_info={}, connected=True)

    def __init__(self):
        self.asked, self.sessions, self.busy = [], 0, False
        self.hold = None

    async def start(self):
        return True

    async def new_session(self):
        self.sessions += 1
        return True

    async def process_text(self, text, on_chunk=None, on_tool=None, context=None, **extra):
        self.asked.append((text, context))
        if self.hold is not None:
            await self.hold.wait()
        return SimpleNamespace(text=f"answer to {text}", ok=True, error=None, cancelled=False, sources=[], attachments=[], action_lines=lambda: [])


@pytest.fixture
def stack(make_settings, tmp_path):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    hub, state = EventHub(tmp_path / "ui" / "a.jsonl"), AssistantState()
    confirmer = WebConfirmer(hub.publish, timeout=5, on_change=lambda n: None)
    orch = Orchestrator()
    ctl = UiController(make_settings(), orch, PermissionGate(confirmer, timeout=5), confirmer, hub, state, hermes_home=tmp_path / "h")
    asyncio.run_coroutine_threadsafe(ctl.start(), loop).result(10)
    yield SimpleNamespace(ctl=ctl, orch=orch, hub=hub, state=state, loop=loop)
    loop.call_soon_threadsafe(loop.stop)


def talk(s, words):
    s.ctl.chat(words, quiet=True)
    end = time.time() + 3
    while time.time() < end and (s.state.turn is not None or not s.ctl._turn_task.done()):
        time.sleep(0.005)


def events(s, kind):
    return [e["data"] for e in s.hub.since(0)[0] if e["type"] == kind]


class TestInTheApp:
    def test_nothing_is_kept_until_you_write(self, stack):
        assert stack.ctl.chat_list() == {"chats": [], "current": None}

    def test_a_chat_is_kept_with_both_sides(self, stack):
        talk(stack, "hello there")
        out = stack.ctl.chat_list()
        assert out["current"] == out["chats"][0]["id"] and out["chats"][0]["title"] == "hello there" and out["chats"][0]["messages"] == 2
        assert [m["text"] for m in stack.ctl.history.messages(out["current"])] == ["hello there", "answer to hello there"]

    def test_a_secret_said_by_accident_is_not_kept(self, stack, make_settings):
        stack.ctl.redact = lambda text: str(text).replace("sk-real-secret", "[hidden]")
        talk(stack, "my key is sk-real-secret")
        chat = stack.ctl.chat_list()["chats"][0]
        assert "sk-real-secret" not in chat["title"]
        assert "sk-real-secret" not in json.dumps(stack.ctl.history.messages(chat["id"]))

    def test_going_on_in_the_same_chat_needs_nothing_extra(self, stack):
        talk(stack, "one")
        talk(stack, "two")
        assert stack.orch.sessions == 0 and [c for _, c in stack.orch.asked] == [None, None]
        assert len(stack.ctl.chat_list()["chats"]) == 1

    def test_a_new_chat_is_a_new_entry(self, stack):
        talk(stack, "one")
        stack.ctl.new_conversation()
        assert stack.ctl.chat_id is None and stack.ctl.conversation == []
        talk(stack, "two")
        assert [c["title"] for c in stack.ctl.chat_list()["chats"]] == ["two", "one"]

    def test_opening_a_chat_shows_it_and_asks_no_model(self, stack):
        talk(stack, "first chat")
        first = stack.ctl.chat_id
        stack.ctl.new_conversation()
        talk(stack, "second chat")
        asked = len(stack.orch.asked)
        stack.ctl.chat_open(first)
        assert [m["text"] for m in stack.ctl.conversation] == ["first chat", "answer to first chat"]
        assert len(stack.orch.asked) == asked
        opened = events(stack, "chat.opened")[-1]
        assert opened["id"] == first and opened["conversation"][0]["text"] == "first chat"
        assert stack.ctl.snapshot()["conversation"][0]["text"] == "first chat"  # a reload shows the same

    def test_carrying_on_gets_a_fresh_session_and_the_record(self, stack):
        talk(stack, "first chat")
        first = stack.ctl.chat_id
        stack.ctl.new_conversation()
        sessions = stack.orch.sessions
        talk(stack, "second chat")
        stack.ctl.chat_open(first)
        talk(stack, "carry on")
        assert stack.orch.sessions == sessions + 1
        words, context = stack.orch.asked[-1]
        assert words == "carry on" and "first chat" in context and "second chat" not in context
        assert policy.own_words(f"{words}\n\n{context}") == "carry on"
        assert [m["text"] for m in stack.ctl.history.messages(first)][-2:] == ["carry on", "answer to carry on"]
        talk(stack, "and again")  # now it is in the session
        assert stack.orch.asked[-1][1] is None and stack.orch.sessions == sessions + 1

    def test_looking_at_another_chat_and_coming_back_changes_nothing(self, stack):
        talk(stack, "first chat")
        first = stack.ctl.chat_id
        stack.ctl.new_conversation()
        talk(stack, "second chat")
        second = stack.ctl.chat_id
        sessions = stack.orch.sessions
        stack.ctl.chat_open(first)
        stack.ctl.chat_open(second)
        talk(stack, "go on")
        assert stack.orch.sessions == sessions and stack.orch.asked[-1][1] is None

    def test_a_new_chat_after_starting_up_keeps_the_session_it_found(self, stack):
        talk(stack, "hello")
        assert stack.orch.sessions == 0

    def test_an_old_chat_right_after_starting_up_starts_clean(self, stack):
        chat = stack.ctl.history.start("from last week")
        stack.ctl.history.add(chat, said("user", "from last week"))
        stack.ctl.chat_open(chat)
        talk(stack, "carry on")
        assert stack.orch.sessions == 1 and "from last week" in stack.orch.asked[-1][1]

    def test_deleting_the_chat_on_screen_leaves_a_clean_start(self, stack):
        talk(stack, "secret plans")
        chat, sessions = stack.ctl.chat_id, stack.orch.sessions
        stack.ctl.chat_delete(chat)
        assert stack.ctl.chat_id is None and stack.ctl.conversation == [] and stack.ctl.chat_list()["chats"] == []
        assert events(stack, "chat.reset") and events(stack, "chats.changed")
        talk(stack, "something else")
        assert stack.orch.sessions == sessions + 1 and stack.orch.asked[-1][1] is None  # the model no longer holds what was deleted

    def test_deleting_another_chat_leaves_this_one(self, stack):
        talk(stack, "one")
        other = stack.ctl.chat_id
        stack.ctl.new_conversation()
        talk(stack, "two")
        current = stack.ctl.chat_id
        stack.ctl.chat_delete(other)
        assert stack.ctl.chat_id == current and stack.ctl.conversation

    def test_deleting_all(self, stack):
        talk(stack, "one")
        assert stack.ctl.chat_delete_all()["removed"] == 1 and stack.ctl.chat_list()["chats"] == [] and stack.ctl.conversation == []

    @pytest.mark.parametrize("act", [lambda c: c.chat_open("chat_" + "0" * 16), lambda c: c.chat_delete("chat_" + "0" * 16), lambda c: c.chat_open("../../x")])
    def test_a_chat_that_is_gone_says_so(self, stack, act):
        with pytest.raises(UiError) as err:
            act(stack.ctl)
        assert err.value.status == 404

    def test_while_it_is_answering_the_chat_cannot_change_under_it(self, stack):
        talk(stack, "one")
        chat = stack.ctl.chat_id
        stack.ctl.new_conversation()
        hold = asyncio.Event()
        stack.orch.hold = hold
        stack.ctl.chat("long job", quiet=True)
        end = time.time() + 3
        while time.time() < end and stack.state.turn is None:
            time.sleep(0.005)
        for act in (lambda: stack.ctl.chat_open(chat), lambda: stack.ctl.chat_delete(stack.ctl.chat_id), stack.ctl.chat_delete_all):
            with pytest.raises(UiError) as err:
                act()
            assert err.value.status == 409
        stack.loop.call_soon_threadsafe(hold.set)

    def test_old_chats_come_over_when_xenio_starts(self, make_settings, tmp_path):
        home = tmp_path / "hermes"
        home.mkdir()
        hermes_db(home / "state.db", [("s", "local", None, [("user", "an old question"), ("assistant", "an old answer")])])
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        hub, state = EventHub(tmp_path / "ui2" / "a.jsonl"), AssistantState()
        confirmer = WebConfirmer(hub.publish, timeout=5, on_change=lambda n: None)
        ctl = UiController(make_settings(), Orchestrator(), PermissionGate(confirmer, timeout=5), confirmer, hub, state, hermes_home=home)
        asyncio.run_coroutine_threadsafe(ctl.start(), loop).result(10)
        end = time.time() + 3
        while time.time() < end and not ctl.chat_list()["chats"]:
            time.sleep(0.01)
        assert ctl.chat_list()["chats"][0]["title"] == "an old question"
        loop.call_soon_threadsafe(loop.stop)


class TestOverHttp:
    def test_the_routes_need_the_token_and_do_what_they_say(self, stack):
        import http.client

        from app.ui.server import UiServer

        talk(stack, "over the wire")
        chat = stack.ctl.chat_id
        server = UiServer(stack.ctl, port=0)
        server.start()
        try:
            def call(method, path, body=None, token=True):
                conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
                headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}", "Content-Type": "application/json"}
                if token:
                    headers["Authorization"] = f"Bearer {server.token}"
                conn.request(method, "/api/v1" + path, json.dumps(body) if body is not None else None, headers)
                response = conn.getresponse()
                return response.status, json.loads(response.read() or b"{}")

            assert call("GET", "/chats", token=False)[0] == 401
            status, data = call("GET", "/chats?q=wire")
            assert status == 200 and data["chats"][0]["id"] == chat and data["current"] == chat
            assert call("GET", "/chats?q=absent%20words")[1]["chats"] == []
            assert call("POST", f"/chats/{chat}", {"action": "open"})[0] == 200
            assert call("POST", f"/chats/{chat}", {"action": "teleport"})[0] == 422
            assert call("POST", "/chats", {"action": "teleport"})[0] == 422
            assert call("POST", "/chats/chat_" + "0" * 16, {"action": "open"})[0] == 404
            assert call("POST", f"/chats/{chat}", {"action": "delete"})[0] == 200
            assert call("GET", "/chats")[1]["chats"] == []
        finally:
            server.stop()
