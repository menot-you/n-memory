"""The owning curator route setter, with no database or vendor operation."""

from __future__ import annotations

import json

from ..config import ConfigError, CuratorRoute, set_curator_route
from ..provider import validate_curator_route
from .common import parse_command


def cmd_curator_set(argv: list[str]) -> None:
    args = parse_command("curator", "set", argv)
    if args.reset:
        if args.provider is not None or args.account is not None:
            raise ConfigError("curator reset cannot also select provider/account")
        route = None
    else:
        if args.provider is None:
            raise ConfigError("curator set requires provider")
        route = CuratorRoute(args.provider, args.account)
        validate_curator_route(route)
    set_curator_route(route)
    print(
        json.dumps(
            {
                "provider": route.provider if route else None,
                "account": route.account if route else None,
            }
        )
    )
