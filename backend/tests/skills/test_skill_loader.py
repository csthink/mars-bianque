"""Loading skills validates every rule of the Agent Skills format strictly."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bianque.skills import SkillError, activate, load_skills, skill_content
from bianque.skills.activation import activation_parameters, render_catalog


def _write(root: Path, directory: str, text: str) -> Path:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(name: str, description: str = "Investigate a thing.", body: str = "Do it.\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n{body}"


def test_skills_are_sorted_by_name_and_parsed(tmp_path: Path) -> None:
    _write(tmp_path, "zeta-check", _skill("zeta-check", "Last one.", "Step one.\nStep two.\n"))
    _write(tmp_path, "alpha-check", _skill("alpha-check", "First one."))
    catalog = load_skills(tmp_path)
    assert catalog.names == ("alpha-check", "zeta-check")
    zeta = catalog.get("zeta-check")
    assert zeta is not None
    assert zeta.description == "Last one."
    assert zeta.body == "Step one.\nStep two."
    assert zeta.path == tmp_path / "zeta-check" / "SKILL.md"
    assert len(zeta.sha256) == 64
    assert set(catalog.hashes()) == {"alpha-check/SKILL.md", "zeta-check/SKILL.md"}
    assert catalog.get("missing") is None


def test_an_empty_directory_has_no_skills(tmp_path: Path) -> None:
    catalog = load_skills(tmp_path)
    assert not catalog
    assert catalog.names == ()


def test_a_missing_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SkillError, match="does not exist"):
        load_skills(tmp_path / "nowhere")


def test_optional_fields_of_the_format_are_accepted(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "full-skill",
        "---\nname: full-skill\ndescription: All fields.\nlicense: Apache-2.0\n"
        "compatibility: Needs the Bianque MCP server.\nmetadata:\n  author: someone\n"
        "allowed-tools: k8s_get_pod_status\n---\nBody.\n",
    )
    assert load_skills(tmp_path).names == ("full-skill",)


@pytest.mark.parametrize(
    ("directory", "text", "message"),
    [
        ("Upper", _skill("Upper"), "name"),
        ("-lead", _skill("-lead"), "name"),
        ("trail-", _skill("trail-"), "name"),
        ("two--hyphens", _skill("two--hyphens"), "name"),
        ("under_score", _skill("under_score"), "name"),
        ("a" * 65, _skill("a" * 65), "name"),
        ("other-name", _skill("this-name"), "directory"),
        ("no-name", "---\ndescription: x\n---\nBody.\n", "name"),
        ("no-description", "---\nname: no-description\n---\nBody.\n", "description"),
        ("long-description", _skill("long-description", "d" * 1025), "description"),
        ("empty-description", '---\nname: empty-description\ndescription: ""\n---\nB.\n',
         "description"),
        ("extra-field", "---\nname: extra-field\ndescription: x\nversion: 1\n---\nB.\n", "version"),
        ("long-compat", "---\nname: long-compat\ndescription: x\ncompatibility: "
         + "c" * 501 + "\n---\nB.\n", "compatibility"),
        ("bad-metadata", "---\nname: bad-metadata\ndescription: x\nmetadata:\n  n: 1\n---\nB.\n",
         "metadata"),
        ("no-frontmatter", "Just a body.\n", "front matter"),
        ("open-frontmatter", "---\nname: open-frontmatter\ndescription: x\n", "front matter"),
        ("not-a-mapping", "---\n- a list\n---\nBody.\n", "front matter"),
        ("long-body", _skill("long-body", body="line\n" * 500), "500 lines"),
        ("wide-body", _skill("wide-body", body="x" * 20001), "20000 characters"),
        ("empty-body", _skill("empty-body", body="\n"), "body"),
    ],
)  # fmt: skip
def test_each_rule_of_the_format_is_enforced(
    tmp_path: Path, directory: str, text: str, message: str
) -> None:
    path = _write(tmp_path, directory, text)
    with pytest.raises(SkillError, match=message) as raised:
        load_skills(tmp_path)
    assert str(path.parent) in str(raised.value)


def test_a_body_just_under_the_limits_is_accepted(tmp_path: Path) -> None:
    _write(tmp_path, "lines", _skill("lines", body="line\n" * 499))
    _write(tmp_path, "chars", _skill("chars", body="x" * 20000))
    assert load_skills(tmp_path).names == ("chars", "lines")


def test_a_scripts_directory_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "with-scripts", _skill("with-scripts"))
    (tmp_path / "with-scripts" / "scripts").mkdir()
    with pytest.raises(SkillError, match="scripts"):
        load_skills(tmp_path)


def test_an_executable_file_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "with-tool", _skill("with-tool"))
    tool = tmp_path / "with-tool" / "assets" / "run"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    os.chmod(tool, 0o755)
    with pytest.raises(SkillError, match="executable"):
        load_skills(tmp_path)


def test_a_loose_file_in_the_skills_directory_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "fine", _skill("fine"))
    (tmp_path / "README.md").write_text("notes\n")
    with pytest.raises(SkillError, match="README.md"):
        load_skills(tmp_path)


def test_a_directory_without_skill_md_is_refused(tmp_path: Path) -> None:
    (tmp_path / "empty-skill").mkdir()
    with pytest.raises(SkillError, match="SKILL.md"):
        load_skills(tmp_path)


def test_skill_content_wraps_the_body_and_lists_references_unread(tmp_path: Path) -> None:
    _write(tmp_path, "plain", _skill("plain", body="Check the pod.\n"))
    _write(tmp_path, "with-refs", _skill("with-refs", body="Read more.\n"))
    references = tmp_path / "with-refs" / "references"
    references.mkdir()
    (references / "b.md").write_text("B\n")
    (references / "a.md").write_text("A\n")
    catalog = load_skills(tmp_path)
    plain, with_refs = catalog.get("plain"), catalog.get("with-refs")
    assert plain is not None and with_refs is not None
    assert skill_content(plain) == '<skill_content name="plain">\nCheck the pod.\n</skill_content>'
    assert with_refs.references == ("references/a.md", "references/b.md")
    content = skill_content(with_refs)
    assert "references/a.md" in content and "references/b.md" in content
    assert "A\n" not in content


def test_the_catalog_and_the_activation_schema_list_names_in_order(tmp_path: Path) -> None:
    _write(tmp_path, "beta", _skill("beta", "Second.", "Secret body of beta.\n"))
    _write(tmp_path, "alpha", _skill("alpha", "First.", "Secret body of alpha.\n"))
    catalog = load_skills(tmp_path)
    assert render_catalog(catalog) == "- alpha: First.\n- beta: Second."
    parameters = activation_parameters(catalog)
    assert parameters["properties"]["name"]["enum"] == ["alpha", "beta"]
    assert parameters["required"] == ["name"]
    assert parameters["additionalProperties"] is False


def test_activation_loads_a_body_once_and_refuses_unknown_names(tmp_path: Path) -> None:
    _write(tmp_path, "alpha", _skill("alpha", "First.", "Body of alpha.\n"))
    catalog = load_skills(tmp_path)
    first = activate(catalog, [], '{"name": "alpha"}')
    assert first.name == "alpha" and first.first
    assert first.result == '<skill_content name="alpha">\nBody of alpha.\n</skill_content>'
    assert first.summary == f"sha256 {first.digest}; first load"
    assert len(first.digest) == 12
    again = activate(catalog, ["alpha"], '{"name": "alpha"}')
    assert again.name == "alpha" and not again.first
    assert "already loaded" in again.result and "Body of alpha" not in again.result
    assert again.summary == f"sha256 {first.digest}; already loaded"
    for arguments in ('{"name": "beta-secret-token"}', "not json", '{"other": 1}', "[]"):
        refused = activate(catalog, [], arguments)
        assert refused.name is None
        assert "alpha" in refused.result and "beta-secret-token" not in refused.result
