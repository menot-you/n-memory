"""A second process for the election tests in `test_sqlite_store.py`.

`python election_child.py <role> <store file>` opens the store, starts the
product's own worker loop for `role`, prints `ready` and sleeps until killed.
The workers' model calls are replaced by no-ops: what is measured is who holds
the lock, never what the owner does with it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from nmemory import curator, system1
from nmemory.embedding import EmbeddingClient
from nmemory.embedding_index import spawn_embedding_worker
from nmemory.space import DIMENSION
from nmemory.store import Store


def main() -> None:
    role, path = sys.argv[1], Path(sys.argv[2])
    store = Store.open(path)
    if role == "embedding":
        client = EmbeddingClient(custom_hook=lambda text: [1.0] * DIMENSION)
        spawn_embedding_worker(store, client)
    elif role in ("curator", "curator-unconfigured"):
        curator.CURATOR_POLL_S = float(os.environ["NOTT_NMEMORY_CURATOR_POLL_S"])
        for name in (
            "cycle",
            "classify_cycle",
            "staged_cycle",
            "expire_staged",
            "reject_unsupported_evidence",
        ):
            setattr(curator.CuratorWorker, name, lambda self, *a, **k: None)
        if role == "curator":
            os.environ["NOTT_NMEMORY_CURATOR"] = "1"
            system1.is_configured = lambda: True  # type: ignore[assignment]
        else:
            system1.is_configured = lambda: False  # type: ignore[assignment]
        curator.spawn_curator_worker(store, root=path.parent, project="election")
    else:
        raise SystemExit(f"unknown role {role!r}")
    print("ready", flush=True)
    time.sleep(600)


if __name__ == "__main__":
    main()
