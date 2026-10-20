"""Native catalogue metadata has no inference or credential input."""

import json
import shutil
from pathlib import Path

import pytest

from nspawn.claude_catalog import ModelCatalogError, catalog_read


def client(tmp_path: Path, output: str) -> Path:
    binary = tmp_path / "claude"
    binary.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "assert request['request'] == {'subtype': 'initialize'}\n"
        "assert sys.stdin.read() == ''\n"
        "assert set(os.environ) <= {'HOME', 'CLAUDE_CONFIG_DIR', 'TMPDIR', 'PATH', 'DISABLE_AUTOUPDATER', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', 'LC_CTYPE'}\n"
        "assert os.environ['HOME'] == os.environ['CLAUDE_CONFIG_DIR']\n"
        "assert '--tools' in sys.argv and '--no-session-persistence' in sys.argv\n"
        + f"sys.stdout.write({output!r})\n"
    )
    binary.chmod(0o700)
    return binary


def response(models: object) -> str:
    return (
        json.dumps(
            {
                "type": "control_response",
                "response": {
                    "request_id": "nott-catalog",
                    "response": {"models": models},
                },
            }
        )
        + "\n"
    )


def test_native_catalogue_has_only_control_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "private-test-canary")
    binary = client(
        tmp_path,
        response(
            [
                {"value": "haiku", "resolvedModel": "claude-haiku-5-5"},
                {"value": "claude-opus-5"},
                {"value": "sonnet", "displayName": "Sonnet 5.5"},
            ]
        ),
    )
    assert catalog_read(binary) == {
        "haiku": "claude-haiku-5-5",
        "claude-opus-5": "claude-opus-5",
    }
    first = catalog_read(binary)
    first.clear()
    assert catalog_read(binary)["haiku"] == "claude-haiku-5-5"


@pytest.mark.parametrize(
    ("output", "error"),
    [
        ("invalid\n", "not JSON"),
        (response(None), "did not return models"),
        (response([{"value": 42}]), "invalid entry"),
        ("x" * (1024 * 1024 + 1), "byte limit"),
        ("", "did not answer"),
    ],
)
def test_native_catalogue_refuses_invalid_output(
    tmp_path: Path, output: str, error: str
) -> None:
    with pytest.raises(ModelCatalogError, match=error):
        catalog_read(client(tmp_path, output))


def test_native_catalogue_requires_an_absolute_binary() -> None:
    with pytest.raises(ModelCatalogError, match="declared binary"):
        catalog_read("claude")


@pytest.mark.parametrize("stage", ["before", "after"])
def test_catalogue_identity_failures_use_the_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    from nspawn import claude_child

    binary = client(
        tmp_path, response([{"value": "haiku", "resolvedModel": "claude-haiku-5-5"}])
    )
    identity_read = claude_child.binary_identity
    calls = 0

    def read(path: str) -> claude_child.BinaryIdentity:
        nonlocal calls
        calls += 1
        if calls == (1 if stage == "before" else 2):
            raise claude_child.BinaryIdentityError(path, "test identity failure")
        return identity_read(path)

    monkeypatch.setattr(claude_child, "binary_identity", read)
    with pytest.raises(ModelCatalogError):
        catalog_read(binary)


def test_catalogue_does_not_start_a_writable_binary(tmp_path: Path) -> None:
    binary = client(
        tmp_path, response([{"value": "haiku", "resolvedModel": "claude-haiku-5-5"}])
    )
    marker = tmp_path / "started"
    binary.write_text(
        binary.read_text().replace(
            "request = json.loads",
            f"open({str(marker)!r}, 'w').write('started')\nrequest = json.loads",
        )
    )
    binary.chmod(0o777)
    with pytest.raises(ModelCatalogError):
        catalog_read(binary)
    assert not marker.exists()


def test_catalogue_starts_a_js_entry_with_the_installed_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = shutil.which("node")
    assert runtime is not None, "This test requires the installed Node runtime"
    monkeypatch.setenv("PATH", str(Path(runtime).parent))
    entry = tmp_path / "cli.js"
    entry.write_text(
        "#!/usr/bin/env missing-test-node\n"
        "const fs = require('fs');\n"
        "const request = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "if (request.request.subtype !== 'initialize') process.exit(4);\n"
        "if (process.env.ANTHROPIC_AUTH_TOKEN) process.exit(5);\n"
        + f"process.stdout.write({json.dumps(response([{'value': 'haiku', 'resolvedModel': 'claude-haiku-5-5'}]))});\n"
    )
    entry.chmod(0o700)
    binary = tmp_path / "claude"
    binary.symlink_to(entry)
    assert catalog_read(binary) == {"haiku": "claude-haiku-5-5"}
