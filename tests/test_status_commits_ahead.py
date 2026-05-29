"""Tests for the 'N commits ahead of <tag>' warning in rlsbl status.

The warning surfaces unreleased work that might otherwise sit silently
(e.g., post-release wrapper commits never released). It is printed on
stdout under the existing status fields, only when there is a prior tag
AND at least one unreleased commit beyond it.
"""

import json
import os
import subprocess

import pytest

from rlsbl.commands.monorepo import _cmd_init, _cmd_add


WARN_PREFIX = "! "


def _make_npm_project(base_path, subdir=".", name="test-pkg", version="0.1.0"):
    """Create a minimal npm project (package.json)."""
    proj_dir = os.path.join(str(base_path), subdir) if subdir != "." else str(base_path)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": name, "version": version}, f)


def _commit_file(repo, name, content="x\n", message="change"):
    """Create/modify a file and commit it, returning the new HEAD SHA."""
    fp = os.path.join(str(repo), name)
    with open(fp, "w") as f:
        f.write(content)
    subprocess.run(["git", "add", name], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(repo), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _tag(repo, tag_name):
    subprocess.run(["git", "tag", tag_name], cwd=str(repo), check=True)


class TestStatusCommitsAheadStandalone:
    """Tests for the warning in a standalone (non-monorepo) project."""

    def test_no_warning_when_no_unreleased_commits(self, mock_git_repo, capsys):
        """If HEAD is at the last tag, no commits ahead -> no warning."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="1.0.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)
        _tag(mock_git_repo, "v1.0.0")

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out

        assert "commits ahead" not in out
        # The warning marker should not appear at the start of any line.
        assert not any(line.startswith(WARN_PREFIX) for line in out.splitlines())

    def test_warning_with_count_and_tag(self, mock_git_repo, capsys):
        """When there are N unreleased commits, print 'N commits ahead of <tag>'."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="1.0.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)
        _tag(mock_git_repo, "v1.0.0")

        # Add three unreleased commits
        _commit_file(mock_git_repo, "a.txt", message="add a")
        _commit_file(mock_git_repo, "b.txt", message="add b")
        _commit_file(mock_git_repo, "c.txt", message="add c")

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out

        # Find the warning line
        warning_lines = [l for l in out.splitlines() if l.startswith(WARN_PREFIX)]
        assert len(warning_lines) == 1, f"expected one warning, got {warning_lines!r}"
        warning = warning_lines[0]
        assert "3 commits ahead of v1.0.0" in warning
        assert "rlsbl release" in warning
        # The separator is an em-dash (U+2014), not a double hyphen.
        assert "— run `rlsbl release`" in warning
        assert "-- run `rlsbl release`" not in warning

    def test_singular_form_for_one_commit(self, mock_git_repo, capsys):
        """A single unreleased commit uses 'commit' (singular), not 'commits'."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="1.0.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)
        _tag(mock_git_repo, "v1.0.0")
        _commit_file(mock_git_repo, "a.txt", message="single change")

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out

        warning_lines = [l for l in out.splitlines() if l.startswith(WARN_PREFIX)]
        assert len(warning_lines) == 1
        assert "1 commit ahead of v1.0.0" in warning_lines[0]
        # Make sure we didn't write '1 commits' (plural)
        assert "1 commits ahead" not in warning_lines[0]

    def test_no_warning_when_no_prior_tag(self, mock_git_repo, capsys):
        """First release scenario: no tag yet -> no warning ('ahead of nothing')."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)
        # NO tag created

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out

        assert "commits ahead" not in out
        assert not any(line.startswith(WARN_PREFIX) for line in out.splitlines())

    def test_warning_goes_to_stdout(self, mock_git_repo, capsys):
        """The warning is part of status output (stdout), not stderr."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="1.0.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)
        _tag(mock_git_repo, "v1.0.0")
        _commit_file(mock_git_repo, "a.txt", message="post-release one")
        _commit_file(mock_git_repo, "b.txt", message="post-release two")

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        captured = capsys.readouterr()

        assert "2 commits ahead of v1.0.0" in captured.out
        assert "commits ahead" not in captured.err
        assert "ahead of" not in captured.err

    def test_warning_appears_after_existing_fields(self, mock_git_repo, capsys):
        """The warning is positioned below the standard status fields."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="1.0.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)
        _tag(mock_git_repo, "v1.0.0")
        _commit_file(mock_git_repo, "a.txt", message="post-release")

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out
        lines = out.splitlines()

        # Locate indices of a few base lines and the warning
        publish_idx = next(i for i, l in enumerate(lines) if l.startswith("Publish:"))
        warning_idx = next(i for i, l in enumerate(lines) if l.startswith(WARN_PREFIX))
        assert warning_idx > publish_idx, (
            f"warning should appear after Publish line; "
            f"got publish at {publish_idx}, warning at {warning_idx}"
        )

    def test_json_includes_commits_ahead_fields(self, mock_git_repo, capsys):
        """--json output includes commits_ahead and commits_ahead_tag."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="1.0.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)
        _tag(mock_git_repo, "v1.0.0")
        _commit_file(mock_git_repo, "a.txt", message="post-release 1")
        _commit_file(mock_git_repo, "b.txt", message="post-release 2")

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {"json": True}, project_root=".")
        out = capsys.readouterr().out
        data = json.loads(out)

        assert data["commits_ahead"] == 2
        assert data["commits_ahead_tag"] == "v1.0.0"

    def test_json_commits_ahead_none_without_tag(self, mock_git_repo, capsys):
        """Without a prior tag, commits_ahead is None in --json."""
        _make_npm_project(mock_git_repo, name="pkg-a", version="0.1.0")
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add package"], cwd=str(mock_git_repo), check=True)

        capsys.readouterr()
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {"json": True}, project_root=".")
        out = capsys.readouterr().out
        data = json.loads(out)

        assert data["commits_ahead"] is None
        assert data["commits_ahead_tag"] is None


class TestStatusCommitsAheadMonorepo:
    """Tests for the warning when status is run inside a monorepo project.

    The warning should use the project's scoped tag (e.g. core@v1.0.0) so
    each project counts and reports commits ahead of its own last tag,
    not some other project's tag.
    """

    def test_monorepo_warning_uses_scoped_tag(self, mock_git_repo, capsys):
        """Inside a monorepo project, warning references the scoped tag."""
        # Two projects: core (tagged) and tools (also tagged later)
        _cmd_init({}, project_root=".")
        core_dir = mock_git_repo / "core"
        core_dir.mkdir()
        with open(core_dir / "package.json", "w") as f:
            json.dump({"name": "core", "version": "1.0.0"}, f)
        _cmd_add(["core"], {}, project_root=".")

        # Tag core
        subprocess.run(["git", "tag", "core@v1.0.0"], cwd=str(mock_git_repo), check=True)

        # Make an unreleased commit affecting core
        _commit_file(mock_git_repo, "core/file.txt", message="post-release core change")

        capsys.readouterr()
        os.chdir(str(core_dir))
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out

        warning_lines = [l for l in out.splitlines() if l.startswith(WARN_PREFIX)]
        assert len(warning_lines) == 1
        assert "core@v1.0.0" in warning_lines[0]
        assert "commit ahead" in warning_lines[0] or "commits ahead" in warning_lines[0]

    def test_monorepo_project_with_no_unreleased_no_warning(self, mock_git_repo, capsys):
        """A monorepo project at its tagged version (no commits ahead) prints no warning."""
        _cmd_init({}, project_root=".")
        proj_dir = mock_git_repo / "alpha"
        proj_dir.mkdir()
        with open(proj_dir / "package.json", "w") as f:
            json.dump({"name": "alpha", "version": "1.0.0"}, f)
        _cmd_add(["alpha"], {}, project_root=".")
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)

        capsys.readouterr()
        os.chdir(str(proj_dir))
        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out

        assert "commits ahead" not in out
        assert not any(line.startswith(WARN_PREFIX) for line in out.splitlines())

    def test_monorepo_directory_filtering_excludes_other_projects(self, mock_git_repo, capsys):
        """Commits touching only another project's files are excluded from the count.

        alpha and beta both tagged, then commits are added touching only
        beta's files. alpha should show 0 commits ahead (no warning),
        not count beta's commits.
        """
        _cmd_init({}, project_root=".")

        alpha = mock_git_repo / "alpha"
        alpha.mkdir()
        with open(alpha / "package.json", "w") as f:
            json.dump({"name": "alpha", "version": "1.0.0"}, f)
        _cmd_add(["alpha"], {}, project_root=".")

        beta = mock_git_repo / "beta"
        beta.mkdir()
        with open(beta / "package.json", "w") as f:
            json.dump({"name": "beta", "version": "1.0.0"}, f)
        _cmd_add(["beta"], {}, project_root=".")

        # Tag both at the same commit
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "beta@v1.0.0"], cwd=str(mock_git_repo), check=True)

        # Add commits touching ONLY beta's files
        _commit_file(mock_git_repo, "beta/x.txt", message="beta change 1")
        _commit_file(mock_git_repo, "beta/y.txt", message="beta change 2")

        from rlsbl.commands.status import run_cmd

        # alpha: no commits touch its files -> no warning
        capsys.readouterr()
        os.chdir(str(alpha))
        run_cmd("npm", [], {}, project_root=".")
        alpha_out = capsys.readouterr().out
        alpha_warnings = [l for l in alpha_out.splitlines() if l.startswith(WARN_PREFIX)]
        assert len(alpha_warnings) == 0, (
            f"alpha should have no warning (commits only touch beta), "
            f"got: {alpha_warnings}"
        )

        # beta: 2 commits touch its files -> warning
        capsys.readouterr()
        os.chdir(str(beta))
        run_cmd("npm", [], {}, project_root=".")
        beta_out = capsys.readouterr().out
        beta_warnings = [l for l in beta_out.splitlines() if l.startswith(WARN_PREFIX)]
        assert len(beta_warnings) == 1
        assert "2 commits ahead" in beta_warnings[0]

    def test_monorepo_directory_filtering_json(self, mock_git_repo, capsys):
        """JSON output reflects directory-filtered commit counts."""
        _cmd_init({}, project_root=".")

        alpha = mock_git_repo / "alpha"
        alpha.mkdir()
        with open(alpha / "package.json", "w") as f:
            json.dump({"name": "alpha", "version": "1.0.0"}, f)
        _cmd_add(["alpha"], {}, project_root=".")

        beta = mock_git_repo / "beta"
        beta.mkdir()
        with open(beta / "package.json", "w") as f:
            json.dump({"name": "beta", "version": "1.0.0"}, f)
        _cmd_add(["beta"], {}, project_root=".")

        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "beta@v1.0.0"], cwd=str(mock_git_repo), check=True)

        # One commit touches alpha, two touch beta
        _commit_file(mock_git_repo, "alpha/a.txt", message="alpha change")
        _commit_file(mock_git_repo, "beta/b1.txt", message="beta change 1")
        _commit_file(mock_git_repo, "beta/b2.txt", message="beta change 2")

        from rlsbl.commands.status import run_cmd

        capsys.readouterr()
        os.chdir(str(alpha))
        run_cmd("npm", [], {"json": True}, project_root=".")
        data = json.loads(capsys.readouterr().out)
        assert data["commits_ahead"] == 1

        capsys.readouterr()
        os.chdir(str(beta))
        run_cmd("npm", [], {"json": True}, project_root=".")
        data = json.loads(capsys.readouterr().out)
        assert data["commits_ahead"] == 2

    def test_monorepo_warns_only_for_projects_with_unreleased_commits(self, mock_git_repo, capsys):
        """Two projects: only the one whose tag is behind HEAD shows the warning.

        Each project uses its own scoped tag (alpha@v* vs beta@v*), so a
        project whose tag is AT HEAD has no commits ahead, while a project
        whose tag is older has commits ahead. Directory filtering ensures
        only commits touching the project's files are counted.
        """
        _cmd_init({}, project_root=".")
        # alpha: older tag, will have unreleased commits
        alpha = mock_git_repo / "alpha"
        alpha.mkdir()
        with open(alpha / "package.json", "w") as f:
            json.dump({"name": "alpha", "version": "1.0.0"}, f)
        _cmd_add(["alpha"], {}, project_root=".")

        # Tag alpha now, before more commits
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)

        # Add commits AFTER alpha's tag (alpha now N commits ahead)
        _commit_file(mock_git_repo, "alpha/x.txt", message="alpha change 1")
        _commit_file(mock_git_repo, "alpha/y.txt", message="alpha change 2")

        # beta: created and tagged at HEAD, so no commits ahead
        beta = mock_git_repo / "beta"
        beta.mkdir()
        with open(beta / "package.json", "w") as f:
            json.dump({"name": "beta", "version": "1.0.0"}, f)
        _cmd_add(["beta"], {}, project_root=".")
        # Tag beta at current HEAD (after all commits)
        subprocess.run(["git", "tag", "beta@v1.0.0"], cwd=str(mock_git_repo), check=True)

        from rlsbl.commands.status import run_cmd

        # alpha shows warning -- 2 commits ahead of alpha@v1.0.0
        capsys.readouterr()
        os.chdir(str(alpha))
        run_cmd("npm", [], {}, project_root=".")
        alpha_out = capsys.readouterr().out
        alpha_warnings = [l for l in alpha_out.splitlines() if l.startswith(WARN_PREFIX)]
        assert len(alpha_warnings) == 1
        assert "alpha@v1.0.0" in alpha_warnings[0]
        assert "2 commits ahead" in alpha_warnings[0]

        # beta does not show warning -- its tag is at HEAD
        capsys.readouterr()
        os.chdir(str(beta))
        run_cmd("npm", [], {}, project_root=".")
        beta_out = capsys.readouterr().out
        beta_warnings = [l for l in beta_out.splitlines() if l.startswith(WARN_PREFIX)]
        assert len(beta_warnings) == 0
