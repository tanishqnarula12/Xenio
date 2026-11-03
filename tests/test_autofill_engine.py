"""Filling forms, against a fake page shaped like what Safari and real Google Forms were measured to report: what goes in, what
stays out, and what is said about it. Values never reach a result."""

import json
import sys
import threading

import pytest

from app.autofill import forms
from app.autofill.engine import Autofill, mask_filled
from app.desktop.service import DesktopError
from app.personal.vault import ProfileVault
from app.safety import policy as p

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the vault's cipher is part of macOS")
SECRET_VALUES = ("asha", "verma", "90000", "example.com", "A1234567", "Pune")


class Key:
    key = b"k" * 32

    def get(self):
        return self.key

    def create(self):
        return self.key


class Window:
    hwnd, pid, app, process, foreground, minimized = 7, 70, "Safari", "Safari", True, False

    def __init__(self, title="Apply | Careers"):
        self.title = title


def text(i, title, what="text field", **kw):
    return {"i": i, "role": kw.pop("role", "AXTextField"), "sub": "", "what": what, "title": title, "desc": "", "value": kw.pop("value", ""),
            "placeholder": kw.pop("placeholder", ""), "dom_id": "", "rect": kw.pop("rect", [10, 10 + i * 40, 300, 30]), "enabled": kw.pop("enabled", True),
            "required": kw.pop("required", False), "invalid": False, "focused": False, "secure": kw.pop("secure", False), "hidden": kw.pop("hidden", False),
            "group": kw.pop("group", ""), "depth": kw.pop("depth", 5)}


def radio(i, name, group, value=0, role="AXRadioButton"):
    return {"i": i, "role": role, "sub": "", "what": "", "title": name, "desc": "", "value": value, "placeholder": "", "dom_id": "",
            "rect": [10, 10 + i * 40, 20, 20], "enabled": True, "required": False, "invalid": False, "focused": False, "secure": False, "hidden": False,
            "group": group, "depth": 6}


def button(i, name):
    return {"i": i, "role": "AXButton", "title": name, "desc": "", "rect": [10, 900 + i, 80, 30], "enabled": True, "hidden": False, "depth": 4}


def heading(words):
    return {"role": "AXHeading", "text": words, "rect": [0, 0, 100, 20], "hidden": False, "depth": 4}


class Page:
    """A page: its controls by index, what each holds, and how it behaves."""

    def __init__(self, nodes, next_nodes=None, title="Apply | Careers", refuses=(), mangles=None, no_press=(), after_submit=None, stop=None):
        self.nodes, self.next_nodes, self.title = nodes, next_nodes, title
        self.values = {n["i"]: n.get("value") for n in nodes if "i" in n}
        self.refuses, self.mangles, self.no_press = set(refuses), mangles or {}, set(no_press)
        self.after_submit, self.stop = after_submit, stop
        self.calls = []
        self.token = 0
        self.keyboard = False

    def form_scan(self, hwnd):
        self.token += 1
        return {"token": self.token, "nodes": [dict(n, value=self.values.get(n.get("i"), n.get("value"))) if "i" in n else n for n in self.nodes],
                "complete": True, "web_area": [0, 0, 800, 900], "url": "https://careers.test/apply", "count": len(self.nodes), "pid": 70, "ms": 12}

    def _control(self, index):
        return next(n for n in self.nodes if n.get("i") == index)

    def form_set(self, token, index, value, blur=False):
        self.calls.append(("set", index))
        if self.stop and len([c for c in self.calls if c[0] == "set"]) == self.stop[0]:
            self.stop[1]()
        if index in self.refuses:
            return {"focused": True, "accepted": True, "value": ""}
        self.values[index] = self.mangles.get(index, value)
        return {"focused": True, "accepted": True, "value": self.values[index]}

    def form_read(self, token, index):
        return {"alive": True, "value": self.values.get(index), "invalid": False}

    def form_press(self, token, index):
        self.calls.append(("press", index))
        control = self._control(index)
        if index in self.no_press:
            return {"pressed": False, "can_press": False, "value": self.values.get(index)}
        if control["role"] == "AXRadioButton":
            for n in self.nodes:
                if n.get("role") == "AXRadioButton" and n.get("group") == control.get("group"):
                    self.values[n["i"]] = 0
            self.values[index] = 1
        elif control["role"] == "AXCheckBox":
            self.values[index] = 0 if self.values.get(index) else 1
        elif control["role"] == "AXButton":
            name = control["title"]
            if self.next_nodes is not None and name == "Next":
                self.nodes, self.next_nodes = self.next_nodes, None
                self.values = {n["i"]: n.get("value") for n in self.nodes if "i" in n}
            elif self.after_submit is not None and name != "Next":
                self.nodes = self.after_submit
        return {"pressed": True, "can_press": True, "value": self.values.get(index)}

    def form_space(self, token, index):
        self.calls.append(("space", index))
        control = self._control(index)
        if control["role"] == "AXRadioButton":
            for n in self.nodes:
                if n.get("role") == "AXRadioButton" and n.get("group") == control.get("group"):
                    self.values[n["i"]] = 0
            self.values[index] = 1
        else:
            self.values[index] = 0 if self.values.get(index) else 1
        return {"focused": True, "value": self.values[index]}

    def form_type_ahead(self, token, index, value):
        self.calls.append(("type_ahead", index))
        options = self._control(index).get("list", [])
        match = next((o for o in options if o.lower().startswith(str(value).lower())), None)
        if match:
            self.values[index] = match
        return {"focused": True, "value": self.values.get(index)}

    def form_date(self, token, index, year, month, day):
        self.calls.append(("date", index))
        self.values[index] = f"{year}-{month:02d}-{day:02d}"
        return {"parts": ["day", "month", "year"], "verified": True}

    def form_is_focused(self, token, index):
        return {"focused": self.keyboard}

    def focused(self, with_value=False):
        return {"found": False}


