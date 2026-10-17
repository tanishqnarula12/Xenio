"""XENIO permission engine — the single source of truth for what an action may do.

Used in two processes:
  * by XENIO (``app.safety.permissions``) to answer Hermes' permission prompts, and
  * inside Hermes by the ``xenio-guard`` plugin, which checks EVERY tool call before it runs.
That is why this module imports only the standard library.

Levels:  READ < SAFE_WRITE < CONFIRM_REQUIRED < HIGH_RISK, plus ``blocked`` (never allowed).
Approach: allowlists first. A command or tool XENIO does not recognise needs confirmation;
anything touching secrets is blocked outright; paths are resolved (``..``, ``~``, Git-Bash
``/c/...``) before they are judged.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import IntEnum
from pathlib import Path, PurePath
from urllib.parse import unquote, urlsplit

POLICY_VERSION = "7"


class RiskLevel(IntEnum):
    READ = 0
    SAFE_WRITE = 1
    CONFIRM_REQUIRED = 2
    HIGH_RISK = 3


@dataclass(frozen=True)
class Decision:
    level: RiskLevel
    action: str  # e.g. "filesystem.delete", "terminal.git_push"
    summary: str  # what will happen, phrased to follow "This will ..."
    blocked: bool = False
    reason: str = ""
    # Destructive targets, e.g. ("delete:C:\\Users\\me\\Desktop\\old",). Once the user approves
    # deleting a folder, deleting it or anything inside it again (another command, a retry) is
    # covered for a short time instead of asking repeatedly.
    scopes: tuple[str, ...] = ()

    @property
    def needs_confirmation(self) -> bool:
        return not self.blocked and self.level >= RiskLevel.CONFIRM_REQUIRED


def scope_covers(approved: str, requested: str) -> bool:
    """``delete:C:\\a`` covers ``delete:C:\\a`` and ``delete:C:\\a\\b.txt`` (same operation, same or inner path)."""
    a_op, _, a_path = approved.partition(":")
    r_op, _, r_path = requested.partition(":")
    if a_op != r_op or not a_path or not r_path:
        return False
    a, r = os.path.normcase(os.path.normpath(a_path)), os.path.normcase(os.path.normpath(r_path))
    return r == a or r.startswith(a.rstrip("\\/") + os.sep)


def read(action: str, summary: str) -> Decision:
    return Decision(RiskLevel.READ, action, summary)


def blocked(action: str, summary: str, reason: str) -> Decision:
    return Decision(RiskLevel.HIGH_RISK, action, summary, blocked=True, reason=reason)


def combine(decisions: Iterable[Decision], fallback: Decision) -> Decision:
    """Most restrictive wins: any block, else the highest level (first summary at that level)."""
    items = list(decisions)
    if not items:
        return fallback
    for d in items:
        if d.blocked:
            return d
    top = max(d.level for d in items)
    chosen = next(d for d in items if d.level == top)
    if len(items) > 1 and top >= RiskLevel.CONFIRM_REQUIRED:
        others = [d.summary for d in items if d is not chosen and d.level >= RiskLevel.CONFIRM_REQUIRED]
        if others:
            chosen = replace(chosen, summary="; ".join([chosen.summary, *others]))
    scopes = tuple(dict.fromkeys(s for d in items for s in d.scopes))
    return replace(chosen, scopes=scopes) if scopes != chosen.scopes else chosen


# =============================================================================
# Paths
# =============================================================================

_WINDOWS = os.name == "nt"
_KNOWN_FOLDER_GUIDS = {
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
}
_known_folders_cache: dict[str, Path] | None = None


def known_folders() -> dict[str, Path]:
    """The user's real Desktop/Documents/... (on this PC the Desktop is redirected to OneDrive)."""
    global _known_folders_cache
    if _known_folders_cache is not None:
        return _known_folders_cache
    home = Path.home()
    found: dict[str, Path] = {}
    if _WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]

            shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
            for name, guid_text in _KNOWN_FOLDER_GUIDS.items():
                guid = _GUID()
                if ole32.CLSIDFromString(ctypes.c_wchar_p(guid_text), ctypes.byref(guid)) != 0:
                    continue
                out = ctypes.c_wchar_p()
                if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0 and out.value:
                    found[name] = Path(out.value)
                ole32.CoTaskMemFree(out)
        except Exception:
            found = {}
    for name in _KNOWN_FOLDER_GUIDS:
        found.setdefault(name, home / name)
    _known_folders_cache = found
    return found


_SECRET_NAMES = re.compile(
    r"^(\.env(\.(?!example$|sample$|template$|dist$)[\w.-]+)?|id_(rsa|dsa|ecdsa|ed25519)(_sk)?|.*\.(pem|key|p12|pfx|kdbx|keychain|keychain-db|ppk|jks|keystore|binarycookies)"
    r"|\.git-credentials|\.netrc|_netrc|\.pgpass|\.npmrc|\.pypirc|credentials(\.json)?|client_secret.*\.json"
    r"|token\.json|google_token\.json|google_client_secret\.json|auth\.json|login data|login data for account|cookies|web data|local state|logins\.json|key[34]\.db"
    r"|cookies\.sqlite|signons\.sqlite|sam|security)$",
    re.IGNORECASE,
)
_SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", "credentials", "protect", "vault", "keychains"}
# Windows "User Data", Firefox profiles everywhere, Chrome/Edge/Brave/Arc profiles and Safari/system cookies on macOS
_BROWSER_PROFILE_MARKERS = ("\\user data\\", "/user data/", "\\firefox\\profiles\\", "/firefox/profiles/",
                            "/application support/google/chrome/", "/application support/microsoft edge/",
                            "/application support/bravesoftware/", "/application support/arc/user data/",
                            "/library/cookies/", "/library/containers/com.apple.safari/data/library/cookies/")
_EXECUTABLE_SUFFIXES = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".lnk", ".reg",
                        ".dll", ".msi", ".scr", ".com", ".hta", ".cpl",
                        # macOS: double-click runs it (.command, .app, .workflow, scripts) or it installs/loads code
                        ".command", ".app", ".pkg", ".mpkg", ".scpt", ".applescript", ".workflow", ".terminal",
                        ".dylib", ".kext", ".plugin"}
_WINDOWS_SYSTEM = ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
                   "system volume information", "recovery", "boot", "perflogs")
_POSIX_SYSTEM = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/sbin", "/sys", "/usr",
                 "/var", "/system", "/library", "/applications", "/private", "/opt", "/volumes", "/cores", "/nix",
                 "/sw", "/net", "/snap", "/srv", "/mnt", "/media")
# Programs that start by themselves at login/boot: writing here installs one (macOS launchd)
_AUTOSTART_DIRS = ("library/launchagents", "library/launchdaemons", "library/startupitems")
TRASH_NAME = "Recycle Bin" if _WINDOWS else "Trash"
# iCloud Drive lives in ~/Library but holds the user's own documents
_ICLOUD_DRIVE = ("Library", "Mobile Documents", "com~apple~CloudDocs")


@dataclass
class PathPolicy:
    """Classifies filesystem operations by WHERE they happen.

    Categories: ``secret`` (keys, .env, browser credential stores: blocked), ``system`` (OS and
    program folders), ``app_data`` (AppData, dot-folders), ``user`` (home, Desktop, Documents,
    Downloads, the XENIO workspace, temp, extra allowed folders), ``outside`` (anything else)."""

    workspace: Path | None = None
    extra_roots: tuple[Path, ...] = ()
    home: Path = field(default_factory=Path.home)
    hermes_home: Path | None = None

    def __post_init__(self) -> None:
        self.home = self._abs(self.home)
        folders = known_folders()
        self.named_roots = {name: self._abs(p) for name, p in folders.items()}
        roots = [self.home, *self.named_roots.values(), self._abs(Path(tempfile.gettempdir()))]
        if self.workspace is not None:
            self.workspace = self._abs(self.workspace)
            roots.append(self.workspace)
        roots.extend(self._abs(p) for p in self.extra_roots)
        self.icloud = self.home.joinpath(*_ICLOUD_DRIVE)
        roots.append(self.icloud)
        # macOS: /var, /tmp and /etc are symlinks into /private, so the same folder has two spellings. Every
        # user root is known under both, or a workspace under /private/var/folders would count as "system".
        roots.extend(self._real(r) for r in list(roots))
        self.user_roots = list(dict.fromkeys(roots))
        temp = self._abs(Path(tempfile.gettempdir()))
        self.temp = temp
        self.temp_roots = tuple(dict.fromkeys(
            [temp, self._real(temp)] + ([] if _WINDOWS else [Path("/tmp"), self._real(Path("/tmp"))])))
        # Deleting or moving these themselves is never allowed: home, Desktop/Documents/... (both the
        # real, possibly OneDrive-redirected folders and the default ~/Name ones), OneDrive and iCloud Drive roots.
        onedrive = [os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
        self.protected_roots = {self.home, *self.named_roots.values(),
                                *(self.home / name for name in _KNOWN_FOLDER_GUIDS),
                                *(self._abs(Path(p)) for p in onedrive if p), self.home / "OneDrive",
                                self.icloud, self.home / "Library", *self.temp_roots}
        if self.workspace is not None:
            self.protected_roots.add(self.workspace)
        self.protected_roots |= {self._real(r) for r in self.protected_roots}

    # ---- resolution ------------------------------------------------------------

    @staticmethod
    def _abs(path: Path) -> Path:
        try:
            return Path(os.path.abspath(os.path.expanduser(str(path))))
        except (OSError, ValueError):
            return Path(str(path))

    @staticmethod
    def _real(path: Path) -> Path:
        try:
            return Path(os.path.realpath(path))
        except (OSError, ValueError):
            return path

    def resolve(self, raw: str, cwd: str | os.PathLike | None = None) -> Path:
        """Turn a user/agent supplied path into an absolute, normalised path (``..`` collapsed)."""
        if raw is None or not str(raw).strip():
            raise ValueError("empty path")
        text = str(raw).strip().strip('"').strip("'")
        if "\x00" in text:
            raise ValueError("path contains a NUL byte")
        for var, value in (("$HOME", str(self.home)), ("${HOME}", str(self.home)), ("%USERPROFILE%", str(self.home)),
                           ("$USERPROFILE", str(self.home)), ("$env:USERPROFILE", str(self.home))):
            if text.startswith(var):
                text = value + text[len(var):]
        if _WINDOWS:
            m = re.match(r"^/([a-zA-Z])(/|$)(.*)", text)  # Git-Bash /c/Users/... -> C:/Users/...
            if m:
                text = f"{m.group(1).upper()}:/{m.group(3)}"
        text = os.path.expanduser(text)
        base = Path(cwd) if cwd else (self.workspace or Path.cwd())
        candidate = Path(text) if os.path.isabs(text) or re.match(r"^[a-zA-Z]:", text) else base / text
        return Path(os.path.abspath(str(candidate)))

    # ---- classification --------------------------------------------------------

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return os.path.normcase(str(path)).startswith(os.path.normcase(str(root)).rstrip("\\/") + os.sep)

    def category(self, path: Path) -> str:
        low = os.path.normcase(str(path)).lower()
        parts = [p.lower() for p in PurePath(path).parts]
        if low.startswith(("\\\\.\\", "\\\\?\\")):
            return "system"  # raw devices
        if _SECRET_NAMES.match(path.name) and not path.name.lower().endswith(".pub"):
            return "secret"
        if any(p in _SECRET_DIRS for p in parts[1:]) and path.suffix.lower() != ".pub":
            return "secret"
        if any(marker in low + "\\" for marker in _BROWSER_PROFILE_MARKERS):
            return "secret"
        if self.hermes_home is not None and self._within(path, self._abs(self.hermes_home)):
            rel = os.path.normcase(str(path))[len(os.path.normcase(str(self._abs(self.hermes_home)))):].lstrip("\\/")
            if rel.split(os.sep)[0].lower() in {"auth.json", "vault", ".env", "pairing"}:
                return "secret"
        if any(path == t or self._within(path, t) for t in self.temp_roots):
            return "user"
        if _WINDOWS:
            if len(parts) <= 1:
                return "system"  # a drive root
            if parts[1] in _WINDOWS_SYSTEM:
                return "system"
            if parts[1] == "users" and not self._within(path, self.home) and path != self.home:
                return "system"  # other users' profiles / C:\Users itself
        else:
            if str(path) == "/" or any(low == s or low.startswith(s + "/") for s in _POSIX_SYSTEM):
                if not self._within(path, self.home):
                    return "system"
            if len(parts) >= 2 and parts[1] in ("users", "home") and path != self.home \
                    and not self._within(path, self.home) and not self._within(self.home, path):
                return "system"  # other users' home folders, /Users/Shared
        if self._within(path, self.home):
            rel = path.relative_to(self.home).parts if path != self.home else ()
            if rel and (rel[0].lower() == "appdata" or (rel[0].startswith(".") and rel[0] not in (".", ".."))):
                return "app_data"
            if rel and rel[0].lower() == "library" and not (path == self.icloud or self._within(path, self.icloud)):
                return "app_data"  # macOS ~/Library: settings, app state, LaunchAgents (iCloud Drive excepted)
        if any(path == root or self._within(path, root) for root in self.user_roots):
            return "user"
        return "outside"

    def describe(self, path: Path) -> str:
        """Friendly location: "XenioTest on your Desktop", "notes.txt in your Documents"."""
        for name, root in self.named_roots.items():
            if path == root:
                return f"your {name} folder"
            if self._within(path, root):
                rel = str(path.relative_to(root))
                return f"{rel} on your Desktop" if name == "Desktop" else f"{rel} in your {name}"
        if self.workspace is not None and self._within(path, self.workspace):
            return f"{path.relative_to(self.workspace)} in the XENIO workspace"
        return str(path)

    def check(self, raw: str, op: str, cwd: str | os.PathLike | None = None, *, recursive: bool = False,
              permanent: bool = True) -> Decision:
        """``op``: read | list | create | modify | move | delete."""
        try:
            path = self.resolve(raw, cwd)
        except ValueError as exc:
            return blocked(f"filesystem.{op}", f"use the path {raw!r}", f"invalid path: {exc}")
        cat = self.category(path)
        where = self.describe(path)
        action = f"filesystem.{op}"
        if cat == "secret":
            return blocked(action, f"{op} {where}",
                           "that is a protected secrets location (keys, .env files, credential stores)")
        top_level = len(PurePath(path).parts) <= (1 if _WINDOWS else 2)  # C:\ ; / and /anything
        if op in ("move", "delete") and (path in self.protected_roots or top_level):
            return blocked(action, f"{op} {where}", "that is a top-level folder XENIO must never remove or move")
        if op in ("read", "list"):
            return read(action, f"{'list' if op == 'list' else 'read'} {where}")
        exists = path.exists()
        is_dir = path.is_dir()
        noun = "the folder" if is_dir else "the file" if exists else ""
        if op == "delete":
            contents = " and everything in it" if is_dir else ""
            if permanent:
                detail = f"permanently delete {noun} {where}{contents}".replace("  ", " ")
            else:
                detail = f"move {noun} {where}{contents} to the {TRASH_NAME}".replace("  ", " ")
            if cat == "system":
                return blocked(action, detail, "deleting operating-system or program folders is not allowed")
            level = RiskLevel.CONFIRM_REQUIRED if cat == "user" else RiskLevel.HIGH_RISK
            return Decision(level, action, detail, scopes=(f"delete:{path}",))
        if op == "move":
            detail = f"move or rename {noun} {where}".replace("  ", " ")
            if cat == "system":
                return blocked(action, detail, "moving operating-system or program folders is not allowed")
            return Decision(RiskLevel.CONFIRM_REQUIRED if cat == "user" else RiskLevel.HIGH_RISK, action, detail,
                            scopes=(f"move:{path}",))
        # create / modify
        verb = "create" if op == "create" or not exists else "change"
        detail = f"{verb} {where}"
        if cat == "system":
            return Decision(RiskLevel.HIGH_RISK, action, f"{detail} (a system folder)")
        norm = str(path).replace("\\", "/").lower()
        if any(f"/{d}/" in norm or norm.endswith(f"/{d}") for d in _AUTOSTART_DIRS):
            return Decision(RiskLevel.HIGH_RISK, action, f"{detail} (a program that starts by itself at login)")
        if cat in ("app_data", "outside"):
            return Decision(RiskLevel.CONFIRM_REQUIRED, action, detail)
        if ".git" in (p.lower() for p in PurePath(path).parts[:-1]):
            # hooks and .git/config (core.fsmonitor, core.pager...) make later "safe" git commands run programs
            return Decision(RiskLevel.CONFIRM_REQUIRED, action, f"{detail} (git's internal files, which can run programs)")
        if path.suffix.lower() in _EXECUTABLE_SUFFIXES:
            return Decision(RiskLevel.CONFIRM_REQUIRED, action, f"{detail} (an executable/script file)")
        if recursive and op == "modify":
            return Decision(RiskLevel.CONFIRM_REQUIRED, action, f"{detail} recursively")
        return Decision(RiskLevel.SAFE_WRITE, action, detail)


# =============================================================================
# Secrets in text
# =============================================================================

# (pattern, keep group 1 as a visible prefix)
_SECRET_VALUE_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), False),
    (re.compile(r"\bsk-(?:or-|proj-|ant-)?[A-Za-z0-9_-]{16,}"), False),
    (re.compile(r"\bnbh_[A-Za-z0-9]{16,}"), False),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), False),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), False),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), False),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), False),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"), False),  # Telegram bot tokens
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{12,}"), True),
    # NAME=value / "name": "value" where the name looks secret; purely numeric values (token counts) are kept.
    # (a value that is already a placeholder such as [REDACTED] / [secret hidden] is left alone: masking is idempotent)
    (re.compile(r"(?i)(\b[A-Za-z0-9_]*(?:api[_-]?key|access[_-]?key|token|secret|password|passwd|credential)"
                r"[A-Za-z0-9_]*[\"']?\s*[=:]\s*[\"']?)(?![0-9.]+\b)(?!\[)[^\s\"',;]{6,}"), True),
]
_SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|COOKIE|AUTH)", re.IGNORECASE)


