"""The workflow graph and the improvement loop on their own: dependencies, branches, failures, retries, cycles, persistence, recovery, limits."""

import asyncio
import json

import pytest

from app.foundry.graph import MAX_LOOP_ITERATIONS, GraphError, Node, WorkflowGraph
from app.foundry.loop import EngineeringLoop, LoopPolicy, identify_issues, owner_of
from app.foundry.store import FoundryStore
from app.engineering.graph import Task


@pytest.fixture
def store(tmp_path):
    return FoundryStore(tmp_path / "f.db")


@pytest.fixture
def g(store):
    return WorkflowGraph(store, "w")


def ids(nodes):
    return [n.id for n in nodes]


def chain(g, *names):
    last = []
    for name in names:
        g.add(Node(name, "task", name, deps=list(last)))
        last = [name]


class TestShape:
    def test_all_the_node_kinds_exist(self, g):
        for kind in ("goal", "planning", "agent", "task", "communication", "review", "testing", "decision", "approval", "completion"):
            g.add(Node(kind, kind, kind))
        with pytest.raises(GraphError):
            g.add(Node("x", "magic", "x"))

    def test_a_dependency_must_exist(self, g):
        with pytest.raises(GraphError):
            g.add(Node("a", "task", "a", deps=["nope"]))

    def test_the_same_id_twice_is_refused(self, g):
        g.add(Node("a", "task", "a"))
        with pytest.raises(GraphError):
            g.add(Node("a", "task", "again"))

    def test_a_cycle_is_refused_and_leaves_the_graph_as_it_was(self, g):
        chain(g, "a", "b", "c")
        with pytest.raises(GraphError) as err:
            g.link("c", "a")
        assert "cycle" in str(err.value) and g.nodes["a"].deps == [] and ids(g.ready()) == ["a"]

    def test_a_node_cannot_wait_for_itself(self, g):
        g.add(Node("a", "task", "a"))
        with pytest.raises(GraphError):
            g.link("a", "a")

    @pytest.mark.parametrize("budget", [{}, {"max_iterations": 0}, {"max_iterations": MAX_LOOP_ITERATIONS + 1}, {"max_iterations": None}])
    def test_a_loop_without_a_limit_cannot_exist(self, g, budget):
        with pytest.raises(GraphError):
            g.add(Node("l", "loop", "l", budget=budget))

    def test_a_loop_with_one_can(self, g):
        assert g.add(Node("l", "loop", "l", budget={"max_iterations": 3})).id == "l"


