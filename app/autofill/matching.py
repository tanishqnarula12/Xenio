"""Which saved detail belongs in which form field (V4 phase 2). Pure: no UI, no vault, no model.

A field is matched on its words (label, placeholder, the section and group it sits in) and its type. The outcome is one of

* ``ready``: one entry fits, with confidence of at least ``AUTO_FILL``;
* ``ambiguous``: more than one fits, or the only one is not certain enough: the user is asked, nothing is guessed;
* ``no_match``: nothing saved fits. Not an error: forms ask for things a profile does not hold;
* ``refused``: passwords, codes, payment details and the like: never filled, whatever is saved.

A wrong value in a form is worse than an empty field, so a word like "emergency" or "father" in a field, or in the section
around it, means the field is about someone else, and only an entry saved under that very name goes there.

Values appear here only to check that they fit a field; nothing returned for display carries them.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field as dataclass_field

from app.personal.vault import secret_label

AUTO_FILL = 0.8
ASK = 0.5

# canonical field -> (category, what the form may call it). Words are compared after ``words()``.
FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "first_name": ("identity", ("first name", "given name", "forename", "fname", "firstname", "name first")),
    "middle_name": ("identity", ("middle name", "middle initial", "mname")),
    "last_name": ("identity", ("last name", "surname", "family name", "lname", "lastname", "name last")),
    "full_name": ("identity", ("name", "full name", "fullname", "applicant name", "candidate name", "legal name",
                               "complete name", "name applicant", "name candidate", "student name", "first and last name",
                               "first last name", "name and surname", "name surname")),
    "preferred_name": ("identity", ("preferred name", "nickname", "nick name", "known as")),
    "email": ("identity", ("email", "e mail", "email address", "e mail address", "mail id", "email id", "e mail id",
                           "mail address", "confirm email", "retype email", "re enter email", "verify email")),
    "phone": ("identity", ("phone", "phone number", "phone no", "mobile", "mobile number", "mobile no", "cell", "cell phone",
                           "cell number", "contact number", "contact no", "telephone", "telephone number", "tel",
                           "whatsapp", "whatsapp number", "primary phone")),
    "date_of_birth": ("identity", ("date birth", "dob", "birth date", "birthdate", "birthday", "d o b")),
    "gender": ("identity", ("gender", "sex", "gender identity")),
    "nationality": ("identity", ("nationality", "citizenship", "citizen")),
    "address": ("address", ("address", "home address", "residential address", "current address", "permanent address",
                            "full address", "postal address", "mailing address", "correspondence address")),
    "address_line1": ("address", ("address line 1", "address 1", "street address", "street", "line 1", "address line1",
                                  "house number", "flat house", "street name")),
    "address_line2": ("address", ("address line 2", "address 2", "apartment", "apt", "suite", "unit", "line 2",
                                  "address line2", "landmark", "locality", "area")),
    "city": ("address", ("city", "town", "city town", "town city", "current city", "suburb")),
    "state": ("address", ("state", "province", "state province", "province state", "region", "state region",
                          "state union territory")),
    "postal_code": ("address", ("zip", "zip code", "zipcode", "postal code", "postcode", "post code", "pin code", "pincode",
                                "zip postal code", "postal zip code")),
    "country": ("address", ("country", "country region", "country residence", "current country", "country territory")),
    "college": ("education", ("college", "university", "institute", "institution", "school", "school name", "college name",
                              "university name", "institute name", "institution name", "alma mater",
                              "college university", "university college", "school university")),
    "degree": ("education", ("degree", "qualification", "highest qualification", "education level", "highest degree",
                             "education", "highest education", "course")),
    "field_of_study": ("education", ("major", "field study", "branch", "specialization", "specialisation", "stream",
                                     "discipline", "department", "subject")),
    "gpa": ("education", ("gpa", "cgpa", "cpi", "grade", "grades", "percentage", "marks", "score")),
    "graduation_year": ("education", ("graduation year", "year graduation", "passing year", "year passing", "class",
                                      "batch", "grad year", "graduation date", "expected graduation")),
    "company": ("work", ("company", "company name", "current company", "employer", "current employer", "organisation",
                         "organization", "employer name", "organization name", "organisation name", "current organization",
                         "workplace")),
    "job_title": ("work", ("job title", "title", "current title", "position", "designation", "role", "current role",
                           "current position", "current designation", "occupation")),
    "years_experience": ("work", ("years experience", "total experience", "experience years", "experience",
                                  "work experience", "total years experience", "years relevant experience")),
    "skills": ("work", ("skills", "key skills", "technical skills", "technologies", "skill set", "skillset", "tech stack")),
    "notice_period": ("work", ("notice period", "notice")),
    "expected_salary": ("work", ("expected salary", "expected ctc", "salary expectation", "salary expectations")),
    "current_salary": ("work", ("current salary", "current ctc", "present salary")),
    "linkedin": ("portfolio", ("linkedin", "linkedin url", "linkedin profile", "linkedin profile url", "linkedin link",
                               "linked in")),
    "github": ("portfolio", ("github", "github url", "github profile", "github profile url", "github link", "git hub")),
    "portfolio": ("portfolio", ("portfolio", "portfolio url", "portfolio website", "portfolio link", "website",
                                "personal website", "personal site", "website url", "homepage", "home page", "blog", "url",
                                "personal url", "web site")),
    "bio": ("portfolio", ("bio", "about", "summary", "professional summary", "about you", "about me", "introduction",
                          "short bio", "profile summary", "tell us about yourself", "tell us yourself")),
    "passport_number": ("other", ("passport number", "passport no", "passport")),
    "pan_number": ("other", ("pan number", "pan", "pan no", "pan card number")),
    "aadhaar_number": ("other", ("aadhaar number", "aadhar number", "aadhaar", "aadhar", "aadhaar no", "aadhar no")),
}
LABELS = {
    "first_name": "First name", "middle_name": "Middle name", "last_name": "Last name", "full_name": "Full name",
    "preferred_name": "Preferred name", "email": "Email", "phone": "Phone", "date_of_birth": "Date of birth", "gender": "Gender",
    "nationality": "Nationality", "address": "Full address", "address_line1": "Address line 1", "address_line2": "Address line 2",
    "city": "City", "state": "State", "postal_code": "PIN / ZIP code", "country": "Country", "college": "College or university",
    "degree": "Degree", "field_of_study": "Field of study", "gpa": "CGPA / GPA", "graduation_year": "Graduation year",
    "company": "Company", "job_title": "Job title", "years_experience": "Years of experience", "skills": "Skills",
    "notice_period": "Notice period", "expected_salary": "Expected salary", "current_salary": "Current salary",
    "linkedin": "LinkedIn", "github": "GitHub", "portfolio": "Portfolio or website", "bio": "Short bio",
    "passport_number": "Passport number", "pan_number": "PAN", "aadhaar_number": "Aadhaar number",
}

_STOP = {"your", "the", "a", "an", "please", "enter", "provide", "type", "here", "my", "of", "in", "this", "is", "you",
         "required", "optional", "mandatory", "input", "field", "e.g", "eg", "what", "what's", "whats", "s", "best", "to", "reach",
         "at", "on", "can", "we", "use", "should", "would", "like", "be", "for", "question"}
# extra words that do not change whose detail, or what kind of detail, a field asks for
_BENIGN = {"primary", "current", "personal", "main", "legal", "contact", "permanent", "residential", "home", "number", "no",
           "id", "address", "details", "detail", "info", "information", "and", "or", "your", "official", "billing",
           "shipping", "delivery", "mailing", "line", "code", "profile", "link", "url", "full", "complete", "valid", "active"}
# aliases too general to count as part of a longer label: "Project name" is not a person's name
_GENERIC = {"name", "title", "role", "address", "state", "url", "area", "class", "unit", "street", "city", "position", "about",
            "summary", "course", "score", "marks", "grade", "subject", "department", "school", "notice", "region", "town",
            "experience", "education", "company", "website", "website url", "blog", "cell", "tel", "pan", "stream", "branch"}
# the field, or the section around it, is about someone or something else
_SOMEONE_ELSE = re.compile(
    r"\b(father|father'?s|mother|mother'?s|parent|parents|guardian|spouse|husband|wife|emergency|reference|references|referee"
    r"|referrer|referral|referred|recommender|nominee|next of kin|kin|sibling|brother|sister|friend|manager|supervisor"
    r"|recruiter|recipient|beneficiary|dependent|dependant|child|son|daughter|landlord|colleague|sender|mentor|advisor"
    r"|co ?founder|contact person)\b", re.IGNORECASE)
_NOT_A_DETAIL = re.compile(r"\b(search|coupon|promo|voucher|captcha|verification|referral code|subject|message|comment|username"
                           r"|user name|login|file name)\b", re.IGNORECASE)
_AGREEMENT = re.compile(r"\b(i agree|i accept|i consent|i confirm|i certify|i declare|i understand|i acknowledge|i authori[sz]e"
                        r"|agree to|accept the|terms|privacy policy|consent|declaration|undertaking|newsletter|subscribe"
                        r"|marketing|send me|contact me about)\b", re.IGNORECASE)
_CHOICE_KINDS = {"select", "combobox", "listbox", "radio", "checkboxes"}
_TEXT_KINDS = {"text", "email", "tel", "url", "number", "textarea"}


@dataclass
class Match:
    status: str  # ready | ambiguous | no_match | refused
    entry_id: str = ""
    derived: str = ""  # a value made from entries: "first_name" from a saved full name, ...
    canonical: str = ""
    confidence: float = 0.0
    candidates: list[dict] = dataclass_field(default_factory=list)  # {"id", "label"}: never a value
    reason: str = ""


def words(label: str) -> list[str]:
    text = re.sub(r"[*:()\[\]{}?!.,/\\|_#\"“”]+", " ", str(label or "").lower().replace("&", " and ").replace("-", " "))
    text = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", text)
    return [w for w in text.split() if w not in _STOP]


def split_identifier(raw: str) -> str:
    """Words from an id like ``applicantEmail`` or ``first_name`` (a field with no other name). A random id gives none."""
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(raw or ""))
    text = re.sub(r"[_\-.:\[\]]+", " ", text).strip()
    if not text or re.search(r"\d{3,}", text) or re.fullmatch(r"[a-z]\d*|[a-f0-9]{6,}", text.lower()):
        return ""
    return text


_ALIASES = {field: {" ".join(words(a)) for a in spec[1]} for field, spec in FIELDS.items()}


def _alias_score(label_words: list[str], alias: str) -> float:
    alias_words = alias.split()
    if label_words == alias_words:
        return 0.97
    if "".join(label_words) == alias.replace(" ", ""):
        return 0.95
    if not set(alias_words) <= set(label_words):
        return 0.0
    extra = [w for w in label_words if w not in alias_words]
    if all(w in _BENIGN for w in extra):
        return max(0.81, 0.92 - 0.03 * len(extra)) if alias not in _GENERIC or len(extra) <= 2 else 0.0
    return 0.0 if alias in _GENERIC else 0.55


def canonical_for(label: str) -> tuple[str, float]:
    """The canonical field a label names, with how sure that is. ("", 0) when none."""
    label_words = words(label)
    if not label_words:
        return "", 0.0
    best, score = "", 0.0
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            s = _alias_score(label_words, alias)
            if s > score or (s == score and s > 0 and len(alias) > len(_longest(best, label_words))):
                best, score = field, s
    return best, score


def _longest(field: str, label_words: list[str]) -> str:
    return max((a for a in _ALIASES.get(field, ()) if set(a.split()) <= set(label_words)), key=len, default="")


# ---- values: do they fit, and how are they written ----------------------------------------------------------------------

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^[+()\d\s.\-]{6,24}$")
_URLISH = re.compile(r"^(https?://)?[\w-]+(\.[\w-]+)+([/?#]\S*)?$", re.IGNORECASE)
_NUMBERISH = re.compile(r"^-?\d+([.,]\d+)?%?$")


def fits(kind: str, value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    if kind == "email":
        return bool(_EMAIL.match(value))
    if kind == "tel":
        return bool(_PHONE.match(value)) and sum(c.isdigit() for c in value) >= 6
    if kind == "url":
        return bool(_URLISH.match(value))
    if kind == "number":
        return bool(_NUMBERISH.match(value))
    if kind == "date":
        return parse_date(value) is not None
    if kind != "textarea" and "\n" in value:
        return False
    return True


_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
           "december"]


def parse_date(value: str, day_first: bool = True) -> dt.date | None:
    """A date as people save it. Numeric day and month that could be either are read day first, as in India, unless
    ``day_first`` is False; ``date_is_clear`` tells whether that reading was needed."""
    text = str(value or "").strip().lower().replace(",", " ")
    try:
        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
        if m:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
        if m:
            a, b = int(m[1]), int(m[2])
            if a > 12:
                return dt.date(int(m[3]), b, a)
            if b > 12:
                return dt.date(int(m[3]), a, b)
            return dt.date(int(m[3]), b, a) if day_first else dt.date(int(m[3]), a, b)
        m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\s+(\d{4})", text)
        if m and (month := _month_number(m[2])):
            return dt.date(int(m[3]), month, int(m[1]))
        m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})", text)
        if m and (month := _month_number(m[1])):
            return dt.date(int(m[3]), month, int(m[2]))
    except ValueError:
        return None
    return None


def _month_number(word: str) -> int | None:
    word = word.lower()
    return next((i + 1 for i, name in enumerate(_MONTHS) if len(word) >= 3 and name.startswith(word)), None)


def date_is_clear(value: str) -> bool:
    """False for "03/04/2000", which is two different dates depending on the reader."""
    return parse_date(value, True) == parse_date(value, False)


def write_date(when: dt.date, pattern: str) -> str | None:
    """``when`` in a form's own format ("DD/MM/YYYY", "mm-dd-yyyy", ...); None when the pattern says nothing clear."""
    p = str(pattern or "").upper()
    m = re.search(r"(DD|MM|YYYY|YY)([-/. ])(DD|MM|YYYY|YY)\2(DD|MM|YYYY|YY)", p)
    if not m:
        return None
    parts = {"DD": f"{when.day:02d}", "MM": f"{when.month:02d}", "YYYY": f"{when.year:04d}", "YY": f"{when.year % 100:02d}"}
    if {m[1], m[3], m[4]} not in ({"DD", "MM", "YYYY"}, {"DD", "MM", "YY"}):
        return None
    return m[2].join(parts[m[i]] for i in (1, 3, 4))


