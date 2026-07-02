"""Tests for --dry-run support in changelog add/amend/edit.

The global --dry-run flag must be threaded from the CLI wrappers
(cmd_chlog_add/amend/edit) into the command implementations, and under
dry-run all validation must still run but NOTHING may be written: no
JSONL append/rewrite, no auto-commit, no config.json exclusion write,
no CHANGELOG.md regeneration, no GitHub Release sync.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import run_git as _run_git, make_commit as _make_commit, git_head
from rlsbl.changelog.files import get_changes_dir, read_unreleased
from rlsbl.commands.changelog_cmd import cmd_add, cmd_amend, cmd_edit


@pytest.fixture
def rlsbl_repo(tmp_path, monkeypatch):
    """Git repo with .rlsbl/ scaffolding, a baseline tag, and a released JSONL."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")
    _run_git(repo, "tag", "v0.1.0")

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")

    # A released, read-only JSONL for amend/edit tests
    released = changes / "0.1.0.jsonl"
    released.write_text("")
    os.chmod(released, 0o444)

    (repo / ".rlsbl" / "config.json").write_text(json.dumps({
        "private": True,
        "targets": [],
        "batch_limits": {"max_commits_per_entry": 2},
    }))

    _run_git(repo, "add", ".rlsbl")
    _run_git(repo, "commit", "-q", "-m", "scaffold")
    return repo


def _snapshot(repo):
    """Capture HEAD, porcelain status, and the content of every .rlsbl file."""
    head = git_head(repo)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    contents = {}
    for path in sorted((repo / ".rlsbl").rglob("*")):
        if path.is_file():
            contents[str(path.relative_to(repo))] = path.read_bytes()
    return head, status, contents


# ---------------------------------------------------------------------------
# cmd_add --dry-run
# ---------------------------------------------------------------------------


