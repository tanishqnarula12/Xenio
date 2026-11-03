"""A form as XENIO understands it (V4 phase 2): from the controls of a window to named fields. Pure, so it can be tested on
what Safari really reports.

Measured on a real page (Safari's accessibility tree): a <label>, aria-label or the placeholder name a control, the browser
names its type ("email field", "telephone number field", "secure text field"), a <fieldset>'s legend names its group.

A page can try to collect details through fields a person cannot see. ``display: none`` never reaches the tree, but a field
pushed off the page (``left: -9999px``), a tiny one, or one inside a box with no height is reported like any other (measured:
Safari gives that last one its full size, on top of the controls below it). Those are marked ``hidden`` and are never filled.
A field hidden by transparency alone cannot be told from a visible one this way: the review lists every field filled, so it
can still be seen there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field

from app.autofill.matching import is_placeholder_option, split_identifier

MIN_TEXT_SIZE = 8  # points: smaller than this no one can read or use a text field
_SEARCH = re.compile(r"\b(search|find|query|filter|jump to)\b", re.IGNORECASE)
_NEXT = re.compile(r"^(next|continue|proceed|next step|next page|save and continue|save & continue|forward|go to next|aage badhe\w*)\b"
                   r"|→$|^›$|^>$", re.IGNORECASE)
_BACK = re.compile(r"^(back|previous|prev|go back|cancel|reset|clear|close|skip)\b", re.IGNORECASE)
_SUBMIT = re.compile(r"\b(submit|apply|send|register|sign up|signup|enrol|enroll|join|book|reserve|finish|complete|confirm|pay"
                     r"|place order|checkout|check out|create account|save|done|request|get started|subscribe)\b", re.IGNORECASE)
_LOGIN = re.compile(r"\b(sign in|log in|login|signin)\b", re.IGNORECASE)
_HUMAN_CHECK = re.compile(r"\b(i'?m not a robot|verify (?:that )?you(?:'re| are) (?:a )?human|recaptcha|hcaptcha|captcha|are you a robot"
                          r"|security check|cloudflare|press and hold)\b", re.IGNORECASE)
_NOT_A_FIELD = re.compile(r"^(help|feedback|help and feedback|more options?|menu|settings|language|account)\b", re.IGNORECASE)
_OTHER_BOX = re.compile(r"^other( response| option| please specify)?\b", re.IGNORECASE)
_REQUIRED_WORDS = re.compile(r"[\s:*]*\b(required question|required field|required|mandatory)\b[\s:*.]*$", re.IGNORECASE)
_GRID_ROW = re.compile(r",\s*response for (.*)$", re.IGNORECASE)
_GRID_OPTION = re.compile(r",\s*response for .*$", re.IGNORECASE)  # "Never, response for Facebook" in a Google Forms grid
_CHROME_BUTTONS = re.compile(r"^(close|minimi[sz]e|zoom|full ?screen)\b", re.IGNORECASE)


@dataclass
class Field:
    id: str
    label: str
    kind: str  # text | email | tel | url | number | textarea | select | combobox | listbox | checkbox | checkboxes | radio | date | password | file
    index: int  # the control in the helper; for a radio group, of its first button
    required: bool = False
    placeholder: str = ""
    section: str = ""
    group: str = ""
    secure: bool = False
    enabled: bool = True
    hidden: bool = False
    invalid: bool = False
    current: str = ""  # what it holds now: never in a tool result
    options: list[str] = dataclass_field(default_factory=list)  # a radio group's or tick boxes' choices
    option_indexes: list[int] = dataclass_field(default_factory=list)
    ticked: list[str] = dataclass_field(default_factory=list)  # tick boxes that are on now: never in a tool result
    other_box: int = -1  # the text box of an "Other" choice

    @property
    def has_value(self) -> bool:
        if self.kind in ("select", "combobox", "listbox"):
            return not is_placeholder_option(self.current)
        if self.kind == "checkboxes":
            return bool(self.ticked)
        if self.kind == "checkbox":
            return self.current in ("1", "1.0", "True")
        return bool(str(self.current or "").strip())


@dataclass
class Form:
    fields: list[Field]
    buttons: list[dict]  # {"name", "index", "kind": next | submit | back | other}
    token: int = 0
    pid: int = 0
    heading: str = ""
    text: str = ""  # the page's words, in document order: judged for tests and human checks, never in a result
    complete: bool = True
    human_check: bool = False
    needs_login: bool = False
    ms: int = 0
    link_nodes: list[dict] = dataclass_field(default_factory=list)  # {"name", "index"}
    plain_spots: list = dataclass_field(default_factory=list)  # rects of text that is no control, in view
    url: str = ""
    links: list[str] = dataclass_field(default_factory=list)


def _kind(node: dict) -> str:
    role, what = node.get("role"), str(node.get("what") or "").lower()
    if node.get("secure") or "secure" in what:
        return "password"
    if role == "AXTextArea":
        return "textarea"
    if role == "AXPopUpButton":
        return "select"
    if role == "AXListBox":
        return "listbox"
    if role == "AXComboBox":
        return "combobox"
    if role == "AXCheckBox":
        return "checkbox"
    if role == "AXRadioButton":
        return "radio"
    if role in ("AXDateTimeArea", "AXDateField"):
        return "date"
    if "email" in what:
        return "email"
    if "telephone" in what or "phone" in what:
        return "tel"
    if "url" in what or "web address" in what:
        return "url"
    if "number" in what:
        return "number"
    if "search" in what or role == "AXSearchField":
        return "search"
    return "text"


def _off_the_page(rect, web_area) -> bool:
    """Above or below the view is ordinary scrolling. Off to the side, or no size at all, is out of sight."""
    if not rect:
        return False
    x, _, w, h = rect
    if w <= 0 or h <= 0:
        return True
    if web_area:
        return x + w <= web_area[0] or x >= web_area[0] + web_area[2]
    return x + w <= 0


def _covered(rect, others: list) -> bool:
    """Other controls take up a good part of this one: a person could not use it, so it is not really there. A button inside
    a field (show, clear) takes up only a small part."""
    if not rect or rect[2] <= 0 or rect[3] <= 0:
        return False
    x, y, w, h = rect
    shared = 0.0
    for other in others:
        if not other or other is rect:
            continue
        ox, oy, ow, oh = other
        shared += max(0.0, min(x + w, ox + ow) - max(x, ox)) * max(0.0, min(y + h, oy + oh) - max(y, oy))
    return shared > 0.3 * w * h


def _name_from_text(text: str) -> str:
    plain = _REQUIRED_WORDS.sub("", " ".join(str(text or "").split()))
    return re.sub(r"\s*[:*]+\s*$", "", plain).strip()


def _says_required(text: str) -> bool:
    return bool(_REQUIRED_WORDS.search(" ".join(str(text or "").split()))) or str(text or "").rstrip().endswith("*")


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def build(scan: dict) -> Form:
    """Named fields from one ``form_scan``."""
    nodes = scan.get("nodes") or []
    web_area = scan.get("web_area")
    control_rects = [n.get("rect") for n in nodes if n.get("role") not in ("AXStaticText", "AXHeading") and not n.get("hidden")]
    section, last_text, last_text_depth = "", "", 0
    question_text = ""
    run: dict | None = None
    fields: list[Field] = []
    buttons: list[dict] = []
    words: list[str] = []
    link_nodes: list[dict] = []
    plain_spots: list = []
    heading = ""
    for node in nodes:
        role = node.get("role")
        if role in ("AXStaticText", "AXHeading"):
            if node.get("hidden"):
                continue
            text = str(node.get("text") or "")
            words.append(text)
            spot = node.get("rect")
            if node.get("plain") and spot and web_area and spot[2] > 4 and spot[3] > 4 \
                    and web_area[1] + 4 < spot[1] and spot[1] + spot[3] < web_area[1] + web_area[3] - 4:
                plain_spots.append(spot)
            if run is not None and text.strip().lower() == run.get("last"):
                continue  # an option's own label, next to it
            if role == "AXHeading":
                section = _name_from_text(text)
                heading = heading or section
                question_text = text
                run = None
            elif run is None:
                question_text = text
            last_text, last_text_depth = text, node.get("depth") or 0
            continue
        name = str(node.get("title") or node.get("desc") or "").strip()
        if role in ("AXButton", "AXLink"):
            run = None
            if name and not node.get("hidden") and not _off_the_page(node.get("rect"), web_area) and node.get("enabled", True):
                words.append(f"[{name}]" if role == "AXButton" else name)
                if role == "AXLink":
                    link_nodes.append({"name": " ".join(name.split()), "index": node["i"]})
                if role == "AXButton" and not _CHROME_BUTTONS.match(name):
                    kind = "next" if _NEXT.search(name) else "back" if _BACK.match(name) else \
                        "submit" if _SUBMIT.search(name) else "other"
                    buttons.append({"name": name, "index": node["i"], "kind": kind})
            continue
        kind = _kind(node)
        placeholder = str(node.get("placeholder") or "")
        small = node.get("rect") and kind not in ("checkbox", "radio") and \
            (node["rect"][2] < MIN_TEXT_SIZE or node["rect"][3] < MIN_TEXT_SIZE)
        hidden = bool(node.get("hidden")) or _off_the_page(node.get("rect"), web_area) or bool(small) or \
            (kind not in ("checkbox", "radio") and _covered(node.get("rect"), control_rects))
        if kind in ("radio", "checkbox"):
            option = _GRID_OPTION.sub("", " ".join(name.split()))
            row = _GRID_ROW.search(name)
            # a grid's tick boxes: each row is its own question ("Japan, response for Beaching"), under the grid's question
            group_name = row.group(1).strip() if row and kind == "checkbox" else str(node.get("group") or "")
            joins = run is not None and run["field"].kind == kind and (
                (group_name and run["group"] == group_name)
                or (not group_name and not run["group"] and abs((node.get("depth") or 0) - run["depth"]) <= 1))
            if not joins:
                question = group_name or (question_text if kind == "radio" or question_text else "")
                field = Field(id=f"f{len(fields) + 1}", label=_name_from_text(question), kind=kind, index=node["i"],
                              required=bool(node.get("required")) or _says_required(question), section=section, group=group_name,
                              enabled=bool(node.get("enabled", True)), hidden=hidden)
                fields.append(field)
                run = {"field": field, "group": group_name, "depth": node.get("depth") or 0}
            field = run["field"]
            field.options.append(option or "Other")  # a choice with no name of its own is Google Forms' "Other"
            field.option_indexes.append(node["i"])
            run["last"] = option.lower()
            if _stringify(node.get("value")) in ("1", "True"):
                field.ticked.append(option or "Other")
                field.current = option or "Other"
            continue
        if run is not None and _OTHER_BOX.match(name or placeholder) and "Other" in run["field"].options:
            run["field"].other_box = node["i"]  # it belongs to that choice: not a question of its own
            continue
        run = None
        if kind == "select" and _NOT_A_FIELD.match(name):
            continue
        label = name or placeholder or (_name_from_text(last_text) if last_text and (node.get("depth") or 0) <= last_text_depth + 3 else "") \
            or split_identifier(node.get("dom_id") or "")
        if kind == "text" and _SEARCH.search(f"{label} {placeholder}") and not node.get("required"):
            kind = "search"  # no form question: browser work can type in it, autofill leaves it alone
        fields.append(Field(id=f"f{len(fields) + 1}", label=_name_from_text(label), kind=kind, index=node["i"],
                            required=bool(node.get("required")) or _says_required(label), placeholder=placeholder, section=section,
                            group=str(node.get("group") or ""), secure=bool(node.get("secure")),
                            enabled=bool(node.get("enabled", True)), hidden=hidden, invalid=bool(node.get("invalid")),
                            current=_stringify(node.get("value")), options=[o for o in node.get("options") or [] if not is_placeholder_option(o)]))
    for field in fields:
        if field.kind == "checkbox":
            if len(field.options) > 1:
                field.kind = "checkboxes"
            else:
                field.label = field.label or (field.options[0] if field.options else "")
                field.current = "1" if field.ticked else ""
                field.index = field.option_indexes[0] if field.option_indexes else field.index
    text = "\n".join(words)
    return Form(fields=fields, buttons=buttons, token=int(scan.get("token") or 0), pid=int(scan.get("pid") or 0), heading=heading,
                text=text, complete=bool(scan.get("complete", True)), human_check=bool(_HUMAN_CHECK.search(text)),
                needs_login=any(f.kind == "password" and not f.hidden for f in fields), ms=int(scan.get("ms") or 0),
                link_nodes=link_nodes, plain_spots=plain_spots[:12], url=str(scan.get("url") or ""), links=list(dict.fromkeys(n["name"][:80] for n in link_nodes)))
