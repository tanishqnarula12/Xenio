"""XENIO's fast path (speed): simple commands done by XENIO itself in well under a second, without the model.

"Open Spotify", "play Tum Hi Ho", "pause", "agla gaana", "awaaz badhao", "volume 40 kar do", "minimize Discord",
"kitne baje hain" ... in English, Hindi/Hinglish and German. Through the model these cost 4-20 s (at least two model
calls: choose the tool, then word the answer) and sometimes a wrong turn ("play a song" opened several tabs); here
each is one action.

Nothing is lost by skipping the model:

* only **fully understood** requests are taken (every clause recognised by ``parse_command``) whose every step is one
  of the actions below; anything else - a question, a vague request, "play X and tell me about it" - goes to Hermes
  unchanged;
* every step is classified by XENIO's policy first (``classify_desktop_tool``, ``classify_tool_call``,
  ``classify_media_action``) and the fast path runs only READ and SAFE_WRITE steps. Anything that needs the user's
  yes (an installer, an app from Downloads ...) goes to Hermes, which asks as always: one confirmation flow;
* replies say only what the action's result verified ("Spotify is open." only after its window appeared);
* the next Hermes turn is told what XENIO did itself, so "close it" or "who sings this?" still work.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.hermes.client import ToolActivity
from app.language.detect import Intent, detect_language, parse_command
from app.logging_setup import event
from app.safety import policy
from app.safety.policy import Decision, PathPolicy, RiskLevel

log = logging.getLogger("xenio.fastpath")

FAST_KINDS = {"OPEN_APPLICATION", "SWITCH_TO", "MINIMIZE", "CLOSE_APPLICATION", "PLAY_MEDIA", "MEDIA_CONTROL", "VOLUME",
              "TIME"}
APP_CONFIDENCE = 0.85  # below this, or ambiguous, the model (which can ask) decides
# Sites people "open" by name; an installed app of that name always wins
WEBSITES = {
    "youtube": "https://www.youtube.com", "youtube music": "https://music.youtube.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "github": "https://github.com", "chatgpt": "https://chatgpt.com",
    "chat gpt": "https://chatgpt.com", "netflix": "https://www.netflix.com", "instagram": "https://www.instagram.com",
    "linkedin": "https://www.linkedin.com", "twitter": "https://x.com", "x": "https://x.com", "reddit": "https://www.reddit.com",
    "whatsapp web": "https://web.whatsapp.com", "google drive": "https://drive.google.com", "google maps": "https://maps.google.com",
    "google calendar": "https://calendar.google.com", "wikipedia": "https://www.wikipedia.org", "claude": "https://claude.ai",
}
_PLAYERS = {"spotify": "spotify", "youtube": "youtube", "youtube music": "youtube"}

# One short sentence per outcome, in the language the user spoke (Hindi in Devanagari: XENIO's Hindi voice reads it)
_SAY = {
    "en": {"open": "{app} is open.", "front": "{app} is in front.", "site": "Opened {site}.", "min": "Minimized {app}.",
           "closed": "Closed {app}.", "still_open": "{app} is still open; it may be asking you to save.",
           "not_open": "{app} isn't open.", "playing": "Playing {what}.", "yt": "Opened {what} on YouTube.",
           "unverified_play": "Started {what} in Spotify, but it isn't confirmed as playing yet.",
           "pause": "Paused.", "play": "Playing{now}.", "next": "Next track{now}.", "previous": "Previous track{now}.",
           "pressed": "Pressed {key}.", "volume": "Volume {n}%.", "muted": "Muted.", "time": "It's {time}.",
           "fail": "That didn't work: {why}"},
    "hi": {"open": "{app} खुल गया।", "front": "{app} सामने है।", "site": "{site} खोल दिया।", "min": "{app} छोटा कर दिया।",
           "closed": "{app} बंद कर दिया।", "still_open": "{app} अभी खुला है; शायद सेव करने को पूछ रहा है।",
           "not_open": "{app} खुला नहीं है।", "playing": "{what} चल रहा है।", "yt": "YouTube पर {what} खोल दिया।",
           "unverified_play": "Spotify में {what} शुरू किया, पर अभी पक्का नहीं कि चल रहा है।",
           "pause": "रोक दिया।", "play": "चालू कर दिया{now}।", "next": "अगला गाना{now}।", "previous": "पिछला गाना{now}।",
           "pressed": "{key} दबा दिया।", "volume": "आवाज़ {n}% पर है।", "muted": "आवाज़ बंद कर दी।",
           "time": "अभी {time} हुए हैं।", "fail": "नहीं हो पाया: {why}"},
    "de": {"open": "{app} ist offen.", "front": "{app} ist im Vordergrund.", "site": "{site} geöffnet.",
           "min": "{app} minimiert.", "closed": "{app} geschlossen.", "still_open": "{app} ist noch offen; vielleicht fragt es "
           "nach dem Speichern.", "not_open": "{app} ist nicht offen.", "playing": "Ich spiele {what}.",
           "yt": "{what} auf YouTube geöffnet.", "unverified_play": "{what} in Spotify gestartet, aber noch nicht bestätigt.",
           "pause": "Pausiert.", "play": "Läuft{now}.", "next": "Nächster Titel{now}.", "previous": "Vorheriger Titel{now}.",
           "pressed": "{key} gedrückt.", "volume": "Lautstärke {n} %.", "muted": "Stumm.", "time": "Es ist {time} Uhr.",
           "fail": "Das hat nicht geklappt: {why}"},
}


@dataclass
class FastReply:
    text: str
    ok: bool
    actions: list[ToolActivity] = field(default_factory=list)
    note: str = ""  # what XENIO did, for Hermes' next turn
    ms: int = 0


@dataclass
class _Step:
    title: str  # shown under the reply ("actions:")
    decision: Decision
    run: Callable[[], dict]  # blocking; raises on failure
    say: Callable[[dict], str]


class _Defer(Exception):
    """This request is not a fast-path request after all: Hermes takes it."""


class FastPath:
    def __init__(self, paths: PathPolicy, music=None, desktop=None, enabled: bool = True, clock=dt.datetime.now):
        self.paths = paths
        self.enabled = enabled
        self._music = music
        self._desktop = desktop
        self._clock = clock

    # ---- collaborators (built on first use: importing them costs nothing until a command needs them) ------------

    @property
    def music(self):
        if self._music is None:
            from app.media import Music

            self._music = Music()
        return self._music

    @property
    def desktop(self):
        if self._desktop is None:
            from app.tools import desktop as desktop_tools

            self._desktop = desktop_tools.service()
        return self._desktop

    def warm(self) -> None:
        """Called at start-up: open the media connection and read the app catalog before the first command."""
        if not self.enabled:
            return
        try:
            self.music.warm()
        except Exception:  # noqa: BLE001 - warming is an optimization only
            pass

        def catalog():
            try:
                self.desktop.catalog.entries()
            except Exception:  # noqa: BLE001
                pass

        import threading

        threading.Thread(target=catalog, name="xenio-catalog-warm", daemon=True).start()

    # ---- entry point --------------------------------------------------------------------------------------------

    async def handle(self, text: str) -> FastReply | None:
        """The reply, or None when Hermes should take the request."""
        if not self.enabled:
            return None
        intents, complete = parse_command(text)
        if not complete or any(i.kind not in FAST_KINDS for i in intents):
            return None
        lang = detect_language(text).code
        say = _SAY.get(lang, _SAY["en"])
        started = time.monotonic()
        try:
            steps = await asyncio.to_thread(self._plan, intents, say)
        except _Defer as exc:
            event(log, "FAST_PATH_DEFERRED", reason=str(exc)[:80])
            return None
        if any(s.decision.blocked or s.decision.level >= RiskLevel.CONFIRM_REQUIRED for s in steps):
            event(log, "FAST_PATH_DEFERRED", reason="needs confirmation")
            return None  # Hermes asks the user through the normal confirmation flow
        if any(s.decision.action.startswith("desktop.") for s in steps):
            self.desktop.new_turn()  # a "Xenio stop" from an earlier request no longer applies
        sentences, actions, done = [], [], []
        ok = True
        for i, step in enumerate(steps):
            t0 = time.monotonic()
            try:
                result = await asyncio.to_thread(step.run)
                sentences.append(step.say(result))
                actions.append(ToolActivity(f"fast-{i}", f"fast: {step.title}", "fast", "completed"))
                done.append(step.title + (" (verified)" if result.get("verified") else ""))
                event(log, "FAST_PATH_ACTION", action=step.decision.action, ok=True, ms=int((time.monotonic() - t0) * 1000))
            except Exception as exc:  # noqa: BLE001 - every failure is reported, never hidden
                why = _reason(exc)
                sentences.append(say["fail"].format(why=why))
                actions.append(ToolActivity(f"fast-{i}", f"fast: {step.title}", "fast", "failed", why))
                event(log, "FAST_PATH_ACTION", logging.WARNING, action=step.decision.action, ok=False, error=why[:120])
                ok = False
                break  # later steps depended on this one ("open Spotify and play ...")
        ms = int((time.monotonic() - started) * 1000)
        event(log, "FAST_PATH", steps=len(steps), ok=ok, ms=ms)
        note = (f"[XENIO did this itself just now, without you: {'; '.join(done)}"
                + (f"; failed: {actions[-1].title[6:]} ({actions[-1].detail})" if not ok else "") + "]") if actions else ""
        return FastReply(" ".join(s for s in sentences if s), ok, actions, note, ms)

    # ---- planning (no side effects) ----------------------------------------------------------------------------

    def _plan(self, intents: list[Intent], say: dict) -> list[_Step]:
        steps: list[_Step] = []
        hint = ""
        for n, intent in enumerate(intents):
            following = intents[n + 1] if n + 1 < len(intents) else None
            target = intent.target
            if intent.kind in ("OPEN_APPLICATION", "SWITCH_TO") and following and following.kind == "PLAY_MEDIA" \
                    and target.lower() in _PLAYERS:
                hint = _PLAYERS[target.lower()]  # "open Spotify and play X": Spotify plays it (and opens itself)
                continue
            if intent.kind in ("OPEN_APPLICATION", "SWITCH_TO"):
                steps.append(self._open(target, say))
            elif intent.kind == "MINIMIZE":
                steps.append(self._window("minimize", target, say))
            elif intent.kind == "CLOSE_APPLICATION":
                steps.append(self._close(target, say))
            elif intent.kind == "PLAY_MEDIA":
                steps.append(self._play(target, hint, say))
            elif intent.kind == "MEDIA_CONTROL":
                steps.append(self._control(target, say))
            elif intent.kind == "VOLUME":
                steps.append(self._volume(target, say))
            elif intent.kind == "TIME":
                steps.append(_Step("tell the time", policy.classify_media_action("time"),
                                   lambda: {"now": self._clock()}, lambda r: say["time"].format(time=_clock_text(r["now"], say))))
        if not steps:
            raise _Defer("nothing to do")
        return steps

    def _open(self, name: str, say: dict) -> _Step:
        from app.desktop.apps import launch_risk

        res = self.desktop.resolve_app(name)
        if res.matches and res.confidence >= APP_CONFIDENCE and not res.ambiguous:
            entry = res.best
            info = {"app_entry": {"name": entry.name, "risk": launch_risk(entry), "path": entry.path}}
            decision = policy.classify_desktop_tool("xenio_open_app", {"name": name}, info)

            def run():
                out = self.desktop.open_app(name, 15.0)
                if not out.get("verified"):
                    raise RuntimeError(f"{entry.name} did not open")
                return out

            return _Step(f"open {entry.name}", decision, run,
                         lambda r: say["front" if r.get("how") == "already_open" else "open"].format(app=r["app"]))
        url = WEBSITES.get(name.strip().lower())
        if url:
            return self._url(url, name.strip(), say)
        raise _Defer(f"no confident app match for {name!r}")

    def _url(self, url: str, site: str, say: dict, line: str = "site") -> _Step:
        from app.tools import web
        from app.tools.base import ToolContext

        decision = policy.classify_tool_call("xenio_open_url", {"url": url}, self.paths, None)

        def run():
            result = web.open_url({"url": url}, ToolContext(paths=self.paths))
            if not result.success:
                raise RuntimeError(result.error or "the browser did not open")
            return result.data or {}

        return _Step(f"open {url}", decision, run, lambda r: say[line].format(site=site.title(), what=site))

    def _window(self, action: str, name: str, say: dict) -> _Step:
        decision = policy.classify_desktop_tool("xenio_window", {"action": action, "name": name})

        def run():
            out = self.desktop.window(action, name)
            if not out.get("verified"):
                raise RuntimeError(f"{name} could not be {action}d")
            return out

        return _Step(f"{action} {name}", decision, run, lambda r: say["min"].format(app=_app_name(r, name)))

    def _close(self, name: str, say: dict) -> _Step:
        decision = policy.classify_desktop_tool("xenio_window", {"action": "close", "name": name})
        if sys.platform != "darwin":
            def run_window():
                return self.desktop.window("close", name)

            return _Step(f"close {name}", decision, run_window,
                         lambda r: say["closed" if r.get("verified") else "still_open"].format(app=name))

        def run():  # macOS: "close Spotify" means quit it, like Cmd+Q (the app may still ask to save)
            from app.desktop import macos

            matches = self.desktop.windows.find_windows(name)
            if not matches:
                return {"result": "not running", "app": name}
            app = matches[0].app
            return {"result": macos.quit_app(matches[0].pid), "app": app}

        def said(r):
            key = {"closed": "closed", "still open": "still_open", "not running": "not_open"}[r["result"]]
            return say[key].format(app=r["app"])

        return _Step(f"quit {name}", decision, run, said)

    def _play(self, target: str, hint: str, say: dict) -> _Step:
        from app.media import MediaError, split_service

        query, said_service = split_service(target)
        try:
            service = self.music.choose(said_service or hint)
        except MediaError as exc:  # e.g. "on Spotify" without Spotify keys: say why, never switch services silently
            problem = exc
            return _Step(f"play {query}", policy.classify_media_action("play_track", query), lambda: _fail(problem),
                         lambda r: "")
        decision = policy.classify_media_action("play_track", f"{query} on {service.title()}")

        def run():
            track = self.music.find(query, service)
            if track.service == "spotify":
                out = self.music.play_spotify(track)
                return {**out, "what": out.get("now_playing") or track.label, "track": track.label}
            check = policy.classify_tool_call("xenio_open_url", {"url": track.url}, self.paths, None)
            if check.blocked or check.level >= RiskLevel.CONFIRM_REQUIRED:
                raise RuntimeError("opening that link needs your confirmation; ask me again in words")
            self._url(track.url, track.title, say).run()
            return {"what": track.title, "service": "youtube", "url": track.url}

        def said(r):
            if r.get("service") == "youtube":
                return say["yt"].format(what=r["what"])
            return say["playing" if r.get("verified") else "unverified_play"].format(what=r["what"])

        return _Step(f"play {query} ({service})", decision, run, said)

    def _control(self, action: str, say: dict) -> _Step:
        def said(r):
            if r.get("verified") is not True:  # the player did not confirm it: never announce it as done
                return say["pressed"].format(key={"play": "play", "pause": "pause", "next": "next",
                                                  "previous": "previous"}[action])
            now = r.get("now_playing") or ""
            return say[action].format(now=f": {now}" if now and action != "pause" else "")

        return _Step({"play": "resume playback", "pause": "pause playback", "next": "next track",
                      "previous": "previous track"}[action], policy.classify_media_action(action),
                     lambda: self.music.control(action), said)

    def _volume(self, change: str, say: dict) -> _Step:
        def said(r):
            if r.get("muted"):
                return say["muted"]
            return say["volume"].format(n=r.get("volume")) if r.get("volume") is not None else say["pressed"].format(key=change)

        return _Step(f"volume {change}", policy.classify_media_action("volume", change),
                     lambda: self.music.volume(change), said)


def _app_name(result: dict, fallback: str) -> str:
    title = str(result.get("window") or "")
    return title.split(" - ")[-1].strip() if title else fallback


def _clock_text(now: dt.datetime, say: dict) -> str:
    if say is _SAY["de"]:
        return now.strftime("%H:%M")
    return now.strftime("%I:%M %p").lstrip("0")


def _fail(exc: Exception) -> dict:
    raise exc


def _reason(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:240]
