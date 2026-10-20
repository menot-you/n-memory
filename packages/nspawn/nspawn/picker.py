"""L2 four-step execution-picker contract.

The ladder is branches keyed on what the caller supplied, not four sequential
steps run every time:

1. An explicit ``requested_provider`` selects one exact route (by provider
   name or by a discovered account alias) and never reroutes to a different
   provider when that route is walled — ``requested_provider`` records
   caller intent, and rerouting an explicit request would contradict it.
2. Its model is validated against the provider's declared ``models`` when
   that list is non-empty; otherwise the provider's own default, then the
   roster default, else a typed refusal. The roster default is NOT inherited
   by a route that declares its own base url and neither ``model`` nor
   ``models``: a redirected route serves its own model namespace, so a
   fleet-wide default there names another upstream. That branch reads the
   ROSTER only — a classification that consults the environment is false
   under ``env -i``.
3. With no explicit provider and an ``execution_class``, eligible unwalled
   Claude providers are ranked by ``rank_providers`` and the top choice
   wins.
4. With neither, the roster's declared default Claude provider is used,
   falling through to the ranked eligible set only when that default route
   is itself walled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from .claude_catalog import ModelCatalogError
from .errors import (
    NERR_1101,
    NERR_1105,
    NERR_1107,
    NERR_1124,
    NERR_1125,
    NERR_1129,
    ErrorEntry,
    NSpawnError,
)
from .native import request_refusal
from .owner_roster import (
    EFFORT_LADDER,
    UNCLASSIFIED,
    ClassPolicy,
    NativeAccount,
    Provider,
    Roster,
    claude_family,
    expand_value_with,
)
from .ranks import REVIEW, rank_providers
from .rows import AccountUsageRow


class AllProvidersWalled(NSpawnError):
    """Every provider/account candidate is cooling down."""

    nearest_reset: int | None

    def __init__(self, message: str, nearest_reset: int | None = None) -> None:
        self.nearest_reset = nearest_reset
        self.message = message
        self.entry = NERR_1105
        Exception.__init__(self, message)

    @property
    def data(self) -> dict[str, int]:
        """What the wire carries beside the message: the reset, when one is known."""
        return (
            {} if self.nearest_reset is None else {"nearest_reset": self.nearest_reset}
        )


class PickerError(NSpawnError):
    """Why an execution route could not be picked. Never a bare ``ValueError``."""

    def __init__(self, message: str, entry: ErrorEntry = NERR_1105) -> None:
        self.message = message
        self.entry = entry
        Exception.__init__(self, message)

    @staticmethod
    def unknown_provider(name: str, declared: Sequence[str]) -> PickerError:
        # The refusal CARRIES the vocabulary, like the two siblings below. Without this
        # the caller has no choice but to read `~/.nott/spawn.toml` from
        # disk — the error already carries the answer with the declared list.
        return PickerError(
            f'no Claude provider or account named "{name}" is declared in the '
            f"roster; it declares [{', '.join(declared)}]",
            entry=NERR_1105,
        )

    @staticmethod
    def unknown_model(provider: str, model: str, declared: list[str]) -> PickerError:
        return PickerError(
            f'provider "{provider}" declares models [{", ".join(declared)}]; '
            f'"{model}" is not one of them',
            entry=NERR_1105,
        )

    @staticmethod
    def no_route_serves_model(model: str, declared: list[str]) -> PickerError:
        return PickerError(
            f'no declared provider serves model "{model}"; '
            f"the providers declare models [{', '.join(declared)}]",
            entry=NERR_1105,
        )

    @staticmethod
    def no_account_runs(
        provider: str, requested: str, seen: Sequence[tuple[str, str, bool]]
    ) -> PickerError:
        described = ", ".join(
            f'{alias} runs "{model}"' + (" (cooling down)" if walled else "")
            for alias, model, walled in seen
        )
        return PickerError(
            f'no unwalled account of provider "{provider}" runs the requested '
            f'model "{requested}"; it saw {described}',
            entry=NERR_1105,
        )

    @staticmethod
    def account_model_conflict(
        account: str, requested: str, declared: str
    ) -> PickerError:
        return PickerError(
            f'account "{account}" runs model "{declared}"; the requested model '
            f'"{requested}" would be replaced. Request "{declared}", or name an '
            "account that does not override the model",
            entry=NERR_1105,
        )

    @staticmethod
    def env_override_conflict(provider: str, name: str, requested: str) -> PickerError:
        return PickerError(
            f'the roster of "{provider}" sets {name}, which would replace the '
            f'requested "{requested}". Omit the request, or remove {name} from the '
            "roster's env or args",
            entry=NERR_1105,
        )

    @staticmethod
    def unknown_effort(provider: str, effort: str, declared: list[str]) -> PickerError:
        return PickerError(
            f'provider "{provider}" declares efforts [{", ".join(declared)}]; '
            f'"{effort}" is not one of them',
            entry=NERR_1124,
        )

    @staticmethod
    def rewritten_effort(
        provider: str, effort: str, wire: str, served: Sequence[str]
    ) -> PickerError:
        alternatives = (
            f"; it serves efforts [{', '.join(served)}]"
            if served
            else "; it declares no effort it can carry"
        )
        return PickerError(
            f'provider "{provider}" runs its turns through the codex app-server, '
            f'which does not carry effort "{effort}": it rewrites it to "{wire}" '
            "on the wire. An effort the route does not serve is a refused turn, "
            f"never a slower one{alternatives}",
            entry=NERR_1124,
        )

    @staticmethod
    def above_class_ceiling(
        execution_class: str, effort: str, policy: ClassPolicy
    ) -> PickerError:
        return PickerError(
            f'class "{execution_class}" buys effort "{policy.effort}" and tops out '
            f'at "{policy.ceiling}"; "{effort}" is above that ceiling. Declare the '
            "class this work actually is, or the owner raises the ceiling in "
            "spawn.toml [classes]",
            entry=NERR_1124,
        )

    @staticmethod
    def above_class_strength(
        execution_class: str,
        max_strength: int,
        *,
        provider: Provider | None = None,
        routes: Sequence[str] = (),
    ) -> PickerError:
        """Refused by cost, not by a wall: waiting changes nothing here."""
        pinned = (
            f'provider "{provider.name}" declares strength {provider.strength}'
            if provider is not None
            else "every otherwise eligible route is above that cap"
        )
        alternatives = (
            f"; routes that serve this class: [{', '.join(routes)}]"
            if routes
            else "; no declared route serves this class within that cap"
        )
        return PickerError(
            f'class "{execution_class}" caps provider strength at {max_strength} '
            f"and {pinned}. Declare the class this work actually is, or the owner "
            f"raises max_strength in spawn.toml [classes]{alternatives}",
            entry=NERR_1125,
        )

    @staticmethod
    def outside_fast_lane(
        route: str, lane: tuple[str | None, str | None], model: str, effort: str
    ) -> PickerError:
        """A class-less run has no class cap, so it runs its route's fast lane."""
        return PickerError(
            f'a run without a class runs only its route\'s fast lane: "{route}" '
            f'takes model "{lane[0]}" at effort "{lane[1]}", not model "{model}" at '
            f'effort "{effort}". Pin that lane, or declare the class this work is',
            entry=NERR_1129,
        )

    @staticmethod
    def below_class_effort(
        execution_class: str, effort: str, *, provider: Provider | None = None
    ) -> PickerError:
        """Refused by the ladder: the route serves nothing this class bought."""
        who = (
            f'provider "{provider.name}" serves no effort'
            if provider is not None
            else "no otherwise eligible route serves an effort"
        )
        return PickerError(
            f'class "{execution_class}" buys effort "{effort}" and {who} at or '
            "below it. Declare the class this work actually is, or pin a route "
            "whose ladder reaches that effort",
            entry=NERR_1124,
        )

    @staticmethod
    def undeclared_route_model(provider: str, defaults_model: str) -> PickerError:
        return PickerError(
            f'provider "{provider}" declares its own base url and neither '
            "`model` nor `models`; a redirected route serves its own model "
            f'namespace, so inheriting the roster default "{defaults_model}" '
            "would name whatever upstream owns that id, not this route. "
            f'Declare `model` or `models` for "{provider}" in spawn.toml',
            entry=NERR_1107,
        )

    @staticmethod
    def no_default_model(provider: str) -> PickerError:
        return PickerError(
            f'provider "{provider}" declares no model and the roster names '
            "no default model",
            entry=NERR_1107,
        )

    @staticmethod
    def no_eligible_provider() -> PickerError:
        return PickerError(
            "no Claude provider is declared in the roster",
            entry=NERR_1101,
        )


