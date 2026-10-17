"""Filesystem capabilities.

Reused from Hermes (not re-implemented): reading files (``read_file``), searching
(``search_files``), creating/overwriting files (``write_file``), editing (``patch``).
Added by XENIO because Hermes only offers them through raw shell commands: listing a folder,
inspecting a path, creating a folder, moving/renaming, and deleting (to the Recycle Bin on
Windows, the Trash on macOS, by default). These run inside Hermes via the xenio-guard plugin, validate every path with the
XENIO policy, and return structured results. Standard-library only.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app.safety.policy import TRASH_NAME, RiskLevel
from app.tools.base import Tool, ToolContext, ToolResult

LIST_LIMIT = 200


def _resolve(ctx: ToolContext, raw: str) -> Path:
    return ctx.paths.resolve(raw, ctx.cwd)


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    try:
        return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN)
    except (OSError, AttributeError):
        return False


def list_directory(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tool = "filesystem.list_directory"
    path = _resolve(ctx, args["path"])
    if not path.exists():
        return ToolResult.fail(tool, f"Folder not found: {ctx.paths.describe(path)}")
    if not path.is_dir():
        return ToolResult.fail(tool, f"Not a folder: {ctx.paths.describe(path)}")
    include_hidden = bool(args.get("include_hidden", False))
    entries = []
    with os.scandir(path) as it:
        for entry in it:
            if not include_hidden and _hidden(entry):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            item: dict[str, Any] = {"name": entry.name, "type": "folder" if is_dir else "file", "modified": _when(info.st_mtime)}
            if not is_dir:
                item["size_bytes"] = info.st_size
            entries.append(item)
    entries.sort(key=lambda e: (e["type"] != "folder", e["name"].lower()))
    shown = entries[:LIST_LIMIT]
    names = ", ".join(e["name"] for e in shown[:15]) or "nothing"
    more = f" (+{len(entries) - 15} more)" if len(entries) > 15 else ""
    return ToolResult.ok(tool, f"{ctx.paths.describe(path)} contains {len(entries)} item(s): {names}{more}",
                         path=str(path), entries=shown, total=len(entries), truncated=len(entries) > LIST_LIMIT)


def inspect_path(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tool = "filesystem.inspect_path"
    path = _resolve(ctx, args["path"])
    if not path.exists():
        return ToolResult.ok(tool, f"{ctx.paths.describe(path)} does not exist", path=str(path), exists=False)
    info = path.stat()
    data: dict[str, Any] = {"path": str(path), "exists": True, "type": "folder" if path.is_dir() else "file",
                            "modified": _when(info.st_mtime), "created": _when(info.st_ctime)}
    if path.is_dir():
        try:
            data["items"] = sum(1 for _ in os.scandir(path))
        except OSError:
            data["items"] = None
    else:
        data["size_bytes"] = info.st_size
    return ToolResult.ok(tool, f"{ctx.paths.describe(path)} is a {data['type']}", **data)


def create_directory(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tool = "filesystem.create_directory"
    path = _resolve(ctx, args["path"])
    if path.exists():
        if path.is_dir():
            return ToolResult.ok(tool, f"{ctx.paths.describe(path)} already exists", path=str(path), created=False)
        return ToolResult.fail(tool, f"A file with that name already exists: {ctx.paths.describe(path)}")
    path.mkdir(parents=True)
    if not path.is_dir():
        return ToolResult.fail(tool, f"The folder was not created: {ctx.paths.describe(path)}")
    return ToolResult.ok(tool, f"Created the folder {ctx.paths.describe(path)}", path=str(path), created=True)


def move_path(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tool = "filesystem.move_path"
    source = _resolve(ctx, args["source"])
    destination = _resolve(ctx, args["destination"])
    if not source.exists():
        return ToolResult.fail(tool, f"Not found: {ctx.paths.describe(source)}")
    final = destination / source.name if destination.is_dir() else destination
    if final.exists() and not args.get("overwrite", False):
        return ToolResult.fail(tool, f"{ctx.paths.describe(final)} already exists (nothing was moved)")
    if not final.parent.exists():
        return ToolResult.fail(tool, f"The destination folder does not exist: {ctx.paths.describe(final.parent)}")
    shutil.move(str(source), str(final))
    if not final.exists() or source.exists():
        return ToolResult.fail(tool, "The move did not complete")
    return ToolResult.ok(tool, f"Moved {ctx.paths.describe(source)} to {ctx.paths.describe(final)}",
                         source=str(source), destination=str(final))


MACOS_TRASH = "/usr/bin/trash"  # macOS 15+: Finder-compatible (Put Back works), needs no automation permission


def _trash_supported() -> bool:
    return os.name == "nt" or (sys.platform == "darwin" and os.path.exists(MACOS_TRASH))


def _recycle(path: Path) -> None:
    """Send a file/folder to the Recycle Bin (Windows) or the Trash (macOS); both are recoverable."""
    if sys.platform == "darwin":
        done = subprocess.run([MACOS_TRASH, "-s", "--", str(path)], capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise OSError(f"Trash operation failed: {(done.stderr or done.stdout).strip() or done.returncode}")
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    fo_delete, flags = 3, 0x0004 | 0x0010 | 0x0040 | 0x0400  # SILENT | NOCONFIRMATION | ALLOWUNDO | NOERRORUI
    op = SHFILEOPSTRUCTW(None, fo_delete, str(path) + "\0", None, flags, False, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Recycle Bin operation failed (code {code})")


def delete_path(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    tool = "filesystem.delete_path"
    path = _resolve(ctx, args["path"])
    if not path.exists() and not path.is_symlink():
        return ToolResult.fail(tool, f"Not found: {ctx.paths.describe(path)} (nothing was deleted)")
    is_dir = path.is_dir() and not path.is_symlink()
    items = sum(1 for _ in path.rglob("*")) if is_dir else 1
    permanent = bool(args.get("permanent", False))
    if not permanent and not _trash_supported():
        return ToolResult.fail(tool, "There is no Recycle Bin/Trash this XENIO can use here; ask again with permanent=true")
    if permanent:
        if is_dir:
            shutil.rmtree(path)
        else:
            path.unlink()
    else:
        _recycle(path)
    if path.exists():
        return ToolResult.fail(tool, f"{ctx.paths.describe(path)} still exists after the delete")
    where = "permanently deleted" if permanent else f"moved to the {TRASH_NAME}"
    return ToolResult.ok(tool, f"{ctx.paths.describe(path)} was {where}", path=str(path), recycled=not permanent,
                         items=items)


_PATH = {"type": "string", "description": "Absolute path, or relative to the session folder. ~ is the user's home."}

TOOLS: list[Tool] = [
    Tool("filesystem.list_directory", "List the files and folders inside a folder (names, types, sizes, dates).",
         {"type": "object", "properties": {"path": _PATH, "include_hidden": {"type": "boolean", "description": "Also list hidden files (default false)."}},
          "required": ["path"]}, RiskLevel.READ, "xenio", "xenio_list_directory", list_directory),
    Tool("filesystem.inspect_path", "Check whether a file or folder exists and get its type, size and dates (never its contents).",
         {"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
         RiskLevel.READ, "xenio", "xenio_inspect_path", inspect_path),
    Tool("filesystem.create_directory", "Create a folder (and any missing parent folders). Succeeds if it already exists.",
         {"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
         RiskLevel.SAFE_WRITE, "xenio", "xenio_create_directory", create_directory),
    Tool("filesystem.move_path", "Move or rename a file or folder. Never overwrites unless overwrite=true. The user is asked to confirm.",
         {"type": "object", "properties": {"source": _PATH, "destination": _PATH,
                                           "overwrite": {"type": "boolean", "description": "Replace an existing destination (default false)."}},
          "required": ["source", "destination"]}, RiskLevel.CONFIRM_REQUIRED, "xenio", "xenio_move_path", move_path),
    Tool("filesystem.delete_path", f"Delete a file or folder. Moves it to the {TRASH_NAME} (recoverable) unless permanent=true. The user is asked to confirm.",
         {"type": "object", "properties": {"path": _PATH, "permanent": {"type": "boolean", "description": f"Delete permanently instead of using the {TRASH_NAME} (default false)."}},
          "required": ["path"]}, RiskLevel.CONFIRM_REQUIRED, "xenio", "xenio_delete_path", delete_path),
    # Reused Hermes built-ins (executed by Hermes, policed by XENIO).
    Tool("filesystem.read_file", "Read a text file (Hermes read_file).", {"type": "object", "properties": {"path": _PATH}},
         RiskLevel.READ, "hermes", "read_file"),
    Tool("filesystem.search_files", "Search file names or contents (Hermes search_files).",
         {"type": "object", "properties": {"pattern": {"type": "string"}, "path": _PATH}}, RiskLevel.READ, "hermes", "search_files"),
    Tool("filesystem.create_file", "Create or overwrite a file with given content (Hermes write_file).",
         {"type": "object", "properties": {"path": _PATH, "content": {"type": "string"}}}, RiskLevel.SAFE_WRITE, "hermes", "write_file"),
    Tool("filesystem.modify_file", "Edit part of a file (Hermes patch).",
         {"type": "object", "properties": {"path": _PATH, "old_string": {"type": "string"}, "new_string": {"type": "string"}}},
         RiskLevel.SAFE_WRITE, "hermes", "patch"),
]


def xenio_tools() -> list[Tool]:
    return [t for t in TOOLS if t.backend == "xenio"]
