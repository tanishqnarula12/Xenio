"""The agents' browsers. Each agent gets its own real WebKit browser with its own private cookies and no window on your screen, so several can work side by side without touching your mouse, your keyboard or your own browser.

A page comes back as text with numbered controls, which costs a fraction of what a screenshot does. Clicking a control is done by the page's own events, not by moving anything on your screen, and the agent cursor is drawn inside that page, labelled as such, at the point really acted on. The picture XENIO keeps of each browser is for you to watch, and no model sees it unless an agent asks to look.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import logging
import shutil
import subprocess
import threading
from pathlib import Path

log = logging.getLogger("xenio.foundry")

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "native" / "build" / "xenio-hands"
BUILD = ROOT / "native" / "hands" / "build.sh"
MAX_BROWSERS = 5
COMMAND_SECONDS = 45.0
# one per agent, and never one that could be mistaken for a warning
COLOURS = ("#2e7dff", "#12a150", "#b154f0", "#e0841b", "#0fa3b1", "#d6336c", "#5c7cfa", "#7a9e00")


class HandsError(Exception):
    pass


def colour_for(name: str) -> str:
    return COLOURS[sum(map(ord, name or "x")) % len(COLOURS)]


def ensure_built(binary: Path = BINARY, build: Path = BUILD) -> Path:
    if binary.is_file():
        return binary
    if not build.is_file() or shutil.which("swiftc") is None:
        raise HandsError("The agents' browser is not built, and Apple's command line tools are not here to build it (xcode-select --install).")
    done = subprocess.run(["/bin/bash", str(build)], capture_output=True, text=True, timeout=300)
    if done.returncode != 0 or not binary.is_file():
        raise HandsError(f"The agents' browser could not be built: {(done.stderr or done.stdout)[-300:]}")
    return binary


class Browser:
    """One browser process, one command at a time."""

    def __init__(self, agent: str, *, live: Path | None = None, profile: str = "", allow_local: int = 0, width: int = 1280, height: int = 860, binary: Path | None = None, vault_dir: Path | None = None, extra: tuple[str, ...] = ()):
        self.agent = agent
        self.vault_dir = vault_dir
        self.extra = extra
        self.live = live
        self.profile = profile
        self.allow_local = allow_local
        self.size = (width, height)
        self.binary = binary or BINARY
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.number = 0
        self.url = ""
        self.title = ""
        self.last_action: dict = {}

    async def start(self) -> None:
        binary = await asyncio.to_thread(ensure_built, self.binary)
        argv = [str(binary), "browser", "--agent", self.agent[:40], "--color", colour_for(self.agent), "--width", str(self.size[0]), "--height", str(self.size[1])]
        if self.live is not None:
            self.live.parent.mkdir(parents=True, exist_ok=True)
            argv += ["--live", str(self.live)]
        if self.profile:
            argv += ["--profile", self.profile]
        if self.allow_local:
            argv += ["--allow-local", str(self.allow_local)]
        if self.vault_dir is not None:
            argv += ["--vault-dir", str(self.vault_dir)]
        argv += list(self.extra)
        self.proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=2_000_000)
        first = await asyncio.wait_for(self.proc.stdout.readline(), 15.0)
        if not first or not json.loads(first).get("ready"):
            raise HandsError("The agents' browser did not start.")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def do(self, command: str, wait: float = COMMAND_SECONDS, **fields) -> dict:
        async with self.lock:
            if not self.running:
                await self.start()
            self.number += 1
            line = json.dumps({"id": self.number, "cmd": command, **fields}) + "\n"
            try:
                self.proc.stdin.write(line.encode())
                await self.proc.stdin.drain()
                raw = await asyncio.wait_for(self._reply(self.number), wait)
            except (TimeoutError, asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                await self.close()
                raise HandsError("The browser stopped answering, so it was closed. Open the page again.") from None
            if not raw:
                await self.close()
                raise HandsError("The browser closed. Open the page again.")
            out = json.loads(raw)
            self.url = out.get("url") or self.url
            self.title = out.get("title") or self.title
            if command in ("open", "click", "type", "choose", "key", "scroll", "back"):
                self.last_action = {"action": command, "x": out.get("x"), "y": out.get("y"), "label": str(out.get("label") or "")[:80], "url": self.url, "title": self.title, "ok": bool(out.get("ok"))}
            return out

    async def _reply(self, number: int) -> bytes:
        """The answer to this command. A login you chose to save is reported along the way, by website only."""
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                return raw
            try:
                said = json.loads(raw)
            except ValueError:
                continue
            if said.get("id") == number:
                return raw

    async def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), 3.0)
        except (ProcessLookupError, TimeoutError, asyncio.TimeoutError):
            pass


class Hands:
    """Who has which browser. They end with the work, or when you say stop."""

    def __init__(self, live_root: Path, binary: Path | None = None, max_browsers: int = MAX_BROWSERS, vault_dir: Path | None = None):
        self.vault_dir = vault_dir
        self.allow_local = 0  # for a test site on this Mac. XENIO itself never sets this
        self.extra: tuple[str, ...] = ()
        self.live_root = Path(live_root)
        self.binary = binary
        self.max_browsers = max_browsers
        self.browsers: dict[tuple[str, str, str], Browser] = {}
        self.slots = asyncio.Semaphore(max_browsers)

    def live_path(self, workflow: str, agent: str) -> Path:
        return self.live_root / workflow / f"{agent}.jpg"

    def open_for(self, workflow: str, agent: str) -> list[Browser]:
        return [b for (w, a, _p), b in self.browsers.items() if w == workflow and a == agent]

    async def browser(self, workflow: str, agent: str, name: str = "", profile: str = "") -> Browser:
        key = (workflow, agent, profile)
        found = self.browsers.get(key)
        if found is not None and found.running:
            return found
        if found is None and len(self.browsers) >= self.max_browsers:
            raise HandsError(f"{self.max_browsers} browsers are already open. Finish with one first: each one is a real browser using real memory.")
        browser = Browser(name or agent, live=self.live_path(workflow, agent), profile=profile, binary=self.binary, vault_dir=self.vault_dir, allow_local=self.allow_local, extra=self.extra)
        await browser.start()
        self.browsers[key] = browser
        return browser

    async def close_agent(self, workflow: str, agent: str) -> None:
        for key in [k for k in self.browsers if k[0] == workflow and k[1] == agent]:
            await self.browsers.pop(key).close()

    async def close_workflow(self, workflow: str) -> None:
        for key in [k for k in self.browsers if k[0] == workflow]:
            await self.browsers.pop(key).close()

    async def close(self) -> None:
        for key in list(self.browsers):
            await self.browsers.pop(key).close()

    def state(self, workflow: str) -> list[dict]:
        return [{"agent": agent, "name": b.agent, "colour": colour_for(b.agent), "url": b.url, "title": b.title, "signed_in": bool(profile), "last": b.last_action, "kind": "private browser", "picture": self.live_path(workflow, agent).is_file()} for (w, agent, profile), b in self.browsers.items() if w == workflow and b.running]


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class Serving:
    """A folder on a local port for as long as it takes to look at it."""

    def __init__(self, folder: Path):
        handler = functools.partial(_Quiet, directory=str(folder))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "Serving":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()