def secret_env_values(environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return sorted({v for k, v in env.items() if _SECRET_ENV_NAME.search(k) and v and len(v) >= 8}, key=len, reverse=True)


def redact_secrets(text: str, known_values: Iterable[str] = ()) -> str:
    """Mask API keys, tokens, private keys and known secret values in any text."""
    if not text:
        return text
    for value in known_values:
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    for pattern, keep_prefix in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(lambda m, k=keep_prefix: (m.group(1) if k else "") + "[REDACTED]", text)
    return text


# =============================================================================
# Terminal commands (Hermes runs them with bash, also on Windows)
# =============================================================================

_VERSION_ARGS = ({"--version"}, {"-v"}, {"-V"}, {"version"}, {"--help"}, {"-h"}, {"help"})
_READ_ONLY = {
    "ls", "dir", "pwd", "echo", "printf", "true", "false", "which", "where", "whereis", "whoami", "hostname",
    "date", "uname", "uptime", "id", "groups", "tree", "nproc", "free", "df", "du", "ps", "tasklist", "ipconfig",
    "ifconfig", "ping", "nslookup", "systeminfo", "ver", "cls", "clear", "sleep", "test", "[", "history", "man",
    "cd", "pushd", "popd", "export", "alias", "unalias", "command", "help", "getconf", "locale", "lsb_release",
    "netstat", "arp", "route", "tracert", "traceroute", "env_check", "seq", "yes",
}
_READS_PATHS = {
    "cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "findstr", "file", "stat", "md5sum",
    "sha1sum", "sha256sum", "diff", "cmp", "strings", "xxd", "hexdump", "od", "jq", "wc", "sort", "uniq", "cut",
    "tr", "column", "nl", "fold", "basename", "dirname", "realpath", "readlink", "awk", "comm", "tac", "rev", "type",
}
_TEST_RUNNERS = {"pytest", "tox", "nox", "jest", "vitest", "mocha", "phpunit", "rspec", "ctest"}
_INTERPRETERS = {"python", "py", "node", "deno", "bun", "ruby", "perl", "php", "java", "lua", "Rscript"}
_SHELLS = {"bash", "sh", "zsh", "dash", "fish", "cmd", "powershell", "pwsh"}
_PKG_MANAGERS = {"pip", "pip3", "uv", "npm", "pnpm", "yarn", "conda", "mamba", "poetry", "pipx", "gem", "composer",
                 "winget", "choco", "scoop", "apt", "apt-get", "brew", "dnf", "yum", "pacman", "snap", "cargo", "go",
                 "dotnet", "nuget", "pdm", "hatch", "rye", "bundle"}
_PKG_READ = {"list", "ls", "show", "freeze", "outdated", "view", "info", "search", "why", "check", "config", "doctor",
             "env", "help", "--version", "-v", "version", "audit", "licenses", "inspect", "tree", "graph", "explain"}
_PKG_CHANGE = {"install", "i", "add", "uninstall", "remove", "rm", "un", "update", "upgrade", "up", "ci", "sync",
               "link", "unlink", "prune", "dedupe", "rebuild", "global", "self-update", "download", "fund"}
_PKG_PUBLISH = {"publish", "unpublish", "deprecate", "owner", "dist-tag", "yank", "push"}
_PKG_TEST = {"test", "t", "tst"}
_PKG_BUILD = {"build", "compile", "check", "lint", "fmt", "format", "vet", "clippy", "typecheck"}
_PKG_RUN = {"run", "start", "exec", "x", "dlx", "serve", "dev", "watch"}
_GIT_READ = {"status", "log", "diff", "show", "rev-parse", "describe", "ls-files", "ls-tree", "blame", "shortlog",
             "reflog", "grep", "help", "version", "whatchanged", "cat-file", "count-objects", "for-each-ref",
             "name-rev", "show-ref", "check-ignore", "var", "worktree", "ls-remote", "rev-list", "merge-base"}
# Git settings that only change output or metadata. Any other one-off setting (`git -c core.pager=...`,
# core.sshCommand, core.fsmonitor, alias.x=!cmd, ...) can make git start a program.
_GIT_HARMLESS_CONFIG = re.compile(
    r"(color\.[\w.-]+|core\.(quotepath|autocrlf|safecrlf|longpaths)|advice\.[\w.-]+|log\.[\w.-]+|user\.(name|email)"
    r"|init\.defaultbranch|column\.[\w.-]+|i18n\.[\w.-]+|pull\.(rebase|ff)|push\.(default|autosetupremote)"
    r"|diff\.(renames|algorithm|noprefix|colormoved))=.*", re.IGNORECASE | re.DOTALL)
# Subcommand options that run a program (--upload-pack, --template hooks, --exec) or write a file (--output).
_GIT_RUNS_PROGRAM_OPTION = re.compile(r"--(upload-pack|receive-pack|exec|template|output|config|ext-diff)(=.*)?",
                                      re.IGNORECASE | re.DOTALL)
_GIT_SAFE_WRITE = {"add", "init", "fetch", "clone", "stash", "commit", "notes", "archive", "bundle", "mv"}
_GIT_CONFIRM = {"push", "pull", "merge", "rebase", "reset", "checkout", "switch", "restore", "cherry-pick", "revert",
                "clean", "rm", "am", "apply", "submodule", "remote", "tag", "branch", "config", "gc", "prune"}
_GIT_HIGH = {"filter-branch", "filter-repo", "update-ref", "replace", "credential", "credential-manager"}
_SYSTEM_HIGH = {"shutdown", "reboot", "halt", "poweroff", "init", "telinit", "bcdedit", "diskpart", "format", "fdisk",
                "parted", "mkfs", "wipefs", "vssadmin", "cipher", "passwd", "chpasswd", "useradd", "userdel",
                "usermod", "cmdkey", "ssh-keygen", "ssh-add", "visudo", "logoff", "takeown", "icacls", "sudo", "su",
                "runas", "doas", "dd", "mount", "umount", "fsutil", "wmic", "bootrec", "sfc", "dism", "net"}
_PROCESS_CONFIRM = {"kill", "taskkill", "killall", "pkill", "xkill", "systemctl", "service", "sc", "launchctl",
                    "setx", "crontab", "schtasks", "at", "chmod", "chown", "chgrp", "attrib", "ln", "mklink",
                    "curl", "wget", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "nc", "ncat", "docker",
                    "podman", "kubectl", "helm", "terraform", "ansible", "ansible-playbook", "psql", "mysql",
                    "sqlite3", "mongo", "mongosh", "redis-cli", "code", "notepad", "explorer", "start", "open",
                    "xdg-open", "make", "cmake", "ninja", "msbuild", "gradle", "mvn", "gh", "az", "aws", "gcloud",
                    "vercel", "netlify", "firebase", "heroku", "fly", "flyctl", "npx", "pnpx", "bunx", "hermes"}
_PRODUCTION = re.compile(r"\b(prod|production|live)\b", re.IGNORECASE)
_DESTRUCTIVE_SQL = re.compile(r"\b(drop|truncate|delete\s+from|alter|update\s+\w+\s+set|flushall|flushdb|dropdatabase)\b",
                              re.IGNORECASE)
_SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "esac", "in"}
_CMD_STYLE = {"del", "erase", "rd", "rmdir", "md", "copy", "xcopy", "robocopy", "move", "ren", "rename", "dir",
              "attrib", "icacls", "findstr", "tasklist", "taskkill", "schtasks", "sc", "reg", "net", "type", "cmd"}
_SECRET_VAR_REF = re.compile(
    r"(\$\{?|%|\$env:)([A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z0-9_]*)", re.IGNORECASE
)
_SECRET_MENTION = re.compile(
    r"(^|[\s'\"=/\\(])(\.env(?:\.(?!example|sample|template)[\w-]+)?|id_(?:rsa|dsa|ecdsa|ed25519)|\.git-credentials"
    r"|\.aws[/\\]credentials|\.ssh[/\\][\w.-]+|login data|key[34]\.db|logins\.json|\.netrc|\.pgpass)(?=$|[\s'\";|&)<>])",
    re.IGNORECASE,
)
_OPERATORS = {"&&", "||", ";", "|", "&", ";;", "|&", "\n"}
_REDIRECTS = {">", ">>", "&>", ">|", "&>>", "1>", "2>", "1>>", "2>>"}


def _program(token: str) -> str:
    name = os.path.basename(token.replace("\\", "/")).lower()
    name = re.sub(r"\.(exe|cmd|bat|com|ps1)$", "", name)
    name = re.sub(r"^(python|pip)\d+(\.\d+)?$", r"\1", name)
    return name


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\r", ""), posix=True, punctuation_chars=";&|<>\n")
    lexer.whitespace = " \t"  # newlines separate commands, so they are punctuation here
    lexer.whitespace_split = True
    return list(lexer)


def _is_operator(tok: str) -> bool:
    return bool(tok) and all(c in ";&|\n" for c in tok)


def _split_segments(tokens: list[str]) -> list[tuple[list[str], str | None]]:
    """[(segment tokens, operator that preceded it)]."""
    segments, current, prev_op = [], [], None
    for tok in tokens:
        if _is_operator(tok):
            if current:
                segments.append((current, prev_op))
            current, prev_op = [], tok.replace("\n", "").strip() or ";"
        else:
            current.append(tok)
    if current:
        segments.append((current, prev_op))
    return segments


@dataclass
class _Ctx:
    paths: PathPolicy
    cwd: str
    depth: int = 0


def _non_flags(args: list[str], slash_flags: bool = False) -> list[str]:
    """Arguments that are not options. ``/x`` counts as an option only for cmd-style programs:
    in Git-Bash ``/c`` is the C: drive."""
    return [a for a in args if not (a.startswith("-") or (slash_flags and re.fullmatch(r"/[A-Za-z?]{1,3}(:\S*)?", a)))]


def _looks_like_path(token: str) -> bool:
    return bool(token) and (("/" in token or "\\" in token or token.startswith((".", "~"))) or bool(re.match(r"^[a-zA-Z]:", token)))


def _path_checks(paths: list[str], op: str, ctx: _Ctx, **kw) -> list[Decision]:
    return [ctx.paths.check(p, op, ctx.cwd, **kw) for p in paths if p and p not in ("/dev/null", "NUL", "nul")]


def _has_glob(token: str) -> bool:
    return any(ch in token for ch in "*?[")


def classify_python_code(code: str, ctx: _Ctx | None = None) -> Decision:
    """Allowlist analysis of Python source (execute_code, python -c)."""
    safe_modules = {"math", "cmath", "statistics", "random", "json", "re", "datetime", "time", "calendar",
                    "collections", "itertools", "functools", "operator", "string", "textwrap", "decimal", "fractions",
                    "uuid", "hashlib", "base64", "typing", "dataclasses", "enum", "pprint", "sys", "platform",
                    "locale", "unicodedata", "bisect", "heapq", "copy", "hermes_tools", "zoneinfo", "numbers"}
    if _SECRET_MENTION.search(code) or _SECRET_VAR_REF.search(code):
        return blocked("code.execute", "run Python code that touches secrets", "the code references secret files or keys")
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return Decision(RiskLevel.CONFIRM_REQUIRED, "code.execute", "run Python code XENIO could not analyse")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [(node.module or "").split(".")[0]]
        else:
            mods = []
        bad = [m for m in mods if m not in safe_modules]
        if bad:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "code.execute", f"run Python code that uses {', '.join(bad)}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"exec", "eval", "compile", "__import__", "breakpoint", "input"}:
                return Decision(RiskLevel.CONFIRM_REQUIRED, "code.execute", f"run Python code that calls {node.func.id}()")
            if node.func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
                mode_text = mode.value if isinstance(mode, ast.Constant) and isinstance(mode.value, str) else "r"
                if any(c in mode_text for c in "wax+"):
                    return Decision(RiskLevel.CONFIRM_REQUIRED, "code.execute", "run Python code that writes files")
                target = node.args[0] if node.args else None
                if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
                    return Decision(RiskLevel.CONFIRM_REQUIRED, "code.execute", "run Python code that opens files")
                if ctx is not None:
                    d = ctx.paths.check(target.value, "read", ctx.cwd)
                    if d.blocked:
                        return d
    return read("code.execute", "run read-only Python code")


def classify_command(command: str, paths: PathPolicy, cwd: str | os.PathLike | None = None, _depth: int = 0) -> Decision:
    """Risk of a shell command. Every segment of a pipeline/chain is judged; the worst wins."""
    text = (command or "").strip()
    if not text:
        return blocked("terminal.empty", "run an empty command", "the command is empty")
    ctx = _Ctx(paths=paths, cwd=str(cwd or paths.workspace or Path.cwd()), depth=_depth)
    if re.search(r"\b(get-childitem|gci|dir|ls)\s+env:", text, re.IGNORECASE):
        return blocked("terminal.env_dump", f"run: {text}", "printing environment variables would expose API keys")
    if _SECRET_VAR_REF.search(text):
        return blocked("terminal.secret_var", f"run: {text}", "the command reads a secret environment variable")
    if _SECRET_MENTION.search(text):
        return blocked("terminal.secret_file", f"run: {text}", "the command touches a protected secrets file")
    if _depth > 2:
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.nested", f"run the nested command: {text}")
    if re.search(r"\$\(|`|<\(|>\(", text):
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.complex", f"run the command: {text}")
    try:
        tokens = _tokenize(text)
    except ValueError:
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.unparsed", f"run the command: {text}")
    decisions = []
    segments = _split_segments(tokens)
    for index, (seg, op) in enumerate(segments):
        piped_from = segments[index - 1][0] if op in ("|", "|&") and index else None
        decisions.append(_classify_segment(seg, ctx, piped_from))
    return combine(decisions, read("terminal.noop", "run nothing"))


