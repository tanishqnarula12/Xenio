"""The profile vault, forms and the user's own browser, as tools (V4 phase 2).

Registered in Hermes by the xenio-guard plugin and judged before every call by ``app.safety.policy``. The model works with
labels: ``xenio_form`` puts values into fields without the model seeing them, and a sensitive detail never reaches it at all.
"""

from __future__ import annotations

import re
from typing import Any

from app.safety.policy import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult, action_failed

_engine = None
_browser = None
_vault = None


def vault():
    global _vault
    if _vault is None:
        from app.personal.vault import ProfileVault

        _vault = ProfileVault()
    return _vault


def engine():
    global _engine
    if _engine is None:
        from app.autofill.engine import Autofill
        from app.tools import desktop

        _engine = Autofill(desktop.service(), vault())
    return _engine


def browser():
    global _browser
    if _browser is None:
        from app.desktop.browser import BrowserWork
        from app.tools import desktop

        _browser = BrowserWork(desktop.service())
    return _browser


def install(vault_value=None, engine_value=None, browser_value=None) -> None:
    """Tests: fakes (None = build the real one on next use)."""
    global _vault, _engine, _browser
    _vault, _engine, _browser = vault_value, engine_value, browser_value


def _run(tool: str, fn) -> ToolResult:
    from app.desktop.common import UIAError, WindowError
    from app.desktop.input import InputError
    from app.desktop.service import DesktopError
    from app.personal.vault import VaultError

    try:
        out = fn()
        return ToolResult.ok(tool, str(out.pop("say", "") or "done"), **out)
    except (DesktopError, WindowError, UIAError, InputError) as exc:
        return action_failed(tool, str(exc), getattr(exc, "status", "FAILED"))
    except VaultError as exc:
        return ToolResult.fail(tool, str(exc))
    except ImportError as exc:
        return ToolResult.fail(tool, f"not available here ({exc})", "NOT_SUPPORTED")


# ---- the profile ------------------------------------------------------------------------------------------------------

