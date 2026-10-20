"""Deterministic simulated engineering-log world."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

BASE_UNIX_SECONDS: int = 1_767_225_600  # 2026-01-01T00:00:00Z
MAX_EVENTS: int = 1_000_000


@dataclass(frozen=True)
class WorldConfig:
    seed: int
    services: int
    engineers: int
    events: int


class WorldError(Exception):
    """World configuration or generation failure."""


@dataclass
class Entities:
    services: list[str]
    engineers: list[str]
    incidents: list[str] = field(default_factory=list)
    deploys: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Rule:
    id: str
    description: str


class EventKind(StrEnum):
    DEPLOY = "deploy"
    INCIDENT_OPEN = "incident_open"
    OWNERSHIP_TRANSFER = "ownership_transfer"
    RESOLVE = "resolve"


@dataclass(frozen=True)
class Event:
    seq: int
    id: str
    kind: EventKind
    service: str
    version: str
    engineer: str
    incident: str | None = None
    caused_by: str | None = None
    at: str = ""


@dataclass(frozen=True)
class World:
    entities: Entities
    rules: list[Rule]
    events: list[Event]


@dataclass(frozen=True)
class DeployRef:
    id: str
    service: str
    version: str


@dataclass(frozen=True)
class IncidentRef:
    id: str
    service: str
    version: str


def pcg32(state: int) -> tuple[int, bytes]:
    """PCG32 step for rand_core::SeedableRng::seed_from_u64 parity."""
    mul = 6364136223846793005
    inc = 11634580027462260723
    state = (state * mul + inc) & 0xFFFFFFFFFFFFFFFF
    xorshifted = (((state >> 18) ^ state) >> 27) & 0xFFFFFFFF
    rot = (state >> 59) & 0xFFFFFFFF
    rot_val = rot % 32
    if rot_val == 0:
        x = xorshifted
    else:
        x = ((xorshifted >> rot_val) | (xorshifted << (32 - rot_val))) & 0xFFFFFFFF
    return state, x.to_bytes(4, "little")


def seed_from_u64(state: int) -> bytes:
    """Derive 32-byte ChaCha seed using standard rand_core PCG32 initialization."""
    seed = bytearray()
    for _ in range(8):
        state, b = pcg32(state)
        seed.extend(b)
    return bytes(seed)


class ChaCha12Rng:
    """Bit-exact Python equivalent of rand_chacha::ChaCha12Rng in rand 0.9."""

    def __init__(self, seed: int) -> None:
        seed_bytes = seed_from_u64(seed)
        self.key0 = [
            int.from_bytes(seed_bytes[i : i + 4], "little") for i in range(0, 16, 4)
        ]
        self.key1 = [
            int.from_bytes(seed_bytes[i : i + 4], "little") for i in range(16, 32, 4)
        ]
        self.block_pos = 0
        self.stream_id = 0
        self.buffer: list[int] = []
        self.buf_index = 64

    def _refill(self) -> None:
        k = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
        b = self.key0[:]
        c = self.key1[:]
        out = [0] * 64
        for block_idx in range(4):
            ctr = self.block_pos + block_idx
            d = [
                ctr & 0xFFFFFFFF,
                (ctr >> 32) & 0xFFFFFFFF,
                self.stream_id & 0xFFFFFFFF,
                (self.stream_id >> 32) & 0xFFFFFFFF,
            ]
            x = k + b + c + d
            for _ in range(6):
                # Column round
                x[0] = (x[0] + x[4]) & 0xFFFFFFFF
                x[12] = (((x[12] ^ x[0]) << 16) | ((x[12] ^ x[0]) >> 16)) & 0xFFFFFFFF
                x[8] = (x[8] + x[12]) & 0xFFFFFFFF
                x[4] = (((x[4] ^ x[8]) << 12) | ((x[4] ^ x[8]) >> 20)) & 0xFFFFFFFF
                x[0] = (x[0] + x[4]) & 0xFFFFFFFF
                x[12] = (((x[12] ^ x[0]) << 8) | ((x[12] ^ x[0]) >> 24)) & 0xFFFFFFFF
                x[8] = (x[8] + x[12]) & 0xFFFFFFFF
                x[4] = (((x[4] ^ x[8]) << 7) | ((x[4] ^ x[8]) >> 25)) & 0xFFFFFFFF

                x[1] = (x[1] + x[5]) & 0xFFFFFFFF
                x[13] = (((x[13] ^ x[1]) << 16) | ((x[13] ^ x[1]) >> 16)) & 0xFFFFFFFF
                x[9] = (x[9] + x[13]) & 0xFFFFFFFF
                x[5] = (((x[5] ^ x[9]) << 12) | ((x[5] ^ x[9]) >> 20)) & 0xFFFFFFFF
                x[1] = (x[1] + x[5]) & 0xFFFFFFFF
                x[13] = (((x[13] ^ x[1]) << 8) | ((x[13] ^ x[1]) >> 24)) & 0xFFFFFFFF
                x[9] = (x[9] + x[13]) & 0xFFFFFFFF
                x[5] = (((x[5] ^ x[9]) << 7) | ((x[5] ^ x[9]) >> 25)) & 0xFFFFFFFF

                x[2] = (x[2] + x[6]) & 0xFFFFFFFF
                x[14] = (((x[14] ^ x[2]) << 16) | ((x[14] ^ x[2]) >> 16)) & 0xFFFFFFFF
                x[10] = (x[10] + x[14]) & 0xFFFFFFFF
                x[6] = (((x[6] ^ x[10]) << 12) | ((x[6] ^ x[10]) >> 20)) & 0xFFFFFFFF
                x[2] = (x[2] + x[6]) & 0xFFFFFFFF
                x[14] = (((x[14] ^ x[2]) << 8) | ((x[14] ^ x[2]) >> 24)) & 0xFFFFFFFF
                x[10] = (x[10] + x[14]) & 0xFFFFFFFF
                x[6] = (((x[6] ^ x[10]) << 7) | ((x[6] ^ x[10]) >> 25)) & 0xFFFFFFFF

                x[3] = (x[3] + x[7]) & 0xFFFFFFFF
                x[15] = (((x[15] ^ x[3]) << 16) | ((x[15] ^ x[3]) >> 16)) & 0xFFFFFFFF
                x[11] = (x[11] + x[15]) & 0xFFFFFFFF
                x[7] = (((x[7] ^ x[11]) << 12) | ((x[7] ^ x[11]) >> 20)) & 0xFFFFFFFF
                x[3] = (x[3] + x[7]) & 0xFFFFFFFF
                x[15] = (((x[15] ^ x[3]) << 8) | ((x[15] ^ x[3]) >> 24)) & 0xFFFFFFFF
                x[11] = (x[11] + x[15]) & 0xFFFFFFFF
                x[7] = (((x[7] ^ x[11]) << 7) | ((x[7] ^ x[11]) >> 25)) & 0xFFFFFFFF

                # Diagonal round
                x[0] = (x[0] + x[5]) & 0xFFFFFFFF
                x[15] = (((x[15] ^ x[0]) << 16) | ((x[15] ^ x[0]) >> 16)) & 0xFFFFFFFF
                x[10] = (x[10] + x[15]) & 0xFFFFFFFF
                x[5] = (((x[5] ^ x[10]) << 12) | ((x[5] ^ x[10]) >> 20)) & 0xFFFFFFFF
                x[0] = (x[0] + x[5]) & 0xFFFFFFFF
                x[15] = (((x[15] ^ x[0]) << 8) | ((x[15] ^ x[0]) >> 24)) & 0xFFFFFFFF
                x[10] = (x[10] + x[15]) & 0xFFFFFFFF
                x[5] = (((x[5] ^ x[10]) << 7) | ((x[5] ^ x[10]) >> 25)) & 0xFFFFFFFF

                x[1] = (x[1] + x[6]) & 0xFFFFFFFF
                x[12] = (((x[12] ^ x[1]) << 16) | ((x[12] ^ x[1]) >> 16)) & 0xFFFFFFFF
                x[11] = (x[11] + x[12]) & 0xFFFFFFFF
                x[6] = (((x[6] ^ x[11]) << 12) | ((x[6] ^ x[11]) >> 20)) & 0xFFFFFFFF
                x[1] = (x[1] + x[6]) & 0xFFFFFFFF
                x[12] = (((x[12] ^ x[1]) << 8) | ((x[12] ^ x[1]) >> 24)) & 0xFFFFFFFF
                x[11] = (x[11] + x[12]) & 0xFFFFFFFF
                x[6] = (((x[6] ^ x[11]) << 7) | ((x[6] ^ x[11]) >> 25)) & 0xFFFFFFFF

                x[2] = (x[2] + x[7]) & 0xFFFFFFFF
                x[13] = (((x[13] ^ x[2]) << 16) | ((x[13] ^ x[2]) >> 16)) & 0xFFFFFFFF
                x[8] = (x[8] + x[13]) & 0xFFFFFFFF
                x[7] = (((x[7] ^ x[8]) << 12) | ((x[7] ^ x[8]) >> 20)) & 0xFFFFFFFF
                x[2] = (x[2] + x[7]) & 0xFFFFFFFF
                x[13] = (((x[13] ^ x[2]) << 8) | ((x[13] ^ x[2]) >> 24)) & 0xFFFFFFFF
                x[8] = (x[8] + x[13]) & 0xFFFFFFFF
                x[7] = (((x[7] ^ x[8]) << 7) | ((x[7] ^ x[8]) >> 25)) & 0xFFFFFFFF

                x[3] = (x[3] + x[4]) & 0xFFFFFFFF
                x[14] = (((x[14] ^ x[3]) << 16) | ((x[14] ^ x[3]) >> 16)) & 0xFFFFFFFF
                x[9] = (x[9] + x[14]) & 0xFFFFFFFF
                x[4] = (((x[4] ^ x[9]) << 12) | ((x[4] ^ x[9]) >> 20)) & 0xFFFFFFFF
                x[3] = (x[3] + x[4]) & 0xFFFFFFFF
                x[14] = (((x[14] ^ x[3]) << 8) | ((x[14] ^ x[3]) >> 24)) & 0xFFFFFFFF
                x[9] = (x[9] + x[14]) & 0xFFFFFFFF
                x[4] = (((x[4] ^ x[9]) << 7) | ((x[4] ^ x[9]) >> 25)) & 0xFFFFFFFF

            init = k + b + c + d
            for i in range(16):
                out[block_idx * 16 + i] = (x[i] + init[i]) & 0xFFFFFFFF
        self.block_pos += 4
        self.buffer = out
        self.buf_index = 0

    def next_u32(self) -> int:
        if self.buf_index >= 64:
            self._refill()
        val = self.buffer[self.buf_index]
        self.buf_index += 1
        return val

    def next_u64(self) -> int:
        w0 = self.next_u32()
        w1 = self.next_u32()
        return w0 | (w1 << 32)

    def random_range_u64_inclusive(self, low: int, high: int) -> int:
        range_val = high - low + 1
        if range_val == 0:
            return self.next_u64()
        s = self.next_u64()
        prod = s * range_val
        result = prod >> 64
        lo_order = prod & 0xFFFFFFFFFFFFFFFF
        range_neg = (-range_val) & 0xFFFFFFFFFFFFFFFF
        if lo_order > range_neg:
            s2 = self.next_u64()
            prod2 = s2 * range_val
            new_hi_order = prod2 >> 64
            if lo_order + new_hi_order >= (1 << 64):
                result += 1
        return low + result

    def random_range_usize(self, low: int, high: int) -> int:
        high_incl = high - 1
        range_val = (high_incl - low + 1) & 0xFFFFFFFF
        if range_val == 0:
            return self.next_u32()
        s = self.next_u32()
        prod = s * range_val
        result = prod >> 32
        lo_order = prod & 0xFFFFFFFF
        range_neg = (-range_val) & 0xFFFFFFFF
        if lo_order > range_neg:
            s2 = self.next_u32()
            prod2 = s2 * range_val
            new_hi_order = prod2 >> 32
            if lo_order + new_hi_order >= (1 << 32):
                result += 1
        return low + result


class GenState:
    def __init__(self, entities: Entities, rng: ChaCha12Rng, num_services: int) -> None:
        self.entities = entities
        self.rng = rng
        self.versions = [0] * num_services
        self.deploys: list[DeployRef] = []
        self.open_incidents: list[IncidentRef] = []
        self.deploy_number = 0
        self.incident_number = 0

    def deploy_event(self, seq: int, at: str) -> Event:
        service_index = self.rng.random_range_usize(0, len(self.entities.services))
        engineer_index = self.rng.random_range_usize(0, len(self.entities.engineers))
        self.versions[service_index] += 1
        self.deploy_number += 1
        deploy = DeployRef(
            id=f"dep-{self.deploy_number}",
            service=self.entities.services[service_index],
            version=f"v{self.versions[service_index]}.0.0",
        )
        self.entities.deploys.append(deploy.id)
        self.deploys.append(deploy)
        return Event(
            seq=seq,
            id=deploy.id,
            kind=EventKind.DEPLOY,
            service=deploy.service,
            version=deploy.version,
            engineer=self.entities.engineers[engineer_index],
            at=at,
        )

    def ownership_event(self, seq: int, at: str) -> Event:
        deploy = self.deploys[self.rng.random_range_usize(0, len(self.deploys))]
        service = deploy.service
        version = deploy.version
        engineer_index = self.rng.random_range_usize(0, len(self.entities.engineers))
        return Event(
            seq=seq,
            id=f"own-{seq}",
            kind=EventKind.OWNERSHIP_TRANSFER,
            service=service,
            version=version,
            engineer=self.entities.engineers[engineer_index],
            at=at,
        )

    def incident_event(self, seq: int, at: str) -> Event:
        deploy = self.deploys[self.rng.random_range_usize(0, len(self.deploys))]
        engineer_index = self.rng.random_range_usize(0, len(self.entities.engineers))
        self.incident_number += 1
        incident = IncidentRef(
            id=f"inc-{self.incident_number}",
            service=deploy.service,
            version=deploy.version,
        )
        self.entities.incidents.append(incident.id)
        self.open_incidents.append(incident)
        return Event(
            seq=seq,
            id=incident.id,
            kind=EventKind.INCIDENT_OPEN,
            service=incident.service,
            version=incident.version,
            engineer=self.entities.engineers[engineer_index],
            incident=incident.id,
            caused_by=deploy.id,
            at=at,
        )

    def resolve_event(self, seq: int, at: str) -> Event:
        incident_index = self.rng.random_range_usize(0, len(self.open_incidents))
        incident = self.open_incidents.pop(incident_index)
        engineer_index = self.rng.random_range_usize(0, len(self.entities.engineers))
        return Event(
            seq=seq,
            id=f"res-{incident.id}",
            kind=EventKind.RESOLVE,
            service=incident.service,
            version=incident.version,
            engineer=self.entities.engineers[engineer_index],
            incident=incident.id,
            at=at,
        )


def validate(config: WorldConfig) -> None:
    if config.services == 0 or config.services > 899:
        raise WorldError("services must be in 1..=899")
    if config.engineers == 0:
        raise WorldError("engineers must be nonzero")
    if not (4 <= config.events <= MAX_EVENTS):
        raise WorldError("events must be in 4..=1000000")


def rules() -> list[Rule]:
    return [
        Rule(
            id="R1",
            description="a deploy names exactly one engineer and service; versions increase per service",
        ),
        Rule(
            id="R2",
            description="an incident-open names the exact prior deploy that caused it",
        ),
        Rule(
            id="R3",
            description="the current owner is the newest ownership-transfer for a service",
        ),
        Rule(
            id="R4",
            description="a resolution names exactly one resolving engineer",
        ),
        Rule(
            id="R5",
            description="out-of-world svc-9xx entities never occur in generated events",
        ),
    ]


def civil_from_days(days_since_epoch: int) -> tuple[int, int, int]:
    """Gregorian civil date from days since the Unix epoch without reading wall clock."""
    z = days_since_epoch + 719_468
    era = (z if z >= 0 else z - 146_096) // 146_097
    day_of_era = z - era * 146_097
    year_of_era = (
        day_of_era - day_of_era // 1_460 + day_of_era // 36_524 - day_of_era // 146_096
    ) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (
        365 * year_of_era + year_of_era // 4 - year_of_era // 100
    )
    month_prime = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * month_prime + 2) // 5 + 1
    month = month_prime + (3 if month_prime < 10 else -9)
    year += 1 if month <= 2 else 0
    return (year, month, day)


def synthetic_timestamp(elapsed_minutes: int) -> str:
    """Generate ISO 8601 UTC timestamp from offset minutes without wall-clock reads."""
    seconds = BASE_UNIX_SECONDS + elapsed_minutes * 60
    days = seconds // 86_400
    seconds_of_day = seconds % 86_400
    year, month, day = civil_from_days(days)
    hour = seconds_of_day // 3_600
    minute = (seconds_of_day % 3_600) // 60
    second = seconds_of_day % 60
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def generate(config: WorldConfig) -> World:
    """Generate deterministic synthetic world obeying R1-R5 invariants."""
    validate(config)

    services = [f"svc-{n}" for n in range(1, config.services + 1)]
    engineers = [f"eng-{n}" for n in range(1, config.engineers + 1)]
    rules_list = rules()

    state = GenState(
        entities=Entities(
            services=services,
            engineers=engineers,
            incidents=[],
            deploys=[],
        ),
        rng=ChaCha12Rng(config.seed),
        num_services=config.services,
    )
    events: list[Event] = []
    elapsed_minutes = 0

    for offset in range(config.events):
        seq = offset + 1
        elapsed_minutes += state.rng.random_range_u64_inclusive(1, 17)
        at = synthetic_timestamp(elapsed_minutes)
        kind = offset % 4
        if kind == 0:
            event = state.deploy_event(seq, at)
        elif kind == 1:
            event = state.ownership_event(seq, at)
        elif kind == 2:
            event = state.incident_event(seq, at)
        else:
            event = state.resolve_event(seq, at)
        events.append(event)

    return World(
        entities=state.entities,
        rules=rules_list,
        events=events,
    )
