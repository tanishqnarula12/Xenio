"""The ``xenio_media`` tool: music and sound for the chat, through the same player code as XENIO's fast path
(``app/media.py``), so "pause", "next song" or "what's playing?" work the same whether the fast path or the model
handles them, by voice or by text.

Registered in Hermes by the xenio-guard plugin and classified by ``policy.classify_media_tool`` before it runs: changing
what plays and how loud is SAFE_WRITE, reading what plays is READ. Results carry ``verified`` from reading the player
back; the system media keys (a browser tab) cannot be read back and say so.
"""

from __future__ import annotations

from typing import Any

from app.safety.policy import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult, action_failed

_music = None


def music():
    global _music
    if _music is None:
        from app.config import load_settings
        from app.media import Music

        fast = load_settings().fast
        _music = Music(fast.music_service, fast.spotify_client_id, fast.spotify_client_secret)
    return _music


def set_music(value) -> None:
    """Tests: install a Music with fakes (None = build the real one on next use)."""
    global _music
    _music = value


def media(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    from app.media import MediaError, split_service

    action = str(args.get("action") or "").lower()
    try:
        if action == "now_playing":
            return ToolResult.ok("media", "read", **music().now_playing())
        if action in ("play", "pause", "next", "previous"):
            return ToolResult.ok("media", action, **music().control(action))
        if action == "volume":
            return ToolResult.ok("media", "volume", **music().volume(str(args.get("level") or "").lower()))
        if action == "play_song":
            query, said = split_service(str(args.get("query") or ""))
            service = music().choose(str(args.get("service") or said or "").lower())
            track = music().find(query, service)
            if track.service == "spotify":
                return ToolResult.ok("media", "play_song", track=track.label, **music().play_spotify(track))
            from app.tools import web

            opened = web.open_url({"url": track.url}, ctx)
            if not opened.success:
                return ToolResult.fail("media", opened.error or "the browser did not open")
            return ToolResult.ok("media", "play_song", player="YouTube in the browser", track=track.title,
                                 url=track.url, verified=None,
                                 note="opened in ONE browser tab; whether it plays cannot be read back")
        return ToolResult.fail("media", f"unknown media action {action!r}", "NOT_SUPPORTED")
    except MediaError as exc:
        return action_failed("media", str(exc), getattr(exc, "status", "FAILED"))


_S = {"type": "string"}
_TOOLS = [
    Tool("media.control",
         "Music and sound on the user's computer, the same player XENIO's own fast path uses. action: play_song "
         "('query': song and artist; 'service': spotify | youtube, else the user's default) | play | pause | next | "
         "previous (Spotify or Apple Music when they are playing, else the system media key) | now_playing (what "
         "Spotify / Music play; a browser tab cannot be read) | volume ('level': up | down | mute | unmute | 0-100). "
         "Report it as done only when the result is verified; for the media key say it was pressed.",
         {"type": "object", "properties": {"action": _S, "query": _S, "service": _S, "level": _S}, "required": ["action"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_media", media),
]


def xenio_tools() -> list[Tool]:
    return list(_TOOLS)
