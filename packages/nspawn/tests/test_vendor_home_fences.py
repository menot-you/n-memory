"""Every refusal branch of the credential read fence is reachable and typed.

Each test below fails a syscall the way the kernel would and asserts the typed
VendorHomeError that names the operation. Nothing here touches a real home.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nspawn import vendor_home
from nspawn.vendor_home import VendorHomeError


def _private_dir(tmp_path: Path) -> Path:
    root = tmp_path / "home"
    root.mkdir()
    os.chmod(root, 0o700)
    return root


def _auth(root: Path, payload: bytes = b"secret-bytes") -> Path:
    source = root / "auth.json"
    source.write_bytes(payload)
    os.chmod(source, 0o600)
    return source


def _failing(exc: OSError):
    def _raise(*args: object, **kwargs: object) -> None:
        raise exc

    return _raise


def test_read_refuses_when_the_first_inspection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _auth(_private_dir(tmp_path))
    monkeypatch.setattr(os, "fstat", _failing(OSError(5, "io")))
    with pytest.raises(VendorHomeError, match="inspect auth source failed"):
        vendor_home.read_owner_credential(source)


def test_read_refuses_when_the_reinspection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _auth(_private_dir(tmp_path))
    real_fstat = os.fstat
    calls = {"n": 0}

    def second_call_fails(fd: int) -> os.stat_result:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(5, "io")
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", second_call_fails)
    with pytest.raises(VendorHomeError, match="reinspect auth source failed"):
        vendor_home.read_owner_credential(source)


def test_read_refuses_a_file_that_changed_while_it_was_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _auth(_private_dir(tmp_path))
    real_fstat = os.fstat
    calls = {"n": 0}

    def mutated_on_reinspection(fd: int) -> os.stat_result:
        calls["n"] += 1
        result = real_fstat(fd)
        if calls["n"] == 2:
            values = list(result)
            values[stat_index("st_mtime")] = values[stat_index("st_mtime")] + 1
            return os.stat_result(values)
        return result

    def stat_index(name: str) -> int:
        return {"st_mtime": 8}[name]

    monkeypatch.setattr(os, "fstat", mutated_on_reinspection)
    with pytest.raises(VendorHomeError, match="changed while it was being copied"):
        vendor_home.read_owner_credential(source)


def test_read_stops_at_the_byte_bound_and_refuses_a_growing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _auth(_private_dir(tmp_path), b"x" * vendor_home.AUTH_MAX_BYTES)
    real_read = os.read
    reads = 0

    def read_while_growing(fd: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        assert reads <= 2, "a growing auth file must not cause an unbounded read"
        with source.open("ab") as writer:
            writer.write(b"x" * vendor_home.AUTH_MAX_BYTES)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", read_while_growing)
    with pytest.raises(VendorHomeError, match="changed while it was being copied"):
        vendor_home.read_owner_credential(source)
    assert reads == 2
