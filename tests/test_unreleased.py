"""Tests for rlsbl.commands.unreleased."""

import json
import os
import subprocess

import pytest

from githarness import commit_file
from rlsbl.commands.monorepo import _cmd_init, _cmd_add
from rlsbl.commands.unreleased import (
    _get_commits_since,
    run_cmd,
)
from rlsbl.utils import get_last_version_tag

from conftest import with_root_member, make_workspace, make_releasable_state


class TestGetLastTag:
    """Tests for get_last_version_tag (consolidated from _get_last_tag)."""

    def test_returns_tag_when_exists(self, mock_git_repo):
        subprocess.run(
            ["git", "tag", "v1.0.0"],
            cwd=str(mock_git_repo), check=True,
        )
        assert get_last_version_tag() == "v1.0.0"

    def test_returns_none_when_no_tags(self, mock_git_repo):
        assert get_last_version_tag() is None

    def test_returns_latest_tag(self, mock_git_repo):
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        # Make a new commit and tag it
        (mock_git_repo / "file.txt").write_text("hello")
        subprocess.run(["git", "add", "file.txt"], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "second"],
            cwd=str(mock_git_repo), check=True,
        )
        subprocess.run(["git", "tag", "v1.1.0"], cwd=str(mock_git_repo), check=True)
        assert get_last_version_tag() == "v1.1.0"


class TestGetCommitsSince:
    """Tests for _get_commits_since."""

    def test_returns_commits_since_tag(self, mock_git_repo):
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        # Add a commit after the tag. It goes through githarness so the repo's
        # own configured identity is what lands in the author field -- the test
        # floor exports an ambient GIT_AUTHOR_NAME that otherwise outranks it.
        commit_file(mock_git_repo, "new.txt", "new", "feat: add new feature")
        commits = _get_commits_since("v1.0.0")
        assert len(commits) == 1
        assert commits[0]["subject"] == "feat: add new feature"
        assert len(commits[0]["hash"]) == 40
        assert commits[0]["author"] == "Test"
        assert commits[0]["date"]  # non-empty ISO date

    def test_returns_empty_when_no_commits_since_tag(self, mock_git_repo):
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        commits = _get_commits_since("v1.0.0")
        assert commits == []

    def test_returns_all_commits_when_tag_is_none(self, mock_git_repo):
        # When tag is None, should get HEAD (just one commit in our fixture)
        commits = _get_commits_since(None)
        assert len(commits) == 1
        assert commits[0]["subject"] == "initial"


