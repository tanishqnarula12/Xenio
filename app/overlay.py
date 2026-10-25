"""XENIO Orb: the floating orb and global summon shortcut (native/orb), started and watched by the UI process.

The helper is a small native macOS app built from native/orb/*.swift (``native/orb/build.sh``; Apple's command line
tools). XENIO builds it when its sources are newer than the build, starts it as a child process and hands it the orb
page's address (with this session's key) on its stdin - never on the command line, where other programs could read
it. The helper quits as soon as that pipe closes, so it never outlives XENIO. If it crashes it is restarted (at most
three times in ten minutes), and every failure is reported in words (Settings > Diagnostics, ``--health``).

It runs as XENIO's child, so macOS attributes it to the app XENIO was started from: the triple-Space shortcut uses
that app's Accessibility permission (no new prompt); without it the helper falls back to Control-Option-Space.
"""

from __future__ import annotations

import json
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.config import PROJECT_ROOT
from app.logging_setup import event

log = logging.getLogger("xenio.overlay")

SOURCE_DIR = PROJECT_ROOT / "native" / "orb"
APP_BUNDLE = PROJECT_ROOT / "native" / "build" / "XENIO Orb.app"
BINARY = APP_BUNDLE / "Contents" / "MacOS" / "XenioOrb"
BUNDLE_ID = "local.xenio.orb"  # native/orb/Info.plist
MAX_RESTARTS = 3
RESTART_WINDOW = 600.0


def supported() -> bool:
    return sys.platform == "darwin"


def needs_build() -> bool:
    if not BINARY.exists():
        return True
    built = BINARY.stat().st_mtime
    return any(p.stat().st_mtime > built for p in SOURCE_DIR.iterdir() if p.suffix in (".swift", ".plist", ".sh"))


def build(run=subprocess.run) -> str | None:
    """Build the helper. Returns None on success, or why it failed (in words)."""
    try:
        done = run(["/bin/bash", str(SOURCE_DIR / "build.sh")], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not run the build ({exc})"
    if done.returncode != 0:
        lines = [ln for ln in (done.stderr or done.stdout or "").splitlines() if ln.strip()]
        why = next((ln for ln in lines if "error:" in ln), lines[-1] if lines else f"exit code {done.returncode}")
        if "xcrun" in why or "swiftc" in why and "not found" in why:
            why = "Apple's command line tools are missing: run xcode-select --install"
        return why.strip()[:300]
    return None


class OrbHelper:
    """Builds, starts, watches and stops the helper. Thread-safe: start/stop may come from any thread."""

    def __init__(self, url: str, lock: Path, binary: Path = BINARY, popen=subprocess.Popen, builder=build):
        self.url = url
        self.lock = lock
        self.binary = binary
        self._popen = popen
        self._build = builder
        self._proc: subprocess.Popen | None = None
        self._guard = threading.Lock()
        self._restarts: list[float] = []
        self._stopping = False
        self.problem = ""  # why it is not running, in words ("" while it runs)
        self.restart_delay = 1.0
        self.status: dict = {}  # what the helper reported (shortcut, permissions, screens)
        self.on_event = None  # (what, tone) -> None: crashes and recoveries, for Activity

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self.running else None

    def start(self) -> bool:
        """Build if needed, then start (in the caller's thread: building takes ~30 s the first time)."""
        if not supported():
            self.problem = "the floating orb needs macOS"
            return False
        with self._guard:
            self._stopping = False
            if self.running:
                return True
        if self.binary == BINARY and needs_build():
            event(log, "Building the XENIO Orb helper")
            why = self._build()
            if why:
                self.problem = f"the floating orb could not be built: {why}"
                event(log, "XENIO Orb build failed", logging.WARNING, error=why)
                return False
        return self._spawn()

    def _spawn(self) -> bool:
        with self._guard:
            if self._stopping:
                return False
            try:
                self.lock.parent.mkdir(parents=True, exist_ok=True)
                proc = self._popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, start_new_session=False)
                proc.stdin.write(json.dumps({"url": self.url, "lock": str(self.lock)}) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self.problem = f"the floating orb could not start: {exc}"
                event(log, "XENIO Orb did not start", logging.WARNING, error=str(exc))
                return False
            self._proc = proc
            self.problem = ""
        threading.Thread(target=self._watch, args=(proc,), name="xenio-orb-watch", daemon=True).start()
        event(log, "XENIO Orb started", pid=proc.pid)
        return True

    def _watch(self, proc: subprocess.Popen) -> None:
        err = ""
        try:
            err = (proc.stderr.read() if proc.stderr else "") or ""
        except (OSError, ValueError):
            pass
        code = proc.wait()
        with self._guard:
            if self._stopping or proc is not self._proc:
                return
            self._proc = None
        if code == 3 or "already running" in err:
            self.problem = "another XENIO's orb is already running"
            return
        if code in (-signal.SIGINT, -signal.SIGTERM):
            return  # Control-C (or a stop) sent to XENIO's process group reaches the helper too: nothing crashed
        now = time.monotonic()
        self._restarts = [t for t in self._restarts if now - t < RESTART_WINDOW] + [now]
        event(log, "XENIO Orb stopped unexpectedly", logging.WARNING, code=code, error=err.strip()[-200:])
        if len(self._restarts) > MAX_RESTARTS:
            self.problem = f"the floating orb kept stopping (last exit code {code}); it was not restarted again"
            self._tell("The floating orb kept stopping; it was not restarted again", "problem")
            return
        time.sleep(self.restart_delay)
        if self._spawn():
            self._tell(f"The floating orb stopped unexpectedly (exit code {code}) and was restarted", "recovery")

    def _tell(self, what: str, tone: str) -> None:
        if self.on_event is not None:
            try:
                self.on_event(what, tone)
            except Exception:  # noqa: BLE001 - a record must never stop the orb
                log.debug("orb event not recorded", exc_info=True)

    def open_app(self) -> bool:
        """Mission Control in XENIO Orb's own window, brought to the front (macOS lets the terminal hand it over)."""
        if not self.send({"type": "openApp"}):
            return False
        try:
            subprocess.run(["/usr/bin/open", "-b", BUNDLE_ID], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass  # the window is open; it may just not be in front
        return True

    def send(self, command: dict) -> bool:
        """A command for the helper (one JSON line on its stdin): {"type": "summon", "mode": "chat"} or "openApp"."""
        if command.get("type") not in ("summon", "openApp"):
            raise ValueError(f"not a helper command: {command.get('type')!r}")
        with self._guard:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                return False
            try:
                proc.stdin.write(json.dumps(command) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                return False
        return True

    def stop(self) -> None:
        with self._guard:
            self._stopping = True
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the helper quits when its stdin closes
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def info(self) -> dict:
        return {"supported": supported(), "running": self.running, "pid": self.pid, "problem": self.problem or None,
                "status": dict(self.status)}
