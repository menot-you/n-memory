"""The per-call nSPAWN driver: one Supervisor serving one dispatch.

`nspawn_run_start`, `nspawn_review_start` and every `nspawn_batch_start` entry are served by a
driver process, `<sys.executable> -P -m nspawn.cli dispatch run`, launched in its
own transient systemd scope (`nspawn-drive-<launch_uuid>`, under the weighted
`nottcontroller.slice` custody parent) with its own session.
The stdio process hands it one JSON frame on stdin and reads one ack from a pipe
it passed down; the driver also persists that ack as `drivers/<uuid>.ack`. From
admission to terminal the run belongs to the driver, so the stdio process owns
no child and a client that dies — EOF, SIGHUP, SIGKILL, a killed scope — takes
no run with it. `nspawn_route_ping` is the stated exemption and stays in-process. On macOS the
driver is a launchd job instead (`LaunchdLauncher`), with the ack through a FIFO.

The seam is `serve(frame, ack_writer, *, supervisor_factory, owns_supervisor)`.
The driver passes `_default_supervisor` and owns it: after the ack it joins the
run's worker, shuts the supervisor down, and only then stops the close watcher,
which serves one last pending close after `remove_instance`. A McpServer built
with an injected supervisor uses `InProcessLauncher` instead: `serve` on a
thread, the same frame crossing as JSON, the injected supervisor never shut
down by the double.

Bounds: the dedup lock times out at 60 s, under the 180 s ack wait, so a
refused lock still acks. The stdio never kills a driver. With no ack by the
deadline it answers from `.ack` (`ack_late`), else `LaunchFailed` when the
driver has exited, else `AdmissionPending` — which `nspawn_run_show` resolves by
`launch_uuid`.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import plistlib
import queue
import select
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import nott_database

from . import config
from .child import CALLER_SOCKET_ENV
from .child import (
    RUN_ID_ENV as RUN_ID_ENV,  # noqa: PLC0414 - re-exported with the driver it marks
)
from .custody import (
    CUSTODY_SLICE,
    ControllerRefusal,
    user_manager_environment,
    weigh_custody_parent,
)
from .errors import UNKNOWN_CODE, NSpawnError, nerr_of
from .process_identity import (
    ProcessIdentity,
    ProcessIdentityError,
    capture_process_identity,
    process_identity_matches,
)
from .registry import NODE_PREFIX, STORE_ERRORS, DriverCeiling, KeyLock, RunKey
from .rows import RunPhase
from .supervisor import (
    DRIVE_CALL,
    HOST_CONTROL_REFUSAL,
    AdmissionHeld,
    AdmissionUnbound,
    DriveCall,
    IdempotencyConflict,
    InvalidRunRequest,
    KeyFenced,
    KeyTaken,
    RunAccepted,
    Supervisor,
    SupervisorError,
    UnknownRun,
    _default_review_prompt,
    join_workers,
)

#: How long the stdio waits for a driver's ack. Never a kill: past it the
#: answer is read from `.ack`, or reported pending or failed.
ACK_WAIT_S = 180.0
#: The dedup key lock's timeout, kept under the ack wait so a refusal still acks.
LOCK_TIMEOUT_S = 60.0
#: Live drivers plus reservations the host admits; each is one process holding
#: its own connection to the registry file.
DRIVER_CEILING = 40
#: The close watcher's period.
WATCH_INTERVAL_S = 1.0
SYSTEMD_RUN = "/usr/bin/systemd-run"
#: `direct` launches without a scope; honoured only against a `_pytest` store.
LAUNCH_MODE_ENV = "NOTT_NSPAWN_DRIVER_LAUNCH"
DRIVE_ARGV = ("-P", "-m", "nspawn.cli", "dispatch", "run")
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class DriverUnavailable(SupervisorError):
    """No driver can be launched here: no user manager, no linger, no run root."""


def wire_code(error: BaseException) -> int:
    """The JSON-RPC code a refusal renders with: typed entries are the caller's."""
    if isinstance(error, (AdmissionHeld, AdmissionUnbound)):
        return INVALID_PARAMS
    code = getattr(getattr(error, "entry", None), "code", UNKNOWN_CODE)
    return INVALID_PARAMS if code != UNKNOWN_CODE else INTERNAL_ERROR


def drivers_dir(run_root: Path) -> Path:
    return run_root / "drivers"


#: Set by a remote host's (a Mac's) ssh launch of this stdio: that session's id.
REMOTE_SESSION_ENV: Final = "NOTT_NSPAWN_REMOTE_SESSION"
#: Set by the host on every non-centre node's nSPAWN: that node's session id.
NODE_SESSION_ENV: Final = "NOTT_NSPAWN_NODE_SESSION"
#: Set to `read` by the host on an audit parent's nSPAWN: caged dispatch only.
GRANT_ENV: Final = "NOTT_NSPAWN_GRANT"


def _session_id(name: str, value: str) -> str:
    """`value` when it is a canonical session uuid; a refusal naming `name`."""
    try:
        canonical = str(uuid.UUID(value)) == value
    except ValueError:
        canonical = False
    if not canonical:
        raise DriverUnavailable(f"{name} is not a session id")
    return value


def node_owner(environ: Mapping[str, str]) -> str | None:
    """`node:<session>` on a non-centre node's nSPAWN, else None: the owner of
    every run it admits, and the only runs it shows, reads, closes and lists."""
    session = environ.get(NODE_SESSION_ENV)
    if session is None:
        return None
    return NODE_PREFIX + _session_id(NODE_SESSION_ENV, session)


def read_grant(environ: Mapping[str, str]) -> bool:
    """Whether this nSPAWN serves an audit parent (`NOTT_NSPAWN_GRANT=read`)."""
    grant = environ.get(GRANT_ENV)
    if grant is None:
        return False
    if grant != "read":
        raise DriverUnavailable(f"{GRANT_ENV} names no grant but 'read'")
    return True


def _this_stdio(addr: str) -> dict[str, Any]:
    """A caller keyed to THIS stdio, the only local trace of a caller with no socket."""
    me = os.getpid()
    return {
        "addr": addr,
        "pid": me,
        "start_ticks": capture_process_identity(me).start_ticks,
    }


