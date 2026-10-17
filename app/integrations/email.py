"""Email, READ ONLY in V4: search, recent important messages, read one message.

Two backends, the first one that is set up wins:
  * Gmail   - through XENIO's Google sign-in (gmail.readonly: Google itself refuses any send/delete)
  * Himalaya - the Himalaya CLI (any IMAP account, e.g. a Gmail app password or a college mailbox);
               set up by the user with `himalaya` (its wizard), passwords stay in Himalaya's own config
Sending email is not a capability at all in V4 (and `himalaya ... send` is blocked by XENIO's policy).

Everything an email contains (sender name, subject, body) was written by someone else and is returned
as untrusted content.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from html import unescape
from pathlib import Path

from app.integrations.base import AuthState, Capability, Integration, IntegrationError, clip
from app.integrations.google import SCOPES, GoogleAuth, GoogleClient
from app.safety.policy import RiskLevel

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_SCOPE = SCOPES[0]
BODY_LIMIT = 6000
IMPORTANT_QUERY = "is:unread (is:important OR is:starred) newer_than:7d"
HIMALAYA_SETUP = ("No email account is connected. Either connect Google (`python -m app.main --connect-google`) "
                  "or set up any mailbox in Himalaya by running `himalaya` in a terminal (its wizard asks for the "
                  "address and an app password).")


@dataclass
class Message:
    id: str
    sender: str
    subject: str
    date: str = ""
    snippet: str = ""
    unread: bool = False
    important: bool = False
    labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", [], False) or k in ("id", "sender", "subject", "unread")}


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", " ", html))


def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _gmail_body(payload: dict) -> str:
    """Plain text of a Gmail message payload (prefers text/plain, falls back to stripped HTML)."""
    plain, html = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            html.append(_html_to_text(_b64(data)))
        for sub in part.get("parts") or []:
            if isinstance(sub, dict):
                walk(sub)

    walk(payload or {})
    text = "\n".join(plain) if plain else "\n".join(html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class GmailBackend:
    name = "gmail"

    def __init__(self, client: GoogleClient):
        self.client = client

    def ready(self) -> bool:
        return self.client.auth.has_scope(_SCOPE)

    def account(self) -> str:
        return (self.client.auth.load() or {}).get("account", "") or "Gmail"

    def search(self, query: str, limit: int) -> list[Message]:
        listing = self.client.get(f"{GMAIL}/messages", {"q": query or "in:inbox", "maxResults": limit})
        out = []
        for ref in (listing.get("messages") or [])[:limit]:
            meta = self.client.get(f"{GMAIL}/messages/{ref['id']}", {
                "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
            headers = {h.get("name", "").lower(): h.get("value", "") for h in (meta.get("payload") or {}).get("headers") or []}
            labels = list(meta.get("labelIds") or [])
            out.append(Message(id=str(meta.get("id") or ref["id"]), sender=clip(headers.get("from"), 120),
                               subject=clip(headers.get("subject") or "(no subject)", 200), date=clip(headers.get("date"), 60),
                               snippet=clip(unescape(meta.get("snippet") or ""), 240), unread="UNREAD" in labels,
                               important="IMPORTANT" in labels or "STARRED" in labels,
                               labels=[label for label in labels if not label.startswith("CATEGORY_")][:6]))
        return out

    def read(self, message_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", message_id or ""):
            raise IntegrationError("That is not a Gmail message id (use an id from an email search).")
        msg = self.client.get(f"{GMAIL}/messages/{message_id}", {"format": "full"})
        payload = msg.get("payload") or {}
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers") or []}
        body = _gmail_body(payload)
        parts = [p.get("filename") for p in payload.get("parts") or [] if isinstance(p, dict) and p.get("filename")]
        return {"id": message_id, "sender": clip(headers.get("from"), 160), "to": clip(headers.get("to"), 200),
                "subject": clip(headers.get("subject"), 240), "date": clip(headers.get("date"), 60),
                "body": body[:BODY_LIMIT], "truncated": len(body) > BODY_LIMIT, "attachments": parts[:10]}


class HimalayaBackend:
    """The Himalaya CLI with --json. Read-only commands only."""

    name = "himalaya"
    RUN = staticmethod(subprocess.run)  # replaced in tests

    def __init__(self, exe: str | None = None, account: str | None = None):
        self.exe = exe or find_himalaya()
        self.account_name = account or os.environ.get("XENIO_HIMALAYA_ACCOUNT") or None

    def _run(self, *args: str, json_out: bool = True, timeout: float = 60) -> str:
        if not self.exe:
            raise IntegrationError("Himalaya is not installed.")
        cmd = [self.exe, *args]
        if self.account_name:
            cmd += ["--account", self.account_name]
        if json_out:
            cmd.append("--json")
        try:
            proc = self.RUN(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrationError(f"Himalaya did not run ({type(exc).__name__}).") from None
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise IntegrationError(f"Himalaya failed: {clip(err[0] if err else 'unknown error', 200)}")
        return proc.stdout

    def ready(self) -> bool:
        if not self.exe:
            return False
        try:
            data = json.loads(self._run("account", "list", timeout=20) or "null")
        except (IntegrationError, ValueError):
            return False
        return bool(data)

    def account(self) -> str:
        return self.account_name or "Himalaya (default account)"

    @staticmethod
    def _query(text: str) -> list[str]:
        """Plain words become a subject/body/sender search; Himalaya's own syntax passes through."""
        text = (text or "").strip()
        if not text:
            return []
        if re.match(r"^(date|after|before|from|to|subject|body|flag|not)\b|\b(and|or|order by)\b", text):
            return text.split()
        q = '"' + text.replace('"', "") + '"'
        return ["subject", q, "or", "body", q, "or", "from", q]

    def search(self, query: str, limit: int) -> list[Message]:
        if query in ("", "in:inbox"):
            raw = self._run("envelope", "list", "--page-size", str(limit))
        else:
            words = self._query("flag flagged or not flag seen" if query == IMPORTANT_QUERY else query)
            raw = self._run("envelope", "search", "--page-size", str(limit), *words)
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            raise IntegrationError("Himalaya sent unreadable output.") from None
        out = []
        for env in (data.get("envelopes") if isinstance(data, dict) else data) or []:
            flags = {str(f.get("iana") or f.get("wire") or "").lower() if isinstance(f, dict) else str(f).lower()
                     for f in env.get("flags") or []}
            sender = (env.get("from") or [{}])[0] if env.get("from") else {}
            who = f"{sender.get('name') or ''} <{sender.get('email', '')}>".strip() if sender else ""
            out.append(Message(id=str(env.get("id")), sender=clip(who, 120), subject=clip(env.get("subject") or "(no subject)", 200),
                               date=clip(env.get("date"), 60), unread="seen" not in flags,
                               important=bool(flags & {"flagged", "important"})))
        return out[:limit]

    def read(self, message_id: str) -> dict:
        if not re.fullmatch(r"[\w.@<>+-]{1,128}", message_id or ""):
            raise IntegrationError("That is not a message id from an email search.")
        text = self._run("message", "read", message_id, json_out=False)
        return {"id": message_id, "body": text[:BODY_LIMIT], "truncated": len(text) > BODY_LIMIT}


