"""Vision providers (V5): who looks at the screenshot. The rest of XENIO only knows ``VisionProvider``.

``HermesVisionProvider`` runs inside Hermes' process (the xenio-guard plugin) and uses Hermes' own model
access, so there is no new LLM, no new account and no new key:

* ``main`` (default): exactly the user's main Hermes model (here gpt-6-luna through the ChatGPT sign-in,
  image input supported). Strict: if that provider cannot be used right now, XENIO says so instead of
  letting Hermes fall back to another provider. Measured 2026-10-04: Hermes' own vision fallback silently
  sent a test image to OpenRouter (pay-per-use) when the Codex credential was not selectable, so XENIO
  never uses that chain unless the user chooses ``auto``.
* ``auto``: Hermes' auxiliary vision routing (``auxiliary.vision`` in Hermes' config). It may use OpenRouter,
  Nous or other providers, which can cost money. Only when the user sets ``XENIO_VISION_PROVIDER=auto``.

``OpenAICompatibleVisionProvider`` is optional: any /chat/completions endpoint with image input
(``XENIO_VISION_API_URL`` / ``XENIO_VISION_MODEL`` / ``XENIO_VISION_API_KEY``). Standard library only.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.vision.capture import CapturedImage


class VisionError(Exception):
    pass


@dataclass
class VisionAnswer:
    text: str
    provider: str
    model: str
    ms: int


class VisionProvider(ABC):
    name = "vision"

    @abstractmethod
    def analyze(self, image: CapturedImage, prompt: str, timeout: float = 90.0) -> VisionAnswer:
        """What the model sees in ``image``, answering ``prompt``. Raises VisionError."""

    async def analyze_image(self, image: CapturedImage, prompt: str, timeout: float = 90.0) -> VisionAnswer:
        return await asyncio.to_thread(self.analyze, image, prompt, timeout)

    def describe(self) -> str:
        return self.name


def _messages(image: CapturedImage, prompt: str) -> list[dict]:
    return [{"role": "user", "content": [{"type": "text", "text": prompt},
                                         {"type": "image_url", "image_url": {"url": image.data_url()}}]}]


class HermesVisionProvider(VisionProvider):
    """Hermes' model access, from inside the Hermes process (the plugin)."""

    def __init__(self, provider: str = "main", model: str = ""):
        self.provider = (provider or "main").strip().lower()
        self.model = (model or "").strip()
        self.name = f"hermes:{self.provider}"

    def target(self) -> tuple[str, str]:
        """(provider, model) the request goes to."""
        if self.provider in ("main", ""):
            try:
                from agent.auxiliary_client import _read_main_model, _read_main_provider
            except ImportError as exc:
                raise VisionError(f"Hermes' model access is not available here ({exc})") from None
            return str(_read_main_provider() or ""), self.model or str(_read_main_model() or "")
        return self.provider, self.model

    def describe(self) -> str:
        try:
            provider, model = self.target()
            return f"{provider}/{model}" if model else provider
        except VisionError:
            return self.name

    def analyze(self, image: CapturedImage, prompt: str, timeout: float = 90.0) -> VisionAnswer:
        try:
            from agent.auxiliary_client import call_llm, extract_content_or_reasoning, resolve_vision_provider_client
        except ImportError as exc:
            raise VisionError(f"Hermes' model access is not available here ({exc})") from None
        started = time.monotonic()
        if self.provider == "auto":
            route: dict = {}
            try:
                response = call_llm(task="vision", messages=_messages(image, prompt), timeout=timeout, route_info=route)
            except Exception as exc:  # noqa: BLE001 - any provider failure is reported, never hidden
                raise VisionError(_failure(exc)) from None
            provider, model = route.get("provider", "auto"), route.get("model", "")
        else:
            provider, model = self.target()
            if not provider or provider in ("auto", "moa"):
                raise VisionError("Hermes has no single main model to look with; set XENIO_VISION_PROVIDER.")
            try:
                resolved, client, final_model = resolve_vision_provider_client(provider=provider, model=model or None)
            except Exception as exc:  # noqa: BLE001
                raise VisionError(f"{provider} could not be prepared for vision ({_failure(exc)})") from None
            if client is None or str(resolved or "").lower() != provider.lower():
                raise VisionError(
                    f"{provider} cannot look at images right now (its sign-in is not usable for this request). XENIO "
                    "does not switch to another, possibly paid, provider on its own. Fix: run `hermes auth reset "
                    f"{provider}` (a cooldown Hermes put on another model blocks it here) or `hermes auth status "
                    f"{provider}`; or choose another vision provider with XENIO_VISION_PROVIDER.")
            try:  # the resolved client directly: no fallback chain behind this call
                response = client.chat.completions.create(model=final_model or model, messages=_messages(image, prompt),
                                                          timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                raise VisionError(_failure(exc)) from None
            model = final_model or model
        text = (extract_content_or_reasoning(response) or "").strip()
        if not text:
            raise VisionError("the vision model returned no description")
        return VisionAnswer(text, str(provider), str(model), int((time.monotonic() - started) * 1000))


class OpenAICompatibleVisionProvider(VisionProvider):
    """Any OpenAI-compatible /chat/completions endpoint with image input (opt-in)."""

    def __init__(self, api_url: str, model: str, api_key: str = "", opener=urllib.request.urlopen):
        if not api_url or not model:
            raise VisionError("XENIO_VISION_API_URL and XENIO_VISION_MODEL are required for this vision provider")
        self.api_url, self.model, self._key, self._open = api_url, model, api_key, opener
        self.name = "openai-compatible"

    def describe(self) -> str:
        from urllib.parse import urlsplit

        return f"{urlsplit(self.api_url).netloc}/{self.model}"

    def analyze(self, image: CapturedImage, prompt: str, timeout: float = 90.0) -> VisionAnswer:
        body = json.dumps({"model": self.model, "messages": _messages(image, prompt), "temperature": 0.1}).encode()
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self._key}"} if self._key else {})}
        started = time.monotonic()
        try:
            with self._open(urllib.request.Request(self.api_url, body, headers), timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            hint = " (check XENIO_VISION_API_KEY)" if exc.code in (401, 403) else ""
            raise VisionError(f"the vision service refused the request (HTTP {exc.code}){hint}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise VisionError(f"could not reach the vision service ({getattr(exc, 'reason', exc)})") from None
        except ValueError:
            raise VisionError("the vision service sent an unreadable answer") from None
        try:
            text = str(payload["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            raise VisionError("the vision service's answer has no text") from None
        if not text:
            raise VisionError("the vision model returned no description")
        return VisionAnswer(text, self.name, self.model, int((time.monotonic() - started) * 1000))


def _failure(exc: Exception) -> str:
    """A short, key-free reason for a provider error."""
    text = " ".join(str(exc).split())[:240] or type(exc).__name__
    lowered = text.lower()
    if any(hint in lowered for hint in ("402", "insufficient", "credits", "billing")):
        return "the provider refused for billing reasons (no credit)"
    if any(hint in lowered for hint in ("timed out", "timeout")):
        return "the vision request timed out"
    if any(hint in lowered for hint in ("does not support", "image input", "multimodal")):
        return "this model does not accept images"
    try:
        from app.safety.policy import redact_secrets

        return redact_secrets(text)
    except ImportError:  # pragma: no cover
        return type(exc).__name__


def create_provider(environ: dict[str, str] | None = None) -> VisionProvider:
    """The configured provider (XENIO_VISION_PROVIDER: main | auto | openai-compatible)."""
    env = os.environ if environ is None else environ
    kind = (env.get("XENIO_VISION_PROVIDER") or "main").strip().lower()
    model = (env.get("XENIO_VISION_MODEL") or "").strip()
    if kind in ("openai-compatible", "openai_compatible", "custom"):
        return OpenAICompatibleVisionProvider(env.get("XENIO_VISION_API_URL", "").strip(), model,
                                              env.get("XENIO_VISION_API_KEY", "").strip())
    return HermesVisionProvider(kind, model)
