"""The ``xenio_engineering`` tool (V6): how Hermes, in the user's chat, works with XENIO's engineering team.

Registered in Hermes by the xenio-guard plugin. It forwards each call to XENIO's engineering service over a local
socket (``XENIO_ENGINEERING_SOCKET``, authenticated with a per-start token that only the chat's Hermes process gets).
The guard classifies every call first (``policy.classify_engineering_tool``): planning and implementing in isolated
worktrees run without an extra question; writing into the user's files, pushing and opening a pull request ask.
Standard library only (runs inside Hermes).
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

from app.safety.policy import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult

TIMEOUT = 60.0


def call_service(action: str, args: dict[str, Any], timeout: float = TIMEOUT) -> dict:
    path, token = os.environ.get("XENIO_ENGINEERING_SOCKET"), os.environ.get("XENIO_ENGINEERING_TOKEN")
    if not path or not token:
        return {"ok": False, "error": "XENIO's engineering team is not running in this session"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall((json.dumps({"token": token, "action": action, "args": args}) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data or b"{}")
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"XENIO's engineering service did not answer ({type(exc).__name__})"}


def engineering(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    action = str(args.get("action") or "status").lower()
    forwarded = {k: args[k] for k in ("request", "project", "path", "mode", "run_id", "auto_run", "remote", "draft")
                 if args.get(k) not in (None, "")}
    out = call_service(action, forwarded)
    if not out.get("ok"):
        return ToolResult.fail("engineering", str(out.get("error") or "the engineering service failed"))
    result = out.get("result")
    return ToolResult.ok("engineering", "done", **(result if isinstance(result, dict) else {"items": result}))


_S, _B = {"type": "string"}, {"type": "boolean"}
_TOOLS = [
    Tool("engineering.team",
         "XENIO's software engineering team for the user's repositories: inspect, plan, implement with specialist "
         "agents in isolated copies, test, security-review and code-review, then report with evidence. Actions: "
         "start (request; project name or path; mode: change | plan | explain | review | test; auto_run=true only "
         "if the user said to just do it) | status (what is running, the plan, failures) | plan | report | list | "
         "approve (the user said go ahead with a planned run) | cancel | apply (write the result into the user's "
         "files) | push (the run's branch) | pull_request (after push). Planning a team run waits for the user's go "
         "ahead. Never claim work is done or tests pass unless status/report says so. Small one-file edits can be "
         "done directly instead.",
         {"type": "object", "properties": {"action": _S, "request": _S, "project": _S, "path": _S, "mode": _S,
                                           "run_id": _S, "auto_run": _B, "remote": _S, "draft": _B},
          "required": ["action"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_engineering", engineering),
]


def xenio_tools() -> list[Tool]:
    return list(_TOOLS)
