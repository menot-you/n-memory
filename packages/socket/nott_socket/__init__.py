"""The shared unix-socket door primitive for NOTT processes, and the stdio MCP client.

# The four load-bearing properties of the door, and each fails closed:

1. The socket is owner-only (0600) from birth, because it is created inside a
   directory only this process can enter (0700), given its mode there, and
   moved onto the caller's path in one atomic step — never widened first and
   narrowed after, and never derived from the process umask.
2. Every admitted uid is proven able to reach the socket before anything is
   created, so a door no admitted caller could ever reach is refused at boot
   instead of listening forever on a path that answers nobody.
3. Every accepted connection is checked against peer credentials (SO_PEERCRED
   on Linux, LOCAL_PEERCRED on macOS) and refused unless the peer's uid is one
   the door was told to admit — on every accept, not the first.
4. An occupied path is refused. The door never unlinks a path it did not
   create, because a blind unlink is exactly the window a racing process
   needs to steal the path.
"""

from .door import (
    SOCKET_MODE,
    STAGING_MODE,
    AcceptError,
    Admission,
    Admitted,
    BindError,
    ListenError,
    ModeError,
    OwnershipError,
    PathOccupied,
    PeerCredentialsError,
    Refused,
    UnixDoor,
    ownership,
)

__all__ = [
    "SOCKET_MODE",
    "STAGING_MODE",
    "AcceptError",
    "Admission",
    "Admitted",
    "BindError",
    "ListenError",
    "ModeError",
    "OwnershipError",
    "PathOccupied",
    "PeerCredentialsError",
    "Refused",
    "UnixDoor",
    "ownership",
]