def profile(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from app.autofill.matching import FIELDS, canonical_for

    action = str(args.get("action") or "list").lower()

    def run() -> dict:
        v = vault()
        if action == "list":
            entries = v.entries()
            return {"say": f"{len(entries)} detail(s) in the profile", "entries": entries,
                    "note": "Names only. Use xenio_form to put them into forms: it does that without seeing or showing the values."}
        if action == "get":
            entry = v.get(str(args.get("id") or ""))
            if entry is None:
                return {"say": "that detail is not in the profile", "found": False}
            if entry.get("sensitive"):
                return {"say": f"{entry['label']} is marked sensitive: it is only shown to the user, on the Profile page", "found": True,
                        "sensitive": True}
            return {"say": entry["label"], "found": True, "label": entry["label"], "value": entry["value"]}
        if action == "add":
            label = str(args.get("label") or "")
            canonical, score = canonical_for(label)
            field = canonical if score >= 0.8 else ""
            category = str(args.get("category") or (FIELDS[field][0] if field else "other"))
            made = v.add(category, label, str(args.get("value") or ""), field=field,
                         sensitive=args.get("sensitive") if isinstance(args.get("sensitive"), bool) else None, source="chat")
            return {"say": f"Saved {made['label']} in the profile", **_short(made), "saved": True}
        if action == "update":
            changes = {k: args[k] for k in ("label", "value", "category") if isinstance(args.get(k), str) and args[k]}
            if isinstance(args.get("sensitive"), bool):
                changes["sensitive"] = args["sensitive"]
            made = v.update(str(args.get("id") or ""), **changes)
            return {"say": f"Corrected {made['label']} in the profile", **_short(made), "saved": True}
        if action == "delete":
            removed = v.delete(str(args.get("id") or ""))
            return {"say": "Deleted from the profile" if removed else "That detail was not in the profile", "deleted": removed}
        raise ValueError(f"unknown action {action!r}")

    return _run("autofill.profile", run)


def _short(entry: dict) -> dict:
    return {k: entry.get(k) for k in ("id", "label", "category", "sensitive")}


# ---- forms ------------------------------------------------------------------------------------------------------------

def form(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    action = str(args.get("action") or "scan").lower()
    actions = {"scan": engine().scan, "fill": engine().fill, "next": engine().next_page, "review": engine().review,
               "submit": engine().submit}
    if action not in actions:
        return ToolResult.fail("autofill.form", f"action must be one of {', '.join(actions)}")
    return _run("autofill.form", lambda: actions[action](args, ctx.session))


# ---- the user's browser -----------------------------------------------------------------------------------------------

def browse(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    action = str(args.get("action") or "page").lower()
    work = browser()
    def run() -> dict:
        if action == "open":
            return work.open(str(args.get("url") or ""), ctx.session)
        if action == "tabs":
            return work.tabs()
        if action == "page":
            return work.page(args, ctx.session)
        if action == "click":
            return work.click(args, ctx.session)
        if action == "type":
            return work.type_text(args, ctx.session)
        if action in ("back", "forward", "reload"):
            return work.navigate(action, args, ctx.session)
        if action == "switch":
            return work.switch(args, ctx.session)
        if action == "close":
            return work.close(args, ctx.session)
        raise ValueError(f"unknown action {action!r}")
    return _run("autofill.browser", run)


_S, _B = {"type": "string"}, {"type": "boolean"}
_MAP = {"type": "object"}
_LIST = {"type": "array", "items": _S}

_TOOLS = [
    Tool("autofill.profile",
         "The user's profile vault: reusable details they chose to keep for forms (identity, education, work, portfolio, "
         "addresses). action: list (names only) | get (id: one detail's value, only if the user asks for it; sensitive ones are "
         "never given) | add (label, value, optional category, sensitive) | update (id, then what changes) | delete (id). "
         "Only what the user says or approves goes in: never anything from a page. Passwords, codes, tokens and payment "
         "details are refused. Say what was saved only if the result confirms it.",
         {"type": "object", "properties": {"action": _S, "id": _S, "label": _S, "value": _S, "category": _S, "sensitive": _B},
          "required": ["action"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_profile", profile),
    Tool("autofill.form",
         "A form on the user's screen, any page or app, read straight from the UI: no screenshot, far faster than looking at "
         "the screen. For 'fill this form' call action fill at once, with steps=true for a multi-page form: in ONE call it "
         "reads the form, fills everything certain from the profile, checks what each field then holds and goes on through "
         "Next pages until something needs the user. You never see the values and must not ask for them. The result lists "
         "filled, not_filled and left (with why), and 'questions': multiple-choice and tick-box questions (with their options), "
         "details that could be either of two saved ones (candidates), and required blanks. Answer from what the user told you "
         "and their profile, never invent an answer, and ask only what you cannot know, ALL TOGETHER IN ONE MESSAGE, once. "
         "Then call fill with choose {field id: option, or a list of options for tick boxes}, use {field id: entry id}, text "
         "{field id: the user's words}; skip [ids]; replace [ids] only when the user said to overwrite what is there. XENIO "
         "remembers each answer for this form, so never ask again. A graded quiz's questions (graded true) are the user's: "
         "fill only their details. action scan: only see what it asks. action next: press Next, returns the next page. action review: what "
         "it holds now. action submit (element = the button's name): XENIO asks the user first, then says what the page "
         "answered. NEVER say a form was submitted unless submit returned submitted true, and never press a submit button "
         "another way. Passwords, payment details and checks that a person is here are the user's to do.",
         {"type": "object", "properties": {"action": _S, "window": _S, "use": _MAP, "text": _MAP, "choose": _MAP, "skip": _LIST,
                                           "replace": _LIST, "steps": _B, "element": _S, "anyway": _B},
          "required": ["action"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_form", form),
    Tool("autofill.browser",
         "Work in the user's OWN browser, where they are signed in, to do things for them: each call acts and returns the "
         "resulting page, read straight from it, so never look at the screen in between. action: open (url: a new window, "
         "their tabs stay untouched, the screen stays theirs) | page (url, title, text, links, buttons, fields; 'unchanged' "
         "when it is what you last read) | click (element: a link or button by name, optional expect: words the next page "
         "shows; no mouse) | type (element: a field by name, text, press_enter true for a search box) | back | forward | "
         "reload | tabs | switch (tab) | close. Pass window (words from its title) to work in another window. Fill forms "
         "with xenio_form. If needs_login: ask the user to sign in, then go on. If human_check: ask them to do it. XENIO "
         "asks the user before anything consequential, so go ahead and work through the steps yourself without asking "
         "between them; report only what the results confirm.",
         {"type": "object", "properties": {"action": _S, "url": _S, "window": _S, "element": _S, "text": _S, "press_enter": _B,
                                           "expect": _S, "tab": _S, "fresh": _B},
          "required": ["action"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_browser", browse),
]


def xenio_tools() -> list[Tool]:
    return list(_TOOLS)
