"""The relevance floor OVER THE WIRE: the live daemon, over stdio, with JSON-RPC in between.

What this file crosses is the REAL transport: `python -m nmemory` in a separate process,
the request going out as JSON over stdin and the response coming back as JSON over stdout
(`server.py run_stdio_server`). The FALSIFIER is threefold:

- the daemon REFUSING the field over the wire (`unknown field 'term_floor'`), which is the
  original defect measured on top of A1b;
- ACCEPTING IT and IGNORING IT, returning `grounded` on the same probe that the floor was
  supposed to prune — worse, because it lies silently;
- the test going back to calling `call_tool` directly, which serializes nothing. There is none
  here: the only channel is `StdioClient`, the same one as in `test_tools.py`.

THE VECTOR LANE IS NOT REACHABLE OVER THIS WIRE, and this measurement comes from the production
code and the environment of this box, not a choice of this test. `cli.py:822` constructs
`MemoryServer(store=..., project_id=..., read_only=...)` without passing `embeddings`, and
`server.py:1206-1207` completes it with `embeddings or EmbeddingClient()` — a client that
reads `OPENAI_API_KEY` from the ENVIRONMENT (`embedding.py:180`). The daemon inherits the
pytest environment (`StdioClient` does `os.environ.copy()`) and this box DOES have the key
set, so the daemon's vector lane is not disabled: it is connected to the real OpenAI, over the
network and costing money. No test may depend on that, and `spawn_embedding_worker` in
`cli.py:816` would drain the `embedding_jobs` that seeding enqueues (`store.py:784`) through
the same path.

Therefore `_wire` SHUTS the key (`OPENAI_API_KEY: ""`, the same axis that
`test_runtime_boundary.py` closes with `env.pop`) and the reads pin `lane: "term"`. With the
key shut, `is_configured()` is false and `retrieve.py:338` does not run the vector lane — which
makes it true, by construction and not by hope, that no probe from here goes out to a vendor.

The wire evidence that `vector_floor` HAS is, therefore, exactly this: it is admitted by the
port's deserializer (if it were not, read 1 would return `isError` with `unknown field`) and
reaches its domain validation, which responds `-32602` over the JSON-RPC channel. The
`missing_evidence` of the vector arm remains proved only in-process, by A1b — and is not
called "over the wire".

THE CLOCK IS THE HOST'S, and that is why the capsules are born PINNED. Over the wire there
is no `now` parameter: `run_stdio_server` calls `server.call_tool(tool_name, arguments)` with
two arguments, so `now` is real `datetime.now(UTC)`. The served `decayed_weight` is
`confidence * 2 ** (-idade_dias / 180)` rounded to 4 decimal places (`retrieve.py:498`, `:160`),
which drifts ~4e-8 per second: two replicas separated by seconds fall on the same side of the
rounding almost always and at other times not — a red flake of ~0.4% per run that would not be
about the floor. `decay = 1.0 if pinned` shuts that axis completely, and the floor is not
affected: retention reads `clears_relevance_floor` on the lane score (`retrieve.py:530`), where
`pinned` does not enter.

NAMES: the DoD writes `exclusions.below_relevance_floor` and `evidence: []`; the port serves
`excluded` and OMITS `results`. We assert what the port serves.
"""
#: `projetodeteste` and not `nmemory`: these tests seed RAW (`Store.append`,
#: which does not canonicalize) and read through the PORT (which canonicalizes).
#: With `nmemory` in the alias fold map — owner decision, 2026-09-19 — reading
#: requested `pynott` and the seed remained in `nmemory`: 30 red tests due to FIXTURE
#: asymmetry, not the product. A real session writes through ingest and reads
#: through the port, hence folding on both sides. The name only needs to be
#: OUTSIDE `PROJECT_ALIASES`.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from storefile import default_store_path
from test_retrieve_floor_at_the_door import (
    NOW,
    SEEDED,
    STOPWORD,
    TERM_FLOOR_ABOVE,
    VECTOR_FLOOR_ABOVE,
    _capsule,
    _payload,
    _rpc_code,
)
from test_tools import StdioClient

from nmemory.store import Store

#: The same test key that `test_tools.py` provides to the daemon.
HMAC_KEY = "wire-local-" * 4

#: The `_abs` probe: `terms` and `project_id` and nothing else. Each read adds
#: (or does not add) the two floors, and that is the only difference between them.
PROBE: dict[str, Any] = {
    "terms": [STOPWORD],
    "project_id": "projetodeteste",
    "lane": "term",
}

#: Outside the cosine domain (`[-1, 1]`): no score reaches it.
VECTOR_FLOOR_OUT_OF_DOMAIN = 1.5


def _seeded_lane_database(prefix: str = "rfltw") -> str:
    """FRESH database with the `SEEDED` capsules from A1b, and returns the URL for the daemon.

    Seeding is in-process because only the content matters and it must be
    IDENTICAL to A1b's — which fixes the measured BM25, `0.16556652460515572` for
    the four, between `TERM_FLOOR_BELOW` and `TERM_FLOOR_ABOVE`. What this file
    proves is not how capsules enter: it is what the port returns when they leave.
    """
    url = default_store_path(prefix)
    store = Store.open(url)
    for i in range(SEEDED):
        content = f"The bench note about the meter number {i} in the log."
        cid, _ = store.append(_capsule(content, f"t:{i}"), NOW)
        store.set_birth_classification(cid, "fact", "project", NOW)
        store.pins.set_pin(cid, True, "wire determinism", "test", NOW)
    return url


