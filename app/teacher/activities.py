"""What a learner can DO in a lesson (V4 phase 3). A model describes an activity as data; Mission Control has one tested component for each kind.
No model ever writes code that runs on the page: expressions are parsed by XENIO's own small maths reader, never evaluated as code, and a
learner's program runs only when they press Run, in a sandbox (app.teacher.sandbox).

    graph        curves with sliders: change a and see it move
    calculator   their own inputs, worked-out outputs: "what if?"
    code         a terminal: edit it, give it input, run it; with checks, an exercise marked by what really ran
    chart        bars or lines of real data
    steps        reasoning one step at a time, with "what happens next?" before each reveal
    frames       an algorithm or process played frame by frame
    match        pair terms and meanings
    order        put steps or events in order

An activity that does not make sense is dropped: a slider with no range, a formula that cannot be read, a match with one pair.
"""

from __future__ import annotations

from app.teacher.text import cut

import math
import re

from app.teacher.sandbox import language_of

MAX_ACTIVITIES = 8
FUNCTIONS = {"sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh", "sqrt", "cbrt", "abs", "ln", "log", "log10", "log2", "exp", "floor", "ceil", "round", "min", "max", "pow"}
CONSTANTS = {"pi", "e"}
_TOKEN = re.compile(r"\s*(\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+|[A-Za-z_][A-Za-z_0-9]*|[-+*/^(),%])")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z_0-9]{0,24}$")


class ExpressionError(ValueError):
    pass


def _tokens(text: str) -> list[str]:
    out, position = [], 0
    text = str(text or "").replace("×", "*").replace("·", "*").replace("÷", "/").replace("−", "-").replace("²", "^2").replace("³", "^3").replace("π", "pi").replace("√", "sqrt")
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN.match(text, position)
        if not match:
            raise ExpressionError(f"cannot read {text[position:position + 10]!r}")
        out.append(match.group(1))
        position = match.end()
    return out


class _Reader:
    """The same small grammar as ui/src/teach/expression.ts: what is checked here is what the page draws."""

    def __init__(self, tokens: list[str], values: dict[str, float]):
        self.tokens, self.at, self.values = tokens, 0, values

    def peek(self) -> str | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> str:
        token = self.peek()
        if token is None:
            raise ExpressionError("it ends too soon")
        self.at += 1
        return token

    def expression(self) -> float:
        value = self.term()
        while self.peek() in ("+", "-"):
            value = value + self.term() if self.take() == "+" else value - self.term()
        return value

    def term(self) -> float:
        value = self.unary()
        while True:
            token = self.peek()
            if token in ("*", "/", "%"):
                self.take()
                right = self.unary()
                value = value * right if token == "*" else (value / right if right else math.nan) if token == "/" else (value % right if right else math.nan)
            elif token is not None and (token == "(" or token[0].isalpha() or token[0] == "_" or token[0].isdigit() or token[0] == "."):
                value = value * self.unary()  # "2x", "3(x + 1)"
            else:
                return value

    def unary(self) -> float:
        if self.peek() == "-":
            self.take()
            return -self.unary()
        if self.peek() == "+":
            self.take()
            return self.unary()
        return self.power()

    def power(self) -> float:
        base = self.atom()
        if self.peek() == "^":
            self.take()
            exponent = self.unary()
            try:
                value = base ** exponent
                return value if isinstance(value, (int, float)) else math.nan
            except (OverflowError, ZeroDivisionError, ValueError):
                return math.nan
        return base

    def atom(self) -> float:
        token = self.take()
        if token == "(":
            value = self.expression()
            if self.take() != ")":
                raise ExpressionError("a bracket is not closed")
            return value
        if token[0].isdigit() or token[0] == ".":
            return float(token)
        if token in FUNCTIONS:
            if self.take() != "(":
                raise ExpressionError(f"{token} needs brackets")
            args = [self.expression()]
            while self.peek() == ",":
                self.take()
                args.append(self.expression())
            if self.take() != ")":
                raise ExpressionError("a bracket is not closed")
            return _call(token, args)
        if token in self.values:
            return self.values[token]
        if token == "pi":
            return math.pi
        if token == "e":
            return math.e
        raise ExpressionError(f"unknown name {token!r}")


