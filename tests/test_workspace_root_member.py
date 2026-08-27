"""Tests for the mandatory root member and the workspace model loader errors.

Every workspace declares the repository root as a member. It owns every tracked
file no other member claims, its name is the reserved literal ``root``, and the
loader refuses a workspace that violates any part of the model -- each refusal
carrying its own remedy.
"""

import json
import os
import subprocess

import pytest

from conftest import workspace_toml
from rlsbl.errors import WorkspaceError
from rlsbl.ownership import ROOT_MEMBER_NAME, ROOT_MEMBER_PATH
from rlsbl.workspace import (
    LAST_IMPLICIT_MODE_VERSION,
    WORKSPACE_DIR,
    WORKSPACE_FILE,
    is_explicit_mode,
    load_releasables,
    load_workspace,
    resolve_project,
)


def write_raw(root, body):
    """Write workspace.toml verbatim, with nothing supplied."""
    ws_dir = root / WORKSPACE_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / WORKSPACE_FILE).write_text(body)
    return ws_dir / WORKSPACE_FILE


# ---------------------------------------------------------------------------
# (a) no root member
# ---------------------------------------------------------------------------


class TestNoRootMember:
    def test_missing_root_member_is_an_error(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n',
            root_member="",
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "declares no root member" in message
        # The remedy is inline and complete: both kinds, spelled out.
        assert 'path = "."' in message
        assert f'name = "{ROOT_MEMBER_NAME}"' in message
        assert "dev_only = true" in message
        assert "releasable =" in message
        assert "migration script" in message

    def test_a_root_member_makes_it_load(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n',
        ))
        projects = load_workspace(str(tmp_path))
        assert sorted(p["name"] for p in projects) == ["a", ROOT_MEMBER_NAME]


# ---------------------------------------------------------------------------
# (b) a root member named anything but `root`
# ---------------------------------------------------------------------------


class TestRootMemberName:
    def test_wrongly_named_root_member_is_an_error(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "."\nname = "monorepo"\ndev_only = true\n'
            'releasable = false\n',
            root_member="",
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "'monorepo'" in message
        assert f"'{ROOT_MEMBER_NAME}'" in message
        assert "migration script" in message

    def test_omitted_name_is_the_reserved_one(self, tmp_path):
        """A root member with no name gets `root`, not the basename of ""."""
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "."\ndev_only = true\nreleasable = false\n',
            root_member="",
        ))
        projects = load_workspace(str(tmp_path))
        assert projects[0]["name"] == ROOT_MEMBER_NAME

    @pytest.mark.parametrize("spelling", ['""', '"."', '"./"'])
    def test_root_path_spellings_normalize(self, tmp_path, spelling):
        write_raw(tmp_path, workspace_toml(
            f'[[projects]]\npath = {spelling}\ndev_only = true\n'
            'releasable = false\n',
            root_member="",
        ))
        projects = load_workspace(str(tmp_path))
        assert projects[0]["path"] == ROOT_MEMBER_PATH
        assert projects[0]["name"] == ROOT_MEMBER_NAME


# ---------------------------------------------------------------------------
# (c) a non-root member named `root`
# ---------------------------------------------------------------------------


