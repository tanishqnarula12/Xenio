"""Real timings of the desktop engine (V4 phase 1): what each step of seeing and acting really cost.

One JSON line per measured step in ``<data dir>/logs/desktop-metrics.jsonl``, written by whichever process did the work
(Hermes' for tools, XENIO's for fast-path commands and whole turns): numbers and fixed names only - never what was on
screen, typed or asked. ``summary()`` turns the newest part of the file into medians and rates for Mission Control and
``--desktop-stats``. Standard library only (it runs inside Hermes).

Stages: capture (a screenshot), perception (reading a window without a model), model (a vision model call), action
(input sent), verification (the UI state checked), task (a whole request that used the desktop).
"""

from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

STAGES = ("capture", "perception", "model", "action", "verification", "task")
FILE = "desktop-metrics.jsonl"
MAX_BYTES = 1_000_000  # then the older half is dropped
TAIL_BYTES = 512_000

_enabled = True


def set_enabled(on: bool) -> None:
    """Tests: nothing is written to the real data folder."""
    global _enabled
    _enabled = on


def _path(folder: Path | None = None) -> Path:
    if folder is None:
        from app.integrations.base import data_dir

        folder = data_dir()
    return Path(folder) / "logs" / FILE


def record(stage: str, ms: float, folder: Path | None = None, **fields) -> None:
    """Never raises: a measurement must not break what it measures."""
    if not _enabled:
        return
    try:
        path = _path(folder)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"t": round(time.time(), 1), "stage": stage, "ms": round(float(ms), 1), **fields}) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)  # one small append: processes do not interleave
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
        if path.stat().st_size > MAX_BYTES:
            _prune(path)
    except Exception:  # noqa: BLE001
        pass


def _prune(path: Path) -> None:
    data = path.read_bytes()
    keep = data[len(data) // 2:]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(keep[keep.find(b"\n") + 1:])
    os.replace(tmp, path)


def _stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {"n": len(ordered), "median_ms": round(statistics.median(ordered), 1),
            "p90_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))], 1), "max_ms": round(ordered[-1], 1)}


def summary(folder: Path | None = None, since_hours: float | None = None, clock=time.time) -> dict:
    """Latency per stage, how requests were answered (which perception level, how many model calls were avoided), and
    how actions ended (failures, recoveries). Empty until something was measured: nothing here is estimated."""
    try:
        with _path(folder).open("rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            fh.seek(max(0, end - TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        lines, end = [], 0
    if end > TAIL_BYTES:
        lines = lines[1:]  # the first line of a tail is usually cut
    oldest = clock() - since_hours * 3600 if since_hours else 0
    timings: dict[str, list[float]] = {s: [] for s in STAGES}
    levels: dict[str, int] = {}
    actions = {"total": 0, "failed": 0, "verified": 0, "unverified": 0, "not_as_expected": 0, "recovered": 0, "cancelled": 0}
    reused = unchanged = 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("t", 0) < oldest or row.get("stage") not in timings:
            continue
        stage = row["stage"]
        timings[stage].append(float(row.get("ms") or 0))
        if stage == "perception":
            level = str(row.get("level") or "?")
            if level != "escalated" and row.get("kind") != "find":  # sent on to the vision model; finding a control
                levels[level] = levels.get(level, 0) + 1
            unchanged += 1 if row.get("unchanged") else 0
        elif stage == "model":
            levels["vision"] = levels.get("vision", 0) + (0 if row.get("reused") else 1)
            reused += 1 if row.get("reused") else 0
        elif stage == "action":
            actions["total"] += 1
            actions["failed"] += 1 if row.get("ok") is False else 0
            actions["cancelled"] += 1 if row.get("cancelled") else 0
            actions["recovered"] += int(row.get("recovered") or 0)
        elif stage == "verification":
            key = {True: "verified", False: "not_as_expected"}.get(row.get("verified"), "unverified")
            actions[key] += 1
    looks = sum(levels.values())
    without_model = looks - levels.get("vision", 0)
    return {"stages": {s: _stats(v) for s, v in timings.items() if v}, "perception_levels": levels,
            "looks": looks, "answered_without_a_model": without_model,
            "without_a_model_rate": round(without_model / looks, 3) if looks else None,
            "vision_answers_reused": reused, "unchanged_observations": unchanged, "actions": actions,
            "failure_rate": round(actions["failed"] / actions["total"], 3) if actions["total"] else None,
            "recovery_rate": round(actions["recovered"] / (actions["failed"] + actions["recovered"]), 3)
            if actions["failed"] + actions["recovered"] else None}


def describe(stats: dict) -> str:
    """``--desktop-stats``: the summary in plain lines."""
    if not stats["stages"]:
        return "Nothing measured yet: use XENIO's desktop tools first."
    lines = ["Stage          calls   median      p90      max"]
    for stage, s in stats["stages"].items():
        lines.append(f"{stage:<14}{s['n']:>5} {s['median_ms']:>7.0f}ms {s['p90_ms']:>7.0f}ms {s['max_ms']:>7.0f}ms")
    a = stats["actions"]
    lines += ["", f"Looks: {stats['looks']} ({stats['answered_without_a_model']} without a model call; "
                  f"levels {stats['perception_levels']}; {stats['vision_answers_reused']} vision answers reused)",
              f"Actions: {a['total']} ({a['failed']} failed, {a['cancelled']} cancelled, {a['recovered']} recovered); "
              f"checks: {a['verified']} verified, {a['not_as_expected']} not as expected, {a['unverified']} not checked"]
    return "\n".join(lines)
