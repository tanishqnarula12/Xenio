"""Permission engine (XENIO side): classify requests, ask the user, remember answers briefly.

Classification itself lives in ``app.safety.policy`` (shared with the xenio-guard plugin that
runs inside Hermes). This module turns Hermes' permission prompts into ``PermissionRequest``s,
applies the policy:

    READ / SAFE_WRITE  -> allowed without asking
    CONFIRM_REQUIRED   -> the user must say yes
    HIGH_RISK          -> the user must say "confirm"
    blocked            -> refused without asking

and fails closed on every error path (no confirmer, timeout, exception, unclear answer).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.hermes.client import HermesPermissionRequest
from app.logging_setup import event
from app.safety.policy import (
    PathPolicy,
    RiskLevel,
    classify_command,
    combine,
    parse_escalation,
    parse_escalation_scopes,
    scope_covers,
)

log = logging.getLogger("xenio.safety")

__all__ = [
    "RiskLevel", "ACTION_RISK", "classify_action", "PermissionRequest", "PermissionDecision", "PermissionGate",
    "Confirmer", "from_hermes_request", "interpret_answer", "is_affirmative", "is_strict_confirmation",
]

# Named actions (spec examples + integrations). Tool calls are classified on their real arguments
# by app.safety.policy; this table is for XENIO's own named actions.
ACTION_RISK: dict[str, RiskLevel] = {
    "read_file": RiskLevel.READ,
    "list_directory": RiskLevel.READ,
    "git_status": RiskLevel.READ,
    "web_search": RiskLevel.READ,
    "search_web": RiskLevel.READ,
    "read_calendar": RiskLevel.READ,
    "create_project_file": RiskLevel.SAFE_WRITE,
    "create_file": RiskLevel.SAFE_WRITE,
    "create_directory": RiskLevel.SAFE_WRITE,
    "create_note": RiskLevel.SAFE_WRITE,
    "create_task": RiskLevel.SAFE_WRITE,
    "git_push": RiskLevel.CONFIRM_REQUIRED,
    "send_email": RiskLevel.CONFIRM_REQUIRED,
    "delete_file": RiskLevel.CONFIRM_REQUIRED,
    "install_package": RiskLevel.CONFIRM_REQUIRED,
    "move_file": RiskLevel.CONFIRM_REQUIRED,
    "destructive_shell_command": RiskLevel.HIGH_RISK,
    "credential_change": RiskLevel.HIGH_RISK,
    "production_database_operation": RiskLevel.HIGH_RISK,
    "financial_action": RiskLevel.HIGH_RISK,
}


def classify_action(action: str) -> RiskLevel:
    """Unknown actions fail closed to CONFIRM_REQUIRED."""
    return ACTION_RISK.get(action, RiskLevel.CONFIRM_REQUIRED)


@dataclass
class PermissionRequest:
    action: str
    description: str  # completes "This will ..."
    risk: RiskLevel
    source: str = "xenio"  # "hermes" | "xenio"
    details: dict[str, Any] = field(default_factory=dict)
    blocked: bool = False
    reason: str = ""
    dedupe_key: str = ""  # same action approved moments ago -> don't ask twice
    scopes: tuple[str, ...] = ()  # destructive targets ("delete:<path>"); see policy.scope_covers


@dataclass
class PermissionDecision:
    approved: bool
    reason: str


_PLUGIN_RULE = re.compile(r"^<([\w.-]+)> \(plugin approval rule\)$")


def from_hermes_request(req: HermesPermissionRequest, paths: PathPolicy | Path) -> PermissionRequest:
    """Classify a Hermes ACP permission prompt with the XENIO policy."""
    if not isinstance(paths, PathPolicy):
        paths = PathPolicy(workspace=paths)
    if req.kind == "edit":
        diffs = [c for c in req.content if isinstance(c, dict) and c.get("type") == "diff" and c.get("path")]
        if not diffs:
            return PermissionRequest("filesystem.modify", req.title or "edit a file", RiskLevel.CONFIRM_REQUIRED, "hermes")
        checks = [paths.check(str(d["path"]), "create" if d.get("oldText") is None else "modify") for d in diffs]
        decision = combine(checks, fallback=checks[0])
        return PermissionRequest(decision.action, decision.summary, decision.level, "hermes",
                                 {"paths": [str(d["path"]) for d in diffs]}, decision.blocked, decision.reason)

    if req.kind == "execute":
        command = str(req.raw_input.get("command") or "").strip()
        description = str(req.raw_input.get("description") or "").strip()
        plugin = _PLUGIN_RULE.match(command)
        if plugin:
            parsed = parse_escalation(description)
            if parsed:  # escalated by xenio-guard: already classified with full context inside Hermes
                level, summary, key = parsed
                return PermissionRequest("policy." + plugin.group(1), summary, level, "hermes",
                                         {"tool": plugin.group(1)}, dedupe_key=key,
                                         scopes=parse_escalation_scopes(description))
            return PermissionRequest("plugin." + plugin.group(1), description or f"use {plugin.group(1)}",
                                     RiskLevel.CONFIRM_REQUIRED, "hermes")
        # Hermes' own dangerous-command prompt: judge the real command; never below CONFIRM.
        decision = classify_command(command, paths, paths.workspace)
        level = max(decision.level, RiskLevel.CONFIRM_REQUIRED)
        summary = decision.summary if decision.summary.startswith(("run", "permanently", "push")) else f"run: {command}"
        return PermissionRequest(decision.action, summary, level, "hermes", {"command": command},
                                 decision.blocked, decision.reason, dedupe_key=f"terminal:{command}",
                                 scopes=decision.scopes)

    return PermissionRequest(req.kind or "unknown", req.title or "perform an unknown action",
                             RiskLevel.CONFIRM_REQUIRED, "hermes")


class _NoClearAnswer:
    """A confirmer's "no" that the user never said: silence or unclear answers. Falsy like any no, but
    reported as what happened instead of "the user declined"."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "NO_CLEAR_ANSWER"