class TestRunCmd:
    """Tests for the unreleased run_cmd function."""

    def test_no_unreleased_commits(self, mock_git_repo, capsys):
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        run_cmd(None, [], {}, project_root=".")
        captured = capsys.readouterr()
        assert "No unreleased commits." in captured.out

    def test_errors_without_jsonl_setup(self, mock_git_repo, capsys):
        """Without .rlsbl/changes/, exits with error."""
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        (mock_git_repo / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=str(mock_git_repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat: add widget"],
            cwd=str(mock_git_repo), check=True,
        )
        with pytest.raises(SystemExit) as exc_info:
            run_cmd(None, [], {}, project_root=".")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "JSONL changelog not set up" in captured.err

    def test_json_output_no_commits(self, mock_git_repo, capsys):
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        data = run_cmd(None, [], {"json": True}, project_root=".")
        assert data["tag"] == "v1.0.0"
        assert data["commits"] == []
        assert data["coverage"] == {"covered": 0, "total": 0, "exempted": 0}


def _make_npm_project(base_path, subdir, name=None, version="0.1.0"):
    """Create a minimal npm project (package.json)."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    if name is None:
        name = subdir
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": name, "version": version}, f)


def _commit_file(repo, name, content="x\n", message="change"):
    """Create/modify a file and commit it, returning the new HEAD SHA."""
    fp = os.path.join(str(repo), name)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w") as f:
        f.write(content)
    subprocess.run(["git", "add", name], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(repo), check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestGetLastTagWithGlob:
    """Tests for get_last_version_tag with tag_glob parameter."""

    def test_tag_glob_filters_tags(self, mock_git_repo):
        """With tag_glob, only matching tags are returned."""
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "beta@v2.0.0"], cwd=str(mock_git_repo), check=True)
        assert get_last_version_tag(tag_glob="alpha@v*") == "alpha@v1.0.0"
        assert get_last_version_tag(tag_glob="beta@v*") == "beta@v2.0.0"

    def test_tag_glob_no_match(self, mock_git_repo):
        """When no tags match the glob, returns None."""
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        assert get_last_version_tag(tag_glob="nonexistent@v*") is None

    def test_no_tag_glob_returns_v_star(self, mock_git_repo):
        """Without tag_glob, defaults to 'v*' and returns matching version tags."""
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)
        assert get_last_version_tag() == "v1.0.0"


class TestUnreleasedMonorepo:
    """Tests for monorepo awareness in the unreleased command."""

    def test_monorepo_uses_scoped_tag(self, mock_git_repo, monkeypatch, capsys):
        """In a monorepo project, unreleased uses the project's scoped tag."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "alpha", version="1.0.0")
        _make_npm_project(mock_git_repo, "beta", version="1.0.0")
        _cmd_add(["alpha"], {"releasable": "false"}, project_root=".")
        _cmd_add(["beta"], {"releasable": "false"}, project_root=".")

        # Set up JSONL changelog for alpha
        changes_dir = mock_git_repo / "alpha" / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        # Tag alpha
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)

        # Add a commit touching alpha
        _commit_file(mock_git_repo, "alpha/new.txt", message="alpha feature")

        monkeypatch.chdir(str(mock_git_repo / "alpha"))
        capsys.readouterr()
        data = run_cmd("npm", [], {"json": True}, project_root=str(mock_git_repo / "alpha"))
        # Should use scoped tag
        assert data["tag"] == "alpha@v1.0.0"

    def test_monorepo_filters_commits_by_directory(self, mock_git_repo, monkeypatch, capsys):
        """Commits touching only another project's files are excluded."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "alpha", version="1.0.0")
        _make_npm_project(mock_git_repo, "beta", version="1.0.0")
        _cmd_add(["alpha"], {"releasable": "false"}, project_root=".")
        _cmd_add(["beta"], {"releasable": "false"}, project_root=".")

        # Give each member its own releasable, and that releasable its
        # changelog -- a member outside every releasable has none.
        make_workspace(str(mock_git_repo), [
            {"path": "alpha", "name": "alpha"},
            {"path": "beta", "name": "beta"},
        ])
        for proj in ["alpha", "beta"]:
            make_releasable_state(mock_git_repo, proj, version="1.0.0")

        # Tag both
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "beta@v1.0.0"], cwd=str(mock_git_repo), check=True)

        # Add commits: one for alpha, two for beta
        _commit_file(mock_git_repo, "alpha/a.txt", message="alpha change")
        _commit_file(mock_git_repo, "beta/b1.txt", message="beta change 1")
        _commit_file(mock_git_repo, "beta/b2.txt", message="beta change 2")

        # Check alpha: should see only 1 commit
        monkeypatch.chdir(str(mock_git_repo / "alpha"))
        capsys.readouterr()
        alpha_data = run_cmd("npm", [], {"json": True}, project_root=str(mock_git_repo / "alpha"))
        assert alpha_data["coverage"]["total"] == 1

        # Check beta: should see only 2 commits
        monkeypatch.chdir(str(mock_git_repo / "beta"))
        capsys.readouterr()
        beta_data = run_cmd("npm", [], {"json": True}, project_root=str(mock_git_repo / "beta"))
        assert beta_data["coverage"]["total"] == 2

    def test_monorepo_no_commits_for_project(self, mock_git_repo, monkeypatch, capsys):
        """When all commits touch other projects, shows no unreleased commits."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "alpha", version="1.0.0")
        _make_npm_project(mock_git_repo, "beta", version="1.0.0")
        _cmd_add(["alpha"], {"releasable": "false"}, project_root=".")
        _cmd_add(["beta"], {"releasable": "false"}, project_root=".")

        # Tag both
        subprocess.run(["git", "tag", "alpha@v1.0.0"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "beta@v1.0.0"], cwd=str(mock_git_repo), check=True)

        # Add commits touching ONLY beta
        _commit_file(mock_git_repo, "beta/b.txt", message="beta only")

        # Check alpha: should see no unreleased commits
        monkeypatch.chdir(str(mock_git_repo / "alpha"))
        capsys.readouterr()
        run_cmd("npm", [], {}, project_root=str(mock_git_repo / "alpha"))
        out = capsys.readouterr().out
        assert "No unreleased commits." in out

    def test_standalone_project_unchanged(self, mock_git_repo, capsys):
        """A standalone (non-monorepo) project behaves as before."""
        with open(str(mock_git_repo / "package.json"), "w") as f:
            json.dump({"name": "standalone", "version": "1.0.0"}, f)
        subprocess.run(["git", "add", "package.json"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add pkg"], cwd=str(mock_git_repo), check=True)
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(mock_git_repo), check=True)

        # Set up JSONL changelog
        changes_dir = mock_git_repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        _commit_file(mock_git_repo, "src/main.js", message="add main")

        capsys.readouterr()
        data = run_cmd("npm", [], {"json": True}, project_root=".")
        assert data["tag"] == "v1.0.0"
        assert data["coverage"]["total"] == 1


def _commit_autogenerated(repo, name, message="chore: regenerate"):
    """Commit a file with the ``Autogenerated: true`` trailer."""
    fp = os.path.join(str(repo), name)
    os.makedirs(os.path.dirname(fp) or str(repo), exist_ok=True)
    with open(fp, "a") as f:
        f.write("x\n")
    subprocess.run(["git", "add", name], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"{message}\n\nAutogenerated: true"],
        cwd=str(repo), check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()


class TestUnreleasedExemptions:
    """``unreleased`` must apply the same exemption filter as ``status``.

    Coverage computed without exemptions made changelog-only and
    autogenerated commits look like uncovered work, so the two commands
    answered the same question differently and repeatedly misled triage.
    """

    def _repo_with_exempt_commits(self, repo):
        """Build a repo with 1 tracked + 1 changelog-only + 1 autogenerated
        commit past the tag. Returns the tracked commit's SHA."""
        with open(str(repo / "package.json"), "w") as f:
            json.dump({"name": "standalone", "version": "1.0.0"}, f)
        changes_dir = repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")
        subprocess.run(
            ["git", "add", "package.json", ".rlsbl/changes/unreleased.jsonl"],
            cwd=str(repo), check=True,
        )
        subprocess.run(["git", "commit", "-q", "-m", "setup"], cwd=str(repo), check=True)
        subprocess.run(["git", "tag", "v1.0.0"], cwd=str(repo), check=True)

        tracked = _commit_file(repo, "src/main.js", message="feat: add main")
        # Changelog-only commit (only touches .rlsbl/changes/)
        _commit_file(
            repo, ".rlsbl/changes/unreleased.jsonl",
            content='{"commits":["deadbee"],"user_facing":false}\n',
            message="changelog: entry",
        )
        # Autogenerated commit
        _commit_autogenerated(repo, "generated.txt")
        return tracked

    def test_exempt_commits_are_not_counted_as_tracked(self, mock_git_repo, capsys):
        self._repo_with_exempt_commits(mock_git_repo)

        capsys.readouterr()
        data = run_cmd("npm", [], {"json": True}, project_root=".")
        assert data["coverage"]["total"] == 1
        assert data["coverage"]["exempted"] == 2
        assert data["coverage"]["covered"] == 0

    def test_text_output_reports_exempted_count(self, mock_git_repo, capsys):
        self._repo_with_exempt_commits(mock_git_repo)

        capsys.readouterr()
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out
        assert "0/1 commits covered (2 exempted)" in out

    def test_exempt_commits_are_marked_in_the_listing(self, mock_git_repo, capsys):
        self._repo_with_exempt_commits(mock_git_repo)

        capsys.readouterr()
        run_cmd("npm", [], {}, project_root=".")
        out = capsys.readouterr().out
        assert "[EXEMPT]" in out
        exempt_lines = [ln for ln in out.splitlines() if "[EXEMPT]" in ln]
        assert len(exempt_lines) == 2

    def test_covered_tracked_commit_counts_as_covered(self, mock_git_repo, capsys):
        tracked = self._repo_with_exempt_commits(mock_git_repo)
        entry = {
            "commits": [tracked], "user_facing": True,
            "description": "Adds main.", "type": "feature",
        }
        path = mock_git_repo / ".rlsbl" / "changes" / "unreleased.jsonl"
        path.write_text(json.dumps(entry) + "\n")

        capsys.readouterr()
        data = run_cmd("npm", [], {"json": True}, project_root=".")
        assert data["coverage"] == {"covered": 1, "total": 1, "exempted": 2}


def _explicit_releasable_workspace(repo, *, member="pkg-a", releasable="alpha",
                                   version="1.0.0"):
    """Create an explicit-mode workspace with one releasable member.

    Returns the releasable's changes directory path.
    """
    from rlsbl.workspace import (
        Releasable,
        get_releasable_changes_dir,
        save_workspace,
        write_releasable_version,
    )

    _make_npm_project(repo, member, version=version)
    save_workspace(
        str(repo),
        with_root_member([{"path": member, "name": member, "releasable": releasable}]),
        releasables=[Releasable(name=releasable)],
    )
    write_releasable_version(str(repo), releasable, version)
    changes_dir = get_releasable_changes_dir(str(repo), releasable)
    os.makedirs(changes_dir, exist_ok=True)
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write("")
    subprocess.run(
        ["git", "add", ".rlsbl-monorepo", member], cwd=str(repo), check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "workspace setup"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "tag", f"{releasable}@v{version}"], cwd=str(repo), check=True,
    )
    return changes_dir


