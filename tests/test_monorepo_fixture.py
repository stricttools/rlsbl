"""Smoke tests for the monorepo_fixture and make_workspace conftest helpers."""

import subprocess

import pytest
import tomlkit

from conftest import make_workspace
from rlsbl.workspace import Releasable, load_releasables, load_workspace, members_of


def test_workspace_toml_exists_with_two_projects(monorepo_fixture):
    ws_file = monorepo_fixture.root / ".rlsbl-monorepo" / "workspace.toml"
    assert ws_file.exists()
    data = tomlkit.loads(ws_file.read_text())
    assert len(data["projects"]) == 2


def test_both_tags_exist(monorepo_fixture):
    result = subprocess.run(
        ["git", "tag", "-l"],
        cwd=str(monorepo_fixture.root),
        capture_output=True,
        text=True,
        check=True,
    )
    tags = result.stdout.strip().splitlines()
    assert "mypylib@v0.1.0" in tags
    assert "mygolib@v0.1.0" in tags


def test_unreleased_jsonl_files_exist(monorepo_fixture):
    assert (monorepo_fixture.python_dir / ".rlsbl" / "changes" / "unreleased.jsonl").exists()
    assert (monorepo_fixture.go_dir / ".rlsbl" / "changes" / "unreleased.jsonl").exists()


class TestMakeWorkspaceReleasables:
    """make_workspace can write an explicit-mode workspace."""

    def test_releasables_are_loadable(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": "pkgA", "name": "pkgA", "releasable": "core"},
                {"path": "pkgB", "name": "pkgB", "releasable": "core"},
                {"path": "pkgC", "name": "pkgC", "releasable": "extras"},
            ],
            releasables=["core", "extras"],
        )
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        assert [r.name for r in releasables] == ["core", "extras"]
        assert {m.name for m in members_of("core", projects)} == {"pkgA", "pkgB"}

    def test_releasable_accepts_instance_dict_and_name(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": "a", "name": "a", "releasable": "one"},
                {"path": "b", "name": "b", "releasable": "two"},
                {"path": "c", "name": "c", "releasable": "three"},
            ],
            releasables=[
                Releasable(name="one"),
                {"name": "two", "tag_format": "{name}/v{version}"},
                "three",
            ],
        )
        releasables = load_releasables(str(tmp_path))
        by_name = {r.name: r for r in releasables}
        assert by_name["one"].tag_format == "{name}@v{version}"
        assert by_name["two"].tag_format == "{name}/v{version}"
        assert by_name["three"].tag_format == "{name}@v{version}"

    def test_no_releasables_section_without_the_argument(self, tmp_path):
        make_workspace(tmp_path, [{"path": "a", "name": "a"}])
        data = tomlkit.loads(
            (tmp_path / ".rlsbl-monorepo" / "workspace.toml").read_text()
        )
        assert "releasables" not in data

    def test_project_outside_every_releasable(self, tmp_path):
        """``releasable = false`` stands a project outside every releasable in
        explicit mode -- the escape hatch load_releasables allows."""
        make_workspace(
            tmp_path,
            [
                {"path": "a", "name": "a", "releasable": "core"},
                {"path": "tools", "name": "tools", "releasable": False},
            ],
            releasables=["core"],
        )
        projects = load_workspace(str(tmp_path))
        load_releasables(str(tmp_path), projects)
        assert {m.name for m in members_of("core", projects)} == {"a"}


class TestMakeWorkspaceRootMember:
    """The repository root itself can be a workspace member."""

    def test_root_member_is_declared(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": ".", "name": "root-pkg"},
                {"path": "pkgA", "name": "pkgA"},
            ],
        )
        projects = load_workspace(str(tmp_path))
        by_name = {p.name: p for p in projects}
        assert by_name["root-pkg"].path == "."

    def test_root_member_of_a_releasable(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": ".", "name": "root-pkg", "releasable": "core"},
                {"path": "pkgA", "name": "pkgA", "releasable": "core"},
            ],
            releasables=["core"],
        )
        projects = load_workspace(str(tmp_path))
        members = members_of("core", projects)
        assert {m.name for m in members} == {"root-pkg", "pkgA"}
        assert {m.path for m in members} == {".", "pkgA"}

    @pytest.mark.parametrize("spelling", ["", ".", "./"])
    def test_root_spellings_normalize(self, tmp_path, spelling):
        make_workspace(tmp_path, [{"path": spelling, "name": "root-pkg"}])
        projects = load_workspace(str(tmp_path))
        assert projects[0].path == "."

    def test_two_root_members_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="repository root"):
            make_workspace(
                tmp_path,
                [
                    {"path": ".", "name": "one"},
                    {"path": "./", "name": "two"},
                ],
            )
