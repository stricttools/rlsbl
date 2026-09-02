"""Tests for `rlsbl monorepo add` creating the releasable it names.

An add naming a releasable that ``[[releasables]]`` does not declare creates it,
the same auto-singleton absorb creates for an arriving member: one entry, with
its ``tag_format`` written EXPLICITLY, derived from the member's primary target
scheme. An add naming one that IS declared joins it, and ``--releasable false``
opts out -- both unchanged.
"""

import json
import os
from unittest.mock import patch

import pytest

import rlsbl
from rlsbl.commands.monorepo import _cmd_add, _cmd_init
from rlsbl.commands.monorepo.commands import _create_releasable
from rlsbl.commands.monorepo.sync import scaffold_releasable_dirs
from rlsbl.workspace import (
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    load_releasables,
    load_workspace,
)


def _npm_project(base_path, subdir):
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": "test-" + os.path.basename(subdir), "version": "0.1.0"}, f)
    return proj_dir


def _go_project(base_path, subdir):
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "go.mod"), "w") as f:
        f.write("module example.com/" + os.path.basename(subdir) + "\n\ngo 1.22\n")
    with open(os.path.join(proj_dir, "VERSION"), "w") as f:
        f.write("0.1.0\n")
    return proj_dir


def _workspace_text(root):
    with open(os.path.join(str(root), WORKSPACE_DIR, WORKSPACE_FILE), encoding="utf-8") as f:
        return f.read()


def _releasable(root, name):
    projects = load_workspace(str(root))
    for rel in load_releasables(str(root), projects):
        if rel.name == name:
            return rel
    return None