NO_CLEAR_ANSWER = _NoClearAnswer()


class Confirmer(Protocol):
    async def confirm(self, request: PermissionRequest, *, strict: bool) -> bool | _NoClearAnswer: ...


class PermissionGate:
    REMEMBER_SECONDS = 120.0

    def __init__(
        self,
        confirmer: Confirmer | None = None,
        *,
        auto_approve_up_to: RiskLevel = RiskLevel.SAFE_WRITE,
        timeout: float = 120.0,
    ):
        self.confirmer = confirmer
        self._auto_max = auto_approve_up_to
        self._timeout = timeout
        self._recent: dict[str, float] = {}
        self._approved_scopes: list[tuple[str, float]] = []  # (scope, expiry)

    def _covered(self, request: PermissionRequest, now: float) -> bool:
        """True if the user just approved this exact action, or a delete/move of the same or an
        enclosing path ("delete the folder and everything in it" covers its files, and retries).
        HIGH_RISK requests are never covered by an earlier approval."""
        if request.dedupe_key and self._recent.get(request.dedupe_key, 0) > now:
            return True
        if not request.scopes or request.risk >= RiskLevel.HIGH_RISK:
            return False
        live = [s for s, expiry in self._approved_scopes if expiry > now]
        return all(any(scope_covers(a, r) for a in live) for r in request.scopes)

    async def check(self, request: PermissionRequest) -> PermissionDecision:
        fields = {"action": request.action, "risk": request.risk.name, "source": request.source}
        event(log, "PERMISSION_CHECK", **fields, blocked=request.blocked)
        if request.blocked:
            event(log, "Permission denied (blocked by policy)", logging.WARNING, **fields, reason=request.reason)
            return PermissionDecision(False, f"blocked by XENIO policy: {request.reason}")
        if request.risk <= self._auto_max:
            return PermissionDecision(True, f"{request.risk.name} actions are allowed automatically")
        now = time.monotonic()
        if self._covered(request, now):
            event(log, "Permission granted (already confirmed moments ago)", **fields)
            return PermissionDecision(True, "already confirmed by the user")
        if self.confirmer is None:
            event(log, "Permission denied (no way to ask the user)", logging.WARNING, **fields)
            return PermissionDecision(False, "no confirmation channel available")
        strict = request.risk >= RiskLevel.HIGH_RISK
        event(log, "CONFIRMATION_REQUESTED", **fields, strict=strict)
        try:
            answer = await asyncio.wait_for(self.confirmer.confirm(request, strict=strict), self._timeout)
            approved = answer is True  # only an explicit yes; anything else is a no
        except TimeoutError:
            event(log, "CONFIRMATION_RECEIVED", logging.WARNING, **fields, approved=False, why="timeout")
            return PermissionDecision(False, "confirmation timed out")
        except Exception:
            log.exception("Confirmer failed; denying")
            return PermissionDecision(False, "confirmation failed")
        event(log, "CONFIRMATION_RECEIVED", **fields, approved=approved)
        if approved:
            expiry = time.monotonic() + self.REMEMBER_SECONDS
            if request.dedupe_key:
                self._recent = {k: v for k, v in self._recent.items() if v > now}
                self._recent[request.dedupe_key] = expiry
            self._approved_scopes = [(s, e) for s, e in self._approved_scopes if e > now]
            self._approved_scopes.extend((s, expiry) for s in request.scopes)
        if approved:
            return PermissionDecision(True, "user approved")
        return PermissionDecision(False, "no clear answer, so it was cancelled" if answer is NO_CLEAR_ANSWER
                                  else "the user declined")


