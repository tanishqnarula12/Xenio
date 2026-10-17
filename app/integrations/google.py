"""Google sign-in and REST access for Calendar and Gmail - standard library only.

XENIO asks Google for the smallest useful permissions:
  * ``gmail.readonly``   - search and read email (V4 never sends email)
  * ``calendar.events``  - read events; creating one is always confirmed by the user first
(Hermes' google-workspace skill asks for much more: sending mail, Drive, Docs, Sheets.)

Sign-in (``python -m app.main --connect-google``) is the standard "installed app" flow: XENIO opens
Google's consent page in the user's browser and catches the answer on a one-time local port
(127.0.0.1, PKCE). XENIO never sees the user's password. The resulting refresh token is kept in
``<data dir>/google_token.json`` (a protected secrets file for XENIO's policy: the model can never
read it) and is never logged or returned by a tool.

The OAuth client comes from the user's own Google Cloud project ("Desktop app" client JSON).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.integrations.base import AuthState, IntegrationError, data_dir

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.events")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
TOKEN_FILE = "google_token.json"
TIMEOUT = 20
SETUP_HINT = ("Google is not connected. One-time setup: create a 'Desktop app' OAuth client in your Google Cloud "
              "project (Gmail API and Google Calendar API enabled), download its JSON to Downloads, then run "
              "`python -m app.main --connect-google` (see docs/PERSONAL_INTEGRATIONS.md).")

URLOPEN: Callable[..., Any] = urllib.request.urlopen  # replaced in tests


def token_path(folder: Path | None = None) -> Path:
    return (folder or data_dir()) / TOKEN_FILE


def _post_form(url: str, fields: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with URLOPEN(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        reason = _google_error(exc)
        raise IntegrationError(f"Google sign-in failed: {reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IntegrationError(f"Could not reach Google ({getattr(exc, 'reason', exc)})") from None


def _google_error(exc: urllib.error.HTTPError) -> str:
    """Google's own short error text (never the request, never a token)."""
    try:
        data = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
    except (ValueError, OSError):
        data = {}
    err = data.get("error")
    if isinstance(err, dict):
        return f"HTTP {exc.code}: {str(err.get('message') or err.get('status') or '')[:200]}"
    if isinstance(err, str):
        return f"HTTP {exc.code}: {err} {str(data.get('error_description') or '')[:160]}".strip()
    return f"HTTP {exc.code}"


# ---- token storage ----------------------------------------------------------------------------------

class GoogleAuth:
    """Holds the refresh token; hands out short-lived access tokens."""

    def __init__(self, folder: Path | None = None, clock: Callable[[], float] = time.time):
        self.path = token_path(folder)
        self._clock = clock
        self._lock = threading.Lock()

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def state(self) -> AuthState:
        data = self.load()
        if data is None:
            return AuthState("not_configured", "Google is not connected", SETUP_HINT)
        granted = set(data.get("scopes") or [])
        missing = [s.rsplit("/", 1)[-1] for s in SCOPES if s not in granted]
        who = data.get("account") or "your Google account"
        if missing:
            return AuthState("error", f"connected as {who}, but without permission for: {', '.join(missing)}",
                             "Run `python -m app.main --connect-google` again and allow every requested permission.")
        return AuthState("connected", f"connected as {who}")

    def has_scope(self, scope: str) -> bool:
        data = self.load()
        return bool(data) and scope in (data.get("scopes") or [])

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            data = self.load()
            if data is None:
                raise IntegrationError(SETUP_HINT)
            if not force_refresh and data.get("access_token") and float(data.get("expiry") or 0) - 60 > self._clock():
                return data["access_token"]
            fresh = _post_form(TOKEN_URL, {"client_id": data.get("client_id", ""), "client_secret": data.get("client_secret", ""),
                                           "refresh_token": data["refresh_token"], "grant_type": "refresh_token"})
            if not fresh.get("access_token"):
                raise IntegrationError("Google did not renew the sign-in. Run `python -m app.main --connect-google` again.")
            data["access_token"] = fresh["access_token"]
            data["expiry"] = self._clock() + float(fresh.get("expires_in") or 3600)
            try:
                self.save(data)
            except OSError:
                pass  # the fresh token still works for this call
            return data["access_token"]


# ---- REST ---------------------------------------------------------------------------------------------

class GoogleClient:
    def __init__(self, auth: GoogleAuth | None = None):
        self.auth = auth or GoogleAuth()

    def get(self, url: str, params: dict[str, Any] | None = None) -> dict:
        return self._request("GET", url, params)

    def post(self, url: str, body: dict, params: dict[str, Any] | None = None) -> dict:
        return self._request("POST", url, params, body)

    def _request(self, method: str, url: str, params: dict[str, Any] | None, body: dict | None = None) -> dict:
        query = urllib.parse.urlencode([(k, v) for k, v in (params or {}).items() if v is not None], doseq=True)
        full = f"{url}?{query}" if query else url
        for attempt in (0, 1):
            token = self.auth.access_token(force_refresh=attempt == 1)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(full, data=data, method=method, headers=headers)
            try:
                with URLOPEN(req, timeout=TIMEOUT) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 0:
                    continue  # access token expired early: renew once
                reason = _google_error(exc)
                if exc.code == 401:
                    raise IntegrationError("Google rejected the sign-in. Run `python -m app.main --connect-google` again.") from None
                if exc.code == 403:
                    raise IntegrationError(f"Google refused the request ({reason}). If it says the API is disabled, "
                                           "enable the Gmail API / Google Calendar API in your Cloud project.") from None
                if exc.code == 429:
                    raise IntegrationError("Google is rate-limiting requests; try again in a minute.") from None
                raise IntegrationError(f"Google request failed ({reason}).") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise IntegrationError(f"Could not reach Google ({getattr(exc, 'reason', exc)}).") from None
            except ValueError:
                raise IntegrationError("Google sent an unreadable answer.") from None
        raise IntegrationError("Google rejected the sign-in.")  # pragma: no cover


