"""Axis B — answer accuracy — with ONE fixed reader and a cell that does not lie.

The pre-registration (`evals/nmemory/tests/fixtures/PRE-REGISTERED-LOSS.txt:63-83`)
requires: 470 scored LongMemEval-S questions answered by the SAME fixed reader
— one model and one prompt, with the identity (model name, prompt sha) recorded
in the receipt — and memory limited to `b ∈ B_GRID` tokens. A run with two
readers does not produce `accuracy_B`; an empty cell is never worth 0 and makes
the matrix not publish.

This module's pieces:
- `window_under_budget` builds the memory window served to the reader, cut by
  the TOKEN, in retrieval rank order — whatever exceeds `b` is not served, not
  even half a token.
- `ReaderIdentity` pins the (model, prompt sha) pair the receipt requires.
- `read_accuracy_report` applies to axis B the same discipline as the axis-A
  reader: a named refusal instead of a zero.
- `accuracy_table` refuses two cells of the same (system, budget) measured by
  different readers — there is no averaging across readers.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import urllib.error
import urllib.request
from collections.abc import Collection, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tiktoken

from nmemory_evals.capture_gap import EvalRefusal
from nmemory_evals.comparison.base import Receipt

#: Calibrated floors for A.3 abstention probes (`longmemeval/pra2/grid.json`):
#: the observed maximum across the 30 `_abs` probes is BM25 39.085 and cosine
#: 0.4441, so any out-of-world probe candidate falls below these floors and
#: returns `missing_evidence` with `results = []`.
ABSTAIN_TERM_FLOOR = 40.0
ABSTAIN_VECTOR_FLOOR = 0.45

#: The pre-registration's budgets, in tokens. Axis B does NOT collapse: the
#: corpus is ~100k tokens per question and `b` cuts different content at each
#: member.
B_GRID: tuple[int, ...] = (2048, 4096, 8192, 16384)

#: The tokenizer declared by the run must be one of these — the window cut
#: counts tokens with a concrete vocabulary, not «approximately».
_TOKENIZERS: frozenset[str] = frozenset({"cl100k_base", "o200k_base"})

_ENC: dict[str, tiktoken.Encoding] = {}


def _tokenize(text: str, tokenizer: str) -> int:
    """Counts tokens; refuses an unknown vocabulary instead of guessing."""
    if tokenizer not in _TOKENIZERS:
        raise EvalRefusal(
            f"tokenizer {tokenizer!r} unknown: the B_GRID cut counts tokens "
            "with a concrete vocabulary (cl100k_base, o200k_base)"
        )
    enc = _ENC.get(tokenizer)
    if enc is None:
        enc = _ENC[tokenizer] = tiktoken.get_encoding(tokenizer)
    return len(enc.encode(text))


@dataclass(frozen=True)
class ReaderIdentity:
    """Who answered: one model and one prompt, both named."""

    model: str
    prompt_sha: str


#: The header served to the reader — the SAME text READER_PROMPT carries, so
#: the cut and the prompt cannot diverge by a single byte.
WINDOW_HEADER = "User context, extracted from memory."


def served_text(janela: list[str]) -> str:
    """The exact block the reader will see: header + passages joined by \n."""
    return WINDOW_HEADER + "\n" + "\n".join(janela)


def window_under_budget(
    passages: list[str], *, budget: int, tokenizer: str, header: str = WINDOW_HEADER
) -> list[str]:
    """Serves passages while the served SERIALIZATION fits the budget.

    The count is of the whole block — header and separators included — because
    that is what the reader receives. Counting passages alone left the `\n` out
    and the window blew past `b` (review blocker 1: 2062 tokens served on a
    2048 budget). The order is the ranking's; no partial cutting.
    """
    if _tokenize("", tokenizer) < 0:  # pragma: no cover — vocabulary guard
        raise EvalRefusal("tokenizer refused even the empty string")
    if _tokenize(header, tokenizer) >= budget:
        raise EvalRefusal(
            f"the header alone ({_tokenize(header, tokenizer)} tokens) does "
            f"not fit budget {budget}: no room for a single passage"
        )
    if header != WINDOW_HEADER:
        raise EvalRefusal(
            "the window header must be the WINDOW_HEADER the prompt carries "
            "— counting one text and serving another is a false budget"
        )
    janela: list[str] = []
    for passagem in passages:
        candidato = served_text(janela + [passagem])
        if _tokenize(candidato, tokenizer) > budget:
            break
        janela.append(passagem)
    return janela


def _exact_int(doc: dict[str, Any], chave: str, caminho: str) -> int:
    valor = doc.get(chave)
    if type(valor) is not int:  # bool is int in Python — `True` is not a count
        raise EvalRefusal(f"{caminho}: {chave} must be an exact int, got {valor!r}")
    return valor


def read_accuracy_report(doc: Any) -> dict[str, Any]:
    """Reads ONE axis-B cell, refusing by name whatever is not a cell.

    The cell covers ALL the questions: `total` must exactly cover the
    `expected_total` the run declares. A partial sample is not accuracy.
    """
    if not isinstance(doc, dict):
        raise EvalRefusal(f"a B cell must be an object, got {type(doc).__name__}")
    origem = "B cell"
    budget = doc.get("token_budget")
    if budget not in B_GRID:
        raise EvalRefusal(f"{origem}: token_budget {budget!r} outside B_GRID {B_GRID}")
    identidades: dict[str, ReaderIdentity] = {}
    for papel in ("reader", "judge"):
        bloco = doc.get(papel)
        if (
            not isinstance(bloco, dict)
            or not bloco.get("model")
            or not bloco.get("prompt_sha")
        ):
            raise EvalRefusal(
                f"{origem}: {papel} without an identity — the pre-registration "
                "records model and prompt sha in the receipt; without them this "
                "is not accuracy_B"
            )
        identidades[papel] = ReaderIdentity(
            model=str(bloco["model"]), prompt_sha=str(bloco["prompt_sha"])
        )
    total = _exact_int(doc, "total", origem)
    correct = _exact_int(doc, "correct", origem)
    expected = _exact_int(doc, "expected_total", origem)
    if total <= 0:
        raise EvalRefusal(
            f"{origem}: an empty cell is not a cell — total 0 is never accuracy 0 "
            "(the matrix does not publish, PRE-REGISTERED-LOSS.txt:88-91)"
        )
    if total != expected:
        raise EvalRefusal(
            f"{origem}: total {total} ≠ expected_total {expected} — a partial "
            "sample is not accuracy_B, the matrix does not publish with one"
        )
    if not 0 <= correct <= total:
        raise EvalRefusal(f"{origem}: correct {correct} outside [0, {total}]")
    uso = doc.get("usage")
    if not isinstance(uso, dict) or set(uso) != {"input_tokens", "output_tokens"}:
        raise EvalRefusal(
            f"{origem}: usage must carry exact input_tokens and output_tokens "
            "— unknown usage does not enter the receipt as zero"
        )
    for campo in ("input_tokens", "output_tokens"):
        _exact_int(uso, campo, f"{origem}.usage")
    out: dict[str, Any] = {
        "system": str(doc.get("system", "")),
        "token_budget": budget,
        "reader": identidades["reader"],
        "judge": identidades["judge"],
        "correct": correct,
        "total": total,
        "accuracy": correct / total,
    }
    sys_key = str(doc.get("system", "")).strip().lower()
    raw_receipt = doc.get("receipt")
    if raw_receipt is None:
        if sys_key in ("zep", "letta", "mem0"):
            raise EvalRefusal(f"{origem}: {sys_key!r} cell without a receipt")
    else:
        if not isinstance(raw_receipt, dict):
            raise EvalRefusal(
                f"{origem}: receipt must be an object, got {type(raw_receipt).__name__}"
            )
        try:
            out["receipt"] = asdict(Receipt(**raw_receipt))
        except TypeError as err:
            raise EvalRefusal(f"{origem}: invalid receipt shape: {err}") from err
    if "corpus_sha256" in doc and doc["corpus_sha256"] is not None:
        csha = str(doc["corpus_sha256"]).strip()
        if not csha:
            raise EvalRefusal(f"{origem}: empty corpus_sha256")
        out["corpus_sha256"] = csha
    return out


def accuracy_table(docs: list[Any]) -> dict[tuple[str, int], dict[str, Any]]:
    """Axis B's table: one cell per (system, budget).

    The reader's AND the judge's identity belongs to the WHOLE RUN — «identical
    for all systems and all b» (PRE-REGISTERED-LOSS.txt:68). Any two cells with
    different readers (or judges) refuse the table, even when the keys do not
    collide (review blocker 3).
    """
    tabela: dict[tuple[str, int], dict[str, Any]] = {}
    referencias: dict[str, ReaderIdentity] = {}
    corpus_ref: str | None = None
    for doc in docs:
        célula = read_accuracy_report(doc)
        chave = (célula["system"], célula["token_budget"])
        anterior = tabela.get(chave)
        if anterior is not None:
            mesmo_leitor = (
                anterior["reader"].model == célula["reader"].model
                and anterior["reader"].prompt_sha == célula["reader"].prompt_sha
            )
            if not mesmo_leitor:
                raise EvalRefusal(
                    f"cell {chave} measured by two different readers "
                    f"({anterior['reader'].model} and {célula['reader'].model}): "
                    "a run with two readers does not produce accuracy_B"
                )
        for papel in ("reader", "judge"):
            ref = referencias.get(papel)
            atual = célula[papel]
            if ref is not None and (
                ref.model != atual.model or ref.prompt_sha != atual.prompt_sha
            ):
                raise EvalRefusal(
                    f"the table mixes different {papel}s ({ref.model} and "
                    f"{atual.model}) — the law is ONE identical fixed reader for "
                    "all systems and all b (PRE-REGISTERED-LOSS.txt:68)"
                )
            referencias[papel] = atual
        csha = célula.get("corpus_sha256")
        if csha is not None:
            if corpus_ref is not None and corpus_ref != csha:
                raise EvalRefusal(
                    f"the table mixes different corpus_sha256 ({corpus_ref} and {csha})"
                )
            corpus_ref = csha
        tabela[chave] = célula
    return tabela


#: The `window` arrives ALREADY serialized (`served_text`, header included) —
#: the template does not repeat it: repeating meant counting one header and
#: serving two (blocker B1 of the second pass: 2048 counted, 2061 served).
READER_PROMPT = (
    "{window}\n\n"
    "Question: {question}\n"
    "Answer in one line, with the answer only. If the context is not enough, "
    "answer: I don't have that information in the context."
)

#: The judge is fixed and binary too — anything other than yes/no is a
#: refusal, never a silent False.
JUDGE_PROMPT = (
    "Question: {question}\n"
    "Gold answer: {gold}\n"
    "Candidate answer: {candidate}\n"
    "Does the candidate match the gold answer? Answer only «yes» or «no»."
)


def prompt_sha(template: str) -> str:
    return hashlib.sha256(template.encode()).hexdigest()


class ZaiEndpoint:
    """One model and one prompt — the owner's law is Z.ai, never
    sonnet/claude.ai.

    The key never enters a prompt, log, or receipt; it is read from the env at
    call time.
    """

    def __init__(self) -> None:
        base = os.environ.get("ZAI_BASE_URL", "").strip().rstrip("/")
        model = os.environ.get("ZAI_DEFAULT_MODEL", "").strip()
        self._key = os.environ.get("ZAI_API_KEY", "").strip()
        for var, valor in (
            ("ZAI_BASE_URL", base),
            ("ZAI_DEFAULT_MODEL", model),
            ("ZAI_API_KEY", self._key),
        ):
            if not valor:
                raise EvalRefusal(
                    f"{var} absent: axis B speaks through Z.ai (owner's law), "
                    "without that variable there is no reader — and an invented reader "
                    "does not produce accuracy_B"
                )
        self.base = base
        self.model = model

    def ask(
        self, user: str, *, max_tokens: int = 300, timeout: int = 180
    ) -> tuple[str, dict[str, int]]:
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": user}],
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.base}/v1/messages",
            data=body,
            headers={
                "Authorization": f"Bearer {self._key}",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                doc = json.load(r)
        except urllib.error.HTTPError as err:
            raise EvalRefusal(
                f"reader: HTTP {err.code} from the Z.ai endpoint"
            ) from err
        except urllib.error.URLError as err:
            raise EvalRefusal(
                f"reader: Z.ai endpoint unreachable: {err.reason}"
            ) from err
        texto = "".join(
            b.get("text", "") for b in doc.get("content", []) if isinstance(b, dict)
        )
        uso = doc.get("usage", {})
        return texto, {
            k: int(uso[k]) for k in ("input_tokens", "output_tokens") if k in uso
        }


def parse_verdict(texto: str) -> bool:
    """`yes`/`no` (also Portuguese `sim`/`nao`, accented or not), case and trailing
    punctuation irrelevant."""
    limpo = texto.strip().lower().rstrip(".")
    if limpo in ("sim", "yes"):
        return True
    if limpo in ("não", "nao", "no"):
        return False
    raise EvalRefusal(
        f"judge answered {texto.strip()!r} — not yes/no; a verdict I cannot "
        "read is a refusal, not a False"
    )


def _corrida(
    adapter: Any,
    perguntas: list[dict[str, Any]],
    budget: int,
    tokenizer: str,
    cliente: Any,
    k: int,
) -> dict[str, Any]:
    """The run identity the checkpoint records — resuming a different run
    through the same file is measuring two things and calling them one.

    Review blocker B2: the identity covers the WHOLE questions, not just the
    `qid`s. Changing the gold or the wording while keeping the id is another
    measurement, and a checkpoint that only compares ids would resume it as if
    it were the same.
    """
    return {
        "system": str(getattr(adapter, "system", "")),
        "budget": budget,
        "tokenizer": tokenizer,
        "k": k,
        "reader_model": cliente.model,
        "reader_prompt_sha": prompt_sha(READER_PROMPT),
        "judge_prompt_sha": prompt_sha(JUDGE_PROMPT),
        "perguntas_sha": hashlib.sha256(
            json.dumps(perguntas, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
    }


def _escrever_atomico(caminho: Path, texto: str) -> None:
    """Tmp + rename: an interruption midway does not destroy the resume."""
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, caminho)


def _retrieve_fenced_texts(
    adapter: Any,
    terms: list[str],
    *,
    k: int,
    allowed_ids: AbstractSet[str] | None = None,
    term_floor: float = 0.0,
    vector_floor: float = 0.0,
) -> tuple[list[str], str, float]:
    """Call adapter.retrieve_texts and enforce per-question haystack fencing."""
    fn = adapter.retrieve_texts
    kwargs: dict[str, Any] = {}
    try:
        sig = inspect.signature(fn)
        params = sig.parameters
        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if allowed_ids is not None and ("allowed_ids" in params or has_var_kw):
            kwargs["allowed_ids"] = allowed_ids
        if term_floor > 0.0 and ("term_floor" in params or has_var_kw):
            kwargs["term_floor"] = term_floor
        if vector_floor > 0.0 and ("vector_floor" in params or has_var_kw):
            kwargs["vector_floor"] = vector_floor
    except TypeError, ValueError:
        pass
    if allowed_ids is not None and "allowed_ids" not in kwargs:
        by_text = getattr(adapter, "_by_text", None) or getattr(
            adapter, "_item_by_text", None
        )
        pool_k = max(
            k,
            int(getattr(adapter, "_ingested_count", 0)),
            len(by_text) if isinstance(by_text, Mapping) else 0,
            100_000,
        )
        textos, outcome, ms = fn(terms, k=pool_k, **kwargs)
        if isinstance(by_text, Mapping):
            textos = [t for t in textos if by_text.get(t) in allowed_ids][:k]
        elif hasattr(adapter, "retrieve"):
            ids, _, _ = adapter.retrieve(terms, k=pool_k)
            if len(ids) == len(textos):
                textos = [
                    t
                    for t, item_id in zip(textos, ids, strict=False)
                    if item_id in allowed_ids
                ][:k]
        return textos, outcome, ms
    textos, outcome, ms = fn(terms, k=k, **kwargs)
    return textos, outcome, ms


def _allowed_for_question(
    q: Mapping[str, Any],
    haystack_ids: Mapping[str, Collection[str]] | None,
) -> frozenset[str] | None:
    qid = str(q["qid"])
    if haystack_ids is not None and qid in haystack_ids:
        return frozenset(haystack_ids[qid])
    raw = q.get("haystack_ids")
    if isinstance(raw, Collection) and not isinstance(raw, str):
        return frozenset(str(x) for x in raw)
    return None


def run_accuracy_cell(
    adapter: Any,
    perguntas: list[dict[str, Any]],
    *,
    budget: int,
    tokenizer: str,
    cliente: Any,
    checkpoint: Path | None = None,
    k: int = 50,
    seeding: dict[str, Any] | None = None,
    corpus_total: int | None = None,
    haystack_ids: Mapping[str, Collection[str]] | None = None,
    corpus_sha256: str | None = None,
) -> dict[str, Any]:
    """One accuracy_B cell: retrieve → window → reader → judge, per question.

    The checkpoint carries the run identity (system, budget, tokenizer, reader,
    questions) and refuses a file from ANOTHER run; it counts only rows of the
    current questions; it writes atomically. At the end, the cell only comes
    out if ALL questions were judged — partial is a refusal, never accuracy.
    """
    if budget not in B_GRID:
        raise EvalRefusal(f"budget {budget} outside B_GRID {B_GRID}")
    if not perguntas:
        raise EvalRefusal("cell without questions: not a cell")
    if corpus_total is None:
        raise EvalRefusal(
            "cell without corpus_total: the expectation comes from the CORPUS, "
            "not the sample — without it, a one-question pilot is "
            "indistinguishable from the pre-registered measurement (blocker B4)"
        )
    if len(perguntas) != corpus_total:
        raise EvalRefusal(
            f"sample of {len(perguntas)} questions on a corpus of {corpus_total}: "
            "a pilot is not a cell — the cell covers ALL the questions"
        )
    corrida = _corrida(adapter, perguntas, budget, tokenizer, cliente, k)
    feitos: dict[str, dict[str, Any]] = {}
    if checkpoint is not None and checkpoint.is_file():
        doc = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("corrida") != corrida:
            raise EvalRefusal(
                "checkpoint from another run (different system/budget/tokenizer/reader/"
                "questions): resuming it would be measuring two things and "
                "calling them one"
            )
        validos = {str(q["qid"]) for q in perguntas}
        feitos = {
            str(r["qid"]): r
            for r in doc.get("linhas", [])
            if isinstance(r, dict) and str(r.get("qid")) in validos
        }
    for q in perguntas:
        qid = str(q["qid"])
        if qid in feitos:
            continue
        allowed = _allowed_for_question(q, haystack_ids)
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
        julgamento, uso_j = cliente.ask(
            JUDGE_PROMPT.format(
                question=str(q["question"]), gold=str(q["gold"]), candidate=resposta
            ),
            max_tokens=32,
        )
        acertou = parse_verdict(julgamento)
        feitos[qid] = {
            "qid": qid,
            "correct": acertou,
            "resposta": resposta,
            "julgamento": julgamento.strip(),
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
                    {"corrida": corrida, "linhas": list(feitos.values())},
                    ensure_ascii=False,
                ),
            )
    if len(feitos) != len(perguntas):
        raise EvalRefusal(
            f"partial cell: {len(feitos)} of {len(perguntas)} questions "
            "judged — partial never publishes"
        )
    linhas = list(feitos.values())
    correct = sum(1 for r in linhas if r["correct"])
    cell_out: dict[str, Any] = {
        "system": corrida["system"],
        "token_budget": budget,
        "reader": {"model": cliente.model, "prompt_sha": prompt_sha(READER_PROMPT)},
        "judge": {"model": cliente.model, "prompt_sha": prompt_sha(JUDGE_PROMPT)},
        "correct": correct,
        "total": len(linhas),
        "expected_total": corpus_total,
        "usage": {
            "input_tokens": sum(
                r.get("usage", {}).get("input_tokens", 0) for r in linhas
            ),
            "output_tokens": sum(
                r.get("usage", {}).get("output_tokens", 0) for r in linhas
            ),
        },
        "tokenizer": tokenizer,
        "seeding": seeding or {},
    }
    raw_receipt = getattr(adapter, "receipt", None)
    if raw_receipt is not None:
        receipt_obj = (
            raw_receipt
            if isinstance(raw_receipt, Receipt)
            else Receipt(**dict(raw_receipt))
        )
        cell_out["receipt"] = asdict(receipt_obj)
    if corpus_sha256 is not None:
        cell_out["corpus_sha256"] = corpus_sha256
    return cell_out


run_cell = run_accuracy_cell


def _turnos_para_texto(turnos: list[dict[str, Any]]) -> str:
    """A session becomes ONE text: `role: content` per turn, in order."""
    partes: list[str] = []
    for turno in turnos:
        if not isinstance(turno, dict) or "content" not in turno:
            raise EvalRefusal(
                f"turn of unknown shape: keys {sorted(turno) if isinstance(turno, dict) else type(turno).__name__}"
            )
        partes.append(f"{turno.get('role', 'user')}: {turno['content']}")
    return "\n".join(partes)


def _session_hash_id(session: Any) -> str:
    if isinstance(session, list):
        canonical = _turnos_para_texto(session)
    else:
        canonical = json.dumps(session, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"s-{digest[:12]}"


def question_haystack_ids(corpus: Path) -> dict[str, frozenset[str]]:
    """Map each question_id in the corpus to the deterministic session IDs of its haystack."""
    doc = json.loads(Path(corpus).read_text(encoding="utf-8"))
    if not isinstance(doc, list):
        raise EvalRefusal(f"corpus must be a list, got {type(doc).__name__}")
    out: dict[str, frozenset[str]] = {}
    for question in doc:
        qid = str(question["question_id"])
        sids = frozenset(
            _session_hash_id(session)
            for session in question.get("haystack_sessions", [])
        )
        out[qid] = sids
    return out


def accuracy_items(corpus: Path) -> list[Any]:
    """The corpus becomes items: one unique session per content, deterministic
    id.

    Deduplication is by CONTENT (canonical JSON hash): LongMemEval repeats the
    same person's haystack across many questions, and seeding twice would
    contaminate the rank with the same text.
    """
    from nmemory_evals.comparison.base import BenchmarkItem

    doc = json.loads(Path(corpus).read_text(encoding="utf-8"))
    if not isinstance(doc, list):
        raise EvalRefusal(f"corpus must be a list, got {type(doc).__name__}")
    vistos: dict[str, Any] = {}
    for pergunta in doc:
        for sessao in pergunta.get("haystack_sessions", []):
            sid = _session_hash_id(sessao)
            if sid in vistos:
                continue
            vistos[sid] = BenchmarkItem(
                id=sid,
                category="session",
                fact=_turnos_para_texto(sessao),
                query_terms=[],
                target="",
                # The server's enumeration (server.py:762) has no «session»;
                # a whole session is a doc — measured by the reviewer against
                # the real validator.
                kind="doc",
            )
    return list(vistos.values())


def scored_questions(corpus: Path) -> list[dict[str, Any]]:
    """The corpus's SCORED questions: those that are not `_abs` probes, with
    the `terms` the runner needs (longmemeval's question_terms)."""
    from nmemory_evals.longmemeval import question_terms

    doc = json.loads(Path(corpus).read_text(encoding="utf-8"))
    out: list[dict[str, Any]] = []
    for q in doc:
        if "_abs" in str(q["question_id"]):
            continue
        row: dict[str, Any] = {
            "qid": str(q["question_id"]),
            "question": str(q["question"]),
            "gold": str(q["answer"]),
            "terms": question_terms(str(q["question"])),
        }
        sessions = q.get("haystack_sessions")
        if isinstance(sessions, list) and sessions:
            row["haystack_ids"] = sorted({_session_hash_id(s) for s in sessions})
        out.append(row)
    return out


def abstention_probes(corpus: Path) -> list[dict[str, Any]]:
    """The corpus's `_abs` probes, with terms for retrieval."""
    from nmemory_evals.longmemeval import question_terms

    doc = json.loads(Path(corpus).read_text(encoding="utf-8"))
    probes: list[dict[str, Any]] = []
    for q in doc:
        if "_abs" not in str(q["question_id"]):
            continue
        row: dict[str, Any] = {
            "qid": str(q["question_id"]),
            "terms": question_terms(str(q["question"])),
        }
        sessions = q.get("haystack_sessions")
        if isinstance(sessions, list) and sessions:
            row["haystack_ids"] = sorted({_session_hash_id(s) for s in sessions})
        probes.append(row)
    if not probes:
        raise EvalRefusal(
            "corpus without _abs probes: there is no A.3 to measure in it"
        )
    return probes


#: Each system's name in the pre-registration's table (arena.load_table).
ARENA_NOMES: dict[str, str] = {
    "nmemory": "nMEM",
    "zep": "Zep",
    "letta": "Letta",
    "mem0": "mem0",
}


def arena_rows(celulas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Axis-B cells → the verdict's `system/metric/budget/value` rows.

    Goes THROUGH the guards first (cell reader + table with a single reader and
    judge identity): exporting without validating bypassed everything the
    reader refuses (blocker B5).
    """
    tabela = accuracy_table(celulas)
    linhas = []
    for (system, budget), célula in sorted(tabela.items()):
        row: dict[str, Any] = {
            "system": ARENA_NOMES.get(system, system),
            "metric": "accuracy_B",
            "budget": budget,
            "value": célula["accuracy"],
            "reader": {
                "model": célula["reader"].model,
                "prompt_sha": célula["reader"].prompt_sha,
            },
            "judge": {
                "model": célula["judge"].model,
                "prompt_sha": célula["judge"].prompt_sha,
            },
        }
        if "receipt" in célula:
            row["receipt"] = célula["receipt"]
        if "corpus_sha256" in célula:
            row["corpus_sha256"] = célula["corpus_sha256"]
        linhas.append(row)
    return linhas


def run_abstain_cell(
    adapter: Any,
    probes: list[dict[str, Any]],
    *,
    k: int = 20,
    haystack_ids: Mapping[str, Collection[str]] | None = None,
    term_floor: float = ABSTAIN_TERM_FLOOR,
    vector_floor: float = ABSTAIN_VECTOR_FLOOR,
    corpus_sha256: str | None = None,
) -> dict[str, Any]:
    """The `_abs` probes: a hit is abstaining OR returning nothing (the
    helper's law).

    The output is the ROW `arena-verdict --results` consumes: metric
    `true_abstain`, null budget, value the rate.
    """
    if not probes:
        raise EvalRefusal("abstention cell without probes: not a cell")
    acertos = 0
    for p in probes:
        allowed = _allowed_for_question(p, haystack_ids)
        textos, outcome, _ms = _retrieve_fenced_texts(
            adapter,
            list(p["terms"]),
            k=k,
            allowed_ids=allowed,
            term_floor=term_floor,
            vector_floor=vector_floor,
        )
        if outcome == "abstain" or len(textos) == 0:
            acertos += 1
    out: dict[str, Any] = {
        "system": ARENA_NOMES.get(
            str(getattr(adapter, "system", "")), str(getattr(adapter, "system", ""))
        ),
        "metric": "true_abstain",
        "budget": None,
        "value": acertos / len(probes),
        "probes": len(probes),
        "acertos": acertos,
    }
    raw_receipt = getattr(adapter, "receipt", None)
    if raw_receipt is not None:
        receipt_obj = (
            raw_receipt
            if isinstance(raw_receipt, Receipt)
            else Receipt(**dict(raw_receipt))
        )
        out["receipt"] = asdict(receipt_obj)
    if corpus_sha256 is not None:
        out["corpus_sha256"] = corpus_sha256
    return out


def build_parser() -> argparse.ArgumentParser:
    """The parser for axis B and abstention — the args the run needs."""
    parser = argparse.ArgumentParser(prog="nmemory-evals eixo-B", add_help=False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    acc = sub.add_parser("accuracy-run")
    acc.add_argument(
        "--system", required=True, choices=["nmemory", "zep", "letta", "mem0"]
    )
    acc.add_argument("--budget", type=int, required=True)
    acc.add_argument("--corpus", type=Path, required=True)
    acc.add_argument("--out", type=Path, required=True)
    acc.add_argument("--checkpoint", type=Path, default=None)
    abs_ = sub.add_parser("abstain-run")
    abs_.add_argument(
        "--system", required=True, choices=["nmemory", "zep", "letta", "mem0"]
    )
    abs_.add_argument("--corpus", type=Path, required=True)
    abs_.add_argument("--out", type=Path, required=True)
    return parser


def _build_adapter(system: str, run_id: str) -> Any:
    """The same criterion as compare: nMEMORY through the binary, vendors over
    HTTP."""
    if system == "nmemory":
        from nmemory_evals.main import benchmark_store_dir, default_binary

        return __import__(
            "nmemory_evals.comparison", fromlist=["NMemoryAdapter"]
        ).NMemoryAdapter(
            binary=default_binary(),
            store_dir=benchmark_store_dir(),
            run_dir=Path("/var/tmp/nmemory-bench/accuracy-run"),
        )
    from nmemory_evals import comparison as _cmp

    return _cmp.build_seeding_adapter(system, run_id=run_id)


def run_accuracy_cmd(args: argparse.Namespace) -> dict[str, Any]:
    """One accuracy_B cell: seeds, asks, judges, writes the cell."""
    if args.system not in ("nmemory", "zep", "letta", "mem0"):
        raise EvalRefusal(f"system {args.system!r} is not one of the arena's four")
    corpus_path = Path(args.corpus)
    if not corpus_path.is_file():
        raise EvalRefusal(f"corpus missing: {args.corpus}")
    corpus_sha = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    adapter = _build_adapter(args.system, run_id)
    itens = accuracy_items(corpus_path)
    hmap = question_haystack_ids(corpus_path)
    adapter.ingest_all(itens)
    perguntas = scored_questions(corpus_path)
    cliente = ZaiEndpoint()
    cell = run_accuracy_cell(
        adapter,
        perguntas,
        budget=args.budget,
        tokenizer="cl100k_base",
        cliente=cliente,
        checkpoint=Path(args.checkpoint) if args.checkpoint else None,
        seeding={"items": len(itens), "scope": "per-question-haystack-fenced"},
        corpus_total=len(perguntas),
        haystack_ids=hmap,
        corpus_sha256=corpus_sha,
    )
    Path(args.out).write_text(
        json.dumps(cell, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return cell


def run_abstain_cmd(args: argparse.Namespace) -> dict[str, Any]:
    """Arm A.3: seeds the abstention corpus and runs the `_abs` probes."""
    corpus_path = Path(args.corpus)
    if not corpus_path.is_file():
        raise EvalRefusal(f"corpus missing: {args.corpus}")
    corpus_sha = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    adapter = _build_adapter(args.system, run_id)
    itens = accuracy_items(corpus_path)
    hmap = question_haystack_ids(corpus_path)
    adapter.ingest_all(itens)
    probes = abstention_probes(corpus_path)
    linha = run_abstain_cell(
        adapter,
        probes,
        haystack_ids=hmap,
        corpus_sha256=corpus_sha,
    )
    linha["seeding"] = {"items": len(itens), "scope": "per-question-haystack-fenced"}
    Path(args.out).write_text(
        json.dumps(linha, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return linha