@dataclass(frozen=True, slots=True)
class PickerResult:
    """Requested route and the provider/account actually selected."""

    requested_provider: str | None
    executed_provider: Provider
    executed_account: str | None
    model: str
    effort: str | None
    warning: str | None = None
    #: The model and effort the caller asked for, None when it asked for none: a
    #: re-pick repeats the request, never the values the first pick resolved.
    requested_model: str | None = None
    requested_effort: str | None = None
    #: A model the verb prefers when the caller asked for none (the ping's fast
    #: lane): resolved like a request, but never judged as a caller's pin.
    preferred_model: str | None = None


@dataclass(frozen=True, slots=True)
class Preference:
    """One route an unpinned review or verification tries, in the declared order.

    `route` is a provider name or an account alias; a `model` or `effort` it
    does not name is the route's own default.
    """

    route: str
    model: str | None = None
    effort: str | None = None

    def __str__(self) -> str:
        return self.route if self.model is None else f"{self.route}/{self.model}"


def preferences(roster: Roster, execution_class: str | None) -> list[Preference]:
    """The order an unpinned witness of another agent's work tries routes in.

    First the roster's `[preference]` list for the class, then, for a review,
    the route `[roles]` binds to the reviewer with its `[role_profile]`, then
    every provider in the class's rank except a named_only one. An entry listed
    twice keeps its first place. Reachability is not judged here: the admission
    skips what cannot serve now and records why.
    """
    declared = [
        Preference(route, model)
        for route, model in roster.preferences.get(execution_class or "", ())
    ]
    role: list[Preference] = []
    reviewer = roster.roles.get("reviewer")
    if execution_class == REVIEW and reviewer is not None:
        profile = roster.role_profiles.get("reviewer", {})
        role.append(Preference(reviewer, profile.get("model"), profile.get("effort")))
    # A named_only route serves only where the roster names it above.
    ranked = [
        Preference(provider.name)
        for provider in rank_providers(roster.providers, execution_class)
        if not provider.named_only
    ]
    return list(dict.fromkeys([*declared, *role, *ranked]))


@dataclass(frozen=True, slots=True)
class Producer:
    """The agent whose work a review or a judge witnesses: it never runs as it."""

    providers: frozenset[str] = frozenset()
    accounts: frozenset[str] = frozenset()
    models: frozenset[str] = frozenset()


def producer_of(roster: Roster, names: Sequence[str]) -> Producer:
    """Each name as the roster reads it: a provider, else an account, else a model.

    A model that no route declares is kept as a model: it excludes nothing
    today, and it still excludes the route that serves it tomorrow.
    """
    providers = {provider.name for provider in roster.providers}
    accounts = {
        alias
        for provider in roster.providers
        for alias in provider.discovered_accounts()
    }
    return Producer(
        providers=frozenset(name for name in names if name in providers),
        accounts=frozenset(
            name for name in names if name not in providers and name in accounts
        ),
        models=frozenset(
            name for name in names if name not in providers and name not in accounts
        ),
    )


#: A full Claude id and the family it belongs to.
_CLAUDE_ID: Final = re.compile(r"claude-(opus|sonnet|haiku)-[a-zA-Z0-9.-]+")


def _model_names(provider: Provider | None, model: str) -> set[str]:
    """`model` with each other name a Claude route gives the same model.

    A family alias (`opus`) is the id the route's catalogue maps it to, and an
    id is its family when the catalogue maps that family to it. With no
    catalogue to read, an id is its family: the exclusion errs toward keeping
    the producer out.
    """
    base = model.removesuffix("[1m]")
    names = {model, base}
    if provider is None or provider.kind != "claude":
        return names
    try:
        catalog: dict[str, str] | None = provider.catalog
    except ModelCatalogError:
        catalog = None
    family = claude_family(base)
    found = _CLAUDE_ID.fullmatch(base)
    if (
        family is None
        and found is not None
        and (catalog is None or catalog.get(found[1]) == base)
    ):
        family = found[1]
    if family is not None:
        names.add(family)
        if catalog is not None and family in catalog:
            names.add(catalog[family])
    return names


