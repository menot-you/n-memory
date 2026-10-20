"""Official LoCoMo (ACL 2024) comparative benchmark runner across memory systems.

Evaluates all conversations and QA pairs across the five official categories
(`single-hop`, `multi-hop`, `temporal`, `open-domain`, `adversarial`) using the
fixed reader and judge from `accuracy.py`, per-conversation session scope
fencing, dataset SHA-256 verification, partial-run refusal, and mandatory
vendor `Receipt` enforcement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import string
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from nmemory_evals.capture_gap import EvalRefusal
from nmemory_evals.comparison.accuracy import (
    ARENA_NOMES,
    B_GRID,
    JUDGE_PROMPT,
    READER_PROMPT,
    WINDOW_HEADER,
    ZaiEndpoint,
    _build_adapter,
    _escrever_atomico,
    _retrieve_fenced_texts,
    parse_verdict,
    prompt_sha,
    served_text,
    window_under_budget,
)
from nmemory_evals.comparison.base import BenchmarkItem, Receipt
from nmemory_evals.longmemeval import question_terms

ADVERSARIAL_JUDGE_PROMPT: Final[str] = (
    "Does the candidate answer abstain from answering the question (stating "
    "that the information is not mentioned, not provided, or cannot be answered "
    "from the conversation) WITHOUT asserting any fabricated factual answer? "
    "Answer only yes or no.\n\n"
    "Question: {question}\n"
    "Adversarial trap: {trap}\n"
    "Candidate answer: {candidate}"
)

LOCOMO_CATEGORIES: Final[tuple[str, ...]] = (
    "single-hop",
    "multi-hop",
    "temporal",
    "open-domain",
    "adversarial",
)

_CATEGORY_MAP: Final[dict[int | str, str]] = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
    "1": "multi-hop",
    "2": "temporal",
    "3": "open-domain",
    "4": "single-hop",
    "5": "adversarial",
    "single-hop": "single-hop",
    "multi-hop": "multi-hop",
    "temporal": "temporal",
    "open-domain": "open-domain",
    "adversarial": "adversarial",
}

_SESSION_KEY = re.compile(r"^session_(\d+)$")
_ARTICLES_RE = re.compile(r"\b(a|an|the|and)\b", re.IGNORECASE)
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)
_PIVOT_RE = re.compile(
    r"\b(but|however|although|though|except|instead|likely|probably)\b",
    re.IGNORECASE,
)
_COURTESY_PREFIX_RE = re.compile(
    r"^(?:(?:i(?:'m|\s+am)\s+)?sorry|(?:i\s+)?apologi[sz]e|unfortunately|regrettably)"
    r"\s*[,:;.-]*\s*",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!?;,]+|\b(?:and|or)\s+(?=(?:she|he|they|it|we|there|the\s+\w+\s+(?:was|is|were|ran|moved|went|lived|happened))\b)",
    re.IGNORECASE,
)

_STEP2_SUFFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("bli", "ble"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
    ("logi", "log"),
)

_STEP3_SUFFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("icate", "ic"),
    ("ative", ""),
    ("alize", "al"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ful", ""),
    ("ness", ""),
)

_STEP4_SUFFIXES: Final[tuple[str, ...]] = (
    "al",
    "ance",
    "ence",
    "er",
    "ic",
    "able",
    "ible",
    "ant",
    "ement",
    "ment",
    "ent",
    "ion",
    "ou",
    "ism",
    "ate",
    "iti",
    "ous",
    "ive",
    "ize",
)


def _is_cons(w: str, i: int) -> bool:
    ch = w[i]
    if ch in "aeiou":
        return False
    if ch == "y":
        return True if i == 0 else not _is_cons(w, i - 1)
    return True


def _measure(w: str) -> int:
    n = len(w)
    i = 0
    while i < n and _is_cons(w, i):
        i += 1
    m = 0
    while i < n:
        while i < n and not _is_cons(w, i):
            i += 1
        if i >= n:
            break
        m += 1
        while i < n and _is_cons(w, i):
            i += 1
    return m


def _has_vowel(w: str) -> bool:
    return any(not _is_cons(w, i) for i in range(len(w)))


def _double_cons(w: str) -> bool:
    return len(w) >= 2 and w[-1] == w[-2] and _is_cons(w, len(w) - 1)


def _cvc(w: str) -> bool:
    return (
        len(w) >= 3
        and _is_cons(w, len(w) - 1)
        and not _is_cons(w, len(w) - 2)
        and _is_cons(w, len(w) - 3)
        and w[-1] not in "wxy"
    )


def _porter_stem(word: str) -> str:
    """Standard 5-step Martin Porter (1980) stemmer for normalized English tokens."""
    w = word.lower()
    if len(w) <= 2:
        return w

    # Step 1a
    if w.endswith(("sses", "ies")):
        w = w[:-2]
    elif not w.endswith("ss") and w.endswith("s"):
        w = w[:-1]

    # Step 1b
    if w.endswith("eed"):
        if _measure(w[:-3]) > 0:
            w = w[:-1]
    else:
        step1b_trimmed: str | None = None
        if w.endswith("ed") and _has_vowel(w[:-2]):
            step1b_trimmed = w[:-2]
        elif w.endswith("ing") and _has_vowel(w[:-3]):
            step1b_trimmed = w[:-3]
        if step1b_trimmed is not None:
            w = step1b_trimmed
            if w.endswith(("at", "bl", "iz")):
                w += "e"
            elif _double_cons(w) and w[-1] not in "lsz":
                w = w[:-1]
            elif _measure(w) == 1 and _cvc(w):
                w += "e"

    # Step 1c
    if w.endswith("y") and _has_vowel(w[:-1]):
        w = w[:-1] + "i"

    # Step 2
    for suf, repl in _STEP2_SUFFIXES:
        if w.endswith(suf):
            stem = w[: -len(suf)]
            if _measure(stem) > 0:
                w = stem + repl
            break

    # Step 3
    for suf, repl in _STEP3_SUFFIXES:
        if w.endswith(suf):
            stem = w[: -len(suf)]
            if _measure(stem) > 0:
                w = stem + repl
            break

    # Step 4
    for suf in _STEP4_SUFFIXES:
        if w.endswith(suf):
            stem = w[: -len(suf)]
            if suf == "ion":
                if stem.endswith(("s", "t")) and _measure(stem) > 1:
                    w = stem
                break
            if _measure(stem) > 1:
                w = stem
            break

    # Step 5a
    if w.endswith("e"):
        stem = w[:-1]
        m = _measure(stem)
        if m > 1 or (m == 1 and not _cvc(stem)):
            w = stem

    # Step 5b
    if _measure(w) > 1 and _double_cons(w) and w.endswith("l"):
        w = w[:-1]

    return w


def _normalize_answer_tokens(text: str) -> list[str]:
    lowered = text.lower().translate(_PUNCT_TABLE)
    without_articles = _ARTICLES_RE.sub(" ", lowered)
    return [_porter_stem(tok) for tok in without_articles.split() if tok]


def _token_f1(prediction: str, ground_truth: str) -> float:
    pred_tokens = _normalize_answer_tokens(prediction)
    truth_tokens = _normalize_answer_tokens(ground_truth)
    if not pred_tokens or not truth_tokens:
        return 1.0 if pred_tokens == truth_tokens else 0.0
    common = Counter(pred_tokens) & Counter(truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(truth_tokens)
    return (2.0 * precision * recall) / (precision + recall)


def locomo_f1(
    prediction: str,
    gold: str,
    category: str,
    *,
    adversarial_answer: str = "",
) -> float:
    """Compute the official LoCoMo F1 score for one QA prediction."""
    if category == "adversarial":
        return (
            1.0
            if _is_adversarial_abstention(
                prediction, adversarial_answer=adversarial_answer
            )
            else 0.0
        )
    if category == "multi-hop":
        gold_parts = [p.strip() for p in gold.split(",") if p.strip()]
        pred_parts = [p.strip() for p in prediction.split(",") if p.strip()]
        if not pred_parts:
            pred_parts = [prediction]
        if gold_parts:
            return sum(
                max(_token_f1(pp, gp) for pp in pred_parts) for gp in gold_parts
            ) / len(gold_parts)
    return _token_f1(prediction, gold)


def verify_dataset_sha256(path: Path, expected_sha256: str | None = None) -> str:
    """Verify the SHA-256 digest of a LoCoMo dataset file."""
    p = Path(path)
    if not p.is_file():
        raise EvalRefusal(f"LoCoMo dataset file missing: {p}")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256.strip().lower():
        raise EvalRefusal(
            f"LoCoMo dataset sha256 {digest} != expected {expected_sha256}"
        )
    return digest


def _normalize_category(raw: Any) -> str:
    key: int | str = raw if isinstance(raw, int) else str(raw).strip().lower()
    cat = _CATEGORY_MAP.get(key)
    if cat is None:
        raise EvalRefusal(
            f"LoCoMo question category {raw!r} is not one of {LOCOMO_CATEGORIES}"
        )
    return cat


def _format_turns(turns: list[dict[str, Any]], date_time: str | None = None) -> str:
    lines: list[str] = []
    if date_time and date_time.strip():
        lines.append(f"[{date_time.strip()}]")
    for turn in turns:
        if not isinstance(turn, dict):
            raise EvalRefusal(
                f"LoCoMo turn must be an object, got {type(turn).__name__}"
            )
        speaker = str(turn.get("speaker") or turn.get("role") or "user").strip()
        text = turn.get("text") if "text" in turn else turn.get("content")
        if text is None and "blip_caption" in turn:
            text = ""
        if not isinstance(text, str):
            raise EvalRefusal(f"LoCoMo turn without text: {sorted(turn)}")
        extras: list[str] = []
        caption = str(turn.get("blip_caption") or "").strip()
        if caption:
            extras.append(f"[image: {caption}]")
        img_query = str(turn.get("query") or "").strip()
        if img_query:
            extras.append(f"[query: {img_query}]")
        suffix = (" " + " ".join(extras)) if extras else ""
        lines.append(f"{speaker}: {text}{suffix}".rstrip())
    return "\n".join(lines)


def _extract_sessions(entry: dict[str, Any], conv_id: str) -> list[tuple[str, str]]:
    """Return `(session_item_id, formatted_text)` pairs for one conversation."""
    conv = entry.get("conversation")
    out: list[tuple[str, str]] = []
    if isinstance(conv, dict):
        numbered: list[tuple[int, list[dict[str, Any]], str | None]] = []
        for k, v in conv.items():
            m = _SESSION_KEY.match(str(k))
            if m and isinstance(v, list):
                idx = int(m.group(1))
                dt = conv.get(f"session_{idx}_date_time")
                numbered.append((idx, v, str(dt) if dt is not None else None))
        if numbered:
            for idx, turns, dt in sorted(numbered, key=lambda x: x[0]):
                if turns:
                    out.append((f"s-{conv_id}-{idx}", _format_turns(turns, dt)))
            return out
    raw_sessions = (
        conv.get("sessions")
        if isinstance(conv, dict) and "sessions" in conv
        else entry.get("sessions")
    )
    if isinstance(raw_sessions, list):
        for idx, s in enumerate(raw_sessions, 1):
            if isinstance(s, dict):
                turns = s.get("turns") or s.get("messages") or []
                dt = s.get("date_time") or s.get("timestamp")
            elif isinstance(s, list):
                turns = s
                dt = None
            else:
                raise EvalRefusal(
                    f"LoCoMo session of unknown shape: {type(s).__name__}"
                )
            if turns:
                out.append(
                    (
                        f"s-{conv_id}-{idx}",
                        _format_turns(turns, str(dt) if dt is not None else None),
                    )
                )
    if not out:
        raise EvalRefusal(f"LoCoMo conversation {conv_id!r} has no sessions")
    return out


def _load_entries(corpus: Path) -> list[dict[str, Any]]:
    doc = json.loads(Path(corpus).read_text(encoding="utf-8"))
    if not isinstance(doc, list) or not doc:
        raise EvalRefusal("LoCoMo dataset must be a non-empty JSON list")
    for row in doc:
        if not isinstance(row, dict):
            raise EvalRefusal("each LoCoMo conversation must be a JSON object")
    return doc


def _conv_id_of(entry: dict[str, Any], index: int) -> str:
    raw = entry.get("sample_id") or entry.get("conv_id") or entry.get("id")
    return str(raw).strip() if raw is not None and str(raw).strip() else f"conv-{index}"


def locomo_items(corpus: Path) -> list[BenchmarkItem]:
    """Convert all conversation sessions in a LoCoMo dataset into BenchmarkItems."""
    entries = _load_entries(corpus)
    items: list[BenchmarkItem] = []
    seen: set[str] = set()
    for idx, entry in enumerate(entries, 1):
        conv_id = _conv_id_of(entry, idx)
        for sid, text in _extract_sessions(entry, conv_id):
            if sid in seen:
                raise EvalRefusal(f"duplicate LoCoMo session id {sid!r}")
            seen.add(sid)
            items.append(
                BenchmarkItem(
                    id=sid,
                    category="session",
                    fact=text,
                    query_terms=[],
                    target=f"locomo:{conv_id}:{sid}",
                    kind="doc",
                )
            )
    return items


def conversation_session_ids(corpus: Path) -> dict[str, frozenset[str]]:
    """Map each conversation ID to the frozenset of its session BenchmarkItem IDs."""
    entries = _load_entries(corpus)
    scopes: dict[str, frozenset[str]] = {}
    for idx, entry in enumerate(entries, 1):
        conv_id = _conv_id_of(entry, idx)
        sids = frozenset(sid for sid, _ in _extract_sessions(entry, conv_id))
        scopes[conv_id] = sids
    return scopes


def locomo_questions(corpus: Path) -> list[dict[str, Any]]:
    """Extract all QA pairs with conversation scope and normalized category."""
    entries = _load_entries(corpus)
    questions: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries, 1):
        conv_id = _conv_id_of(entry, idx)
        sids = sorted(sid for sid, _ in _extract_sessions(entry, conv_id))
        qa_list = entry.get("qa") or entry.get("questions") or []
        if not isinstance(qa_list, list) or not qa_list:
            raise EvalRefusal(f"LoCoMo conversation {conv_id!r} has no qa pairs")
        for q_idx, qa in enumerate(qa_list, 1):
            if not isinstance(qa, dict):
                raise EvalRefusal(
                    f"LoCoMo QA entry must be an object, got {type(qa).__name__}"
                )
            q_text = str(qa.get("question") or "").strip()
            if not q_text:
                raise EvalRefusal(f"LoCoMo question {conv_id}:{q_idx} has empty text")
            category = _normalize_category(qa.get("category"))
            raw_ans = qa.get("answer")
            adv_ans = str(qa.get("adversarial_answer") or "").strip()
            if category == "adversarial":
                gold_str = ""
                if not adv_ans and raw_ans is not None:
                    adv_ans = str(raw_ans).strip()
            else:
                if raw_ans is None:
                    raise EvalRefusal(
                        f"LoCoMo question {conv_id}:{q_idx} ({category}) missing answer"
                    )
                gold_str = str(raw_ans)
            qid = str(qa.get("qid") or qa.get("question_id") or f"{conv_id}-q{q_idx}")
            questions.append(
                {
                    "qid": qid,
                    "conv_id": conv_id,
                    "category": category,
                    "question": q_text,
                    "gold": gold_str,
                    "adversarial_answer": adv_ans,
                    "terms": question_terms(q_text),
                    "haystack_ids": sids,
                }
            )
    return questions


_WHETHER_SIMPLE_SUBJECT_VERB_RE = re.compile(
    r"^(?:whether|if)\s+(?:she|he|they|it|we|[a-z]+)\s+"
    r"(?:(?:ever|did|had|has|have|was|were|is|are|can|could|would|will|should|might|may|been)\s+)*"
    r"[a-z]+(?:\s+(?:in|at|to|for|on|with|from|into|of|by|about))?(?:\s+(?:the|a|an))?$",
    re.IGNORECASE,
)
_TRAP_DISCLAIMER_PREFIX_RE = re.compile(
    r"^(?:(?:the\s+context|the\s+conversation|it)\s+(?:does\s+not|doesn't)\s+(?:mention|state|specify|provide|say)|"
    r"(?:there\s+is\s+)?no\s+(?:mention|information|record)(?:\s+in\s+the\s+(?:context|conversation))?(?:\s+(?:of|about|on))?|"
    r"(?:it\s+is\s+)?not\s+(?:mentioned|provided|specified|stated)(?:\s+in\s+the\s+(?:context|conversation))?|"
    r"i\s+(?:do\s+not|don't)\s+(?:know|have\s+(?:that\s+|enough\s+|any\s+)?information)(?:\s+in\s+the\s+(?:context|conversation))?(?:\s+(?:about|on|to\s+determine))?)\s*$",
    re.IGNORECASE,
)
_TRAP_PASSIVE_TAIL_RE = re.compile(
    r"^\s*(?:is|was|are|were)\s+(?:never\s+|not\s+)(?:mentioned|provided|specified|stated|determined)\b",
    re.IGNORECASE,
)


def _clause_disclaims_trap(clause: str, trap: str) -> bool:
    idx = clause.find(trap)
    if idx < 0:
        return True
    before = clause[:idx].strip()
    after = clause[idx + len(trap) :].strip()
    if _TRAP_PASSIVE_TAIL_RE.search(after) and (
        re.fullmatch(r"(?:the|a|an)?", before)
        or _WHETHER_SIMPLE_SUBJECT_VERB_RE.match(before)
    ):
        return True
    if re.fullmatch(
        r"(?:she|he|they|it|we|[a-z]+)\s+(?:has\s+|had\s+)?never\s+[a-z]+(?:\s+(?:the|a|an|in|at|to))?",
        before,
    ):
        return True
    if _TRAP_DISCLAIMER_PREFIX_RE.match(before):
        return True
    m = re.search(r"\b(?:whether|if)\b", before)
    if m and _TRAP_DISCLAIMER_PREFIX_RE.match(before[: m.start()].strip()):
        return bool(_WHETHER_SIMPLE_SUBJECT_VERB_RE.match(before[m.start() :].strip()))
    return False


def _is_adversarial_abstention(response: str, *, adversarial_answer: str = "") -> bool:
    low = _COURTESY_PREFIX_RE.sub("", response.strip()).strip().lower()
    if not low:
        return False
    if _PIVOT_RE.search(low):
        return False
    trap = adversarial_answer.strip().lower().rstrip(".")
    clauses = [
        _COURTESY_PREFIX_RE.sub("", c).strip()
        for c in _CLAUSE_SPLIT_RE.split(low)
        if _COURTESY_PREFIX_RE.sub("", c).strip()
    ]
    if not clauses:
        return False
    abstain_phrases = (
        "i don't have that information",
        "i do not have that information",
        "don't have enough information",
        "do not have enough information",
        "not mentioned in the context",
        "not mentioned in the conversation",
        "no information in the context",
        "no information available",
        "unanswerable",
        "cannot be answered",
        "can't be answered",
        "cannot be determined",
        "can't be determined",
        "not provided",
        "not mentioned",
        "not specified",
        "not stated",
        "unknown from the",
    )
    for clause in clauses:
        if not any(p in clause for p in abstain_phrases):
            return False
        if trap and trap in clause and not _clause_disclaims_trap(clause, trap):
            return False
    return True


def run_locomo(
    adapter: Any,
    questions: list[dict[str, Any]],
    *,
    dataset_sha256: str,
    budget: int = 4096,
    tokenizer: str = "cl100k_base",
    cliente: Any,
    conversation_scopes: Mapping[str, Collection[str]] | None = None,
    expected_conversations: int | None = 10,
    expected_total: int | None = 1986,
    require_all_categories: bool = True,
    k: int = 20,
    checkpoint: Path | None = None,
) -> dict[str, Any]:
    """Run the LoCoMo evaluation over an adapter with per-conversation scope isolation."""
    if budget not in B_GRID:
        raise EvalRefusal(f"budget {budget} outside B_GRID {B_GRID}")
    if not questions:
        raise EvalRefusal("LoCoMo run without questions")
    if not dataset_sha256 or not dataset_sha256.strip():
        raise EvalRefusal("LoCoMo run requires dataset_sha256")
    conv_ids = {
        str(q.get("conv_id", "")).strip()
        for q in questions
        if str(q.get("conv_id", "")).strip()
    }
    if expected_conversations is not None:
        if len(conv_ids) != expected_conversations:
            raise EvalRefusal(
                f"LoCoMo sample has {len(conv_ids)} conversations, expected {expected_conversations}"
            )
        if (
            conversation_scopes is not None
            and len(conversation_scopes) != expected_conversations
        ):
            raise EvalRefusal(
                f"LoCoMo conversation_scopes has {len(conversation_scopes)} conversations, "
                f"expected {expected_conversations}"
            )
    if expected_total is not None and len(questions) != expected_total:
        raise EvalRefusal(
            f"LoCoMo sample has {len(questions)} questions, expected {expected_total}"
        )
    if require_all_categories:
        present_cats = {_normalize_category(q["category"]) for q in questions}
        missing_cats = sorted(set(LOCOMO_CATEGORIES) - present_cats)
        if missing_cats:
            raise EvalRefusal(f"LoCoMo sample missing categories: {missing_cats}")

    raw_sys = str(getattr(adapter, "system", "")).strip().lower()
    arena_sys = ARENA_NOMES.get(raw_sys, str(getattr(adapter, "system", "")))
    receipt_dict: dict[str, Any] | None = None
    raw_receipt = getattr(adapter, "receipt", None)
    if raw_sys not in ("nmemory", "nmem"):
        if raw_receipt is None:
            raise EvalRefusal(
                f"LoCoMo system {arena_sys!r} without a receipt: third-party "
                "systems require a validated Receipt"
            )
        rec_obj = (
            raw_receipt
            if isinstance(raw_receipt, Receipt)
            else Receipt(**dict(raw_receipt))
        )
        receipt_dict = asdict(rec_obj)
    elif raw_receipt is not None:
        rec_obj = (
            raw_receipt
            if isinstance(raw_receipt, Receipt)
            else Receipt(**dict(raw_receipt))
        )
        receipt_dict = asdict(rec_obj)

    run_meta = {
        "system": arena_sys,
        "dataset_sha256": dataset_sha256,
        "budget": budget,
        "tokenizer": tokenizer,
        "k": k,
        "reader_model": cliente.model,
        "reader_prompt_sha": prompt_sha(READER_PROMPT),
        "judge_prompt_sha": prompt_sha(JUDGE_PROMPT),
        "questions_sha": hashlib.sha256(
            json.dumps(questions, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }
    done: dict[str, dict[str, Any]] = {}
    if checkpoint is not None and checkpoint.is_file():
        ck_doc = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not isinstance(ck_doc, dict) or ck_doc.get("run_meta") != run_meta:
            raise EvalRefusal("LoCoMo checkpoint belongs to a different run")
        valid_qids = {str(q["qid"]) for q in questions}
        done = {
            str(r["qid"]): r
            for r in ck_doc.get("rows", [])
            if isinstance(r, dict) and str(r.get("qid")) in valid_qids
        }

    for q in questions:
        qid = str(q["qid"])
        if qid in done:
            continue
        conv_id = str(q.get("conv_id", ""))
        allowed: frozenset[str] | None = None
        if conversation_scopes is not None and conv_id in conversation_scopes:
            allowed = frozenset(conversation_scopes[conv_id])
        elif isinstance(q.get("haystack_ids"), Collection):
            allowed = frozenset(str(x) for x in q["haystack_ids"])
        category = _normalize_category(q["category"])
        textos, _outcome, _ms = _retrieve_fenced_texts(
            adapter, list(q["terms"]), k=k, allowed_ids=allowed
        )
        janela = window_under_budget(
            textos, budget=budget, tokenizer=tokenizer, header=WINDOW_HEADER
        )
        resposta, uso_l = cliente.ask(
            READER_PROMPT.format(
                window=served_text(janela), question=str(q["question"])
            )
        )
        uso_j: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        julgamento = "abstain"
        adv_trap = str(q.get("adversarial_answer", ""))
        if category == "adversarial":
            abstained = _is_adversarial_abstention(
                resposta, adversarial_answer=adv_trap
            )
            julgamento, uso_j = cliente.ask(
                ADVERSARIAL_JUDGE_PROMPT.format(
                    question=str(q["question"]),
                    trap=adv_trap or "(none)",
                    candidate=resposta,
                ),
                max_tokens=32,
            )
            acertou = abstained and parse_verdict(julgamento)
            f1_val = 1.0 if acertou else 0.0
        else:
            julgamento, uso_j = cliente.ask(
                JUDGE_PROMPT.format(
                    question=str(q["question"]),
                    gold=str(q["gold"]),
                    candidate=resposta,
                ),
                max_tokens=32,
            )
            acertou = parse_verdict(julgamento)
            f1_val = locomo_f1(
                resposta,
                str(q.get("gold", "")),
                category,
                adversarial_answer=adv_trap,
            )

        done[qid] = {
            "qid": qid,
            "conv_id": conv_id,
            "category": category,
            "correct": acertou,
            "f1": f1_val,
            "response": resposta,
            "judgment": julgamento.strip(),
            "usage": {
                "input_tokens": uso_l.get("input_tokens", 0)
                + uso_j.get("input_tokens", 0),
                "output_tokens": uso_l.get("output_tokens", 0)
                + uso_j.get("output_tokens", 0),
            },
        }
        if checkpoint is not None:
            _escrever_atomico(
                checkpoint,
                json.dumps(
                    {"run_meta": run_meta, "rows": list(done.values())},
                    ensure_ascii=False,
                ),
            )

    if len(done) != len(questions):
        raise EvalRefusal(
            f"LoCoMo partial run: {len(done)} of {len(questions)} questions judged"
        )

    rows = list(done.values())
    total = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    mean_f1 = sum(float(r.get("f1", 0.0)) for r in rows) / total
    cat_scores: dict[str, dict[str, Any]] = {}
    for cat in LOCOMO_CATEGORIES:
        cat_rows = [r for r in rows if r["category"] == cat]
        c_tot = len(cat_rows)
        c_ok = sum(1 for r in cat_rows if r["correct"])
        c_acc = (c_ok / c_tot) if c_tot else 0.0
        c_f1 = sum(float(r.get("f1", 0.0)) for r in cat_rows) / c_tot if c_tot else 0.0
        cat_scores[cat] = {
            "correct": c_ok,
            "total": c_tot,
            "accuracy": c_acc,
            "judge_accuracy": c_acc,
            "mean_f1": c_f1,
        }

    return {
        "system": arena_sys,
        "dataset_sha256": dataset_sha256,
        "token_budget": budget,
        "tokenizer": tokenizer,
        "reader": {"model": cliente.model, "prompt_sha": prompt_sha(READER_PROMPT)},
        "judge": {"model": cliente.model, "prompt_sha": prompt_sha(JUDGE_PROMPT)},
        "correct": correct,
        "total": total,
        "expected_conversations": (
            expected_conversations
            if expected_conversations is not None
            else len(conv_ids)
        ),
        "expected_total": expected_total if expected_total is not None else total,
        "accuracy": correct / total,
        "judge_accuracy": correct / total,
        "mean_f1": mean_f1,
        "category_scores": cat_scores,
        "usage": {
            "input_tokens": sum(
                r.get("usage", {}).get("input_tokens", 0) for r in rows
            ),
            "output_tokens": sum(
                r.get("usage", {}).get("output_tokens", 0) for r in rows
            ),
        },
        "receipt": receipt_dict,
        "questions": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    """CLI parser for the LoCoMo benchmark runner."""
    parser = argparse.ArgumentParser(prog="nmemory-evals locomo", add_help=False)
    parser.add_argument(
        "--system", required=True, choices=["nmemory", "zep", "letta", "mem0"]
    )
    parser.add_argument("--budget", type=int, default=4096)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--expected-sha256", type=str, default=None)
    parser.add_argument("--expected-conversations", type=int, default=10)
    parser.add_argument("--expected-total", type=int, default=1986)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, default=None)
    return parser


def run_locomo_cmd(args: argparse.Namespace) -> dict[str, Any]:
    """Seed a LoCoMo dataset into the target system and evaluate all questions."""
    sha = verify_dataset_sha256(
        Path(args.corpus), getattr(args, "expected_sha256", None)
    )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    adapter = _build_adapter(args.system, run_id)
    try:
        items = locomo_items(Path(args.corpus))
        scopes = conversation_session_ids(Path(args.corpus))
        questions = locomo_questions(Path(args.corpus))
        adapter.ingest_all(items)
        cliente = ZaiEndpoint()
        report = run_locomo(
            adapter,
            questions,
            dataset_sha256=sha,
            budget=args.budget,
            tokenizer="cl100k_base",
            cliente=cliente,
            conversation_scopes=scopes,
            expected_conversations=getattr(args, "expected_conversations", 10),
            expected_total=getattr(args, "expected_total", 1986),
            checkpoint=Path(args.checkpoint) if args.checkpoint else None,
        )
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return report
    finally:
        adapter.close()
