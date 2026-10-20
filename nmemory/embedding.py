"""Embedding client (3072-dimension vectors in the space named by `space`).

The HTTP client for capsule and query embeddings.
Index maintenance (the background worker) lives in `embedding_index`.
"""

from __future__ import annotations

import contextlib
import functools
import http.client
import io
import json
import logging
import math
import os
import re
import socket
import threading
import time
import urllib.parse
from collections.abc import Buffer, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .space import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_PROMPT_VERSION,
    DIMENSION,
    EmbeddingError,
    EmbeddingQuotaExhausted,
    EmbeddingRateLimited,
    EmbeddingTransportError,
    SpaceFingerprint,
)

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_MAX_BATCH = 100
OPENAI_ENDPOINT = "https://api.openai.com/v1/embeddings"
OPENAI_MAX_INPUT_CHARS = 16000
#: Retry attempts and status codes eligible for exponential backoff retry. A rate limit
#: 429 is cured by waiting; an exhausted balance 429 is not, which is why the body is in the exception.
RETRY_ATTEMPTS = 5
RETRYABLE_STATUS = (429, 500, 503)
#: Body markers of a 429 that waiting does not cure: an exhausted OpenAI balance or a
#: Gemini monthly spending cap. Not `RESOURCE_EXHAUSTED` alone: Gemini's per-minute
#: limit carries that status too, and waiting does cure it.
QUOTA_EXHAUSTED_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "spending cap",
    "exceeded its monthly",
)
#: Seconds a recall waits for its query embedding, in ONE attempt, before going
#: lexical: a host that drops packets must not stall every recall. Indexing keeps the
#: long timeout and its retries.
QUERY_TIMEOUT_S = 3.0
INDEX_TIMEOUT_S = 120.0


def openai_truncated(text: str, max_chars: int = OPENAI_MAX_INPUT_CHARS) -> str:
    return text[:max_chars]


def openai_payload(texts: list[str]) -> dict[str, Any]:
    inputs = [openai_truncated(t) for t in texts]
    return {
        "model": DEFAULT_OPENAI_MODEL,
        "input": inputs,
        "dimensions": DIMENSION,
    }


class _ReadsWithin(io.RawIOBase):
    """A socket's reads, each armed with the time `left()` before the deadline."""

    def __init__(self, sock: socket.socket, left: Callable[[], float]) -> None:
        self.sock, self.left = sock, left

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        self.sock.settimeout(self.left())
        return self.sock.recv_into(buffer)


@dataclass(frozen=True)
class EmbeddingUsageStats:
    openai_requests: int
    openai_prompt_tokens: int

    def is_empty(self) -> bool:
        return self.openai_requests == 0 and self.openai_prompt_tokens == 0


#: An error message is cut this far, always AFTER redaction: a cut first could keep a
#: key's prefix that no longer matches the key.
ERROR_MESSAGE_CHARS = 600
_KEY_SHAPED = re.compile(r"(?i)(bearer\s+|key=)[^\s&\"',;]+")


def redacted(text: str, *keys: str | None) -> str:
    """`text` without the given keys, any `key=` value or any `Bearer` token."""
    for key in keys:
        if key:
            text = text.replace(key, "[redacted]")
    return _KEY_SHAPED.sub(r"\1[redacted]", text)


def zero_padded(vec: list[float]) -> list[float]:
    """`vec` in the store's DIMENSION; trailing zeros leave every cosine unchanged."""
    return (vec + [0.0] * DIMENSION)[:DIMENSION]


def checked_vector(values: Any, native: int | None) -> list[float]:
    """`values` as a storable vector, or EmbeddingError.

    Exactly `native` components (any count up to DIMENSION when None), each a finite
    number, with a direction; then zero-padded to DIMENSION and normalized.
    """
    if not isinstance(values, list):
        raise EmbeddingError("an embedding must be a list of numbers")
    if native is not None and len(values) != native:
        raise EmbeddingError(f"expected {native} dimensions, got {len(values)}")
    if not 0 < len(values) <= DIMENSION:
        raise EmbeddingError(f"expected 1..{DIMENSION} dimensions, got {len(values)}")
    try:
        vec = [float(v) for v in values]
    except (TypeError, ValueError, OverflowError) as e:
        raise EmbeddingError(f"an embedding component is not a number: {e}") from e
    # A zero or non-finite vector has no direction: its cosine is NaN.
    norm = math.sqrt(sum(x * x for x in vec))
    if not math.isfinite(norm) or norm < 1e-12:
        raise EmbeddingError(f"the embedding has no direction (norm {norm})")
    return l2_normalize(zero_padded(vec))


