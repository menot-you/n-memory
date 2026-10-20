#!/usr/bin/env python3
"""System1 — the System One client the HOOKS use, and the CLI the routes call.

WHY IT DOES NOT REUSE `nspawn.system1`. The hooks run in the owner's client, outside this
repository's `uv` workspace and often on another machine; importing `mcps/nspawn`
would tie the law to the workspace. They are two callers of the SAME HTTP protocol, and
`plugin/tests/test_system1_wire.py` fails if the two ends diverge — the duplication
is deliberate and watched, not forgotten.

FAIL-OPEN, ALWAYS. A hook that depends on the network cannot hold the owner's session: with no
credential, no network, or past the deadline, this exits silently with code 3 and the caller
goes on as if the System1 did not exist. A judgement that never arrived is never «the answer is no».

USAGE
    echo '<state>' | system1.py noul   '{"id": "the question"}'
    echo '<state>' | system1.py choice '{"id": {"instructions": "...", "criteria": {...}}}'
    echo '<state>' | system1.py score  '{"id": {"instructions": "...", "criteria": [...]}}'

Emits JSON on stdout: {"id": {"type", "value", "probabilities"?, "confidence"?, "legend"?}}
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any


def _renamed_table() -> ModuleType:
    """The hooks' renamed-variable table, beside this file. Loaded from its file, not by
    a bare `import renamed`, which would take whichever table another loader put first
    on the import path."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "renamed.py")
    spec = importlib.util.spec_from_file_location("nott_hooks_renamed", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


advisory: Callable[[str], str | None] = _renamed_table().advisory


def endpoint() -> str:
    """The environment's endpoint, else `[system1].endpoint` of ~/.nott/spawn.toml, in nSPAWN's
    order (`nspawn.system1.resolve_endpoint`): CLAUDE.md gives the endpoint that one home and
    the key the environment. No default: the service is self-hosted, and an unset one asks
    nobody. Without `tomllib` (Python before 3.11; the Macs run 3.9) only the environment
    counts. The suggester passes it to `ask`; drift and the CLI stay on the environment, so
    the roster does not turn drift on."""
    named = advisory("NOTT_SYSTEM1_ENDPOINT")
    if named:
        return named
    try:
        import tomllib

        document = tomllib.loads((Path.home() / ".nott/spawn.toml").read_text())
    except ImportError, OSError, RuntimeError, ValueError:
        return ""
    # nSPAWN's rule (owner_roster.py): the old table would lose its endpoint in silence.
    if "jev" in document:
        raise LookupError("[jev] was renamed to [system1]; rename the table")
    table = document.get("system1")
    value = table.get("endpoint") if isinstance(table, dict) else None
    return value if isinstance(value, str) else ""


MODEL = advisory("NOTT_SYSTEM1_MODEL") or "laya-nott"

SCOPE = "SUGGEST"


def _own_route() -> tuple[str, str, str] | None:
    """(endpoint, model, key) of the SUGGEST route, or None when it is not set.

    The three go together, as in `nott_system1.route`: a partial route is refused by
    the names it lacks, never completed with the base key."""
    names = [
        f"NOTT_SYSTEM1_{SCOPE}_{part}" for part in ("ENDPOINT", "MODEL", "API_KEY")
    ]
    values = [(advisory(name) or "").strip() for name in names]
    if not any(values):
        return None
    missing = [name for name, value in zip(names, values) if not value]
    if missing:
        raise LookupError(
            f"{', '.join(missing)} not set while the other {SCOPE} route variables are"
        )
    return values[0], values[1], values[2]


def route() -> tuple[str, str, str]:
    """(endpoint, model, key) the hooks ask with: the SUGGEST route, else the base one."""
    own = _own_route()
    if own is not None:
        return own
    return endpoint(), MODEL, (advisory("NOTT_SYSTEM1_API_KEY") or "").strip()


#: A hook has to give the turn back to the owner. The ceiling is short on purpose: better
#: to have no judgement at all than to make the owner wait for the network.
TIMEOUT = float(advisory("NOTT_SYSTEM1_TIMEOUT") or "8")
MAX_QUESTIONS = 64


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the bearer header at the configured endpoint."""

    def redirect_request(
        self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def ask(
    kind: str,
    state: str,
    questions: dict[str, str | dict[str, object]],
    *,
    timeout: float | None = None,
    url: str | None = None,
) -> dict[str, dict[str, object]]:
    own = _own_route()
    if own is not None:
        url = own[0] if url is None else url
        model, chave = own[1], own[2]
    else:
        model = MODEL
        chave = (advisory("NOTT_SYSTEM1_API_KEY") or "").strip()
        url = (advisory("NOTT_SYSTEM1_ENDPOINT") or "") if url is None else url
    if not chave or not url or not questions or len(questions) > MAX_QUESTIONS:
        raise LookupError(
            "no endpoint, no credential, no questions, or a batch above the ceiling"
        )
    corpo = json.dumps(
        {
            "state": state,
            "model": model,
            "questions": {
                i: (
                    {"type": kind, "instructions": q}
                    if isinstance(q, str)
                    else {
                        "type": kind,
                        "instructions": q["instructions"],
                        **(
                            {"criteria": q["criteria"]}
                            if q.get("criteria") is not None
                            else {}
                        ),
                    }
                )
                for i, q in questions.items()
            },
        }
    ).encode()
    pedido = urllib.request.Request(
        url,
        data=corpo,
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {chave}",
        },
    )
    try:
        with _OPENER.open(pedido, timeout=timeout or TIMEOUT) as r:
            dados = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        # NEVER the error body: it can quote the request, and the request carries the `state`,
        # which is the owner's content. Only the failure type.
        raise LookupError(type(e).__name__) from None
    return {
        i: {
            k: v
            for k, v in (
                ("type", b.get("type")),
                ("value", b.get(b.get("type", ""))),
                ("probabilities", b.get("probabilities")),
                ("confidence", b.get("confidence")),
                ("legend", b.get("legend")),
            )
            if v is not None
        }
        for i, b in (dados.get("answers") or {}).items()
    }


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("noul", "choice", "score"):
        print(__doc__ or "", file=sys.stderr)
        return 2
    try:
        out = ask(sys.argv[1], sys.stdin.read(), json.loads(sys.argv[2]))
    except LookupError as e:
        print(f"system1 indisponivel: {e}", file=sys.stderr)
        return 3  # UNAVAILABLE, not «the answer is no»
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
