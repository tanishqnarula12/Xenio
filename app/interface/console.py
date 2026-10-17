"""Terminal text mode: ``python -m app.main``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading

from app.config import Settings
from app.memory.manager import MemoryManager
from app.orchestrator import Orchestrator, describe_actions
from app.safety.permissions import NO_CLEAR_ANSWER, PermissionRequest, interpret_answer
from app.sources import describe_sources

log = logging.getLogger("xenio.console")

HELP = """Commands:
  /new              start a fresh conversation with Hermes
  /memory [words]   show what Hermes remembers (optionally filtered)
  /help             show this help
  /exit             quit (also: exit, quit, Ctrl+C when idle)
While XENIO is answering, Ctrl+C stops the answer."""


class ConsoleIO:
    """Reads stdin on a daemon thread so a blocked ``input()`` never prevents a clean exit."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _ensure_reader(self) -> asyncio.Queue[str | None]:
        if self._queue is None:
            self._loop = asyncio.get_running_loop()
            self._queue = asyncio.Queue()
            self._thread = threading.Thread(target=self._read_forever, name="xenio-stdin", daemon=True)
            self._thread.start()
        return self._queue

    def _read_forever(self) -> None:
        assert self._loop is not None and self._queue is not None
        while True:
            line = sys.stdin.readline()
            if not line:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
                return
            self._loop.call_soon_threadsafe(self._queue.put_nowait, line.rstrip("\r\n"))

    async def readline(self, prompt: str = "") -> str | None:
        """One line of input, or None on EOF / interrupt."""
        queue = self._ensure_reader()
        if prompt:
            self.write(prompt, end="")
        return await queue.get()

    def discard_typeahead(self) -> None:
        """Drop lines typed while XENIO was busy, so they can't answer a permission prompt."""
        queue = self._ensure_reader()
        while not queue.empty():
            queue.get_nowait()

    def wake(self) -> None:
        """Unblock a pending readline() (used for Ctrl+C at the prompt)."""
        queue = self._ensure_reader()
        queue.put_nowait(None)

    @staticmethod
    def write(text: str = "", end: str = "\n") -> None:
        sys.stdout.write(text + end)
        sys.stdout.flush()


class ConsoleConfirmer:
    def __init__(self, io: ConsoleIO):
        self._io = io

    async def confirm(self, request: PermissionRequest, *, strict: bool) -> bool:
        self._io.discard_typeahead()
        self._io.write()
        self._io.write(f"!! PERMISSION REQUIRED [{request.risk.name}]")
        self._io.write(f"   This will {request.description}.")
        prompt = ("   High-risk action. Type 'confirm' to continue or 'no' to cancel: " if strict
                  else "   Do you want me to continue? [yes/no]: ")
        for _attempt in range(3):
            answer = await self._io.readline(prompt)
            if answer is None:  # EOF / interrupted: never a yes
                break
            verdict = interpret_answer(answer, strict=strict)
            if verdict != "unclear":
                approved = verdict == "yes"
                self._io.write("   -> continuing" if approved else "   -> cancelled")
                return approved
            prompt = "   Please type 'confirm' or 'no': " if strict else "   Please confirm yes or no: "
        self._io.write("   -> cancelled (no clear answer)")
        return NO_CLEAR_ANSWER


async def _show_notifications(io: ConsoleIO, notifications) -> None:
    """Print undelivered notifications (results of scheduled checks) and mark them delivered."""
    if notifications is None:
        return
    pending = await asyncio.to_thread(notifications.pending, 10)
    if not pending:
        return
    io.write()
    for note in pending:
        io.write(note.line())
    await asyncio.to_thread(notifications.mark_delivered, [n.id for n in pending])


async def run_text_mode(settings: Settings, orchestrator: Orchestrator, io: ConsoleIO, memory: MemoryManager,
                        notifications=None, proactive=None) -> int:
    name = settings.name.upper()
    loop = asyncio.get_running_loop()
    state = {"quit": False, "turn": None}
    background = None

    async def on_new_notifications(_new) -> None:
        if state["turn"] is None:  # idle at the prompt: show them now; otherwise after the answer
            await _show_notifications(io, notifications)
            io.write(f"{name} > ", end="")

    def on_sigint(_signum, _frame) -> None:
        loop.call_soon_threadsafe(_handle_interrupt)

    def _handle_interrupt() -> None:
        if state["turn"] is not None:
            io.write("\n[stopping...]")
            loop.create_task(orchestrator.interrupt())
        else:
            state["quit"] = True
            io.wake()

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        io.write("Connecting to Hermes (the first start can take ~30 s)...")
        if not await orchestrator.start():
            io.write(f"{name}: I can't reach my reasoning engine right now. Run `python -m app.main --health` for details.")
            return 1
        agent = orchestrator.hermes.agent_info
        io.write(f"{name} is ready - powered by {agent.get('name', 'Hermes')} {agent.get('version', '')}. /help for commands.")
        await _show_notifications(io, notifications)  # anything that arrived while XENIO was closed
        if proactive is not None:
            background = loop.create_task(proactive.run_forever(deliver=on_new_notifications))

        while not state["quit"]:
            io.write()
            line = await io.readline(f"{name} > ")
            if line is None or state["quit"]:
                break
            text = line.strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in {"/exit", "/quit", "exit", "quit"}:
                break
            if lowered == "/help":
                io.write(HELP)
                continue
            if lowered == "/new":
                ok = await orchestrator.new_session()
                io.write("Started a new conversation." if ok else f"{name}: I couldn't start a new conversation.")
                continue
            if lowered.startswith("/memory"):
                await _show_memory(io, memory, text[len("/memory"):].strip())
                continue

            io.write()
            io.write(f"{name}:")
            streamed = {"any": False, "text": []}

            def on_chunk(chunk: str) -> None:
                streamed["any"] = True
                streamed["text"].append(chunk)
                sys.stdout.write(chunk)
                sys.stdout.flush()

            turn = asyncio.ensure_future(orchestrator.process_text(text, on_chunk=on_chunk))
            state["turn"] = turn
            try:
                response = await turn
            finally:
                state["turn"] = None
            if response.cancelled:
                io.write("\n[interrupted]")
            elif not response.ok or not streamed["any"]:
                io.write(response.text)
            else:
                io.write()
                shown = "".join(streamed["text"]).strip()
                if response.text.strip() != shown:  # XENIO corrected the reply against the tool results
                    extra = response.text[len(shown):].strip() if response.text.startswith(shown) else response.text
                    io.write(extra if response.text.startswith(shown) else f"[XENIO correction] {extra}")
            for line in response.action_lines() + describe_sources(response.sources):
                io.write(line)
            await _show_notifications(io, notifications)
        io.write("Goodbye.")
        return 0
    finally:
        signal.signal(signal.SIGINT, previous)
        if background is not None:
            background.cancel()
        await orchestrator.close()


async def _show_memory(io: ConsoleIO, memory: MemoryManager, query: str) -> None:
    entries = await memory.recall(query)
    if not entries:
        io.write("Hermes has no stored memories" + (f" matching {query!r}." if query else " yet."))
        return
    for entry in entries:
        io.write(f"  [{entry.store}] {entry.text}")
