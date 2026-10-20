"""The LIVE competitors over HTTP, without an SDK — and without an invented route.

The arena pre-registration (`evals/nmemory/tests/fixtures/PRE-REGISTERED-LOSS.txt`)
requires `LIVE = {Zep, Letta, mem0}` measured on the SAME harness, each with a
receipt. This module provides all three, and none of them needs a vendor
account:

* **mem0** self-hosted (Apache-2.0, `mem0/mem0-api-server`): the OSS server
  exposes `POST /memories` and `POST /search` WITHOUT the `/v1/` prefix — that
  prefix belongs only to the hosted `api.mem0.ai`, and confusing the two is the
  documented bug `mem0ai/mem0#4445`. Source: docs.mem0.ai/open-source/features/rest-api,
  read 2026-09-17.

  **The published image is arm64 only** (measured 2026-09-17:
  `docker manifest inspect` lists `linux/arm64` and nothing else), so it does
  NOT run on the amd64 host where the arena runs, and the owner's other host is
  arm64 without docker. Self-hosting mem0 here requires building the image from
  the vendor's own Dockerfile — which is still THEIR server, not a wrapper of
  mine, and that is the difference LOCK 1 requires. This is race
  work, not adapter work: the adapter talks to any address that answers.
* **Letta** self-hosted (`letta/letta:latest`, port 8283, bearer with
  `LETTA_SERVER_PASSWORD`): archival memory is the store, and the SAME OpenAPI
  interface serves both Letta Cloud and the open-source server. Source:
  docs.letta.com/v1-sdk/docker, read 2026-09-17.

* **Zep** Community Edition (`ghcr.io/getzep/zep`, port 8000): the CE was
  discontinued as a PRODUCT in April 2025, and that is where this file's
  earlier conclusion came from — «either a Zep Cloud account, or swap `Zep` for
  `Graphiti` in the ratified set». That conclusion was wrong because it had
  never been tested: the 0.27.2 image (2024-09-11) still pulls, starts against
  a Postgres with pgvector and serves the sessions. Measured 2026-09-17: `add`
  200, `search` 200 with the nearest first, correct rank-1 on all three
  questions, ~180 ms. So the pre-registration stands as ratified, without a
  paid account and without swapping the set.

  Two of its shapes that the adapter respects instead of working around. (1)
  The CE does NOT serve `openapi.json`: it publishes `X-Zep-Version` on every
  response, and that is the receipt's `sdk_version` (see `probe_header`). (2)
  The binary has `text-embedding-ada-002` HARD-CODED, with no config or
  variable — if the OpenAI account does not serve that retired model, search
  dies with 403 and reads as «Zep found nothing». The fix is
  `llm.openai_endpoint` in the Zep config pointed at a model-name rewriter; the
  vectors still come from OpenAI, and only the name Zep hard-coded is what gets
  rewritten. Recipe in `tools/zep-ce/`.

## No path is guessed

Every adapter DISCOVERS the routes in the server's own `openapi.json` before
using them — and where there is no OpenAPI (Zep CE), it ASKS for the route and
reads the version from the header. Two things for free: a route that does not
exist in that version refuses by NAME (never a silent 404 turning into «the
vendor found nothing»), and the server's `info.version` is the receipt's
`sdk_version` — read from the vendor's process, as the `Receipt` law requires,
instead of a string I wrote myself.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from nmemory_evals.capture_gap import EvalRefusal
from nmemory_evals.comparison.base import (
    BaseMemoryAdapter,
    BenchmarkItem,
    MemorySystem,
    Receipt,
)

_TIMEOUT: Final = 60.0

#: A benchmark step that takes longer than this is not a latency number, it is
#: a stuck run: refuse instead of poisoning the average.
_INGEST_TIMEOUT: Final = 300.0


@dataclass(frozen=True)
class Discovery:
    """What the vendor's server says about itself."""

    version: str
    paths: frozenset[str]
    #: route → names of the GET parameters it accepts. Exists for the same
    #: reason as `paths`: a `?search=` I picked that the server ignored would
    #: return the whole page unfiltered, and that would read as perfect recall.
    get_params: dict[str, frozenset[str]]

    def param(self, path: str, candidates: tuple[str, ...]) -> str:
        """The first candidate parameter the route ANNOUNCES, or refusal."""
        offered = self.get_params.get(path, frozenset())
        for name in candidates:
            if name in offered:
                return name
        raise EvalRefusal(
            f"{path}: none of {list(candidates)} among the advertised parameters "
            f"{sorted(offered)} — without a search filter the route returns everything "
            "and recall comes out perfect by accident"
        )


