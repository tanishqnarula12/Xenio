"""Nabh Cloud: one client for what XENIO uses it for besides speech (OpenAI-compatible). Standard library only, because the
xenio-guard plugin imports it inside Hermes.

Measured on this account (2026-11-03), short requests:

    a teacher                                 glm-5-2                a whole lesson in 19 s, the most complete of 8 tried
    checking it                               glm-5-2 again          a fresh reading: found both errors planted in a lesson, in 11 s; the 4 others tried failed
    a quick one for small jobs                mistral-nemo-12b       1.2 s
    embeddings                                qwen3-embedding-4b     English and Hindi of one idea 0.85, another idea 0.46
    reading scans and photos                  paddleocr-vl           2.4 s, exact (qwen2-5-vl-72b: 20 s)
    pictures                                  stable-diffusion-xl    19 s for 1024x1024

Every failure is a ``NabhError`` with a kind the caller can act on and words fit to show. The key is never logged, and
never in a message: a response is cleaned of anything that looks like one before it can reach an error.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Mapping

BASE_URL = "https://api.nabh.cloud/v1"
TEACHER_MODEL = "glm-5-2"
REVIEW_MODEL = "glm-5-2"
FAST_MODEL = "mistral-nemo-12b"
EMBEDDING_MODEL = "qwen3-embedding-4b"
OCR_MODEL = "paddleocr-vl"
IMAGE_MODEL = "stable-diffusion-xl"
_USER_AGENT = "XENIO personal assistant"
_KEYLIKE = re.compile(r"\bnbh_[A-Za-z0-9]{6,}|(?i:bearer\s+)[A-Za-z0-9._~+/=-]{8,}")

_defaults: dict[str, str] = {}


RATE_WAIT_SECONDS = 70.0  # the longest a request waits for Nabh's limit to pass


def _wait_of(headers) -> int | None:
    """How long Nabh wants us to wait: its Retry-After, or else the moment its limit resets (measured: 120 requests a minute, on all kinds of request together)."""
    try:
        wait = int(headers.get("Retry-After") or 0)
        if not wait and headers.get("X-RateLimit-Reset"):
            wait = int(headers["X-RateLimit-Reset"]) - int(time.time()) + 1
    except (TypeError, ValueError):
        return None
    return wait if 0 < wait <= 3600 else None


class _Answer(str):
    """A model's words, and whether its length limit stopped them."""

    cut = False
    usage: dict = {}

    def __new__(cls, text: str, cut: bool = False):
        made = super().__new__(cls, text)
        made.cut = cut
        return made


