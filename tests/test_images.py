"""Image Studio (V4 phase 4): real states, real errors, no picture unless a picture came back. A fake provider stands in for Nabh here; the real run is
recorded in docs/IMAGE_STUDIO.md."""

import asyncio
import json
import logging
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

import pytest

from app.images import studio as studio_module
from app.images.providers import DEFAULT_NEGATIVE, SETUP_HELP, SIZES, Generated, ImageError, NabhImages
from app.images.studio import ACTIVE, ImageStudio, StudioError, refused_prompt, size_for
from app.integrations.nabh import Nabh, NabhError, image_kind
from app.language.detect import parse_image_command
from app.safety import policy as p
from app.safety.policy import RiskLevel
from app.tools import images as image_tool
from app.tools.base import ToolContext

PATHS = p.default_path_policy()
mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


def png(width: int = 8, height: int = 8) -> bytes:
    """A real PNG, of a real size."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    row = b"\x00" + b"\x40\x80\xc0" * width
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")


def size_of(raw: bytes) -> tuple[int, int]:
    return struct.unpack(">II", raw[16:24])


class Provider:
    name = "fake"

    def __init__(self, data=None, error=None, gate=None, sizes=()):
        self.data, self.error, self.gate, self.sizes = data if data is not None else png(), error, gate, sizes
        self.calls = []
        self.running = self.most = 0
        self._lock = threading.Lock()

    def generate(self, prompt, size, negative="", seed=None):
        self.calls.append({"prompt": prompt, "size": size, "seed": seed})
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
        try:
            if self.gate is not None:
                assert self.gate.wait(5)
            if self.error is not None:
                raise self.error
            return Generated(self.data, seed, "fake-model", "0")
        finally:
            with self._lock:
                self.running -= 1


def make(tmp_path, provider=None, background=False, refiner=None, clock=time.time, notify=None):
    made = provider if provider is not None else Provider()
    return ImageStudio(tmp_path / "images", provider_factory=lambda: made, refiner=refiner or (lambda words: f"vivid {words}"), background=background, clock=clock, notify=notify), made


def wait_until(check, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


class TestAPictureIsMade:
    def test_every_real_state_in_order_and_the_picture_is_kept(self, tmp_path):
        seen = []
        studio, provider = make(tmp_path, notify=lambda job: seen.append(job["state"]))
        job = studio.submit("a lighthouse", conversation="c1", turn="t1")
        done = studio.get(job["id"])
        assert seen == ["preparing", "sending", "generating", "receiving", "complete"]
        assert done["state"] == "complete" and done["conversation"] == "c1" and done["turn"] == "t1" and done["model"] == "fake-model"
        assert provider.calls[0]["prompt"] == "vivid a lighthouse" and done["refined"] is True
        data, kind = studio.file(job["id"])
        assert data == provider.data and kind == "image/png"

    def test_what_the_page_sees_has_no_place_on_disk_and_no_percentage(self, tmp_path):
        studio, _ = make(tmp_path)
        public = studio.public(studio.get(studio.submit("x y")["id"]))
        assert "file" not in public and "sent_prompt" not in public and "fingerprint" not in public
        assert public["has_image"] is True
        assert not any("percent" in k or "progress" in k for k in public)

    def test_a_job_survives_a_restart(self, tmp_path):
        studio, provider = make(tmp_path)
        job = studio.submit("a lighthouse")
        later = ImageStudio(tmp_path / "images", provider_factory=lambda: provider, background=False)
        assert later.get(job["id"])["state"] == "complete" and later.file(job["id"])[0] == provider.data and later.list()[0]["id"] == job["id"]

    def test_only_two_are_generated_at_once_and_the_chat_is_free_meanwhile(self, tmp_path):
        gate = threading.Event()
        studio, provider = make(tmp_path, Provider(gate=gate), background=True)
        started = time.monotonic()
        jobs = [studio.submit(f"picture {i}") for i in range(4)]
        assert time.monotonic() - started < 1.0  # it did not wait for any picture
        assert wait_until(lambda: provider.running == 2)
        time.sleep(0.05)
        assert provider.most == 2
        gate.set()
        assert wait_until(lambda: all(studio.get(j["id"])["state"] == "complete" for j in jobs))

    def test_a_refiner_that_fails_costs_nothing(self, tmp_path):
        from app.integrations import nabh

        class Slow:
            fast_model = "m"

            def chat(self, *a, **k):
                raise NabhError("timeout", "too slow")

        studio = ImageStudio(tmp_path / "i", provider_factory=lambda: provider, background=False)
        provider = Provider()
        original = nabh.client
        nabh.client = lambda environ=None: Slow()
        try:
            job = studio.submit("a red fox in snow")
        finally:
            nabh.client = original
        assert provider.calls[0]["prompt"] == "a red fox in snow" and studio.get(job["id"])["state"] == "complete" and not studio.get(job["id"])["refined"]


class TestWhenItFails:
    @pytest.mark.parametrize("kind", ["rate_limit", "quota", "auth", "timeout", "network", "refused", "provider"])
    def test_a_provider_error_is_the_jobs_real_state(self, tmp_path, kind):
        studio, _ = make(tmp_path, Provider(error=ImageError(kind, f"nabh said {kind}", 30 if kind == "rate_limit" else None)))
        job = studio.get(studio.submit("x y")["id"])
        assert job["state"] == "failed" and job["error"]["kind"] == kind and kind in job["error"]["message"]
        assert studio.file(job["id"]) is None and job.get("file") is None
        if kind == "rate_limit":
            assert job["error"]["retry_after"] == 30

    @pytest.mark.parametrize("not_a_picture", [b"Here is your image: a lighthouse on a cliff", b"", b"<html>error</html>", b'{"error": "x"}'])
    def test_words_are_never_a_picture(self, tmp_path, not_a_picture):
        studio, provider = make(tmp_path, Provider(data=not_a_picture))
        provider.data = not_a_picture
        job = studio.get(studio.submit("x y")["id"])
        assert job["state"] == "failed" and job["error"]["kind"] == "bad_response" and studio.file(job["id"]) is None

    def test_a_bug_looks_like_a_failure_and_says_nothing_inside(self, tmp_path):
        studio, _ = make(tmp_path, Provider(error=ValueError("key=sk-secret-value")))
        job = studio.get(studio.submit("x y")["id"])
        assert job["state"] == "failed" and "sk-secret" not in json.dumps(job)

    def test_no_setup_says_what_to_set_and_no_more(self, tmp_path):
        studio = ImageStudio(tmp_path / "i", provider_factory=lambda: None, background=False)
        job = studio.get(studio.submit("x y")["id"])
        assert job["state"] == "failed" and job["error"] == {"kind": "config", "message": SETUP_HELP} and "NABH_API_KEY" in SETUP_HELP

    def test_a_job_whose_process_went_away_does_not_spin_forever(self, tmp_path):
        now = [1000.0]
        studio, _ = make(tmp_path, Provider(gate=threading.Event()), background=True, clock=lambda: now[0])
        job = studio.submit("x y")
        now[0] += studio_module.ABANDONED_SECONDS + 1
        assert studio.get(job["id"])["state"] == "failed" and studio.get(job["id"])["error"]["kind"] == "lost"

    @pytest.mark.parametrize("bad", ["", "  ", None])
    def test_nothing_to_draw_is_refused(self, tmp_path, bad):
        studio, provider = make(tmp_path)
        with pytest.raises(StudioError):
            studio.submit(bad)
        assert provider.calls == []

    @pytest.mark.parametrize("bad", ["../../etc/passwd", "img_123", "img_" + "z" * 16, "", None])
    def test_an_id_cannot_reach_outside_the_folder(self, tmp_path, bad):
        studio, _ = make(tmp_path)
        with pytest.raises(StudioError):
            studio.get(bad)

    def test_it_retries_a_failed_job_as_a_new_one(self, tmp_path):
        provider = Provider(error=ImageError("timeout", "slow"))
        studio, _ = make(tmp_path, provider)
        failed = studio.submit("a red fox")
        provider.error = None
        again = studio.retry(failed["id"])
        assert again["id"] != failed["id"] and studio.get(again["id"])["state"] == "complete" and studio.get(failed["id"])["state"] == "failed"


class TestCancelling:
    def test_it_says_cancelled_at_once_and_the_late_picture_is_thrown_away(self, tmp_path):
        gate = threading.Event()
        studio, provider = make(tmp_path, Provider(gate=gate), background=True)
        job = studio.submit("x y")
        assert wait_until(lambda: provider.running == 1)
        assert studio.cancel(job["id"])["state"] == "cancelled"
        gate.set()
        assert wait_until(lambda: provider.running == 0)
        time.sleep(0.05)
        now = studio.get(job["id"])
        assert now["state"] == "cancelled" and studio.file(job["id"]) is None
        assert not list((tmp_path / "images" / job["id"]).glob("image.*")) and not list((tmp_path / "images" / job["id"]).glob("original.*"))

    def test_a_step_that_writes_at_the_same_moment_cannot_undo_it(self, tmp_path):
        studio, _ = make(tmp_path)
        job = studio.submit("x y")
        record = tmp_path / "images" / job["id"] / "job.json"
        running = {**json.loads(record.read_text()), "state": "generating"}
        record.write_text(json.dumps(running))
        (tmp_path / "images" / job["id"] / "cancel").write_text("1")
        assert studio.get(job["id"])["state"] == "cancelled"

    def test_cancelling_something_finished_changes_nothing(self, tmp_path):
        studio, provider = make(tmp_path)
        job = studio.submit("x y")
        assert studio.cancel(job["id"])["state"] == "complete" and studio.file(job["id"])[0] == provider.data

    def test_a_cancelled_one_can_be_tried_again(self, tmp_path):
        gate = threading.Event()
        studio, provider = make(tmp_path, Provider(gate=gate), background=True)
        job = studio.submit("x y")
        assert wait_until(lambda: provider.running == 1)
        studio.cancel(job["id"])
        gate.set()
        again = studio.retry(job["id"])
        assert again["id"] != job["id"] and wait_until(lambda: studio.get(again["id"])["state"] == "complete")


class TestDuplicates:
    def test_a_double_click_makes_one_picture(self, tmp_path):
        gate = threading.Event()
        studio, provider = make(tmp_path, Provider(gate=gate), background=True)
        one, two = studio.submit("a lighthouse"), studio.submit("A  LIGHTHOUSE")
        assert two["id"] == one["id"] and two["same_request"] is True
        assert wait_until(lambda: provider.running == 1)
        gate.set()
        assert wait_until(lambda: studio.get(one["id"])["state"] == "complete")
        assert len(provider.calls) == 1

    def test_it_is_still_the_same_moments_after_it_finished(self, tmp_path):
        now = [1000.0]
        studio, provider = make(tmp_path, clock=lambda: now[0])
        one = studio.submit("a lighthouse")
        now[0] += studio_module.SAME_REQUEST_SECONDS - 1
        assert studio.submit("a lighthouse")["id"] == one["id"] and len(provider.calls) == 1

    def test_asking_later_is_a_new_picture(self, tmp_path):
        now = [1000.0]
        studio, provider = make(tmp_path, clock=lambda: now[0])
        one = studio.submit("a lighthouse")
        now[0] += studio_module.SAME_REQUEST_SECONDS + 1
        assert studio.submit("a lighthouse")["id"] != one["id"] and len(provider.calls) == 2

    def test_a_failure_or_a_cancel_does_not_block_asking_again(self, tmp_path):
        provider = Provider(error=ImageError("timeout", "slow"))
        studio, _ = make(tmp_path, provider)
        one = studio.submit("a lighthouse")
        provider.error = None
        assert studio.submit("a lighthouse")["id"] != one["id"]

    def test_a_different_shape_is_a_different_request(self, tmp_path):
        studio, _ = make(tmp_path)
        assert studio.submit("a lighthouse")["id"] != studio.submit("a lighthouse", "tall")["id"]

    def test_three_of_them_on_purpose_are_three(self, tmp_path):
        studio, provider = make(tmp_path)
        ids = {studio.submit("a lighthouse", again=i > 0)["id"] for i in range(3)}
        assert len(ids) == 3 and len(provider.calls) == 3


class TestFromAPicture:
    def test_a_variation_uses_the_same_words_with_a_new_seed_and_the_original_stays(self, tmp_path):
        calls = []
        studio, provider = make(tmp_path, refiner=lambda words: calls.append(words) or f"vivid {words}")
        first = studio.submit("a lighthouse")
        made = studio.get(first["id"])
        kids = studio.variation(first["id"], 3)
        assert len(kids) == 3 and calls == ["a lighthouse"]  # no second rewording: the idea does not drift
        assert {c["prompt"] for c in provider.calls} == {"vivid a lighthouse"}
        assert len({c["seed"] for c in provider.calls}) == 4
        assert all(k["parent"] == first["id"] and k["kind"] == "variation" and k["group"] == kids[0]["group"] for k in kids)
        assert studio.get(first["id"]) == made

    def test_variations_stop_at_four(self, tmp_path):
        studio, _ = make(tmp_path)
        assert len(studio.variation(studio.submit("a b")["id"], 50)) == 4

    def test_refining_keeps_the_seed_adds_the_change_and_the_original_stays(self, tmp_path):
        studio, provider = make(tmp_path)
        first = studio.submit("a lighthouse")
        before = studio.file(first["id"])
        child = studio.refine(first["id"], "at night")
        assert child["parent"] == first["id"] and child["kind"] == "refined" and "a lighthouse. at night" in child["prompt"]
        assert provider.calls[1]["seed"] == provider.calls[0]["seed"]
        assert studio.file(first["id"]) == before

    def test_refining_needs_a_change(self, tmp_path):
        studio, _ = make(tmp_path)
        with pytest.raises(StudioError):
            studio.refine(studio.submit("a b")["id"], "  ")

    def test_regenerating_is_a_new_job_from_the_same_words(self, tmp_path):
        studio, provider = make(tmp_path)
        first = studio.submit("a lighthouse")
        again = studio.regenerate(first["id"])
        assert again["id"] != first["id"] and again["kind"] == "regenerated" and provider.calls[1]["prompt"] == provider.calls[0]["prompt"]
        assert provider.calls[1]["seed"] != provider.calls[0]["seed"]

    @pytest.mark.parametrize("act", [lambda s: s.variation("img_" + "0" * 16), lambda s: s.refine("img_" + "0" * 16, "x"), lambda s: s.regenerate("img_" + "0" * 16), lambda s: s.retry("img_" + "0" * 16), lambda s: s.cancel("img_" + "0" * 16)])
    def test_a_picture_that_is_gone_says_so(self, tmp_path, act):
        studio, _ = make(tmp_path)
        with pytest.raises(StudioError) as err:
            act(studio)
        assert err.value.status == 404


class TestShapes:
    @pytest.mark.parametrize("words,want", [("a futuristic wallpaper of a city", "wide"), ("a phone wallpaper of a forest", "tall"), ("a movie poster for a heist", "tall"), ("a cat", "square"), ("a banner for my site", "wide")])
    def test_the_shape_comes_from_the_words(self, words, want):
        assert size_for(words) == SIZES[want]

    def test_a_named_shape_wins(self):
        assert size_for("a wallpaper", "tall") == SIZES["tall"] and size_for("a cat", "1024x576") == "1024x576"

    @mac_only
    @pytest.mark.parametrize("shape", ["wide", "tall"])
    def test_a_provider_that_only_makes_squares_gets_a_square_and_the_true_size_is_shown(self, tmp_path, shape):
        provider = Provider(data=png(1024, 1024), sizes=("1024x1024",))
        studio, _ = make(tmp_path, provider)
        job = studio.get(studio.submit("a city", shape)["id"])
        assert provider.calls[0]["size"] == "1024x1024" and job["state"] == "complete"
        assert job["size"] == SIZES[shape] and job["cropped_from"] == "1024x1024"
        width, height = (int(n) for n in SIZES[shape].split("x"))
        assert size_of(studio.file(job["id"])[0]) == (width, height)
        assert (tmp_path / "images" / job["id"] / job["original"]).read_bytes() == provider.data  # the whole picture is kept

    def test_if_it_cannot_be_cut_it_says_the_size_it_really_is(self, tmp_path, monkeypatch):
        monkeypatch.setattr(studio_module, "_crop", lambda *a: False)
        provider = Provider(data=png(16, 16), sizes=("1024x1024",))
        studio, _ = make(tmp_path, provider)
        job = studio.get(studio.submit("a city", "wide")["id"])
        assert job["state"] == "complete" and job["size"] == "1024x1024" and not job.get("cropped_from") and studio.file(job["id"])[0] == provider.data

    def test_a_provider_that_makes_that_shape_is_simply_asked_for_it(self, tmp_path):
        provider = Provider(sizes=("1024x1024", "1024x576"))
        studio, _ = make(tmp_path, provider)
        job = studio.get(studio.submit("a city", "wide")["id"])
        assert provider.calls[0]["size"] == "1024x576" and not job.get("cropped_from")

    def test_the_prompt_writer_is_told_what_will_be_cut(self):
        assert "wide" in studio_module._framing("1024x576") and "tall" in studio_module._framing("576x1024") and studio_module._framing("1024x1024") == ""


class TestSaving:
    def test_only_when_asked_never_over_a_file_and_the_original_stays(self, tmp_path):
        studio, provider = make(tmp_path)
        job = studio.submit("a lighthouse: at dusk!")
        downloads = tmp_path / "downloads"
        assert not downloads.exists()
        one, two = studio.save(job["id"], downloads), studio.save(job["id"], downloads)
        assert one["saved"] != two["saved"] and Path(one["saved"]).read_bytes() == provider.data and Path(two["saved"]).read_bytes() == provider.data
        assert Path(one["saved"]).name == "a-lighthouse-at-dusk.png" and studio.file(job["id"])[0] == provider.data

    def test_a_name_from_a_prompt_cannot_climb_out_of_the_folder(self, tmp_path):
        studio, _ = make(tmp_path)
        job = studio.submit("../../../etc/evil")
        saved = Path(studio.save(job["id"], tmp_path / "d")["saved"])
        assert saved.parent == tmp_path / "d"

    def test_there_must_be_a_picture_to_save(self, tmp_path):
        studio, _ = make(tmp_path, Provider(error=ImageError("timeout", "slow")))
        with pytest.raises(StudioError):
            studio.save(studio.submit("x y")["id"], tmp_path / "d")


class TestWhatItWillNotMake:
    @pytest.mark.parametrize("prompt", ["nude child", "a naked 12 year old", "sexual picture of a schoolgirl", "erotic teen"])
    def test_sexual_images_of_children(self, tmp_path, prompt):
        studio, provider = make(tmp_path)
        assert refused_prompt(prompt)
        with pytest.raises(StudioError) as err:
            studio.submit(prompt)
        assert err.value.status == 403 and provider.calls == []

    @pytest.mark.parametrize("prompt", ["a child reading a book", "kids flying a kite", "a naked mole rat", "a teen at a science fair"])
    def test_innocent_pictures_are_fine(self, prompt):
        assert not refused_prompt(prompt)


class TestPrivacy:
    def test_a_prompt_is_not_logged(self, tmp_path, caplog):
        caplog.set_level(logging.DEBUG)
        studio, _ = make(tmp_path, Provider(error=ImageError("timeout", "slow")))
        studio.submit("my very private idea")
        assert "my very private idea" not in caplog.text


class TestNabh:
    def client(self, reply):
        nabh = Nabh("sk-test-key")
        nabh.post = lambda path, payload, timeout=0: reply if not isinstance(reply, Exception) else (_ for _ in ()).throw(reply)
        return nabh

    def test_a_real_answer_is_a_picture(self):
        import base64

        out = NabhImages(self.client({"data": [{"b64_json": base64.b64encode(png()).decode()}], "cost_inr": "0"})).generate("x", "1024x1024")
        assert image_kind(out.data) == "png" and out.cost == "0" and out.model

    @pytest.mark.parametrize("reply", [{"data": []}, {"data": [{}]}, {"choices": [{"message": {"content": "Here is an image of a cat"}}]}, {}])
    def test_a_text_answer_is_not(self, reply):
        with pytest.raises(ImageError) as err:
            NabhImages(self.client(reply)).generate("x", "1024x1024")
        assert err.value.kind == "bad_response"

    @pytest.mark.parametrize("kind,want", [("rejected", "refused"), ("rate_limit", "rate_limit"), ("auth", "auth"), ("timeout", "timeout"), ("network", "network"), ("quota", "quota")])
    def test_its_errors_keep_their_meaning(self, kind, want):
        with pytest.raises(ImageError) as err:
            NabhImages(self.client(NabhError(kind, "no", 9))).generate("x", "1024x1024")
        assert err.value.kind == want and err.value.retry_after == 9 and "sk-test-key" not in str(err.value)

    def test_it_only_asks_nabh_for_what_nabh_makes(self):
        assert NabhImages.sizes == ("1024x1024",)

    def test_the_default_negative_goes_with_the_request(self):
        import base64

        seen = {}
        nabh = Nabh("k")
        nabh.post = lambda path, payload, timeout=0: seen.update(payload) or {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        NabhImages(nabh).generate("x", "1024x1024", seed=5)
        assert seen["negative_prompt"] == DEFAULT_NEGATIVE and seen["seed"] == 5 and seen["n"] == 1


class TestAsking:
    @pytest.mark.parametrize("words,prompt", [
        ("Generate an image of a red fox", "a red fox"), ("Xenio, create a picture of a castle", "a castle"), ("Create a futuristic wallpaper of a city at night", "a futuristic wallpaper of a city at night"),
        ("draw a dragon", "a dragon"), ("ek pahad ki tasveer banao", "pahad"),
    ])
    def test_natural_requests(self, words, prompt):
        got = parse_image_command(words)
        assert got is not None
        if prompt:
            assert prompt in got["prompt"]

    def test_three_of_them(self):
        assert parse_image_command("Generate three images of a red fox")["count"] == 3

    @pytest.mark.parametrize("words,n", [("Make three variations of this", 3), ("make a variation", 1), ("give me two more variations", 2)])
    def test_more_of_the_last_one(self, words, n):
        got = parse_image_command(words)
        assert got == {"variations": n}

    def test_illustrating_something_named(self):
        assert "photosynthesis" in parse_image_command("Illustrate photosynthesis")["prompt"]

    @pytest.mark.parametrize("words", ["Generate an image of this", "illustrate this concept", "draw that", "draw a flowchart of binary search", "create a diagram of the heart", "what is an image", "open the photos app", "hello"])
    def test_what_is_not_a_plain_request_goes_to_the_model(self, words):
        assert parse_image_command(words) is None


class TestPolicy:
    def test_asked_for_it_runs(self):
        d = p.classify_image_tool({"action": "generate", "prompt": "a fox"}, "generate an image of a fox")
        assert d.level == RiskLevel.SAFE_WRITE and not d.blocked

    def test_not_asked_for_it_asks(self):
        d = p.classify_image_tool({"action": "generate", "prompt": "a fox"}, "what is the capital of France")
        assert d.level == RiskLevel.CONFIRM_REQUIRED

    @pytest.mark.parametrize("words", ["my password is hunter2xyz and key sk-abcdefghijklmnopqrstuvwxyz123456", "card 4242 4242 4242 4242"])
    def test_a_secret_never_goes(self, words):
        assert p.classify_image_tool({"action": "generate", "prompt": words}, "generate an image").blocked

    def test_what_it_will_not_make_is_blocked(self):
        assert p.classify_image_tool({"action": "generate", "prompt": "nude child"}, "generate an image of a nude child").blocked

    @pytest.mark.parametrize("action", ["status", "list"])
    def test_looking_is_a_read(self, action):
        assert p.classify_image_tool({"action": action}, "x").level <= RiskLevel.SAFE_WRITE

    def test_it_cannot_run_when_nobody_is_there(self):
        allowed = p.classify_image_tool({"action": "generate", "prompt": "a fox"}, "generate an image of a fox")
        assert p.unattended_desktop(allowed, "xenio_image", "cron_1").blocked and not p.unattended_desktop(allowed, "xenio_image", "s1").blocked


class TestTheTool:
    @pytest.fixture
    def studio(self, tmp_path):
        made, _ = make(tmp_path)
        image_tool.install(made)
        yield made
        image_tool.install(None)

    def call(self, **args):
        return image_tool.image(args, ToolContext(paths=PATHS, session="s1"))

    def test_generate_returns_a_card_and_never_claims_to_have_seen_it(self, studio):
        out = self.call(action="generate", prompt="a fox")
        job = out.data["jobs"][0]
        assert out.success and f"[xenio-card image {job['id']}]" in out.message
        assert "never describe" in out.data["note"]
        assert studio.get(job["id"])["conversation"] == "s1"

    def test_several(self, studio):
        out = self.call(action="generate", prompt="a fox", count=3)
        assert len(out.data["jobs"]) == 3 and out.message.count("[xenio-card image") == 3

    def test_status_cancel_and_list(self, studio):
        job = self.call(action="generate", prompt="a fox").data["jobs"][0]
        assert self.call(action="status", id=job["id"]).data["jobs"][0]["state"] == "complete"
        assert self.call(action="cancel", id=job["id"]).success
        assert self.call(action="list").data["pictures"][0]["id"] == job["id"]

    def test_a_refusal_is_a_failure_with_its_reason(self, studio):
        out = self.call(action="generate", prompt="nude child")
        assert not out.success and "does not make" in out.error

    def test_unknown_actions_and_missing_pictures(self, studio):
        assert not self.call(action="teleport").success
        assert not self.call(action="status", id="img_" + "0" * 16).success
        assert not self.call(action="variation", id="img_" + "0" * 16).success

    def test_a_failed_picture_is_reported_as_failed(self, tmp_path):
        made, _ = make(tmp_path, Provider(error=ImageError("rate_limit", "slow down", 5)))
        image_tool.install(made)
        try:
            started = self.call(action="generate", prompt="a fox").data["jobs"][0]
            job = self.call(action="status", id=started["id"]).data["jobs"][0]
            assert job["state"] == "failed" and job["error"]["kind"] == "rate_limit"
        finally:
            image_tool.install(None)


class TestFastPath:
    def fast(self, images):
        from app.fastpath import FastPath

        return FastPath(PATHS, images=images)

    def test_it_starts_a_job_with_no_turn_of_the_main_model(self, tmp_path):
        studio, provider = make(tmp_path)
        started = time.monotonic()
        reply = asyncio.run(self.fast(studio).handle("generate an image of a red fox"))
        assert time.monotonic() - started < 1.0
        assert reply.attachments[0]["kind"] == "image" and studio.get(reply.attachments[0]["id"]) is not None and provider.calls

    def test_it_never_says_a_picture_is_done_before_it_is(self, tmp_path):
        studio, _ = make(tmp_path, Provider(gate=threading.Event()), background=True)
        reply = asyncio.run(self.fast(studio).handle("generate an image of a red fox"))
        assert not any(word in reply.text.lower() for word in ("here is", "here's", "done", "ready"))

    def test_more_of_the_last_one(self, tmp_path):
        studio, provider = make(tmp_path)
        studio.submit("a fox")
        reply = asyncio.run(self.fast(studio).handle("make three variations"))
        assert len(reply.attachments) == 3 and len(provider.calls) == 4

    def test_no_picture_to_vary_goes_to_the_model(self, tmp_path):
        studio, _ = make(tmp_path)
        assert asyncio.run(self.fast(studio).handle("make three variations")) is None

    def test_a_refusal_is_said(self, tmp_path):
        studio, provider = make(tmp_path)
        reply = asyncio.run(self.fast(studio).handle("generate an image of a nude child"))
        assert reply is not None and "does not make" in reply.text and provider.calls == [] and not reply.attachments


class TestThePage:
    """The buttons on a picture, through the real controller."""

    @pytest.fixture
    def ctl(self, tmp_path):
        from types import SimpleNamespace

        from app.ui.controller import UiController

        ctl = UiController.__new__(UiController)
        made, provider = make(tmp_path)
        ctl._images = made
        ctl.provider = provider
        ctl.orchestrator = SimpleNamespace(fast=None)
        return ctl

    def test_make_look_and_fetch(self, ctl):
        job = ctl.image_make({"prompt": "a fox"})
        assert ctl.image(job["id"])["state"] == "complete" and ctl.image_list()["images"][0]["id"] == job["id"]
        data, kind = ctl.image_file(job["id"])
        assert data == ctl.provider.data and kind == "image/png"

    @pytest.mark.parametrize("action,more", [("regenerate", {}), ("variation", {"count": 2}), ("refine", {"change": "at night"})])
    def test_each_button_is_a_new_job_and_the_original_stays(self, ctl, action, more):
        job = ctl.image_make({"prompt": "a fox"})
        before = ctl.image_file(job["id"])
        out = ctl.image_change(job["id"], {"action": action, **more})
        new = out["jobs"] if "jobs" in out else [out]
        assert all(n["id"] != job["id"] and n["parent"] == job["id"] for n in new) and ctl.image_file(job["id"]) == before

    def test_errors_are_what_the_page_can_show(self, ctl):
        from app.ui.controller import UiError

        with pytest.raises(UiError) as err:
            ctl.image_make({"prompt": ""})
        assert err.value.status == 422
        with pytest.raises(UiError) as err:
            ctl.image("img_" + "0" * 16)
        assert err.value.status == 404
        with pytest.raises(UiError):
            ctl.image_file("img_" + "0" * 16)
        with pytest.raises(UiError):
            ctl.image_change(ctl.image_make({"prompt": "a b"})["id"], {"action": "teleport"})


class TestTheChatStaysFree:
    """A picture takes half a minute, and the conversation goes on meanwhile: the real controller and fast path, with only Hermes and Nabh replaced."""

    def test_the_turn_ends_at_once_and_another_message_can_be_sent(self, tmp_path, make_settings):
        from types import SimpleNamespace

        from app.fastpath import FastPath
        from app.safety.permissions import PermissionGate
        from app.ui.approvals import WebConfirmer
        from app.ui.assistant import AssistantState
        from app.ui.controller import UiController
        from app.ui.events import EventHub

        gate_open = threading.Event()
        studio, provider = make(tmp_path, Provider(gate=gate_open), background=True)

        class Orchestrator:
            hermes = SimpleNamespace(agent_info={}, connected=True)
            busy = False
            fast = FastPath(PATHS, images=studio)

            async def start(self):
                return True

            async def process_text(self, text, on_chunk=None, on_tool=None, context=None, **extra):
                reply = await self.fast.handle(text)
                words = reply.text if reply else "an answer from the model"
                on_chunk(words)
                return SimpleNamespace(text=words, ok=True, error=None, cancelled=False, sources=[], attachments=list(getattr(reply, "attachments", None) or []), action_lines=lambda: [])

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        hub, state = EventHub(tmp_path / "ui" / "a.jsonl"), AssistantState()
        confirmer = WebConfirmer(hub.publish, timeout=5, on_change=lambda n: None)
        ctl = UiController(make_settings(), Orchestrator(), PermissionGate(confirmer, timeout=5), confirmer, hub, state, hermes_home=tmp_path / "h")
        ctl._images = studio
        asyncio.run_coroutine_threadsafe(ctl.start(), loop).result(10)
        try:
            started = time.monotonic()
            ctl.chat("generate an image of a red fox", quiet=True)
            assert wait_until(lambda: state.turn is None and len(ctl.conversation) == 2, 2.0), f"still busy: {state.turn}"
            assert time.monotonic() - started < 1.5
            job_id = ctl.conversation[-1]["attachments"][0]["id"]
            assert wait_until(lambda: provider.running == 1)
            assert studio.get(job_id)["state"] in ACTIVE  # the picture is still being made...
            ctl.chat("tell me why foxes are red", quiet=True)  # ...and this is not refused or held up
            assert wait_until(lambda: len(ctl.conversation) == 4, 2.0)
            assert ctl.conversation[-1]["text"] == "an answer from the model" and studio.get(job_id)["state"] in ACTIVE
        finally:
            gate_open.set()
            asyncio.run_coroutine_threadsafe(ctl.stop() if hasattr(ctl, "stop") else asyncio.sleep(0), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