class Windows:
    def __init__(self, window):
        self.window = window

    def foreground_window(self):
        return self.window

    def session_locked(self):
        return False

    def focus(self, hwnd):
        return True


class Desktop:
    _data_dir = None

    def __init__(self, page, window=None):
        self.uia, self.window = page, window or Window(page.title)
        self.windows = Windows(self.window)
        self._input = threading.Lock()
        self._running = None
        self._generation = {}
        self.is_stopped = False
        self.keys = []

    def pick_window(self, args, session=""):
        return self.window

    def stopped(self):
        return self.is_stopped

    def keep_awake(self):
        pass

    def publish(self, *a, **k):
        pass

    def _controller_for(self, *a, **k):
        class C:
            def __init__(me):
                me.outer = self

            def insert_text(me, value):
                self.keys.append(value)
                self.uia.values[self.uia.typing_into] = value

            def click(me, x, y):
                pass
        return C()


def Vault():
    import tempfile
    from pathlib import Path

    return ProfileVault(Path(tempfile.mkdtemp()) / "v.db", Key())


@pytest.fixture
def vault(tmp_path):
    v = ProfileVault(tmp_path / "v.db", Key())
    v.add("identity", "First name", "Asha", field="first_name")
    v.add("identity", "Last name", "Verma", field="last_name")
    v.add("identity", "Email", "asha@example.com", field="email")
    v.add("identity", "Mobile", "+91 90000 00000", field="phone")
    v.add("identity", "Gender", "Female", field="gender")
    v.add("address", "City", "Pune")
    return v


def engine(page, vault, tmp_path=None, **kw):
    return Autofill(Desktop(page, **kw), vault, tmp_path)


BASIC = [heading("Apply for Engineer"), text(0, "First name *", required=True), text(1, "Email address", "email field", required=True),
         text(2, "Mobile", "telephone number field"), button(90, "Next"), button(91, "Submit application")]


def no_values(result):
    blob = json.dumps(result, ensure_ascii=False).lower()
    for value in SECRET_VALUES:
        assert value.lower() not in blob, value


class TestFilling:
    def test_everything_certain_goes_in_and_is_checked(self, vault):
        page = Page(list(BASIC))
        out = engine(page, vault).fill({}, "s")
        assert {f["label"] for f in out["filled"]} == {"First name", "Email address", "Mobile"}
        assert page.values[0] == "Asha" and page.values[1] == "asha@example.com" and page.values[2] == "+91 90000 00000"
        assert out["verified"] is True and out["submitted"] is False and "Nothing has been submitted" in out["say"]

    def test_no_value_is_in_any_result(self, vault, tmp_path):
        page = Page(list(BASIC))
        e = engine(page, vault, tmp_path)
        no_values(e.scan({}, "s"))
        no_values(e.fill({}, "s"))
        no_values(e.review({}, "s"))
        no_values((tmp_path / "form_state.json").read_text())

    def test_what_the_user_typed_stays_and_is_said_so(self, vault):
        page = Page([text(0, "City", value="Typed by me")])
        out = engine(page, vault).fill({}, "s")
        assert page.values[0] == "Typed by me" and out["left"][0]["status"] == "keep" and not out["filled"]

    def test_replaced_when_the_user_says_so(self, vault):
        page = Page([text(0, "City", value="Typed by me")])
        out = engine(page, vault).fill({"replace": ["f1"]}, "s")
        assert page.values[0] == "Pune" and out["filled"]

    def test_the_same_value_already_there_is_no_work_and_no_complaint(self, vault):
        page = Page([text(0, "City", value="Pune")])
        out = engine(page, vault).fill({}, "s")
        assert not page.calls and not out["left"] and not out["not_filled"]

    @pytest.mark.parametrize("node", [text(0, "Home address", rect=[-9999, 40, 300, 30]), text(0, "City", rect=[10, 40, 1, 1]), text(0, "City", hidden=True)])
    def test_a_field_no_one_can_see_is_left_alone(self, vault, node):
        page = Page([node])
        out = engine(page, vault).fill({}, "s")
        assert not page.calls and out["left"][0]["status"] == "hidden"

    def test_a_field_other_controls_sit_on_is_left_alone(self, vault):
        on_top = text(0, "City", rect=[10, 100, 300, 30])
        page = Page([on_top, text(1, "Unrelated", rect=[10, 100, 300, 30])])
        out = engine(page, vault).fill({}, "s")
        assert ("set", 0) not in page.calls and any(i["status"] == "hidden" for i in out["left"])

    def test_passwords_are_never_filled(self, vault):
        page = Page([text(0, "Password", "secure text field", secure=True)])
        e = engine(page, vault)
        out = e.fill({}, "s")
        assert not page.calls and out["left"][0]["status"] == "never" and out["needs_login"] is True

    def test_a_disabled_field_is_said_so(self, vault):
        page = Page([text(0, "City", enabled=False)])
        out = engine(page, vault).fill({}, "s")
        assert out["left"][0]["status"] == "disabled" and not page.calls

    def test_a_missing_detail_is_not_a_failure(self, vault):
        page = Page([text(0, "College", required=True)])
        out = engine(page, vault).fill({}, "s")
        assert out["verified"] is True and out["questions"][0]["label"] == "College"
        assert "still need an answer" in out["say"]


