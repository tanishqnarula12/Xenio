"""The macOS counterpart of ``uia.UIAClient`` (V5.5): what is on screen, by name, through the Accessibility API
(the data VoiceOver uses). Same three questions, same answers:

* ``find(window, query)``: the controls of one window with names, types, positions and a match score;
* ``at(x, y)``: the control a click there would hit, with its parents' names (the policy judges a click by it);
* ``focused()``: the focused control: is it a password field (never typed into), what text does it hold (to verify
  typing);
* ``read_text(window)`` and ``editor(window)`` (V3, macOS only): the window's exact text, and its code editor
  (app.desktop.pagetext).

Control types use the same names as on Windows (button, edit, listitem, menuitem ...), so the safety policy needs no
macOS-specific rules for them. The lookups run in XENIO's warm accessibility helper (``macos.helper()``).
"""

from __future__ import annotations

from app.desktop import macos
from app.desktop.common import UIAError


class AXClient:
    def __init__(self, timeout: float = 6.0, helper=None):
        self.timeout = timeout
        self._helper = helper

    def _call(self, cmd: str, timeout: float | None = None, **args) -> dict:
        try:
            out = (self._helper or macos.helper()).call(cmd, timeout=timeout or self.timeout, **args)
        except macos.MacError as exc:
            raise UIAError(str(exc)) from None
        if not isinstance(out, dict):
            raise UIAError("the accessibility helper gave no answer")
        return out

    def at(self, x: int, y: int) -> dict:
        return self._call("at", x=int(x), y=int(y))

    def focused(self, with_value: bool = False) -> dict:
        return self._call("focused", with_value=bool(with_value))

    def find(self, hwnd: int, query: str, limit: int = 8, max_nodes: int = 2500, budget: float = 2.5) -> dict:
        return self._call("find", timeout=budget + 4, id=int(hwnd), query=query or "", limit=int(limit),
                          max_nodes=int(max_nodes), budget=float(budget))

    def read_text(self, hwnd: int, max_chars: int = 60000) -> dict:
        """The window's text (a web page: the page only, also what is scrolled out of view) and its code editors."""
        return self._call("text", timeout=10, id=int(hwnd), max_chars=int(max_chars))

    def editor(self, hwnd: int, focus: bool = True) -> dict:
        """The window's code editor (Ace, Monaco, CodeMirror or a big text area), focused through accessibility."""
        return self._call("editor", timeout=10, id=int(hwnd), focus=bool(focus))

    def press(self, hwnd: int, name: str) -> dict:
        """Press the button named exactly ``name`` through accessibility (wherever it is on the page)."""
        return self._call("press", timeout=8, id=int(hwnd), name=str(name))

    # ---- forms (app.autofill) --------------------------------------------------------------------------------------

    def form_scan(self, hwnd: int) -> dict:
        return self._call("form_scan", timeout=8, id=int(hwnd))

    def form_read(self, token: int, index: int) -> dict:
        return self._call("form_read", timeout=3, token=int(token), index=int(index))

    def form_set(self, token: int, index: int, text: str, blur: bool = False) -> dict:
        return self._call("form_set", timeout=4, token=int(token), index=int(index), text=str(text), blur=bool(blur))

    def form_press(self, token: int, index: int) -> dict:
        return self._call("form_press", timeout=4, token=int(token), index=int(index))

    def form_type_ahead(self, token: int, index: int, text: str) -> dict:
        return self._call("form_type_ahead", timeout=4, token=int(token), index=int(index), text=str(text))

    def form_date(self, token: int, index: int, year: int, month: int, day: int) -> dict:
        return self._call("form_date", timeout=5, token=int(token), index=int(index), year=int(year), month=int(month), day=int(day))

    def form_point(self, token: int, index: int) -> dict:
        return self._call("form_point", timeout=3, token=int(token), index=int(index))

    def form_space(self, token: int, index: int) -> dict:
        return self._call("form_space", timeout=3, token=int(token), index=int(index))

    def form_list_point(self, token: int, index: int) -> dict:
        return self._call("form_list_point", timeout=3, token=int(token), index=int(index))

    def form_list_pick(self, token: int, index: int, values: list[str]) -> dict:
        return self._call("form_list_pick", timeout=4, token=int(token), index=int(index), values=list(values))

    def form_list_value(self, token: int, index: int) -> dict:
        return self._call("form_list_value", timeout=3, token=int(token), index=int(index))

    def form_is_focused(self, token: int, index: int) -> dict:
        return self._call("form_is_focused", timeout=2, token=int(token), index=int(index))

    def browser_tabs(self, hwnd: int) -> dict:
        return self._call("browser_tabs", timeout=4, id=int(hwnd))

    def browser_tab(self, hwnd: int, name: str) -> dict:
        return self._call("browser_tab", timeout=4, id=int(hwnd), name=str(name))

    def browser_button(self, hwnd: int, action: str) -> dict:
        return self._call("browser_button", timeout=4, id=int(hwnd), action=str(action))
