"""Compose fail-closed Claude node cages.

Native writers and readers use the ordinary client launch instead of these
settings.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .admission import AdmissionRefusal, PrelaunchProfileV1, runtime_files
from .child import CREDENTIAL_ENV_NAMES
from .errors import NERR_1116, NSpawnError

#: The shared temp the node host makes its per-run scratch in.
SCRATCH_ROOT: Final = Path("/var/tmp")
#: The PATH the client and its tools resolve `bwrap` and the shell from.
RUNTIME_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
#: The system trees a tool needs to execute anything at all (E0 settings-2).
RUNTIME_READ: Final[tuple[str, ...]] = (
    "/usr",
    "/bin",
    "/lib",
    "/lib64",
    "/etc",
    "/dev",
)
#: Host-only names the E0 scrub left visible to tools.
HOST_ONLY_ENV: Final[tuple[str, ...]] = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
#: Where a route's own endpoint may be to count as private: this host, and the private
#: networks (RFC 1918, the tailnet's 100.64.0.0/10, IPv6 unique local).
PRIVATE_NETWORKS: Final = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    )
)
#: The one shape a private endpoint may have: `scheme://host[:port]`, and
#: an optional final `/`. No userinfo, `\`, path, query, fragment or space, so
#: the client's WHATWG URL parser cannot read another host in it.
_ENDPOINT: Final = re.compile(
    r"(https?)://(localhost|[0-9.]+|\[[0-9A-Fa-f:.]+\])(?::([0-9]{1,5}))?/?"
)


class RestrictedLaunchRefused(NSpawnError):
    """A required boundary of the node cage cannot be established."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_1116, f"claude restricted launch: {detail}")


def make_scratch(root: Path = SCRATCH_ROOT) -> Path:
    """A fresh short scratch directory, 0700, for one caged run. The caller
    removes it, and only once the run's slice is empty: a surviving descendant
    would otherwise meet a freed name under a world-writable parent."""
    try:
        return Path(tempfile.mkdtemp(prefix="nott-claude-", dir=root))
    except OSError as error:
        raise RestrictedLaunchRefused(f"scratch root {root}: {error}") from error


def private_endpoint(url: str) -> str | None:
    """`url` rebuilt from its scheme, host and port when it is an endpoint on this
    host or a private network, else None. The client gets the rebuilt URL, so it
    reaches exactly the host that this check read.

    The host must be `localhost` or an address in `PRIVATE_NETWORKS`. Any other
    name never counts, because it can resolve to any address.
    """
    match = _ENDPOINT.fullmatch(url)
    if match is None:
        return None
    scheme, host, port = match.groups()
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            return None
        if not any(address in network for network in PRIVATE_NETWORKS):
            return None
        host = f"[{address}]" if address.version == 6 else str(address)
    return f"{scheme}://{host}" + ("" if port is None else f":{int(port)}")


NODE_BOOTSTRAP = """import os, socket, subprocess, sys, time
bridge = subprocess.Popen([sys.argv[1], "TCP4-LISTEN:39871,bind=127.0.0.1,reuseaddr,fork", "UNIX-CONNECT:" + sys.argv[2]])
deadline = time.monotonic() + 60
while True:
    if bridge.poll() is not None or time.monotonic() >= deadline:
        raise SystemExit(126)
    try:
        with socket.create_connection(("127.0.0.1", 39871), .1):
            break
    except OSError:
        time.sleep(.01)
os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
"""


@dataclass(frozen=True, slots=True)
class NodeLaunch:
    """The full process boundary, established before SDK or native bootstrap."""

    argv: tuple[str, ...]
    env: dict[str, str]
    proxy: Path | None


def _destination_within(path: str, grants: list[str]) -> bool:
    target = Path(path)
    return any(
        target == Path(grant) or target.is_relative_to(grant) for grant in grants
    )