class TestReservedNameCollision:
    def test_non_root_member_named_root_is_an_error(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "packages/root"\nname = "root"\n'
            'releasable = false\n',
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "reserved" in message
        assert "packages/root" in message
        # An operator decision, not something a script can mechanically fix.
        assert "your decision" in message
        assert "migration script" not in message


# ---------------------------------------------------------------------------
# (d) the watch key
# ---------------------------------------------------------------------------


class TestWatchKeyRefused:
    def test_watch_key_is_an_error(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n'
            'watch = ["shared/**"]\n',
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "'watch'" in message
        assert "no longer supported" in message
        assert "most specific declared path" in message
        assert "migration script" in message


# ---------------------------------------------------------------------------
# (e) implicit mode
# ---------------------------------------------------------------------------


class TestImplicitModeRefused:
    def test_missing_releasables_section_is_an_error(self, tmp_path):
        write_raw(
            tmp_path,
            '[[projects]]\npath = "."\nname = "root"\ndev_only = true\n'
            'releasable = false\n',
        )
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "[[releasables]]" in message
        assert "implicit mode" in message
        # The remedy for a workspace that cannot convert today: an exact pin
        # plus a todo, not a shrug.
        assert LAST_IMPLICIT_MODE_VERSION in message
        assert "todo" in message

    def test_empty_releasables_section_is_explicit_mode(self, tmp_path):
        write_raw(
            tmp_path,
            'releasables = []\n\n[[projects]]\npath = "."\nname = "root"\n'
            'dev_only = true\nreleasable = false\n',
        )
        projects = load_workspace(str(tmp_path))
        assert len(projects) == 1
        assert is_explicit_mode(str(tmp_path))
        assert load_releasables(str(tmp_path), projects) == []


# ---------------------------------------------------------------------------
# (f) a root-member releasable with no explicit tag format
# ---------------------------------------------------------------------------


class TestRootReleasableTagFormat:
    def test_default_tag_format_is_refused(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "."\nname = "root"\nreleasable = "core"\n',
            releasables=["core"],
            root_member="",
        ))
        with pytest.raises(WorkspaceError) as exc:
            load_workspace(str(tmp_path))
        message = str(exc.value)
        assert "'core'" in message
        assert "tag_format" in message
        assert "v{version}" in message
        assert "{name}@v{version}" in message
        # Which scheme the repository uses is the operator's to state.
        assert "Only you can say" in message

    @pytest.mark.parametrize("fmt", ["v{version}", "{name}@v{version}"])
    def test_explicit_tag_format_loads(self, tmp_path, fmt):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "."\nname = "root"\nreleasable = "core"\n',
            releasables=[{"name": "core", "tag_format": fmt}],
            root_member="",
        ))
        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        assert releasables[0].tag_format == fmt

    def test_a_non_root_releasable_may_inherit_the_default(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = "core"\n',
            releasables=["core"],
        ))
        releasables = load_releasables(str(tmp_path))
        assert releasables[0].name == "core"


# ---------------------------------------------------------------------------
# Root-member directory resolution
# ---------------------------------------------------------------------------


class TestResolveProjectWithRootMember:
    """Inside a workspace, some member always answers.

    A member declared at ``path = "."`` always matched every directory under
    the repository root; the root member being mandatory just makes that
    universal.
    """

    def test_repo_root_resolves_to_the_root_member(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n',
        ))
        resolved = resolve_project(str(tmp_path), str(tmp_path))
        assert resolved["name"] == ROOT_MEMBER_NAME

    def test_unclaimed_subdirectory_resolves_to_the_root_member(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n',
        ))
        (tmp_path / "docs").mkdir()
        resolved = resolve_project(str(tmp_path), str(tmp_path / "docs"))
        assert resolved["name"] == ROOT_MEMBER_NAME

    def test_member_directory_still_wins(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n',
        ))
        (tmp_path / "a" / "src").mkdir(parents=True)
        resolved = resolve_project(str(tmp_path), str(tmp_path / "a" / "src"))
        assert resolved["name"] == "a"

    def test_most_specific_member_directory_wins(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n\n'
            '[[projects]]\npath = "a/inner"\nname = "inner"\nreleasable = false\n',
        ))
        (tmp_path / "a" / "inner" / "src").mkdir(parents=True)
        resolved = resolve_project(str(tmp_path), str(tmp_path / "a" / "inner" / "src"))
        assert resolved["name"] == "inner"

    def test_outside_the_workspace_is_none(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        write_raw(repo, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = false\n',
        ))
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        assert resolve_project(str(repo), str(outside)) is None


# ---------------------------------------------------------------------------
# monorepo init: the root member's kind is declared, never defaulted
# ---------------------------------------------------------------------------


def _fresh_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True, timeout=30)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, check=True, timeout=30)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, timeout=30)
    (tmp_path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, timeout=30)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True, timeout=30)
    return tmp_path


