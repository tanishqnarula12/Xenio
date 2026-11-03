"""Diagrams from structure (V4 phase 3). A model describes what is connected to what; XENIO writes the diagram itself, so a stray
quote or bracket in a label can never break it. Mission Control draws them with Mermaid, offline, from the UI's own files.

A diagram that makes no sense as a diagram (one box, arrows to nowhere) is dropped rather than shown.
"""

from __future__ import annotations

from app.teacher.text import cut

import re

KINDS = ("flowchart", "cycle", "mindmap", "hierarchy", "sequence", "timeline", "comparison")
MAX_NODES = 18
_SHAPES = {"start": '(["{}"])', "end": '(["{}"])', "step": '["{}"]', "decision": '{{"{}"}}', "data": '[/"{}"/]'}


def _label(text, limit: int = 110) -> str:
    """Safe inside a quoted Mermaid label."""
    clean = " ".join(str(text or "").split())
    clean = clean.replace('"', "'").replace("`", "'").replace("#", "no. ").replace("<", "‹").replace(">", "›").replace("\\", "/")
    clean = re.sub(r"[{}\[\]|]", " ", clean).strip()
    clean = " ".join(clean.split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"  # never in the middle of a word


def _word(text, limit: int = 60) -> str:
    """Safe where Mermaid takes no quotes (a participant's name, a message)."""
    return re.sub(r"[;:#<>{}\[\]|\"`\\]+", " ", " ".join(str(text or "").split())).strip()[:limit].rstrip()


def _tree(raw, depth: int = 0, count: list | None = None) -> dict | None:
    count = count if count is not None else [0]
    if isinstance(raw, str):
        raw = {"label": raw}
    if not isinstance(raw, dict) or depth > 4:
        return None
    label = _label(raw.get("label") or raw.get("name") or raw.get("text") or raw.get("root") or "", 60)
    if not label or count[0] >= MAX_NODES + 8:
        return None
    count[0] += 1
    note = cut(raw.get("note"), 400)
    kids = [_tree(k, depth + 1, count) for k in (raw.get("children") or raw.get("branches") or [])] if isinstance(raw.get("children") or raw.get("branches") or [], list) else []
    return {"label": label, "note": note, "children": [k for k in kids if k]}


def _notes(node: dict, into: dict) -> dict:
    if node.get("note"):
        into[node["label"]] = node["note"]
    for kid in node["children"]:
        _notes(kid, into)
    return into


def normalize(raw) -> dict | None:
    """A diagram ready to show (``mermaid`` or the structured parts), or None when it has nothing to show."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or raw.get("type") or "flowchart").strip().lower()
    if kind not in KINDS:
        kind = "flowchart"
    out = {"kind": kind, "title": _label(raw.get("title") or "", 90), "caption": cut(raw.get("caption"), 400)}
    if kind in ("flowchart", "cycle"):
        return _flow(raw, out)
    if kind in ("mindmap", "hierarchy"):
        return _tree_diagram(raw, out)
    if kind == "sequence":
        return _sequence(raw, out)
    if kind == "timeline":
        events = [{"when": _label(e.get("when") or e.get("date") or "", 40), "what": cut(e.get("what") or e.get("event"), 240)}
                  for e in (raw.get("events") or []) if isinstance(e, dict)]
        events = [e for e in events if e["when"] and e["what"]][:14]
        return {**out, "events": events, "alt": "; ".join(f"{e['when']}: {e['what']}" for e in events)} if len(events) >= 2 else None
    columns = [_label(c, 50) for c in (raw.get("columns") or [])][:6]
    rows = [[cut(c, 220) for c in r][:len(columns)] for r in (raw.get("rows") or []) if isinstance(r, list)][:12]
    rows = [r + [""] * (len(columns) - len(r)) for r in rows if any(r)]
    return {**out, "columns": columns, "rows": rows, "alt": f"A table comparing {', '.join(columns)}"} if len(columns) >= 2 and rows else None


def _flow(raw: dict, out: dict) -> dict | None:
    nodes, ids, notes = [], {}, {}
    for item in raw.get("nodes") or []:
        if isinstance(item, str):
            item = {"label": item}
        if not isinstance(item, dict):
            continue
        label = _label(item.get("label") or item.get("text") or item.get("id"))
        if not label or len(nodes) >= MAX_NODES:
            continue
        safe = f"n{len(nodes) + 1}"
        ids[str(item.get("id") or label)] = safe
        ids[label] = safe
        shape = str(item.get("shape") or item.get("type") or "step").lower()
        nodes.append((safe, label, shape if shape in _SHAPES else "step"))
        if str(item.get("note") or "").strip():
            notes[label] = cut(item["note"], 400)
    if len(nodes) < 2:
        return None
    edges = []
    for edge in raw.get("edges") or raw.get("links") or []:
        if not isinstance(edge, dict):
            continue
        a, b = ids.get(str(edge.get("from"))), ids.get(str(edge.get("to")))
        if a and b and a != b:
            edges.append((a, b, _label(edge.get("label") or "", 40)))
    if not edges:  # a list of steps in order
        edges = [(nodes[i][0], nodes[i + 1][0], "") for i in range(len(nodes) - 1)]
    if out["kind"] == "cycle" and not any(a == nodes[-1][0] and b == nodes[0][0] for a, b, _ in edges):
        edges.append((nodes[-1][0], nodes[0][0], ""))
    used = {x for a, b, _ in edges for x in (a, b)}
    nodes = [n for n in nodes if n[0] in used]
    if len(nodes) < 2:
        return None
    direction = str(raw.get("direction") or "TD").upper()
    lines = [f"flowchart {direction if direction in ('TD', 'LR') else 'TD'}"]
    lines += [f"  {safe}{_SHAPES[shape].format(label)}" for safe, label, shape in nodes]
    lines += [f"  {a} -->" + (f'|"{label}"| ' if label else " ") + b for a, b, label in edges]
    names = {safe: label for safe, label, _ in nodes}
    alt = "; ".join(f"{names[a]} leads to {names[b]}" + (f" ({label})" if label else "") for a, b, label in edges)
    return {**out, "mermaid": "\n".join(lines), "alt": alt[:1500], "notes": notes}


def _tree_diagram(raw: dict, out: dict) -> dict | None:
    root = _tree(raw.get("tree") or raw.get("mindmap") or raw if (raw.get("tree") or raw.get("mindmap") or raw.get("label") or raw.get("root")) else {"label": raw.get("title"), "children": raw.get("children") or raw.get("branches")})
    if not root or not root["children"]:
        return None
    alt: list[str] = []
    if out["kind"] == "mindmap":
        lines = ["mindmap", f'  root(("{root["label"]}"))']
        counter = [0]

        def walk(node, depth):
            for kid in node["children"]:
                counter[0] += 1
                lines.append("  " * depth + f'n{counter[0]}("{kid["label"]}")')
                alt.append(f"{node['label']}: {kid['label']}")
                walk(kid, depth + 1)
        walk(root, 2)
    else:
        lines = ["flowchart TD", f'  n0["{root["label"]}"]']
        counter = [0]

        def walk(node, name):
            for kid in node["children"]:
                counter[0] += 1
                mine = f"n{counter[0]}"
                lines.append(f'  {mine}["{kid["label"]}"]')
                lines.append(f"  {name} --> {mine}")
                alt.append(f"{node['label']}: {kid['label']}")
                walk(kid, mine)
        walk(root, "n0")
    return {**out, "mermaid": "\n".join(lines), "alt": "; ".join(alt)[:1500], "notes": _notes(root, {})}


def _sequence(raw: dict, out: dict) -> dict | None:
    steps = [m for m in (raw.get("messages") or raw.get("steps") or []) if isinstance(m, dict)][:16]
    who = [_word(p, 30) for p in (raw.get("participants") or []) if _word(p, 30)]
    for step in steps:
        for side in ("from", "to"):
            name = _word(step.get(side), 30)
            if name and name not in who:
                who.append(name)
    who = who[:8]
    ids = {name: f"p{i + 1}" for i, name in enumerate(who)}
    lines = ["sequenceDiagram"] + [f"  participant {ids[name]} as {name}" for name in who]
    said = []
    for step in steps:
        a, b, text = ids.get(_word(step.get("from"), 30)), ids.get(_word(step.get("to"), 30)), _label(step.get("text") or step.get("message") or "", 120)
        if a and b and text:
            lines.append(f"  {a}->>{b}: {text}")
            said.append(f"{_word(step.get('from'), 30)} to {_word(step.get('to'), 30)}: {text}")
    return {**out, "mermaid": "\n".join(lines), "alt": "; ".join(said)[:1500]} if len(said) >= 2 else None
