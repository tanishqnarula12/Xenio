"""XENIO's profile vault (V4 phase 2): the reusable details the user chose to keep, for filling forms.

Deliberately not Hermes' memory: memory is injected into every prompt, this is looked up only when a form needs it, and
the model sees labels, not values (app.autofill). Every record, labels included, is encrypted at rest:

* the key is a random 256-bit value in the macOS login Keychain, written through ``security -i`` on stdin so it never
  appears in a process list, and read only by /usr/bin/security;
* each record is AES-256-CBC with its own random IV (CommonCrypto, part of macOS, no new dependency), authenticated with
  HMAC-SHA256 over the IV, ciphertext and record id, so a record can neither be altered nor moved to another id;
* ``<data dir>/profile_vault.db`` is mode 0600, with SQLite's secure_delete on: a deleted record is overwritten, and
  nothing is cached between calls, so a deleted value is gone for autofill at once.

Protects against: the file being copied, backed up or read by a tool, and values reaching logs or prompts. Not against
another program running as this user, which could ask Keychain the same way (XENIO's own terminal tool is refused that
command by app.safety.policy).

Refused outright: passwords, codes, tokens, API keys and payment details. They do not belong here, whatever they are called.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from app.integrations.base import data_dir
from app.safety.policy import contains_personal_secret, contains_secret

DB_FILE = "profile_vault.db"
KEYCHAIN_SERVICE = "XENIO Profile Vault"
KEYCHAIN_ACCOUNT = "profile-key"
_SECURITY = "/usr/bin/security"
MAX_LABEL = 80
MAX_VALUE = 2000
MAX_ENTRIES = 500

CATEGORIES = {"identity": "Identity and contact", "education": "Education", "work": "Work and skills",
              "portfolio": "Portfolio and projects", "address": "Addresses", "other": "Other details"}
SOURCES = {"mission_control": "You added this in Mission Control", "chat": "You said this in a conversation",
           "approved": "You approved saving this"}


class VaultError(Exception):
    pass


class VaultUnavailable(VaultError):
    """The key cannot be had: not macOS, or the Keychain is locked or refused. Nothing can be read or saved."""


class VaultCorrupt(VaultError):
    """A record failed its authentication: it was changed, or the key is not the one it was written with."""


class SecretRefused(VaultError):
    """A password, code, token or payment detail: never stored here."""


# ---- what is refused, what is sensitive ------------------------------------------------------------------------------

_POSTAL_PIN = re.compile(r"\bpin\s*-?\s*code\b|\bpincode\b", re.IGNORECASE)  # India's postal code, not a secret
_SECRET_LABEL = re.compile(
    r"pass(word|wd|code|phrase|key)\b|\bpin\b|\botp\b|one[ _-]?time|\b2fa\b|\bmfa\b|totp|\bcvv\b|\bcvc\b|security\s+(code|answer|question)"
    r"|card\s*(number|no\b|num\b|details?)|credit\s*card|debit\s*card|\bcc\s*(num|no)\b|expiry\s+date|\biban\b|routing\s+number"
    r"|(bank\s+)?account\s*(number|no\b)|\bupi\b|\bifsc\b|\bswift\s+code\b|\bsort\s+code\b|\btoken\b|api\s*[-_ ]?key|secret\b|private\s+key"
    r"|recovery\s+(code|phrase)|seed\s+phrase|session\s+id|cookie|\bssn\b|social\s+security|mother'?s\s+maiden", re.IGNORECASE)
_SENSITIVE_LABEL = re.compile(
    r"passport|aadh?aa?r|\bpan\b|national\s+id|\bvisa\b|driv(ing|er'?s?)\s+licen[cs]e|voter\s+id|tax\s+(id|number)|\btin\b"
    r"|salary|\bctc\b|income|medical|health|disab|blood\s+group|religion|caste|ethnic", re.IGNORECASE)


def secret_label(label: str) -> bool:
    text = _POSTAL_PIN.sub(" ", str(label or ""))
    return bool(_SECRET_LABEL.search(text))


def sensitive_label(label: str) -> bool:
    return bool(_SENSITIVE_LABEL.search(str(label or "")))


def refuse_secret(label: str, value: str) -> None:
    if secret_label(label):
        raise SecretRefused(f"{str(label)[:40]!r} is a password, code or payment detail: XENIO never stores those")
    if contains_secret(value, strict=False) or contains_personal_secret(value):
        raise SecretRefused("that value looks like a password, key, token or card number: XENIO never stores those")


def mask(value: str) -> str:
    """Enough to tell two entries apart, not enough to use."""
    text = str(value or "")
    return "•" * 6 + (text[-2:] if len(text) >= 8 else "")


# ---- the key --------------------------------------------------------------------------------------------------------

class KeychainKey:
    """The vault's key in the macOS login Keychain."""

    def __init__(self, service: str = KEYCHAIN_SERVICE, account: str = KEYCHAIN_ACCOUNT, runner=subprocess.run):
        self.service, self.account, self._run = service, account, runner

    def get(self) -> bytes | None:
        """The key; None when there is none yet."""
        if sys.platform != "darwin":
            raise VaultUnavailable("the profile vault uses the macOS Keychain, which is not available on this system")
        try:
            out = self._run([_SECURITY, "find-generic-password", "-s", self.service, "-a", self.account, "-w"],
                            capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:  # a locked Keychain can wait on its own password dialog
            raise VaultUnavailable(f"the macOS Keychain did not answer ({type(exc).__name__}): is it locked?") from None
        if out.returncode == 44:  # errSecItemNotFound
            return None
        if out.returncode != 0:
            raise VaultUnavailable(f"the macOS Keychain refused to give the vault's key (code {out.returncode}): is it locked?")
        try:
            return bytes.fromhex(out.stdout.strip())
        except ValueError:
            raise VaultUnavailable("the vault's Keychain entry is not a key XENIO wrote") from None

    def create(self) -> bytes:
        """A new key. Through stdin: arguments can be seen in a process list, even for a moment."""
        if sys.platform != "darwin":
            raise VaultUnavailable("the profile vault uses the macOS Keychain, which is not available on this system")
        key = secrets.token_bytes(32)
        line = (f'add-generic-password -U -s "{self.service}" -a "{self.account}" -l "{self.service}" '
                f"-T {_SECURITY} -w {key.hex()}\n")
        try:
            out = self._run([_SECURITY, "-i"], input=line, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VaultUnavailable(f"the macOS Keychain did not answer ({type(exc).__name__}): is it locked?") from None
        if out.returncode != 0 or self.get() != key:
            raise VaultUnavailable("the macOS Keychain did not keep the vault's new key: is it locked?")
        return key


# ---- the cipher ------------------------------------------------------------------------------------------------------

_ENCRYPT, _DECRYPT, _AES, _PKCS7 = 0, 1, 0, 1
_VERSION = b"\x01"
_IV, _TAG = 16, 32


def _common_crypto():
    fn = ctypes.CDLL("/usr/lib/libSystem.B.dylib").CCCrypt
    fn.restype = ctypes.c_int32
    fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                   ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    return fn


def _aes(op: int, key: bytes, iv: bytes, data: bytes) -> bytes:
    out = ctypes.create_string_buffer(len(data) + 32)
    moved = ctypes.c_size_t(0)
    status = _common_crypto()(op, _AES, _PKCS7, key, len(key), iv, data, len(data), out, len(out), ctypes.byref(moved))
    if status != 0:
        raise VaultCorrupt(f"a record could not be {'read' if op == _DECRYPT else 'protected'} (CommonCrypto {status})")
    return out.raw[:moved.value]


def _subkeys(master: bytes) -> tuple[bytes, bytes]:
    return (hmac.new(master, b"xenio-vault-encrypt-1", hashlib.sha256).digest(),
            hmac.new(master, b"xenio-vault-authenticate-1", hashlib.sha256).digest())


def seal(master: bytes, record_id: str, plain: bytes) -> bytes:
    enc, mac = _subkeys(master)
    iv = secrets.token_bytes(_IV)
    body = iv + _aes(_ENCRYPT, enc, iv, plain)
    return _VERSION + body + hmac.new(mac, _VERSION + record_id.encode() + b"\0" + body, hashlib.sha256).digest()


def unseal(master: bytes, record_id: str, blob: bytes) -> bytes:
    enc, mac = _subkeys(master)
    if len(blob) < 1 + _IV + _TAG or blob[:1] != _VERSION:
        raise VaultCorrupt("a record is not in the vault's format")
    body, tag = blob[1:-_TAG], blob[-_TAG:]
    if not hmac.compare_digest(tag, hmac.new(mac, _VERSION + record_id.encode() + b"\0" + body, hashlib.sha256).digest()):
        raise VaultCorrupt("a record failed its check: it was changed, or the key is not the one it was written with")
    return _aes(_DECRYPT, enc, body[:_IV], body[_IV:])


# ---- the store -------------------------------------------------------------------------------------------------------

_SCHEMA = "CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, blob BLOB NOT NULL);"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class ProfileVault:
    """``keys``: where the key lives (``get`` -> bytes | None, ``create`` -> bytes)."""

    def __init__(self, path: Path | None = None, keys=None):
        self.path = Path(path) if path else data_dir() / DB_FILE
        self.keys = keys if keys is not None else KeychainKey()

    # ---- plumbing ------------------------------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA secure_delete=ON")
        conn.executescript(_SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return conn

    def _key(self, create: bool = False) -> bytes | None:
        key = self.keys.get()
        if key is None and create:
            key = self.keys.create()
        return key

    def _read_all(self) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT id, blob FROM entries").fetchall()
        finally:
            conn.close()
        if not rows:
            return []
        key = self._key()
        if key is None:  # records without their key: never "an empty profile"
            raise VaultUnavailable("the vault's key is missing from the macOS Keychain, so its details cannot be read")
        out = []
        for record_id, blob in rows:
            record = json.loads(unseal(key, record_id, bytes(blob)).decode("utf-8"))
            out.append(record)
        out.sort(key=lambda r: (list(CATEGORIES).index(r.get("category")) if r.get("category") in CATEGORIES else 99,
                                r.get("created") or "", r.get("id") or ""))
        return out

    def _write(self, record: dict) -> None:
        blob = seal(self._key(create=True), record["id"], json.dumps(record, ensure_ascii=False).encode("utf-8"))
        conn = self._connect()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO entries (id, blob) VALUES (?, ?)", (record["id"], blob))
        finally:
            conn.close()

    # ---- what callers use ---------------------------------------------------------------------------------------

    def status(self) -> dict:
        """Whether the vault can be used right now, in words the Profile page can show."""
        try:
            entries = self._read_all()
            return {"available": True, "count": len(entries), "protected_by": "macOS Keychain, AES-256"}
        except VaultError as exc:
            return {"available": False, "count": 0, "reason": str(exc)}

    def full_entries(self) -> list[dict]:
        """Every entry with its value: for the autofill engine and the owner's own UI only. Never logged, and not to be
        put in a tool result as it is (``public``)."""
        return self._read_all()

    def get(self, entry_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT blob FROM entries WHERE id = ?", (str(entry_id),)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        key = self._key()
        if key is None:
            raise VaultUnavailable("the vault's key is missing from the macOS Keychain")
        return json.loads(unseal(key, str(entry_id), bytes(row[0])).decode("utf-8"))

    @staticmethod
    def public(entry: dict, with_values: bool = False) -> dict:
        """An entry as shown: without its value unless asked, and a sensitive value only ever masked."""
        out = {k: entry.get(k) for k in ("id", "category", "field", "label", "sensitive", "source", "created", "updated")}
        out["source_words"] = SOURCES.get(str(entry.get("source")), str(entry.get("source") or ""))
        out["corrected"] = bool(entry.get("corrected"))
        if with_values:
            out["value"] = mask(entry.get("value", "")) if entry.get("sensitive") else entry.get("value", "")
            out["masked"] = bool(entry.get("sensitive"))
        return out

    def entries(self, with_values: bool = False) -> list[dict]:
        return [self.public(e, with_values) for e in self._read_all()]

    def add(self, category: str, label: str, value: str, field: str = "", sensitive: bool | None = None,
            source: str = "mission_control") -> dict:
        label, value = " ".join(str(label or "").split()), str(value or "").strip()
        self._check(category, label, value, source)
        existing = self._read_all()
        if len(existing) >= MAX_ENTRIES:
            raise VaultError(f"the profile holds {MAX_ENTRIES} details, its limit: remove some first")
        now = _now()
        record = {"id": secrets.token_hex(8), "category": category, "label": label, "value": value,
                  "field": str(field or "")[:40], "sensitive": sensitive_label(label) if sensitive is None else bool(sensitive),
                  "source": source, "created": now, "updated": now}
        self._write(record)
        return self.public(record)

    def update(self, entry_id: str, *, label: str | None = None, value: str | None = None, category: str | None = None,
               field: str | None = None, sensitive: bool | None = None) -> dict:
        record = self.get(entry_id)
        if record is None:
            raise VaultError("that detail is not in the profile any more")
        new_label = " ".join(str(label).split()) if label is not None else record["label"]
        new_value = str(value).strip() if value is not None else record["value"]
        new_category = category if category is not None else record["category"]
        self._check(new_category, new_label, new_value, record.get("source") or "mission_control")
        record.update(label=new_label, value=new_value, category=new_category, updated=_now(), corrected=True)
        if field is not None:
            record["field"] = str(field)[:40]
        if sensitive is not None:
            record["sensitive"] = bool(sensitive)
        elif sensitive_label(new_label):
            record["sensitive"] = True  # renamed to something sensitive: never less protected than a new entry
        self._write(record)
        return self.public(record)

    def delete(self, entry_id: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                removed = conn.execute("DELETE FROM entries WHERE id = ?", (str(entry_id),)).rowcount
        finally:
            conn.close()
        return bool(removed)

    @staticmethod
    def _check(category: str, label: str, value: str, source: str) -> None:
        if category not in CATEGORIES:
            raise VaultError(f"category must be one of: {', '.join(CATEGORIES)}")
        if not label or not value:
            raise VaultError("a detail needs a name and a value")
        if len(label) > MAX_LABEL or len(value) > MAX_VALUE:
            raise VaultError(f"too long: at most {MAX_LABEL} characters for the name and {MAX_VALUE} for the value")
        if source not in SOURCES:
            raise VaultError("unknown source")
        refuse_secret(label, value)
