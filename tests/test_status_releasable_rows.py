"""Tests for Phase 5.5: shared tag-glob resolver + per-releasable status rows.

`rlsbl monorepo status` previously rendered one row per project and derived
tag globs per-member even for releasable members, so releasable members showed
Tag "(none)". It now renders one row per releasable (version, tag, coverage,
members) using the shared tag-glob resolver, so the releasable's real tag is
shown.
"""

import os
import subprocess

from conftest import make_ctx  # noqa: F401  (keeps fixture wiring consistent)

from rlsbl.commands.monorepo import _cmd_status
from rlsbl.tag_glob import resolve_monorepo_tag_glob, releasable_tag_glob
from rlsbl.workspace import (
    Releasable,
    WorkspaceProject,
    save_workspace,
    write_releasable_version,
)


def _make_npm_project(base_path, subdir, version="0.1.0"):
    import json
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": subdir, "version": version}, f)
    return subdir


class TestSharedTagGlobResolver:
    def test_releasable_glob_from_tag_format(self, tmp_path):
        rel = Releasable(name="www", tag_format="{name}@v{version}")
        glob = resolve_monorepo_tag_glob(None, str(tmp_path), releasable=rel)
        assert glob == "www@v*"

    def test_releasable_glob_custom_format(self, tmp_path):
        rel = Releasable(name="core", tag_format="v{version}")
        assert resolve_monorepo_tag_glob(None, str(tmp_path), releasable=rel) == "v*"

    def test_releasable_tag_glob_helper(self):
        assert releasable_tag_glob("{name}@v{version}", "www") == "www@v*"
        assert releasable_tag_glob("v{version}", "core") == "v*"

    def test_target_glob_when_no_releasable(self, tmp_path):
        _make_npm_project(tmp_path, "pkg-a", version="1.0.0")
        proj = WorkspaceProject({"name": "pkg-a", "path": "pkg-a"})
        glob = resolve_monorepo_tag_glob(proj, str(tmp_path), releasable=None)
        assert glob == "pkg-a@v*"


class TestPerReleasableStatusRows:
    def test_releasable_row_shows_real_tag(self, mock_git_repo, capsys):
        """A releasable member must show the releasable's real tag, not (none)."""
        _make_npm_project(mock_git_repo, "pkg-a", version="1.0.0")
        _make_npm_project(mock_git_repo, "pkg-b", version="1.0.0")
        save_workspace(
            str(mock_git_repo),
            [
                {"path": "pkg-a", "name": "pkg-a", "releasable": "alpha"},
                {"path": "pkg-b", "name": "pkg-b", "releasable": "alpha"},
            ],
            releasables=[Releasable(name="alpha")],
        )
        write_releasable_version(str(mock_git_repo), "alpha", "1.0.0")
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        out = capsys.readouterr().out

        # One row for the releasable with its real tag (regression: was "(none)")
        assert "alpha@v1.0.0" in out
        assert "(none)" not in out
        # Per-releasable header + members column
        assert "Releasable" not in out.split("\n")[0] or "Members" in out
        assert "Members" in out
        assert "Kind" in out
        # Members column lists both members
        assert "pkg-a" in out and "pkg-b" in out
        # Releasable version shown
        assert "1.0.0" in out

    def test_standalone_project_keeps_own_row(self, mock_git_repo, capsys):
        """A standalone project (releasable=false) keeps its own row alongside
        releasable rows."""
        _make_npm_project(mock_git_repo, "lib", version="2.0.0")
        _make_npm_project(mock_git_repo, "tool", version="3.0.0")
        save_workspace(
            str(mock_git_repo),
            [
                {"path": "lib", "name": "lib", "releasable": "alpha"},
                {"path": "tool", "name": "tool", "releasable": False},
            ],
            releasables=[Releasable(name="alpha")],
        )
        write_releasable_version(str(mock_git_repo), "alpha", "2.0.0")

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        out = capsys.readouterr().out

        assert "alpha" in out   # releasable row
        assert "tool" in out    # standalone project row
        # tool is a project row, not counted as a releasable member
        tool_line = [l for l in out.split("\n") if l.startswith("tool")][0]
        assert "project" in tool_line
