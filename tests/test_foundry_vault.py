"""The password vault, for real: the real native helper, real encryption, a real WebKit browser and a real local website that records what it receives. Only the fingerprint itself cannot be scripted, and the one test that needs it says so.

The rule under test: a password can be placed, and never seen. So every test also searches everything XENIO could have seen for it.
"""

import asyncio
import base64
import http.server
import inspect
import json
import subprocess
import threading
import urllib.parse
from pathlib import Path

import pytest

from app.foundry import hands as hands_module
from app.foundry import vault as vault_module
from app.foundry.hands import Browser
from app.foundry.vault import Vault, VaultError, host_matches

BINARY = hands_module.BINARY
needs_helper = pytest.mark.skipif(not BINARY.is_file(), reason="needs native/build/xenio-hands")
SECRET = "correct horse battery staple"
USER = "asha.sharma@example.com"
PASSWORD = "Sup3r-S3cret-P@ss-9931!"


def vault_run(folder, *args, secrets=None):
    argv = [str(BINARY), "vault", *args, "--dir", str(folder)]
    given = {"args": argv, "capture_output": True, "text": True, "timeout": 60}
    if secrets is not None:
        argv.append("--secrets-on-stdin")
        given["input"] = json.dumps(secrets) + "\n"
    done = subprocess.run(**given)
    return json.loads(done.stdout.strip().splitlines()[-1]), done.stdout + done.stderr


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "vault"


@pytest.fixture
def ready(folder):
    out, _ = vault_run(folder, "setup", secrets={"secret": SECRET})
    assert out["ok"]
    return folder


def add(folder, host="127.0.0.1", username=USER, password=PASSWORD):
    return vault_run(folder, "add", "--host", host, "--label", "test", secrets={"secret": SECRET, "username": username, "password": password})


class Site(http.server.BaseHTTPRequestHandler):
    """A website with a login form. It keeps what it was sent, so the test can see that the real password arrived."""

    received: list = []
    pages: dict = {}

    def log_message(self, *_):
        pass

    def do_GET(self):
        body = self.pages.get(self.path.split("?")[0], "<h1>nothing</h1>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        self.received.append({k: v[0] for k, v in form.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<title>Welcome</title><h1>Welcome back</h1><p>You are signed in.</p>")


LOGIN = """<!doctype html><title>Sign in</title><h1>Sign in</h1>
<form method="post" action="/in">
  <input type="password" name="trap" style="display:none">
  <label>Email <input type="email" name="email" autocomplete="username"></label>
  <label>Password <input id="pw" type="password" name="password" autocomplete="current-password"></label>
  <button type="button" id="eye" onclick="document.getElementById('pw').type='text'">Show password</button>
  <button type="submit">Sign in</button>
</form>"""
TWO_STEP = """<!doctype html><title>Sign in</title><h1>Sign in</h1>
<form method="post" action="/in" id="f">
  <div id="one"><label>Email <input type="email" name="email" id="email"></label><button type="button" id="next" onclick="document.getElementById('one').style.display='none';document.getElementById('two').style.display='block'">Next</button></div>
  <div id="two" style="display:none"><label>Password <input type="password" name="password"></label><button type="submit">Sign in</button></div>
</form>"""
NOT_HOSTILE = "<!doctype html><title>Nothing</title><h1>No form here</h1><p>Just words.</p>"


@pytest.fixture
def site():
    Site.received = []
    Site.pages = {"/login": LOGIN, "/two": TWO_STEP, "/plain": NOT_HOSTILE}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()


def browser(port, folder):
    return Browser("Research Agent", allow_local=port, vault_dir=folder, extra=("--allow-secret-over-pipe", "--no-windows"))


async def sign_in(port, folder, item, path="/login", host="127.0.0.1", **extra):
    b = browser(port, folder)
    said = []

    async def do(command, **fields):
        out = await b.do(command, **fields)
        said.append(json.dumps(out))
        return out

    try:
        opened = await do("open", url=f"http://{host}:{port}{path}")
        assert opened["ok"]
        result = await do("sign_in", item=item, secret=extra.pop("secret", SECRET), **extra)
        snapshot = await do("snapshot")
        words = await do("read")
        return result, snapshot, words, said, b, do
    except Exception:
        await b.close()
        raise


