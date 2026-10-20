"""schema.py: the registry's DDL, judged on a real file where it enforces."""

from collections.abc import Iterator
from contextlib import contextmanager

import nott_database
import pytest
from hypothesis import given
from hypothesis import strategies as st
from registry_reset import store

from nspawn import schema
from nspawn.registry import Registry, UnrecognizedSchema
from nspawn.rows import (
    RUN_EVENT_KIND_VALUES,
    AccountUsageRow,
    AccountWallRow,
    ProviderWallRow,
)

HASH = "0" * 64


def _write(statement: str, params: tuple[object, ...] = ()) -> None:
    conn = nott_database.connect(store())
    try:
        with nott_database.write(conn):
            conn.execute(statement, params)
    finally:
        conn.close()


def _run(run_id: str = "r-1") -> None:
    _write(
        "INSERT INTO runs(run_id, worktree, phase, sandbox, model, owner_instance, "
        "created_at, updated_at, backend) "
        "VALUES (?, '/w', 'running', 'read-only', 'opus', 'i', 1, 1, 'claude')",
        (run_id,),
    )


def _event(seq: int, kind: str = "lifecycle", payload: str = "{}") -> None:
    _write(
        "INSERT INTO run_events(run_id, seq, at, kind, payload, chained_hash) "
        "VALUES ('r-1', ?, 1, ?, ?, ?)",
        (seq, kind, payload, HASH),
    )


def test_a_created_store_carries_the_one_version_this_build_opens() -> None:
    conn = nott_database.connect(store(), readonly=True)
    try:
        assert nott_database.user_version(conn) == schema.REGISTRY_SCHEMA_VERSION == 7
    finally:
        conn.close()


def test_a_version_2_store_gains_the_key_fences_on_open() -> None:
    _write("DROP TRIGGER run_keys_refuse_fenced")
    _write("DROP TABLE key_fences")
    _write("DROP TABLE owner_stamps")
    _write("PRAGMA user_version = 2")
    with Registry(store(), "i-upgrade") as upgraded:
        assert upgraded.open(now=1) == schema.REGISTRY_SCHEMA_VERSION == 7
    _write("INSERT INTO key_fences VALUES ('/p', 'k', 1)")
    with pytest.raises(nott_database.IntegrityError, match="UNIQUE"):
        _write("INSERT INTO key_fences VALUES ('/p', 'k', 2)")
    _run()
    with pytest.raises(nott_database.IntegrityError, match="fenced"):
        _write(
            "INSERT INTO run_keys VALUES ('r-1', 'l-1', '/p', 'k', ?, ?)", (HASH, HASH)
        )


def test_a_version_3_store_gains_the_owner_stamps_on_open() -> None:
    """The table stays in the schema, so the store version does not change: a
    digest is unique and no run and no project scopes it."""
    _write("DROP TABLE owner_stamps")
    _write("PRAGMA user_version = 3")
    with Registry(store(), "i-upgrade") as upgraded:
        assert upgraded.open(now=1) == schema.REGISTRY_SCHEMA_VERSION == 7
        _run()
        _write("INSERT INTO owner_stamps VALUES (?, 'r-1', 1)", (HASH,))
        with pytest.raises(nott_database.IntegrityError, match="UNIQUE"):
            _write("INSERT INTO owner_stamps VALUES (?, 'r-2', 2)", (HASH,))
        _write("DELETE FROM runs WHERE run_id = 'r-1'")
    # Set back to 3 for an older release, then opened by this one again: every
    # row written before is still there.
    _write("PRAGMA user_version = 3")
    with Registry(store(), "i-again") as again:
        assert again.open(now=2) == 7
    with pytest.raises(nott_database.IntegrityError, match="UNIQUE"):
        _write("INSERT INTO owner_stamps VALUES (?, 'r-3', 3)", (HASH,))


