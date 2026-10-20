"""Exact-secret redaction for executor output.


nSPAWN copies the owner credential into the run-private config directory and
exports that directory to the vendor child, so the child can read its own
credential — a hook, a tool call, or a prompt-injected instruction is enough.
Every byte the child then writes crosses into nSPAWN's own surfaces: the output
tail, the transcript, the durable last item, the event journal, the registry row.

THE FENCE IS EXACT-VALUE MATCHING, NEVER A PATTERN CLASSIFIER. nSPAWN HOLDS the
credential bytes, so it can PROVE a match instead of guessing at one: a regex for
"things that look like a token" is evadable by construction and buys the next
class of evasion every hardening round, while "these exact bytes" is a fact a
tool can settle.

Redaction runs on the DECODED string, never on transport bytes, so it is
representation-independent: JSON escaping, `\\u` escaping and re-serialization all
decode back to the same characters, and matching after the decode sees through
every one of them.

KNOWN BOUNDARY, STATED ONCE: a child that TRANSFORMS the secret before emitting
it — base64, reversed, split across two records — defeats exact matching. Closing
that needs confinement, not a cleverer matcher.

Production caller: the supervisor's fold in SL-D, which is not built yet. The
contract is proven here by driving real credential bytes through the redaction.
`with_values` is deliberately NOT ported: it is `pub(crate)` in the oracle and
belongs beside the vendor home in SL-E that calls it.
"""

import json
from typing import Any

#: What replaces a matched secret. Free of quotes and backslashes so a
#: substitution inside a JSON string can never change the document's shape.
REDACTION_MARKER = "[redacted-credential]"

#: The shortest EXTRACTED credential fragment treated as secret material. The
#: exact credential document bypasses this floor; the floor applies only to
#: fragments pulled out of a structured or tokenized credential, where values
#: such as "max" are not secret material.
MIN_SECRET_BYTES = 16


def _is_secret_character(character: str) -> bool:
    """`=` is deliberately a SEPARATOR, not secret material.

    In a non-JSON credential it is the assignment (`ANTHROPIC_TOKEN=<secret>`),
    and folding it into the run would make the needle the whole `NAME=value`
    pair — which the child never emits, so nothing would ever match. A base64
    secret loses only its trailing padding, and the payload before the padding
    is still a needle.
    """
    return character.isascii() and (character.isalnum() or character in "-_.+/~")


def _push_exact(needles: list[str], candidate: str) -> None:
    if candidate:
        needles.append(candidate)


def _push_bounded(needles: list[str], candidate: str) -> None:
    # The oracle measures BYTES here, not characters.
    if len(candidate.encode("utf-8")) >= MIN_SECRET_BYTES:
        needles.append(candidate)


def _collect_string_values(value: Any, needles: list[str]) -> None:
    if isinstance(value, str):
        _push_bounded(needles, value)
    elif isinstance(value, list):
        for item in value:
            _collect_string_values(item, needles)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_string_values(item, needles)


def _collect_token_runs(text: str, needles: list[str]) -> None:
    """Split a non-JSON credential into maximal runs of the secret alphabet."""
    run: list[str] = []
    for character in text:
        if _is_secret_character(character):
            run.append(character)
        else:
            _push_bounded(needles, "".join(run))
            run.clear()
    _push_bounded(needles, "".join(run))


def overlap(end: str, start: str) -> int:
    """The length of the longest end of `end` that `start` begins with, in one
    linear pass (Knuth-Morris-Pratt) over at most `len(start)` of its characters."""
    if not start:
        return 0
    fail = [0] * len(start)
    k = 0
    for i in range(1, len(start)):
        while k and start[i] != start[k]:
            k = fail[k - 1]
        if start[i] == start[k]:
            k += 1
        fail[i] = k
    k = 0
    for c in end[-len(start) :]:
        while k and (k == len(start) or c != start[k]):
            k = fail[k - 1]
        if c == start[k]:
            k += 1
    return k


class SecretNeedles:
    """The exact byte runs from one credential that must never reach a surface.

    `__repr__` is hand-written: a derived one would print the secrets into the
    first log line that formats a home.
    """

    __slots__ = ("_needles",)

    def __init__(self, needles: tuple[str, ...] = ()) -> None:
        # Sorted longest-first so an ENCLOSING match is consumed before any
        # substring of it, which keeps one secret from producing nested markers.
        # Ties break lexicographically so the order is deterministic.
        ordered = sorted(set(needles), key=lambda needle: (-len(needle), needle))
        self._needles = tuple(ordered)

    @classmethod
    def from_credential(cls, data: bytes) -> SecretNeedles:
        """Fingerprint one credential file.

        The whole trimmed document is one needle — the child that `cat`s the file
        emits it verbatim — and every string value inside it is another, because
        the child that reads one field, or re-serializes with different spacing,
        emits only that. When the document is not JSON the same coverage comes
        from tokenizing it on non-secret characters.
        """
        needles: list[str] = []
        text = data.decode("utf-8", errors="replace")
        _push_exact(needles, text.strip())
        try:
            parsed = json.loads(data)
        except ValueError, UnicodeDecodeError:
            _collect_token_runs(text, needles)
        else:
            _collect_string_values(parsed, needles)
        return cls(tuple(needles))

    @classmethod
    def none(cls) -> SecretNeedles:
        """A needle set that redacts nothing. Only for surfaces with no
        credential behind them."""
        return cls(())

    def merged(
        self, other: SecretNeedles, *, maximum_bytes: int | None = None
    ) -> SecretNeedles:
        """Union source snapshots; a requested bound refuses rather than forgets."""
        combined = SecretNeedles(self._needles + other._needles)
        if (
            maximum_bytes is not None
            and sum(len(needle.encode("utf-8")) for needle in combined._needles)
            > maximum_bytes
        ):
            raise ValueError("secret snapshot retention exceeds its byte budget")
        return combined

    def is_empty(self) -> bool:
        return not self._needles

    def __len__(self) -> int:
        return len(self._needles)

    def __repr__(self) -> str:
        return f"SecretNeedles(needles={len(self._needles)})"

    def redact(self, text: str) -> str:
        """Replace every occurrence of every needle."""
        current = text
        for needle in self._needles:
            if needle in current:
                current = current.replace(needle, REDACTION_MARKER)
        return current

    def releasable(self, text: str) -> int:
        """How much of redacted `text` may leave before more text arrives: all of
        it but a tail that could begin a needle, which the next text may complete."""
        return len(text) - max((overlap(text, n) for n in self._needles), default=0)

    def redact_value(self, value: Any) -> Any:
        """Redact every string in a parsed record — values AND KEYS.

        A tool-call payload is arbitrary model-authored JSON and nothing forbids
        the secret from landing in the key position.

        Deviation from the oracle, deliberate: the Rust takes `&mut Value` and
        rebuilds the map in place. Python strings are immutable, so this RETURNS
        the redacted value instead of mutating through a reference. Same result,
        no sentinel.
        """
        if not self._needles:
            return value
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, dict):
            return {
                self.redact(key) if isinstance(key, str) else key: self.redact_value(
                    item
                )
                for key, item in value.items()
            }
        return value