def _wire(url: str) -> StdioClient:
    """A fresh daemon, already initialized, speaking only over stdio and WITHOUT vendor key.

    The empty key is a fence, not cosmetic: see file header. `StdioClient` does
    `env_copy.update(env)` over a copy of the environment, so `""` is the only
    way to clear it from here — and `EmbeddingClient.is_configured()` checks
    `bool(self.api_key or self.custom_hook)`, for which `""` is absent.
    """
    client = StdioClient(
        env={
            "NOTT_DATABASE_DIR": str(Path(url).parent),
            "NOTT_NMEMORY_HMAC_KEY": HMAC_KEY,
            "OPENAI_API_KEY": "",
        }
    )
    client.send("initialize", {"protocolVersion": "2024-11-05"})
    return client


def _retrieve(args: dict[str, Any], prefix: str = "rfltw") -> dict[str, Any]:
    """ONE read: fresh database, fresh daemon, one call, and closes.

    The database and the daemon are fresh per read, and not out of economy: `retrieve`
    records a receipt per call and serves its id (`receipt_id`), so two calls
    to the SAME daemon would differ in `rcpt-1` versus `rcpt-2` and byte-by-byte
    comparison would no longer be about the floor.
    """
    client = _wire(_seeded_lane_database(prefix))
    try:
        return client.call_tool("nmemory_capsule_retrieve", args)
    finally:
        client.close()


# --------------------------------------------------------------------------
# READ 1 — both floors EXPLICITLY above, over the wire
# --------------------------------------------------------------------------


def test_both_floors_above_the_lane_score_serve_missing_evidence_on_the_wire() -> None:
    """The `missing_evidence` envelope arrives SERIALIZED, without `isError`.

    Both floors go in the SAME request on purpose: if the port's deserializer
    did not recognize one of them, the response would be `isError` with `unknown field`
    and none of the assertions below would ever run. This is how `vector_floor`
    is proved admitted over the wire on a box where its lane cannot run.
    """
    res = _retrieve(
        {
            **PROBE,
            "term_floor": TERM_FLOOR_ABOVE,
            "vector_floor": VECTOR_FLOOR_ABOVE,
        }
    )
    payload = _payload(res)
    assert payload["outcome"] == "missing_evidence", payload
    assert payload["excluded"]["below_relevance_floor"] == SEEDED, payload
    assert "results" not in payload, payload


# --------------------------------------------------------------------------
# READ 2 — both fields ABSENT serve today's envelope, byte-for-byte
# --------------------------------------------------------------------------


def test_absent_floors_and_explicit_zero_serve_the_same_bytes_on_the_wire() -> None:
    """`0.0` is an EXACT no-op on the wire, and proof is IN-RUN, not a committed fixture.

    We compare the entire JSON-RPC RESPONSE and not just the served text: the ids
    align across fresh clients (`initialize` = 1, `tools/call` = 2), so the
    envelope, id, and `jsonrpc` all enter the assertion for free.

    The first comparison is the CONTROL and without it the others are worthless: two
    absent reads that already differed from each other would make any subsequent
    difference unreadable. It was a control like this that caught the receipt counter
    in A1b.
    """
    absent_once = _retrieve(dict(PROBE))
    absent_twice = _retrieve(dict(PROBE))
    assert absent_once == absent_twice, (
        "envelope not deterministic without any floor: "
        f"{json.dumps(absent_once)[:400]} != {json.dumps(absent_twice)[:400]}"
    )

    explicit_zero = _retrieve({**PROBE, "term_floor": 0.0, "vector_floor": 0.0})
    assert explicit_zero == absent_once, (
        f"{json.dumps(explicit_zero)[:400]} != {json.dumps(absent_once)[:400]}"
    )

    # And today's envelope is indeed what we think it is: `grounded` with all
    # four. Without this, three identical error responses would pass the test.
    payload = _payload(absent_once)
    assert payload["outcome"] == "grounded", payload
    assert len(payload["results"]) == SEEDED, payload


# --------------------------------------------------------------------------
# TYPED refusal crosses the wire through the ERROR channel, not in-band
# --------------------------------------------------------------------------


def test_vector_floor_out_of_the_cosine_domain_is_refused_on_the_wire() -> None:
    """`run_stdio_server` merges the handler dict into the envelope: `error` is at top-level.

    This is the only thing `vector_floor` can prove OVER THE WIRE on this box —
    see file header — and the channel is the non-obvious half: the handler returns
    `{"jsonrpc", "error"}` and `run_stdio_server` does `{**resp_payload}` over
    `{"jsonrpc", "id"}` (`server.py:4811-4815`), so the refusal goes out as a real
    JSON-RPC error and not as an in-band `isError`. Both channels exist in
    nMEMORY and confusing them has already cost an erroneous reading.
    """
    res = _retrieve({**PROBE, "vector_floor": VECTOR_FLOOR_OUT_OF_DOMAIN})
    assert _rpc_code(res) == -32602
    assert "vector_floor" in res["error"]["message"], res
