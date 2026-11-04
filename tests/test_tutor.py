"""The teacher in the chat: who gets it, what it is told, and that the user always gets a reply. Nabh is replaced; the routing was tuned on the user's real messages."""

import asyncio
from types import SimpleNamespace

import pytest

from app.hermes.client import HermesReply
from app.integrations.nabh import NabhError
from app.orchestrator import Orchestrator
from app.teacher.tutor import PROMPT, Tutor, language_rule, wants_teaching


class TestWhoGetsTheTeacher:
    @pytest.mark.parametrize("words", [
        "Hey! teach me rabin karp algorithm properly", "explain me P, NP, NP-Hard, NP-Complete problems in design and analysis of algorithm", "Solve SAT step-by-step", "what is recursion?",
        "why is the sky blue?", "how does a bill become a law in India?", "difference between TCP and UDP", "photosynthesis samjhao simple me", "recursion kya hota hai", "क्या है गुरुत्वाकर्षण",
        "can you explain binary search?", "Xenio, could you teach me DBMS normalisation please", "i wanted to learn dsa to crack good companies", "quiz me on the French Revolution", "What is 17 times 23?", "erkläre mir Rekursion",
    ])
    def test_someone_wanting_to_understand(self, words):
        assert wants_teaching(words)

    @pytest.mark.parametrize("words", [
        "open safari", "play some lofi on spotify", "What is on my Google calendar today?", "How many emails did I get today?", "What time is it in Tokyo right now?", "explain this error", "what is on the screen", "What did the engineering team find?",
        "Is it done?", "Is your engineering team working on anything right now?", "why vision model crashed!?", "will solve later", "leave it I il solve myself", "is listening to it.", "What's up? Good morning.", "Hi! In one short sentence, who are you?",
        "Why you talking in Hinglish!?", "generate an image of a red fox", "make a full lesson on gravity", "what is the latest stable Python release?", "fill this form", "hello", "", "How many items are in my Downloads folder?",
    ])
    def test_everything_else_stays_with_the_main_model(self, words):
        assert not wants_teaching(words)

    @pytest.mark.parametrize("words", ["in english bro", "why?", "explain me properly bro not in short!! like properly explain me!!", "B", "another example", "Show it as a diagram", "yes, go on", "What about this case?"])
    def test_a_follow_up_keeps_the_teacher(self, words):
        assert wants_teaching(words, taught_last=True)

    @pytest.mark.parametrize("words", ["open safari", "what is on my calendar", "and my notes?", "remind me at 5"])
    def test_a_command_after_a_lesson_is_still_a_command(self, words):
        assert not wants_teaching(words, taught_last=True)

    @pytest.mark.parametrize("words", ["tell me how can i improve my resume more!?", "summarise this", "what does it say about gravity?", "explain chapter 2", "look at this and give feedback"])
    def test_a_question_about_what_they_attached(self, words):
        assert wants_teaching(words, has_file=True)

    @pytest.mark.parametrize("words", ["send this to Rahul", "save it to my Desktop", "ok"])
    def test_other_things_done_with_a_file_stay_with_the_main_model(self, words):
        assert not wants_teaching(words, has_file=True)


class TestWhatItIsTold:
    def test_properly_not_briefly_and_never_making_things_up(self):
        for words in ("250 to 500 words", "worked example", "```mermaid", "Never invent", "never LaTeX", "Never ask a question out of nowhere", "<<next:", "Do not reveal"):
            assert words in PROMPT

    def test_none_of_the_voice_persona(self):
        assert "read aloud" not in PROMPT and "spoken" not in PROMPT

    @pytest.mark.parametrize("words,rule", [("explain P vs NP in design and analysis", "English"), ("photosynthesis samjhao simple me, exam hai kal", "Hinglish"), ("गुरुत्वाकर्षण क्या है", "Devanagari"), ("Erkläre mir bitte wie das funktioniert und warum", "German")])
    def test_it_answers_in_the_language_they_wrote_in(self, words, rule):
        assert rule in language_rule(words)

    def test_a_short_follow_up_keeps_the_language_unless_it_says_otherwise(self):
        assert "Hinglish" in language_rule("why?", "Reply in natural Hinglish")
        assert "English" in language_rule("in english bro please now")


class FakeClient:
    teacher_model = "writer"

    def __init__(self, text="Recursion is a function calling itself.\n\n<<next: Trace it | Quiz me>>", fail=None):
        self.text, self.fail, self.payloads = text, fail, []

    def stream_text(self, payload, timeout=0, on_piece=None):
        self.payloads.append(payload)
        if self.fail == "before":
            raise NabhError("rate_limit", "limited", 30)
        for i in range(0, len(self.text), 6):
            if on_piece(self.text[i:i + 6]) is False:
                return self.text[:i]
            if self.fail == "midway" and i >= 12:
                raise NabhError("network", "lost")
        return self.text


