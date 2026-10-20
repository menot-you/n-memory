"""Layer 1 parity for the `nERR-NNNN` catalog.

`oracle_error_catalog.json` is a fixture of the canonical error definitions,
ordered by their source enumeration. The tests below assert the module against
that fixture, never against itself.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from nspawn import errors

FIXTURE = Path(__file__).parent / "oracle_error_catalog.json"


@pytest.fixture(scope="module")
def oracle() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


#: Lines where the port PURPOSELY DIVERGES from the frozen oracle, by owner
#: order (2026-08-28/29): every env var invented by this repository uses prefix
#: NOTT_. Each entry carries oracle and port texts; the test below
#: rejects a divergence that ceased to exist.
ENV_LAW_DIVERGENCES: dict[int, dict[str, tuple[str, str]]] = {
    1104: {
        "description": (
            "HOME is not set",
            "spawn.toml is absent and NOTT_NSPAWN_ROSTER's path does not exist",
        ),
        "action": (
            "export HOME in the launcher",
            "declare [[provider]] in ~/.nott/spawn.toml, or export NOTT_NSPAWN_ROSTER for compatibility",
        ),
    },
}

#: The port-only rows, frozen as literals rather than derived from the
#: production objects they check.
PORT_ONLY_DIVERGENCES: dict[int, tuple[str, str, str]] = {
    1129: (
        "class-less run outside its fast lane",
        "a run without a class asked for another model or effort than its route's fast lane",
        "pin the route's fast model at its lowest effort, or declare the class this work is",
    ),
    1128: (
        "session read failed",
        "the vendor session reader did not complete",
        "check the named reason and try the session read again",
    ),
    1124: (
        "effort not in account's declared ladder",
        "the chosen account declares an effort outside the efforts it offers",
        "pick an effort the account offers, or add the effort to the roster",
    ),
    1125: (
        "provider strength above the class cap",
        "the route this class would take is stronger than the class allows",
        "declare the class this work is, or raise max_strength in spawn.toml [classes]",
    ),
    1126: (
        "native plugin grant refused",
        "the vendor's advertised plugin skills are not the admitted plugin version",
        "reinstall the plugin so the vendor's installed copy is the admitted one; waiting changes nothing",
    ),
    1127: (
        "agy cage refused",
        "a boundary of the OS cage around the AGY process cannot be established or failed its self-test",
        "read the named boundary; the AGY route stays refused until the cage self-test holds",
    ),
}


def test_fixture_metadata_is_valid(oracle: dict[str, Any]) -> None:
    meta = oracle["oracle_meta"]
    assert meta["module"] == "src/error.rs"
    assert errors.UNKNOWN_CODE == meta["unknown_code"]


def test_every_catalog_row_matches_the_oracle(oracle: dict[str, Any]) -> None:
    # Compare every donor row byte-for-byte; the one port-only code has its own
    # literal contract below.
    ported_oracle_rows = tuple(
        row for row in errors.ALL if row.code not in PORT_ONLY_DIVERGENCES
    )
    assert len(ported_oracle_rows) == len(oracle["entries"])
    for ported, expected in zip(ported_oracle_rows, oracle["entries"], strict=True):
        assert ported.code == expected["code"]
        allowed = ENV_LAW_DIVERGENCES.get(ported.code, {})
        for field in ("description", "meaning", "action"):
            if field in allowed:
                from_oracle, from_port = allowed[field]
                assert expected[field] == from_oracle
                assert getattr(ported, field) == from_port
            else:
                assert getattr(ported, field) == expected[field]


def test_no_recorded_divergence_has_gone_stale(oracle: dict[str, Any]) -> None:
    # The table is a closed allowlist: a new divergence or one that ceased
    # to exist would cause next read to trust an unauthorized deviation.
    assert {code: set(fields) for code, fields in ENV_LAW_DIVERGENCES.items()} == {
        1104: {"description", "action"},
    }
    by_code = {entry["code"]: entry for entry in oracle["entries"]}
    for code, fields in ENV_LAW_DIVERGENCES.items():
        assert code in by_code, f"NERR_{code} does not exist in the oracle"
        ported = next(row for row in errors.ALL if row.code == code)
        for field, (from_oracle, from_port) in fields.items():
            assert from_oracle != from_port, (
                f"NERR_{code}.{field}: the two texts are equal, there is no divergence"
            )
            assert getattr(ported, field) == from_port
            assert by_code[code][field] == from_oracle


def test_no_recorded_port_only_row_has_gone_stale() -> None:
    assert set(PORT_ONLY_DIVERGENCES) == {1124, 1125, 1126, 1127, 1128, 1129}
    by_code = {entry.code: entry for entry in errors.ALL}
    for code, expected in PORT_ONLY_DIVERGENCES.items():
        assert code in by_code, f"NERR_{code} missing from errors.ALL"
        actual = by_code[code]
        assert (actual.description, actual.meaning, actual.action) == expected


def test_the_unknown_sentinel_matches_the_oracle(oracle: dict[str, Any]) -> None:
    expected = oracle["unknown"]
    assert errors.UNKNOWN.code == expected["code"]
    assert errors.UNKNOWN.description == expected["description"]
    assert errors.UNKNOWN.meaning == expected["meaning"]
    assert errors.UNKNOWN.action == expected["action"]


def test_codes_are_unique_and_the_catalog_stays_ordered() -> None:
    codes = [entry.code for entry in errors.ALL]
    assert len(set(codes)) == len(codes)
    assert codes == sorted(codes)


def test_wire_text_carries_the_code_contract() -> None:
    shape = re.compile(r"^nERR-\d{4}::.+ :: .+ :: .+$")
    for entry in errors.ALL:
        assert shape.match(entry.wire()), entry.wire()
    assert errors.NERR_1101.wire().startswith(
        "nERR-1101::no <vendor> binary on this host :: "
    )
    assert errors.NERR_1101.code_str() == "1101"


def test_from_code_falls_back_to_the_sentinel_rather_than_raising() -> None:
    assert errors.from_code(1101) is errors.NERR_1101
    assert errors.from_code(4103).code == 4103
    assert errors.from_code(1) is errors.UNKNOWN
    assert errors.from_code(-7) is errors.UNKNOWN


def test_classify_keeps_the_oracle_rule_order() -> None:
    # "no codex binary" satisfies rule 1; nothing earlier can claim it.
    assert errors.classify("no codex binary on this host") is errors.NERR_1101
    # A reason matching BOTH rule 1 and rule 2 must resolve to rule 1, which is
    # the whole reason the order is preserved rather than sorted.
    both = "no claude binary: version check failed"
    assert errors.classify(both) is errors.NERR_1101
    # The broad "spawn " rule is last, so a guardian reason wins over it.
    assert errors.classify("spawn guardian saw no clean receipt") is errors.NERR_2005
    assert errors.classify("spawn refused") is errors.NERR_2002
    assert errors.classify("something nobody catalogued") is errors.UNKNOWN


def test_the_root_error_carries_its_catalog_entry() -> None:
    raised = errors.NSpawnError(errors.NERR_1103, "line 4, column 2")
    assert raised.entry is errors.NERR_1103
    assert "nERR-1103::" in str(raised)
    assert "[line 4, column 2]" in str(raised)
    assert isinstance(raised, Exception)


def test_nerr_1104_names_the_source_that_WINS_before_the_fallback() -> None:
    """The refusal must direct the reader to the winning knob, in that order.

    This lane demoted `NOTT_NSPAWN_ROSTER` to fallback: winner is
    `[store].roster` in `~/.nott/spawn.toml`. Text that only names the
    variable tells reader to tweak the losing knob — and refusal persists,
    because the file decides. `description` and `action` are the ONLY
    thing a cold reader receives here.
    """
    entry = errors.NERR_1104
    for field in (entry.description, entry.action):
        assert "spawn.toml" in field, field
        assert "NOTT_NSPAWN_ROSTER" in field, field
        assert field.index("spawn.toml") < field.index("NOTT_NSPAWN_ROSTER"), field


def test_nerr_1104_path_formatting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import supervisor
    from nspawn.errors import NERR_1104, NSpawnError

    missing_path = tmp_path / "nonexistent" / "spawn.toml"
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(missing_path))
    monkeypatch.setattr(supervisor.config, "resolve_roster_path", lambda: None)

    with pytest.raises(NSpawnError) as excinfo:
        supervisor._read_roster(str(tmp_path))

    assert excinfo.value.entry is NERR_1104
    err_str = str(excinfo.value)
    assert str(missing_path) in err_str
    assert "NOTT_NSPAWN_ROSTER" in err_str
