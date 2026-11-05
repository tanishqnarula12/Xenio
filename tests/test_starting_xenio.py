"""Starting XENIO when it is already running shows its window, and does not start a second copy."""

import asyncio
import http.server
import threading
from types import SimpleNamespace

import pytest

from app import main


def serving(page: bytes):
    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def xenio():
    server = serving(b"<!doctype html><html><head><title>XENIO</title></head></html>")
    yield server.server_port
    server.shutdown()


def test_it_recognises_itself(xenio):
    assert main._xenio_answers(xenio) is True


def test_something_else_on_the_port_is_not_xenio():
    server = serving(b"<html><head><title>Another app</title></head></html>")
    try:
        assert main._xenio_answers(server.server_port) is False
    finally:
        server.shutdown()


def test_nothing_on_the_port_is_not_xenio():
    server = serving(b"")
    port = server.server_port
    server.shutdown()
    server.server_close()
    assert main._xenio_answers(port) is False


def test_when_it_is_already_running_the_window_opens_and_nothing_else_starts(xenio, monkeypatch, capsys):
    shown = []
    monkeypatch.setattr(main, "_show_running_window", lambda: shown.append(True))
    settings = None  # anything that went on to start XENIO would need real settings, and fail here
    assert asyncio.run(main._run_ui(settings, SimpleNamespace(ui_port=xenio, no_browser=False))) == 0
    assert shown == [True] and "already running" in capsys.readouterr().out


def test_no_browser_means_no_window(xenio, monkeypatch):
    shown = []
    monkeypatch.setattr(main, "_show_running_window", lambda: shown.append(True))
    assert asyncio.run(main._run_ui(None, SimpleNamespace(ui_port=xenio, no_browser=True))) == 0
    assert shown == []
