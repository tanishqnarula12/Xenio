"""A window's text, read from its accessibility tree (V3): what ``xenio_gui`` action ``read`` returns, and where the
code editor is in it (``editor=true``).

Why: a description of a screenshot loses exactly the details a coding problem depends on (measured on GeeksforGeeks:
the vision model read the constraint "1<=n<=10^5" as 10^6), and problem pages often refuse copying. The tree that
VoiceOver reads has the page's exact text - also the parts scrolled out of view - with superscripts and subscripts
marked, in about 0.1 s, without the clipboard and without a screenshot.

Pure functions over a snapshot of the tree (nested dicts built by ``app.desktop.macos``), so they are tested without a
Mac. A node: ``role``, ``sub`` (subrole), ``text`` (AXValue when it is text), ``name`` (title or description),
``rect`` [x, y, w, h], ``cls`` (the web element's CSS classes), ``focused``, ``kids``.
"""

from __future__ import annotations

import re

SUP, SUB, PRE = "AXSuperscriptStyleGroup", "AXSubscriptStyleGroup", "AXPreformattedStyleGroup"
_INPUTS = {"AXTextArea", "AXTextField", "AXComboBox"}
_BUTTONS = {"AXButton", "AXPopUpButton", "AXMenuButton", "AXCheckBox", "AXRadioButton"}
_SKIP = {"AXImage", "AXScrollBar", "AXMenu", "AXMenuBar", "AXValueIndicator", "AXSplitter"}
# code editors in web pages and Electron apps: their root, their (often 1-pixel) input box, the scrolling text area
# a click puts the caret in, and one rendered line
_EDITOR_ROOTS = {"ace_editor": "Ace", "monaco-editor": "Monaco", "cm-editor": "CodeMirror", "CodeMirror": "CodeMirror"}
_EDITOR_INPUTS = {"ace_text-input": "Ace", "inputarea": "Monaco", "cm-content": "CodeMirror"}
_SCROLLERS = {"ace_scroller", "lines-content", "cm-scroller", "CodeMirror-scroll"}
_CODE_LINES = {"ace_line", "view-line", "cm-line", "CodeMirror-line"}
MIN_TEXT_AREA = 150 * 60  # a plain text area at least this big (square points) counts as an editor
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")  # GeeksforGeeks ends each output block with one


def _cls(node: dict) -> set[str]:
    value = node.get("cls")
    return set(value) if isinstance(value, list) else set()


def _rect(node: dict) -> tuple[float, float, float, float] | None:
    r = node.get("rect")
    return tuple(r) if isinstance(r, (list, tuple)) and len(r) == 4 else None


def _outside(rect, bounds) -> bool:
    """Entirely left or right of the window: a closed drawer or an off-canvas menu, never seen. (Content above or
    below the view is kept: it is the page, scrolled.)"""
    if rect is None or bounds is None or rect[2] <= 0:
        return False
    return rect[0] >= bounds[0] + bounds[2] or rect[0] + rect[2] <= bounds[0]


def _tiny(rect) -> bool:
    return rect is not None and (rect[2] < 8 or rect[3] < 8)


class _Writer:
    """Turns runs of text into lines: runs inside one block are joined (with a space where the layout shows a gap or
    the line wraps), a new block below the last one starts a new line, and preformatted text keeps its lines."""

    def __init__(self):
        self.lines: list[str] = []
        self.cur = ""
        self.last = None
        self.boundary = False
        self.prefix = ""
        self.cw = None  # the code editors' character width

    def block(self) -> None:
        self.boundary = True

    def newline(self) -> None:
        if self.cur.strip():
            self.lines.append(self.cur.rstrip())
        self.cur, self.last = "", None

    def line(self, text: str) -> None:
        self.newline()
        self.lines.append(text.rstrip())
        self.boundary = True

    def run(self, text: str, rect, pre: bool = False) -> None:
        text = _ZERO_WIDTH.sub("", text.replace("\xa0", " "))
        if pre and "\n" in text:
            for i, part in enumerate(text.split("\n")):
                if i:
                    self.newline()
                self.run(part, None if i else rect, pre)
            self.last = rect
            return
        if not text.strip():
            if self.cur and not self.cur.endswith(" "):
                self.cur += " "
            return
        if self.prefix:  # ^ or _ glued to what it belongs to: 10^5, x_i
            text, self.prefix = self.prefix + text.strip(), ""
            self.cur += text
            if rect and self.last:  # its width, but the line's height: a raised or lowered digit is not a new line
                rect = (rect[0], self.last[1], rect[2], self.last[3])
            self.last, self.boundary = rect or self.last, False
            return
        if self.cur and self.last and rect:
            lx, ly, lw, lh = self.last
            # below the last run's last line (lines may overlap: measured 20-point runs 17 points apart)
            below = rect[1] >= ly + lh - min(lh, rect[3]) / 2
            beside = not below and rect[1] + rect[3] > ly + 2 and rect[0] >= lx + lw - 2  # same line, to the right
            if (self.boundary and not beside) or (below and pre):
                gap = rect[1] - (ly + lh)
                self.newline()
                if self.boundary and (gap > max(8, 0.6 * min(lh, rect[3])) or rect[1] < ly - 2) \
                        and self.lines and self.lines[-1]:
                    self.lines.append("")  # a gap, or a jump back up the page: a new paragraph or region
            elif (below or self.boundary or rect[0] - (lx + lw) > 1.5) \
                    and not self.cur.endswith(" ") and not text.startswith(" "):
                self.cur += " "
        elif self.cur and self.boundary and not self.cur.endswith(" "):
            self.cur += " "
        self.cur += text if self.cur else text.lstrip()
        self.last, self.boundary = rect or self.last, False


