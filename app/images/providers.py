"""Who makes the picture. One small interface, so the provider can change without touching anything else: the studio only ever sees ``generate``.
Today: Nabh's Stable Diffusion XL (the user's choice). Standard library only, with the key staying on this side: the page never sees it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.integrations import nabh as nabh_module
from app.integrations.nabh import NabhError

# The shapes a picture can come in. A provider says which it can really make (`sizes`); any other shape is cut from what it made, and the original is kept.
SIZES = {"square": "1024x1024", "wide": "1024x576", "tall": "576x1024", "landscape": "1024x682", "portrait": "682x1024"}
DEFAULT_NEGATIVE = "blurry, low quality, deformed, distorted, disfigured, bad anatomy, extra limbs, watermark, signature, jpeg artifacts, ugly text"


class ImageError(Exception):
    """``kind``: config | auth | quota | rate_limit | timeout | network | refused | provider | bad_response"""

    def __init__(self, kind: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


@dataclass
class Generated:
    data: bytes
    seed: int | None = None
    model: str = ""
    cost: str | None = None


class ImageProvider(Protocol):
    name: str
    sizes: tuple[str, ...]  # what it can really make, the most useful first

    def generate(self, prompt: str, size: str, negative: str = "", seed: int | None = None) -> Generated: ...


class NabhImages:
    name = "nabh"
    sizes = ("1024x1024",)  # measured 2026-11-03: Nabh's SDXL refuses every other size, and silently ignores width, height and aspect_ratio

    def __init__(self, client: nabh_module.Nabh):
        self.client = client

    def generate(self, prompt: str, size: str, negative: str = "", seed: int | None = None) -> Generated:
        try:
            data, meta = self.client.image(prompt, size, negative or DEFAULT_NEGATIVE, seed)
        except NabhError as exc:
            kind = {"rejected": "refused"}.get(exc.kind, exc.kind)
            raise ImageError(kind, str(exc), exc.retry_after) from None
        return Generated(data, meta.get("seed"), self.client.image_model, None if meta.get("cost_inr") is None else str(meta["cost_inr"]))


def configured() -> ImageProvider | None:
    """The provider this XENIO is set up with, or None (the setup message then says what to set)."""
    client = nabh_module.client()
    return NabhImages(client) if client is not None else None


SETUP_HELP = "Image generation is not set up: add NABH_API_KEY to XENIO's .env (see .env.example), then restart XENIO."