class NabhError(Exception):
    """``kind``: config | auth | quota | rate_limit | timeout | network | rejected | provider | bad_response"""

    def __init__(self, kind: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


def configure(values: Mapping[str, str]) -> None:
    """In XENIO's own process (values from its settings; inside Hermes they arrive in the environment instead)."""
    _defaults.clear()
    _defaults.update({k: v for k, v in values.items() if v})


def _setting(name: str, environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return str(env.get(name) or _defaults.get(name) or "")


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Nabh:
    def __init__(self, api_key: str, base_url: str = BASE_URL, teacher_model: str = TEACHER_MODEL, fast_model: str = FAST_MODEL,
                 embedding_model: str = EMBEDDING_MODEL, ocr_model: str = OCR_MODEL, image_model: str = IMAGE_MODEL, opener=None, review_model: str = REVIEW_MODEL):
        if not api_key:
            raise NabhError("config", "no Nabh key is set: add NABH_API_KEY to XENIO's .env")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.teacher_model, self.fast_model, self.embedding_model = teacher_model, fast_model, embedding_model
        self.ocr_model, self.image_model, self.review_model = ocr_model, image_model, review_model
        self._open = opener or self._urlopen

    @staticmethod
    def _urlopen(request, timeout):
        return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())

    def post(self, path: str, payload: dict, timeout: float = 60.0) -> dict:
        request = urllib.request.Request(self.base_url + path, data=json.dumps(payload).encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "User-Agent": _USER_AGENT})
        try:
            with self._open(request, timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except TimeoutError:
            raise NabhError("timeout", f"Nabh did not answer in {int(timeout)} s") from None
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise NabhError("timeout", f"Nabh did not answer in {int(timeout)} s") from None
            raise NabhError("network", "Nabh could not be reached: check the internet connection") from None
        except OSError:
            raise NabhError("network", "Nabh could not be reached: check the internet connection") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise NabhError("bad_response", "Nabh sent something that is not JSON") from None
        if not isinstance(data, dict):
            raise NabhError("bad_response", "Nabh sent an answer of an unknown shape")
        return data

    def stream_text(self, payload: dict, timeout: float = 240.0, on_piece=None) -> str:
        """A long answer, as it is written (measured: Nabh ends a request at about 100 s with a 504 unless it is streamed). ``timeout``
        is for the whole answer; a pause between pieces gets at most 90 s."""
        request = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps({**payload, "stream": True}).encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": _USER_AGENT})
        end = time.monotonic() + timeout
        pieces: list[str] = []
        finish = ""
        usage: dict = {}
        try:
            with self._open(request, min(timeout, 90.0)) as response:
                for raw in response:
                    if time.monotonic() > end:
                        raise NabhError("timeout", f"the answer took longer than {int(timeout)} s")
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                        usage = chunk.get("usage") or usage
                        choice = (chunk.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                    except (ValueError, AttributeError, IndexError):
                        continue
                    finish = choice.get("finish_reason") or finish
                    if isinstance(delta.get("content"), str):
                        pieces.append(delta["content"])
                        if on_piece is not None and delta["content"] and on_piece(delta["content"]) is False:  # the user stopped it
                            break
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except TimeoutError:
            raise NabhError("timeout", "Nabh stopped answering part way through") from None
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise NabhError("timeout", "Nabh stopped answering part way through") from None
            raise NabhError("network", "Nabh could not be reached: check the internet connection") from None
        except OSError:
            raise NabhError("network", "the connection to Nabh was lost part way through") from None
        text = "".join(pieces)
        if not text.strip():
            raise NabhError("bad_response", "Nabh's answer was empty")
        answer = _Answer(text, cut=finish == "length")
        answer.usage = usage if isinstance(usage, dict) else {}
        return answer

    def stream_message(self, payload: dict, timeout: float = 240.0, keep_going=None) -> dict:
        """One assistant turn that may call tools, streamed for the same reason as ``stream_text``. ``{"content", "tool_calls", "cut"}``. ``keep_going`` returning False ends it early."""
        request = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps({**payload, "stream": True}).encode("utf-8"), method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": _USER_AGENT})
        end = time.monotonic() + timeout
        pieces: list[str] = []
        calls: dict[int, dict] = {}
        finish = ""
        usage: dict = {}
        try:
            with self._open(request, min(timeout, 90.0)) as response:
                for raw in response:
                    if time.monotonic() > end:
                        raise NabhError("timeout", f"the answer took longer than {int(timeout)} s")
                    if keep_going is not None and keep_going() is False:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                        usage = chunk.get("usage") or usage
                        choice = (chunk.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                    except (ValueError, AttributeError, IndexError):
                        continue
                    finish = choice.get("finish_reason") or finish
                    if isinstance(delta.get("content"), str):
                        pieces.append(delta["content"])
                    for part in delta.get("tool_calls") or []:
                        if not isinstance(part, dict):
                            continue
                        slot = calls.setdefault(int(part.get("index") or 0), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = part.get("id") or slot["id"]
                        function = part.get("function") or {}
                        slot["name"] = function.get("name") or slot["name"]
                        slot["arguments"] += function.get("arguments") or ""
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except TimeoutError:
            raise NabhError("timeout", "Nabh stopped answering part way through") from None
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise NabhError("timeout", "Nabh stopped answering part way through") from None
            raise NabhError("network", "Nabh could not be reached: check the internet connection") from None
        except OSError:
            raise NabhError("network", "the connection to Nabh was lost part way through") from None
        return {"content": "".join(pieces), "tool_calls": [calls[i] for i in sorted(calls)], "cut": finish == "length", "usage": usage if isinstance(usage, dict) else {}}

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> NabhError:
        try:
            body = json.loads(exc.read() or b"{}")
            said = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else body.get("error")
        except (ValueError, OSError, AttributeError):
            said = ""
        said = _KEYLIKE.sub("[hidden]", str(said or ""))[:200]
        code = exc.code
        if code in (401, 403):
            return NabhError("auth", "Nabh refused the key: check NABH_API_KEY")
        if code == 402:
            return NabhError("quota", said or "the Nabh account has no balance left")
        if code == 429:
            wait = _wait_of(exc.headers)
            return NabhError("rate_limit", "Nabh is limiting requests right now" + (f": try again in {wait} s" if wait else ": try again in a moment"), wait)
        if code in (400, 404, 413, 422):
            return NabhError("rejected", said or "Nabh rejected the request")
        return NabhError("provider", f"Nabh had a problem ({code})" + (f": {said}" if said else ""))

    # ---- what it is used for -----------------------------------------------------------------------------------------

    def chat(self, messages: list[dict], model: str = "", max_tokens: int = 1500, temperature: float = 0.4, timeout: float = 90.0) -> str:
        data = self.post("/chat/completions", {"model": model or self.teacher_model, "messages": messages, "max_tokens": max_tokens,
                                               "temperature": temperature}, timeout)
        try:
            text = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise NabhError("bad_response", "Nabh's answer had no message") from None
        if not isinstance(text, str) or not text.strip():
            raise NabhError("bad_response", "Nabh's answer was empty")
        return text

    retry_pause = 1.5

    def chat_json(self, messages: list[dict], model: str = "", max_tokens: int = 4000, temperature: float = 0.4, timeout: float = 240.0, retries: int = 1, budget: float | None = None) -> dict:
        """A JSON object from a model. One more try when what comes back cannot be read as one, or was stopped by the length limit. ``budget``: seconds for all
        the tries together. When they run out, the whole part of a cut-off answer is better than nothing."""
        end = time.monotonic() + budget if budget else None
        last, kept = "", None
        failure: NabhError | None = None
        for attempt in range(retries + 1):
            left = end - time.monotonic() if end else timeout
            if left < 10:
                break
            ask = messages if not last else [*messages, {"role": "assistant", "content": last[:800]},
                                              {"role": "user", "content": "That was not one valid JSON object. Send only the JSON object, nothing else."}]
            try:
                last = self.stream_text({"model": model or self.teacher_model, "messages": ask, "max_tokens": max_tokens, "temperature": temperature}, min(timeout, left))
            except NabhError as exc:  # measured: under load an answer can come back empty, and another try works
                failure = exc
                if attempt >= retries or exc.kind not in ("bad_response", "provider", "rate_limit", "timeout"):
                    break
                pause = self.retry_pause * (attempt + 1)
                if exc.kind == "rate_limit":  # Nabh said how long: a sooner try would only be refused again
                    pause = max(pause, exc.retry_after or 0)
                    if pause > (end - time.monotonic() - 10 if end else RATE_WAIT_SECONDS):
                        break
                time.sleep(pause)
                last = ""
                continue
            failure = None
            parsed = first_json_object(last)
            if getattr(last, "cut", False) and attempt < retries:  # stopped by the length limit: what came is only part of it, so ask with more room
                kept = parsed or kept
                max_tokens, last = int(max_tokens * 1.5), ""
                continue
            if parsed is not None:
                return parsed
        if kept is not None:
            return kept
        raise failure or NabhError("bad_response", "the model's answer could not be read")

    def embed(self, texts: list[str], model: str = "", timeout: float = 60.0) -> list[list[float]]:
        data = self.post("/embeddings", {"model": model or self.embedding_model, "input": texts}, timeout)
        items = data.get("data")
        if not isinstance(items, list) or len(items) != len(texts):
            raise NabhError("bad_response", "Nabh sent the wrong number of embeddings")
        try:
            ordered = sorted(items, key=lambda x: x.get("index", 0))
            return [[float(v) for v in item["embedding"]] for item in ordered]
        except (KeyError, TypeError, ValueError):
            raise NabhError("bad_response", "Nabh's embeddings could not be read") from None

    def read_image(self, image: bytes, mime: str = "image/jpeg", ask: str = "Read every line of text in this image exactly, in reading order.",
                   model: str = "", timeout: float = 60.0) -> str:
        uri = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        return self.chat([{"role": "user", "content": [{"type": "text", "text": ask}, {"type": "image_url", "image_url": {"url": uri}}]}],
                         model or self.ocr_model, 4000, 0.0, timeout)

    def image(self, prompt: str, size: str = "1024x1024", negative: str = "", seed: int | None = None, model: str = "",
              timeout: float = 240.0) -> tuple[bytes, dict]:
        """A real picture: its bytes, and what Nabh said about it. A text answer is never a picture."""
        payload: dict = {"model": model or self.image_model, "prompt": prompt, "n": 1, "size": size}
        if negative:
            payload["negative_prompt"] = negative
        if seed is not None:
            payload["seed"] = int(seed)
        data = self.post("/images/generations", payload, timeout)
        items = data.get("data")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise NabhError("bad_response", "Nabh answered without a picture")
        item = items[0]
        raw = None
        if isinstance(item.get("b64_json"), str):
            try:
                raw = base64.b64decode(item["b64_json"], validate=False)
            except (ValueError, TypeError):
                raise NabhError("bad_response", "Nabh's picture could not be decoded") from None
        elif isinstance(item.get("url"), str) and item["url"].lower().startswith("https://"):
            try:
                with self._open(urllib.request.Request(item["url"], headers={"User-Agent": _USER_AGENT}), timeout) as response:
                    raw = response.read()
            except (urllib.error.URLError, OSError):
                raise NabhError("network", "Nabh's picture could not be downloaded") from None
        if not raw or image_kind(raw) is None:
            raise NabhError("bad_response", "Nabh's answer was not a picture")
        return raw, {"size": data.get("size") or size, "cost_inr": data.get("cost_inr"), "seed": item.get("seed") or data.get("seed") or seed}


def image_kind(raw: bytes) -> str | None:
    """What these bytes really are, from their first bytes."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def first_json_object(text: str) -> dict | None:
    """The JSON object in a model's answer, with or without a fence and words around it."""
    body = re.sub(r"(?s)<think>.*?</think>", " ", str(text or ""))
    fence = re.search(r"(?s)```(?:json)?\s*(\{.*?\})\s*```", body)
    candidates = [fence.group(1)] if fence else []
    start = body.find("{")
    if start >= 0:
        depth, in_string, escaped = 0, False, False
        for i in range(start, len(body)):
            ch = body[i]
            if in_string:
                escaped = ch == "\\" and not escaped
                if ch == '"' and not escaped and body[i - 1] != "\\":
                    in_string = False
                if ch != "\\":
                    escaped = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(body[start:i + 1])
                    break
    for raw in candidates:
        for attempt in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):  # a trailing comma is the commonest slip
            try:
                parsed = json.loads(attempt)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return _whole_part_of(body[start:]) if start >= 0 else None


def _whole_part_of(text: str) -> dict | None:
    """An answer cut off by the length limit (measured: a long lesson part): everything complete in it, closed where it stopped."""
    stack: list[str] = []
    in_string, escaped = False, False
    best: tuple[int, list[str]] | None = None
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if stack:
                best = (i + 1, list(stack))
    if best is None:
        return None
    end, still_open = best
    try:
        parsed = json.loads(text[:end] + "".join(reversed(still_open)))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def client(environ: Mapping[str, str] | None = None) -> Nabh | None:
    """The configured client; None when no key is set."""
    key = _setting("XENIO_NABH_API_KEY", environ)
    if not key:
        return None
    return Nabh(key, _setting("XENIO_NABH_BASE_URL", environ) or BASE_URL, _setting("XENIO_NABH_TEACHER_MODEL", environ) or TEACHER_MODEL,
                _setting("XENIO_NABH_FAST_MODEL", environ) or FAST_MODEL, _setting("XENIO_NABH_EMBEDDING_MODEL", environ) or EMBEDDING_MODEL,
                _setting("XENIO_NABH_OCR_MODEL", environ) or OCR_MODEL, _setting("XENIO_NABH_IMAGE_MODEL", environ) or IMAGE_MODEL,
                review_model=_setting("XENIO_NABH_REVIEW_MODEL", environ) or REVIEW_MODEL)
