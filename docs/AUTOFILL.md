# Profile vault, forms and browser work (V4 phase 2)

Say "fill this form" and XENIO fills it from your saved details, checks every field, and leaves the submit to you. Ask it to do
a job on the web and it works step by step in your own browser, where you are signed in. No screenshot or vision model is
involved in any of this: everything is read straight from the page.

```
"fill this form"     no model, nothing leaves the Mac     read, map, fill, check, on through "Next"     about 1 s
anything longer      the model, with names only           xenio_form, xenio_browser, one call per step  a model turn each
```

| Piece | File | What it does |
|---|---|---|
| Vault | `app/personal/vault.py` | your details, encrypted, refusing secrets |
| Mapping | `app/autofill/matching.py` | which detail goes in which field, or a question |
| Reading | `app/autofill/forms.py` | a page's controls become named fields and questions |
| Filling | `app/autofill/engine.py` | scan, fill, next, review, submit, stop |
| Browser work | `app/desktop/browser.py` | open, page, click, type, back, forward, tabs |
| Rules | `app/safety/policy.py` | `classify_profile_tool`, `classify_form_tool`, `classify_browser_tool_work`, `graded_form` |
| Page | Mission Control > Profile | see, add, correct, delete, and the last form's outcome |

## The vault

* **Encrypted at rest, names included.** AES-256 with HMAC-SHA256 through CommonCrypto (part of macOS, no new dependency). The
  key is a random value in the login Keychain, written through `security -i` on stdin so it never shows in a process list.
* **Separate from memory.** Memory is in every prompt. The vault is read only at the moment a form needs it.
* **The model sees names, not values.** Values go straight from the vault into the form. A result lists fields and statuses.
  If you ask in chat what an ordinary detail says, that answer does go through the model; a sensitive one never does.
* **Refused outright:** passwords, codes, tokens, API keys, card and bank details, security answers. In the vault and in forms.
* **Sensitive** (identity documents, salary, health, or your own tick): hidden on the page, never given to the model, and XENIO
  asks before using it. Once you say yes it is not asked again for that form. A later read of that page has it masked.
* **Only what you give.** What you say in your own words is saved directly; anything else offered for saving asks first.
* **Deleting** erases the record, and takes effect at once: values are read at the moment of filling, never cached, so a detail
  deleted while a form is open cannot go in.

Protects against the file being copied, backed up or read by a tool, and values reaching logs or prompts. It does not protect
against another program running as you, which could ask Keychain the same way. XENIO's own terminal is refused that command, and
its file tools are refused the file.

## Filling

