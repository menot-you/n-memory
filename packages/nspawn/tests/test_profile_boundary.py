"""Actual file and descriptor controls, with private inputs and no native query."""

import fcntl
import hashlib
import os
from pathlib import Path

import pytest
from test_admission import profile

from nspawn.admission import AdmissionRefusal, PrelaunchProfileV1
from nspawn.route import profile_read, profile_seal


def staged(tmp_path: Path) -> tuple[Path, Path, PrelaunchProfileV1]:
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    path = root / "prelaunch.json"
    prepared = PrelaunchProfileV1(profile())
    path.write_bytes(prepared.encoded)
    path.chmod(0o600)
    return root, path, prepared


def test_verified_profile_is_sealed_read_only_and_retains_the_verified_bytes(
    tmp_path: Path,
) -> None:
    root, path, expected = staged(tmp_path)
    received = profile_read(path, expected.digest, grant_root=root)
    path.write_bytes(b"changed after the verified read")
    with profile_seal(received) as descriptor:
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        assert os.get_inheritable(descriptor)
        with pytest.raises(OSError):
            os.write(descriptor, b"mutation")
        assert os.read(descriptor, len(expected.encoded) + 1) == expected.encoded
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert (
            "sha256:"
            + hashlib.sha256(os.read(descriptor, len(expected.encoded))).hexdigest()
            == expected.digest
        )
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize(
    "fault",
    [
        "world_readable",
        "linked_file",
        "linked_parent",
        "linked_root",
        "outside",
        "hard_link",
        "noncanonical",
        "digest",
    ],
)
def test_uncontrolled_profile_input_is_refused(tmp_path: Path, fault: str) -> None:
    root, path, expected = staged(tmp_path)
    digest = expected.digest
    if fault == "world_readable":
        path.chmod(0o644)
    elif fault == "linked_file":
        linked = root / "linked.json"
        linked.symlink_to(path)
        path = linked
    elif fault == "linked_parent":
        directory = root / "linked"
        directory.symlink_to(root, target_is_directory=True)
        path = directory / path.name
    elif fault == "linked_root":
        linked = tmp_path / "linked-root"
        linked.symlink_to(root, target_is_directory=True)
        root = linked
        path = linked / path.name
    elif fault == "outside":
        outside = tmp_path / "outside.json"
        outside.write_bytes(expected.encoded)
        outside.chmod(0o600)
        path = outside
    elif fault == "hard_link":
        os.link(path, root / "another.json")
    elif fault == "noncanonical":
        encoded = expected.encoded + b"\n"
        path.write_bytes(encoded)
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    else:
        digest = "sha256:" + "0" * 64
    with pytest.raises(AdmissionRefusal):
        profile_read(path, digest, grant_root=root)


def test_private_nested_grant_path_is_read_without_following_links(
    tmp_path: Path,
) -> None:
    root, path, expected = staged(tmp_path)
    child = root / "activation"
    child.mkdir(mode=0o700)
    nested = child / path.name
    path.rename(nested)
    assert profile_read(nested, expected.digest, grant_root=root) == expected


def test_consumer_io_failure_keeps_its_operational_class() -> None:
    error = OSError("consumer pipe failed after sealing")
    with (
        pytest.raises(OSError) as observed,
        profile_seal(PrelaunchProfileV1(profile())),
    ):
        raise error
    assert observed.value is error
