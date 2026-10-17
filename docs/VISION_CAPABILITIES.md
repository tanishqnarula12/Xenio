# Vision capabilities (V5 audit, 2026-10-04)

What Hermes Agent 0.21.5 (as installed on this PC) can already do with images, what the configured models
accept, and how XENIO integrates screen vision. All statements below were checked on this machine. The
model-support values come from Hermes' own catalog (`agent.image_routing`); gpt-6-luna was also proven live.

## What Hermes already has

| Capability | Hermes | In XENIO's Hermes sessions (`hermes-acp` toolset) | XENIO's use |
|---|---|---|---|
| Analyse an image (URL, local file, `data:`) | `vision_analyze` (`tools/vision_tools.py`) | yes | kept, now **path-checked** by XENIO's policy (V5 fix) |
| Native image routing | `agent/image_routing.py`: a vision-capable main model gets the pixels in its context ("native"); otherwise an auxiliary vision model describes them as text | yes | not used for the screen (see below) |
| Browser screenshots | `browser_vision` (screenshot of Hermes' own automation browser + analysis) | yes | reused for pages in Hermes' browser (V3 rules apply) |
| Desktop control | `computer_use` (cua backend: screenshots, mouse, keyboard) | **no** (excluded from `hermes-acp`) | not used; XENIO's policy keeps it CONFIRM_REQUIRED |
| Video | `video_analyze` | no (opt-in toolset) | path-checked if ever enabled |
| Image attachments over ACP | `acp_adapter/content.py` accepts image blocks in a prompt | yes | not needed: XENIO sends speech/text |
| Multimodal tool results | `{"_multimodal": true, ...}` envelopes; Hermes stores only their text summary | yes | not used (XENIO returns text) |
| OCR | no separate OCR; vision models read text in images | - | the vision model reads text (no new dependency) |
| Image resizing | Pillow (missing in Hermes' environment here: `hermes pm repair` would restore it) | - | XENIO does not need it: Windows scales the capture |
| Screen capture of the user's desktop | only through `computer_use` | no | **added by XENIO** (`app/vision/capture.py`) |

## Model compatibility

| Model (Hermes' chain) | Image input (Hermes catalog) | Verified |
|---|---|---|
| **gpt-6-luna** via `openai-codex` (main) | yes, "native" | **yes, live**: read a terminal error and a code word from a test card (3.9 s) and real screens in the acceptance tests |
| `thinkingmachines/inkling:free` via OpenRouter (fallback 1) | yes | not tested (free, rate-limited) |
| `nvidia/nemotron-3-ultra-550b-a55b:free` (fallback 2) | **no** (text only) | - |
| `gemini-3.8-flash` via Google AI Studio (fallback 3) | yes | not tested |

CURRENT MODEL: gpt-6-luna through the ChatGPT sign-in (Codex). It supports image input, so **no paid
vision model is needed**: screen looks use the same subscription as every other XENIO request.

VISION-CAPABLE OPTIONS (only if you want something else; XENIO never switches on its own):

| Option | How | Cost |
|---|---|---|
| gpt-6-luna (default) | `XENIO_VISION_PROVIDER=main` | your ChatGPT plan's Codex quota (no per-call charge) |
| Hermes' auxiliary routing | `XENIO_VISION_PROVIDER=auto` | may use OpenRouter / Nous / others: **pay per use** |
| nabh.cloud `qwen2-5-vl-72b` or `paddleocr-vl` (in your nabh model list) | `XENIO_VISION_PROVIDER=openai-compatible` + `XENIO_VISION_API_URL` / `XENIO_VISION_MODEL` / `XENIO_VISION_API_KEY` | nabh's pricing (not checked) |
| gemini-3.8-flash with your AI Studio key | `openai-compatible` with Google's OpenAI-compatible endpoint | AI Studio free tier limits |

RECOMMENDED OPTION: keep the default (gpt-6-luna). It is the model that also reasons about the answer, it
read text and errors correctly in every test, and it costs nothing extra.

## Two problems found during the audit (both handled)

1. **Silent paid fallback.** Asked explicitly for `openai-codex`, Hermes' auxiliary vision call fell back to
   OpenRouter when the Codex credential was not selectable, and a test image was analysed there (cost:
   $0.00016, the only OpenRouter usage on the key). XENIO therefore resolves the provider itself and calls
   exactly that client; if it is not available, the look fails with the reason (`app/vision/providers.py`).
2. **Codex blocked for auxiliary requests.** Hermes' credential pool had put the Codex sign-in on long
   cooldowns for models the ChatGPT plan refused (gpt-6-sol, gpt-5.4, ... during the V3 model setup). A
   request that names no model is blocked while any cooldown is active, and auxiliary requests (vision)
   name none. Fixed with Hermes' own `hermes auth reset openai-codex` (clears statuses and cooldowns; the
   sign-in tokens are untouched). `--health` warns if it happens again, and the tool's error names the fix.

## Recommended integration path (implemented)

Vision is a **sense of XENIO, not a second assistant**: Hermes decides when it needs to see, calls
`xenio_look_at_screen`, gets TEXT back and reasons with it like any other tool result.

* XENIO captures the screen itself (Hermes' only desktop capture is `computer_use`, which also clicks and types).
* The screenshot is described by the main model through Hermes' own model access (strict, no fallback).
* Text instead of native pixels: XENIO can then mask secrets the model copied and detect instructions
  written on the screen before the main model sees anything, and no image ever enters the conversation
  or Hermes' session database.
* Pages in Hermes' browser keep using `browser_vision`; pages in the user's browser are on the screen.

See [VISION_ARCHITECTURE.md](VISION_ARCHITECTURE.md) and [VISION_SECURITY.md](VISION_SECURITY.md).

## Limitations

* Windows only (GDI capture). The primary display and the active window; no second monitor selection by name.
* Windows that block screenshots (DRM video, some banking or password apps) and the lock screen capture
  black: XENIO says so instead of guessing.
* Small text: the screen is sent at 1600 px on the long side (OpenAI models work at most at 768 px on the
  short side anyway). Very small fonts can be unreadable; the model is told to say so.
* The vision model only sees one moment: no video, no "watch this window" (not part of V5).
