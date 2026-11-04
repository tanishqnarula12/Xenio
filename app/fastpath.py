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
from app.language.detect import Intent, detect_language, parse_command, parse_form_command, parse_image_command, parse_teach_command, parse_ui_command
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
           "form_done": "Filled {n} fields and checked them.", "form_none": "Your profile has nothing for this form yet: add your details on the Profile page, or tell me them.",
           "form_failed": "Could not enter {what}.", "form_ask": "Tell me your answers for: {what}.",
           "form_not_sent": "It is not submitted: check it first.",
           "image_started": "Making it now.", "image_started_many": "Making {n} pictures now.", "lesson_writing": "Writing your lesson on {topic}.", "lesson_unknown": "I don't know {topic} well enough to teach it without making things up. {why} If you have notes on it, add the file and I'll teach from them.", 
           "clicked": "Clicked {what}.", "clicked_unconfirmed": "I clicked {what}, but nothing visibly changed.",
           "scrolled": "Scrolled.",
           "fail": "That didn't work: {why}"},
    "hi": {"open": "{app} खुल गया।", "front": "{app} सामने है।", "site": "{site} खोल दिया।", "min": "{app} छोटा कर दिया।",
           "closed": "{app} बंद कर दिया।", "still_open": "{app} अभी खुला है; शायद सेव करने को पूछ रहा है।",
           "not_open": "{app} खुला नहीं है।", "playing": "{what} चल रहा है।", "yt": "YouTube पर {what} खोल दिया।",
           "unverified_play": "Spotify में {what} शुरू किया, पर अभी पक्का नहीं कि चल रहा है।",
           "pause": "रोक दिया।", "play": "चालू कर दिया{now}।", "next": "अगला गाना{now}।", "previous": "पिछला गाना{now}।",
           "pressed": "{key} दबा दिया।", "volume": "आवाज़ {n}% पर है।", "muted": "आवाज़ बंद कर दी।",
           "form_done": "{n} फ़ील्ड भर दिए और जाँच लिए।", "form_none": "इस फ़ॉर्म के लिए आपकी प्रोफ़ाइल में अभी कुछ नहीं है: Profile पेज पर जोड़ दीजिए या मुझे बता दीजिए।",
           "form_failed": "{what} नहीं भर पाया।", "form_ask": "इनके जवाब बता दीजिए: {what}।",
           "form_not_sent": "सबमिट नहीं किया है: पहले देख लीजिए।",
           "image_started": "बना रहा हूँ।", "image_started_many": "{n} तस्वीरें बना रहा हूँ।", "lesson_writing": "{topic} का पाठ लिख रहा हूँ।", "lesson_unknown": "{topic} के बारे में मुझे इतना नहीं पता कि बिना कुछ गढ़े सिखा सकूँ। {why} आपके पास नोट्स हों तो फ़ाइल जोड़ दीजिए, मैं उन्हीं से सिखाऊँगा।", 
           "clicked": "{what} पर क्लिक कर दिया।", "clicked_unconfirmed": "{what} पर क्लिक किया, पर स्क्रीन पर कुछ नहीं बदला।",
           "scrolled": "स्क्रॉल कर दिया।",
           "time": "अभी {time} हुए हैं।", "fail": "नहीं हो पाया: {why}"},
    "de": {"open": "{app} ist offen.", "front": "{app} ist im Vordergrund.", "site": "{site} geöffnet.",
           "min": "{app} minimiert.", "closed": "{app} geschlossen.", "still_open": "{app} ist noch offen; vielleicht fragt es "
           "nach dem Speichern.", "not_open": "{app} ist nicht offen.", "playing": "Ich spiele {what}.",
           "yt": "{what} auf YouTube geöffnet.", "unverified_play": "{what} in Spotify gestartet, aber noch nicht bestätigt.",
           "pause": "Pausiert.", "play": "Läuft{now}.", "next": "Nächster Titel{now}.", "previous": "Vorheriger Titel{now}.",
           "pressed": "{key} gedrückt.", "volume": "Lautstärke {n} %.", "muted": "Stumm.", "time": "Es ist {time} Uhr.",
           "form_done": "{n} Felder ausgefüllt und geprüft.", "form_none": "Dein Profil enthält dafür noch nichts: füge es auf der Profilseite hinzu oder sag es mir.",
           "form_failed": "{what} konnte nicht eingetragen werden.", "form_ask": "Sag mir deine Antworten für: {what}.",
           "form_not_sent": "Nicht abgeschickt: prüfe es zuerst.",
           "image_started": "Ich mache es jetzt.", "image_started_many": "Ich mache jetzt {n} Bilder.", "lesson_writing": "Ich schreibe deine Lektion zu {topic}.", "lesson_unknown": "Ich kenne {topic} nicht gut genug, um es zu unterrichten, ohne etwas zu erfinden. {why} Wenn du Notizen hast, füge die Datei hinzu, dann unterrichte ich danach.", 
           "clicked": "{what} geklickt.", "clicked_unconfirmed": "Ich habe {what} geklickt, aber nichts hat sich sichtbar geändert.",
           "scrolled": "Gescrollt.",
           "fail": "Das hat nicht geklappt: {why}"},
}


