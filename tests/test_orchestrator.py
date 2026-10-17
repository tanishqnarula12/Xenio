import asyncio
from pathlib import Path

from app.hermes.client import (
    HermesClient,
    HermesError,
    HermesPermissionRequest,
    HermesReply,
    HermesTimeout,
    HermesUnavailable,
)
from app.orchestrator import (
    MSG_EMPTY_REPLY,
    MSG_HERMES_ERROR,
    MSG_HERMES_TIMEOUT,
    MSG_HERMES_UNAVAILABLE,
    Orchestrator,
)
from app.safety.permissions import PermissionGate


class FakeHermes(HermesClient):
    def __init__(self, reply=None, error=None, delay=0.0):
        self.reply = reply or HermesReply("Hello. How can I help?")
        self.error = error
        self.delay = delay
        self.messages = []
        self.cancelled = 0
        self.handler = None
        self.agent_info = {"name": "fake", "version": "1"}

    async def start(self):
        if isinstance(self.error, HermesUnavailable):
            raise self.error

    async def send_message(self, message, on_chunk=None):
        self.messages.append(message)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        if on_chunk:
            on_chunk(self.reply.text)
        return self.reply

    async def cancel(self):
        self.cancelled += 1

    async def new_session(self):
        pass

    async def health_check(self):
        return self.error is None

    async def close(self):
        pass

    def set_permission_handler(self, handler):
        self.handler = handler


class AlwaysYes:
    def __init__(self):
        self.asked = []

    async def confirm(self, request, *, strict):
        self.asked.append((request, strict))
        return True


def make(hermes, gate=None):
    return Orchestrator(hermes, gate, Path("C:/xenio-workspace"))


async def test_process_text_returns_hermes_reply():
    hermes = FakeHermes()
    chunks = []
    response = await make(hermes).process_text("  hello  ", on_chunk=chunks.append)
    assert response.ok and response.text == "Hello. How can I help?"
    assert hermes.messages == ["hello"]
    assert chunks == ["Hello. How can I help?"]


async def test_empty_input_never_reaches_hermes():
    hermes = FakeHermes()
    response = await make(hermes).process_text("   ")
    assert not response.ok and response.error == "empty_input"
    assert hermes.messages == []


async def test_error_mapping():
    cases = [
        (HermesUnavailable("down"), MSG_HERMES_UNAVAILABLE, "hermes_unavailable"),
        (HermesTimeout("slow"), MSG_HERMES_TIMEOUT, "hermes_timeout"),
        (HermesError("odd"), MSG_HERMES_ERROR, "hermes_error"),
    ]
    for error, message, code in cases:
        response = await make(FakeHermes(error=error)).process_text("hi")
        assert (response.ok, response.text, response.error) == (False, message, code)


async def test_empty_reply_and_cancelled_reply():
    response = await make(FakeHermes(HermesReply(""))).process_text("hi")
    assert response.text == MSG_EMPTY_REPLY and not response.ok
    response = await make(FakeHermes(HermesReply("partial", stop_reason="cancelled"))).process_text("hi")
    assert response.cancelled and response.ok


async def test_start_reports_unavailable():
    assert await make(FakeHermes(error=HermesUnavailable("x"))).start() is False
    assert await make(FakeHermes()).start() is True


async def test_turns_are_serialized_and_interrupt_cancels():
    hermes = FakeHermes(delay=0.2)
    orch = make(hermes)
    first = asyncio.ensure_future(orch.process_text("one"))
    await asyncio.sleep(0.05)
    assert orch.busy
    await orch.interrupt()
    assert hermes.cancelled == 1
    await first
    await orch.interrupt()  # idle: nothing to cancel
    assert hermes.cancelled == 1


async def test_hermes_permission_goes_through_gate():
    hermes = FakeHermes()
    confirmer = AlwaysYes()
    make(hermes, PermissionGate(confirmer))
    approved = await hermes.handler(
        HermesPermissionRequest("git push", "execute", raw_input={"command": "git push origin main"})
    )
    assert approved is True
    request, strict = confirmer.asked[0]
    assert request.action == "terminal.git_push" and request.source == "hermes" and strict is False


async def test_hermes_permission_without_gate_denies():
    hermes = FakeHermes()
    make(hermes, None)
    assert await hermes.handler(HermesPermissionRequest("x", "execute")) is False


class AlwaysNo:
    async def confirm(self, request, *, strict):
        return False


class AskingHermes(FakeHermes):
    """Asks for a git push during the turn, like Hermes does over ACP."""

    async def send_message(self, message, on_chunk=None):
        await self.handler(HermesPermissionRequest("git push", "execute", raw_input={"command": "git push origin main"}))
        return HermesReply("The push was not done because you said no.")


