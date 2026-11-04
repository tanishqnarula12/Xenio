# The Teacher (V4 phase 3)

Ask anything in the chat, in your own words and any language. XENIO works out by itself that you want to learn, and teaches you right there: a well-formatted explanation of one idea, with a flowchart
or diagram wherever one helps, and then buttons for what could come next ("Walk through an example", "Show it as a diagram", "Quiz me"). There is nothing to start, and nothing is asked out of nowhere.

Everything else is there for when you pick it, in the chat:

| You pick | You get |
|---|---|
| A diagram | a drawn flowchart, cycle, mind map or sequence, or one you can click for more |
| Something to try | a graph with sliders, a calculator, code you can run with your own input, a step-through, match, order |
| Practice | one question at a time, hints before answers, and your real progress kept |
| Your notes | add a file to the message: it teaches from them |
| A whole lesson | say "make a full lesson on X", or use More options on the Learn page |

**The Learn page** keeps what you have been learning: your subjects, what is due for review, and your lessons.

| Piece | File | What it does |
|---|---|---|
| Modes | `app/teacher/modes.py` | teach, explain simply, show visually, hints, quiz, test, review mistakes, my material |
| Writing | `app/teacher/composer.py` | four parts side by side, then a second reader |
| Diagrams | `app/teacher/diagrams.py` | structure to Mermaid: flowchart, cycle, mind map, hierarchy, sequence, timeline, comparison |
| Activities | `app/teacher/activities.py` | graph, calculator, code, chart, steps, frames, match, order |
| Code | `app/teacher/sandbox.py` | Python, JavaScript, C and C++, on Run only |
| Material | `app/teacher/materials.py` | PDFs, scans, photos, documents, notes |
| Records | `app/teacher/store.py`, `engine.py` | subjects, concepts, attempts, reviews (`learning.db`) |
| Tool | `app/tools/teacher.py` | `xenio_teach` |
| Page | `ui/src/pages/Learn.tsx`, `ui/src/teach/` | the lesson, its activities, your progress |

## How teaching in the chat works

A typed question about understanding something goes to the teacher (`app/teacher/tutor.py`): Nabh's `glm-5-2`, with clean instructions and the conversation so far, streaming straight into the chat. It has its own head because real chats showed the main model cannot teach: its persona says "replies are read
aloud: keep them short" and its memory says "brief, Hinglish", which gave 80 words on P vs NP, in Hindi to an English question, with a wrong claim in it.

| The same question, from a real chat | Main model | Teacher |
|---|---|---|
| Length | 80 words, 89 after "explain properly" | 649 words |
| Language | Hindi, to an English question | English |
| First words | 7.7 s | 2 s |
| Correct | "P ⊂ NP ⊂ NP-Complete ⊂ NP-Hard" is false | checked by reading; says P vs NP is open |

**Who gets it** is decided from how the message is written (`wants_teaching`), tuned on 529 real messages: questions and requests to understand, a follow-up to something just taught, and questions about an attached file. Anything needing XENIO's hands, the user's own data or today's news stays with the
main model, and so does everything spoken. If Nabh is limiting or down, the main model answers instead (it also has `TEACHING_NOTE`). A reply ends with one line, `<<next: A | B | C>>`, which `app/followups.py` takes out everywhere and the page shows as buttons. The main model is told what was taught, so a later turn knows.

## Diagrams and practice questions in the chat

Diagrams have a clear hierarchy: the main idea stands out, decisions and outcomes have their own look, and steps can carry a small second line. If a model's diagram does not parse, XENIO tries the plain version, then a repaired one, and only then shows the source.

Asking for multiple-choice questions gives a real practice card: progress, letters and keyboard keys, a check button, a hint, why each option is right or wrong, and at the end your score and a retry of the ones you missed. Your progress on a card stays in that browser.

If `glm-5-2` is out of credit, the chat falls back to `deepseek-v4-1-flash`, then `gpt-oss-120b`, so a low balance slows teaching and does not stop it.

## How a whole lesson is written

Nabh's `glm-5-2` writes four parts at once, so a lesson takes as long as its longest part: the explanation, the diagrams, the things to do, the practice. Only the
explanation is essential; a part that fails leaves the rest, and the lesson says which is missing. A second, fresh reading then checks it in the background,
above all the answers to its questions. The card shows what that reader changed. A check can correct but never take away: a part it returned smaller stays as written.

