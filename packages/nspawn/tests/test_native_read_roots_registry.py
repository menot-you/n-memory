"""Native read-grant provenance: validated at construction and in the store."""

import json
from dataclasses import FrozenInstanceError

import nott_database
import pytest
from registry_reset import sql
from test_native_session_registry import registry as _registry_fixture
from test_native_session_registry import row

from nspawn.registry import RegistryError
from nspawn.rows import NativeSessionContext, RunPhase

registry = _registry_fixture

ROOTS = ("/work/review", "/data/granted", "/usr/bin/python3.14")


@pytest.mark.parametrize(
    "roots",
    [
        (),
        [],
        ["/a"],
        ("relative",),
        ("/a/../b",),
        ("/a/./b",),
        ("//a",),
        ("/a/",),
        ("/a//b",),
        ("/bad\npath",),
        ("/bad\x7fpath",),
        ("/a", "/a"),
        (None,),
        ("/" + "a" * 4096,),
        tuple(f"/a{i}" for i in range(11)),
    ],
)
def test_read_root_constructor_refuses_invalid_provenance(roots):
    with pytest.raises((ValueError, TypeError)):
        NativeSessionContext("/account", read_roots=roots)


def test_read_roots_roundtrip_and_immutable(registry):
    context = NativeSessionContext("/account", read_roots=ROOTS)
    with pytest.raises(FrozenInstanceError):
        context.read_roots = None
    registry.admit_run(row("native"), False, 1, 100, native_context=context)
    assert registry.resolve_native_session("vendor-thread")[1] == context
    stored = sql(registry, "SELECT read_roots FROM native_contexts").fetchone()
    assert json.loads(stored["read_roots"]) == list(ROOTS)


@pytest.mark.parametrize(
    "bad", [[], [["/a"]], [None], [1], ["/a"] * 11, {"a": "/a"}, "/a"]
)
def test_database_refuses_wrong_array_shape(registry, bad):
    registry.insert_run(row("ctx"))
    with pytest.raises(nott_database.IntegrityError):
        sql(
            registry,
            "INSERT INTO native_contexts(run_id,home,read_roots) VALUES ('ctx','/account',?)",
            (json.dumps(bad),),
        )


def test_database_refuses_an_update_to_a_wrong_array_shape(registry):
    registry.insert_run(row("ctx"))
    sql(
        registry,
        "INSERT INTO native_contexts(run_id,home,read_roots) VALUES ('ctx','/account',?)",
        (json.dumps(["/a"]),),
    )
    with pytest.raises(nott_database.IntegrityError):
        sql(
            registry,
            "UPDATE native_contexts SET read_roots = ? WHERE run_id = 'ctx'",
            (json.dumps([None]),),
        )


@pytest.mark.parametrize(
    "bad", [["relative"], ["/a/../b"], ["/a", "/a"], ["/" + "a" * 4096]]
)
def test_malformed_stored_roots_refuse_at_constructor_boundary(registry, bad):
    registry.insert_run(row("ctx", phase=RunPhase.COMPLETED))
    sql(
        registry,
        "INSERT INTO native_contexts(run_id,home,read_roots) VALUES ('ctx','/account',?)",
        (json.dumps(bad),),
    )
    with pytest.raises(RegistryError, match="invalid context"):
        registry.resolve_native_session("vendor-thread")
