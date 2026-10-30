"""Bounded activity facts accumulated by the journal's incremental importer."""

from __future__ import annotations

import json
import math
import os
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from . import LINE_MAX_BYTES

GAP_LIMIT: Final = 256
FIELDS: Final = ("mtime_ns", "size", "last_at", "max_gap_seconds", "gaps")


class ActivityRefused(ValueError):
    """A native source cannot supply a consistent activity observation."""


def check(info: os.stat_result, saved: dict[str, Any] | None) -> None:
    """Fence the opened source and retain its identity across import batches."""
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or not info.st_mode & stat.S_IRUSR
    ):
        raise ActivityRefused("transcript is not an owner regular file")
    if saved is not None:
        if (info.st_dev, info.st_ino) != (saved["device"], saved["inode"]):
            raise ActivityRefused("transcript replaced")
        if info.st_size < saved["size"]:
            raise ActivityRefused("source shrank")


def begin(info: os.stat_result) -> dict[str, Any]:
    """An empty accumulator for one fenced source, before its first record."""
    check(info, None)
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mtime_ns": info.st_mtime_ns,
        "size": 0,
        "last_at": None,
        "previous_at": None,
        "max_gap_seconds": 0,
        "gaps": [],
    }


def add(saved: dict[str, Any], raw: bytes) -> None:
    """Add one complete record in native order, including snapshot clocks."""
    if len(raw) + 1 > LINE_MAX_BYTES:
        raise ActivityRefused("transcript record exceeds byte ceiling")
    if not raw.strip():
        return
    try:
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ActivityRefused("invalid transcript record")
        stamp = record.get("timestamp")
        if stamp is None and record.get("type") == "file-history-snapshot":
            snapshot = record.get("snapshot")
            if isinstance(snapshot, dict):
                stamp = snapshot.get("timestamp")
        if stamp is None:
            return
        if not isinstance(stamp, str):
            raise ActivityRefused("invalid transcript timestamp")
        clock = datetime.fromisoformat(stamp)
        if clock.tzinfo is None:
            raise ActivityRefused("transcript timestamp lacks timezone")
        at = clock.timestamp()
        if not math.isfinite(at):
            raise ActivityRefused("invalid transcript timestamp")
    except ActivityRefused:
        raise
    except ValueError, RecursionError, OverflowError:
        raise ActivityRefused("transcript activity unreadable") from None
    previous = saved["previous_at"]
    if previous is not None and at >= previous:
        seconds = at - previous
        saved["gaps"].append({"start": previous, "end": at, "seconds": seconds})
        del saved["gaps"][:-GAP_LIMIT]
        saved["max_gap_seconds"] = max(saved["max_gap_seconds"], seconds)
    saved["previous_at"] = at
    saved["last_at"] = at if saved["last_at"] is None else max(saved["last_at"], at)


def observation(
    vendor: str,
    status: str | None,
    reason: str | None,
    saved: str | None,
    consumed: int,
    session_status: str | None,
    home: Path,
    path: Path,
) -> dict[str, Any]:
    """Gate every export on healthy rows, matching identity, and current fences."""
    if status is not None:
        return {"reason": reason or "native transcript import failed"}
    if session_status not in {"live", "waiting", "ended"}:
        return {"reason": "native transcript session is not healthy"}
    if vendor != "claude":
        return {"reason": "native transcript translator unavailable"}
    if saved is None:
        return {"reason": "native transcript activity not imported"}
    value = json.loads(saved)
    if value["size"] != consumed:
        return {"reason": "native transcript activity import incomplete"}
    if "reason" in value:
        return {"reason": value["reason"]}
    try:
        # Inspect metadata only. Never open or parse the source at export time.
        info = os.stat(path, follow_symlinks=False)
        check(info, value)
        if (info.st_size, info.st_mtime_ns) != (value["size"], value["mtime_ns"]):
            raise ActivityRefused("native transcript changed since import")
        parts = path.relative_to(home).parts
        if not parts or any(part in {".", ".."} for part in parts):
            raise ActivityRefused("native transcript path is not under its home")
        for directory in (
            home,
            *(home.joinpath(*parts[:i]) for i in range(1, len(parts))),
        ):
            parent = directory.lstat()
            if (
                not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != os.geteuid()
                or parent.st_mode & 0o022
            ):
                raise ActivityRefused(
                    "native transcript parent is not owner-controlled"
                )
    except ActivityRefused as error:
        return {"reason": str(error)}
    except OSError, ValueError:
        return {"reason": "native transcript unavailable"}
    return {field: value[field] for field in FIELDS}