def embedding_key_variable(provider: str) -> str:
    return "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"


def selfhosted_endpoint() -> str:
    """`NOTT_NMEMORY_EMBED_ENDPOINT`, an http(s) URL, or EmbeddingError."""
    endpoint = os.environ.get("NOTT_NMEMORY_EMBED_ENDPOINT", "").strip()
    try:
        parts = urllib.parse.urlsplit(endpoint)
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        parts.port  # noqa: B018 - a port that is not a number raises ValueError
    except ValueError:
        valid = False
    if not valid:
        raise EmbeddingError(
            "NOTT_NMEMORY_EMBED_PROVIDER=openai-compatible needs"
            " NOTT_NMEMORY_EMBED_ENDPOINT, an http(s) URL such as"
            f" http://host:11434/v1/embeddings; got {endpoint!r}"
        )
    return endpoint


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm < 1e-12:
        return vec
    return [x / norm for x in vec]


class EmbeddingClient:
    """Client for 3072-dim embeddings from Gemini, OpenAI or a self-hosted endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_OPENAI_MODEL,
        custom_hook: Callable[[str], list[float]] | None = None,
        space_fingerprint: SpaceFingerprint | None = None,
    ) -> None:
        #: Why this client has no vector lane; the server still starts (owner decision,
        #: 2026-10-03: a bad embedding configuration costs the vector lane, never memory).
        self.config_error: str | None = None
        #: A self-hosted OpenAI-protocol endpoint; it takes no key, so none is sent.
        self.endpoint: str | None = None
        #: Every key this process could hold; no error message carries one.
        self._secrets = (
            api_key,
            os.environ.get("GEMINI_API_KEY"),
            os.environ.get("OPENAI_API_KEY"),
        )
        try:
            current = SpaceFingerprint.current()
            if current.provider == "openai-compatible":
                self.endpoint = selfhosted_endpoint()
                model = current.model
        except EmbeddingError as e:
            self.config_error = self.safe(str(e))
            logger.warning(
                "embedding configuration refused; recall is lexical only: %s",
                self.config_error,
            )
            current = SpaceFingerprint(
                "unconfigured", "none", DIMENSION, DEFAULT_PROMPT_VERSION
            )
        self.space_provider = current.provider
        env_key = embedding_key_variable(self.space_provider)
        self.api_key = api_key if api_key is not None else os.environ.get(env_key)
        if self.endpoint or self.config_error is not None:
            self.api_key = None
        if self.space_provider == "gemini" and model == DEFAULT_OPENAI_MODEL:
            model = DEFAULT_GEMINI_MODEL
        self.model = model
        self.custom_hook = custom_hook
        self.space_fingerprint = space_fingerprint or current
        # Per client, so a client's cache dies with it; failures are never cached.
        self.embed_query = functools.lru_cache(maxsize=2048)(self._embed_query)
        self._usage_lock = threading.Lock()
        self._openai_requests: int = 0
        self._openai_prompt_tokens: int = 0

    def safe(self, text: str) -> str:
        """`text` fit for an exception or a log: redacted, THEN cut."""
        return redacted(text, *self._secrets)[:ERROR_MESSAGE_CHARS]

    @contextlib.contextmanager
    def _redacting(self) -> Iterator[None]:
        """Every EmbeddingError leaving the client is redacted and cut, with no
        chained cause that could still carry a credential into a log."""
        try:
            yield
        except EmbeddingError as e:
            raise type(e)(self.safe(str(e))) from None

    def set_hook(self, hook: Callable[[str], list[float]] | None) -> None:
        self.custom_hook = hook

    def is_configured(self) -> bool:
        return self.config_error is None and bool(
            self.api_key or self.endpoint or self.custom_hook
        )

    def usage_stats(self) -> EmbeddingUsageStats:
        with self._usage_lock:
            return EmbeddingUsageStats(
                openai_requests=self._openai_requests,
                openai_prompt_tokens=self._openai_prompt_tokens,
            )

    def record_usage(self, requests: int, prompt_tokens: int) -> None:
        with self._usage_lock:
            self._openai_requests += requests
            self._openai_prompt_tokens += prompt_tokens

    def _embed_query(self, text: str) -> list[float]:
        return self.embed_text(text, QUERY_TIMEOUT_S, attempts=1)

    def embed_document(
        self,
        text: str,
        *,
        guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    ) -> list[float]:
        return self.embed_text(text, guard=guard)

    def embed_documents(
        self,
        texts: list[str],
        *,
        guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    ) -> list[list[float]]:
        if not texts:
            return []
        if self.custom_hook:
            with guard() if guard is not None else contextlib.nullcontext():
                out = [self._hooked(t) for t in texts]
            self.record_usage(1, sum(len(t.split()) for t in texts))
            return out

        return self._request([openai_truncated(t) for t in texts], guard=guard)

    def embed_text(
        self,
        text: str,
        timeout: float | None = None,
        attempts: int = RETRY_ATTEMPTS,
        *,
        guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    ) -> list[float]:
        if self.custom_hook:
            with guard() if guard is not None else contextlib.nullcontext():
                vec = self._hooked(text)
            self.record_usage(1, len(text.split()))
            return vec

        texts = [openai_truncated(text)]
        return self._request(texts, timeout, attempts, guard)[0]

    def _hooked(self, text: str) -> list[float]:
        """The hook's vector, checked; any hook failure is a redacted EmbeddingError."""
        assert self.custom_hook is not None
        with self._redacting():
            try:
                values = self.custom_hook(text)
            except (OSError, ValueError, TypeError) as e:
                raise EmbeddingError(
                    f"embedding hook failed: {type(e).__name__}: {self.safe(str(e))}"
                ) from None
            return checked_vector(values, self.space_fingerprint.dimension)

    def _request(
        self,
        texts: list[str],
        timeout: float | None = None,
        attempts: int = RETRY_ATTEMPTS,
        guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    ) -> list[list[float]]:
        """The vectors for `texts`; each attempt ends within `timeout` seconds
        (INDEX_TIMEOUT_S by default) of elapsed time, however slowly the host answers."""
        with self._redacting():
            if self.config_error is not None:
                raise EmbeddingError(self.config_error)
            if self.space_provider == "gemini" and len(texts) > GEMINI_MAX_BATCH:
                return [
                    vec
                    for start in range(0, len(texts), GEMINI_MAX_BATCH)
                    for vec in self._request(
                        texts[start : start + GEMINI_MAX_BATCH],
                        timeout,
                        attempts,
                        guard,
                    )
                ]
            url, body, headers = self._prepared(texts)
            limit = self._answer_ceiling(len(texts))
            seconds = INDEX_TIMEOUT_S if timeout is None else timeout
            data = self._post_within(
                url, body, headers, seconds, limit, attempts, guard
            )
            try:
                return self._vectors(data, texts)
            except (
                AttributeError,
                TypeError,
                ValueError,
                KeyError,
                IndexError,
                OverflowError,
            ) as e:
                raise EmbeddingError(
                    f"{self.space_provider} embedding response is malformed: {e}"
                ) from e

    def _prepared(self, texts: list[str]) -> tuple[str, bytes, dict[str, str]]:
        """The request for `texts` in the active provider's format: url, body, headers."""
        payload: dict[str, Any]
        if self.endpoint:
            url = self.endpoint
            # No `dimensions`: a self-hosted model answers in its native size, padded below.
            payload = {"model": self.model, "input": texts}
            headers = {"Content-Type": "application/json"}
        elif not self.api_key:
            env_key = embedding_key_variable(self.space_provider)
            raise EmbeddingError(
                f"{env_key} environment variable is not set; embedding worker is idle"
            )
        elif self.space_provider == "gemini":
            url = f"{GEMINI_ENDPOINT}/{self.model}:batchEmbedContents"
            payload = {
                "requests": [
                    {
                        "model": f"models/{self.model}",
                        "content": {"parts": [{"text": t}]},
                        "outputDimensionality": DIMENSION,
                    }
                    for t in texts
                ]
            }
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            }
        else:
            url = OPENAI_ENDPOINT
            payload = openai_payload(texts)
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }

        return url, json.dumps(payload).encode("utf-8"), headers

    def _answer_ceiling(self, count: int) -> int:
        """The most bytes an answer of `count` vectors may take: a float in JSON is
        under 32 bytes, so 64 per value plus per-item and envelope room is generous;
        anything past it is a flood, not an answer. It assumes compact or modestly
        indented JSON: whitespace has no finite maximum, so an answer pretty-printed
        with a large indent is refused, by design."""
        native = self.space_fingerprint.dimension if self.endpoint else DIMENSION
        return 65536 + count * (native * 64 + 4096)

    def _parsed(self, raw: bytes) -> Any:
        """`raw` as JSON; nesting too deep for the parser is a malformed answer."""
        try:
            return json.loads(raw.decode("utf-8"))
        except RecursionError as e:
            raise EmbeddingError(
                f"{self.space_provider} embedding response is malformed: nested too deep"
            ) from e

    def _truncated(self, e: http.client.IncompleteRead) -> EmbeddingError:
        """The error for an answer that ended before its declared end."""
        return EmbeddingError(
            f"{self.space_provider} embedding answer was truncated: {e!r}"
        )

    def _whole(self, raw: bytes, missing: int | None, limit: int) -> bytes:
        """`raw` if it is the complete answer within `limit` bytes, else EmbeddingError."""
        if len(raw) > limit:
            raise EmbeddingError(
                f"{self.space_provider} embedding answer is larger than {limit} bytes"
            )
        if missing:
            raise EmbeddingError(
                f"{self.space_provider} embedding answer was truncated:"
                f" {missing} bytes missing"
            )
        return raw

    def _failure(self, status: int, reason: str, detail: str) -> EmbeddingError:
        """The error for an HTTP failure. The body IS INCLUDED: an exhausted balance
        429 and a rate limit 429 need opposite cures, and only the body tells them
        apart. Not cut here: `safe` cuts after redaction."""
        message = (
            f"{self.space_provider} embedding API call failed:"
            f" HTTP Error {status}: {reason}: {detail}"
        )
        if status == 429 and any(
            marker in detail for marker in QUOTA_EXHAUSTED_MARKERS
        ):
            return EmbeddingQuotaExhausted(message)
        if status == 429:
            return EmbeddingRateLimited(message)
        if 500 <= status <= 599:
            return EmbeddingTransportError(message)
        return EmbeddingError(message)

    def _post_within(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        seconds: float,
        limit: int,
        attempts: int = 1,
        guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    ) -> Any:
        """One synchronous POST that ends within `seconds` of elapsed time, retried
        `attempts` times on a status waiting cures.

        urllib's timeout bounds each socket operation, not the call: a host that
        trickles a byte at a time never trips it. Here the socket timeout is the
        time LEFT, set before the connect and before every read of the status line,
        the headers and the body; at the deadline the response and the connection
        are closed. No thread. Redirects and proxies are deliberately not followed:
        a 3xx surfaces as a malformed-response EmbeddingError. Name resolution is
        unbounded, and each address attempt, the TLS handshake and the request send
        are bounded per operation only: a hostname endpoint can stall in DNS, an IP
        literal cannot. The answer is refused past `limit` bytes or when truncated;
        an error page is kept only to its first 64 KiB.
        """
        for attempt in range(attempts):
            answer = self._post_once(url, body, headers, seconds, limit, guard)
            if not isinstance(answer, EmbeddingError):
                return answer
            if attempt == attempts - 1:
                raise answer
            time.sleep(2**attempt)
        raise EmbeddingError(f"{self.space_provider} embedding made no attempt")

    def _post_once(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        seconds: float,
        limit: int,
        guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    ) -> Any:
        """The parsed answer, or the EmbeddingError of a status in RETRYABLE_STATUS
        (returned so the caller may retry it); any other failure raises."""
        ends = time.monotonic() + seconds

        def left() -> float:
            remaining = ends - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no complete answer within {seconds} s")
            return remaining

        conn: http.client.HTTPConnection | None = None
        response: http.client.HTTPResponse | None = None
        try:
            # Inside the boundary: any URL error is an EmbeddingError, never raw.
            parts = urllib.parse.urlsplit(url)
            connection_class = (
                http.client.HTTPSConnection
                if parts.scheme == "https"
                else http.client.HTTPConnection
            )
            path = parts.path + (f"?{parts.query}" if parts.query else "")
            # ponytail: getaddrinfo is unbounded, and each address attempt, the TLS
            # handshake and the send get per-operation timeouts only; bounding them
            # needs a thread or a private http.client seam. An IP literal never
            # resolves.
            conn = connection_class(parts.hostname or "", parts.port, timeout=left())
            if guard is not None:
                # DNS and connection setup do not hold the store's write lock.
                conn.connect()
                left()
            with guard() if guard is not None else contextlib.nullcontext():
                if conn.sock is not None:
                    conn.sock.settimeout(left())
                conn.request("POST", path or "/", body=body, headers=headers)
            sock = conn.sock
            assert sock is not None
            # Read on our own socket: `getresponse` closes it under `Connection:
            # close`, and a closed socket cannot take the next read's timeout.
            response = http.client.HTTPResponse(sock, method="POST")
            response.fp.close()  # drops the makefile reference, not the socket
            response.fp = io.BufferedReader(_ReadsWithin(sock, left))
            response.begin()
            if response.status >= 400:
                # The status decides; the body is only a best-effort prefix, so a
                # stalled or truncated error body cannot change the class.
                prefix = b""
                try:
                    while len(prefix) < 65536:
                        left()
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        prefix += chunk
                except OSError, http.client.HTTPException:
                    pass
                detail = prefix[:65536].decode("utf-8", "replace")
                failure = self._failure(response.status, response.reason, detail)
                # Waiting on an exhausted balance is dead time: no wait restores it.
                if response.status in RETRYABLE_STATUS and not isinstance(
                    failure, EmbeddingQuotaExhausted
                ):
                    return failure
                raise failure
            chunks: list[bytes] = []
            size = 0
            while size <= limit:
                left()  # read1 can answer from the buffer without touching the socket
                chunk = response.read1(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            return self._parsed(self._whole(b"".join(chunks), response.length, limit))
        except http.client.IncompleteRead as e:
            raise self._truncated(e) from e
        except (OSError, http.client.HTTPException) as e:
            raise EmbeddingTransportError(
                f"{self.space_provider} embedding API call failed: {e}"
            ) from e
        except ValueError as e:
            raise EmbeddingError(
                f"{self.space_provider} embedding API call failed: {e}"
            ) from e
        finally:
            if response is not None:
                response.close()
            if conn is not None:
                conn.close()

    def _vectors(self, data: Any, texts: list[str]) -> list[list[float]]:
        """The response's vectors, checked and padded to DIMENSION."""
        if self.space_provider == "gemini":
            items = [e.get("values", []) for e in data.get("embeddings", [])]
            if len(items) != len(texts):
                raise EmbeddingError(f"expected {len(texts)} Gemini embedding results")
            tokens = sum(len(t.split()) for t in texts)
        else:
            results = data.get("data") if isinstance(data, dict) else None
            if not isinstance(results, list) or len(results) != len(texts):
                raise EmbeddingError(f"expected {len(texts)} OpenAI embedding results")
            indexed: dict[int, list[Any]] = {}
            for result in results:
                if not isinstance(result, dict):
                    raise EmbeddingError("OpenAI embedding result must be an object")
                index = result.get("index")
                if type(index) is not int or not 0 <= index < len(texts):
                    raise EmbeddingError(
                        "OpenAI embedding index must be an in-range integer"
                    )
                if index in indexed:
                    raise EmbeddingError("OpenAI embedding indices must be unique")
                values = result.get("embedding")
                if not isinstance(values, list):
                    raise EmbeddingError("OpenAI embedding result must carry a vector")
                indexed[index] = values
            # OpenAI identifies inputs by index; response position is not identity.
            items = [indexed[index] for index in range(len(texts))]
            tokens = data.get("usage", {}).get(
                "prompt_tokens", sum(len(t.split()) for t in texts)
            )
        self.record_usage(1, int(tokens))
        out: list[list[float]] = []
        native = self.space_fingerprint.dimension if self.endpoint else DIMENSION
        for vals in items:
            # A wrong model's size never lands in this space, padded or not.
            out.append(checked_vector(vals, native))
        return out
