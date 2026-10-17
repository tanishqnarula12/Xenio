"""XENIO tool registry: discovery of every tool XENIO can use, and gated execution of its own.

* ``backend="hermes"`` tools are Hermes built-ins (terminal, read_file, write_file, ...). XENIO
  reuses them; they run inside Hermes and are checked by the xenio-guard plugin.
* ``backend="xenio"`` tools (filesystem.list_directory, create_directory, move_path, delete_path,
  inspect_path, web.open_url, web.download_file) are XENIO's own. Inside Hermes they are registered
  by the plugin; here they can also be executed directly through ``ToolRegistry.execute``
  (permission check -> confirmation -> execution -> structured result).

Each integration is a module exposing ``TOOLS: list[Tool]`` (filesystem, terminal, web, github).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from app.logging_setup import event
from app.safety.permissions import PermissionGate, PermissionRequest
from app.safety.policy import PathPolicy
from app.tools import filesystem, github, terminal, web
from app.tools.base import Tool, ToolContext, ToolResult

log = logging.getLogger("xenio.tools")

ALL_TOOLS: list[Tool] = [*filesystem.TOOLS, *terminal.TOOLS, *web.TOOLS, *github.TOOLS]


class ToolRegistry:
    def __init__(self, paths: PathPolicy, tools: Iterable[Tool] | None = None, gate: PermissionGate | None = None):
        self.paths = paths
        self.gate = gate
        self._tools: dict[str, Tool] = {}
        for tool in tools if tools is not None else ALL_TOOLS:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name) or next((t for t in self._tools.values() if t.hermes_name == name), None)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def describe(self) -> list[dict[str, Any]]:
        """Discovery: what exists, who executes it, and its typical permission level."""
        return [
            {"name": t.name, "model_name": t.hermes_name, "backend": t.backend, "description": t.description,
             "permission_level": t.permission_level.name, "input_schema": t.input_schema}
            for t in self._tools.values()
        ]

    async def execute(self, name: str, args: dict[str, Any] | None, cwd: str | None = None) -> ToolResult:
        args = args if isinstance(args, dict) else {}
        tool = self.get(name)
        event(log, "TOOL_REQUESTED", tool=name)
        if tool is None:
            return ToolResult.fail(name, f"Unknown tool {name!r}")
        if tool.handler is None:
            return ToolResult.fail(tool.name, f"{tool.name} is executed by Hermes, not directly by XENIO")
        ctx = ToolContext(paths=self.paths, cwd=cwd)
        problem = tool.validate(args)
        if problem:
            return ToolResult.fail(tool.name, f"Invalid arguments: {problem}")
        decision = tool.classify(args, ctx)
        if decision.blocked:
            event(log, "PERMISSION_CHECK", tool=tool.name, risk=decision.level.name, decision="blocked")
            return ToolResult.fail(tool.name, f"Blocked by XENIO policy: {decision.reason}")
        gate = self.gate or PermissionGate(None)
        verdict = await gate.check(
            PermissionRequest(decision.action, decision.summary, decision.level, "xenio", {"tool": tool.name})
        )
        if not verdict.approved:
            return ToolResult.fail(tool.name, f"Not done: {verdict.reason}")
        return await asyncio.to_thread(tool.run, args, ctx)
