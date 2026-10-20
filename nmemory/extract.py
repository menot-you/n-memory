"""Deterministic candidate extraction from free-text blobs (w2-kinds, u-r11).

Pure, non-LLM candidate extraction adhering to S1-S4 and fixed precedence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class CandidateKind(StrEnum):
    FACT = "fact"
    PROCEDURE = "procedure"
    DECISION = "decision"
    TASK = "task"
    EPIC = "epic"
    PLAN = "plan"
    BRAINSTORM = "brainstorm"
    DOC = "doc"
    CONSTRAINT = "constraint"
    CAPABILITY = "capability"
    FAILURE_PATTERN = "failure_pattern"
    DOD = "dod"

    @classmethod
    def from_str(cls, value: str) -> CandidateKind:
        try:
            return cls(value.strip().lower())
        except ValueError:
            raise ValueError(f"unknown candidate kind: {value}") from None


@dataclass(frozen=True)
class ExtractCandidate:
    content: str
    kind: CandidateKind
    cue: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "kind": self.kind.value,
            "cue": self.cue,
        }


DECISION_WORDS = {
    "adopted",
    "adotada",
    "adotado",
    "adotamos",
    "chose",
    "chosen",
    "congelado",
    "congelada",
    "decided",
    "decidida",
    "decidido",
    "decidimos",
    "decidiu",
    "decision",
    "decisao",
    "decisão",
    "escolhemos",
    "escolhida",
    "escolhido",
    "frozen",
    "locked",
    "optamos",
    "opted",
    "ratificado",
    "ratificada",
    "ratified",
    "rejeitado",
    "rejeitada",
    "rejected",
    "settled",
    "superseded",
    "supersedes",
    "travado",
    "travada",
    "vetado",
    "vetada",
    "veto",
    "vetoed",
}

DECISION_PHRASES = [
    "we will use",
    "going with",
    "instead of",
    "vamos usar",
    "em vez de",
    "ao invés de",
    "ao inves de",
]

IMPERATIVE_OPENERS = {
    "abra",
    "add",
    "adicione",
    "always",
    "aplique",
    "apply",
    "atualize",
    "avoid",
    "build",
    "call",
    "check",
    "commit",
    "configure",
    "copy",
    "create",
    "crie",
    "delete",
    "deploy",
    "disable",
    "do",
    "don't",
    "enable",
    "ensure",
    "evite",
    "execute",
    "exija",
    "export",
    "exporte",
    "faca",
    "faça",
    "fetch",
    "garanta",
    "gere",
    "import",
    "importe",
    "inicie",
    "inject",
    "install",
    "instale",
    "jamais",
    "keep",
    "mantenha",
    "move",
    "nao",
    "não",
    "never",
    "nunca",
    "open",
    "pare",
    "pin",
    "prefer",
    "prefira",
    "pull",
    "push",
    "rebase",
    "register",
    "reinicie",
    "remova",
    "remove",
    "restart",
    "rode",
    "run",
    "sempre",
    "serialize",
    "set",
    "start",
    "stop",
    "strip",
    "suba",
    "test",
    "teste",
    "update",
    "use",
    "valide",
    "validate",
    "verifique",
    "verify",
    "wrap",
}

PROCEDURE_LABELS = [
    "how-to",
    "howto",
    "passo a passo",
    "passos",
    "procedimento",
    "procedure",
    "steps",
]

PROCEDURE_OPENERS = ["how to", "steps to", "como fazer", "passos para"]

DECLARATIVE_FRAMES = [
    " is ",
    " are ",
    " was ",
    " were ",
    " has ",
    " have ",
    " lives at ",
    " lives in ",
    " uses ",
    " requires ",
    " depends on ",
    " defaults to ",
    " runs on ",
    " contains ",
    " belongs to ",
    " means ",
    " maps to ",
    " points to ",
    " equals ",
    " = ",
    " -> ",
    " => ",
    " owns ",
    " must ",
    " é ",
    " são ",
    " foi ",
    " foram ",
    " era ",
    " eram ",
    " fica em ",
    " ficam em ",
    " usa ",
    " usam ",
    " tem ",
    " têm ",
    " requer ",
    " requerem ",
    " depende de ",
    " dependem de ",
    " significa ",
    " significam ",
    " aponta para ",
    " apontam para ",
    " roda em ",
    " rodam em ",
    " pertence a ",
    " pertencem a ",
    " contém ",
    " contêm ",
    " deve ",
    " devem ",
]

PRESCRIPTIVE_WORDS = {"never", "always", "nunca", "sempre", "jamais"}

TODO_WORDS = {
    "fixme",
    "pendencia",
    "pendência",
    "pendente",
    "tarefa",
    "task",
    "todo",
}

TODO_LABELS = ["a fazer"]
TODO_CHECKBOX_CUE = "todo-checkbox:'[ ]'"

EPIC_LABELS = [
    "campaign",
    "campanha",
    "epic",
    "epico",
    "initiative",
    "iniciativa",
    "marco",
    "milestone",
    "workstream",
    "épico",
]

PLAN_LABELS = ["plan", "plano"]

IDEA_LABELS = [
    "brainstorm",
    "hipotese",
    "hipótese",
    "hypothesis",
    "idea",
    "ideia",
]

OPEN_QUESTION_OPENERS = [
    "could we",
    "should we",
    "what if",
    "why not",
    "deveriamos",
    "deveríamos",
    "e se",
    "poderiamos",
    "poderíamos",
    "por que nao",
    "por que não",
    "que tal",
]

DOC_LABELS = [
    "doc",
    "docs",
    "documentacao",
    "documentation",
    "documentação",
    "guia",
    "guide",
    "manual",
    "readme",
    "reference",
    "referencia",
    "referência",
    "runbook",
]

CONSTRAINT_LABELS = ["constraint", "restricao", "restrição"]

PROHIBITION_FRAMES = [
    ("must", "not"),
    ("must", "never"),
    ("não", "pode"),
    ("nao", "pode"),
    ("não", "podem"),
    ("nao", "podem"),
    ("não", "deve"),
    ("nao", "deve"),
    ("não", "devem"),
    ("nao", "devem"),
]

CAPABILITY_LABELS = ["capability", "capacidade"]

USE_WHEN_OPENERS = ["use when", "use quando"]

FAILURE_LABELS = ["failure", "falha", "sintoma", "symptom"]

FAILURE_FRAMES = [
    ("fails", "with"),
    ("fails", "when"),
    ("breaks", "with"),
    ("breaks", "when"),
    ("falha", "com"),
    ("falham", "com"),
    ("falha", "quando"),
    ("falham", "quando"),
    ("quebra", "com"),
    ("quebram", "com"),
    ("quebra", "quando"),
    ("quebram", "quando"),
]


def extract(text: str) -> list[ExtractCandidate]:
    """Extract 0..N typed candidates from a free-text blob."""
    candidates: list[ExtractCandidate] = []
    fence: str | None = None
    for line in text.splitlines():
        trimmed = line.strip()
        if fence is not None:
            if trimmed.startswith(fence) and not trimmed.lstrip(fence[0]):
                fence = None
            continue
        if trimmed.startswith(("```", "~~~")):
            marker_count = len(trimmed) - len(trimmed.lstrip(trimmed[0]))
            fence = trimmed[:marker_count]
            continue
        if is_indented_code(line):
            continue
        for sentence in split_sentences(trimmed):
            if sentence.endswith(";"):
                sentence = sentence[:-1].rstrip()
            candidate = candidate_for_segment(sentence)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def is_indented_code(line: str) -> bool:
    lstripped = line.lstrip(" \t")
    indent_len = len(line) - len(lstripped)
    indent = line[:indent_len]
    deep = "\t" in indent or indent_len >= 4
    if not deep:
        return False
    rest = line.lstrip()
    if not rest:
        return False
    dressed = (
        rest[0] in ("-", "*", "+", ">", "#")
        or rest.startswith(("[ ]", "[x]", "[X]"))
        or rest[0].isdigit()
    )
    return not dressed


def split_sentences(line: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    n = len(line)
    i = 0
    while i < n:
        if line[i] == ";" and i + 1 < n and line[i + 1] == " ":
            piece = line[start : i + 1].strip()
            pieces.append(piece)
            start = i + 1
            i += 1
            continue

        if line[i] in (".", "!", "?") and i + 1 < n and line[i + 1] == " ":
            before = line[start:i]
            prev_words = before.split()
            prev_word = prev_words[-1] if prev_words else ""
            if len(prev_word) <= 1 or "." in prev_word:
                i += 1
                continue
            after = line[i + 1 :].lstrip()
            if not after or not after[0].isupper():
                i += 1
                continue
            piece = line[start : i + 1].strip()
            pieces.append(piece)
            start = i + 1
            i += 1
            continue
        i += 1

    tail = line[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces


def candidate_for_segment(line: str) -> ExtractCandidate | None:
    body, unchecked_checkbox = strip_markers(line)
    if len(body.split()) < 2:
        return None
    folded = body.lower()

    if body.endswith("?"):
        cue = brainstorm_cue(folded)
        if cue is not None:
            return ExtractCandidate(
                content=body, kind=CandidateKind.BRAINSTORM, cue=cue
            )
        if unchecked_checkbox:
            return ExtractCandidate(
                content=body, kind=CandidateKind.TASK, cue=TODO_CHECKBOX_CUE
            )
        return None

    # Precedence
    tcue = task_cue(folded, unchecked_checkbox)
    if tcue:
        return ExtractCandidate(content=body, kind=CandidateKind.TASK, cue=tcue)
    ecue = epic_cue(folded)
    if ecue:
        return ExtractCandidate(content=body, kind=CandidateKind.EPIC, cue=ecue)
    pcue = plan_cue(folded)
    if pcue:
        return ExtractCandidate(content=body, kind=CandidateKind.PLAN, cue=pcue)
    bcue = brainstorm_cue(folded)
    if bcue:
        return ExtractCandidate(content=body, kind=CandidateKind.BRAINSTORM, cue=bcue)
    dcue = doc_cue(folded)
    if dcue:
        return ExtractCandidate(content=body, kind=CandidateKind.DOC, cue=dcue)
    plcue = procedure_label_cue(folded)
    if plcue:
        return ExtractCandidate(content=body, kind=CandidateKind.PROCEDURE, cue=plcue)
    clcue = constraint_label_cue(folded)
    if clcue:
        return ExtractCandidate(content=body, kind=CandidateKind.CONSTRAINT, cue=clcue)
    cpcue = capability_cue(folded)
    if cpcue:
        return ExtractCandidate(content=body, kind=CandidateKind.CAPABILITY, cue=cpcue)
    flcue = failure_label_cue(folded)
    if flcue:
        return ExtractCandidate(
            content=body, kind=CandidateKind.FAILURE_PATTERN, cue=flcue
        )
    dcue = decision_cue(folded)
    if dcue:
        return ExtractCandidate(content=body, kind=CandidateKind.DECISION, cue=dcue)
    prcue = prohibition_cue(folded)
    if prcue:
        return ExtractCandidate(content=body, kind=CandidateKind.CONSTRAINT, cue=prcue)
    ffcue = failure_frame_cue(folded)
    if ffcue:
        return ExtractCandidate(
            content=body, kind=CandidateKind.FAILURE_PATTERN, cue=ffcue
        )
    prdcue = procedure_cue(folded)
    if prdcue:
        return ExtractCandidate(content=body, kind=CandidateKind.PROCEDURE, cue=prdcue)
    fcue = fact_cue(folded, body)
    if fcue:
        return ExtractCandidate(content=body, kind=CandidateKind.FACT, cue=fcue)
    return None


def strip_markers(line: str) -> tuple[str, bool]:
    rest = line.strip()
    unchecked_checkbox = False
    saw_stamp = False
    speaker_stripped = False

    while True:
        before = rest
        rest = rest.lstrip("#").lstrip()
        rest = rest.lstrip(">").lstrip()
        for marker in ("- ", "* ", "+ ", "[x] ", "[X] "):
            if rest.startswith(marker):
                rest = rest[len(marker) :].lstrip()
        if rest.startswith("[ ] "):
            rest = rest[4:].lstrip()
            unchecked_checkbox = True

        rest = strip_ordered_marker(rest)

        stamped = strip_bracket_stamp(rest)
        if stamped is not None:
            rest = stamped
            saw_stamp = True

        if saw_stamp and not speaker_stripped:
            speaker = strip_speaker_tag(rest)
            if speaker is not None:
                rest = speaker
                speaker_stripped = True

        if rest == before:
            return (rest, unchecked_checkbox)


def strip_bracket_stamp(rest: str) -> str | None:
    if not rest.startswith("["):
        return None
    close = rest.find("]")
    if close == -1 or close > 32:
        return None
    interior = rest[1:close]
    if "[" in interior or not any(c.isdigit() for c in interior):
        return None
    after = rest[close + 1 :]
    if after.startswith(" "):
        return after[1:].lstrip()
    return None


def strip_speaker_tag(rest: str) -> str | None:
    colon = rest.find(":")
    if colon == -1:
        return None
    tag = rest[:colon]
    if not tag or len(tag) > 24:
        return None
    if not all(c.isalnum() or c in "_-." for c in tag):
        return None
    if is_cue_word(tag.lower()):
        return None
    after = rest[colon + 1 :]
    if after.startswith(" "):
        return after[1:].lstrip()
    return None


def is_cue_word(folded: str) -> bool:
    return (
        folded in TODO_WORDS
        or folded in EPIC_LABELS
        or folded in PLAN_LABELS
        or folded in IDEA_LABELS
        or folded in DOC_LABELS
        or folded in PROCEDURE_LABELS
        or folded in CONSTRAINT_LABELS
        or folded in CAPABILITY_LABELS
        or folded in FAILURE_LABELS
        or folded in DECISION_WORDS
        or folded in IMPERATIVE_OPENERS
        or folded == "step"
    )


def strip_ordered_marker(rest: str) -> str:
    digits = 0
    while digits < len(rest) and rest[digits].isdigit():
        digits += 1
    if digits == 0:
        return rest
    after = rest[digits:]
    if (after.startswith((".", ")"))) and len(after) > 1 and after[1] == " ":
        return after[2:].lstrip()
    return rest


def words(folded: str) -> list[str]:
    import re

    return [w for w in re.findall(r"[a-z0-9áàâãéèêíïóôõöúçñ']+", folded) if w]


def labeled(folded: str, markers: list[str]) -> str | None:
    for marker in markers:
        if not folded.startswith(marker):
            continue
        rest = folded[len(marker) :]
        if rest.startswith(":"):
            return marker
        if rest.startswith(" "):
            spaced = rest[1:]
            if spaced.startswith((":", "— ", "- ")):
                return marker
    return None


def opener_prefix(folded: str, openers: list[str]) -> str | None:
    for opener in openers:
        if folded.startswith(opener):
            rest = folded[len(opener) :]
            if not rest or rest[0] in (" ", ",", "?"):
                return opener
    return None


def task_cue(folded: str, unchecked_checkbox: bool) -> str | None:
    if unchecked_checkbox:
        return TODO_CHECKBOX_CUE
    w_list = words(folded)
    if w_list and w_list[0] in TODO_WORDS:
        return f"todo-word:'{w_list[0]}'"
    marker = labeled(folded, TODO_LABELS)
    if marker:
        return f"todo-label:'{marker}'"
    return None


def epic_cue(folded: str) -> str | None:
    marker = labeled(folded, EPIC_LABELS)
    return f"epic-label:'{marker}'" if marker else None


def plan_cue(folded: str) -> str | None:
    marker = labeled(folded, PLAN_LABELS)
    return f"plan-label:'{marker}'" if marker else None


def brainstorm_cue(folded: str) -> str | None:
    marker = labeled(folded, IDEA_LABELS)
    if marker:
        return f"idea-label:'{marker}'"
    opener = opener_prefix(folded, OPEN_QUESTION_OPENERS)
    return f"open-question:'{opener}'" if opener else None


def doc_cue(folded: str) -> str | None:
    marker = labeled(folded, DOC_LABELS)
    return f"doc-label:'{marker}'" if marker else None


def word_pair_cue(folded: str, pairs: list[tuple[str, str]]) -> str | None:
    tokens = words(folded)
    for a, b in pairs:
        for i in range(len(tokens) - 1):
            if tokens[i] == a and tokens[i + 1] == b:
                return f"{a} {b}"
    return None


def constraint_label_cue(folded: str) -> str | None:
    marker = labeled(folded, CONSTRAINT_LABELS)
    return f"constraint-label:'{marker}'" if marker else None


def prohibition_cue(folded: str) -> str | None:
    pair = word_pair_cue(folded, PROHIBITION_FRAMES)
    return f"prohibition:'{pair}'" if pair else None


def capability_cue(folded: str) -> str | None:
    marker = labeled(folded, CAPABILITY_LABELS)
    if marker:
        return f"capability-label:'{marker}'"
    opener = opener_prefix(folded, USE_WHEN_OPENERS)
    return f"use-when:'{opener}'" if opener else None


def failure_label_cue(folded: str) -> str | None:
    marker = labeled(folded, FAILURE_LABELS)
    return f"failure-label:'{marker}'" if marker else None


def failure_frame_cue(folded: str) -> str | None:
    pair = word_pair_cue(folded, FAILURE_FRAMES)
    return f"failure-frame:'{pair}'" if pair else None


def decision_cue(folded: str) -> str | None:
    for w in words(folded):
        if w in DECISION_WORDS:
            return f"decision-word:'{w}'"
    for phrase in DECISION_PHRASES:
        if phrase in folded:
            return f"decision-phrase:'{phrase}'"
    return None


def procedure_label_cue(folded: str) -> str | None:
    marker = labeled(folded, PROCEDURE_LABELS)
    if marker:
        return f"procedure-label:'{marker}'"
    opener = opener_prefix(folded, PROCEDURE_OPENERS)
    return f"how-to-opener:'{opener}'" if opener else None


def procedure_cue(folded: str) -> str | None:
    if folded.startswith("step "):
        after = folded[5:]
        if after and after[0].isdigit():
            end = 0
            while end < len(after) and after[end].isdigit():
                end += 1
            num = after[:end]
            return f"step-prefix:'step {num}'"
    w_list = words(folded)
    if w_list and w_list[0] in IMPERATIVE_OPENERS:
        return f"imperative-opener:'{w_list[0]}'"
    if (folded.startswith(("to ", "para "))) and ": " in folded:
        opener = "to" if folded.startswith("to ") else "para"
        return f"goal-colon:'{opener} …:'"
    return None


def fact_cue(folded: str, original: str) -> str | None:
    for frame in DECLARATIVE_FRAMES:
        if frame in folded:
            entity = entity_in(original)
            if entity is not None:
                return f"declarative:'{frame.strip()}' entity:'{entity}'"
    for w in words(folded):
        if w in PRESCRIPTIVE_WORDS:
            return f"prescriptive:'{w}'"
    return None


def entity_in(segment: str) -> str | None:
    for raw in segment.split():
        if len(raw) > 2 and raw.startswith("`") and raw.endswith("`"):
            return raw
        token = raw.strip("()[]{}.,;:!?\"'`…")
        if len(token) < 2:
            continue
        if any(c in token for c in "(){};"):
            continue
        concrete = (
            "/" in token
            or "\\" in token
            or "::" in token
            or any(c.isdigit() for c in token)
            or "." in token
            or any(c.isupper() for c in token[1:])
        )
        if concrete:
            return token
    return None
