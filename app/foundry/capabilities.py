"""What XENIO can really do right now, each answer found by asking and never assumed. When something a job needs is missing, it says so before any tokens are spent, and it says what would fix it.

Capabilities are what can be accomplished. Tools are how. Skills are packaged know-how. Agents are who does it, models are how they think, and connectors reach other services. Workflows put them together.
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from dataclasses import dataclass

from app.foundry import hands as hands_module
from app.foundry import sandbox

log = logging.getLogger("xenio.foundry")
CACHE_SECONDS = 20.0

# what each capability is, what it uses, and what an agent is given to use it
CATALOG = {
    "thinking": ("Thinking", "model", "models on Nabh, chosen for each kind of work"),
    "web_search": ("Searching the web", "tool", "web_search and web_extract: no key needed"),
    "vision": ("Seeing pictures and pages", "model", "kimi-k3, with qwen3-6-27b behind it"),
    "browser": ("A private browser for each agent", "tool", "real WebKit browsers, with no window on your screen"),
    "signed_in_sites": ("Websites you are signed in to", "tool", "your own browser, where you are already signed in: Safari through its own scripting on any desktop, or any browser on the screen you are looking at. The vault is only used if a page asks to sign in"),
    "desktop_control": ("Using your real mouse and keyboard", "computer use", "one agent at a time, only after your yes"),
    "screen_reading": ("Reading what is on your screen", "computer use", "window text first, then a picture"),
    "code": ("Writing and running code", "tool", "the macOS sandbox, with no network and your home folder shut"),
    "documents": ("Word documents", "tool", "made here, checked with macOS"),
    "spreadsheets": ("Excel workbooks", "tool", "made here, with every formula worked out and checked"),
    "presentations": ("PowerPoint decks", "tool", "made here, with a look from the job's art direction"),
    "images": ("Making pictures", "tool", "Stable Diffusion XL on Nabh, square only"),
    "figma": ("Designing in Figma", "connector", "in the file you have open, through XENIO's plugin in the Figma app: agents draw whole frames, look at them, and fix them. Everything can be undone"),
    "design": ("Designing pages, decks and documents", "tool", "an art direction for each job and design skills, checked by looking at the result. Not in Figma: Figma is not connected"),
    "gmail": ("Reading your Gmail", "connector", "your Google sign-in, read only, after your yes for each job"),
    "calendar": ("Reading your Google Calendar", "connector", "your Google sign-in, read only, after your yes for each job"),
    "drive": ("Google Drive", "connector", "not connected to agents yet"),
    "github": ("Reading your GitHub", "connector", "the gh command's sign-in, read only, after your yes for each job"),
    "linkedin": ("LinkedIn", "website", "in your own browser, where you are signed in. LinkedIn's terms limit automated use"),
    "crm": ("A CRM system", "website", "in your own browser, where you are signed in"),
}


@dataclass
class Found:
    available: bool
    missing: str = ""


class Capabilities:
    def __init__(self, nabh_factory=None, vault=None, binary=None, google_ready=None, github_ready=None, mac=None, clock=time.monotonic):
        self._nabh = nabh_factory
        self.vault = vault
        self.binary = binary or hands_module.BINARY
        self.google_ready = google_ready
        self.github_ready = github_ready
        self.mac = mac
        self.clock = clock
        self.figma = None  # whether the XENIO plugin is open in Figma, set by the service
        self._cache: tuple[float, list[dict]] | None = None

    def _mac(self):
        if self.mac is not None:
            return self.mac
        if sys.platform != "darwin":
            return None
        from app.desktop import macos

        return macos

    def _ask(self, name: str) -> Found:
        if name in ("thinking", "vision", "images"):
            try:
                ok = self._nabh is not None and self._nabh() is not None
            except Exception:  # noqa: BLE001
                ok = False
            return Found(ok, "" if ok else "a Nabh key (XENIO_NABH_API_KEY in .env)")
        if name == "web_search":
            return Found(True)
        if name == "browser":
            ok = self.binary.is_file() or (shutil.which("swiftc") is not None and hands_module.BUILD.is_file())
            return Found(ok, "" if ok else "native/hands/build.sh, which needs Apple's command line tools")
        if name == "signed_in_sites":  # your own browser is already signed in: the vault matters only if a page asks, and the agent says so then
            by_screen = self._ask("desktop_control")
            if by_screen.available:
                return by_screen
            probe = getattr(self._mac(), "safari_scripting", None)  # Safari's own scripting needs no Accessibility, and reaches every desktop
            if probe is None:
                return by_screen
            try:
                missing = str(probe())
            except Exception:  # noqa: BLE001
                missing = "Safari answering XENIO"
            return Found(True) if not missing else Found(False, f"{by_screen.missing}, or {missing}")
        if name in ("desktop_control", "screen_reading"):
            mac = self._mac()
            if mac is None:
                return Found(False, "a Mac")
            try:
                trusted = bool(mac.trusted())
            except Exception:  # noqa: BLE001
                trusted = False
            if not trusted:
                return Found(False, "Accessibility permission, in System Settings, Privacy & Security, for the app XENIO runs in")
            if name == "screen_reading":
                try:
                    seen = bool(mac.screen_access())
                except Exception:  # noqa: BLE001
                    seen = False
                if not seen:
                    return Found(False, "Screen Recording permission, in System Settings, Privacy & Security, for the app XENIO runs in")
            return Found(True)
        if name == "code":
            ok = sandbox.available()
            return Found(ok, "" if ok else "macOS's sandbox-exec, which is what keeps agents' commands contained")
        if name in ("documents", "spreadsheets", "presentations", "design"):
            return Found(True)
        if name in ("gmail", "calendar"):
            try:
                ok = bool(self.google_ready and self.google_ready())
            except Exception:  # noqa: BLE001
                ok = False
            return Found(ok, "" if ok else "your Google sign-in, under Settings")
        if name == "figma":  # never assumed: a real job took a Figma design, spent 330,000 tokens on notes, and never touched the file
            try:
                live = bool(self.figma and self.figma())
            except Exception:  # noqa: BLE001
                live = False
            return Found(True) if live else Found(False, "the XENIO plugin running in your Figma file: in the Figma app, open the file, then Plugins, Development, XENIO (the first time: Plugins, Development, Import plugin from manifest, and pick native/figma-plugin/manifest.json in XENIO's folder), and paste the pairing code from the War Room's Connectors")
        if name == "drive":
            return Found(False, "a Drive connector, which XENIO does not have yet")
        if name == "github":
            try:
                ok = bool(self.github_ready and self.github_ready())
            except Exception:  # noqa: BLE001
                ok = False
            return Found(ok, "" if ok else "signing in with gh auth login, or a GitHub token in .env")
        if name in ("linkedin", "crm"):
            return self._ask("signed_in_sites")
        return Found(False, "XENIO does not know this capability")

    def report(self, fresh: bool = False) -> list[dict]:
        if not fresh and self._cache is not None and self.clock() - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]
        out = []
        for name, (title, kind, how) in CATALOG.items():
            found = self._ask(name)
            out.append({"id": name, "name": title, "kind": kind, "how": how, "available": found.available, "missing": found.missing})
        self._cache = (self.clock(), out)
        return out

    def check(self, wanted: list[str]) -> list[dict]:
        """Of what this job wants, what is not there. A name XENIO does not know is not there either: it is reported, and never assumed."""
        known = {c["id"]: c for c in self.report()}
        gaps = []
        for name in dict.fromkeys(str(w) for w in wanted or []):
            item = known.get(name)
            if item is None:
                gaps.append({"id": name, "name": name, "missing": "XENIO has no way to do this yet"})
            elif not item["available"]:
                gaps.append({"id": name, "name": item["name"], "missing": item["missing"]})
        return gaps