class TestWrongMappings:
    def test_someone_elses_field_stays_empty(self, vault):
        page = Page([text(0, "Emergency contact phone", "telephone number field"), text(1, "Father's name")])
        engine(page, vault).fill({}, "s")
        assert not page.calls

    @staticmethod
    def two_emails(vault):
        vault.delete(next(x["id"] for x in vault.entries() if x["label"] == "Email"))
        vault.add("identity", "Personal email", "asha@example.com", field="email")
        vault.add("identity", "Work email", "asha@work.example.com", field="email")

    def test_a_detail_saved_under_the_fields_very_name_needs_no_question(self, vault):
        vault.add("identity", "Work email", "asha@work.example.com", field="email")
        page = Page([text(0, "Email")])
        assert engine(page, vault).fill({}, "s")["verified"] is True and page.values[0] == "asha@example.com"

    def test_two_candidates_are_a_question_and_nothing_is_guessed(self, vault):
        self.two_emails(vault)
        page = Page([text(0, "Email")])
        out = engine(page, vault).fill({}, "s")
        assert not page.calls and out["questions"][0]["status"] == "ask" and len(out["questions"][0]["candidates"]) == 2
        no_values(out["questions"])

    def test_the_users_choice_settles_it(self, vault):
        self.two_emails(vault)
        page = Page([text(0, "Email")])
        e = engine(page, vault)
        chosen = next(c["id"] for c in e.scan({}, "s")["fields"][0]["candidates"] if c["label"] == "Work email")
        out = e.fill({"use": {"f1": chosen}}, "s")
        assert page.values[0] == "asha@work.example.com" and out["filled"][0]["from"] == "Work email"

    def test_a_chosen_detail_that_does_not_fit_is_refused(self, vault):
        made = vault.add("other", "Nickname", "tiger")
        page = Page([text(0, "Email", "email field")])
        out = engine(page, vault).fill({"use": {"f1": made["id"]}}, "s")
        assert not page.calls and out["left"][0]["why"].startswith("that detail does not fit")

    def test_an_answer_passed_as_an_id_is_told_what_to_do_instead(self, vault):
        page = Page([text(0, "Branch")])
        out = engine(page, vault).fill({"use": {"f1": "CSE"}}, "s")
        assert not page.calls and "not the id of a saved detail" in out["left"][0]["why"]

    def test_an_unknown_field_id_is_said_not_ignored(self, vault):
        page = Page(list(BASIC))
        out = engine(page, vault).fill({"text": {"f99": "x"}}, "s")
        assert any(i["status"] == "not_found" and i["id"] == "f99" for i in out["left"])


class TestMadeValues:
    def test_a_number_box_month_is_checked_against_what_it_was_given(self, vault):
        vault.add("identity", "Date of birth", "2000-01-31", field="date_of_birth")
        nodes = [heading("Enter your birthday"), text(0, "Day of the month", "number field", group="Enter your birthday"),
                 text(1, "Month", "number field", group="Enter your birthday"), text(2, "Year", "number field", group="Enter your birthday")]
        page = Page(nodes)
        real = page.form_set

        def number_box(token, index, value, blur=False):  # a number box keeps 1 for 01
            out = real(token, index, str(int(value)) if str(value).isdigit() else value, blur)
            return out
        page.form_set = number_box
        out = engine(page, vault).fill({}, "s")
        assert out["verified"] is True and (page.values[0], page.values[1], page.values[2]) == ("31", "1", "2000")

    def test_a_native_date_gets_a_second_go_when_a_key_was_lost(self, vault):
        vault.add("identity", "Date of birth", "2000-01-31", field="date_of_birth")
        node = dict(text(0, "Date of birth", "date time area"), role="AXDateTimeArea")
        page = Page([node])
        tries = []

        def date(token, index, year, month, day):
            tries.append(1)
            return {"parts": ["day", "month", "year"], "verified": len(tries) > 1}
        page.form_date = date
        out = engine(page, vault).fill({}, "s")
        assert len(tries) == 2 and out["verified"] is True


class TestDeleted:
    def test_deleted_before_reading_is_not_offered(self, vault):
        for entry in vault.entries():
            if entry["label"] == "City":
                vault.delete(entry["id"])
        page = Page([text(0, "City")])
        out = engine(page, vault).fill({}, "s")
        assert not page.calls and out["left"]

    def test_deleted_after_it_was_read_cannot_be_filled(self, vault):
        page = Page([text(0, "City")])
        e = engine(page, vault)
        e.scan({}, "s")  # the page is read: City would go in
        vault.delete(next(x["id"] for x in vault.entries() if x["label"] == "City"))
        out = e.fill({}, "s")
        assert not page.calls and page.values[0] == "" and "no longer exists" in out["left"][0]["why"]

    def test_a_chosen_detail_deleted_since_is_said_so(self, vault):
        made = vault.add("identity", "Backup email", "b@example.com")
        vault.delete(made["id"])
        page = Page([text(0, "Email", "email field")])
        out = engine(page, vault).fill({"use": {"f1": made["id"]}}, "s")
        assert not page.calls and "no longer exists" in out["left"][0]["why"]

    def test_a_vault_that_cannot_be_read_says_so(self, vault):
        vault.keys = type("Locked", (), {"get": lambda s: None, "create": lambda s: None})()
        with pytest.raises(DesktopError, match="profile could not be read"):
            engine(Page(list(BASIC)), vault).scan({}, "s")


