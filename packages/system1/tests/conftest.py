"""The suite never inherits a System1 variable or a renamed variable's old spelling from the host."""

import os

import pytest

from nott_system1.renamed import RENAMED, RETIRED


@pytest.fixture(autouse=True)
def _no_old_spelling(monkeypatch: pytest.MonkeyPatch) -> None:
    # A host that still exports an old spelling makes every read refuse by name; the
    # suite measures the transport, not the host's migration. A host that exports a
    # NOTT_SYSTEM1_ name wins over the private file the file-route cases write. A case
    # that needs either sets it itself.
    inherited = [name for name in os.environ if name.startswith("NOTT_SYSTEM1_")]
    for name in (*RENAMED.values(), *RETIRED, *inherited):
        monkeypatch.delenv(name, raising=False)
