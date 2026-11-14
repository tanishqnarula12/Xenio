"""Agents in the CEO's own browser, by whichever route reaches it.

Safari's own scripting reaches every window on every desktop and works in the background, so it comes first whenever Safari
allows it. Otherwise the screen-reader route (BrowserWork), which reaches only windows on the desktop the CEO is looking at.
Each agent keeps the route its window was found by.
"""

from __future__ import annotations

import time

from app.desktop.browser import BrowserWork
from app.desktop.safari_script import JS_OFF, SafariScript
from app.desktop.service import DesktopError

RECHECK_JS = 60.0  # seconds: after the CEO turns Safari's switch on, the next page works without a restart


class OwnBrowser:
    def __init__(self, desktop, script: SafariScript | None = None, ax: BrowserWork | None = None, clock=time.monotonic):
        self.desktop = desktop
        self.ax = ax if ax is not None else BrowserWork(desktop, strict=True)
        self.script = script if script is not None else SafariScript()
        self._clock = clock
        self._mode: dict[str, object] = {}  # session -> the route its window was found by
        self._js_off_at: float | None = None

    @property
    def _by_session(self) -> dict:
        return {**getattr(self.ax, "_by_session", {}), **self.script._by_session}

    def _safari(self) -> bool:
        try:
            return self.ax._which_browser().lower() == "safari"
        except Exception:  # noqa: BLE001 - nothing could be listed: Safari is the Mac's own
            return True

    def _try_script(self) -> bool:
        return self._safari() and (self._js_off_at is None or self._clock() - self._js_off_at > RECHECK_JS)

    def _route(self, session: str):
        route = self._mode.get(session)
        if route is None:  # never "whatever is in front": on a real test that was the CEO's own YouTube
            raise DesktopError("you have no window of your own yet: open the address with page_open, or name one of the CEO's tabs with page_use_tab")
        return route

    def scripted(self, session: str) -> bool:
        return self._mode.get(session) is self.script

    def open(self, url: str, session: str = "") -> dict:
        if self._try_script():
            try:
                view = self.script.open(url, session)
                self._js_off_at = None
                self._mode[session] = self.script
                return view
            except DesktopError as exc:
                if not getattr(exc, "js_off", False):
                    raise
                self._js_off_at = self._clock()  # its window was closed again; the screen-reader route may still reach one here
        try:
            view = self.ax.open(url, session)
        except DesktopError as exc:
            if self._js_off_at is not None and "another desktop" in str(exc):
                raise DesktopError(f"Safari's windows are on another desktop, which only Safari's own scripting reaches. {JS_OFF}", "NEEDS_PERMISSION") from None
            raise
        self._mode[session] = self.ax
        return view

    def tabs(self) -> dict:
        if self._safari():  # every desktop, and nothing inside a page is read
            listing = self.script.tabs()
            return {"windows": listing["windows"]}
        return self.ax.tabs()

    def tab_titles(self) -> list[str]:
        return [t for w in self.tabs().get("windows") or [] for t in w.get("tabs") or []]

    def adopt(self, session: str, window: str):
        if self._try_script():
            where = self.script.adopt(session, window)
            try:
                self.script.page({}, session)
                self._mode[session] = self.script
                self._js_off_at = None
                return where
            except DesktopError as exc:
                self.script._by_session.pop(session, None)
                if not getattr(exc, "js_off", False):
                    raise
                self._js_off_at = self._clock()
        try:
            target = self.ax.adopt(session, window)
        except DesktopError as exc:
            if self._js_off_at is not None:
                raise DesktopError(f"{exc}. If that tab is on another desktop, only Safari's own scripting reaches it. {JS_OFF}", "NEEDS_PERMISSION") from None
            raise
        self._mode[session] = self.ax
        return target

    def switch(self, args: dict, session: str = "") -> dict:
        return self._route(session).switch(args, session)

    def page(self, args: dict | None = None, session: str = "") -> dict:
        return self._route(session).page(args or {}, session)

    def click(self, args: dict, session: str = "") -> dict:
        return self._route(session).click(args, session)

    def type_text(self, args: dict, session: str = "") -> dict:
        return self._route(session).type_text(args, session)

    def navigate(self, how: str, args: dict | None = None, session: str = "") -> dict:
        return self._route(session).navigate(how, args or {}, session)

    def pick(self, args: dict | None = None, session: str = ""):
        return self._route(session).pick(args or {}, session)

    def close(self, args: dict | None = None, session: str = "") -> dict:
        route = self._mode.pop(session, None)
        return route.close(args or {}, session) if route is not None else {"closed": False}
