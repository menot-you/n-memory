"""The trust boundary of `nmemory_capsule_retrieve`, by CHANNEL.

Three defects from #174 live here, and what unites them is the constructor that validates:

- **M1** — `time_window={"from": "ontem"}` slips through. The server only checks
  truthiness (`time_window.get("from") or ...`), and `retrieve.py` swallows the parser's
  `ValueError` with `pass`, leaving the bound as `None`. The fence vanishes and
  the caller receives MORE than requested. An equally malformed `known_at`, parsed
  by the SAME `parse_rfc3339`, already returns `-32602`: the asymmetry is indefensible.
- **B1/U0a** — `min_confidence` is read in `server.py` but is not in
  `TOOL_SPECS`, and the validator rejects unknown keys. The engine filters by
  it (`retrieve.py:445`) and no MCP caller can reach it.
- **B8** — the top commit of #174 flattened five assertions of
  `["error"]["code"] == -32602` into `is_error_response(...)`. Two were
  unsustainable, and the cure was to split by CHANNEL, never flatten all five.

The channel contract, MEASURED and not assumed:

| failure | channel |
|---|---|
| TYPE (`time_window: "x"`, `session_id: 12345`) | in-band `isError: true`, serde text |
| VALUE (`known_at`, `effort_id`, `topic_id`, and now `time_window`) | JSON-RPC `-32602` |

Flattening both into an `is_error_response` loses the exact distinction that
nMEMORY documents in its own server description (q88).
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
from datetime import UTC, datetime
from typing import Any

from storefile import sample_capsule_id

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.server import MemoryServer
from nmemory.store import Store

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
TERM = "cronometro"


def _server() -> MemoryServer:
    store = Store.open_disposable_test_store()
    for i in range(4):
        content = f"Nota sobre {TERM} de bancada numero {i}."
        cid, _ = store.append(
            Capsule(
                content=content,
                provenance=Provenance(
                    source="test",
                    anchor=f"t:{i}",
                    source_hash=sha256_hex(content.encode("utf-8")),
                ),
                confidence=0.9,
                freshness=Freshness(valid_from=NOW),
                scope=Scope(project_id="projetodeteste"),
                authority_class=AuthorityClass.USER_STATED,
                instruction_taint=False,
            ),
            NOW,
        )
        store.set_birth_classification(cid, "fact", "project", NOW)
    return MemoryServer(
        store=store, embeddings=None, anchor_root=".", project_id="projetodeteste"
    )


def _call(args: dict[str, Any]) -> dict[str, Any]:
    return _server().call_tool("nmemory_capsule_retrieve", args, None, now=NOW)


def _rpc_code(res: dict[str, Any]) -> int:
    """The JSON-RPC code, or a failure that NAMES the channel on which the response arrived."""
    assert "error" in res, (
        f"esperado erro JSON-RPC e veio pelo canal in-band: {json.dumps(res)[:200]}"
    )
    code: int = res["error"]["code"]
    return code


def _is_in_band_error(res: dict[str, Any]) -> bool:
    return bool(res.get("result", res).get("isError"))


# --------------------------------------------------------------------------
# M1 — the time fence fails CLOSED
# --------------------------------------------------------------------------


def test_time_window_rejects_a_malformed_from_bound() -> None:
    """RED: `{"from": "ontem"}` returns `grounded`/`abstain` with the fence wide open."""
    res = _call(
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "time_window": {"from": "ontem"},
        }
    )
    assert _rpc_code(res) == -32602


def test_time_window_rejects_a_malformed_to_bound() -> None:
    """The upper bound has its own mutation and its own red."""
    res = _call(
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "time_window": {"to": "amanha"},
        }
    )
    assert _rpc_code(res) == -32602


def test_time_window_still_accepts_well_formed_bounds() -> None:
    """The cure rejects malformed inputs without closing the door to valid ones."""
    res = _call(
        {
            "terms": [TERM],
            "project_id": "projetodeteste",
            "time_window": {"from": "2020-01-01T00:00:00Z"},
        }
    )
    assert "error" not in res, res
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["outcome"] in ("grounded", "missing_evidence"), payload


# --------------------------------------------------------------------------
# U0a — `min_confidence` reaches the engine
# --------------------------------------------------------------------------


def test_min_confidence_is_an_accepted_field() -> None:
    """RED: currently the validator returns `unknown field 'min_confidence'`."""
    res = _call(
        {"terms": [TERM], "project_id": "projetodeteste", "min_confidence": 0.1}
    )
    assert not _is_in_band_error(res), res
    assert "error" not in res, res


def test_min_confidence_actually_fences() -> None:
    """Being accepted is not enough: it must CUT. Capsules have confidence 0.9."""
    res = _call(
        {"terms": [TERM], "project_id": "projetodeteste", "min_confidence": 0.99}
    )
    assert "error" not in res, res
    payload = json.loads(res["result"]["content"][0]["text"])
    assert payload["outcome"] != "grounded", (
        f"min_confidence=0.99 must exclude 0.9 capsules; got {payload['outcome']}"
    )


# --------------------------------------------------------------------------
# B8 — each failure asserted on the channel it actually uses
# --------------------------------------------------------------------------


def test_type_faults_arrive_in_band_as_iserror() -> None:
    """TYPE faults come from the deserializer and arrive in-band (q88), never -32602."""
    for args in (
        {"terms": [TERM], "time_window": "invalid"},
        {"terms": [TERM], "session_id": 12345},
    ):
        res = _call(args)
        assert _is_in_band_error(res), f"{args} devia ser in-band: {res}"
        assert "error" not in res, f"{args} nao devia usar o canal JSON-RPC: {res}"


def test_value_faults_arrive_as_jsonrpc_32602() -> None:
    """VALUE faults pass the deserializer and are rejected by the verb."""
    for args in (
        {"terms": [TERM], "known_at": "ontem"},
        {"terms": [TERM], "effort_id": sample_capsule_id(999999)},
        {"terms": [TERM], "topic_id": sample_capsule_id(999999)},
        {"terms": [TERM], "session_id": "   "},
        {"terms": [TERM], "weight_blend": 1.5},
    ):
        res = _call(args)
        assert _rpc_code(res) == -32602, f"{args} devia ser -32602: {res}"
