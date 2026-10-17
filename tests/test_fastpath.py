"""The fast path (speed): simple commands done by XENIO itself, with the same policy, honest replies, and Hermes for
everything else. Fake desktop and music: nothing is opened, played or changed."""

import asyncio
import datetime as dt
import subprocess

import pytest

from app.desktop import apps as A
from app.fastpath import FastPath
from app.hermes.client import HermesReply
from app.media import MediaError, Music, Track, split_service
from app.orchestrator import Orchestrator
from app.safety import policy as p


class FakeDesktop:
    def __init__(self, entries):
        self.catalog = A.AppCatalog(None, loader=lambda: list(entries))
        self.opened, self.windowed, self.turns = [], [], 0
        self.fail_open = False

    def resolve_app(self, name):
        return self.catalog.resolve(name)

    def open_app(self, name, wait):
        entry = self.catalog.resolve(name).best
        self.opened.append(entry.name)
        if self.fail_open:
            return {"app": entry.name, "verified": False}
        return {"app": entry.name, "verified": True, "how": "new_window"}

    def window(self, action, name):
        self.windowed.append((action, name))
        return {"window": f"Friends - {name.title()}", "verified": True}

    def new_turn(self):
        self.turns += 1


class FakeMusic:
    def __init__(self, service="youtube"):
        self.service, self.calls = service, []

    def choose(self, hint=""):
        if hint == "spotify" and self.service != "spotify":
            raise MediaError("Spotify search is not set up yet")
        return hint or self.service

    def find(self, query, service):
        self.calls.append(("find", query, service))
        if service == "spotify":
            return Track("Believer", "spotify", "spotify:track:abc", "Imagine Dragons")
        return Track("Imagine Dragons - Believer (Official Music Video)", "youtube", "https://www.youtube.com/watch?v=7wtfhZwyrcc")

    def play_spotify(self, track):
        self.calls.append(("spotify", track.url))
        return {"verified": True, "now_playing": "Believer by Imagine Dragons"}

    def control(self, action):
        self.calls.append(("control", action))
        return {"player": "Spotify", "verified": True, "now_playing": "Thunder"}

    def volume(self, change):
        self.calls.append(("volume", change))
        return {"volume": 40 if change.isdigit() else 60, "muted": change == "mute", "verified": True}

    def warm(self):
        pass


CATALOG = [A.AppEntry("Spotify", "com.spotify.client", "/Applications/Spotify.app", "applications"),
           A.AppEntry("Discord", "com.hnc.Discord", "/Applications/Discord.app", "applications"),
           A.AppEntry("Safari", "com.apple.Safari", "/Applications/Safari.app", "applications"),
           A.AppEntry("Install Thing", "x.install", "/Users/me/Downloads/Install Thing.app", "applications")]


@pytest.fixture
def fast(tmp_path, monkeypatch):
    opened_urls = []
    from app.tools import web

    monkeypatch.setattr(web, "OPENER", lambda url: opened_urls.append(url) or True)
    paths = p.PathPolicy(workspace=tmp_path)
    fp = FastPath(paths, music=FakeMusic(), desktop=FakeDesktop(CATALOG), clock=lambda: dt.datetime(2026, 10, 17, 20, 42))
    fp.opened_urls = opened_urls
    return fp


def run(coro):
    return asyncio.run(coro)


def test_open_an_app_is_one_verified_action(fast):
    reply = run(fast.handle("Hey Xenio, open Spotify"))
    assert reply.text == "Spotify is open." and fast.desktop.opened == ["Spotify"]
    assert [a.status for a in reply.actions] == ["completed"] and "opened" not in reply.note.lower() or "open Spotify" in reply.note


def test_hinglish_and_german_get_replies_in_their_language(fast):
    assert run(fast.handle("Discord kholo")).text == "Discord खुल गया।"
    assert run(fast.handle("Öffne Discord")).text == "Discord ist offen."


def test_play_a_song_opens_exactly_one_tab(fast):
    reply = run(fast.handle("play believer by imagine dragons"))
    assert fast.opened_urls == ["https://www.youtube.com/watch?v=7wtfhZwyrcc"]  # one tab, the video itself
    assert reply.text.startswith("Opened Imagine Dragons - Believer") and "YouTube" in reply.text


def test_open_spotify_and_play_lets_spotify_play_it(fast):
    fast.music.service = "spotify"
    reply = run(fast.handle("open spotify and play believer"))
    assert ("spotify", "spotify:track:abc") in fast.music.calls and fast.desktop.opened == []  # Spotify opens itself
    assert reply.text == "Playing Believer by Imagine Dragons."


def test_asking_for_spotify_without_keys_says_why_and_never_switches_service(fast):
    reply = run(fast.handle("play believer on spotify"))
    assert "not set up" in reply.text and fast.opened_urls == [] and reply.actions[0].status == "failed"