class Hermes:
    connected = True

    def __init__(self):
        self.messages, self.sessions = [], 0

    def set_permission_handler(self, handler):
        pass

    async def new_session(self):
        self.sessions += 1

    async def send_message(self, message, on_chunk=None, **kw):
        self.messages.append(message)
        on_chunk("from the main model")
        return HermesReply(text="from the main model")


def talk(tmp_path, *lines, client=None, context=None, fast=True):
    client = client if client is not None else FakeClient()
    hermes = Hermes()
    orch = Orchestrator(hermes, None, tmp_path, tutor=Tutor(nabh_factory=lambda: client))

    async def go():
        out = []
        for line in lines:
            pieces = []
            reply = await orch.process_text(line, on_chunk=pieces.append, context=context, fast=fast)
            out.append((reply, "".join(pieces)))
            await asyncio.sleep(0)
        return out

    return asyncio.run(go()), client, hermes, orch


class TestAReply:
    def test_it_streams_without_the_line_and_offers_buttons_and_no_main_model_turn(self, tmp_path):
        ((reply, streamed),), client, hermes, _ = talk(tmp_path, "what is recursion?")
        assert reply.text == "Recursion is a function calling itself." == streamed and reply.next == ["Trace it", "Quiz me"] and reply.ok
        assert hermes.messages == [] and client.payloads[0]["model"] == "writer"
        assert client.payloads[0]["messages"][0]["role"] == "system" and "superb teacher" in client.payloads[0]["messages"][0]["content"]

    def test_no_word_arrives_after_the_reply_has_ended(self):
        client, got = FakeClient(), []

        async def go():
            loop = asyncio.get_running_loop()
            real = loop.call_soon_threadsafe
            loop.call_soon_threadsafe = lambda cb, *a, **k: real(loop.call_later, 0.05, cb, *a) if cb.__name__ == "deliver" else real(cb, *a, **k)  # the loop gets to them late
            out = await Tutor(nabh_factory=lambda: client).answer("what is recursion?", [], None, got.append)
            return out, "".join(got)

        (written, stopped), at_the_end = asyncio.run(go())
        assert at_the_end == client.text == written and not stopped

    def test_a_follow_up_has_the_conversation(self, tmp_path):
        _, client, _, _ = talk(tmp_path, "what is recursion?", "in english bro")
        said = client.payloads[1]["messages"]
        assert [m["role"] for m in said] == ["system", "user", "assistant", "user"] and said[1]["content"] == "what is recursion?" and "calling itself" in said[2]["content"] and said[3]["content"] == "in english bro"

    def test_the_main_model_hears_what_was_taught(self, tmp_path):
        _, _, hermes, _ = talk(tmp_path, "what is recursion?", "open safari")
        assert "XENIO taught this itself" in hermes.messages[-1] and "what is recursion?" in hermes.messages[-1]

    def test_a_new_conversation_forgets(self, tmp_path):
        client = FakeClient()
        hermes = Hermes()
        orch = Orchestrator(hermes, None, tmp_path, tutor=Tutor(nabh_factory=lambda: client))

        async def go():
            await orch.process_text("what is recursion?", on_chunk=lambda w: None)
            await orch.new_session()
            await orch.process_text("what is gravity?", on_chunk=lambda w: None)

        asyncio.run(go())
        assert [m["role"] for m in client.payloads[1]["messages"]] == ["system", "user"]

    def test_what_they_attached_goes_with_the_question(self, tmp_path):
        note = "[XENIO: files] <<XENIO-FILE>>my resume<<END-XENIO-FILE>>"
        ((reply, _),), client, hermes, _ = talk(tmp_path, "tell me how can i improve my resume more!?", context=note, fast=False)
        assert "my resume" in client.payloads[0]["messages"][-1]["content"] and hermes.messages == [] and reply.ok

    def test_spoken_requests_stay_with_the_main_model(self, tmp_path):
        ((reply, _),), client, hermes, _ = talk(tmp_path, "what is recursion?", context="[XENIO voice mode: short]")
        assert reply.text == "from the main model" and client.payloads == [] and len(hermes.messages) == 1


