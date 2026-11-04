"""The improvement loop: test, review, find the problems, send them to whoever wrote that code, fix, test again. It ends when the evidence is clean or a limit is reached, and then you decide. It never runs on forever, and it never takes an agent's word for it.

Tests are run by XENIO itself. Reviewers cannot write, so whoever wrote the code cannot be the one who passes it. Every iteration is kept.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.engineering.graph import Task
from app.foundry.store import FoundryStore

STAGES = ("PLAN", "IMPLEMENT", "TEST", "REVIEW", "IDENTIFY_ISSUES", "REVISE", "RE-TEST", "ACCEPT_OR_ESCALATE")
_BAD_TESTS = ("failed", "error", "timeout")
_REVIEWERS = ("security", "reviewer")


@dataclass
class LoopPolicy:
    max_iterations: int = 3
    max_seconds: float = 3600.0
    max_tool_calls: int = 400
    fix_medium: bool = True  # a medium finding is worked on while there is budget; only a high one blocks acceptance

    def public(self) -> dict:
        return {"max_iterations": self.max_iterations, "max_seconds": int(self.max_seconds), "max_tool_calls": self.max_tool_calls}


def identify_issues(run) -> list[dict]:
    """What is wrong, from evidence only: XENIO's test runs, its scanner, and reviewer findings that point at a real place. An agent's opinion that all is well counts for nothing."""
    issues: list[dict] = []
    base = {t["command"]: t for t in run.baseline_tests}
    for test in run.final_tests:
        was = base.get(test["command"], {}).get("status")
        if test["status"] in _BAD_TESTS and (was != test["status"] or test.get("failed", 0) > base.get(test["command"], {}).get("failed", 0)):
            names = ", ".join((test.get("failures") or [])[:5])
            issues.append({"kind": "test", "severity": "high", "summary": f"{test['command']} now {test['status']}" + (f": {names}" if names else ""), "detail": str(test.get("tail") or "")[-1500:], "source": "XENIO ran the tests", "file": ""})
    for finding in run.security_findings:
        if finding.get("severity") == "high":  # the scanner's mediums are prompts for a person to look ("a dependency changed"): sending agents after them burns money and fixes nothing
            words = str(finding.get("advice") or finding.get("issue") or finding.get("rule") or "a security problem")
            issues.append({"kind": "security", "severity": "high", "summary": f"{finding.get('rule') or 'security'}: {words}"[:300], "file": str(finding.get("file") or ""), "line": finding.get("line"), "detail": str(finding.get("evidence") or "")[:300], "source": "XENIO's security scan"})
    for name, review in run.reviews.items():
        for finding in (review.get("findings") or []) if isinstance(review, dict) else []:
            if finding.get("severity") in ("high", "medium") and finding.get("verified_location"):
                issues.append({"kind": "review", "severity": finding["severity"], "summary": str(finding.get("issue") or finding.get("summary") or "")[:300], "file": str(finding.get("file") or ""), "line": finding.get("line"), "detail": str(finding.get("fix") or "")[:400], "source": f"{name} review"})
    for item in run.test_tampering:
        issues.append({"kind": "tampering", "severity": "high", "summary": f"tests were weakened: {item}"[:300], "file": "", "detail": "", "source": "XENIO compared the diff"})
    if not any(isinstance(run.reviews.get(r), dict) and "findings" in run.reviews[r] for r in _REVIEWERS):
        issues.append({"kind": "unreviewed", "severity": "high", "summary": "nobody independent reviewed this", "file": "", "detail": "; ".join(str((run.reviews.get(r) or {}).get("error") or (run.reviews.get(r) or {}).get("skipped") or "") for r in _REVIEWERS)[:300], "source": "XENIO", "unassignable": True})
    return issues


def owner_of(issue: dict, tasks: list[Task], writes) -> str:
    """Who fixes this: whoever last changed that file. Otherwise whoever wrote the most recent code."""
    writers = [t for t in tasks if writes(t) and t.status == "succeeded"]
    path = str(issue.get("file") or "")
    if path:
        for task in reversed(writers):
            if any(str(c).split(" ", 1)[-1] == path for c in task.result.get("changed_files") or []):
                return task.role
    if issue.get("kind") in ("test", "tampering"):
        testers = [t for t in writers if "test" in t.role]
        if issue.get("kind") == "tampering" and testers:
            return testers[-1].role
    return writers[-1].role if writers else ""


class EngineeringLoop:
    """Runs over an engineering Run that has already implemented its tasks. ``hooks`` is the engine: pause points, graph nodes, messages, fixes."""

    def __init__(self, store: FoundryStore, workflow: str, loop_id: str, policy: LoopPolicy, hooks, clock=time.monotonic):
        self.store = store
        self.workflow = workflow
        self.loop_id = loop_id
        self.policy = policy
        self.hooks = hooks
        self.clock = clock

    def iterations(self) -> list[dict]:
        rows = self.store.query("SELECT body FROM loop_iterations WHERE workflow_id = ? AND loop_id = ? ORDER BY iteration", (self.workflow, self.loop_id))
        return [json.loads(r["body"]) for r in rows]

    def _record(self, number: int, body: dict) -> dict:
        kept = {"iteration": number, **body}
        self.store.execute("INSERT OR REPLACE INTO loop_iterations (workflow_id, loop_id, iteration, body, at) VALUES (?,?,?,?,?)", (self.workflow, self.loop_id, number, json.dumps(kept, ensure_ascii=False, default=str), self.store.clock()))
        self.store.event("loop.iteration", self.workflow, loop=self.loop_id, iteration=number, stage=body.get("stage", ""), issues=len(body.get("issues") or []), outcome=body.get("outcome", ""))
        return kept

    async def run(self) -> dict:
        started = self.clock()
        done = self.iterations()
        number = len(done)  # after a restart: what finished is not done again
        tools_at_start = self.hooks.tool_calls()
        last: list[dict] = done[-1].get("issues", []) if done else []
        while True:
            why = self._limit(number, started, tools_at_start)
            if why:
                return self._finish(number, False, last, why)
            number += 1
            await self.hooks.checkpoint()
            issues = await self.hooks.verify(number)  # TEST, REVIEW, IDENTIFY_ISSUES
            blocking = [i for i in issues if i["severity"] == "high"]
            workable = [i for i in issues if not i.get("unassignable") and (i["severity"] == "high" or (self.policy.fix_medium and number == 1))]  # the smaller findings get one pass: chasing them round after round is where tokens go
            if not blocking and not workable:
                self._record(number, {"stage": "ACCEPT_OR_ESCALATE", "issues": issues, "outcome": "accepted"})
                return self._finish(number, True, issues, "tests and independent review found nothing that blocks this")
            if not workable:
                self._record(number, {"stage": "ACCEPT_OR_ESCALATE", "issues": issues, "outcome": "escalated"})
                return self._finish(number, False, issues, "what is wrong cannot be fixed by the team: " + blocking[0]["summary"])
            why = self._limit(number, started, tools_at_start)
            if why:
                self._record(number, {"stage": "ACCEPT_OR_ESCALATE", "issues": issues, "outcome": "out of budget"})
                if not blocking:
                    return self._finish(number, True, issues, f"nothing blocks this; some smaller findings remain ({why})")
                return self._finish(number, False, issues, why)
            await self.hooks.checkpoint()
            fixes = await self.hooks.revise(number, workable)  # REVISE, assigned to the responsible agents
            self._record(number, {"stage": "REVISE", "issues": issues, "fixes": fixes, "outcome": "revised"})
            last = issues

    def _limit(self, number: int, started: float, tools_at_start: int) -> str:
        if number >= self.policy.max_iterations:
            return f"it reached the limit of {self.policy.max_iterations} rounds"
        if self.clock() - started > self.policy.max_seconds:
            return f"it reached the limit of {int(self.policy.max_seconds)} s"
        if self.hooks.tool_calls() - tools_at_start > self.policy.max_tool_calls:
            return f"it reached the limit of {self.policy.max_tool_calls} tool calls"
        return ""

    def _finish(self, number: int, accepted: bool, issues: list[dict], why: str) -> dict:
        out = {"accepted": accepted, "iterations": number, "issues": issues, "why": why}
        self.store.event("loop.finished", self.workflow, loop=self.loop_id, accepted=accepted, iterations=number, why=why)
        return out
