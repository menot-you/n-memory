"""The credential must not survive anywhere a surface can read it.

Every test plants a SENTINEL and then greps the produced text for it, rather
than asserting that a marker appeared: a redactor that inserts the marker and
also leaves the secret would pass the second check and fail this one.
"""

import json

from fixture_data import synthetic_token

from nspawn import redact
from nspawn.redact import REDACTION_MARKER, SecretNeedles

SENTINEL = synthetic_token("test_redact:1")
CREDENTIAL = json.dumps(
    {"sessionKey": SENTINEL, "subscriptionType": "max", "account": {"uuid": "u" * 20}}
).encode("utf-8")


def test_the_marker_cannot_reshape_a_json_document() -> None:
    assert '"' not in REDACTION_MARKER
    assert "\\" not in REDACTION_MARKER


def test_the_whole_document_is_a_needle_even_verbatim() -> None:
    needles = SecretNeedles.from_credential(CREDENTIAL)
    catted = CREDENTIAL.decode("utf-8")
    assert SENTINEL not in needles.redact(catted)


def test_one_field_read_out_of_the_document_is_redacted() -> None:
    needles = SecretNeedles.from_credential(CREDENTIAL)
    said = f"the token is {SENTINEL}, use it"
    redacted = needles.redact(said)
    assert SENTINEL not in redacted
    assert REDACTION_MARKER in redacted


def test_a_short_value_is_not_treated_as_secret_material() -> None:
    # "max" is a subscription type, not a credential. A floor that swallowed it
    # would redact the word out of every unrelated sentence.
    needles = SecretNeedles.from_credential(CREDENTIAL)
    assert needles.redact("the plan is max") == "the plan is max"


def test_the_floor_is_measured_in_bytes_not_characters() -> None:
    # 15 multi-byte characters are under the character floor and OVER the byte
    # floor; the oracle measures bytes.
    value = "é" * 15
    needles = SecretNeedles.from_credential(json.dumps({"k": value}).encode("utf-8"))
    assert value not in needles.redact(f"leak {value} here")


def test_a_non_json_credential_is_tokenized_and_the_equals_is_a_separator() -> None:
    needles = SecretNeedles.from_credential(f"ANTHROPIC_TOKEN={SENTINEL}\n".encode())
    # The child emits the VALUE, never the NAME=value pair, so the value alone
    # has to be a needle.
    assert SENTINEL not in needles.redact(f"got {SENTINEL} ok")


def test_redaction_sees_through_json_escaping() -> None:
    needles = SecretNeedles.from_credential(CREDENTIAL)
    # A re-serialized document decodes back to the same characters.
    reserialized = json.dumps({"tok": SENTINEL}, indent=4)
    decoded = json.loads(reserialized)
    assert SENTINEL not in needles.redact_value(decoded)["tok"]


def test_a_secret_in_a_json_KEY_is_redacted() -> None:
    # Where a naive redactor leaks: the payload is model-authored and nothing
    # forbids the secret from landing in the key position.
    needles = SecretNeedles.from_credential(CREDENTIAL)
    redacted = needles.redact_value(
        {SENTINEL: "value", "k": [SENTINEL, {"deep": SENTINEL}]}
    )
    assert SENTINEL not in json.dumps(redacted)
    assert REDACTION_MARKER in json.dumps(redacted)


def test_non_string_leaves_are_untouched() -> None:
    needles = SecretNeedles.from_credential(CREDENTIAL)
    assert needles.redact_value({"n": 1, "b": True, "z": None}) == {
        "n": 1,
        "b": True,
        "z": None,
    }


def test_one_secret_produces_one_marker_not_nested_ones() -> None:
    # Longest-first: the enclosing match is consumed before any substring of it.
    needles = SecretNeedles.from_credential(CREDENTIAL)
    redacted = needles.redact(CREDENTIAL.decode("utf-8"))
    assert redacted.count(REDACTION_MARKER) == 1


def test_an_empty_needle_set_redacts_nothing_and_says_so() -> None:
    empty = SecretNeedles.none()
    assert empty.is_empty()
    assert len(empty) == 0
    assert empty.redact(SENTINEL) == SENTINEL
    assert empty.redact_value({SENTINEL: SENTINEL}) == {SENTINEL: SENTINEL}


def test_the_needle_set_never_prints_its_secrets() -> None:
    needles = SecretNeedles.from_credential(CREDENTIAL)
    assert not needles.is_empty()
    for rendered in (repr(needles), str(needles), f"{needles}"):
        assert SENTINEL not in rendered
        assert "sk-ant" not in rendered
    assert repr(needles) == f"SecretNeedles(needles={len(needles)})"


def test_the_known_boundary_is_real_and_stated() -> None:
    # A transformed secret DEFEATS exact matching. This is documented, not fixed
    # here, and the test exists so nobody mistakes the fence for one it is not.
    import base64

    needles = SecretNeedles.from_credential(CREDENTIAL)
    transformed = base64.b64encode(SENTINEL.encode()).decode()
    assert transformed in needles.redact(f"leak {transformed}")
    assert "boundary" in redact.__doc__.lower()