# ---- interpreting the user's answer (typed or transcribed) -----------------------

_YES = {"y", "yes", "yeah", "yep", "yup", "yah", "ya", "sure", "ok", "okay", "alright", "allow", "approve",
        "approved", "proceed", "continue", "affirmative", "absolutely", "definitely", "correct", "confirm",
        "confirmed", "certainly"}
_YES_PHRASES = ("go ahead", "do it", "please do", "of course", "that's fine", "thats fine", "sounds good", "yes please")
_NO = {"n", "no", "nope", "nah", "don't", "dont", "deny", "denied", "cancel", "stop", "never", "negative", "abort",
       "not", "reject", "wait", "hold"}
_FILLER = {"please", "xenio", "zenio", "hey", "it", "that", "this", "go", "ahead", "do", "just", "now", "the", "then",
           "thanks", "thank", "you", "i", "said", "mean", "can", "yes", "sure", "ok", "okay", "a", "and", "so",
           "well", "um", "uh", "right", "fine", "please", "for", "me", "with", "delete", "remove", "folder", "file",
           "run", "install", "push", "move", "create", "is", "its", "it's", "go", "on", "confirm", "confirmed"}


def interpret_answer(answer: str, strict: bool = False) -> str:
    """'yes', 'no' or 'unclear'. Negation wins; a long unrelated sentence is never a yes;
    HIGH_RISK (strict) needs the word "confirm"."""
    text = (answer or "").lower().replace("’", "'")
    words = re.findall(r"[a-z']+", text)
    if not words:
        return "unclear"
    if any(w in _NO for w in words) or re.search(r"\bdo not\b|\bhold on\b|\bno way\b", text):
        return "no"
    if strict:
        return "yes" if any(w in ("confirm", "confirmed") for w in words) else "unclear"
    has_yes = any(w in _YES for w in words) or any(p in text for p in _YES_PHRASES)
    if not has_yes:
        return "unclear"
    unrelated = [w for w in words if w not in _YES and w not in _FILLER]
    return "yes" if len(unrelated) <= 3 else "unclear"


def is_new_request(answer: str) -> bool:
    """An "answer" that is really the user's next request ("No, play some music instead", or a new question typed or
    said while XENIO was asking): three or more words beyond yes, no and filler. Unless it is a clear yes, the
    question ends as not approved and the words are kept as the next request instead of being used up as an
    unclear answer."""
    words = re.findall(r"[a-z']+", (answer or "").lower().replace("’", "'"))
    return len([w for w in words if w not in _YES and w not in _NO and w not in _FILLER]) >= 3


def is_affirmative(answer: str) -> bool:
    return interpret_answer(answer) == "yes"


def is_strict_confirmation(answer: str) -> bool:
    return interpret_answer(answer, strict=True) == "yes"
