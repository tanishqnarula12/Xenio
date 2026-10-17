"""Application discovery, resolution and launch (V5.5).

Nothing is hardcoded per application. The catalog is what Windows itself offers to start:

* **Start menu apps** (``Get-StartApps``): every app in the Start menu, desktop and Store/packaged alike (Discord,
  OBS, Claude, ChatGPT, WhatsApp, VS Code, Calculator ...), with the AppUserModelID Windows launches it by;
* **App Paths** (registry): programs registered by executable name (``chrome.exe``, ``msedge.exe`` ...);
* **PATH**: a program name typed exactly (``notepad``, ``mspaint``) when it is on the PATH.

``resolve(name)`` scores the catalog against the spoken name (exact name, then words, then close spellings, plus a
few everyday categories such as "browser" or "code editor"). Several equally good matches are reported as
ambiguous so the user is asked; nothing is picked at random. ``launch()`` starts the app the way the Start menu does
(``shell:AppsFolder\\<AppUserModelID>``) and then **verifies** that one of its windows really appeared.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

CACHE_SECONDS = 600
CACHE_FILE = "apps_cache.json"

# Known folder GUIDs used in Start-menu AppIDs ("{6D809377-...}\\App\\app.exe").
_KNOWN_FOLDERS = {
    "{6D809377-6AF0-444B-8957-A3773F02200E}": "ProgramFiles", "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}": "ProgramFiles(x86)",
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}": r"%WINDIR%\System32", "{F38BF404-1D43-42F2-9305-67DE0B28FC23}": "%WINDIR%",
    "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}": "%LOCALAPPDATA%", "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}": "%APPDATA%",
    "{5E6C858F-0E22-4760-9AFE-EA3317B67173}": "%USERPROFILE%", "{374DE290-123F-4565-9164-39C4925E467B}": r"%USERPROFILE%\Downloads",
}

# Everyday categories -> words that identify such apps. Used only when no app is literally named like that.
CATEGORIES = {
    "browser": ["chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium"],
    "web browser": ["chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium"],
    "code editor": ["visual studio code", "vs code", "cursor", "sublime", "notepad++", "pycharm", "intellij", "zed"],
    "editor": ["visual studio code", "cursor", "sublime", "notepad++", "notepad"],
    "ide": ["visual studio code", "pycharm", "intellij", "visual studio", "android studio"],
    "streaming app": ["obs"], "recording app": ["obs"], "screen recorder": ["obs", "snipping tool"],
    "music app": ["spotify", "apple music", "media player"], "music": ["spotify", "apple music", "media player"],
    "chat app": ["whatsapp", "discord", "telegram", "teams", "slack", "signal"],
    "terminal": ["terminal", "powershell", "command prompt"], "command line": ["terminal", "powershell", "command prompt"],
    "file manager": ["file explorer"], "files": ["file explorer"], "my files": ["file explorer"],
    "calculator": ["calculator"], "notes": ["notepad", "sticky notes", "onenote"], "mail": ["outlook", "mail"],
    "email": ["outlook", "mail", "thunderbird"], "camera": ["camera"], "settings": ["settings"],
}
_ALIASES = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
            "explorer": "file explorer", "cmd": "command prompt", "calc": "calculator", "obs": "obs studio",
            "teams": "microsoft teams", "edge": "microsoft edge", "chrome": "google chrome", "word": "word",
            "excel": "excel", "powerpoint": "powerpoint", "gpt": "chatgpt", "chat gpt": "chatgpt", "whats app": "whatsapp"}
_FILLER = {"the", "my", "app", "application", "program", "desktop", "for", "windows", "please", "a", "an", "mera", "meri",
           "mein", "meinen", "meine", "die", "das", "der", "den", "app?"}
_RISKY_NAME = re.compile(r"\b(uninstall|uninstaller|setup|installer|install|update|updater|repair|remove)\b", re.I)
_UNTRUSTED_DIRS = re.compile(r"\\(downloads|temp|tmp|appdata\\local\\temp|inetcache|content\.outlook)\\", re.I)


class AppError(Exception):
    pass


@dataclass
class AppEntry:
    name: str
    app_id: str = ""  # AppUserModelID / Start-menu id (launch via shell:AppsFolder)
    path: str = ""  # executable path when known
    source: str = "start_menu"  # start_menu | app_paths | path
    kind: str = "desktop"  # desktop | packaged

    @property
    def exe_stem(self) -> str:
        target = self.path or (self.app_id if self.app_id.lower().endswith(".exe") else "")
        return Path(target).stem.lower() if target else ""

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class Resolution:
    query: str
    matches: list[tuple[float, AppEntry]] = field(default_factory=list)

    @property
    def best(self) -> AppEntry | None:
        return self.matches[0][1] if self.matches else None

    @property
    def confidence(self) -> float:
        return self.matches[0][0] if self.matches else 0.0

    @property
    def ambiguous(self) -> bool:
        if len(self.matches) < 2:
            return False
        top, second = self.matches[0][0], self.matches[1][0]
        return second >= top - 0.05 and self.matches[0][1].name.lower() != self.matches[1][1].name.lower()

    def candidates(self, limit: int = 4) -> list[str]:
        best = self.confidence
        return [e.name for s, e in self.matches if s >= best - 0.05][:limit]

    def describe(self) -> dict:
        e = self.best
        launch = "shell:AppsFolder" if e and e.app_id else "executable" if e else ""
        return {"name": e.name if e else "", "launch_method": launch, "target": (e.app_id or e.path) if e else "",
                "source": e.source if e else "", "confidence": round(self.confidence, 2)}


def _expand_app_id_path(app_id: str) -> str:
    """A Start-menu id that is really a file path (with a known-folder GUID prefix) -> the path, else ''."""
    m = re.match(r"^(\{[0-9A-Fa-f-]{36}\})\\(.+)$", app_id)
    if m and m.group(1).upper() in _KNOWN_FOLDERS:
        base = _KNOWN_FOLDERS[m.group(1).upper()]
        base = os.environ.get(base, base) if not base.startswith("%") else os.path.expandvars(base)
        return os.path.join(base, m.group(2))
    if re.match(r"^[A-Za-z]:\\", app_id):
        return app_id
    return ""


def _start_apps(runner=subprocess.run) -> list[AppEntry]:
    done = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
                  capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    raw = (done.stdout or b"").decode("utf-8", "replace").strip()
    data = json.loads(raw) if raw else []
    entries = []
    for item in data if isinstance(data, list) else [data]:
        name, app_id = str(item.get("Name") or "").strip(), str(item.get("AppID") or "").strip()
        if not name or not app_id or app_id.lower().startswith(("http:", "https:")):
            continue
        entries.append(AppEntry(name, app_id, _expand_app_id_path(app_id), "start_menu",
                                "packaged" if "!" in app_id else "desktop"))
    return entries


def _app_paths() -> list[AppEntry]:
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []
    out = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    exe = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, exe) as sub:
                        path = str(winreg.QueryValueEx(sub, "")[0]).strip('"')
                except OSError:
                    continue
                if exe.lower().endswith(".exe") and path:
                    out.append(AppEntry(Path(exe).stem, "", os.path.expandvars(path), "app_paths"))
    return out


class AppCatalog:
    def __init__(self, cache_dir: Path | None = None, loader=None, clock=time.time):
        self._cache_dir = cache_dir
        self._loader = loader or (lambda: _start_apps() + _app_paths())
        self._clock = clock
        self._entries: list[AppEntry] = []
        self._loaded_at = 0.0

    def _cache_path(self) -> Path | None:
        return self._cache_dir / CACHE_FILE if self._cache_dir else None

    def entries(self, refresh: bool = False) -> list[AppEntry]:
        now = self._clock()
        if not refresh and self._entries and now - self._loaded_at < CACHE_SECONDS:
            return self._entries
        path = self._cache_path()
        if not refresh and not self._entries and path and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if now - float(data.get("at", 0)) < CACHE_SECONDS:
                    self._entries, self._loaded_at = [AppEntry(**e) for e in data.get("apps", [])], float(data["at"])
                    return self._entries
            except (OSError, ValueError, TypeError, KeyError):
                pass
        self._entries, self._loaded_at = dedupe(self._loader()), now
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"at": now, "apps": [asdict(e) for e in self._entries]}), encoding="utf-8")
            except OSError:
                pass
        return self._entries

    def search(self, query: str, limit: int = 10) -> list[AppEntry]:
        res = resolve(query, self.entries())
        if not res.matches:
            res = resolve(query, self.entries(refresh=True))
        return [e for _s, e in res.matches[:limit]]

    def resolve(self, query: str) -> "Resolution":
        res = resolve(query, self.entries())
        if not res.matches or res.confidence < 0.8:  # maybe installed since the catalog was read
            fresh = resolve(query, self.entries(refresh=True))
            if fresh.confidence > res.confidence:
                res = fresh
        if not res.matches:
            exe = _on_path(query)
            if exe:
                res = Resolution(query, [(0.9, AppEntry(Path(exe).stem, "", exe, "path"))])
        return res


def dedupe(entries: list[AppEntry]) -> list[AppEntry]:
    """App Paths list many Start-menu apps again under their executable name ("chrome" = Google Chrome, "code" =
    Visual Studio Code); keep only the Start-menu entry so one app is never "ambiguous" with itself."""
    start = [e for e in entries if e.source == "start_menu"]
    start_names = {normalize_name(e.name) for e in start}
    start_paths = {os.path.normcase(e.path) for e in start if e.path}
    start_ids = {e.app_id.lower() for e in start}
    out = list(start)
    for e in entries:
        if e.source == "start_menu":
            continue
        if (normalize_name(e.name) in start_names or os.path.normcase(e.path) in start_paths
                or e.name.lower() in start_ids):
            continue
        out.append(e)
    return out


def default_browser() -> str:
    """The user's default browser as an app word ("chrome", "edge", ...), from Windows' https association."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
                                                      r"\https\UserChoice") as key:
            prog = str(winreg.QueryValueEx(key, "ProgId")[0]).lower()
    except (OSError, ImportError):
        return ""
    for word, browser in (("chrome", "chrome"), ("msedge", "edge"), ("brave", "brave"), ("firefox", "firefox"),
                          ("opera", "opera"), ("vivaldi", "vivaldi")):
        if prog.startswith(word):
            return browser
    return ""


