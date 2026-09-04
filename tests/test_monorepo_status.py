"""Tests for monorepo status subcommand and monorepo-aware rlsbl status."""

import json
import os
import subprocess

import pytest

from pathlib import Path

from conftest import make_ctx, with_root_member, make_workspace
from rlsbl.release_record import range_anchor as _range_anchor
from rlsbl.commands.monorepo import _cmd_init, _cmd_add, _cmd_status
from rlsbl.errors import WorkspaceError
from rlsbl.workspace import load_workspace, save_workspace, WORKSPACE_DIR, WORKSPACE_FILE


def _make_npm_project(base_path, subdir, version="0.1.0"):
    """Create a minimal npm project (package.json) so detect_targets finds it."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump({"name": "test-" + subdir.replace("/", "-"), "version": version}, f)
    return subdir


def _member_table(out):
    """The per-member table's lines, skipping the per-releasable summary.

    `monorepo status` renders the releasable summary first (Name/Kind/Version/
    Tag/Coverage/Members) and the per-member table after it. Tests about the
    member columns want the second table.
    """
    lines = [line for line in out.strip().split("\n") if line.strip()]
    for i, line in enumerate(lines):
        if line.startswith("Project"):
            return lines[i:]
    raise AssertionError(f"no per-member table in output:\n{out}")


def _member_header(out):
    """The per-member table's header line."""
    return _member_table(out)[0]


