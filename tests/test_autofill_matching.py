"""Which saved detail goes in which field. A wrong value is worse than an empty field."""

import datetime as dt

import pytest

from app.autofill import matching as m

ENTRIES = [
    {"id": "email", "label": "Email", "value": "asha@example.com", "field": "email"},
    {"id": "phone", "label": "Mobile", "value": "+91 90000 00000", "field": "phone"},
    {"id": "name", "label": "Full name", "value": "Asha Verma", "field": "full_name"},
    {"id": "city", "label": "City", "value": "Pune"},
    {"id": "father", "label": "Father's name", "value": "Ravi Verma"},
    {"id": "linkedin", "label": "LinkedIn", "value": "https://linkedin.com/in/asha"},
    {"id": "home", "label": "Home address", "value": "12 MG Road", "field": "address"},
    {"id": "work", "label": "Work address", "value": "4 Tech Park", "field": "address"},
    {"id": "college", "label": "College", "value": "COEP"},
]


def field(label, kind="text", **kw):
    return m.match_field(label, kind, ENTRIES, **kw)


@pytest.mark.parametrize("label, kind, entry", [
    ("Email address", "email", "email"), ("E-mail", "text", "email"), ("Your email", "text", "email"), ("Email ID *", "text", "email"),
    ("Mobile", "tel", "phone"), ("Phone number", "tel", "phone"), ("Contact number", "text", "phone"), ("WhatsApp number", "tel", "phone"),
    ("Name", "text", "name"), ("Full name", "text", "name"), ("Your name", "text", "name"), ("Candidate name", "text", "name"),
    ("City", "text", "city"), ("City / Town", "text", "city"), ("LinkedIn profile", "text", "linkedin"), ("LinkedIn URL", "url", "linkedin"),
    ("University", "text", "college"), ("College name", "text", "college"), ("Father's name", "text", "father"),
])
def test_what_a_form_calls_a_detail(label, kind, entry):
    got = field(label, kind)
    assert (got.status, got.entry_id) == ("ready", entry), got
    assert got.confidence >= m.AUTO_FILL


@pytest.mark.parametrize("label, kind, section", [
    ("Emergency contact phone", "tel", ""), ("Emergency contact name", "text", ""), ("Mother's name", "text", ""),
    ("Guardian email", "email", ""), ("Referee's email", "email", ""), ("Spouse name", "text", ""), ("Manager's phone", "tel", ""),
    ("Name", "text", "Emergency contact"), ("Phone", "tel", "Reference 1"), ("Email", "email", "Nominee details"),
])
def test_a_field_about_someone_else_never_gets_the_users_detail(label, kind, section):
    got = field(label, kind, section=section)
    assert got.status == "no_match" and not got.entry_id, got


@pytest.mark.parametrize("label", ["Project name", "Company name", "Event name", "File name", "Username", "Email subject", "Coupon code",
                                   "Search", "Subject", "Message", "Team name", "Book title"])
def test_a_word_in_common_is_not_a_match(label):
    assert field(label).status != "ready", label


@pytest.mark.parametrize("label, kind", [("Password", "password"), ("Confirm password", "text"), ("New password", "text"), ("OTP", "text"),
                                         ("One-time code", "text"), ("Card number", "text"), ("CVV", "text"), ("CAPTCHA", "text"),
                                         ("UPI PIN", "text"), ("Security answer", "text"), ("Anything", "password")])
def test_never_filled_whatever_is_saved(label, kind):
    got = m.match_field(label, kind, [*ENTRIES, {"id": "x", "label": label, "value": "abc"}])
    assert got.status == "refused" and not got.entry_id


def test_a_secure_field_is_refused_by_what_it_is_not_what_it_says():
    assert field("Email", "text", secure=True).status == "refused"


def test_pin_code_is_a_postal_code_not_a_secret():
    assert m.match_field("PIN code", "text", [{"id": "z", "label": "PIN code", "value": "411001"}]).status == "ready"
    assert m.match_field("ZIP / Postal code", "text", [{"id": "z", "label": "PIN code", "value": "411001", "field": "postal_code"}]).entry_id == "z"


class TestAsked:
    def test_two_of_a_kind_is_a_question(self):
        got = field("Address")
        assert got.status == "ambiguous" and {c["id"] for c in got.candidates} == {"home", "work"}

    def test_the_question_never_carries_a_value(self):
        assert "MG Road" not in str(field("Address").candidates)

    def test_its_own_name_settles_it(self):
        assert field("Work address").entry_id == "work"

    def test_contact_alone_says_too_little(self):
        got = field("Contact")
        assert got.status == "ambiguous" and {c["id"] for c in got.candidates} == {"phone", "email"}

    def test_a_field_that_is_only_probably_it_is_a_question(self):
        got = m.match_field("Highest degree earned so far", "text", [{"id": "d", "label": "Degree", "value": "BTech", "field": "degree"}])
        assert got.status == "ambiguous" and got.confidence < m.AUTO_FILL