class TestRecovery:
    def test_a_field_that_does_not_take_it_is_reported_and_the_rest_goes_on(self, vault):
        page = Page(list(BASIC), refuses=[1])
        out = engine(page, vault).fill({}, "s")
        assert out["verified"] is False and [f["label"] for f in out["not_filled"]] == ["Email address"]
        assert {f["label"] for f in out["filled"]} == {"First name", "Mobile"} and "Could not enter" in out["say"]

    def test_then_real_keys_if_the_keyboard_is_really_in_that_field(self, vault):
        page = Page([text(0, "City")], refuses=[0])
        page.keyboard, page.typing_into = True, 0
        e = engine(page, vault)
        out = e.fill({}, "s")
        assert e.desktop.keys == ["Pune"] and out["filled"][0]["how"] == "keyboard"

    def test_no_keys_if_it_is_not(self, vault):
        page = Page([text(0, "City")], refuses=[0])
        e = engine(page, vault)
        out = e.fill({}, "s")
        assert not e.desktop.keys and out["not_filled"]

    def test_a_page_that_changes_the_value_is_not_believed(self, vault):
        page = Page([text(0, "City")], mangles={0: "Delhi"})
        out = engine(page, vault).fill({}, "s")
        assert out["not_filled"] and not out["filled"] and out["verified"] is False

    def test_a_phone_number_the_page_formats_is_the_same_number(self, vault):
        page = Page([text(0, "Mobile", "telephone number field")], mangles={0: "(900) 000-0000"})
        assert engine(page, vault).fill({}, "s")["verified"] is True

    def test_a_number_box_writes_01_as_1(self):
        from app.autofill import matching

        assert matching.same_value("01", "1", "number") and not matching.same_value("01", "2", "number")

    def test_a_control_that_throws_is_one_failure_not_the_end(self, vault):
        page = Page(list(BASIC))
        real = page.form_set

        def flaky(token, index, value, blur=False):
            if index == 1:
                raise RuntimeError("STALE: the form changed: read it again")
            return real(token, index, value, blur)
        page.form_set = flaky
        out = engine(page, vault).fill({}, "s")
        assert out["not_filled"][0]["why"] == "the page changed while it was being filled" and len(out["filled"]) == 2


class TestCancellation:
    def test_a_stop_between_fields_says_exactly_what_was_done(self, vault):
        holder = {}
        page = Page(list(BASIC), stop=(1, lambda: setattr(holder["e"].desktop, "is_stopped", True)))
        holder["e"] = engine(page, vault)
        out = holder["e"].fill({}, "s")
        assert out["stopped"] == "stopped" and out["verified"] is False
        assert [f["label"] for f in out["filled"]] == ["First name"]
        assert {i["label"] for i in out["left"] if i["status"] == "not_reached"} == {"Email address", "Mobile"}
        assert page.values[1] == "" and "the rest was not" in out["say"]

    def test_a_newer_request_ends_the_old_run(self, vault):
        holder = {}

        def newer():
            holder["e"].desktop._generation["s"] = 1
        page = Page(list(BASIC), stop=(1, newer))
        holder["e"] = engine(page, vault)
        assert holder["e"].fill({}, "s")["stopped"] == "superseded"

    def test_nothing_starts_after_a_stop(self, vault):
        e = engine(Page(list(BASIC)), vault)
        e.desktop.is_stopped = True
        with pytest.raises(DesktopError, match="stopped"):
            e.fill({}, "s")

    def test_two_at_once_are_refused_not_mixed(self, vault):
        e = engine(Page(list(BASIC)), vault)
        e.desktop._input.acquire()
        with pytest.raises(DesktopError, match="BUSY"):
            import app.autofill.engine as module

            module.BUSY_WAIT = 0.01
            e.fill({}, "s")
        e.desktop._input.release()


QUESTIONS = [heading("Register"), text(0, "Email address", "email field", required=True),
             heading("T-shirt size"), radio(10, "S", "T-shirt size"), radio(11, "M", "T-shirt size"), radio(12, "L", "T-shirt size"),
             heading("Availability"), radio(20, "Friday", "Availability", role="AXCheckBox"), radio(21, "Saturday", "Availability", role="AXCheckBox"),
             radio(22, "Sunday", "Availability", role="AXCheckBox"),
             heading("Gender"), radio(30, "Male", "Gender"), radio(31, "Female", "Gender"), button(90, "Submit")]