class TestRunning:
    def test_dependencies_set_the_order(self, g):
        chain(g, "a", "b")
        assert ids(g.ready()) == ["a"]
        g.set("a", "done")
        assert ids(g.ready()) == ["b"]

    def test_several_can_run_side_by_side(self, g):
        g.add(Node("root", "goal", "r"))
        g.add(Node("a", "task", "a", deps=["root"]))
        g.add(Node("b", "task", "b", deps=["root"]))
        g.set("root", "done")
        assert set(ids(g.ready())) == {"a", "b"}

    def test_more_urgent_first(self, g):
        g.add(Node("a", "task", "a"))
        g.add(Node("b", "task", "b", priority=5))
        assert ids(g.ready()) == ["b", "a"]

    def test_a_failure_blocks_what_depends_on_it_with_the_reason(self, g):
        chain(g, "a", "b", "c")
        g.set("a", "failed", error="boom")
        g.ready()
        assert g.nodes["b"].status == "blocked" and g.nodes["c"].status == "blocked" and "a did not finish" in g.nodes["b"].error and g.settled()

    def test_a_report_still_runs_after_a_failure(self, g):
        g.add(Node("a", "task", "a"))
        g.add(Node("r", "completion", "r", deps=["a"], always=True))
        g.set("a", "failed", error="boom")
        assert ids(g.ready()) == ["r"]

    def test_a_retry_is_limited(self, g):
        g.add(Node("a", "task", "a", max_retries=1))
        assert g.fail("a", "x").status == "pending" and g.fail("a", "x").status == "failed"

    def test_branches(self, g):
        g.add(Node("check", "testing", "c"))
        g.add(Node("fix", "task", "f", deps=["check"], when={"node": "check", "output": "clean", "equals": False}))
        g.add(Node("ship", "task", "s", deps=["check"], when={"node": "check", "output": "clean", "equals": True}))
        g.set("check", "done", outputs={"clean": True})
        assert ids(g.ready()) == ["ship"] and g.nodes["fix"].status == "skipped"

    def test_a_branch_on_failure(self, g):
        g.add(Node("a", "task", "a"))
        g.add(Node("help", "task", "h", deps=["a"], when={"node": "a", "status": "failed"}, always=True))
        g.set("a", "failed", error="x")
        assert ids(g.ready()) == ["help"]

    def test_a_skipped_step_does_not_block(self, g):
        chain(g, "a", "b")
        g.set("a", "skipped")
        assert ids(g.ready()) == ["b"]

    def test_cancel_leaves_finished_work_alone(self, g):
        chain(g, "a", "b")
        g.set("a", "done")
        assert g.cancel_open("stopped") == ["b"] and g.nodes["a"].status == "done" and g.nodes["b"].error == "stopped"

    def test_reopen_keeps_the_old_answer_in_history(self, g):
        g.add(Node("d", "decision", "d"))
        g.set("d", "done", outputs={"text": "mongo"})
        g.reopen("d", "you asked for postgres")
        node = g.nodes["d"]
        assert node.status == "pending" and node.outputs == {} and any(h.get("outputs") == {"text": "mongo"} for h in node.history)

    def test_every_change_is_an_event_in_order(self, store, g):
        g.add(Node("a", "task", "a", agent="backend"))
        g.set("a", "running")
        g.set("a", "done")
        got = [e for e in store.events("w") if e["type"] == "node"]
        assert [e["data"]["status"] for e in got] == ["pending", "running", "done"] and got[0]["agent"] == "backend" and got[0]["task"] == "a"


class TestPersistence:
    def test_it_comes_back_exactly(self, store, g):
        chain(g, "a", "b")
        g.set("a", "done", outputs={"x": 1})
        back = WorkflowGraph.load(store, "w")
        assert back.nodes["a"].outputs == {"x": 1} and back.nodes["b"].deps == ["a"] and back.nodes["a"].status == "done" and ids(back.ready()) == ["b"]

    def test_recovery_keeps_what_finished_and_sorts_the_rest(self, store, g):
        g.add(Node("done", "task", "d"))
        g.add(Node("reader", "review", "r"))
        g.add(Node("writer", "task", "w", writes=True))
        g.add(Node("later", "task", "l", deps=["writer"]))
        g.set("done", "done", outputs={"x": 1})
        g.set("reader", "running")
        g.set("writer", "running")
        back = WorkflowGraph.load(store, "w")
        out = back.recover()
        assert out == {"kept": ["done"], "repeat": ["reader"], "check": ["writer"]}
        assert back.nodes["done"].status == "done" and back.nodes["reader"].status == "pending" and back.nodes["later"].status == "pending"

    def test_a_workflow_sees_only_its_own_nodes(self, store, g):
        g.add(Node("a", "task", "a"))
        WorkflowGraph(store, "other").add(Node("b", "task", "b"))
        assert ids(WorkflowGraph.load(store, "w").nodes.values()) == ["a"]


class FakeRun:
    def __init__(self, **kw):
        self.baseline_tests, self.final_tests, self.security_findings, self.reviews, self.test_tampering = [], [], [], {"security": {"findings": []}, "reviewer": {"findings": []}}, []
        self.__dict__.update(kw)


