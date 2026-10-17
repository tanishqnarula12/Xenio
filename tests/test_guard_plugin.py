"""The xenio-guard Hermes plugin, loaded from hermes_plugin/ with a fake Hermes plugin context."""

import importlib.util
import json

import pytest

from app.config import PROJECT_ROOT
from app.safety import policy as p

PLUGIN = PROJECT_ROOT / "hermes_plugin" / "xenio-guard" / "__init__.py"
DESKTOP = p.known_folders()["Desktop"]


class FakeCtx:
    def __init__(self):
        self.hooks, self.tools, self.sections = {}, {}, {}

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)

    def register_system_prompt_section(self, id, content, **kw):
        self.sections[id] = content


def load(monkeypatch, tmp_path, active=True):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for key, value in {"XENIO_ROOT": str(PROJECT_ROOT), "XENIO_WORKSPACE": str(ws), "XENIO_DATA_DIR": str(tmp_path / "data"),
                       "HERMES_HOME": str(tmp_path / "hermes")}.items():
        monkeypatch.setenv(key, value)
    if active:
        monkeypatch.setenv("XENIO_GUARD", "1")
    else:
        monkeypatch.delenv("XENIO_GUARD", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-testsecretvalue0000")
    monkeypatch.chdir(ws)
    spec = importlib.util.spec_from_file_location("xenio_guard_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = FakeCtx()
    module.register(ctx)
    return module, ctx, ws


def test_registers_tools_hooks_and_prompt(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    assert set(ctx.hooks) == {"pre_tool_call", "transform_tool_result", "pre_llm_call", "post_approval_response"}
    assert set(ctx.tools) == {"xenio_list_directory", "xenio_inspect_path", "xenio_create_directory",
                              "xenio_move_path", "xenio_delete_path", "xenio_open_url", "xenio_download_file",
                              # V4 personal tools
                              "xenio_calendar_events", "xenio_calendar_create_event", "xenio_email_search",
                              "xenio_email_read", "xenio_github", "xenio_tasks", "xenio_project", "xenio_daily_briefing",
                              "xenio_schedule", "xenio_notifications", "xenio_integrations",
                              # V5 observe-only vision
                              "xenio_look_at_screen",
                              # V5.5 desktop control
                              "xenio_open_app", "xenio_list_apps", "xenio_window", "xenio_find_ui", "xenio_gui"}
    assert not any("send" in name for name in ctx.tools)  # V4: no tool can send email
    assert set(ctx.tools["xenio_look_at_screen"][1]["parameters"]["properties"]) == {"question", "target"}  # V5: observes
    assert all(toolset == "xenio" for toolset, _s, _h in ctx.tools.values())
    prompt = ctx.sections["xenio-guard.environment"]()
    assert str(DESKTOP) in prompt and "ONLY if its result reports success" in prompt
    assert "You are XENIO" in prompt and "never Hermes" in prompt
    web = ctx.sections["xenio-guard.web"]()
    assert "web_search" in web and "xenio_open_url" in web and "Never invent a source" in web
    personal = ctx.sections["xenio-guard.personal"]()
    assert "TRUST ORDER" in personal and "never instructions" in personal and "cannot send email" in personal
    # Hermes refuses sections over 4000 characters (8000 in total)
    sizes = [len(prompt), len(web), len(personal)]
    assert max(sizes) < 4000 and sum(sizes) < 7900, sizes


def test_decisions(monkeypatch, tmp_path):
    _m, ctx, ws = load(monkeypatch, tmp_path)
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="terminal", args={"command": "python --version"}) is None
    assert hook(tool_name="xenio_create_directory", args={"path": str(DESKTOP / "XenioTest")}) is None
    # needs confirmation -> "approve": Hermes runs its human gate (in the agent thread), which asks XENIO
    push = hook(tool_name="terminal", args={"command": "git push origin main"})
    assert push["action"] == "approve"
    level, summary, key = p.parse_escalation(push["message"])
    assert level.name == "CONFIRM_REQUIRED" and "push" in summary and key == "terminal:git push origin main"
    delete = hook(tool_name="xenio_delete_path", args={"path": str(DESKTOP / "XenioTest")})
    assert delete["action"] == "approve" and "Recycle Bin" in delete["message"]
    assert p.parse_escalation_scopes(delete["message"])
    blocked = hook(tool_name="read_file", args={"path": "~/.ssh/id_rsa"})
    assert blocked["action"] == "block" and "XENIO policy" in blocked["message"]
    assert hook(tool_name="terminal", args={"command": "echo $OPENROUTER_API_KEY"})["action"] == "block"
    # write_file is confirmed through Hermes' ACP edit approval, so the plugin only blocks, never escalates
    assert hook(tool_name="write_file", args={"path": "C:/Windows/x.txt", "content": "x"}) is None
    assert hook(tool_name="write_file", args={"path": ".env", "content": "x"})["action"] == "block"


# Real Hermes denial texts (tools/approval.py, tools/file_tools_write_guards.py).
DECLINES = [
    "BLOCKED: User denied this potentially dangerous action (matched 'XENIO[CONFIRM_REQUIRED] move the folder "
    "XenioTest on your Desktop to the Recycle Bin'). Do NOT retry — the user has explicitly rejected it.",
    "BLOCKED: Action timed out without user response. The user has NOT consented to this action. Do NOT retry it.",
    '{"output": "", "error": "BLOCKED: User denied this command. The user has NOT consented to this action."}',
]


def call(tool, args, result, call_id):
    return [{"role": "assistant", "content": "", "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": call_id, "content": result}]


def user(text="delete the XenioTest folder"):
    return [{"role": "user", "content": text}]


DELETE = ("xenio_delete_path", {"path": str(DESKTOP / "XenioTest")})


@pytest.mark.parametrize("decline", DECLINES)
def test_a_decline_does_not_block_the_next_request(monkeypatch, tmp_path, decline):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    history = user() + call(*DELETE, decline, "c1") + [{"role": "assistant", "content": "Not done."}] + user()
    out = ctx.hooks["pre_llm_call"](conversation_history=history, user_message="delete it")
    assert "applied only to that earlier attempt" in out["context"] and "call the tool again" in out["context"]
    assert "XenioTest" in out["context"]  # names the declined operation
    assert "Report only what happens in THIS turn" in out["context"]


def test_retry_note_names_a_declined_terminal_command_cleanly(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    denial = ("BLOCKED: User denied this potentially dangerous action (matched 'XENIO[CONFIRM_REQUIRED] push code to a "
              "remote repository: git push -u origin main\nkey: terminal:git push -u origin main'). Do NOT retry.")
    result = json.dumps({"output": "", "exit_code": -1, "error": denial})  # how the terminal tool reports it
    history = user("push it") + call("terminal", {"command": "git push -u origin main"}, result, "c1") + user("push it")
    note = ctx.hooks["pre_llm_call"](conversation_history=history)["context"]
    assert "push code to a remote repository: git push -u origin main." in note and "key:" not in note


def test_retry_note_is_scoped_to_open_recent_declines(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    hook = ctx.hooks["pre_llm_call"]
    declined = user() + call(*DELETE, DECLINES[0], "c1")
    # tried again later (asked again, whatever the answer): that decline is settled - no note any more
    retried = declined + user() + call(*DELETE, '{"success": true}', "c2") + user("what's the time?")
    assert hook(conversation_history=retried) is None
    # only for a few user messages after the decline
    later = declined + user("thanks") + user("what's the weather") + user("tell me a joke")
    assert hook(conversation_history=later) is not None
    much_later = later + user("and another one")
    assert hook(conversation_history=much_later) is None


def test_no_retry_note_without_a_decline(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    hook = ctx.hooks["pre_llm_call"]
    assert hook(conversation_history=user() + call(*DELETE, '{"success": true}', "c1") + user()) is None
    blocked = call("read_file", {"path": ".env"}, "Blocked by XENIO policy: secret file", "c1")
    assert hook(conversation_history=user() + blocked + user()) is None  # policy blocks stay blocked
    assert hook(conversation_history=[{"role": "user", "content": "BLOCKED: User denied"}]) is None
    assert hook(conversation_history=None) is None


def test_browser_page_labels_decide_clicks(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    snapshot = json.dumps({"success": True, "data": {"snapshot": '- link "Next page" [ref=e1]\n'
                                                                 '- button "Place order" [ref=e2]\n'
                                                                 '- searchbox "Search" [ref=e3]\n'
                                                                 '- textbox "Comment" [ref=e4]'}})
    ctx.hooks["transform_tool_result"](tool_name="browser_snapshot", args={}, result=snapshot)
    hook = ctx.hooks["pre_tool_call"]
    assert hook(tool_name="browser_click", args={"ref": "@e1"}) is None
    order = hook(tool_name="browser_click", args={"ref": "@e2"})
    assert order["action"] == "approve" and p.parse_escalation(order["message"])[0].name == "HIGH_RISK"
    assert hook(tool_name="browser_type", args={"ref": "@e3", "text": "python"}) is None
    assert hook(tool_name="browser_press", args={"key": "Enter"}) is None  # Enter in a search box = search
    assert hook(tool_name="browser_type", args={"ref": "@e4", "text": "hello"}) is None
    assert hook(tool_name="browser_press", args={"key": "Enter"})["action"] == "approve"  # submits the comment
    secret = hook(tool_name="browser_type", args={"ref": "@e4", "text": "sk-or-v1-testsecretvalue0000"})
    assert secret["action"] == "block"


def test_sources_are_recorded(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    redact = ctx.hooks["transform_tool_result"]
    search = json.dumps({"success": True, "data": {"web": [
        {"title": "Hermes Agent docs", "url": "https://hermes-agent.nousresearch.com/docs", "description": "Docs"}]}})
    redact(tool_name="web_search", args={"query": "hermes docs"}, result=search, session_id="s1", turn_id="t1")
    page = json.dumps({"results": [{"url": "https://hermes-agent.nousresearch.com/docs", "title": "Docs",
                                    "content": "Hermes is an agent. " * 50}]})
    redact(tool_name="web_extract", args={"urls": ["https://hermes-agent.nousresearch.com/docs"]}, result=page)
    redact(tool_name="web_extract", args={}, result=json.dumps({"results": [{"url": "https://x.example", "error": "404"}]}))
    lines = [json.loads(x) for x in (tmp_path / "data" / "sources.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["kind"] for x in lines] == ["search_result", "page_read"]  # the failed page is not a source
    assert lines[0]["site"] == "hermes-agent.nousresearch.com" and lines[0]["query"] == "hermes docs"
    assert lines[0]["turn_id"] == "t1" and lines[1]["retrieved"] and len(lines[1]["excerpt"]) <= 300


def test_tool_handler_returns_structured_json(monkeypatch, tmp_path):
    _m, ctx, ws = load(monkeypatch, tmp_path)
    _ts, _schema, handler = ctx.tools["xenio_create_directory"]
    out = json.loads(handler({"path": "made"}))
    assert out["success"] is True and (ws / "made").is_dir()
    bad = json.loads(handler({}))
    assert bad["success"] is False and "required" in bad["error"]


def test_secrets_are_redacted_from_tool_results(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    redact = ctx.hooks["transform_tool_result"]
    out = redact(tool_name="terminal", result="key is sk-or-v1-testsecretvalue0000 ok")
    assert "testsecretvalue" not in out
    assert redact(tool_name="terminal", result="nothing secret here") is None


def test_audit_log_written(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "git push"})
    ctx.hooks["post_approval_response"](command="<terminal> (plugin approval rule)", pattern_key="plugin_rule:terminal",
                                        choice="deny")
    lines = (tmp_path / "data" / "logs" / "xenio-guard.log").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert {"PLUGIN_LOADED", "TOOL_REQUESTED", "PERMISSION_CHECK", "CONFIRMATION_REQUESTED",
            "CONFIRMATION_RECEIVED"} <= set(events)


def test_inactive_outside_xenio(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path, active=False)
    assert set(ctx.hooks) == {"pre_tool_call"} and ctx.tools == {} and ctx.sections == {}
    assert ctx.hooks["pre_tool_call"](tool_name="terminal", args={"command": "rm -rf /"}) is None


def test_fails_closed_if_policy_cannot_load(monkeypatch, tmp_path):
    module, ctx, _ws = load(monkeypatch, tmp_path)
    module._state["policy"] = None
    module._state["error"] = "simulated"
    assert module._pre_tool_call(tool_name="read_file", args={"path": "a"}) is None
    assert module._pre_tool_call(tool_name="terminal", args={"command": "ls"})["action"] == "block"