class TestType:
    def test_an_email_field_takes_only_an_email(self):
        got = m.match_field("Email", "email", [{"id": "e", "label": "Email", "value": "not an email", "field": "email"}])
        assert got.status != "ready"

    def test_a_phone_field_takes_only_a_phone_number(self):
        assert m.match_field("Phone", "tel", [{"id": "e", "label": "Phone", "value": "ask me", "field": "phone"}]).status != "ready"

    def test_a_field_with_no_name_is_known_by_its_type(self):
        assert m.match_field("", "email", ENTRIES).entry_id == "email"

    def test_a_multi_line_value_does_not_go_in_a_single_line_field(self):
        assert not m.fits("text", "a\nb") and m.fits("textarea", "a\nb")

    @pytest.mark.parametrize("kind, value, ok", [("url", "github.com/asha", True), ("url", "see my site", False), ("number", "8.7", True),
                                                 ("number", "eight", False), ("date", "31/01/2000", True), ("date", "soon", False)])
    def test_fits(self, kind, value, ok):
        assert m.fits(kind, value) is ok


class TestMadeFromOthers:
    PARTS = [{"id": "f", "label": "First name", "value": "Asha", "field": "first_name"},
             {"id": "l", "label": "Last name", "value": "Verma", "field": "last_name"}]

    def test_a_full_name_from_its_parts(self):
        got = m.match_field("Full name", "text", self.PARTS)
        assert (got.status, got.derived) == ("ready", "full_name")
        assert m.derive_value("full_name", m.group_entries(self.PARTS)) == "Asha Verma"

    def test_first_and_last_from_a_two_word_name(self):
        entries = [{"id": "n", "label": "Full name", "value": "Asha Verma", "field": "full_name"}]
        assert m.match_field("First name", "text", entries).status == "ready"
        assert m.derive_value("last_name", m.group_entries(entries)) == "Verma"

    def test_a_longer_name_is_not_split_by_guessing(self):
        entries = [{"id": "n", "label": "Full name", "value": "Asha Rani Verma Singh", "field": "full_name"}]
        assert m.match_field("Last name", "text", entries).status == "ambiguous"

    def test_date_boxes_under_date_of_birth(self):
        entries = [{"id": "d", "label": "Date of birth", "value": "2000-01-31", "field": "date_of_birth"}]
        got = m.match_field("Month", "select", entries, group="Date of birth")
        assert (got.status, got.derived) == ("ready", "dob_month")
        assert m.derive_value("dob_month", m.group_entries(entries)) == "January"
        assert m.match_field("Month", "select", entries, group="Start date").status != "ready"


class TestOptions:
    @pytest.mark.parametrize("value, options, want", [
        ("India", ["Choose…", "United States", "India"], "India"), ("India", ["IN", "US"], "IN"), ("India", ["India (+91)", "Iran"], "India (+91)"),
        ("Male", ["Male", "Female"], "Male"), ("Yes", ["Y", "N"], "Y"), ("United States", ["USA", "UK"], "USA"),
    ])
    def test_the_option_that_says_it(self, value, options, want):
        assert m.match_option(value, options)[0] == want

    @pytest.mark.parametrize("value, options", [("India", ["Germany", "France"]), ("Female", ["Select"]), ("India", ["Indiana"]),
                                                ("Pune", ["Pune East", "Pune West"])])
    def test_no_guessing(self, value, options):
        assert m.match_option(value, options) == ("", 0.0)


class TestDates:
    @pytest.mark.parametrize("raw", ["2000-01-31", "31/01/2000", "31-01-2000", "31 January 2000", "31 Jan 2000", "January 31, 2000", "31st Jan 2000"])
    def test_how_people_write_a_date(self, raw):
        assert m.parse_date(raw) == dt.date(2000, 1, 31)

    def test_rubbish_is_no_date(self):
        assert m.parse_date("31/13/2000") is None and m.parse_date("next week") is None

    def test_a_date_that_reads_two_ways_is_known_to(self):
        assert not m.date_is_clear("03/04/2000") and m.date_is_clear("31/01/2000") and m.date_is_clear("2000-04-03")

    @pytest.mark.parametrize("pattern, want", [("DD/MM/YYYY", "31/01/2000"), ("mm-dd-yyyy", "01-31-2000"), ("Joining date (DD.MM.YYYY)", "31.01.2000"),
                                               ("YYYY-MM-DD", "2000-01-31"), ("no format here", None)])
    def test_in_the_forms_own_format(self, pattern, want):
        assert m.write_date(dt.date(2000, 1, 31), pattern) == want


class TestWhatTheFieldHolds:
    @pytest.mark.parametrize("expected, actual, kind, same", [
        ("A@B.co", "a@b.co", "email", True), ("+91 90000 00000", "(900) 000-0000", "tel", True), ("+91 90000 00000", "+91 90000 00001", "tel", False),
        ("https://x.com/a/", "x.com/a", "url", True), ("x.com/a", "x.com/b", "url", False), ("Asha", "asha", "text", False), ("Asha ", "Asha", "text", True),
        ("Asha", None, "text", False), ("2000-01-31", "31/01/2000", "date", True), ("India", "india", "select", True),
    ])
    def test_allowing_for_what_pages_do(self, expected, actual, kind, same):
        assert m.same_value(expected, actual, kind) is same


@pytest.mark.parametrize("words, kinds", [(["Pay now", "Card holder"], ["financial"]), (["I agree to the terms"], ["legal"]), (["Passport number"], ["government"]),
                                          (["Medical history"], ["medical"]), (["First name", "Next", "Apply"], [])])
def test_what_makes_a_form_high_impact(words, kinds):
    assert m.impact(words) == kinds
