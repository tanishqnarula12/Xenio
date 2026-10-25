"""The real ACP bridge + adapter, driven against tests/fake_acp_agent.py over stdio."""

import asyncio
import json

import pytest

from app.hermes.adapter import HermesAcpClient, _pick_option
from app.hermes.client import HermesError, HermesUnavailable
from app.hermes.fingerprint import prompt_fingerprint
from app.hermes.guard import SOURCE_DIR
from app.safety.policy import POLICY_VERSION

PROMPT = prompt_fingerprint(SOURCE_DIR / "__init__.py")  # the guard's rules a saved session was started with


async def test_old_policy_session_is_not_resumed(client_factory, make_settings, tmp_path):
    settings = make_settings()
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"session_id": "known-session", "workspace": str(settings.hermes.workspace)}))
    client = client_factory(session_file=session_file)
    await client.start()
    assert client.session_id == "new-session"  # its frozen system prompt predates the current rules


@pytest.fixture
async def client_factory(make_settings, tmp_path):
    clients = []

    def _make(session_file=None, hermes_home=None, **overrides):
        settings = make_settings(**overrides)
        # Never the developer's real Hermes config: tests get their own (empty unless written).
        client = HermesAcpClient(settings.hermes, session_file=session_file, hermes_home=hermes_home or tmp_path / "hermes")
        clients.append(client)
        return client

    yield _make
    for c in clients:
        await c.close()


async def test_start_handshake_and_session(client_factory):
    client = client_factory()
    await client.start()
    assert client.connected
    assert client.agent_info == {"name": "fake-hermes", "version": "9.9.9"}
    assert client.session_id == "new-session"


async def test_send_message_streams_chunks(client_factory):
    client = client_factory()
    seen = []
    reply = await client.send_message("hello", on_chunk=seen.append)
    assert reply.text == "Hello!"
    assert reply.stop_reason == "end_turn"
    assert seen == ["Hel", "lo!"]


async def test_multiple_assistant_messages_are_separated(client_factory):
    reply = await client_factory().send_message("multi")
    assert reply.text == "First.\n\nSecond."


async def test_permission_request_approved_uses_allow_once(client_factory):
    client = client_factory()
    received = []

    async def handler(req):
        received.append(req)
        return True

    client.set_permission_handler(handler)
    reply = await client.send_message("permission")
    assert reply.text == "outcome=selected:allow_once"  # never the standing "allow_session"
    assert received[0].kind == "execute"
    assert received[0].raw_input["command"] == "rm -rf build"


async def test_permission_request_denied(client_factory):
    client = client_factory()

    async def handler(req):
        return False

    client.set_permission_handler(handler)
    assert (await client.send_message("permission")).text == "outcome=selected:deny"


async def test_permission_without_handler_or_failing_handler_denies(client_factory):
    client = client_factory()
    assert (await client.send_message("permission")).text == "outcome=selected:deny"

    async def broken(req):
        raise RuntimeError("boom")

    client.set_permission_handler(broken)
    assert (await client.send_message("permission")).text == "outcome=selected:deny"


async def test_permission_timeout_denies(client_factory):
    client = client_factory(XENIO_PERMISSION_TIMEOUT="5")

    async def never(req):
        await asyncio.sleep(60)
        return True

    client.set_permission_handler(never)
    assert (await client.send_message("permission")).text == "outcome=selected:deny"


async def test_cancel_stops_turn(client_factory):
    client = client_factory()
    turn = asyncio.ensure_future(client.send_message("slow"))
    await asyncio.sleep(0.5)
    await client.cancel()
    reply = await asyncio.wait_for(turn, 10)
    assert reply.cancelled
    assert reply.text == "Working"


async def test_crash_mid_turn_raises_unavailable_then_recovers(client_factory):
    client = client_factory()
    with pytest.raises(HermesUnavailable):
        await client.send_message("crash")
    assert not client.connected
    reply = await client.send_message("hello")  # restarts Hermes transparently
    assert reply.text == "Hello!"


async def test_missing_executable_is_unavailable(client_factory):
    client = client_factory(HERMES_COMMAND="definitely-not-a-real-hermes-binary acp")
    with pytest.raises(HermesUnavailable, match="not found"):
        await client.start()
    assert await client.health_check() is False


async def test_refusal_without_text_raises(client_factory):
    with pytest.raises(HermesError):
        await client_factory().send_message("refuse")


async def test_session_resume_from_saved_id(client_factory, make_settings, tmp_path):
    settings = make_settings()
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"session_id": "known-session", "workspace": str(settings.hermes.workspace),
                                        "policy_version": POLICY_VERSION, "prompt": PROMPT}))
    client = client_factory(session_file=session_file)
    await client.start()
    assert client.session_id == "known-session"
    reply = await client.send_message("hello")
    assert reply.text == "Hello!"  # the replayed history chunk was not mixed in


