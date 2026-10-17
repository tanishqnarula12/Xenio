"""XENIO orchestrator: one entry point for every interface (terminal, voice, later others).

input text -> Hermes -> response, with graceful failure messages, logging, interruption,
and Hermes' permission prompts routed through the safety gate.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.hermes.client import (
    ChunkHandler,
    HermesClient,
    HermesError,
    HermesPermissionRequest,
    HermesTimeout,
    HermesUnavailable,
)
from app.language.detect import language_note
from app.logging_setup import event
from app.safety.permissions import PermissionGate, from_hermes_request
from app.safety.policy import PathPolicy
from app.sources import Source, SourceLog

log = logging.getLogger("xenio.orchestrator")

MSG_HERMES_UNAVAILABLE = "I can't reach my reasoning engine right now."
MSG_HERMES_TIMEOUT = "My reasoning engine is taking too long to answer. Please try again."
MSG_HERMES_ERROR = "Something went wrong in my reasoning engine. Please try again."
MSG_EMPTY_REPLY = "My reasoning engine returned an empty answer."


_STATUS_LABEL = {"completed": "done", "failed": "FAILED", "in_progress": "unfinished", "pending": "not run"}

# Hermes' model sometimes explains a result wrongly. Where XENIO's own records prove the reply false,
# XENIO says so in the reply itself (printed and spoken).
_DECLINE_CLAIM = re.compile(r"\b(because you (declined|rejected|denied|refused)|you (have )?(declined|rejected|denied|refused|"
                            r"said no)|(declined|rejected|denied) by you|you chose not to|you (have )?explicitly rejected)",
                            re.IGNORECASE)
_DONE_VERBS = (r"created|deleted|moved|renamed|pushed|sent|submitted|saved|downloaded|installed|committed|added|updated|"
               r"scheduled|removed|completed|opened|booked")
_SUCCESS_CLAIM = re.compile(rf"(\b(successfully|i('ve| have) ({_DONE_VERBS})|(has|have) been ({_DONE_VERBS})|is now ({_DONE_VERBS}))\b"
                            r"|^\s*(done|all done|finished|completed)\b)", re.IGNORECASE)
_FAILED_ACTION_CLAIM = re.compile(
    r"\b(?:i (?:couldn'?t|could not|was unable to|wasn'?t able to|failed to)|unable to|failed to) (?P<verb>create|delete|move|"
    r"rename|save|push|send|add|complete|finish|open|download|update|schedule|remove|install|commit)\b"
    rf"|\b(?:was|were|has|have)(?:n'?t| not)(?: been)? (?P<done>{_DONE_VERBS})\b", re.IGNORECASE)


def claims_outcome(text: str) -> bool:
    """True when ``text`` states how an action ended ("Done.", "I've created...", "you declined", "I couldn't
    push"). Voice mode holds such sentences back until ``truth_check`` has compared them with the tool results."""
    return bool(_SUCCESS_CLAIM.search(text) or _DECLINE_CLAIM.search(text) or _FAILED_ACTION_CLAIM.search(text))


def truth_check(text: str, tool_calls: list, declined: list[str], approved: list[str]) -> tuple[str, bool]:
    """Where XENIO's own records (the tool results) prove the reply wrong: (correction, replace).

    The tool result is the source of truth, not the model: a reply claiming success when nothing succeeded
    is REPLACED by what really happened (before it is spoken); other contradictions get a correction note.
    ('', False) when the reply agrees with the records."""
    failed = [t for t in tool_calls if getattr(t, "status", "") == "failed"]
    completed = [t for t in tool_calls if getattr(t, "status", "") == "completed"]
    if approved and not declined and _DECLINE_CLAIM.search(text):
        what = (f" It ran, but failed: {(failed[0].detail or failed[0].title)[:160]}" if failed
                else " It was allowed to run.")
        return f"(XENIO correction: you said yes in this turn - nothing was declined.{what})", False
    if (failed or declined) and not completed and _SUCCESS_CLAIM.search(text):
        if failed:
            reason = " ".join((failed[0].detail or f"{failed[0].title} failed").split())[:200]
            return f"The operation failed: {reason}.", True
        return f"That was not done: {' '.join(declined[0].split())[:200]}.", True
    claim = _FAILED_ACTION_CLAIM.search(text) if completed and not failed and not declined else None
    if claim:
        stem = (claim.group("verb") or claim.group("done") or "").lower()[:4]
        matching = [t for t in completed if stem and stem in (t.title or "").lower()]
        if matching:  # the same kind of action the reply says failed actually succeeded
            return f"(XENIO correction: the tool reported success: {' '.join((matching[0].title or '').split())[:120]}.)", False
    return "", False


def describe_actions(tool_calls: list, declined: list[str] | None = None) -> list[str]:
    """Ground truth under each reply: what Hermes actually ran and how each tool call ended, plus the
    actions the user declined (Hermes reports no tool call for those)."""
    lines = [f"   [declined] {' '.join(d.split())[:110]}" for d in declined or []]
    for call in tool_calls:
        title = " ".join((getattr(call, "title", "") or getattr(call, "tool_call_id", "")).split())
        status = _STATUS_LABEL.get(getattr(call, "status", ""), getattr(call, "status", "") or "?")
        detail = getattr(call, "detail", "") if getattr(call, "status", "") == "failed" else ""
        lines.append(f"   [{status}] {title[:110]}" + (f" - {detail[:140]}" if detail else ""))
    return (["   actions:"] + lines) if lines else []


@dataclass
class XenioResponse:
    text: str
    ok: bool
    error: str | None = None  # empty_input | hermes_unavailable | hermes_timeout | hermes_error | empty_reply
    cancelled: bool = False
    tool_calls: list = field(default_factory=list)  # what Hermes actually did (ToolActivity), for display
    sources: list[Source] = field(default_factory=list)  # web pages this answer used (recorded by the plugin)
    declined: list[str] = field(default_factory=list)  # actions the user said no to (or did not confirm)

    def action_lines(self) -> list[str]:
        return describe_actions(self.tool_calls, self.declined)


class Orchestrator:
    def __init__(self, hermes: HermesClient, gate: PermissionGate | None, paths: PathPolicy | Path,
                 sources_file: Path | None = None, fast_path=None):
        self.hermes = hermes
        self.fast = fast_path  # app.fastpath.FastPath: simple commands without the model (None = always Hermes)
        self._local_notes: list[str] = []  # what the fast path did since Hermes' last turn
        self.gate = gate
        self.paths = paths if isinstance(paths, PathPolicy) else PathPolicy(workspace=paths)
        self.sources = SourceLog(sources_file)
        self._declined: list[str] = []
        self._approved: list[str] = []  # actions the user explicitly said yes to in this turn
        self._busy = asyncio.Lock()
        hermes.set_permission_handler(self._on_hermes_permission)

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def _accepts_on_tool(self) -> bool:
        """Older/other HermesClient implementations may not take ``on_tool``; then tool events are simply skipped."""
        import inspect

        try:
            params = inspect.signature(self.hermes.send_message).parameters
        except (TypeError, ValueError):
            return False
        return "on_tool" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    async def start(self) -> bool:
        try:
            await self.hermes.start()
            return True
        except HermesError as exc:
            event(log, "Hermes unavailable at startup", logging.ERROR, error=str(exc))
            return False

    async def process_text(
        self, text: str, on_chunk: ChunkHandler | None = None, context: str | None = None,
        on_tool: Callable[[object], None] | None = None,
    ) -> XenioResponse:
        """``context`` is an interface note for Hermes (e.g. "this is a spoken conversation"),
        appended after the user's words so Hermes can adapt its answer. ``on_tool`` sees every tool
        start/finish while the turn runs (voice mode narrates them)."""
        text = text.strip()
        if not text:
            return XenioResponse("", ok=False, error="empty_input")
        async with self._busy:
            event(log, "User command received", chars=len(text))
            log.debug("User command text: %s", text)
            started = time.monotonic()
            if self.fast is not None:
                fast = await self.fast.handle(text)
                if fast is not None:  # done by XENIO itself, in well under a second
                    if fast.note:
                        self._local_notes = [*self._local_notes, fast.note][-5:]
                    if on_chunk is not None:
                        on_chunk(fast.text)
                    # a failed step is a normal reply ("That didn't work: ..."), marked [FAILED] in the action list
                    return XenioResponse(fast.text, ok=True, tool_calls=fast.actions)
            # V5.5: the language this request was spoken in (reply in it) and its language-independent intents
            local, self._local_notes = "\n".join(self._local_notes), []
            notes = "\n".join(n for n in (context, local, language_note(text)) if n)
            message = f"{text}\n\n{notes}" if notes else text
            mark = self.sources.mark()
            self._declined, self._approved = [], []
            try:
                extra_args = {"on_tool": on_tool} if on_tool is not None and self._accepts_on_tool() else {}
                reply = await self.hermes.send_message(message, on_chunk=on_chunk, **extra_args)
            except HermesTimeout as exc:
                event(log, "Hermes timed out", logging.ERROR, error=str(exc))
                return XenioResponse(MSG_HERMES_TIMEOUT, ok=False, error="hermes_timeout")
            except HermesUnavailable as exc:
                event(log, "Hermes unavailable", logging.ERROR, error=str(exc))
                return XenioResponse(MSG_HERMES_UNAVAILABLE, ok=False, error="hermes_unavailable")
            except HermesError as exc:
                event(log, "Hermes error", logging.ERROR, error=str(exc))
                return XenioResponse(MSG_HERMES_ERROR, ok=False, error="hermes_error")

            event(
                log,
                "Hermes response received",
                chars=len(reply.text),
                ms=int((time.monotonic() - started) * 1000),
                stop=reply.stop_reason,
                tools=len(reply.tool_calls),
            )
            tools = list(reply.tool_calls)
            # read after the turn: Hermes may have moved to a new session during it (lost-session recovery)
            sources = self.sources.since(mark, getattr(self.hermes, "session_id", None))
            if sources:
                event(log, "Web sources used", count=len(sources), sites=",".join(sorted({s.site for s in sources}))[:200])
            extra = {"tool_calls": tools, "sources": sources, "declined": list(self._declined)}
            if reply.cancelled:
                return XenioResponse(reply.text, ok=True, cancelled=True, **extra)
            if not reply.text:
                return XenioResponse(MSG_EMPTY_REPLY, ok=False, error="empty_reply", **extra)
            correction, replace = truth_check(reply.text, tools, self._declined, self._approved)
            if correction:
                event(log, "REPLY_CORRECTED", logging.WARNING, note=correction, replaced=replace)
                if replace:
                    log.debug("Replaced model reply: %s", reply.text)
                    return XenioResponse(correction, ok=True, **extra)
                return XenioResponse(f"{reply.text}\n\n{correction}", ok=True, **extra)
            return XenioResponse(reply.text, ok=True, **extra)

    async def interrupt(self) -> None:
        """Stop the Hermes turn in progress, if any (barge-in, Ctrl+C)."""
        if self.busy:
            await self.hermes.cancel()

    async def new_session(self) -> bool:
        try:
            await self.hermes.new_session()
            return True
        except HermesError as exc:
            event(log, "Could not start a new session", logging.ERROR, error=str(exc))
            return False

    async def close(self) -> None:
        await self.hermes.close()

    async def _on_hermes_permission(self, req: HermesPermissionRequest) -> bool:
        request = from_hermes_request(req, self.paths)
        event(log, "Hermes requested permission", action=request.action, risk=request.risk.name)
        if self.gate is None:
            self._declined.append(f"{request.description} (no one to confirm it)")
            return False
        decision = await self.gate.check(request)
        if not decision.approved:
            self._declined.append(f"{request.description} ({decision.reason})")
        elif decision.reason == "user approved":
            self._approved.append(request.description)
        return decision.approved
