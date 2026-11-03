"""The learner's own study material (V4 phase 3): text files, code, PDFs, Word and RTF documents, photos and scans of notes.

A PDF's words come from its text layer page by page, so an answer can name the page. A page with no text layer (a scan) and a photo of
notes are read by Nabh's OCR model. Passages are searched two ways and merged: by their words (SQLite FTS5, local) and by meaning (Nabh
embeddings, which also match Hindi to English). Without Nabh, the word search still works: that is said, not hidden.
"""

from __future__ import annotations

import array
import base64
import math
import mimetypes
import re
import subprocess
from pathlib import Path

from app.integrations.nabh import Nabh, NabhError
from app.safety.policy import redact_secrets
from app.teacher.store import LearningStore, now_iso

MAX_BYTES = 80 * 1024 * 1024
MAX_CHARS = 3_000_000
MAX_OCR_PAGES = 60
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 120
EMBED_BATCH = 16
_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".tex", ".csv", ".tsv", ".json", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
                  ".rs", ".rb", ".php", ".swift", ".kt", ".sql", ".html", ".htm", ".css", ".sh", ".yaml", ".yml", ".toml", ".ipynb"}
_OFFICE_SUFFIXES = {".docx", ".doc", ".rtf", ".rtfd", ".odt", ".webarchive"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".tiff", ".tif"}


class MaterialError(Exception):
    pass