def _redirect_targets(seg: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split a segment into (words, write targets, read sources). Handles ``2>&1``, ``>/dev/null``."""
    words, writes, reads_, i = [], [], [], 0
    while i < len(seg):
        tok = seg[i]
        nxt = seg[i + 1] if i + 1 < len(seg) else None
        if tok.isdigit() and nxt and nxt[0] in "<>":
            i += 1  # file-descriptor number of a redirect (the "2" in 2>&1)
            continue
        if tok in (">&", "<&", ">&-", "<&-"):
            i += 2  # fd duplication: 2>&1
            continue
        if set(tok) <= set("<>&|") and ">" in tok:
            if nxt is not None:
                writes.append(nxt)
            i += 2
            continue
        if tok in ("<", "<<", "<<<"):
            if nxt is not None and tok == "<":
                reads_.append(nxt)
            i += 2
            continue
        words.append(tok)
        i += 1
    return words, writes, reads_


def _classify_segment(seg: list[str], ctx: _Ctx, piped_from: list[str] | None) -> Decision:
    words, writes, sources = _redirect_targets(seg)
    extra = _path_checks(sources, "read", ctx) + [
        ctx.paths.check(w, "modify" if _exists(ctx, w) else "create", ctx.cwd) for w in writes
        if w not in ("/dev/null", "NUL", "nul")
    ]
    # A bare env/printenv/set/export -p prints every environment variable, API keys included.
    if words and _program(words[0]) in ("env", "printenv", "set", "export", "declare", "typeset", "compgen"):
        rest = [w for w in words[1:] if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w)]
        prog0 = _program(words[0])
        dumps = (prog0 in ("env", "printenv") and not [w for w in rest if not w.startswith("-")]) or \
                (prog0 == "set" and not rest) or (prog0 in ("export", "declare", "typeset") and
                                                  (not rest or rest[0] in ("-p", "-x", "-px", "-xp"))) or \
                (prog0 == "compgen" and "-v" in rest) or (prog0 == "printenv" and rest)
        if dumps:
            return blocked("terminal.env_dump", f"run: {' '.join(seg)}",
                           "printing environment variables would expose API keys")
    # Strip wrappers, assignments and shell control keywords in any order:
    # "FOO=1 env nohup cmd" -> "cmd";  "if test -d x" / "then echo y" / "fi" -> the commands inside.
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or
                     words[0] in ("time", "nohup", "command", "builtin", "exec", "nice", "stdbuf", "xargs", "env")
                     or words[0] in _SHELL_KEYWORDS):
        words = words[1:]
    # cmd.exe's "if [not] exist <path> <command>": a path test followed by a command.
    if len(words) >= 2 and words[0].lower() == "exist" or (len(words) >= 3 and [w.lower() for w in words[:2]] == ["not", "exist"]):
        start = 1 if words[0].lower() == "exist" else 2
        check = ctx.paths.check(words[start], "read", ctx.cwd)
        rest = words[start + 1:]
        inner = _classify_segment(rest, ctx, None) if rest else read("terminal.test", f"check {words[start]}")
        return combine([check, inner], inner)
    if not words:
        return combine(extra, read("terminal.noop", "run nothing"))
    prog, args = _program(words[0]), words[1:]
    text = " ".join(seg)
    decision = _classify_program(prog, args, ctx, text, piped_from)
    if prog in ("cd", "pushd") and args:
        try:
            ctx.cwd = str(ctx.paths.resolve(args[0], ctx.cwd))
        except ValueError:
            pass
    return combine([decision, *extra], decision)


def _exists(ctx: _Ctx, raw: str) -> bool:
    try:
        return ctx.paths.resolve(raw, ctx.cwd).exists()
    except ValueError:
        return False


def _classify_program(prog: str, args: list[str], ctx: _Ctx, text: str, piped_from: list[str] | None) -> Decision:
    confirm = lambda action, summary: Decision(RiskLevel.CONFIRM_REQUIRED, action, summary)  # noqa: E731
    high = lambda action, summary: Decision(RiskLevel.HIGH_RISK, action, summary)  # noqa: E731
    run_text = f"run the command: {text}"
    slash = prog in _CMD_STYLE
    if args and set(args) in _VERSION_ARGS:
        return read("terminal.version", f"check the {prog} version")
    if prog == "set":
        return read("terminal.set", f"set shell options: {text}")
    if piped_from is not None and prog in (_SHELLS | _INTERPRETERS | {"iex", "invoke-expression"}) and not _non_flags(args):
        source = _program(piped_from[0]) if piped_from else ""
        if source in {"curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod"}:
            return high("terminal.remote_script",
                        f"run a script downloaded from the internet: {' '.join(piped_from)} | {text}")
        return confirm("terminal.pipe_to_shell", run_text)

    if prog in _READ_ONLY:
        paths = [a for a in _non_flags(args, slash) if _looks_like_path(a) or _exists(ctx, a)] \
            if prog in {"ls", "dir", "tree", "du"} else []
        if prog in {"ls", "dir", "tree", "du"} and not paths:
            return read("terminal.list", f"list {ctx.paths.describe(Path(ctx.cwd))}")
        return combine(_path_checks(paths, "list", ctx), read(f"terminal.{prog}", f"run: {text}"))
    if prog in _READS_PATHS:
        if prog == "awk" and re.search(r"system\s*\(|>\s*\"", text):
            return confirm("terminal.awk", run_text)
        paths = [a for a in _non_flags(args, slash) if _looks_like_path(a) or _exists(ctx, a)]
        return combine(_path_checks(paths, "read", ctx), read(f"terminal.{prog}", f"read files: {text}"))
    if prog == "find":
        roots = [a for a in args if not a.startswith(("-", "(", ")", "!"))][:1] or ["."]
        if "-delete" in args:
            try:
                root_cat = ctx.paths.category(ctx.paths.resolve(roots[0], ctx.cwd))
            except ValueError:
                root_cat = "outside"
            if root_cat in ("system", "secret"):
                return blocked("terminal.find_delete", f"delete files matching: {text}",
                               "mass deletion inside system or secrets folders is not allowed")
            return high("terminal.find_delete", f"permanently delete every file matching: {text}")
        if any(a in args for a in ("-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")):
            return confirm("terminal.find_exec", run_text)
        return combine(_path_checks(roots, "list", ctx), read("terminal.find", f"search for files: {text}"))
    if prog == "sed":
        files = [a for a in _non_flags(args)[1:] if _looks_like_path(a) or _exists(ctx, a)]
        if any(a.startswith("-i") or a == "--in-place" for a in args):
            return combine(_path_checks(files, "modify", ctx), confirm("terminal.sed", f"edit files in place: {text}"))
        return combine(_path_checks(files, "read", ctx), read("terminal.sed", f"read files: {text}"))
    if prog in ("mkdir", "md"):
        targets = _non_flags(args, slash)
        return combine(_path_checks(targets, "create", ctx), confirm("terminal.mkdir", run_text))
    if prog == "touch":
        targets = _non_flags(args, slash)
        return combine([ctx.paths.check(t, "modify" if _exists(ctx, t) else "create", ctx.cwd) for t in targets],
                       confirm("terminal.touch", run_text))
    if prog in ("cp", "copy", "xcopy", "robocopy"):
        items = _non_flags(args, slash)
        if len(items) < 2:
            return confirm("terminal.copy", run_text)
        dest = items[-1]
        decisions = _path_checks(items[:-1], "read", ctx) + [
            ctx.paths.check(dest, "modify" if _exists(ctx, dest) and not Path(ctx.paths.resolve(dest, ctx.cwd)).is_dir()
                            else "create", ctx.cwd)]
        if prog == "robocopy" and any(a.lower() in ("/mir", "/purge") for a in args):
            decisions.append(high("terminal.robocopy_mirror", f"mirror folders (deletes extra files): {text}"))
        return combine(decisions, confirm("terminal.copy", run_text))
    if prog in ("mv", "move", "ren", "rename"):
        items = _non_flags(args, slash)
        if len(items) < 2:
            return confirm("terminal.move", run_text)
        return combine(_path_checks(items[:-1], "move", ctx) + _path_checks(items[-1:], "create", ctx),
                       confirm("terminal.move", run_text))
    if prog in ("rm", "rmdir", "rd", "del", "erase", "unlink", "shred", "trash", "remove-item", "ri"):
        recursive = prog in ("rmdir", "rd") or any(
            re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|-recurse", a, re.IGNORECASE)
            or (slash and a.lower() == "/s") for a in args)
        targets = _non_flags(args, slash)
        if not targets:
            return confirm("terminal.delete", run_text)
        decisions = []
        for t in targets:
            if _has_glob(t):
                parent = os.path.dirname(t) or "."
                d = ctx.paths.check(parent, "delete", ctx.cwd)
                if d.blocked or (parent in ("/", "~", "~/", ".") and recursive):
                    decisions.append(d if d.blocked else blocked("filesystem.delete", f"delete everything in {parent}",
                                                                 "wildcard deletion of a top-level folder"))
                    continue
                inner = ctx.paths.check(t.replace("*", "x").replace("?", "x"), "delete", ctx.cwd)
                decisions.append(replace(inner, summary=f"permanently delete everything matching {t}"))
            else:
                decisions.append(ctx.paths.check(t, "delete", ctx.cwd, recursive=recursive))
        return combine(decisions, confirm("terminal.delete", run_text))
    if prog == "git":
        return _classify_git(args, ctx, text)
    if prog == "gh":
        return classify_gh(args, text)
    if prog == "himalaya":
        return classify_himalaya(args, text)
    if prog in _PKG_MANAGERS:
        return _classify_package_manager(prog, args, ctx, text)
    if prog in _TEST_RUNNERS:
        return read("terminal.test", f"run the tests: {text}")
    if prog in _INTERPRETERS:
        return _classify_interpreter(prog, args, ctx, text)
    if prog in _SHELLS:
        return _classify_shell(prog, args, ctx, text)
    if prog in _SYSTEM_HIGH or prog.startswith("mkfs"):
        if prog == "net" and args and args[0].lower() in ("view", "statistics", "config", "share", "session", "use"):
            return read("terminal.net", f"run: {text}")
        if prog == "net" and args and args[0].lower() in ("start", "stop", "pause", "continue"):
            return confirm("terminal.service", f"start or stop a Windows service: {text}")
        return high(f"terminal.{prog}", f"run the system command: {text}")
    if prog in ("reg", "reg.exe"):
        if args and args[0].lower() in ("query", "export", "compare"):
            return read("terminal.reg_query", f"read the registry: {text}")
        return high("terminal.registry", f"change the Windows registry: {text}")
    if prog in ("schtasks", "crontab"):
        if any(a.lower() in ("/query", "-l") for a in args):
            return read("terminal.tasks", f"list scheduled tasks: {text}")
        return high("terminal.scheduled_task", f"change scheduled tasks: {text}")
    if prog in ("psql", "mysql", "sqlite3", "mongo", "mongosh", "redis-cli"):
        if _DESTRUCTIVE_SQL.search(text) or _PRODUCTION.search(text):
            return high("terminal.database", f"change a database: {text}")
        return confirm("terminal.database", f"connect to a database: {text}")
    if prog in ("kubectl", "helm", "terraform", "ansible", "ansible-playbook", "vercel", "netlify", "firebase",
                "heroku", "fly", "flyctl"):
        verb = (args[0].lower() if args else "")
        if verb in ("get", "describe", "logs", "top", "version", "plan", "list", "status", "show", "output", "validate"):
            return read(f"terminal.{prog}", f"run: {text}")
        return high(f"terminal.{prog}", f"change deployed infrastructure: {text}")
    if prog in ("docker", "podman"):
        verb = (args[0].lower() if args else "")
        if verb in ("ps", "images", "logs", "inspect", "version", "info", "stats", "top", "history"):
            return read("terminal.docker", f"run: {text}")
        if verb in ("rm", "rmi", "prune", "system", "volume", "network") or "--force" in args or "-f" in args:
            return high("terminal.docker_delete", f"remove containers or data: {text}")
        return confirm("terminal.docker", run_text)
    if prog in ("curl", "wget", "iwr", "irm", "invoke-webrequest", "invoke-restmethod"):
        sends = re.search(r"(^|\s)(-d|--data\S*|-F|--form\S*|-T|--upload-file|-X\s*(POST|PUT|PATCH|DELETE)|--post\S*"
                          r"|-Method\s+(Post|Put|Patch|Delete)|-Body|-InFile)\b", text, re.IGNORECASE)
        if sends:
            return confirm("terminal.network_send", f"send data to a website: {text}")
        outputs = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-o", "--output", "-O", "-OutFile")]
        if outputs:
            return combine(_path_checks(outputs, "create", ctx), read("terminal.download", f"download: {text}"))
        return read("terminal.http_get", f"fetch a web page: {text}")
    if prog in _PROCESS_CONFIRM:
        if prog in ("chmod", "chown", "chgrp", "attrib", "icacls") and any(a in ("-R", "/s", "/S", "--recursive") for a in args):
            targets = _non_flags(args)[1:] or ["."]
            decisions = _path_checks(targets, "modify", ctx, recursive=True)
            return combine(decisions, high("terminal.permissions", f"change permissions recursively: {text}"))
        if prog == "sc" and args and args[0].lower() in ("query", "qc", "queryex"):
            return read("terminal.sc", f"query services: {text}")
        if prog == "sc" and args and args[0].lower() in ("delete", "config", "create"):
            return high("terminal.service_config", f"change a Windows service: {text}")
        if prog == "systemctl" and args and args[0] in ("status", "list-units", "is-active", "show", "cat"):
            return read("terminal.systemctl", f"query services: {text}")
        if prog in ("gh", "az", "aws", "gcloud") and re.search(r"\b(auth|login|logout|configure|credential|secret)\b", text, re.I):
            return high("terminal.credentials", f"change cloud credentials: {text}")
        if prog in ("gh", "az", "aws", "gcloud") and re.search(r"\b(delete|destroy|remove|rm)\b", text, re.I):
            return high(f"terminal.{prog}_delete", f"delete cloud/GitHub resources: {text}")
        if prog == "hermes":
            return confirm("terminal.hermes", f"run a Hermes command: {text}")
        return confirm(f"terminal.{prog}", run_text)
    if prog in ("invoke-expression", "iex", "eval", "source", "."):
        return confirm("terminal.eval", run_text)
    if prog.startswith("./") or prog.endswith((".sh", ".py", ".js", ".ps1", ".bat", ".cmd")) or "/" in prog:
        return confirm("terminal.script", f"run the script: {text}")
    return confirm(f"terminal.{prog}", f"run '{prog}', a command XENIO does not recognise: {text}")


def _classify_git(args: list[str], ctx: _Ctx, text: str) -> Decision:
    settings: list[str] = []
    while args and (args[0] in ("-C", "-c", "--git-dir", "--work-tree") or args[0].startswith("-")):
        if args[0] == "-c":
            settings.append(args[1] if len(args) > 1 else "")
        elif args[0].startswith(("--config-env", "--exec-path")):
            settings.append(args[0])
        args = args[2:] if args[0] in ("-C", "-c", "--git-dir", "--work-tree") else args[1:]
    if not args:
        return read("terminal.git", f"run: {text}")
    sub, rest = args[0].lower(), args[1:]
    if any(not _GIT_HARMLESS_CONFIG.fullmatch(s) for s in settings) or any(
            _GIT_RUNS_PROGRAM_OPTION.fullmatch(a) for a in rest) or (
            sub in ("clone", "ls-remote") and any(a == "-u" or a == "-c" for a in rest)):
        return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.git_{sub}",
                        f"run git with a setting or option that can start another program: {text}")
    if sub in _GIT_HIGH or (sub == "config" and re.search(r"credential", text, re.I)):
        return Decision(RiskLevel.HIGH_RISK, f"terminal.git_{sub}", f"run the git command: {text}")
    if sub == "push" and (any(a in ("--force", "-f", "--force-with-lease", "--delete", "--mirror", "--prune") for a in rest)
                          or any(a.startswith(("+", ":")) for a in _non_flags(rest))):
        return Decision(RiskLevel.HIGH_RISK, "terminal.git_force_push", f"force-push or delete remote history: {text}")
    if sub == "reset" and "--hard" in rest or sub == "clean" and any(re.fullmatch(r"-\w*f\w*", a) for a in rest):
        return Decision(RiskLevel.HIGH_RISK, f"terminal.git_{sub}", f"discard uncommitted work: {text}")
    branch = _classify_git_branch(sub, rest, text)
    if branch is not None:
        return branch
    if sub in ("branch", "tag", "remote", "stash", "worktree", "config", "submodule") and (
            not rest or all(a in ("-a", "-v", "-vv", "-l", "--list", "-r", "--show-current", "list", "show",
                                  "--get", "--get-all", "--list", "-l", "get-url", "status") for a in rest[:1])):
        return read(f"terminal.git_{sub}", f"run: {text}")
    if sub in _GIT_READ:
        return read(f"terminal.git_{sub}", f"run: {text}")
    if sub in _GIT_SAFE_WRITE:
        return Decision(RiskLevel.SAFE_WRITE, f"terminal.git_{sub}", f"run: {text}")
    if sub == "push":
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.git_push", f"push code to a remote repository: {text}")
    if sub in _GIT_CONFIRM:
        return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.git_{sub}", f"run the git command: {text}")
    return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.git_{sub}", f"run the git command: {text}")


def _classify_git_branch(sub: str, rest: list[str], text: str) -> Decision | None:
    """Creating a branch is a safe local change; deleting one is not."""
    flags = [a for a in rest if a.startswith("-")]
    names = _non_flags(rest)
    if sub == "branch" and names:
        if "-D" in flags or ("--delete" in flags or "-d" in flags) and ("--force" in flags or "-f" in flags):
            return Decision(RiskLevel.HIGH_RISK, "terminal.git_branch_delete",
                            f"force-delete the branch {names[0]} (unmerged work on it is lost): {text}")
        if "-d" in flags or "--delete" in flags:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.git_branch_delete", f"delete the branch {names[0]}: {text}")
        if any(f in flags for f in ("-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force", "-u",
                                    "--set-upstream-to", "--unset-upstream", "--edit-description")):
            return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.git_branch", f"run the git command: {text}")
        if not flags:
            return Decision(RiskLevel.SAFE_WRITE, "terminal.git_branch_create", f"create the branch {names[0]}")
    if (sub == "checkout" and rest[:1] == ["-b"] or sub == "switch" and rest[:1] in (["-c"], ["--create"])) and len(names) >= 1:
        return Decision(RiskLevel.SAFE_WRITE, "terminal.git_branch_create", f"create and switch to the branch {names[0]}")
    return None


# GitHub CLI: reading is free, writing asks, and releases / settings / deletions / credentials are HIGH_RISK.
_GH_READ_VERBS = {"list", "view", "status", "diff", "checks", "search", "browse", "watch", "download", "ls"}
_GH_HIGH = {("repo", "delete"), ("repo", "archive"), ("repo", "unarchive"), ("repo", "rename"), ("repo", "edit"),
            ("repo", "transfer"), ("release", "create"), ("release", "edit"), ("release", "delete"),
            ("release", "upload"), ("release", "delete-asset"), ("issue", "delete"), ("issue", "transfer"),
            ("gist", "delete"), ("run", "delete"), ("cache", "delete"), ("label", "delete"), ("variable", "set"),
            ("variable", "delete"), ("secret", "set"), ("secret", "delete"), ("ssh-key", "add"), ("ssh-key", "delete"),
            ("gpg-key", "add"), ("gpg-key", "delete"), ("ruleset", "create"), ("ruleset", "delete"),
            ("codespace", "delete"), ("org", "delete")}


def classify_gh(args: list[str], text: str) -> Decision:
    words = _non_flags(args)
    group = words[0].lower() if words else ""
    verb = words[1].lower() if len(words) > 1 else ""
    flags = [a for a in args if a.startswith("-")]
    if not group or group in ("help", "version", "--version", "status", "search", "browse", "completion"):
        return read("terminal.gh", f"run: {text}")
    if group == "auth":
        if verb == "token" or any(f in flags for f in ("--show-token", "-t")):
            return blocked("terminal.gh_token", f"run: {text}", "it would print your GitHub access token")
        if verb == "status":
            return read("terminal.gh_auth", f"check GitHub sign-in status: {text}")
        return Decision(RiskLevel.HIGH_RISK, "terminal.gh_auth", f"change your GitHub sign-in: {text}")
    if group == "api":
        method = next((args[i + 1].upper() for i, a in enumerate(args[:-1]) if a in ("-X", "--method")), "")
        sends_fields = any(f.split("=")[0] in ("-f", "-F", "--field", "--raw-field", "--input") for f in flags)
        method = method or ("POST" if sends_fields else "GET")
        if method == "GET":
            return read("terminal.gh_api", f"read from the GitHub API: {text}")
        if method == "DELETE":
            return Decision(RiskLevel.HIGH_RISK, "terminal.gh_api_delete", f"delete something on GitHub: {text}")
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.gh_api_write", f"change something on GitHub: {text}")
    if (group, verb) in _GH_HIGH or verb in ("delete", "remove", "rm"):
        return Decision(RiskLevel.HIGH_RISK, f"terminal.gh_{group}_{verb}", f"change or delete GitHub {group}s or settings: {text}")
    if group in ("secret", "variable", "ssh-key", "gpg-key", "config") and verb not in ("list", "get"):
        return Decision(RiskLevel.HIGH_RISK, f"terminal.gh_{group}", f"change GitHub credentials or settings: {text}")
    if verb in _GH_READ_VERBS or group in ("secret", "variable", "ssh-key", "gpg-key", "config"):
        return read(f"terminal.gh_{group}", f"run: {text}")
    if group == "pr" and verb == "merge":
        if "--admin" in flags:
            return Decision(RiskLevel.HIGH_RISK, "terminal.gh_pr_merge", f"merge a pull request bypassing branch protection: {text}")
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.gh_pr_merge", f"merge a pull request on GitHub: {text}")
    if (group, verb) in (("pr", "checkout"), ("repo", "clone"), ("gist", "clone")):
        return Decision(RiskLevel.SAFE_WRITE, f"terminal.gh_{group}_{verb}", f"run: {text}")
    if group in ("pr", "issue", "gist", "repo", "release", "label", "run", "workflow", "project", "discussion"):
        return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.gh_{group}_{verb or 'write'}",
                        f"{verb or 'change'} GitHub {group} content (visible to others): {text}")
    return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.gh_{group}", f"run the GitHub command: {text}")


# Himalaya email CLI (V4: email is read-only - sending is not enabled at all).
_HIMALAYA_VALUE_OPTIONS = {"-c", "--config", "-a", "--account", "--log-level", "--log-file", "-m", "--mailbox",
                           "-p", "--page", "-s", "--page-size", "-w", "--max-width", "--backend", "-d", "--dir",
                           "--downloads-dir"}
_HIMALAYA_READ = {("envelope", "list"), ("envelope", "search"), ("envelope", "get"), ("message", "read"),
                  ("message", "export"), ("message", "get"), ("mailbox", "list"), ("folder", "list"), ("account", "list"),
                  ("account", "check"), ("flag", "list"), ("attachment", "list"), ("template", "write"),
                  ("template", "reply"), ("template", "forward")}
_HIMALAYA_BACKENDS = {"gmail", "imap", "jmap", "msgraph", "maildir", "mbox", "pimdir", "smtp", "sieve"}


def classify_himalaya(args: list[str], text: str) -> Decision:
    words, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in _HIMALAYA_VALUE_OPTIONS:
            skip = True
        elif not a.startswith("-"):
            words.append(a.lower())
    group, verb = (words + ["", ""])[:2]
    lowered = [w for w in words[:4]]
    if not group or group in ("configure", "wizard"):
        return Decision(RiskLevel.HIGH_RISK, "terminal.himalaya_setup",
                        f"set up an email account (asks for its password): {text}")
    if group in ("help", "completion", "manual", "json-schema") or "--help" in args or "-h" in args:
        return read("terminal.himalaya", f"run: {text}")
    if "send" in lowered or group == "smtp" or (group == "message" and verb in ("write", "reply", "forward", "send")) \
            or (group == "template" and verb == "send") or ("drafts" in lowered and "send" in lowered):
        return blocked("terminal.email_send", f"send an email: {text}",
                       "sending email is not enabled in XENIO yet (email is read-only)")
    if (group, verb) in _HIMALAYA_READ or (group in _HIMALAYA_BACKENDS and set(lowered[1:]) & {"list", "get", "search",
                                                                                              "read", "fetch", "export"}
                                           and not set(lowered[1:]) & {"delete", "trash", "modify", "update", "create"}):
        return read("terminal.himalaya_read", f"read email: {text}")
    if (group, verb) == ("attachment", "download"):
        return Decision(RiskLevel.SAFE_WRITE, "terminal.himalaya_attachment", f"save email attachments: {text}")
    if verb in ("expunge", "purge") or (group == "mailbox" and verb == "delete"):
        return Decision(RiskLevel.HIGH_RISK, "terminal.himalaya_delete", f"permanently delete email: {text}")
    if verb in ("delete", "move", "copy", "add", "set", "remove", "create", "save", "edit") or group == "sieve":
        return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.himalaya_{group}_{verb}", f"change your mailbox: {text}")
    return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.himalaya", f"run the email command: {text}")


def _classify_package_manager(prog: str, args: list[str], ctx: _Ctx, text: str) -> Decision:
    words = [a for a in args if not a.startswith("-")]
    verb = words[0].lower() if words else ""
    if prog == "uv" and verb == "pip":
        verb = words[1].lower() if len(words) > 1 else ""
    if prog in ("go", "cargo", "dotnet") and verb in ("test",):
        return read("terminal.test", f"run the tests: {text}")
    if prog in ("go", "cargo", "dotnet", "npm", "pnpm", "yarn") and (verb in _PKG_BUILD or (verb == "run" and len(words) > 1 and words[1] in _PKG_BUILD)):
        return Decision(RiskLevel.SAFE_WRITE, "terminal.build", f"build the project: {text}")
    if verb in ("config", "env") and len(words) > 1 and words[1].lower() not in ("get", "list", "ls", "show"):
        return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.{prog}_config", f"change {prog} settings: {text}")
    if not verb or verb in _PKG_READ:
        return read(f"terminal.{prog}", f"run: {text}")
    if verb in _PKG_TEST or (verb == "run" and len(words) > 1 and words[1].lower() in ("test", "tests", "unit")):
        return read("terminal.test", f"run the tests: {text}")
    if verb in _PKG_PUBLISH:
        return Decision(RiskLevel.HIGH_RISK, "terminal.publish", f"publish a package publicly: {text}")
    if verb in ("login", "adduser", "logout", "token", "set-token"):
        return Decision(RiskLevel.HIGH_RISK, "terminal.credentials", f"change package-registry credentials: {text}")
    if verb in _PKG_CHANGE:
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.install_package", f"install or change software packages: {text}")
    if verb in _PKG_RUN:
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.run_project", f"run the project: {text}")
    return Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.{prog}", f"run the command: {text}")


def _classify_interpreter(prog: str, args: list[str], ctx: _Ctx, text: str) -> Decision:
    if args[:2] in (["-m", "pytest"], ["-m", "unittest"], ["-m", "doctest"]):
        return read("terminal.test", f"run the tests: {text}")
    if args[:2] == ["-m", "pip"]:
        return _classify_package_manager("pip", args[2:], ctx, text)
    if prog in ("python", "py") and args[:1] == ["-c"] and len(args) > 1:
        return replace(classify_python_code(args[1], ctx), summary=f"run Python code: {args[1][:120]}")
    if prog == "node" and args[:1] in (["-e"], ["-p"]) and len(args) > 1:
        if re.fullmatch(r"\s*(console\.log\()?\s*process\.(version|versions(\.\w+)?|platform|arch)\s*\)?\s*;?\s*", args[1]):
            return read("terminal.version", f"check the node version: {text}")
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.node_eval", f"run JavaScript code: {args[1][:120]}")
    if prog in ("python", "py") and args[:1] in (["-VV"],):
        return read("terminal.version", f"check the {prog} version")
    return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.run_program", f"run a program: {text}")


def _classify_shell(prog: str, args: list[str], ctx: _Ctx, text: str) -> Decision:
    nested = None
    lowered = [a.lower() for a in args]
    for flag in ("-c", "/c", "/k", "-command", "-c", "-commandwithargs"):
        if flag in lowered:
            idx = lowered.index(flag)
            nested = " ".join(args[idx + 1:])
            break
    if nested is None and prog in ("powershell", "pwsh") and args and not args[0].startswith("-"):
        nested = " ".join(args)
    if nested is None:
        if any(a.lower() in ("-encodedcommand", "-enc", "-e") for a in args):
            return Decision(RiskLevel.HIGH_RISK, "terminal.encoded", f"run an encoded/hidden command: {text}")
        return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.shell", f"run a shell script: {text}")
    if prog in ("powershell", "pwsh"):
        return classify_powershell(nested, ctx.paths, ctx.cwd, ctx.depth + 1)
    return classify_command(nested, ctx.paths, ctx.cwd, ctx.depth + 1)


# PowerShell (Hermes may call `powershell -Command ...` from bash on Windows)
_PS_ALIASES = {
    "gci": "get-childitem", "ls": "get-childitem", "dir": "get-childitem", "gc": "get-content", "cat": "get-content",
    "type": "get-content", "ni": "new-item", "md": "new-item-dir", "mkdir": "new-item-dir", "ri": "remove-item",
    "rm": "remove-item", "del": "remove-item", "erase": "remove-item", "rd": "remove-item", "rmdir": "remove-item",
    "mi": "move-item", "mv": "move-item", "move": "move-item", "rni": "rename-item", "ren": "rename-item",
    "cpi": "copy-item", "cp": "copy-item", "copy": "copy-item", "sc": "set-content", "ac": "add-content",
    "spps": "stop-process", "kill": "stop-process", "iex": "invoke-expression", "iwr": "invoke-webrequest",
    "irm": "invoke-restmethod", "echo": "write-output", "write": "write-output", "sl": "set-location",
    "cd": "set-location", "pwd": "get-location", "gps": "get-process", "ps": "get-process", "gsv": "get-service",
    "saps": "start-process", "start": "start-process",
}
_PS_SWITCHES = {"recurse", "force", "whatif", "confirm", "verbose", "file", "directory", "hidden", "readonly",
                "system", "passthru", "nonewline", "append", "noclobber", "debug", "recursive", "r", "wait"}
_PS_READ = {"get-childitem", "get-content", "get-item", "get-itemproperty", "test-path", "get-location", "get-process",
            "get-service", "get-command", "get-date", "write-output", "write-host", "select-object", "where-object",
            "sort-object", "format-table", "format-list", "measure-object", "get-member", "resolve-path",
            "get-filehash", "select-string", "get-computerinfo", "get-host", "set-location", "out-string",
            "convertto-json", "convertfrom-json", "get-psdrive", "get-volume", "get-netipaddress", "foreach-object"}


def classify_powershell(script: str, paths: PathPolicy, cwd: str | os.PathLike | None = None, _depth: int = 1) -> Decision:
    text = script.strip().strip('"').strip("'")
    if re.search(r"\$psversiontable", text, re.IGNORECASE) and not re.search(r"(remove|set|new|stop)-", text, re.I):
        return read("terminal.version", "check the PowerShell version")
    if re.search(r"\benv:", text, re.IGNORECASE) and not re.search(r"env:(path|username|computername|os|temp)\b", text, re.I):
        return blocked("terminal.env_dump", f"run: {text}", "reading environment variables could expose API keys")
    if _SECRET_MENTION.search(text):
        return blocked("terminal.secret_file", f"run: {text}", "the command touches a protected secrets file")
    ctx = _Ctx(paths=paths, cwd=str(cwd or paths.workspace or Path.cwd()), depth=_depth)
    decisions = []
    for statement in re.split(r"[;|\n]", text):
        try:
            words = shlex.split(statement, posix=False)
        except ValueError:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.powershell", f"run PowerShell: {text}")
        if not words:
            continue
        verb = _PS_ALIASES.get(words[0].lower(), words[0].lower())
        params, positional, i = {}, [], 1
        while i < len(words):
            w = words[i].strip('"').strip("'")
            if w.startswith("-") and len(w) > 1:
                name = w[1:].lower().rstrip(":")
                if name not in _PS_SWITCHES and i + 1 < len(words) and not words[i + 1].startswith("-"):
                    params[name] = words[i + 1].strip('"').strip("'")
                    i += 2
                else:
                    params[name] = True
                    i += 1
            else:
                positional.append(w)
                i += 1
        target = params.get("path") or params.get("literalpath") or (positional[0] if positional else None)
        recurse = any(k.startswith("rec") for k in params)
        if verb in _PS_READ:
            if verb in ("get-content", "get-item", "select-string", "get-filehash") and isinstance(target, str):
                decisions.append(paths.check(target, "read", ctx.cwd))
            else:
                decisions.append(read(f"terminal.ps_{verb}", f"run: {statement.strip()}"))
        elif verb in ("new-item", "new-item-dir"):
            if isinstance(target, str):
                op = "modify" if _exists(ctx, target) else "create"
                decisions.append(paths.check(target, op, ctx.cwd))
            else:
                decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.ps_new_item", f"run: {statement.strip()}"))
        elif verb in ("set-content", "add-content", "out-file"):
            dest = params.get("path") or params.get("filepath") or (positional[0] if positional else None)
            decisions.append(paths.check(dest, "modify", ctx.cwd) if isinstance(dest, str) else
                             Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.ps_write", f"run: {statement.strip()}"))
        elif verb == "remove-item":
            decisions.append(paths.check(target, "delete", ctx.cwd, recursive=recurse) if isinstance(target, str) else
                             Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.ps_delete", f"run: {statement.strip()}"))
        elif verb in ("move-item", "rename-item", "copy-item"):
            op = "read" if verb == "copy-item" else "move"
            items = [p for p in (target, params.get("destination") or (positional[1] if len(positional) > 1 else None))
                     if isinstance(p, str)]
            checks = [paths.check(items[0], op, ctx.cwd)] if items else []
            if len(items) > 1:
                checks.append(paths.check(items[1], "create", ctx.cwd))
            decisions.append(combine(checks, Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.ps_{verb}", f"run: {statement.strip()}")))
            if verb != "copy-item":
                decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.ps_{verb}", f"run: {statement.strip()}"))
        elif verb in ("stop-process", "stop-service", "restart-service", "start-service", "set-service", "start-process"):
            decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.ps_{verb}", f"run: {statement.strip()}"))
        elif verb in ("set-itemproperty", "new-itemproperty", "remove-itemproperty", "set-executionpolicy",
                      "restart-computer", "stop-computer", "format-volume", "clear-disk", "initialize-disk",
                      "remove-partition", "set-localuser", "new-localuser", "remove-localuser", "invoke-expression"):
            decisions.append(Decision(RiskLevel.HIGH_RISK, f"terminal.ps_{verb}", f"run the system command: {statement.strip()}"))
        elif verb in ("invoke-webrequest", "invoke-restmethod"):
            sends = any(k in params for k in ("body", "infile")) or str(params.get("method", "")).lower() in ("post", "put", "patch", "delete")
            decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, "terminal.network_send", f"send data to a website: {statement.strip()}")
                             if sends else read("terminal.http_get", f"fetch a web page: {statement.strip()}"))
        else:
            decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, f"terminal.ps_{verb}", f"run PowerShell: {statement.strip()}"))
    return combine(decisions, read("terminal.noop", "run nothing"))


# =============================================================================
# Web and browser
# =============================================================================
# Reading the web is free. Typing and clicking ordinary things is a safe change. Anything that
# submits, sends, posts, uploads or changes an account asks first; payments, password/2FA changes
# and account deletion are HIGH_RISK. Reading cookies, saved logins or site storage, putting a
# secret into a page, or switching off browser security is blocked.

_URL_WITH_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_URL_HOSTLIKE = re.compile(r"^[\w.-]+(:\d+)?([/?#]|$)")
_URL_USERINFO = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#@\s]*@")
_BROWSER_SECRET_PAGES = re.compile(
    r"^(chrome|edge|brave|opera|vivaldi)://(settings/(passwords|autofill|payments|addresses|cookies|content/cookies"
    r"|siteData)|password-manager|wallet)|^about:(logins|preferences#privacy)", re.IGNORECASE)
_BROWSER_INTERNAL = {"chrome", "edge", "brave", "opera", "vivaldi", "about", "chrome-extension", "moz-extension"}

_MONEY_OR_ACCOUNT = re.compile(
    r"\b(pay|payment|purchase|buy|checkout|check[ _-]out|place[ _-]?order|order[ _-]?now|confirm[ _-]?order|transfer"
    r"|send[ _-]?money|withdraw|donate|wire|billing|upgrade[ _-]?plan|delete[ _-]?(my[ _-]?)?account|close[ _-]?account"
    r"|deactivate|change[ _-]?password|reset[ _-]?password|update[ _-]?password|new[ _-]?password|two[ _-]?factor|2fa"
    r"|recovery[ _-]?(code|email|phone))\b", re.IGNORECASE)
_SUBMIT_WORDS = re.compile(
    r"\b(submit|send|post|publish|tweet|reply|comment|share|upload|save|update|apply|confirm|delete|remove|sign[ _-]?in"
    r"|log[ _-]?in|login|sign[ _-]?up|signup|register|follow|unfollow|like|unsubscribe|subscribe|accept|agree|approve"
    r"|merge|invite|create|add[ _-]?to[ _-]?cart|book|reserve|request|join|vote|review|report|block|archive|enroll)\b",
    re.IGNORECASE)
_SEARCH_WORDS = re.compile(r"\b(search|query|find|lookup|filter)\b|name=['\"]?q\b|#q\b", re.IGNORECASE)
_SENSITIVE_FIELD = re.compile(r"pass(word|wd|code|phrase)?\b|\bpin\b|\botp\b|one[ _-]?time|2fa|totp|cvv|cvc|card[ _-]?(number|no)"
                              r"|cc[ _-]?num|iban|routing|\bssn\b|social[ _-]?security", re.IGNORECASE)

_JS_SECRETS = re.compile(r"document\s*\.\s*cookie|\b(localStorage|sessionStorage|indexedDB|cookieStore)\b"
                         r"|navigator\s*\.\s*credentials|PasswordCredential|type\s*=\s*\\?[\"']?password", re.IGNORECASE)
_JS_OPAQUE = re.compile(r"\beval\s*\(|\bFunction\s*\(|setTimeout\s*\(\s*['\"`]|setInterval\s*\(\s*['\"`]|\bimport\s*\(",
                        re.IGNORECASE)
_JS_SENDS = re.compile(r"\bfetch\s*\(|XMLHttpRequest|sendBeacon|new\s+WebSocket|EventSource|\.submit\s*\(|requestSubmit"
                       r"|navigator\s*\.\s*clipboard|\bpostMessage\s*\(|\bFormData\b", re.IGNORECASE)
_JS_CLICKS = re.compile(r"\.click\s*\(|dispatchEvent\s*\(|new\s+(Mouse|Pointer|Keyboard|Submit)Event", re.IGNORECASE)
_JS_CHANGES = re.compile(r"\.(value|checked|selected|innerHTML|outerHTML|textContent|innerText)\s*=(?!=)|setAttribute\s*\("
                         r"|\.remove\s*\(|removeChild|appendChild|insertAdjacent|location(\.href)?\s*=(?!=)"
                         r"|location\.(assign|replace)\s*\(|history\.(back|forward|go)\s*\(", re.IGNORECASE)

# browser_exec (Hermes' Browser Use mode) runs Python with these helpers pre-imported.
_BROWSER_NAV_HELPERS = {"new_tab", "goto_url", "goto", "navigate", "open_url"}
_BROWSER_READ_HELPERS = {"wait_for_load", "page_info", "capture_screenshot", "ensure_real_tab", "screenshot",
                         "list_tabs", "get_text", "wait", "sleep"}
_PY_SAFE_BUILTINS = {"print", "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "sorted", "reversed",
                     "enumerate", "range", "min", "max", "sum", "any", "all", "zip", "map", "filter", "isinstance",
                     "round", "repr", "abs", "format", "iter", "next", "type", "hasattr", "frozenset", "divmod", "chr",
                     "ord", "hex", "bytes", "slice", "ValueError", "Exception", "KeyError", "TypeError", "RuntimeError"}
_PY_SYSTEM_NAMES = {"os", "subprocess", "shutil", "socket", "ctypes", "winreg", "pathlib", "Path", "requests", "httpx",
                    "urllib", "importlib", "builtins", "pickle", "marshal", "globals", "locals", "vars", "getattr",
                    "setattr", "delattr", "open", "__builtins__"}

_CDP_BLOCKED = re.compile(
    r"^(Network\.(getCookies|getAllCookies|setCookies?|deleteCookies|clearBrowserCookies|getRequestPostData)"
    r"|Storage\.(getCookies|setCookies|clearCookies|clearDataForOrigin|getSharedStorageEntries)|DOMStorage\.|IndexedDB\."
    r"|CacheStorage\.|WebAuthn\.|Fetch\.continueWithAuth|Security\.(setIgnoreCertificateErrors|disable)"
    r"|Page\.setBypassCSP|Autofill\.)", re.IGNORECASE)
_CDP_READ = re.compile(
    r"^((DOM|CSS|DOMSnapshot)\.(get|describe|query|resolve|request|enable|disable)|Accessibility\.|Overlay\.|Log\.|Performance\."
    r"|Page\.(captureScreenshot|getLayoutMetrics|getFrameTree|getNavigationHistory|printToPDF|enable|disable|getResourceTree)"
    r"|Runtime\.(getProperties|enable|disable)|Target\.(getTargets|getTargetInfo|attachToTarget|activateTarget|setDiscoverTargets)"
    r"|Browser\.(getVersion|getWindowForTarget)|Network\.(enable|disable)|Emulation\.)", re.IGNORECASE)


def normalize_url(raw: str) -> str:
    """``geeksforgeeks.org`` -> ``https://geeksforgeeks.org``; links with a scheme are kept as they are."""
    text = (raw or "").strip().strip("<>\"'") if isinstance(raw, str) else ""
    if not text or _URL_WITH_SCHEME.match(text):
        return text
    if _URL_HOSTLIKE.match(text) and not re.match(r"^[a-zA-Z]:[\\/]", text):
        host = re.split(r"[:/?#]", text, maxsplit=1)[0].lower()
        return ("http://" if host in ("localhost", "127.0.0.1") else "https://") + text
    return text


def url_host(url: str) -> str:
    try:
        return (urlsplit(normalize_url(url)).hostname or "").lower()
    except ValueError:
        return ""


def contains_secret(text: str, strict: bool = True) -> bool:
    """True if ``text`` holds an API key, token, private key or a value of a secret environment variable.
    ``strict`` also counts ``password=...`` style assignments."""
    if not text:
        return False
    for value in secret_env_values():
        if value in text:
            return True
    patterns = _SECRET_VALUE_PATTERNS if strict else _SECRET_VALUE_PATTERNS[:-1]
    return any(p.search(text) for p, _ in patterns)


def classify_url(raw: str, paths: PathPolicy | None = None, cwd: str | os.PathLike | None = None, *,
                 verb: str = "open") -> Decision:
    url = normalize_url(raw)
    if not url:
        return blocked("web.url", f"{verb} a web page", "the link is missing")
    if any(ch in url for ch in "\x00\r\n") or len(url) > 8192:
        return blocked("web.url", f"{verb} a web page", "the link is not valid")
    if _URL_USERINFO.match(url):
        return blocked("web.url", f"{verb} a link with a login in it", "the link contains a username or password")
    if contains_secret(url):
        return blocked("web.url", f"{verb} a link", "the link contains an API key, token or password")
    scheme = url.split(":", 1)[0].lower()
    if scheme in ("http", "https"):
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        if not host:
            return blocked("web.url", f"{verb} {url[:80]}", "the link has no website name")
        return read("web.open", f"{verb} {host}")
    if scheme == "file" or len(scheme) == 1:  # file:///C:/x or a bare Windows path C:\x
        local = unquote(urlsplit(url).path) if scheme == "file" else url
        if scheme == "file" and re.match(r"^/[a-zA-Z]:", local):
            local = local[1:]
        if paths is None:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "web.open_file", f"{verb} the local file {local}")
        check = paths.check(local, "read", cwd)
        return check if check.blocked else read("web.open_file", f"{verb} {check.summary.removeprefix('read ')}")
    if _BROWSER_SECRET_PAGES.match(url):
        return blocked("web.browser_secrets", f"{verb} {url}", "that browser page shows saved passwords, cookies or payment data")
    if scheme in _BROWSER_INTERNAL:
        return Decision(RiskLevel.CONFIRM_REQUIRED, "web.browser_internal", f"{verb} the browser's internal page {url[:80]}")
    if scheme == "javascript":
        js = classify_js(unquote(url[len("javascript:"):]))
        return js if js.blocked else replace(js, level=max(js.level, RiskLevel.CONFIRM_REQUIRED))
    if scheme in ("data", "blob", "view-source"):
        return read("web.open", f"{verb} an inline page")
    if scheme in ("mailto", "tel", "sms"):
        return Decision(RiskLevel.CONFIRM_REQUIRED, "web.app_link", f"start a message or call ({scheme} link)")
    return Decision(RiskLevel.CONFIRM_REQUIRED, "web.app_link", f"{verb} a {scheme}: link, which starts another program")


