"""Web capabilities.

Reused from Hermes (not re-implemented): ``web_search`` (search results with titles and links),
``web_extract`` (clean text of pages, with Hermes' own private-network and secret-in-link checks) and
the browser (``browser_exec`` in Hermes' default Browser Use mode, or the step-by-step
``browser_*`` tools). Every one of those calls is classified by the xenio-guard plugin first.

Hermes' browser is its own invisible Chromium, so XENIO adds two small tools:

* ``xenio_open_url`` shows a page to the user in THEIR default browser (nothing is automated there);
* ``xenio_download_file`` saves a file from an http(s) link: Downloads folder by default, never over
  an existing file unless asked, never into secret/system folders, size-limited, never opened or run,
  and marked as "downloaded from the internet" so Windows warns before anyone opens it.

Standard-library only: these run inside the Hermes process via the xenio-guard plugin.
"""

from __future__ import annotations

import hashlib
import os
import socket
import ssl
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.safety.policy import (
    RiskLevel,
    classify_url,
    download_target,
    normalize_url,
    safe_filename,
    url_host,
    _DOWNLOAD_PROGRAM_SUFFIXES,
)
from app.tools.base import Tool, ToolContext, ToolResult

DEFAULT_MAX_MB = 200
MAX_MB_LIMIT = 2048
TIMEOUT_SECONDS = 30
_CHUNK = 64 * 1024
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) XENIO/0.3 (personal assistant)"


def _default_opener(url: str) -> bool:
    if os.name == "nt":
        os.startfile(url)  # the user's default browser; only http(s) links get here
        return True
    return webbrowser.open(url, new=2)


# Replaceable in tests.
OPENER: Callable[[str], bool] = _default_opener
URLOPEN: Callable[..., Any] = urllib.request.urlopen


def open_url(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tool = "web.open_url"
    url = normalize_url(args["url"])
    if not url.lower().startswith(("http://", "https://")) or not url_host(url):
        return ToolResult.fail(tool, f"Not a web address: {args['url']!r} (only http and https pages can be opened)")
    try:
        opened = OPENER(url)
    except OSError as exc:
        return ToolResult.fail(tool, f"Windows could not open your browser: {exc.strerror or exc}")
    if opened is False:
        return ToolResult.fail(tool, "No web browser is available to open the page")
    return ToolResult.ok(tool, f"Opened {url} in your default web browser", url=url, host=url_host(url))


def _human_size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} bytes"


def _without_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _max_bytes(args: dict[str, Any]) -> int:
    raw = args.get("max_mb")
    env = os.environ.get("XENIO_DOWNLOAD_MAX_MB", "")
    mb = raw if isinstance(raw, int) and not isinstance(raw, bool) else int(env) if env.isdigit() else DEFAULT_MAX_MB
    return max(1, min(mb, MAX_MB_LIMIT)) * 1024 * 1024