It **refuses to make things up**:

* A subject the writer does not recognise gets an honest "I don't know this well enough", no lesson, and no empty subject left behind. Add your notes and it teaches from them. Notes added later mean a new lesson, not the old one.
* "Study with my material" needs material, and says so before any model is asked.
* A citation has to point at a passage the writer was really given. Any other `[S3]` is taken out.
* What is debated or varies by country or syllabus goes under "What to double-check".

No model can promise it is always right. That is why there is a second reader, and why the first question comes straight after the lesson.

## Your progress is yours

* **Only real answers count.** Every concept's state comes from recorded attempts: not tried, needs practice, shown. Nothing a model says about you changes it.
* **Answers stay hidden** until you have answered. Hints come before the answer; seeing the answer first counts for nothing, and the question comes back.
* **Spaced review:** a right answer moves a concept on (1, 3, 7, 16, 35, 75 days), a wrong one brings it back.
* **Editable:** on the page you can correct a wrong marking, delete an attempt, forget a subject. Marks on code come from the program's own output.
* **Memory** gets only useful learning preferences. Lessons and progress stay in `learning.db`.

## Code that really runs

Terminal style, with your own input. It runs only when you press Run, in `sandbox-exec`: no network, no access to your files, 5 s of CPU, 10 s in all. Tested for real: reading home,
listing it, writing to it, network and curl all fail, and an endless loop dies at 5 s. An exercise is marked by what its checks really printed.

Formulas in graphs and calculators are read by XENIO's own small parser, in Python and in the page. Nothing a model writes is ever evaluated, and diagrams are cleaned before
they are drawn.

## Files in the chat

The paperclip, drag and drop, or paste: PDFs, documents, photos, code, up to 5 files of 80 MB in one message, kept 14 days. What a file says is data: it never counts as your words, cannot approve
anything, and text in it that gives orders is treated as an attempt to give orders. Scans go through `paddleocr-vl`; a photo with no words gets a description from `qwen2-5-vl-72b`.

## Which Nabh model does what

Measured against each other on real requests, not taken from a list.

| Job | Model | Why |
|---|---|---|
| Writing lessons | `glm-5-2` | best of those compared: 19 s for a part, four kinds of diagram; others took 50 to 125 s or failed |
| Checking them | `glm-5-2` | found both planted errors in 11 s; four others failed |
| Marking your words, writing image prompts | `mistral-nemo-12b` | fastest, about 1 s, when Nabh is not busy |
| Searching your material | `qwen3-embedding-4b` | matched English and Hindi at 0.85 against 0.46 for unrelated text |
| Reading scans | `paddleocr-vl` | 2.4 s and exact |
| Pictures | `stable-diffusion-xl` | your choice |

Any of them can be changed in `.env` (`NABH_TEACHER_MODEL` and so on).

## Measured

Real lessons through the real pipeline, 2026-11-02 and 03.

| | Written | With the check |
|---|---|---|
| Binary search | 40 s | 74 s |
| Supply and demand | 46 s | 67 s |
| Newton's second law, in Hinglish | 44 s | 65 s |
| The Preamble of India | 59 s | 85 s |
| How vaccines work (Nabh busy) | 108 s | |
| The Krebs cycle | 61 s | |
| Your own notes, on a subject no model knows | 31 s | |

The second reader found real errors each time, such as swapped labels on a diagram, inconsistent currency, and "the vaccine cannot multiply" when live vaccines do.

## Limits

* **Chat teaching does not update the Learn page's progress yet.** Lessons and their questions do.

* **A whole lesson takes one to two minutes.** Teaching in the chat answers like any other message.
* **Whole lessons:** The chat says it is writing and the orb says "writing the lesson", but it does not appear piece by piece yet. After 140 s it comes with whichever parts are ready, and says which are missing; only the explanation can take longer. A part cut off by the length limit is asked for once more with more room.
* **A wrong answer can still get through** two readers, above all in narrow or recent subjects. Look under "What to double-check".
* **A subject's first visit downloads nothing, but drawing diagrams loads about 5 MB** of Mermaid from XENIO itself the first time a lesson opens.
* C and C++ need Apple's command line tools; without them Run says so.
