"""`nmemory embedding show`: the embedding index's status."""

from __future__ import annotations

import json
import sys

import nott_database
from nott_database import DatabaseError

from .. import NMemoryError
from ..queries.embedding import embedding_status
from .common import open_store_from_args, parse_command


def cmd_embedding_show(argv: list[str]) -> None:
    args = parse_command("embedding", "show", argv)
    store = open_store_from_args(args)

    try:
        status = embedding_status(store.db)
    except nott_database.RefusedError:
        raise
    except (NMemoryError, DatabaseError, nott_database.Error) as e:
        msg = " ".join(str(e).splitlines())
        print(f"nmemory: embedding show: {msg}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        print(json.dumps(status.to_dict()))
    else:
        line = f"{status.active_space} · {status.phase} · {status.active_vectors} active vectors · {status.pending} pending"
        if status.exhausted:
            line += f" · {status.exhausted} exhausted"
        if status.last_error is not None:
            line += f" · last error {status.last_error}"
        print(line)