@needs_helper
class TestTheFile:
    def test_nothing_readable_is_written_and_only_you_can_open_the_file(self, ready):
        item, out = add(ready)
        assert item["ok"] and PASSWORD not in out
        raw = (ready / "vault.json").read_bytes()
        for plain in (PASSWORD, SECRET):
            assert plain.encode() not in raw
            assert base64.b64encode(plain.encode()) not in raw
        assert USER.encode() in raw  # the CEO asked for XENIO to know which email it would use: the email is readable, and only the password is sealed
        assert (ready / "vault.json").stat().st_mode & 0o777 == 0o600 and ready.stat().st_mode & 0o777 == 0o700

    def test_the_same_password_saved_twice_never_looks_the_same(self, ready):
        add(ready, "one.example.com")
        add(ready, "two.example.com")
        boxes = [i["box"] for i in json.loads((ready / "vault.json").read_text())["items"]]
        assert len(boxes) == 2 and boxes[0] != boxes[1]

    def test_the_wrong_secret_opens_nothing(self, ready):
        add(ready)
        out, text = vault_run(ready, "add", "--host", "x.example.com", secrets={"secret": "this is not the secret", "username": "a", "password": "b"})
        assert not out["ok"] and "not the secret password" in out["error"] and PASSWORD not in text

    def test_a_weak_secret_is_refused(self, folder):
        out, _ = vault_run(folder, "setup", secrets={"secret": "short"})
        assert not out["ok"] and not folder.exists()

    def test_it_cannot_be_set_up_over_itself(self, ready):
        add(ready)
        out, _ = vault_run(ready, "setup", secrets={"secret": "another secret entirely"})
        assert not out["ok"] and len(vault_run(ready, "status")[0]["items"]) == 1

    def test_status_says_names_only_and_never_needs_unlocking(self, ready):
        add(ready)
        out, text = vault_run(ready, "status")
        assert out["items"][0]["host"] == "127.0.0.1" and out["items"][0]["account"] == USER and PASSWORD not in text and "box" not in text and "salt" not in text

    def test_there_is_no_command_that_gives_a_password_back(self, ready):
        add(ready)
        for command in ("read", "get", "show", "reveal", "export", "dump", "list", "print", "decrypt"):
            out, text = vault_run(ready, command, secrets={"secret": SECRET})
            assert not out["ok"] and PASSWORD not in text

    def test_removing_takes_the_secret_too(self, ready):
        item, _ = add(ready)
        out, _ = vault_run(ready, "remove", "--id", item["id"], secrets={"secret": "wrong wrong wrong"})
        assert not out["ok"] and len(vault_run(ready, "status")[0]["items"]) == 1
        out, _ = vault_run(ready, "remove", "--id", item["id"], secrets={"secret": SECRET})
        assert out["ok"] and not out["items"]

    @pytest.mark.parametrize("saved, page, fits", [
        ("google.com", "google.com", True),
        ("google.com", "accounts.google.com", True),
        ("https://www.google.com/x", "accounts.google.com", True),
        ("google.com", "google.com.evil.com", False),
        ("google.com", "evilgoogle.com", False),
        ("accounts.google.com", "google.com", False),
        ("accounts.google.com", "mail.google.com", False),
    ])
    def test_a_login_goes_only_to_its_own_website(self, folder, saved, page, fits):
        out, _ = vault_run(folder, "check-host", "--page", page, "--saved", saved)
        assert out["matches"] is fits
        assert host_matches(page, vault_module.host_of(saved)) is fits

    @pytest.mark.parametrize("bad", ["co.uk", "co.in", "localhost", "com", "10.0.0.1", ""])
    def test_a_login_cannot_be_tied_to_something_that_is_not_one_website(self, ready, bad):
        out, _ = add(ready, bad)
        assert not out["ok"]


@needs_helper
class TestNothingWaitsForEver:
    def test_waiting_for_your_finger_from_inside_a_window_comes_back(self, folder):
        """The bug that hung saving a login after the fingerprint was accepted: waiting from where a browser's handlers run."""
        out, _ = vault_run(folder, "check-wait")
        assert out == {"ok": True, "got": 42}


