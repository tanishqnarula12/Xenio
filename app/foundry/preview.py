"""The thing the team built, running, so you can try it and ask for changes. It runs from a throwaway copy, inside the sandbox, on a local port: your home folder unreadable, no keys, no internet, and nothing it writes lands in the project."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.foundry import sandbox

START_SECONDS = 25.0
RUN_FILE = ".xenio-run"
_SKIP = shutil.ignore_patterns(".git", "node_modules", ".venv", ".deps", "__pycache__", ".pytest_cache")
_STATIC_DIRS = ("", "web", "public", "dist", "build", "site", "static", "frontend", "client")
_ENTRY_FILES = ("run.py", "app.py", "main.py", "server.py", "wsgi.py", "asgi.py")


@dataclass
class Command:
    how: str  # a shell command, using $PORT
    kind: str


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:60_000]
    except OSError:
        return ""


def _python_entry(root: Path, python: str) -> Command | None:
    if (root / "manage.py").is_file():
        return Command(f'"{python}" manage.py runserver 127.0.0.1:$PORT --noreload', "django")
    candidates: list[tuple[Path, str]] = [(root / name, name[:-3]) for name in _ENTRY_FILES]
    for folder in ("app", "src", "server", "backend", "api"):
        if (root / folder / "__init__.py").is_file():
            candidates.append((root / folder / "__init__.py", folder))
        candidates += [(root / folder / name, f"{folder}.{name[:-3]}") for name in _ENTRY_FILES]
    for path, module in candidates:
        if not path.is_file():
            continue
        text = _read(path)
        fast = re.search(r"^(\w+)\s*=\s*FastAPI\(", text, re.M)
        if fast:
            return Command(f'"{python}" -m uvicorn {module}:{fast.group(1)} --host 127.0.0.1 --port $PORT', "fastapi")
        if re.search(r"\bFlask\(|def create_app\b", text):
            return Command(f'"{python}" -m flask --app {module} run --host 127.0.0.1 --port $PORT --no-reload', "flask")
    for path, _module in candidates:
        if path.is_file() and path.name != "__init__.py" and re.search(r"__main__|http\.server|listen\(|serve\(", _read(path)):
            return Command(f'"{python}" {path.relative_to(root)}', "python")
    return None


def detect(root: Path, python: str) -> Command | None:
    """How this project starts. What the team wrote in .xenio-run counts first."""
    root = Path(root)
    written = root / RUN_FILE
    if written.is_file():
        line = next((x.strip() for x in _read(written).splitlines() if x.strip() and not x.strip().startswith("#")), "")
        if line:
            return Command(line, "declared")
    for folder in (root, *[root / name for name in ("web", "frontend", "client", "ui")]):
        manifest = folder / "package.json"
        if not manifest.is_file():
            continue
        try:
            scripts = json.loads(_read(manifest)).get("scripts") or {}
        except ValueError:
            continue
        where = "" if folder == root else f'cd "{folder.relative_to(root)}" && '
        script = "dev" if "dev" in scripts else "start" if "start" in scripts else ""
        if script:
            body = str(scripts[script])
            flags = " -- --port $PORT --host 127.0.0.1" if "vite" in body else " -- -p $PORT -H 127.0.0.1" if "next" in body else ""
            return Command(f"{where}npm run {script}{flags}", "node")
    found = _python_entry(root, python)
    if found:
        return found
    for name in _STATIC_DIRS:
        if (root / name / "index.html").is_file():
            return Command(f'/usr/bin/python3 -m http.server $PORT --bind 127.0.0.1 --directory "{name or "."}"', "static")
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _answers(port: int) -> int | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/", timeout=4) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except OSError:
        return None


class Previews:
    def __init__(self, spawner=sandbox.spawn):
        self.spawner = spawner
        self.running: dict[str, dict] = {}
        self.failed: dict[str, dict] = {}  # why the last start failed, so it can be shown beside the button

    def status(self, key: str) -> dict:
        one = self.running.get(key)
        if one is None:
            return self.failed.get(key) or {"state": "stopped"}
        if one["process"].poll() is not None:
            self.running.pop(key, None)
            return {"state": "stopped", "detail": "it stopped by itself"}
        return {"state": "running", "url": one["url"], "command": one["command"], "kind": one["kind"], "since": one["since"], "status": one.get("status")}

    def stop(self, key: str) -> bool:
        self.failed.pop(key, None)
        one = self.running.pop(key, None)
        if one is None:
            return False
        try:
            os.killpg(one["process"].pid, signal.SIGKILL)
        except OSError:
            pass
        return True

    def stop_all(self) -> None:
        for key in list(self.running):
            self.stop(key)

    def start(self, key: str, source: Path, work: Path, python: str, readable: list[Path] | None = None, keep: bool = True) -> dict:
        """Starts it and asks it for its front page. Returns what really happened: a server that crashes on start, or answers 500, is a failure, and the reason goes back to the team."""
        self.stop(key)
        command = detect(Path(source), python)
        if command is None:
            detail = "there is nothing here to start, such as a server or a web page"
            if keep:
                self.failed[key] = {"state": "failed", "detail": detail}  # you pressed the button: it has to say something
            return {"ok": False, "state": "nothing", "detail": detail}
        copy = Path(work) / "preview"
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(source, copy, symlinks=True, ignore=_SKIP)
        modules = Path(source) / "node_modules"
        if modules.is_dir():
            (copy / "node_modules").symlink_to(modules)
        home = Path(work) / "preview-home"
        home.mkdir(parents=True, exist_ok=True)
        log = Path(work) / "preview.log"
        log.write_text("")
        port = _free_port()
        bins = [str(Path(python).parent)] if os.path.isabs(python) else []
        env = {"PORT": str(port), "HOST": "127.0.0.1", "FLASK_RUN_PORT": str(port), "HOME": str(home), "TMPDIR": str(home), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "npm_config_cache": str(home), "BROWSER": "none",
               "PATH": ":".join([f"{copy}/node_modules/.bin", *bins, sandbox._SYSTEM_PATH])}
        allowed = [Path(p) for p in [*(readable or []), source] if Path(p).exists()] + sandbox.tool_roots("node", "npm", "python3", python)  # a venv only points at its Python, which may live in the home folder
        if os.path.isabs(python):
            allowed.append(Path(python).parent.parent)
        try:
            process = self.spawner(["/bin/bash", "-c", command.how], copy, [copy, home], readable=allowed, env=env, log=log)
        except sandbox.SandboxUnavailable as exc:
            return {"ok": False, "state": "failed", "detail": str(exc)}
        deadline = time.monotonic() + START_SECONDS
        status = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            status = _answers(port)
            if status is not None:
                break
            time.sleep(0.4)
        tail = _read(log)[-1500:].strip()
        if process.poll() is not None or status is None or status >= 500:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            why = "it stopped as soon as it started" if process.poll() is not None and status is None else f"it answered with an error ({status})" if status else f"it did not answer within {int(START_SECONDS)} s"
            if keep:
                self.failed[key] = {"state": "failed", "detail": why, "command": command.how, "log": tail}
            return {"ok": False, "state": "failed", "command": command.how, "detail": why, "log": tail}
        url = f"http://127.0.0.1:{port}/"
        if keep:
            self.running[key] = {"process": process, "url": url, "command": command.how, "kind": command.kind, "since": time.time(), "status": status}
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        return {"ok": True, "state": "running" if keep else "checked", "url": url, "command": command.how, "kind": command.kind, "status": status}


__all__ = ["Previews", "detect", "subprocess"]