# ---- sign-in (run by the user: python -m app.main --connect-google) --------------------------------------

# Google names the download client_secret_<id>.json; people often rename it (credentials.json, ...).
_CLIENT_PATTERNS = ("client_secret*.json", "*credentials*.json", "google_client_secret.json")


def client_kind(path: Path) -> str | None:
    """"installed" (a 'Desktop app' client), "web" (a 'Web application' client) or None (not an OAuth client)."""
    try:
        if path.stat().st_size > 20_000:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for kind in ("installed", "web"):
        info = data.get(kind) if isinstance(data, dict) else None
        if isinstance(info, dict) and info.get("client_id") and info.get("client_secret"):
            return kind
    return None


def find_client_file(folders: list[Path]) -> Path | None:
    """The Google OAuth client JSON to sign in with: the newest 'Desktop app' client in the given folders. A
    'Web application' client (e.g. another project's website login in Downloads) is only returned when there
    is no Desktop client at all, so ``load_client`` can explain what to create."""
    found: dict[Path, str] = {}
    for folder in folders:
        for pattern in _CLIENT_PATTERNS:
            try:
                matches = [p for p in folder.glob(pattern) if p.is_file()]
            except OSError:
                continue
            for path in matches:
                if (kind := client_kind(path)) is not None:
                    found[path.resolve()] = kind
    pool = [p for p, kind in found.items() if kind == "installed"] or list(found)
    return max(pool, key=lambda p: p.stat().st_mtime) if pool else None


def load_client(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IntegrationError(f"Could not read the OAuth client file {path.name}: {exc}") from None
    info = data.get("installed") or data.get("web") or {}
    if not info.get("client_id") or not info.get("client_secret"):
        raise IntegrationError(f"{path.name} is not a Google OAuth client file (no client_id/client_secret).")
    if "web" in data and "installed" not in data:
        project = info.get("project_id") or "unknown project"
        raise IntegrationError(
            f"{path.name} (project {project}) is a 'Web application' client. XENIO signs in like a desktop app, "
            "so it needs a 'Desktop app' OAuth client: Google Cloud console > APIs & Services > Credentials > "
            "Create credentials > OAuth client ID > Application type: Desktop app > Download JSON, then run "
            "--connect-google again (or pass --client-file PATH).")
    return info["client_id"], info["client_secret"]


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] = {}

    def do_GET(self):  # noqa: N802 - http.server API
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        type(self).result = query
        ok = "code" in query
        page = ("<h2>XENIO is connected to Google.</h2><p>You can close this tab.</p>" if ok else
                f"<h2>Google sign-in was not completed</h2><p>{query.get('error', '')}</p>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())

    def log_message(self, *args):  # keep the code out of the console
        pass


def sign_in(client_file: Path, open_browser: Callable[[str], Any], say: Callable[[str], None] = print,
            timeout: float = 300.0, folder: Path | None = None) -> AuthState:
    """Interactive Google consent via the user's browser; saves the token. Raises IntegrationError."""
    client_id, client_secret = load_client(client_file)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    _Catcher.result = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_address[1]}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "access_type": "offline", "prompt": "consent"})
    say("Opening Google's consent page in your browser. If it does not open, paste this link into it:\n" + url)
    open_browser(url)
    deadline = time.monotonic() + timeout
    try:
        while not _Catcher.result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    answer = _Catcher.result
    if not answer:
        raise IntegrationError("No answer from Google within 5 minutes; run --connect-google again.")
    if answer.get("state") != state:
        raise IntegrationError("Google's answer did not match this sign-in (state mismatch); run it again.")
    if "code" not in answer:
        raise IntegrationError(f"Google sign-in was cancelled ({answer.get('error', 'no code')}).")
    token = _post_form(TOKEN_URL, {"client_id": client_id, "client_secret": client_secret, "code": answer["code"],
                                   "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"})
    if not token.get("refresh_token"):
        raise IntegrationError("Google returned no refresh token; remove XENIO under myaccount.google.com/permissions "
                               "and connect again.")
    granted = (token.get("scope") or "").split()
    auth = GoogleAuth(folder)
    auth.save({"client_id": client_id, "client_secret": client_secret, "refresh_token": token["refresh_token"],
               "access_token": token.get("access_token", ""), "expiry": time.time() + float(token.get("expires_in") or 3600),
               "scopes": granted, "connected": time.strftime("%Y-%m-%dT%H:%M:%S")})
    try:
        profile = GoogleClient(auth).get("https://gmail.googleapis.com/gmail/v1/users/me/profile")
        data = auth.load() or {}
        data["account"] = profile.get("emailAddress", "")
        auth.save(data)
    except IntegrationError:
        pass  # Gmail scope refused: Calendar may still work; state() reports what is missing
    return auth.state()


def sign_out(folder: Path | None = None) -> bool:
    """Revoke the token at Google (best effort) and delete it locally."""
    auth = GoogleAuth(folder)
    data = auth.load()
    if data is None:
        return False
    try:
        _post_form(REVOKE_URL, {"token": data["refresh_token"]})
    except IntegrationError:
        pass
    auth.path.unlink(missing_ok=True)
    return True