def _interaction(target: str, what: str) -> Decision:
    """A click or submit, judged by the words on/around its target ("Place order", "Search", ...)."""
    label = " ".join((target or "").split())[:100]
    shown = f"{what} ({label})" if label else what
    if _MONEY_OR_ACCOUNT.search(target or ""):
        return Decision(RiskLevel.HIGH_RISK, "browser.account_or_payment",
                        f"{shown} - it looks like a payment, purchase, password or account change")
    if _SUBMIT_WORDS.search(target or "") and not _SEARCH_WORDS.search(target or ""):
        return Decision(RiskLevel.CONFIRM_REQUIRED, "browser.submit", f"{shown} - it may submit, send or change something")
    return Decision(RiskLevel.SAFE_WRITE, "browser.interact", shown)


def classify_typing(target: str, text: str) -> Decision:
    label = " ".join((target or "").split())[:60] or "a field"
    if contains_secret(text or "", strict=False):
        return blocked("browser.type_secret", f"type into {label} on a website",
                       "the text contains an API key, token or other secret")
    if _SENSITIVE_FIELD.search(target or ""):
        return Decision(RiskLevel.HIGH_RISK, "browser.type_sensitive", f"type into a password, code or payment field ({label})")
    return Decision(RiskLevel.SAFE_WRITE, "browser.type", f"type into {label} on the page")