class TestAddDryRun:
    def test_dry_run_writes_nothing(self, rlsbl_repo, capsys):
        """add --dry-run must leave the tree and git state byte-identical."""
        sha = _make_commit(rlsbl_repo)
        before = _snapshot(rlsbl_repo)

        cmd_add({
            "commits": sha[:12],
            "description": "New feature",
            "type": "feature",
            "user-facing": True,
            "dry-run": True,
        }, project_root=rlsbl_repo)

        after = _snapshot(rlsbl_repo)
        assert after == before  # no commits, no file changes, byte-identical
        assert read_unreleased(get_changes_dir(".")) == []

        out = capsys.readouterr().out
        assert "(dry-run: no files written)" in out
        # The entry JSON that WOULD be written is printed as the first line
        printed = json.loads(out.splitlines()[0])
        assert printed["user_facing"] is True
        assert printed["description"] == "New feature"
        assert printed["type"] == "feature"
        assert printed["commits"] == [sha]  # resolved full hash

    def test_dry_run_still_validates_bad_hash(self, rlsbl_repo):
        """Validation still runs under dry-run: unresolvable hash errors out."""
        with pytest.raises(SystemExit) as exc_info:
            cmd_add({
                "commits": "deadbeefdeadbeef",
                "description": "Stuff",
                "type": "fix",
                "user-facing": True,
                "dry-run": True,
            }, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_dry_run_still_validates_batch_limit(self, rlsbl_repo):
        """Batch limit check still fires under dry-run without --allow-batch."""
        shas = [_make_commit(rlsbl_repo, filename=f"f{i}.txt") for i in range(3)]
        with pytest.raises(SystemExit) as exc_info:
            cmd_add({
                "commits": ",".join(shas),
                "description": "Big batch",
                "type": "feature",
                "user-facing": True,
                "dry-run": True,
            }, project_root=rlsbl_repo)
        assert exc_info.value.code == 1

    def test_dry_run_allow_batch_does_not_write_config(self, rlsbl_repo, capsys):
        """--allow-batch under dry-run must not write the config.json exclusion."""
        shas = [_make_commit(rlsbl_repo, filename=f"f{i}.txt") for i in range(3)]
        before = _snapshot(rlsbl_repo)

        cmd_add({
            "commits": ",".join(shas),
            "description": "Big batch",
            "type": "feature",
            "user-facing": True,
            "allow-batch": True,
            "dry-run": True,
        }, project_root=rlsbl_repo)

        after = _snapshot(rlsbl_repo)
        assert after == before
        config = json.loads((rlsbl_repo / ".rlsbl" / "config.json").read_text())
        assert "exclusions" not in config.get("batch_limits", {})
        assert "(dry-run: no files written)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_amend --dry-run
# ---------------------------------------------------------------------------


class TestAmendDryRun:
    def test_dry_run_writes_nothing(self, rlsbl_repo, capsys):
        """amend --dry-run must not touch the released JSONL, CHANGELOG, or gh."""
        sha = _make_commit(rlsbl_repo)
        before = _snapshot(rlsbl_repo)

        with patch(
            "rlsbl.commands.changelog_cmd._sync_github_release"
        ) as mock_sync, patch(
            "rlsbl.commands.changelog_cmd.generate_changelog"
        ) as mock_gen:
            cmd_amend({
                "version": "0.1.0",
                "commits": sha,
                "description": "Amended fix",
                "type": "fix",
                "user-facing": True,
                "validate-hashes": True,
                "dry-run": True,
            }, project_root=rlsbl_repo)

        after = _snapshot(rlsbl_repo)
        assert after == before
        mock_sync.assert_not_called()
        mock_gen.assert_not_called()
        # Released file stays read-only and empty
        released = rlsbl_repo / ".rlsbl" / "changes" / "0.1.0.jsonl"
        assert released.read_text() == ""
        assert not os.access(released, os.W_OK)

        out = capsys.readouterr().out
        assert "(dry-run: no files written)" in out
        assert "Amended fix" in out

    def test_dry_run_still_validates_missing_file(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        with pytest.raises(SystemExit) as exc_info:
            cmd_amend({
                "version": "9.9.9",
                "commits": sha,
                "description": "x",
                "type": "fix",
                "user-facing": True,
                "validate-hashes": True,
                "dry-run": True,
            }, project_root=rlsbl_repo)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# cmd_edit --dry-run
# ---------------------------------------------------------------------------


class TestEditDryRun:
    def test_dry_run_writes_nothing(self, rlsbl_repo, capsys):
        """edit --dry-run must not rewrite the JSONL file or commit."""
        sha = _make_commit(rlsbl_repo)
        # Seed an unreleased entry to edit (real add, no dry-run)
        cmd_add({
            "commits": sha,
            "description": "Original",
            "type": "feature",
            "user-facing": True,
            "auto-commit": True,
        }, project_root=rlsbl_repo)
        before = _snapshot(rlsbl_repo)

        cmd_edit({
            "commits": sha,
            "description": "Edited description",
            "type": "",
            "user-facing": None,
            "dry-run": True,
        }, project_root=rlsbl_repo)

        after = _snapshot(rlsbl_repo)
        assert after == before
        entries = read_unreleased(get_changes_dir("."))
        assert entries[0].description == "Original"

        out = capsys.readouterr().out
        assert "(dry-run: no files written)" in out
        assert "Edited description" in out


# ---------------------------------------------------------------------------
# CLI wrappers must thread the global dry_run flag into the flags dict
# ---------------------------------------------------------------------------


class TestWrapperThreading:
    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.commands.changelog_cmd.cmd_add")
    def test_add_wrapper_threads_dry_run(self, mock_add, _root):
        import rlsbl

        rlsbl.cmd_chlog_add(
            commits="abc", description="d", type="fix",
            user_facing=True, auto_commit=True, allow_batch=False,
            dry_run=True,
        )
        flags = mock_add.call_args[0][0]
        assert flags.get("dry-run") is True

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.commands.changelog_cmd.cmd_amend")
    def test_amend_wrapper_threads_dry_run(self, mock_amend, _root):
        import rlsbl

        rlsbl.cmd_chlog_amend(
            version="0.1.0", commits="abc", description="d", type="fix",
            user_facing=True, validate_hashes=True,
            dry_run=True,
        )
        flags = mock_amend.call_args[0][0]
        assert flags.get("dry-run") is True

    @patch("rlsbl._require_sub_project_root", return_value=Path("/fake/project"))
    @patch("rlsbl.commands.changelog_cmd.cmd_edit")
    def test_edit_wrapper_threads_dry_run(self, mock_edit, _root):
        import rlsbl

        rlsbl.cmd_chlog_edit(
            commits="abc", type="fix", description="d",
            user_facing=None, auto_commit=True,
            dry_run=True,
        )
        flags = mock_edit.call_args[0][0]
        assert flags.get("dry-run") is True