@needs_helper
class TestYourFingerprint:
    def test_a_key_locked_to_a_finger_cannot_be_used_without_one(self, folder):
        """Asked of the Secure Enclave chip, with no window shown. A real finger on the real sensor cannot be scripted: that part is for you to try."""
        out, _ = vault_run(folder, "check-finger-lock")
        if not out.get("available"):
            pytest.skip("no Touch ID on this Mac")
        assert out["ok"] and out["used_without_finger"] is False


@needs_helper
class TestPlacedAndNeverSeen:
    def test_the_real_password_reaches_the_website_and_nothing_xenio_sees_contains_it(self, ready, site):
        item, _ = add(ready)

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"])
            await b.close()
            return result, snapshot, words, said

        result, snapshot, words, said = asyncio.run(go())
        assert result["ok"] and result["filled"] == ["username", "password"] and result["submitted"]
        assert Site.received == [{"email": USER, "password": PASSWORD}]  # the real thing arrived, and nothing went in the hidden trap field
        assert "Welcome back" in words["text"]
        everything = " ".join(said)
        assert PASSWORD not in everything and SECRET not in everything

    def test_an_agent_reading_the_filled_form_sees_no_value(self, ready, site):
        item, _ = add(ready)

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"], submit=False)
            await b.close()
            return result, snapshot, words, said

        result, snapshot, words, said = asyncio.run(go())
        assert result["ok"] and not result["submitted"] and not Site.received
        assert "filled from the CEO's vault" in snapshot["lines"]
        everything = " ".join(said)
        assert PASSWORD not in everything and USER not in everything

    def test_a_show_password_button_cannot_turn_it_into_words(self, ready, site):
        item, _ = add(ready)

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"], submit=False)
            clicked = await do("click", text="Show password")
            after = await do("snapshot")
            await b.close()
            return clicked, after, said

        clicked, after, said = asyncio.run(go())
        assert clicked["ok"] and PASSWORD not in after["lines"] and PASSWORD not in " ".join(said)

    def test_an_agent_cannot_type_into_a_filled_field(self, ready, site):
        item, _ = add(ready)

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"], submit=False)
            ref = next(line.split("]")[0][1:] for line in snapshot["lines"].splitlines() if "vault" in line and "Password" in line)
            out = await do("type", ref=ref, text="overwritten")
            await b.close()
            return out

        assert not asyncio.run(go())["ok"]

    def test_name_on_one_screen_and_password_on_the_next_takes_one_unlock(self, ready, site):
        item, _ = add(ready)

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"], path="/two")
            await b.close()
            return result, words, said

        result, words, said = asyncio.run(go())
        assert result["ok"] and result["filled"] == ["username", "password"]
        assert Site.received and Site.received[0]["email"] == USER and Site.received[0]["password"] == PASSWORD
        assert PASSWORD not in " ".join(said)


@needs_helper
class TestItRefusesRatherThanLeaks:
    def test_the_wrong_secret_places_nothing(self, ready, site):
        item, _ = add(ready)

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"], secret="wrong wrong wrong")
            await b.close()
            return result, said

        result, said = asyncio.run(go())
        assert not result["ok"] and not Site.received and PASSWORD not in " ".join(said)

    def test_another_website_gets_nothing_and_your_secret_is_not_even_tried(self, ready, site):
        item, _ = add(ready, "bank.example.com")

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"])
            await b.close()
            return result, snapshot, said

        result, snapshot, said = asyncio.run(go())
        assert not result["ok"] and "not bank.example.com" in result["error"] and not Site.received and "filled from" not in snapshot["lines"]

    def test_moving_a_login_to_another_website_in_the_file_does_not_work(self, ready, site):
        item, _ = add(ready, "bank.example.com")
        data = json.loads((ready / "vault.json").read_text())
        data["items"][0]["host"] = "127.0.0.1"  # an attacker edits the file so the bank login goes to their page
        (ready / "vault.json").write_text(json.dumps(data))

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"])
            await b.close()
            return result, said

        result, said = asyncio.run(go())
        assert not result["ok"] and "tampered" in result["error"] and not Site.received and PASSWORD not in " ".join(said)

    def test_a_changed_file_is_noticed(self, ready, site):
        item, _ = add(ready)
        data = json.loads((ready / "vault.json").read_text())
        box = bytearray(base64.b64decode(data["items"][0]["box"]))
        box[20] ^= 1
        data["items"][0]["box"] = base64.b64encode(bytes(box)).decode()
        (ready / "vault.json").write_text(json.dumps(data))

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"])
            await b.close()
            return result

        result = asyncio.run(go())
        assert not result["ok"] and "tampered" in result["error"] and not Site.received

    def test_a_page_with_no_form_gets_nothing(self, ready, site):
        item, _ = add(ready)

        async def go():
            result, snapshot, words, said, b, do = await sign_in(site, ready, item["id"], path="/plain")
            await b.close()
            return result

        result = asyncio.run(go())
        assert not result["ok"] and not Site.received

    def test_without_the_test_switch_the_pipe_cannot_carry_your_secret(self, ready, site):
        """XENIO's own browsers never get --allow-secret-over-pipe. Sent anyway, it is ignored."""
        item, _ = add(ready)

        async def go():
            b = Browser("Agent", allow_local=site, vault_dir=ready, extra=("--no-windows",))
            await b.do("open", url=f"http://127.0.0.1:{site}/login")
            out = await b.do("sign_in", item=item["id"], secret=SECRET)
            await b.close()
            return out

        out = asyncio.run(go())
        assert not out.get("ok") and not Site.received


