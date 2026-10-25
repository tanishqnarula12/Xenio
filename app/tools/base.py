"""Tool abstraction: name, description, input schema, permission level, execute() -> ToolResult.

Standard-library only: the ``xenio-guard`` Hermes plugin imports this inside the Hermes process
to run XENIO's own tools. Every result is structured and every failure says so explicitly, so
nothing can be reported as done unless it actually succeeded.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.safety.policy import Decision, PathPolicy, RiskLevel, classify_tool_call

log = logging.getLogger("xenio.tools")

# Optional sink for audit events (the plugin points this at its JSON-lines log inside Hermes).
AUDIT: Callable[[str, dict[str, Any]], None] | None = None


def audit(event_name: str, **fields: Any) -> None:
    if AUDIT is not None:
        try:
            AUDIT(event_name, fields)
            return
        except Exception:
            pass
    log.info(event_name, extra={"fields": fields})


@dataclass
class ToolResult:
    success: bool
    tool: str
    message: str = ""
    data: dict[str, Any] | None = None
    error: str | None = None
    status: str | None = None  # a failure's kind (STATUSES); a success's is derived from its evidence

    @classmethod
    def ok(cls, tool: str, message: str, **data: Any) -> ToolResult:
        return cls(True, tool, message, data or None)

    @classmethod
    def fail(cls, tool: str, error: str, status: str = "FAILED") -> ToolResult:
        return cls(False, tool, error=error, status=status)

    @property
    def outcome(self) -> str:
        """What the evidence supports: SUCCEEDED only when nothing says otherwise; an action whose result carries
        ``verified`` other than True is UNVERIFIED; work still going on is RUNNING."""
        if not self.success:
            return self.status or "FAILED"
        data = self.data or {}
        if data.get("still_running") or data.get("running"):
            return "RUNNING"
        if "verified" in data and data["verified"] is not True:
            return "UNVERIFIED"
        return "SUCCEEDED"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"success": self.success, "tool": self.tool, "status": self.outcome}
        if self.success:
            out["message"] = self.message
            if self.data:
                out["data"] = self.data
        else:
            out["error"] = self.error or "unknown error"
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


# What a tool result may report (``status``): NEEDS_APPROVAL is never a result - XENIO's guard asks before the call.
STATUSES = ("SUCCEEDED", "UNVERIFIED", "RUNNING", "FAILED", "NOT_SUPPORTED", "NEEDS_CLARIFICATION", "NEEDS_PERMISSION")


def failure_status(message: str) -> str:
    """The kind of a desktop/media failure, from XENIO's own wording of it."""
    return "NEEDS_PERMISSION" if _missing_permission(message) else "FAILED"


def _missing_permission(message: str) -> str:
    from app.desktop.macos import ACCESSIBILITY_STEP, SCREEN_STEP  # stdlib-only module; the constants are plain text

    if ACCESSIBILITY_STEP in message:
        return "accessibility"
    if SCREEN_STEP in message:
        return "screen"
    return "automation" if "(Automation permission)" in message else ""


def action_failed(tool: str, message: str, status: str = "FAILED") -> ToolResult:
    """A desktop/media failure with its kind. A missing macOS permission also brings up the System Settings page to
    switch it on (only the page: the user grants it)."""
    kind = _missing_permission(message)
    if status == "FAILED" and kind:
        status = "NEEDS_PERMISSION"
        from app.desktop.macos import open_privacy_settings

        if open_privacy_settings(kind):
            message += " XENIO has opened that page in System Settings; the user only has to switch it on."
    return ToolResult.fail(tool, message, status)


@dataclass
class ToolContext:
    paths: PathPolicy
    cwd: str | None = None
    session: str = ""  # the Hermes session the call belongs to (desktop actions: one task per user request)


_JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "number": (int, float), "object": dict, "array": list}


@dataclass(frozen=True)
class Tool:
    name: str  # XENIO name, e.g. "filesystem.create_directory"
    description: str
    input_schema: dict[str, Any]
    permission_level: RiskLevel  # typical level; each call is classified on its real arguments
    backend: str  # "xenio" = executed by XENIO code; "hermes" = a Hermes built-in tool XENIO reuses
    hermes_name: str  # the tool name the model calls
    handler: Callable[[dict[str, Any], ToolContext], ToolResult] | None = None

    def classify(self, args: dict[str, Any], ctx: ToolContext) -> Decision:
        return classify_tool_call(self.hermes_name, args, ctx.paths, ctx.cwd)

    def validate(self, args: Any) -> str | None:
        """Minimal JSON-schema check: object, required keys, primitive types, no empty strings."""
        if not isinstance(args, dict):
            return "arguments must be an object"
        props = self.input_schema.get("properties", {})
        for key in self.input_schema.get("required", []):
            if key not in args or args[key] is None or (isinstance(args[key], str) and not args[key].strip()):
                return f"'{key}' is required"
        for key, value in args.items():
            spec = props.get(key)
            if spec is None:
                return f"unknown argument '{key}'"
            expected = _JSON_TYPES.get(spec.get("type", ""))
            if expected and (not isinstance(value, expected) or (spec.get("type") == "integer" and isinstance(value, bool))):
                return f"'{key}' must be of type {spec.get('type')}"
        return None

    def hermes_schema(self) -> dict[str, Any]:
        return {"name": self.hermes_name, "description": self.description, "parameters": self.input_schema}

    def run(self, args: Any, ctx: ToolContext) -> ToolResult:
        """Execute a XENIO-backed tool. Policy blocks are enforced here as well (defense in depth);
        confirmation of risky calls happens BEFORE this is called (Hermes' approval gate / registry)."""
        if self.handler is None:
            return ToolResult.fail(self.name, f"{self.name} is executed by Hermes, not by XENIO")
        problem = self.validate(args)
        if problem:
            audit("TOOL_EXECUTION_FAILED", tool=self.name, error=f"invalid arguments: {problem}")
            return ToolResult.fail(self.name, f"Invalid arguments: {problem}")
        decision = self.classify(args, ctx)
        if decision.blocked:
            audit("TOOL_EXECUTION_FAILED", tool=self.name, error=f"blocked: {decision.reason}")
            return ToolResult.fail(self.name, f"Blocked by XENIO policy: {decision.reason}")
        audit("TOOL_EXECUTION_STARTED", tool=self.name, level=decision.level.name)
        started = time.monotonic()
        try:
            result = self.handler(args, ctx)
        except PermissionError as exc:
            result = ToolResult.fail(self.name, f"Permission denied by the operating system: {exc.filename or exc}")
        except FileNotFoundError as exc:
            result = ToolResult.fail(self.name, f"Not found: {exc.filename or exc}")
        except OSError as exc:
            result = ToolResult.fail(self.name, f"Operating-system error: {exc.strerror or exc}")
        except Exception as exc:  # a tool bug must never look like success
            result = ToolResult.fail(self.name, f"Unexpected error: {type(exc).__name__}: {exc}")
        if not isinstance(result, ToolResult):
            result = ToolResult.fail(self.name, "tool returned an invalid result")
        audit("TOOL_EXECUTION_COMPLETED" if result.success else "TOOL_EXECUTION_FAILED", tool=self.name,
              ms=int((time.monotonic() - started) * 1000), **({"error": result.error} if not result.success else {}))
        return result