@dataclass
class FastReply:
    text: str
    ok: bool
    actions: list[ToolActivity] = field(default_factory=list)
    note: str = ""  # what XENIO did, for Hermes' next turn
    ms: int = 0
    attachments: list[dict] = field(default_factory=list)  # cards shown with the reply: {"kind", "id"}


@dataclass
class _Step:
    title: str  # shown under the reply ("actions:")
    decision: Decision
    run: Callable[[], dict]  # blocking; raises on failure
    say: Callable[[dict], str]


class _Defer(Exception):
    """This request is not a fast-path request after all: Hermes takes it."""


class FastPath:
    def __init__(self, paths: PathPolicy, music=None, desktop=None, enabled: bool = True, clock=dt.datetime.now,
                 reminders=None, tasks=None, autofill=None, teacher=None, images=None):
        self.paths = paths
        self.enabled = enabled
        self._music = music
        self._desktop = desktop
        self._clock = clock
        self.reminders = reminders  # app.reminders.Reminders (None: reminders go to Hermes)
        self._tasks = tasks  # () -> app.personal.tasks.TaskList
        self._autofill = autofill
        self._teacher = teacher
        self._images = images
        self.on_tool = None  # tells the orb what a long job is really doing (set by the orchestrator per request)
        self.progress = None  # words to show at once before something that takes a while (set by the orchestrator per request)

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
        personal = await self._personal(text)
        if personal is not None:
            return personal
        if parse_form_command(text):
            return await self._form(text)
        lesson = parse_teach_command(text)
        if lesson is not None:
            return await self._teach(lesson, text)
        picture = parse_image_command(text)
        if picture is not None:
            return await self._image(picture, text)
        ui = parse_ui_command(text)
        if ui is not None:
            return await self._ui(ui, text)
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

    # ---- one deterministic action on the window in front -----------------------------------------------------------

    async def _ui(self, args: dict, text: str) -> FastReply | None:
        """"Click Compile and Run", "scroll down", "press enter": the same policy decides on the same facts, and only
        what needs no yes runs here. A control that is not found, or not found uniquely, goes to the model, which can look
        further or ask. The reply says what the screen confirmed."""
        started = time.monotonic()
        say = _SAY.get(detect_language(text).code, _SAY["en"])

        def run() -> dict | None:
            decision = policy.classify_gui_action(args, self.desktop.probe(args))
            if decision.blocked or decision.level >= RiskLevel.CONFIRM_REQUIRED:
                return None  # Hermes asks the user, or explains the refusal, through the normal flow
            self.desktop.new_turn()
            return self.desktop.gui(args)

        try:
            result = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 - nothing was confirmed here: the model can look further or ask
            event(log, "FAST_PATH_DEFERRED", reason=str(exc)[:80])
            return None
        if result is None:
            event(log, "FAST_PATH_DEFERRED", reason="needs confirmation")
            return None
        what = str(args.get("element") or args.get("keys") or "")
        changed = result.get("screen_changed")
        if args["action"] == "click":
            line = say["clicked" if changed else "clicked_unconfirmed"].format(what=what)
        elif args["action"] == "key":
            line = say["pressed"].format(key=what)
        else:
            line = say["scrolled"]
        ms = int((time.monotonic() - started) * 1000)
        event(log, "FAST_PATH", steps=1, ok=True, ms=ms, action=args["action"], screen_changed=changed)
        title = f"{args['action']} {what}".strip()
        return FastReply(line, True, [ToolActivity("fast-0", f"fast: {title}", "fast", "completed")],
                         f"[XENIO did this itself just now, without you: {title}]", ms)

    @property
    def autofill(self):
        if self._autofill is None:
            from app.tools import autofill as autofill_tools

            self._autofill = autofill_tools.engine()
        return self._autofill

    @property
    def teacher(self):
        if self._teacher is None:
            from app.tools import teacher as teacher_tool

            self._teacher = teacher_tool.teacher()
        return self._teacher

    async def _teach(self, lesson: dict, text: str) -> FastReply | None:
        """"Teach me X": the lesson with its diagrams, written by Nabh's teaching model and checked, with no turn of the main model. If it
        cannot be written, the main model teaches in conversation."""
        from app.teacher.engine import UnknownSubject

        started = time.monotonic()
        code = detect_language(text).code
        language = {"hi": "Hindi (or Hinglish if the request was in Latin letters)", "de": "German"}.get(code, "")
        say = _SAY.get(code, _SAY["en"])
        if self.progress is not None:
            self.progress(say["lesson_writing"].format(topic=lesson["topic"]) + "\n\n")

        def tell(status: str) -> None:  # a lesson takes a minute: the orb says what is really going on
            if self.on_tool is not None:
                self.on_tool(ToolActivity("fast-lesson", "xenio_teach: writing the lesson", "other", status))

        tell("in_progress")

        def run() -> dict:
            self.teacher.start(lesson["topic"], mode=lesson["mode"], language=language)
            return self.teacher.compose(lesson["topic"], mode=lesson["mode"])

        try:
            made = await asyncio.to_thread(run)
        except UnknownSubject as exc:  # said plainly: a model asked to teach it anyway would make something up
            tell("completed")
            return FastReply(say["lesson_unknown"].format(topic=lesson["topic"], why=str(exc)), True, [], "", int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - nothing was written: the model can still teach, and say why
            tell("failed")
            event(log, "FAST_PATH_DEFERRED", reason=str(exc)[:80])
            return None
        tell("completed")
        line = str(made.get("overview") or made.get("title") or "")
        ms = int((time.monotonic() - started) * 1000)
        event(log, "FAST_PATH", steps=1, ok=True, ms=ms, action="teach", diagrams=len(made.get("diagrams") or []))
        note = f"[XENIO wrote a lesson itself just now, shown as a card: {made.get('title')}.]"
        return FastReply(line, True, [ToolActivity("fast-0", "fast: write a lesson", "fast", "completed")], note, ms, [made["card"]])

    @property
    def images(self):
        if self._images is None:
            from app.tools import images as images_tool

            self._images = images_tool.studio()
        return self._images

    async def _image(self, picture: dict, text: str) -> FastReply | None:
        """"Generate an image of...": a job starts at once and the chat stays free; the picture appears by itself. No turn of the main model."""
        from app.images.studio import StudioError

        started = time.monotonic()
        say = _SAY.get(detect_language(text).code, _SAY["en"])

        def run() -> list[dict] | None:
            if "variations" in picture:
                recent = [j for j in self.images.list(8) if j.get("has_image")]
                return self.images.variation(recent[0]["id"], picture["variations"]) if recent else None
            return [self.images.submit(picture["prompt"], again=i > 0) for i in range(picture["count"])]

        try:
            jobs = await asyncio.to_thread(run)
        except StudioError as exc:
            return FastReply(str(exc), False, [ToolActivity("fast-0", "fast: make a picture", "fast", "failed", str(exc))], "", int((time.monotonic() - started) * 1000))
        if jobs is None:
            return None  # "more variations" with nothing to vary: the model can ask what of
        ms = int((time.monotonic() - started) * 1000)
        event(log, "FAST_PATH", steps=len(jobs), ok=True, ms=ms, action="image")
        note = f"[XENIO started making a picture itself just now, shown in the conversation: {', '.join(j['id'] for j in jobs)}. xenio_image refine or variation can change it; you cannot see it.]"
        return FastReply(say["image_started_many" if len(jobs) > 1 else "image_started"].format(n=len(jobs)), True, [ToolActivity("fast-0", "fast: make a picture", "fast", "completed")], note, ms,
                         [{"kind": "image", "id": j["id"]} for j in jobs])

    async def _form(self, text: str) -> FastReply | None:
        """"Fill this form": read, map, fill, check, all from the page, through its Next pages, with no screenshot and no model,
        so no value leaves the Mac. The same policy decides on the same facts: whatever needs a yes or a refusal goes through
        the normal flow, where it is asked or explained."""
        started = time.monotonic()
        say = _SAY.get(detect_language(text).code, _SAY["en"])
        args = {"action": "fill", "steps": True}

        def run() -> dict | None:
            facts = self.autofill.probe(args)
            if facts.get("probe_error"):
                return None
            decision = policy.classify_form_tool(args, facts)
            if decision.blocked or decision.level >= RiskLevel.CONFIRM_REQUIRED:
                return None
            self.desktop.new_turn()
            return self.autofill.fill(args)

        try:
            result = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001 - nothing was confirmed here: the model can look further or ask
            event(log, "FAST_PATH_DEFERRED", reason=str(exc)[:80])
            return None
        if result is None:
            event(log, "FAST_PATH_DEFERRED", reason="form needs confirmation")
            return None
        filled, failed, left = result["filled"], result["not_filled"], result["left"]
        asks = [i["label"] for i in result.get("questions") or []]
        line = say["form_done"].format(n=len(filled)) if filled else say["form_none"]
        if failed:
            line += " " + say["form_failed"].format(what=", ".join(f["label"] for f in failed[:4]))
        if asks:
            line += " " + say["form_ask"].format(what=", ".join(asks[:6]))
        line += " " + say["form_not_sent"]
        ms = int((time.monotonic() - started) * 1000)
        event(log, "FAST_PATH", steps=len(filled), ok=result["verified"], ms=ms, action="form_fill", failed=len(failed), left=len(left))
        return FastReply(line, bool(result["verified"]), [ToolActivity("fast-0", "fast: fill the form", "fast", "completed" if result["verified"] else "failed")],
                         f"[XENIO filled the form itself just now, without you: {result['say']} xenio_form scan shows its fields.]", ms)

    # ---- reminders and the user's tasks (app/personal/quick.py) ---------------------------------------------------

    def _task_list(self):
        if self._tasks is not None:
            return self._tasks()
        from app.personal.tasks import TaskList

        return TaskList()

    async def _personal(self, text: str) -> FastReply | None:
        from app.personal import quick

        req = quick.parse(text, self._clock())
        if req is None or (req.kind == "remind" and self.reminders is None):
            return None
        started = time.monotonic()
        try:
            reply, title = await asyncio.to_thread(self._do_personal, req, quick)
        except _Defer as exc:
            event(log, "FAST_PATH_DEFERRED", reason=str(exc)[:80])
            return None
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            why = _reason(exc)
            event(log, "FAST_PATH_ACTION", logging.WARNING, action=f"personal.{req.kind}", ok=False, error=why[:120])
            return FastReply(f"That didn't work: {why}", False,
                             [ToolActivity("fast-0", f"fast: {req.kind}", "fast", "failed", why)],
                             ms=int((time.monotonic() - started) * 1000))
        ms = int((time.monotonic() - started) * 1000)
        event(log, "FAST_PATH", steps=1, ok=True, ms=ms, action=f"personal.{req.kind}")
        return FastReply(reply, True, [ToolActivity("fast-0", f"fast: {title}", "fast", "completed")],
                         f"[XENIO did this itself just now, without you: {title}]", ms)

    def _do_personal(self, req, quick) -> tuple[str, str]:
        if req.kind == "remind":
            made = self.reminders.create(req.reminder.text, req.reminder.when)
            return (f"Okay, I'll remind you to {made['text']} {req.reminder.when.display}.",
                    f"reminder set: {made['text']} ({req.reminder.when.display})")
        tasks = self._task_list()
        if req.kind == "add_task":
            task = tasks.add(req.title, priority=req.priority, due=req.due)
            due = f", due {task.due}" if task.due else ""
            return f"Added to your tasks: {task.title}{due}.", f"task added: {task.title}"
        if req.kind == "list_tasks":
            open_tasks = tasks.list("open")
            return quick.describe_tasks(open_tasks), f"listed {len(open_tasks)} open task(s)"
        if req.kind == "complete_task":
            found = [t for t in tasks.find(req.query) if t.status != "done"]
            if len(found) != 1:
                raise _Defer(f"{len(found)} tasks match {req.query!r}")
            done = tasks.complete(found[0].id)
            return f"Marked done: {done.title}.", f"task done: {done.title}"
        raise _Defer(req.kind)

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