def same_value(expected: str, actual: str | None, kind: str = "text") -> bool:
    """What the field holds now is what was meant, allowing for what pages do to a value (an email lowered, a phone number
    formatted, "https://" added)."""
    if actual is None:
        return False
    want, got = str(expected).strip(), str(actual).strip()
    if want == got:
        return True
    if kind == "email":
        return want.lower() == got.lower()
    if kind == "tel":
        a, b = re.sub(r"\D", "", want), re.sub(r"\D", "", got)
        return len(a) >= 6 and (a == b or a.endswith(b) and len(b) >= 7 or b.endswith(a))
    if kind == "url":
        norm = lambda u: re.sub(r"^https?://(www\.)?", "", u.lower()).rstrip("/")  # noqa: E731
        return norm(want) == norm(got)
    if kind == "date":
        a, b = parse_date(want), parse_date(got)
        return a is not None and a == b
    if kind == "number":  # a number box writes "01" as 1
        try:
            return float(want.replace(",", ".")) == float(got.replace(",", "."))
        except ValueError:
            return False
    return " ".join(want.split()).lower() == " ".join(got.split()).lower() if kind in ("select", "combobox", "listbox", "radio") \
        else " ".join(want.split()) == " ".join(got.split())


_OPTION_SAME = {"india": ("in", "ind", "bharat"), "united states": ("us", "usa", "u s a", "united states america", "america"),
                "united kingdom": ("uk", "gb", "great britain", "britain"), "male": ("m", "man"), "female": ("f", "woman"),
                "yes": ("y", "true"), "no": ("n", "false")}