class TestMonorepoInitRootMember:
    def test_dev_node_root_member(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo.commands import _cmd_init

        _fresh_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        _cmd_init({"auto-commit": False, "root-dev-node": True}, project_root=tmp_path)

        projects = load_workspace(str(tmp_path))
        assert [p["name"] for p in projects] == [ROOT_MEMBER_NAME]
        assert projects[0]["path"] == ROOT_MEMBER_PATH
        assert projects[0]["dev_only"] is True
        # Explicit mode with no releasables yet -- not implicit mode.
        assert is_explicit_mode(str(tmp_path))
        assert load_releasables(str(tmp_path), projects) == []

    def test_releasable_root_member(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo.commands import _cmd_init

        _fresh_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        _cmd_init(
            {
                "auto-commit": False,
                "root-releasable": "core",
                "root-tag-format": "v{version}",
            },
            project_root=tmp_path,
        )

        projects = load_workspace(str(tmp_path))
        releasables = load_releasables(str(tmp_path), projects)
        assert projects[0]["releasable"] == "core"
        assert [r.name for r in releasables] == ["core"]
        assert releasables[0].tag_format == "v{version}"

    def test_missing_kind_is_a_hard_error(self, tmp_path, monkeypatch, capsys):
        from rlsbl.commands.monorepo.commands import _cmd_init

        _fresh_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _cmd_init({"auto-commit": False}, project_root=tmp_path)
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--root-dev-node" in err
        assert "--root-releasable" in err
        assert "no default" in err

    def test_releasable_without_tag_format_is_a_hard_error(
        self, tmp_path, monkeypatch, capsys,
    ):
        from rlsbl.commands.monorepo.commands import _cmd_init

        _fresh_repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit):
            _cmd_init(
                {"auto-commit": False, "root-releasable": "core"},
                project_root=tmp_path,
            )
        assert "--tag-format is required" in capsys.readouterr().err

    def test_the_cli_demands_the_kind(self):
        """The choice flag is required, so argv without it is refused."""
        from rlsbl import app

        result = app.test(["monorepo", "init", "--help"])
        assert "--root-dev-node" in result.stdout
        assert "--root-releasable" in result.stdout


# ---------------------------------------------------------------------------
# Checks that must not demand a manifest from a manifest-less root member
# ---------------------------------------------------------------------------


def _workspace_ctx(root):
    from pathlib import Path

    from rlsbl.check_context import WorkspaceCheckContext

    projects = load_workspace(str(root))
    return WorkspaceCheckContext(
        project_root=Path(root),
        workspace_root=Path(root),
        config={},
        projects=projects,
        releasables=load_releasables(str(root), projects),
    )


class TestRootMemberCheckExemptions:
    def _repo(self, tmp_path):
        write_raw(tmp_path, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = "a"\n',
            releasables=["a"],
        ))
        pkg = tmp_path / "a"
        pkg.mkdir()
        (pkg / "pyproject.toml").write_text(
            '[project]\nname = "a"\nversion = "0.1.0"\n'
        )
        (pkg / ".rlsbl").mkdir()
        (pkg / ".rlsbl" / "config.json").write_text(
            json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
        )
        return tmp_path

    def test_targets_check_exempts_a_manifestless_root_member(self, tmp_path):
        from conftest import capture_all_checks

        repo = self._repo(tmp_path)
        assert not os.path.isfile(repo / "pyproject.toml")
        checks = capture_all_checks()
        result = checks["workspace-targets"](_workspace_ctx(repo))
        assert result.status == "pass", result

    def test_stale_entries_check_exempts_a_manifestless_root_member(self, tmp_path):
        from conftest import capture_all_checks

        repo = self._repo(tmp_path)
        checks = capture_all_checks()
        result = checks["workspace-stale-entries"](_workspace_ctx(repo))
        assert result.status == "pass", result

    def test_a_manifestless_regular_member_is_still_stale(self, tmp_path):
        from conftest import capture_all_checks

        repo = self._repo(tmp_path)
        (repo / "b").mkdir()
        write_raw(repo, workspace_toml(
            '[[projects]]\npath = "a"\nname = "a"\nreleasable = "a"\n\n'
            '[[projects]]\npath = "b"\nname = "b"\nreleasable = false\n',
            releasables=["a"],
        ))
        checks = capture_all_checks()
        result = checks["workspace-stale-entries"](_workspace_ctx(repo))
        assert result.status == "fail", result


# ---------------------------------------------------------------------------
# Derivations keyed on the reserved name
# ---------------------------------------------------------------------------


class TestReservedNameDerivations:
    def test_router_job_keys_are_valid_workflow_keys(self, tmp_path):
        import re

        from rlsbl.ci_router import _router_ci_job_keys

        member = {"path": ".", "name": ROOT_MEMBER_NAME}
        keys = _router_ci_job_keys(member, project_dir=str(tmp_path))
        # No CI sources on disk: the fallback key is still derived from the
        # reserved name, and is a legal GitHub job id.
        assert keys
        for key in keys:
            assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key), key
            assert key.startswith(ROOT_MEMBER_NAME)

    def test_root_publisher_prefix_uses_the_reserved_name(self, tmp_path):
        from rlsbl.commands.monorepo.publish_inline import _root_job_prefix

        member = {"path": ".", "name": ROOT_MEMBER_NAME}
        assert _root_job_prefix(member, str(tmp_path)) == ROOT_MEMBER_NAME