def classify_js(expression: str) -> Decision:
    text = expression or ""
    short = " ".join(text.split())[:100]
    if _JS_SECRETS.search(text):
        return blocked("browser.js_secrets", f"run JavaScript in the page: {short}",
                       "it reads cookies, saved logins, passwords or site storage")
    if contains_secret(text, strict=False):
        return blocked("browser.js_secrets", f"run JavaScript in the page: {short}", "it would put a secret into a web page")
    if _JS_OPAQUE.search(text):
        return Decision(RiskLevel.CONFIRM_REQUIRED, "browser.js", f"run JavaScript XENIO cannot check: {short}")
    if _JS_SENDS.search(text):
        risk = _interaction(text, "submit or send data from the page")
        form_only = not re.search(r"\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|clipboard|postMessage"
                                  r"|FormData", text, re.IGNORECASE)
        if form_only and risk.level == RiskLevel.SAFE_WRITE and _SEARCH_WORDS.search(text):
            return Decision(RiskLevel.SAFE_WRITE, "browser.search", "submit a search on the page")
        return replace(risk, level=max(risk.level, RiskLevel.CONFIRM_REQUIRED),
                       action="browser.submit" if risk.level < RiskLevel.HIGH_RISK else risk.action)
    if _JS_CLICKS.search(text):
        return _interaction(text, "click on the page")
    if _JS_CHANGES.search(text):
        return Decision(RiskLevel.SAFE_WRITE, "browser.js_change", f"change the page or move to another page: {short}")
    return read("browser.js_read", "read the page")


def classify_cdp(method: str, params: dict | None, paths: PathPolicy | None = None, cwd=None) -> Decision:
    name = (method or "").strip()
    p = params if isinstance(params, dict) else {}
    if not name:
        return blocked("browser.cdp", "send a browser command", "the command name is missing")
    if _CDP_BLOCKED.match(name):
        return blocked("browser.cdp_secrets", f"send the browser command {name}",
                       "it reads or changes cookies, saved logins or site storage, or turns off browser security")
    if name in ("Runtime.evaluate", "Runtime.callFunctionOn"):
        return classify_js(str(p.get("expression") or p.get("functionDeclaration") or ""))
    if name in ("Page.navigate", "Target.createTarget"):
        return classify_url(str(p.get("url") or ""), paths, cwd)
    if name == "Input.insertText":
        return classify_typing("", str(p.get("text") or ""))
    if name == "Input.dispatchKeyEvent":
        key = str(p.get("key") or p.get("code") or "")
        if key.lower() in ("enter", "return", "numpadenter"):
            return Decision(RiskLevel.CONFIRM_REQUIRED, "browser.submit", "press Enter on the page, which may submit a form")
        return classify_typing("", str(p.get("text") or ""))
    if name.startswith("Input.dispatchMouseEvent") or name.startswith("Input.dispatchTouchEvent"):
        return Decision(RiskLevel.CONFIRM_REQUIRED, "browser.click_xy",
                        "click on the page at a screen position (XENIO cannot see what is there)")
    if name == "Page.handleJavaScriptDialog":
        return (Decision(RiskLevel.CONFIRM_REQUIRED, "browser.dialog", "press OK on a dialog box on the page")
                if p.get("accept") else Decision(RiskLevel.SAFE_WRITE, "browser.dialog", "close a dialog box on the page"))
    if _CDP_READ.match(name):
        return read("browser.cdp_read", f"inspect the page ({name})")
    return Decision(RiskLevel.CONFIRM_REQUIRED, "browser.cdp", f"send the raw browser command {name}")


def _string_parts(node: ast.AST | None) -> str:
    """The literal text inside an argument (also the fixed parts of f-strings and concatenations)."""
    if node is None:
        return ""
    return " ".join(n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str))


def _is_literal(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def classify_browser_code(code: str, paths: PathPolicy | None = None, cwd: str | os.PathLike | None = None) -> Decision:
    """Hermes' ``browser_exec``: Python driving the browser through pre-imported helpers
    (new_tab, goto_url, page_info, js, fill_input, click_at_xy, cdp, ...). Each helper call is judged
    by what it does, so reading pages never asks and only real submissions/sends do."""
    if not isinstance(code, str) or not code.strip():
        return blocked("browser.exec", "run browser code", "the code is missing")
    base = classify_python_code(code, _Ctx(paths=paths, cwd=str(cwd or Path.cwd())) if paths else None)
    if base.blocked:
        return base
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return Decision(RiskLevel.CONFIRM_REQUIRED, "browser.exec", "run browser code XENIO could not read")
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    defined |= {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    decisions: list[Decision] = [base] if base.level > RiskLevel.READ else []
    hosts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _PY_SYSTEM_NAMES \
                and node.id not in defined and base.level == RiskLevel.READ:
            decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, "browser.exec_system",
                                      f"run browser code that also uses {node.id} (access to your computer)"))
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else ""
        args = node.args
        first = args[0] if args else None
        if name in _BROWSER_NAV_HELPERS:
            if _is_literal(first):
                decisions.append(classify_url(first.value, paths, cwd))
                hosts.append(url_host(first.value) or first.value[:60])
            else:
                decisions.append(read("browser.navigate", "open a web page"))
        elif name in _BROWSER_READ_HELPERS:
            continue
        elif name == "js":
            text = _string_parts(first)
            decisions.append(classify_js(text) if text.strip() else
                             Decision(RiskLevel.CONFIRM_REQUIRED, "browser.js", "run JavaScript XENIO cannot check"))
        elif name in ("fill_input", "type_text", "fill", "type_into"):
            decisions.append(classify_typing(_string_parts(first), _string_parts(args[1]) if len(args) > 1 else ""))
        elif name in ("click_at_xy", "click_xy", "mouse_click", "click_at"):
            decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, "browser.click_xy",
                                      "click on the page at a screen position (XENIO cannot see what is there)"))
        elif name in ("click", "click_element", "click_text"):
            decisions.append(_interaction(_string_parts(first), "click on the page"))
        elif name in ("press_key", "press", "key_press", "send_keys"):
            key = _string_parts(first).strip().lower()
            decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, "browser.submit", "press Enter on the page, which may submit a form")
                             if key in ("enter", "return", "\n") else Decision(RiskLevel.SAFE_WRITE, "browser.key", f"press {key or 'a key'}"))
        elif name in ("upload_file", "set_input_files", "upload"):
            files = [n.value for n in args[1:] if _is_literal(n)]
            checks = [paths.check(f, "read", cwd) for f in files] if paths else []
            upload = Decision(RiskLevel.CONFIRM_REQUIRED, "browser.upload", "upload a file to a website")
            decisions.append(combine([*checks, upload], upload))
        elif name == "cdp":
            params = {kw.arg: (kw.value.value if _is_literal(kw.value) else _string_parts(kw.value))
                      for kw in node.keywords if kw.arg}
            if len(args) > 1 and isinstance(args[1], ast.Dict):
                params.update({k.value: _string_parts(v) for k, v in zip(args[1].keys, args[1].values) if _is_literal(k)})
            decisions.append(classify_cdp(first.value if _is_literal(first) else "", params, paths, cwd)
                             if _is_literal(first) else
                             Decision(RiskLevel.CONFIRM_REQUIRED, "browser.cdp", "send a raw browser command XENIO cannot check"))
        elif name and name not in _PY_SAFE_BUILTINS and name not in defined:
            decisions.append(Decision(RiskLevel.CONFIRM_REQUIRED, "browser.exec", f"run browser code that calls {name}()"))
    where = ", ".join(dict.fromkeys(h for h in hosts if h))
    fallback = read("browser.read", f"open and read {where}" if where else "read a web page in the browser")
    decision = combine(decisions, fallback)
    if decision.level == RiskLevel.READ and where:
        decision = replace(decision, summary=f"open and read {where}")
    return decision


