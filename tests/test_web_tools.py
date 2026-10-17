"""xenio_open_url and xenio_download_file against a local test web server (no internet needed)."""

import hashlib
import os
import socket
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.safety import policy as p
from app.safety.permissions import PermissionGate
from app.tools import web
from app.tools.base import ToolContext
from app.tools.registry import ToolRegistry

PAYLOAD = b"%PDF-1.4 hello from xenio\n" * 100


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        for key, value in headers:
            self.send_header(key, value)
        if not any(k == "Content-Length" for k, _ in headers):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/files/report.pdf":
            self._send(200, PAYLOAD, [("Content-Type", "application/pdf")])
        elif path == "/big.bin":
            self._send(200, b"x" * (3 * 1024 * 1024))
        elif path == "/liar.bin":  # claims 10 bytes, sends 3 MB: the streamed size still counts
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"y" * (3 * 1024 * 1024))
        elif path == "/get":  # name only in Content-Disposition
            self._send(200, b"col1,col2\n", [("Content-Disposition", 'attachment; filename="data.csv"')])
        elif path == "/getexe":
            self._send(200, b"MZ....", [("Content-Disposition", 'attachment; filename="setup.exe"')])
        elif path == "/redirect":
            self._send(302, headers=[("Location", "/files/report.pdf")])
        elif path in ("/401", "/403", "/404", "/429", "/500"):
            self._send(int(path[1:]), b"nope")
        else:
            self._send(404)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    paths = p.PathPolicy(workspace=ws)
    paths.named_roots = {**paths.named_roots, "Downloads": downloads}
    return ToolContext(paths=paths, cwd=str(ws))


def run(name, args, ctx):
    tool = next(t for t in web.TOOLS if t.hermes_name == name)
    return tool.run(args, ctx)


# ---- open in the user's browser ----------------------------------------------------------------

