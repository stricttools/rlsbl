"""An explicitly-empty flag value is refused, never read as absence.

``--name ""`` is not the same statement as omitting ``--name``: strictcli
delivers an omitted optional flag as absent, so a handler that receives the
empty string was given one on the command line. Reading it as absence turns a
statement into a silence -- most sharply on ``rlsbl monorepo release init
--releasables ""``, where an empty filter used to select EVERY releasable
instead of the none the caller named.

Every named string flag goes through one shared refusal at the CLI boundary
(``rlsbl._refuse_empty_flags``), so these tests drive the real parser rather
than any per-command check.
"""

import json
import os

import pytest

import rlsbl


def _npm_project(root, subdir):
    proj_dir = os.path.join(str(root), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as handle:
        json.dump({"name": subdir, "version": "0.1.0"}, handle)
    return proj_dir


def _assert_refused(result, flag):
    assert result.exit_code != 0, result.stdout
    combined = (result.stderr or "") + (result.stdout or "")
    assert flag in combined, combined
    assert "empty" in combined, combined


class TestMonorepoCommands:
    """The monorepo commands that take named string flags."""

    def test_add_refuses_an_empty_name(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        _npm_project(mock_git_repo, "pkg-a")
        result = rlsbl.app.test([
            "monorepo", "add", "pkg-a", "--releasable", "pkg-a", "--name", "",
        ])
        _assert_refused(result, "name")

    def test_add_refuses_an_empty_releasable(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        _npm_project(mock_git_repo, "pkg-a")
        result = rlsbl.app.test([
            "monorepo", "add", "pkg-a", "--releasable", "",
        ])
        _assert_refused(result, "releasable")

    def test_init_refuses_an_empty_root_releasable(self, mock_git_repo):
        result = rlsbl.app.test([
            "monorepo", "init",
            "--root-releasable", "", "--tag-format", "v{version}",
        ])
        _assert_refused(result, "root-releasable")

    def test_graph_refuses_an_empty_root(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test(["monorepo", "graph", "--root", ""])
        _assert_refused(result, "root")

    def test_graph_refuses_an_empty_reverse(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test(["monorepo", "graph", "--reverse", ""])
        _assert_refused(result, "reverse")

    def test_impact_refuses_an_empty_since(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test(["monorepo", "impact", "--since", ""])
        _assert_refused(result, "since")

    def test_absorb_refuses_an_empty_name(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "monorepo", "absorb", "/nowhere/src", "dest",
            "--name", "", "--approve-consequential",
        ])
        _assert_refused(result, "name")


class TestBatchReleaseInitFilter:
    """An empty --releasables used to mean "every releasable"."""

    def _workspace(self, root):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        for name in ("pkg-a", "pkg-b"):
            _npm_project(root, name)
            rlsbl.app.test(["monorepo", "add", name, "--releasable", name])

    def test_an_empty_filter_is_refused(self, mock_git_repo):
        self._workspace(mock_git_repo)
        result = rlsbl.app.test([
            "monorepo", "release", "init", "--releasables", "",
        ])
        _assert_refused(result, "releasables")

    def test_the_refused_run_writes_no_batch_file(self, mock_git_repo):
        self._workspace(mock_git_repo)
        rlsbl.app.test(["monorepo", "release", "init", "--releasables", ""])
        batch = (
            mock_git_repo / ".rlsbl-monorepo" / "releases" / "unreleased.toml"
        )
        assert not batch.exists(), batch.read_text()

    def test_a_named_filter_still_selects_exactly_it(self, mock_git_repo):
        self._workspace(mock_git_repo)
        result = rlsbl.app.test([
            "monorepo", "release", "init", "--releasables", "pkg-a",
        ])
        assert result.exit_code == 0, result.stderr
        text = (
            mock_git_repo / ".rlsbl-monorepo" / "releases" / "unreleased.toml"
        ).read_text()
        assert "pkg-a" in text
        assert "[releasables.pkg-b]" not in text


class TestDevInstallFilters:
    def test_include_refuses_an_empty_value(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "dev", "install", "--target", "venv", "--include", "",
        ])
        _assert_refused(result, "include")

    def test_exclude_refuses_an_empty_value(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "dev", "install", "--target", "venv", "--exclude", "",
        ])
        _assert_refused(result, "exclude")


class TestTargetResolvers:
    def test_scaffold_refuses_an_empty_target(self, mock_git_repo):
        _npm_project(mock_git_repo, ".")
        result = rlsbl.app.test([
            "scaffold", "--target", "", "--no-auto-commit", "--no-auto-tag",
        ])
        _assert_refused(result, "target")

    def test_check_name_refuses_an_empty_target(self, mock_git_repo):
        result = rlsbl.app.test(["check-name", "somename", "--target", ""])
        assert result.exit_code != 0


class TestWhitespaceCountsAsEmpty:
    def test_a_blank_value_is_refused_like_an_empty_one(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test(["monorepo", "graph", "--root", "   "])
        _assert_refused(result, "root")


class TestOmittedFlagsStillWork:
    """The refusal is about a SUPPLIED empty value, never about absence."""

    def test_graph_without_root_still_runs(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test(["monorepo", "graph"])
        assert result.exit_code == 0, result.stderr

    def test_batch_init_without_a_filter_scaffolds_every_releasable(
        self, mock_git_repo
    ):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        for name in ("pkg-a", "pkg-b"):
            _npm_project(mock_git_repo, name)
            rlsbl.app.test(["monorepo", "add", name, "--releasable", name])

        result = rlsbl.app.test(["monorepo", "release", "init"])
        assert result.exit_code == 0, result.stderr
        text = (
            mock_git_repo / ".rlsbl-monorepo" / "releases" / "unreleased.toml"
        ).read_text()
        assert "pkg-a" in text
        assert "pkg-b" in text