class TestQuestions:
    def test_multiple_choice_and_tick_boxes_are_questions_with_their_options(self, vault):
        out = engine(Page(list(QUESTIONS)), vault).scan({}, "s")
        fields = {f["label"]: f for f in out["fields"]}
        assert fields["T-shirt size"]["kind"] == "radio" and fields["T-shirt size"]["options"] == ["S", "M", "L"]
        assert fields["Availability"]["kind"] == "checkboxes" and fields["Availability"]["options"] == ["Friday", "Saturday", "Sunday"]
        assert {q["label"] for q in out["questions"]} == {"T-shirt size", "Availability"}

    def test_a_question_the_profile_answers_is_answered(self, vault):
        page = Page(list(QUESTIONS))
        out = engine(page, vault).fill({}, "s")
        assert page.values[31] == 1 and page.values[30] == 0
        assert any(f["label"] == "Gender" for f in out["filled"])

    def test_nothing_is_invented_for_a_question(self, vault):
        page = Page(list(QUESTIONS))
        out = engine(page, vault).fill({}, "s")
        assert page.values[10] == page.values[11] == page.values[12] == 0 and page.values[20] == 0
        assert {q["label"] for q in out["questions"]} == {"T-shirt size", "Availability"}

    def test_the_users_answers_go_in_and_are_read_back(self, vault):
        page = Page(list(QUESTIONS))
        out = engine(page, vault).fill({"choose": {"f2": "M", "f3": ["Friday", "Sunday"]}}, "s")
        assert page.values[11] == 1 and page.values[20] == 1 and page.values[22] == 1 and page.values[21] == 0
        assert out["verified"] is True and not out["questions"]

    def test_an_answer_that_is_no_option_is_said_with_the_options(self, vault):
        page = Page(list(QUESTIONS))
        out = engine(page, vault).fill({"choose": {"f2": "XXL"}}, "s")
        failed = next(f for f in out["not_filled"] if f["label"] == "T-shirt size")
        assert failed["options"] == ["S", "M", "L"] and page.values[10] == 0

    def test_a_tick_box_answer_with_one_wrong_option_says_which(self, vault):
        page = Page(list(QUESTIONS))
        out = engine(page, vault).fill({"choose": {"f3": ["Friday", "Monday"]}}, "s")
        failed = next(f for f in out["not_filled"] if f["label"] == "Availability")
        assert "Monday" in failed["why"] and page.values[20] == 1

    def test_answered_by_the_user_already_it_is_kept(self, vault):
        nodes = [heading("T-shirt size"), radio(10, "S", "T-shirt size", value=1), radio(11, "M", "T-shirt size")]
        page = Page(nodes)
        out = engine(page, vault).fill({}, "s")
        assert page.values[10] == 1 and not page.calls and out["left"][0]["status"] == "keep"

    def test_ticks_already_on_stay_on_unless_replaced(self, vault):
        nodes = [heading("Days"), radio(20, "Friday", "Days", value=1, role="AXCheckBox"), radio(21, "Saturday", "Days", role="AXCheckBox")]
        page = Page(nodes)
        e = engine(page, vault)
        e.fill({"choose": {"f1": ["Saturday"]}}, "s")
        assert page.values[20] == 1 and page.values[21] == 1
        page2 = Page([heading("Days"), radio(20, "Friday", "Days", value=1, role="AXCheckBox"), radio(21, "Saturday", "Days", role="AXCheckBox")])
        engine(page2, vault).fill({"choose": {"f1": ["Saturday"]}, "replace": ["f1"]}, "s2")
        assert page2.values[20] == 0 and page2.values[21] == 1

    def test_a_radio_with_no_press_takes_focus_and_space(self, vault):
        """Measured on real Google Forms: its radio buttons offer no AXPress."""
        page = Page(list(QUESTIONS), no_press=[11])
        out = engine(page, vault).fill({"choose": {"f2": "M"}}, "s")
        assert ("space", 11) in page.calls and page.values[11] == 1
        assert next(f for f in out["filled"] if f["label"] == "T-shirt size")["how"] == "keys"

    def test_agreements_are_never_ticked_by_themselves(self, vault):
        nodes = [heading("Terms"), radio(40, "I agree to the terms", "", role="AXCheckBox")]
        page = Page(nodes)
        out = engine(page, vault).fill({}, "s")
        assert page.values[40] == 0 and not page.calls and out["left"]

    def test_ticking_an_agreement_asks_first(self, vault):
        nodes = [heading("Terms"), radio(40, "I agree to the terms", "", role="AXCheckBox")]
        e = engine(Page(nodes), vault)
        args = {"action": "fill", "choose": {"f1": "yes"}}
        decision = p.classify_form_tool(args, e.probe(args, "s"))
        assert decision.needs_confirmation and "I agree" in decision.summary

    def test_other_gets_the_users_own_words(self, vault):
        nodes = [heading("Colour"), radio(50, "Red", "Colour"), radio(51, "", "Colour"), text(52, "Other response", group="Colour", depth=6)]
        page = Page(nodes)
        e = engine(page, vault)
        assert len(e.scan({}, "s")["fields"]) == 1
        out = e.fill({"choose": {"f1": "Teal"}}, "s")
        assert page.values[51] == 1 and page.values[52] == "Teal" and out["verified"] is True


class TestTheKeyboard:
    """XENIO's quick chat holds the keyboard while it is open: keys must not land there."""

    NODES = [{"role": "AXHeading", "text": "Register", "rect": [10, 100, 200, 20], "hidden": False, "depth": 3, "plain": True},
             heading("T-shirt size"), radio(10, "S", "T-shirt size"), radio(11, "M", "T-shirt size")]

    def scan(self, page):
        real = page.form_scan

        def with_view(hwnd):
            return {**real(hwnd), "web_area": [0, 50, 800, 800]}
        page.form_scan = with_view

    def run(self, page, hit):
        self.scan(page)
        page.at = hit
        e = engine(page, Vault())
        clicks = []
        e._click_back = lambda p, point: clicks.append(point)
        out = e.fill({"choose": {"f1": "M"}}, "s")
        return out, clicks

    def test_a_click_on_this_pages_own_text_first(self):
        page = Page(list(self.NODES), no_press=[11])
        out, clicks = self.run(page, lambda x, y: {"found": True, "pid": 70, "type": "text"})
        assert clicks == [(110, 110)] and page.values[11] == 1 and out["verified"] is True

    def test_only_once_on_a_page(self):
        nodes = [*self.NODES, heading("Size two"), radio(20, "A", "Size two"), radio(21, "B", "Size two")]
        page = Page(nodes, no_press=[11, 21])
        self.scan(page)
        page.at = lambda x, y: {"found": True, "pid": 70, "type": "text"}
        e = engine(page, Vault())
        clicks = []
        e._click_back = lambda p, point: clicks.append(point)
        e.fill({"choose": {"f1": "M", "f2": "B"}}, "s")
        assert len(clicks) == 1

    @pytest.mark.parametrize("hit", [{"found": True, "pid": 70, "type": "link"}, {"found": True, "pid": 70, "type": "button"},
                                     {"found": True, "pid": 999, "type": "text"}, {"found": False}])
    def test_never_on_a_control_or_another_window(self, hit):
        page = Page(list(self.NODES), no_press=[11])
        _, clicks = self.run(page, lambda x, y: hit)
        assert not clicks

    def test_not_at_all_if_no_key_is_needed(self):
        page = Page(list(self.NODES))
        _, clicks = self.run(page, lambda x, y: {"found": True, "pid": 70, "type": "text"})
        assert not clicks

    def test_a_chat_that_still_holds_it_means_not_confirmed_never_claimed(self, monkeypatch):
        page = Page(list(self.NODES), no_press=[11])
        page.form_space = lambda token, index: {"focused": True, "value": 0}
        page.form_point = lambda token, index: {"point": None}
        out, _ = self.run(page, lambda x, y: {"found": False})
        assert out["verified"] is False and out["not_filled"][0]["label"] == "T-shirt size"