class TestFindingProblems:
    def test_a_scanner_finding_says_what_it_is_so_it_can_be_fixed(self):
        found = identify_issues(FakeRun(security_findings=[{"rule": "sql-string", "severity": "high", "file": "a.py", "line": 3, "evidence": "execute(f'...')", "advice": "use parameters"}]))
        assert found[0]["summary"] == "sql-string: use parameters" and found[0]["detail"] == "execute(f'...')" and found[0]["file"] == "a.py"

    def test_a_scanner_medium_is_a_note_not_a_job(self):
        assert identify_issues(FakeRun(security_findings=[{"rule": "dependency-change", "severity": "medium", "file": "requirements.txt", "advice": "check it"}])) == []

    def test_a_clean_run_has_none(self):
        assert identify_issues(FakeRun(final_tests=[{"command": "pytest", "status": "passed"}])) == []

    def test_a_new_test_failure_is_high_and_says_which(self):
        found = identify_issues(FakeRun(baseline_tests=[{"command": "pytest", "status": "passed"}], final_tests=[{"command": "pytest", "status": "failed", "failed": 1, "failures": ["test_x"], "tail": "boom"}]))
        assert found[0]["severity"] == "high" and "test_x" in found[0]["summary"] and found[0]["detail"] == "boom"

    def test_a_failure_that_was_already_there_is_not_blamed_on_the_team(self):
        assert identify_issues(FakeRun(baseline_tests=[{"command": "pytest", "status": "failed", "failed": 2}], final_tests=[{"command": "pytest", "status": "failed", "failed": 2}])) == []

    def test_but_more_of_them_is(self):
        assert identify_issues(FakeRun(baseline_tests=[{"command": "pytest", "status": "failed", "failed": 2}], final_tests=[{"command": "pytest", "status": "failed", "failed": 3}]))

    def test_a_finding_that_points_at_nothing_does_not_count(self):
        reviews = {"security": {"findings": []}, "reviewer": {"findings": [{"severity": "high", "issue": "invented", "file": "nope.py", "verified_location": False}, {"severity": "high", "issue": "real", "file": "a.py", "line": 1, "verified_location": True}]}}
        found = identify_issues(FakeRun(reviews=reviews))
        assert [f["summary"] for f in found] == ["real"]

    def test_weakened_tests_are_high(self):
        assert identify_issues(FakeRun(test_tampering=["t: removed asserts"]))[0]["severity"] == "high"

    def test_no_independent_review_cannot_pass_and_nobody_can_fix_it(self):
        found = identify_issues(FakeRun(reviews={"reviewer": {"error": "provider down"}, "security": {"skipped": "disabled"}}))
        assert found[0]["kind"] == "unreviewed" and found[0]["severity"] == "high" and found[0]["unassignable"]

    def test_the_writer_of_a_file_fixes_it(self):
        tasks = [Task("a", "a", "backend", status="succeeded", result={"changed_files": ["M src/a.py"]}), Task("b", "b", "frontend", status="succeeded", result={"changed_files": ["M ui/b.js"]})]
        assert owner_of({"file": "src/a.py"}, tasks, lambda t: True) == "backend"
        assert owner_of({"file": ""}, tasks, lambda t: True) == "frontend"
        assert owner_of({"file": "src/a.py"}, tasks, lambda t: False) == ""


class Hooks:
    def __init__(self, rounds, store=None):
        self.rounds, self.verified, self.revised, self.tools, self.paused = list(rounds), [], [], 0, 0

    def tool_calls(self):
        return self.tools

    async def checkpoint(self):
        self.paused += 1

    async def verify(self, number):
        self.verified.append(number)
        return self.rounds.pop(0) if self.rounds else []

    async def revise(self, number, issues):
        self.revised.append((number, issues))
        self.tools += 10
        return [{"agent": "backend", "ok": True}]


HIGH = {"kind": "test", "severity": "high", "summary": "boom"}
MEDIUM = {"kind": "review", "severity": "medium", "summary": "meh"}


