"""Pictures as jobs (V4 phase 4). Each request is a job with an id, its own folder and its conversation, so a result always finds the right place
and survives a restart. Jobs run in the background: the chat stays free while one is generating.

State is one small file per job, read and written by whichever process is involved (XENIO itself, or its tools inside Hermes). The state says
only what really happened: preparing -> sending -> generating -> receiving -> complete | failed | cancelled. Nabh reports no progress while it works,
so there is no percentage anywhere: a percentage would be made up.

Cancelling: Nabh cannot be told to stop, so a cancelled job's picture is thrown away when it arrives, and the job says cancelled at once.
A second identical request while one is running, or moments after, is the same job: a double click makes one picture.

Shape: a provider makes only certain sizes (Nabh's SDXL: square). Another shape is cut from the middle of what it made, with the original kept beside it,
and the job says the true size.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from app.images.providers import SETUP_HELP, SIZES, Generated, ImageError, configured
from app.integrations import nabh as nabh_module
from app.integrations.base import data_dir
from app.integrations.nabh import NabhError, image_kind

STATES = ("preparing", "sending", "generating", "receiving", "complete", "failed", "cancelled")
ACTIVE = ("preparing", "sending", "generating", "receiving")
SAME_REQUEST_SECONDS = 20
MAX_PROMPT = 1500
MAX_PARALLEL = 2
REFINE_SECONDS = 8.0  # the person's own words go if the quick model is slower than this: the picture matters more than the polish
ABANDONED_SECONDS = 420  # a job still "running" after this belonged to a process that has gone
CARD_TAG = "[xenio-card image {}]"
_ID = re.compile(r"^img_[0-9a-f]{16}$")
_REFINE = ("You turn a person's request into a prompt for Stable Diffusion XL. Keep exactly what they asked for: subject, style, mood and any words they gave; never add a different subject or a person they did not ask for. "
           "Add what makes SDXL look good: concrete visual details, composition, lighting, medium or lens, and quality words. A diagram or explanation request becomes a clean, labelled-free illustration, since SDXL cannot spell. "
           "Write it in English whatever language they used. At most 60 words. Reply with the prompt only.")
_BLOCKED = re.compile(r"\b(child|children|kid|kids|minor|minors|teen|teens|teenager|underage|schoolgirl|schoolboy|loli|shota|\d{1,2}[- ]?(?:year|yr)[- ]?old)\b[^.]{0,80}\b(nude|naked|sex|sexual|porn|erotic|nsfw|lingerie|undress)"
                      r"|\b(nude|naked|sex|sexual|porn|erotic|nsfw|lingerie|undress)\b[^.]{0,80}\b(child|children|kid|kids|minor|minors|teen|teens|teenager|underage|schoolgirl|schoolboy|loli|shota|\d{1,2}[- ]?(?:year|yr)[- ]?old)\b", re.IGNORECASE)


class StudioError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def refused_prompt(prompt: str) -> str:
    """What XENIO will not make, whoever asks."""
    if _BLOCKED.search(prompt or ""):
        return "XENIO does not make sexual images of children."
    return ""


def size_for(words: str, asked: str = "") -> str:
    if asked in SIZES.values():
        return asked
    if asked in SIZES:
        return SIZES[asked]
    low = (words or "").lower()
    if re.search(r"\b(phone wallpaper|portrait|poster|story|vertical|9:16|mobile wallpaper|phone background)\b", low):
        return SIZES["tall"]
    if re.search(r"\b(wallpaper|desktop background|banner|landscape|cinematic|widescreen|16:9|header)\b", low):
        return SIZES["wide"]
    return SIZES["square"]


def _crop(source: Path, target: Path, size: str) -> bool:
    """The middle of a picture, by macOS itself (sips): no new dependency, and the source stays as it is."""
    width, height = size.split("x")
    try:
        done = subprocess.run(["/usr/bin/sips", "--cropToHeightWidth", height, width, str(source), "--out", str(target)], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and target.exists() and target.stat().st_size > 0


def _framing(size: str) -> str:
    """Tells the prompt writer what will be cut away, so the subject is not."""
    width, height = (int(n) for n in size.split("x"))
    if width > height:
        return " (Frame: it will be cropped to a wide shape, so keep the main subject in the middle band.)"
    if height > width:
        return " (Frame: it will be cropped to a tall shape, so keep the main subject in the middle column.)"
    return ""


class ImageStudio:
    def __init__(self, folder: Path | None = None, provider_factory=None, refiner=None, notify=None, clock=time.time, background: bool = True):
        self.folder = Path(folder) if folder else data_dir() / "images"
        self._provider = provider_factory or configured
        self._refiner = refiner
        self.notify = notify
        self._clock = clock
        self._background = background
        self._slots = threading.BoundedSemaphore(MAX_PARALLEL)
        self._lock = threading.Lock()

    # ---- the record of a job ---------------------------------------------------------------------------------------------

    def _home(self, job_id: str) -> Path:
        if not _ID.match(str(job_id or "")):
            raise StudioError("no such picture", 404)
        return self.folder / job_id

    def _write(self, job: dict) -> None:
        home = self._home(job["id"])
        home.mkdir(parents=True, exist_ok=True)
        tmp = home / "job.tmp"
        tmp.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, home / "job.json")
        if self.notify is not None:
            try:
                self.notify(self.public(job))
            except Exception:  # noqa: BLE001 - a screen not listening is no reason to fail a picture
                pass

    def get(self, job_id: str) -> dict | None:
        try:
            job = json.loads((self._home(job_id) / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if job["state"] in ACTIVE and self._cancelled(job_id):  # asked to stop, even if another step wrote at that very moment
            job.update(state="cancelled")
        if job["state"] in ACTIVE and self._clock() - job.get("updated", 0) > ABANDONED_SECONDS:
            job.update(state="failed", error={"kind": "lost", "message": "XENIO stopped while this was generating. Try again."})
        return job

    @staticmethod
    def public(job: dict) -> dict:
        """What the page and the model may see. Never a key, and never the file's place on disk."""
        return {k: job.get(k) for k in ("id", "state", "prompt", "size", "kind", "parent", "conversation", "turn", "seed", "model", "error", "created", "updated", "ms", "refined", "group", "saved", "cropped_from")} | {"has_image": job.get("state") == "complete"}

    def list(self, limit: int = 40, conversation: str = "") -> list[dict]:
        if not self.folder.is_dir():
            return []
        found = []
        for home in sorted(self.folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit * 3]:
            job = self.get(home.name) if _ID.match(home.name) else None
            if job and (not conversation or job.get("conversation") == conversation):
                found.append(self.public(job))
        return found[:limit]

    def file(self, job_id: str) -> tuple[bytes, str] | None:
        job = self.get(job_id)
        if not job or job["state"] != "complete":
            return None
        path = self._home(job_id) / job["file"]
        try:
            return path.read_bytes(), f"image/{image_kind(path.read_bytes()[:16]) or 'png'}"
        except OSError:
            return None

    # ---- making pictures -------------------------------------------------------------------------------------------------

    def submit(self, prompt: str, size: str = "", conversation: str = "", turn: str = "", kind: str = "new", parent: str = "", seed: int | None = None, group: str = "", prepared: str = "", refine: bool = True, again: bool = False) -> dict:
        """A job, started at once, or the same one if this request has just been made. Raises only for what can never work."""
        text = " ".join(str(prompt or "").split())[:MAX_PROMPT]
        if not text:
            raise StudioError("describe the picture first")
        reason = refused_prompt(text)
        if reason:
            raise StudioError(reason, 403)
        wanted = size_for(text, size)
        fingerprint = hashlib.sha256(f"{text.lower()}|{wanted}|{kind}|{parent}|{seed}|{group}".encode()).hexdigest()[:24]
        with self._lock:
            if not again:
                for other in self.list(12):
                    full = self.get(other["id"])
                    if full and full.get("fingerprint") == fingerprint and full["state"] != "failed" and full["state"] != "cancelled" and (full["state"] in ACTIVE or self._clock() - full.get("created", 0) < SAME_REQUEST_SECONDS):
                        return {**self.public(full), "same_request": True}
            job = {"id": "img_" + secrets.token_hex(8), "state": "preparing", "prompt": text, "sent_prompt": prepared or text, "prepared": bool(prepared), "size": wanted, "kind": kind, "parent": parent, "conversation": conversation, "turn": turn, "group": group,
                   "seed": seed if seed is not None else random.randint(1, 2_147_483_000), "fingerprint": fingerprint, "created": self._clock(), "updated": self._clock(), "error": None, "refine": refine}
            self._write(job)
        if self._background:
            threading.Thread(target=self._run, args=(job["id"],), name="xenio-image", daemon=True).start()
        else:
            self._run(job["id"])
        return self.public(job)

    def variation(self, job_id: str, count: int = 1, conversation: str = "") -> list[dict]:
        """New pictures of the same idea. The original stays as it is."""
        source = self.get(job_id)
        if source is None:
            raise StudioError("that picture is not there any more", 404)
        count = max(1, min(int(count or 1), 4))
        group = "grp_" + secrets.token_hex(4)
        return [self.submit(source["prompt"], source["size"], conversation or source.get("conversation", ""), kind="variation", parent=job_id, group=group, prepared=source.get("sent_prompt", ""), again=True) for _ in range(count)]

    def refine(self, job_id: str, change: str, conversation: str = "") -> dict:
        """The same idea with the learner's change. The original stays as it is."""
        source = self.get(job_id)
        if source is None:
            raise StudioError("that picture is not there any more", 404)
        words = " ".join(str(change or "").split())
        if not words:
            raise StudioError("say what to change")
        return self.submit(f"{source['prompt']}. {words}", source["size"], conversation or source.get("conversation", ""), kind="refined", parent=job_id, seed=source.get("seed"), again=True)

    def regenerate(self, job_id: str) -> dict:
        """Another try at the same thing, from the same words. The original stays as it is."""
        source = self.get(job_id)
        if source is None:
            raise StudioError("that picture is not there any more", 404)
        return self.submit(source["prompt"], source["size"], source.get("conversation", ""), kind="regenerated", parent=job_id, prepared=source.get("sent_prompt", ""), again=True)

    def retry(self, job_id: str) -> dict:
        source = self.get(job_id)
        if source is None:
            raise StudioError("that picture is not there any more", 404)
        if source["state"] in ACTIVE:
            return self.public(source)
        return self.submit(source["prompt"], source["size"], source.get("conversation", ""), source.get("turn", ""), source.get("kind", "new"), source.get("parent", ""), prepared=source.get("sent_prompt", "") if source.get("refined") or source.get("prepared") else "", again=True)

    def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job is None:
            raise StudioError("that picture is not there any more", 404)
        if job["state"] in ACTIVE:
            (self._home(job_id) / "cancel").write_text("1", encoding="utf-8")
            job.update(state="cancelled", updated=self._clock())
            self._write(job)
        return self.public(job)

    def _cancelled(self, job_id: str) -> bool:
        return (self._home(job_id) / "cancel").exists()

    def _step(self, job: dict, state: str, **more) -> bool:
        """Move on, unless it was cancelled meanwhile: a cancelled job stays cancelled."""
        if self._cancelled(job["id"]):
            return False
        job.update(state=state, updated=self._clock(), **more)
        self._write(job)
        return True

    def _prepare_prompt(self, job: dict) -> str:
        """What SDXL is really sent: the person's request made vivid by a quick model. If that fails, their own words go, and nothing is lost."""
        if job.get("prepared"):
            return job["sent_prompt"]
        if self._refiner is not None:
            return self._refiner(job["prompt"]) or job["prompt"]
        if not job.get("refine", True):
            return job["prompt"]
        try:
            client = nabh_module.client()
            if client is None:
                return job["prompt"]
            better = client.chat([{"role": "system", "content": _REFINE}, {"role": "user", "content": job["prompt"] + _framing(job["size"])}], client.fast_model, 200, 0.5, REFINE_SECONDS).strip().strip('"')
            return better if 8 <= len(better) <= MAX_PROMPT and not refused_prompt(better) else job["prompt"]
        except NabhError:
            return job["prompt"]

    def _run(self, job_id: str) -> None:
        job = self.get(job_id)
        if job is None:
            return
        started = time.monotonic()
        try:
            provider = self._provider()
        except NabhError:
            provider = None
        if provider is None:
            job.update(state="failed", error={"kind": "config", "message": SETUP_HELP}, updated=self._clock())
            self._write(job)
            return
        sent = self._prepare_prompt(job)
        if not self._step(job, "sending", sent_prompt=sent, refined=sent != job["prompt"]):
            return
        with self._slots:
            if not self._step(job, "generating"):
                return
            try:
                can_make = tuple(getattr(provider, "sizes", ()) or ())
                asked = job["size"] if not can_make or job["size"] in can_make else can_make[0]
                made: Generated = provider.generate(sent, asked, seed=job.get("seed"))
            except ImageError as exc:
                if not self._cancelled(job_id):
                    self._step(job, "failed", error={"kind": exc.kind, "message": str(exc), "retry_after": exc.retry_after}, ms=int((time.monotonic() - started) * 1000))
                return
            except Exception as exc:  # noqa: BLE001 - a bug here must look like a failure, never like a picture
                if not self._cancelled(job_id):
                    self._step(job, "failed", error={"kind": "provider", "message": f"The picture could not be made ({type(exc).__name__})."}, ms=int((time.monotonic() - started) * 1000))
                return
        if not self._step(job, "receiving"):
            return  # a cancelled job's picture is thrown away
        kind = image_kind(made.data)
        if kind is None:
            self._step(job, "failed", error={"kind": "bad_response", "message": "What came back was not a picture."})
            return
        name = f"image.{ 'jpg' if kind == 'jpeg' else kind}"
        home = self._home(job_id)
        more: dict = {}
        (home / name).write_bytes(made.data)
        if asked != job["size"]:
            original = f"original{Path(name).suffix}"
            os.replace(home / name, home / original)
            if _crop(home / original, home / name, job["size"]):
                more = {"original": original, "cropped_from": asked}
            else:  # the whole picture, and the size it really is
                shutil.copyfile(home / original, home / name)
                more = {"original": original, "size": asked}
        if not self._step(job, "complete", file=name, model=made.model, seed=made.seed if made.seed is not None else job.get("seed"), ms=int((time.monotonic() - started) * 1000), **more):
            for path in (home / name, home / more.get("original", name)):
                path.unlink(missing_ok=True)  # cancelled at the last moment: thrown away, as promised

    # ---- keeping one -----------------------------------------------------------------------------------------------------

    def save(self, job_id: str, folder: Path | None = None) -> dict:
        """Only when the user asks. Never over an existing file, and the original stays in the studio."""
        job = self.get(job_id)
        if job is None or job["state"] != "complete":
            raise StudioError("there is no finished picture to save", 409)
        target_dir = Path(folder) if folder else Path.home() / "Downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^\w -]+", "", job["prompt"])[:50].strip().replace(" ", "-") or "xenio-image"
        suffix = Path(job["file"]).suffix
        target = target_dir / f"{stem}{suffix}"
        count = 1
        while target.exists():
            count += 1
            target = target_dir / f"{stem}-{count}{suffix}"
        shutil.copyfile(self._home(job_id) / job["file"], target)
        job["saved"] = str(target)
        self._write(job)
        return {"saved": str(target)}