def is_producer_model(
    provider: Provider | None, model: str | None, producer: Producer
) -> bool:
    """Whether `provider` running `model` runs one of the producer's models."""
    if model is None or not producer.models:
        return False
    names = _model_names(provider, model)
    return any(names & _model_names(provider, own) for own in producer.models)


def producer_refusal(
    roster: Roster, producer: Producer, route: str, model: str | None
) -> str | None:
    """Why `route` running `model` is the producer, or None when it is not."""
    if route in producer.accounts:
        return "the producer's account"
    selected = roster.select(route)
    if selected is not None and selected.name in producer.providers:
        return "the producer's route"
    if is_producer_model(selected, model, producer):
        return "the producer's model"
    return None


def without_accounts(roster: Roster, accounts: frozenset[str]) -> Roster:
    """A copy of `roster` in which no provider runs on `accounts`.

    The owner's roster object is not changed: the exclusion holds for this
    dispatch only, and the next one may take every account again.
    """
    if not accounts:
        return roster
    return replace(
        roster,
        providers=[
            replace(
                provider,
                excluded_accounts=[
                    *provider.excluded_accounts,
                    *(
                        alias
                        for alias in provider.discovered_accounts()
                        if alias in accounts and alias not in provider.excluded_accounts
                    ),
                ],
            )
            for provider in roster.providers
        ],
    )


def exclusion_check(provider: Provider, accounts: Sequence[str]) -> None:
    """Refuse exclusion aliases that do not name a configured account."""
    unknown = sorted(set(provider.excluded_accounts) - set(accounts))
    if unknown:
        raise PickerError(
            f'provider "{provider.name}" excluded_accounts names unknown accounts: '
            + ", ".join(unknown)
        )


def _nearest_reset(
    candidates: Sequence[Provider],
    provider_walls: dict[str, int],
    account_walls: dict[str, int],
    now: int,
) -> int | None:
    """The soonest future reset among every wall touching ``candidates``."""
    blocking: list[int] = []
    for provider in candidates:
        accounts = provider.discovered_accounts()
        exclusion_check(provider, accounts)
        if accounts and all(alias in provider.excluded_accounts for alias in accounts):
            continue
        provider_until = provider_walls.get(provider.name)
        if provider_until is not None and provider_until > now:
            blocking.append(provider_until)
        for alias in accounts:
            if alias in provider.excluded_accounts:
                continue
            account_until = account_walls.get(alias)
            if account_until is not None and account_until > now:
                blocking.append(account_until)
    return min(blocking) if blocking else None


def _occupancy(provider: Provider, alias: str, account_load: dict[str, int]) -> float:
    """Live runs on this alias divided by what the owner says it is worth.

    RELATIVE, never the raw count: a 3090 serving four at once and a laptop
    serving one are both full at their own number. An alias absent from the
    map is idle — the reader omits zeroes rather than padding them — and an
    account the roster never declared is worth one.
    """
    declared = provider.accounts.get(alias)
    capacity = declared.capacity if declared is not None else 1
    return account_load.get(alias, 0) / capacity


#: The length of each window the spread reads, by the last part of its stored
#: name (`seven_day`, `codex.primary.five_hour`).
WINDOW_SECONDS: Final = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}


@dataclass(frozen=True, slots=True)
class AccountUse:
    """What the registry knows of each account's use: its stored quota windows,
    and when its newest run started (epoch ms). Empty without a registry."""

    windows: tuple[AccountUsageRow, ...] = ()
    last_run: Mapping[str, int] = field(default_factory=dict)


def _headroom(use: AccountUse, alias: str, span: str, now: int) -> tuple[float, int]:
    """`alias`'s tightest current `span` window: its unused share and the seconds
    to its reset. A window past its reset, or never observed, is empty and whole.

    Only a window of every model counts: Claude's, and Codex's main `codex`
    limit (`codex.primary.seven_day`, or a slot with no limit). Another Codex
    limit (`codex_other.primary.seven_day`) meters one model, and its stored
    name does not say which, so it does not lower the score of another model.
    """
    return min(
        (
            (max(0.0, 1.0 - row.utilization), row.resets_at - now)
            for row in use.windows
            if row.account_alias == alias
            and row.usage_window.rpartition(".")[2] == span
            and row.usage_window.removeprefix("codex.").count(".") < 2
            and row.resets_at > now
        ),
        default=(1.0, WINDOW_SECONDS[span]),
    )


def _spread(
    provider: Provider,
    aliases: Sequence[str],
    now: int,
    account_load: dict[str, int] | None,
    use: AccountUse | None,
) -> str:
    """The one of `aliases` (eligible, never empty) that takes the next run.

    Score: the weekly unused share per second to its reset, times the five-hour
    unused share, over 1 + occupancy. A week that resets soon loses what is not
    used, so it goes first. The highest score wins, then the account whose last
    run is oldest. Declared order breaks only a tie of both, and every tie of
    endpoint accounts (`base_url`, a capacity host such as qwen): they have no
    reset clock to start, and the qwen roster declares the 3090 first on purpose.
    """
    load = account_load or {}
    if provider.fill_first:
        # The first account with a free slot, so a cold host wakes only when
        # every warm one is full. All full: the emptiest, as below.
        free = [a for a in aliases if _occupancy(provider, a, load) < 1]
        if free:
            return free[0]
    known = use or AccountUse()
    hosts = all(
        (account := provider.accounts.get(alias)) is not None and account.base_url
        for alias in aliases
    )

    def rank(alias: str) -> tuple[float, int]:
        week, until = _headroom(known, alias, "seven_day", now)
        hour, _ = _headroom(known, alias, "five_hour", now)
        score = week / until * hour / (1 + _occupancy(provider, alias, load))
        return score, (0 if hosts else -known.last_run.get(alias, 0))

    return max(aliases, key=rank)


