"Tests for tombstone error redaction and opacity."

from __future__ import annotations

from storefile import sample_capsule_id

from nmemory.repositories.capsules import Tombstoned


def test_tombstoned_error_redaction() -> None:
    sensitive_id = sample_capsule_id(9999) + "-super-secret-slug"
    err = Tombstoned(sensitive_id)
    msg = str(err)
    # The message must be opaque and not expose the capsule id or internal details
    assert sensitive_id not in msg
    assert "super-secret" not in msg
    assert "tombstoned" in msg


def test_tombstoned_error_message_safe() -> None:
    err = Tombstoned("cap-1")
    assert (
        str(err)
        == "capsule is tombstoned; the content is gone, only the marker remains"
    )
