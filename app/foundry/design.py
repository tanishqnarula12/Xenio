"""Why every page came out looking the same, and what stops it.

A model asked for "a strong design" goes to the same place every time, and a prompt that names what good looks like sends it there faster. So XENIO hands out a different art direction for each job, keeps a record of the fonts and colours it has really used, and says not to use them again. A small cheap call picks the direction that suits the subject and the skills that suit that direction, out of all the ones you have installed.
"""

from __future__ import annotations

import random

from app.foundry.messages import neutral

# Each one is a different kind of page, not a colour swap. "ground" keeps it from being dark every time
DIRECTIONS = (
    {"name": "Swiss grid", "ground": "light", "look": "a strict modular grid, flush-left, one sans-serif in few weights, one loud spot colour, a lot of white, numbers as graphic elements", "skills": "typography layout grid"},
    {"name": "Editorial magazine", "ground": "light", "look": "a serif masthead, drop caps, real columns, pull quotes that break the grid, captions, like a printed feature spread", "skills": "typography editorial"},
    {"name": "Cinematic", "ground": "dark", "look": "full-bleed scenes one after another, like the title sequence of a film: slow reveals, wide letterspacing, a single light source", "skills": "animation motion gsap scroll"},
    {"name": "Neo-brutalist", "ground": "light", "look": "raw black borders, hard offset shadows, clashing flat colours, a monospace for labels, things stacked like stickers", "skills": "ui design"},
    {"name": "Retro-futurist", "ground": "either", "look": "the future as imagined in 1975: NASA manuals, instrument panels, chunky rounded type, warm oranges and off-white", "skills": "animation motion"},
    {"name": "Paper collage", "ground": "light", "look": "a cut-and-paste zine: torn paper edges in SVG, pieces at slight angles, tape, stamps, a mix of type like ransom notes", "skills": "ui design"},
    {"name": "Art deco", "ground": "either", "look": "1920s geometry: sunbursts and stepped frames in fine gold lines, tall narrow capitals, strict symmetry", "skills": "typography"},
    {"name": "Bauhaus", "ground": "light", "look": "primary shapes that are the layout itself: big circles, squares and triangles in red, blue and yellow on cream, lowercase geometric type", "skills": "animation layout"},
    {"name": "Instrument panel", "ground": "dark", "look": "a monospace readout: live-looking figures, scales, tick marks, thin hairlines and one signal colour, like lab equipment", "skills": "data animation"},
    {"name": "Soft organic", "ground": "light", "look": "warm natural colours, flowing blobs and hand-drawn lines, a rounded friendly serif, nothing with a hard corner", "skills": "animation ui"},
    {"name": "Quiet luxury", "ground": "light", "look": "enormous empty space, thin high-contrast serif capitals, one material colour, everything slow and restrained, no decoration", "skills": "typography apple"},
    {"name": "Toy box", "ground": "light", "look": "chunky rounded shapes that squash and bounce, bright flat colours, big friendly type, things you want to poke", "skills": "animation motion interaction"},
    {"name": "Blueprint", "ground": "dark", "look": "a technical drawing on cyan-blue paper: dimension lines, callouts and annotations pointing at the subject, drafting type", "skills": "svg layout"},
    {"name": "Broadsheet", "ground": "light", "look": "the front page of an old newspaper: a masthead, tight columns with rules between them, headlines of different sizes, small print", "skills": "typography editorial"},
    {"name": "Comic panels", "ground": "light", "look": "the page as a comic: panels and gutters, halftone dots, angled captions, sound effects as type, each scroll a new panel", "skills": "animation scroll"},
    {"name": "One stage", "ground": "either", "look": "one scene that stays pinned while scrolling transforms it step by step: the story is told by what changes in place", "skills": "gsap scroll animation"},
    {"name": "Sideways gallery", "ground": "either", "look": "chapters that move horizontally, like walking along a gallery wall: big framed pieces with small labels beside them", "skills": "gsap scroll"},
    {"name": "Bento", "ground": "either", "look": "a tight mosaic of tiles of different sizes, each one its own small idea, with one tile that moves, as in a product keynote", "skills": "apple ui layout"},
    {"name": "Ma", "ground": "light", "look": "Japanese restraint: asymmetry and emptiness, vertical type, a single ink stroke, content sitting off-centre with room to breathe", "skills": "typography"},
    {"name": "Chrome and gloss", "ground": "dark", "look": "early-2000s future: liquid metal gradients, glossy pills, lens flares, a wide techno typeface, unashamedly loud", "skills": "animation"},
    {"name": "Risograph", "ground": "light", "look": "two-colour print with misregistered layers, grain and overprinted shapes, on warm paper, like a gig poster", "skills": "ui design"},
    {"name": "Field notes", "ground": "light", "look": "a researcher's notebook: grid paper, handwriting for notes, pinned specimens and sketches in SVG, annotations in the margin", "skills": "svg typography"},
)
BY_NAME = {d["name"].lower(): d for d in DIRECTIONS}
CANDIDATES = 5
REMEMBER = 8

