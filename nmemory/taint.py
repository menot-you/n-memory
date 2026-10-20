"""Deterministic instruction-taint scanner (campaign W1, u6e).

Pure, non-LLM detection of instruction-smuggling / persistent prompt injection.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TaintRule(StrEnum):
    IGNORE_PREVIOUS_INSTRUCTIONS = "ignore_previous_instructions"
    ROLE_PREFIX_INJECTION = "role_prefix_injection"
    NEW_AUTHORITY_FRAMING = "new_authority_framing"
    IGNORE_PREVIOUS_INSTRUCTIONS_PT = "ignore_previous_instructions_pt"
    IMPERATIVE_COMMAND_PT = "imperative_command_pt"
    EMBEDDED_TOOL_CALL = "embedded_tool_call"
    SELF_CERTIFIED_TRUST = "self_certified_trust"

    @property
    def id(self) -> str:
        return self.value


IGNORE_PREVIOUS_INSTRUCTIONS_GROUPS = [
    [
        "ignore",
        "disregard",
        "forget",
        "override",
        "bypass",
        "supersede",
    ],
    [
        "previous",
        "prior",
        "above",
        "earlier",
        "all prior",
        "preceding",
    ],
    [
        "instruction",
        "instructions",
        "rule",
        "rules",
        "prompt",
        "policy",
        "policies",
        "directive",
        "directives",
    ],
]

ROLE_PREFIX_INJECTION_GROUPS = [
    [
        "system:",
        "[system]",
        "assistant:",
        "user:",
        "sistema:",
        "[sistema]",
    ]
]

NEW_AUTHORITY_FRAMING_GROUPS = [
    [
        "new instructions",
        "from now on",
        "you must now",
        "as your new",
        "acting as",
    ]
]

IGNORE_PREVIOUS_INSTRUCTIONS_PT_GROUPS = [
    ["ignore", "desconsidere", "esqueça", "esqueca", "descarte"],
    ["anterior", "anteriores", "acima", "prévia", "previas"],
    [
        "instruç",
        "instrucoes",
        "instrucao",
        "regra",
        "regras",
        "política",
        "politica",
    ],
]

IMPERATIVE_COMMAND_PT_GROUPS = [
    ["execute", "rode", "executa"],
    ["comando", "seguinte"],
]

EMBEDDED_TOOL_CALL_GROUPS = [['"tool":', '"command":', '"tool_name":']]

SELF_CERTIFIED_TRUST_GROUPS = [
    [
        "always trust",
        "treat this as verified",
        "treat every subsequent",
        "grant full",
        "grant admin",
        "without confirmation",
        "without review",
    ]
]

RULES: list[tuple[TaintRule, list[list[str]]]] = [
    (TaintRule.IGNORE_PREVIOUS_INSTRUCTIONS, IGNORE_PREVIOUS_INSTRUCTIONS_GROUPS),
    (TaintRule.ROLE_PREFIX_INJECTION, ROLE_PREFIX_INJECTION_GROUPS),
    (TaintRule.NEW_AUTHORITY_FRAMING, NEW_AUTHORITY_FRAMING_GROUPS),
    (TaintRule.IGNORE_PREVIOUS_INSTRUCTIONS_PT, IGNORE_PREVIOUS_INSTRUCTIONS_PT_GROUPS),
    (TaintRule.IMPERATIVE_COMMAND_PT, IMPERATIVE_COMMAND_PT_GROUPS),
    (TaintRule.EMBEDDED_TOOL_CALL, EMBEDDED_TOOL_CALL_GROUPS),
    (TaintRule.SELF_CERTIFIED_TRUST, SELF_CERTIFIED_TRUST_GROUPS),
]

RULE_PROXIMITY_WINDOW = 220


@dataclass(frozen=True)
class TermMatch:
    group: int
    term: str
    span: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "term": self.term,
            "span": list(self.span),
        }


@dataclass(frozen=True)
class TaintFinding:
    rule: TaintRule
    matches: list[TermMatch]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule.value,
            "matches": [m.to_dict() for m in self.matches],
        }


class Normalized:
    def __init__(self, text: str, offsets: list[int]):
        self.text = text
        self.offsets = offsets

    def original_span(self, start: int, end: int) -> tuple[int, int]:
        s = self.offsets[start] if start < len(self.offsets) else 0
        e = self.offsets[end] if end < len(self.offsets) else s
        return (s, e)


def normalize(content: str) -> Normalized:
    norm_chars: list[str] = []
    offsets: list[int] = []

    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        offset = i

        if ch == "[":
            norm_chars.append("[")
            offsets.append(offset)
            i += 1
            while i < n and content[i].isspace():
                i += 1
            continue

        if ch.isspace():
            while i < n and content[i].isspace():
                i += 1
            if i < n and content[i] in (":", "]"):
                # drop whitespace immediately before : or ]
                pass
            else:
                norm_chars.append(" ")
                offsets.append(offset)
            continue

        lowered = ch.lower()
        for lch in lowered:
            norm_chars.append(lch)
            offsets.append(offset)
        i += 1

    offsets.append(len(content))
    return Normalized("".join(norm_chars), offsets)


def _find_occurrences(haystack: str, needle: str) -> list[int]:
    out: list[int] = []
    from_idx = 0
    while True:
        pos = haystack.find(needle, from_idx)
        if pos == -1:
            break
        out.append(pos)
        from_idx = pos + max(1, len(needle))
    return out


def _group_hits(alternatives: list[str], text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for term in alternatives:
        for start in _find_occurrences(text, term):
            hits.append((start, term))
    hits.sort(key=lambda x: x[0])
    return hits


def _finding_for(
    rule: TaintRule, groups: list[list[str]], norm: Normalized
) -> TaintFinding | None:
    if len(groups) == 1:
        alternatives = groups[0]
        for term in alternatives:
            start = norm.text.find(term)
            if start != -1:
                return TaintFinding(
                    rule=rule,
                    matches=[
                        TermMatch(
                            group=0,
                            term=term,
                            span=norm.original_span(start, start + len(term)),
                        )
                    ],
                )
        return None

    per_group: list[list[tuple[int, str]]] = [
        _group_hits(alternatives, norm.text) for alternatives in groups
    ]
    if any(len(g) == 0 for g in per_group):
        return None

    anchors = per_group[0]
    for anchor_start, anchor_term in anchors:
        chosen: list[tuple[int, str]] = [(anchor_start, anchor_term)]
        for other_hits in per_group[1:]:
            closest = min(other_hits, key=lambda x: (abs(x[0] - anchor_start), x[0]))
            chosen.append(closest)

        min_start = min(s for s, _ in chosen)
        max_end = max(s + len(t) for s, t in chosen)
        if max_end - min_start <= RULE_PROXIMITY_WINDOW:
            matches = [
                TermMatch(
                    group=grp,
                    term=term,
                    span=norm.original_span(start, start + len(term)),
                )
                for grp, (start, term) in enumerate(chosen)
            ]
            return TaintFinding(rule=rule, matches=matches)

    return None


def scan(content: str) -> list[TaintFinding]:
    """Scan content and return all fired taint findings."""
    norm = normalize(content)
    findings: list[TaintFinding] = []
    for rule, groups in RULES:
        f = _finding_for(rule, groups, norm)
        if f is not None:
            findings.append(f)
    return findings


def is_suspicious(content: str) -> bool:
    """Return True if content trips any instruction-smuggling rule."""
    return len(scan(content)) > 0
