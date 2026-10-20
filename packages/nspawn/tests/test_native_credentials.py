"""Read existing owner homes without copying, and keep the file fences."""

import os

import pytest

from nspawn.vendor_home import VendorHomeError, read_owner_credential


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o755])
def test_private_file_in_an_owner_controlled_home_is_read_without_copy(tmp_path, mode):
    home = tmp_path / "account"
    home.mkdir()
    home.chmod(mode)
    auth = home / "auth.json"
    auth.write_bytes(b'{"token":"synthetic-native-account-credential"}')
    auth.chmod(0o600)
    before = auth.stat()
    assert read_owner_credential(auth) == auth.read_bytes()
    after = auth.stat()
    assert (
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) == (
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    assert list(home.iterdir()) == [auth]


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o777])
def test_other_writers_cannot_replace_the_credential_directory(tmp_path, mode):
    home = tmp_path / "account"
    home.mkdir()
    home.chmod(mode)
    auth = home / "auth.json"
    auth.write_bytes(b"synthetic-native-account-credential")
    auth.chmod(0o600)
    with pytest.raises(VendorHomeError):
        read_owner_credential(auth)


@pytest.mark.parametrize("fault", ["public-file", "symlink", "hardlink", "empty"])
def test_owner_home_reader_keeps_the_file_fences(tmp_path, fault):
    auth = tmp_path / "auth.json"
    auth.write_bytes(b"synthetic-native-account-credential")
    auth.chmod(0o600)
    if fault == "public-file":
        auth.chmod(0o644)
    elif fault == "symlink":
        target = tmp_path / "target"
        auth.rename(target)
        auth.symlink_to(target)
    elif fault == "hardlink":
        os.link(auth, tmp_path / "second-link")
    else:
        auth.write_bytes(b"")
    with pytest.raises(VendorHomeError):
        read_owner_credential(auth)


@pytest.mark.parametrize("fault", ["public-file", "symlink", "hardlink", "public-home"])
def test_a_permanently_unsafe_credential_refuses_on_the_first_read(
    tmp_path, monkeypatch, fault
):
    from nspawn import native

    pauses: list[float] = []
    monkeypatch.setattr(native.time, "sleep", pauses.append)
    home = tmp_path / "account"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_bytes(b"synthetic-native-account-credential")
    auth.chmod(0o600)
    if fault == "public-file":
        auth.chmod(0o644)
    elif fault == "symlink":
        target = home / "target"
        auth.rename(target)
        auth.symlink_to(target)
    elif fault == "hardlink":
        os.link(auth, home / "second-link")
    else:
        home.chmod(0o777)
    with pytest.raises(native.NativePreparationError):
        native._credential(auth)
    assert pauses == []


def test_an_empty_credential_is_read_again_through_every_pause(tmp_path, monkeypatch):
    from nspawn import native

    pauses: list[float] = []
    monkeypatch.setattr(native.time, "sleep", pauses.append)
    auth = tmp_path / "auth.json"
    auth.write_bytes(b"")
    auth.chmod(0o600)
    with pytest.raises(native.NativePreparationError):
        native._credential(auth)
    assert pauses == list(native.CREDENTIAL_RETRY_PAUSES)
