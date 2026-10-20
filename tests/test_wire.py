"""The stdio byte meter and the cursor codec the paged surfaces share."""

from nmemory.wire import decode_cursor, encode_cursor, wire_bytes


def test_wire_bytes_counts_utf8_and_falls_back_to_ascii_on_a_lone_surrogate() -> None:
    assert wire_bytes("é") == len('"é"'.encode())
    assert wire_bytes("\ud800") == len('"\\ud800"')


def test_cursor_round_trips_and_refuses_what_it_could_not_have_issued() -> None:
    cursor = encode_cursor(["tag", "ç", 2**63 - 1])
    assert decode_cursor(cursor, 4096) == ["tag", "ç", 2**63 - 1]
    assert decode_cursor(cursor, len(cursor) - 1) is None
    for forged in ("", "!!!", "é", "W1tbW1"):
        assert decode_cursor(forged, 4096) is None
