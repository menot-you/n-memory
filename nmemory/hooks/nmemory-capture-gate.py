#!/usr/bin/env python3
"""Stop hook: block turn completion when an owner correction or decision was not captured.

Runs on main owner sessions only. When the latest owner message states a correction
or decision, this hook checks the transcript after that message for an acknowledged
nmemory_capsule_ingest call with authority_class="user-stated" and a verbatim
source_quote from that owner message. Until that capture succeeds in the active
project, every Stop attempt returns decision="block".

Fail-open on missing files, malformed payloads, or timeouts.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import signal
import sys
from pathlib import Path
from types import ModuleType

DEADLINE = 4.0
INPUT_LIMIT = 1_000_000
TAIL_BYTES = 2_000_000
MIN_QUOTE_CHARS = 12
CAPSULE_ID = re.compile(r"^cap-\d+$")
INGEST_TOOL = re.compile(r"^(?:mcp__[A-Za-z0-9_.-]+__)?nmemory_capsule_ingest$")

INJECTED = re.compile(
    r"(<|Another Claude session|\[Request interrupted|This session is being continued"
    r"|Caveat:|\[SYSTEM|\[sent by|\[Workflow harness|From claude-main|Stop hook feedback"
    r"|The [\w-]+ plugin sent|Your claude\.ai usage limit|# AGENTS\.md instructions)",
)
PASTE = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.DOTALL)
IMAGE = re.compile(r"\[Image #\d+\]")
QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
NOT_TYPED = frozenset({"suggestion_accepted", "system"})

BACKREF = re.compile(
    r"\bj[aá]\s+(te\s+|lhe\s+|vos\s+|tinha\s+|havia\s+|tinhamos\s+|tínhamos\s+)?"
    r"(falei|disse|pedi|decidi|expliquei|mandei|avisei|ensinei|corrigi|respondi|defini|"
    r"definimos|decidimos|combinamos|falamos|conversamos|discutimos|aprovei|autorizei|"
    r"escolhi|repeti|registrei|registramos|orientei|reclamei|falado|dito|pedido|decidido)\b"
    r"|\bj[aá]\s+(foi|est[aá]|t[aá]|era|tinha sido)\s+(decidid|definid|combinad|registrad|"
    r"aprovad|falad|explicad|pedid|na mem|no nmemory|no claude)"
    r"|\bcomo\s+(eu\s+)?(j[aá]\s+)?(te\s+|tinha\s+)?(disse|falei|pedi|expliquei|mandei|avisei)\b"
    r"|\b(eu\s+)?(te\s+|j[aá]\s+te\s+)(disse|falei|pedi|avisei|expliquei|mandei)\b"
    r"|\b(igual|conforme|que\s+nem|n[aã]o\s+[eé]\s+o\s+que|exatamente\s+o\s+que)\s+(eu\s+)?"
    r"(disse|falei|pedi|mandei)\b"
    r"|\beu\s+(disse|falei|pedi|avisei|expliquei|mandei|reclamei)\s+(antes|ontem|v[aá]rias|mil|"
    r"diversas|isso|pra\s+n[aã]o|para\s+n[aã]o|que\s+n[aã]o)\b"
    r"|\bquantas\s+vezes\b|\bmil\s+vezes\b|\bv[aá]rias\s+vezes\b"
    r"|\brepet(indo|o)\b|\bvou\s+(te\s+)?repetir\b"
    r"|\bI\s+(already\s+|have\s+already\s+|'ve\s+already\s+|'ve\s+)?(told|said|asked|explained|decided)\b"
    r"|\bas\s+I\s+(said|told|asked|mentioned)\b|\bhow\s+many\s+times\b"
    r"|\bwe\s+(already\s+)?(decided|agreed)\b",
    re.IGNORECASE,
)
FORGOT = re.compile(
    r"\b(voc[eê]\s+|vc\s+)?(j[aá]\s+)?esque(ceu|ceram|cendo)\b"
    r"|\bn[aã]o\s+(se\s+)?lembr(a|ou|aram)\b"
    r"|\b(tenho|tive)\s+que\s+(ficar\s+)?(pedir|falar|repetir|insistir|lembrar)\b"
    r"|\binsistindo\b|\byou\s+forgot\b|\bI\s+keep\s+(telling|saying|asking)\b",
    re.IGNORECASE,
)
AGAIN = re.compile(
    r"\b(de\s+novo|outra\s+vez|novamente|mais\s+uma\s+vez|again)\b", re.IGNORECASE
)
REPROACH = re.compile(
    r"\b(voc[eê]|vc|voces|vocês|agent|agente|teatro|furando|"
    r"errad[oa]s?|erro|problema|nunca|sempre|tamb[eé]m|cad[eê]|preocupad[oa]|"
    r"ignor\w+|faltou|deixou)\b|\?\?",
    re.IGNORECASE,
)
DIRECTIVE = re.compile(
    r"\b(a\s+partir\s+de\s+agora|regra\s*:|sempre\s+use|nunca\s+use|"
    r"(?:fica\s+decidido|[eé]\s+canon|from\s+now\s+on\s+always)\b|standing\s+rule\s*:)",
    re.IGNORECASE,
)
SENTENCE = re.compile(r"[^.\n;]+[.\n;]?")


def _renamed() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "nott_hooks_renamed", Path(__file__).resolve().parent / "renamed.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Deadline(BaseException):
    """Alarm boundary for fail-open exit."""


def _expire(_signum: int, _frame: object) -> None:
    raise Deadline


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def owner_turn(row: dict[str, object]) -> tuple[str, str] | None:
    """Return (raw_text, words_text) when row is a real owner turn, else None."""
    rtype = row.get("type")
    if rtype == "response_item":
        payload = row.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "message"
            or payload.get("role") != "user"
        ):
            return None
        content: object = payload.get("content")
    elif rtype == "user":
        if (
            row.get("isMeta")
            or row.get("isSidechain")
            or row.get("isCompactSummary")
            or row.get("isVisibleInTranscriptOnly")
            or row.get("toolUseResult") is not None
        ):
            return None
        origin = row.get("origin")
        if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
            return None
        if row.get("promptSource") in NOT_TYPED:
            return None
        msg = row.get("message")
        content = msg.get("content") if isinstance(msg, dict) else msg
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return None
    else:
        return None
    raw = _extract_text(content).strip()
    if not raw:
        return None
    unpasted = IMAGE.sub("", PASTE.sub("", raw)).strip()
    if not unpasted or INJECTED.match(unpasted):
        return None
    words = QUOTE_LINE.sub("", unpasted).strip()
    if not words or INJECTED.match(words):
        return None
    return raw, words


def needs_capture(words: str) -> bool:
    """True when the owner text states a correction or standing rule."""
    if BACKREF.search(words) or FORGOT.search(words) or DIRECTIVE.search(words):
        return True
    return any(
        AGAIN.search(sentence) and REPROACH.search(sentence)
        for sentence in SENTENCE.findall(words)
    )


def owner_session(payload: dict[str, object]) -> bool:
    """True for a main owner session.

    False for a subagent, an nSPAWN child, an sdk-cli probe or an Agent SDK run in
    Python (sdk-py): these have no owner. cli and sdk-ts (the owner's Guara nodes)
    stay owner sessions.
    """
    if payload.get("agent_id"):
        return False
    nspawn_sid = os.environ.get("NOTT_NSPAWN_SESSION_ID", "")
    if nspawn_sid and payload.get("session_id") == nspawn_sid:
        return False
    return not (
        os.environ.get("NOTT_NSPAWN_RUN_ID")
        or os.environ.get("CLAUDE_CODE_ENTRYPOINT") in ("sdk-cli", "sdk-py")
    )


def _config_project(cfg_path: Path) -> str | None:
    import tomllib

    with contextlib.suppress(OSError, ValueError):
        doc = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        for candidate in (
            doc.get("project"),
            (doc.get("github") or {}).get("project")
            if isinstance(doc.get("github"), dict)
            else None,
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _active_project(cwd: object) -> str:
    env_proj = (_renamed().advisory("NOTT_NMEMORY_PROJECT") or "").strip()
    if env_proj:
        return env_proj
    if isinstance(cwd, str) and cwd.strip():
        path = Path(cwd.strip())
        for candidate in (path, *path.parents):
            cfg = candidate / ".nott/nmemory/config.toml"
            if cfg.is_file():
                configured = _config_project(cfg)
                if configured:
                    return configured
            if candidate.name == "pynott":
                return "pynott"
        if path.name:
            return path.name
    return "pynott"


def _allowed_project(project_id: object, cwd: object) -> bool:
    if project_id is None:
        return True
    if not isinstance(project_id, str):
        return False
    pid = project_id.strip()
    if not pid:
        return True
    active = _active_project(cwd)
    return pid == active or pid.startswith(active + "/")


def _quote_matches(source_quote: object, raw: str, words: str) -> bool:
    if not isinstance(source_quote, str):
        return False
    quote = source_quote.strip()
    required = min(MIN_QUOTE_CHARS, len(words.strip()))
    if len(quote) < max(1, required):
        return False
    return quote in raw or quote in words


def _latest_owner_turn(
    path: Path,
) -> tuple[tuple[str, str], list[bytes]] | None:
    after_lines: list[bytes] = []
    carry = b""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        while end > 0 or carry:
            start = max(0, end - TAIL_BYTES)
            handle.seek(start, os.SEEK_SET)
            chunk = handle.read(end - start) + carry
            end = start
            raw_lines = chunk.split(b"\n")
            if start > 0:
                carry = raw_lines[0]
                raw_lines = raw_lines[1:]
            else:
                carry = b""
            for raw_line in reversed(raw_lines):
                line = raw_line.strip()
                if not line:
                    continue
                if b'"user"' in line and b'"tool_result"' not in line:
                    try:
                        parsed = json.loads(line.decode("utf-8", errors="replace"))
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        turn = owner_turn(parsed)
                        if turn is not None:
                            after_lines.reverse()
                            return turn, after_lines
                if (
                    b"nmemory_capsule_ingest" in line
                    or b"tool_result" in line
                    or b"function_call_output" in line
                ):
                    after_lines.append(line)
    return None


def _items_from_input(inp: dict[str, object]) -> list[dict[str, object]]:
    raw_items = inp.get("items")
    if isinstance(raw_items, list):
        return [it for it in raw_items if isinstance(it, dict)]
    return [inp]


def _valid_ingest_receipt(
    items: list[dict[str, object]],
    text: str,
    raw: str,
    words: str,
    cwd: object,
) -> bool:
    try:
        reply = json.loads(text)
    except ValueError:
        return False
    if not isinstance(reply, dict):
        return False
    outcomes = reply.get("outcomes")
    if not isinstance(outcomes, list):
        return False
    for idx, item in enumerate(items):
        if idx >= len(outcomes) or not isinstance(outcomes[idx], dict):
            continue
        outcome = outcomes[idx]
        status = outcome.get("status")
        cid = outcome.get("id")
        if status not in ("captured", "deduplicated"):
            continue
        if not isinstance(cid, str) or not CAPSULE_ID.match(cid):
            continue
        if item.get("authority_class") != "user-stated":
            continue
        if not _allowed_project(item.get("project_id"), cwd):
            continue
        if _quote_matches(item.get("source_quote"), raw, words):
            return True
    return False


def _turn_has_valid_capture(
    lines: list[bytes],
    raw: str,
    words: str,
    cwd: object,
) -> bool:
    calls: dict[str, list[dict[str, object]]] = {}
    for line in lines:
        if b"nmemory_capsule_ingest" not in line and not any(
            cid.encode("utf-8") in line for cid in calls
        ):
            continue
        try:
            row = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        rtype = row.get("type")
        if rtype == "event_msg":
            payload = row.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "item_completed":
                continue
            item = payload.get("item")
            if (
                not isinstance(item, dict)
                or item.get("type") != "McpToolCall"
                or item.get("status") != "completed"
            ):
                continue
            tool = item.get("tool")
            inp = item.get("arguments")
            result = item.get("result")
            if (
                isinstance(tool, str)
                and INGEST_TOOL.match(tool)
                and isinstance(inp, dict)
                and isinstance(result, dict)
                and result.get("isError") is not True
                and _valid_ingest_receipt(
                    _items_from_input(inp),
                    _extract_text(result.get("content")),
                    raw,
                    words,
                    cwd,
                )
            ):
                return True
            continue
        if rtype == "response_item":
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if ptype == "function_call":
                call_id = payload.get("call_id")
                name = payload.get("name")
                raw_args = payload.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except ValueError:
                        raw_args = None
                if (
                    isinstance(call_id, str)
                    and isinstance(name, str)
                    and INGEST_TOOL.match(name)
                    and isinstance(raw_args, dict)
                ):
                    calls[call_id] = _items_from_input(raw_args)
            elif ptype == "function_call_output":
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id in calls:
                    text = _extract_text(payload.get("output"))
                    if _valid_ingest_receipt(calls[call_id], text, raw, words, cwd):
                        return True
            continue
        msg = row.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        if rtype == "assistant":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                call_id = block.get("id")
                name = block.get("name")
                inp = block.get("input")
                if (
                    isinstance(call_id, str)
                    and isinstance(name, str)
                    and INGEST_TOOL.match(name)
                    and isinstance(inp, dict)
                ):
                    calls[call_id] = _items_from_input(inp)
        elif rtype == "user":
            for block in content:
                if (
                    not isinstance(block, dict)
                    or block.get("type") != "tool_result"
                    or block.get("is_error") is True
                ):
                    continue
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or call_id not in calls:
                    continue
                text = _extract_text(block.get("content"))
                if _valid_ingest_receipt(calls[call_id], text, raw, words, cwd):
                    return True
    return False


def _snippet_of(words: str) -> str:
    for line in words.splitlines():
        clean = line.strip()
        if len(clean) >= MIN_QUOTE_CHARS:
            return clean[:100]
    return words.strip()[:100]


def evaluate(raw_stdin: str) -> str:
    if len(raw_stdin) > INPUT_LIMIT:
        return ""
    payload = json.loads(raw_stdin)
    if not isinstance(payload, dict) or not owner_session(payload):
        return ""
    tpath_str = payload.get("transcript_path")
    if not isinstance(tpath_str, str) or not tpath_str or "/subagents/" in tpath_str:
        return ""
    tpath = Path(tpath_str)
    if not tpath.is_file():
        return ""
    found = _latest_owner_turn(tpath)
    if found is None:
        return ""
    (raw_text, words_text), after_rows = found
    if not needs_capture(words_text):
        return ""
    cwd = payload.get("cwd")
    if _turn_has_valid_capture(after_rows, raw_text, words_text, cwd):
        return ""
    active_project = _active_project(cwd)
    snippet = _snippet_of(words_text)
    reason = (
        "Persist the owner correction or decision in nMEMORY before you end this turn. "
        'Call nmemory_capsule_ingest with authority_class="user-stated", '
        f'project_id="{active_project}", and source_quote set to a verbatim substring of '
        f"the owner message (for example: {json.dumps(snippet, ensure_ascii=False)}). "
        'Wait for status="captured" or status="deduplicated".'
    )
    return json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False)


def main() -> int:
    signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, DEADLINE)
    try:
        out = evaluate(sys.stdin.read(INPUT_LIMIT + 1))
    except BaseException:  # noqa: BLE001 — fail-open boundary for Stop hook
        out = ""
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    if out:
        with contextlib.suppress(BaseException):
            sys.stdout.write(out + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    os._exit(main())
