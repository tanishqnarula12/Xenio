"""The ways of being taught. ``compose`` is what shapes a written lesson; ``guide`` is how XENIO behaves in conversation."""

from __future__ import annotations

MODES: dict[str, dict[str, str]] = {
    "teach": {"label": "Teach me",
              "compose": "A structured lesson, step by step, from what the learner needs first to the goal. Include a flowchart or mind map of the whole idea.",
              "guide": "Teach one small step at a time, then ask one question to see if it landed, and use the answer to decide what comes next."},
    "simple": {"label": "Explain simply",
               "compose": "Explain it as simply as it can be, with an everyday analogy first, short sentences and no jargon that is not explained. One simple diagram.",
               "guide": "Use everyday words and an analogy. If the learner is still unsure, try a different analogy, not the same words again."},
    "visual": {"label": "Show visually",
               "compose": "Teach mainly through diagrams: at least three that show different things (the structure as a mind map, the process as a flowchart, and how the parts relate or change). Words only support them.",
               "guide": "Lead with the diagrams on the lesson card and talk through them. A picture from xenio_image only when a real image shows something a diagram cannot (a place, an organism, an object)."},
    "hints": {"label": "Give me hints",
              "compose": "Do not state the answers to the practice problems anywhere. Each question has three hints, from a nudge to almost the answer. The worked example must be a different problem.",
              "guide": "Never give the answer first. On each request give the next hint with xenio_teach hint, and praise real reasoning. Reveal only if the learner asks to see it."},
    "quiz": {"label": "Quiz me",
             "compose": "Mostly practice: a short recap, then at least six varied questions from easy to hard, mixing multiple choice and open ones.",
             "guide": "Ask one question at a time, wait for the answer, grade it with xenio_teach answer, explain why in a sentence or two, then the next."},
    "test": {"label": "Test my understanding",
             "compose": "Questions that test understanding, not memory: why, what if, spot the mistake, apply it to a new case. At least five, and at least two open ones.",
             "guide": "Ask varied questions that need real understanding. After each, find out what the learner really thinks, not only whether it was right."},
    "mistakes": {"label": "Review my mistakes",
                 "compose": "Built around this learner's own mistakes listed below: for each, what the wrong idea was, why it feels right, what is true, and a new question to check it is fixed.",
                 "guide": "Go through the learner's recorded mistakes one by one: what they thought, why that is tempting, what is true, then a fresh question on it."},
    "material": {"label": "Study with my material",
                 "compose": "Use only the learner's material below for what it says, and cite it as [S1], [S2]. If it does not cover something, say so in 'uncertain' rather than filling the gap from memory.",
                 "guide": "Answer from the learner's material: search it with xenio_teach search_material, and cite the source and page. If it does not say, tell them, and only then offer general knowledge, marked as such."},
}
DEFAULT_MODE = "teach"
RULES = ("A good tutor: find out the goal and level when they matter, one short question at most. Explain prerequisites before what depends on them. "
         "Use worked examples. Use the learner's own answers to decide what next. Revisit weak ideas. Say what you are unsure of, and never invent "
         "a fact, source or quote. Mastery comes only from answers recorded with xenio_teach, never from your impression.")


def mode_of(name: str | None) -> str:
    wanted = str(name or "").strip().lower().replace(" ", "_")
    aliases = {"explain": "simple", "explain_simply": "simple", "visual": "visual", "show_visually": "visual", "hint": "hints", "give_me_hints": "hints",
               "quiz_me": "quiz", "test_my_understanding": "test", "review": "mistakes", "review_my_mistakes": "mistakes", "study": "material",
               "study_with_my_material": "material", "teach_me": "teach"}
    wanted = aliases.get(wanted, wanted)
    return wanted if wanted in MODES else DEFAULT_MODE