* **Fields:** text, email, phone, URL, number, text areas, native dates and drop-downs, the page's own lists, single tick boxes.
* **Questions:** multiple choice, several tick boxes on one question, grids (a question per row), "Other" with its own box.
* **What goes in:** only what is certain (confidence 0.8 or more). A label naming someone else ("Emergency contact", "Father's
  name", anything under "Nominee details") never gets your detail unless you saved one under that very name.
* **What stays out:** a field a person cannot see (off the page, tiny, inside a box with no height, or under other controls),
  passwords, anything already holding your text (unless you say to replace it), agreements and "I consent" boxes (it asks).
* **Checked:** every field is read back, then once more at the end, because pages change values. What a page reformats (a phone
  number, a lowercase email, `01` in a number box) still counts; anything else is reported as not entered.
* **Stoppable:** "Xenio stop", Esc or a newer request end it between two fields. The result says what was done and what was not.
* **Asks once:** everything it needs from you comes in one message. Answers are remembered for that form, by question, for 30
  minutes, in memory only.
* **Many pages:** with `steps` it goes on through "Next" by itself and stops before Submit, where a required answer is missing, or
  where a new page needs a yes. Every page is judged again.

**Submitting always asks.** The question carries the review: how many fields XENIO filled and what is still empty. Money, legal,
identity and health forms, and any form that used a sensitive detail, need a real "confirm". Afterwards only the page's own
answer counts: `submitted` is true if it says so, false if it refuses, and null if it does not clearly say.

## Graded forms and running tests

**A graded quiz's questions are yours.** A form that shows "1 point" beside a question or "Total points" gets your name and details
from XENIO, but its questions are left alone and answering them is refused. Mock and practice tests are not affected. A running
test or contest is refused at every step, as everywhere else (`REALTIME_DESKTOP.md`).

## Browser work

Your own browser, reached the way a screen reader reaches it, so nothing is exposed that the page does not show a person.

* `open` makes a **new window**. Your tabs stay as they are and the screen stays yours.
* Every action returns the page after it: no separate look, and no model turn just to see what happened.
* `click` needs no mouse and does not take the screen. A page that has not changed comes back as `unchanged` instead of being
  sent again.
* **Yours to do:** signing in (`needs_login`) and checks that a person is here (`human_check`). XENIO asks you and carries on.
* Each click is judged by what it says, as with `xenio_gui`: ordinary ones run, consequential ones ask, payments need "confirm".
* While it works the display is kept awake (`caffeinate`, 90 s at a time, gone by itself): macOS shields every window while the
  display sleeps, measured on battery after 2 idle minutes. It never wakes a display that is already off.

## Control behaviours worth knowing

All measured, in the WebKit engine Safari uses:

| Control | What it does | What XENIO does |
|---|---|---|
| Text field | setting the value without focus reports success and does nothing | focus, then set: the page gets input and change events |
| Google Forms radio | offers no AXPress; a press reports success and does nothing | focus and Space; a real click if that did nothing |
| Tick box | takes AXPress, and answers a moment later | press, then wait for the value |
| `<select>` | its menu does not open through accessibility and its options are not in the tree | focus, then type the option's name |
| Google Forms list | answers neither AXPress nor focus; its options are in the tree while closed | a real click opens it, then press or click the option |
| Native date | parts are named controls; several digits in one key event lose all but the first | focus each part, one key per digit |

**XENIO's quick chat holds the keyboard while it is open**, and activating another app does not take it back. Text, tick boxes and
pressable radios need no keyboard. Before the first key press on a page, XENIO clicks once on plain text of that page (checked
first: no control, this very window) and puts the pointer back. Tested with a stand-in panel: nothing landed in the chat.

## Measured

Real WebKit (a private test browser, not your Safari), real Accessibility and real keys, on local demo pages built from what real
Google Forms reported. Fake details only.

| | Time |
|---|---|
| Reading a form | 230-300 ms the first time, 11-30 ms after |
| One text field | 1-29 ms |
| 9 fields of every kind, checked, with hidden traps left alone | 0.6 s |
| Three pages, six fields, in one call | 0.68 s |
| Fill with the quick chat stand-in holding the keyboard | 0.9-1.3 s |
| Submit, until the page's answer | 0.16 s |
| Reading a page / the same page again | 250 ms / 25 ms, nothing re-sent |
| Clicking a link, until the next page is back | about 0.1 s |

## Limits

* **Real Safari is not fully tested.** Safari's own tree was read and matches, but whole fills ran in the same engine in a private
  browser, to keep your windows out of it. Chrome is not installed here, so Chromium browsers are untested.
* **`open` has not run for real.** It needs your OK for Automation the first time (macOS asks once). Its logic is tested on fakes.
* **Real Google Forms** were read, and one public tutorial form was filled locally and never submitted, before the dropdown and radio
  fixes. Those fixes are tested on a replica of what it reported. A form of your own is the check still to do.
* **The real orb's quick chat** was simulated by a stand-in panel of the same kind.
* **Transparency:** a field hidden only by `opacity: 0` looks like any other to accessibility. It would appear in the result and on
  the Last form card, and sensitive details still ask first.
* **Files** cannot be attached yet: file uploads are reported as yours to do.
* **A "Next" that really submits** cannot be told from a real Next. A page's button named Next is treated as Next.
* **Options that are only pictures** have no words to match: XENIO asks.
* **Date fields read their part names in English.** On a Mac in another language they are reported as not filled, never guessed.

## Turning it on

The running gateway keeps its old code until XENIO restarts:

```bash
.venv/bin/python -m app.main --install-guard
(cd ui && npm run build)
.venv/bin/python -m app.main --ui
```

The prompts changed, so XENIO starts a fresh Hermes session on the next start.