def _on_path(query: str) -> str:
    word = query.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.+-]{2,40}", word):
        return ""
    return shutil.which(word if word.endswith(".exe") else word + ".exe") or ""


def normalize_name(text: str) -> str:
    words = [w for w in re.split(r"[^\w+]+", (text or "").lower()) if w and w not in _FILLER]
    joined = " ".join(words)
    return _ALIASES.get(joined, joined)


def _compact(text: str) -> str:
    return re.sub(r"[^\w+]", "", (text or "").lower())


def _score(query: str, entry: AppEntry, raw: str = "") -> float:
    name = normalize_name(entry.name)
    if not query or not name:
        return 0.0
    if query == name or (raw and _compact(raw) == _compact(entry.name)):  # "whats app" = WhatsApp
        return 1.0
    qwords, nwords = query.split(), name.split()
    if name.startswith(query + " ") or name.endswith(" " + query) or (len(qwords) == 1 and qwords[0] in nwords):
        return 0.92 if len(nwords) <= 3 else 0.85
    if all(w in nwords for w in qwords):
        return 0.85
    if entry.exe_stem and query.replace(" ", "") == entry.exe_stem:
        return 0.9
    ratio = difflib.SequenceMatcher(None, query, name).ratio()
    if ratio >= 0.85:
        return 0.8 * ratio
    if len(qwords) == 1 and len(query) >= 4:
        close = max((difflib.SequenceMatcher(None, query, w).ratio() for w in nwords), default=0)
        if close >= 0.85:
            return 0.7 * close
    return 0.0