def node_tool_settings(
    scope: Mapping[str, Any],
    scratch: str,
    configuration: list[str],
    credential_names: set[str] | frozenset[str] = CREDENTIAL_ENV_NAMES,
) -> dict[str, Any]:
    """The native tool cage is narrower than producer administration.

    Bash receives no authentication environment or network. Native file tools
    cannot read administrative state or modify selected launch inputs. The
    outer cage continues to bound startup, producers and every native process.
    """
    administrative = [path for path in scope["administrative"] if path != scratch]
    hidden = [
        path
        for path in administrative
        if _destination_within(path, [*scope["read"], *scope["write"]])
    ]
    read = list(dict.fromkeys([*RUNTIME_READ, *scope["read"], scratch]))
    write = list(dict.fromkeys([*scope["write"], scratch]))
    denies = list(dict.fromkeys([*administrative, *configuration]))

    # Native gitignore rules use // for filesystem-root paths. Escape literal
    # destination names before adding the deliberate descendant wildcard.
    def permission_path(path: str) -> str:
        return "/" + "".join("\\" + char if char in "\\*?[]" else char for char in path)

    permissions = [
        f"{tool}({permission_path(path)}/**)"
        for path in denies
        for tool in ("Read", "Edit", "Write", "Glob", "Grep")
    ]
    permissions += [
        f"{tool}({permission_path(path)})"
        for path in configuration
        for tool in ("Read", "Edit", "Write")
    ]
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            "filesystem": {
                "denyRead": ["/", *hidden, *configuration],
                "allowRead": read,
                "denyWrite": [
                    *administrative,
                    *configuration,
                    *([] if scope["write"] else scope["read"]),
                ],
                "allowWrite": write,
            },
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "credentials": {
                "envVars": [
                    {"name": name, "mode": "deny"}
                    for name in sorted(set(credential_names) | set(HOST_ONLY_ENV))
                ]
            },
        },
        "settings": {"permissions": {"deny": permissions}},
    }


def _tool_boundary(data: dict[str, Any]) -> None:
    options = data["constructor"]["options"]
    scratch = options.get("env", {}).get("TMPDIR")
    if not isinstance(scratch, str):
        raise AdmissionRefusal("native tool boundary has no scratch destination")
    credential_names = set(CREDENTIAL_ENV_NAMES)
    for producer in [*data["mcp"], *data["producer"]]:
        credential_names.update(producer["inputs"].get("credential_environment", []))
    required = node_tool_settings(
        data["scope"],
        scratch,
        [entry["path"] for entry in data["configuration"]],
        credential_names,
    )
    if options.get("sandbox") != required["sandbox"]:
        raise AdmissionRefusal("native tool boundary differs from its admitted scope")
    settings = options.get("settings", {})
    denied = settings.get("permissions", {}).get("deny", [])
    if not isinstance(denied, list) or any(
        permission not in denied
        for permission in required["settings"]["permissions"]["deny"]
    ):
        raise AdmissionRefusal("native tool boundary exposes administrative inputs")


def runtime_roots(data: Mapping[str, Any]) -> tuple[str, ...]:
    """Original installed roots selected by existing Python/plugin inputs."""
    roots = {
        path
        for item in data["mcp"]
        for path in item["inputs"]
        .get("environment", {})
        .get("PYTHONPATH", "")
        .split(":")
        if path
    }
    plugin = data["project"]["plugin_parent"]
    if plugin is not None and Path(plugin).is_dir():
        roots.add(plugin)
    roots.update(data["policy"]["trust_filter"].get("installed_plugins", []))
    protected = [
        data["project"]["root"],
        *data["scope"]["write"],
        *data["scope"]["administrative"],
    ]
    selected: list[str] = []
    for root in sorted(roots, key=lambda value: (value.count("/"), value)):
        path = Path(root)
        if (
            not path.is_absolute()
            or path.resolve() != path
            or not path.is_dir()
            or root == "/"
            or any(
                path.is_relative_to(other) or Path(other).is_relative_to(path)
                for other in protected
            )
        ):
            raise AdmissionRefusal("runtime root overlaps or lacks a canonical input")
        if "/usr" in data["scope"]["read"] and path.is_relative_to("/usr"):
            continue
        if not any(path.is_relative_to(parent) for parent in selected):
            selected.append(root)
    return tuple(selected)