def _unwalled_account(
    provider: Provider,
    account_walls: dict[str, int],
    now: int,
    account_load: dict[str, int] | None = None,
    use: AccountUse | None = None,
) -> tuple[str | None, bool]:
    """The account to run on, and whether any unwalled one exists.

    A provider with no secrets directory declares no accounts at all: that
    is a valid env/token-file route, never a blocked one.

    `_spread` chooses among the unwalled accounts. Without a load map or a use
    it has nothing to compare, so the first unwalled alias wins, which is what a
    caller with no registry to ask still gets. A wall outranks the score: an
    idle account that is cooling down is not a place to send work.
    """
    # DECLARED order when the roster declares accounts, because that is the
    # order the owner wrote and the order `_spread` keeps when the score and
    # the last run tie too.
    # `discovered_accounts()` sorts, which is right for a secrets directory —
    # its filenames are facts with no order of their own — and wrong here: the
    # qwen roster declares the 3090 first and the laptop sorts first, so the
    # alphabet sent every idle dispatch at the laptop.
    accounts = provider.ordered_accounts()
    exclusion_check(provider, accounts)
    if not accounts:
        return None, True
    unwalled = [
        alias
        for alias in accounts
        if alias not in provider.excluded_accounts
        and ((cooling := account_walls.get(alias)) is None or cooling <= now)
    ]
    if not unwalled:
        return None, False
    return _spread(provider, unwalled, now, account_load, use), True


def _account_running(
    provider: Provider,
    model: str,
    account_walls: dict[str, int],
    now: int,
    account_load: dict[str, int] | None,
    use: AccountUse | None = None,
) -> str:
    """The unwalled account `_spread` chooses among those that run `model`;
    refused naming every account and model seen when none does."""
    seen = []
    accounts = provider.ordered_accounts()
    exclusion_check(provider, accounts)
    for alias in accounts:
        if alias in provider.excluded_accounts:
            continue
        account = provider.accounts.get(alias)
        # No override: the account runs whatever declared model is asked of it.
        runs = (account.model if account is not None else None) or model
        cooling = account_walls.get(alias)
        seen.append((alias, runs, cooling is not None and cooling > now))
    running = [alias for alias, runs, walled in seen if runs == model and not walled]
    if not running:
        raise PickerError.no_account_runs(provider.name, model, seen)
    return _spread(provider, running, now, account_load, use)


def key_refusal(provider: Provider, alias: str | None, model: str | None) -> str | None:
    """Why `alias` (the route itself for None) may not run `model`, or None.

    A base url sends the key to another endpoint (a local qwen), so
        that account is not one. `None` is a model chosen later (a new canvas node).
    """
    if model is not None and "haiku" in model.lower():
        return None
    account = provider.accounts.get(alias or "")
    auth = (account.auth_env if account is not None else None) or provider.auth_env
    base = (account.base_url if account is not None else None) or (
        provider.env or {}
    ).get("ANTHROPIC_BASE_URL")
    if auth != "ANTHROPIC_API_KEY" or base:
        return None
    where = f'account "{alias}"' if alias else f'provider "{provider.name}"'
    return (
        f"{where} sends an Anthropic API key, which runs a Haiku model "
        f"only, not {model or 'a model chosen later'}"
    )


def _runs(
    provider: Provider, alias: str | None, model: str | None, pinned: bool
) -> str | None:
    """The model `alias` runs: a pinned model, else the account's own, else `model`.

    `None` stays `None`: a canvas node switches model at any time, so the
    account's declared model does not protect it.
    """
    account = provider.accounts.get(alias or "")
    if pinned or model is None or account is None:
        return model
    return account.model or model


def _key_emptied_message(model: str | None) -> str:
    return (
        "every open account left for this run sends an Anthropic API "
        f"key, which runs a Haiku model only, not {model or 'a model chosen later'}"
    )


def keyless(provider: Provider, model: str | None, pinned: bool = False) -> Provider:
    """`provider` with the accounts `key_refusal` refuses for `model` excluded.

    Eligibility, the account choice and the wall's reset read this view, so an
    unpinned run moves to the next account or route and never to the key.
    """
    keyed = [
        alias
        for alias in provider.ordered_accounts()
        if alias not in provider.excluded_accounts
        and key_refusal(provider, alias, _runs(provider, alias, model, pinned))
    ]
    if not keyed:
        return provider
    return replace(provider, excluded_accounts=[*provider.excluded_accounts, *keyed])


def _is_eligible(
    provider: Provider,
    provider_walls: dict[str, int],
    account_walls: dict[str, int],
    now: int,
) -> bool:
    provider_until = provider_walls.get(provider.name)
    if provider_until is not None and provider_until > now:
        return False
    _, has_unwalled_account = _unwalled_account(provider, account_walls, now)
    return has_unwalled_account


def _select_account(
    provider: Provider,
    pinned_alias: str | None,
    provider_walls: dict[str, int],
    account_walls: dict[str, int],
    now: int,
    account_load: dict[str, int] | None = None,
    use: AccountUse | None = None,
) -> str | None:
    """The alias this run executes under, or ``None`` for an env/token route.

    Raises:
        AllProvidersWalled: naming the blocking wall's nearest reset.
    """
    exclusion_check(provider, provider.ordered_accounts())
    provider_until = provider_walls.get(provider.name)
    if provider_until is not None and provider_until > now:
        raise AllProvidersWalled(
            f'provider "{provider.name}" is cooling down', provider_until
        )
    if pinned_alias is not None:
        if pinned_alias in provider.excluded_accounts:
            raise PickerError(f'account "{pinned_alias}" is excluded in the roster')
        account_until = account_walls.get(pinned_alias)
        if account_until is not None and account_until > now:
            raise AllProvidersWalled(
                f'account "{pinned_alias}" is cooling down', account_until
            )
        return pinned_alias
    alias, ok = _unwalled_account(provider, account_walls, now, account_load, use)
    if not ok:
        reset = _nearest_reset([provider], provider_walls, account_walls, now)
        raise AllProvidersWalled(
            f'every allowed account of provider "{provider.name}" is cooling down or excluded',
            reset,
        )
    return alias


