"""XENIO's side of the password vault, and it holds nothing. Everything secret happens in the native helper (native/hands/vault.swift): your typing goes into its own windows, and the decrypting and the filling happen inside it.

Nothing in this file can take or return a password, on purpose, and there is no command that reads one back. What crosses this line is a website's name, a label and "filled".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from app.foundry import hands as hands_module

PANEL_SECONDS = 600.0  # you may take your time over a window


class VaultError(Exception):
    pass


def host_of(url: str) -> str:
    from urllib.parse import urlsplit

    text = str(url or "").strip().lower()
    host = (urlsplit(text if "://" in text else f"https://{text}").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def host_matches(page: str, saved: str) -> bool:
    """Saved for google.com: fine on accounts.google.com, and never on google.com.evil.com. The helper checks this again itself, on what the browser really shows."""
    page, saved = page.lower(), saved.lower()
    return page == saved or page.endswith("." + saved)


class Vault:
    def __init__(self, folder: Path, binary: Path | None = None, runner=subprocess.run):
        self.folder = Path(folder)
        self.binary = binary or hands_module.BINARY
        self.runner = runner

    def _run(self, *args: str, timeout: float = PANEL_SECONDS) -> dict:
        binary = hands_module.ensure_built(self.binary)
        done = self.runner([str(binary), "vault", *args, "--dir", str(self.folder)], capture_output=True, text=True, timeout=timeout)
        lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
        try:
            out = json.loads(lines[-1]) if lines else {}
        except ValueError:
            out = {}
        if not isinstance(out, dict) or not out.get("ok"):
            raise VaultError(str(out.get("error") if isinstance(out, dict) else "") or "the vault did not answer")
        return out

    def status(self) -> dict:
        """What is in there by name, and whether your finger is set up. This never asks you for anything."""
        return self._run("status", timeout=20.0)

    def setup(self) -> dict:
        return self._run("setup")

    def enrol_touch(self) -> dict:
        return self._run("touch")

    def add(self, host: str = "") -> dict:
        """Opens a window for you to type into. What you type never comes back here."""
        return self._run("add", *(["--host", str(host)[:200]] if host else []))

    def remove(self, item: str) -> dict:
        return self._run("remove", "--id", str(item)[:40])

    def open_site(self, url: str, popen=subprocess.Popen) -> dict:
        """A window for you, at this website, in a browser of its own that keeps nothing. If there is a saved login your fingerprint is asked for and it is placed. If you sign in by hand you are offered to save it, as Google does. XENIO only ever hears that it happened."""
        parts = urlsplit(str(url or "").strip() if "://" in str(url or "") else f"https://{str(url or '').strip()}")
        if parts.scheme != "https" or not parts.hostname:
            raise VaultError("that is not a website address")
        address = parts.geturl()
        binary = hands_module.ensure_built(self.binary)
        saved = self.login_for(address)
        proc = popen([str(binary), "browser", "--visible", "--width", "1100", "--height", "800", "--agent", "You", "--vault-dir", str(self.folder)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        commands = [{"id": 1, "cmd": "open", "url": address}]
        if saved is not None:
            commands.append({"id": 2, "cmd": "sign_in", "item": saved["id"], "submit": True})
        proc.stdin.write("".join(json.dumps(c) + "\n" for c in commands))
        proc.stdin.flush()
        return {"opened": parts.hostname, "saved_login": saved is not None}

    def fill_window(self, pid: int, title: str, item: str) -> dict:
        """Your email and password into a window of your own browser, after your fingerprint. The helper checks the window shows that website over HTTPS. Nothing secret comes back here: "filled" or why not."""
        return self._run("fill-window", "--pid", str(int(pid)), "--title", str(title)[:200], "--item", str(item)[:40], timeout=200.0)

    def login_for(self, url_or_host: str) -> dict | None:
        """The saved login for this website, by name only."""
        host = host_of(url_or_host)
        try:
            items = self.status().get("items") or []
        except (VaultError, OSError, subprocess.SubprocessError, hands_module.HandsError):
            return None
        fits = [i for i in items if host_matches(host, str(i.get("host") or ""))]
        return max(fits, key=lambda i: len(str(i.get("host") or "")), default=None)
