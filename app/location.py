"""Where the user is: what XENIO tells Hermes for weather, local time and "near me" questions.

Two sources, never a guess: the place the user typed in Settings, or the Mac's own Location Services through XENIO's
native helper (it asks macOS for permission; see native/). Stored in ``<data dir>/location.json`` so the xenio_location
tool reads it inside Hermes too. Nothing here looks the location up on the internet.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.integrations.base import IntegrationError, data_dir

STALE_DEVICE_SECONDS = 6 * 3600  # a device fix older than this is reported as possibly out of date


def location_file(base: Path | None = None) -> Path:
    return (base or data_dir()) / "location.json"


def load(base: Path | None = None) -> dict | None:
    try:
        data = json.loads(location_file(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and (data.get("label") or data.get("lat") is not None) else None


def _coord(value: Any, limit: float, what: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise IntegrationError(f"{what} must be a number") from None
    if math.isnan(number) or abs(number) > limit:
        raise IntegrationError(f"{what} must be between -{limit:g} and {limit:g}")
    return round(number, 5)


def save(label: str = "", lat: Any = None, lon: Any = None, source: str = "settings", accuracy_m: Any = None,
         base: Path | None = None) -> dict | None:
    """Store the location (``label`` empty and no coordinates = forget it)."""
    label = " ".join(str(label or "").split())[:120]
    la, lo = _coord(lat, 90, "latitude"), _coord(lon, 180, "longitude")
    if (la is None) != (lo is None):
        raise IntegrationError("give both latitude and longitude, or neither")
    path = location_file(base)
    if not label and la is None:
        path.unlink(missing_ok=True)
        return None
    if source not in ("settings", "device"):
        raise IntegrationError("unknown location source")
    data = {"label": label or None, "lat": la, "lon": lo, "source": source,
            "accuracy_m": round(float(accuracy_m)) if accuracy_m not in (None, "") else None,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)
    return data


def describe(base: Path | None = None, now: float | None = None) -> dict:
    """What the xenio_location tool returns: the place, coordinates, local time and time zone, and how sure it is."""
    data = load(base)
    local = datetime.now().astimezone()
    out: dict[str, Any] = {"local_time": local.isoformat(timespec="minutes"), "time_zone": str(local.tzinfo),
                           "utc_offset": local.strftime("%z")}
    if data is None:
        out.update(known=False, note="The user's location is not set. Ask the user where they are (or which city to "
                                     "use), and tell them they can set it in XENIO's Settings.")
        return out
    out.update(known=True, place=data.get("label"), latitude=data.get("lat"), longitude=data.get("lon"),
               source="the user's setting" if data.get("source") == "settings" else "this Mac's Location Services",
               updated=data.get("updated"))
    if data.get("source") == "device":
        try:
            age = (now or time.time()) - datetime.fromisoformat(str(data["updated"])).timestamp()
        except (KeyError, ValueError):
            age = None
        if age is not None and age > STALE_DEVICE_SECONDS:
            out["note"] = f"This position is {age / 3600:.0f} hours old; the user may have moved."
    return out