def find_himalaya() -> str | None:
    exe = shutil.which("himalaya")
    if exe:
        return exe
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        candidate = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "himalaya" / "himalaya.exe"
        if candidate.is_file():
            return str(candidate)
    return None


class EmailIntegration(Integration):
    name = "email"
    title = "Email"
    capabilities = (
        Capability("email.search", "read", RiskLevel.READ, "search messages, recent important/unread messages"),
        Capability("email.read", "read", RiskLevel.READ, "read one message"),
    )  # sending is deliberately absent in V4

    def __init__(self, gmail: GmailBackend | None = None, himalaya: HimalayaBackend | None = None):
        self.gmail = gmail or GmailBackend(GoogleClient(GoogleAuth()))
        self.himalaya = himalaya or HimalayaBackend()

    def backend(self):
        if self.gmail.ready():
            return self.gmail
        if self.himalaya.ready():
            return self.himalaya
        return None

    def auth_state(self) -> AuthState:
        backend = self.backend()
        if backend is None:
            return AuthState("not_configured", "no email account connected", HIMALAYA_SETUP)
        return AuthState("connected", f"{backend.account()} via {backend.name} (read-only)")

    def _require(self):
        backend = self.backend()
        if backend is None:
            raise IntegrationError(HIMALAYA_SETUP)
        return backend

    def search(self, query: str = "", limit: int = 10, important_only: bool = False) -> dict:
        backend = self._require()
        limit = max(1, min(int(limit or 10), 25))
        q = IMPORTANT_QUERY if important_only and not query else query
        messages = backend.search(q, limit)
        return {"account": backend.account(), "backend": backend.name, "query": q or "inbox",
                "messages": [m.to_dict() for m in messages]}

    def read(self, message_id: str) -> dict:
        backend = self._require()
        return {"account": backend.account(), "backend": backend.name, **backend.read(str(message_id).strip())}