def _resolve_model(
    provider: Provider, requested_model: str | None, defaults_model: str | None
) -> str:
    """Pick the model this spawn runs.

    U7 — the validation that used to apply ONLY to `requested_model is not None`
    now applies on both branches. A roster default that names a model the
    proxy does not serve was failing in the vendor, mid-run; the picker
    catches it before any process exists.
    """
    # Pick the candidate. Validation runs in `_validate_model` below; it sees
    # the same candidate regardless of how it was chosen.
    if requested_model is not None:
        candidate = requested_model
    elif provider.model is not None:
        candidate = provider.model
    elif provider.models:
        # A provider that declares what it serves is answered from THAT list.
        # Falling through to the roster default handed `agy` "claude-opus-5",
        # a model it does not serve: the pair left the picker already invalid
        # and only the vendor found out, mid-run.
        candidate = provider.models[0]
    elif defaults_model is None:
        raise PickerError.no_default_model(provider.name)
    elif _routes_off_native(provider):
        # The SAME rule one branch up, for the case where the list is empty:
        # `codex` declares neither, so the fleet default reached the catalog
        # and was refused there — blaming the model, listing sixty ids of
        # other routes. The proxy routes by MODEL ID, never by provider name,
        # so an inherited default is either from another namespace (refused)
        # or an id another upstream owns (dispatched to the wrong one under
        # this label). Neither is a route the roster declared.
        raise PickerError.undeclared_route_model(provider.name, defaults_model)
    else:
        candidate = defaults_model
    _validate_model(provider, candidate)
    return candidate


def _routes_off_native(provider: Provider) -> bool:
    """Whether the ROSTER redirects this route's base url. Reads no environment.

    Two shapes, one meaning: one endpoint for the whole provider, in its env
    block, or one per account. The account shape is the local qwen, and
    reading only the env block let it inherit the fleet default — the same
    mistake this predicate exists to stop, arriving by the other door.
    """
    if (provider.env or {}).get("ANTHROPIC_BASE_URL"):
        return True
    return any(account.base_url for account in provider.accounts.values())


def _validate_model(provider: Provider, candidate: str) -> None:
    """Refuse a candidate the provider cannot serve, before any process exists."""
    if not provider.serves(candidate):
        raise PickerError.unknown_model(provider.name, candidate, provider.models)


#: Efforts the codex app-server accepts by name and then REWRITES, mapped to
#: what it actually puts on the wire. Measured against codex-cli 0.154.0 on
#: 2026-09-17, one `turn/start` per name against a local upstream that logged
#: the request body: `low`/`medium`/`high`/`xhigh`/`max` — and even a name the
#: binary does not know — travel verbatim, while `ultra` arrives as
#: `reasoning.effort: "medium"`, same model, nothing else changed. Sakana then
#: refuses the turn outright (`must be one of "high", "xhigh", "max", got
#: "medium"`), and an OpenAI route would simply have thought at `medium` under
#: the caller's `ultra` label.
#:
#: The roster may DECLARE `ultra` for a codex route — the owner's `codex` and
#: `fugu` both do, and that is owner data, not ours to edit. The transport is
#: still the thing that cannot carry it.
CODEX_REWRITTEN_EFFORTS = {"ultra": "medium"}


def served_efforts(provider: Provider) -> list[str]:
    """The declared ladder minus what this route's transport rewrites.

    What `nspawn_provider_list` advertises and what the picker accepts read from
    here, so the report can never teach a caller an effort the dispatch refuses.
    """
    if provider.kind != "codex":
        return list(provider.efforts)
    return [e for e in provider.efforts if e not in CODEX_REWRITTEN_EFFORTS]


def _class_effort(
    roster: Roster, execution_class: str | None, requested_effort: str | None
) -> ClassPolicy | None:
    """The policy governing this dispatch, or ``None`` when the roster has none.

    A pinned effort above the ceiling is refused by name. A roster with no
    `[classes]` governs nothing and the old ladder stands.
    """
    name = execution_class or UNCLASSIFIED
    policy = roster.classes.get(name)
    if policy is None or requested_effort is None:
        return policy
    if requested_effort in EFFORT_LADDER and EFFORT_LADDER.index(
        requested_effort
    ) > EFFORT_LADDER.index(policy.ceiling):
        raise PickerError.above_class_ceiling(name, requested_effort, policy)
    return policy


def _serves_at_or_below(provider: Provider, effort: str) -> bool:
    """Whether the route can run the class WITHOUT going over what it bought.

    A route with no declared ladder serves anything. A route with one must
    have a rung at or below `effort`, or it cannot take this class at all.

    This is what makes `policy.effort` comparable between classes, which is
    the whole basis of `classify._is_cheaper`. That number is not a price on
    its own: it is the input to `_clamp_to_served`, and the strength cap
    decides which route it lands on. Guaranteeing that the served effort never
    exceeds the class initial on ANY eligible route is what lets two classes
    be compared by their initials whichever routes they would each pick.

    Found by an independent review, 2026-09-21, and real on the owner's own
    roster: `agy` serves only `high` and up, so a `low` class dispatched there
    ran `high`.
    """
    if not provider.efforts:
        return True
    return any(
        e in EFFORT_LADDER and EFFORT_LADDER.index(e) <= EFFORT_LADDER.index(effort)
        for e in provider.efforts
    )


def _clamp_to_served(provider: Provider, effort: str) -> str:
    """The highest effort the provider serves at or below ``effort``.

    A class initial the route does not serve is not a refusal: the class
    asked for "this much", and the route gives the most it has under that.

    When it has NOTHING under that, it gives the LEAST it has. Taking the most
    instead inverted the axis — a class starting at ``low`` ran at ``ultra``
    on a route serving only ``high`` and ``ultra``, while the same route under
    a class starting at ``max`` ran at ``high``. That makes a lower class
    initial buy a higher bill, and voids `classify`'s whole guarantee that an
    observation can only lower what is spent. Measured by an independent
    review, 2026-09-21.
    """
    if not provider.efforts or effort in provider.efforts:
        return effort
    on_ladder = [e for e in provider.efforts if e in EFFORT_LADDER]
    below = [
        e for e in on_ladder if EFFORT_LADDER.index(e) <= EFFORT_LADDER.index(effort)
    ]
    if below:
        return max(below, key=_ladder_rank)
    return min(on_ladder or provider.efforts, key=_ladder_rank)


def _ladder_rank(effort: str) -> int:
    return (
        EFFORT_LADDER.index(effort) if effort in EFFORT_LADDER else len(EFFORT_LADDER)
    )


