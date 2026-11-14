"""The CEO's own token limits: for a whole job, and for one agent's task."""

import asyncio
import json

import pytest

from app.foundry.engine import FoundryError
from tests.test_foundry_engine import Thinker, build, until

ANALYSIS = {"kind": "software", "size": "medium", "summary": "an app", "needs": [{"name": "Backend engineer", "work": "build", "why": "the app"}]}


def started(tmp_path, **limits):
    seen = {}

    def script(kind, prompt, cwd):
        return json.dumps({"summary": "x", "status": "done"})

    async def go():
        engine, team, _ = build(tmp_path, Thinker(analysis=ANALYSIS), script=script)
        wf = await engine.start("build a small notes app", autonomy="supervisor", **limits)
        wid = wf["id"]
        await until(lambda: engine.flows[wid].team is not None and engine.flows[wid].budget.max_tokens != 0, 20)
        await until(lambda: engine.get(wid)["status"] in ("done", "failed", "stopped") or any(n.startswith("task-") for n in engine.flows[wid].graph.nodes), 20)
        seen["max"] = engine.flows[wid].budget.max_tokens
        seen["scopes"] = [dict(s) for s in engine.session_registry.all().values()] if hasattr(engine.session_registry, "all") else []
        seen["wf"] = engine.get(wid)
        return engine, wid

    engine, wid = asyncio.run(go())
    return engine, wid, seen


def test_your_default_limits_are_saved_and_read_back(tmp_path):
    engine, _, _ = build(tmp_path)
    assert engine.limits() == {"job_tokens": 0, "task_tokens": 0}  # until you set them, XENIO decides
    engine.set_limits("150k".replace("k", "000"), 40_000)
    assert engine.limits() == {"job_tokens": 150_000, "task_tokens": 40_000}


@pytest.mark.parametrize("bad", ["lots", -5, 100, 10**12])
def test_a_limit_that_is_not_a_sensible_number_is_refused(tmp_path, bad):
    engine, _, _ = build(tmp_path)
    with pytest.raises(FoundryError):
        engine.set_limits(bad, 0)


def test_a_job_started_with_your_limit_keeps_to_it(tmp_path):
    engine, wid, seen = started(tmp_path, job_tokens=90_000, task_tokens=25_000)
    assert seen["max"] == 90_000 and seen["wf"]["budget_tokens"] == 90_000 and seen["wf"]["task_tokens"] == 25_000


def test_without_a_limit_xenio_picks_by_size(tmp_path):
    from app.foundry.engine import TOKENS_BY_SIZE

    _, _, seen = started(tmp_path)
    assert seen["max"] == TOKENS_BY_SIZE["medium"]


def test_your_saved_defaults_apply_to_a_new_job(tmp_path):
    engine, _, _ = build(tmp_path)
    engine.set_limits(70_000, 0)

    async def go():
        engine2, _, _ = build(tmp_path, Thinker(analysis=ANALYSIS))
        wf = await engine2.start("build a small notes app", autonomy="supervisor")
        await until(lambda: engine2.flows[wf["id"]].team is not None, 20)
        await until(lambda: engine2.flows[wf["id"]].budget.max_tokens == 70_000, 5)
        await engine2.stop(wf["id"])
        return True

    assert asyncio.run(go())


def test_each_agent_task_gets_your_task_limit(tmp_path):
    limits = []

    async def go():
        engine, _, _ = build(tmp_path, Thinker(analysis=ANALYSIS))
        real = engine.session_registry.add

        def watched(session, scope):  # what every agent's session was really given
            limits.append(scope.get("max_turn_tokens"))
            return real(session, scope)

        engine.session_registry.add = watched
        wf = await engine.start("build a small notes app", autonomy="supervisor", task_tokens=12_000)
        await until(lambda: engine.get(wf["id"])["status"] in ("done", "failed", "stopped"), 30)

    asyncio.run(go())
    assert limits and all(x == 12_000 for x in limits), limits


def test_you_can_change_a_running_jobs_limits_and_it_is_recorded(tmp_path):
    async def go():
        engine, _, _ = build(tmp_path, Thinker(analysis=ANALYSIS))
        wf = await engine.start("build a small notes app", autonomy="supervisor")
        wid = wf["id"]
        await until(lambda: engine.flows[wid].team is not None, 20)
        out = engine.set_budget(wid, job_tokens=60_000, task_tokens=15_000)
        live = engine.flows[wid]
        result = (live.budget.max_tokens, out.get("budget_tokens"), out.get("task_tokens"), [i for i in engine.state(wid)["interventions"] if i["kind"] == "budget"])
        if engine.get(wid)["status"] not in ("done", "failed", "stopped"):
            await engine.stop(wid)
        return result

    max_tokens, job, task, recorded = asyncio.run(go())
    assert max_tokens == 60_000 and job == 60_000 and task == 15_000 and recorded and "60,000" in recorded[0]["effect"][0]


def test_only_you_can_set_limits_never_an_agent(tmp_path):
    from app.foundry.service import FoundryService

    engine, _, _ = build(tmp_path)
    service = FoundryService.__new__(FoundryService)
    service.engine, service.skills, service.registry = engine, None, engine.registry
    for command in ("set_limits", "set_budget"):
        with pytest.raises(FoundryError, match="only you"):
            asyncio.run(service.dispatch(command, {"job_tokens": 10_000_000, "workflow": "x"}, confirmed=False))
    assert engine.limits() == {"job_tokens": 0, "task_tokens": 0}
    assert asyncio.run(service.dispatch("set_limits", {"job_tokens": 80_000, "task_tokens": 20_000}, confirmed=True)) == {"job_tokens": 80_000, "task_tokens": 20_000}
