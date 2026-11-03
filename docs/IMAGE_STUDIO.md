# Image Studio (V4 phase 4)

Say "generate an image of a harbour at dawn" and a real picture appears in the conversation. The chat stays free while it is made.

```
"generate an image of..."   no turn of the main model    job starts in about 0.01 s      picture in 20 to 60 s
anything less plain         the model, through xenio_image
```

| Piece | File | What it does |
|---|---|---|
| Provider | `app/images/providers.py` | one small interface (`generate`, `sizes`); today Nabh's Stable Diffusion XL |
| Jobs | `app/images/studio.py` | states, duplicates, cancelling, variations, refining, cropping, saving |
| Tool | `app/tools/images.py` | `xenio_image`: generate, variation, refine, retry, cancel, status, list |
| Rules | `app/safety/policy.py` | `classify_image_tool`, `wants_image`; `app/images/studio.py` `refused_prompt` |
| Card | `ui/src/images/ImageCard.tsx`, `GenerationMark.tsx` | the picture in the conversation, and its animation |

## Setting it up

XENIO's `.env` needs a Nabh key. `STT_API_KEY` is used when `STT_API_URL` is on nabh.cloud, or set `NABH_API_KEY` yourself (`.env.example` lists
the names). Without one, a job fails with a message saying what to set; nothing pretends. The key stays on the server: not in the page, not in a job's
record, not in the logs.

## What a job really reports

`preparing` (a quick model improves the prompt) -> `sending` -> `generating` -> `receiving` -> `complete`, or `failed`, or `cancelled`.

Those are real steps. Nabh reports nothing while it generates, so **there is no percentage anywhere**; a number would be made up. Words are never a picture: the
answer's first bytes must really be PNG, JPEG or WebP, or the job fails with `bad_response`.

* **Duplicates:** the same request while one is running, or within 20 s, is the same job. A double click makes one picture. Three on purpose are three.
* **Cancelling:** Nabh cannot be told to stop, so the job says cancelled at once and the picture is thrown away when it arrives.
* **Variation:** the same prompt as the original, word for word, with a new seed, so the idea does not drift.
* **Refine:** the prompt plus your change, with the same seed. The original stays.
* **Save:** only when asked, to Downloads, never over an existing file. The studio keeps its copy.
* **Prompt writer slow or down:** after 8 s your own words go instead. It costs nothing but polish.
* **Two at a time.** More wait their turn; the chat does not.
* **After a restart:** finished pictures are still there; a job whose process went away says so after 7 minutes instead of spinning for ever.

## Shapes

**Nabh's SDXL makes only 1024 x 1024.** Measured 2026-11-03: every other size is refused, and `width`, `height` and `aspect_ratio` are silently ignored. So a
wallpaper is cut from the middle of a square, by macOS's own `sips`, at a true 1024 x 576 (tall: 576 x 1024). The prompt writer is told what will be cut. The
card says so, the whole square is kept as `original.png`, and if cutting fails the job reports the size it really is. A provider that can make wide pictures
only has to list them in `sizes`.

That is sharp enough for a window and soft for a full Retina desktop wallpaper.

## What it will not do

* Sexual images of children are refused, whoever asks, on every route.
* A description holding a key, password or card number never leaves the Mac.
* A picture you did not ask for asks first, because its description goes to the provider. Reading another page or mail can never start one silently.
* Stable Diffusion cannot spell. Flowcharts and diagrams go to the Teacher, where they are drawn from structure.

## Measured

Real SDXL through the real studio, server and page.

| | |
|---|---|
| Job appears | 0.01 s |
| Whole picture, including the prompt writer | 22 to 57 s (Nabh varies; 30 to 39 s was usual) |
| Prompt writer | 1 to 10 s, now cut off at 8 s |
| Cancel | at once; no file left afterwards |
| Cost | Nabh reported 0 |

## Limits

* The model that writes the prompt cannot see the result, and neither can XENIO: it never claims to have seen a picture.
* `seed` and `negative_prompt` are accepted by Nabh; that they change the picture has not been proven.
* A cut from a square loses the top and bottom, so a wallpaper has less in it than a true wide picture would.