class TestMonorepoStatus:
    """Tests for the 'rlsbl monorepo status' subcommand."""

    def test_fresh_workspace_shows_its_root_member(self, mock_git_repo, capsys):
        """A workspace always has its root member, so it is never empty."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        assert "No projects in workspace." not in captured.out
        assert _member_header(captured.out).startswith("Project")
        rows = _member_table(captured.out)[1:]
        assert [r.split()[0] for r in rows] == ["root"]

    def test_status_with_projects(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "pkg-a", version="1.0.0")
        _cmd_add(["pkg-a"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        # Header
        assert "Project" in captured.out
        assert "Path" in captured.out
        assert "Target" in captured.out
        assert "Version" in captured.out
        assert "Released" in captured.out
        assert "Coverage" in captured.out
        # Project row
        assert "pkg-a" in captured.out
        assert "npm" in captured.out
        assert "1.0.0" in captured.out

    def test_status_shows_unreleased(self, mock_git_repo, capsys):
        """Project with no tag shows (none) for tag and info in Unreleased column."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "mylib", version="0.2.0")
        _cmd_add(["mylib"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        assert "(none)" in captured.out

    def test_status_shows_released(self, mock_git_repo, capsys):
        """A project with an archived release shows that release."""
        from conftest import archive_release, git_head, release_record_dir

        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "mylib", version="1.0.0")
        _cmd_add(["mylib"], {"releasable": "false"}, project_root=".")
        subprocess.run(
            ["git", "tag", "mylib@v1.0.0"],
            cwd=str(mock_git_repo),
            check=True,
        )
        archive_release(
            release_record_dir(mock_git_repo / "mylib"), "1.0.0", git_head(mock_git_repo),
        )
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        # The root member has released nothing; mylib's row names its release.
        mylib_row = _row_for(captured.out, "mylib")
        assert "1.0.0" in mylib_row
        assert "(none)" not in mylib_row

    def test_status_no_workspace(self, mock_git_repo, capsys):
        """Without an initialized workspace, status should error."""
        with pytest.raises(SystemExit):
            _cmd_status({}, project_root=".")

    def test_status_multiple_projects(self, mock_git_repo, capsys):
        """Status displays all projects in the workspace."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "alpha", version="1.0.0")
        _make_npm_project(mock_git_repo, "beta", version="2.0.0")
        _cmd_add(["alpha"], {"releasable": "false"}, project_root=".")
        _cmd_add(["beta"], {"releasable": "false"}, project_root=".")
        # Release only alpha -- tag AND archive, since a tagged project with no
        # archive is a repository that was never backfilled, and the release record
        # refuses to answer for one.
        from conftest import archive_release, git_head, release_record_dir

        subprocess.run(
            ["git", "tag", "alpha@v1.0.0"],
            cwd=str(mock_git_repo),
            check=True,
        )
        archive_release(
            release_record_dir(mock_git_repo / "alpha"), "1.0.0", git_head(mock_git_repo),
        )
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        lines = _member_table(captured.out)
        # Header + the root member + 2 project rows
        assert len(lines) == 4
        assert "alpha" in captured.out
        assert "beta" in captured.out


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


def _commit_autogenerated(repo, name, message="chore: regenerate"):
    """Commit a file carrying the ``Autogenerated: true`` trailer."""
    fp = os.path.join(str(repo), name)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "a") as f:
        f.write("x\n")
    subprocess.run(["git", "add", name], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"{message}\n\nAutogenerated: true"],
        cwd=str(repo), check=True,
    )


def _releasable_workspace(repo, members=("pkg-a", "pkg-b"), name="alpha",
                          version="1.0.0", with_changes_dir=True,
                          changelog_bullets=0):
    """Explicit-mode workspace with one releasable, tagged at *version*.

    ``changelog_bullets`` writes a decoy CHANGELOG.md whose bullets sit above
    the tagged version heading -- the shape the old column counted.
    Returns the releasable's changes directory path.
    """
    from rlsbl.workspace import (
        Releasable,
        get_releasable_changes_dir,
        get_releasable_dir,
        save_workspace,
        write_releasable_version,
    )

    for member in members:
        _make_npm_project(repo, member, version=version)
    save_workspace(
        str(repo),
        with_root_member([{"path": m, "name": m, "releasable": name} for m in members]),
        releasables=[Releasable(name=name)],
    )
    write_releasable_version(str(repo), name, version)

    changes_dir = get_releasable_changes_dir(str(repo), name)
    if with_changes_dir:
        os.makedirs(changes_dir, exist_ok=True)
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write("")
    if changelog_bullets:
        rel_dir = get_releasable_dir(str(repo), name)
        os.makedirs(rel_dir, exist_ok=True)
        bullets = "\n".join(f"- bogus {i}" for i in range(changelog_bullets))
        with open(os.path.join(rel_dir, "CHANGELOG.md"), "w") as f:
            f.write(
                "<!-- Generated by rlsbl -->\n\n# Changelog\n\n"
                f"## Unreleased\n\n{bullets}\n\n"
                f"## {version}\n\n- Old thing.\n"
            )

    subprocess.run(
        ["git", "add", ".rlsbl-monorepo", *members], cwd=str(repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "workspace setup"], cwd=str(repo), check=True,
    )
    subprocess.run(["git", "tag", f"{name}@v{version}"], cwd=str(repo), check=True)
    # The RELEASE RECORD entry the status table reads: the archive, not the tag.
    from conftest import archive_release, git_head

    archive_release(
        os.path.join(os.path.dirname(changes_dir), "releases"),
        version, git_head(repo),
    )
    return changes_dir


def _write_entry(changes_dir, sha, description="Thing.", entry_type="feature"):
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "a") as f:
        f.write(json.dumps({
            "commits": [sha], "user_facing": True,
            "description": description, "type": entry_type,
        }) + "\n")


def _row_for(out, name):
    for line in out.splitlines():
        if line.startswith(name + " ") or line.strip() == name:
            return line
    raise AssertionError(f"no row for {name!r} in:\n{out}")


class TestMonorepoStatusCoverage:
    """The status table reports real JSONL coverage, not CHANGELOG bullets.

    The column used to count bullet lines above the last version heading in
    the generated CHANGELOG.md. That file only catches up with the JSONL at
    release time, so the number said nothing about whether the unreleased
    commits actually had entries -- it read as "work is documented" when no
    entry existed at all.
    """

    def test_header_is_coverage(self, mock_git_repo, capsys):
        _releasable_workspace(mock_git_repo)
        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        header = _member_header(capsys.readouterr().out)
        assert "Coverage" in header
        assert "Unreleased" not in header

    def test_changelog_bullets_do_not_drive_the_column(self, mock_git_repo, capsys):
        """Three stale bullets above the heading, one uncovered commit."""
        _releasable_workspace(mock_git_repo, changelog_bullets=3)
        _commit_file(mock_git_repo, "pkg-a/feature.js", message="feat: thing")

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "alpha")

        assert "3 entries" not in row
        assert "0/1" in row

    def test_covered_commit_is_reported_covered(self, mock_git_repo, capsys):
        changes_dir = _releasable_workspace(mock_git_repo)
        sha = _commit_file(mock_git_repo, "pkg-a/feature.js", message="feat: thing")
        _write_entry(changes_dir, sha)

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "alpha")

        assert "1/1" in row

    def test_scope_spans_every_member_of_the_releasable(self, mock_git_repo, capsys):
        _releasable_workspace(mock_git_repo)
        _commit_file(mock_git_repo, "pkg-a/a.js", message="feat: a")
        _commit_file(mock_git_repo, "pkg-b/b.js", message="feat: b")

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "alpha")

        assert "0/2" in row

    def test_exempt_commits_are_reported(self, mock_git_repo, capsys):
        _releasable_workspace(mock_git_repo)
        _commit_file(mock_git_repo, "pkg-a/a.js", message="feat: a")
        _commit_autogenerated(mock_git_repo, "pkg-a/generated.js")

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "alpha")

        assert "0/1 (1 exempted)" in row

    def test_member_row_scopes_like_the_releasable_row(self, mock_git_repo, capsys):
        """Both tables read the releasable's release record, so both count the same way.

        A commit touching only the releasable's own state directory belongs to
        no member's declared path, and is inside the releasable's scope alone.
        The per-member row reads that same releasable's changes directory, so
        scoping it as a bare member made its ``(N exempted)`` suffix disagree
        with the releasable row's on the very same commit.
        """
        _releasable_workspace(mock_git_repo)
        _commit_autogenerated(
            mock_git_repo,
            ".rlsbl-monorepo/releasables/alpha/releases/note.toml",
        )

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        out = capsys.readouterr().out

        assert "0/0 (1 exempted)" in _row_for(out, "alpha")
        assert "0/0 (1 exempted)" in _row_for(out, "pkg-a")

    def test_member_row_still_counts_only_its_own_files(self, mock_git_repo, capsys):
        """Widening to the state directory does not widen to sibling members."""
        _releasable_workspace(mock_git_repo)
        _commit_file(mock_git_repo, "pkg-a/a.js", message="feat: a")
        _commit_file(mock_git_repo, "pkg-b/b.js", message="feat: b")

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        out = capsys.readouterr().out

        assert "0/2" in _row_for(out, "alpha")
        assert "0/1" in _row_for(out, "pkg-a")
        assert "0/1" in _row_for(out, "pkg-b")

    def test_releasable_without_changes_dir_shows_no_changelog(
        self, mock_git_repo, capsys,
    ):
        _releasable_workspace(mock_git_repo, name="beta", with_changes_dir=False)

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "beta")

        assert "no changelog" in row

    def test_standalone_project_row_uses_its_own_changes_dir(
        self, mock_git_repo, capsys,
    ):
        """A project outside every releasable keeps per-package JSONL."""
        from rlsbl.workspace import Releasable, save_workspace, write_releasable_version

        _make_npm_project(mock_git_repo, "pkg-a", version="1.0.0")
        _make_npm_project(mock_git_repo, "tool", version="3.0.0")
        save_workspace(
            str(mock_git_repo),
            with_root_member([
                {"path": "pkg-a", "name": "pkg-a", "releasable": "alpha"},
                {"path": "tool", "name": "tool", "releasable": False},
            ]),
            releasables=[Releasable(name="alpha")],
        )
        write_releasable_version(str(mock_git_repo), "alpha", "1.0.0")
        tool_changes = mock_git_repo / "tool" / ".rlsbl" / "changes"
        tool_changes.mkdir(parents=True, exist_ok=True)
        (tool_changes / "unreleased.jsonl").write_text("")
        subprocess.run(
            ["git", "add", ".rlsbl-monorepo", "pkg-a", "tool"],
            cwd=str(mock_git_repo), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "setup"], cwd=str(mock_git_repo), check=True,
        )
        subprocess.run(
            ["git", "tag", "tool@v3.0.0"], cwd=str(mock_git_repo), check=True,
        )
        from conftest import archive_release, git_head, release_record_dir

        archive_release(
            release_record_dir(mock_git_repo / "tool"), "3.0.0", git_head(mock_git_repo),
        )
        sha = _commit_file(mock_git_repo, "tool/t.js", message="feat: tool")
        _write_entry(str(tool_changes), sha)

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        out = capsys.readouterr().out

        assert "1/1" in _row_for(out, "tool")


def _unreleasable_member_workspace(repo, name="pkg-a", version="1.0.0",
                                   with_changes_dir=True, tag=True):
    """Workspace whose one package stands outside every releasable.

    Built directly rather than through ``monorepo add`` so the JSONL
    directory's existence is a property of the fixture, not of whatever the
    scaffolder happened to do. Such a member keeps its own per-package
    changes directory. Returns it.
    """
    from rlsbl.workspace import save_workspace

    _make_npm_project(repo, name, version=version)
    make_workspace(
        str(repo),
        [{"path": name, "name": name, "releasable": False}],
        releasables=[],
    )
    changes_dir = os.path.join(str(repo), name, ".rlsbl", "changes")
    if with_changes_dir:
        os.makedirs(changes_dir, exist_ok=True)
        with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
            f.write("")
    subprocess.run(
        ["git", "add", ".rlsbl-monorepo", name], cwd=str(repo), check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "workspace setup"], cwd=str(repo), check=True,
    )
    if tag:
        from conftest import archive_release, git_head

        subprocess.run(
            ["git", "tag", f"{name}@v{version}"], cwd=str(repo), check=True,
        )
        archive_release(
            os.path.join(str(repo), name, ".rlsbl", "releases"),
            version, git_head(repo),
        )
    return changes_dir


class TestMonorepoStatusUnreleasableMemberCoverage:
    """A member outside every releasable reports coverage from its own JSONL."""

    def test_header_is_coverage(self, mock_git_repo, capsys):
        _unreleasable_member_workspace(mock_git_repo)
        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        header = _member_header(capsys.readouterr().out)
        assert "Coverage" in header
        assert "Unreleased" not in header

    def test_uncovered_commit_reported(self, mock_git_repo, capsys):
        _unreleasable_member_workspace(mock_git_repo)
        _commit_file(mock_git_repo, "pkg-a/feature.js", message="feat: thing")

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "pkg-a")

        assert "0/1" in row

    def test_covered_commit_reported(self, mock_git_repo, capsys):
        changes_dir = _unreleasable_member_workspace(mock_git_repo)
        sha = _commit_file(mock_git_repo, "pkg-a/feature.js", message="feat: thing")
        _write_entry(changes_dir, sha)

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "pkg-a")

        assert "1/1" in row

    def test_exempt_commit_reported(self, mock_git_repo, capsys):
        _unreleasable_member_workspace(mock_git_repo)
        _commit_file(mock_git_repo, "pkg-a/feature.js", message="feat: thing")
        _commit_autogenerated(mock_git_repo, "pkg-a/generated.js")

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "pkg-a")

        assert "0/1 (1 exempted)" in row

    def test_missing_changes_dir_shows_no_changelog(self, mock_git_repo, capsys):
        _unreleasable_member_workspace(mock_git_repo, name="pkg-d", with_changes_dir=False)

        capsys.readouterr()
        _cmd_status({}, project_root=str(mock_git_repo))
        row = _row_for(capsys.readouterr().out, "pkg-d")

        assert "no changelog" in row


class TestStatusMonorepoAware:
    """Tests for monorepo awareness in 'rlsbl status'."""

    def test_status_shows_monorepo_hint(self, mock_git_repo, monkeypatch, capsys):
        """When inside a monorepo project, status output includes the hint."""
        # Set up monorepo workspace
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "core", version="1.0.0")
        _cmd_add(["core"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()

        # Change into the project directory
        monkeypatch.chdir(str(mock_git_repo / "core"))

        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, ctx=make_ctx("."))
        captured = capsys.readouterr()

        assert "Part of monorepo" in captured.out
        assert "rlsbl monorepo status" in captured.out
        assert "core@v1.0.0" in captured.out

    def test_status_standalone_unchanged(self, mock_git_repo, capsys):
        """When NOT in a monorepo, no hint shown."""
        # Create a standalone npm project (no monorepo init)
        with open(str(mock_git_repo / "package.json"), "w") as f:
            json.dump({"name": "standalone", "version": "1.0.0"}, f)

        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, ctx=make_ctx("."))
        captured = capsys.readouterr()

        assert "Part of monorepo" not in captured.out
        assert "Mono tag" not in captured.out
        # Normal status output still works
        assert "Package:" in captured.out
        assert "standalone" in captured.out

    def test_status_at_the_workspace_root_reports_the_root_member(
        self, mock_git_repo, capsys,
    ):
        """The workspace root is the root member's directory, so status is
        scoped to it -- and still says the repository is a monorepo."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "core", version="1.0.0")
        _cmd_add(["core"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()

        # Create a package.json at root so status command works
        with open(str(mock_git_repo / "package.json"), "w") as f:
            json.dump({"name": "monorepo-root", "version": "0.0.1"}, f)

        from rlsbl.commands.status import run_cmd
        run_cmd("npm", [], {}, ctx=make_ctx("."))
        captured = capsys.readouterr()

        assert "Part of monorepo" in captured.out
        # The root member IS a registered member, so its tag is scoped to it.
        assert "Mono tag:  root@v0.0.1" in captured.out


class TestStatusTagScoping:
    """Status coverage reads the project's own release record, scoped by tag_glob."""

    def test_monorepo_passes_tag_glob(self, mock_git_repo, monkeypatch, capsys):
        """In a monorepo project, the release record read receives the computed tag_glob."""
        from unittest.mock import patch

        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "mylib", version="1.0.0")
        _cmd_add(["mylib"], {"releasable": "false"}, project_root=".")

        # Set up .rlsbl/changes so coverage code path is triggered
        changes_dir = mock_git_repo / "mylib" / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        capsys.readouterr()

        monkeypatch.chdir(str(mock_git_repo / "mylib"))

        captured_calls = []

        def spy_range_anchor(releases_dir, *, tag_glob=None, cwd=None):
            captured_calls.append((releases_dir, tag_glob))
            return _range_anchor(releases_dir, tag_glob=tag_glob, cwd=cwd)

        with patch(
            "rlsbl.commands.status.range_anchor",
            side_effect=spy_range_anchor,
        ):
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {}, ctx=make_ctx("."))

        assert len(captured_calls) == 1
        assert captured_calls[0][1] == "mylib@v*"

    def test_standalone_no_tag_glob(self, mock_git_repo, monkeypatch, capsys):
        """In a standalone project, the release record read receives no tag_glob."""
        from unittest.mock import patch

        # Create a standalone npm project (no monorepo init)
        with open(str(mock_git_repo / "package.json"), "w") as f:
            json.dump({"name": "standalone", "version": "1.0.0"}, f)

        # Set up .rlsbl/changes so coverage code path is triggered
        changes_dir = mock_git_repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        capsys.readouterr()

        captured_calls = []

        def spy_range_anchor(releases_dir, *, tag_glob=None, cwd=None):
            captured_calls.append((releases_dir, tag_glob))
            return _range_anchor(releases_dir, tag_glob=tag_glob, cwd=cwd)

        with patch(
            "rlsbl.commands.status.range_anchor",
            side_effect=spy_range_anchor,
        ):
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {}, ctx=make_ctx("."))

        assert len(captured_calls) == 1
        assert captured_calls[0][1] is None

    def test_collect_status_forwards_tag_glob(self, mock_git_repo, capsys):
        """_collect_status passes tag_glob through to the release record read."""
        from unittest.mock import patch

        with open(str(mock_git_repo / "package.json"), "w") as f:
            json.dump({"name": "test-pkg", "version": "1.0.0"}, f)

        # Set up .rlsbl/changes
        changes_dir = mock_git_repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        captured_calls = []

        def spy_range_anchor(releases_dir, *, tag_glob=None, cwd=None):
            captured_calls.append((releases_dir, tag_glob))
            return _range_anchor(releases_dir, tag_glob=tag_glob, cwd=cwd)

        with patch(
            "rlsbl.commands.status.range_anchor",
            side_effect=spy_range_anchor,
        ):
            from rlsbl.commands.status import _collect_status
            _collect_status("npm", ".", tag_glob="my-project@v*", ctx=make_ctx("."))

        assert len(captured_calls) == 1
        assert captured_calls[0][1] == "my-project@v*"

    def test_workspace_root_uses_the_root_members_tag_glob(
        self, mock_git_repo, monkeypatch, capsys,
    ):
        """The workspace root resolves to the root member, so its tag glob
        scopes the unreleased range."""
        from unittest.mock import patch

        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "core", version="1.0.0")
        _cmd_add(["core"], {"releasable": "false"}, project_root=".")

        # Create a package.json at root so status works
        with open(str(mock_git_repo / "package.json"), "w") as f:
            json.dump({"name": "monorepo-root", "version": "0.0.1"}, f)

        # Set up .rlsbl/changes at root
        changes_dir = mock_git_repo / ".rlsbl" / "changes"
        changes_dir.mkdir(parents=True, exist_ok=True)
        (changes_dir / "unreleased.jsonl").write_text("")

        capsys.readouterr()

        captured_calls = []

        def spy_range_anchor(releases_dir, *, tag_glob=None, cwd=None):
            captured_calls.append((releases_dir, tag_glob))
            return _range_anchor(releases_dir, tag_glob=tag_glob, cwd=cwd)

        with patch(
            "rlsbl.commands.status.range_anchor",
            side_effect=spy_range_anchor,
        ):
            from rlsbl.commands.status import run_cmd
            run_cmd("npm", [], {}, ctx=make_ctx("."))

        assert len(captured_calls) == 1
        assert captured_calls[0][1] == "root@v*"