def runtime_inventory_capture(
    data: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Inventory every installed entry, including bootstrap, cache and aliases."""
    from .route import RouteRefused, file_identity

    inventory: dict[str, Any] = {}
    identities: dict[str, dict[str, str]] = {}

    def refused(error: OSError) -> None:
        raise AdmissionRefusal("runtime inventory is unavailable") from error

    try:
        for location in runtime_roots(data):
            root = Path(location)
            directories, links = [], {}
            for parent, names, filenames in os.walk(
                root, followlinks=False, onerror=refused
            ):
                directories.append(str(Path(parent).relative_to(root)))
                for name in sorted([*names, *filenames]):
                    path = Path(parent) / name
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        target = path.resolve(strict=True)
                        if not target.is_relative_to(root) or not (
                            target.is_file() or target.is_dir()
                        ):
                            raise AdmissionRefusal(
                                "runtime inventory has an external or unknown link"
                            )
                        links[str(path.relative_to(root))] = str(target)
                        if target.is_dir():
                            continue
                    elif stat.S_ISDIR(info.st_mode):
                        continue
                    elif not stat.S_ISREG(info.st_mode):
                        raise AdmissionRefusal(
                            "runtime inventory contains a nonregular input"
                        )
                    identity = file_identity(path)
                    identities[identity["path"]] = identity
                    if len(identities) > 16384:
                        raise AdmissionRefusal("runtime inventory exceeds its bound")
            inventory[location] = {
                "directories": sorted(directories),
                "links": links,
            }
    except (OSError, RuntimeError, RouteRefused) as error:
        raise AdmissionRefusal("runtime inventory changed or is unavailable") from error
    return inventory, list(identities.values())


def runtime_inventory_verify(
    data: Mapping[str, Any], *, captured_configuration: bool = False
) -> None:
    """Revalidate original inputs and complete topology before bootstrap effects."""
    from .route import RouteRefused, file_identity

    inventory, identities = runtime_inventory_capture(data)
    expected = data["policy"]["trust_filter"].get("runtime_inventory", {})
    if inventory != expected:
        raise AdmissionRefusal("runtime inventory topology differs")
    selected = runtime_files(data["runtime"])
    selected.extend(
        identity
        for producer in [*data["mcp"], *data["producer"]]
        for identity in producer["executables"]
    )
    expected_files: dict[str, dict[str, str]] = {}
    for identity in selected:
        prior = expected_files.get(identity["path"])
        if prior is not None and prior != identity:
            raise AdmissionRefusal("runtime input has incompatible identities")
        expected_files[identity["path"]] = identity
    observed = {identity["path"]: identity for identity in identities}
    roots = tuple(inventory)
    if set(observed) != {
        path
        for path in expected_files
        if any(Path(path).is_relative_to(root) for root in roots)
    }:
        raise AdmissionRefusal("runtime inventory contains missing or extra inputs")
    configuration = (
        {item["path"] for item in data["configuration"]}
        if captured_configuration
        else set()
    )
    try:
        for path, identity in expected_files.items():
            if path in configuration:
                continue
            actual = observed.get(path) or file_identity(Path(path))
            if actual != identity:
                raise AdmissionRefusal("runtime content identity differs")
    except RouteRefused as error:
        raise AdmissionRefusal("runtime input is unavailable") from error


def configuration_verify(
    data: Mapping[str, Any],
    configuration: Mapping[str, Path],
    pinned: Mapping[str, int],
) -> None:
    """Named configuration overlays retain sealed bytes and native path identity."""
    expected = tuple(dict.fromkeys(item["path"] for item in data["configuration"]))
    if tuple(configuration) != expected:
        raise AdmissionRefusal("configuration snapshot differs from selected inputs")
    if not expected:
        return
    root = configuration[expected[0]].parent
    protected = [
        path
        for kind in ("read", "write", "administrative")
        for path in data["scope"][kind]
    ]
    info = root.lstat()
    parent = root.parent.lstat()
    if (
        root.resolve() != root
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o555
        or not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
        or any(
            root.is_relative_to(path) or Path(path).is_relative_to(root)
            for path in protected
        )
        or set(root.iterdir()) != set(configuration.values())
    ):
        raise AdmissionRefusal("configuration snapshot topology differs")
    for index, location in enumerate(expected):
        source = configuration[location]
        descriptor = pinned.get(location)
        if source != root / str(index) or descriptor is None:
            raise AdmissionRefusal("configuration snapshot identity differs")
        info = source.lstat()
        admitted = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_size != admitted.st_size
            or stat.S_IMODE(info.st_mode) != stat.S_IMODE(admitted.st_mode)
        ):
            raise AdmissionRefusal("configuration snapshot file identity differs")
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
        captured = hashlib.sha256()
        offset = 0
        while chunk := os.pread(descriptor, 65536, offset):
            captured.update(chunk)
            offset += len(chunk)
        if digest != captured.digest():
            raise AdmissionRefusal("configuration snapshot captured bytes differ")


def compose_node_launch(
    profile: PrelaunchProfileV1,
    *,
    consumer_argv: tuple[str, ...],
    host_auth: Mapping[str, str],
    schema: Path,
    grant_root: Path,
    descriptor: int,
    pinned: Mapping[str, int] | None = None,
    producer_auth: Mapping[str, str] | None = None,
    configuration: Mapping[str, Path] | None = None,
) -> NodeLaunch:
    """Compose a Linux mount/network/PID cage for the complete node process.

    This is passive: it creates no process, proxy, hook, helper or MCP server.
    The owning launcher supplies its exact verified descriptor and grant root.
    """
    from .network import destination_read

    data = profile.data()
    scope = data["scope"]
    _tool_boundary(data)
    roots = runtime_roots(data)
    if roots:
        runtime_inventory_verify(data, captured_configuration=pinned is not None)
    if configuration is not None:
        if pinned is None:
            raise AdmissionRefusal("configuration snapshots require sealed inputs")
        configuration_verify(data, configuration, pinned)
    if not consumer_argv or consumer_argv[0] != data["runtime"]["node"]["path"]:
        raise AdmissionRefusal("consumer runtime differs from PRELAUNCH")
    bwrap = shutil.which("bwrap", path=RUNTIME_PATH)
    if bwrap is None:
        raise AdmissionRefusal("node bootstrap confinement is unavailable")
    env = {"PATH": RUNTIME_PATH, "LANG": "C.UTF-8"}
    nonsecret = data["constructor"]["options"].get("env", {})
    if not isinstance(nonsecret, dict) or any(
        not isinstance(name, str)
        or not isinstance(value, str)
        or name in CREDENTIAL_ENV_NAMES
        or name in HOST_ONLY_ENV
        or name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        for name, value in nonsecret.items()
    ):
        raise AdmissionRefusal(
            "constructor environment exceeds its admitted vocabulary"
        )
    env.update(nonsecret)
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR"):
        if name not in env or not _destination_within(
            env[name], scope["administrative"]
        ):
            raise AdmissionRefusal(
                "native state and scratch must have admitted destinations"
            )
    if not host_auth or set(host_auth) - CREDENTIAL_ENV_NAMES:
        raise AdmissionRefusal("native account authentication is unavailable")
    env.update(host_auth)
    credential_channels = {
        name
        for producer in [*data["mcp"], *data["producer"]]
        for name in producer["inputs"].get("credential_environment", [])
    }
    if set(producer_auth or {}) != credential_channels or any(
        not isinstance(name, str)
        or not name.startswith("NOTT_")
        or not isinstance(value, str)
        or not value
        for name, value in (producer_auth or {}).items()
    ):
        raise AdmissionRefusal("producer authentication differs from admitted channels")
    env.update(producer_auth or {})
    writable = [*scope["write"], *scope["administrative"]]
    readonly = [
        *scope["read"],
        schema.as_posix(),
        *(identity["path"] for identity in runtime_files(data["runtime"])),
        *(item["path"] for item in data["configuration"]),
        *(
            path
            for path in (
                data["project"]["plugin_parent"],
                data["project"]["plugin_view"],
            )
            if path is not None
        ),
    ]
    for item in [*data["mcp"], *data["producer"]]:
        if (
            item["caller"] != data["node_id"]
            or item["project"] != data["project"]["id"]
        ):
            raise AdmissionRefusal("producer caller or project differs")
        for kind in ("read", "write", "administrative"):
            grants = scope[kind] if kind == "read" else writable
            if any(
                not _destination_within(path, grants) for path in item["grants"][kind]
            ):
                raise AdmissionRefusal("producer filesystem grants exceed the node")
        if set(item["grants"]["network"]) - set(scope["network"]) or any(
            not _destination_within(path, writable) for path in item["destinations"]
        ):
            raise AdmissionRefusal("producer destinations exceed the node")
        readonly.extend(identity["path"] for identity in item["executables"])
    for destination in scope["network"]:
        destination_read(destination)
    for path in [*readonly, *writable]:
        candidate = Path(path)
        if path == "/" or candidate.resolve() != candidate or not candidate.exists():
            raise AdmissionRefusal(
                "node filesystem destination is absent or noncanonical"
            )
    if any(
        _destination_within(path, writable)
        for path in ("/usr", "/etc", "/bin", "/lib", "/lib64", "/proc", "/dev")
    ):
        raise AdmissionRefusal("node administrative grants expose platform writes")
    # Broad accepted project/admin grants are mounted first. Exact configuration,
    # runtime and producer inputs overlay them read-only.
    mounts: dict[str, bool] = {path: False for path in readonly}
    mounts.update({path: True for path in writable})
    for path in readonly[len(scope["read"]) :]:
        mounts[path] = False
    exact = {
        data["runtime"][name]["path"]
        for name in ("launcher", "node", "consumer", "sdk", "sdk_package", "native")
    }
    exact.update(item["path"] for item in data["configuration"])
    exact.update(
        identity["path"]
        for item in [*data["mcp"], *data["producer"]]
        for identity in item["executables"]
    )
    for root in roots:
        for location in tuple(mounts):
            if Path(location).is_relative_to(root) and location not in exact:
                del mounts[location]
        mounts[root] = False
    if "/usr" in scope["read"]:
        for location in tuple(mounts):
            if (
                location != "/usr"
                and Path(location).is_relative_to("/usr")
                and location not in exact
            ):
                del mounts[location]
    # Another socket can sit beside a granted one (the node host's owner door
    # does): no other mount may show its directory.
    sockets = [path for path in scope["read"] if Path(path).is_socket()]
    if any(
        Path(door).parent.is_relative_to(path)
        for door in sockets
        for path in mounts
        if path not in sockets
    ):
        raise AdmissionRefusal("a grant shows a socket's directory")
    parents = {
        str(parent)
        for path in mounts
        for parent in Path(path).parents
        if str(parent) != "/"
    }
    argv = [
        bwrap,
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        # Nested native bubblewrap prepares its root at /tmp, independently of
        # TMPDIR. Give it a private empty mount, never the host's temporary files.
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
    ]
    for parent in sorted(parents, key=lambda path: (path.count("/"), path)):
        if parent not in {"/proc", "/dev"}:
            argv.extend(("--dir", parent))
    for path in sorted(mounts, key=lambda path: (path.count("/"), path)):
        if configuration is not None and path in configuration:
            argv.extend(("--ro-bind", str(configuration[path]), path))
        elif pinned is not None and path in pinned:
            mode = stat.S_IMODE(os.fstat(pinned[path]).st_mode)
            argv.extend(
                ("--perms", f"{mode:04o}", "--ro-bind-data", str(pinned[path]), path)
            )
        else:
            argv.extend(("--bind" if mounts[path] else "--ro-bind", path, path))
    # Platform loader symlinks are immutable aliases of the admitted /usr
    # runtime. Bubblewrap preserves the launcher's explicit pass_fds unchanged;
    # no path copy or descriptor recreation occurs inside the cage.
    if "/usr" in mounts:
        for alias in ("/bin", "/lib", "/lib64"):
            source = Path(alias)
            if source.is_symlink() and source.resolve().is_relative_to("/usr"):
                argv.extend(("--symlink", os.readlink(source), alias))
    if descriptor < 3:
        raise AdmissionRefusal("the sealed profile descriptor is not private")
    argv.extend(("--chdir", data["cwd"]))
    proxy = None
    command = consumer_argv
    if scope["network"]:
        bridge = next(
            (item for item in data["producer"] if item["id"] == "network-bridge"), None
        )
        bootstrap = next(
            (item for item in data["producer"] if item["id"] == "network-bootstrap"),
            None,
        )
        endpoint = (
            None
            if bridge is None or len(bridge["arguments"]) != 2
            else bridge["arguments"][1]
        )
        if not isinstance(endpoint, str) or not endpoint.startswith("UNIX-CONNECT:/"):
            raise AdmissionRefusal("private egress endpoint was not admitted")
        proxy = Path(endpoint.removeprefix("UNIX-CONNECT:"))
        if len(os.fsencode(proxy)) >= 108 or not _destination_within(
            str(proxy), scope["administrative"]
        ):
            raise AdmissionRefusal("private egress port exceeds its admitted boundary")
        if (
            bridge is None
            or bootstrap is None
            or bridge["arguments"]
            != [
                "TCP4-LISTEN:39871,bind=127.0.0.1,reuseaddr,fork",
                "UNIX-CONNECT:" + str(proxy),
            ]
            or bootstrap["arguments"] != ["-c", NODE_BOOTSTRAP]
        ):
            raise AdmissionRefusal("network bootstrap producers were not admitted")
        command = (
            bootstrap["command"],
            "-c",
            NODE_BOOTSTRAP,
            bridge["command"],
            str(proxy),
            *consumer_argv,
        )
        env.update(
            {
                "HTTP_PROXY": "http://127.0.0.1:39871",
                "HTTPS_PROXY": "http://127.0.0.1:39871",
                "ALL_PROXY": "http://127.0.0.1:39871",
                "NO_PROXY": "",
            }
        )
    argv.extend(("--", *command))
    return NodeLaunch(tuple(argv), env, proxy)