def test_media_volume_and_time(fast):
    assert run(fast.handle("agla gaana")).text == "अगला गाना: Thunder।"
    assert run(fast.handle("pause")).text == "Paused."
    assert run(fast.handle("volume 40 kar do")).text == "आवाज़ 40% पर है।"
    assert run(fast.handle("mute")).text == "Muted."
    assert run(fast.handle("what time is it")).text == "It's 8:42 PM."
    assert ("volume", "40") in fast.music.calls and ("control", "next") in fast.music.calls


def test_websites_open_when_no_app_has_that_name(fast):
    assert run(fast.handle("open youtube")).text == "Opened Youtube." and fast.opened_urls == ["https://www.youtube.com"]


@pytest.mark.parametrize("text", [
    "what is the capital of France",  # a question: Hermes
    "open spotify and tell me a joke",  # partly understood: Hermes takes all of it
    "send Rahul hello",  # sending is never a fast-path action
    "search for flights to Delhi",
    "open photoshop",  # not installed, not a known site: Hermes can ask
    "play salt and pepper",  # "and" inside a song name: not fully understood, so not guessed
])
def test_everything_else_goes_to_hermes(fast, text):
    assert run(fast.handle(text)) is None


def test_actions_that_need_a_yes_go_to_hermes(fast):
    """An app started from Downloads needs confirmation: the fast path never asks, Hermes does (one flow)."""
    assert run(fast.handle("open install thing")) is None and fast.desktop.opened == []


def test_unverified_is_never_reported_as_done(fast):
    fast.desktop.fail_open = True
    reply = run(fast.handle("open discord"))
    assert reply.text.startswith("That didn't work") and reply.actions[0].status == "failed"
    assert "failed" in reply.note


def test_disabled_fast_path_does_nothing(fast):
    fast.enabled = False
    assert run(fast.handle("open spotify")) is None


def test_media_policy_never_needs_more_than_safe_write():
    for action in ("play", "pause", "next", "previous", "volume", "play_track"):
        assert p.classify_media_action(action, "x").level is p.RiskLevel.SAFE_WRITE
    assert p.classify_media_action("time").level is p.RiskLevel.READ
    assert p.classify_media_action("format disk").blocked


# ---- the orchestrator: fast replies, and Hermes is told what happened -------------------------------------------

class FakeHermes:
    def __init__(self):
        self.messages = []

    def set_permission_handler(self, handler):
        pass

    async def send_message(self, message, on_chunk=None, on_tool=None):
        self.messages.append(message)
        return HermesReply("Sure.")


def test_orchestrator_uses_the_fast_path_and_tells_hermes_next_turn(fast, tmp_path):
    hermes = FakeHermes()
    orch = Orchestrator(hermes, None, fast.paths, sources_file=tmp_path / "s.jsonl", fast_path=fast)
    chunks = []
    reply = run(orch.process_text("open spotify", on_chunk=chunks.append))
    assert reply.ok and reply.text == "Spotify is open." and hermes.messages == [] and chunks == ["Spotify is open."]
    assert reply.action_lines()[1].strip().startswith("[done] fast: open Spotify")
    run(orch.process_text("who made that app?"))
    assert "XENIO did this itself" in hermes.messages[0] and "open Spotify" in hermes.messages[0]
    run(orch.process_text("thanks"))
    assert "XENIO did this itself" not in hermes.messages[1]  # told once


# ---- music helpers ----------------------------------------------------------------------------------------------

def test_service_hints_are_split_off_the_song():
    assert split_service("believer on spotify") == ("believer", "spotify")
    assert split_service("tum hi ho youtube par") == ("tum hi ho", "youtube")
    assert split_service("kesariya") == ("kesariya", "")


def test_music_chooses_spotify_only_when_it_is_set_up():
    plain = Music(spotify_installed=True)
    assert plain.choose() == "youtube"
    with pytest.raises(MediaError, match="SPOTIFY_CLIENT_ID"):
        plain.choose("spotify")


def test_macos_volume_is_read_back(monkeypatch):
    seen = []

    def fake(cmd, **kw):
        seen.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "40|false\n", "")

    monkeypatch.setattr("sys.platform", "darwin")
    out = Music(runner=fake).volume("40")
    assert out == {"volume": 40, "muted": False, "verified": True} and "set volume output volume 40" in seen[0]


def test_denied_automation_names_the_setting(monkeypatch):
    def denied(cmd, **kw):
        if cmd[0].endswith("pgrep"):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "execution error: Not authorized to send Apple events to Spotify. (-1743)")

    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(MediaError, match="Automation"):
        Music(runner=denied).control("pause")