BRIEF = """You are the art director of a design studio. Choose how one thing should look, so that it is memorable and not like the studio's last jobs.

The job, as data: {goal}
{wants}
Pick one of these directions, the one that makes the most interesting match with this subject. An unexpected match is often the best one:
{directions}

If the job itself names a style, or says what the CEO wants it to look like, that wins over this list: follow it, and give it your own name.

Used on recent jobs, so not to be used again: {recent}

Skills the team has. Pick at most 2 that would really help make this direction. Pick none if none would:
{skills}

Reply with only this JSON:
{{"direction": "its name", "concept": "the one idea behind this page, in 1 or 2 sentences, specific to this subject", "palette": ["4 hex colours"], "fonts": ["a display font from Google Fonts", "a text font from Google Fonts"], "layout": "how the page is built, in one sentence", "signature": "the one interaction or moment people will remember", "skills": ["skill names"]}}"""


def candidates(recent: list[dict], seed: str, count: int = CANDIDATES) -> list[dict]:
    """A fresh handful each time: never one used lately, and not dark again after a run of dark."""
    used = {str(r.get("direction") or "").lower() for r in recent[:REMEMBER]}
    fresh = [d for d in DIRECTIONS if d["name"].lower() not in used] or list(DIRECTIONS)
    last_grounds = [BY_NAME.get(str(r.get("direction") or "").lower(), {}).get("ground") for r in recent[:2]]
    if len(last_grounds) == 2 and last_grounds[0] == last_grounds[1] and last_grounds[0] in ("dark", "light"):
        other = [d for d in fresh if d["ground"] != last_grounds[0]]
        fresh = other if len(other) >= count else fresh
    rng = random.Random(seed)
    rng.shuffle(fresh)
    return fresh[:count]


def recent_line(recent: list[dict]) -> str:
    fonts: list[str] = []
    colours: list[str] = []
    for record in recent[:REMEMBER]:
        looks = record.get("looks") or {}
        fonts += [str(f) for f in [*(record.get("fonts") or []), *(looks.get("fonts") or [])] if f and str(f).lower() not in ("sans-serif", "serif", "system-ui", "monospace")]
        colours += [str(c) for c in (record.get("palette") or [])[:2]]
    fonts = list(dict.fromkeys(fonts))[:12]
    colours = list(dict.fromkeys(colours))[:8]
    if not fonts and not colours:
        return "nothing yet"
    return "; ".join(part for part in (f"fonts {', '.join(fonts)}" if fonts else "", f"colours {', '.join(colours)}" if colours else "") if part)


def ask_for(goal: str, wants: list[str], recent: list[dict], skills: list[dict], seed: str) -> str:
    picked = candidates(recent, seed)
    shown = "\n".join(f"- {d['name']}: {d['look']}" for d in picked)
    listed = "\n".join(f"- {neutral(s.get('name', ''))}: {neutral(str(s.get('description') or ''))[:90]}" for s in skills[:24]) or "(none installed)"
    extra = ("What it must get across:\n" + "\n".join(f"- {neutral(w)}" for w in wants[:6]) + "\n") if wants else ""
    return BRIEF.format(goal=neutral(goal)[:600], wants=extra, directions=shown, recent=recent_line(recent), skills=listed)


def fallback(recent: list[dict], seed: str) -> dict:
    """No model answered, and the page should still not look like the last one."""
    pick = candidates(recent, seed, 1)[0]
    return {"direction": pick["name"], "concept": "", "palette": [], "fonts": [], "layout": pick["look"], "signature": "", "skills": []}


def brief_text(brief: dict, recent: list[dict]) -> str:
    direction = str(brief.get("direction") or "")
    known = BY_NAME.get(direction.lower())
    lines = [f"Art direction for this job: {neutral(direction)}. This is for you: its name never appears in the work."]
    if known:
        lines.append(f"What that means: {known['look']}. Commit to it completely. A timid version is worse than none.")
    for label, key in (("The idea", "concept"), ("How the page is built", "layout"), ("The moment people remember", "signature")):
        if brief.get(key):
            lines.append(f"{label}: {neutral(str(brief[key]))[:400]}")
    if brief.get("palette"):
        lines.append("Colours: " + ", ".join(neutral(str(c))[:20] for c in brief["palette"][:5]))
    if brief.get("fonts"):
        lines.append("Fonts, from Google Fonts: " + ", ".join(neutral(str(f))[:40] for f in brief["fonts"][:3]))
    avoid = recent_line(recent)
    if avoid != "nothing yet":
        lines.append(f"Recent jobs used these, so do not: {avoid}.")
    return "\n".join(lines)