_CATEGORY_INDEX = {normalize_name(k): v for k, v in CATEGORIES.items()}


def resolve(query: str, entries: list[AppEntry], browser: str | None = None) -> Resolution:
    q = normalize_name(query)
    scored = [(s, e) for e in entries if (s := _score(q, e, query)) > 0]
    hints = _CATEGORY_INDEX.get(q)
    if not scored and hints:  # "open the streaming app" -> apps of that kind
        preferred = ""
        if q in ("browser", "web browser"):  # "open the browser" = the default browser, not a question
            preferred = default_browser() if browser is None else browser
        for rank, hint in enumerate(hints):
            for e in entries:
                if _score(normalize_name(hint), e) >= 0.85 and not any(e is x for _s, x in scored):
                    is_default = bool(preferred) and _score(normalize_name(preferred), e) >= 0.85
                    scored.append((0.97 if is_default else 0.9 - 0.001 * rank, e))
    if not _RISKY_NAME.search(query):  # "Telegram" means the app, not "Uninstall Telegram"
        scored = [(s * 0.85 if _RISKY_NAME.search(e.name) else s, e) for s, e in scored]
    # one entry per app name (Start menu + App Paths often list the same program); prefer Start-menu entries
    best: dict[str, tuple[float, AppEntry]] = {}
    for s, e in scored:
        key = e.name.lower()
        if key not in best or (s, e.source == "start_menu") > (best[key][0], best[key][1].source == "start_menu"):
            best[key] = (s, e)
    if not best and query.strip():  # spoken in another script or misheard: compare by sound ("डिस्कॉर्ड" ~ Discord)
        from app.language.translit import sound_key

        key = sound_key(query)
        if len(key) >= 3:
            for e in entries:
                spoken = [w for w in re.split(r"[^\w+]+", e.name) if w and not (w.isupper() and len(w) <= 4)]  # no "CRM"
                words = [sound_key(w) for w in spoken] + [sound_key(normalize_name(e.name))]
                ratio = max((difflib.SequenceMatcher(None, key, w).ratio() for w in words if w), default=0)
                if ratio >= 0.8 and e.name.lower() not in best:
                    risky = 0.85 if _RISKY_NAME.search(e.name) else 1.0
                    best[e.name.lower()] = (0.75 * ratio * risky, e)
    matches = sorted(best.values(), key=lambda se: (-se[0], _RISKY_NAME.search(se[1].name) is not None, len(se[1].name)))
    return Resolution(query, matches)


