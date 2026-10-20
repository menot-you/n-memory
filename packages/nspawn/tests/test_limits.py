"""Layer 1 parity: every ceiling is exactly specified.

The values are asserted as literals rather than recomputed from the module, so a
drift in `limits.py` fails here instead of agreeing with itself.
"""

from nspawn import limits


def test_durable_ceilings_match_the_oracle() -> None:
    assert limits.MAX_DURABLE_ROW_BYTES == 65536
    assert limits.MAX_DURABLE_IDENTIFIER_BYTES == 512
    assert limits.MAX_DURABLE_PATH_BYTES == 4096
    assert limits.MAX_LAST_ITEM_BYTES == 240
    assert limits.MAX_DURABLE_OBSERVATION_BYTES == 16384


def test_wire_ceilings_match_the_oracle() -> None:
    assert limits.MAX_FILES_PER_ITEM == 256
    assert limits.MAX_METHOD_BYTES == 256


def test_the_identifier_ceiling_is_not_the_fabricated_64() -> None:
    # The first port attempt shipped 64 here and the transplanted schema still
    # encodes it in `CHECK (octet_length(owner_instance) BETWEEN 1 AND 64)`.
    # This asserts the oracle's number so that regression cannot return quietly.
    assert limits.MAX_DURABLE_IDENTIFIER_BYTES != 64