def classify_browser_tool(tool: str, a: dict, paths: PathPolicy, cwd, page: dict | None = None) -> Decision | None:
    """Hermes' step-by-step browser tools (used when browser.backend is not Browser Use).
    ``page`` (kept by the plugin from the latest snapshot): {"refs": {"@e5": 'button "Place order"'},
    "last_typed": 'searchbox "Search"'}."""
    page = page or {}
    refs = page.get("refs") or {}
    if tool == "browser_navigate":
        return classify_url(str(a.get("url") or ""), paths, cwd)
    if tool == "browser_click":
        label = refs.get(str(a.get("ref") or "").lstrip("@")) or refs.get(str(a.get("ref") or ""))
        return _interaction(label or "", "click an element on the page") if label else \
            Decision(RiskLevel.SAFE_WRITE, "browser.interact", "click an element on the page")
    if tool == "browser_type":
        label = refs.get(str(a.get("ref") or "").lstrip("@")) or refs.get(str(a.get("ref") or "")) or ""
        return classify_typing(label, str(a.get("text") or ""))
    if tool == "browser_press":
        key = str(a.get("key") or "").strip().lower()
        if key not in ("enter", "return", "numpadenter"):
            return Decision(RiskLevel.SAFE_WRITE, "browser.key", f"press {key or 'a key'} on the page")
        typed = str(page.get("last_typed") or "")
        if typed and _SEARCH_WORDS.search(typed):
            return Decision(RiskLevel.SAFE_WRITE, "browser.search", "press Enter to search")
        return _interaction(typed + " submit", "press Enter, which submits the form") if typed else \
            Decision(RiskLevel.CONFIRM_REQUIRED, "browser.submit", "press Enter on the page, which may submit a form")
    if tool == "browser_console":
        return classify_js(str(a["expression"])) if a.get("expression") else read("browser.console", "read the browser console")
    if tool == "browser_cdp":
        return classify_cdp(str(a.get("method") or ""), a.get("params"), paths, cwd)
    if tool == "browser_exec":
        return classify_browser_code(a.get("code"), paths, cwd)
    if tool == "browser_dialog":
        if str(a.get("action") or "").lower() == "accept":
            return Decision(RiskLevel.CONFIRM_REQUIRED, "browser.dialog", "press OK on a dialog box on the page (it may confirm an action)")
        return Decision(RiskLevel.SAFE_WRITE, "browser.dialog", "close a dialog box on the page")
    return None


# Downloads: never executed, never over a secret/system file, never over an existing file unless asked.
_DOWNLOAD_PROGRAM_SUFFIXES = _EXECUTABLE_SUFFIXES | {
    ".msix", ".msixbundle", ".appx", ".appxbundle", ".msp", ".jar", ".apk", ".dmg", ".pkg", ".sh", ".psm1", ".py",
    ".pyw", ".iso", ".img", ".vhd", ".vhdx", ".docm", ".xlsm", ".pptm", ".sys", ".drv", ".scf", ".url", ".inf",
    ".pif", ".chm", ".msc", ".application", ".gadget", ".settingcontent-ms", ".library-ms", ".appref-ms"}
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str, fallback: str = "download") -> str:
    """A plain file name: no folders, no ``..``, no characters Windows forbids, no device names."""
    text = unquote(name or "").replace("\\", "/").split("/")[-1]
    # Windows drops trailing dots/spaces; leading dots stay (".env" must keep its name to be recognised)
    text = re.sub(r'[<>:"|?*\x00-\x1f]', "_", text).strip(" ").rstrip(" .")
    if not text.strip("."):
        text = fallback
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = "_" + text
    return text[:180]


def download_target(url: str, destination: str | None, paths: PathPolicy, cwd=None) -> Path:
    """Where a download is saved: the Downloads folder (or ``destination``), named after the link."""
    from_url = safe_filename(urlsplit(normalize_url(url)).path.rsplit("/", 1)[-1])
    if not destination or not str(destination).strip():
        return paths.named_roots.get("Downloads", paths.home / "Downloads") / from_url
    dest = paths.resolve(str(destination), cwd)
    if dest.is_dir() or str(destination).rstrip().endswith(("/", "\\")):
        return dest / from_url
    return dest.parent / safe_filename(dest.name, from_url)


def classify_download(a: dict, paths: PathPolicy, cwd=None) -> Decision:
    raw = a.get("url")
    url = normalize_url(raw if isinstance(raw, str) else "")
    check_url = classify_url(url, paths, cwd, verb="download from")
    if check_url.blocked:
        return check_url
    if not url.lower().startswith(("http://", "https://")):
        return blocked("web.download", f"download {str(raw)[:80]}", "only http and https links can be downloaded")
    try:
        target = download_target(url, a.get("destination"), paths, cwd)
    except ValueError as exc:
        return blocked("web.download", "download a file", f"invalid destination: {exc}")
    exists = target.exists()
    check = paths.check(str(target), "modify" if exists else "create", cwd)
    if check.blocked:
        return check
    host, where = url_host(url), paths.describe(target)
    if target.suffix.lower() in _DOWNLOAD_PROGRAM_SUFFIXES:
        return Decision(max(check.level, RiskLevel.CONFIRM_REQUIRED), "web.download_program",
                        f"download {where} from {host} (a program or script; XENIO will not run it)")
    if exists and a.get("overwrite"):
        return Decision(max(check.level, RiskLevel.CONFIRM_REQUIRED), "web.download_replace",
                        f"download from {host} and replace {where}")
    if check.level > RiskLevel.SAFE_WRITE:
        return replace(check, action="web.download", summary=f"download from {host} to {where}")
    return Decision(RiskLevel.SAFE_WRITE, "web.download", f"download {where} from {host}")


_SECRET_STATEMENT = re.compile(
    r"\b(password|passwd|passcode|passphrase|api[ _-]?key|secret[ _-]?key|access[ _-]?token|auth[ _-]?token"
    r"|bearer[ _-]?token|refresh[ _-]?token|private[ _-]?key|pin[ _-]?code|otp)\b\s*(is|was|=|:)\s*\S{4,}", re.IGNORECASE)


def _strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


_SECRET_STATEMENT_EXTRA = re.compile(
    r"\b(session[ _-]?(cookie|id|token)|cookie|card (number|no\.?)|cvv|cvc|security code|ssh key|recovery (code|phrase)"
    r"|seed phrase|2fa (code|secret)|backup codes?)\b\s*(is|was|=|:)?\s*\S{4,}", re.IGNORECASE)
_CARD_DIGITS = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        d = int(ch) * (2 if double else 1)
        total += d - 9 if d > 9 else d
        double = not double
    return total % 10 == 0


def contains_personal_secret(text: str) -> bool:
    """Secrets people say out loud: "my password is ...", card numbers, cookies, recovery codes, keys."""
    if not text:
        return False
    if contains_secret(text, strict=True) or _SECRET_STATEMENT.search(text) or _SECRET_STATEMENT_EXTRA.search(text):
        return True
    for m in _CARD_DIGITS.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def classify_memory_write(tool: str, a: dict) -> Decision:
    """Hermes memory / skills: useful context may be saved, secrets never."""
    strict = tool == "memory"
    for text in _strings(a):
        if contains_secret(text, strict=strict) or (strict and contains_personal_secret(text)):
            return blocked(f"{tool}.secret", "save an API key, token or password", "secrets are never stored in memory or skills")
    return Decision(RiskLevel.SAFE_WRITE, f"tool.{tool}", "update XENIO's memory" if strict else "update a Hermes skill")


# ---- memory value (V4): keep concise lasting facts, drop chatter --------------------------------------

_EXPLICIT_MEMORY = re.compile(
    r"\b(remember|memori[sz]e|don'?t forget|do not forget|keep in mind|make a note|note (that|this|down)|save (that|this)"
    r"|from now on|going forward|always|never|i prefer|i'd prefer|i like|i don'?t like|call me|my name is|i am called"
    r"|i'?m working on|my (main |current )?project)\b", re.IGNORECASE)
_TRANSIENT = re.compile(
    r"\b(right now|at the moment|just now|for now|temporar(y|ily)|this (session|conversation|chat|turn)|today|tonight"
    r"|in a (minute|moment|bit)|currently (testing|trying|running|debugging|checking)|(last|previous) (message|turn|command))\b",
    re.IGNORECASE)
_SPECULATION = re.compile(r"\b(appears to|seems to|probably|might be|may be|likely|possibly|i think|apparently)\b", re.IGNORECASE)
_EVENT_LOG = re.compile(
    r"\b(user (asked|said|tested|tried|requested|wanted to|then|initially|was testing)|initially tried|then explored"
    r"|in (this|the) (session|conversation)|during (this|the) (session|conversation))\b", re.IGNORECASE)
_TRANSCRIPT = re.compile(r"(?m)^\s*(user|assistant|xenio|hermes|human|ai)\s*:", re.IGNORECASE)
MEMORY_ENTRY_MAX = 400


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= 0.75 or (len(wa) >= 3 and wa <= wb)


def explicit_memory_request(user_message: str) -> bool:
    """The user's own words ask XENIO to remember something (or state a lasting preference)."""
    return bool(_EXPLICIT_MEMORY.search(user_message or ""))


def assess_memory_write(a: dict, existing: Iterable[str] = (), explicit: bool = False) -> tuple[str, str]:
    """Value of a memory tool call: ("HIGH_VALUE"|"LOW_VALUE"|"DUPLICATE"|"SECRET", reason).

    HIGH_VALUE: explicit preferences/instructions, project facts, workflows, decisions - and anything the user
    explicitly asked to remember. LOW_VALUE (not saved unless the user asked): temporary states, one-off events,
    guesses about the user, transcripts, long dumps. Removing entries is always allowed. If unsure: don't save."""
    ops = a.get("operations") if isinstance(a.get("operations"), list) else [a]
    existing = [e for e in existing if e and e.strip()]
    for op in ops:
        if not isinstance(op, dict):
            continue
        action = str(op.get("action") or a.get("action") or "add").lower()
        content = str(op.get("content") or op.get("new_text") or "")
        if action == "remove" or not content.strip():
            continue
        if contains_personal_secret(content):
            return "SECRET", "it contains a password, key, token, card number or similar secret"
        if _TRANSCRIPT.search(content):
            return "LOW_VALUE", "it is a conversation transcript, not a fact"
        if len(content) > MEMORY_ENTRY_MAX:
            return "LOW_VALUE", f"it is {len(content)} characters long; memory keeps short facts (under {MEMORY_ENTRY_MAX})"
        if action == "add" and any(_similar(content, e) for e in existing):
            return "DUPLICATE", "an entry saying the same thing is already in memory"
        if not explicit:
            if _TRANSIENT.search(content):
                return "LOW_VALUE", "it describes something temporary"
            if _EVENT_LOG.search(content):
                return "LOW_VALUE", "it records what happened in a conversation, not a lasting fact"
            if _SPECULATION.search(content):
                return "LOW_VALUE", "it is a guess about the user, not something they said"
    return "HIGH_VALUE", "useful lasting information"


# ---- personal tools (V4) -------------------------------------------------------------------------------

PERSONAL_TOOLS = {"xenio_calendar_events", "xenio_calendar_create_event", "xenio_email_search", "xenio_email_read",
                  "xenio_github", "xenio_tasks", "xenio_project", "xenio_daily_briefing", "xenio_schedule",
                  "xenio_notifications", "xenio_integrations"}
_PERSONAL_READ = {"xenio_calendar_events", "xenio_email_search", "xenio_email_read", "xenio_github",
                  "xenio_daily_briefing", "xenio_integrations"}


def classify_personal_tool(tool: str, a: dict) -> Decision:
    action = str(a.get("action") or "").lower()
    if tool in _PERSONAL_READ:
        what = {"xenio_calendar_events": "read your calendar", "xenio_email_search": "search your email",
                "xenio_email_read": "read an email", "xenio_github": "read GitHub",
                "xenio_daily_briefing": "prepare your daily briefing", "xenio_integrations": "check connected services"}
        return read(f"personal.{tool[6:]}", what[tool])
    # Everything below writes something: secrets are never stored in tasks, projects, events or schedules.
    if any(contains_personal_secret(t) for t in _strings(a)):
        return blocked(f"personal.{tool[6:]}.secret", "store a password, key or token", "secrets are never stored by XENIO")
    if tool == "xenio_notifications":
        return read("personal.notifications", "read notifications") if action != "clear" else \
            Decision(RiskLevel.SAFE_WRITE, "personal.notifications", "mark notifications as read")
    if tool == "xenio_tasks":
        if action in ("", "list"):
            return read("personal.tasks", "read your task list")
        if action == "delete":
            return Decision(RiskLevel.CONFIRM_REQUIRED, "personal.tasks_delete", f"delete task #{a.get('id')} from your task list")
        if action in ("add", "update", "complete"):
            return Decision(RiskLevel.SAFE_WRITE, f"personal.tasks_{action}", f"{action} a task in your task list")
    if tool == "xenio_project":
        if action in ("", "list", "context"):
            return read("personal.project", "read your project notes")
        if action in ("set_current", "update", "note"):
            return Decision(RiskLevel.SAFE_WRITE, f"personal.project_{action}", f"update your project notes ({action})")
    if tool == "xenio_calendar_create_event":
        title = " ".join(str(a.get("title") or "an event").split())[:80]
        return Decision(RiskLevel.CONFIRM_REQUIRED, "personal.calendar_create",
                        f"add '{title}' to your Google Calendar from {a.get('start')} to {a.get('end')}")
    if tool == "xenio_schedule":
        if action in ("", "list"):
            return read("personal.schedule", "list your scheduled checks")
        if action == "create":
            task = " ".join(str(a.get("task") or "").split())[:160]
            when = " ".join(str(a.get("schedule") or "").split())[:60]
            once = when.lower().startswith("in ") or bool(re.match(r"^\d{4}-\d{2}-\d{2}", when))
            return Decision(RiskLevel.CONFIRM_REQUIRED, "personal.schedule_create",
                            f"schedule a {'one-time' if once else 'RECURRING'} read-only check '{a.get('name')}' "
                            f"({when}): {task}")
        if action == "remove":
            return Decision(RiskLevel.CONFIRM_REQUIRED, "personal.schedule_remove", f"remove the scheduled check '{a.get('name')}'")
    return Decision(RiskLevel.CONFIRM_REQUIRED, f"personal.{tool[6:]}", f"use {tool} ({action or 'unknown action'})")


# ---- prompt injection (V4): external content is data -------------------------------------------------

UNTRUSTED_SOURCE_TOOLS = {"web_extract", "web_search", "browser_exec", "browser_snapshot", "browser_navigate",
                          "browser_vision", "read_file", "xenio_email_search", "xenio_email_read", "xenio_calendar_events",
                          "xenio_github", "xenio_daily_briefing", "session_search", "terminal",
                          "xenio_look_at_screen", "vision_analyze", "video_analyze",  # V5: text seen in images
                          "xenio_find_ui"}  # V5.5: names of on-screen controls (chat text, page text)
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|system|safety)?\s*"
    r"(instructions|rules|prompts?|guidelines|policies)"
    r"|you are now\b|new instructions\s*:|system prompt|developer mode|jailbreak"
    r"|\b(assistant|ai|agent|xenio|hermes|claude|gpt)\s*[:,]?\s*(please\s+)?(send|forward|email|upload|post|delete|run|execute"
    r"|download|transfer|reveal|share)\b"
    r"|\b(send|forward|email|upload|post|exfiltrate|transfer)\b[^.\n]{0,60}\bto\s+[\w.+-]+@[\w-]+\.[\w.]+"
    r"|\b(do not|don'?t)\s+tell\s+the\s+user"
    r"|<\s*/?\s*(system|instructions?)\s*>",
    re.IGNORECASE)