def test_open_url_uses_the_default_browser(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr(web, "OPENER", lambda url: opened.append(url) or True)
    result = run("xenio_open_url", {"url": "geeksforgeeks.org"}, ctx)
    assert result.success and opened == ["https://geeksforgeeks.org"]
    assert result.data["host"] == "geeksforgeeks.org" and "default web browser" in result.message


def test_open_url_rejects_non_web_links_and_reports_failures(ctx, monkeypatch):
    opened = []
    monkeypatch.setattr(web, "OPENER", lambda url: opened.append(url) or True)
    bad = run("xenio_open_url", {"url": "ms-settings:privacy"}, ctx)
    assert not bad.success and "Blocked by XENIO policy" in bad.error and opened == []
    assert not run("xenio_open_url", {"url": "javascript:alert(1)"}, ctx).success
    assert not run("xenio_open_url", {}, ctx).success  # malformed arguments

    def broken(url):
        raise OSError(2, "No application is associated")

    monkeypatch.setattr(web, "OPENER", broken)
    failed = run("xenio_open_url", {"url": "https://example.com"}, ctx)
    assert not failed.success and "could not open your browser" in failed.error
    monkeypatch.setattr(web, "OPENER", lambda url: False)
    assert not run("xenio_open_url", {"url": "https://example.com"}, ctx).success


# ---- downloads ----------------------------------------------------------------------------------

def test_download_saves_to_downloads_and_never_runs_it(ctx, server):
    result = run("xenio_download_file", {"url": f"{server}/files/report.pdf"}, ctx)
    target = ctx.paths.named_roots["Downloads"] / "report.pdf"
    assert result.success, result.error
    assert target.read_bytes() == PAYLOAD and result.data["bytes"] == len(PAYLOAD)
    assert result.data["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert result.data["program_file"] is False and "not opened or run" in result.message
    assert not list(target.parent.glob("*.xenio-part"))
    if os.name == "nt":
        assert result.data["marked_from_internet"] is True
        with open(f"{target}:Zone.Identifier", encoding="utf-8") as fh:
            assert "ZoneId=3" in fh.read()


def test_download_never_overwrites_unless_asked(ctx, server):
    target = ctx.paths.named_roots["Downloads"] / "report.pdf"
    target.write_bytes(b"mine")
    refused = run("xenio_download_file", {"url": f"{server}/files/report.pdf"}, ctx)
    assert not refused.success and "already exists" in refused.error and target.read_bytes() == b"mine"
    replaced = run("xenio_download_file", {"url": f"{server}/files/report.pdf", "overwrite": True}, ctx)
    assert replaced.success and target.read_bytes() == PAYLOAD


def test_download_destination_folder_and_file(ctx, server):
    folder = ctx.paths.workspace / "docs"
    folder.mkdir()
    into_folder = run("xenio_download_file", {"url": f"{server}/files/report.pdf", "destination": str(folder)}, ctx)
    assert into_folder.success and (folder / "report.pdf").is_file()
    named = run("xenio_download_file", {"url": f"{server}/files/report.pdf",
                                        "destination": str(folder / "renamed.pdf")}, ctx)
    assert named.success and (folder / "renamed.pdf").is_file()
    missing = run("xenio_download_file", {"url": f"{server}/files/report.pdf",
                                          "destination": str(ctx.paths.workspace / "nope" / "x.pdf")}, ctx)
    assert not missing.success and "does not exist" in missing.error


def test_download_size_limit(ctx, server):
    declared = run("xenio_download_file", {"url": f"{server}/big.bin", "max_mb": 1}, ctx)
    assert not declared.success and "limit" in declared.error and "Nothing was saved" in declared.error
    streamed = run("xenio_download_file", {"url": f"{server}/liar.bin", "max_mb": 1}, ctx)
    assert not streamed.success and "limit" in streamed.error
    leftovers = list(ctx.paths.named_roots["Downloads"].iterdir())
    assert leftovers == []  # no partial files left behind


def test_download_uses_server_file_name_but_never_sneaks_in_a_program(ctx, server):
    named = run("xenio_download_file", {"url": f"{server}/get"}, ctx)
    assert named.success and named.data["path"].endswith("data.csv")
    exe = run("xenio_download_file", {"url": f"{server}/getexe"}, ctx)
    assert not exe.success and "program file (setup.exe)" in exe.error
    assert not (ctx.paths.named_roots["Downloads"] / "setup.exe").exists()


def test_download_follows_redirects(ctx, server):
    result = run("xenio_download_file", {"url": f"{server}/redirect"}, ctx)
    assert result.success and result.data["source"].endswith("/files/report.pdf")


@pytest.mark.parametrize("path,phrase", [
    ("/401", "requires you to sign in"),
    ("/403", "refused access"),
    ("/404", "not found"),
    ("/429", "limiting requests"),
    ("/500", "website had a problem"),
])
def test_download_http_errors_are_reported_honestly(ctx, server, path, phrase):
    result = run("xenio_download_file", {"url": f"{server}{path}"}, ctx)
    assert not result.success and phrase in result.error and "Nothing was saved" in result.error
    assert list(ctx.paths.named_roots["Downloads"].iterdir()) == []


@pytest.mark.parametrize("reason,phrase", [
    (socket.gaierror(11001, "getaddrinfo failed"), "could not be found (DNS lookup failed)"),
    (TimeoutError("timed out"), "did not answer"),
    (ConnectionRefusedError(10061, "refused"), "refused the connection"),
])
def test_download_network_failures(ctx, monkeypatch, reason, phrase):
    def fail(*_a, **_k):
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(web, "URLOPEN", fail)
    result = run("xenio_download_file", {"url": "https://no-such-site.example/file.pdf"}, ctx)
    assert not result.success and phrase in result.error


def test_download_timeout_while_reading(ctx, monkeypatch):
    def slow(*_a, **_k):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(web, "URLOPEN", slow)
    result = run("xenio_download_file", {"url": "https://example.com/a.pdf"}, ctx)
    assert not result.success and "timeout" in result.error


def test_download_invalid_or_dangerous_requests(ctx):
    assert "Invalid arguments" in run("xenio_download_file", {"url": ""}, ctx).error
    assert "Invalid arguments" in run("xenio_download_file", {"url": "https://e.com/a", "max_mb": "big"}, ctx).error
    blocked = run("xenio_download_file", {"url": "https://e.com/x", "destination": "~/.ssh/authorized_keys"}, ctx)
    assert not blocked.success and "Blocked by XENIO policy" in blocked.error
    not_web = run("xenio_download_file", {"url": "file:///C:/Windows/win.ini"}, ctx)
    assert not not_web.success and "Blocked by XENIO policy" in not_web.error


# ---- through the registry: confirmation for program files -------------------------------------

class Confirmer:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def confirm(self, request, *, strict=False):
        self.asked.append(request.description)
        return self.answer


async def test_registry_asks_before_downloading_a_program(ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "URLOPEN", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(TimeoutError()))
    no = Confirmer(False)
    registry = ToolRegistry(ctx.paths, gate=PermissionGate(no))
    result = await registry.execute("xenio_download_file", {"url": "https://example.com/setup.exe"}, cwd=ctx.cwd)
    assert not result.success and "Not done" in result.error and calls == []  # declined -> nothing fetched
    assert no.asked and "setup.exe" in no.asked[0]
    names = {t["model_name"] for t in registry.describe()}
    assert {"xenio_open_url", "xenio_download_file", "web_search", "web_extract", "browser_exec"} <= names


def test_github_cli_status_never_needs_or_shows_a_token(monkeypatch):
    from app.tools import github

    monkeypatch.setattr(github.shutil, "which", lambda name: None)
    assert github.gh_status() == (False, False, github.gh_status()[2])
    assert "not installed" in github.gh_status()[2]

    class Done:
        def __init__(self, code):
            self.returncode, self.stdout, self.stderr = code, "token: gho_secret", ""

    monkeypatch.setattr(github.shutil, "which", lambda name: "C:/gh.exe")
    seen = []
    monkeypatch.setattr(github.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or Done(0))
    installed, signed_in, detail = github.gh_status()
    assert installed and signed_in and "gho_" not in detail and seen[0][1:] == ["auth", "status"]
    monkeypatch.setattr(github.subprocess, "run", lambda cmd, **kw: Done(1))
    assert github.gh_status()[:2] == (True, False)
