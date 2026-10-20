"""Installed recipe advice uses one string-map reader."""

from pathlib import Path

import pytest

from nspawn.install import InstallRefused, installed, metadata_read, skills_target


def skill(home: Path, monkeypatch: pytest.MonkeyPatch, metadata: str) -> None:
    monkeypatch.setenv("HOME", str(home))
    root = skills_target("claude") / "deploy"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Deploy an admitted release.\n"
        "metadata:\n" + metadata + "---\nUse the ship route.\n"
    )


def test_installed_recipe_retains_quoted_empty_server_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill(
        tmp_path,
        monkeypatch,
        "  nott-kind: recipe\n  nott-provider: claude\n  nott-model: haiku\n"
        "  nott-skills: 'ship'\n  nott-mcp: \"\"\n",
    )
    rows = installed("claude", "skill")
    assert rows[0].get("metadata") == {
        "nott-kind": "recipe",
        "nott-provider": "claude",
        "nott-model": "haiku",
        "nott-skills": "ship",
        "nott-mcp": "",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        "  nott-kind: recipe\n  nott-kind: recipe\n",
        "  nott-kind: [recipe]\n",
        "  nott-kind:\n    nested: recipe\n",
        "  nott-kind: |\n    recipe\n",
        "  nott-kind: {value: recipe}\n",
        '  nott-kind: "recipe" trailing\n',
    ],
)
def test_recipe_reader_refuses_non_string_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    skill(tmp_path, monkeypatch, metadata)
    with pytest.raises(InstallRefused) as error:
        metadata_read(skills_target("claude") / "deploy/SKILL.md")
    assert str(error.value) == (
        "recipe metadata must contain unique string keys and values: deploy"
    )


def test_metadata_size_limit_applies_to_frontmatter_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill(tmp_path, monkeypatch, '  nott-kind: recipe\n  nott-mcp: ""\n')
    document = skills_target("claude") / "deploy/SKILL.md"
    with document.open("a") as stream:
        stream.write("Reference material.\n" * 10000)
    assert installed("claude", "skill")[0]["metadata"]["nott-mcp"] == ""


def test_quoted_scalar_round_trip_and_control_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import random

    from nspawn.install import metadata_read

    randomizer = random.Random(712)
    folder = tmp_path / "deploy"
    folder.mkdir()
    document = folder / "SKILL.md"
    for _ in range(100):
        value = "".join(
            randomizer.choices("abc XYZ-:_#'\"", k=randomizer.randrange(40))
        )
        document.write_text("---\nmetadata:\n  value: " + json.dumps(value) + "\n---\n")
        assert metadata_read(document) == {"value": value}
    for value in ("secret\nline", "secret\x00line", "secret\tline"):
        document.write_text("---\nmetadata:\n  value: " + json.dumps(value) + "\n---\n")
        with pytest.raises(
            InstallRefused,
            match="^recipe metadata must contain unique string keys and values: deploy$",
        ):
            metadata_read(document)


def test_bad_metadata_does_not_hide_other_installed_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill(tmp_path, monkeypatch, "  custom: [nested]\n")
    good = skills_target("claude") / "watch-pr"
    good.mkdir()
    (good / "SKILL.md").write_text("---\nmetadata:\n  nott-kind: recipe\n---\n")
    rows = installed("claude", "skill")
    assert [row["name"] for row in rows] == ["deploy", "watch-pr"]
    assert "metadata" not in rows[0]
    assert rows[1]["metadata"] == {"nott-kind": "recipe"}