def _resolve_effort(
    provider: Provider,
    requested_effort: str | None,
    defaults_effort: str | None,
    roster: Roster | None = None,
    execution_class: str | None = None,
) -> str | None:
    policy = (
        _class_effort(roster, execution_class, requested_effort)
        if roster is not None
        else None
    )
    if policy is not None and requested_effort is None:
        effort: str | None = _clamp_to_served(provider, policy.effort)
    else:
        effort = _pick_effort(provider, requested_effort, defaults_effort)
    # Whoever chose it — caller, class policy, provider line or roster default —
    # a rewritten effort makes the run's recorded effort a lie, so the guard
    # sits on the RESOLVED value rather than on the request branch alone.
    if provider.kind == "codex" and effort in CODEX_REWRITTEN_EFFORTS:
        raise PickerError.rewritten_effort(
            provider.name,
            effort,
            CODEX_REWRITTEN_EFFORTS[effort],
            served_efforts(provider),
        )
    return effort


def _pick_effort(
    provider: Provider, requested_effort: str | None, defaults_effort: str | None
) -> str | None:
    if requested_effort is not None:
        if provider.efforts and requested_effort not in provider.efforts:
            raise PickerError.unknown_effort(
                provider.name, requested_effort, provider.efforts
            )
        return requested_effort
    if provider.effort is not None:
        return provider.effort
    if defaults_effort is not None and (
        not provider.efforts or defaults_effort in provider.efforts
    ):
        return defaults_effort
    return None


#: The Claude child env names that carry the model and the effort
#: (`child.build_child_env`); the fast lane is `model_fast`'s, not the caller's.
_MODEL_ENV = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
_EFFORT_ENV = ("CLAUDE_CODE_EFFORT_LEVEL",)


def refuse_roster_override(
    roster: Roster,
    provider: Provider,
    account: NativeAccount | None,
    requested_model: str | None,
    requested_effort: str | None,
) -> None:
    """Refuse a caller-pinned model or effort that a roster env entry would replace.

    The child applies `[defaults.env]`, `[provider.env]` and the account's env
    AFTER the requested values, so a literal there wins in the child. Only a child
    that receives the roster's env and args is judged: the cage composes its own.
    """
    # The env and args as the child receives them: `Roster.resolve`, expanded
    # the way `child.build_child_env` and the supervisor expand them.
    resolved = roster.resolve(provider.name)
    env: dict[str, str] = {}
    for layer in (resolved.env or provider.env, (account.env or {}) if account else {}):
        for name, raw in layer.items():
            value = expand_value_with(raw, resolved.plugin_root)
            if value or "$" not in raw:
                env[name] = value
    for names, requested in (
        (_MODEL_ENV, requested_model),
        (_EFFORT_ENV, requested_effort),
    ):
        if requested is None:
            continue
        for name in names:
            if name in env and env[name] != requested:
                raise PickerError.env_override_conflict(provider.name, name, requested)
    # The CLI ranks these flags above the env, and the roster's args reach argv
    # as declared (`Roster.resolve`: the provider's, else the defaults').
    args = [expand_value_with(arg, resolved.plugin_root) for arg in resolved.args]
    for flag, requested in (
        ("--model", requested_model),
        ("--effort", requested_effort),
    ):
        if requested is None:
            continue
        for index, arg in enumerate(args):
            name, joined, value = arg.partition("=")
            if name != flag:
                continue
            if not joined:
                value = args[index + 1] if index + 1 < len(args) else ""
            if value != requested:
                raise PickerError.env_override_conflict(provider.name, flag, requested)


def fast_lane(provider: Provider, account: str | None) -> tuple[str | None, str | None]:
    """The model and effort of `provider`'s fast lane on `account`: the account's
    own model when it declares one (it runs no other), else the fast model, else
    the model; and the lowest effort the route serves, else its default."""
    declared = provider.accounts.get(account or "")
    model = (
        (declared.model if declared is not None else None)
        or provider.model_fast
        or provider.model
    )
    effort = min(
        (e for e in provider.efforts if e in EFFORT_LADDER),
        key=EFFORT_LADDER.index,
        default=provider.effort,
    )
    return model, effort


def refuse_outside_fast_lane(
    roster: Roster,
    requested_provider: str | None,
    model: str | None,
    effort: str | None,
) -> None:
    """Refuse a class-less run that asks for more than its route's fast lane: no
    class cap and no System1 bound it (the node host's gist is such a run)."""
    provider = roster.select(requested_provider)
    if provider is None:
        raise PickerError.unknown_provider(
            requested_provider or "", _declared_names(roster)
        )
    account = None if requested_provider == provider.name else requested_provider
    lane = fast_lane(provider, account)
    if (model, effort) != lane:
        raise PickerError.outside_fast_lane(
            requested_provider or provider.name, lane, str(model), str(effort)
        )


def _declared_names(roster: Roster) -> list[str]:
    """Every name `roster.select` would have accepted: providers, then aliases.

    Same order `select` searches in, so the list reads as the answer to the
    question the caller just got wrong.
    """
    names: list[str] = []
    for provider in roster.providers:
        names.append(provider.name)
        names.extend(provider.discovered_accounts())
    return names


def _class_strength_policy(
    roster: Roster, execution_class: str | None
) -> ClassPolicy | None:
    """The cap governing this dispatch, or ``None`` when nothing caps it.

    No class is no cap (batch, ping and the credential repick are internal),
    and a class absent from `[classes]` is no cap either — the same partial
    table `_class_effort` already honours for the effort.
    """
    if execution_class is None:
        return None
    return roster.classes.get(execution_class)


def _class_admits(
    provider: Provider,
    policy: ClassPolicy | None,
    requested_effort: str | None = None,
) -> bool:
    """Whether this route may carry this class at all.

    Two conditions, and a route must pass both.

    The strength cap: undeclared strength is undeclared, never a zero — and
    never capped.

    Without an explicit effort, a route that serves nothing at or below the
    class initial cannot take it. That used to be answered by promoting the dispatch to the
    cheapest rung the route DID serve, which is necessarily dearer than the
    class asked for — three escalations in this campaign trace to that one
    line, the last of them real on the owner's own roster, where `agy` serves
    only `high` and up while `mechanical_fanout` buys `low`.

    An explicit effort replaces that initial, within the class ceiling checked
    by `_class_effort`. `_resolve_effort` still validates the selected provider's
    ladder and transport; an explicit request never bypasses the strength cap.

    The default ladder also makes `policy.effort` comparable between classes, which
    `classify._is_cheaper` rests on: that number is the INPUT to
    `_clamp_to_served`, and the strength cap decides which route it lands on.
    Once no eligible route can exceed the class initial, comparing two class
    initials is sound whichever routes they would each pick. Found by an
    independent review, 2026-09-21.
    """
    if policy is None:
        return True
    if (
        policy.max_strength is not None
        and provider.strength is not None
        and provider.strength > policy.max_strength
    ):
        return False
    return requested_effort is not None or _serves_at_or_below(provider, policy.effort)


