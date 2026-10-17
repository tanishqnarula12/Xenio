"""The macOS counterpart of ``uia.UIAClient`` (V5.5): what is on screen, by name, through the Accessibility API
(the data VoiceOver uses). Same three questions, same answers:

* ``find(window, query)``: the controls of one window with names, types, positions and a match score;
* ``at(x, y)``: the control a click there would hit, with its parents' names (the policy judges a click by it);
* ``focused()``: the focused control: is it a password field (never typed into), what text does it hold (to verify
  typing).

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