def _runs(node: dict, out: list) -> list:
    for kid in node.get("kids") or []:
        if kid.get("role") == "AXStaticText" and (kid.get("text") or kid.get("name")):
            out.append(kid)
        else:
            _runs(kid, out)
    return out


def char_width(tree: dict) -> float | None:
    """The width of one character in the page's code editors (monospaced), from their longest token: the widths of
    short tokens are rounded up (measured: "(" 9 points in a 7.25-point font), which turned 8 spaces into 7."""
    best, stack = None, [tree]
    while stack:
        node = stack.pop()
        if _cls(node) & _CODE_LINES:
            for r in _runs(node, []):
                text, rect = r.get("text") or r.get("name") or "", _rect(r)
                if rect and rect[2] > 0 and (best is None or len(text) > best[0]):
                    best = (len(text), rect[2] / len(text))
            continue
        stack.extend(node.get("kids") or [])
    return best[1] if best else None


def code_line(node: dict, cw: float | None = None) -> str:
    """One rendered line of a code editor, with its indentation and spacing rebuilt from where each token sits (the
    editor draws spaces as gaps, so they are not in the tree). ``cw``: the editor's character width."""
    runs = [r for r in _runs(node, []) if _rect(r)]
    if not runs:
        return ""
    if not cw:
        cw = char_width(node) or 7.0
    x = (_rect(node) or _rect(runs[0]))[0]
    out = ""
    for r in runs:
        text = (r.get("text") or r.get("name") or "").replace("\xa0", " ")
        rx, _, rw, _ = _rect(r)
        out += " " * max(0, round((rx - x) / cw)) + text
        x = rx + rw
    return out


def _walk(node: dict, w: _Writer, bounds, pre: bool = False) -> None:
    role, sub, rect, cls = node.get("role") or "", node.get("sub") or "", _rect(node), _cls(node)
    if _outside(rect, bounds) or sub == "AXSecureTextField" or role in _SKIP:
        return
    if cls & _CODE_LINES:
        w.line(code_line(node, w.cw))
        return
    if role == "AXStaticText":
        text = node.get("text") or node.get("name") or ""
        if isinstance(text, str) and text:
            w.run(text, rect, pre)
        return
    kids = node.get("kids") or []
    if role in _INPUTS:
        if _tiny(rect):
            return  # an editor's hidden input box: the editor's lines are read instead
        value = node.get("text")
        if role == "AXTextArea" and isinstance(value, str) and value.strip() and cls.isdisjoint(_EDITOR_INPUTS):
            w.block()  # a text area's own value has its exact lines
            for line in _ZERO_WIDTH.sub("", value).strip("\n").split("\n"):
                w.line(line)
            w.block()
            return
        if not _runs(node, []):
            value = node.get("text")
            if isinstance(value, str) and value.strip():
                w.block()
                if "\n" in value.strip():
                    for line in value.strip("\n").split("\n"):
                        w.line(line)
                else:
                    w.run(f"[{node.get('name') or 'text field'}: {value.strip()}]", rect)
                w.block()
            return
    if role in _BUTTONS and node.get("name"):
        w.block()
        w.run(f"[{' '.join(str(node['name']).split())}]", rect)
        w.block()
        return
    if role == "AXLink" and not kids and node.get("name"):
        w.run(str(node["name"]), rect, pre)
        return
    inline = role == "AXLink" or (sub.endswith("StyleGroup") and sub != PRE) or any("MathJax" in c or "katex" in c
                                                                                    for c in cls)
    if sub == SUP:
        w.prefix = "^"
    elif sub == SUB:
        w.prefix = "_"
    if not inline:
        w.block()
    for kid in kids:
        _walk(kid, w, bounds, pre or sub == PRE)
    if sub in (SUP, SUB):
        w.prefix = ""
    if not inline:
        w.block()


