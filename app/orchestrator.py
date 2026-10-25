"""XENIO orchestrator: one entry point for every interface (terminal, voice, later others).

input text -> Hermes -> response, with graceful failure messages, logging, interruption,
and Hermes' permission prompts routed through the safety gate.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
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
MSG_DUPLICATE = "I'm already working on that exact request - I'll answer it once."


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


# Code on the user's screen (2026-10-25): replies said "compiled with no errors" and "compile finished clean" when the
# run showed no result at all, and "Done - pasted full C++ solution in the editor" in a turn that ran no tool.
_CODE_COMPILE_CLAIM = re.compile(
    r"\bcompil\w*\b(?:(?![.!?]).){0,40}?\b(?:clean(?:ly)?|fine|successful(?:ly)?|without (?:any )?errors?|with no errors?"
    r"|no errors?|ok(?:ay)?)\b|\bno (?:compile|compilation) errors?\b|\b(?:it|code|program|solution) compiles\b", re.I)
_CODE_PASS_CLAIM = re.compile(
    r"\b(?:test ?cases?|tests?|examples?|sample)\b(?:(?![.!?]).){0,25}?\bpass(?:ed|es)?\b"
    r"|\bpasse[sd]\b(?:(?![.!?]).){0,25}?\b(?:tests?|test ?cases?|examples?|sample)\b|\bmatches the expected\b", re.I)
_CODE_WRITE_CLAIM = re.compile(
    r"\b(?:pasted|typed|wrote|written|entered)\b(?:(?![.!?]).){0,60}?\b(?:in|into)\b(?:(?![.!?]).){0,30}?\beditor\b", re.I)


def claims_outcome(text: str) -> bool:
    """True when ``text`` states how an action ended ("Done.", "I've created...", "you declined", "I couldn't
    push", "it compiled", "the tests passed"). Voice mode holds such sentences back until the checks have compared
    them with the tool results."""
    return bool(_SUCCESS_CLAIM.search(text) or _DECLINE_CLAIM.search(text) or _FAILED_ACTION_CLAIM.search(text)
                or _CODE_COMPILE_CLAIM.search(text) or _CODE_PASS_CLAIM.search(text) or _CODE_WRITE_CLAIM.search(text))


def code_claim_check(text: str, tool_calls: list) -> str:
    """A reply about code on the screen that says more than the tool results do: written into the editor when no
    write ran in this request, compiled or passed when the last run of this request does not show it. The result's
    outcome (what XENIO measured) is the truth. Silent when nothing was written or run in this request and the reply
    does not claim a write: a claim about compiling may then come from the user's own run on screen."""
    from app.desktop.task import code_facts

    gui = [t for t in tool_calls if str(getattr(t, "title", "") or "").startswith("xenio_gui")]
    facts = [f for f in (code_facts(str(getattr(t, "result", "") or "")) for t in gui) if f]
    if _CODE_WRITE_CLAIM.search(text or ""):
        if not gui:  # (a plain text written with xenio_gui has no code outcome: then nothing is said)
            return "(XENIO correction: no desktop action ran in this request, so nothing was written into an editor.)"
        if facts and not any(f["entered"] for f in facts):
            return f"(XENIO correction: {facts[-1]['say']})"
    if not facts:
        return ""
    last = facts[-1]
    if (_CODE_COMPILE_CLAIM.search(text or "") and last["compiled"] is not True) or \
            (_CODE_PASS_CLAIM.search(text or "") and last["tests_passed"] is not True):
        return f"(XENIO correction: {last['say']})"
    return ""


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


_REMINDER_CLAIM = re.compile(r"\b(?:reminder\b[^.?!]{0,40}\b(?:set|scheduled|active|created|added)\b|(?:i'?ll|i will|will)"
                             r" (?:remind|notify|nudge) you\b|reminder (?:is |has been )?(?:set|scheduled|active))", re.I)


def reminder_claim_check(text: str, tool_calls: list, pending_reminders: Callable[[], int]) -> str:
    """A reply that says a reminder is set, in a turn where no reminder was made, while XENIO's scheduler holds none at
    all: the model is repeating something it believed earlier (observed: "reminder already set" when nothing was).
    The scheduler is the truth. Silent whenever a reminder really is pending (it may be the one meant)."""
    if not _REMINDER_CLAIM.search(text or ""):
        return ""
    if any("xenio_schedule" in (getattr(t, "title", "") or "") or "reminder set" in (getattr(t, "title", "") or "")
           for t in tool_calls):
        return ""
    try:
        if pending_reminders() > 0:
            return ""
    except Exception:  # noqa: BLE001 - when the scheduler cannot be read, say nothing rather than guess
        return ""
    return ("(XENIO correction: no reminder is set - XENIO's scheduler has none. Say \"remind me at ... to ...\" and "
            "it will be set.)")


def _pending_reminders() -> int:
    from app.config import default_hermes_home
    from app.reminders import Reminders

    return len(Reminders(default_hermes_home()).list())


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
    command_id: str = ""  # this request's own id (logs, duplicate suppression)
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
        self.pending_reminders: Callable[[], int] = _pending_reminders  # XENIO reminders waiting in Hermes' scheduler
        self._local_notes: list[str] = []  # what the fast path did since Hermes' last turn
        self.gate = gate
        self.paths = paths if isinstance(paths, PathPolicy) else PathPolicy(workspace=paths)
        self.sources = SourceLog(sources_file)
        self._declined: list[str] = []
        self._approved: list[str] = []  # actions the user explicitly said yes to in this turn
        self._busy = asyncio.Lock()
        self._in_flight: dict[str, str] = {}  # the words of each request running or waiting -> its command id
        self.stop_dir: Path | None = None  # XENIO's data folder: a stop also stops desktop actions (set by main)
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
        start/finish while the turn runs (voice mode narrates them).

        Every request gets a command id. The same words again while that request is still running (or waiting for
        the one before it) are not run a second time: a double submit, or one request reaching XENIO twice (the quick
        chat and voice), would otherwise solve and type the same thing twice. Once it has ended, the same words are a
        new request and run normally."""
        text = text.strip()
        if not text:
            return XenioResponse("", ok=False, error="empty_input")
        key, command_id = " ".join(text.casefold().split()), uuid.uuid4().hex[:12]
        if key in self._in_flight:
            event(log, "DUPLICATE_COMMAND_SUPPRESSED", logging.WARNING, command_id=command_id,
                  running=self._in_flight[key])
            return XenioResponse(MSG_DUPLICATE, ok=False, error="duplicate", command_id=command_id)
        self._in_flight[key] = command_id
        try:
            response = await self._process(text, command_id, on_chunk, context, on_tool)
        finally:
            self._in_flight.pop(key, None)
        response.command_id = command_id
        return response

    async def _process(self, text: str, command_id: str, on_chunk: ChunkHandler | None, context: str | None,
                       on_tool: Callable[[object], None] | None) -> XenioResponse:
        async with self._busy:
            event(log, "User command received", chars=len(text), command_id=command_id)
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
            if not correction:
                correction = await asyncio.to_thread(reminder_claim_check, reply.text, tools, self.pending_reminders)
            if not correction:
                correction = code_claim_check(reply.text, tools)
            if correction:
                event(log, "REPLY_CORRECTED", logging.WARNING, note=correction, replaced=replace)
                if replace:
                    log.debug("Replaced model reply: %s", reply.text)
                    return XenioResponse(correction, ok=True, **extra)
                return XenioResponse(f"{reply.text}\n\n{correction}", ok=True, **extra)
            return XenioResponse(reply.text, ok=True, **extra)

    async def interrupt(self) -> None:
        """Stop the Hermes turn in progress, if any (barge-in, Ctrl+C, the UI's stop) - and XENIO's hands with it:
        Hermes abandons a running tool call on a stop, but the thread typing it would go on."""
        if self.busy:
            if self.stop_dir is not None:
                from app.desktop.stop import request_stop

                request_stop("cancel", self.stop_dir)
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
