"""Real files for real work: Word documents, Excel workbooks and PowerPoint decks, from one small description, with the standard library alone. An agent says what goes in them, and XENIO writes the file, so the model spends its tokens on the content and not on zip files and XML.

Then each file is checked, because a file existing proves nothing. It is opened again, by macOS's own parser where there is one, and every spreadsheet formula is worked out here and compared, so that a workbook arrives with its numbers already right.
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import tempfile
import xml.dom.minidom as dom
import zipfile
from html import escape
from pathlib import Path

MAX_TEXT = 200_000
MAX_SHEET_ROWS = 5_000
MAX_SLIDES = 80
OFFICE = (".docx", ".xlsx", ".pptx")
PICTURES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
EMU = 12700  # a point
SLIDE_W, SLIDE_H = 960 * EMU, 540 * EMU


class DeliverableError(Exception):
    pass


def x(text) -> str:
    """Text as XML. Characters XML cannot hold are left out, not passed on to break the file."""
    return escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(text if text is not None else "")), quote=True)


def _zip(path: Path, parts: dict[str, str | bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))  # it has to come first
        for name, body in parts.items():
            package.writestr(name, body)
    temp.replace(path)


XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"

# ---- Word --------------------------------------------------------------------------------------------------------------

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _run(text: str, bold: bool = False, size: int = 0, colour: str = "") -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:color w:val="{colour}"/>' if colour else "") + (f'<w:sz w:val="{size * 2}"/>' if size else "")
    return f'<w:r>{"<w:rPr>" + props + "</w:rPr>" if props else ""}<w:t xml:space="preserve">{x(text)}</w:t></w:r>'


def _inline(text: str, **kw) -> str:
    """**bold** inside a line, the one piece of markup models always use."""
    out = []
    for index, piece in enumerate(re.split(r"\*\*", str(text or ""))):
        if piece:
            out.append(_run(piece, bold=index % 2 == 1 or kw.get("bold", False), size=kw.get("size", 0), colour=kw.get("colour", "")))
    return "".join(out)


def make_document(path: Path, title: str, blocks: list[dict], accent: str = "1F4E79") -> dict:
    accent = _hex(accent, "1F4E79")
    body = [f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>{_inline(title)}</w:p>'] if title else []
    for block in blocks if isinstance(blocks, list) else []:
        kind = str(block.get("type") or "paragraph") if isinstance(block, dict) else "paragraph"
        block = block if isinstance(block, dict) else {"text": block}
        if kind == "heading":
            level = min(3, max(1, int(block.get("level") or 1)))
            body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>{_inline(block.get("text"))}</w:p>')
        elif kind == "bullets":
            for item in block.get("items") or []:
                body.append(f'<w:p><w:pPr><w:pStyle w:val="ListBullet"/><w:ind w:left="360" w:hanging="240"/></w:pPr>{_run("•  ")}{_inline(item)}</w:p>')
        elif kind == "table":
            rows = [r for r in block.get("rows") or [] if isinstance(r, list)]
            if rows:
                columns = max(len(r) for r in rows)
                grid = "".join(f'<w:gridCol w:w="{int(9000 / columns)}"/>' for _ in range(columns))
                cells = []
                for number, row in enumerate(rows):
                    shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{accent}"/>' if number == 0 else ""
                    cells.append("<w:tr>" + "".join(f'<w:tc><w:tcPr><w:tcW w:w="{int(9000 / columns)}" w:type="dxa"/>{shade}</w:tcPr><w:p>{_inline(c, bold=number == 0, colour="FFFFFF" if number == 0 else "")}</w:p></w:tc>' for c in [*row, *[""] * (columns - len(row))]) + "</w:tr>")
                border = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>' for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
                body.append(f'<w:tbl><w:tblPr><w:tblW w:w="9000" w:type="dxa"/><w:tblBorders>{border}</w:tblBorders></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{"".join(cells)}</w:tbl><w:p/>')
        else:
            body.append(f"<w:p>{_inline(block.get('text'))}</w:p>")
    document = f'{XML}<w:document {W}><w:body>{"".join(body)}<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1200" w:right="1300" w:bottom="1200" w:left="1300" w:header="600" w:footer="600" w:gutter="0"/></w:sectPr></w:body></w:document>'
    style = lambda sid, name, size, colour="", bold=True, before=240, after=80, based="Normal": f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="{based}"/><w:qFormat/><w:pPr><w:spacing w:before="{before}" w:after="{after}"/></w:pPr><w:rPr>{"<w:b/>" if bold else ""}{f"<w:color w:val={chr(34)}{colour}{chr(34)}/>" if colour else ""}<w:sz w:val="{size * 2}"/></w:rPr></w:style>'  # noqa: E731
    styles = f'{XML}<w:styles {W}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="22"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>{style("Title", "Title", 26, accent, True, 0, 240)}{style("Heading1", "heading 1", 18, accent)}{style("Heading2", "heading 2", 14, accent)}{style("Heading3", "heading 3", 12, accent)}{style("ListBullet", "List Bullet", 11, "", False, 0, 60)}</w:styles>'
    _zip(path, {
        "[Content_Types].xml": f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>',
        "_rels/.rels": f'{XML}<Relationships xmlns="{PKG_RELS}"><Relationship Id="rId1" Type="{RELS}/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": document,
        "word/styles.xml": styles,
        "word/_rels/document.xml.rels": f'{XML}<Relationships xmlns="{PKG_RELS}"><Relationship Id="rId1" Type="{RELS}/styles" Target="styles.xml"/></Relationships>',
    })
    return check(path)


# ---- Excel: formulas are worked out here, so the numbers are in the file and have been checked ----------------------------

def col_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def col_number(name: str) -> int:
    value = 0
    for char in name.upper():
        value = value * 26 + ord(char) - 64
    return value - 1


class FormulaError(Exception):
    pass


_TOKEN = re.compile(r"""\s*(?:(?P<sheet>(?:'[^']+'|[A-Za-z_][\w]*)!)?(?P<ref>\$?[A-Za-z]{1,3}\$?\d+)(?::(?P<ref2>\$?[A-Za-z]{1,3}\$?\d+))?|(?P<num>\d+(?:\.\d*)?|\.\d+)(?P<pct>%)?|(?P<name>[A-Za-z]+)(?=\()|(?P<str>"[^"]*")|(?P<op><=|>=|<>|[-+*/^(),=<>&]))""")


class _Formula:
    """A small, safe reader for what spreadsheets really use. No eval: what it does not know it says it does not know."""

    FUNCTIONS = {"SUM": lambda a: sum(_numbers(a)), "AVERAGE": lambda a: _mean(_numbers(a)), "AVG": lambda a: _mean(_numbers(a)), "MIN": lambda a: min(_numbers(a) or [0]), "MAX": lambda a: max(_numbers(a) or [0]), "COUNT": lambda a: len(_numbers(a)), "COUNTA": lambda a: len([v for v in _flat(a) if v not in ("", None)]), "ABS": lambda a: abs(_number(_flat(a)[0])), "ROUND": lambda a: round(_number(_flat(a)[0]) + 1e-12, int(_number(_flat(a)[1])) if len(_flat(a)) > 1 else 0), "IF": None}

    def __init__(self, text: str, workbook: "_Book", sheet: str):
        self.tokens, at = [], 0
        while at < len(text):
            found = _TOKEN.match(text, at)
            if found is None or found.end() == at:
                if text[at:].strip():
                    raise FormulaError(f"cannot read {text[at:at + 12]!r}")
                break
            self.tokens.append(found)
            at = found.end()
        self.at, self.workbook, self.sheet = 0, workbook, sheet

    def peek(self):
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def op(self, *ops) -> str:
        token = self.peek()
        if token is not None and token.group("op") in ops:
            self.at += 1
            return token.group("op")
        return ""

    def value(self):
        out = self.compare()
        if self.peek() is not None:
            raise FormulaError("there is more after the end of the formula")
        return out

    def compare(self):
        left = self.add()
        while True:
            token = self.op("=", "<>", "<=", ">=", "<", ">")
            if not token:
                return left
            right = self.add()
            left = {"=": left == right, "<>": left != right, "<=": _number(left) <= _number(right), ">=": _number(left) >= _number(right), "<": _number(left) < _number(right), ">": _number(left) > _number(right)}[token]

    def add(self):
        left = self.mul()
        while True:
            token = self.op("+", "-", "&")
            if not token:
                return left
            right = self.mul()
            left = f"{_text(left)}{_text(right)}" if token == "&" else _number(left) + _number(right) if token == "+" else _number(left) - _number(right)

    def mul(self):
        left = self.power()
        while True:
            token = self.op("*", "/")
            if not token:
                return left
            right = _number(self.power())
            if token == "/" and right == 0:
                raise FormulaError("it divides by zero")
            left = _number(left) * right if token == "*" else _number(left) / right

    def power(self):
        left = self.unary()
        if self.op("^"):
            return _number(left) ** _number(self.power())
        return left

    def unary(self):
        if self.op("-"):
            return -_number(self.unary())
        self.op("+")
        return self.atom()

    def atom(self):
        token = self.peek()
        if token is None:
            raise FormulaError("the formula ends too soon")
        self.at += 1
        if token.group("num"):
            return float(token.group("num")) / (100 if token.group("pct") else 1)
        if token.group("str"):
            return token.group("str")[1:-1]
        if token.group("ref"):
            sheet = (token.group("sheet") or "").rstrip("!").strip("'") or self.sheet
            if token.group("ref2"):
                return self.workbook.range(sheet, token.group("ref"), token.group("ref2"))
            return self.workbook.cell(sheet, token.group("ref"))
        if token.group("name"):
            name = token.group("name").upper()
            if name not in self.FUNCTIONS:
                raise FormulaError(f"XENIO cannot check {name}()")
            self.at += 1  # the bracket
            args = []
            if not self.op(")"):
                while True:
                    args.append(self.compare())
                    if self.op(")"):
                        break
                    if not self.op(","):
                        raise FormulaError("a bracket was not closed")
            if name == "IF":
                return args[1] if args[0] else (args[2] if len(args) > 2 else False)
            return self.FUNCTIONS[name](args)
        if token.group("op") == "(":
            inside = self.compare()
            if not self.op(")"):
                raise FormulaError("a bracket was not closed")
            return inside
        raise FormulaError(f"cannot read {token.group(0).strip()!r}")


def _flat(args) -> list:
    out = []
    for arg in args:
        out.extend(_flat(arg)) if isinstance(arg, list) else out.append(arg)
    return out


def _numbers(args) -> list[float]:
    return [float(v) for v in _flat(args) if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _mean(values: list[float]) -> float:
    if not values:
        raise FormulaError("it averages nothing")
    return sum(values) / len(values)


def _number(value) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if value in ("", None):
        return 0.0
    try:
        return float(str(value))
    except ValueError:
        raise FormulaError(f"{str(value)[:20]!r} is not a number") from None


def _text(value) -> str:
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return "" if value is None else str(value)


class _Book:
    def __init__(self, sheets: list[dict]):
        self.sheets = {str(s.get("name") or f"Sheet{i + 1}")[:31]: s.get("rows") or [] for i, s in enumerate(sheets)}
        self.cache: dict[tuple[str, str], object] = {}
        self.busy: set[tuple[str, str]] = set()
        self.problems: dict[tuple[str, str], str] = {}

    def cell(self, sheet: str, ref: str):
        ref = ref.replace("$", "").upper()
        key = (sheet, ref)
        if key in self.cache:
            return self.cache[key]
        if sheet not in self.sheets:
            raise FormulaError(f"there is no sheet {sheet!r}")
        if key in self.busy:
            raise FormulaError("the formula refers to itself")
        match = re.fullmatch(r"([A-Z]+)(\d+)", ref)
        row, col = int(match.group(2)) - 1, col_number(match.group(1))
        rows = self.sheets[sheet]
        raw = rows[row][col] if row < len(rows) and isinstance(rows[row], list) and col < len(rows[row]) else None
        if isinstance(raw, str) and raw.startswith("="):
            self.busy.add(key)
            try:
                value = _Formula(raw[1:], self, sheet).value()
            finally:
                self.busy.discard(key)
        else:
            value = raw
        self.cache[key] = value
        return value

    def range(self, sheet: str, one: str, two: str) -> list:
        a, b = re.fullmatch(r"([A-Z]+)(\d+)", one.replace("$", "").upper()), re.fullmatch(r"([A-Z]+)(\d+)", two.replace("$", "").upper())
        return [self.cell(sheet, f"{col_name(c)}{r}") for r in range(min(int(a.group(2)), int(b.group(2))), max(int(a.group(2)), int(b.group(2))) + 1) for c in range(min(col_number(a.group(1)), col_number(b.group(1))), max(col_number(a.group(1)), col_number(b.group(1))) + 1)]


def make_spreadsheet(path: Path, sheets: list[dict]) -> dict:
    sheets = [s for s in (sheets if isinstance(sheets, list) else []) if isinstance(s, dict)]
    if not sheets:
        raise DeliverableError("a workbook needs at least one sheet")
    book = _Book(sheets)
    faults = []
    parts: dict[str, str | bytes] = {}
    names = list(book.sheets)
    for number, name in enumerate(names, 1):
        rows = [r for r in book.sheets[name] if isinstance(r, list)][:MAX_SHEET_ROWS]
        out = []
        widths: dict[int, int] = {}
        for r, row in enumerate(rows, 1):
            cells = []
            for c, raw in enumerate(row):
                if raw in (None, ""):
                    continue
                ref = f"{col_name(c)}{r}"
                shown = str(raw)
                header = r == 1
                if isinstance(raw, str) and raw.startswith("="):
                    try:
                        value = book.cell(name, ref)
                    except (FormulaError, RecursionError, ValueError, ZeroDivisionError, OverflowError, TypeError, IndexError) as exc:
                        faults.append(f"{name}!{ref} {raw[:40]}: {exc}")
                        value = None
                    shown = _text(value)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        cells.append(f'<c r="{ref}" s="{1 if header else 2}"><f>{x(raw[1:])}</f><v>{float(value)!r}</v></c>')
                    elif value is None:
                        cells.append(f'<c r="{ref}"><f>{x(raw[1:])}</f></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="str"><f>{x(raw[1:])}</f><v>{x(value)}</v></c>')
                elif isinstance(raw, bool):
                    cells.append(f'<c r="{ref}" t="b"><v>{int(raw)}</v></c>')
                elif isinstance(raw, (int, float)):
                    cells.append(f'<c r="{ref}" s="{1 if header else 2 if isinstance(raw, float) and raw != int(raw) else 0}"><v>{raw!r}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="inlineStr" s="{1 if header else 0}"><is><t xml:space="preserve">{x(raw)}</t></is></c>')
                widths[c] = max(widths.get(c, 8), min(60, len(shown) + 2))
            out.append(f'<row r="{r}">{"".join(cells)}</row>')
        columns = "".join(f'<col min="{c + 1}" max="{c + 1}" width="{w}" customWidth="1"/>' for c, w in sorted(widths.items()))
        frozen = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' if len(rows) > 1 else ""
        parts[f"xl/worksheets/sheet{number}.xml"] = f'{XML}<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{frozen}{"<cols>" + columns + "</cols>" if columns else ""}<sheetData>{"".join(out)}</sheetData></worksheet>'
    sheet_list = "".join(f'<sheet name="{x(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1))
    parts.update({
        "[Content_Types].xml": f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(names) + 1)) + "</Types>",
        "_rels/.rels": f'{XML}<Relationships xmlns="{PKG_RELS}"><Relationship Id="rId1" Type="{RELS}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'{XML}<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{RELS}"><calcPr fullCalcOnLoad="1"/><sheets>{sheet_list}</sheets></workbook>'.replace("<calcPr fullCalcOnLoad=\"1\"/><sheets>", "<sheets>").replace("</sheets></workbook>", '</sheets><calcPr fullCalcOnLoad="1"/></workbook>'),
        "xl/_rels/workbook.xml.rels": f'{XML}<Relationships xmlns="{PKG_RELS}">' + "".join(f'<Relationship Id="rId{i}" Type="{RELS}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(names) + 1)) + f'<Relationship Id="rId{len(names) + 1}" Type="{RELS}/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": f'{XML}<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs></styleSheet>',
    })
    _zip(path, parts)
    out = check(path)
    out["faults"] = [*faults, *out["faults"]]
    out["ok"] = not out["faults"]
    return out


# ---- PowerPoint: every slide is laid out here, on a fixed canvas, with a look from the job's art direction ---------------------

A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
R = f'xmlns:r="{RELS}"'
THEMES = (
    {"bg": "FFFFFF", "text": "111111", "muted": "666666", "accent": "E4572E", "head": "Helvetica Neue", "body": "Helvetica Neue"},
    {"bg": "0F1B2D", "text": "F4F1E8", "muted": "9FB0C3", "accent": "F2B134", "head": "Georgia", "body": "Helvetica Neue"},
    {"bg": "F6F1E7", "text": "1E1E1E", "muted": "6F6A5F", "accent": "1F6F5C", "head": "Georgia", "body": "Georgia"},
    {"bg": "101010", "text": "F5F5F5", "muted": "A0A0A0", "accent": "7CFFB2", "head": "Avenir Next", "body": "Avenir Next"},
    {"bg": "EEF2FF", "text": "1B1B3A", "muted": "5A5A7A", "accent": "4338CA", "head": "Avenir Next", "body": "Avenir Next"},
)


def _hex(value, default: str) -> str:
    text = str(value or "").strip().lstrip("#").upper()
    return text if re.fullmatch(r"[0-9A-F]{6}", text) else default


def _theme(raw, seed: str) -> dict:
    base = dict(THEMES[sum(map(ord, seed or "x")) % len(THEMES)])
    if isinstance(raw, dict):
        for key in ("bg", "text", "muted", "accent"):
            base[key] = _hex(raw.get(key), base[key])
        for key in ("head", "body"):
            if raw.get(key):
                base[key] = re.sub(r"[^\w -]", "", str(raw[key]))[:40] or base[key]
    # a theme chosen by a model has to be readable, whatever it chose
    if _contrast(base["text"], base["bg"]) < 7:
        base["text"] = "111111" if _luminance(base["bg"]) > 0.4 else "F5F5F5"
    if _contrast(base["muted"], base["bg"]) < 4.5:
        base["muted"] = "555555" if _luminance(base["bg"]) > 0.4 else "B8B8B8"
    if _contrast(base["accent"], base["bg"]) < 3:
        base["accent"] = base["text"]
    return base


def _luminance(colour: str) -> float:
    def part(value: int) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * part(int(colour[:2], 16)) + 0.7152 * part(int(colour[2:4], 16)) + 0.0722 * part(int(colour[4:6], 16))


def _contrast(one: str, two: str) -> float:
    a, b = _luminance(one), _luminance(two)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


class _Slide:
    def __init__(self, theme: dict):
        self.theme, self.shapes, self.rels, self.number = theme, [], [], 2

    def _id(self) -> int:
        self.number += 1
        return self.number

    def text(self, x_pt, y_pt, w_pt, h_pt, paragraphs, size=18, colour="", font="", bold=False, align="l", anchor="t", spacing=0, name="Text"):
        colour = colour or self.theme["text"]
        font = font or self.theme["body"]
        runs = []
        for paragraph in paragraphs if isinstance(paragraphs, list) else [paragraphs]:
            bullet = False
            if isinstance(paragraph, dict):
                bullet = bool(paragraph.get("bullet"))
                paragraph = paragraph.get("text")
            mark = f'<a:buFont typeface="Arial"/><a:buChar char="•"/>' if bullet else "<a:buNone/>"
            indent = ' marL="228600" indent="-228600"' if bullet else ""
            pieces = "".join(f'<a:r><a:rPr lang="en-US" sz="{int(size * 100)}" b="{int(bold or i % 2 == 1)}" dirty="0"><a:solidFill><a:srgbClr val="{colour}"/></a:solidFill><a:latin typeface="{x(font)}"/></a:rPr><a:t>{x(piece)}</a:t></a:r>' for i, piece in enumerate(re.split(r"\*\*", str(paragraph or ""))) if piece) or f'<a:endParaRPr lang="en-US" sz="{int(size * 100)}"/>'
            runs.append(f'<a:p><a:pPr algn="{align}"{indent}><a:spcAft><a:spcPts val="{int(spacing * 100)}"/></a:spcAft>{mark}</a:pPr>{pieces}</a:p>')
        shape = self._id()
        self.shapes.append(f'<p:sp><p:nvSpPr><p:cNvPr id="{shape}" name="{name} {shape}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{int(x_pt * EMU)}" y="{int(y_pt * EMU)}"/><a:ext cx="{int(w_pt * EMU)}" cy="{int(h_pt * EMU)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr><p:txBody><a:bodyPr wrap="square" lIns="0" tIns="0" rIns="0" bIns="0" anchor="{anchor}"><a:normAutofit/></a:bodyPr><a:lstStyle/>{"".join(runs)}</p:txBody></p:sp>')

    def rect(self, x_pt, y_pt, w_pt, h_pt, colour):
        shape = self._id()
        self.shapes.append(f'<p:sp><p:nvSpPr><p:cNvPr id="{shape}" name="Shape {shape}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{int(x_pt * EMU)}" y="{int(y_pt * EMU)}"/><a:ext cx="{int(w_pt * EMU)}" cy="{int(h_pt * EMU)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{colour}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></p:txBody></p:sp>')

    def picture(self, target: str, x_pt, y_pt, w_pt, h_pt):
        shape = self._id()
        rid = f"rId{len(self.rels) + 2}"
        self.rels.append((rid, target))
        self.shapes.append(f'<p:pic><p:nvPicPr><p:cNvPr id="{shape}" name="Picture {shape}"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm><a:off x="{int(x_pt * EMU)}" y="{int(y_pt * EMU)}"/><a:ext cx="{int(w_pt * EMU)}" cy="{int(h_pt * EMU)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')

    def xml(self) -> str:
        return f'{XML}<p:sld {A} {P} {R}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="{self.theme["bg"]}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>{"".join(self.shapes)}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'


def _bullets(items, limit=7) -> list[dict]:
    return [{"text": str(i)[:220], "bullet": True} for i in (items or [])[:limit]]


def _draw(slide: _Slide, s: dict, media: dict, number: int, total: int) -> None:
    t = slide.theme
    layout = str(s.get("layout") or ("bullets" if s.get("bullets") else "title")).lower()
    title, subtitle = str(s.get("title") or ""), str(s.get("subtitle") or "")
    picture = media.get(str(s.get("image") or ""))
    if layout in ("title", "cover", "section"):
        big = 56 if layout != "section" else 44
        slide.text(60, 90, 830, 220, title[:110], size=big if len(title) < 40 else big - 14, font=t["head"], bold=True, anchor="b", name="Title")
        slide.rect(60, 328, 56, 6, t["accent"])
        if subtitle:
            slide.text(60, 350, 800, 90, subtitle[:220], size=20, colour=t["muted"], name="Subtitle")
        return
    if layout == "quote":
        slide.text(80, 120, 800, 260, f"“{str(s.get('quote') or title)[:280]}”", size=34, font=t["head"], anchor="ctr", name="Quote")
        if s.get("source") or subtitle:
            slide.text(80, 400, 800, 40, f"{str(s.get('source') or subtitle)[:100]}", size=16, colour=t["muted"], name="Source")
        return
    if layout == "stat":
        slide.text(60, 50, 840, 60, title[:90], size=26, font=t["head"], bold=True, name="Title")
        stats = [i for i in (s.get("stats") or []) if isinstance(i, dict)][:4] or [{"value": s.get("stat"), "label": s.get("label")}]
        width = (840 - 24 * (len(stats) - 1)) / len(stats)
        for index, item in enumerate(stats):
            left = 60 + index * (width + 24)
            slide.rect(left, 170, 40, 5, t["accent"])
            slide.text(left, 186, width, 120, str(item.get("value") or "")[:14], size=56 if len(stats) < 3 else 42, colour=t["accent"], font=t["head"], bold=True, name="Figure")
            slide.text(left, 310, width, 110, str(item.get("label") or "")[:120], size=16, colour=t["muted"], name="Label")
        if s.get("source"):
            slide.text(60, 490, 840, 24, f"Source: {str(s['source'])[:140]}", size=10, colour=t["muted"], name="Source")
    else:
        slide.text(60, 44, 840, 70, title[:100], size=30, font=t["head"], bold=True, name="Title")
        slide.rect(60, 116, 56, 4, t["accent"])
        if layout in ("two", "compare", "columns"):
            for side, left in (("left", 60), ("right", 498)):
                part = s.get(side) if isinstance(s.get(side), dict) else {"items": s.get(side) or []}
                slide.text(left, 136, 402, 30, str(part.get("title") or "")[:60], size=18, colour=t["accent"], bold=True, name="Heading")
                slide.text(left, 172, 402, 310, _bullets(part.get("items"), 6), size=16, spacing=7, name="Body")
        elif picture:
            slide.text(60, 136, 450, 350, _bullets(s.get("bullets")), size=18, spacing=9, name="Body")
            slide.picture(picture, 540, 136, 360, 360)
        elif s.get("rows"):
            rows = [r for r in s["rows"] if isinstance(r, list)][:9]
            cols = max((len(r) for r in rows), default=1)
            for r, row in enumerate(rows):
                for c, cell in enumerate(row[:cols]):
                    slide.text(60 + c * 840 / cols, 136 + r * 38, 840 / cols - 12, 34, str(cell)[:60], size=14, bold=r == 0, colour=t["accent"] if r == 0 else "", name="Cell")
        else:
            slide.text(60, 136, 840, 350, _bullets(s.get("bullets"), 8), size=20, spacing=10, name="Body")
    if s.get("source") and layout != "stat":
        slide.text(60, 490, 760, 24, f"Source: {str(s['source'])[:140]}", size=10, colour=t["muted"], name="Source")
    slide.text(860, 506, 60, 20, f"{number}", size=10, colour=t["muted"], align="r", name="Number")


def _too_much(number: int, s: dict) -> list[str]:
    """Words that would not fit a slide are said out loud. Cutting them off quietly sent a real deck out with sentences ending halfway."""
    out = []
    title = str(s.get("title") or "")
    if len(title) > 90:
        out.append(f"slide {number}: the title is {len(title)} characters, keep it under 90")
    columns = [s.get(side) for side in ("left", "right") if s.get(side)]
    lists = [("bullets", s.get("bullets"), 8), *[("column", (c.get("items") if isinstance(c, dict) else c), 6) for c in columns]]
    for name, items, most in lists:
        items = items if isinstance(items, list) else []
        if len(items) > most:
            out.append(f"slide {number}: {len(items)} {name} lines, at most {most} fit")
        out += [f"slide {number}: a line is {len(str(i))} characters, keep it under 170" for i in items if len(str(i)) > 170][:2]
    for item in (s.get("stats") if isinstance(s.get("stats"), list) else []):
        if isinstance(item, dict) and len(str(item.get("value") or "")) > 12:
            out.append(f"slide {number}: the figure {str(item['value'])[:24]!r} is too long to show large")
    if len(str(s.get("quote") or "")) > 270:
        out.append(f"slide {number}: the quote is too long")
    return out


def make_presentation(path: Path, title: str, slides: list[dict], theme: dict | None = None, root: Path | None = None) -> dict:
    slides = [s for s in (slides if isinstance(slides, list) else []) if isinstance(s, dict)][:MAX_SLIDES]
    if not slides:
        raise DeliverableError("a presentation needs at least one slide")
    too_much = [fault for number, spec in enumerate(slides, 1) for fault in _too_much(number, spec)]
    look = _theme(theme, str(title))
    parts: dict[str, str | bytes] = {}
    media: dict[str, str] = {}
    root = Path(root) if root else None
    for slide in slides:
        name = str(slide.get("image") or "")
        if name and root is not None and name not in media:
            source = Path((root / name).resolve())
            if source.is_file() and source.is_relative_to(root.resolve()) and source.suffix.lower() in (".png", ".jpg", ".jpeg"):
                target = f"image{len(media) + 1}{source.suffix.lower()}"
                parts[f"ppt/media/{target}"] = source.read_bytes()
                media[name] = f"../media/{target}"
    for number, spec in enumerate(slides, 1):
        slide = _Slide(look)
        _draw(slide, spec, media, number, len(slides))
        parts[f"ppt/slides/slide{number}.xml"] = slide.xml()
        extra = "".join(f'<Relationship Id="{rid}" Type="{RELS}/image" Target="{target}"/>' for rid, target in slide.rels)
        parts[f"ppt/slides/_rels/slide{number}.xml.rels"] = f'{XML}<Relationships xmlns="{PKG_RELS}"><Relationship Id="rId1" Type="{RELS}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>{extra}</Relationships>'
    count = len(slides)
    ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, count + 1))
    pres_rels = f'<Relationship Id="rId1" Type="{RELS}/slideMaster" Target="slideMasters/slideMaster1.xml"/>' + "".join(f'<Relationship Id="rId{i + 1}" Type="{RELS}/slide" Target="slides/slide{i}.xml"/>' for i in range(1, count + 1)) + f'<Relationship Id="rId{count + 2}" Type="{RELS}/theme" Target="theme/theme1.xml"/>'
    tree = '<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/></p:spTree>'
    mapping = '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    colours = "".join(f'<a:{n}><a:srgbClr val="{v}"/></a:{n}>' for n, v in (("dk1", look["text"]), ("lt1", look["bg"]), ("dk2", look["text"]), ("lt2", look["bg"]), ("accent1", look["accent"]), ("accent2", look["muted"]), ("accent3", look["accent"]), ("accent4", look["muted"]), ("accent5", look["accent"]), ("accent6", look["muted"]), ("hlink", look["accent"]), ("folHlink", look["muted"])))
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    theme_xml = f'{XML}<a:theme {A} name="XENIO"><a:themeElements><a:clrScheme name="XENIO">{colours}</a:clrScheme><a:fontScheme name="XENIO"><a:majorFont><a:latin typeface="{x(look["head"])}"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="{x(look["body"])}"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="XENIO"><a:fillStyleLst>{fill}{fill}{fill}</a:fillStyleLst><a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="12700"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst>{fill}{fill}{fill}</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>'
    parts.update({
        "[Content_Types].xml": f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/><Default Extension="jpg" ContentType="image/jpeg"/><Default Extension="jpeg" ContentType="image/jpeg"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>' + "".join(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' for i in range(1, count + 1)) + "</Types>",
        "_rels/.rels": f'{XML}<Relationships xmlns="{PKG_RELS}"><Relationship Id="rId1" Type="{RELS}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
        "ppt/presentation.xml": f'{XML}<p:presentation {A} {R} {P} saveSubsetFonts="1"><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst><p:sldIdLst>{ids}</p:sldIdLst><p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'{XML}<Relationships xmlns="{PKG_RELS}">{pres_rels}</Relationships>',
        "ppt/slideMasters/slideMaster1.xml": f'{XML}<p:sldMaster {A} {R} {P}><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg>{tree}</p:cSld>{mapping}<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>',
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": f'{XML}<Relationships xmlns="{PKG_RELS}"><Relationship Id="rId1" Type="{RELS}/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="{RELS}/theme" Target="../theme/theme1.xml"/></Relationships>',
        "ppt/slideLayouts/slideLayout1.xml": f'{XML}<p:sldLayout {A} {R} {P} type="blank" preserve="1"><p:cSld name="Blank">{tree}</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>',
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": f'{XML}<Relationships xmlns="{PKG_RELS}"><Relationship Id="rId1" Type="{RELS}/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>',
        "ppt/theme/theme1.xml": theme_xml,
    })
    _zip(path, parts)
    out = check(path)
    out["faults"] = [*too_much, *out["faults"]]
    out["ok"] = not out["faults"]
    return out


