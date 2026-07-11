"""Tests for monorepo release-init command (batch release file scaffolding)."""

import json
import os
import subprocess
import time

import pytest
import tomlkit

from conftest import make_commit, make_workspace, run_git

from rlsbl.commands.monorepo import _cmd_batch_release_init
from rlsbl.commands.monorepo.batch_release_init import _render_commented_section
from rlsbl.release_file import get_batch_release_file_path


class TestBatchReleaseInit:
    """Tests for _cmd_batch_release_init."""

    def test_scaffolds_correct_toml_structure(self, mock_git_repo):
        """Creates unreleased.toml with [packages.<name>] sections and detected targets."""
        # Set up a workspace with two projects (npm and pypi)
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
            {"path": "pkg-b", "name": "pkg-b"},
        ])

        # Create project dirs with target files
        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        pkg_b = mock_git_repo / "pkg-b"
        pkg_b.mkdir()
        (pkg_b / "pyproject.toml").write_text(
            '[project]\nname = "pkg-b"\nversion = "0.1.0"\n'
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        assert os.path.exists(batch_path)

        data = tomlkit.loads(open(batch_path).read())
        assert "packages" in data

        # pkg-a should have npm target
        assert "pkg-a" in data["packages"]
        assert data["packages"]["pkg-a"]["bump"] == ""
        assert data["packages"]["pkg-a"]["description"] == ""
        assert "npm" in data["packages"]["pkg-a"]["include"]
        assert data["packages"]["pkg-a"]["exclude"] == []

        # pkg-b should have pypi target
        assert "pkg-b" in data["packages"]
        assert data["packages"]["pkg-b"]["bump"] == ""
        assert data["packages"]["pkg-b"]["description"] == ""
        assert "pypi" in data["packages"]["pkg-b"]["include"]
        assert data["packages"]["pkg-b"]["exclude"] == []

    def test_errors_on_existing_non_empty_file(self, mock_git_repo):
        """Exits with error if unreleased.toml already exists and is non-empty."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Create the file with content
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)
        with open(batch_path, "w") as f:
            f.write("[packages.pkg-a]\nbump = \"patch\"\n")

        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(project_root=mock_git_repo)
        assert exc_info.value.code == 1

    def test_noops_on_empty_existing_file(self, mock_git_repo, capsys):
        """An empty existing file is treated as pristine: no-op success, left as-is.

        Under the refuse-unless-pristine semantics, init never overwrites an
        existing file. An empty file has nothing filled in, so it is pristine
        and init no-ops successfully (rather than the old behavior of
        overwriting it with a fresh scaffold).
        """
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Create an empty file
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)
        with open(batch_path, "w") as f:
            pass

        # Should not raise -- idempotent no-op
        _cmd_batch_release_init(project_root=mock_git_repo)

        # File is left untouched (still empty), NOT scaffolded
        assert open(batch_path).read() == ""
        captured = capsys.readouterr()
        assert "nothing to do" in captured.out

    def test_filled_file_preserved_and_errors(self, mock_git_repo):
        """A second init on an operator-filled file preserves it and errors."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # First init scaffolds a pristine file
        _cmd_batch_release_init(project_root=mock_git_repo)
        batch_path = get_batch_release_file_path(str(mock_git_repo))

        # Operator fills in bump and description
        doc = tomlkit.loads(open(batch_path).read())
        doc["packages"]["pkg-a"]["bump"] = "minor"
        doc["packages"]["pkg-a"]["description"] = "a real release"
        filled_text = tomlkit.dumps(doc)
        with open(batch_path, "w") as f:
            f.write(filled_text)

        # Second init must error and NOT clobber the filled file
        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(project_root=mock_git_repo)
        assert exc_info.value.code == 1
        assert open(batch_path).read() == filled_text

    def test_pristine_file_noop_success(self, mock_git_repo, capsys):
        """A second init on a still-pristine file no-ops successfully."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)
        batch_path = get_batch_release_file_path(str(mock_git_repo))
        pristine_text = open(batch_path).read()

        capsys.readouterr()  # drop first-init output

        # Second init on the pristine file: no raise, file unchanged
        _cmd_batch_release_init(project_root=mock_git_repo)
        assert open(batch_path).read() == pristine_text
        captured = capsys.readouterr()
        assert "nothing to do" in captured.out

    def test_race_stale_writer_does_not_clobber(self, mock_git_repo):
        """The write step refuses to clobber a file filled by a racing init.

        Simulates the TOCTOU stale-writer path: the batch exists() check
        passed while the file was absent, then a racing init created and
        filled it before this writer reached its write step. Invoking the
        write step (``_scaffold_package_sections``) directly against a
        now-filled file must hit the atomic exclusive-create guard and refuse.
        """
        from rlsbl.commands.monorepo.batch_release_init import _scaffold_package_sections
        from rlsbl.workspace import load_workspace

        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        os.makedirs(os.path.dirname(batch_path), exist_ok=True)

        # A racing init got here first and wrote a filled file.
        racer_text = '[packages.pkg-a]\nbump = "minor"\ndescription = "racer"\ninclude = ["npm"]\nexclude = []\n'
        with open(batch_path, "w") as f:
            f.write(racer_text)

        projects = load_workspace(str(mock_git_repo))

        # The stale writer reaches its write step; must refuse and preserve.
        with pytest.raises(SystemExit) as exc_info:
            _scaffold_package_sections(str(mock_git_repo), projects, batch_path, None)
        assert exc_info.value.code == 1
        assert open(batch_path).read() == racer_text

    def test_skips_dev_node_projects(self, mock_git_repo):
        """Dev-node projects are excluded from the batch release file."""
        make_workspace(mock_git_repo, [
            {"path": "lib", "name": "lib"},
            {"path": "test-infra", "name": "test-infra", "dev_node": True},
        ])

        lib_dir = mock_git_repo / "lib"
        lib_dir.mkdir()
        (lib_dir / "package.json").write_text(
            json.dumps({"name": "lib", "version": "1.0.0"}) + "\n"
        )

        infra_dir = mock_git_repo / "test-infra"
        infra_dir.mkdir()
        (infra_dir / "package.json").write_text(
            json.dumps({"name": "test-infra", "version": "0.1.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "lib" in data["packages"]
        assert "test-infra" not in data["packages"]

    def test_creates_releases_directory(self, mock_git_repo):
        """Creates the releases/ directory if it doesn't exist."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        releases_dir = mock_git_repo / ".rlsbl-monorepo" / "releases"
        assert not releases_dir.exists()

        _cmd_batch_release_init(project_root=mock_git_repo)

        assert releases_dir.exists()
        assert (releases_dir / "unreleased.toml").exists()

    def test_errors_without_workspace(self, mock_git_repo):
        """Exits with error when no workspace.toml exists."""
        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(project_root=mock_git_repo)
        assert exc_info.value.code == 1

    def test_skips_projects_without_targets(self, mock_git_repo, capsys):
        """Projects with no detectable targets are skipped with a warning."""
        make_workspace(mock_git_repo, [
            {"path": "has-target", "name": "has-target"},
            {"path": "no-target", "name": "no-target"},
        ])

        has_dir = mock_git_repo / "has-target"
        has_dir.mkdir()
        (has_dir / "package.json").write_text(
            json.dumps({"name": "has-target", "version": "1.0.0"}) + "\n"
        )

        no_dir = mock_git_repo / "no-target"
        no_dir.mkdir()
        # No target files -- just an empty dir

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "has-target" in data["packages"]
        assert "no-target" not in data["packages"]

        captured = capsys.readouterr()
        assert "no targets detected for no-target" in captured.err


class TestPackagesFilter:
    """Tests for --packages flag filtering."""

    def _setup_two_packages(self, mock_git_repo):
        """Helper: create workspace with pkg-a (npm) and pkg-b (pypi)."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
            {"path": "pkg-b", "name": "pkg-b"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        pkg_b = mock_git_repo / "pkg-b"
        pkg_b.mkdir()
        (pkg_b / "pyproject.toml").write_text(
            '[project]\nname = "pkg-b"\nversion = "0.1.0"\n'
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

    def test_packages_filters_to_specified(self, mock_git_repo):
        """Only the named package appears in the scaffolded file."""
        self._setup_two_packages(mock_git_repo)

        _cmd_batch_release_init(project_root=mock_git_repo, packages="pkg-a")

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "pkg-a" in data["packages"]
        assert "pkg-b" not in data["packages"]
        # pkg-b should not appear even as a comment
        raw = open(batch_path).read()
        assert "pkg-b" not in raw

    def test_packages_multiple(self, mock_git_repo):
        """Multiple comma-separated names are all included."""
        self._setup_two_packages(mock_git_repo)

        _cmd_batch_release_init(project_root=mock_git_repo, packages="pkg-a,pkg-b")

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "pkg-a" in data["packages"]
        assert "pkg-b" in data["packages"]

    def test_packages_unknown_name_errors(self, mock_git_repo):
        """Providing a name that doesn't exist in workspace.toml exits with error."""
        self._setup_two_packages(mock_git_repo)

        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(project_root=mock_git_repo, packages="nonexistent")
        assert exc_info.value.code == 1

    def test_packages_partial_unknown_errors(self, mock_git_repo, capsys):
        """Even one unknown name in a mixed list causes an error."""
        self._setup_two_packages(mock_git_repo)

        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(
                project_root=mock_git_repo, packages="pkg-a,ghost"
            )
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "ghost" in captured.err


class TestCommentOutZeroCommits:
    """Tests for commenting out packages with no unreleased commits."""

    def test_zero_commits_commented_out(self, mock_git_repo):
        """A tagged package with no subsequent commits is rendered as comments."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Tag the project so it has a "last release"
        run_git(mock_git_repo, "tag", "pkg-a@v1.0.0")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        raw = open(batch_path).read()

        # Should appear as comments, not as a real TOML section
        assert "# pkg-a: no unreleased commits since pkg-a@v1.0.0" in raw
        assert "# [packages.pkg-a]" in raw
        assert '# bump = ""' in raw

        # Should NOT be parseable as a real packages entry
        data = tomlkit.loads(raw)
        assert "pkg-a" not in data.get("packages", {})

    def test_package_with_commits_not_commented(self, mock_git_repo):
        """A tagged package WITH subsequent commits is scaffolded normally."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Tag the project
        run_git(mock_git_repo, "tag", "pkg-a@v1.0.0")

        # Make an unreleased commit touching pkg-a files
        (pkg_a / "index.js").write_text(f"// change {time.monotonic_ns()}\n")
        run_git(mock_git_repo, "add", "pkg-a/index.js")
        run_git(mock_git_repo, "commit", "-q", "-m", "update pkg-a")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        # Should be a real section, not commented out
        assert "pkg-a" in data["packages"]
        assert data["packages"]["pkg-a"]["bump"] == ""

    def test_untagged_package_not_commented(self, mock_git_repo):
        """A package with no tags at all (first release) is scaffolded normally."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "0.1.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # No tag -- first release scenario
        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        data = tomlkit.loads(open(batch_path).read())

        assert "pkg-a" in data["packages"]

    def test_mixed_active_and_inactive(self, mock_git_repo):
        """Active packages get real sections; inactive ones get commented sections."""
        make_workspace(mock_git_repo, [
            {"path": "active", "name": "active"},
            {"path": "stale", "name": "stale"},
        ])

        active_dir = mock_git_repo / "active"
        active_dir.mkdir()
        (active_dir / "package.json").write_text(
            json.dumps({"name": "active", "version": "1.0.0"}) + "\n"
        )

        stale_dir = mock_git_repo / "stale"
        stale_dir.mkdir()
        (stale_dir / "package.json").write_text(
            json.dumps({"name": "stale", "version": "0.5.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        # Tag both
        run_git(mock_git_repo, "tag", "active@v1.0.0")
        run_git(mock_git_repo, "tag", "stale@v0.5.0")

        # Make a commit touching only 'active'
        (active_dir / "index.js").write_text(f"// change {time.monotonic_ns()}\n")
        run_git(mock_git_repo, "add", "active/index.js")
        run_git(mock_git_repo, "commit", "-q", "-m", "update active")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        raw = open(batch_path).read()
        data = tomlkit.loads(raw)

        # 'active' is a real section
        assert "active" in data["packages"]

        # 'stale' is commented out
        assert "stale" not in data.get("packages", {})
        assert "# stale: no unreleased commits since stale@v0.5.0" in raw
        assert "# [packages.stale]" in raw


class TestRenderCommentedSection:
    """Regression tests for _render_commented_section.

    v0.57.1 fixed a bug where tomlkit.dumps() received a raw list instead
    of a tomlkit item, crashing on Python 3.14. These tests verify the
    function produces valid commented TOML output for various inputs.
    """

    def test_returns_string_with_comment_prefix(self):
        """Output is a string where every line starts with '# '."""
        result = _render_commented_section("my-pkg", ["npm"], "no changes")
        assert isinstance(result, str)
        for line in result.splitlines():
            assert line.startswith("# "), f"Line missing comment prefix: {line!r}"

    def test_single_target_include_rendered(self):
        """A single-element include list is rendered correctly."""
        result = _render_commented_section("my-pkg", ["pypi"], "some reason")
        assert '# include = ["pypi"]' in result
        assert "# [packages.my-pkg]" in result
        assert "# my-pkg: some reason" in result

    def test_multiple_targets_include_rendered(self):
        """A multi-element include list is rendered as a TOML array."""
        result = _render_commented_section("widget", ["npm", "pypi", "docker"], "tagged")
        assert "# [packages.widget]" in result
        # The include line must contain all three targets in array syntax
        include_line = [l for l in result.splitlines() if "include" in l][0]
        assert "npm" in include_line
        assert "pypi" in include_line
        assert "docker" in include_line

    def test_empty_target_list_does_not_crash(self):
        """An empty target list produces valid output (edge case)."""
        result = _render_commented_section("empty", [], "no targets")
        assert isinstance(result, str)
        assert "# include = []" in result

    def test_standard_fields_present(self):
        """All standard fields (bump, description, context, exclude) appear."""
        result = _render_commented_section("pkg", ["npm"], "reason")
        assert '# bump = ""' in result
        assert '# description = ""' in result
        assert '# context = ""' in result
        assert "# exclude = []" in result


class TestBatchReleaseInitAutoCommit:
    """monorepo release init auto-commits the scaffolded file."""

    def test_auto_commits_batch_release_file(self, mock_git_repo):
        """After monorepo release init, the scaffolded file is committed to git."""
        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])

        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )

        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        _cmd_batch_release_init(project_root=mock_git_repo)

        batch_path = get_batch_release_file_path(str(mock_git_repo))
        assert os.path.exists(batch_path)

        # The file must be tracked (committed), not just on disk
        result = subprocess.run(
            ["git", "ls-files", batch_path],
            cwd=str(mock_git_repo),
            capture_output=True, text=True,
        )
        assert result.stdout.strip(), \
            "monorepo release init should auto-commit the scaffolded file"

        # Working tree should be clean for this file
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", batch_path],
            cwd=str(mock_git_repo),
            capture_output=True, text=True,
        )
        assert status.stdout.strip() == "", \
            f"batch release file should be clean after auto-commit, got: {status.stdout}"

    def test_commit_failure_is_loud(self, mock_git_repo, monkeypatch, capsys):
        """A failed auto-commit of the batch release file surfaces loudly
        (SystemExit + actionable error), not silently swallowed."""
        import rlsbl.utils as utils

        make_workspace(mock_git_repo, [
            {"path": "pkg-a", "name": "pkg-a"},
        ])
        pkg_a = mock_git_repo / "pkg-a"
        pkg_a.mkdir()
        (pkg_a / "package.json").write_text(
            json.dumps({"name": "pkg-a", "version": "1.0.0"}) + "\n"
        )
        run_git(mock_git_repo, "add", ".")
        run_git(mock_git_repo, "commit", "-q", "-m", "add workspace")

        def boom(*a, **k):
            raise subprocess.CalledProcessError(1, ["safegit", "commit"])

        monkeypatch.setattr(utils, "commit_files", boom)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_batch_release_init(project_root=mock_git_repo)

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "unreleased.toml" in err
        assert "commit" in err.lower()
        # File remains on disk (write succeeded, commit failed).
        assert os.path.exists(get_batch_release_file_path(str(mock_git_repo)))
