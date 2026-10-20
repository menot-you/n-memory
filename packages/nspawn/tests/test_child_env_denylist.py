"""The child inherits the operator's environment, minus their secrets.

F-024, measured 2026-08-29: a spawned child received **231 environment keys, 78
of them named `*KEY*` or `*TOKEN*`** — `AIKIDO_API_KEY`, `CARGO_REGISTRY_TOKEN`,
`CLOUDFLARE_API_KEY`, `ASC_KEY_ID`. `CHILD_ENV_ALLOWLIST` names four variables
and does not limit anything: the ambient environment is copied first and the
"allowlist" is applied afterwards as an override.

The inheritance is DELIBERATE and must stay — sealing the child into an empty
home stripped `~/.claude` and its `permissions.defaultMode`, and LD-3 died with
the child waiting for an approval from nobody. What the child needed was the
config directory, never the operator's credentials for unrelated services.

So the cure is a denylist over the inheritance, not a return to the seal.
"""

from __future__ import annotations

from pathlib import Path

from nspawn import child
from nspawn.owner_roster import Provider


def test_ambient_secrets_do_not_reach_the_child() -> None:
    ambient = {
        "PATH": "/usr/bin",
        "HOME": "/home/test-user",
        "CLAUDE_CONFIG_DIR": "/home/test-user/.claude",
        "AIKIDO_API_KEY": "secret-1",
        "CARGO_REGISTRY_TOKEN": "secret-2",
        "CLOUDFLARE_API_KEY": "secret-3",
        "GITHUB_TOKEN": "secret-4",
        "SOME_PASSWORD": "secret-5",
        "MY_SECRET_VALUE": "secret-6",
        # The provider's OWN credential, declared here because the ambient is what
        # `build_child_env` reads. Before 2026-09-03 it came from `os.environ`, so these two
        # cases passed for whoever had `ZAI_API_KEY` exported and failed for everyone else.
        "ZAI_API_KEY": "declared-by-this-test",
    }
    env, _ = child.build_child_env(
        Provider(name="zai", auth_env="ZAI_API_KEY", env={}),
        None,
        ambient=ambient,
        base_environ={"PATH": "/usr/bin"},
    )

    leaked = sorted(k for k in env if k in ambient and "secret-" in ambient[k])
    assert not leaked, f"the child inherited the operator's secrets: {leaked}"


def test_the_inheritance_that_ld3_needs_survives() -> None:
    """The cure must not re-break what the seal broke."""
    ambient = {
        "PATH": "/usr/bin",
        "HOME": "/home/test-user",
        "CLAUDE_CONFIG_DIR": "/home/test-user/.claude",
        "LANG": "en_US.UTF-8",
        "AIKIDO_API_KEY": "secret-1",
        "ZAI_API_KEY": "declared-by-this-test",
    }
    env, _ = child.build_child_env(
        Provider(name="zai", auth_env="ZAI_API_KEY", env={}),
        None,
        ambient=ambient,
        base_environ={"PATH": "/usr/bin"},
    )

    assert env["CLAUDE_CONFIG_DIR"] == "/home/test-user/.claude"
    assert env["HOME"] == "/home/test-user"
    assert env["LANG"] == "en_US.UTF-8"


def test_the_provider_may_still_declare_a_credential_by_that_name(
    tmp_path: Path,
) -> None:
    """The denylist filters the AMBIENT, never what the roster declares."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "default.token").write_text("token-secret\n")
    env, _ = child.build_child_env(
        Provider(
            name="custom",
            secrets=secrets,
            env={"ANTHROPIC_AUTH_TOKEN": "declared-by-the-roster"},
        ),
        None,
        ambient={"PATH": "/usr/bin"},
        base_environ={"PATH": "/usr/bin"},
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "declared-by-the-roster"


def test_a_caller_on_another_endpoint_does_not_redirect_the_child(
    tmp_path: Path,
) -> None:
    """Measured 2026-09-23: a session running on z.ai dispatched a claude child;
    the claude child inherited its `ANTHROPIC_BASE_URL`, sent the account's
    OAuth token to z.ai and answered "401 token expired or incorrect" while
    the same token worked from any other caller. The endpoint is the route's,
    never the caller's."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "default.token").write_text("token-secret\n")
    env, _ = child.build_child_env(
        Provider(name="claude", secrets=secrets, env={}),
        None,
        ambient={
            "PATH": "/usr/bin",
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            "ANTHROPIC_SMALL_FAST_MODEL": "glm-4.5-air",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
        },
        base_environ={"PATH": "/usr/bin"},
    )
    leaked = {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    } & set(env)
    assert not leaked, leaked


def test_a_route_that_declares_its_endpoint_keeps_it(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "default.token").write_text("token-secret\n")
    env, _ = child.build_child_env(
        Provider(
            name="zai",
            secrets=secrets,
            env={"ANTHROPIC_BASE_URL": "https://declared.example"},
        ),
        None,
        ambient={"PATH": "/usr/bin", "ANTHROPIC_BASE_URL": "https://caller.example"},
        base_environ={"PATH": "/usr/bin"},
    )
    assert env["ANTHROPIC_BASE_URL"] == "https://declared.example"
