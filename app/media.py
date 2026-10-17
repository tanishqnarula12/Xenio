"""Music and sound for XENIO's fast path: play a song in one step, pause / resume / next / previous, the volume.

Speed comes from doing exactly one thing per request, with no model in the loop:

* **play a song**: one lookup, one action. YouTube's own search API finds the video (~0.8 s, a warm connection is
  kept) and exactly ONE browser tab opens it. With Spotify keys (a free developer app: ``SPOTIFY_CLIENT_ID`` /
  ``SPOTIFY_CLIENT_SECRET``), Spotify's search API finds the track (~0.2 s) and the Spotify app plays it, verified
  by reading back what is playing;
* **pause / resume / next / previous**: sent to the player that is running (Spotify or Music, through AppleScript,
  and verified), otherwise the system media keys (any player, e.g. YouTube in a browser; not verifiable);
* **volume**: the system output volume, read back after every change.

Nothing here decides what is allowed: ``app/fastpath.py`` classifies every action with XENIO's policy first.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import httpx

from app.config import Secret

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
       "Safari/605.1.15")
_YT_SEARCH = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
_YT_VIDEOS_ONLY = "EgIQAQ=="  # the "Videos" filter: no channels, playlists or shorts shelves first
_YT_HIT = re.compile(rb'"videoRenderer":\{"videoId":"([\w-]{11})".{0,3000}?"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"',
                     re.S)
_YT_CLIENT_VERSION = "2.20251015.01.00"
VOLUME_STEP = 10

# "play X on spotify" / "X youtube par chalao" / "spiele X auf Spotify"
_SERVICE_HINT = re.compile(r"\s+(?:on|in|from|auf|par|pe|per)\s+(?P<s>spotify|youtube(?: music)?|apple music|yt)$|"
                           r"\s+(?P<s2>spotify|youtube) (?:par|pe)$", re.IGNORECASE)


class MediaError(Exception):
    pass


@dataclass
class Track:
    title: str
    service: str  # youtube | spotify
    url: str  # https://www.youtube.com/watch?v=... or spotify:track:...
    artist: str = ""

    @property
    def label(self) -> str:
        return f"{self.title} by {self.artist}" if self.artist else self.title


def split_service(query: str) -> tuple[str, str]:
    """("believer on spotify") -> ("believer", "spotify"); no hint -> (query, "")."""
    m = _SERVICE_HINT.search(query.strip())
    if not m:
        return query.strip(), ""
    word = (m.group("s") or m.group("s2") or "").lower()
    service = "spotify" if word == "spotify" else "music" if word == "apple music" else "youtube"
    return query[:m.start()].strip(), service


def _osascript(script: str, timeout: float = 6.0, runner=subprocess.run) -> str:
    done = runner(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        err = (done.stderr or "").strip()
        app = re.search(r'application "([^"]+)"', script)
        if "-1743" in err or "not allowed" in err.lower():
            name = app.group(1) if app else "that app"
            raise MediaError(f"macOS has not allowed XENIO to control {name}. Open System Settings > Privacy & Security "
                             f"> Automation and switch on {name} for the app XENIO runs in (Terminal), then ask again.")
        raise MediaError(err[-200:] or f"osascript exited with {done.returncode}")
    return done.stdout.strip()


def _running(app: str, runner=subprocess.run) -> bool:
    return runner(["/usr/bin/pgrep", "-xq", app], capture_output=True, timeout=3).returncode == 0


class Music:
    def __init__(self, service: str = "auto", spotify_id: str = "", spotify_secret: Secret | None = None,
                 client: httpx.Client | None = None, runner=subprocess.run, spotify_installed=None):
        self.service = (service or "auto").lower()
        self._spotify_id = spotify_id
        self._spotify_secret = spotify_secret or Secret("")
        self._client = client
        self._run = runner
        self._spotify_installed = spotify_installed
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    # ---- connections ------------------------------------------------------------------------------------------

    def client(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                # kept open: a warm connection saves the TLS handshake on every song
                self._client = httpx.Client(timeout=httpx.Timeout(6.0, connect=3.0), follow_redirects=True,
                                            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
                                            limits=httpx.Limits(keepalive_expiry=300))
            return self._client

    def warm(self) -> None:
        """Open the connection in the background (called when XENIO starts), so the first song is fast too."""
        def run():
            try:
                self.client().head("https://www.youtube.com/")
            except httpx.HTTPError:
                pass
        threading.Thread(target=run, name="xenio-media-warm", daemon=True).start()

    # ---- which service --------------------------------------------------------------------------------------------

    @property
    def spotify_ready(self) -> bool:
        return bool(self._spotify_id and self._spotify_secret) and sys.platform == "darwin" and self.spotify_installed

    @property
    def spotify_installed(self) -> bool:
        if self._spotify_installed is None:
            self._spotify_installed = any(os.path.isdir(p) for p in ("/Applications/Spotify.app",
                                                                     os.path.expanduser("~/Applications/Spotify.app")))
        return bool(self._spotify_installed)

    def choose(self, hint: str = "") -> str:
        """The service for a song: what the user said, else XENIO_MUSIC_SERVICE, else Spotify when it is set up."""
        wanted = hint or (self.service if self.service != "auto" else "")
        if wanted == "spotify":
            if not self.spotify_ready:
                raise MediaError("Spotify search is not set up yet: add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET (a "
                                 "free app at developer.spotify.com) to XENIO's .env, and the Spotify app must be installed.")
            return "spotify"
        if wanted in ("youtube", ""):
            return "spotify" if not wanted and self.spotify_ready else "youtube"
        raise MediaError(f"XENIO cannot search {wanted} for songs; it plays from YouTube or Spotify.")

    # ---- finding a song ---------------------------------------------------------------------------------------------

    def find(self, query: str, service: str) -> Track:
        if not query.strip():
            raise MediaError("which song?")
        return self._spotify(query) if service == "spotify" else self._youtube(query)

    def _youtube(self, query: str) -> Track:
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": _YT_CLIENT_VERSION, "hl": "en"}},
                "query": query, "params": _YT_VIDEOS_ONLY}
        buf = b""
        try:
            with self.client().stream("POST", _YT_SEARCH, json=body) as r:
                if r.status_code != 200:
                    raise MediaError(f"YouTube search answered {r.status_code}")
                for chunk in r.iter_bytes(16384):  # the first result arrives in the first few KB: stop there
                    buf += chunk
                    m = _YT_HIT.search(buf)
                    if m:
                        title = json.loads(b'"' + m.group(2) + b'"')
                        return Track(" ".join(title.split()), "youtube",
                                     f"https://www.youtube.com/watch?v={m.group(1).decode()}")
                    if len(buf) > 2_000_000:
                        break
        except httpx.HTTPError as exc:
            raise MediaError(f"YouTube could not be searched ({type(exc).__name__})") from None
        raise MediaError(f"YouTube found no video for {query!r}")

    def _spotify_bearer(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        basic = base64.b64encode(f"{self._spotify_id}:{self._spotify_secret.reveal()}".encode()).decode()
        try:
            r = self.client().post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                                   headers={"Authorization": f"Basic {basic}"})
        except httpx.HTTPError as exc:
            raise MediaError(f"Spotify could not be reached ({type(exc).__name__})") from None
        if r.status_code != 200:
            raise MediaError("Spotify refused XENIO's keys (check SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET)")
        data = r.json()
        self._token = (data["access_token"], time.time() + float(data.get("expires_in", 3600)))
        return self._token[0]

    def _spotify(self, query: str) -> Track:
        try:
            r = self.client().get("https://api.spotify.com/v1/search", params={"q": query, "type": "track", "limit": 1},
                                  headers={"Authorization": f"Bearer {self._spotify_bearer()}"})
        except httpx.HTTPError as exc:
            raise MediaError(f"Spotify could not be searched ({type(exc).__name__})") from None
        items = (r.json().get("tracks") or {}).get("items") or [] if r.status_code == 200 else []
        if not items:
            raise MediaError(f"Spotify found no song for {query!r}")
        t = items[0]
        return Track(t["name"], "spotify", t["uri"], ", ".join(a["name"] for a in t.get("artists", [])[:2]))

    # ---- playing --------------------------------------------------------------------------------------------------

    def play_spotify(self, track: Track) -> dict:
        """Start ``track`` in the Spotify app and read back what is playing (opens Spotify when it is closed)."""
        out = _osascript(f'tell application "Spotify"\nplay track "{track.url}"\ndelay 0.6\n'
                         'return (player state as string) & "|" & (name of current track) & "|" & '
                         '(artist of current track)\nend tell', timeout=15, runner=self._run)
        state, _, rest = out.partition("|")
        name, _, artist = rest.partition("|")
        return {"player": "Spotify", "verified": state == "playing" and name.strip().lower() == track.title.lower(),
                "state": state, "now_playing": f"{name} by {artist}" if artist else name}

    def control(self, action: str) -> dict:
        """play | pause | next | previous, for whatever is playing."""
        if sys.platform != "darwin":
            return self._keys({"play": "playpause", "pause": "playpause", "next": "medianext",
                               "previous": "mediaprev"}[action])
        verbs = {"play": "play", "pause": "pause", "next": "next track", "previous": "previous track"}
        for app in ("Spotify", "Music"):
            if _running(app, self._run):
                out = _osascript(f'tell application "{app}"\n{verbs[action]}\ndelay 0.35\n'
                                 'return (player state as string) & "|" & (name of current track)\nend tell',
                                 runner=self._run)
                state, _, name = out.partition("|")
                ok = state == ("paused" if action == "pause" else "playing")
                return {"player": app, "verified": ok, "state": state, "now_playing": name}
        from app.desktop import macos

        if not macos.trusted():
            raise MediaError("Nothing is playing in Spotify or Music, and the system media keys need the "
                             "Accessibility permission. " + macos.ACCESSIBILITY_STEP)
        macos.press_media_key({"play": macos.NX_PLAY, "pause": macos.NX_PLAY, "next": macos.NX_NEXT,
                               "previous": macos.NX_PREVIOUS}[action])
        return {"player": "media keys", "verified": None}

    def _keys(self, key: str) -> dict:
        from app.desktop.input import DesktopController

        DesktopController().hotkey(key)
        return {"player": "media keys", "verified": None}

    # ---- volume ---------------------------------------------------------------------------------------------------

    def volume(self, change: str) -> dict:
        """up | down | mute | unmute | "0".."100". Returns the volume read back afterwards."""
        if sys.platform != "darwin":
            if change.isdigit():
                raise MediaError("setting an exact volume is supported on macOS only")
            return self._keys({"up": "volumeup", "down": "volumedown", "mute": "volumemute", "unmute": "volumemute"}[change])
        read = 'return (output volume of (get volume settings) as string) & "|" & (output muted of (get volume settings) as string)'
        if change.isdigit():
            script = f"set volume output volume {max(0, min(int(change), 100))} without output muted\n{read}"
        elif change in ("up", "down"):
            sign = "+" if change == "up" else "-"
            script = (f"set v to output volume of (get volume settings)\nset volume output volume (v {sign} {VOLUME_STEP})"
                      f"{' without output muted' if change == 'up' else ''}\n{read}")
        elif change in ("mute", "unmute"):
            script = f"set volume {'with' if change == 'mute' else 'without'} output muted\n{read}"
        else:
            raise MediaError(f"unknown volume change {change!r}")
        level, _, muted = _osascript(script, runner=self._run).partition("|")
        return {"volume": int(level) if level.isdigit() else None, "muted": muted == "true", "verified": True}
