"""Smoke tests for the monorepo_fixture and make_workspace conftest helpers."""

import subprocess

import pytest
import tomlkit

from conftest import declared_members, make_workspace
from rlsbl.workspace import Releasable, load_releasables, load_workspace, members_of


def test_workspace_toml_exists_with_two_projects(monorepo_fixture):
    ws_file = monorepo_fixture.root / ".rlsbl-monorepo" / "workspace.toml"
    assert ws_file.exists()
    data = tomlkit.loads(ws_file.read_text())
    # The two declared members, plus the mandatory root member.
    assert len(data["projects"]) == 3
    assert [p["name"] for p in data["projects"] if p["path"] == "."] == ["root"]


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
        # Only the dict form declared a format; the instance and the bare
        # name declared none, so their effective format is the workspace
        # scheme while their declared value stays absent.
        assert not by_name["one"].declares_tag_format
        assert by_name["one"].effective_tag_format == "{name}@v{version}"
        assert by_name["two"].tag_format == "{name}/v{version}"
        assert not by_name["three"].declares_tag_format
        assert by_name["three"].effective_tag_format == "{name}@v{version}"

    def test_releasables_are_derived_without_the_argument(self, tmp_path):
        """Omitting the argument derives one releasable per releasable member.

        A workspace with no [[releasables]] section is implicit mode, which the
        loader refuses, so the helper never writes one.
        """
        make_workspace(tmp_path, [{"path": "a", "name": "a"}])
        data = tomlkit.loads(
            (tmp_path / ".rlsbl-monorepo" / "workspace.toml").read_text()
        )
        assert [r["name"] for r in data["releasables"]] == ["a"]
        by_name = {p["name"]: p for p in data["projects"]}
        assert by_name["a"]["releasable"] == "a"
        assert by_name["root"]["releasable"] is False

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


class TestMakeWorkspaceProjectKeys:
    """Every key save_workspace serializes survives make_workspace.

    A silently dropped key produced a workspace that did not describe what the
    test declared -- the test then asserted against a graph, a mirror remote or
    a registry name that was never written.
    """

    def test_depends_on_round_trips(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": "lib", "name": "lib"},
                {"path": "app", "name": "app", "depends_on": ["lib"]},
            ],
        )
        by_name = {p.name: p for p in load_workspace(str(tmp_path))}
        assert by_name["app"].depends_on == ["lib"]
        assert by_name["lib"].depends_on == []

    def test_subtree_remote_round_trips(self, tmp_path):
        make_workspace(
            tmp_path,
            [{"path": "a", "name": "a",
              "subtree_remote": "git@github.com:o/a.git"}],
        )
        assert declared_members(load_workspace(str(tmp_path)))[0].subtree_remote == (
            "git@github.com:o/a.git"
        )

    def test_registry_and_import_names_round_trip(self, tmp_path):
        make_workspace(
            tmp_path,
            [{"path": "a", "name": "a",
              "registry_name": "a-on-pypi", "import_name": "a_pkg"}],
        )
        proj = declared_members(load_workspace(str(tmp_path)))[0]
        assert proj.registry_name == "a-on-pypi"
        assert proj.import_name == "a_pkg"

    def test_library_and_dev_flags_round_trip(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": "a", "name": "a", "library": True},
                {"path": "t", "name": "t", "dev_only": True, "dev_node": True},
            ],
        )
        by_name = {p.name: p for p in load_workspace(str(tmp_path))}
        assert by_name["a"].library is True
        assert by_name["t"].dev_only is True
        assert by_name["t"].dev_node is True

    def test_watch_key_is_refused(self, tmp_path):
        """The helper serializes only keys workspace.toml still carries."""
        with pytest.raises(ValueError, match="watch"):
            make_workspace(
                tmp_path, [{"path": "a", "name": "a", "watch": ["a/**"]}],
            )

    def test_unknown_key_is_refused(self, tmp_path):
        """A key make_workspace cannot serialize is a hard error naming it --
        never a workspace quietly missing what the caller declared."""
        with pytest.raises(ValueError) as exc_info:
            make_workspace(
                tmp_path,
                [{"path": "a", "name": "a", "dependz_on": ["b"]}],
            )
        msg = str(exc_info.value)
        assert "dependz_on" in msg
        assert "a" in msg

    def test_known_keys_are_not_refused(self, tmp_path):
        """The guard rejects only genuinely unknown keys."""
        make_workspace(
            tmp_path,
            [{
                "path": "a", "name": "a", "library": True,
                "dev_only": False, "dev_node": False, "releasable": "core",
                "depends_on": [], "subtree_remote": "", "registry_name": "a",
                "import_name": "a",
            }],
            releasables=["core"],
        )
        assert declared_members(load_workspace(str(tmp_path)))[0].name == "a"


class TestMakeWorkspaceRootMember:
    """The repository root itself can be a workspace member."""

    def test_root_member_is_declared(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": ".", "name": "root"},
                {"path": "pkgA", "name": "pkgA"},
            ],
        )
        projects = load_workspace(str(tmp_path))
        by_name = {p.name: p for p in projects}
        assert by_name["root"].path == "."

    def test_root_member_of_a_releasable(self, tmp_path):
        make_workspace(
            tmp_path,
            [
                {"path": ".", "name": "root", "releasable": "core"},
                {"path": "pkgA", "name": "pkgA", "releasable": "core"},
            ],
            releasables=[{"name": "core", "tag_format": "v{version}"}],
        )
        projects = load_workspace(str(tmp_path))
        members = members_of("core", projects)
        assert {m.name for m in members} == {"root", "pkgA"}
        assert {m.path for m in members} == {".", "pkgA"}

    @pytest.mark.parametrize("spelling", ["", ".", "./"])
    def test_root_spellings_normalize(self, tmp_path, spelling):
        make_workspace(tmp_path, [{"path": spelling, "name": "root"}])
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