def loop(store, rounds, **policy):
    hooks = Hooks(rounds)
    lp = EngineeringLoop(store, "w", "loop", LoopPolicy(**policy), hooks)
    return lp, hooks, asyncio.run(lp.run())


class TestTheLoop:
    def test_clean_the_first_time_means_no_revision(self, store):
        _, hooks, out = loop(store, [[]])
        assert out["accepted"] and out["iterations"] == 1 and hooks.revised == []

    def test_problems_go_round_until_they_are_gone(self, store):
        lp, hooks, out = loop(store, [[HIGH], [HIGH], []])
        assert out["accepted"] and out["iterations"] == 3 and [n for n, _ in hooks.revised] == [1, 2]
        assert [i["stage"] for i in lp.iterations()] == ["REVISE", "REVISE", "ACCEPT_OR_ESCALATE"]

    def test_smaller_findings_get_one_pass_and_then_do_not_keep_it_going(self, store):
        _, hooks, out = loop(store, [[MEDIUM], [MEDIUM], [MEDIUM]])
        assert out["accepted"] and out["iterations"] == 2 and [n for n, _ in hooks.revised] == [1]

    def test_it_can_never_go_on_forever(self, store):
        _, hooks, out = loop(store, [[HIGH]] * 50, max_iterations=3)
        assert not out["accepted"] and out["iterations"] == 3 and len(hooks.verified) == 3 and "limit of 3 rounds" in out["why"] and out["issues"] == [HIGH]

    def test_a_small_finding_at_the_limit_does_not_block(self, store):
        _, _, out = loop(store, [[MEDIUM]] * 5, max_iterations=2)
        assert out["accepted"]

    def test_a_time_limit(self, store):
        ticks = iter([0, 100, 100, 100, 100, 100, 100, 100])
        hooks = Hooks([[HIGH]] * 9)
        out = asyncio.run(EngineeringLoop(store, "w", "l", LoopPolicy(max_seconds=50, max_iterations=9), hooks, clock=lambda: next(ticks)).run())
        assert not out["accepted"] and "50 s" in out["why"]

    def test_a_tool_limit(self, store):
        _, _, out = loop(store, [[HIGH]] * 9, max_tool_calls=5, max_iterations=9)
        assert not out["accepted"] and "5 tool calls" in out["why"]

    def test_what_nobody_can_fix_goes_to_you_at_once(self, store):
        cannot = {"kind": "unreviewed", "severity": "high", "summary": "nobody reviewed this", "unassignable": True}
        _, hooks, out = loop(store, [[cannot]])
        assert not out["accepted"] and hooks.revised == [] and "nobody reviewed" in out["why"]

    def test_every_iteration_is_kept_and_a_restart_does_not_repeat_them(self, store):
        lp, _, _ = loop(store, [[HIGH], [HIGH]], max_iterations=2)
        kept = lp.iterations()
        again = Hooks([[]])
        out = asyncio.run(EngineeringLoop(store, "w", "loop", LoopPolicy(max_iterations=5), again).run())
        assert len(kept) >= 1 and again.verified == [len(kept) + 1] and out["accepted"]

    def test_it_waits_at_your_pause_points(self, store):
        _, hooks, _ = loop(store, [[HIGH], []])
        assert hooks.paused >= 3

    def test_the_stages_are_all_named(self):
        from app.foundry.loop import STAGES

        assert STAGES == ("PLAN", "IMPLEMENT", "TEST", "REVIEW", "IDENTIFY_ISSUES", "REVISE", "RE-TEST", "ACCEPT_OR_ESCALATE")

    def test_stored_iterations_survive(self, store):
        lp, _, _ = loop(store, [[HIGH], []])
        rows = store.query("SELECT body FROM loop_iterations WHERE workflow_id = 'w'")
        assert json.loads(rows[0]["body"])["issues"] == [HIGH] and len(lp.iterations()) == len(rows)
