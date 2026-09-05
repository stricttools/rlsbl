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


class TestPositionalsAreNamedAsArguments:
    """A positional is not a flag, and the remedy must not invent one.

    The shared refusal used to render every name as ``--name``, so an empty
    positional produced a message naming a flag that does not exist and told
    the caller to "drop" an argument a required positional cannot be without.
    """

    def _assert_argument_refusal(self, result, argument):
        assert result.exit_code != 0, result.stdout
        combined = (result.stderr or "") + (result.stdout or "")
        assert f"the {argument} argument" in combined, combined
        assert "empty" in combined, combined
        assert f"--{argument}" not in combined, combined
        assert "drop" not in combined, combined

    def test_add_names_the_path_argument(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "monorepo", "add", "", "--releasable", "pkg-a",
        ])
        self._assert_argument_refusal(result, "path")

    def test_absorb_names_the_source_repo_argument(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "monorepo", "absorb", "", "dest", "--approve-consequential",
        ])
        self._assert_argument_refusal(result, "source_repo")

    def test_absorb_names_the_dest_path_argument(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "monorepo", "absorb", "/nowhere/src", "",
            "--approve-consequential",
        ])
        self._assert_argument_refusal(result, "dest_path")


class TestTheRemainingSilentSites:
    """Every supplied-but-empty value is refused, not read as absence.

    Each of these used to reach a handler that tested the value for truth and
    took the absent branch: ``--version ""`` selected the LATEST release,
    ``--description ""`` was a no-op reported as success, an empty ``--prefix``
    checked the unprefixed names, and an empty ``monorepo remove`` path exited
    0 with a warning.
    """

    def test_undo_refuses_an_empty_version(self, mock_git_repo):
        result = rlsbl.app.test([
            "release", "undo", "--version", "", "--approve-consequential",
        ])
        _assert_refused(result, "version")

    def test_backfill_refuses_an_empty_overrides(self, mock_git_repo):
        result = rlsbl.app.test([
            "release", "backfill", "--overrides", "",
            "--approve-consequential",
        ])
        _assert_refused(result, "overrides")

    def test_status_refuses_an_empty_target(self, mock_git_repo):
        _npm_project(mock_git_repo, ".")
        result = rlsbl.app.test(["status", "--target", ""])
        _assert_refused(result, "target")

    def test_deprecate_refuses_an_empty_reason(self, mock_git_repo):
        result = rlsbl.app.test([
            "release", "deprecate", "0.1.0", "--reason", "",
            "--approve-consequential",
        ])
        _assert_refused(result, "reason")

    def test_deprecate_refuses_an_empty_use(self, mock_git_repo):
        result = rlsbl.app.test([
            "release", "deprecate", "0.1.0", "--use", "",
            "--approve-consequential",
        ])
        _assert_refused(result, "use")

    def test_yank_refuses_an_empty_reason(self, mock_git_repo):
        result = rlsbl.app.test([
            "release", "yank", "0.1.0", "--reason", "",
            "--approve-consequential",
        ])
        _assert_refused(result, "reason")

    def test_yank_refuses_an_empty_use(self, mock_git_repo):
        result = rlsbl.app.test([
            "release", "yank", "0.1.0", "--use", "",
            "--approve-consequential",
        ])
        _assert_refused(result, "use")

    def test_release_edit_refuses_an_empty_version_argument(self, mock_git_repo):
        result = rlsbl.app.test(["release", "edit", ""])
        assert result.exit_code != 0
        combined = (result.stderr or "") + (result.stdout or "")
        assert "the version argument" in combined, combined
        assert "empty" in combined, combined

    def test_deploy_refuses_an_empty_target_name_argument(self, mock_git_repo):
        result = rlsbl.app.test([
            "deploy", "", "--approve-consequential",
        ])
        assert result.exit_code != 0
        combined = (result.stderr or "") + (result.stdout or "")
        assert "the target_name argument" in combined, combined
        assert "empty" in combined, combined

    def test_check_names_refuses_an_empty_prefix(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "monorepo", "check-names", "--target", "npm", "--prefix", "",
        ])
        _assert_refused(result, "prefix")

    def test_check_names_refuses_an_empty_suffix(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test([
            "monorepo", "check-names", "--target", "npm", "--suffix", "",
        ])
        _assert_refused(result, "suffix")

    def test_changelog_edit_refuses_an_empty_id(self, mock_git_repo):
        result = rlsbl.app.test([
            "changelog", "edit", "--id", "", "--type", "fix",
        ])
        _assert_refused(result, "id")

    def test_changelog_edit_refuses_an_empty_description(self, mock_git_repo):
        result = rlsbl.app.test([
            "changelog", "edit", "--id", "01ABC", "--description", "",
        ])
        _assert_refused(result, "description")

    def test_the_empty_description_refusal_names_unset_description(
        self, mock_git_repo
    ):
        result = rlsbl.app.test([
            "changelog", "edit", "--id", "01ABC", "--description", "",
        ])
        combined = (result.stderr or "") + (result.stdout or "")
        assert "--unset-description" in combined, combined

    def test_watch_refuses_an_empty_run_id(self, mock_git_repo):
        result = rlsbl.app.test(["watch", "--run-id", ""])
        _assert_refused(result, "run-id")

    def test_monorepo_remove_refuses_an_empty_path_argument(self, mock_git_repo):
        rlsbl.app.test(["monorepo", "init", "--root-dev-node"])
        result = rlsbl.app.test(["monorepo", "remove", ""])
        assert result.exit_code != 0, result.stdout
        combined = (result.stderr or "") + (result.stdout or "")
        assert "the path argument" in combined, combined
        assert "empty" in combined, combined


class TestSuppliedEmptyIsNeverReportedAsAbsence:
    """Sites that already hard-errored, but described the value as missing.

    Each refusal was truthful about the outcome and wrong about the cause: the
    caller HAD supplied the value, so a message about a missing one sends them
    looking for the wrong mistake.
    """

    def test_changelog_add_refuses_an_empty_commits(self, mock_git_repo):
        result = rlsbl.app.test([
            "changelog", "add", "--commits", "", "--no-user-facing",
        ])
        _assert_refused(result, "commits")

    def test_changelog_add_refuses_an_empty_description(self, mock_git_repo):
        result = rlsbl.app.test([
            "changelog", "add", "--commits", "HEAD", "--description", "",
            "--type", "fix",
        ])
        _assert_refused(result, "description")

    def test_changelog_remap_refuses_an_empty_map_file(self, mock_git_repo):
        result = rlsbl.app.test(["changelog", "remap", "--map-file", ""])
        _assert_refused(result, "map-file")

    def test_changelog_remap_still_refuses_naming_no_source(self, mock_git_repo):
        """Absence keeps the at-least-one refusal; only supplied-empty moved."""
        result = rlsbl.app.test(["changelog", "remap"])
        assert result.exit_code != 0
        combined = (result.stderr or "") + (result.stdout or "")
        assert "map-file" in combined, combined


class TestTheHandCheckedSitesShareTheOneMessage:
    """The three per-site checks route through the shared refusal."""

    def test_go_module_path_refuses_an_empty_from_module(self, mock_git_repo):
        result = rlsbl.app.test([
            "rewrite", "go-module-path", "--from-module", "",
            "--to-module", "example.com/n",
        ])
        _assert_refused(result, "from-module")

    def test_go_module_path_refuses_an_empty_to_module(self, mock_git_repo):
        result = rlsbl.app.test([
            "rewrite", "go-module-path", "--from-module", "example.com/o",
            "--to-module", "",
        ])
        _assert_refused(result, "to-module")

    def test_transition_record_refuses_an_empty_subject(self, mock_git_repo):
        result = rlsbl.app.test([
            "transition", "record", "--non-version-tag", "",
            "--reason", "a nightly build marker", "--approve-consequential",
        ])
        _assert_refused(result, "non-version-tag")

    def test_transition_record_refuses_an_empty_reason(self, mock_git_repo):
        result = rlsbl.app.test([
            "transition", "record", "--non-version-tag", "nightly",
            "--reason", "", "--approve-consequential",
        ])
        _assert_refused(result, "reason")

    def test_release_scrub_refuses_an_empty_reason(self, mock_git_repo):
        result = rlsbl.app.test([
            "release", "scrub", "--pattern", "secret", "--mangle",
            "--entire-history", "--reason", "", "--approve-consequential",
        ])
        _assert_refused(result, "reason")


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