def _mark_from_internet(path: Path, url: str) -> bool:
    """Windows "Mark of the Web": Windows and Office warn before the file is opened or run."""
    if os.name != "nt":
        return False
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="utf-8") as fh:
            fh.write(f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={_without_query(url)}\r\n")
        return True
    except OSError:
        return False


class _DownloadError(Exception):
    pass


def _http_error_text(code: int) -> str:
    known = {
        401: "the site requires you to sign in (HTTP 401)",
        403: "the site refused access - it may require a login or block downloads (HTTP 403)",
        404: "the file was not found (HTTP 404)",
        407: "a proxy requires a login (HTTP 407)",
        410: "the file is no longer available (HTTP 410)",
        429: "the site is limiting requests right now (HTTP 429) - try again later",
    }
    if code in known:
        return known[code]
    return f"the website had a problem (HTTP {code})" if code >= 500 else f"the website refused the request (HTTP {code})"


def _network_error_text(reason: Any) -> str:
    if isinstance(reason, socket.gaierror):
        return "the website could not be found (DNS lookup failed) - check the address or the internet connection"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return f"the website did not answer within {TIMEOUT_SECONDS} seconds"
    if isinstance(reason, ConnectionRefusedError):
        return "the website refused the connection"
    if isinstance(reason, ssl.SSLError):
        return "the website's security certificate could not be verified"
    return f"could not connect to the website ({reason})"


def download_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tool = "web.download_file"
    url = normalize_url(args["url"])
    if not url.lower().startswith(("http://", "https://")) or not url_host(url):
        return ToolResult.fail(tool, f"Not a downloadable web address: {args['url']!r} (http and https only)")
    overwrite = bool(args.get("overwrite", False))
    limit = _max_bytes(args)
    try:
        target = download_target(url, args.get("destination"), ctx.paths, ctx.cwd)
    except ValueError as exc:
        return ToolResult.fail(tool, f"Invalid destination: {exc}")
    where = ctx.paths.describe
    if target.exists() and not overwrite:
        return ToolResult.fail(tool, f"{where(target)} already exists - nothing was downloaded. Choose another name, "
                                     "or ask again to replace it.")
    if not target.parent.is_dir():
        return ToolResult.fail(tool, f"The folder {where(target.parent)} does not exist - nothing was downloaded")
    part = target.with_name(target.name + ".xenio-part")
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"})
    try:
        with URLOPEN(request, timeout=TIMEOUT_SECONDS) as response:
            final_url = response.geturl() or url
            redirect = classify_url(final_url)
            if redirect.blocked or not final_url.lower().startswith(("http://", "https://")):
                raise _DownloadError(f"the link redirected somewhere XENIO does not download from ({_without_query(final_url)[:100]})")
            length = response.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > limit:
                raise _DownloadError(f"the file is {_human_size(int(length))}, over the {_human_size(limit)} limit")
            suggested = response.headers.get_filename()
            if suggested and not target.suffix and not args.get("destination"):
                named = target.with_name(safe_filename(suggested, target.name))
                if named.suffix.lower() in _DOWNLOAD_PROGRAM_SUFFIXES:
                    raise _DownloadError(f"the server is sending a program file ({named.name}). Nothing was saved; "
                                         f"to get it anyway, ask to download it as {named.name} and confirm")
                check = ctx.paths.check(str(named), "create")
                if not check.blocked and check.level <= RiskLevel.SAFE_WRITE and not named.exists():
                    target, part = named, named.with_name(named.name + ".xenio-part")
            digest, size = hashlib.sha256(), 0
            with open(part, "wb") as fh:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise _DownloadError(f"the file is larger than the {_human_size(limit)} limit")
                    digest.update(chunk)
                    fh.write(chunk)
            content_type = response.headers.get_content_type()
        if target.exists() and not overwrite:
            raise _DownloadError(f"{where(target)} appeared while downloading; it was left untouched")
        os.replace(part, target)
    except _DownloadError as exc:
        return ToolResult.fail(tool, f"Download failed: {exc}. Nothing was saved.")
    except urllib.error.HTTPError as exc:
        return ToolResult.fail(tool, f"Download failed: {_http_error_text(exc.code)}. Nothing was saved.")
    except urllib.error.URLError as exc:
        return ToolResult.fail(tool, f"Download failed: {_network_error_text(exc.reason)}. Nothing was saved.")
    except (TimeoutError, socket.timeout):
        return ToolResult.fail(tool, f"Download failed: the website stopped answering (timeout after {TIMEOUT_SECONDS} s). "
                                     "Nothing was saved.")
    except ValueError as exc:
        return ToolResult.fail(tool, f"Download failed: invalid link ({exc}). Nothing was saved.")
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
    if not target.is_file() or target.stat().st_size != size:
        return ToolResult.fail(tool, "Download failed: the file was not written completely")
    marked = _mark_from_internet(target, final_url)
    program = target.suffix.lower() in _DOWNLOAD_PROGRAM_SUFFIXES
    note = " It is a program file: XENIO did not open or run it." if program else " It was not opened or run."
    return ToolResult.ok(tool, f"Downloaded {target.name} ({_human_size(size)}) to {where(target)}.{note}",
                         path=str(target), bytes=size, sha256=digest.hexdigest(), content_type=content_type,
                         source=_without_query(final_url), marked_from_internet=marked, program_file=program)


_URL = {"type": "string", "description": "A web address, e.g. https://www.geeksforgeeks.org (https:// is added if missing)."}

TOOLS: list[Tool] = [
    Tool("web.open_url",
         "Show a web page to the user by opening it in THEIR default web browser on this computer (e.g. 'open "
         "GeeksforGeeks on my laptop'). http/https only. This does not let you read the page - use web_extract or "
         "the browser tools for that.",
         {"type": "object", "properties": {"url": _URL}, "required": ["url"]},
         RiskLevel.READ, "xenio", "xenio_open_url", open_url),
    Tool("web.download_file",
         "Download a file from an http(s) link. Saves to the user's Downloads folder unless 'destination' (a folder or "
         "full file path) is given. Never overwrites an existing file unless overwrite=true, stops above the size "
         "limit, and never opens or runs the file. Program files (.exe, .msi, scripts) need the user's confirmation.",
         {"type": "object", "properties": {
             "url": _URL,
             "destination": {"type": "string", "description": "Folder or file path to save to (default: Downloads)."},
             "overwrite": {"type": "boolean", "description": "Replace an existing file with the same name (default false)."},
             "max_mb": {"type": "integer", "description": f"Size limit in MB (default {DEFAULT_MAX_MB})."}},
          "required": ["url"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_download_file", download_file),
    # Reused Hermes built-ins (executed by Hermes, checked by XENIO's policy first).
    Tool("web.search", "Search the web (Hermes web_search).", {"type": "object", "properties": {"query": {"type": "string"}}},
         RiskLevel.READ, "hermes", "web_search"),
    Tool("web.extract", "Read web pages as clean text (Hermes web_extract).",
         {"type": "object", "properties": {"urls": {"type": "array"}}}, RiskLevel.READ, "hermes", "web_extract"),
    Tool("browser.exec", "Drive Hermes' own browser with Python helpers (Hermes browser_exec, Browser Use mode). Each "
         "call is judged by what it does: reading is free, submitting/sending asks, cookies and passwords are blocked.",
         {"type": "object", "properties": {"code": {"type": "string"}}}, RiskLevel.READ, "hermes", "browser_exec"),
    Tool("browser.navigate", "Open a page in Hermes' browser (Hermes browser_navigate; when Browser Use mode is off).",
         {"type": "object", "properties": {"url": {"type": "string"}}}, RiskLevel.READ, "hermes", "browser_navigate"),
    Tool("browser.click", "Click an element (Hermes browser_click). Submit/send/buy buttons ask first.",
         {"type": "object", "properties": {"ref": {"type": "string"}}}, RiskLevel.SAFE_WRITE, "hermes", "browser_click"),
    Tool("browser.type", "Type into a field (Hermes browser_type). Password/payment fields are HIGH_RISK; secrets are blocked.",
         {"type": "object", "properties": {"ref": {"type": "string"}, "text": {"type": "string"}}},
         RiskLevel.SAFE_WRITE, "hermes", "browser_type"),
    Tool("browser.screenshot", "Look at the page (Hermes browser_vision).", {"type": "object", "properties": {"question": {"type": "string"}}},
         RiskLevel.READ, "hermes", "browser_vision"),
]


def xenio_tools() -> list[Tool]:
    return [t for t in TOOLS if t.backend == "xenio"]