class TestAskedOnlyOnce:
    def test_all_the_questions_come_together(self, vault):
        out = engine(Page(list(QUESTIONS)), vault).fill({}, "s")
        assert "ask the rest together in one message" in out["say"]

    def test_an_answer_is_remembered_for_this_form(self, vault):
        page = Page(list(QUESTIONS))
        e = engine(page, vault)
        e.fill({"choose": {"f2": "M"}}, "s")
        page.values[11] = 0  # say the page was reloaded: the answer is not asked for again
        out = e.fill({}, "s")
        assert page.values[11] == 1 and "T-shirt size" not in [q["label"] for q in out["questions"]]

    def test_answers_are_by_question_not_by_position(self, vault):
        page = Page(list(QUESTIONS))
        e = engine(page, vault)
        e.fill({"choose": {"f2": "M"}}, "s")
        moved = [text(0, "New field"), *QUESTIONS]
        page.nodes = moved
        page.values = {n["i"]: n.get("value") for n in moved if "i" in n}
        out = e.fill({}, "s")
        assert page.values[11] == 1 and not any(q["label"] == "T-shirt size" for q in out["questions"])

    def test_another_conversation_starts_fresh(self, vault):
        page = Page(list(QUESTIONS))
        e = engine(page, vault)
        e.fill({"choose": {"f2": "M"}}, "s")
        page.values[11] = 0
        e.fill({}, "other")
        assert page.values[11] == 0

    def test_a_yes_to_a_sensitive_detail_is_not_asked_again_on_this_form(self, vault):
        vault.add("other", "Passport number", "A1234567")
        page = Page([text(0, "Passport number")])
        e = engine(page, vault)
        args = {"action": "fill"}
        assert p.classify_form_tool(args, e.probe(args, "s")).needs_confirmation
        e.fill(args, "s")  # the guard asked, the user said yes
        assert not p.classify_form_tool(args, e.probe(args, "s")).needs_confirmation


class TestGraded:
    QUIZ = [heading("Physics quiz"), text(0, "Email address", "email field"), heading("What is the unit of force?"),
            {"role": "AXStaticText", "text": "1 point", "rect": [0, 0, 10, 10], "hidden": False, "depth": 5},
            radio(10, "Newton", "What is the unit of force?"), radio(11, "Joule", "What is the unit of force?")]

    def test_its_details_are_filled_and_its_questions_are_not(self, vault):
        page = Page(list(self.QUIZ))
        out = engine(page, vault).fill({}, "s")
        assert page.values[0] == "asha@example.com" and page.values[10] == page.values[11] == 0
        assert "graded" in out["say"] and any(i["status"] == "graded" for i in out["left"])
        assert not out["questions"]

    def test_answering_it_is_refused(self, vault):
        e = engine(Page(list(self.QUIZ)), vault)
        args = {"action": "fill", "choose": {"f2": "Newton"}}
        assert p.classify_form_tool(args, e.probe(args, "s")).blocked

    def test_the_engine_refuses_it_even_if_asked_directly(self, vault):
        page = Page(list(self.QUIZ))
        with pytest.raises(DesktopError, match="graded"):
            engine(page, vault).fill({"choose": {"f2": "Newton"}}, "s")
        assert page.values[10] == 0

    @pytest.mark.parametrize("words, graded", [("1 point", True), ("Total points 20", True), ("5 marks", True), ("Rate from 1 to 5 points of contact", False),
                                               ("Thanks for your feedback", False)])
    def test_what_says_graded(self, words, graded):
        assert p.graded_form(words) is graded

    def test_a_practice_quiz_is_not_refused(self):
        assert p.graded_form("1 point", "Mock test 3") is False


class TestClassTests:
    @pytest.mark.parametrize("title", ["Unit Test- Civics (AB12.C)", "Class Test 2 - Physics", "Mid-term Maths", "Midsem paper", "End Sem Chemistry",
                                       "Weekly Quiz 4", "Internal Test - DBMS", "Viva - OS", "Class Test - EE101.CS", "Unit Test - Legal Framework", "Unit Test - Tutorial 3",
                                       "Unit Test - Insurance Coverage", "Unit Test 1 - CI", "Unit Test 2 - C Programming", "Class Test 3 - Java"])
    def test_a_colleges_own_test_is_a_test(self, title):
        assert p.live_test(title)

    @pytest.mark.parametrize("title", ["test_vault.py - Unit Test - Visual Studio Code", "How to write a unit test in Python", "Unit tests - pytest documentation",
                                       "Add unit test for login · Pull Request #4 · GitHub", "unit test for login.py", "Python unit test framework", "Unit test tutorial", "Practice Unit Test 3", "Mock Class Test", "Mid-Century Modern Furniture",
                                       "Class Notes", "Testing"])
    def test_a_programmers_unit_test_or_practice_is_not(self, title):
        assert not p.live_test(title)

    def test_such_a_form_is_refused_whole(self, vault):
        page = Page(list(BASIC), title="Unit Test- Civics (AB12.C)")
        e = engine(page, vault)
        for action in ("scan", "fill", "next", "submit"):
            assert p.classify_form_tool({"action": action}, e.probe({"action": action}, "s")).blocked
        assert not page.calls

    def test_a_quiz_by_its_title_keeps_its_questions_for_the_user(self):
        assert p.graded_form("no points shown", "Weekly check quiz") and not p.graded_form("x", "Quiz practice sheet".replace("Quiz practice sheet", "Survey"))