#: The re-arm trigger as version 4 wrote it, before a notice counted its armings.
V4_ARM_TRIGGER = """
CREATE TRIGGER runs_arm_notices AFTER UPDATE OF phase ON runs
WHEN NEW.phase IN ('closed','completed','failed','orphaned')
 AND (OLD.phase NOT IN ('closed','completed','failed','orphaned')
      OR (OLD.phase = 'orphaned' AND NEW.phase <> OLD.phase))
BEGIN
    UPDATE notices
       SET phase = NEW.phase,
           ready_at = CAST(unixepoch('subsec') * 1000 AS INTEGER),
           claimed_at = NULL, claim_lease_until = NULL, claim_token = NULL,
           delivered_at = NULL
     WHERE run_id = NEW.run_id;
END;
"""


def _generation() -> int:
    conn = nott_database.connect(store(), readonly=True)
    try:
        (row,) = nott_database.query(
            conn, "SELECT generation FROM notices WHERE run_id = 'r-1'"
        )
        return int(row[0])
    finally:
        conn.close()


def test_a_version_4_store_gains_the_owner_and_the_generation_on_open() -> None:
    _write("DROP TRIGGER runs_arm_notices")
    _write(V4_ARM_TRIGGER)
    _write("ALTER TABLE notices DROP COLUMN generation")
    _write("ALTER TABLE runs DROP COLUMN owner")
    _write("PRAGMA user_version = 4")
    _run()
    _write(
        "INSERT INTO notices(run_id, caller_addr, caller_pid, caller_start_ticks, "
        "caller_project, created_at) VALUES ('r-1', 'node:n', 1, 0, '/p', 1)"
    )
    with Registry(store(), "i-upgrade") as upgraded:
        assert upgraded.open(now=1) == schema.REGISTRY_SCHEMA_VERSION == 7
    assert _generation() == 0
    _write("UPDATE runs SET phase = 'orphaned' WHERE run_id = 'r-1'")
    assert _generation() == 1
    _write("UPDATE runs SET owner = 'node:n' WHERE run_id = 'r-1'")
    with pytest.raises(nott_database.IntegrityError, match="CHECK"):
        _write("UPDATE runs SET owner = '' WHERE run_id = 'r-1'")
    # Set back to 4 for an older release, then opened by this one again: nothing
    # is added twice, every row stays, and the trigger still counts.
    _write("PRAGMA user_version = 4")
    with Registry(store(), "i-again") as again:
        assert again.open(now=2) == 7
        row = again.get_run("r-1")
        assert row is not None and row.owner == "node:n"
    _write("UPDATE runs SET phase = 'completed' WHERE run_id = 'r-1'")
    assert _generation() == 2


def test_a_fenced_key_binds_no_run_whoever_writes_the_row() -> None:
    _run()
    _write("INSERT INTO key_fences VALUES ('/p', 'k', 1)")
    with pytest.raises(nott_database.IntegrityError, match="fenced"):
        _write(
            "INSERT INTO run_keys VALUES ('r-1', 'l-1', '/p', 'k', ?, ?)", (HASH, HASH)
        )
    _write(
        "INSERT INTO run_keys VALUES ('r-1', 'l-1', '/p', 'other', ?, ?)", (HASH, HASH)
    )


def test_a_version_5_store_upgrades_without_guessing_old_terminal_receipts() -> None:
    # Reconstruct the prior schema in this test's own disposable file, not in
    # a live store. The owning Registry is the only upgrader under test.
    _write("DROP TRIGGER runs_check_admission")
    _write("DROP TRIGGER runs_record_terminal_receipt")
    for table in (
        "admission_keys",
        "admission_bindings",
        "node_holds",
        "terminal_receipts",
    ):
        _write(f"DROP TABLE {table}")
    _write("PRAGMA user_version = 5")
    _run()
    _write("UPDATE runs SET phase = 'completed', updated_at = 2 WHERE run_id = 'r-1'")
    with Registry(store(), "upgrader") as registry:
        assert registry.open(3) == 7
        assert registry.terminal_receipt_show("r-1") is None
        assert registry.admission_show("r-1") is None
        assert registry.get_run("r-1").phase.value == "completed"
        assert registry.database_rollback() == 5
        assert registry.upgrade() == 7
        assert registry.terminal_receipt_show("r-1") is None


