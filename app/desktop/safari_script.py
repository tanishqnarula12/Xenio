"""The CEO's own Safari, driven through Safari's own scripting: on any desktop, in the background, never with their mouse or keyboard.

Why this exists, measured on 2026-11-06: the CEO works in a full-screen editor, so every Safari window sits on another desktop
(Space). macOS Accessibility does not reach windows on another Space, so the screen-reader route saw no window at all, an
agent's three tries to open a page all "failed", and each try left a window behind. Safari's scripting reaches every window
on every desktop. Reading and acting inside a page needs Safari's "Allow JavaScript from Apple Events", a switch only the
CEO can turn on (Safari > Settings > Advanced > Show features for web developers, then Develop > Developer Settings).

What it will not do, whatever it is asked: act in a window it did not open or was not pointed to by name, touch the tab the
CEO is looking at, type into a password box, read what a password box holds, or close a window that is not its own.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time

from app.desktop.service import DesktopError

JS_OFF = ("Safari does not let XENIO read pages yet. To let agents work in your own Safari on any desktop, turn on Safari > "
          "Settings > Advanced > \"Show features for web developers\", then Develop > Developer Settings > \"Allow JavaScript "
          "from Apple Events\". Until then agents can only use Safari windows on the desktop you are looking at")
GONE = "that Safari window or tab is gone: it was closed"
OPEN_WAIT = 20.0
SETTLE_WAIT = 8.0
STEADY = 1.5  # seconds a page must look the same to count as settled
OPEN_STEADY = 3.0  # a page just opened loads in bursts: LinkedIn paused longer than 1.5 s between them, and was read at 730 of 23,667 characters
ACT_WAIT = 10.0  # seconds to wait for a button or box to appear before saying it is not there
_SEP = "␞"  # between fields of one line, which no page title is likely to hold

_OPEN = """on run argv
tell application "Safari"
set oldIds to id of every window
make new document with properties {URL:item 1 of argv}
set newIds to id of every window
repeat with i in newIds
if oldIds does not contain (contents of i) then return (contents of i) as text
end repeat
return ""
end tell
end run"""

_RUN = """on run argv
tell application "Safari"
return do JavaScript (item 3 of argv) in tab ((item 2 of argv) as integer) of window id ((item 1 of argv) as integer)
end tell
end run"""

_LIST = """on run argv
set sep to item 1 of argv
set out to ""
tell application "Safari"
repeat with w in (every window)
try
set out to out & "W" & sep & (id of w) & sep & (index of w) & linefeed
set i to 0
repeat with t in (every tab of w)
set i to i + 1
set nm to ""
try
set nm to name of t
end try
set u to ""
try
set u to URL of t
end try
if nm is missing value then set nm to ""
if u is missing value then set u to ""
set out to out & "T" & sep & i & sep & nm & sep & u & linefeed
end repeat
end try
end repeat
end tell
return out
end run"""

_CLOSE = """on run argv
tell application "Safari"
set w to window id ((item 1 of argv) as integer)
if (count of tabs of w) is less than 2 then close w
end tell
end run"""

# One page script for every command. The agent's words arrive as a JSON literal, never as code.
_PAGE = r"""JSON.stringify((function (cmd) {
  function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }
  function shown(e) { var r = e.getBoundingClientRect(); var s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }
  function labelOf(e) {
    var t = e.getAttribute('aria-label') || '';
    if (!t && e.id) { var l = (e.getRootNode().querySelector ? e.getRootNode() : document).querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = l.innerText; }
    if (!t && e.getAttribute('aria-labelledby')) { var by = document.getElementById(e.getAttribute('aria-labelledby').split(' ')[0]); if (by) t = by.innerText; }
    if (!t) { var p = e.closest('label'); if (p) t = p.innerText; }
    if (!t) t = e.getAttribute('placeholder') || e.getAttribute('name') || e.getAttribute('title') || '';
    return clean(t).slice(0, 80);
  }
  function nameOf(e) {
    if (e.tagName === 'INPUT') return clean(((e.getAttribute('type') || '').match(/checkbox|radio/) ? labelOf(e) : e.value) || labelOf(e)).slice(0, 80);
    var inner = clean(e.innerText), named = clean(e.getAttribute('aria-label') || e.getAttribute('title'));
    return ((/[A-Za-z0-9]/.test(inner) ? inner : (named || inner)) || labelOf(e)).slice(0, 80);
  }
  function secure(e) { return (e.getAttribute('type') || '').toLowerCase() === 'password' || /current-password|new-password/.test(e.getAttribute('autocomplete') || ''); }
  function everywhere(sel) {  /* the page and every open shadow root in it: web components keep their boxes there */
    var out = [], roots = [document];
    while (roots.length) {
      var root = roots.shift();
      root.querySelectorAll(sel).forEach(function (e) { out.push(e); });
      root.querySelectorAll('*').forEach(function (e) { if (e.shadowRoot) roots.push(e.shadowRoot); });
    }
    return out;
  }
  function controls() {
    var b = [], l = [], f = [];
    everywhere('button,input,textarea,select,a[href],[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=switch],[role=option],[role=textbox],[role=searchbox],[role=combobox],[contenteditable=true],[contenteditable=""],[contenteditable=plaintext-only]').forEach(function (e) {
      if (!shown(e) || e.disabled) return;
      var tag = e.tagName, type = (e.getAttribute('type') || '').toLowerCase(), role = e.getAttribute('role') || '';
      var typed = e.isContentEditable || ['textbox', 'searchbox', 'combobox'].indexOf(role) >= 0;  /* LinkedIn's Headline is a div with role=textbox: it was filed as a button, and could never be typed into */
      if (tag === 'A' || role === 'link') l.push(e);
      else if (!typed && (tag === 'BUTTON' || role || ['submit', 'button', 'checkbox', 'radio'].indexOf(type) >= 0)) b.push(e);
      else if (tag === 'INPUT' && ['hidden', 'file', 'image', 'reset'].indexOf(type) >= 0) return;
      else f.push(e);
    });
    return {b: b, l: l, f: f};
  }
  function find(list, name, key) {
    var want = clean(name).toLowerCase();
    if (!want) return null;
    var exact = list.filter(function (e) { return key(e).toLowerCase() === want; });
    if (exact.length) return exact[0];
    var part = list.filter(function (e) { return key(e).toLowerCase().indexOf(want) >= 0; });
    return part.length ? part[0] : null;
  }
  function page() {
    var c = controls();
    var frames = Array.prototype.slice.call(document.querySelectorAll('iframe')).map(function (i) { return i.src || ''; }).join(' ');
    var main = document.querySelector('main, [role=main]');  /* what the page is about, not its menus and "people also viewed": a LinkedIn profile is 24,000 characters in all */
    var open = everywhere('[role=dialog],[aria-modal=true]').filter(shown).map(function (d) { return d.innerText; }).join('\n');
    var text = (open ? open + '\n' : '') + (main && shown(main) ? main.innerText : (document.body ? document.body.innerText : ''));
    return {title: document.title, url: location.href, ready: document.readyState, text: text.slice(0, 9000),
            buttons: c.b.slice(0, 40).map(nameOf), links: c.l.slice(0, 60).map(nameOf),
            fields: c.f.slice(0, 40).map(function (e) { return {label: labelOf(e), kind: e.isContentEditable ? 'text area' : (e.tagName === 'TEXTAREA' ? 'text area' : (e.tagName === 'SELECT' ? 'list' : ((e.getAttribute('type') || 'text').toLowerCase()))), secure: secure(e)}; }),
            needs_login: c.f.some(secure), human_check: /recaptcha|hcaptcha|turnstile|challenges\.cloudflare/.test(frames),
            busy: everywhere('[aria-busy=true],[role=progressbar]').some(shown), hidden: document.visibilityState === 'hidden'};
  }
  if (cmd.do === 'read') return page();
  if (cmd.do === 'click') {
    var c = controls();
    var e = find(c.b.concat(c.l), cmd.name, nameOf) || find(c.b.concat(c.l), cmd.name, labelOf);
    if (!e) return {error: 'nothing to press says ' + JSON.stringify(cmd.name), buttons: c.b.slice(0, 25).map(nameOf), links: c.l.slice(0, 25).map(nameOf)};
    var said = nameOf(e);
    e.scrollIntoView({block: 'center'});
    e.click();
    return {pressed: said};
  }
  if (cmd.do === 'type') {
    var f = find(controls().f, cmd.name, labelOf);
    if (!f) return {error: 'no box is labelled ' + JSON.stringify(cmd.name), fields: controls().f.slice(0, 25).map(labelOf)};
    if (secure(f)) return {error: 'password', field: labelOf(f)};
    f.focus();
    if (f.isContentEditable) {
      var range = document.createRange(); range.selectNodeContents(f); var sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
      if (!document.execCommand('insertText', false, cmd.text)) { f.textContent = cmd.text; f.dispatchEvent(new InputEvent('input', {bubbles: true})); }
    } else if (f.tagName === 'SELECT') {
      var want = clean(cmd.text).toLowerCase();
      var o = Array.prototype.find.call(f.options, function (o) { return clean(o.text).toLowerCase() === want; }) || Array.prototype.find.call(f.options, function (o) { return clean(o.text).toLowerCase().indexOf(want) >= 0; });
      if (!o) return {error: 'that list has no ' + JSON.stringify(cmd.text), field: labelOf(f)};
      f.value = o.value; f.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
      var proto = f.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(f, cmd.text);
      f.dispatchEvent(new InputEvent('input', {bubbles: true, data: cmd.text, inputType: 'insertText'}));
      f.dispatchEvent(new Event('change', {bubbles: true}));
    }
    var now = f.isContentEditable ? clean(f.innerText) : (f.tagName === 'SELECT' ? clean(f.options[f.selectedIndex].text) : f.value);
    var held = f.tagName === 'SELECT' ? now.toLowerCase().indexOf(clean(cmd.text).toLowerCase()) >= 0 : clean(now) === clean(cmd.text);
    if (cmd.enter) {
      if (f.form) { if (f.form.requestSubmit) f.form.requestSubmit(); else f.form.submit(); }
      else { ['keydown', 'keypress', 'keyup'].forEach(function (k) { f.dispatchEvent(new KeyboardEvent(k, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true})); }); }
    }
    return {field: labelOf(f), held: held};
  }
  if (cmd.do === 'back') { history.back(); return {}; }
  return {error: 'unknown'};
})(__ARGS__))"""


def _mac_locked() -> bool:
    try:
        from app.desktop import macos

        return bool(macos.session_locked())
    except Exception:  # noqa: BLE001 - not a Mac: nothing to say
        return False


def js_off() -> DesktopError:
    error = DesktopError(JS_OFF, "NEEDS_PERMISSION")
    error.js_off = True
    return error


def _digest(view: dict) -> str:
    return hashlib.sha256(f"{view.get('url')}\n{view.get('text')}".encode("utf-8", "replace")).hexdigest()


class SafariScript:
    """Agents' windows and tabs in the CEO's Safari, each reached by Safari's window id. The CEO's own tab stays where it is."""

    scripted = True  # no screen position: no arrow, no screenshot, and the vault's fill needs the window on screen

    def __init__(self, runner=subprocess.run, sleep=time.sleep, clock=time.monotonic, locked=None):
        self._run = runner
        self._locked = locked if locked is not None else _mac_locked
        self._sleep = sleep
        self._clock = clock
        self._by_session: dict[str, tuple[int, int]] = {}  # session -> (Safari window id, tab number)
        self._own: set[int] = set()  # windows it opened: the only ones it closes
        self.desktop = None

    # ---- talking to Safari ------------------------------------------------------------------------------------------

    def _osa(self, script: str, *args: str, timeout: float = 20.0) -> str:
        try:
            done = self._run(["/usr/bin/osascript", "-e", script, "--", *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DesktopError(f"Safari did not answer ({type(exc).__name__})") from None
        if done.returncode != 0:
            err = (done.stderr or "").strip()
            if "Allow JavaScript from Apple Events" in err:
                raise js_off()
            if "-1743" in err or "not allowed" in err.lower():
                raise DesktopError("macOS has not allowed XENIO to control Safari: System Settings > Privacy & Security > Automation, switch on Safari for the app XENIO runs in", "NEEDS_PERMISSION")
            if "-1728" in err or "-1719" in err:
                raise DesktopError(GONE)
            raise DesktopError(f"Safari could not do it: {err[-200:] or done.returncode}")
        return (done.stdout or "").strip()

    def _unlocked(self) -> None:
        """XENIO's rule on a locked Mac holds here too, though nothing here goes through the keyboard: the CEO is not there to see or stop it."""
        if self._locked():
            raise DesktopError("The Mac is locked, so nothing was opened, clicked or typed. Tell the CEO to unlock it and ask again.")

    def _where(self, session: str) -> tuple[int, int]:
        where = self._by_session.get(session)
        if where is None:  # never "whatever is in front": on a real test that was the CEO's own YouTube
            raise DesktopError("you have no window of your own yet: open the address with page_open, or name one of the CEO's tabs with page_use_tab")
        return where

    def _page(self, session: str, cmd: dict) -> dict:
        wid, tab = self._where(session)
        out = self._osa(_RUN, str(wid), str(tab), _PAGE.replace("__ARGS__", json.dumps(cmd, ensure_ascii=True)))
        try:
            answer = json.loads(out or "{}")
        except ValueError:
            raise DesktopError("the page answered with something that could not be read") from None
        return answer if isinstance(answer, dict) else {}

    def _view(self, raw: dict) -> dict:
        fields = [{"label": str(f.get("label") or ""), "kind": str(f.get("kind") or ""), "is_password": bool(f.get("secure"))} for f in raw.get("fields") or [] if isinstance(f, dict)]
        return {"window": str(raw.get("title") or "")[:160], "url": str(raw.get("url") or ""), "text": str(raw.get("text") or ""),
                "buttons": [b for b in raw.get("buttons") or [] if b], "links": [x for x in raw.get("links") or [] if x], "fields": fields,
                "needs_login": bool(raw.get("needs_login")), "human_check": bool(raw.get("human_check")), "held_back": bool(raw.get("hidden"))}

    def _settle(self, session: str, before: str = "", wait: float = SETTLE_WAIT, steady: float = STEADY) -> tuple[dict, bool]:
        """The page once it has loaded and stopped changing, and whether it changed from ``before``.

        "Loaded" is not enough: LinkedIn's profile grew from 962 to 24,000 characters over 6 s after that, and its edit form
        appeared 4 to 6 s after the pencil was pressed. A page counts as settled when it has looked the same for STEADY seconds."""
        self._where(session)  # no window of its own is a refusal, at once: never retried
        end = self._clock() + wait
        raw: dict = {}
        last, since = "", self._clock()
        while True:
            try:
                raw = self._page(session, {"do": "read"})
            except DesktopError as exc:
                if exc.status == "NEEDS_PERMISSION" or str(exc) == GONE or self._clock() >= end:
                    raise
                raw = {}
            view = self._view(raw)
            now = _digest(view) + str(len(view["fields"]))  # not the count of fetches: LinkedIn never stops fetching, and waiting for it took 20 s for no better a read
            if now != last:
                last, since = now, self._clock()
            changed = bool(before) and _digest(view) != before
            ready = raw.get("ready") == "complete" and (view["text"].strip() or view["fields"]) and (not before or changed)
            if ready and not raw.get("busy") and self._clock() - since >= steady:
                return view, changed
            if self._clock() >= end:
                if ready and raw.get("busy"):
                    view["say"] = "The page still shows it is loading: read it again in a moment."
                return view, changed
            self._sleep(0.3)

    def _held_back(self, session: str) -> str:
        """Safari all but stops a page whose window is not on the CEO's screen: LinkedIn then never drew its edit form. Said plainly, so the agent asks instead of wandering."""
        try:
            hidden = bool(self._page(session, {"do": "read"}).get("hidden"))
        except DesktopError:
            return ""
        return (" Safari is holding this page back because its window is not on the CEO's screen, so it may not have finished drawing."
                " Ask the CEO with ask_ceo to bring that Safari window into view, then try again.") if hidden else ""

    def _act(self, session: str, cmd: dict) -> dict:
        """Like a person waiting for a button to appear before pressing it: the target is looked for again until ACT_WAIT runs out."""
        end = self._clock() + ACT_WAIT
        while True:
            done = self._page(session, cmd)
            missing = str(done.get("error") or "").startswith(("nothing to press", "no box is labelled"))
            if not missing or self._clock() >= end:
                return done
            self._sleep(0.5)

    # ---- what agents do ---------------------------------------------------------------------------------------------

    def open(self, url: str, session: str = "") -> dict:
        """A new window of its own. Nothing the CEO has open is touched."""
        self._unlocked()
        made = self._osa(_OPEN, url)
        if not made.isdigit():
            raise DesktopError("Safari took the address but no new window appeared: nothing is confirmed")
        wid = int(made)
        self._own.add(wid)
        self._by_session[session] = (wid, 1)
        try:
            view, _ = self._settle(session, wait=OPEN_WAIT, steady=OPEN_STEADY)
        except DesktopError:
            self.close({}, session)  # a window it cannot use is not left on the CEO's Mac
            raise
        view["verified"] = bool(view["text"].strip() or view["fields"])
        if not view["verified"]:
            view["say"] = "The window opened, but the page has shown nothing yet."
        return view

    def page(self, args: dict | None = None, session: str = "") -> dict:
        view, _ = self._settle(session, wait=3.0)
        return view

    def tabs(self) -> dict:
        """Every Safari window and its tabs, by title. On every desktop."""
        out, current = [], None
        for line in self._osa(_LIST, _SEP).splitlines():
            parts = line.split(_SEP)
            if parts[0] == "W" and len(parts) >= 3:
                current = {"id": int(parts[1]) if parts[1].isdigit() else 0, "browser": "Safari", "in_front": parts[2] == "1", "tabs": [], "urls": []}
                out.append(current)
            elif parts[0] == "T" and current is not None and len(parts) >= 4:
                current["tabs"].append(" ".join(parts[2].split())[:120] or "(untitled)")
                current["urls"].append(parts[3])
        for w in out:
            w["window"] = w["tabs"][0] if len(w["tabs"]) == 1 else f"{len(w['tabs'])} tabs: " + "; ".join(w["tabs"][:3])
            w["mine"] = w["id"] in self._own
        return {"windows": [{k: w[k] for k in ("window", "browser", "in_front", "tabs")} for w in out], "_all": out}

    def adopt(self, session: str, window: str) -> tuple[int, int]:
        """A tab the CEO already has open, such as a site they are signed in to. It has to be named, and only one may fit. It is read where it is: the CEO's own view does not change."""
        wanted = " ".join(str(window or "").split()).lower()
        if len(wanted) < 3:
            raise DesktopError("name the tab, by part of its title: use page_tabs to see them")
        found = [(w["id"], i + 1) for w in self.tabs()["_all"] for i, title in enumerate(w["tabs"]) if wanted in title.lower()]
        if len(found) != 1:
            raise DesktopError(f"{len(found)} tabs have {window!r} in their title: name one more exactly" if found else f"no tab has {window!r} in its title: use page_tabs to see them")
        self._by_session[session] = found[0]
        return found[0]

    def switch(self, args: dict, session: str = "") -> dict:
        """Another tab of the same window, by part of its title. Read where it is, without bringing it to the front."""
        wid, _ = self._where(session)
        wanted = " ".join(str(args.get("tab") or "").split()).lower()
        mine = next((w for w in self.tabs()["_all"] if w["id"] == wid), None)
        hits = [i + 1 for i, title in enumerate((mine or {}).get("tabs") or []) if wanted and wanted in title.lower()]
        if len(hits) != 1:
            raise DesktopError(f"{len(hits)} tabs in that window have {args.get('tab')!r} in their title" if hits else f"no tab in that window has {args.get('tab')!r} in its title")
        self._by_session[session] = (wid, hits[0])
        return self.page({}, session)

    def click(self, args: dict, session: str = "") -> dict:
        self._unlocked()
        before = _digest(self._view(self._page(session, {"do": "read"})))
        done = self._act(session, {"do": "click", "name": str(args.get("element") or "")})
        if done.get("error"):
            choices = ", ".join(str(x) for x in (done.get("buttons") or []) + (done.get("links") or []) if x)[:400]
            raise DesktopError(f"{done['error']}." + self._held_back(session) + (f" What can be pressed: {choices}" if choices else ""))
        self._sleep(0.4)
        view, changed = self._settle(session, before)
        view.update(pressed=str(done.get("pressed") or args.get("element") or ""), verified=changed)
        return view

    def type_text(self, args: dict, session: str = "") -> dict:
        self._unlocked()
        before = _digest(self._view(self._page(session, {"do": "read"}))) if args.get("press_enter") else ""
        done = self._act(session, {"do": "type", "name": str(args.get("element") or ""), "text": str(args.get("text") or ""), "enter": bool(args.get("press_enter"))})
        if done.get("error") == "password":
            raise DesktopError(f"{done.get('field') or 'that box'} is a password box: agents never type into one. Use page_sign_in")
        if done.get("error"):
            raise DesktopError(f"{done['error']}." + self._held_back(session) + (f" Boxes on the page: {', '.join(str(x) for x in done.get('fields') or [] if x)[:300]}" if done.get("fields") else ""))
        if args.get("press_enter"):
            self._sleep(0.4)
            view, _ = self._settle(session, before)
        else:
            view = self._view(self._page(session, {"do": "read"}))
        view.update(field=str(done.get("field") or args.get("element") or ""), verified=bool(done.get("held")))
        return view

    def navigate(self, how: str, args: dict | None = None, session: str = "") -> dict:
        if how != "back":
            raise DesktopError(f"{how} is not something this browser does")
        self._unlocked()
        before = _digest(self._view(self._page(session, {"do": "read"})))
        self._page(session, {"do": "back"})
        self._sleep(0.4)
        view, changed = self._settle(session, before)
        view["verified"] = changed
        return view

    def pick(self, args: dict | None = None, session: str = ""):
        self._where(session)
        raise DesktopError("this window is worked through Safari itself and can be on another desktop, so it has no place on your screen to look at or to sign in through")

    def close(self, args: dict | None = None, session: str = "") -> dict:
        where = self._by_session.pop(session, None)
        if where is None or where[0] not in self._own:  # the CEO's own windows are never closed
            return {"closed": False}
        self._own.discard(where[0])
        try:
            self._osa(_CLOSE, str(where[0]))
        except DesktopError:
            return {"closed": False}
        return {"closed": True}