class TestLiveTests:
    def test_a_running_test_is_refused_by_its_title(self, vault):
        e = engine(Page(list(BASIC), title="Coding round | Online Assessment"), vault)
        decision = p.classify_form_tool({"action": "fill"}, e.probe({"action": "fill"}, "s"))
        assert decision.blocked and "test" in decision.reason

    def test_a_running_test_is_refused_by_its_page(self, vault):
        nodes = [{"role": "AXStaticText", "text": "Contest Ends in 00 : 01 : 40 : 49", "rect": [0, 0, 1, 1], "hidden": False, "depth": 3}, *BASIC]
        e = engine(Page(nodes), vault)
        for action in ("scan", "fill", "submit"):
            assert p.classify_form_tool({"action": action}, e.probe({"action": action}, "s")).blocked

    def test_the_engine_refuses_it_even_if_asked_directly(self, vault):
        nodes = [{"role": "AXStaticText", "text": "Contest Ends in 00 : 01 : 40 : 49", "rect": [0, 0, 1, 1], "hidden": False, "depth": 3}, *BASIC]
        page = Page(nodes)
        with pytest.raises(DesktopError):
            engine(page, vault).fill({}, "s")
        assert not page.calls


class TestSensitive:
    def test_a_sensitive_detail_asks_first_with_its_name_not_its_value(self, vault):
        vault.add("other", "Passport number", "A1234567")
        e = engine(Page([text(0, "Passport number")]), vault)
        args = {"action": "fill"}
        decision = p.classify_form_tool(args, e.probe(args, "s"))
        assert decision.needs_confirmation and "Passport number" in decision.summary and "A1234567" not in decision.summary

    def test_after_it_is_filled_a_read_of_that_page_cannot_carry_it(self, vault):
        vault.add("other", "Passport number", "A1234567")
        page = Page([text(0, "Passport number")])
        engine(page, vault).fill({}, "s")
        assert page.values[0] == "A1234567"
        assert mask_filled("field holds A1234567 here") == "field holds [saved detail] here"

    def test_an_ordinary_detail_is_not_masked(self, vault):
        engine(Page([text(0, "City")]), vault).fill({}, "s")
        assert mask_filled("Pune") == "Pune"


class TestSteps:
    PAGE1 = [heading("Step 1"), text(0, "First name"), button(90, "Next")]
    PAGE2 = [heading("Step 2"), text(0, "Email address", "email field"), button(90, "Submit")]

    def test_it_goes_through_the_pages_in_one_call_and_stops_before_submit(self, vault):
        page = Page(list(self.PAGE1), next_nodes=list(self.PAGE2))
        out = engine(page, vault).fill({"steps": True}, "s")
        assert out["pages"] == 2 and {f["label"] for f in out["filled"]} == {"First name", "Email address"}
        assert out["submit"] == ["Submit"] and out["submitted"] is False and ("press", 90) in page.calls

    def test_without_steps_it_stays_on_this_page(self, vault):
        page = Page(list(self.PAGE1), next_nodes=list(self.PAGE2))
        out = engine(page, vault).fill({}, "s")
        assert out["pages"] == 1 and ("press", 90) not in page.calls and out["next"] == ["Next"]

    def test_it_stops_where_a_required_answer_is_missing(self, vault):
        nodes = [heading("Step 1"), text(0, "College", required=True), button(90, "Next")]
        page = Page(nodes, next_nodes=list(self.PAGE2))
        out = engine(page, vault).fill({"steps": True}, "s")
        assert out["pages"] == 1 and ("press", 90) not in page.calls

    def test_a_sensitive_detail_on_a_later_page_still_stops_for_a_yes(self, vault):
        vault.add("other", "Passport number", "A1234567")
        page2 = [heading("Step 2"), text(0, "Passport number"), button(90, "Submit")]
        page = Page(list(self.PAGE1), next_nodes=page2)
        out = engine(page, vault).fill({"steps": True}, "s")
        assert page.values[0] == "" and "needs your yes" in out["stop_reason"] and out["pages"] == 2

    def test_a_running_test_on_a_later_page_stops_it(self, vault):
        page2 = [{"role": "AXStaticText", "text": "Test ends in 00:10:00", "rect": [0, 0, 1, 1], "hidden": False, "depth": 3}, heading("Step 2"), text(0, "City")]
        page = Page(list(self.PAGE1), next_nodes=page2)
        out = engine(page, vault).fill({"steps": True}, "s")
        assert page.values[0] == "" and "stopped at this page" in out["stop_reason"]

    def test_next_returns_the_next_page(self, vault):
        page = Page(list(self.PAGE1), next_nodes=list(self.PAGE2))
        out = engine(page, vault).next_page({"action": "next"}, "s")
        assert out["pressed"] == "Next" and out["fields"][0]["label"] == "Email address" and out["verified"] is True

    def test_next_will_not_press_something_else(self, vault):
        page = Page([text(0, "City"), button(90, "Submit")])
        with pytest.raises(DesktopError, match="not a Next button"):
            engine(page, vault).next_page({"action": "next", "element": "Submit"}, "s")
        assert not page.calls

    def test_a_next_that_changes_nothing_is_said_so(self, vault, monkeypatch):
        import app.autofill.engine as module

        monkeypatch.setattr(module, "PAGE_CHANGE_WAIT", 0.05)
        page = Page([text(0, "City"), button(90, "Next")])
        with pytest.raises(DesktopError, match="did not change"):
            engine(page, vault).next_page({"action": "next"}, "s")