def launch_risk(entry: AppEntry) -> str:
    """"normal" | "installer" (uninstall/setup/update tools) | "untrusted_location" (Downloads, Temp ...)."""
    if _RISKY_NAME.search(entry.name) or _RISKY_NAME.search(Path(entry.path or "").stem):
        return "installer"
    if entry.path and _UNTRUSTED_DIRS.search(os.path.normcase(entry.path)):
        return "untrusted_location"
    return "normal"


# ---- launch + verification ----------------------------------------------------------------------------------

def _keys(entry: AppEntry) -> list[str]:
    """Words that identify the app's windows: its name, its executable, the last part of its AppID."""
    generic = {"microsoft", "google", "studio", "app", "desktop", "64bit", "x64", "classic", "the", "for", "windows"}
    keys = [normalize_name(entry.name)]
    keys += [w for w in normalize_name(entry.name).split() if len(w) >= 3 and w not in generic]
    if entry.exe_stem:
        keys.append(entry.exe_stem)
    tail = re.split(r"[.!_\\]", entry.app_id)[-1].lower() if entry.app_id else ""
    if len(tail) >= 3 and tail not in generic and tail != "app":
        keys.append(tail)
    return list(dict.fromkeys(k for k in keys if k))


_BROWSER_PROCESSES = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "chromium", "arc"}


def window_matches(entry: AppEntry, window) -> bool:
    """Is this window the app's? The process name decides; a title only counts when it ENDS with the app's name
    (apps title their windows "document - App") and never for browser windows, whose titles are page titles
    ("Discord tips - Google Chrome" is not Discord)."""
    process = Path(window.process).stem.lower()
    keys = _keys(entry)
    if any(key == process or key.replace(" ", "") == process for key in keys):
        return True
    if process in _BROWSER_PROCESSES:
        return False
    title = " ".join((window.title or "").lower().split())
    return any(len(key) >= 4 and (title == key or title.endswith(" " + key)) for key in keys)


def launch(entry: AppEntry, starter=None) -> None:
    """Start the app like the Start menu does. Raises AppError when Windows refuses."""
    starter = starter or os.startfile  # type: ignore[attr-defined]
    target = f"shell:AppsFolder\\{entry.app_id}" if entry.app_id else entry.path
    if not target:
        raise AppError(f"don't know how to start {entry.name}")
    try:
        starter(target)
    except OSError as exc:
        raise AppError(f"Windows could not start {entry.name} ({exc.strerror or exc})") from None


def wait_for_window(entry: AppEntry, before: set[int], list_windows, timeout: float = 25.0, settle: float = 1.2,
                    clock=time.monotonic, sleep=time.sleep):
    """Poll until a window of ``entry`` appears (a new one, or an existing one coming to the front).
    Returns (window | None, how): "new_window" | "already_open" | "timeout"."""
    deadline = clock() + timeout
    found = None
    while clock() < deadline:
        windows = list_windows()
        new = [w for w in windows if w.hwnd not in before and window_matches(entry, w)]
        fronted = [w for w in windows if w.foreground and window_matches(entry, w)]
        if new or fronted:
            found, how = (new or fronted)[0], "new_window" if new else "already_open"
            sleep(settle)  # splash/updater windows: take the settled main window
            settled = [w for w in list_windows() if window_matches(entry, w)]
            main = max(settled, key=lambda w: ((w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]), w.foreground),
                       default=found)
            return main, how
        sleep(0.3)
    return found, "timeout"