def _call(
    url: str,
    payload: dict[str, Any] | None,
    *,
    token: str | None,
    timeout: float,
    method: str = "POST",
) -> tuple[Any, float]:
    """One JSON call, and the REAL wall time it took.

    HTTP, network, or non-JSON-body errors are all refusals: a vendor that did
    not answer did not produce a zero, it produced an empty cell.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        raise EvalRefusal(f"{method} {url}: HTTP {err.code} {err.reason}") from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise EvalRefusal(f"{method} {url}: no response ({err!r})") from err
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    try:
        return json.loads(raw.decode()), elapsed_ms
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise EvalRefusal(f"{method} {url}: response is not JSON ({err!r})") from err


def discover(
    base_url: str, *, token: str | None, required: tuple[str, ...]
) -> Discovery:
    """Reads the server's `openapi.json` and requires the routes the adapter uses.

    The `info.version` is the identity the vendor gives itself and goes into
    the receipt. A missing route refuses here, naming it: it is the difference
    between «this product version does not have this operation» and «the
    product found nothing».
    """
    doc, _ = _call(
        f"{base_url.rstrip('/')}/openapi.json",
        None,
        token=token,
        timeout=_TIMEOUT,
        method="GET",
    )
    if not isinstance(doc, dict):
        raise EvalRefusal(f"{base_url}: openapi.json is not an object")
    version = str((doc.get("info") or {}).get("version") or "").strip()
    if not version:
        raise EvalRefusal(
            f"{base_url}: openapi.json without `info.version` — without the server's "
            "identity there is no receipt, and without a receipt the number is not publishable"
        )
    raw_paths = doc.get("paths") or {}
    paths = frozenset(str(p) for p in raw_paths)
    get_params: dict[str, frozenset[str]] = {}
    for path, ops in raw_paths.items():
        params = (ops or {}).get("get", {}).get("parameters") or []
        get_params[str(path)] = frozenset(
            str(prm["name"])
            for prm in params
            if isinstance(prm, dict) and "name" in prm
        )
    missing = [p for p in required if p not in paths]
    if missing:
        raise EvalRefusal(
            f"{base_url} (version {version}): missing routes {missing}; "
            f"the server announces {len(paths)} routes"
        )
    return Discovery(version=version, paths=paths, get_params=get_params)


class Mem0HttpAdapter(BaseMemoryAdapter):
    """mem0 OSS over HTTP. Routes without `/v1/`, as the self-hosted server serves them."""

    ADD: Final = "/memories"
    SEARCH: Final = "/search"

    def __init__(
        self,
        base_url: str,
        *,
        user_id: str,
        token: str | None = None,
        infer: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._user = user_id
        self._token = token
        #: `infer=False` belongs to axis B: mem0 OSS extracts with an LLM per
        # message (~60 s/session; measured 2026-09-17) and seeding the corpus
        # would take days. Without the flag the body does not carry it — the
        # product as it is.
        self._infer = infer
        self._discovery = discover(
            self._base, token=token, required=(self.ADD, self.SEARCH)
        )
        #: vendor memory id → benchmark item id. mem0 rewrites and merges
        # facts, so one item can spawn several memories and one memory can
        # survive a merge: the map is many-to-one, and an id that comes back
        # from search without being here is a memory the vendor synthesized —
        # it counts as a miss, and the id stays raw in the observation.
        self._by_memory: dict[str, str] = {}
        self._ingested_count = 0

    @property
    def system(self) -> MemorySystem:
        return MemorySystem.MEM0

    @property
    def provenance_preserved(self) -> bool:
        """False by the product's shape, not by opinion: mem0 extracts and
        rewrites facts out of messages and stores text plus metadata — there is
        no `file:line` concept to preserve."""
        return False

    @property
    def tamper_evident_audit(self) -> bool:
        """The OSS server has a `/logs` of requests, which is an operational
        trail, not a hash chain over content."""
        return False

    @property
    def explicit_abstain_support(self) -> bool:
        """`POST /search` returns a list, possibly empty. An empty list is an
        absence of results, not an abstention the system declared."""
        return False

    def ingest_all(self, items: list[BenchmarkItem]) -> float:
        total = 0.0
        for item in items:
            doc, ms = _call(
                f"{self._base}{self.ADD}",
                {
                    "messages": [{"role": "user", "content": item.fact}],
                    "user_id": self._user,
                    "metadata": {"item_id": item.id, "category": item.category},
                    **({} if self._infer else {"infer": False}),
                },
                token=self._token,
                timeout=_INGEST_TIMEOUT,
            )
            for mid in _mem0_ids(doc):
                self._by_memory[mid] = item.id
            self._ingested_count = int(getattr(self, "_ingested_count", 0)) + 1
            if self.receipt is None:
                self.receipt = Receipt(
                    endpoint=f"{self._base}{self.ADD}",
                    sdk_version=self._discovery.version,
                    response_id=next(iter(_mem0_ids(doc)), ""),
                    data=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            total += ms
        return total

    def retrieve(self, terms: list[str], k: int = 5) -> tuple[list[str], str, float]:
        doc, ms = _call(
            f"{self._base}{self.SEARCH}",
            {"query": " ".join(terms), "user_id": self._user, "limit": k},
            token=self._token,
            timeout=_TIMEOUT,
        )
        # `hit` is about the CORPUS, not about list length: mem0 merges and
        # rewrites facts, and an id it created comes back raw in the observation
        # (deliberate) but is not an item anyone measured. A `hit` over zero
        # corpus ids would be a hit recorded in the published evidence.
        mapped = [(mid, self._by_memory.get(mid)) for mid in _mem0_ids(doc)][:k]
        ids = [known or mid for mid, known in mapped]
        return ids, ("hit" if any(known for _, known in mapped) else "miss"), ms

    def retrieve_texts(
        self,
        terms: list[str],
        k: int = 5,
        *,
        allowed_ids: AbstractSet[str] | None = None,
        **_: Any,
    ) -> tuple[list[str], str, float]:
        pool_limit = (
            max(
                k,
                int(getattr(self, "_ingested_count", 0)),
                len(self._by_memory),
                100_000,
            )
            if allowed_ids is not None
            else k
        )
        doc, ms = _call(
            f"{self._base}{self.SEARCH}",
            {
                "query": " ".join(terms),
                "user_id": self._user,
                "limit": pool_limit,
            },
            token=self._token,
            timeout=_TIMEOUT,
        )
        # The text mem0 SERVES is its own — merged, rewritten, as it comes.
        # Rewriting it back to the seed text would be me answering.
        raw_texts = _mem0_texts(doc)
        raw_ids = _mem0_ids(doc)
        if allowed_ids is not None:
            filtered = [
                (t, self._by_memory.get(mid))
                for t, mid in zip(raw_texts, raw_ids, strict=False)
                if self._by_memory.get(mid) in allowed_ids
            ][:k]
            textos = [t for t, _ in filtered]
            return (
                textos,
                ("hit" if any(known for _, known in filtered) else "miss"),
                ms,
            )
        textos = raw_texts[:k]
        mapped = [(mid, self._by_memory.get(mid)) for mid in raw_ids][:k]
        return textos, ("hit" if any(known for _, known in mapped) else "miss"), ms


class LettaHttpAdapter(BaseMemoryAdapter):
    """Letta self-hosted: archival memory is the store, by passages."""

    PASSAGES: Final = "/v1/agents/{agent_id}/archival-memory"
    #: The product's OWN search. The list route also accepts `?search=`, but
    #: measured on 0.11.7 on 2026-09-17 that filter is a literal substring —
    #: a one-word `search=` returned 1 passage and a two-word `search=` returned 0
    #: with both words in that same passage — so a multi-word question never matches and
    #: the arm would publish 0.0% without the product having failed. This one
    #: is semantic.
    SEARCH: Final = "/v1/agents/{agent_id}/archival-memory/search"

    def __init__(
        self, base_url: str, *, agent_id: str, token: str | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._agent = agent_id
        self._token = token
        self._discovery = discover(
            self._base, token=token, required=(self.PASSAGES, self.SEARCH)
        )
        self._search_param = self._discovery.param(
            self.SEARCH, ("query", "search", "search_query")
        )
        self._limit_param = self._discovery.param(self.SEARCH, ("top_k", "limit"))
        #: VERBATIM passage text → item id. The Letta semantic search result
        #: carries `timestamp`, `content` and `tags` and NO id at all (schema
        #: `ArchivalMemorySearchResult`, read from the server's openapi), so the
        #: verbatim content is the only identity channel. Mapping by text
        #: instead of prefixing `[id]` onto the passage keeps what the product
        #: indexes and ranks intact. The equality is EXACT: collapsing
        #: whitespace would merge `if x:\n    a()\n    b()` with
        #: `if x:\n    a()\nb()`, which are different programs, and an outside
        #: passage could normalize onto a corpus item — a fabricated hit
        #: (finding from the review of #605).
        self._by_text: dict[str, str] = {}
        self._ingested_count = 0

    @property
    def _url(self) -> str:
        return f"{self._base}{self.PASSAGES.format(agent_id=self._agent)}"

    @property
    def _search_url(self) -> str:
        return f"{self._base}{self.SEARCH.format(agent_id=self._agent)}"

    @property
    def system(self) -> MemorySystem:
        return MemorySystem.LETTA

    @property
    def provenance_preserved(self) -> bool:
        """A passage is text plus embedding, with optional `source_id`; it does
        not carry the physical line the text came from."""
        return False

    @property
    def tamper_evident_audit(self) -> bool:
        return False

    @property
    def explicit_abstain_support(self) -> bool:
        return False

    def ingest_all(self, items: list[BenchmarkItem]) -> float:
        self._reject_ambiguous_corpus(items)
        self._require_empty_archive()
        total = 0.0
        for item in items:
            doc, ms = _call(
                self._url,
                {"text": item.fact},
                token=self._token,
                timeout=_INGEST_TIMEOUT,
            )
            if not _letta_ids(doc):
                raise EvalRefusal(
                    f"item {item.id!r}: the passage POST returned no id at all "
                    "— without proof of ingestion of THIS item its searches come out "
                    "empty and the arm would publish zero instead of refusing"
                )
            self._by_text[item.fact] = item.id
            self._ingested_count = int(getattr(self, "_ingested_count", 0)) + 1
            if self.receipt is None:
                self.receipt = Receipt(
                    endpoint=self._url,
                    sdk_version=self._discovery.version,
                    response_id=next(iter(_letta_ids(doc)), ""),
                    data=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            total += ms
        return total

    def _reject_ambiguous_corpus(self, items: list[BenchmarkItem]) -> None:
        """Two IDENTITY ambiguities, refused BEFORE the first POST — finding
        them halfway would leave the archive half full.

        Two items with the same text: this route's search returns no id, so
        content is the identity and the rank would be undecidable. An item
        whose id already starts with the reserved prefix: a passage from
        OUTSIDE the corpus receives that name, and a collision would give that
        item a hit nobody measured — the runner scores by id equality and does
        not look at `outcome`.
        """
        por_texto: dict[str, str] = {}
        for item in items:
            if item.id.startswith(_FOREIGN):
                raise EvalRefusal(
                    f"item {item.id!r} uses the prefix {_FOREIGN!r}, reserved for "
                    "passages from outside the corpus: a collision would fabricate a hit"
                )
            anterior = por_texto.setdefault(item.fact, item.id)
            if anterior != item.id:
                raise EvalRefusal(
                    f"items {anterior!r} and {item.id!r} have the same text: the Letta "
                    "search returns no id, so the content is the identity and "
                    "two items with the same text make the rank undecidable"
                )

    def _require_empty_archive(self) -> None:
        """The archive must START EMPTY, and that is verified — not assumed.

        Identity travels through content: a passage left over from another run
        with the same text gets credit as an item of this one, and the arm
        would measure the agent's history instead of the ingestion I just made.
        """
        doc, _ = _call(
            f"{self._url}?{urllib.parse.urlencode({'limit': 1})}",
            None,
            token=self._token,
            timeout=_TIMEOUT,
            method="GET",
        )
        if _letta_ids(doc):
            raise EvalRefusal(
                f"{self._url}: the agent's archive is not empty — identity "
                "by content would credit passages from another run to this one"
            )

    def retrieve(self, terms: list[str], k: int = 5) -> tuple[list[str], str, float]:
        query = urllib.parse.urlencode(
            {self._search_param: " ".join(terms), self._limit_param: k}
        )
        doc, ms = _call(
            f"{self._search_url}?{query}",
            None,
            token=self._token,
            timeout=_TIMEOUT,
            method="GET",
        )
        # No dedup: if the product spent two slots on the same fact, that is
        # what it returned, and collapsing them would improve its rank on my
        # own account.
        mapped = [(texto, self._by_text.get(texto)) for texto in _letta_texts(doc)]
        mapped = mapped[:k]
        ids = [known or f"{_FOREIGN}{_fingerprint(texto)}" for texto, known in mapped]
        return ids, ("hit" if any(known for _, known in mapped) else "miss"), ms

    def retrieve_texts(
        self,
        terms: list[str],
        k: int = 5,
        *,
        allowed_ids: AbstractSet[str] | None = None,
        **_: Any,
    ) -> tuple[list[str], str, float]:
        pool_limit = (
            max(
                k,
                int(getattr(self, "_ingested_count", 0)),
                len(self._by_text),
                100_000,
            )
            if allowed_ids is not None
            else k
        )
        query = urllib.parse.urlencode(
            {
                self._search_param: " ".join(terms),
                self._limit_param: pool_limit,
            }
        )
        doc, ms = _call(
            f"{self._search_url}?{query}",
            None,
            token=self._token,
            timeout=_TIMEOUT,
            method="GET",
        )
        # The route already is the product's semantic search; the texts are its
        # `content` values, in the order they came. No dedup, by the same
        # principle as retrieve.
        raw_texts = _letta_texts(doc)
        if allowed_ids is not None:
            raw_texts = [t for t in raw_texts if self._by_text.get(t) in allowed_ids]
        textos = raw_texts[:k]
        mapped = [(texto, self._by_text.get(texto)) for texto in textos]
        return textos, ("hit" if any(known for _, known in mapped) else "miss"), ms


def _mem0_ids(doc: Any) -> list[str]:
    """The memory ids of a mem0 response, in both shapes it uses:
    `{"results": [...]}` and the bare list. A shape I do not recognize is a
    refusal — reading zero ids out of a full response would be an invented
    miss."""
    return _ids_of(doc, ("id", "memory_id"))


def _letta_ids(doc: Any) -> list[str]:
    return _ids_of(doc, ("id", "passage_id"))


#: The name a passage from OUTSIDE the corpus gets in the observation. It is a
#: RESERVED prefix: `_reject_ambiguous_corpus` refuses a benchmark item that
#: uses it, because the runner scores by id equality and a collision would
#: fabricate a hit.
_FOREIGN: Final = "letta:"


def _fingerprint(texto: str) -> str:
    """A stable name for a passage that is not from the corpus — short, because
    it goes into the recorded observation, and it never collides with a
    benchmark id."""
    return hashlib.sha256(texto.encode()).hexdigest()[:12]


def _letta_texts(doc: Any) -> list[str]:
    """The contents of an archival-search response, in the order it gave them.

    A row without `content` is a refusal: it is this route's only identity
    channel, and reading it as an empty list would be an invented miss.
    """
    if isinstance(doc, dict) and doc.get("error"):
        raise EvalRefusal(
            f"archival search with an error in the body: {doc['error']!r} — a response "
            "that the server itself marks as a failure is not zero results"
        )
    rows = doc.get("results") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise EvalRefusal(f"archival search of unknown shape: {type(rows).__name__}")
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise EvalRefusal(f"row of unknown shape: {type(row).__name__}")
        texto = row.get("content")
        if not isinstance(texto, str) or not texto.strip():
            raise EvalRefusal(
                f"row without `content`: keys {sorted(row)} — without the text there is no "
                "identity on this route, and zero results is not the same as a "
                "response I cannot read"
            )
        out.append(texto)
    return out


def _ids_of(doc: Any, keys: tuple[str, ...]) -> list[str]:
    rows = (
        doc.get("results", doc.get("memories", doc.get("passages")))
        if isinstance(doc, dict)
        else doc
    )
    if rows is None and isinstance(doc, dict):
        rows = [doc] if any(k in doc for k in keys) else []
    if not isinstance(rows, list):
        raise EvalRefusal(f"response of unknown shape: {type(rows).__name__}")
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise EvalRefusal(f"row of unknown shape: {type(row).__name__}")
        found = next((str(row[k]) for k in keys if row.get(k)), None)
        if found is None:
            raise EvalRefusal(f"row without id ({keys}): keys {sorted(row)}")
        out.append(found)
    return out


def probe_header(
    url: str, *, token: str | None, header: str, timeout: float = _TIMEOUT
) -> str:
    """GETs a route and returns the value of `header`, or refuses.

    Exists because Zep CE 0.27.2 does not serve `openapi.json`: it publishes its
    version in the header of every response, and the routes either exist or 404.
    Asking for the route is the only proof that it exists, and the header is the
    only identity the server gives of itself — the two things `discover()` takes
    from the OpenAPI.
    """
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            value = str(resp.headers.get(header) or "").strip()
    except urllib.error.HTTPError as err:
        raise EvalRefusal(
            f"GET {url}: HTTP {err.code} {err.reason} — the route is not there, "
            "and a 404 during measurement reads as «the vendor found nothing»"
        ) from err
    except (urllib.error.URLError, TimeoutError) as err:
        raise EvalRefusal(f"GET {url}: no response ({err!r})") from err
    if not value:
        raise EvalRefusal(
            f"{url}: response without {header} — without the server's identity there is "
            "no receipt, and without a receipt the number is not publishable"
        )
    return value


class ZepHttpAdapter(BaseMemoryAdapter):
    """Zep Community Edition self-hosted (`ghcr.io/getzep/zep`, port 8000).

    The CE was discontinued as a product, but the 0.27.2 image (2024-09-11)
    still exists and serves the sessions API: it is the pre-registration's third
    LIVE, without a paid account and without swapping the ratified set. Two of
    its shapes that this adapter respects instead of working around: there is no
    `openapi.json` (identity comes from the header) and `POST .../memory`
    answers `OK` in plain text, not JSON.
    """

    SESSIONS: Final = "/api/v1/sessions"
    MEMORY: Final = "/api/v1/sessions/{session_id}/memory"
    SEARCH: Final = "/api/v1/sessions/{session_id}/search"
    VERSION_HEADER: Final = "X-Zep-Version"

    def __init__(
        self,
        base_url: str,
        *,
        session_id: str,
        user_id: str = "nmemory-bench",
        token: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._session = session_id
        self._user = user_id
        self._token = token
        self._version = probe_header(
            f"{self._base}{self.MEMORY.format(session_id=session_id)}",
            token=token,
            header=self.VERSION_HEADER,
        )
        self._ingested_count = 0

    @property
    def _memory_url(self) -> str:
        return f"{self._base}{self.MEMORY.format(session_id=self._session)}"

    @property
    def _search_url(self) -> str:
        return f"{self._base}{self.SEARCH.format(session_id=self._session)}"

    @property
    def system(self) -> MemorySystem:
        return MemorySystem.ZEP

    @property
    def provenance_preserved(self) -> bool:
        """A message carries free `metadata`, so I could put the item id there —
        but the product has no `file:line` anchor concept to preserve."""
        return False

    @property
    def tamper_evident_audit(self) -> bool:
        """The CE has a router request log, which is an operational trail, not a
        hash chain over content."""
        return False

    @property
    def explicit_abstain_support(self) -> bool:
        """`POST .../search` is a vector search: it always returns the `limit`
        nearest, even when none of them helps. It never declares an
        abstention."""
        return False

    def ingest_all(self, items: list[BenchmarkItem]) -> float:
        total = 0.0
        for item in items:
            # The response body is `OK` in plain text: using `_call` here would
            # refuse an ingest that worked. The time is measured the same way.
            body = json.dumps(
                {
                    "messages": [
                        {
                            "role": "user",
                            "role_type": "user",
                            "content": item.fact,
                            "metadata": {
                                "item_id": item.id,
                                "category": item.category,
                            },
                        }
                    ]
                }
            ).encode()
            req = urllib.request.Request(self._memory_url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            if self._token:
                req.add_header("Authorization", f"Bearer {self._token}")
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=_INGEST_TIMEOUT) as resp:
                    resp.read()
            except urllib.error.HTTPError as err:
                raise EvalRefusal(
                    f"POST {self._memory_url}: HTTP {err.code} {err.reason}"
                ) from err
            except (urllib.error.URLError, TimeoutError) as err:
                raise EvalRefusal(
                    f"POST {self._memory_url}: no response ({err!r})"
                ) from err
            total += (time.perf_counter() - start) * 1000.0
            self._ingested_count = int(getattr(self, "_ingested_count", 0)) + 1
        if items and self.receipt is None:
            # Zep's `add` returns no id at all. The real id exists: it is the
            # `uuid` the server gave the message, and it reads back out of the
            # session itself.
            doc, _ = _call(
                self._memory_url,
                None,
                token=self._token,
                timeout=_TIMEOUT,
                method="GET",
            )
            messages = (
                (doc or {}).get("messages") or [] if isinstance(doc, dict) else []
            )
            uuids = [
                str(m.get("uuid"))
                for m in messages
                if isinstance(m, dict) and m.get("uuid")
            ]
            if not uuids:
                raise EvalRefusal(
                    f"{self._memory_url}: the ingest answered but the session has no "
                    "message with a uuid — without a real id there is no receipt"
                )
            self.receipt = Receipt(
                endpoint=self._memory_url,
                sdk_version=self._version,
                response_id=uuids[-1],
                data=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        return total

    def retrieve(self, terms: list[str], k: int = 5) -> tuple[list[str], str, float]:
        doc, ms = _call(
            self._search_url,
            {"text": " ".join(terms), "limit": k},
            token=self._token,
            timeout=_TIMEOUT,
        )
        hits = doc if isinstance(doc, list) else []
        # The order is the server's: Zep returns `dist` as similarity and
        # already sorted. Reordering by a criterion of mine would measure my
        # own ranker.
        mapped: list[tuple[str, str | None]] = []
        for hit in hits[:k]:
            message = (hit or {}).get("message") or {} if isinstance(hit, dict) else {}
            meta = message.get("metadata") or {}
            item_id = meta.get("item_id") if isinstance(meta, dict) else None
            mapped.append(
                (str(message.get("uuid") or ""), str(item_id) if item_id else None)
            )
        ids = [known or uuid for uuid, known in mapped]
        return ids, ("hit" if any(known for _, known in mapped) else "miss"), ms

    def retrieve_texts(
        self,
        terms: list[str],
        k: int = 5,
        *,
        allowed_ids: AbstractSet[str] | None = None,
        **_: Any,
    ) -> tuple[list[str], str, float]:
        pool_limit = (
            max(k, int(getattr(self, "_ingested_count", 0)), len(allowed_ids), 100_000)
            if allowed_ids is not None
            else k
        )
        doc, ms = _call(
            self._search_url,
            {
                "text": " ".join(terms),
                "limit": pool_limit,
            },
            token=self._token,
            timeout=_TIMEOUT,
        )
        if not isinstance(doc, list):
            # The review proved it: an ERROR response used to become [] and the
            # abstention cell counted that as a perfect hit. An unknown shape is
            # a refusal — legitimate zero results are an empty list, nothing
            # else.
            raise EvalRefusal(
                f"zep search of unknown shape: {type(doc).__name__} — {doc!r:.120}"
            )
        hits = doc
        # The order stays the server's; the text is the `content` of the message
        # Zep served. A row without text is a refusal, as in the other routes.
        textos: list[str] = []
        mapped: list[Any] = []
        for hit in hits:
            message = (hit or {}).get("message") or {} if isinstance(hit, dict) else {}
            texto = message.get("content")
            if not isinstance(texto, str) or not texto.strip():
                raise EvalRefusal(
                    f"search message without `content`: {sorted(message) or message!r}"
                )
            meta = message.get("metadata") or {}
            item_id = meta.get("item_id") if isinstance(meta, dict) else None
            if allowed_ids is not None and item_id not in allowed_ids:
                continue
            textos.append(texto)
            mapped.append(item_id)
            if len(textos) >= k:
                break
        return textos, ("hit" if any(m for m in mapped) else "miss"), ms


def _mem0_texts(doc: Any) -> list[str]:
    """The memory TEXTS mem0 serves, in both response shapes.

    The text is its own — merged, rewritten, as it comes. A row without text is
    a refusal: reading zero texts out of a full response would be an empty
    window invented by me.
    """
    rows = doc.get("results") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise EvalRefusal(f"response of unknown shape: {type(rows).__name__}")
    out: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise EvalRefusal(f"row of unknown shape: {type(row).__name__}")
        texto = row.get("memory", row.get("text"))
        if not isinstance(texto, str) or not texto.strip():
            raise EvalRefusal(f"row without memory text: keys {sorted(row)}")
        out.append(texto)
    return out


ARENA_URL_VARS: Final = {
    "zep": "NOTT_ARENA_ZEP_URL",
    "letta": "NOTT_ARENA_LETTA_URL",
    "mem0": "NOTT_ARENA_MEM0_URL",
}
#: The Letta agent must exist ON the server before the run (the archival memory
#: hangs off it), so the id is configuration, not something I invent.
LETTA_AGENT_VAR: Final = "NOTT_ARENA_LETTA_AGENT"


def build_seeding_adapter(name: str, *, run_id: str) -> BaseMemoryAdapter:
    """The axis-B adapter: the same one as `build_http_adapter`, with the mem0
    seed in `infer=false` — the measured flag; see Mem0HttpAdapter.__init__."""
    adapter = build_http_adapter(name, run_id=run_id)
    if isinstance(adapter, Mem0HttpAdapter):
        return Mem0HttpAdapter(
            adapter._base, user_id=adapter._user, token=adapter._token, infer=False
        )
    return adapter


def build_http_adapter(name: str, *, run_id: str) -> BaseMemoryAdapter:
    """The adapter for a LIVE, or a refusal naming what is missing.

    `run_id` goes into the identity each product uses to separate memories
    (session in Zep, `user_id` in mem0): two runs on the SAME instance would
    share the store, the second would read what the first wrote, and recall
    would come out perfect by contamination instead of by retrieval.
    """
    key = name.strip().lower()
    var = ARENA_URL_VARS.get(key)
    if var is None:
        raise EvalRefusal(
            f"{name!r} is not one of the pre-registration's LIVE "
            f"({sorted(ARENA_URL_VARS)}): the ratified set is not widened "
            "from the command line"
        )
    base = os.environ.get(var, "").strip()
    if not base:
        raise EvalRefusal(
            f"{key}: {var} is not set — without an address there is no product to "
            "measure, and a blank address would give zero hits that would read "
            "as the vendor's defeat"
        )
    if key == "zep":
        return ZepHttpAdapter(base, session_id=f"arena-{run_id}")
    if key == "mem0":
        return Mem0HttpAdapter(base, user_id=f"arena-{run_id}")  # default infer
    agent = os.environ.get(LETTA_AGENT_VAR, "").strip()
    if not agent:
        raise EvalRefusal(
            f"letta: {LETTA_AGENT_VAR} is not set — archival memory "
            "hangs off an agent that has to exist on the server"
        )
    return LettaHttpAdapter(base, agent_id=agent)