_PLACEHOLDER_OPTION = re.compile(r"^(select|choose|pick|please|--|—|–|\.\.\.|…|none selected)", re.IGNORECASE)


def is_placeholder_option(text: str) -> bool:
    return not str(text or "").strip() or bool(_PLACEHOLDER_OPTION.match(str(text).strip()))


def match_option(value: str, options: list[str]) -> tuple[str, float]:
    """The option that says ``value``: ("India", 1.0) for "India"; ("", 0) when none is good enough. Two equally good
    options are no match: choosing either would be a guess."""
    want = " ".join(words(value)) or str(value or "").strip().lower()
    if not want:
        return "", 0.0
    scored: list[tuple[float, str]] = []
    for option in options:
        if is_placeholder_option(option):
            continue
        have = " ".join(words(option)) or option.strip().lower()
        if have == want:
            score = 1.0
        elif have in _OPTION_SAME.get(want, ()) or want in _OPTION_SAME.get(have, ()):
            score = 0.92
        elif set(want.split()) <= set(have.split()) and len(want) >= 3:
            score = 0.85  # "India" in "India (+91)"
        elif set(have.split()) <= set(want.split()) and len(have) >= 3:
            score = 0.8
        else:
            continue
        scored.append((score, option))
    if not scored:
        return "", 0.0
    scored.sort(key=lambda s: -s[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return "", 0.0
    return scored[0][1], scored[0][0]


# ---- a field and the saved entries ------------------------------------------------------------------------------------

def entry_canonical(entry: dict) -> str:
    named = str(entry.get("field") or "")
    if named in FIELDS:
        return named
    found, score = canonical_for(str(entry.get("label") or ""))
    return found if score >= AUTO_FILL else ""


def refused_reason(label: str, kind: str, secure: bool = False) -> str:
    if secure or kind == "password":
        return "a password field: you sign in yourself, XENIO never fills these"
    if secret_label(label):
        return "a password, code or payment field: XENIO never fills these"
    if re.search(r"captcha|not a robot|verify (that )?you are human", str(label or ""), re.IGNORECASE):
        return "a check that a person is here: only you can do this"
    return ""


def _candidate(entry: dict) -> dict:
    return {"id": entry["id"], "label": entry.get("label") or LABELS.get(entry_canonical(entry), "")}


def _choose_among(found: list[dict], label_words: list[str]) -> dict | None:
    """Several entries of one kind (a work and a home address): the one whose own name says what the field says, or None.
    A sole entry always stands."""
    if len(found) == 1:
        return found[0]
    wanted = set(label_words)
    scored = [(len(set(words(e.get("label") or "")) & wanted), e) for e in found]
    top = max(s for s, _ in scored)
    best = [e for s, e in scored if s == top]
    return best[0] if top > 0 and len(best) == 1 and any(w not in _BENIGN for w in set(words(best[0].get("label") or "")) & wanted) else None


def _derive(canonical: str, by_field: dict[str, list[dict]], kind: str) -> tuple[str, float]:
    """(how, confidence) for a value made from other entries."""
    def only(name):
        found = by_field.get(name) or []
        return found[0] if len(found) == 1 else None
    if canonical == "full_name" and only("first_name") and only("last_name"):
        return "full_name", 0.9
    if canonical in ("first_name", "last_name") and (full := only("full_name")):
        return canonical, 0.85 if len(str(full.get("value") or "").split()) == 2 else 0.6
    if canonical == "address" and only("address_line1") and (only("city") or only("postal_code")):
        return "address", 0.85
    if canonical == "address_line1" and only("address"):
        return "address_line1", 0.6
    return "", 0.0


def derive_value(how: str, by_field: dict[str, list[dict]]) -> str:
    def value(name):
        found = by_field.get(name) or []
        return str(found[0].get("value") or "").strip() if len(found) == 1 else ""
    if how == "full_name":
        return " ".join(v for v in (value("first_name"), value("middle_name"), value("last_name")) if v)
    if how in ("first_name", "last_name"):
        parts = value("full_name").split()
        return (parts[0] if how == "first_name" else parts[-1]) if parts else ""
    if how == "address":
        return ", ".join(v for v in (value("address_line1"), value("address_line2"), value("city"), value("state"),
                                     value("postal_code"), value("country")) if v)
    if how == "address_line1":
        return value("address")
    if how.startswith("dob_"):
        when = parse_date(value("date_of_birth"))
        if when is None:
            return ""
        return {"dob_day": str(when.day), "dob_month": _MONTHS[when.month - 1].capitalize(), "dob_year": str(when.year)}[how]
    return ""


def group_entries(entries: list[dict]) -> dict[str, list[dict]]:
    by_field: dict[str, list[dict]] = {}
    for entry in entries:
        canonical = entry_canonical(entry)
        if canonical:
            by_field.setdefault(canonical, []).append(entry)
    return by_field


def match_field(label: str, kind: str, entries: list[dict], *, section: str = "", group: str = "", placeholder: str = "",
                secure: bool = False) -> Match:
    """The saved detail for one field."""
    reason = refused_reason(label, kind, secure) or refused_reason(placeholder, kind, secure)
    if reason:
        return Match("refused", reason=reason)
    if kind == "file":
        return Match("no_match", reason="a file upload: XENIO cannot choose files yet, you attach it yourself")
    if kind in ("checkbox", "checkboxes") and _AGREEMENT.search(label):
        return Match("no_match", reason="an agreement or a choice only you can make")
    name = label or placeholder
    label_words = words(name)

    # 1. an entry saved under this very name, even for "Father's name": the user chose that wording. Not for a plain "Email"
    # in a section about someone else ("Nominee details"): the word there is the form's, and the email is the nominee's
    around_someone_else = bool(_SOMEONE_ELSE.search(f"{section} {group}"))
    if not around_someone_else:
        for entry in entries:
            if label_words and words(entry.get("label") or "") == label_words and fits(_value_kind(kind), entry.get("value")):
                return Match("ready", entry["id"], canonical=entry_canonical(entry), confidence=0.96)
    if around_someone_else or _SOMEONE_ELSE.search(" ".join((label, placeholder))):
        return Match("no_match", reason="this asks about someone else, not you")
    if _NOT_A_DETAIL.search(name):
        return Match("no_match", reason="not a personal detail")

    by_field = group_entries(entries)
    # dates split in three boxes: "Day" under "Date of birth"
    part = {("day",): "day", ("day", "month"): "day", ("date",): "day", ("month",): "month", ("year",): "year"}.get(tuple(label_words))
    if part and re.search(r"birth|\bdob\b", f"{section} {group}", re.IGNORECASE) and len(by_field.get("date_of_birth") or []) == 1:
        return Match("ready", derived=f"dob_{part}", canonical="date_of_birth", confidence=0.9)

    canonical, score = canonical_for(name)
    if score < AUTO_FILL and placeholder and placeholder != name:
        other, other_score = canonical_for(placeholder)  # "Contact" with placeholder "you@example.com" says little; a word does
        if other_score > score:
            canonical, score = other, other_score
    # the field's own type speaks when its name does not
    if score < AUTO_FILL and kind in ("email", "tel"):
        canonical, score = ("email" if kind == "email" else "phone"), 0.85
    if label_words in (["contact"], ["contact", "details"]):
        found = [e for f in ("phone", "email") for e in by_field.get(f, []) if fits(_value_kind(kind), e.get("value"))]
        if len(found) == 1:
            return Match("ambiguous", found[0]["id"], canonical=entry_canonical(found[0]), confidence=0.6,
                         candidates=[_candidate(found[0])], reason="the field says only 'contact'")
        if found:
            return Match("ambiguous", confidence=0.5, candidates=[_candidate(e) for e in found],
                         reason="the field says only 'contact'")

    if canonical and score >= ASK:
        found = [e for e in by_field.get(canonical, []) if fits(_value_kind(kind), e.get("value"))]
        if found:
            chosen = _choose_among(found, label_words)
            if chosen is None:
                return Match("ambiguous", canonical=canonical, confidence=round(score, 2),
                             candidates=[_candidate(e) for e in found], reason="more than one saved detail fits")
            status = "ready" if score >= AUTO_FILL else "ambiguous"
            return Match(status, chosen["id"], canonical=canonical, confidence=round(score, 2),
                         candidates=[] if status == "ready" else [_candidate(chosen)],
                         reason="" if status == "ready" else "the field's name does not clearly say this")
        if by_field.get(canonical):
            return Match("no_match", canonical=canonical, reason="the saved detail does not fit this field's type")
        how, derived_score = _derive(canonical, by_field, kind)
        if how:
            confidence = round(min(score, derived_score), 2)
            status = "ready" if confidence >= AUTO_FILL else "ambiguous"
            return Match(status, derived=how, canonical=canonical, confidence=confidence,
                         reason="" if status == "ready" else "this would be split out of another saved detail")

    # 2. the name is close to a custom entry's
    close = []
    for entry in entries:
        theirs = set(words(entry.get("label") or ""))
        ours = set(label_words)
        if theirs and ours and len(theirs & ours) / len(theirs | ours) >= 0.6 and fits(_value_kind(kind), entry.get("value")):
            close.append(entry)
    if close:
        return Match("ambiguous", confidence=0.65, candidates=[_candidate(e) for e in close],
                     canonical=entry_canonical(close[0]) if len(close) == 1 else "",
                     entry_id=close[0]["id"] if len(close) == 1 else "", reason="the names are close, not the same")
    return Match("no_match", canonical=canonical if score >= ASK else "", reason="nothing saved for this")


def _value_kind(kind: str) -> str:
    return "text" if kind in _CHOICE_KINDS or kind == "checkbox" else kind


# ---- how much care a form's submission takes ---------------------------------------------------------------------------

_IMPACT = (
    ("financial", re.compile(r"\b(pay|payment|checkout|purchase|buy|order|invoice|billing|credit|debit|loan|emi|bank|invest"
                             r"|insurance|tax|donat\w*|transfer|wallet|subscription plan)\b", re.IGNORECASE)),
    ("legal", re.compile(r"\b(agree|agreement|terms|consent|declaration|declare|affidavit|signature|sign here|contract|legal"
                         r"|undertaking|certify|nda|waiver|power of attorney)\b", re.IGNORECASE)),
    ("government", re.compile(r"\b(passport|visa|aadh?aa?r|\bpan\b|government|licen[cs]e|voter|immigration|census)\b", re.IGNORECASE)),
    ("medical", re.compile(r"\b(medical|health|diagnosis|prescription|patient|disab\w*)\b", re.IGNORECASE)),
    ("account", re.compile(r"\b(delete account|close account|deactivate|change password|reset password|recovery)\b", re.IGNORECASE)),
)


def impact(texts: list[str]) -> list[str]:
    """What makes submitting this form high impact, from the words on it. Empty for an ordinary form."""
    joined = " ".join(str(t or "") for t in texts)
    return [name for name, pattern in _IMPACT if pattern.search(joined)]