def caller_identity(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """The Claude Code session that owns the messaging socket, or no caller.

    It is the stdio's PARENT, cross-checked against the `<pid>.sock` name —
    never this process, which the session can restart while it lives.

    A non-centre node is the caller ahead of any socket it inherited: its row is
    `node:<session>`, which only the host's `notice take` reads.

    A session on another machine has no socket here. Its row is
    `remote:<session-id>`, keyed to THIS stdio (the only local trace of it):
    `push_ready` never sends to it, and the drain takes it once this stdio is
    gone or the notice has waited NOTICE_STALE_S.
    """
    node = node_owner(environ)
    if node is not None:
        return _this_stdio(node)
    socket = environ.get(CALLER_SOCKET_ENV)
    if not socket:
        session = environ.get(REMOTE_SESSION_ENV)
        if session is None:
            return None
        return _this_stdio(f"remote:{_session_id(REMOTE_SESSION_ENV, session)}")
    parent = os.getppid()
    if Path(socket).name != f"{parent}.sock":
        return None
    try:
        identity = capture_process_identity(parent)
    except ProcessIdentityError:
        return None
    return {"addr": socket, "pid": parent, "start_ticks": identity.start_ticks}


def _common_dir(gitdir: Path) -> str:
    """realpath of the common dir a git dir names (`commondir`), else its own."""
    try:
        relative = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return os.path.realpath(gitdir)
    return os.path.realpath(gitdir / relative)


def project_key(cwd: str, environ: Mapping[str, str] | None = None) -> str:
    """What `git rev-parse --path-format=absolute --git-common-dir` answers in
    `cwd`, realpath'd; realpath(cwd) outside a repository.

    GIT_COMMON_DIR and GIT_DIR win over discovery, as they do for git. Read off
    the filesystem, not by running git: a dispatch spawns nothing before
    admission decides it may. The SessionStart drain keys through this same
    function, so a notice and the session that drains it cannot disagree.
    """
    environ = os.environ if environ is None else environ
    here = Path(os.path.realpath(cwd))
    if common := environ.get("GIT_COMMON_DIR"):
        return os.path.realpath(here / common)
    if gitdir := environ.get("GIT_DIR"):
        return _common_dir(here / gitdir)
    for directory in (here, *here.parents):
        marker = directory / ".git"
        if marker.is_dir():
            return _common_dir(marker)
        if marker.is_file():
            with contextlib.suppress(OSError, ValueError):
                line = marker.read_text(encoding="utf-8").strip()
                if line.startswith("gitdir:"):
                    return _common_dir(directory / line.removeprefix("gitdir:").strip())
            return str(here)
    return str(here)


def _caller_project(arguments: Mapping[str, Any]) -> str:
    """The stdio's project; the call's own cwd when the stdio's was deleted
    (a lane worktree removed after its merge), a typed refusal with neither.

    Ceiling: the fallback keys by the CALL's cwd, which may sit in another
    repository than the session's. Its notice then drains in that repository's
    next session, and its idempotency key lives in that project's key space.
    The session's own project is not known once its cwd is gone.
    """
    try:
        return project_key(os.getcwd())
    except OSError:
        cwd = arguments.get("cwd")
        if isinstance(cwd, str) and cwd:
            return project_key(cwd)
        raise DriverUnavailable(
            "the stdio's working directory is gone and the call names no cwd"
        ) from None


def make_frame(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    drivers_dir: Path | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One dispatch as the driver receives it."""
    return {
        "tool": tool,
        "arguments": dict(arguments),
        "launch_uuid": str(uuid.uuid4()),
        "caller": caller_identity(os.environ if environ is None else environ),
        "project": _caller_project(arguments),
        "drivers_dir": None if drivers_dir is None else str(drivers_dir),
    }


# -- the driver side ------------------------------------------------------------


def _log(message: str) -> None:
    sys.stderr.write(f"nspawn.driver: {message}\n")
    sys.stderr.flush()


def refusal_ack(error: BaseException) -> dict[str, Any]:
    """A refusal as an ack; one raised after `admit_run` names its failed run."""
    rpc_message = getattr(error, "rpc_message", None)
    data = getattr(error, "data", None)
    refused: dict[str, Any] = {
        **(data if isinstance(data, dict) else {}),
        "type": type(error).__name__,
        "message": rpc_message if isinstance(rpc_message, str) else str(error),
        "code": getattr(error, "code", INTERNAL_ERROR)
        if isinstance(rpc_message, str)
        else wire_code(error),
    }
    nerr = nerr_of(error)
    if nerr is not None:
        refused["nerr"] = nerr
    ack: dict[str, Any] = {"refused": refused}
    if isinstance(error, SupervisorError) and error.run_id is not None:
        ack["run_id"] = error.run_id
        ack["phase"] = RunPhase.FAILED.value
    return ack


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _requested_prompt(tool: str, arguments: Mapping[str, Any]) -> str:
    prompt = arguments.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if tool == "nspawn_review_start":
        return _default_review_prompt(str(arguments["branch"]), arguments.get("base"))
    return ""


class FingerprintUnavailable(SupervisorError):
    """A keyed review's commits could not be read in time. Retryable: the same
    call again — a guessed fingerprint would answer a spurious conflict."""

    data: dict[str, Any] | None = None


#: A review with no `base` is read against the remote's default branch, else
#: the local `main`: that commit is what a moved default base changes.
DEFAULT_REVIEW_BASES = ("origin/HEAD", "main")


def _branch_head(cwd: str, branch: str, *, strict: bool) -> str | None:
    """The commit a review's branch names now: a moved branch is another request.

    A git that timed out is a retryable refusal when the fingerprint decides a
    key (`strict`), else no commit: an unkeyed fingerprint is never compared.
    """
    try:
        found = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "rev-parse",
                "--verify",
                "--quiet",
                f"{branch}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        if not strict:
            return None
        unavailable = FingerprintUnavailable(
            f"git did not resolve {branch!r} in {cwd} within 10 s; retry the same call"
        )
        unavailable.data = {"retryable": True}
        raise unavailable from error
    except OSError:
        return None
    return found.stdout.strip() or None if found.returncode == 0 else None


def _refuse_nul(arguments: Mapping[str, Any]) -> None:
    """A NUL byte fits neither an argv (git) nor a registry text column, so
    no string argument may carry one; the field is named, its value is not.

    A nested object — the orchestrator binding is one — is read field by field,
    so a coordinate carrying a NUL is refused HERE rather than reaching the
    binding's own validation as an argv this process already composed.
    """
    for name, value in arguments.items():
        if isinstance(value, Mapping):
            _refuse_nul({f"{name}.{key}": item for key, item in value.items()})
            continue
        values = value if isinstance(value, list) else [value]
        if any(isinstance(item, str) and "\x00" in item for item in values):
            raise InvalidRunRequest(f"{name} contains a NUL byte")


def _realpath(field: str, path: str) -> str:
    try:
        return os.path.realpath(path)
    except ValueError as error:  # an embedded NUL byte
        raise InvalidRunRequest(f"{field} is not a path: {error}") from error


def request_fingerprint(
    tool: str,
    arguments: Mapping[str, Any],
    *,
    group: str | None,
    batch: Sequence[str] | None = None,
    strict: bool = True,
) -> str:
    """sha256 of everything that makes a dispatch this dispatch.

    Paths are realpath'd, so a retry through `cwd/` or a symlink is the same
    request; a review also names its base and the commits of its branch and
    base. A batch entry names the whole provider list, so a repeat with more
    providers is a conflict, not extra runs under the same key. Arguments are
    taken as sent: an omitted model, effort or provider and an explicit value
    equal to its default are different requests.
    """
    cwd = str(arguments.get("cwd") or "")
    system_prompt = arguments.get("system_prompt")
    request: dict[str, Any] = {
        "tool": tool,
        "prompt_sha256": _sha256(_requested_prompt(tool, arguments)),
        "read_only": tool != "nspawn_run_start" or bool(arguments.get("read_only")),
        "provider": arguments.get("provider"),
        "model": arguments.get("model"),
        "effort": arguments.get("effort"),
        "execution_class": arguments.get("execution_class"),
        "timeout_ms": int(arguments.get("timeout_ms") or 0),
        "session_id": arguments.get("session_id"),
        "group": group,
        "system_prompt_sha256": None
        if system_prompt is None
        else _sha256(str(system_prompt)),
        "read_paths": sorted(
            _realpath("read_paths", path) for path in arguments.get("read_paths") or ()
        ),
        "cwd": _realpath("cwd", cwd) if cwd else None,
    }
    # Only when true: every key admitted before the mode existed keeps its print.
    if arguments.get("native_write"):
        request["native_write"] = True
    # Only when named, for the same reason; the order of the names is no change.
    if arguments.get("producer"):
        request["producer"] = sorted(arguments["producer"])
    if arguments.get("original_run") is not None:
        request["original_run"] = arguments["original_run"]
    for coordinate in ("activation", "session_operation", "command"):
        if arguments.get(coordinate) is not None:
            request[coordinate] = arguments[coordinate]
    # Only when bound, for the same reason: a default request's key is BYTE
    # IDENTICAL to the one it had before orchestrator turns existed. Every
    # coordinate of a binding is part of the print, so another session, another
    # project, another repository or another runtime is another request rather
    # than this one's retry.
    if arguments.get("orchestrator"):
        request["orchestrator"] = arguments["orchestrator"]
    if arguments.get("inference_profile") is not None:
        request["inference_profile"] = arguments["inference_profile"]
    if batch is not None:
        # One print for the whole batch: the list and the key's index name the
        # entry's provider, and every `key/*` row is compared against this one.
        request["batch"] = list(batch)
        request["provider"] = None
    if tool == "nspawn_review_start":
        branch = str(arguments.get("branch"))
        base = arguments.get("base")
        request["branch"] = branch
        request["base"] = base
        request["branch_head"] = (
            _branch_head(cwd, branch, strict=strict) if cwd else None
        )
        bases = DEFAULT_REVIEW_BASES if base is None else (str(base),)
        request["base_head"] = None
        for name in bases if cwd else ():
            if head := _branch_head(cwd, name, strict=strict):
                request["base_head"] = head
                break
    return _sha256(json.dumps(request, sort_keys=True))


def _reattach(
    lock: KeyLock, call: DriveCall, owner: str | None
) -> dict[str, Any] | None:
    """The key's run as this dispatch's answer, or None when the key is free.

    Its phase is read under the run row's lock. A live run gets this caller's
    pending notices row, committed before the ack; a final one gets none — the
    ack carrying the phase IS the notice. `orphaned` is terminal but not final
    (`update_run` moves it again): its row is written delivered as `orphaned`,
    so the trigger re-arms it if the run reaches another terminal. A run whose
    `owner` is not this supervisor's is never reattached.
    """
    assert call.run_key is not None
    with lock.transaction():
        answer = _reattach_in_transaction(lock, call, owner)
    if answer is not None:
        # The reattach committed: the lock has nothing left to hold.
        lock.commit()
    return answer


def _reattach_in_transaction(
    lock: KeyLock, call: DriveCall, owner: str | None
) -> dict[str, Any] | None:
    assert call.run_key is not None
    if other := lock.batch_conflict(call.run_key.fingerprint):
        conflict = IdempotencyConflict(
            f"idempotency key {lock.batch_key!r} already names run {other}, "
            "admitted for a different batch; use another key for this one"
        )
        conflict.data = {"run_id": other}
        raise conflict
    hit = lock.find()
    if hit is None:
        if lock.fenced():
            raise KeyFenced(
                f"idempotency key {lock.key!r} was released naming no run; "
                "nothing is admitted under it"
            )
        return None
    if hit.fingerprint != call.run_key.fingerprint:
        conflict = IdempotencyConflict(
            f"idempotency key {lock.key!r} already names run {hit.run_id}, "
            "admitted for a different request; use another key for this one"
        )
        conflict.data = {"run_id": hit.run_id}
        raise conflict
    if hit.owner != owner:
        # The owner is immutable, and another owner's run is not named.
        raise IdempotencyConflict(
            f"idempotency key {lock.key!r} already names a run another caller "
            "admitted; use another key for this one"
        )
    notice = call.notice()
    orphaned = hit.phase is RunPhase.ORPHANED
    if notice is not None and (orphaned or not hit.phase.is_terminal()):
        lock.reattach(hit.run_id, notice, orphaned=orphaned)
    return {"run_id": hit.run_id, "deduplicated": True, "phase": hit.phase.value}


def _release(lock: KeyLock | None) -> None:
    """After admission: the admission committed, so the key lock is let go."""
    if lock is not None:
        lock.commit()


def _dispatch(
    supervisor: Supervisor, tool: str, arguments: Mapping[str, Any]
) -> RunAccepted:
    # The frame crossed as JSON: lists go back to the tuples these methods take.
    kwargs: dict[str, Any] = {
        name: tuple(value) if isinstance(value, list) else value
        for name, value in arguments.items()
    }
    method: Callable[..., RunAccepted]
    if tool == "nspawn_run_start":
        method, head = supervisor.execute, "prompt"
    elif tool == "nspawn_review_start":
        method, head = supervisor.review, "branch"
    else:
        raise DriverUnavailable(f"a driver serves no tool named {tool!r}")
    # A frame can arrive on stdin, past the MCP door's schema: a field the verb does
    # not take is refused by name, never forwarded.
    taken = inspect.signature(method).parameters
    unknown = sorted(set(kwargs) - set(taken))
    if unknown and not any(p.kind is p.VAR_KEYWORD for p in taken.values()):
        raise InvalidRunRequest(f"{tool} takes no {', '.join(unknown)}")
    return method(kwargs.pop(head, None), kwargs.pop("cwd"), **kwargs)


def _persist_ack(frame: Mapping[str, Any], ack: Mapping[str, Any]) -> None:
    """`drivers/<uuid>.ack`, 0600, whatever the answer. A failure is logged only."""
    directory = frame.get("drivers_dir")
    if not isinstance(directory, str):
        return
    target = Path(directory) / f"{frame['launch_uuid']}.ack"
    staging = target.with_suffix(".ack.tmp")
    try:
        Path(directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(ack, stream)
        os.replace(staging, target)
    except OSError as error:
        _log(f"the ack of launch {frame['launch_uuid']} was not persisted: {error}")


def _write_ack(writer: Callable[[dict[str, Any]], None], ack: dict[str, Any]) -> None:
    """A lost ack pipe is ignored: the driver keeps driving; `.ack` answers."""
    try:
        writer(ack)
    except OSError as error:
        _log(f"the ack was not delivered: {error}")


class _CloseWatcher:
    """Serves `run_control` close requests for this driver's run, every second.

    `stop` ends the loop and waits out a close it is serving, so that close's
    outcome is recorded while this instance is still registered: recorded
    after `remove_instance`, the requester reads the claim as a dead
    instance's and closes the run again under its own name. `finish` then
    serves one last pending request after `remove_instance`, so a request
    that landed between the terminal push and the exit is still answered.
    """

    def __init__(self, supervisor: Supervisor, call: DriveCall) -> None:
        self._supervisor = supervisor
        self._call = call
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"nspawn-close-{call.launch_uuid}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        run_id = self._call.run_id
        if run_id is None:
            return
        # Any failure is logged and the loop goes on: a watcher that died would
        # leave every later close of this run waiting out its lease.
        try:
            self._supervisor.serve_close_request(run_id)
        except Exception as error:  # noqa: BLE001 - a watcher loop that dies strands every later close; logged, recorded, and the next second retries
            _log(f"close watcher: {type(error).__name__}: {error}")

    def _loop(self) -> None:
        while not self._stop.wait(WATCH_INTERVAL_S):
            self._serve()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def finish(self) -> None:
        self.stop()
        self._serve()


def serve(
    frame: Mapping[str, Any],
    ack_writer: Callable[[dict[str, Any]], None],
    *,
    supervisor_factory: Callable[[], Supervisor],
    owns_supervisor: bool,
) -> dict[str, Any]:
    """Admit one dispatch, ack it, and — owning the supervisor — drive it out.

    The public `execute`/`review` are called, so a fake's overrides are reached;
    per-call data reaches `_admit` through `DRIVE_CALL`. Everything after the
    admission runs inside `finally`: join the worker, retry a refused terminal
    write, end FAILED a run still unended, then shut down, so the terminal push, `implementation.close()` and
    prompt cleanup always run.
    """
    tool = str(frame["tool"])
    arguments = dict(frame["arguments"])
    key = arguments.pop("idempotency_key", None)
    group = arguments.pop("group", None)
    batch = arguments.pop("batch", None)
    project = frame.get("project")
    call = DriveCall(
        launch_uuid=str(frame["launch_uuid"]),
        group=group,
        caller=frame.get("caller"),
        project=project,
    )
    token = DRIVE_CALL.set(call)
    supervisor: Supervisor | None = None
    watcher: _CloseWatcher | None = None
    lock: KeyLock | None = None
    try:
        try:
            _refuse_nul({**arguments, "idempotency_key": key, "group": group})
            supervisor = supervisor_factory()
            if (
                tool == "nspawn_run_start"
                and any(
                    arguments.get(name) is not None
                    for name in ("activation", "session_operation")
                )
                and not supervisor.host_control
            ):
                raise InvalidRunRequest(HOST_CONTROL_REFUSAL)
            if owns_supervisor:
                watcher = _CloseWatcher(supervisor, call)
                watcher.start()
            if isinstance(project, str):
                call.run_key = RunKey(
                    launch_uuid=call.launch_uuid,
                    project=project,
                    idempotency_key=key,
                    fingerprint=request_fingerprint(
                        tool,
                        arguments,
                        group=group,
                        batch=batch,
                        strict=key is not None,
                    ),
                    prompt_sha256=_sha256(_requested_prompt(tool, arguments)),
                )
            ack: dict[str, Any] | None = None
            if key is not None and call.run_key is not None:
                # Before any side effect: a second driver of this key waits
                # here, then reads the first one's committed run_keys row.
                lock = supervisor.lock_key(call.run_key.project, key, LOCK_TIMEOUT_S)
                ack = _reattach(lock, call, supervisor.owner)
            if ack is None:
                try:
                    accepted = _dispatch(supervisor, tool, arguments)
                except KeyTaken:
                    # The unique index's backstop: re-read under the lock.
                    ack = (
                        None
                        if lock is None
                        else _reattach(lock, call, supervisor.owner)
                    )
                    if ack is None:
                        raise
                else:
                    _release(lock)
                    ack = {
                        "run_id": accepted.run_id,
                        "deduplicated": False,
                        "phase": accepted.phase.value,
                    }
        except (NSpawnError, ControllerRefusal) as refusal:
            ack = refusal_ack(refusal)
        _persist_ack(frame, ack)
        _write_ack(ack_writer, ack)
        return ack
    finally:
        if lock is not None:
            lock.close()
        DRIVE_CALL.reset(token)
        # A review that fails over hands the call to its successor's worker.
        join_workers(call)
        if owns_supervisor and supervisor is not None:
            try:
                try:
                    # A terminal write the registry refused is retried here, on
                    # a fresh connection: it arms the notice and pushes it.
                    drained = supervisor.publish_pending()
                    # A retried write can hand a review on: wait for that run.
                    join_workers(call)
                    if not drained:
                        _log("a refused terminal write is still unrecorded at exit")
                    elif call.run_id is not None:
                        supervisor.fail_unended(call.run_id)
                        # A refused FAILED write waits in the queue: one retry.
                        drained = supervisor.publish_pending()
                        join_workers(call)
                        if not drained:
                            _log("the driver-exit failed write is still unrecorded")
                # A store error is the one cause a later pass can cure; any
                # other refusal propagates after shutdown, never exit 0.
                except STORE_ERRORS as error:
                    _log(f"the driver-exit failed write raised {type(error).__name__}")
                finally:
                    if watcher is not None:
                        watcher.stop()
                    supervisor.shutdown()
            finally:
                if watcher is not None:
                    watcher.finish()


def _read_all(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _driver_supervisor(launch_uuid: str, *, host_control: bool = False) -> Supervisor:
    from .mcp import _default_supervisor

    supervisor = _default_supervisor(sidecar_kind="driver", host_control=host_control)
    # Registered: the launch reservation has become a live driver instance.
    with supervisor._lock:
        supervisor._registry.release_launch(launch_uuid)
    return supervisor


def ack_writer(frame: Mapping[str, Any]) -> Callable[[dict[str, Any]], None]:
    """Where the ack goes: the inherited `ack_fd` (Linux) or the launch FIFO (macOS).

    The FIFO is opened only to write: the stdio holds its read end from before the
    job starts, and an open with no reader is ENXIO, a lost ack, never a hang.
    """
    ack_fd = frame.get("ack_fd")
    ack_fifo = frame.get("ack_fifo")

    def write(ack: dict[str, Any]) -> None:
        fd = ack_fd
        if isinstance(ack_fifo, str):
            fd = os.open(ack_fifo, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        if not isinstance(fd, int):
            return
        try:
            os.write(fd, json.dumps(ack).encode("utf-8") + b"\n")
        finally:
            os.close(fd)

    return write


def drive_main(argv: Sequence[str]) -> int:
    """The driver process: read the frame, move 0/1/2 to the log, serve it."""
    if list(argv) == ["--check"]:
        return 0
    if list(argv) not in ([], ["--host-control"]):
        _log(
            "usage: nspawn dispatch run [--check | --host-control]. Read the frame from stdin."
        )
        return 2
    try:
        frame = json.loads(_read_all(0))
        launch_uuid = str(uuid.UUID(str(frame["launch_uuid"])))
    except (ValueError, KeyError, TypeError) as error:
        _log(f"the frame is not a driver frame: {type(error).__name__}")
        return 2
    directory = drivers_dir(config.resolve_run_root())
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = os.open(
        directory / f"{launch_uuid}.log",
        os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    for number in (0, 1, 2):
        os.dup2(log, number)
    if log > 2:
        os.close(log)
    identity = capture_process_identity(os.getpid())
    record = {
        "pid": identity.pid,
        "boot_id": identity.boot_id,
        "start_ticks": identity.start_ticks,
        "unit": f"nspawn-drive-{launch_uuid}.scope",
    }
    pidfile = os.open(
        directory / f"{launch_uuid}.pid",
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(pidfile, "w", encoding="utf-8") as stream:
        json.dump(record, stream)
    frame["drivers_dir"] = str(directory)
    # One spelling everywhere: the files above are named by the canonical form.
    frame["launch_uuid"] = launch_uuid
    serve(
        frame,
        ack_writer(frame),
        supervisor_factory=lambda: _driver_supervisor(
            launch_uuid, host_control=list(argv) == ["--host-control"]
        ),
        owns_supervisor=True,
    )
    return 0


# -- the stdio side: launchers ------------------------------------------------


def _launch_refusal(
    kind: str, message: str, launch_uuid: str, **extra: Any
) -> dict[str, Any]:
    return {
        "refused": {
            "type": kind,
            "message": message,
            "code": INTERNAL_ERROR,
            "launch_uuid": launch_uuid,
            **extra,
        }
    }


def _read_persisted(directory: Path | None, launch_uuid: str) -> dict[str, Any] | None:
    if directory is None:
        return None
    with contextlib.suppress(OSError, ValueError):
        decoded = json.loads((directory / f"{launch_uuid}.ack").read_text("utf-8"))
        if isinstance(decoded, dict):
            return decoded
    return None


def settle(
    directory: Path | None,
    launch_uuid: str,
    *,
    exited: bool,
    rc: int | None,
    log: str | None,
) -> dict[str, Any]:
    """The answer when no ack arrived by the deadline. Nothing is killed."""
    persisted = _read_persisted(directory, launch_uuid)
    if persisted is not None:
        return {**persisted, "ack_late": True}
    if exited:
        return _launch_refusal(
            "LaunchFailed",
            f"the driver for launch {launch_uuid} exited before its ack "
            f"(rc={rc}); its log is {log}",
            launch_uuid,
            rc=rc,
            log=log,
        )
    return _launch_refusal(
        "AdmissionPending",
        f"the driver for launch {launch_uuid} has not acknowledged within "
        f"{ACK_WAIT_S:.0f} s and was NOT stopped. Do not dispatch again: "
        f"nspawn_run_show with launch_uuid={launch_uuid} resolves it",
        launch_uuid,
    )


def resolve_launch(
    directory: Path | None,
    launch_uuid: str,
    run_of_launch: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """`nspawn_run_show` by launch_uuid: the `.ack`, then the launch's `run_keys`
    row (committed with its admission, before any ack), or where it stands."""
    try:
        canonical = str(uuid.UUID(launch_uuid))
    except ValueError:
        raise UnknownRun(f"{launch_uuid!r} is not a launch uuid") from None
    persisted = _read_persisted(directory, canonical)
    if persisted is not None:
        return persisted
    if run_of_launch is not None and (run_id := run_of_launch(canonical)) is not None:
        return {"run_id": run_id}
    if directory is not None:
        with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
            recorded = json.loads((directory / f"{canonical}.pid").read_text("utf-8"))
            alive = process_identity_matches(
                ProcessIdentity(
                    pid=int(recorded["pid"]),
                    boot_id=str(recorded["boot_id"]),
                    start_ticks=int(recorded["start_ticks"]),
                )
            )
            return settle(
                directory,
                canonical,
                exited=not alive,
                rc=None,
                log=str(directory / f"{canonical}.log"),
            )
        if (directory / f"{canonical}.log").exists():
            return settle(directory, canonical, exited=False, rc=None, log=None)
    raise UnknownRun(f"no launch named {canonical!r}")


class Launcher(Protocol):
    threads: list[threading.Thread]

    def dispatch(
        self, frames: Sequence[dict[str, Any]], stdio: Supervisor
    ) -> list[dict[str, Any]]: ...


_EXITED = object()


class InProcessLauncher:
    """The double: `serve` on a thread per entry, the frame crossing as JSON.

    Entries launch one after another here — the ack of one is read before the
    next starts — so a double's calls keep their order; the real launcher runs
    them concurrently under one deadline.
    """

    def __init__(
        self, factory: Callable[[], Supervisor], *, owns_supervisor: bool
    ) -> None:
        self.factory = factory
        self.owns_supervisor = owns_supervisor
        self.threads: list[threading.Thread] = []

    def dispatch(
        self, frames: Sequence[dict[str, Any]], stdio: Supervisor | None = None
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + ACK_WAIT_S
        return [self._one(frame, deadline) for frame in frames]

    def _one(self, frame: dict[str, Any], deadline: float) -> dict[str, Any]:
        wire = json.loads(json.dumps(frame))
        answers: queue.Queue[object] = queue.Queue()
        crashed: list[BaseException] = []

        def run() -> None:
            try:
                serve(
                    wire,
                    answers.put,
                    supervisor_factory=self.factory,
                    owns_supervisor=self.owns_supervisor,
                )
            except Exception as error:  # noqa: BLE001 - the in-process stand-in for a driver process dying; reported as LaunchFailed
                crashed.append(error)
            finally:
                answers.put(_EXITED)

        thread = threading.Thread(
            target=run, name=f"nspawn-drive-{wire['launch_uuid']}", daemon=True
        )
        self.threads.append(thread)
        thread.start()
        try:
            answer = answers.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            answer = None
        if isinstance(answer, dict):
            return answer
        directory = wire.get("drivers_dir")
        return settle(
            None if directory is None else Path(directory),
            wire["launch_uuid"],
            exited=answer is _EXITED,
            rc=None,
            log=repr(crashed[0]) if crashed else None,
        )


def _linger_enabled() -> bool:
    """Without linger the user manager, and every drive scope, stops at logout."""
    try:
        found = subprocess.run(
            ["loginctl", "show-user", str(os.geteuid()), "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return found.returncode == 0 and found.stdout.strip() == "yes"


def _direct_launch() -> bool:
    """`NOTT_NSPAWN_DRIVER_LAUNCH=direct`, honoured only against a test store:
    a `NOTT_DATABASE_DIR` that is not this account's live store directory."""
    mode = os.environ.get(LAUNCH_MODE_ENV)
    if not mode:
        return False
    if mode != "direct":
        raise DriverUnavailable(f"{LAUNCH_MODE_ENV}={mode!r} is not a launch mode")
    if os.path.realpath(nott_database.directory()) == os.path.realpath(
        nott_database.live_directory()
    ):
        raise DriverUnavailable(
            f"{LAUNCH_MODE_ENV}=direct is honoured only against a test store "
            f"({nott_database.DIRECTORY_VARIABLE}), never the live one"
        )
    return True


@dataclass
class _Launched:
    frame: dict[str, Any]
    directory: Path
    process: subprocess.Popen[bytes] | None
    ack_fd: int | None
    error: str | None = None
    #: The launchd job (`<domain>/<label>`) a macOS driver runs as.
    service: str | None = None


class SystemdLauncher:
    """The Linux launcher: one scoped driver per entry, acks under one deadline."""

    def __init__(self, *, host_control: bool = False) -> None:
        self.host_control = host_control
        self.threads: list[threading.Thread] = []

    def dispatch(
        self, frames: Sequence[dict[str, Any]], stdio: Supervisor
    ) -> list[dict[str, Any]]:
        try:
            manager = self._environment()
        except (DriverUnavailable, ControllerRefusal, config.ConfigError) as refusal:
            return [
                _launch_refusal("DriverUnavailable", str(refusal), f["launch_uuid"])
                for f in frames
            ]
        uuids = [str(frame["launch_uuid"]) for frame in frames]
        self.threads = [thread for thread in self.threads if thread.is_alive()]
        try:
            with stdio._lock:
                stdio._registry.reserve_launches(
                    uuids,
                    int(time.time() * 1000),
                    ceiling=DRIVER_CEILING,
                    stale_after_ms=stdio.STALE_AFTER_MS,
                    lapse_ms=int(ACK_WAIT_S * 1000),
                )
        except DriverCeiling as refusal:
            return [_launch_refusal("DriverCeiling", str(refusal), u) for u in uuids]
        deadline = time.monotonic() + ACK_WAIT_S
        launched = [self._launch(frame, manager, deadline) for frame in frames]
        answers = [self._collect(one, deadline) for one in launched]
        # Any refusal ends the launch: a driver that refused before its
        # supervisor existed never released the slot itself (one refused after
        # it already did; the DELETE is idempotent). Only AdmissionPending keeps
        # it, for a driver that may still register. A stdio dying first leaves
        # the slot to its lapse; the driver does not release on refusal, since
        # the refusal may be the store being unreachable.
        for answer, launch_uuid in zip(answers, uuids, strict=True):
            refused = answer.get("refused")
            if refused is not None and refused.get("type") != "AdmissionPending":
                with stdio._lock:
                    stdio._registry.release_launch(launch_uuid)
        return answers

    @staticmethod
    def _environment() -> dict[str, str] | None:
        """The bus variables to merge, or None for a direct (unscoped) launch."""
        if _direct_launch():
            return None
        manager = user_manager_environment()
        if not _linger_enabled():
            raise DriverUnavailable(
                f"user linger is off for uid {os.geteuid()}: without it the user "
                "manager, and every drive scope, stops at the last logout"
            )
        weigh_custody_parent(manager)
        return {
            name: manager[name]
            for name in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
        }

    def _launch(
        self, frame: dict[str, Any], manager: dict[str, str] | None, deadline: float
    ) -> _Launched:
        launch_uuid = str(frame["launch_uuid"])
        if not frame.get("drivers_dir"):
            return _Launched(
                frame, Path("."), None, None, "the stdio holds no run root"
            )
        directory = Path(frame["drivers_dir"])
        # A failure here refuses THIS entry: raised, it would lose the answers
        # of the batch entries already launched, and leak their ack fds.
        log: int | None = None
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            log = os.open(
                directory / f"{launch_uuid}.log",
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            read_fd, write_fd = os.pipe()
        except OSError as error:
            if log is not None:
                os.close(log)
            return _Launched(
                frame, directory, None, None, f"{type(error).__name__}: {error}"
            )
        argv = [sys.executable, *DRIVE_ARGV]
        if self.host_control:
            argv.append("--host-control")
        if manager is not None:
            argv = [
                SYSTEMD_RUN,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit=nspawn-drive-{launch_uuid}",
                f"--slice={CUSTODY_SLICE}",
                "--",
                *argv,
            ]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                pass_fds=(write_fd,),
                start_new_session=True,
                env={**os.environ, **(manager or {})},
            )
        except OSError as error:
            os.close(read_fd)
            return _Launched(
                frame, directory, None, None, f"{type(error).__name__}: {error}"
            )
        finally:
            # The stdio keeps no copy of the write end: the driver's exit is EOF.
            os.close(write_fd)
            os.close(log)
        payload = json.dumps({**frame, "ack_fd": write_fd}).encode("utf-8")
        reaper = threading.Thread(
            target=self._feed_and_reap,
            args=(process, payload),
            name=f"nspawn-reap-{launch_uuid}",
            daemon=True,
        )
        self.threads.append(reaper)
        reaper.start()
        return _Launched(frame, directory, process, read_fd)

    @staticmethod
    def _feed_and_reap(process: subprocess.Popen[bytes], payload: bytes) -> None:
        """Write the frame, close stdin, and reap: with --scope it is the driver."""
        stdin = process.stdin
        if stdin is not None:
            with contextlib.suppress(OSError):
                stdin.write(payload)
            with contextlib.suppress(OSError):
                stdin.close()
        process.wait()

    @staticmethod
    def _read_ack(fd: int, deadline: float) -> dict[str, Any] | None:
        buffer = b""
        try:
            while b"\n" not in buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    return None
                chunk = os.read(fd, 65536)
                if not chunk:
                    return None
                buffer += chunk
        finally:
            os.close(fd)
        with contextlib.suppress(ValueError):
            decoded = json.loads(buffer.split(b"\n", 1)[0])
            if isinstance(decoded, dict):
                return decoded
        return None

    def _collect(self, launched: _Launched, deadline: float) -> dict[str, Any]:
        launch_uuid = str(launched.frame["launch_uuid"])
        log = str(launched.directory / f"{launch_uuid}.log")
        if launched.process is None or launched.ack_fd is None:
            return _launch_refusal(
                "LaunchFailed",
                f"the driver for launch {launch_uuid} did not start: {launched.error}",
                launch_uuid,
                rc=None,
                log=log,
            )
        ack = self._read_ack(launched.ack_fd, deadline)
        if ack is not None:
            return ack
        with contextlib.suppress(subprocess.TimeoutExpired):
            launched.process.wait(
                timeout=max(0.0, min(2.0, deadline - time.monotonic()))
            )
        rc = launched.process.poll()
        return settle(
            launched.directory, launch_uuid, exited=rc is not None, rc=rc, log=log
        )


#: The launchd client; a job is bootstrapped into the user's GUI domain, else user.
LAUNCHCTL = "/bin/launchctl"
#: How often the stdio asks launchd whether a driver job has exited.
JOB_POLL_S = 0.5
#: How often a reaper asks the same, for the job's whole run.
REAP_INTERVAL_S = 2.0


def driver_plist(
    label: str,
    argv: Sequence[str],
    environment: Mapping[str, str],
    *,
    stdin: Path,
    log: Path,
) -> dict[str, Any]:
    """One driver as a launchd job: run once at load, never relaunched."""
    return {
        "Label": label,
        "ProgramArguments": list(argv),
        "EnvironmentVariables": dict(environment),
        "StandardInPath": str(stdin),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "RunAtLoad": True,
        "KeepAlive": False,
    }


def write_private(path: Path, data: bytes) -> None:
    """A new 0600 file; an existing one or a symlink in its place is refused."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _job(service: str) -> dict[str, str] | None:
    """`launchctl print`'s top-level fields; None once the job is not loaded.

    A launchctl that could not answer is an empty dict: nothing is known, so
    nothing is concluded.
    """
    try:
        found = subprocess.run(
            [LAUNCHCTL, "print", service],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return {}
    if found.returncode:
        return None
    fields = {}
    for line in found.stdout.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t") and " = " in line:
            name, value = line.strip().split(" = ", 1)
            fields[name] = value
    return fields


def _job_exited(job: dict[str, str] | None) -> bool:
    return job is None or (
        job.get("state") == "not running" and job.get("runs", "0") != "0"
    )


def _job_rc(job: dict[str, str] | None) -> int | None:
    with contextlib.suppress(KeyError, ValueError):
        return int((job or {})["last exit code"])
    return None


def _decode_ack(buffer: bytes) -> dict[str, Any] | None:
    with contextlib.suppress(ValueError):
        decoded = json.loads(buffer.split(b"\n", 1)[0])
        if isinstance(decoded, dict):
            return decoded
    return None


class LaunchdLauncher(SystemdLauncher):
    """The macOS launcher: one launchd job per driver.

    A job (not a detached child) gets its own resource coalition, which is the
    darwin custody's unit, and outlives the session
    that dispatched it. The job is a generated plist (`RunAtLoad`, `KeepAlive`
    false) bootstrapped into `gui/<uid>`, else `user/<uid>`; the frame arrives
    on stdin from a 0600 file and the environment in the 0600 plist, both in
    the private drivers directory; the ack returns through a FIFO there, since
    a job inherits no descriptor. Its exit is read from `launchctl print`, and
    the job is booted out after it.

    ponytail: a stdio that dies before its driver exits leaves the finished
    job loaded (never relaunched) and its plist on disk; a startup sweep of
    `nspawn-drive-*` jobs is the upgrade path.
    """

    def __init__(
        self, argv: Sequence[str] | None = None, *, host_control: bool = False
    ) -> None:
        super().__init__(host_control=host_control)
        self.argv = [sys.executable, *DRIVE_ARGV] if argv is None else list(argv)
        if host_control:
            self.argv.append("--host-control")
        self._direct = False

    def _environment(self) -> dict[str, str] | None:  # type: ignore[override]
        self._direct = _direct_launch()
        return None

    def _launch(
        self, frame: dict[str, Any], manager: dict[str, str] | None, deadline: float
    ) -> _Launched:
        if self._direct:
            return super()._launch(frame, manager, deadline)
        launch_uuid = str(frame["launch_uuid"])
        if not frame.get("drivers_dir"):
            return _Launched(
                frame, Path("."), None, None, "the stdio holds no run root"
            )
        directory = Path(frame["drivers_dir"])
        label = f"nspawn-drive-{launch_uuid}"
        log = directory / f"{launch_uuid}.log"
        fifo, stdin, plist = _launch_files(directory, launch_uuid)
        read_fd: int | None = None
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.close(
                os.open(
                    log,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                    0o600,
                )
            )
            os.mkfifo(fifo, 0o600)
            read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            write_private(
                stdin, json.dumps({**frame, "ack_fifo": str(fifo)}).encode("utf-8")
            )
            write_private(
                plist,
                plistlib.dumps(
                    driver_plist(label, self.argv, os.environ, stdin=stdin, log=log)
                ),
            )
            service = f"{self._bootstrap(plist)}/{label}"
        except (OSError, ValueError, DriverUnavailable) as error:
            if read_fd is not None:
                os.close(read_fd)
            _remove(fifo, stdin, plist)
            return _Launched(
                frame, directory, None, None, f"{type(error).__name__}: {error}"
            )
        return _Launched(frame, directory, None, read_fd, service=service)

    @staticmethod
    def _bootstrap(plist: Path) -> str:
        """The domain that took the job; each refusal is named when none does."""
        refusals = []
        for domain in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
            try:
                found = subprocess.run(
                    [LAUNCHCTL, "bootstrap", domain, str(plist)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                refusals.append(f"{domain}: {error}")
                continue
            if found.returncode == 0:
                return domain
            refusals.append(f"{domain}: {found.stderr.strip()[:256]}")
        raise DriverUnavailable(f"launchd refused the driver job: {refusals}")

    @staticmethod
    def _reap(service: str, paths: Sequence[Path]) -> None:
        """Wait out the job, boot it out, and remove its launch files."""
        while not _job_exited(_job(service)):
            time.sleep(REAP_INTERVAL_S)
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                [LAUNCHCTL, "bootout", service],
                capture_output=True,
                timeout=30,
                check=False,
            )
        _remove(*paths)

    def _collect(self, launched: _Launched, deadline: float) -> dict[str, Any]:
        if launched.service is None or launched.ack_fd is None:
            return super()._collect(launched, deadline)
        launch_uuid = str(launched.frame["launch_uuid"])
        buffer = b""
        job: dict[str, str] | None = {}
        next_poll = 0.0
        fd = launched.ack_fd
        try:
            while b"\n" not in buffer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                chunk: bytes | None = None
                if select.select([fd], [], [], min(remaining, JOB_POLL_S))[0]:
                    with contextlib.suppress(BlockingIOError):
                        chunk = os.read(fd, 65536)
                if chunk:
                    buffer += chunk
                    continue
                if chunk == b"":
                    # EOF: no writer holds the FIFO now; wait, do not spin.
                    time.sleep(0.05)
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + JOB_POLL_S
                    job = _job(launched.service)
                    if _job_exited(job):
                        # An ack written just before the exit is still queued.
                        with contextlib.suppress(OSError):
                            buffer += os.read(fd, 65536)
                        break
        finally:
            os.close(fd)
            # Only now: a bootout before this read would erase the exit code.
            reaper = threading.Thread(
                target=self._reap,
                args=(
                    launched.service,
                    _launch_files(launched.directory, launch_uuid),
                ),
                name=f"nspawn-reap-{launch_uuid}",
                daemon=True,
            )
            self.threads.append(reaper)
            reaper.start()
        ack = _decode_ack(buffer)
        if ack is not None:
            return ack
        return settle(
            launched.directory,
            launch_uuid,
            exited=_job_exited(job),
            rc=_job_rc(job),
            log=str(launched.directory / f"{launch_uuid}.log"),
        )


def _launch_files(directory: Path, launch_uuid: str) -> tuple[Path, Path, Path]:
    """The ack FIFO, the frame and the plist of one launchd driver job."""
    return (
        directory / f"{launch_uuid}.ack.fifo",
        directory / f"{launch_uuid}.frame",
        directory / f"nspawn-drive-{launch_uuid}.plist",
    )


def _remove(*paths: Path) -> None:
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def default_launcher(
    platform: str = sys.platform, *, host_control: bool = False
) -> SystemdLauncher:
    """The launcher a server without an injected supervisor uses on `platform`."""
    return (
        LaunchdLauncher(host_control=host_control)
        if platform == "darwin"
        else SystemdLauncher(host_control=host_control)
    )