class TestXenioHoldsNothing:
    def test_nothing_in_python_can_take_or_return_a_password(self):
        for name, member in inspect.getmembers(Vault, inspect.isfunction):
            words = " ".join(inspect.signature(member).parameters)
            assert "password" not in words and "secret" not in words and "username" not in words, name

    def test_xenios_browsers_are_never_started_with_the_test_switch(self):
        source = Path(hands_module.__file__).read_text()
        assert "allow-secret-over-pipe" not in source

    @needs_helper
    def test_a_login_is_found_by_website_name_only(self, ready):
        add(ready, "google.com")
        vault = Vault(ready)
        found = vault.login_for("https://accounts.google.com/signin")
        assert found["host"] == "google.com" and set(found) == {"id", "host", "label", "added", "account"} and found["account"] == USER
        assert vault.login_for("https://google.com.evil.com/") is None

    def test_it_says_so_when_the_helper_refuses(self, tmp_path):
        def refuses(argv, **_):
            return subprocess.CompletedProcess(argv, 1, stdout='{"ok": false, "error": "you cancelled"}\n', stderr="")

        with pytest.raises(VaultError, match="you cancelled"):
            Vault(tmp_path, binary=Path(__file__), runner=refuses).status()


@needs_helper
class TestAnAgentUsingIt:
    """The whole path an agent takes, through the real toolbox and a real browser: it asks to sign in, the login is placed, and it carries on knowing nothing."""

    def box(self, tmp_path, site, ready):
        from app.foundry.hands import Hands
        from app.foundry.toolbox import Toolbox

        hands = Hands(tmp_path / "live", vault_dir=ready)
        hands.allow_local = site
        hands.extra = ("--allow-secret-over-pipe", "--no-windows")
        told = []

        class Engine:
            notify = staticmethod(lambda kind, text: None)

            class bus:
                @staticmethod
                def send(workflow, sender, recipient, kind, content, **_):
                    told.append(content)

            class store:
                events = []

                @classmethod
                def event(cls, kind, workflow, agent="", task="", **facts):
                    cls.events.append((kind, facts))

            class registry:
                @staticmethod
                def get(name):
                    return None

            flows = {}

        box = Toolbox(Engine, hands=hands, vault=Vault(ready))
        return box, hands, told, Engine.store.events

    SCOPE = {"workflow": "w", "role": "browser-agent", "task": "t", "tools": ["page_open", "page_read", "page_sign_in", "page_click", "page_type"]}

    def test_with_no_login_saved_the_ceo_is_told_and_nothing_is_guessed(self, tmp_path, ready, site):
        box, hands, told, _ = self.box(tmp_path, site, ready)

        async def go():
            await box.call("page_open", {"url": f"http://127.0.0.1:{site}/login"}, self.SCOPE)
            out = await box.call("page_sign_in", {"why": "to edit the profile"}, self.SCOPE)
            await hands.close()
            return out

        text, ok = asyncio.run(go())
        assert not ok and "no login" in text and any("needs a login" in t for t in told) and not Site.received

    def test_an_agent_cannot_type_a_password_even_if_it_somehow_had_one(self, tmp_path, ready, site):
        box, hands, _, _ = self.box(tmp_path, site, ready)

        async def go():
            page, _ = await box.call("page_open", {"url": f"http://127.0.0.1:{site}/login"}, self.SCOPE)
            ref = next(int(line.split("]")[0][1:]) for line in page.splitlines() if "password field" in line)
            out = await box.call("page_type", {"ref": ref, "text": "hunter2"}, self.SCOPE)
            await hands.close()
            return out

        text, ok = asyncio.run(go())
        assert not ok and "private box" in text

    def test_a_refused_fingerprint_means_no_sign_in_and_the_agent_is_told_only_that(self, tmp_path, ready, site):
        add(ready)
        box, hands, told, events = self.box(tmp_path, site, ready)
        hands.extra = ("--no-windows",)  # no secret over the pipe, and no window can be answered: as when you cancel

        async def go():
            await box.call("page_open", {"url": f"http://127.0.0.1:{site}/login"}, self.SCOPE)
            out = await box.call("page_sign_in", {"why": "to edit the profile"}, self.SCOPE)
            await hands.close()
            return out

        text, ok = asyncio.run(go())
        assert not ok and "Not signed in" in text and not Site.received and PASSWORD not in text and USER not in text
        assert any("wants to sign in" in t for t in told)  # the CEO was told why before being asked
        assert ("vault.used", {"host": "127.0.0.1", "ok": False, "by": "browser-agent"}) in events

    def test_opening_a_site_for_you_hands_over_no_secret_and_needs_https(self, tmp_path, ready):
        add(ready, "example.com")
        started = []

        class Pipe:
            written = ""

            def write(self, text):
                Pipe.written += text

            def flush(self):
                pass

        class Process:
            stdin = Pipe()

        def popen(argv, **_):
            started.append(argv)
            return Process()

        vault = Vault(ready)
        out = vault.open_site("https://www.example.com/login", popen=popen)
        assert out == {"opened": "www.example.com", "saved_login": True}
        assert "--visible" in started[0] and "--allow-secret-over-pipe" not in started[0]
        assert '"sign_in"' in Pipe.written and PASSWORD not in Pipe.written and USER not in Pipe.written
        with pytest.raises(VaultError):
            vault.open_site("http://example.com/", popen=popen)



