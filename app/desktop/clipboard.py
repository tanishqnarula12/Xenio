"""The clipboard, borrowed for a moment: XENIO pastes text into apps (and reads text out of them) through it, then puts
the user's own clipboard back.

Why paste and not type: typing goes key by key (30 ms each, so 2,000 characters take a minute) and code editors in
web pages and apps auto-close brackets and auto-indent every line as it is typed, which mangles code. A paste inserts
the text exactly, in a fraction of a second, and is undone with one Command-Z.

macOS only (``NSPasteboard`` through JavaScript for Automation, no extra packages):

* ``save()`` keeps the user's clipboard - every item and every type (text, images, files) - in memory only (never on
  disk; it can hold a copied password);
* ``set_text()`` puts XENIO's text there, marked ``org.nspasteboard.TransientType`` so clipboard-history apps do not
  record it;
* ``restore()`` puts the user's clipboard back, unless something else was copied in the meantime (the change count
  moved), which is then left alone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

# a copied full-screen screenshot is ~30 MB (TIFF + PNG); bigger than this is not kept - and then XENIO does not use
# the clipboard at all (measured: a 30 MB limit lost the user's copied screenshot)
MAX_SAVE_BYTES = 160_000_000

_JXA = r"""
ObjC.import('AppKit');
function stdin() {
  var data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
  return $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
}
function run(argv) {
  var pb = $.NSPasteboard.generalPasteboard;
  var cmd = argv[0];
  if (cmd === 'count') return String(pb.changeCount);
  if (cmd === 'save') {
    var items = pb.pasteboardItems, out = [], total = 0;
    for (var i = 0; i < items.count; i++) {
      var item = items.objectAtIndex(i), types = item.types, entry = [];
      for (var j = 0; j < types.count; j++) {
        var t = types.objectAtIndex(j), d = item.dataForType(t);
        if (d.isNil()) continue;
        total += Number(d.length);  // JXA gives NSData.length as a string: += would concatenate the digits
        if (total > LIMIT) return JSON.stringify({change: pb.changeCount, tooBig: true});
        entry.push([t.js, d.base64EncodedStringWithOptions(0).js]);
      }
      out.push(entry);
    }
    return JSON.stringify({change: pb.changeCount, items: out});
  }
  if (cmd === 'set') {
    var text = stdin();
    pb.clearContents;
    pb.setStringForType($(text), $.NSPasteboardTypeString);
    pb.setStringForType($(''), $('org.nspasteboard.TransientType'));
    return String(pb.changeCount);
  }
  if (cmd === 'restore') {
    var snap = JSON.parse(stdin());
    if (pb.changeCount != snap.expect) return 'changed';
    pb.clearContents;
    var objects = $.NSMutableArray.array;
    snap.items.forEach(function (entry) {
      var item = $.NSPasteboardItem.alloc.init;
      entry.forEach(function (pair) {
        item.setDataForType($.NSData.alloc.initWithBase64EncodedStringOptions($(pair[1]), 0), $(pair[0]));
      });
      objects.addObject(item);
    });
    if (snap.items.length) pb.writeObjects(objects);
    return 'restored';
  }
  return 'unknown';
}
""".replace("LIMIT", str(MAX_SAVE_BYTES))


class ClipboardError(Exception):
    pass


@dataclass
class Saved:
    change: int
    items: list | None  # None: too big to keep


def supported() -> bool:
    return sys.platform == "darwin"


class Clipboard:
    def __init__(self, run=subprocess.run):
        self._run = run

    def _jxa(self, command: str, stdin: str | None = None, timeout: float = 10) -> str:
        if not supported():
            raise ClipboardError("pasting and copying through the clipboard works on macOS only; use type instead")
        try:
            done = self._run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _JXA, command], input=stdin,
                             capture_output=True, text=True, encoding="utf-8", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClipboardError(f"the clipboard could not be used ({exc})") from None
        if done.returncode != 0:
            raise ClipboardError(f"the clipboard could not be used ({(done.stderr or '').strip()[-200:]})")
        return (done.stdout or "").rstrip("\n")

    def change_count(self) -> int:
        return int(self._jxa("count"))

    def save(self) -> Saved:
        data = json.loads(self._jxa("save", timeout=20))
        return Saved(int(data["change"]), None if data.get("tooBig") else data.get("items") or [])

    def set_text(self, text: str) -> int:
        """Put ``text`` on the clipboard; returns the new change count."""
        return int(self._jxa("set", stdin=text))

    def get_text(self) -> str:
        """The clipboard's plain text, exactly (pbpaste, UTF-8)."""
        try:
            done = self._run(["/usr/bin/pbpaste", "-Prefer", "txt"], capture_output=True, timeout=10,
                             env={**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ClipboardError(f"the clipboard could not be read ({exc})") from None
        out = done.stdout
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out or "")

    def restore(self, saved: Saved, expect: int) -> bool:
        """Put the user's clipboard back if nothing else was copied since ``expect``. True when restored."""
        if saved.items is None:
            return False
        return self._jxa("restore", stdin=json.dumps({"expect": expect, "items": saved.items}), timeout=20) == "restored"
