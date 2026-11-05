"""Which model thinks for which agent. Keys stay in XENIO's environment: an agent definition only ever names a provider and a model.

Thinking (plans, proposals, reviews of text) goes straight to Nabh: fast and streaming. Work with tools (editing files, running tests) runs in a Hermes session, where xenio-guard enforces the agent's role; its model is set per agent, and the session reports what really ran.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.integrations import nabh as nabh_module
from app.integrations.nabh import NabhError

FALLBACK_NABH = ("glm-5-2", "deepseek-v4-pro", "kimi-k2-7-code")
# measured on the same design question (2026-11-04): these gave a usable answer in 5-9 s; kimi-k3, qwen3 and minimax-m3 came back empty
# cheap first: on a nearly empty account deepseek-v4-1-flash, gpt-oss and gemma were still accepted while GLM and Kimi were refused, which says what they cost
DEBATE_SEATS = ("deepseek-v4-1-flash", "gpt-oss-120b", "deepseek-v4-pro", "kimi-k2-7-code")
# who may do what, best first. A tool loop needs a model that really calls tools (glm, kimi, qwen, measured); a review with no tools can go to the cheapest head that reasons well.
POOLS = {"build": ("glm-5-2", "kimi-k2-7-code", "qwen3-5-397b"), "ui": ("glm-5-2", "kimi-k2-7-code", "qwen3-5-397b"), "test": ("kimi-k2-7-code", "qwen3-5-397b", "glm-5-2"), "docs": ("qwen3-5-397b", "kimi-k2-7-code"),
         "review": ("deepseek-v4-pro", "deepseek-v4-1-flash", "gpt-oss-120b"), "security": ("deepseek-v4-1-flash", "deepseek-v4-pro", "gpt-oss-120b"), "plan": ("glm-5-2", "kimi-k2-7-code")}


def work_of(agent) -> str:
    """What kind of work this is, which decides which models are fit for it."""
    caps = set(agent.capabilities)
    words = f"{agent.name} {agent.role}".lower()
    if "docs_write" in caps and "scoped_file_write" not in caps:
        return "docs"
    if "test_execution" in caps:
        return "test"
    if "security_checks" in caps:
        return "security"
    if "scoped_file_write" not in caps:
        return "review" if "diff_review" in caps else "plan"
    return "ui" if re.search(r"front|\bui\b|design|css|web", words) else "build"


class ModelPool:
    """Spreads the work. Each job goes to the least busy model fit for it, then the one that has spent least here, so no single model carries a whole build and one model's limit does not stall the team."""

    def __init__(self, pools: dict[str, tuple[str, ...]] | None = None):
        self.pools = pools or POOLS
        self.active: dict[str, int] = {}
        self.spent: dict[str, int] = {}

    def pick(self, kind: str) -> str:
        pool = self.pools.get(kind) or self.pools["build"]
        return min(enumerate(pool), key=lambda item: (self.active.get(item[1], 0), self.spent.get(item[1], 0) // 150_000, item[0]))[1]

    def enter(self, model: str) -> None:
        self.active[model] = self.active.get(model, 0) + 1

    def leave(self, model: str, tokens: int = 0, actual: str = "") -> None:
        self.active[model] = max(0, self.active.get(model, 0) - 1)
        if tokens:
            who = actual or model
            self.spent[who] = self.spent.get(who, 0) + tokens
JUDGE_MODEL = "glm-5-2"
NABH = ("", "nabh")


class ModelError(Exception):
    def __init__(self, message: str, kind: str = "provider"):
        super().__init__(message)
        self.kind = kind


class BudgetExceeded(ModelError):
    def __init__(self, message: str):
        super().__init__(message, "budget")


@dataclass
class Budget:
    """Hard limits for one workflow. Reaching one stops the work and asks you: it is never quietly extended."""

    max_calls: int = 200
    max_seconds: float = 3600.0
    max_tokens: int = 2_000_000
    calls: int = 0
    tokens: int = 0
    started: float = field(default_factory=time.monotonic)

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"the budget of {self.max_calls} model calls is used up")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded(f"the budget of about {self.max_tokens} tokens is used up")
        if time.monotonic() - self.started > self.max_seconds:
            raise BudgetExceeded(f"the time budget of {int(self.max_seconds)} s is used up")

    def spend(self, chars: int, measured: int = 0) -> None:
        self.calls += 1
        self.tokens += measured or max(1, chars // 4)  # Nabh's count when it gives one

    def public(self) -> dict:
        return {"calls": self.calls, "max_calls": self.max_calls, "tokens_estimated": self.tokens, "max_tokens": self.max_tokens, "seconds": int(time.monotonic() - self.started), "max_seconds": int(self.max_seconds)}


class OutOfCredit(BudgetExceeded):
    """The account paying for the models has run out. Nothing about the work is wrong: it waits, keeps everything, and carries on when you press Resume."""

    def __init__(self, message: str = "the model account is out of credit"):
        super().__init__(message)
        self.kind = "quota"


class AcpThinker:
    """Thinking on any model Hermes serves, over ACP: one session, on the agent's model, that can only read, whatever the agent's own role allows. The guard enforces that on every tool call."""

    def __init__(self, sessions, session_registry, cost=None, scratch: Path | None = None):
        from app.engineering.roles import CostPolicy

        self.sessions = sessions
        self.registry = session_registry
        self.cost = cost or CostPolicy()
        self.scratch = scratch

    async def think(self, agent, messages: list[dict], cwd: Path | None, budget: Budget | None, workflow: str, task: str, timeout: float) -> tuple[str, str]:
        from app.engineering.roles import Role
        from app.hermes.client import HermesError

        problem = self.cost.check(agent.to_role())
        if problem:  # never quietly swapped for something else, and never paid for without being told to
            raise ModelError(problem, "cost")
        if budget is not None:
            budget.check()
        reader = Role(agent.id, agent.name, ("repository_read",), "")
        folder = cwd or self.scratch or Path.cwd()
        prompt = "\n\n".join(str(m.get("content") or "") for m in messages)
        session = ""
        try:
            session = await self.sessions.open(folder, agent.model, agent.provider)
            self.registry.add(session, {"run": workflow, "task": task or "think", "role": agent.id, "tools": sorted(reader.tools), "terminal": reader.terminal_mode, "root": str(folder), "allowed_paths": [], "docs_only": False})
            turn = await self.sessions.prompt(session, prompt, timeout=timeout)
        except HermesError as exc:
            raise ModelError(f"{agent.model}: {' '.join(str(exc).split())[:200]}", "provider") from None
        finally:
            if session:
                self.registry.remove(session)
        if turn.cancelled:
            raise ModelError("stopped", "cancelled")
        if budget is not None:
            budget.calls += 1
            budget.tokens += int((turn.usage or {}).get("totalTokens") or (len(prompt) + len(turn.text)) // 4)
        return turn.text, turn.model or agent.model


class ModelRouter:
    def __init__(self, nabh_factory=None, fallback: tuple[str, ...] = FALLBACK_NABH, acp: AcpThinker | None = None, seats: tuple[str, ...] = DEBATE_SEATS, judge: str = JUDGE_MODEL):
        self._nabh = nabh_factory or nabh_module.client
        self.fallback = fallback
        self.acp = acp
        self.seats = seats
        self.judge = judge

    def available(self) -> bool:
        try:
            return self._nabh() is not None
        except NabhError:
            return False

    def _client(self):
        try:
            client = self._nabh()
        except NabhError as exc:
            raise ModelError(str(exc), exc.kind) from None
        if client is None:
            raise ModelError("thinking needs a Nabh key (NABH_API_KEY)", "config")
        return client

    @staticmethod
    def uses_acp(agent) -> bool:
        return str(getattr(agent, "provider", "") or "") not in NABH

    def seat(self, agents: list) -> dict[str, str]:
        """Different heads for one question: disagreement should come from different reasoning. A model you pinned on an agent is kept."""
        taken = {str(a.model) for a in agents if a.model}
        free = [m for m in self.seats if m not in taken] or list(self.seats)
        seated, at = {}, 0
        for agent in agents:
            if agent.model:
                seated[agent.id] = agent.model
            else:
                seated[agent.id] = free[at % len(free)]
                at += 1
        return seated

    def chain(self, agent=None, prefer: str = "") -> list[str]:
        """The chosen model first, then the fallbacks. A model pinned to another provider runs over ACP, not here."""
        pinned = str(getattr(agent, "model", "") or "") if not self.uses_acp(agent) else ""
        first = prefer or pinned
        return [*([first] if first else []), *[m for m in self.fallback if m != first]]

    def think(self, messages: list[dict], agent=None, budget: Budget | None = None, max_tokens: int = 3000, timeout: float = 90.0, prefer: str = "") -> tuple[str, str]:
        """(the answer, the model that gave it)."""
        client = self._client()
        last = None
        for model in self.chain(agent, prefer):
            if budget is not None:
                budget.check()
            try:
                answer = client.stream_text({"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.3, "stream_options": {"include_usage": True}}, timeout)
                text = str(answer)
            except NabhError as exc:
                last = exc
                if exc.kind == "quota":
                    raise OutOfCredit(str(exc)) from None
                if exc.kind in ("auth", "config"):
                    break
                continue
            if budget is not None:
                budget.spend(sum(len(str(m.get("content") or "")) for m in messages) + len(text), int((getattr(answer, "usage", None) or {}).get("total_tokens") or 0))
            return text, model
        raise ModelError(f"no model could answer: {last}", getattr(last, "kind", "provider"))

    def think_json(self, messages: list[dict], agent=None, budget: Budget | None = None, max_tokens: int = 3000, timeout: float = 90.0, prefer: str = "") -> tuple[dict, str]:
        text, model = self.think(messages, agent, budget, max_tokens, timeout, prefer)
        return self._read_json(text, model, lambda again: self.think(again, agent, budget, max_tokens, timeout, model), messages)

    @staticmethod
    def _read_json(text: str, model: str, retry, messages: list[dict]) -> tuple[dict, str]:
        found = nabh_module.first_json_object(text)
        if found is None:  # one more try: a model's prose is no plan
            text, model = retry([*messages, {"role": "assistant", "content": text[:600]}, {"role": "user", "content": "That was not one valid JSON object. Send only the JSON object."}])
            found = nabh_module.first_json_object(text)
        if found is None:
            raise ModelError("the model's answer could not be read", "bad_response")
        return found, model

    async def ask(self, messages: list[dict], agent=None, budget: Budget | None = None, max_tokens: int = 3000, timeout: float = 120.0, prefer: str = "", cwd: Path | None = None, workflow: str = "", task: str = "") -> tuple[str, str]:
        """One agent thinking, on the route its model needs. Nabh for speed; ACP for anything Hermes serves."""
        if agent is not None and self.uses_acp(agent):
            if self.acp is None:
                raise ModelError(f"{agent.model} needs a Hermes session and none is available here", "config")
            return await self.acp.think(agent, messages, cwd, budget, workflow, task, timeout)
        return await asyncio.to_thread(self.think, messages, agent, budget, max_tokens, timeout, prefer)

    async def ask_json(self, messages: list[dict], agent=None, budget: Budget | None = None, max_tokens: int = 3000, timeout: float = 120.0, prefer: str = "", cwd: Path | None = None, workflow: str = "", task: str = "") -> tuple[dict, str]:
        text, model = await self.ask(messages, agent, budget, max_tokens, timeout, prefer, cwd, workflow, task)
        found = nabh_module.first_json_object(text)
        if found is not None:
            return found, model
        again = [*messages, {"role": "assistant", "content": text[:600]}, {"role": "user", "content": "That was not one valid JSON object. Send only the JSON object."}]
        text, model = await self.ask(again, agent, budget, max_tokens, timeout, model, cwd, workflow, task)
        found = nabh_module.first_json_object(text)
        if found is None:
            raise ModelError("the model's answer could not be read", "bad_response")
        return found, model
