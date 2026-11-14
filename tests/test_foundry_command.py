"""The CEO talks to the team while it works, as you would talk to one person: heard at once, acknowledged, and obeyed. A real streaming thread and a real engine; only the models are scripted."""

import asyncio
import json
import threading
import time

import pytest

from app.foundry.messages import TEAM, XENIO, YOU
from tests.test_foundry_engine import Gate, Thinker, _reach_a_running_writer, build, finished, repo, status, until, working  # noqa: F401
from tests.test_foundry_runtime import Scripted, call, root, session  # noqa: F401


class SlowModel:
    """Streams slowly, as a real page does, and notes what happened to the reply it was writing."""

    def __init__(self, *turns):
        self.turns, self.seen, self.dropped, self.started = list(turns), [], 0, threading.Event()

    def stream_message(self, payload, timeout=0, keep_going=None):
        self.seen.append(payload["messages"][-1]["content"])
        turn = self.turns.pop(0)
        if turn == "SLOW":
            self.started.set()
            for _ in range(600):
                time.sleep(0.01)
                if keep_going is not None and not keep_going():
                    self.dropped += 1
                    return {"content": "half a page on the old plan", "tool_calls": [], "cut": False}
            return {"content": "a whole page on the old plan", "tool_calls": [], "cut": False}
        return {"content": turn, "tool_calls": [], "cut": False}


class TestHeardMidWork:
    def test_the_ceo_reaches_an_agent_inside_a_reply_and_the_reply_on_the_old_plan_is_dropped(self, tmp_path, root):
        model = SlowModel("SLOW", "Switching to blue. Done.")
        native, go = session(tmp_path, root, model)
        heard = []
        native.on_heard = lambda scope, line: heard.append((scope["role"], line))

        async def main():
            turn = asyncio.ensure_future(go("build the page"))
            while not native.running():
                await asyncio.sleep(0.01)
            while not model.started.is_set():
                await asyncio.sleep(0.01)
            said = time.monotonic()
            assert native.tell(native.running()[0], "make it blue", urgent=True)
            out = await turn
            return out, time.monotonic() - said

        out, seconds = asyncio.run(main())
        assert seconds < 1.0  # it did not wait six seconds for the old reply to finish
        assert "make it blue" in model.seen[-1] and "outranks the plan" in model.seen[-1]
        assert out.text == "Switching to blue. Done." and "old plan" not in out.text
        assert heard == [("backend", "Switching to blue.")]  # its own words, from what it was going to say anyway

    def test_a_teammate_does_not_cost_the_reply_being_written(self, tmp_path, root):
        client = Scripted([call("xenio_list_directory", path="src")], "All done.")
        native, go = session(tmp_path, root, client)

        async def main():
            turn = asyncio.ensure_future(go("do it"))
            while not native.running():
                await asyncio.sleep(0)
            native.tell(native.running()[0], "the schema changed", urgent=False)
            return await turn

        out = asyncio.run(main())
        assert out.text == "All done."
        said = [m["content"] for p in client.seen for m in p["messages"] if m["role"] == "user"]
        assert any("the schema changed" in m and "not an instruction" in m for m in said)

    def test_an_agent_cannot_finish_without_hearing_what_was_just_said(self, tmp_path, root):
        client = Scripted("Finished the old way.", "Changed it, finished.")
        native, go = session(tmp_path, root, client)

        def speak_as_it_finishes():
            native.tell(native.running()[0], "use the new name", urgent=False)
            return "Finished the old way."

        client.turns[0] = speak_as_it_finishes
        out = asyncio.run(go("do it"))
        assert out.text == "Changed it, finished."
        assert any("use the new name" in m["content"] for m in client.seen[-1]["messages"] if m["role"] == "user")

    def test_a_stop_does_not_wait_for_the_model_either(self, tmp_path, root):
        model = SlowModel("SLOW")
        native, go = session(tmp_path, root, model)

        async def main():
            turn = asyncio.ensure_future(go("build"))
            while not model.started.is_set():
                await asyncio.sleep(0.01)
            said = time.monotonic()
            await native.cancel(native.running()[0])
            out = await turn
            return out, time.monotonic() - said

        out, seconds = asyncio.run(main())
        assert out.cancelled and seconds < 1.0

    def test_someone_not_working_cannot_be_told_and_says_so(self, tmp_path, root):
        native, _ = session(tmp_path, root, Scripted())
        assert native.tell("nobody", "hello") is False