def serviceable_routes(
    roster: Roster,
    now: int,
    *,
    exclude: str | None = None,
    provider_walls: dict[str, int] | None = None,
    account_walls: dict[str, int] | None = None,
    execution_class: str | None = None,
    read_paths: Sequence[str] = (),
    system_prompt: str | None = None,
) -> list[str]:
    """Provider names that can carry this work RIGHT NOW, best first.

    Read off the roster and the live walls alone — no vendor call, no
    inference. It exists so a refusal can name where to go instead of handing
    the caller a vendor string and a question: an agent that has to ask the
    owner which account to use is the supervision this system exists to
    remove.

    A provider with no declared model cannot serve anything, and one whose
    every account is walled is out until its wall lifts, so neither is named.
    Nor is one whose launcher would REFUSE this particular work: the same
    `request_refusal` `pick_execution` applies runs here, with the run's own
    `read_paths` and `system_prompt`. Naming a route that answers the caller
    with a second refusal is worse than naming none — it is the supervision
    this clause exists to remove, handed back with extra steps.
    """
    walled_providers = provider_walls if provider_walls is not None else {}
    walled_accounts = account_walls if account_walls is not None else {}
    policy = _class_strength_policy(roster, execution_class)
    eligible = [
        provider
        for provider in roster.providers
        if provider.name != exclude
        and not provider.named_only
        and _class_admits(provider, policy)
        and (provider.model is not None or roster.defaults.model is not None)
        and _is_eligible(provider, walled_providers, walled_accounts, now)
        and not request_refusal(
            provider, read_paths=read_paths, system_prompt=system_prompt
        )
    ]
    return [provider.name for provider in rank_providers(eligible, execution_class)]


def _model_catalog(provider: Provider) -> list[str] | None:
    """The models a route serves, or None when the route declares no catalog.

    `models` wins. Without it, a route whose every usable account sets its own
    model serves those models only: an account model wins over `model`, and an
    excluded account runs nothing. Else the route is open.
    """
    if provider.models:
        return provider.models
    usable = [
        account
        for alias, account in provider.accounts.items()
        if alias not in provider.excluded_accounts
    ]
    account_models = [account.model for account in usable if account.model]
    if not account_models or len(account_models) < len(usable):
        return None
    return list(dict.fromkeys(account_models))


