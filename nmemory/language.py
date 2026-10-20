"""Is this capsule's prose English, and did it keep its spaces? The capture checks.

Only prose is judged: code, paths, URLs, identifiers,
hashes and markup lines are removed first, so an English sentence about
`precisa_de_humano` passes. The rule was measured on 1,819 translated capsule pairs
plus 1,327 English commit lines (none refused), the 41 English E1 gold facts
(none refused) and 16 short technical English lines a review found (none
refused), while 97.1% of the Portuguese originals are refused.
"""

from __future__ import annotations

import re
from functools import cache
from typing import Any

# Fewer prose words than this is too little text to judge: a label, a status,
# pure code.
MIN_PROSE_WORDS = 4
# The question is English or Portuguese, the two languages this store's writers
# use. Judged over all ~97 languages, short technical English was taken for
# Latin, French or Nigerian Pidgin ("Fix syntax error in configuration." read as
# Latin at 0.59); judged over these two, no English capsule, E1 fact or commit
# line read as Portuguese.
LANGUAGES = ("en", "pt")
# The detector alone still took short English for Portuguese: four or five words
# ("Enforce resource quota policies.", "Escalate to Roberto or Fernando.") are
# too few n-grams to decide. Portuguese prose carries its function words, so a
# verdict also needs two of these (none that is also an English word), not
# counting a particle between two capitalised words ("Roberto da Silva", "Rio de
# Janeiro"). Telegraphic Portuguese has none of them, and the detector is then
# near certain: no English line scored above 0.96 for Portuguese, while those
# lines score above 0.99, so that certainty alone also refuses, from eight prose
# words up (a search over five-word English found one at 0.993). Measured: no
# English refused, 883 of 910 held-out Portuguese refused. Portuguese words left
# unquoted inside English still count: quote what is cited.
# ponytail: Portuguese dense with English loanwords and no function words
# ("Deploy falhou no cluster ..." at 0.69) is captured; a Portuguese-only
# vocabulary check is the upgrade if that shows up in the store.
_FUNCTION_WORDS = (
    "de da das dos que não nao para uma uns umas com por mais como está esta isso "
    "também tambem são sao ser foi pelo pela pelos pelas sem já aos às em na nas "
    "nos mas seu sua ele ela eles elas ainda quando onde porque então entao muito "
    "cada tem têm há deve pode fica ficou"
)
FUNCTION_WORDS = frozenset(_FUNCTION_WORDS.split())
MIN_FUNCTION_WORDS = 2
CERTAIN = 0.99
MIN_CERTAIN_WORDS = 8

_STRIP = (
    re.compile(r"```.*?```", re.DOTALL),
    re.compile(r"`[^`\n]*`"),
    # A quoted title or phrase is cited, not written: "the rule titled '...'".
    re.compile(r"(?<!\w)'[^'\n]+'(?!\w)|\"[^\"\n]+\"|\u201c[^\u201d\n]+\u201d"),
    re.compile(r"https?://\S+"),
    # Anchored at a token's start: unanchored, a run without a slash was retried
    # at every offset, and 50 KB of unspaced text took 14 s.
    re.compile(r"(?<!\S)\S*[/\\]\S*"),
    re.compile(r"\b\w+(?:[._:#@-]\w+)+\b"),
    re.compile(r"\b\w*_\w*\b"),
    re.compile(r"\b[0-9a-f]{7,}\b"),
    re.compile(r"^\s*[{\[<].*$", re.MULTILINE),
)
_WORD = re.compile(r"[^\W\d_]{2,}")


# Prose that lost its spaces ("Preserve fullmeasurementevidence", Codex 2026-10)
# carries runs of 21 or more lowercase letters, longer than the English a capsule
# uses ("internationalization" has 20) and than lowercase identifiers such as
# `onanimationiteration`. Capitals and digits do not count, so
# `PYTHONDONTWRITEBYTECODE` and `20260906T024004Z95029` are not glued, and a
# camelCase name is set aside like the rest of the code. Three distinct such
# runs, one word in twenty at least, make the text glued. Measured on the store of
# 2026-10-07: no Claude Code capture (0 of 7,297), and 396 of the 2,305 Codex
# captures since 2026-09-28, 377 of the 532 under 8% whitespace.
# ponytail: glued text made of shorter runs (`blanketqualitypass`) goes unnoticed;
# a word list is the upgrade if the advisory should reach it.
GLUED_LOWERCASE = 21
MIN_GLUED_WORDS = 3
GLUED_SHARE = 0.05
_CAMEL_CASE = re.compile(r"\b\w*[a-z][A-Z]\w*\b")
_ALNUM = re.compile(r"[^\W_]+")


def _code_removed(text: str) -> str:
    for rx in _STRIP:
        text = rx.sub(" ", text)
    return text


def prose(text: str) -> str:
    """The words of `text` that are prose, with code and identifiers removed."""
    return " ".join(_WORD.findall(_code_removed(text)))


def glued_words(text: str) -> list[str] | None:
    """The distinct words of `text`'s prose that lost their spaces, if it is glued."""
    words = _ALNUM.findall(_CAMEL_CASE.sub(" ", _code_removed(text)))
    glued = list(
        dict.fromkeys(
            w for w in words if sum(c.islower() for c in w) >= GLUED_LOWERCASE
        )
    )
    if len(glued) >= MIN_GLUED_WORDS and len(glued) >= GLUED_SHARE * len(words):
        return glued
    return None


@cache
def _identifier() -> Any:
    # Imported on first use: numpy and the model cost the server nothing until
    # the first capture asks.
    from py3langid.langid import (  # type: ignore[import-untyped]
        MODEL_FILE,
        LanguageIdentifier,
    )

    identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    identifier.set_languages(list(LANGUAGES))
    return identifier


def foreign_language(text: str) -> str | None:
    """The detected language when `text`'s prose is confidently not English, else None."""
    words = prose(text).split()
    if len(words) < MIN_PROSE_WORDS:
        return None
    lang, probability = _identifier().classify(" ".join(words))
    if lang == "en":
        return None
    certain = probability >= CERTAIN and len(words) >= MIN_CERTAIN_WORDS
    if certain or _function_words(words) >= MIN_FUNCTION_WORDS:
        return str(lang)
    return None


def _function_words(words: list[str]) -> int:
    def particle(n: int) -> bool:
        return (
            0 < n < len(words) - 1
            and words[n - 1][0].isupper()
            and words[n + 1][0].isupper()
        )

    return sum(
        w.lower() in FUNCTION_WORDS and not particle(n) for n, w in enumerate(words)
    )