def document(tree: dict) -> dict:
    """The page itself when the window shows one (the largest web area: a browser's toolbar and tab names are not
    the page), else the whole window."""
    best, best_area = None, -1.0
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("role") == "AXWebArea":
            r = _rect(node)
            area = r[2] * r[3] if r else 0.0
            if area > best_area:
                best, best_area = node, area
            continue
        stack.extend(node.get("kids") or [])
    return best or tree


def render(node: dict, bounds=None) -> str:
    w = _Writer()
    w.cw = char_width(node)
    _walk(node, w, bounds if bounds is not None else _rect(node))
    w.newline()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(w.lines)).strip("\n")


def editors(tree: dict, bounds=None) -> list[dict]:
    """The code editors in the window, best first: a known editor (Ace, Monaco, CodeMirror) before a plain text area,
    the focused one first, then the biggest. Each: kind, focused, rect, click (a point in its text area), lines (the
    rendered lines), text (what it shows), and ``node`` (its input box, which the caller focuses).

    An editor is its input box (often 1 pixel, at the caret) plus the area that shows its lines. WebKit leaves plain
    boxes out of the tree, so the editor's own box may be missing (measured: present on GeeksforGeeks, absent on a
    test page with the same Ace): an input box outside any editor box belongs to the text area that contains it."""
    bounds = bounds if bounds is not None else _rect(tree)
    cw = char_width(tree)
    areas: list[dict] = []
    inputs: list[tuple] = []

    def new_area(kind, rect) -> dict:
        area = {"kind": kind, "rect": rect, "scroller": None, "lines": []}
        areas.append(area)
        return area

    def visit(node: dict, area: dict | None) -> None:
        cls, rect = _cls(node), _rect(node)
        kind = next((k for c, k in _EDITOR_ROOTS.items() if c in cls), None)
        if kind and area is None:
            area = new_area(kind, rect)
        if cls & _SCROLLERS:
            if area is None:
                area = new_area(None, rect)
            if area["scroller"] is None:
                area["scroller"] = rect
        if cls & _CODE_LINES and area is not None:
            area["lines"].append(code_line(node, cw))
            return
        role, sub = node.get("role") or "", node.get("sub") or ""
        if role in ("AXTextArea", "AXTextField") and sub != "AXSecureTextField" and not _outside(rect, bounds):
            inputs.append((next((k for c, k in _EDITOR_INPUTS.items() if c in cls), None), node, area))
        kids = node.get("kids") or []
        if area is None and any(_cls(k) & _CODE_LINES for k in kids):
            area = new_area(None, rect)
        for kid in kids:
            visit(kid, area)

    def containing(rect) -> dict | None:
        best = None
        for a in areas:
            r = a["scroller"] or a["rect"]
            if r and rect and r[0] - 2 <= rect[0] <= r[0] + r[2] + 2 and r[1] - 2 <= rect[1] <= r[1] + r[3] + 2 \
                    and (best is None or r[2] * r[3] < (best["scroller"] or best["rect"])[2] * (best["scroller"] or best["rect"])[3]):
                best = a
        return best

    visit(tree, None)
    out, seen = [], set()
    for known, node, area in inputs:
        area = area or (containing(_rect(node)) if known else None)
        kind = known or (area or {}).get("kind")
        rect = _rect(node)
        if not kind:
            if node.get("role") != "AXTextArea" or area is not None or not rect or rect[2] * rect[3] < MIN_TEXT_AREA:
                continue
            kind = "text area"
        if area is not None:
            if id(area) in seen:
                continue  # one editor, several input boxes (CodeMirror exposes more than one)
            seen.add(id(area))
            rect = area["rect"] or area["scroller"] or rect
        if rect is None or rect[2] <= 0 or rect[3] <= 0 or _outside(rect, bounds):
            continue
        view = (area or {}).get("scroller") or rect
        click = [int(view[0] + min(60, view[2] / 2)), int(view[1] + min(10, view[3] / 2))]
        lines = (area or {}).get("lines") or []
        text = "\n".join(lines) if lines else (node.get("text") if kind == "text area" and isinstance(node.get("text"), str) else "")
        out.append({"kind": kind, "focused": bool(node.get("focused")), "rect": [int(v) for v in rect],
                    "click": click, "lines": len(lines) or len(text.splitlines()), "text": text, "node": node})
    out.sort(key=lambda e: (e["kind"] == "text area", not e["focused"], -(e["rect"][2] * e["rect"][3])))
    return out