async def test_declined_actions_are_listed_under_the_reply():
    orch = make(AskingHermes(), PermissionGate(AlwaysNo()))
    response = await orch.process_text("push it")
    assert len(response.declined) == 1 and "push" in response.declined[0]
    assert response.action_lines()[:2] == ["   actions:", f"   [declined] {response.declined[0]}"]
    again = await make(FakeHermes(), PermissionGate(AlwaysNo())).process_text("hello")
    assert again.declined == [] and again.action_lines() == []  # nothing carried over to other turns


async def test_silence_is_reported_as_no_answer_not_as_a_decline():
    """Live: the user said nothing to a prompt; XENIO cancelled (right) but listed "the user declined" (wrong)."""
    from app.safety.permissions import NO_CLEAR_ANSWER

    class Silent:
        async def confirm(self, request, *, strict):
            return NO_CLEAR_ANSWER

    response = await make(AskingHermes(), PermissionGate(Silent())).process_text("push it")
    assert response.declined[0].endswith("(no clear answer, so it was cancelled)")

    class Truthy:  # anything but an explicit True is a no
        async def confirm(self, request, *, strict):
            return "yes"

    assert "declined" in (await make(AskingHermes(), PermissionGate(Truthy())).process_text("push it")).declined[0]


def test_truth_check_corrects_replies_that_contradict_xenios_records():
    from app.hermes.client import ToolActivity
    from app.orchestrator import truth_check

    failed = [ToolActivity("t1", "terminal: git push", "execute", "failed", "fatal: no upstream branch")]
    # the live case: the user said yes, git failed, the model blamed the user's "no" -> a note is added
    note, replace = truth_check("The git push was not done because you declined this action.", failed, [], ["push code"])
    assert "you said yes" in note and "fatal: no upstream branch" in note and not replace
    # a real decline is not "corrected"; nor is talk about an earlier turn when nothing was approved now
    assert truth_check("Not done because you declined.", [], ["push code (the user declined)"], []) == ("", False)
    assert truth_check("Earlier you declined the push.", [], [], []) == ("", False)
    # V4: claiming success when nothing succeeded -> the reply is REPLACED by the tool's real result
    assert truth_check("I've pushed the branch successfully.", failed, [], []) == (
        "The operation failed: fatal: no upstream branch.", True)
    assert truth_check("Done.", failed, [], []) == ("The operation failed: fatal: no upstream branch.", True)
    assert truth_check("The file has been deleted.", [], ["delete x (the user declined)"], []) == (
        "That was not done: delete x (the user declined).", True)
    done = [ToolActivity("t2", "terminal: git push", "execute", "completed")]
    assert truth_check("I've pushed the branch successfully.", done, [], ["push code"]) == ("", False)
    # the tool succeeded but the reply says it failed -> correction note (same kind of action only)
    note, replace = truth_check("Sorry, I couldn't push the branch.", done, [], [])
    assert "reported success: terminal: git push" in note and not replace
    reads = [ToolActivity("t3", "web_search: crm docs", "fetch", "completed")]
    assert truth_check("I couldn't download the file.", reads, [], []) == ("", False)  # unrelated success: no claim


class ApprovingHermes(FakeHermes):
    async def send_message(self, message, on_chunk=None):
        await self.handler(HermesPermissionRequest("git push", "execute", raw_input={"command": "git push origin main"}))
        from app.hermes.client import ToolActivity
        return HermesReply("The push was not done because you declined it.",
                           tool_calls=[ToolActivity("t1", "terminal: git push", "execute", "failed", "fatal: no upstream")])


async def test_orchestrator_appends_the_correction_to_the_reply():
    response = await make(ApprovingHermes(), PermissionGate(AlwaysYes())).process_text("push it")
    assert response.text.startswith("The push was not done") and "XENIO correction: you said yes" in response.text


class FalselyDoneHermes(FakeHermes):
    async def send_message(self, message, on_chunk=None):
        from app.hermes.client import ToolActivity
        return HermesReply("Done! I've created the event.", tool_calls=[
            ToolActivity("t1", "xenio_calendar_create_event", "other", "failed", "Google refused the request (HTTP 403)")])


async def test_a_false_success_claim_is_replaced_before_it_is_spoken():
    """Phase 17: the tool result is the source of truth - "Done" after a failed tool never reaches the user."""
    response = await make(FalselyDoneHermes(), None).process_text("add lunch to my calendar")
    assert response.text == "The operation failed: Google refused the request (HTTP 403)."
    assert "Done" not in response.text and response.ok
