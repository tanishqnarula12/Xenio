"""Transport-agnostic Hermes interface.

The rest of XENIO depends only on this module. Whether Hermes is reached over ACP stdio
(the current implementation, see ``adapter.py``), an HTTP gateway, or something else is an
adapter detail.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class HermesError(Exception):
    """Hermes failed or answered with something XENIO could not use."""


class HermesUnavailable(HermesError):
    """Hermes could not be started, or its process/connection died."""


class HermesTimeout(HermesError):
    """Hermes did not answer within the configured time."""


@dataclass
class ToolActivity:
    """A tool call Hermes made during a turn, as reported by Hermes itself."""

    tool_call_id: str
    title: str = ""
    kind: str = ""
    status: str = ""  # pending | in_progress | completed | failed
    detail: str = ""  # Hermes' own error text when the call failed (shown so failures are explained)


@dataclass
class HermesReply:
    text: str
    stop_reason: str = "end_turn"  # end_turn | cancelled | max_tokens | max_turn_requests | refusal
    tool_calls: list[ToolActivity] = field(default_factory=list)
    usage: dict[str, Any] | None = None

    @property
    def cancelled(self) -> bool:
        return self.stop_reason == "cancelled"


@dataclass
class HermesPermissionRequest:
    """Hermes asking the user before a dangerous command or a file edit."""

    title: str
    kind: str  # ACP tool kind: "execute", "edit", "delete", ...
    raw_input: dict[str, Any] = field(default_factory=dict)
    content: list[dict[str, Any]] = field(default_factory=list)
    options: list[dict[str, Any]] = field(default_factory=list)


PermissionHandler = Callable[[HermesPermissionRequest], Awaitable[bool]]
ChunkHandler = Callable[[str], None]


class HermesClient(ABC):
    agent_info: dict[str, Any]

    @abstractmethod
    async def start(self) -> None:
        """Connect to Hermes and open (or resume) the conversation session."""

    @abstractmethod
    async def send_message(self, message: str, on_chunk: ChunkHandler | None = None,
                           on_tool: Callable[[ToolActivity], None] | None = None) -> HermesReply:
        """Run one user turn through Hermes. ``on_chunk`` receives reply text as it streams; ``on_tool``
        each tool call as it starts and ends."""

    @abstractmethod
    async def cancel(self) -> None:
        """Ask Hermes to stop the turn in progress (barge-in / Ctrl+C)."""

    @abstractmethod
    async def new_session(self) -> None:
        """Start a fresh conversation."""

    @abstractmethod
    async def health_check(self) -> bool:
        """True when Hermes is reachable and speaks the expected protocol."""

    @abstractmethod
    async def close(self) -> None:
        """Shut Hermes down cleanly."""

    @abstractmethod
    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        """Register who decides Hermes' permission requests. With none, every request is denied."""
