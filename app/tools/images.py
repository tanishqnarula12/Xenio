"""Pictures as a tool (V4 phase 4). Registered in Hermes by the xenio-guard plugin; judged by ``app.safety.policy`` before each call. A job starts and the
turn goes on: the picture appears in the conversation by itself, so the chat is never held up."""

from __future__ import annotations

from typing import Any

from app.images.studio import ACTIVE, CARD_TAG, ImageStudio, StudioError
from app.safety.policy import RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult

_studio = None


def studio() -> ImageStudio:
    global _studio
    if _studio is None:
        _studio = ImageStudio()
    return _studio


def install(value=None) -> None:
    """Tests: a studio on a temporary folder with a fake provider (None = build the real one on next use)."""
    global _studio
    _studio = value


def _about(jobs: list[dict]) -> dict:
    running = any(j["state"] in ACTIVE for j in jobs)
    return {"jobs": [{"id": j["id"], "state": j["state"], "prompt": j["prompt"], **({"error": j["error"]} if j.get("error") else {})} for j in jobs], "running": running,
            "note": "This is a real picture being made, and it appears in the conversation by itself. Say so in a few words. You cannot see it: never describe it as if you could, and never say it is finished unless status says complete."}


def image(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    action = str(args.get("action") or "generate").lower()
    s = studio()
    count = args.get("count") if isinstance(args.get("count"), int) else 1
    try:
        if action == "generate":
            jobs = [s.submit(str(args.get("prompt") or ""), str(args.get("size") or ""), conversation=ctx.session, again=i > 0) for i in range(max(1, min(count, 4)))]
        elif action == "variation":
            jobs = s.variation(str(args.get("id") or ""), count)
        elif action == "refine":
            jobs = [s.refine(str(args.get("id") or ""), str(args.get("change") or ""))]
        elif action == "retry":
            jobs = [s.retry(str(args.get("id") or ""))]
        elif action == "cancel":
            jobs = [s.cancel(str(args.get("id") or ""))]
        elif action == "status":
            found = s.get(str(args.get("id") or ""))
            if found is None:
                return ToolResult.fail("images.image", "there is no such picture")
            jobs = [s.public(found)]
        elif action == "list":
            return ToolResult.ok("images.image", "recent pictures", pictures=[{"id": j["id"], "state": j["state"], "prompt": j["prompt"]} for j in s.list(10)])
        else:
            return ToolResult.fail("images.image", f"unknown action {action!r}")
    except StudioError as exc:
        return ToolResult.fail("images.image", str(exc))
    tags = " ".join(CARD_TAG.format(j["id"]) for j in jobs)
    words = {"status": jobs[0]["state"], "cancel": "Cancelled."}.get(action, "A real picture is being made; it will appear in the conversation.")
    return ToolResult.ok("images.image", f"{words} {tags}".strip(), **_about(jobs))


_S, _I = {"type": "string"}, {"type": "integer"}
_TOOLS = [
    Tool("images.image",
         "Make real pictures with Stable Diffusion XL: 'generate an image of...', 'create a wallpaper...', 'illustrate this concept', 'make three variations'. action: generate (prompt: what to show, in the user's words with any detail they gave; size: "
         "square, wide, tall, landscape or portrait, optional; count up to 4) | variation (id, count: new pictures of the same idea; the original stays) | refine (id, change: what to change; the original stays) | retry (id) | cancel (id) | status (id) | list. "
         "Call it right away instead of asking for details, unless something essential is missing. It starts a job and returns: the picture appears in the conversation by itself. You cannot see pictures: a description in text is never a picture. "
         "For diagrams and flowcharts use xenio_teach show instead: Stable Diffusion cannot spell, so it is for pictures, not labelled diagrams.",
         {"type": "object", "properties": {"action": _S, "prompt": _S, "size": _S, "count": _I, "id": _S, "change": _S}, "required": ["action"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_image", image),
]


def xenio_tools() -> list[Tool]:
    return list(_TOOLS)