# Not flagged on purpose: plain commands such as `curl ... | bash` or "run the following command". Documentation
# is full of them (live: Hermes' own install line in a search result), and running any command still needs the
# user's yes under the normal policy. Detection looks for text that addresses the assistant or overrides rules.


def find_injection(text: str) -> str | None:
    """The first instruction-like phrase in untrusted content (emails, pages, issues, documents), or None."""
    if not text:
        return None
    m = _INJECTION.search(text)
    return " ".join(m.group(0).split())[:120] if m else None


_SAFE_AFTER_INJECTION = {"xenio_list_directory", "xenio_inspect_path", "read_file", "search_files", "web_search",
                         "skills_list", "skill_view", "session_search", "xenio_calendar_events", "xenio_email_search",
                         "xenio_email_read", "xenio_github", "xenio_daily_briefing", "xenio_integrations", "clarify",
                         "todo_list", "vision_analyze", "xenio_notifications", "xenio_look_at_screen",
                         "xenio_list_apps", "xenio_find_ui"}


def escalate_after_injection(decision: Decision, tool: str, phrase: str) -> Decision:
    """After this turn read external content that tried to give instructions, nothing but plain reading runs
    without the user's yes: a write, a command, a page visit or a memory update could be the injected order."""
    if decision.blocked or decision.needs_confirmation or tool in _SAFE_AFTER_INJECTION:
        return decision
    return Decision(RiskLevel.CONFIRM_REQUIRED, decision.action,
                    f"{decision.summary} (asked right after reading outside content that tried to give instructions: "
                    f"\"{phrase[:60]}\")", scopes=decision.scopes)


# ---- vision (V5): looking at the screen is explicit, observe-only and never unattended ---------------------

SCREEN_TARGETS = ("screen", "window")
# The user's own words point at something on their screen ("look at my screen", "what's wrong here?", "read this
# error", "what app is open"). Deictic words count: "this error" with no error in the text means the one in view.
_SCREEN_REQUEST = re.compile(
    r"\b(?:screen|screenshot|display|monitor|desktop|window|look(?:ing)?\s+at|take\s+a\s+look|have\s+a\s+look"
    r"|(?:can|do)\s+you\s+see|see\s+(?:this|that|it|what|my|the)|in\s+front\s+of\s+me|showing|shown|visible"
    r"|(?:this|that|these)\s+(?:error|errors|page|window|graph|chart|picture|image|photo|message|pop-?up|dialog|code|tab"
    r"|site|website|app|document|email|table|diagram|button|warning|text|screen|output)"
    r"|read\s+(?:this|that|it|out|what|the\s+text)|what(?:'s|\s+is)\s+(?:open|wrong\s+here|this|that)"
    r"|(?:which|what)\s+(?:app|application|program|window|tab|site|website|page)s?\b"
    r"|here\b)", re.IGNORECASE)
_XENIO_NOTES = re.compile(r"\[XENIO[^\]]*\]", re.DOTALL)
RECENT_LOOK_SECONDS = 300  # a follow-up within this window may look again without asking
STALE_LOOK_SECONDS = 30  # after this, a new question about the screen gets "it may have changed: look again"


def asked_about_screen(user_message: str) -> bool:
    """True when the user's own words (not XENIO's appended notes) refer to their screen."""
    return bool(_SCREEN_REQUEST.search(_XENIO_NOTES.sub(" ", user_message or "")))


def classify_screen_look(a: dict) -> Decision:
    target = str(a.get("target") or "screen").strip().lower()
    if target not in SCREEN_TARGETS:
        return blocked("vision.look_at_screen", "look at the screen", f"target must be one of {', '.join(SCREEN_TARGETS)}")
    if not isinstance(a.get("question"), str) or not a["question"].strip():
        return blocked("vision.look_at_screen", "look at the screen", "the 'question' argument is missing")
    return read("vision.look_at_screen", "look at your screen" if target == "screen" else "look at your active window")


def screen_capture_consent(decision: Decision, user_message: str, session_id: str = "",
                           recently_looked: bool = False) -> Decision:
    """Explicit capture only. Scheduled/unattended runs never look; a look the user's words did not ask for (and that
    does not follow a look moments ago) needs their yes - e.g. a model that decides on its own, or a page that asks."""
    if decision.blocked:
        return decision
    if str(session_id or "").startswith("cron_"):
        return blocked("vision.unattended", decision.summary,
                       "XENIO never looks at the screen in a scheduled or unattended run")
    if asked_about_screen(user_message) or recently_looked:
        return decision
    return Decision(RiskLevel.CONFIRM_REQUIRED, decision.action,
                    f"{decision.summary} (a screenshot goes to the vision model and is then discarded)")


def classify_image_source(tool: str, source: object, paths: PathPolicy, cwd) -> Decision:
    """Hermes' vision_analyze / video_analyze load an image (or video) from a link, a local file or inline data.
    Local files go through the same folder rules as reading a file (secret files stay blocked); links through the
    link rules (no logins or keys in the URL)."""
    what = "an image" if tool == "vision_analyze" else "a video"
    if not isinstance(source, str) or not source.strip():
        return blocked(f"vision.{tool}", f"look at {what}", "no image or video was named")
    src = source.strip()
    if src.lower().startswith("data:"):
        return read(f"vision.{tool}", f"look at {what} attached to the conversation")
    if src.lower().startswith(("http://", "https://")):
        decision = classify_url(src, paths, cwd, verb="look at")
        return decision if decision.blocked else read(f"vision.{tool}", f"look at {what} from {url_host(src)}")
    if src.lower().startswith("file:") or re.match(r"^[a-zA-Z]:[\\/]", src) or not re.match(r"^[a-zA-Z][\w+.-]*:", src):
        local = unquote(urlsplit(src).path).lstrip("/") if src.lower().startswith("file:") else src
        if src.lower().startswith("file:") and not re.match(r"^[a-zA-Z]:", local):
            local = "/" + local
        check = paths.check(local, "read", cwd)
        return check if check.blocked else read(f"vision.{tool}", f"look at {what} ({check.summary.removeprefix('read ')})")
    return blocked(f"vision.{tool}", f"look at {what} from {src[:60]}", "only links, local files and inline images can be viewed")


# ---- desktop control (V5.5): XENIO's hands, judged by what an action really touches -------------------------------

DESKTOP_READ_TOOLS = {"xenio_list_apps", "xenio_find_ui"}
DESKTOP_TOOLS = DESKTOP_READ_TOOLS | {"xenio_open_app", "xenio_window", "xenio_gui"}
MESSAGING_APPS = {"discord", "whatsapp", "whatsapp.root", "telegram", "slack", "ms-teams", "teams", "signal", "skype",
                  "zoom", "outlook", "olk", "thunderbird", "messenger", "instagram", "mailspring", "element", "wechat",
                  # macOS app names
                  "messages", "mail", "microsoft outlook", "microsoft teams", "msteams", "zoom.us", "facetime",
                  "spark", "spark desktop", "airmail 5", "mimestream", "beeper", "telegram desktop"}
_BROWSER_APPS = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "chromium", "arc",
                 "google chrome", "safari", "microsoft edge", "brave browser", "orion", "zen", "dia"}
_MESSAGING_TITLE = re.compile(r"whatsapp|discord|slack|messenger|gmail|outlook|telegram|instagram|linkedin|teams|"
                              r"\bmail\b|inbox|signal|x\.com|twitter", re.IGNORECASE)
TERMINAL_APPS = {"windowsterminal", "powershell", "pwsh", "cmd", "conhost", "wt", "bash", "mintty", "openconsole",
                 "wsl", "ubuntu", "git-bash",
                 "terminal", "iterm2", "iterm", "warp", "ghostty", "alacritty", "kitty", "wezterm", "wezterm-gui",
                 "hyper", "tabby", "rio"}
_CREDENTIAL_APPS = {"1password", "bitwarden", "keepass", "keepassxc", "lastpass", "dashlane", "enpass", "keeper",
                    "nordpass", "roboform", "protonpass", "authy", "credentialuibroker",
                    "passwords", "keychain access", "1password 7", "1password 8", "strongbox", "proton pass",
                    "keeper password manager", "securityagent", "coreautha"}
_CREDENTIAL_TITLE = re.compile(r"password manager|credential manager|manage passwords|saved passwords|passwords? - |"
                               r"windows security.*sign in|vault", re.IGNORECASE)
_SECURITY_APPS = {"sechealthui", "securityhealthsystray", "regedit", "gpedit", "secpol", "mmc", "useraccountcontrolsettings",
                  "wf", "systempropertiesprotection", "msconfig"}
_SECURITY_TITLE = re.compile(r"windows security|virus & threat|firewall|user account control|registry editor|"
                             r"group policy|bitlocker|defender|smartscreen|"
                             r"privacy & security|filevault|login items|users & groups|touch id & password|"
                             r"device management|full disk access", re.IGNORECASE)
_DESKTOP_SEND = re.compile(r"\b(send|post|publish|tweet|reply|comment|share|upload|submit|invite|forward|broadcast"
                           r"|start (?:a )?(?:video )?call|call|go live|start streaming)\b", re.IGNORECASE)
_DESKTOP_DESTRUCTIVE = re.compile(r"\b(delete|remove|erase|discard|clear all|empty recycle bin|empty trash|empty bin"
                                  r"|permanently|move to trash|move to bin"
                                  r"|sign out|log out|logout|leave (?:group|server|channel)|block)\b", re.IGNORECASE)
_DESKTOP_SYSTEM = re.compile(r"\b(install|uninstall|format|wipe|reset (?:this )?pc|factory reset|update now|restart now"
                             r"|shut ?down|run as administrator)\b", re.IGNORECASE)
_FIELD_SEARCH = re.compile(r"search|find|quick switcher|switcher|filter|jump to|go to|address|url|omnibox|suche|खोज",
                           re.IGNORECASE)
_SEND_KEYS = {"enter", "ctrl+enter", "alt+enter", "ctrl+shift+enter", "cmd+enter", "cmd+shift+d"}  # Mail: cmd+shift+d
_ACTIVATED_BY_ENTER = {"listitem", "menuitem", "treeitem", "dataitem", "tabitem", "link", "button", "radiobutton",
                       "checkbox", "splitbutton"}


def _preview(text: str, limit: int = 60) -> str:
    """Typed text inside prompts, between guillemets (the plugin keeps it out of logs)."""
    flat = " ".join(str(text or "").split())
    return f"«{flat[:limit]}{'…' if len(flat) > limit else ''}»"


def _app_kind(info: dict) -> set[str]:
    app, title = str(info.get("app") or "").lower(), str(info.get("title") or "")
    kinds = set()
    if app in MESSAGING_APPS or (app in _BROWSER_APPS and _MESSAGING_TITLE.search(title)):
        kinds.add("messaging")
    if app in TERMINAL_APPS or (app == "explorer" and title.strip().lower() == "run"):
        kinds.add("terminal")
    if app in _CREDENTIAL_APPS or _CREDENTIAL_TITLE.search(title):
        kinds.add("credentials")
    if app in _SECURITY_APPS or _SECURITY_TITLE.search(title):
        kinds.add("security")
    if app in ("explorer", "finder"):
        kinds.add("files")
    return kinds


def classify_app_launch(name: str, entry: dict | None) -> Decision:
    """``entry``: what the app catalog resolved (``name``, ``risk``, ``path``), or None if nothing matched."""
    if not isinstance(name, str) or not name.strip():
        return blocked("desktop.open_app", "open an app", "the app's name is missing")
    if not entry:
        return read("desktop.open_app", f"look for an app called {name.strip()[:60]!r}")
    shown = str(entry.get("name") or name)[:80]
    if entry.get("risk") == "installer":
        return Decision(RiskLevel.HIGH_RISK, "desktop.installer", f"run {shown!r}, which installs, updates or uninstalls software")
    if entry.get("risk") == "untrusted_location":
        return Decision(RiskLevel.HIGH_RISK, "desktop.untrusted_program",
                        f"start {shown!r} from {entry.get('path', 'a download folder')} - downloaded programs never start "
                        "without your confirmation")
    return Decision(RiskLevel.SAFE_WRITE, "desktop.open_app", f"open {shown}")


def classify_window_action(action: str, name: str) -> Decision:
    act = str(action or "").lower()
    if act == "list":
        return read("desktop.windows", "list the open windows")
    if act not in ("focus", "minimize", "maximize", "restore", "close"):
        return blocked("desktop.window", f"{act} a window", "unknown window action")
    if not str(name or "").strip():
        return blocked("desktop.window", f"{act} a window", "name the window or app")
    what = {"close": "close {} (gracefully: it may ask to save; nothing is forced)"}.get(act, act + " {}")
    return Decision(RiskLevel.SAFE_WRITE, f"desktop.window_{act}", what.format(str(name)[:60]))


def classify_gui_action(args: dict, info: dict | None = None) -> Decision:
    """A mouse/keyboard action, judged by its consequence. ``info`` (from the plugin's probe): the app in front
    (``app``, ``title``), the element a click would hit (``element`` with UI Automation name, type, parents,
    is_password), the focused field (``focused``) and the text typed into that app earlier (``typed``)."""
    a = args if isinstance(args, dict) else {}
    info = info or {}
    action = str(a.get("action") or "").lower()
    kinds = _app_kind(info)
    app = str(info.get("app") or "the active app")
    where = f" in {app}"
    el = info.get("element") or {}
    focused = info.get("focused") or {}
    label = " ".join(str(x) for x in (el.get("name"), *(el.get("parents") or [])[:2], a.get("target")) if x)
    if info.get("locked") or str(info.get("process") or "").lower() in ("lockapp.exe", "loginwindow") \
            or str(focused.get("name") or "").lower() == "lock screen":
        # measured: with the PC locked, the focused element was the lock screen - typing there is entering a password
        return blocked("desktop.locked", f"{action} while the PC is locked",
                       "the PC is locked: XENIO never types or clicks on the lock screen. Ask the user to unlock it")
    if "credentials" in kinds and action != "move":
        return blocked("desktop.credentials", f"{action} in {app}",
                       "XENIO never operates password managers or credential stores")
    if action in ("move", "scroll"):
        return Decision(RiskLevel.SAFE_WRITE, f"desktop.{action}", f"{'move the mouse' if action == 'move' else 'scroll'}{where}")
    if action in ("click", "double_click", "right_click"):
        if not label.strip():
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.click", f"click an unnamed spot{where}")
        decision = classify_visual_action("click", label)
        if decision.blocked or decision.level >= RiskLevel.HIGH_RISK:
            return replace(decision, summary=f"{decision.summary}{where}")
        if _DESKTOP_SYSTEM.search(label):
            return Decision(RiskLevel.HIGH_RISK, "desktop.system", f"click {label[:60]!r}{where} (installs, updates or resets something)")
        if "messaging" in kinds and _DESKTOP_SEND.search(label):
            typed = f" {_preview(info['typed'])}" if info.get("typed") else ""
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.send", f"send the message{typed}{where} (click {el.get('name') or 'Send'})")
        if _DESKTOP_SEND.search(label) or _DESKTOP_DESTRUCTIVE.search(label):
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.consequential", f"click {label[:60]!r}{where}")
        if "security" in kinds:
            return Decision(RiskLevel.HIGH_RISK, "desktop.security_settings", f"click {label[:60]!r} in security settings")
        return Decision(RiskLevel.SAFE_WRITE, "desktop.click", f"click {label[:60]!r}{where}")
    if action == "type":
        text = a.get("text")
        if not isinstance(text, str) or not text:
            return blocked("desktop.type", "type", "nothing to type")
        if contains_secret(text, strict=True) or contains_personal_secret(text):
            return blocked("desktop.type_secret", f"type into {app}",
                           "XENIO never types API keys, passwords, tokens or other secrets, even when asked")
        if focused.get("is_password") or _SENSITIVE_FIELD.search(str(focused.get("name") or "")):
            return blocked("desktop.type_password", f"type into a password field{where}",
                           "XENIO does not enter passwords or codes into login fields")
        field = str(focused.get("name") or focused.get("type") or "the focused field")[:50]
        if "terminal" in kinds:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.type_terminal",
                            f"type {_preview(text)} into a terminal{where} (that runs commands; the terminal tool is safer)")
        if "security" in kinds:
            return Decision(RiskLevel.HIGH_RISK, "desktop.security_settings", f"type into security settings{where}")
        if "messaging" in kinds and "\n" in text:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.send", f"type {_preview(text)}{where} - the line break can send it")
        return Decision(RiskLevel.SAFE_WRITE, "desktop.type", f"type {_preview(text)} into {field}{where}")
    if action == "key":
        try:
            from app.desktop.input import normalize_combo  # stdlib-only, imported lazily (policy stays importable)

            combo = normalize_combo(str(a.get("keys") or ""))
        except Exception as exc:  # noqa: BLE001 - malformed keys never run
            return blocked("desktop.key", "press a key", f"invalid key: {exc}")
        if combo in ("ctrl+alt+delete", "ctrl+alt+del", "ctrl+alt+backspace"):  # (Mac "delete" is backspace)
            return blocked("desktop.key", "press Ctrl+Alt+Delete", "secure system keys are not sent")
        if combo == "alt+cmd+esc":  # macOS Force Quit: ends apps without letting them save
            return blocked("desktop.key", "open Force Quit", "XENIO never force-quits apps (unsaved work would be lost)")
        if combo == "alt+shift+cmd+q":  # macOS: log out at once, without asking any app to save
            return blocked("desktop.key", "log out immediately", "this logs out without letting apps save")
        if combo == "shift+cmd+q":
            return Decision(RiskLevel.HIGH_RISK, "desktop.logout", "log out of the Mac (every app is closed)")
        if combo == "enter" and str(focused.get("type") or "").lower() in _ACTIVATED_BY_ENTER:
            # Enter on a highlighted entry activates it, exactly like clicking it (measured: in Discord's quick switcher
            # the focus is the highlighted result "Rahul", not a message box) - so it is judged as that click
            name = str(focused.get("name") or "")
            decision = classify_gui_action({"action": "click"}, {**info, "focused": {},
                                                                 "element": {"name": name, "type": focused.get("type"), "parents": []}})
            return decision if decision.blocked else replace(decision, summary=f"press enter on {name[:60]!r}{where}")
        if combo in _SEND_KEYS and "messaging" in kinds and not _FIELD_SEARCH.search(str(focused.get("name") or "")):
            typed = f" {_preview(info['typed'])}" if info.get("typed") else ""
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.send", f"send the message{typed}{where} (press {combo})")
        if combo in _SEND_KEYS and "terminal" in kinds:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.terminal_enter", f"press {combo} in a terminal{where} (runs the typed command)")
        if combo == "win+r":
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.run_dialog", "open the Run dialog (it can start any program)")
        if combo == "cmd+space":
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.run_dialog", "open Spotlight (it can start any program)")
        if combo in ("delete", "shift+delete") and "files" in kinds:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.delete_files", f"press {combo} in File Explorer (deletes the selected files)")
        if combo in ("cmd+backspace", "alt+cmd+backspace") and "files" in kinds:
            return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.delete_files", f"press {combo} in Finder (deletes the selected files)")
        if combo in ("shift+cmd+backspace", "alt+shift+cmd+backspace") and "files" in kinds:
            return Decision(RiskLevel.HIGH_RISK, "desktop.empty_trash", "empty the Trash in Finder (files are gone for good)")
        if "security" in kinds:
            return Decision(RiskLevel.HIGH_RISK, "desktop.security_settings", f"press {combo} in security settings")
        return Decision(RiskLevel.SAFE_WRITE, "desktop.key", f"press {combo}{where}")
    if action == "drag":
        return Decision(RiskLevel.CONFIRM_REQUIRED, "desktop.drag", f"drag{where} (it can move files or data)")
    return blocked("desktop.action", f"{action or 'an unknown action'} on the screen", "unknown desktop action")