def pick_execution(
    roster: Roster,
    now: int,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    requested_effort: str | None = None,
    execution_class: str | None = None,
    preferred_model: str | None = None,
    account_load: dict[str, int] | None = None,
    account_use: AccountUse | None = None,
    producer: str | None = None,
    provider_walls: dict[str, int] | None = None,
    account_walls: dict[str, int] | None = None,
    read_paths: Sequence[str] = (),
    system_prompt: str | None = None,
) -> PickerResult:
    """Apply the declared four-step ladder and return the observed route."""
    walled_providers = provider_walls if provider_walls is not None else {}
    walled_accounts = account_walls if account_walls is not None else {}
    warning: str | None = None
    account_pinned = False
    strength_policy = _class_strength_policy(roster, execution_class)
    # Validate the pin before letting it replace the class's initial effort.
    _class_effort(roster, execution_class, requested_effort)
    wanted = requested_model if requested_model is not None else preferred_model
    policy = roster.classes.get(execution_class or UNCLASSIFIED)
    pinned_model = requested_model is not None

    def planned(p: Provider) -> str | None:
        # The model `_resolve_model` picks below, before the account is known.
        fast = p.model_fast if policy is not None and policy.fast else None
        return (
            wanted
            or fast
            or p.model
            or next(iter(p.models), None)
            or (roster.defaults.model)
        )

    def keyed(p: Provider) -> Provider:
        return keyless(p, planned(p), pinned_model)

    def key_emptied(p: Provider) -> bool:
        accounts = p.ordered_accounts()
        if not accounts:
            return key_refusal(p, None, planned(p)) is not None
        view = keyed(p)
        return any(a not in p.excluded_accounts for a in accounts) and all(
            a in view.excluded_accounts for a in accounts
        )

    def eligible(p: Provider) -> bool:
        return not key_emptied(p) and _is_eligible(
            keyed(p), walled_providers, walled_accounts, now
        )

    def walled(routes: list[Provider]) -> NSpawnError:
        pool = [keyed(p) for p in routes if not key_emptied(p)]
        reset = _nearest_reset(pool, walled_providers, walled_accounts, now)
        if reset is None and len(pool) < len(routes):
            return PickerError(_key_emptied_message(planned(routes[0])))
        return AllProvidersWalled("every eligible Claude route is cooling down", reset)

    if requested_provider is not None:
        provider = roster.select(requested_provider)
        if provider is None:
            raise PickerError.unknown_provider(
                requested_provider, _declared_names(roster)
            )
        if refusal := request_refusal(
            provider, read_paths=read_paths, system_prompt=system_prompt
        ):
            raise PickerError(refusal)
        # An explicit request is never rerouted (step 1 of the ladder), so a pin
        # above the class cap is a refusal that names where to go instead.
        if not _class_admits(provider, strength_policy, requested_effort):
            assert execution_class is not None and strength_policy is not None
            cap = strength_policy.max_strength
            if cap is None or provider.strength is None or provider.strength <= cap:
                raise PickerError.below_class_effort(
                    execution_class, strength_policy.effort, provider=provider
                )
            raise PickerError.above_class_strength(
                execution_class,
                cap,
                provider=provider,
                routes=serviceable_routes(
                    roster,
                    now,
                    provider_walls=walled_providers,
                    account_walls=walled_accounts,
                    execution_class=execution_class,
                    read_paths=read_paths,
                    system_prompt=system_prompt,
                ),
            )
        pinned_alias = (
            requested_provider if requested_provider != provider.name else None
        )
        account_pinned = pinned_alias is not None
        if account_pinned or not provider.ordered_accounts():
            runs = _runs(provider, pinned_alias, planned(provider), pinned_model)
            if refusal := key_refusal(provider, pinned_alias, runs):
                raise PickerError(refusal)
        elif key_emptied(provider):
            raise PickerError(_key_emptied_message(planned(provider)))
        executed_account = _select_account(
            keyed(provider),
            pinned_alias,
            walled_providers,
            walled_accounts,
            now,
            account_load,
            account_use,
        )
    else:
        candidates = [p for p in roster.providers if not p.named_only]
        if requested_model is not None and candidates:
            # A pinned model narrows the roster once, here, so every rule below
            # (capability, class, cap, producer, walls, default) sees only
            # routes that serve it. A route whose declared accounts are all
            # excluded runs nothing, so it neither declares nor is open.
            live = [
                p
                for p in candidates
                if not (p.accounts and set(p.accounts) <= set(p.excluded_accounts))
            ]
            declaring = [
                p for p in live if requested_model in (_model_catalog(p) or (p.model,))
            ]
            # No route declares it: a route with no catalog trusts the live one.
            serving = declaring or [p for p in live if _model_catalog(p) is None]
            if not serving:
                raise PickerError.no_route_serves_model(
                    requested_model,
                    list(
                        dict.fromkeys(
                            m for p in candidates for m in _model_catalog(p) or []
                        )
                    ),
                )
            candidates = serving
        candidates = [
            provider
            for provider in candidates
            if request_refusal(
                provider, read_paths=read_paths, system_prompt=system_prompt
            )
            is None
        ]
        if not candidates:
            if any(not p.named_only for p in roster.providers):
                raise PickerError(
                    "no declared provider supports the requested read_paths/system_prompt capabilities"
                )
            raise PickerError.no_eligible_provider()
        if execution_class is not None:
            open_ = [p for p in candidates if eligible(p)]
            affordable = [
                p for p in open_ if _class_admits(p, strength_policy, requested_effort)
            ]
            # Two causes, two answers: a pool the cap emptied never refills, so
            # telling the caller to wait for a wall would be a lie.
            if open_ and not affordable:
                assert strength_policy is not None
                cap = strength_policy.max_strength
                if cap is None:
                    raise PickerError.below_class_effort(
                        execution_class, strength_policy.effort
                    )
                raise PickerError.above_class_strength(execution_class, cap)
            ranked = rank_providers(affordable, execution_class, producer=producer)
            if not ranked:
                raise walled(candidates)
            provider = ranked[0]
            executed_account = _select_account(
                keyed(provider),
                None,
                walled_providers,
                walled_accounts,
                now,
                account_load,
                account_use,
            )
        else:
            default_provider = next((p for p in candidates if p.default), candidates[0])
            if eligible(default_provider):
                provider = default_provider
                executed_account = _select_account(
                    keyed(provider),
                    None,
                    walled_providers,
                    walled_accounts,
                    now,
                    account_load,
                    account_use,
                )
            else:
                ranked = rank_providers([p for p in candidates if eligible(p)], None)
                if not ranked:
                    raise walled(candidates)
                provider = ranked[0]
                executed_account = _select_account(
                    keyed(provider),
                    None,
                    walled_providers,
                    walled_accounts,
                    now,
                    account_load,
                    account_use,
                )
                warning = (
                    f'default provider "{default_provider.name}" is cooling down; '
                    f'routed to "{provider.name}"'
                )

    if provider.kind not in ("claude", "codex", "agy"):
        raise PickerError(
            f"provider {provider.name!r} has kind {provider.kind!r}: "
            f"the {provider.kind} launcher is not implemented yet"
        )

    # The class's fast lane ranks below a pin and the verb's preference: it is
    # the owner's default for that kind of work, not a request a re-pick repeats.
    fast_lane = None
    if wanted is None and policy is not None and policy.fast:
        fast_lane = wanted = provider.model_fast
    model = _resolve_model(provider, wanted, roster.defaults.model)
    # THE ACCOUNT ID WINS, and it wins HERE. `build_child_env` already applied it to the
    # child's environment (child.py:300) — the same logical model has different
    # IDs in vLLM and ollama — but the picker result is what the run
    # row records. Applied only there, the store would name one model and the child
    # would run another, leaving the fleet spend record incorrect.
    chosen = provider.accounts.get(executed_account or "")
    if (
        chosen is not None
        and chosen.model
        and requested_model is not None
        and requested_model != chosen.model
        and not account_pinned
    ):
        # A pinned model chooses among the accounts that run it; load chose
        # without asking (audit 2026-10-09: eight qwen pins refused this way).
        executed_account = _account_running(
            keyed(provider),
            requested_model,
            walled_accounts,
            now,
            account_load,
            account_use,
        )
        chosen = provider.accounts.get(executed_account or "")
    if (
        chosen is not None
        and chosen.model
        and fast_lane is not None
        and fast_lane != chosen.model
        and provider.serves(
            chosen.model,
            declared=[*provider.models, *([provider.model] if provider.model else [])],
        )
        and not account_pinned
    ):
        # Load picked an account pinned to another DECLARED model of the route
        # (its `model` is declared even when the optional `models` is omitted);
        # prefer one that runs the class's fast lane. An undeclared id is the
        # same model spelled by that account's server (vLLM `m-27b`, ollama
        # `m:27b`), so load stands. None runs it: the lane is a default, not a
        # pin, so the load's account and its model stand.
        try:
            executed_account = _account_running(
                keyed(provider),
                fast_lane,
                walled_accounts,
                now,
                account_load,
                account_use,
            )
            chosen = provider.accounts.get(executed_account or "")
        except PickerError:
            pass
    if chosen is not None and chosen.model:
        # A caller-pinned model is never swapped for the account's: refuse before
        # any run exists. An omitted model takes the account's override.
        if requested_model is not None and requested_model != chosen.model:
            raise PickerError.account_model_conflict(
                executed_account or "", requested_model, chosen.model
            )
        model = chosen.model
    if provider.model_fast is not None:
        # The fast lane reaches the child as ANTHROPIC_DEFAULT_HAIKU_MODEL with
        # no other check; a dead id there fails in the vendor, mid-run.
        _validate_model(provider, provider.model_fast)
    effort = _resolve_effort(
        provider,
        requested_effort,
        roster.defaults.effort,
        roster=roster,
        execution_class=execution_class,
    )
    return PickerResult(
        requested_provider=requested_provider,
        executed_provider=provider,
        executed_account=executed_account,
        model=model,
        effort=effort,
        warning=warning,
        requested_model=requested_model,
        requested_effort=requested_effort,
        preferred_model=preferred_model,
    )
