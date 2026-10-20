"""Memory system adapters for comparative benchmarking."""

from __future__ import annotations

import time
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Any

from nmemory_evals.capture_gap import EvalRefusal
from nmemory_evals.comparison.base import (
    BaseMemoryAdapter,
    BenchmarkItem,
    MemorySystem,
)
from nmemory_evals.driver import Driver, EvaluationMode, prepare_database, tool_payload


class NMemoryAdapter(BaseMemoryAdapter):
    """Adapter for nMEMORY driven over public stdio JSON-RPC MCP interface."""

    def __init__(
        self, binary: Path | str, store_dir: Path, run_dir: Path | str
    ) -> None:
        self._binary = binary
        self._store_dir = store_dir
        self._run_dir = run_dir
        self._driver: Driver | None = None
        self._capsule_id_by_item: dict[str, str] = {}
        self._item_by_capsule_id: dict[str, str] = {}
        self._items_by_capsule_id: dict[str, list[str]] = {}
        self._by_text: dict[str, str] = {}
        self._ingested_count = 0
        self._req_seq = 1

    @property
    def system(self) -> MemorySystem:
        return MemorySystem.NMEMORY

    @property
    def provenance_preserved(self) -> bool:
        return True

    @property
    def tamper_evident_audit(self) -> bool:
        return True

    @property
    def explicit_abstain_support(self) -> bool:
        return True

    def _get_driver(self) -> Driver:
        if self._driver is None:
            # Opening never creates the store: make it when the directory has none.
            prepare_database(self._binary, self._store_dir, EvaluationMode.REUSE)
            self._driver = Driver.spawn(
                binary=self._binary,
                run_dir=self._run_dir,
                store_dir=self._store_dir,
                project="pynott",
                semantic=False,
            )
            self._driver.handshake()
        return self._driver

    def ingest_all(self, items: list[BenchmarkItem]) -> float:
        driver = self._get_driver()
        start = time.perf_counter()
        ingest_payload = [
            {
                "content": item.fact,
                "source": f"benchmark:{item.id}",
                # The anchor is the gold set's own, verbatim. `:1` was a line
                # INVENTED by the instrument to get through the re-obtainable
                # proof gate (#686): it masked 38 refusals and anchored each
                # fact on a line where it does not stand.
                "anchor": item.target,
                "kind": item.kind,
                "project_id": "pynott",
            }
            for item in items
        ]
        resp = driver.request(
            self._req_seq,
            "tools/call",
            {"name": "nmemory_capsule_ingest", "arguments": {"items": ingest_payload}},
        )
        self._req_seq += 1
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        payload = tool_payload(resp)
        outcomes = payload.get("outcomes", [])
        for item, outcome in zip(items, outcomes, strict=True):
            # A gate refusal is an outcome WITHOUT `id`: `outcome["id"]` raised
            # `KeyError`, which read as a driver failure. The product's refusal
            # names itself, with the item and the reason the server wrote.
            if outcome.get("status") == "rejected" or "id" not in outcome:
                raise EvalRefusal(
                    f"ingest refused for {item.id}: {outcome.get('error', outcome)}"
                )
            cid = str(outcome["id"])
            self._capsule_id_by_item[item.id] = cid
            self._item_by_capsule_id[cid] = item.id
            aliases = self.__dict__.setdefault("_items_by_capsule_id", {}).setdefault(
                cid, []
            )
            if item.id not in aliases:
                aliases.append(item.id)
            self.__dict__.setdefault("_by_text", {})[item.fact] = item.id
            self._ingested_count = int(getattr(self, "_ingested_count", 0)) + 1

        return elapsed_ms

    def _retrieve_payload(
        self,
        terms: list[str],
        k: int,
        *,
        allowed_ids: AbstractSet[str] | None = None,
        term_floor: float = 0.0,
        vector_floor: float = 0.0,
    ) -> tuple[dict[str, Any], float]:
        driver = self._get_driver()
        start = time.perf_counter()
        pool_limit = (
            max(
                k,
                int(getattr(self, "_ingested_count", 0)),
                len(getattr(self, "_capsule_id_by_item", {})),
                len(getattr(self, "_items_by_capsule_id", {})),
                100_000,
            )
            if allowed_ids is not None
            else k
        )
        arguments: dict[str, Any] = {
            "terms": terms,
            "project_id": "pynott",
            "lane": "term",
            "limit": pool_limit,
        }
        if allowed_ids is not None:
            arguments["token_budget"] = 10_000_000
        if term_floor > 0.0:
            arguments["term_floor"] = term_floor
        if vector_floor > 0.0:
            arguments["vector_floor"] = vector_floor
        resp = driver.request(
            self._req_seq,
            "tools/call",
            {
                "name": "nmemory_capsule_retrieve",
                "arguments": arguments,
            },
        )
        self._req_seq += 1
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        payload = tool_payload(resp)
        # The real server serves `outcome` in every retrieve response
        # (server.py:3352); its absence is an unknown shape, not an abstention
        # — the review showed that silence becoming a perfect abstention.
        if "outcome" not in payload:
            raise EvalRefusal(
                f"retrieve response without outcome: keys {sorted(payload)}"
            )
        # The outcome's domain: the protocol knows grounded/missing_evidence/
        # abstain. «error» or any other label passed presence validation and
        # the abstention counted it as a perfect hit (blocker B6).
        if payload["outcome"] not in ("grounded", "missing_evidence", "abstain"):
            raise EvalRefusal(
                f"outcome outside the protocol: {payload['outcome']!r} — an "
                "invalid response is never a metric"
            )
        if payload["outcome"] == "grounded" and "results" not in payload:
            raise EvalRefusal(
                f"retrieve response without results: keys {sorted(payload)} — "
                "grounded requires a declared results list"
            )
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise EvalRefusal(f"results of unknown shape: {type(results).__name__}")
        return payload, elapsed_ms

    def retrieve(self, terms: list[str], k: int = 5) -> tuple[list[str], str, float]:
        payload, elapsed_ms = self._retrieve_payload(terms, k)
        outcome = str(payload["outcome"])
        retrieved_item_ids: list[str] = []
        for r in payload.get("results", [])[:k]:
            cid = str(r.get("id"))
            retrieved_item_ids.append(self._item_by_capsule_id.get(cid, cid))
        return retrieved_item_ids, outcome, elapsed_ms

    def retrieve_texts(
        self,
        terms: list[str],
        k: int = 5,
        *,
        allowed_ids: AbstractSet[str] | None = None,
        term_floor: float = 0.0,
        vector_floor: float = 0.0,
    ) -> tuple[list[str], str, float]:
        payload, elapsed_ms = self._retrieve_payload(
            terms,
            k,
            allowed_ids=allowed_ids,
            term_floor=term_floor,
            vector_floor=vector_floor,
        )
        outcome = str(payload["outcome"])
        # The product's text field is `headline` (server.py:3352) — the real
        # contract, not whatever I invent. An evidence without it is a refusal,
        # never a window shortened on my own account.
        textos: list[str] = []
        item_map = getattr(self, "_item_by_capsule_id", {})
        items_map = getattr(self, "_items_by_capsule_id", {})
        for r in payload.get("results", []):
            if not isinstance(r, dict):
                raise EvalRefusal(f"evidence of unknown shape: {type(r).__name__}")
            texto = r.get("headline")
            if not isinstance(texto, str) or not texto.strip():
                raise EvalRefusal(
                    f"evidence without headline: keys {sorted(r)} — outcome {outcome!r}"
                )
            cid = str(r.get("id", ""))
            if allowed_ids is not None:
                aliases = items_map.get(cid) or [item_map.get(cid, cid)]
                if not any(aid in allowed_ids for aid in aliases):
                    continue
            textos.append(texto)
            if len(textos) >= k:
                break
        return textos, outcome, elapsed_ms

    def close(self) -> None:
        if self._driver is not None:
            self._driver.finish()
            self._driver = None
