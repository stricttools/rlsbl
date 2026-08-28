"""What two `rlsbl changelog add` runs at once do to each other.

The append itself is a single append-mode write that never reads the file back,
so two concurrent adds cannot clobber each other's entry -- the property the
torn-line test below pins from the other side, since a guarded append is the
one place a reader is involved at all.

The collision that IS real is between their auto-COMMITS: whichever commits
first stages the file as it stands and carries BOTH entries, and the loser's
commit then finds nothing left to stage and exits non-zero. That is not the
entry going missing, and it must not be reported as if it were. What must be
reported, loudly, is the opposite case: the entry appended to the working tree
with no commit carrying it.
"""

import json
import subprocess
from unittest import mock

import pytest

from conftest import make_commit as _make_commit, run_git as _run_git
from rlsbl.changelog.files import append_entry, get_changes_dir, read_unreleased
from rlsbl.changelog.schema import (
    ChangelogEntry,
    generate_entry_id,
    serialize_entry,
)
from rlsbl.commands.changelog_cmd import cmd_add


@pytest.fixture
def rlsbl_repo(tmp_path, monkeypatch):
    """A git repo with .rlsbl/changes/ and a baseline version tag."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")
    _run_git(repo, "tag", "v0.0.0")

    (repo / ".rlsbl" / "changes").mkdir(parents=True)
    return repo


def _entry(description="Something"):
    return ChangelogEntry(
        commits=["a" * 40], user_facing=True, description=description, type="fix",
        id=generate_entry_id(),
    )


class TestGuardedAppend:
    """The append never damages, and is never damaged by, existing content."""

    def test_torn_last_line_does_not_swallow_the_new_entry(self, tmp_path):
        """A file whose last line lost its newline still gets a whole new line.

        An interrupted write or a hand edit can leave the final line without its
        newline. A plain append then concatenates the new entry onto the damaged
        one, so BOTH lines are lost: the damaged one stays unparseable and the
        new one never exists as a line of its own.
        """
        changes_dir = tmp_path / "changes"
        changes_dir.mkdir()
        torn = serialize_entry(_entry("Torn"))[:-10]  # truncated mid-line, no newline
        (changes_dir / "unreleased.jsonl").write_text(torn)

        append_entry(str(changes_dir), _entry("Whole"))

        lines = (changes_dir / "unreleased.jsonl").read_text().splitlines()
        assert len(lines) == 2, "the new entry must start its own line"
        assert lines[0] == torn, "the damaged line stays exactly as damaged as it was"
        assert json.loads(lines[1])["description"] == "Whole"

    def test_append_carries_the_whole_batch_over_a_racing_write(self, tmp_path):
        """A line written by a racer between two appends survives both.

        The append reads nothing back, so content that appeared after the caller
        built its entry -- another process's line -- is still there afterwards.
        """
        changes_dir = tmp_path / "changes"
        changes_dir.mkdir()
        target = changes_dir / "unreleased.jsonl"

        append_entry(str(changes_dir), _entry("First"))
        with open(target, "a", encoding="utf-8") as f:  # the racing process
            f.write(serialize_entry(_entry("Racer")) + "\n")
        append_entry(str(changes_dir), _entry("Third"))

        assert [e.description for e in read_unreleased(str(changes_dir))] == [
            "First", "Racer", "Third",
        ]


class TestAutoCommitRace:
    """The auto-commit's outcome is judged by git, not by its exit status."""

    def _flags(self):
        return {
            "commits": None,  # filled per test
            "description": "",
            "type": "",
            "user-facing": False,
            "auto-commit": True,
        }

    def test_losing_the_commit_race_is_not_reported_as_a_lost_entry(
        self, rlsbl_repo, capsys,
    ):
        """A concurrent add that committed our line first is a benign outcome.

        The losing run's own commit fails ("nothing left to stage"), and it must
        say so as what it is -- the entry IS recorded -- instead of leaving the
        operator with a bare commit failure to interpret.
        """
        sha = _make_commit(rlsbl_repo)
        flags = self._flags() | {"commits": sha}

        def racing_commit(message, files, **kwargs):
            """The other add commits the file (both lines), then we fail."""
            _run_git(rlsbl_repo, "add", ".rlsbl/changes/unreleased.jsonl")
            _run_git(rlsbl_repo, "commit", "-q", "-m", "changelog: the other add")
            return False

        with mock.patch(
            "rlsbl.commands.changelog_cmd.commit_files", side_effect=racing_commit,
        ):
            cmd_add(flags, project_root=rlsbl_repo)

        out = capsys.readouterr()
        combined = out.out + out.err
        assert "concurrent" in combined.lower(), combined
        assert len(read_unreleased(get_changes_dir(str(rlsbl_repo)))) == 1

    def test_an_uncommitted_entry_is_a_hard_error(self, rlsbl_repo, capsys):
        """A commit that really did not happen must not be a warning.

        The entry is on disk and no commit carries it: a tree-cleaning step or
        another session's checkout loses it, and the operator was told the add
        succeeded.
        """
        sha = _make_commit(rlsbl_repo)
        flags = self._flags() | {"commits": sha}

        with mock.patch(
            "rlsbl.commands.changelog_cmd.commit_files", return_value=False,
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_add(flags, project_root=rlsbl_repo)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "unreleased.jsonl" in err
        assert "safegit commit" in err

    def test_a_clean_commit_says_nothing_about_races(self, rlsbl_repo, capsys):
        """The ordinary path is unchanged: one commit, no race commentary."""
        sha = _make_commit(rlsbl_repo)
        flags = self._flags() | {"commits": sha}

        cmd_add(flags, project_root=rlsbl_repo)

        out = capsys.readouterr()
        assert "concurrent" not in (out.out + out.err).lower()
        head_blob = subprocess.run(
            ["git", "-C", str(rlsbl_repo), "show",
             "HEAD:.rlsbl/changes/unreleased.jsonl"],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout
        assert head_blob.strip(), "the entry must be committed"
