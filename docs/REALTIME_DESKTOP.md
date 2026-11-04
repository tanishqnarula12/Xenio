# Real-time desktop engine (V4 phase 1)

XENIO used to see the screen by sending a screenshot to a vision model: 22 s per look (median of 58 real looks, up to
68 s). A desktop request with a look took 50.7 s, one without took 17.2 s. Yet all 59 questions the model really asked were
about text ("read the error", "what does the editor show"). The engine answers those from the window itself and keeps the
vision model for what only pixels can show.

```
xenio_look_at_screen
   guard: consent, then "is this a running test?" (title or page)      never read, by any route
   1. accessibility  the window's own text and controls                0.2-0.6 s (a web page too: WebKit's tree)
   2. on-device OCR  a window whose tree shows nothing                 0.2-0.35 s, nothing leaves the Mac
   3. vision model   pictures, charts, how something looks; or no text  10-22 s; need_pixels=true asks for it directly
```

| Piece | File | What it does |
|---|---|---|
| Perception ladder | `app/desktop/perception.py` | the order above, change detection, what a look returns |
| OCR | `app/desktop/ocr.py` | Apple's Vision framework, in the accessibility helper process |
| Verified actions, batches | `app/desktop/service.py` | `expect`, `batch`, `pick_window`, `publish` |
| Guard | `app/safety/policy.py`, `hermes_plugin/xenio-guard` | `live_test_page`, `screen_look_on_live_test`, `_classify_batch` |
| Deterministic commands | `app/fastpath.py`, `app/language/detect.py` | "click Compile and Run", "scroll down", "press enter" without a model |
| Numbers | `app/desktop/metrics.py` | one line per step, numbers only: `--desktop-stats`, System page |

## Seeing