class Told:
    """A team whose agents hear things while they work, as native ones do."""

    def __init__(self, team):
        self.team, self.told = team, []

    def __getattr__(self, name):
        return getattr(self.team, name)

    def tell(self, session, text, urgent=False):
        self.told.append((session, text, urgent))
        return True


def told_team(tmp_path, thinker=None, script=working):
    engine, team, thinker = build(tmp_path, thinker, script=script)
    engine.sessions = Told(team)
    engine.orch_sessions = engine.sessions
    return engine, team, thinker


class TestTheManagerAnswersAtOnce:
    def test_an_order_is_acknowledged_and_delivered_before_any_model_is_asked(self, tmp_path, repo):
        gate = Gate()

        async def go():
            engine, _, thinker = told_team(tmp_path, Thinker(triage={"kind": "note"}), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            asked = len(thinker.asked)
            engine.message(wid, "Make the button blue")
            assert len(thinker.asked) == asked  # all of this happened with no model and no tokens
            acks = [m for m in engine.bus.history(wid, kind="ACK") if m.recipient == YOU]
            assert acks and acks[-1].sender == XENIO and "mid-task" in acks[-1].content
            told = engine.sessions.told
            assert told and "Make the button blue" in told[-1][1] and told[-1][2] is True
            await engine.stop(wid)
            gate.release.set()

        asyncio.run(go())

    def test_a_message_to_one_agent_reaches_that_agent_and_nobody_else(self, tmp_path, repo):
        gate = Gate()

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(triage={"kind": "note"}), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.message(wid, "Not you", to="frontend")
            first = list(engine.sessions.told)
            engine.message(wid, "You, please", to="backend")
            second = engine.sessions.told[len(first):]
            await engine.stop(wid)
            gate.release.set()
            return first, second

        first, second = asyncio.run(go())
        assert not first and second and "You, please" in second[0][1]

    def test_ask_the_backend_to_do_something_goes_to_the_backend(self, tmp_path, repo):
        gate = Gate()

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(triage={"kind": "note"}), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            record = engine.message(wid, "Ask the backend engineer to log every reset")
            await engine.stop(wid)
            gate.release.set()
            return record

        assert asyncio.run(go())["to"] == "backend"

    def test_a_whole_team_message_with_no_name_stays_for_the_whole_team(self, tmp_path, repo):
        gate = Gate()

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(triage={"kind": "note"}), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            record = engine.message(wid, "Make it faster")
            await engine.stop(wid)
            gate.release.set()
            return record

        assert asyncio.run(go())["to"] == TEAM

    def test_an_agent_that_has_read_it_says_so_in_its_own_name(self, tmp_path, repo):
        gate = Gate()

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(triage={"kind": "note"}), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.agent_heard({"workflow": wid, "role": "backend", "task": "api"}, "Moving it to blue.")
            engine.agent_heard({"workflow": wid, "role": "frontend", "task": "ui"}, "")
            acks = [m for m in engine.bus.history(wid, kind="ACK") if m.recipient == YOU]
            await engine.stop(wid)
            gate.release.set()
            return acks

        acks = asyncio.run(go())
        assert (acks[-2].sender, acks[-2].content) == ("backend", "Moving it to blue.") and acks[-1].sender == "frontend" and "Got it" in acks[-1].content

    def test_every_task_is_handed_over_and_taken_up_out_loud(self, tmp_path, repo):
        async def go():
            engine, _, _ = build(tmp_path)
            wf = await engine.start("Add password reset", str(repo), auto_run=True)
            await until(finished(engine, wf["id"]))
            return engine.bus.history(wf["id"])

        messages = asyncio.run(go())
        assert any(m.type == "DELEGATION" and m.sender == XENIO and m.recipient == "backend" and "yours" in m.content for m in messages)
        assert any(m.type == "ACK" and m.sender == "backend" and m.recipient == XENIO for m in messages)

    def test_a_question_gets_a_real_answer_from_the_manager_and_is_not_an_order(self, tmp_path, repo):
        gate = Gate()

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(triage={"kind": "question"}), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.message(wid, "How is it going?")
            await until(lambda: [m for m in engine.bus.history(wid, kind="RESULT") if m.recipient == YOU])
            answer = [m for m in engine.bus.history(wid, kind="RESULT") if m.recipient == YOU][0]
            told, orders = list(engine.sessions.told), engine.get(wid).get("orders", [])
            await engine.stop(wid)
            gate.release.set()
            return answer, told, orders

        answer, told, orders = asyncio.run(go())
        assert answer.sender == XENIO and answer.content == "An honest answer." and not told and not orders

    @pytest.mark.parametrize("words, end", [("Stop all agents", "stopped"), ("stop everything", "stopped")])
    def test_stop_all_agents_stops_them(self, tmp_path, repo, words, end):
        gate = Gate()

        async def go():
            engine, _, _ = told_team(tmp_path, script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.message(wid, words)
            await until(lambda: engine.get(wid)["status"] == end)
            gate.release.set()

        asyncio.run(go())

    def test_continue_means_carry_on_when_it_is_paused(self, tmp_path, repo):
        gate = Gate()

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(triage={"kind": "note"}), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.pause(wid)
            engine.message(wid, "Continue with the remaining tasks")
            assert not engine.get(wid)["paused"]
            await engine.stop(wid)
            gate.release.set()

        asyncio.run(go())


class TestOrdersAreObeyed:
    def test_a_change_of_mind_goes_straight_to_the_plan_with_no_debate(self, tmp_path, repo):
        gate = Gate()
        triage = {"kind": "requirement", "requirement": "Use SendGrid, not SMTP", "reopens_decision": True}
        adapt = {"keep": ["api"], "change": {"api": "use SendGrid"}, "why": "the CEO said so"}

        async def go():
            engine, _, thinker = told_team(tmp_path, Thinker(topics=(), triage=triage, adapt=adapt), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            debates = len(engine.decisions.list(wid))
            engine.message(wid, "Switch to SendGrid instead of SMTP")
            await until(lambda: any(n.startswith("adapt-order") and s == "done" for n, s in status(engine, wid).items()))
            assert len(engine.decisions.list(wid)) == debates  # nobody voted on it
            gate.release.set()
            await until(finished(engine, wid))
            return engine, wid

        engine, wid = asyncio.run(go())
        assert "Use SendGrid, not SMTP" in engine.get(wid)["constraints"]

    def test_asking_for_their_view_still_gets_their_view(self, tmp_path, repo):
        gate = Gate()
        triage = {"kind": "requirement", "requirement": "Rethink storage", "reopens_decision": True, "question": "Should storage change?"}

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(topics=(), triage=triage), script=gate.script())
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.message(wid, "Reconsider the storage layer")
            await until(lambda: "decision-1" in status(engine, wid))
            gate.release.set()
            await until(finished(engine, wid))
            return engine.decisions.list(wid)

        assert asyncio.run(go())[-1]["question"] == "Should storage change?"

    def test_an_order_given_after_everyone_has_finished_becomes_new_work(self, tmp_path, repo):
        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(topics=(), triage={"kind": "requirement", "requirement": "Add an audit log"}, adapt={"add": [{"id": "change", "title": "Audit log", "role": "backend", "allowed_paths": ["README.md"], "description": "add it"}], "why": "ordered"}))
            wf = await engine.start("Add password reset", str(repo), auto_run=True)
            wid = wf["id"]
            live = engine.flows[wid]
            await until(lambda: live.run is not None and live.orch is not None and all(t.status == "succeeded" for t in live.orch.graphs[live.run.id].tasks.values()))
            engine._redirect(live, None, "Add an audit log")
            await until(finished(engine, wid))
            return status(engine, wid)

        assert asyncio.run(go()).get("task-change") == "done"

    def test_an_order_to_an_idle_agent_becomes_its_task(self, tmp_path, repo):
        async def go():
            seen = []

            def script(kind, prompt, cwd):
                seen.append(prompt)
                return working(kind, prompt, cwd)

            engine, _, thinker = told_team(tmp_path, Thinker(topics=(), triage={"kind": "requirement", "requirement": "Write the changelog"}, adapt={"add": [{"id": "log", "title": "Changelog", "role": "docs", "allowed_paths": ["CHANGELOG.md"], "description": "write it"}], "why": "ordered"}), script=script)
            wf = await engine.start("Add password reset", str(repo), auto_run=False)
            wid = wf["id"]
            await until(lambda: engine.flows[wid].graph.nodes.get("go-ahead") is not None and engine.flows[wid].graph.nodes["go-ahead"].status == "waiting")
            engine.message(wid, "Write the changelog", to="docs")
            await until(lambda: any(n.startswith("adapt-order") for n in status(engine, wid)))
            asked = [a for a in thinker.asked if "changed a decision while working" in a]
            await until(lambda: [a for a in thinker.asked if "changed a decision while working" in a])
            asked = [a for a in thinker.asked if "changed a decision while working" in a]
            await engine.stop(wid)
            return asked

        asked = asyncio.run(go())
        assert asked and "gave this directly to" in asked[0] and "role docs" in asked[0]

    def test_the_reviewer_is_asked_about_each_order_and_a_no_sends_someone_to_fix_it(self, tmp_path, repo):
        rounds, fixed = [], []
        gate = Gate()

        def script(kind, prompt, cwd):
            if kind == "review":
                if "The CEO ordered" not in prompt:
                    return json.dumps({"summary": "nothing to add", "findings": []})  # security is not asked about orders
                rounds.append(prompt)
                met = bool(fixed)
                return json.dumps({"summary": "checked", "findings": [], "acceptance": [{"criterion": "The CEO ordered: Add an audit log", "met": met, "why": "it is in the diff" if met else "there is no log anywhere"}]})
            if "The CEO asked for this and it is not done" in prompt:
                fixed.append(prompt)
            return gate.script()(kind, prompt, cwd)

        async def go():
            engine, _, _ = told_team(tmp_path, Thinker(topics=(), triage={"kind": "requirement", "requirement": "Add an audit log"}), script=script)
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.message(wid, "Add an audit log")
            await until(lambda: engine.get(wid).get("orders") and engine.get(wid)["orders"][0].get("binding"))
            gate.release.set()
            await until(finished(engine, wid))
            return engine, wid

        engine, wid = asyncio.run(go())
        assert "The CEO ordered: Add an audit log" in rounds[0]
        order = engine.get(wid)["orders"][0]
        assert order["state"] == "done" and order["evidence"] == "it is in the diff"
        assert fixed and any(m["type"] == "DELEGATION" and "not done" in m["content"] for m in engine.state(wid)["messages"])

    def test_a_reviewer_tidying_up_the_words_still_counts(self, tmp_path, repo):
        engine, _, _ = build(tmp_path)

        async def go():
            wf = await engine.start("Add password reset", str(repo))
            live = engine.flows[wf["id"]]
            engine._save(live, orders=[{"id": "o1", "text": "The website must be about tea instead of coffee and use an all green design.", "state": "with the team", "binding": True, "at": "", "evidence": ""}])
            engine.note_orders(live, [{"criterion": "the ceo ordered: website must be about tea instead of coffee, all-green design", "met": True, "why": "it says tea"}])
            state = engine.get(wf["id"])["orders"][0]
            await engine.stop(wf["id"])
            return state

        state = asyncio.run(go())
        assert state["state"] == "done" and state["evidence"] == "it says tea"

    def test_an_order_is_not_marked_done_by_an_answer_about_something_else(self, tmp_path, repo):
        engine, _, _ = build(tmp_path)

        async def go():
            wf = await engine.start("Add password reset", str(repo))
            live = engine.flows[wf["id"]]
            engine._save(live, orders=[{"id": "o1", "text": "Add an audit log for every reset", "state": "with the team", "binding": True, "at": "", "evidence": ""}])
            engine.note_orders(live, [{"criterion": "The CEO ordered: use blue buttons", "met": True, "why": "they are blue"}])
            state = engine.get(wf["id"])["orders"][0]
            await engine.stop(wf["id"])
            return state

        assert asyncio.run(go())["state"] == "with the team"

    def test_a_reviewer_who_skipped_an_order_is_asked_about_it_once_on_its_own(self, tmp_path, repo):
        class Answers(Thinker):
            def stream_text(self, payload, timeout=0, on_piece=None):
                text = payload["messages"][-1]["content"]
                if "has to say whether each order" in text:
                    self.asked.append(text)
                    return json.dumps({"acceptance": [{"criterion": "The CEO ordered: Add an audit log", "met": True, "why": "it is in the diff"}]})
                return super().stream_text(payload, timeout, on_piece)

        gate = Gate()

        def script(kind, prompt, cwd):
            if kind == "review":
                return json.dumps({"summary": "fine", "findings": []})  # says nothing about the order
            return gate.script()(kind, prompt, cwd)

        async def go():
            engine, _, thinker = told_team(tmp_path, Answers(topics=(), triage={"kind": "requirement", "requirement": "Add an audit log"}), script=script)
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.message(wid, "Add an audit log")
            await until(lambda: engine.get(wid).get("orders") and engine.get(wid)["orders"][0].get("binding"))
            gate.release.set()
            await until(finished(engine, wid))
            return engine.get(wid), thinker

        wf, thinker = asyncio.run(go())
        assert wf["orders"][0]["state"] == "done" and any("has to say whether each order" in a for a in thinker.asked)

    def test_it_is_not_verified_while_an_order_is_unconfirmed(self, tmp_path, repo):
        gate = Gate()

        def script(kind, prompt, cwd):
            if kind == "review":
                return json.dumps({"summary": "fine", "findings": []})  # says nothing about the order
            return gate.script()(kind, prompt, cwd)

        async def go():
            class Silent(Thinker):
                def stream_text(self, payload, timeout=0, on_piece=None):
                    if "has to say whether each order" in payload["messages"][-1]["content"]:
                        return json.dumps({"acceptance": []})  # still says nothing
                    return super().stream_text(payload, timeout, on_piece)

            engine, _, _ = told_team(tmp_path, Silent(topics=(), triage={"kind": "requirement", "requirement": "Add an audit log"}), script=script)
            wid = await _reach_a_running_writer(engine, repo, gate)
            engine.message(wid, "Add an audit log")
            await until(lambda: engine.get(wid).get("orders") and engine.get(wid)["orders"][0].get("binding"))
            gate.release.set()
            await until(finished(engine, wid))
            return engine.get(wid)

        wf = asyncio.run(go())
        assert not wf.get("verified") and "nobody independent has confirmed" in wf["outcome"] and "audit log" in wf["outcome"]

    def test_an_agent_cannot_pose_as_the_ceo_or_the_manager(self, tmp_path, repo):
        from app.foundry.messages import MessageError

        engine, _, _ = build(tmp_path)
        for who in (YOU, XENIO):
            with pytest.raises(MessageError):
                engine.bus.send("w", who, TEAM, "ACK", "approved")
