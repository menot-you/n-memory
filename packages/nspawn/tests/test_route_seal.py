"""nspawn route start seals its launch inputs on a Python whose fcntl has no seal names.

The uv Python on SERVER (python-build-standalone 3.14.7) has no fcntl.F_ADD_SEALS, while
the kernel seals memfds (measured 2026-10-22). Each test also takes the names away, so it
holds on a Python that has them.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sys
from pathlib import Path

import pytest

from nspawn import route

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="memfd seals are Linux only"
)

NAMES = (
    "F_ADD_SEALS",
    "F_GET_SEALS",
    "F_SEAL_SEAL",
    "F_SEAL_SHRINK",
    "F_SEAL_GROW",
    "F_SEAL_WRITE",
)
ALL_SEALS = 0x000F  # F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE
F_GET_SEALS = 1034


@pytest.fixture
def no_seal_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NAMES:
        monkeypatch.delattr(fcntl, name, raising=False)


@pytest.mark.usefixtures("no_seal_names")
def test_a_descriptor_seal_holds_its_bytes_sealed() -> None:
    with route.descriptor_seal(b"launch bytes") as descriptor:
        assert os.pread(descriptor, 64, 0) == b"launch bytes"
        assert fcntl.fcntl(descriptor, F_GET_SEALS) & ALL_SEALS == ALL_SEALS


@pytest.mark.usefixtures("no_seal_names")
def test_an_identity_seal_pins_the_file_bytes_sealed(tmp_path: Path) -> None:
    path = (tmp_path / "settings.json").resolve()
    path.write_bytes(b'{"v": 1}')
    digest = "sha256:" + hashlib.sha256(b'{"v": 1}').hexdigest()
    with route.identity_seal(path, digest) as descriptor:
        path.write_bytes(b'{"v": 2}')
        assert os.pread(descriptor, 64, 0) == b'{"v": 1}'
        assert fcntl.fcntl(descriptor, F_GET_SEALS) & ALL_SEALS == ALL_SEALS