class TestAddCreatesReleasable:
    def test_new_releasable_is_created_with_explicit_tag_format(self, mock_git_repo):
        """An add naming an undeclared releasable creates it, format written out."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "pkg-a"}, project_root=".")

        rel = _releasable(mock_git_repo, "pkg-a")
        assert rel is not None
        assert rel.declares_tag_format
        assert rel.tag_format == "{name}@v{version}"
        assert 'tag_format = "{name}@v{version}"' in _workspace_text(mock_git_repo)

    def test_created_releasable_name_may_differ_from_member_name(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "packages/cli")
        _cmd_add(
            ["packages/cli"],
            {"releasable": "toolkit", "name": "cli"},
            project_root=".",
        )

        rel = _releasable(mock_git_repo, "toolkit")
        assert rel is not None
        assert rel.tag_format == "{name}@v{version}"
        member = [p for p in load_workspace(str(mock_git_repo)) if p["path"] == "packages/cli"][0]
        assert member["releasable"] == "toolkit"

    def test_go_member_derives_the_path_scheme(self, mock_git_repo):
        """Go tags through the module proxy's path scheme, not the @-scheme."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _go_project(mock_git_repo, "packages/widget")
        _cmd_add(["packages/widget"], {"releasable": "widget"}, project_root=".")

        rel = _releasable(mock_git_repo, "widget")
        assert rel is not None
        assert rel.tag_format == "packages/widget/v{version}"

    def test_sync_scaffolds_the_created_releasable_state(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "pkg-a"}, project_root=".")

        scaffold_releasable_dirs(str(mock_git_repo))
        rel_dir = os.path.join(
            str(mock_git_repo), WORKSPACE_DIR, "releasables", "pkg-a",
        )
        assert os.path.isfile(os.path.join(rel_dir, "version"))
        assert os.path.isfile(os.path.join(rel_dir, "changes", "unreleased.jsonl"))

    def test_creation_is_announced(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "pkg-a"}, project_root=".")
        out = capsys.readouterr().out
        assert "pkg-a" in out
        assert "{name}@v{version}" in out

    def test_an_existing_entry_keeps_its_absent_tag_format(self, mock_git_repo):
        """Creating one entry rewrites the section; absence still round-trips.

        The whole ``[[releasables]]`` list is handed to the writer when an add
        creates an entry, so a sibling that never declared ``tag_format`` must
        come back out without the key rather than with the default spelled in.
        """
        _cmd_init({"root-dev-node": True}, project_root=".")
        ws = os.path.join(str(mock_git_repo), WORKSPACE_DIR, WORKSPACE_FILE)
        with open(ws, encoding="utf-8") as f:
            text = f.read()
        assert "releasables = []" in text
        with open(ws, "w", encoding="utf-8") as f:
            f.write(text.replace(
                "releasables = []", '[[releasables]]\nname = "legacy"',
            ))

        _npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "pkg-a"}, project_root=".")

        legacy = _releasable(mock_git_repo, "legacy")
        assert legacy is not None
        assert not legacy.declares_tag_format
        text = _workspace_text(mock_git_repo)
        assert text.count("tag_format") == 1

    def test_dry_run_reports_the_creation_and_writes_nothing(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        capsys.readouterr()
        _cmd_add(["pkg-a"], {"releasable": "pkg-a"}, project_root=".", dry_run=True)

        out = capsys.readouterr().out
        assert "{name}@v{version}" in out
        assert _releasable(mock_git_repo, "pkg-a") is None


class TestAddMixedTagSchemes:
    def test_mixed_schemes_refuse_and_name_the_flag(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "packages/widget")
        _go_project(mock_git_repo, "packages/widget")

        with pytest.raises(SystemExit):
            _cmd_add(["packages/widget"], {"releasable": "widget"}, project_root=".")
        err = capsys.readouterr().err
        assert "incompatible monorepo tag schemes" in err
        assert "--tag-format" in err
        assert _releasable(mock_git_repo, "widget") is None

    def test_tag_format_resolves_the_refusal(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "packages/widget")
        _go_project(mock_git_repo, "packages/widget")

        _cmd_add(
            ["packages/widget"],
            {"releasable": "widget", "tag-format": "packages/widget/v{version}"},
            project_root=".",
        )
        rel = _releasable(mock_git_repo, "widget")
        assert rel is not None
        assert rel.tag_format == "packages/widget/v{version}"


class TestAddTagFormatLegality:
    def test_tag_format_is_illegal_when_joining_an_existing_releasable(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        _npm_project(mock_git_repo, "pkg-b")
        _cmd_add(["pkg-a"], {"releasable": "core"}, project_root=".")
        capsys.readouterr()

        with pytest.raises(SystemExit):
            _cmd_add(
                ["pkg-b"],
                {"releasable": "core", "tag-format": "v{version}"},
                project_root=".",
            )
        err = capsys.readouterr().err
        assert "--tag-format" in err
        # The existing releasable keeps the format it was created with.
        assert _releasable(mock_git_repo, "core").tag_format == "{name}@v{version}"

    def test_tag_format_is_illegal_with_releasable_false(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")

        with pytest.raises(SystemExit):
            _cmd_add(
                ["pkg-a"],
                {"releasable": "false", "tag-format": "v{version}"},
                project_root=".",
            )
        err = capsys.readouterr().err
        assert "--tag-format" in err


class TestAddUnchangedPaths:
    def test_joining_an_existing_releasable_adds_no_entry(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        _npm_project(mock_git_repo, "pkg-b")
        _cmd_add(["pkg-a"], {"releasable": "core"}, project_root=".")
        _cmd_add(["pkg-b"], {"releasable": "core"}, project_root=".")

        projects = load_workspace(str(mock_git_repo))
        rels = load_releasables(str(mock_git_repo), projects)
        assert [r.name for r in rels] == ["core"]
        assert {p["name"]: p["releasable"] for p in projects if p["path"] != "."} == {
            "pkg-a": "core", "pkg-b": "core",
        }

    def test_releasable_false_declares_no_releasable(self, mock_git_repo):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")

        projects = load_workspace(str(mock_git_repo))
        assert load_releasables(str(mock_git_repo), projects) == []
        member = [p for p in projects if p["path"] == "pkg-a"][0]
        assert member["releasable"] is False

    def test_missing_releasable_flag_still_errors(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _npm_project(mock_git_repo, "pkg-a")
        with pytest.raises(SystemExit):
            _cmd_add(["pkg-a"], {}, project_root=".")
        assert "--releasable is required" in capsys.readouterr().err


class TestRootMemberReleasable:
    """`add` never creates the root member's releasable.

    A workspace this command can load already declares a root member, so an add
    naming the repository root is refused as a path the workspace claims --
    before any releasable is created. Creating the root member's releasable is
    `monorepo init --root-releasable`'s job, which requires the format stated.
    """

    def test_add_of_the_repository_root_is_refused(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        with pytest.raises(SystemExit):
            _cmd_add(["."], {"releasable": "core", "target": "npm"}, project_root=".")
        assert "already exists in workspace" in capsys.readouterr().err
        assert _releasable(mock_git_repo, "core") is None

    def test_the_helper_takes_the_format_as_stated(self):
        from rlsbl.targets import TargetEntry

        rel = _create_releasable(
            "core", "v{version}", [TargetEntry(name="npm", path=".")], ".",
        )
        assert rel.name == "core"
        assert rel.tag_format == "v{version}"


@pytest.mark.repo_cwd
class TestAddTagFormatCliWiring:
    """--tag-format reaches the handler under the name the handler reads."""

    def test_tag_format_reaches_the_dispatch_target(self):
        with patch("rlsbl.commands.monorepo._cmd_add") as m:
            result = rlsbl.app.test(
                ["monorepo", "add", "pkgs/foo", "--releasable", "foo",
                 "--tag-format", "pkgs/foo/v{version}"]
            )
        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] == ["pkgs/foo"]
        assert m.call_args[0][1]["tag-format"] == "pkgs/foo/v{version}"
