"""Check deterministic test credentials without credential source files."""

import re

from fixture_data import synthetic_token


def test_synthetic_token_shape_and_provenance() -> None:
    value = synthetic_token("redaction")
    assert value == synthetic_token("redaction")
    assert value != synthetic_token("other-account")
    assert re.fullmatch(r"sk-ant-oat01-[0-9a-f]{64}", value)


def test_wire_fixture_checks_the_source_digest(tmp_path) -> None:
    import shutil

    import fixture_data
    import pytest

    name = "claude-stream-wire-2.1.250.ndjson"
    fixture = fixture_data.wire_fixture(name)
    selected = tmp_path / fixture.name
    shutil.copy2(fixture, selected)
    provenance = fixture.with_suffix(".projection.json")
    if provenance.is_file():
        shutil.copy2(provenance, tmp_path)
    assert fixture_data.wire_fixture(name, tmp_path) == selected
    selected.write_bytes(selected.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="digest"):
        fixture_data.wire_fixture(name, tmp_path)


def test_wire_fixture_refuses_a_missing_recording(tmp_path) -> None:
    import fixture_data
    import pytest

    with pytest.raises(FileNotFoundError):
        fixture_data.wire_fixture("claude-stream-wire-2.1.251.ndjson", tmp_path)
