"""What a value costs on the stdio door, and the paged surfaces' cursor codec."""

from __future__ import annotations

import base64
import json
from typing import Any


def wire_bytes(value: Any) -> int:
    """The bytes of `value` as JSON, written the way the stdio door writes it:
    UTF-8, escaped to ASCII only when the text cannot be encoded (a lone surrogate)."""
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except UnicodeEncodeError:
        return len(json.dumps(value, ensure_ascii=True))


def encode_cursor(value: list[Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, max_chars: int) -> Any:
    """What `encode_cursor` encoded, or None for a string it could not have issued.
    The length bound keeps a hostile string from reaching base64 and JSON at all."""
    if not cursor or len(cursor) > max_chars:
        return None
    try:
        return json.loads(
            base64.b64decode(
                (cursor + "=" * (-len(cursor) % 4)).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
        )
    except ValueError, RecursionError:
        return None
