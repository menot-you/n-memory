"""Compare store operations with a Python model after each generated step."""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from nmemory.repositories.capsules import Tombstoned
from nmemory.retrieve import RetrieveParams, retrieve
from nmemory.server import MemoryServer
from nmemory.store import Store


class StoreIntegrityMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.store = Store.open_disposable_test_store()
        self.server = MemoryServer(store=self.store, hmac_env_key=b"integrity-test-key")
        self.records: dict[str, tuple[str, str, str, str]] = {}
        self.forgotten: set[str] = set()
        self.superseded: set[str] = set()

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        response = self.server.call_tool(name, args)
        assert "error" not in response, response
        result = response["result"]
        assert not result.get("isError"), result
        return json.loads(result["content"][0]["text"])

    def capture(
        self, words: list[str], kind: str, project: str, supersedes: str | None = None
    ) -> None:
        term = f"integrityterm{len(self.records)}x"
        content = f"The store keeps this test record for later reads. {term} {' '.join(words)}"
        args: dict[str, Any] = {
            "content": content,
            "kind": kind,
            "project_id": project,
            "source": "test:integrity",
            "anchor": "tests/test_integrity_stateful.py:1",
            "hints": False,
        }
        if supersedes is not None:
            args["supersedes"] = supersedes
        result = self.call("nmemory_capsule_ingest", args)
        assert result["captured"] == 1, result
        cid = result["outcomes"][0]["id"]
        self.records[cid] = (content, term, kind, project)
        if supersedes is not None:
            self.superseded.add(supersedes)

    @rule(
        words=st.lists(
            st.sampled_from(["read", "write", "store", "record", "test"]), max_size=8
        ),
        kind=st.sampled_from(["fact", "task", "plan"]),
        project=st.sampled_from(["integrity-one", "integrity-two", "integrity-three"]),
    )
    def ingest(self, words: list[str], kind: str, project: str) -> None:
        self.capture(words, kind, project)

    @precondition(lambda self: bool(self.records.keys() - self.forgotten))
    @rule(data=st.data())
    def supersede(self, data: st.DataObject) -> None:
        target = data.draw(
            st.sampled_from(sorted(self.records.keys() - self.forgotten))
        )
        _, _, kind, project = self.records[target]
        self.capture(["replacement"], kind, project, supersedes=target)

    @precondition(lambda self: bool(self.records.keys() - self.forgotten))
    @rule(data=st.data(), mode=st.sampled_from(["redacted", "purged"]))
    def forget(self, data: st.DataObject, mode: str) -> None:
        cid = data.draw(st.sampled_from(sorted(self.records.keys() - self.forgotten)))
        self.call(
            "nmemory_capsule_purge",
            {"id": cid, "mode": mode, "reason": "Remove a generated test record."},
        )
        self.forgotten.add(cid)

    @precondition(lambda self: bool(self.records))
    @rule(data=st.data())
    def read(self, data: st.DataObject) -> None:
        cid = data.draw(st.sampled_from(sorted(self.records)))
        if cid in self.forgotten:
            with pytest.raises(Tombstoned):
                self.store.capsules.get(cid)
        else:
            stored = self.store.capsules.get(cid)
            assert stored is not None
            assert stored.capsule.content == self.records[cid][0]

    def retrieved(self, term: str, project: str) -> set[str]:
        result = retrieve(
            self.store,
            RetrieveParams(query=term, terms=[term], project_id=project, lane="term"),
        )
        return {row["id"] for row in result["evidence"]}

    @precondition(lambda self: bool(self.records))
    @rule(data=st.data())
    def retrieve_exact_term(self, data: st.DataObject) -> None:
        cid = data.draw(st.sampled_from(sorted(self.records)))
        _, term, _, project = self.records[cid]
        expected = set() if cid in self.forgotten | self.superseded else {cid}
        assert self.retrieved(term, project) == expected

    @invariant()
    def model_and_store_agree(self) -> None:
        for cid, (content, term, kind, project) in self.records.items():
            if cid not in self.forgotten:
                stored = self.store.capsules.get(cid)
                assert stored is not None
                assert stored.capsule.content == content
                assert stored.capsule.scope.project_id == project
                classification = self.store.classifications.get_classification(cid)
                assert classification is not None
                assert classification.kind.value == kind
            if cid in self.superseded | self.forgotten:
                assert cid not in self.retrieved(term, project)
        chain = self.store.audit.verify_chain()
        assert chain.is_ok, chain
        assert chain.count >= len(self.records)
        with self.store.db.connection() as conn:
            assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


TestStoreIntegrity = StoreIntegrityMachine.TestCase
# test_property.py uses Hypothesis defaults. Bound this store test's I/O work.
TestStoreIntegrity.settings = settings(
    max_examples=10, stateful_step_count=20, deadline=None
)