class TestSubmit:
    FORM = [heading("Apply"), text(0, "City", value="Pune"), button(90, "Submit application")]

    def after(self, words):
        return [{"role": "AXStaticText", "text": words, "rect": [0, 0, 100, 10], "hidden": False, "depth": 3}, heading("Apply")]

    def test_only_what_the_page_says_counts(self, vault):
        page = Page(list(self.FORM), after_submit=self.after("Thank you! Your application has been received."))
        out = engine(page, vault).submit({"action": "submit"}, "s")
        assert out["submitted"] is True and out["verified"] is True and out["pressed"] == "Submit application"

    def test_a_refusal_by_the_page_is_said(self, vault):
        page = Page(list(self.FORM), after_submit=[*self.after("Please enter a valid phone number"), text(0, "City", value="Pune")])
        out = engine(page, vault).submit({"action": "submit"}, "s")
        assert out["submitted"] is False and "valid phone" in out["say"]

    def test_silence_is_not_success(self, vault, monkeypatch):
        import app.autofill.engine as module

        monkeypatch.setattr(module, "PAGE_CHANGE_WAIT", 0.05)
        out = engine(Page(list(self.FORM)), vault).submit({"action": "submit"}, "s")
        assert out["submitted"] is None and out["verified"] is False and "does not clearly say" in out["say"]

    def test_required_blanks_stop_it(self, vault):
        page = Page([text(0, "College", required=True), button(90, "Submit")])
        with pytest.raises(DesktopError, match="required fields are still empty"):
            engine(page, vault).submit({"action": "submit"}, "s")
        assert ("press", 90) not in page.calls

    def test_which_button_is_a_question_when_there_are_two(self, vault):
        page = Page([text(0, "City"), button(90, "Submit"), button(91, "Save and apply")])
        with pytest.raises(DesktopError, match="which button"):
            engine(page, vault).submit({"action": "submit"}, "s")

    def test_a_running_test_cannot_be_submitted(self, vault):
        page = Page(list(self.FORM), title="Hiring test | Round 2 assessment")
        with pytest.raises(DesktopError):
            engine(page, vault).submit({"action": "submit"}, "s")
        assert not page.calls


class TestFormsAreRead:
    def test_google_forms_labels_lose_their_required_words(self):
        form = forms.build({"nodes": [text(0, "Best email address to reach you: Required question")], "web_area": [0, 0, 800, 900]})
        assert form.fields[0].label == "Best email address to reach you" and form.fields[0].required is True

    def test_a_star_means_required(self):
        assert forms.build({"nodes": [text(0, "City *")]}).fields[0].required is True

    def test_the_help_menu_is_no_question(self):
        node = dict(text(0, "help and feedback"), role="AXPopUpButton")
        assert not forms.build({"nodes": [node]}).fields

    def test_a_grid_of_radio_buttons_is_a_question_per_row(self):
        nodes = [heading("Rate"), radio(1, "Good, response for Morning", "Morning"), radio(2, "Bad, response for Morning", "Morning"),
                 radio(3, "Good, response for Evening", "Evening"), radio(4, "Bad, response for Evening", "Evening")]
        fields = forms.build({"nodes": nodes}).fields
        assert [f.label for f in fields] == ["Morning", "Evening"] and fields[0].options == ["Good", "Bad"] and fields[0].section == "Rate"

    def test_a_grid_of_tick_boxes_is_a_question_per_row(self):
        nodes = [heading("Where"), radio(1, "Japan, response for Beach", "", role="AXCheckBox"), radio(2, "Peru, response for Beach", "", role="AXCheckBox"),
                 radio(3, "Japan, response for Ski", "", role="AXCheckBox"), radio(4, "Peru, response for Ski", "", role="AXCheckBox")]
        fields = forms.build({"nodes": nodes}).fields
        assert [f.label for f in fields] == ["Beach", "Ski"] and all(f.kind == "checkboxes" for f in fields) and fields[1].options == ["Japan", "Peru"]

    def test_an_options_own_label_does_not_end_the_question(self):
        nodes = [heading("Size"), radio(1, "S", ""), {"role": "AXStaticText", "text": "S", "hidden": False, "depth": 6},
                 radio(2, "M", ""), {"role": "AXStaticText", "text": "M", "hidden": False, "depth": 6}]
        fields = forms.build({"nodes": nodes}).fields
        assert len(fields) == 1 and fields[0].options == ["S", "M"] and fields[0].label == "Size"

    def test_a_list_knows_its_options_and_choice(self):
        node = dict(text(0, "Continent"), role="AXListBox", options=["Choose", "Asia", "Africa"], value="Asia")
        field = forms.build({"nodes": [node]}).fields[0]
        assert field.kind == "listbox" and field.options == ["Asia", "Africa"] and field.has_value

    @pytest.mark.parametrize("words, attr", [("I'm not a robot", "human_check"), ("Protected by reCAPTCHA", "human_check")])
    def test_a_human_check_is_noticed(self, words, attr):
        form = forms.build({"nodes": [{"role": "AXStaticText", "text": words, "hidden": False, "depth": 1}]})
        assert getattr(form, attr) is True