class TestUnreleasedReleasableMember:
    """A releasable member's JSONL lives at the releasable level.

    ``unreleased`` resolved ``.rlsbl/changes/`` relative to the member
    directory, so every releasable member was told the JSONL changelog was
    not set up -- while ``rlsbl changelog add`` had been writing entries into
    the releasable's directory all along.
    """

    def test_member_finds_the_releasable_changes_dir(
        self, mock_git_repo, monkeypatch, capsys,
    ):
        changes_dir = _explicit_releasable_workspace(mock_git_repo)
        _commit_file(mock_git_repo, "pkg-a/feature.js", message="feat: thing")

        monkeypatch.chdir(str(mock_git_repo / "pkg-a"))
        capsys.readouterr()
        # Must not error with "JSONL changelog not set up".
        data = run_cmd("npm", [], {"json": True}, project_root=str(mock_git_repo / "pkg-a"))
        assert data["tag"] == "alpha@v1.0.0"
        assert data["coverage"]["total"] == 1
        assert data["coverage"]["covered"] == 0
        assert changes_dir  # the releasable dir, not pkg-a/.rlsbl/changes

    def test_member_reads_entries_from_the_releasable_changes_dir(
        self, mock_git_repo, monkeypatch, capsys,
    ):
        changes_dir = _explicit_releasable_workspace(mock_git_repo)
        sha = _commit_file(mock_git_repo, "pkg-a/feature.js", message="feat: thing")
        entry = {
            "commits": [sha], "user_facing": True,
            "description": "New thing.", "type": "feature",
        }
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write(json.dumps(entry) + "\n")

        monkeypatch.chdir(str(mock_git_repo / "pkg-a"))
        capsys.readouterr()
        data = run_cmd("npm", [], {"json": True}, project_root=str(mock_git_repo / "pkg-a"))
        assert data["coverage"] == {"covered": 1, "total": 1, "exempted": 0}