class TestWhenItCannot:
    def test_nabh_limiting_means_the_main_model_answers(self, tmp_path):
        ((reply, streamed),), _, hermes, _ = talk(tmp_path, "what is recursion?", client=FakeClient(fail="before"))
        assert reply.text == "from the main model" == streamed and len(hermes.messages) == 1

    def test_no_key_means_the_main_model_answers(self, tmp_path):
        hermes = Hermes()
        orch = Orchestrator(hermes, None, tmp_path, tutor=Tutor(nabh_factory=lambda: None))
        reply = asyncio.run(orch.process_text("what is recursion?", on_chunk=lambda w: None))
        assert reply.text == "from the main model"

    def test_a_drop_midway_keeps_what_was_shown_and_says_so(self, tmp_path):
        ((reply, _),), _, hermes, _ = talk(tmp_path, "what is recursion?", client=FakeClient(fail="midway"))
        assert hermes.messages == [] and reply.text.startswith("Recursion is") and "dropped" in reply.text

    def test_stopping_stops(self, tmp_path):
        client = FakeClient()
        hermes = Hermes()
        tutor = Tutor(nabh_factory=lambda: client)
        orch = Orchestrator(hermes, None, tmp_path, tutor=tutor)
        pieces = []

        def stop_after_first(words):
            pieces.append(words)
            tutor.stop()

        reply = asyncio.run(orch.process_text("what is recursion?", on_chunk=stop_after_first))
        assert reply.cancelled is True and reply.next == [] and hermes.messages == []


class TestAskingForPractice:
    @pytest.mark.parametrize("text", ["give me mcq questions on binary search", "give me mcqs on recursion", "make a quiz about the krebs cycle", "5 practice questions on photosynthesis", "quiz me on binary search", "test me on recursion", "harder questions on recursion", "10 questions on the french revolution", "mock test on thermodynamics", "flashcards for organic chemistry", "multiple choice questions about gravity", "bro give me advanced mcq questions on dbms"])
    def test_reaches_the_teacher(self, text):
        assert wants_teaching(text)

    @pytest.mark.parametrize("text", ["build a quiz app", "build me a quiz website with a leaderboard", "create an mcq platform for my school", "code a quiz game in python", "make a quiz app", "implement a multiple choice api", "open my quiz results", "send the quiz to my friend"])
    def test_building_one_or_handling_one_does_not(self, text):
        assert not wants_teaching(text)


class TestWhatItIsToldAboutQuizzesAndDiagrams:
    def test_a_quiz_is_a_card_that_keeps_the_answer_hidden(self):
        for words in ("```quiz", '"answer": 0', "Do not reveal the answer", "Test understanding, not recall", "mistake real people make"):
            assert words in PROMPT

    def test_the_prompt_can_be_formatted(self):
        text = PROMPT.format(language="Reply in English.")
        assert '{"title": "Binary search"' in text and 'C{"lo <= hi?"}' in text and "Reply in English." in text

    def test_diagrams_are_told_to_show_structure(self):
        for words in ("subgraph", ":::key", ":::good", "mindmap", "at most one line of detail"):
            assert words in PROMPT


class TestWhenTheBestTeacherCannotAnswer:
    class Refusing:
        teacher_model = "writer"

        def __init__(self, refused, kind="quota"):
            self.refused, self.kind, self.asked = refused, kind, []

        def stream_text(self, payload, timeout=0, on_piece=None):
            self.asked.append(payload["model"])
            if payload["model"] in self.refused:
                raise NabhError(self.kind, "Credit balance is too low for this request.")
            on_piece("An answer.")
            return "An answer."

    def go(self, client):
        return asyncio.run(Tutor(nabh_factory=lambda: client).answer("what is recursion?", [], None, lambda p: None))

    def test_a_cheaper_teacher_takes_over(self):
        client = self.Refusing({"writer"})
        assert self.go(client) == ("An answer.", False) and client.asked[0] == "writer" and len(client.asked) == 2

    def test_only_when_all_of_them_refuse_does_the_main_model_take_it(self):
        from app.teacher.tutor import BACKUP_MODELS

        client = self.Refusing({"writer", *BACKUP_MODELS})
        assert self.go(client) is None and len(client.asked) == 1 + len(BACKUP_MODELS)

    def test_a_bad_key_is_not_tried_again_on_others(self):
        client = self.Refusing({"writer"}, "auth")
        assert self.go(client) is None and client.asked == ["writer"]

    def test_words_already_shown_are_not_followed_by_a_second_answer(self):
        class Drops:
            teacher_model = "writer"
            asked = 0

            def stream_text(self, payload, timeout=0, on_piece=None):
                Drops.asked += 1
                on_piece("Half an ")
                raise NabhError("network", "lost")

        out = asyncio.run(Tutor(nabh_factory=lambda: Drops()).answer("what is recursion?", [], None, lambda p: None))
        assert Drops.asked == 1 and out[0].startswith("Half an") and "dropped" in out[0]
