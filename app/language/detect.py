"""Language awareness (V5.5): which language the user spoke, what they asked for, which language to answer in.

Hermes (the main model) understands English, Hindi, Hinglish and German itself; XENIO does not translate anything.
This module only gives it two short hints with the user's words:

* the **language** of this request (English / Hindi or Hinglish / German, mixed or not), so the reply comes back in
  the same language - Hindi in Devanagari, because XENIO's Hindi voice pronounces Devanagari correctly and
  romanized Hindi badly (measured: "Discord khol diya" came back from speech recognition as "Discord call Dia");
* the **normalized intents** for common desktop requests, independent of language:
  "Discord kholo" = "Open Discord" = "Öffne Discord" = "डिस्कॉर्ड खोलो" -> OPEN_APPLICATION(Discord).
  The same intents let XENIO's fast path (``app/fastpath.py``) carry out simple commands itself in well under a
  second ("Tum Hi Ho chalao" -> PLAY_MEDIA, "awaaz badhao" -> VOLUME(up), "agla gaana" -> MEDIA_CONTROL(next)).

Detection is by script (Devanagari), German letters/words and Hinglish words; nothing leaves the machine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN_WORD = re.compile(r"[A-Za-zÄÖÜäöüß]+")
_GERMAN_LETTERS = re.compile(r"[äöüßÄÖÜ]")

# Hinglish (romanized Hindi). Strong words are rarely English; weak ones count only in pairs.
_HI_STRONG = {"kholo", "khol", "kholna", "kholiye", "karo", "kardo", "karna", "kariye", "bhejo", "bhej", "likho", "likh",
              "chalao", "chala", "chalu", "band", "dikhao", "dikha", "batao", "bata", "mujhe", "mera", "meri", "mere",
              "kya", "kaise", "kahan", "kyun", "chahiye", "abhi", "phir", "fir", "aur", "hai", "hain", "nahi", "nahin",
              "haan", "bhai", "yaar", "dhundho", "dhoondo", "suno", "jao", "jaao", "lagao", "hatao", "kijiye", "dijiye",
              "wala", "wali", "accha", "acha", "theek", "thik", "bolo", "bol", "padho", "padh", "usko", "isko", "unko",
              # music, volume and time (the fast path's commands)
              "gaana", "gana", "gaane", "agla", "agle", "pichla", "pichhla", "bajao", "awaaz", "awaz", "aawaz", "badhao",
              "ghatao", "dheere", "sunao", "baje", "kitne"}
_HI_WEAK = {"kar", "do", "de", "ko", "ka", "ki", "ke", "se", "par", "pe", "mein", "main", "to", "toh", "ek", "ye", "wo"}
_HI_PHRASES = re.compile(r"\b(?:kar|kr)\s+(?:do|de|dena|diya|dijiye|lo|le)\b", re.IGNORECASE)  # "open kar do"
# German. Strong words are not English; weak ones count only in pairs.
_DE_STRONG = {"öffne", "öffnen", "offne", "oeffne", "starte", "starten", "schließe", "schließen", "schliesse", "beende",
              "bitte", "suche", "suchen", "schreibe", "schreib", "tippe", "sende", "schicke", "zeige", "zeig", "wechsle",
              "minimiere", "maximiere", "und", "dann", "meinen", "meine", "nach", "danke", "ich", "nicht", "jetzt",
              "geöffnet", "ist", "wie", "spät", "heute", "morgen", "kannst", "du", "mir", "sag"}
_DE_WEAK = {"die", "der", "das", "den", "ein", "eine", "mit", "für", "auf", "zu", "im", "es", "mein"}
_EN_WORDS = {"open", "and", "search", "for", "the", "send", "type", "write", "close", "my", "on", "then", "please",
             "start", "launch", "message", "to", "what", "is", "switch", "go", "back"}

LANGUAGE_NAMES = {"en": "English", "hi": "Hindi/Hinglish", "de": "German"}


@dataclass
class LanguageInfo:
    code: str  # en | hi | de
    mixed: bool = False  # code-switching (e.g. Hinglish with English words, German with English)
    script: str = "latin"  # latin | devanagari | mixed

    @property
    def name(self) -> str:
        return LANGUAGE_NAMES.get(self.code, self.code)


def detect_language(text: str) -> LanguageInfo:
    text = text or ""
    words = [w.lower() for w in _LATIN_WORD.findall(text)]
    english = sum(1 for w in words if w in _EN_WORDS)
    if _DEVANAGARI.search(text):
        latin = len(words)
        return LanguageInfo("hi", mixed=latin > 0, script="mixed" if latin else "devanagari")
    hi_strong = sum(1 for w in words if w in _HI_STRONG) + len(_HI_PHRASES.findall(text))
    hi_weak = sum(1 for w in words if w in _HI_WEAK)
    de_strong = sum(1 for w in words if w in _DE_STRONG) + (2 if _GERMAN_LETTERS.search(text) else 0)
    de_weak = sum(1 for w in words if w in _DE_WEAK)
    hi_score = hi_strong * 2 + (hi_weak if hi_strong else 0)
    de_score = de_strong * 2 + (de_weak if de_strong else 0)
    if hi_score >= 2 and hi_score >= de_score:
        return LanguageInfo("hi", mixed=english > 0)
    if de_score >= 2:
        return LanguageInfo("de", mixed=english > 0 and any(w in ("search", "and", "open", "for") for w in words))
    return LanguageInfo("en")


# ---- normalized intents -------------------------------------------------------------------------------------

@dataclass
class Intent:
    kind: str  # OPEN_APPLICATION | CLOSE_APPLICATION | SWITCH_TO | MINIMIZE | SEARCH | TYPE_TEXT | SEND_MESSAGE | START | STOP
    #            | PLAY_MEDIA | MEDIA_CONTROL (play/pause/next/previous) | VOLUME (up/down/mute/unmute/0-100) | TIME
    target: str = ""
    extra: dict = field(default_factory=dict)

    def describe(self) -> str:
        if self.kind == "MEDIA_CONTROL":
            return {"play": "resume playback", "pause": "pause playback", "next": "play the next track",
                    "previous": "play the previous track"}.get(self.target, self.target)
        if self.kind == "VOLUME":
            return f"set the volume to {self.target}%" if self.target.isdigit() else f"volume {self.target}"
        if self.kind == "TIME":
            return "tell the time"
        what = {"OPEN_APPLICATION": "open", "CLOSE_APPLICATION": "close", "SWITCH_TO": "switch to",
                "MINIMIZE": "minimize", "SEARCH": "search for", "TYPE_TEXT": "type", "SEND_MESSAGE": "send a message",
                "START": "start", "STOP": "stop", "PLAY_MEDIA": "play"}[self.kind]
        if self.kind == "SEND_MESSAGE":
            to = f" to {self.extra['to']!r}" if self.extra.get("to") else ""
            return f"{what}{to}" + (f": {self.target!r}" if self.target else "")
        if self.kind == "TYPE_TEXT" and self.extra.get("to"):
            return f"type {self.target!r} in the chat with {self.extra['to']!r}"
        return f"{what} {self.target!r}" if self.target else what


_SPLIT = re.compile(r"\s*(?:,|\band then\b|\bthen\b|\band\b|\baur\b|\bphir\b|\bfir\b|\bund dann\b|\bund\b|\bdann\b|और|फिर)\s+",
                    re.IGNORECASE)
_LEADING_CONNECTOR = re.compile(r"^(?:then|and|aur|phir|fir|dann|und|और|फिर)\s+", re.IGNORECASE)
_NAME_PREFIX = re.compile(r"^\s*(?:(?:hey|hi|ok|okay)\s+)?[xz]en(?:io|ia|yo)\b[\s,.!]*", re.IGNORECASE)
_STRIP = re.compile(r"^(?:please|bitte|zara|jaldi|my|mera|meri|mere|meinen|meine|mein|the|den|die|das)\s+|"
                    r"\s+(?:please|bitte|app|application|ko|bhi)$", re.IGNORECASE)

_MUSIC = r"(?:the\s+)?(?:music|song|track|video|playback|gaana|gana|gaane|गाना|musik|lied)"
# Media commands with a fixed meaning: kind -> [(value, pattern)]. Checked before "play X", so "gaana chalao" means
# "resume" and never a search for a song called "gaana".
_FIXED: list[tuple[str, str, re.Pattern]] = [(k, v, re.compile(p, re.IGNORECASE)) for k, v, p in [
    ("TIME", "", r"^(?:what(?:'s| is) the time(?: now)?|what time is it(?: now)?|tell me the time|current time|time"
                 r"|(?:abhi )?(?:kya|kitna) time (?:hua )?hai|kitne baje(?: hain| hai)?|time kya hai|टाइम क्या है"
                 r"|कितने बजे (?:हैं|है)|wie spät ist es|wie viel uhr ist es|wieviel uhr ist es)$"),
    ("MEDIA_CONTROL", "pause", rf"^(?:pause(?: {_MUSIC})?|stop {_MUSIC}|{_MUSIC} (?:roko|rok do|band karo|band kar do|"
                               rf"pause karo|pause kar do)|pausieren|musik (?:stoppen|anhalten)|गाना (?:रोको|बंद करो))$"),
    ("MEDIA_CONTROL", "play", rf"^(?:resume(?: {_MUSIC})?|unpause|continue {_MUSIC}|play|play {_MUSIC}|"
                              rf"(?:play|put on) (?:some )?(?:music|songs?|a song)|{_MUSIC} (?:chalao|chala do|chalu karo|"
                              rf"chalu kar do|resume karo|bajao)|weiter(?:spielen)?|fortsetzen|गाना (?:चलाओ|बजाओ))$"),
    ("MEDIA_CONTROL", "next", r"^(?:next|next (?:song|track|one|video)|skip(?: (?:this|the)? ?(?:song|track))?"
                              r"|play the next (?:song|track)|(?:agla|agle|next) (?:gaana|gana|song)(?: chalao| lagao| bajao)?"
                              r"|nächste[rs]? (?:lied|titel|song)|अगला गाना(?: चलाओ| बजाओ)?)$"),
    ("MEDIA_CONTROL", "previous", r"^(?:previous|previous (?:song|track|one)|last song|play the (?:previous|last) (?:song|track)"
                                  r"|(?:pichla|pichhla|peechla) (?:gaana|gana|song)(?: chalao| lagao| bajao)?"
                                  r"|vorherige[rs]? (?:lied|titel|song)|पिछला गाना(?: चलाओ| बजाओ)?)$"),
    ("VOLUME", "up", r"^(?:volume up|turn (?:it |the volume |the sound )?up|louder|increase (?:the )?volume|raise (?:the )?"
                     r"volume|(?:awaaz|awaz|aawaz|volume) (?:badhao|badha do|tez karo|tez kar do|zyada karo|up karo)|lauter"
                     r"|आवाज़? (?:बढ़ाओ|तेज़? करो))$"),
    ("VOLUME", "down", r"^(?:volume down|turn (?:it |the volume |the sound )?down|quieter|softer|lower (?:the )?volume"
                       r"|decrease (?:the )?volume|(?:awaaz|awaz|aawaz|volume) (?:kam karo|kam kar do|dheere karo|ghatao|"
                       r"down karo)|leiser|आवाज़? (?:कम करो|धीरे करो))$"),
    ("VOLUME", "mute", r"^(?:mute(?: (?:the )?(?:sound|volume|audio))?|(?:awaaz|awaz|aawaz) band (?:karo|kar do)|"
                       r"stumm(?:schalten)?|म्यूट करो|आवाज़? बंद करो)$"),
    ("VOLUME", "unmute", r"^(?:unmute(?: (?:the )?(?:sound|volume|audio))?|ton an|आवाज़? (?:चालू|वापस) करो)$"),
]]

# (kind, pattern with a <t> group). Order matters: more specific first.
_PATTERNS: list[tuple[str, re.Pattern]] = [(k, re.compile(p, re.IGNORECASE)) for k, p in [
    ("STOP", r"^(?:stop|abort|cancel|ruko|ruk jao|band karo sab|stopp|halt|abbrechen|रुको|बंद करो सब)[.!]*$()"),
    # volume to a number: "set the volume to 40", "volume 40 kar do", "Lautstärke auf 40"
    ("VOLUME", r"^(?:set (?:the )?volume (?:to|at)|volume (?:to|at)?|lautstärke (?:auf)?)\s*(?P<t>\d{1,3})\s*(?:%|percent|prozent)?"
               r"(?:\s+(?:kar(?:o| do| de)?|karo|stellen))?$"),
    # play: "play Tum Hi Ho", "play believer on youtube", "Tum Hi Ho chalao/bajao", "spiele Believer"
    ("PLAY_MEDIA", r"^(?:play|put on)\s+(?P<t>.+)$"),
    ("PLAY_MEDIA", r"^(?P<t>.+?)\s+(?:play kar(?:o| do| de)?|chalao|chala do|chala de|bajao|baja do|laga do|lagao|sunao|suna do)$"),
    ("PLAY_MEDIA", r"^(?P<t>.+?)\s+(?:चलाओ|बजाओ|प्ले करो|लगाओ|सुनाओ)$"),
    ("PLAY_MEDIA", r"^(?:spiele?|spiel)\s+(?P<t>.+)$"),
    # send: "send Rahul hello" / "Rahul ko hello bhejo" / "sende Rahul hallo" / "message Rahul hello"
    ("SEND_MESSAGE", r"^(?:send|message|text)\s+(?P<to>[\w.@]+)\s+(?P<t>.+)$"),
    ("SEND_MESSAGE", r"^(?P<to>[\w.@]+)\s+ko\s+(?P<t>.+?)\s+(?:bhejo|bhej do|bhej de|send kar(?:o| do)?|message kar(?:o| do)?)$"),
    ("SEND_MESSAGE", r"^(?P<to>\S+)\s+को\s+(?P<t>.+?)\s+(?:भेजो|भेज दो|सेंड करो)$"),
    ("SEND_MESSAGE", r"^(?:sende|schicke)\s+(?P<to>[\w.@]+)\s+(?P<t>.+)$"),
    ("TYPE_TEXT", r"^(?P<to>[\w.@]+)\s+ko\s+(?P<t>.+?)\s+(?:type kar(?:o| do)?|likho|likh do)$"),
    ("TYPE_TEXT", r"^(?P<to>\S+)\s+को\s+(?P<t>.+?)\s+(?:टाइप करो|लिखो|लिख दो)$"),
    ("TYPE_TEXT", r"^(?:type|write)\s+(?P<t>.+)$"),
    ("TYPE_TEXT", r"^(?P<t>.+?)\s+(?:likho|likh do|type kar(?:o| do)?|लिखो|लिख दो|टाइप करो)$"),
    ("TYPE_TEXT", r"^(?:schreibe|schreib|tippe)\s+(?P<t>.+)$"),
    ("SEARCH", r"^(?:search(?:\s+for)?|google|look up)\s+(?P<t>.+)$"),
    ("SEARCH", r"^(?P<t>.+?)\s+(?:search kar(?:o| do)?|dhundho|dhoondo|सर्च करो|खोजो|ढूंढो)$"),
    ("SEARCH", r"^(?:suche(?:\s+nach)?|such(?:\s+nach)?)\s+(?P<t>.+)$"),
    ("SEARCH", r"^nach\s+(?P<t>.+?)\s+suchen$"),
    ("CLOSE_APPLICATION", r"^(?:close|quit|exit)\s+(?P<t>.+)$"),
    ("CLOSE_APPLICATION", r"^(?P<t>.+?)\s+(?:band kar(?:o| do| de)?|close kar(?:o| do)?|बंद करो|बंद कर दो)$"),
    ("CLOSE_APPLICATION", r"^(?:schließe|schliesse|beende)\s+(?P<t>.+)$"),
    ("CLOSE_APPLICATION", r"^(?P<t>.+?)\s+(?:schließen|beenden)$"),
    ("MINIMIZE", r"^(?:minimi[sz]e)\s+(?P<t>.+)$"),
    ("MINIMIZE", r"^(?P<t>.+?)\s+(?:minimize kar(?:o| do)?|chhota karo)$"),
    ("SWITCH_TO", r"^(?:switch(?:\s+back)?\s+to|go\s+back\s+to|go\s+to|focus)\s+(?P<t>.+)$"),
    ("SWITCH_TO", r"^(?P<t>.+?)\s+(?:par|pe)\s+(?:jao|jaao|wapas jao|switch karo)$"),
    ("SWITCH_TO", r"^(?:wechsle\s+zu|geh(?:e)?\s+zu)\s+(?P<t>.+)$"),
    ("START", r"^(?P<t>.+?)\s+(?:start|shuru|शुरू|स्टार्ट)\s+(?:kar(?:o| do| de)?|करो|कर दो)$"),
    ("START", r"^(?:start|begin)\s+(?P<t>recording|streaming|the recording|the stream|a call|a meeting)$"),
    ("OPEN_APPLICATION", r"^(?:open|launch|start|run|bring up)\s+(?:up\s+)?(?P<t>.+)$"),
    ("OPEN_APPLICATION", r"^(?P<t>.+?)\s+(?:kholo|khol do|khol de|kholna|kholiye|open kar(?:o| do| de)?|open karna"
                         r"|chalu kar(?:o| do)?|chalao|start kar(?:o| do)?|launch kar(?:o| do)?)$"),
    ("OPEN_APPLICATION", r"^(?P<t>.+?)\s+(?:खोलो|खोल दो|खोलें|खोलिए|चालू करो|ओपन करो|ओपन कर दो)$"),
    ("OPEN_APPLICATION", r"^(?:öffne|offne|oeffne|starte)\s+(?P<t>.+)$"),
    ("OPEN_APPLICATION", r"^(?P<t>.+?)\s+(?:öffnen|starten)$"),
]]


def _clean_target(text: str) -> str:
    text = text.strip(" .!?,;:\"'«»।")
    for _ in range(3):
        text = _STRIP.sub("", text).strip(" .!?,;:")
    return text


def parse_command(text: str) -> tuple[list[Intent], bool]:
    """Language-independent intents of a request, in order, and whether EVERY clause was understood (the fast path
    acts only on fully understood requests; anything else goes to Hermes, which understands the rest)."""
    body = _NAME_PREFIX.sub("", (text or "").strip())
    intents: list[Intent] = []
    complete = True
    for clause in _SPLIT.split(body):
        clause = _LEADING_CONNECTOR.sub("", clause.strip(" .!?,;:।"))
        if not clause:
            continue
        fixed = next(((kind, value) for kind, value, pattern in _FIXED if pattern.match(clause)), None)
        if fixed:
            intents.append(Intent(fixed[0], fixed[1]))
            continue
        understood = False
        for kind, pattern in _PATTERNS:
            m = pattern.match(clause)
            if not m:
                continue
            groups = m.groupdict()
            if kind == "STOP":
                intents.append(Intent("STOP"))
                understood = True
            else:
                target = _clean_target(groups.get("t") or "")
                if not target:
                    continue
                understood = True
                extra = {"to": _clean_target(groups["to"])} if groups.get("to") else {}
                if kind == "TYPE_TEXT" and intents and intents[-1].kind == "SEND_MESSAGE" and not intents[-1].target:
                    intents[-1].target = target
                    break
                intents.append(Intent(kind, target, extra))
            break
        complete = complete and understood
    return intents, complete and bool(intents)


def parse_intents(text: str) -> list[Intent]:
    """Language-independent intents of a request, in order. Unknown clauses are skipped (Hermes understands them)."""
    return parse_command(text)[0]


def language_note(text: str, info: LanguageInfo | None = None) -> str:
    """The hint added to a request for Hermes ('' for plain English without recognised intents)."""
    info = info or detect_language(text)
    parts = []
    if info.code == "hi":
        parts.append("[XENIO language: the user spoke Hindi/Hinglish" + (" mixed with English" if info.mixed else "")
                     + ". Treat it as ONE request. Reply in Hindi written in Devanagari script, keeping app, product and "
                       "proper names (Discord, Chrome, GitHub) in English letters; short and natural, like a person.]")
    elif info.code == "de":
        parts.append("[XENIO language: the user spoke German" + (" mixed with English" if info.mixed else "")
                     + ". Treat it as ONE request. Reply in German, short and natural.]")
    intents = parse_intents(text)
    if intents and info.code != "en":
        parts.append("[XENIO understood: " + "; then ".join(i.describe() for i in intents) + ".]")
    return "\n".join(parts)