def _call(name: str, args: list[float]) -> float:
    try:
        if name in ("min", "max"):
            return min(args) if name == "min" else max(args)
        if name == "pow":
            return args[0] ** args[1]
        x = args[0]
        table = {"sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos, "atan": math.atan, "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh, "sqrt": math.sqrt,
                 "cbrt": lambda v: math.copysign(abs(v) ** (1 / 3), v), "abs": abs, "ln": math.log, "log": math.log10, "log10": math.log10, "log2": math.log2, "exp": math.exp, "floor": math.floor, "ceil": math.ceil, "round": round}
        return float(table[name](x))
    except (ValueError, OverflowError, IndexError, ZeroDivisionError):
        return math.nan


def evaluate(text: str, values: dict[str, float]) -> float:
    reader = _Reader(_tokens(text), values)
    value = reader.expression()
    if reader.peek() is not None:
        raise ExpressionError(f"unexpected {reader.peek()!r}")
    return float(value)


def readable(text: str, names: set[str]) -> bool:
    """It can be read, and every name in it is known."""
    try:
        evaluate(text, {name: 1.0 for name in names})
        return True
    except ExpressionError:
        return False


def _text(value, limit: int = 600) -> str:
    return cut(value, limit)


def _number(value, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _sliders(raw) -> list[dict]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not _NAME.match(str(item.get("name") or "")):
            continue
        low, high = _number(item.get("min"), 0.0), _number(item.get("max"), 10.0)
        if low is None or high is None or low >= high or item["name"] in FUNCTIONS or item["name"] in CONSTANTS or item["name"] == "x":
            continue
        value = _number(item.get("value"), (low + high) / 2)
        step = _number(item.get("step"), (high - low) / 100) or (high - low) / 100
        out.append({"name": item["name"], "label": _text(item.get("label") or item["name"], 60), "min": low, "max": high, "step": abs(step), "value": min(high, max(low, value))})
    return out[:6]


def _graph(raw: dict, base: dict) -> dict | None:
    params = _sliders(raw.get("params") or raw.get("sliders"))
    names = {p["name"] for p in params} | {"x"}
    functions = []
    for item in raw.get("functions") or ([raw.get("expr")] if raw.get("expr") else []):
        if isinstance(item, str):
            item = {"expr": item}
        if isinstance(item, dict) and readable(item.get("expr"), names):
            functions.append({"expr": str(item["expr"])[:200], "label": _text(item.get("label") or item["expr"], 60)})
    if not functions:
        return None
    x = raw.get("x") if isinstance(raw.get("x"), dict) else {}
    low, high = _number(x.get("min"), -10.0), _number(x.get("max"), 10.0)
    if low >= high:
        low, high = -10.0, 10.0
    y = raw.get("y") if isinstance(raw.get("y"), dict) else {}
    out = {**base, "functions": functions[:4], "params": params, "x": {"min": low, "max": high, "label": _text(x.get("label"), 40)},
           "y": {"min": _number(y.get("min")), "max": _number(y.get("max")), "label": _text(y.get("label"), 40)}}
    points = [{"x": _number(p.get("x")), "y": _number(p.get("y")), "label": _text(p.get("label"), 40)} for p in raw.get("points") or [] if isinstance(p, dict) and _number(p.get("x")) is not None and _number(p.get("y")) is not None]
    return {**out, "points": points[:12]}


def _calculator(raw: dict, base: dict) -> dict | None:
    inputs = []
    for item in raw.get("inputs") or []:
        if isinstance(item, dict) and _NAME.match(str(item.get("name") or "")) and item["name"] not in FUNCTIONS and item["name"] not in CONSTANTS:
            inputs.append({"name": item["name"], "label": _text(item.get("label") or item["name"], 60), "value": _number(item.get("value"), 0.0), "unit": _text(item.get("unit"), 16)})
    names = {i["name"] for i in inputs}
    outputs = [{"label": _text(o.get("label"), 80), "expr": str(o["expr"])[:200], "unit": _text(o.get("unit"), 16), "digits": int(_number(o.get("digits"), 3))}
               for o in raw.get("outputs") or [] if isinstance(o, dict) and readable(o.get("expr"), names)]
    return {**base, "inputs": inputs[:8], "outputs": outputs[:8]} if inputs and outputs else None


def _chart(raw: dict, base: dict) -> dict | None:
    labels = [_text(x, 40) for x in raw.get("labels") or []][:24]
    series = []
    for item in raw.get("series") or []:
        if not isinstance(item, dict):
            continue
        values = [_number(v) for v in item.get("values") or []][:len(labels)]
        if len(values) == len(labels) and all(v is not None for v in values):
            series.append({"name": _text(item.get("name"), 40), "values": values})
    if len(labels) < 2 or not series:
        return None
    return {**base, "chart": "line" if str(raw.get("chart") or raw.get("kind") or "").lower() == "line" else "bar", "labels": labels, "series": series[:4], "x_label": _text(raw.get("x_label"), 40), "y_label": _text(raw.get("y_label"), 40)}


def _code(raw: dict, base: dict) -> dict | None:
    language = language_of(raw.get("language"))
    code = str(raw.get("code") or "").strip("\n")
    if language is None or (not code and not raw.get("checks")):
        return None
    checks = [{"name": _text(c.get("name"), 60), "stdin": str(c.get("stdin") or "")[:2000], "expected": str(c.get("expected") if c.get("expected") is not None else "")[:2000]}
              for c in raw.get("checks") or [] if isinstance(c, dict) and c.get("expected") is not None][:6]
    return {**base, "language": language, "code": code[:8000], "stdin": str(raw.get("stdin") or "")[:2000], "task": _text(raw.get("task"), 500), "checks": checks, "solution": str(raw.get("solution") or "")[:8000]}


def _steps(raw: dict, base: dict) -> dict | None:
    steps = []
    for item in raw.get("steps") or []:
        if isinstance(item, str):
            item = {"body": item}
        if isinstance(item, dict) and _text(item.get("body") or item.get("title")):
            step = {"title": _text(item.get("title"), 100), "body": _text(item.get("body"), 700), "code": str(item.get("code") or "")[:1200]}
            if _text(item.get("predict")):
                step["predict"] = _text(item["predict"], 200)
            steps.append(step)
    return {**base, "steps": steps[:14]} if len(steps) >= 2 else None


def _frames(raw: dict, base: dict) -> dict | None:
    frames = []
    for item in raw.get("frames") or []:
        if not isinstance(item, dict) or not isinstance(item.get("cells"), list) or not item["cells"]:
            continue
        cells = [_text(c, 14) for c in item["cells"]][:24]
        def indexes(key):
            return [int(i) for i in item.get(key) or [] if isinstance(i, int) and 0 <= i < len(cells)]
        pointers = {_text(k, 12): int(v) for k, v in (item.get("pointers") or {}).items() if isinstance(v, int) and 0 <= v < len(cells)} if isinstance(item.get("pointers"), dict) else {}
        frames.append({"cells": cells, "highlight": indexes("highlight"), "dim": indexes("dim"), "pointers": pointers, "note": _text(item.get("note"), 240)})
    return {**base, "frames": frames[:30]} if len(frames) >= 2 else None


def _match(raw: dict, base: dict) -> dict | None:
    pairs = []
    for item in raw.get("pairs") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            a, b = _text(item[0], 120), _text(item[1], 200)
        elif isinstance(item, dict):
            a, b = _text(item.get("left") or item.get("term"), 120), _text(item.get("right") or item.get("meaning"), 200)
        else:
            continue
        if a and b and a != b:
            pairs.append([a, b])
    return {**base, "pairs": pairs[:8]} if len(pairs) >= 3 else None


def _order(raw: dict, base: dict) -> dict | None:
    items = [_text(i, 160) for i in raw.get("items") or [] if _text(i)][:8]
    return {**base, "items": items} if len(items) >= 3 and len(set(items)) == len(items) else None


_BUILDERS = {"graph": _graph, "calculator": _calculator, "chart": _chart, "code": _code, "steps": _steps, "frames": _frames, "match": _match, "order": _order}


def normalize(raw) -> dict | None:
    """An activity ready to show, or None when it makes no sense."""
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("type") or raw.get("kind") or "").strip().lower()
    builder = _BUILDERS.get(kind)
    if builder is None:
        return None
    base = {"type": kind, "title": _text(raw.get("title"), 100), "prompt": _text(raw.get("prompt") or raw.get("try"), 300), "explain": _text(raw.get("explain"), 500)}
    return builder(raw, base)
