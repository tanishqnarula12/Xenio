"""The ``xenio_foundry`` tool: how you command the team from chat or by voice. "Build this" is enough. It reaches XENIO's service over the same guarded local socket as the engineering tool, and the guard classifies each call first (``policy.classify_foundry_tool``): starting, redirecting and stopping work run at once; approving something, or giving an agent new powers, asks you. Standard library only (runs inside Hermes)."""

from __future__ import annotations

from typing import Any

from app.safety.policy import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult
from app.tools.engineering import call_service

FIELDS = ("goal", "path", "project", "autonomy", "workflow", "text", "to", "node", "note", "decision", "choice", "agent", "skill", "request", "agents", "network", "version", "constraints", "priority", "reason", "picks", "accept_risk", "everyone")


def foundry(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    command = str(args.get("command") or "status").lower()
    forwarded = {"command": command, **{k: args[k] for k in FIELDS if args.get(k) not in (None, "")}}
    out = call_service("foundry", forwarded, timeout=300.0)  # reading a new skill can take minutes
    if not out.get("ok"):
        return ToolResult.fail("foundry", str(out.get("error") or "the team could not do that"))
    result = out.get("result")
    return ToolResult.ok("foundry", "done", **(result if isinstance(result, dict) else {"items": result}))


_TOOLS = [
    Tool("foundry.team",
         "Your engineering organization. When the user asks to build, create or make something substantial, call this with command=start and goal: the team takes it from there, end to end, and comes back only for what is really the user's to decide. Say it has started and that they can watch it in the War Room; do not stay and report every step. Other commands: status (where it stands), message (pass on what the user says, with text; 'stop, use PostgreSQL instead' goes here), pause, resume, stop, approve, reject, decide (decision, choice or text), skill_request (install a skill from GitHub, npm or npx: 'request' is the user's words; shows what it found, nothing runs yet), skill_install (skill: after the user has seen the assessment), skill_assign (skill, agents).",
         {"type": "object", "properties": {"command": {"type": "string", "enum": ["start", "status", "list", "message", "pause", "resume", "stop", "stop_all", "approve", "reject", "decide", "skill_request", "skill_install", "skill_reject", "skill_assign", "skill_disable", "skill_uninstall"]}, "goal": {"type": "string", "description": "what to build, in the user's words"}, "path": {"type": "string", "description": "only if they named an existing project folder"}, "project": {"type": "string", "description": "only if they named an existing project"}, "workflow": {"type": "string"}, "text": {"type": "string"}, "to": {"type": "string", "description": "an agent, or leave out for the whole team"}, "node": {"type": "string"}, "note": {"type": "string"}, "decision": {"type": "string"}, "choice": {"type": "string"}, "skill": {"type": "string"}, "request": {"type": "string"}, "agents": {"type": "array", "items": {"type": "string"}}, "autonomy": {"type": "string", "enum": ["supervisor", "careful"]}}, "required": ["command"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_foundry", foundry),
]


def xenio_tools() -> list[Tool]:
    return list(_TOOLS)