# ---- reading a file back, and checking it -------------------------------------------------------------------------------

def _words_in(package: zipfile.ZipFile, pattern: str, tag: str) -> str:
    out = []
    for name in sorted(n for n in package.namelist() if re.fullmatch(pattern, n)):
        body = package.read(name).decode("utf-8", "replace")
        out.append(" ".join(re.findall(rf"<{tag}(?: [^>]*)?>([^<]*)</{tag}>", body)))
    return "\n".join(out)


def text_of(path: Path, limit: int = MAX_TEXT) -> str:
    """What the file says, for whoever has to review it. A reviewer shown a binary diff would see nothing at all."""
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix in OFFICE:
            with zipfile.ZipFile(path) as package:
                if suffix == ".docx":
                    text = _words_in(package, r"word/document\.xml", "w:t")
                elif suffix == ".pptx":
                    slides = sorted((n for n in package.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"(\d+)\.xml", n).group(1)))
                    text = "\n".join(f"Slide {i}: " + " ".join(re.findall(r"<a:t>([^<]*)</a:t>", package.read(n).decode("utf-8", "replace"))) for i, n in enumerate(slides, 1))
                else:
                    out = []
                    for name in sorted(n for n in package.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
                        body = package.read(name).decode("utf-8", "replace")
                        for row in re.findall(r"<row [^>]*>(.*?)</row>", body, re.S):
                            cells = []
                            for cell in re.findall(r"<c [^>]*?(?:/>|>.*?</c>)", row, re.S):
                                formula = re.search(r"<f>([^<]*)</f>", cell)
                                value = re.search(r"<v>([^<]*)</v>", cell) or re.search(r"<t[^>]*>([^<]*)</t>", cell)
                                cells.append((f"={formula.group(1)} -> " if formula else "") + (value.group(1) if value else ""))
                            out.append(" | ".join(cells))
                    text = "\n".join(out)
            return _unescape(text)[:limit]
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except (OSError, zipfile.BadZipFile, KeyError):
        return ""


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#x27;", "'").replace("&amp;", "&")


def _opens_on_this_mac(path: Path) -> str:
    """Asked of macOS's own readers, which did not write this file. Empty means it opened, or that there is nothing here to ask."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx" and shutil.which("textutil"):
            done = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True, timeout=30)
            return "" if done.returncode == 0 and done.stdout.strip() else f"macOS could not read it: {done.stderr.strip()[:160]}"
        if suffix in (".pptx", ".xlsx") and shutil.which("qlmanage"):
            with tempfile.TemporaryDirectory(prefix="xenio-open") as folder:
                done = subprocess.run(["qlmanage", "-t", "-s", "400", "-o", folder, str(path)], capture_output=True, text=True, timeout=40)
                return "" if list(Path(folder).glob("*.png")) else "macOS could not draw a preview of it"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not be checked: {exc}"[:160]
    return ""


def preview(path: Path, out: Path, size: int = 1100) -> Path | None:
    """A picture of the first page or slide, drawn by macOS, for someone to look at."""
    path, out = Path(path), Path(out)
    if not shutil.which("qlmanage"):
        return None
    out.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(out), str(path)], capture_output=True, text=True, timeout=40)
    found = out / f"{path.name}.png"
    return found if done.returncode == 0 and found.is_file() else None


def check(path: Path) -> dict:
    """Is this file what it claims to be, and does it hold anything? It says what was found, and it never just says yes."""
    path = Path(path)
    out: dict = {"path": path.name, "ok": False, "kind": path.suffix.lower().lstrip("."), "faults": [], "facts": {}}
    if not path.is_file():
        out["faults"].append("the file does not exist")
        return out
    size = path.stat().st_size
    out["facts"]["bytes"] = size
    if size == 0:
        out["faults"].append("the file is empty")
        return out
    suffix = path.suffix.lower()
    if suffix in OFFICE:
        try:
            with zipfile.ZipFile(path) as package:
                bad = package.testzip()
                if bad:
                    out["faults"].append(f"{bad} inside it is damaged")
                names = package.namelist()
                need = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[suffix]
                if need not in names:
                    out["faults"].append(f"it is missing {need}")
                for name in names:
                    if name.endswith((".xml", ".rels")):
                        try:
                            dom.parseString(package.read(name))
                        except Exception as exc:  # noqa: BLE001 - whatever is wrong with it, it is broken
                            out["faults"].append(f"{name} is not valid: {str(exc)[:100]}")
                            break
                if suffix == ".pptx":
                    out["facts"]["slides"] = len([n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)])
                if suffix == ".xlsx":
                    out["facts"]["sheets"] = len([n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)])
                    out["facts"]["formulas"] = sum(package.read(n).decode("utf-8", "replace").count("<f>") for n in names if n.startswith("xl/worksheets/"))
        except zipfile.BadZipFile:
            out["faults"].append("it is not a real Office file")
            return out
        text = text_of(path)
        out["facts"]["words"] = len(text.split())
        if not text.strip():
            out["faults"].append("it has nothing in it")
        problem = _opens_on_this_mac(path) if not out["faults"] else ""
        if problem:
            out["faults"].append(problem)
    elif suffix in (".md", ".txt", ".csv", ".json", ".html", ".htm"):
        text = text_of(path)
        out["facts"]["words"] = len(text.split())
        if not text.strip():
            out["faults"].append("it has nothing in it")
    out["ok"] = not out["faults"]
    out["checked_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    return out


def sources_in(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"https?://[^\s)\]>\"']+", text)))


def check_folder(root: Path, skip: tuple[str, ...] = (".git", ".venv", "node_modules", "__pycache__", ".deps", ".agents")) -> list[dict]:
    """Every deliverable in a job's folder, checked."""
    root = Path(root)
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or set(path.relative_to(root).parts) & set(skip) or path.name.startswith("."):
            continue
        if path.suffix.lower() in (*OFFICE, ".md", ".txt", ".csv", ".json"):
            result = check(path)
            result["path"] = path.relative_to(root).as_posix()
            found.append(result)
    return found