def test_unknown_generation_at_the_upgrade_write_lock_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write("PRAGMA user_version = 5")
    with Registry(store(), "upgrader") as registry, Registry(store(), "peer") as peer:
        transaction = registry._transaction

        @contextmanager
        def peer_first() -> Iterator[None]:
            # The first reader saw 5; another opener commits a newer schema
            # before that reader obtains the owning write lock.
            with nott_database.write(peer.connection):
                peer.connection.execute("PRAGMA user_version = 8").close()
            with transaction():
                yield

        monkeypatch.setattr(registry, "_transaction", peer_first)
        with pytest.raises(UnrecognizedSchema, match="version 8"):
            registry.upgrade()
        assert nott_database.user_version(peer.connection) == 8


def test_the_schema_is_tables_then_triggers_then_the_version() -> None:
    assert schema.SCHEMA_DDL.startswith(schema.TABLES_DDL + schema.TRIGGERS_DDL)
    assert "CREATE TRIGGER" not in schema.TABLES_DDL


def test_run_events_is_append_only_and_its_sequence_only_increases() -> None:
    _run()
    _event(1)
    with pytest.raises(nott_database.IntegrityError, match="append-only"):
        _write("UPDATE run_events SET payload = '[]'")
    with pytest.raises(nott_database.IntegrityError, match="append-only"):
        _write("DELETE FROM run_events")
    with pytest.raises(nott_database.IntegrityError, match="must increase"):
        _event(1)
    with pytest.raises(nott_database.IntegrityError, match="existing run"):
        _write(
            "INSERT INTO run_events(run_id, seq, at, kind, payload, chained_hash) "
            "VALUES ('absent', 1, 1, 'lifecycle', '{}', ?)",
            (HASH,),
        )
    _event(2)


def test_run_events_refuses_an_open_kind_and_a_payload_that_is_not_json() -> None:
    _run()
    for kind in RUN_EVENT_KIND_VALUES:
        assert f"'{kind}'" in schema.TABLES_DDL
    with pytest.raises(nott_database.IntegrityError, match="CHECK"):
        _event(1, kind="invented")
    with pytest.raises(nott_database.IntegrityError, match="CHECK"):
        _event(1, payload="{not json")


def test_wall_source_values_matches_the_check_constraint() -> None:
    for source in schema.WALL_SOURCE_VALUES:
        _write("INSERT OR REPLACE INTO provider_walls VALUES ('p', 1, ?, 1)", (source,))
    with pytest.raises(nott_database.IntegrityError, match="CHECK"):
        _write("INSERT OR REPLACE INTO provider_walls VALUES ('p', 1, 'guess', 1)")


def test_the_usage_table_keys_on_the_account_AND_the_window() -> None:
    # With the alias alone as the key, an account's second window would overwrite
    # its first in silence — the composite key is what makes a window a row.
    _write("INSERT INTO account_usage VALUES ('a', 'five_hour', 0.5, 1, 1)")
    _write("INSERT INTO account_usage VALUES ('a', 'seven_day', 0.5, 1, 1)")
    with pytest.raises(nott_database.IntegrityError, match="UNIQUE"):
        _write("INSERT INTO account_usage VALUES ('a', 'five_hour', 0.1, 1, 1)")
    # No ceiling (overage is legal), and the floor stays.
    _write("INSERT INTO account_usage VALUES ('b', 'w', 1.5, 1, 1)")
    with pytest.raises(nott_database.IntegrityError, match="CHECK"):
        _write("INSERT INTO account_usage VALUES ('c', 'w', -0.1, 1, 1)")


def test_a_usage_row_is_built_straight_from_a_dict_row_record() -> None:
    record = {
        "account_alias": "nott-primary",
        "usage_window": "seven_day_oauth_apps",
        "utilization": 0.42,
        "resets_at": 1_700_000_000,
        "observed_at": 1_699_999_000,
    }
    row = AccountUsageRow(**record)
    assert row.usage_window == "seven_day_oauth_apps"
    assert row.utilization == 0.42
    # An overage reading is legal: the vendor's own `isUsingOverage` says so.
    assert AccountUsageRow(**{**record, "utilization": 1.5}).utilization == 1.5


@given(st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False))
def test_any_finite_non_negative_utilization_is_admitted(fraction: float) -> None:
    assert (
        AccountUsageRow(
            account_alias="a",
            usage_window="w",
            utilization=fraction,
            resets_at=0,
            observed_at=0,
        ).utilization
        == fraction
    )


