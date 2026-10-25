"""One coding task on the user's screen: a user request that reads a problem page and writes code into its editor.

Measured on GeeksforGeeks (2026-10-25): one request typed the same program twice, a later one wrote it into XENIO's own
chat box, and replies said "compiled with no errors" when the page showed no result at all. The task makes each step
explicit and checkable:

    INSPECTING -> SOLVING -> WRITING -> VERIFYING -> VERIFIED -> RUNNING -> RESULT_CONFIRMED

with RESULT_FAILED (it ran: error, wrong output, time limit), INCONCLUSIVE (written or run, but the result could not be
read), FAILED, CANCELLED and TIMED_OUT. A step that does not follow from the current state is refused
(``TransitionError``), so a second write cannot start while one is still in flight.

Every write is recorded (a digest of the program, never its text): the same program is not written again once it is
in the editor, unless the editor shows something else (evidence), and then at most ``MAX_SAME_WRITES`` times. A new
user request is a new task. Standard library only (it runs inside Hermes).
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskState(str, Enum):
    INSPECTING = "INSPECTING"
    SOLVING = "SOLVING"
    WRITING = "WRITING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    RUNNING = "RUNNING"
    RESULT_CONFIRMED = "RESULT_CONFIRMED"
    RESULT_FAILED = "RESULT_FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


S = TaskState
_ENDS = {S.FAILED, S.CANCELLED, S.TIMED_OUT}
_AFTER_A_RESULT = {S.INSPECTING, S.SOLVING, S.WRITING, S.RUNNING}  # read again, fix and write again, run again
TRANSITIONS: dict[TaskState | None, set[TaskState]] = {
    None: {S.INSPECTING, S.WRITING, S.RUNNING},
    S.INSPECTING: {S.SOLVING} | _ENDS,
    S.SOLVING: {S.INSPECTING, S.WRITING, S.RUNNING, S.CANCELLED},
    S.WRITING: {S.VERIFYING} | _ENDS,
    S.VERIFYING: {S.WRITING, S.VERIFIED, S.INCONCLUSIVE} | _ENDS,  # WRITING: one evidence-based rewrite
    S.VERIFIED: _AFTER_A_RESULT | {S.CANCELLED},
    S.RUNNING: {S.RESULT_CONFIRMED, S.RESULT_FAILED, S.INCONCLUSIVE} | _ENDS,
    S.RESULT_CONFIRMED: _AFTER_A_RESULT | {S.CANCELLED},
    S.RESULT_FAILED: _AFTER_A_RESULT | {S.CANCELLED},
    S.INCONCLUSIVE: _AFTER_A_RESULT | {S.CANCELLED},
    S.FAILED: _AFTER_A_RESULT | {S.CANCELLED},
    S.CANCELLED: set(),  # the next request starts a new task
    S.TIMED_OUT: set(),
}
IN_FLIGHT = {S.INSPECTING, S.WRITING, S.VERIFYING, S.RUNNING}
MAX_SAME_WRITES = 2  # the same program per task: once, and once more only when the editor shows something else
MAX_WRITES = 8  # programs per task (debugging writes new versions: "up to 3 times" plus fallbacks)


class TransitionError(Exception):
    """A step that does not follow from the task's state (e.g. a second write while one is in flight)."""