* **A look returns words.** `read_by` says how, `windows` which ones (app, title, full screen or not). For the whole screen
  it reads the windows really visible, front to back (at most 3, never XENIO's own).
* **A long window also says what is on screen.** An IDE's tree held 60 000 characters: past the 12 000 sent, OCR adds what
  the window shows right now.
* **Unchanged is reported, not re-sent.** The same text again in one request comes back as `unchanged: true` without the
  text (`fresh=true` asks for it again). In a new request the text is sent, with `changed_since_last_look: false`.
* **Vision answers are reused** when the screen is the same (the menu bar's clock left out) and the question means the same
  ("what is the error?" / "read the error exactly"). The old key needed identical wording and pixels: 0 of 58 looks hit it.
  Comparing is strict on purpose: even a blinked caret counts as a change, since a wrong reuse is worse than one more call.
* **Controls an app does not expose** are found by their visible words (`find_ui` with `level: "ocr"`). A line is clickable
  only if it is the name itself: "run" inside a sentence is not a control called Run.

`XENIO_PERCEPTION=vision` goes back to always using the vision model. If reading a window fails, the look falls back to
vision by itself.

## Acting

* **`expect`** on any action says what should be true afterwards: `text`, `gone`, `element`, `title`, `timeout` (seconds, at
  most 10). XENIO polls the UI (from 50 ms) instead of waiting blindly and taking two screenshots. The result says
  `verified` true or false; what the window holds is not returned. A stop during the wait is `verified: null`, never a
  failed check.
* **`batch`** runs up to 8 of click, double_click, scroll, key, type and wait in one call, with no model turn between them.
  **Every step is judged by the policy right before it runs, on the UI as it is then.** It stops before anything needing
  the user's yes (`needs_confirmation`, run that step alone and XENIO asks), at anything refused (`blocked`), at the first
  surprise (`not_as_expected`), and on a stop or a newer request (`cancelled`). The result lists what ran and what remains,
  so the model can plan again from there. A control not found yet is looked for once more (`recovered`).
* **The fast path** takes short, complete commands ("click Run", "Run pe click karo", "scroll down", "press enter") with the
  same policy on the same facts. Whatever needs a yes, or a control it cannot find exactly, goes to the model. The reply
  says only what the screen confirmed.
* **XENIO's own windows are never the target** unless named: the quick chat holds the keyboard while it is open.

## One look, one plan, done

Looking stopped being the slow part: reading a window takes about 40 ms, and only pictures need the vision model's 13 to 34 s. Whole tasks still took about 19 s, because a slow model took a turn before and after every step. So a
job on what is on the screen ("open the plans page and choose the cheapest plan") now goes differently (`app/desktop/quickplan.py`, tried by `app/fastpath.py`):

```
the window as JSON (text + controls with ids)   about 0.2 s, no screenshot
one call to Nabh's glm-5-2: the whole plan      1 to 3 s
the plan runs as one batch                      no model turn between steps; every step judged by the policy right before it runs
a new page?                                     read it, plan again, at most 3 times
```

| Real local pages, real WebKit, real clicks, real model | Time |
|---|---|
| Open a page and choose the cheapest plan (2 pages, 2 plans) | 4.5 s |
| Scroll down a bit | 1.8 s |

It stays out of anything consequential (`pay`, `send`, `delete`, `submit`, `log in`...) so the main model can ask first. A plan that heads for such a button anyway stops before it: tested on a real "Place order" button, nothing was ordered. If it cannot plan, or needs a yes, the main
model takes over and is told what already happened. It is refused on running tests like every other look. The smallest Nabh model produced no usable plans, so it uses `glm-5-2`. The window's text goes to Nabh, with passwords never read and secrets masked.

## Rules that did not change

Consent for looking at the screen, masked secrets, untrusted-content marking, one input action at a time, the emergency
stop, and every approval. New: **a running test or contest is not read by any route**, including a screenshot and
`xenio_find_ui`, and is recognised by what its page shows as well as by its title: a countdown to its end ("Contest Ends in
00 : 01 : 40 : 49") or the control that ends it ("Finish Contest"). Practice pages, past and virtual contests, promotions and
articles that mention exams are not affected (checked against every real practice page in the history: 10 of 10 untouched).

## Measured

Real Safari, real Accessibility and real input on local demo pages (real Ace editor), a real Calculator, and the real free
vision model on a generated test card. No private pixels were sent.

| | Before | After |
|---|---|---|
| A look that text can answer | 22 s median, 48 s p90 | 0.2-0.6 s |
| A window whose tree shows nothing | 22 s | 0.2-0.35 s (OCR through the helper) |
| The same look again in one request | 22 s, 0 of 58 reused | about 10 ms, nothing re-sent |
| The same vision question reworded | a new model call | 0 ms, no model call |
| Calculator: 7 x 6 =, checked for 42 | 4+ model turns | 0.9 s, one call, verified |
| Three-step wizard, each step checked | 3+ model turns | 1.8 s, one call, verified |
| "Click Compile and Run" | a model turn (about 17 s) | 0.74 s, no model |
| Editor write behind XENIO's chat | failed: "that window no longer exists" | verified, run, full screen kept |

"Before" is 58 real looks and 120 real desktop requests in `xenio-guard.log` and Hermes' `state.db`. Not measured yet: whole
requests through the live model after this change. `python -m app.main --desktop-stats` and Mission Control's System page
report them from real use.

## Limits

* "DOM" here means the accessibility tree a browser exposes (roles, names, text, the page's CSS classes), not JavaScript in
  the page. Reading scripts' state or shadow details is not possible this way.
* OCR reads what is drawn: it can misread small text, and it never sees what is scrolled away.
* There is no stronger reasoning model to escalate to: the ladder ends at the configured vision model (no paid models
  without the user's choice).
* A page's own full screen (an editor's button) ends on any Esc and only the page can bring it back: this is reported.
* The orb's step-by-step label is tested in the frontend and backend, but has not yet been seen in a live session.

## Turning it on

The running gateway keeps its old code until XENIO restarts:

```bash
.venv/bin/python -m app.main --install-guard
(cd ui && npm run build)
.venv/bin/python -m app.main --ui
```
