"""Suite fences against the owner's LIVE state: configuration and keys.

The store needs no fence here: the `nott_database` pytest plugin gives every test
its own `NOTT_DATABASE_DIR`, and `nott_database` refuses the live store directory
under pytest.

`NOTT_NMEMORY_CONFIG` points to a path that does NOT exist, so the owner's
`~/.nott/memory.toml` (System1 endpoint, github-sync fence) never reaches a test. A
non-existent path is the only correct way to say "no file" — `/dev/null` exists
and parses as an empty document.
"""

from __future__ import annotations

import os
import resource
import shutil
import tempfile
from collections.abc import Iterator

import pytest
from nott_system1 import SCOPES, route_variables
from nott_system1.renamed import RENAMED as SYSTEM1_RENAMED
from nott_system1.renamed import RETIRED as SYSTEM1_RETIRED

from nmemory.renamed import RENAMED

#: The host's production identity: the live store's keys, vault and scope, and the
#: session a live client runs under. A box that serves nMEMORY exports them in its login
#: shell, and a test run from that shell inherited them — it, and every child it spawns
#: (`nmemory mcp serve` over stdio). A case that needs one sets it itself.
HOST_IDENTITY = (
    "NOTT_NMEMORY_HMAC_KEY",
    "NOTT_NMEMORY_HMAC_KEY_FILE",
    "NOTT_NMEMORY_PROJECT",
    "NOTT_NMEMORY_HARNESS",
    "NOTT_NMEMORY_MODEL",
    "NOTT_NMEMORY_SESSION_ID",
    *(name for scope in (None, *sorted(SCOPES)) for name in route_variables(scope)),
)


#: The address space a mutmut worker for this member gets. mutmut bounds a mutant's CPU time
#: and nothing else, and a mutant that turns a bounded loop into an unbounded allocation grew
#: #832's workers to 12 GiB each on SERVER, until the run was killed for the host's memory.
#: Past this bound the mutant fails with MemoryError, which mutmut counts as killed. A worker
#: starts as a fork of mutmut's parent (2.5 GiB measured) and the others peaked under 5 GiB.
MUTANT_ADDRESS_SPACE = 6 * 2**30


def pytest_configure(config: pytest.Config) -> None:
    # Only a worker: its MUTANT_UNDER_TEST names a mutant. mutmut's own phases run the whole
    # suite in the parent every worker is forked from.
    if "__mutmut_" not in os.environ.get("MUTANT_UNDER_TEST", ""):
        return
    # A finite hard limit bounds the soft one, so under a hard limit below the bound there is
    # nothing to lower, and the call never asks for more than the hard limit allows.
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    if soft == resource.RLIM_INFINITY or soft > MUTANT_ADDRESS_SPACE:
        resource.setrlimit(resource.RLIMIT_AS, (MUTANT_ADDRESS_SPACE, hard))


@pytest.fixture(autouse=True, scope="session")
def _fence_production_config() -> Iterator[None]:
    previous = {
        name: os.environ.get(name)
        for name in (
            "NOTT_NMEMORY_CONFIG",
            "NOTT_NMEMORY_SYSTEM1",
            "NOTT_SYSTEM1_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
        )
    }
    # SYSTEM1 IS THE FOURTH. With `NOTT_NMEMORY_SYSTEM1=1` and credentials in owner's `.env`,
    # any test running `cycle()` without flipping the switch PAID for real calls
    # to the external service — measured on 2026-09-18: `test_budget_exhausted_means_
    # zero_calls` turned red because LIVE System1 answered `none` to fixtures
    # and the new filter removed them from the cycle. A test enables System1 with `_system1_ligado`
    # (patch of `is_configured`), never by environment inheritance.
    os.environ.pop("NOTT_NMEMORY_SYSTEM1", None)
    os.environ.pop("NOTT_SYSTEM1_API_KEY", None)
    # OPENAI_API_KEY IS THE FIFTH, and enters for the SAME reason as System1 above.
    # Measured on 2026-09-18 running `tests/`: each `nmemory_capsule_retrieve` in the suite
    # opened the vector lane, read the key from environment (`embedding.py:195`), and
    # hit `api.openai.com/v1/embeddings` — `HTTP 429` in the log across 998
    # tests. The `429` is rate limiting, NOT revocation: `GET /v1/models` with the
    # same key answered `200`. The owner disabled the key for QUOTA and the
    # suite kept calling. There were six scattered `monkeypatch.delenv`:
    # they prove the problem and don't solve it, because they only cover the test that
    # remembered. Whoever wants embeddings injects `custom_hook` or passes `api_key=`,
    # and neither path goes through the environment.
    os.environ.pop("OPENAI_API_KEY", None)
    # GEMINI_API_KEY IS THE SIXTH, and enters through the gap left by the FIFTH.
    # The fence named ONE provider because there was only one; when the default became
    # Gemini (2026-09-21), `EmbeddingClient` stopped reading `OPENAI_API_KEY` and
    # started reading this one — and the fence, purporting to fence "the paid key", let
    # the entire suite reach out to `generativelanguage.googleapis.com` with the
    # owner's live credentials. The lesson is not "a variable was missing": it is that a
    # fence written with ONE provider's name lies the day there are two. If
    # `PROVIDERS` grows, this list grows in the same commit.
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ["NOTT_NMEMORY_CONFIG"] = "/nonexistent/nmemory-tests/memory.toml"
    # OLD SPELLINGS. A host that still exports a renamed variable's old name makes every
    # read refuse by name; the suite measures nMEMORY, not the host's migration. They
    # leave with the rest and come back after; a case that needs one sets it itself.
    for old in (*RENAMED.values(), *SYSTEM1_RENAMED.values(), *SYSTEM1_RETIRED):
        previous.setdefault(old, os.environ.pop(old, None))
    for name in HOST_IDENTITY:
        previous.setdefault(name, os.environ.pop(name, None))
    previous.setdefault(
        "NOTT_NSPAWN_RUN_ID", os.environ.pop("NOTT_NSPAWN_RUN_ID", None)
    )
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# THE FENCE ALSO RUNS AT IMPORT: a caller started during collection inherits the
# missing config before any fixture runs.
os.environ["NOTT_NMEMORY_CONFIG"] = "/nonexistent/nmemory-tests/memory.toml"


@pytest.fixture(autouse=True, scope="session")
def _fence_real_home() -> Iterator[None]:
    """HOME is a temporary directory for the whole suite, so `nmemory rule write` can never reach the real ~/.nott/RULES.md. The project file needs an
    explicit --root, which only a test's own temporary tree gives."""
    previous = os.environ.get("HOME")
    home = tempfile.mkdtemp(prefix="nmemory-home-")
    os.environ["HOME"] = home
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous
        shutil.rmtree(home, ignore_errors=True)