class TestMonorepoStatusWatch:
    """The watch key is gone: a workspace carrying one cannot be read at all."""

    def test_watch_key_makes_the_workspace_unreadable(self, mock_git_repo, capsys):
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "tooling", version="1.0.0")
        _cmd_add(["tooling"], {"releasable": "false"}, project_root=".")

        projects = load_workspace(".")
        for p in projects:
            if p["name"] == "tooling":
                p["watch"] = ["Package.swift", "shared/**"]
        # save_workspace directly: make_workspace refuses the key outright,
        # and the point here is that the LOADER refuses the file.
        save_workspace(".", projects)

        capsys.readouterr()
        with pytest.raises(WorkspaceError, match="'watch' key is no longer supported"):
            _cmd_status({}, project_root=".")

    def test_watch_key_on_one_member_is_still_refused(self, mock_git_repo, capsys):
        """One member's watch key is enough: the whole workspace is refused."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "tooling", version="1.0.0")
        _make_npm_project(mock_git_repo, "core", version="1.0.0")
        _cmd_add(["tooling"], {"releasable": "false"}, project_root=".")
        _cmd_add(["core"], {"releasable": "false"}, project_root=".")

        projects = load_workspace(".")
        for p in projects:
            if p["name"] == "tooling":
                p["watch"] = ["Package.swift"]
        save_workspace(".", projects)

        capsys.readouterr()
        with pytest.raises(WorkspaceError) as exc:
            _cmd_status({}, project_root=".")
        assert "tooling" in str(exc.value)

    def test_there_is_no_watch_column_at_all(self, mock_git_repo, capsys):
        """The column is gone from the renderer, not merely absent from a row.

        This slot used to assert that a workspace without watch keys showed
        no Watch column, which a workspace WITH them would still have shown.
        No workspace can carry the key now, so the column has no source.
        """
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "tooling", version="1.0.0")
        _cmd_add(["tooling"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        assert "Watch" not in captured.out

        from rlsbl.commands.monorepo import commands as monorepo_commands

        source = Path(monorepo_commands.__file__).read_text(encoding="utf-8")
        assert '"Watch"' not in source


class TestMonorepoStatusRemote:
    """Tests for mirror-destination display in monorepo status.

    The destination is declared by the RELEASABLE a member belongs to, and the
    column shows it against the member.
    """

    def test_status_shows_remote_column(self, mock_git_repo, capsys):
        """A member of a mirrored releasable shows the URL in Remote."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "tooling", version="1.0.0")
        _cmd_add(["tooling"], {"releasable": "false"}, project_root=".")

        projects = load_workspace(".")
        for p in projects:
            if p["name"] == "tooling":
                p["releasable"] = "tooling"
        make_workspace(".", projects, releasables=[
            {"name": "tooling",
             "subtree_remote": "git@github.com:user/tooling.git"},
        ])

        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        assert "Remote" in captured.out
        assert "git@github.com:user/tooling.git" in captured.out

    def test_status_no_remote_shows_dash(self, mock_git_repo, capsys):
        """A member of an unmirrored releasable shows '-' in Remote."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "tooling", version="1.0.0")
        _make_npm_project(mock_git_repo, "core", version="1.0.0")
        _cmd_add(["tooling"], {"releasable": "false"}, project_root=".")
        _cmd_add(["core"], {"releasable": "false"}, project_root=".")

        projects = load_workspace(".")
        for p in projects:
            if p["name"] in ("tooling", "core"):
                p["releasable"] = p["name"]
        make_workspace(".", projects, releasables=[
            {"name": "tooling",
             "subtree_remote": "git@github.com:user/tooling.git"},
            {"name": "core"},
        ])

        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        assert "Remote" in captured.out
        # core should show "-" in the Remote column
        lines = _member_table(captured.out)
        core_line = [l for l in lines if "core" in l and "tooling" not in l][0]
        # The last column should be "-" for core (since it has no remote)
        assert "-" in core_line


def _make_npm_project_with_deps(base_path, subdir, version="0.1.0", deps=None):
    """Create an npm project with optional dependencies in package.json."""
    proj_dir = os.path.join(str(base_path), subdir)
    os.makedirs(proj_dir, exist_ok=True)
    pkg = {"name": subdir, "version": version}
    if deps:
        pkg["dependencies"] = deps
    with open(os.path.join(proj_dir, "package.json"), "w") as f:
        json.dump(pkg, f)
    return subdir


def _setup_workspace_with_deps(base_path):
    """Create a workspace with three projects where lib-b depends on lib-a,
    and lib-c depends on both lib-a and lib-b.

    Returns the list of project dicts.
    """
    ws_dir = os.path.join(str(base_path), WORKSPACE_DIR)
    os.makedirs(ws_dir, exist_ok=True)

    _make_npm_project_with_deps(base_path, "lib-a", version="1.0.0")
    _make_npm_project_with_deps(base_path, "lib-b", version="1.0.0", deps={"lib-a": "^1.0.0"})
    _make_npm_project_with_deps(base_path, "lib-c", version="1.0.0", deps={"lib-a": "^1.0.0", "lib-b": "^1.0.0"})

    projects = [
        {"path": "lib-a", "name": "lib-a"},
        {"path": "lib-b", "name": "lib-b"},
        {"path": "lib-c", "name": "lib-c"},
    ]
    make_workspace(str(base_path), projects)
    return projects


class TestMonorepoStatusLibrary:
    """Tests for Library column in monorepo status."""

    def test_library_column_shown_when_project_is_library(self, mock_git_repo, capsys):
        """Library column appears with 'yes' when a project has library = true."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "mylib", version="1.0.0")
        _cmd_add(["mylib"], {"releasable": "false", "library": "true"}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        header_line = _member_header(captured.out)
        assert "Library" in header_line
        data_line = _member_table(captured.out)[1]
        assert "yes" in data_line

    def test_library_column_hidden_when_no_library_projects(self, mock_git_repo, capsys):
        """Library column is omitted when no project has library = true."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "app-a", version="1.0.0")
        _make_npm_project(mock_git_repo, "app-b", version="2.0.0")
        _cmd_add(["app-a"], {"releasable": "false"}, project_root=".")
        _cmd_add(["app-b"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        header_line = _member_header(captured.out)
        assert "Library" not in header_line

    def test_library_column_mixed_workspace(self, mock_git_repo, capsys):
        """Mixed workspace: column shown, 'yes' for library projects, blank for others."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "mylib", version="1.0.0")
        _make_npm_project(mock_git_repo, "myapp", version="2.0.0")
        _cmd_add(["mylib"], {"releasable": "false", "library": "true"}, project_root=".")
        _cmd_add(["myapp"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        header_line = _member_header(captured.out)
        assert "Library" in header_line
        lines = _member_table(captured.out)
        lib_line = [l for l in lines[1:] if "mylib" in l][0]
        app_line = [l for l in lines[1:] if "myapp" in l][0]
        # Find Library column position from header
        lib_col_start = header_line.index("Library")
        lib_col_end = lib_col_start + len("Library")
        # Check that mylib has "yes" in the Library column area
        assert lib_line[lib_col_start:lib_col_end].strip() == "yes"
        # Check that myapp has blank in the Library column area
        assert app_line[lib_col_start:lib_col_end].strip() == ""


class TestMonorepoStatusDeps:
    """Tests for Deps and Rdeps columns in monorepo status."""

    def test_deps_rdeps_columns_shown_when_deps_exist(self, mock_git_repo, capsys):
        """Deps and Rdeps columns appear when projects have intra-workspace deps."""
        _setup_workspace_with_deps(mock_git_repo)
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        header_line = _member_header(captured.out)
        assert "Deps" in header_line
        assert "Rdeps" in header_line

    def test_deps_rdeps_columns_hidden_when_no_deps(self, mock_git_repo, capsys):
        """Deps and Rdeps columns are hidden when no intra-workspace deps exist."""
        _cmd_init({"root-dev-node": True}, project_root=".")
        _make_npm_project(mock_git_repo, "standalone-a", version="1.0.0")
        _make_npm_project(mock_git_repo, "standalone-b", version="1.0.0")
        _cmd_add(["standalone-a"], {"releasable": "false"}, project_root=".")
        _cmd_add(["standalone-b"], {"releasable": "false"}, project_root=".")
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        header_line = _member_header(captured.out)
        assert "Deps" not in header_line
        assert "Rdeps" not in header_line

    def test_correct_dep_counts(self, mock_git_repo, capsys):
        """Verify correct dep and rdep counts for a known dependency structure.

        lib-a: 0 deps, 2 rdeps (lib-b and lib-c depend on it)
        lib-b: 1 dep (lib-a), 1 rdep (lib-c depends on it)
        lib-c: 2 deps (lib-a, lib-b), 0 rdeps
        """
        _setup_workspace_with_deps(mock_git_repo)
        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        lines = _member_table(captured.out)
        header = lines[0]

        # Find column start positions using header text positions
        deps_start = header.index("Deps")
        rdeps_start = header.index("Rdeps")

        # Find end positions: next column start or end of line
        # Columns are separated by 2+ spaces; find next column start after each
        def _col_end(start, col_name):
            """Find end of a column: start of next column header or end of line."""
            after = start + len(col_name)
            # Look for next non-space char after a gap of spaces
            rest = header[after:]
            stripped = rest.lstrip()
            if not stripped:
                return None  # last column
            return after + (len(rest) - len(stripped))

        deps_end = _col_end(deps_start, "Deps")
        rdeps_end = _col_end(rdeps_start, "Rdeps")

        # Parse each project row using fixed positions
        for line in lines[1:]:
            name = line.split()[0]
            deps_val = line[deps_start:deps_end].strip() if deps_end else line[deps_start:].strip()
            rdeps_val = line[rdeps_start:rdeps_end].strip() if rdeps_end else line[rdeps_start:].strip()

            if name == "lib-a":
                assert deps_val == "0", f"lib-a should have 0 deps, got {deps_val}"
                assert rdeps_val == "2", f"lib-a should have 2 rdeps, got {rdeps_val}"
            elif name == "lib-b":
                assert deps_val == "1", f"lib-b should have 1 dep, got {deps_val}"
                assert rdeps_val == "1", f"lib-b should have 1 rdep, got {rdeps_val}"
            elif name == "lib-c":
                assert deps_val == "2", f"lib-c should have 2 deps, got {deps_val}"
                assert rdeps_val == "0", f"lib-c should have 0 rdeps, got {rdeps_val}"

    def test_deps_columns_before_remote(self, mock_git_repo, capsys):
        """Deps/Rdeps columns appear after Unreleased but before Remote."""
        _setup_workspace_with_deps(mock_git_repo)

        # Mirror lib-a's releasable (the watch key is no longer legal, and the
        # mirror destination is a releasable key)
        projects = load_workspace(".")
        make_workspace(".", projects, releasables=[
            {"name": "lib-a",
             "subtree_remote": "git@github.com:user/lib-a.git"},
            {"name": "lib-b"},
            {"name": "lib-c"},
        ])

        capsys.readouterr()
        _cmd_status({}, project_root=".")
        captured = capsys.readouterr()
        header_line = _member_header(captured.out)

        # All dynamic columns should be present
        assert "Deps" in header_line
        assert "Rdeps" in header_line
        assert "Remote" in header_line

        # Verify ordering: Deps before Rdeps before Remote
        deps_pos = header_line.index("Deps")
        rdeps_pos = header_line.index("Rdeps")
        remote_pos = header_line.index("Remote")
        assert deps_pos < rdeps_pos < remote_pos