def classify_desktop_tool(tool: str, args: dict, info: dict | None = None) -> Decision:
    a = args if isinstance(args, dict) else {}
    if tool in DESKTOP_READ_TOOLS:
        return read(f"desktop.{tool[6:]}", "look up apps" if tool == "xenio_list_apps" else "read the controls of a window")
    if tool == "xenio_open_app":
        return classify_app_launch(str(a.get("name") or ""), (info or {}).get("app_entry"))
    if tool == "xenio_window":
        return classify_window_action(str(a.get("action") or ""), str(a.get("name") or ""))
    return classify_gui_action(a, info)


def unattended_desktop(decision: Decision, tool: str, session_id: str = "") -> Decision:
    """Scheduled/unattended runs never use XENIO's hands (no continuous unattended GUI automation)."""
    if tool in DESKTOP_TOOLS and str(session_id or "").startswith("cron_"):
        return blocked("desktop.unattended", decision.summary, "XENIO never controls the desktop in a scheduled or unattended run")
    return decision


# Foundation for LATER screen interaction (V5 is observe-only: no XENIO tool clicks or types on the screen).
# Hermes' own computer_use stays CONFIRM_REQUIRED and is not in XENIO's tool set at all.
_SECURITY_OFF = re.compile(
    r"\b(?:turn\s+off|disable|stop|pause)\b.{0,40}\b(?:defender|firewall|antivirus|anti-virus|real-?time\s+protection"
    r"|smartscreen|uac|user\s+account\s+control|tamper\s+protection|bitlocker|windows\s+security)", re.IGNORECASE)
_REVEAL_SECRET = re.compile(r"\b(?:show|reveal|copy|export|view)\b.{0,30}\b(?:password|passwords|passkey|token|api\s*key"
                            r"|secret|recovery\s+code|private\s+key|cookie)", re.IGNORECASE)


def classify_visual_action(action: str, target: str = "", text: str = "") -> Decision:
    """How risky a screen action would be, judged by its consequence: reading is READ; scrolling and harmless UI is
    SAFE_WRITE; submitting, sending, uploading or changing data needs a yes; payments, credential and security
    changes are HIGH_RISK; revealing passwords/tokens or switching off security protections is BLOCKED."""
    act = (action or "").strip().lower()
    label = " ".join((target or "").split())[:100]
    where = f" ({label})" if label else ""
    if act in ("inspect", "read", "describe", "look", "screenshot"):
        return read("screen.read", f"read what is on the screen{where}")
    if _SECURITY_OFF.search(f"{label} {text}"):
        return blocked("screen.security_off", f"{act} on {label or 'the screen'}", "switching off security protections is never allowed")
    if _REVEAL_SECRET.search(f"{label} {text}"):
        return blocked("screen.reveal_secret", f"{act} on {label or 'the screen'}", "XENIO never extracts passwords, tokens or keys")
    if act in ("scroll", "hover", "focus", "navigate", "switch_window", "zoom"):
        return Decision(RiskLevel.SAFE_WRITE, "screen.navigate", f"{act} on the screen{where}")
    if act in ("click", "double_click", "right_click", "press"):
        if not label:  # a bare coordinate says nothing about the consequence
            return Decision(RiskLevel.CONFIRM_REQUIRED, "screen.click", "click a spot on the screen")
        decision = _interaction(label, "click on the screen")
        return replace(decision, action=decision.action.replace("browser.", "screen."))
    if act == "type":
        decision = classify_typing(label, text)
        return replace(decision, action=decision.action.replace("browser.", "screen."))
    if act == "drag":
        return Decision(RiskLevel.CONFIRM_REQUIRED, "screen.drag", f"drag on the screen{where} (it can move files or data)")
    return Decision(RiskLevel.CONFIRM_REQUIRED, f"screen.{act or 'unknown'}", f"{act or 'an unknown action'} on the screen{where}")


# =============================================================================
# Tool calls (what the xenio-guard plugin checks before Hermes runs a tool)
# =============================================================================

READ_TOOLS = {"web_search", "web_extract", "vision_analyze", "session_search", "skills_list", "skill_view",
              "todo_list", "clarify", "browser_navigate", "browser_snapshot", "browser_scroll", "browser_back",
              "browser_get_images", "browser_vision", "text_to_speech", "x_search", "video_analyze",
              "xenio_list_directory", "xenio_inspect_path"}
SAFE_WRITE_TOOLS = {"memory", "skill_manage", "browser_click", "browser_type", "browser_press", "browser_dialog",
                    "delegate_task", "image_generate", "video_generate"}
CONFIRM_TOOLS = {"browser_cdp", "browser_exec", "manage_connections", "cronjob_manage", "computer_use",
                 "ha_call_service", "send_message"}
HIGH_TOOLS = {"browser_vault_list", "browser_vault_unlock", "browser_vault_fill", "browser_vault_save_login",
              "browser_vault_enter_code"}
EDIT_TOOLS = {"write_file", "patch"}  # Hermes asks XENIO itself (ACP edit approval) — only blocks are applied here


def classify_tool_call(tool: str, args: dict | None, paths: PathPolicy, cwd: str | os.PathLike | None = None,
                       page: dict | None = None) -> Decision:
    """``page``: what the plugin knows about the open browser page (see ``classify_browser_tool``)."""
    a = args if isinstance(args, dict) else {}
    cwd = a.get("workdir") or cwd
    if tool.startswith("browser_") and tool not in HIGH_TOOLS:
        decision = classify_browser_tool(tool, a, paths, cwd, page)
        if decision is not None:
            return decision
    if tool in ("memory", "skill_manage"):
        return classify_memory_write(tool, a)
    if tool == "terminal":
        command = a.get("command")
        if not isinstance(command, str):
            return blocked("terminal.invalid", "run a malformed command", "the command argument is missing or not text")
        return classify_command(command, paths, cwd)
    if tool == "execute_code":
        code = a.get("code")
        if not isinstance(code, str) or not code.strip():
            return blocked("code.invalid", "run malformed code", "the code argument is missing")
        return classify_python_code(code, _Ctx(paths=paths, cwd=str(cwd or paths.workspace or Path.cwd())))
    if tool == "read_file":
        return _path_arg(a, "path", "read", paths, cwd)
    if tool == "search_files":
        target = a.get("path") or "."
        return paths.check(str(target), "list", cwd)
    if tool == "write_file":
        return _path_arg(a, "path", "write", paths, cwd)
    if tool == "patch":
        if a.get("mode") == "patch":
            found = re.findall(r"\*\*\* (?:Update|Add|Delete) File: (.+)", str(a.get("patch") or ""))
            if not found:
                return blocked("filesystem.patch", "apply a malformed patch", "no file paths in the patch")
            return combine([paths.check(p.strip(), "modify", cwd) for p in found], read("filesystem.patch", "patch files"))
        return _path_arg(a, "path", "modify", paths, cwd)
    if tool == "process_manage":
        action = str(a.get("action") or "").lower()
        if action in ("list", "poll", "log", "wait", ""):
            return read("process.inspect", f"check background processes ({action or 'list'})")
        if action in ("close", "handoff"):
            return Decision(RiskLevel.SAFE_WRITE, f"process.{action}", f"{action} a background process")
        return Decision(RiskLevel.CONFIRM_REQUIRED, f"process.{action}", f"{action} a running background process")
    if tool.startswith("xenio_"):
        return _classify_xenio_tool(tool, a, paths, cwd)
    if tool == "cronjob_manage":
        if str(a.get("action") or "list").lower() in ("list", "status", "get"):
            return read("tool.cronjob", "list scheduled jobs")
        # Hermes' own jobs get every tool; XENIO's (xenio_schedule) are read-only and run under this policy.
        return blocked("tool.cronjob", f"{a.get('action')} a Hermes cron job",
                       "in XENIO, scheduled checks are managed with xenio_schedule (read-only and safety-checked)")
    if tool in ("vision_analyze", "video_analyze"):
        return classify_image_source(tool, a.get("image_url" if tool == "vision_analyze" else "video_url"), paths, cwd)
    if tool in READ_TOOLS:
        return read(f"tool.{tool}", f"use {tool}")
    if tool in SAFE_WRITE_TOOLS:
        return Decision(RiskLevel.SAFE_WRITE, f"tool.{tool}", f"use {tool}")
    if tool in HIGH_TOOLS:
        return Decision(RiskLevel.HIGH_RISK, f"tool.{tool}", "use saved website passwords or login codes")
    if tool in CONFIRM_TOOLS:
        return Decision(RiskLevel.CONFIRM_REQUIRED, f"tool.{tool}", f"use the {tool} tool")
    return Decision(RiskLevel.CONFIRM_REQUIRED, f"tool.{tool}", f"use the {tool} tool (not reviewed by XENIO)")


def _path_arg(args: dict, key: str, op: str, paths: PathPolicy, cwd) -> Decision:
    raw = args.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return blocked(f"filesystem.{op}", f"{op} a file", f"the '{key}' argument is missing")
    if op == "write":
        try:
            op = "modify" if paths.resolve(raw, cwd).exists() else "create"
        except ValueError as exc:
            return blocked("filesystem.write", f"write {raw!r}", f"invalid path: {exc}")
    return paths.check(raw, op, cwd)


def _classify_xenio_tool(tool: str, a: dict, paths: PathPolicy, cwd) -> Decision:
    if tool in DESKTOP_TOOLS:  # without the plugin's live probe: text-only rules (secret typing stays blocked)
        return classify_desktop_tool(tool, a, None)
    if tool == "xenio_look_at_screen":
        return classify_screen_look(a)  # the plugin adds screen_capture_consent (it knows the user's words)
    if tool in ("xenio_list_directory", "xenio_inspect_path"):
        return _path_arg(a, "path", "list", paths, cwd)
    if tool == "xenio_create_directory":
        return _path_arg(a, "path", "create", paths, cwd)
    if tool == "xenio_move_path":
        src, dst = a.get("source"), a.get("destination")
        if not isinstance(src, str) or not isinstance(dst, str) or not src.strip() or not dst.strip():
            return blocked("filesystem.move", "move a file", "both 'source' and 'destination' are required")
        move = paths.check(src, "move", cwd)
        dest = paths.check(dst, "create", cwd)
        if move.blocked or dest.blocked:
            return move if move.blocked else dest
        level = max(move.level, dest.level, RiskLevel.CONFIRM_REQUIRED)
        return Decision(level, "filesystem.move",
                        f"move {paths.describe(paths.resolve(src, cwd))} to {paths.describe(paths.resolve(dst, cwd))}",
                        scopes=move.scopes)
    if tool == "xenio_delete_path":
        raw = a.get("path")
        if not isinstance(raw, str) or not raw.strip():
            return blocked("filesystem.delete", "delete something", "the 'path' argument is missing")
        return paths.check(raw, "delete", cwd, recursive=True, permanent=bool(a.get("permanent")))
    if tool == "xenio_open_url":
        decision = classify_url(a.get("url") if isinstance(a.get("url"), str) else "", paths, cwd)
        if decision.blocked:
            return decision
        url = normalize_url(a.get("url"))
        if not url.lower().startswith(("http://", "https://")):
            return blocked("web.open_in_browser", f"open {str(a.get('url'))[:80]}",
                           "only http and https web pages can be opened in your browser")
        return read("web.open_in_browser", f"open {url_host(url)} in your web browser")
    if tool == "xenio_download_file":
        return classify_download(a, paths, cwd)
    if tool in PERSONAL_TOOLS:
        return classify_personal_tool(tool, a)
    return Decision(RiskLevel.CONFIRM_REQUIRED, f"tool.{tool}", f"use {tool}")


# =============================================================================
# Escalation messages (plugin -> Hermes approval gate -> ACP -> XENIO)
# =============================================================================

_ESCALATION_HEADER = re.compile(r"^XENIO\[(?P<level>[A-Z_]+)\]\s*(?P<summary>.*)$")


def escalation_message(decision: Decision, key: str = "") -> str:
    """``XENIO[LEVEL] summary`` + optional ``key: ...`` (dedupe) + ``scope: ...`` lines (approval coverage)."""
    lines = [f"XENIO[{decision.level.name}] {' / '.join(decision.summary.splitlines())}"]
    if key:
        lines.append(f"key: {' '.join(key.splitlines())}")
    lines.extend(f"scope: {s}" for s in decision.scopes)
    return "\n".join(lines)


def _escalation_parts(message: str) -> tuple[re.Match, list[str]] | None:
    lines = (message or "").strip().splitlines()
    m = _ESCALATION_HEADER.match(lines[0]) if lines else None
    if not m or m.group("level") not in RiskLevel.__members__:
        return None
    return m, lines[1:]


def parse_escalation(message: str) -> tuple[RiskLevel, str, str] | None:
    parts = _escalation_parts(message)
    if parts is None:
        return None
    m, rest = parts
    key = next((line[len("key: "):].strip() for line in rest if line.startswith("key: ")), "")
    return RiskLevel[m.group("level")], m.group("summary").strip(), key


def parse_escalation_scopes(message: str) -> tuple[str, ...]:
    parts = _escalation_parts(message)
    return tuple(line[len("scope: "):].strip() for line in parts[1] if line.startswith("scope: ")) if parts else ()


def default_path_policy(environ: dict[str, str] | None = None) -> PathPolicy:
    """Path policy configured from XENIO_* environment variables (used inside Hermes)."""
    env = os.environ if environ is None else environ
    workspace = env.get("XENIO_WORKSPACE")
    extra = tuple(Path(p) for p in re.split(r"[;,]", env.get("XENIO_ALLOWED_DIRS", "")) if p.strip())
    hermes_home = env.get("HERMES_HOME") or (os.path.join(env["LOCALAPPDATA"], "hermes") if sys.platform == "win32"
                                            and env.get("LOCALAPPDATA") else os.path.expanduser("~/.hermes"))
    return PathPolicy(workspace=Path(workspace) if workspace else None, extra_roots=extra, hermes_home=Path(hermes_home))