@needs_helper
class TestPlacedInYourOwnBrowser:
    """The step that places your email and password into a window of your own browser, through Accessibility, with no keyboard and no mouse. Tried on a WebKit window of XENIO's own, never on yours: Safari is the same WebKit underneath."""

    @pytest.fixture(autouse=True)
    def unlocked(self):
        from app.desktop import macos

        if macos.session_locked():  # as for the arrows: Accessibility answers nothing about windows while the Mac is locked or its display sleeps
            pytest.skip("the Mac is locked: XENIO touches nothing on a locked Mac, so there is nothing to test")

    def test_it_lands_in_the_page_and_nothing_secret_comes_back(self, ready, site):
        item, _ = add(ready)

        async def go():
            b = Browser("Tester", allow_local=site)
            await b.do("open", url=f"http://127.0.0.1:{site}/login")
            out, text = vault_run(ready, "fill-window", "--pid", str(b.proc.pid), "--title", "Sign in", "--item", item["id"], "--allow-local", str(site), secrets={"secret": SECRET})
            await b.do("click", text="Sign in")
            await b.close()
            return out, text

        out, text = asyncio.run(go())
        assert out["ok"] and out["filled"] == ["email", "password"] and PASSWORD not in text
        assert Site.received and Site.received[0]["password"] == PASSWORD and Site.received[0]["email"] == USER

    def test_a_window_showing_another_website_gets_nothing(self, ready, site):
        item, _ = add(ready, "linkedin.com")

        async def go():
            b = Browser("Tester", allow_local=site)
            await b.do("open", url=f"http://127.0.0.1:{site}/login")
            out, text = vault_run(ready, "fill-window", "--pid", str(b.proc.pid), "--title", "Sign in", "--item", item["id"], "--allow-local", str(site), secrets={"secret": SECRET})
            await b.close()
            return out, text

        out, text = asyncio.run(go())
        assert not out["ok"] and "does not show linkedin.com" in out["error"] and PASSWORD not in text and not Site.received
