"""The embedding space: which provider, model, dimension and prompt version a vector
belongs to.

A leaf: it imports the standard library and the package's base error only, so the
store, the database and the embedding client can all name the space without
importing one another. It reads `NOTT_NMEMORY_EMBED_PROVIDER` and, for a
self-hosted `openai-compatible` endpoint, `NOTT_NMEMORY_EMBED_MODEL` and
`NOTT_NMEMORY_EMBED_DIMENSION`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import NMemoryError

DIMENSION = 3072
DEFAULT_OPENAI_MODEL = "text-embedding-3-large"
DEFAULT_GEMINI_MODEL = "gemini-embedding-2"
#: Accepted providers in `NOTT_NMEMORY_EMBED_PROVIDER`. Any other value is
#: refused at the gate: a mistyped name must not silently fall back to default and
#: write vectors in the space of an unrequested provider.
#: `openai-compatible` is any self-hosted server speaking the OpenAI embeddings
#: protocol (Ollama, vLLM, llama.cpp): its endpoint and model come from the environment.
PROVIDERS = ("openai", "gemini", "openai-compatible")
DEFAULT_PROMPT_VERSION = "nmemory-retrieval-v1"


class EmbeddingError(NMemoryError):
    pass


class EmbeddingRateLimited(EmbeddingError):
    """HTTP 429 without a quota refusal: waiting can restore the endpoint."""


class EmbeddingTransportError(EmbeddingError):
    """A transport failure or HTTP 5xx: the endpoint, not the job, failed."""


class EmbeddingQuotaExhausted(EmbeddingError):
    """The provider refuses until an operator acts (balance, spending cap): waiting
    cures nothing, so the call is not retried and the vector lane reports it."""


@dataclass(frozen=True)
class SpaceFingerprint:
    provider: str
    model: str
    dimension: int
    prompt_version: str

    def as_str(self) -> str:
        return f"{self.provider}:{self.model}:{self.dimension}:{self.prompt_version}"

    @classmethod
    def default_openai(cls) -> SpaceFingerprint:
        return cls(
            provider="openai",
            model=DEFAULT_OPENAI_MODEL,
            dimension=DIMENSION,
            prompt_version=DEFAULT_PROMPT_VERSION,
        )

    @classmethod
    def default_gemini(cls) -> SpaceFingerprint:
        return cls(
            provider="gemini",
            model=DEFAULT_GEMINI_MODEL,
            dimension=DIMENSION,
            prompt_version=DEFAULT_PROMPT_VERSION,
        )

    @classmethod
    def current(cls) -> SpaceFingerprint:
        provider = os.environ.get("NOTT_NMEMORY_EMBED_PROVIDER", "gemini").strip()
        if provider not in PROVIDERS:
            raise EmbeddingError(
                f"NOTT_NMEMORY_EMBED_PROVIDER={provider!r} is not a known provider; "
                f"use one of {PROVIDERS}"
            )
        if provider == "openai-compatible":
            model = os.environ.get("NOTT_NMEMORY_EMBED_MODEL", "").strip()
            if not model:
                raise EmbeddingError(
                    "NOTT_NMEMORY_EMBED_PROVIDER=openai-compatible needs"
                    " NOTT_NMEMORY_EMBED_MODEL"
                )
            raw = os.environ.get("NOTT_NMEMORY_EMBED_DIMENSION", "").strip()
            # ASCII digits only: `str.isdigit` accepts "²", which `int` refuses. The
            # length cap keeps `int` under Python's digit limit, which raises ValueError;
            # leading zeros are dropped first, so "01024" stays 1024.
            digits = raw.lstrip("0") or "0"
            if (
                not (raw.isascii() and raw.isdigit())
                or len(digits) > len(str(DIMENSION))
                or not 0 < int(digits) <= DIMENSION
            ):
                raise EmbeddingError(
                    "NOTT_NMEMORY_EMBED_PROVIDER=openai-compatible needs"
                    f" NOTT_NMEMORY_EMBED_DIMENSION, the model's native size in"
                    f" 1..{DIMENSION}; got {raw!r}"
                )
            # Model and native size are in the fingerprint, so these vectors never mix
            # with another model's; the store keeps them zero-padded to DIMENSION.
            return cls(provider, model, int(digits), DEFAULT_PROMPT_VERSION)
        return cls.default_gemini() if provider == "gemini" else cls.default_openai()
