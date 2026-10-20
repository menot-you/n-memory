"""`nERR-NNNN` errors — the catalog the operator reads, and this package's error root.

`nERR-NNNN::description :: meaning :: action`; the code is the contract. Every
entry below is extracted from the contract rather than retyped, and `classify` keeps
the oracle's rule ORDER because it is first-match-wins.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorEntry:
    """One catalog row: the code is the contract, the three texts are the voice."""

    code: int
    description: str
    meaning: str
    action: str

    def code_str(self) -> str:
        """Four-character zero-padded form, e.g. `1101`."""
        return f"{self.code:04d}"

    def wire(self) -> str:
        """The wire text — `nERR-NNNN::description :: meaning :: action`."""
        return f"nERR-{self.code_str()}::{self.description} :: {self.meaning} :: {self.action}"

    def __str__(self) -> str:
        return self.wire()


class NSpawnError(Exception):
    """The one root of this package's typed exception hierarchy.

    It carries a catalog entry so that any refusal crossing a boundary can be
    read back as `nERR-NNNN`, never as free prose.
    """

    def __init__(self, entry: ErrorEntry, detail: str | None = None) -> None:
        self.entry = entry
        self.detail = detail
        super().__init__(
            entry.wire() if detail is None else f"{entry.wire()} [{detail}]"
        )


def nerr_of(error: BaseException) -> int | None:
    """The catalog code `error` carries, or None when it carries no entry."""
    code = getattr(getattr(error, "entry", None), "code", None)
    return code if isinstance(code, int) else None


#: The unknown-code sentinel. Returned by `from_code` when an integer has no
#: registered entry. An error landing here is a missing variant, never a place
#: to leave production errors.
UNKNOWN_CODE = 9999
UNKNOWN = ErrorEntry(
    code=UNKNOWN_CODE,
    description="unknown error",
    meaning="an error escaped without a registered nERR code",
    action="add the missing variant to mcps/nspawn/src/error.rs and to ERROR-CATALOG.md",
)

# --- 1xxx: nspawn one-shot -----------------------------
NERR_1101 = ErrorEntry(
    code=1101,
    description="no <vendor> binary on this host",
    meaning="the vendor lane the roster names has no executable the one-shot can find",
    action="declare [defaults].{claude,codex,grok,agy}_bin in the roster, or set the provider's own bin",
)
NERR_1102 = ErrorEntry(
    code=1102,
    description="claude version check failed",
    meaning="the version probe on the claude binary failed before any turn was spent",
    action="run `nott doctor --vendor claude` for what version is expected",
)
NERR_1103 = ErrorEntry(
    code=1103,
    description="roster parse failed",
    meaning="the roster at ~/.nott/config.toml could not be parsed",
    action="run `nott doctor`; the doctor names the line and column",
)
NERR_1104 = ErrorEntry(
    code=1104,
    description=("spawn.toml is absent and NOTT_NSPAWN_ROSTER's path does not exist"),
    meaning="the door has no place to read the owner roster from",
    action=(
        "declare [[provider]] in ~/.nott/spawn.toml, or export NOTT_NSPAWN_ROSTER for compatibility"
    ),
)
NERR_1105 = ErrorEntry(
    code=1105,
    description="model not in account's declared set",
    meaning="the model the caller named is not in the chosen account's models array",
    action="pick a model the account offers, or add the model to the roster",
)
NERR_1106 = ErrorEntry(
    code=1106,
    description="advertised value refused by the setter",
    meaning="the picker emitted this model, the operator accepted, the door refused",
    action="refresh the picker; this is emit-half and accept-half disagreeing",
)
NERR_1107 = ErrorEntry(
    code=1107,
    description="no default model",
    meaning="the chosen account declares no default and the caller named none",
    action="set [defaults].model or pass --model explicitly",
)
NERR_1108 = ErrorEntry(
    code=1108,
    description="<vendor> version check failed",
    meaning="the version probe on a non-claude vendor binary failed",
    action="run `nott doctor --vendor <name>`",
)
NERR_1109 = ErrorEntry(
    code=1109,
    description="no codex binary resolved",
    meaning="codex was selected for a turn and no codex binary was found",
    action="set --codex-bin, NSPAWN_CODEX_BIN, or [defaults].codex_bin",
)
NERR_1111 = ErrorEntry(
    code=1111,
    description="session id mint failed",
    meaning="a session id could not be minted before spawn",
    action="retry",
)
NERR_1112 = ErrorEntry(
    code=1112,
    description="run-private home prep failed",
    meaning="the per-turn home could not be staged",
    action="check disk space and $TMPDIR writability",
)
NERR_1113 = ErrorEntry(
    code=1113,
    description="abort requested",
    meaning="the caller cancelled the turn before the vendor answered",
    action="no action; the cancel landed",
)
NERR_1114 = ErrorEntry(
    code=1114,
    description="the turn was aborted",
    meaning="the vendor reported TurnOutcome::Aborted",
    action="inspect the vendor's stderr",
)
NERR_1115 = ErrorEntry(
    code=1115,
    description="the vendor died",
    meaning="the vendor process exited non-zero before producing a reply",
    action="check the vendor's stderr; if wrapped by nott-organs/<name>-pin, verify the wrapper",
)
NERR_1116 = ErrorEntry(
    code=1116,
    description="vendor spawn refused",
    meaning="the vendor's spawn layer refused before the turn started",
    action="see the vendor's describe() output",
)
NERR_1117 = ErrorEntry(
    code=1117,
    description="vendor outcome anomalous",
    meaning="the vendor returned a result that does not match any known good shape",
    action="capture the full reply bytes; this is vendor-shape drift",
)
NERR_1118 = ErrorEntry(
    code=1118,
    description="empty reply",
    meaning="the vendor returned success but the reply body was empty",
    action="check the vendor's stdout; success with empty reply is a vendor bug",
)
NERR_1119 = ErrorEntry(
    code=1119,
    description="<vendor> exited with an unknown status",
    meaning="the vendor's exit code is outside the known mapping",
    action="update the vendor's exit-code table in cli_oneshot.rs",
)
NERR_1120 = ErrorEntry(
    code=1120,
    description="no last-message file",
    meaning="the vendor's codex-exec mode did not write its reply file",
    action="re-run with verbose logging; check vendor support for the requested output mode",
)
NERR_1121 = ErrorEntry(
    code=1121,
    description="rate limited",
    meaning="the vendor refused on quota",
    action="wait the retry window, or rotate vendor",
)
NERR_1124 = ErrorEntry(
    code=1124,
    description="effort not in account's declared ladder",
    meaning="the chosen account declares an effort outside the efforts it offers",
    action="pick an effort the account offers, or add the effort to the roster",
)

NERR_1125 = ErrorEntry(
    code=1125,
    description="provider strength above the class cap",
    meaning="the route this class would take is stronger than the class allows",
    action="declare the class this work is, or raise max_strength in spawn.toml [classes]",
)
NERR_1126 = ErrorEntry(
    code=1126,
    description="native plugin grant refused",
    meaning="the vendor's advertised plugin skills are not the admitted plugin version",
    action="reinstall the plugin so the vendor's installed copy is the admitted one; waiting changes nothing",
)
NERR_1127 = ErrorEntry(
    code=1127,
    description="agy cage refused",
    meaning="a boundary of the OS cage around the AGY process cannot be established or failed its self-test",
    action="read the named boundary; the AGY route stays refused until the cage self-test holds",
)
NERR_1128 = ErrorEntry(
    code=1128,
    description="session read failed",
    meaning="the vendor session reader did not complete",
    action="check the named reason and try the session read again",
)
NERR_1129 = ErrorEntry(
    code=1129,
    description="class-less run outside its fast lane",
    meaning="a run without a class asked for another model or effort than its route's fast lane",
    action="pin the route's fast model at its lowest effort, or declare the class this work is",
)

# --- 2xxx: nspawn process guardian ---------------------
NERR_2001 = ErrorEntry(
    code=2001,
    description="process custody: receipt root not staged",
    meaning="the receipt root could not be staged",
    action="check $TMPDIR and the calling process's write permissions",
)
NERR_2002 = ErrorEntry(
    code=2002,
    description="spawn failed",
    meaning="the vendor child could not be spawned",
    action="check the binary path exists, is executable, and matches the OS architecture",
)
NERR_2003 = ErrorEntry(
    code=2003,
    description="pipe claim failed",
    meaning="the child was spawned but a pipe could not be claimed",
    action="retry",
)
NERR_2004 = ErrorEntry(
    code=2004,
    description="termination timeout",
    meaning="the child exceeded the termination deadline",
    action="the vendor is hung; check the vendor's stderr",
)
NERR_2005 = ErrorEntry(
    code=2005,
    description="vendor exited without clean receipt",
    meaning="the vendor exited but the guardian never saw a clean-receipt file",
    action="the vendor does not cooperate with the guardian protocol; wrap the vendor or unset the receipt-root fallback",
)
NERR_2006 = ErrorEntry(
    code=2006,
    description="process identity not established",
    meaning="the vendor's PID could not be cross-referenced against the spawned child's identity",
    action="retry",
)
NERR_2007 = ErrorEntry(
    code=2007,
    description="process guardian startup failed",
    meaning="the guardian could not bind its startup identity",
    action="the guardian is incompatible with this binary; see nERR-2005",
)
NERR_2008 = ErrorEntry(
    code=2008,
    description="startup bound but no clean receipt",
    meaning="the vendor entered cleanup without writing the receipt file",
    action="see nERR-2005",
)
NERR_2009 = ErrorEntry(
    code=2009,
    description="version-check timeout without clean receipt",
    meaning="a version-check step exceeded its deadline without a clean exit",
    action="see nERR-2005",
)
NERR_2010 = ErrorEntry(
    code=2010,
    description="process identity not established (legacy)",
    meaning="older form of nERR-2006",
    action="see nERR-2006",
)
NERR_2011 = ErrorEntry(
    code=2011,
    description="process guardian failure",
    meaning="catch-all for guardian failures",
    action="run `nott doctor` for the specific failure class",
)

# --- 3xxx: the ACP door --------------------------------
NERR_3101 = ErrorEntry(
    code=3101,
    description="engine answer error",
    meaning="the vendor turn returned an Err reason the door does not know how to render as a chat chunk",
    action="the error text is in data; surface it",
)
NERR_3102 = ErrorEntry(
    code=3102,
    description="session/prompt needs text or a resource link",
    meaning="the prompt array had no text or resource_link entries",
    action="editor sent a malformed prompt; this is a transport bug, not a NOTT bug",
)
NERR_3103 = ErrorEntry(
    code=3103,
    description="session/set_config_option on unknown session",
    meaning="the session id the editor addressed does not match any open session",
    action="editor should call session/new first",
)
NERR_3104 = ErrorEntry(
    code=3104,
    description="session/prompt on unknown session",
    meaning="same as nERR-3103 for prompts",
    action="same; call session/new first",
)
NERR_3105 = ErrorEntry(
    code=3105,
    description="turn failed",
    meaning="the turn returned CliOutcome::Failed(reason)",
    action="see the underlying nERR-1xxx or nERR-2xxx reason",
)
NERR_3106 = ErrorEntry(
    code=3106,
    description="the turn was cancelled",
    meaning="the turn returned Cancelled or a matching Failed(CANCELLED_TURN)",
    action="no action; the cancel landed",
)
NERR_3107 = ErrorEntry(
    code=3107,
    description="cancel on session with no in-flight turn",
    meaning="cancellation was sent but the editor had no active sender",
    action="editor should not cancel a session with no in-flight turn",
)

# --- 4xxx: diagnostics ---------------------------------
NERR_4101 = ErrorEntry(
    code=4101,
    description="tool not in $PATH",
    meaning="the doctor found no executable for the named tool",
    action="install the tool, or accept that the doctor cannot evaluate that axis",
)
NERR_4102 = ErrorEntry(
    code=4102,
    description="credential file not readable",
    meaning="the named token file is missing or 0600-mismatched",
    action="chmod 0600 <path> and ensure the file exists",
)
NERR_4103 = ErrorEntry(
    code=4103,
    description="account not in roster",
    meaning="the operator named an account the roster does not carry",
    action="edit ~/.nott/config.toml to add the account, or pick a different account",
)

#: The catalog as a flat tuple, ordered by code. Adding an entry means adding it
#: here too; `test_errors.py` reads this tuple and rejects code/text drift.
ALL: tuple[ErrorEntry, ...] = (
    NERR_1101,
    NERR_1102,
    NERR_1103,
    NERR_1104,
    NERR_1105,
    NERR_1106,
    NERR_1107,
    NERR_1108,
    NERR_1109,
    NERR_1111,
    NERR_1112,
    NERR_1113,
    NERR_1114,
    NERR_1115,
    NERR_1116,
    NERR_1117,
    NERR_1118,
    NERR_1119,
    NERR_1120,
    NERR_1121,
    NERR_1124,
    NERR_1125,
    NERR_1126,
    NERR_1127,
    NERR_1128,
    NERR_1129,
    NERR_2001,
    NERR_2002,
    NERR_2003,
    NERR_2004,
    NERR_2005,
    NERR_2006,
    NERR_2007,
    NERR_2008,
    NERR_2009,
    NERR_2010,
    NERR_2011,
    NERR_3101,
    NERR_3102,
    NERR_3103,
    NERR_3104,
    NERR_3105,
    NERR_3106,
    NERR_3107,
    NERR_4101,
    NERR_4102,
    NERR_4103,
)


def from_code(code: int) -> ErrorEntry:
    """Look up by integer code, falling back to the `nERR-9999` sentinel."""
    for candidate in ALL:
        if candidate.code == code:
            return candidate
    return UNKNOWN


def classify(reason: str) -> ErrorEntry:
    """Classify a free-form refusal string to its catalog entry.

    FIRST MATCH WINS and the order is the oracle's: `no ... binary` is tested
    before `version check`, and the broad `spawn ` rule is tested last, so a
    reason matching two rules resolves the way the Rust does.
    """
    if "no " in reason and "binary" in reason:
        return NERR_1101
    if "claude binary:" in reason or "version check" in reason:
        return NERR_1102
    if "roster:" in reason:
        return NERR_1103
    if "HOME is not set" in reason:
        return NERR_1104
    if "declares models" in reason:
        return NERR_1105
    if "offers no model" in reason:
        return NERR_1106
    if "no default model" in reason:
        return NERR_1107
    if "run-private home" in reason:
        return NERR_1112
    if "clean receipt" in reason or "guardian" in reason:
        return NERR_2005
    if "spawn " in reason:
        return NERR_2002
    return UNKNOWN
