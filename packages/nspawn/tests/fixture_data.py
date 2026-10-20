"""Generate credential shapes from deterministic public test data."""

import hashlib
import json
from pathlib import Path


def synthetic_token(label: str) -> str:
    """Return a token shape. The label is public fixture data."""
    return "sk-ant-oat01-" + hashlib.sha256(f"public-test:{label}".encode()).hexdigest()


# These pins identify the immutable recording and its reviewed public derivative.
WIRE_DIGESTS = {
    "claude-stream-wire-2.1.250.ndjson": (
        "059e8714d544eca8141fbfbeb81d29b7a6416bc1c3b8f33249de390056b10a71",
        "6f08d7ae314cbfa1b5243e8507648e73cb13e1f106eb502a31623e8338731607",
    ),
    "claude-stream-wire-2.1.251.ndjson": (
        "4c5d48f32850de6294f8470ad9e51c483b9f4e815df4c067cad899abc8c6e35d",
        "b9b0793c8ab2a51d87b2242166eafeeca6c8a6a74cfd17b041dfc27a386d8465",
    ),
    "codex-appserver-wire-0.153.4.ndjson": (
        "695fa2a524ee24535fef74a23652228ac2a82d6915e5d547f5c0e07e961c7943",
        "933da13750439e41d318c3b24ed17c6e87f9d2d6e0d302e016945d4862905625",
    ),
}


def wire_fixture(name: str, directory: Path | None = None) -> Path:
    """Select a recording or its declared public derivative. Check the exact pins."""
    directory = Path(__file__).parent / "fixtures" if directory is None else directory
    source_digest, public_digest = WIRE_DIGESTS[name]
    source = directory / name
    public = directory / name.replace(".ndjson", ".public.ndjson")
    if source.exists() and public.exists():
        raise ValueError("wire fixture has ambiguous source and public forms")
    selected = source if source.exists() else public
    expected = source_digest if selected == source else public_digest
    if hashlib.sha256(selected.read_bytes()).hexdigest() != expected:
        raise ValueError("wire fixture digest does not match its pinned form")
    if selected == public:
        metadata = json.loads(public.with_suffix(".projection.json").read_text())
        if (
            metadata.get("schema") != 1
            or metadata.get("procedure") != "nspawn-wire-public-v1"
            or metadata.get("source") != {"name": name, "sha256": source_digest}
            or metadata.get("derivative")
            != {"name": public.name, "sha256": public_digest}
        ):
            raise ValueError("wire fixture provenance does not match its pinned digest")
    return selected