def chunks_of(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Passages that end at a sentence or paragraph where they can, with some overlap so an idea is not cut in two."""
    body = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    out, start = [], 0
    while start < len(body):
        end = min(len(body), start + size)
        if end < len(body):
            cut = max(body.rfind("\n\n", start + size // 2, end), body.rfind(". ", start + size // 2, end), body.rfind("। ", start + size // 2, end))
            if cut > start:
                end = cut + 1
        piece = body[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(body):
            break
        start = max(end - overlap, start + 1)
    return out


def read_pages(path: Path, nabh: Nabh | None = None, pdf_reader=None, runner=subprocess.run) -> dict:
    """{"kind", "pages": [(page number or 0, text)], "unread": pages with no words, "ocr": pages read by OCR}."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path, nabh, pdf_reader)
    if suffix in _IMAGE_SUFFIXES:
        if nabh is None:
            raise MaterialError("reading a photo or scan needs Nabh's OCR model: no Nabh key is set")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        data, mime = _jpeg_for(path, mime, runner)
        try:
            return {"kind": "image", "pages": [(0, nabh.read_image(data, mime))], "unread": 0, "ocr": 1}
        except NabhError as exc:
            raise MaterialError(f"the image could not be read: {exc}") from None
    if suffix in _OFFICE_SUFFIXES:
        out = runner(["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise MaterialError("that document could not be read")
        return {"kind": "document", "pages": [(0, out.stdout)], "unread": 0, "ocr": 0}
    if suffix in _TEXT_SUFFIXES or not suffix:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-1")
        if suffix == ".ipynb":
            text = _notebook_text(text)
        return {"kind": "text", "pages": [(0, text)], "unread": 0, "ocr": 0}
    raise MaterialError(f"XENIO cannot read {suffix} files yet: PDF, Word, RTF, text, code and photos work")


def _jpeg_for(path: Path, mime: str, runner) -> tuple[bytes, str]:
    if mime in ("image/jpeg", "image/png", "image/webp") and path.stat().st_size < 8_000_000:
        return path.read_bytes(), mime
    import tempfile

    with tempfile.TemporaryDirectory() as folder:  # HEIC, TIFF and very large photos: macOS's own converter
        target = Path(folder) / "page.jpg"
        out = runner(["/usr/bin/sips", "-s", "format", "jpeg", "-Z", "2200", str(path), "--out", str(target)], capture_output=True, timeout=60)
        if out.returncode != 0 or not target.exists():
            raise MaterialError("that image could not be read")
        return target.read_bytes(), "image/jpeg"


def _notebook_text(text: str) -> str:
    import json

    try:
        cells = json.loads(text).get("cells") or []
    except (ValueError, AttributeError):
        return text
    return "\n\n".join("".join(c.get("source") or []) for c in cells if isinstance(c, dict))


def _read_pdf(path: Path, nabh: Nabh | None, pdf_reader) -> dict:
    if pdf_reader is None:
        from app.desktop import macos

        def pdf_reader(cmd, **args):  # the same warm helper as accessibility: a bad file cannot take Hermes down with it
            return macos.helper().call(cmd, timeout=90, **args)
    try:
        out = pdf_reader("pdf_text", path=str(path))
    except Exception as exc:  # noqa: BLE001
        raise MaterialError(f"that PDF could not be read: {exc}") from None
    pages = [(i + 1, str(t or "")) for i, t in enumerate(out.get("pages") or [])]
    if not pages:
        raise MaterialError("that PDF could not be read, or it is locked with a password")
    empty = [n for n, t in pages if len(t.strip()) < 20]
    read, ocr = dict(pages), 0
    if empty and nabh is not None:
        for number in empty[:MAX_OCR_PAGES]:
            try:
                image = pdf_reader("pdf_page_image", path=str(path), page=number - 1)
                read[number] = nabh.read_image(base64.b64decode(image["jpeg"]), "image/jpeg")
                ocr += 1
            except Exception:  # noqa: BLE001 - counted below: this page stays unread and that is said
                continue
    unread = sum(1 for t in read.values() if len(t.strip()) < 20)
    return {"kind": "pdf", "pages": sorted(read.items()), "unread": unread, "ocr": ocr}


def _blob(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _vector(raw: bytes | None) -> array.array | None:
    if not raw:
        return None
    out = array.array("f")
    out.frombytes(raw)
    return out


def _cosine(a, b) -> float:
    if a is None or b is None or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else 0.0


def add(store: LearningStore, topic_id: int, title: str, pages: list[tuple[int, str]], path: str = "", kind: str = "text", unread: int = 0, ocr: int = 0,
        nabh: Nabh | None = None, secrets: tuple = ()) -> dict:
    """Keep a piece of material: its passages, and what they mean if Nabh can say."""
    passages: list[tuple[int, str]] = []
    total = 0
    for number, text in pages:
        for piece in chunks_of(redact_secrets(str(text or ""), secrets)):
            total += len(piece)
            if total > MAX_CHARS:
                break
            passages.append((number, piece))
    if not passages:
        raise MaterialError("no words could be read from it" + (": it looks like a scan, and reading scans needs Nabh's OCR model" if nabh is None else ""))
    vectors: list[bytes | None] = [None] * len(passages)
    embedded = 0
    if nabh is not None:
        try:
            for start in range(0, len(passages), EMBED_BATCH):
                batch = passages[start:start + EMBED_BATCH]
                for i, vector in enumerate(nabh.embed([p[1][:3000] for p in batch])):
                    vectors[start + i] = _blob(vector)
                    embedded += 1
        except NabhError:
            vectors = [None] * len(passages)  # the word search still works; the result says so
            embedded = 0
    material_id, _ = store.execute("INSERT INTO materials (topic_id, title, path, kind, pages, chars, unread_pages, ocr_pages, embedded, added) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (topic_id, title[:200], path[:600], kind, len({n for n, _ in pages}), total, unread, ocr, 1 if embedded else 0, now_iso()))
    conn = store.connect()
    try:
        with conn:
            for (number, piece), vector in zip(passages, vectors):
                cur = conn.execute("INSERT INTO chunks (material_id, topic_id, page, text, embedding) VALUES (?,?,?,?,?)", (material_id, topic_id, number, piece, vector))
                conn.execute("INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (cur.lastrowid, piece))
    finally:
        conn.close()
    return {"id": material_id, "title": title[:200], "kind": kind, "passages": len(passages), "pages": len({n for n, _ in pages}), "unread_pages": unread, "ocr_pages": ocr,
            "search": "by meaning and by words" if embedded else "by words only"}


def delete(store: LearningStore, material_id: int) -> bool:
    rows = store.query("SELECT id, text FROM chunks WHERE material_id = ?", (material_id,))
    conn = store.connect()
    try:
        with conn:
            for row in rows:
                conn.execute("INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', ?, ?)", (row["id"], row["text"]))
            conn.execute("DELETE FROM chunks WHERE material_id = ?", (material_id,))
            removed = conn.execute("DELETE FROM materials WHERE id = ?", (material_id,)).rowcount
    finally:
        conn.close()
    return bool(removed)


_FTS_WORD = re.compile(r"\w+", re.UNICODE)


def search(store: LearningStore, topic_id: int, query: str, nabh: Nabh | None = None, limit: int = 6) -> dict:
    """The passages that answer ``query``, best first, each with its source and page: {"passages", "by"}."""
    words = _FTS_WORD.findall(query or "")
    ranks: dict[int, float] = {}
    if words:
        match = " OR ".join('"' + w + '"' for w in words[:24])
        try:
            for position, row in enumerate(store.query(
                    "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid WHERE chunks_fts MATCH ? AND c.topic_id = ? ORDER BY rank LIMIT 30", (match, topic_id))):
                ranks[row["id"]] = ranks.get(row["id"], 0.0) + 1.0 / (60 + position)
        except Exception:  # noqa: BLE001 - an odd query: meaning search below can still answer
            pass
    by = "words"
    if nabh is not None:
        try:
            wanted = array.array("f", nabh.embed([query[:2000]])[0])
            rows = store.query("SELECT id, embedding FROM chunks WHERE topic_id = ? AND embedding IS NOT NULL LIMIT 4000", (topic_id,))
            scored = sorted(((_cosine(wanted, _vector(r["embedding"])), r["id"]) for r in rows), reverse=True)[:30]
            for position, (score, chunk_id) in enumerate(scored):
                if score > 0.2:
                    ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (60 + position)
            if rows:
                by = "meaning and words"
        except NabhError:
            pass
    best = sorted(ranks, key=lambda k: -ranks[k])[:limit]
    out = []
    for chunk_id in best:
        row = store.one("SELECT c.text, c.page, m.title, m.id AS material FROM chunks c JOIN materials m ON m.id = c.material_id WHERE c.id = ?", (chunk_id,))
        if row:
            out.append({"title": row["title"], "page": row["page"], "text": row["text"][:1200], "material": row["material"]})
    return {"passages": out, "by": by}