async def test_session_lost_after_resume_is_recovered_transparently(client_factory, make_settings, tmp_path):
    """Live bug: session/load succeeded, then Hermes answered the first prompt with "refusal" (session not
    found). XENIO starts a new session and sends the request again instead of failing."""
    settings = make_settings()
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"session_id": "forgotten-session", "workspace": str(settings.hermes.workspace),
                                        "policy_version": POLICY_VERSION, "prompt": PROMPT}))
    client = client_factory(session_file=session_file)
    await client.start()
    assert client.session_id == "forgotten-session"
    reply = await client.send_message("hello")
    assert reply.text == "Hello!" and client.session_id == "new-session"
    assert json.loads(session_file.read_text())["session_id"] == "new-session"
    await client.close()


HERMES_CONFIG = """model:
  # Default model
  default: "{model}"
  provider: "openai-codex"
agent:
  reasoning_effort: low
fallback_providers:
  - provider: openrouter
    model: thinkingmachines/inkling:free
  - provider: gemini
    model: gemini-3.8-flash
"""


async def test_saved_session_is_not_resumed_after_the_model_changed(client_factory, make_settings, tmp_path):
    """Hermes keeps the model a session was created with, so a new model needs a new session."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(HERMES_CONFIG.format(model="gpt-6-luna"), encoding="utf-8")
    settings = make_settings()
    session_file = tmp_path / "session.json"
    first = client_factory(session_file=session_file, hermes_home=home)
    await first.start()
    saved = json.loads(session_file.read_text())
    assert saved["model"].startswith("openai-codex:gpt-6-luna|low|openrouter:thinkingmachines/inkling:free")
    # same model: the saved conversation is resumed
    saved["session_id"] = "known-session"
    session_file.write_text(json.dumps(saved))
    same = client_factory(session_file=session_file, hermes_home=home)
    await same.start()
    assert same.session_id == "known-session"
    # the user switched models (hermes model): start fresh instead of keeping the old model
    (home / "config.yaml").write_text(HERMES_CONFIG.format(model="gpt-5.6-luna"), encoding="utf-8")
    changed = client_factory(session_file=session_file, hermes_home=home)
    await changed.start()
    assert changed.session_id == "new-session"
    assert settings  # (settings only build the fake agent command)


def test_read_model_setup(tmp_path):
    from app.hermes.model_info import read_model_setup

    assert read_model_setup(tmp_path).signature == ""  # no config: nothing known
    (tmp_path / "config.yaml").write_text(HERMES_CONFIG.format(model="gpt-6-luna").replace("\n", "\r\n"), encoding="utf-8")
    setup = read_model_setup(tmp_path)
    assert (setup.provider, setup.model, setup.reasoning_effort) == ("openai-codex", "gpt-6-luna", "low")
    assert setup.fallbacks == (("openrouter", "thinkingmachines/inkling:free"), ("gemini", "gemini-3.8-flash"))
    assert "if it fails or hits a limit: thinkingmachines/inkling:free via openrouter" in setup.describe()


async def test_unknown_saved_session_falls_back_to_new(client_factory, make_settings, tmp_path):
    settings = make_settings()
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"session_id": "gone", "workspace": str(settings.hermes.workspace)}))
    client = client_factory(session_file=session_file)
    await client.start()
    assert client.session_id == "new-session"
    assert json.loads(session_file.read_text())["session_id"] == "new-session"


async def test_new_session(client_factory, tmp_path):
    session_file = tmp_path / "s.json"
    client = client_factory(session_file=session_file)
    await client.start()
    await client.new_session()
    assert client.session_id == "new-session"


async def test_failed_tool_calls_keep_hermes_error_text(client_factory):
    from app.orchestrator import describe_actions

    client = client_factory()
    await client.start()
    reply = await client.send_message("failing-tool")
    assert [(t.status, t.detail) for t in reply.tool_calls] == [("failed", "Error: HTTP 403 Forbidden")]
    assert describe_actions(reply.tool_calls)[1] == "   [FAILED] web_extract: https://x.example - Error: HTTP 403 Forbidden"
    await client.close()


@pytest.mark.parametrize("show", [False, True])
async def test_hermes_child_environment(make_settings, tmp_path, show):
    seen = {}

    class Capture:
        def __init__(self, command, cwd, env, on_notification, on_request):
            seen.update(env)

        async def start(self):
            raise HermesUnavailable("captured")

    settings = make_settings(XENIO_SHOW_BROWSER="1" if show else "0", XENIO_DOWNLOAD_MAX_MB="50")
    client = HermesAcpClient(settings.hermes, session_file=tmp_path / "s.json", connection_factory=Capture)
    with pytest.raises(HermesUnavailable):
        await client.start()
    assert seen["XENIO_GUARD"] == "1" and seen["XENIO_TOOL_SEARCH"] == "off" and seen["XENIO_DOWNLOAD_MAX_MB"] == "50"
    assert seen["XENIO_DATA_DIR"] == str(tmp_path)
    assert ("AGENT_BROWSER_HEADED" in seen) is show  # Hermes' own browser stays hidden unless asked


def test_pick_option_never_grants_standing_permission():
    options = [
        {"optionId": "allow_session", "kind": "allow_always"},
        {"optionId": "allow_once", "kind": "allow_once"},
        {"optionId": "deny", "kind": "reject_once"},
    ]
    assert _pick_option(options, True) == "allow_once"
    assert _pick_option(options, False) == "deny"
    assert _pick_option([{"optionId": "x", "kind": "allow_always"}], True) is None
