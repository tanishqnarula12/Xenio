"""Which version of XENIO's code a Hermes gateway is running.

Hermes 0.22 runs XENIO's sessions inside its gateway, a long-lived process (started on demand, ``--idle-exit``). The
xenio-guard plugin imports XENIO's tools and policy into it once, at start. After XENIO is updated, that gateway keeps
running the old code until it exits - observed twice: a fixed tool kept its old behaviour inside Hermes. So the plugin
records the fingerprint of the code it loaded (``gateway_code.json``), and XENIO restarts a gateway whose fingerprint
no longer matches before it connects (app/main.py).

The fingerprint is the paths, sizes and modification times of XENIO's Python files and the plugin: stat only, never
contents, fast enough to compute at every start. Deliberately free of other imports: it also runs inside Hermes.

``prompt_fingerprint`` is the other half (V3): Hermes freezes a session's system prompt - XENIO's rules included -
when the session starts and reuses it for every later turn, so a restarted gateway with new rules still showed a
resumed conversation the old ones (observed: the editor rules never reached the model). XENIO keeps the fingerprint of
the guard's prompt with the saved session and starts a new session when it changes.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path

RECORD = "gateway_code.json"


def code_fingerprint(root: str | os.PathLike) -> str:
    root = Path(root)
    digest = hashlib.sha256()
    for base in (root / "app", root / "hermes_plugin"):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if not name.endswith((".py", ".yaml")):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                digest.update(f"{path.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


def record_gateway_code(data_dir: str | os.PathLike, root: str | os.PathLike, pid: int) -> None:
    """Inside a gateway (the plugin): note which code this process loaded."""
    path = Path(data_dir) / RECORD
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": pid, "code": code_fingerprint(root)}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def recorded_gateway_code(data_dir: str | os.PathLike) -> dict:
    try:
        data = json.loads((Path(data_dir) / RECORD).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def prompt_fingerprint(plugin_file: str | os.PathLike) -> str:
    """The source of the guard plugin's system-prompt functions (its top-level functions named ``*_prompt``); "" when
    the plugin cannot be read."""
    try:
        source = Path(plugin_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return ""
    digest = hashlib.sha256()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.endswith("_prompt"):
            digest.update((ast.get_source_segment(source, node) or "").encode())
    return digest.hexdigest()[:16]