@given(st.floats(max_value=-1e-9, allow_nan=False, allow_infinity=False))
def test_no_negative_utilization_is_admitted(fraction: float) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        AccountUsageRow(
            account_alias="a",
            usage_window="w",
            utilization=fraction,
            resets_at=0,
            observed_at=0,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_utilization_that_is_not_a_number_is_refused(bad: float) -> None:
    # PostgreSQL sorts NaN ABOVE every number, so `CHECK (utilization >= 0)`
    # would admit it and every later comparison would read a reading nobody made.
    with pytest.raises(ValueError, match="must be finite"):
        AccountUsageRow(
            account_alias="a",
            usage_window="w",
            utilization=bad,
            resets_at=0,
            observed_at=0,
        )


def test_a_boolean_utilization_is_refused_as_a_type_error() -> None:
    with pytest.raises(TypeError, match="must be a number"):
        AccountUsageRow(
            account_alias="a",
            usage_window="w",
            utilization=True,  # type: ignore[arg-type]
            resets_at=0,
            observed_at=0,
        )
    with pytest.raises(TypeError, match="must be a number"):
        AccountUsageRow(
            account_alias="a",
            usage_window="w",
            utilization="0.5",  # type: ignore[arg-type]
            resets_at=0,
            observed_at=0,
        )


def test_every_usage_row_bound_the_column_carries_is_refused_in_the_constructor() -> (
    None
):
    base: dict[str, object] = {
        "account_alias": "a",
        "usage_window": "w",
        "utilization": 0.0,
        "resets_at": 0,
        "observed_at": 0,
    }
    for field in ("account_alias", "usage_window"):
        with pytest.raises(ValueError, match=field):
            AccountUsageRow(**{**base, field: ""})
        with pytest.raises(ValueError, match=field):
            AccountUsageRow(**{**base, field: "x" * 513})
    for field in ("resets_at", "observed_at"):
        with pytest.raises(ValueError, match="must not be negative"):
            AccountUsageRow(**{**base, field: -1})
        with pytest.raises(TypeError, match="must be an integer"):
            AccountUsageRow(**{**base, field: 1.5})


def test_the_two_wall_rows_carry_their_columns_and_refuse_the_rest() -> None:
    provider = ProviderWallRow(
        provider="anthropic", cooling_until=200, source="vendor_reset", observed_at=100
    )
    assert (provider.provider, provider.cooling_until) == ("anthropic", 200)
    account = AccountWallRow(
        account_alias="nott-primary",
        cooling_until=200,
        source="default_1h",
        limit_type="five_hour",
        observed_at=100,
    )
    assert account.limit_type == "five_hour"
    with pytest.raises(ValueError, match="provider"):
        ProviderWallRow(
            provider="", cooling_until=1, source="vendor_reset", observed_at=0
        )
    with pytest.raises(ValueError, match="must not be negative"):
        ProviderWallRow(
            provider="p", cooling_until=-1, source="vendor_reset", observed_at=0
        )
    with pytest.raises(ValueError, match="must not be negative"):
        ProviderWallRow(
            provider="p", cooling_until=1, source="vendor_reset", observed_at=-1
        )
    with pytest.raises(ValueError, match="source"):
        AccountWallRow(
            account_alias="a",
            cooling_until=1,
            source="",
            limit_type="t",
            observed_at=0,
        )
    with pytest.raises(ValueError, match="limit_type"):
        AccountWallRow(
            account_alias="a",
            cooling_until=1,
            source="vendor_reset",
            limit_type="",
            observed_at=0,
        )
    with pytest.raises(ValueError, match="account_alias"):
        AccountWallRow(
            account_alias="",
            cooling_until=1,
            source="vendor_reset",
            limit_type="t",
            observed_at=0,
        )
    with pytest.raises(ValueError, match="must not be negative"):
        AccountWallRow(
            account_alias="a",
            cooling_until=-1,
            source="vendor_reset",
            limit_type="t",
            observed_at=0,
        )
    with pytest.raises(ValueError, match="must not be negative"):
        AccountWallRow(
            account_alias="a",
            cooling_until=1,
            source="vendor_reset",
            limit_type="t",
            observed_at=-1,
        )