def digest(text: str) -> str:
    """The program's identity, spacing aside (editors re-indent): never its text."""
    return hashlib.sha256(re.sub(r"\s+", "", text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class Write:
    attempt: int
    digest: str
    window: int
    chars: int
    how: str = ""  # pasted | typed
    verified: bool | None = None
    at: float = 0.0
    whole: bool = True  # replaced the editor's whole content (False: typed in at the caret)


@dataclass
class EditorTask:
    session: str = ""
    turn: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: TaskState | None = None
    window: int = 0  # the window the task works in (read, written, run)
    history: list[tuple[str, float]] = field(default_factory=list)
    writes: list[Write] = field(default_factory=list)
    run: dict | None = None  # the last run's status and parsed facts
    clock: object = time.time

    def can(self, state: TaskState) -> bool:
        return state in TRANSITIONS[self.state]

    def to(self, state: TaskState) -> None:
        if not self.can(state):
            raise TransitionError(f"{self.state.value if self.state else 'NEW'} -> {state.value} is not a valid step")
        self.state = state
        self.history.append((state.value, round(self.clock(), 3)))

    def end(self, state: TaskState) -> None:
        """FAILED / CANCELLED / TIMED_OUT from wherever the task is (never raises: an ending is always recorded)."""
        if self.can(state):
            self.to(state)

    @property
    def in_flight(self) -> bool:
        return self.state in IN_FLIGHT

    # ---- the write ledger ------------------------------------------------------------------------------------------

    def last_write(self, text: str, window: int) -> Write | None:
        d = digest(text)
        return next((w for w in reversed(self.writes) if w.whole and w.digest == d and w.window == window), None)

    def admit_write(self, text: str, window: int, whole: bool = True) -> None:
        """Raises TransitionError when this write must not start: one is in flight, or - for a write that replaces the
        editor's whole content - the same program was written as often as allowed, or too many programs were. (Text
        typed in at the caret may repeat: "add the same line twice" is two writes.)"""
        if self.in_flight:
            raise TransitionError(f"a {self.state.value.lower()} step of this request is still running")
        if not whole:
            return
        same = sum(1 for w in self.writes if w.whole and w.digest == digest(text) and w.window == window)
        if same >= MAX_SAME_WRITES:
            raise TransitionError(f"this exact program was already written {same} times in this request")
        programs = sum(1 for w in self.writes if w.whole)
        if programs >= MAX_WRITES:
            raise TransitionError(f"{programs} programs were already written in this request")

    def record_write(self, text: str, window: int, how: str, verified: bool | None, whole: bool = True) -> Write:
        w = Write(len(self.writes) + 1, digest(text), window, len(text), how, verified, round(self.clock(), 3), whole)
        self.writes.append(w)
        return w

    def public(self) -> dict:
        return {"id": self.id, "state": self.state.value if self.state else "NEW", "writes": len(self.writes),
                "steps": [s for s, _ in self.history][-12:]}


# ---- what the user is told --------------------------------------------------------------------------------------

def _run_facts(run: dict) -> tuple[bool | None, bool | None, str]:
    """(compiled, tests_passed, what the run showed) - True / False, or None = not known."""
    status, kind = run.get("status"), run.get("kind")
    if not run.get("ran"):
        compiled, tests, said = None, None, "it was not run (no Run button was found)"
    elif status == "error" and kind == "compile":
        error = f": {run['first_error']}" if run.get("first_error") else ""
        compiled, tests, said = False, False, f"it did NOT compile{error}"
    elif status == "error":
        compiled, tests, said = True, False, f"it compiled, but the run failed: {run.get('kind_text') or 'error'}"
    elif status == "wrong_output":
        compiled, tests, said = True, False, "it compiled and ran, but its output differs from the expected output"
    elif status == "matches_expected":
        compiled, tests, said = True, True, "it compiled and its output matches the expected output for the example input"
    elif status == "ran":
        compiled, tests, said = True, None, "it compiled and ran, but the page showed no expected output to compare with"
    else:  # no_output
        compiled, tests, said = None, None, ("Compile & Run was clicked, but no result appeared, so it is NOT known "
                                             "whether it compiled")
    counts = run.get("tests")
    if counts:
        tests = counts["passed"] == counts["total"]
        said += f" (test cases passed: {counts['passed']}/{counts['total']})"
    return compiled, tests, said


_COUNTS = re.compile(r"test cases passed: (\d+)/(\d+)", re.I)
_MESSAGE = re.compile(r"\*\*message:\*\*\s*(.+?)(?:\s+-\s+\*\*|$)")


def code_facts(text: str) -> dict | None:
    """The facts in an ``outcome`` sentence (as it reaches XENIO in Hermes' report of a finished tool call), or None
    when ``text`` holds none. The inverse of ``outcome``'s wording: XENIO checks the model's reply against it."""
    text = text or ""
    entered = False if "code was NOT entered" in text else True if "code was entered" in text else None
    if "did NOT compile" in text:
        compiled = False
    elif "NOT known whether it compiled" in text or "it was not run" in text.lower():
        compiled = None
    else:
        compiled = True if re.search(r"\bit compiled\b", text, re.I) else None
    if entered is None and compiled is None and "Compile & Run was clicked" not in text and "was not run" not in text:
        return None
    counts = _COUNTS.search(text)
    tests = (int(counts.group(1)) == int(counts.group(2))) if counts else True if "matches the expected output" in text \
        else False if compiled is False or "differs from the expected output" in text or "the run failed" in text else None
    said = _MESSAGE.search(text)
    return {"entered": entered, "compiled": compiled, "tests_passed": tests,
            "say": (said.group(1) if said else text).strip()[:300]}


def outcome(entered: bool | None, verified: bool | None, run: dict | None = None, reason: str = "") -> dict:
    """The facts of a write and its run, and the one sentence that states them - never stronger than the evidence.
    ``entered`` None: nothing was written by this call (only run). ``compiled`` / ``tests_passed``: True, False, or
    None = not known. Code entered, contents verified, compiled and tests passed are four different facts."""
    if entered is False:
        say = f"The code was NOT entered{': ' + reason if reason else ''}"
    elif entered is None:
        say = ""
    elif verified is True:
        say = "The code was entered and verified in the editor"
    elif verified is False:
        say = "The code was entered, but the editor does NOT hold it exactly as written"
    else:
        say = "The code was entered, but the editor's contents could not be read back to check it"
    compiled = tests = None
    if run and entered is not False:
        compiled, tests, said = _run_facts(run)
        say = f"{say}; {said}" if say else said[:1].upper() + said[1:]
    elif entered:
        say += "; it has not been compiled or run"
    status = (run or {}).get("status")
    return {"code_entered": entered, "editor_verified": verified if entered else None, "compiled": compiled,
            "tests_passed": tests,
            "inconclusive": bool(entered and verified is None) or status == "no_output" or (status == "ran" and tests is None)
            or bool(run and not run.get("ran")),
            "say": say + "."}
