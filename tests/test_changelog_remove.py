"""Tests for the `changelog remove` subcommand.

Removal is the surface two error messages used to name and rlsbl did not have:
the orphan check told operators to run `rlsbl changelog edit --commits <hash>
--remove`, a flag that never existed. It could not be an edit mode either --
`changelog edit` declares itself a sparse update and the framework refuses an
invocation that writes no property -- so it is its own command, and these tests
pin its round trip, its refusals, and its wiring.
"""

import json
import os
from unittest import mock

import pytest

import rlsbl
from conftest import make_commit as _make_commit, run_git as _run_git
from rlsbl.changelog.files import get_changes_dir, is_read_only
from rlsbl.changelog.schema import parse_jsonl
from rlsbl.commands.changelog_cmd import cmd_add, cmd_remove


@pytest.fixture
def rlsbl_repo(tmp_path, monkeypatch):
    """A git repo with .rlsbl/ scaffolding and a baseline version tag."""
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

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")

    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci"}) + "\n"
    )
    return repo


def _add_unreleased_entry(repo, sha, description="Feature", entry_type="feature",
                          user_facing=True):
    """Append an entry to unreleased.jsonl through cmd_add."""
    flags = {
        "commits": sha,
        "description": description if user_facing else "",
        "type": entry_type if user_facing else "",
        "user-facing": user_facing,
        "auto-commit": False,
    }
    with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
        cmd_add(flags, project_root=repo)


def _unreleased_entries(repo):
    return parse_jsonl(
        os.path.join(get_changes_dir(str(repo)), "unreleased.jsonl")
    )


def _create_released_jsonl(repo, version, entries):
    """Write a released (chmod 444) versioned JSONL file."""
    changes = repo / ".rlsbl" / "changes"
    jsonl_path = changes / f"{version}.jsonl"

    lines = []
    for entry in entries:
        data = {
            "commits": entry["commits"],
            "user_facing": entry.get("user_facing", False),
        }
        if entry.get("id"):
            data["id"] = entry["id"]
        if entry.get("description"):
            data["description"] = entry["description"]
        if entry.get("type"):
            data["type"] = entry["type"]
        lines.append(json.dumps(data, separators=(",", ":")))

    jsonl_path.write_text("\n".join(lines) + "\n")
    os.chmod(str(jsonl_path), 0o444)
    return jsonl_path


class TestRemoveFromUnreleased:

    def test_removes_the_entry_and_leaves_the_others(self, rlsbl_repo):
        keep_sha = _make_commit(rlsbl_repo, "keep.txt")
        drop_sha = _make_commit(rlsbl_repo, "drop.txt")
        _add_unreleased_entry(rlsbl_repo, keep_sha, description="Kept")
        _add_unreleased_entry(rlsbl_repo, drop_sha, description="Dropped")

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            cmd_remove(
                {"commits": drop_sha, "auto-commit": False},
                project_root=rlsbl_repo,
            )

        entries = _unreleased_entries(rlsbl_repo)
        assert [e.description for e in entries] == ["Kept"]
        assert entries[0].commits == [keep_sha]

    def test_removing_by_id_selects_the_same_entry(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="By id")
        entry_id = _unreleased_entries(rlsbl_repo)[0].id
        assert entry_id

        cmd_remove(
            {"id": entry_id, "auto-commit": False}, project_root=rlsbl_repo,
        )

        assert _unreleased_entries(rlsbl_repo) == []

    def test_the_rewritten_file_still_parses(self, rlsbl_repo):
        first = _make_commit(rlsbl_repo, "a.txt")
        second = _make_commit(rlsbl_repo, "b.txt")
        third = _make_commit(rlsbl_repo, "c.txt")
        _add_unreleased_entry(rlsbl_repo, first, description="First")
        _add_unreleased_entry(rlsbl_repo, second, description="Second")
        _add_unreleased_entry(rlsbl_repo, third, description="Third")

        cmd_remove(
            {"commits": second, "auto-commit": False}, project_root=rlsbl_repo,
        )

        path = os.path.join(get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl")
        raw = open(path, encoding="utf-8").read()
        # Every surviving line is a whole JSON object, and the file ends with
        # exactly one newline -- an atomic rewrite, not a spliced-out line.
        assert raw.endswith("\n") and not raw.endswith("\n\n")
        for line in raw.splitlines():
            json.loads(line)
        assert [e.description for e in _unreleased_entries(rlsbl_repo)] == [
            "First", "Third",
        ]

    def test_auto_commit_commits_the_file(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="Committed")

        with mock.patch(
            "rlsbl.commands.changelog_cmd.commit_files"
        ) as mock_commit:
            cmd_remove({"commits": sha}, project_root=rlsbl_repo)

        mock_commit.assert_called_once()
        message, files = mock_commit.call_args[0][0], mock_commit.call_args[0][1]
        assert "changelog: remove from unreleased:" in message
        assert os.path.join(
            get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl"
        ) in files

    def test_dry_run_writes_nothing(self, rlsbl_repo, capsys):
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="Survives")
        path = os.path.join(get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl")
        before = open(path, encoding="utf-8").read()

        with mock.patch(
            "rlsbl.commands.changelog_cmd.commit_files"
        ) as mock_commit:
            cmd_remove(
                {"commits": sha, "dry-run": True}, project_root=rlsbl_repo,
            )

        assert open(path, encoding="utf-8").read() == before
        mock_commit.assert_not_called()
        assert "dry-run: no files written" in capsys.readouterr().out


class TestRemoveFromReleased:

    def test_round_trip_unlocks_relocks_and_regenerates(self, rlsbl_repo):
        keep_sha = _make_commit(rlsbl_repo, "keep.txt")
        drop_sha = _make_commit(rlsbl_repo, "drop.txt")
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [keep_sha], "user_facing": True,
             "description": "Kept feature", "type": "feature"},
            {"commits": [drop_sha], "user_facing": True,
             "description": "Wrongly recorded feature", "type": "feature"},
        ])
        assert is_read_only(str(jsonl_path))

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"), \
                mock.patch(
                    "rlsbl.commands.changelog_cmd._sync_github_release"
                ) as mock_sync:
            cmd_remove(
                {"commits": drop_sha, "auto-commit": False},
                project_root=rlsbl_repo,
            )

        # Relocked, the surviving entry untouched, CHANGELOG.md regenerated
        # without the removed text, and that version's Release notes re-synced.
        assert is_read_only(str(jsonl_path))
        entries = parse_jsonl(str(jsonl_path))
        assert [e.description for e in entries] == ["Kept feature"]

        changelog = (rlsbl_repo / "CHANGELOG.md").read_text()
        assert "Kept feature" in changelog
        assert "Wrongly recorded feature" not in changelog
        mock_sync.assert_called_once_with("1.0.0")

    def test_auto_commit_covers_the_regenerated_files(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        other = _make_commit(rlsbl_repo, "other.txt")
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True,
             "description": "Gone", "type": "feature"},
            {"commits": [other], "user_facing": True,
             "description": "Stays", "type": "fix"},
        ])

        with mock.patch(
            "rlsbl.commands.changelog_cmd.commit_files"
        ) as mock_commit, mock.patch(
            "rlsbl.commands.changelog_cmd._sync_github_release"
        ):
            cmd_remove({"commits": sha}, project_root=rlsbl_repo)

        message, files = mock_commit.call_args[0][0], mock_commit.call_args[0][1]
        assert message == "changelog: remove from 1.0.0: Gone"
        assert str(jsonl_path) in files
        assert os.path.join(str(rlsbl_repo), "CHANGELOG.md") in files

    def test_dry_run_leaves_the_released_file_alone(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo)
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True,
             "description": "Untouched", "type": "feature"},
        ])
        before = open(str(jsonl_path), encoding="utf-8").read()

        with mock.patch(
            "rlsbl.commands.changelog_cmd._sync_github_release"
        ) as mock_sync:
            cmd_remove(
                {"commits": sha, "dry-run": True}, project_root=rlsbl_repo,
            )

        assert open(str(jsonl_path), encoding="utf-8").read() == before
        assert is_read_only(str(jsonl_path))
        mock_sync.assert_not_called()


class TestRewritePreservesFileMode:
    """A rewrite must not change WHAT the file is, only what it says.

    The atomic rewrite behind `remove` and `edit` used to pin 0o600 -- the mode
    the older hand-rolled mkstemp write happened to leave -- so an ordinary
    0o644 changelog silently became owner-only the first time anyone edited or
    removed an entry. `remap` already carries the same guarantee
    (``preserve_mode=True``); these pin it for the other two writers.
    """

    def test_removing_from_unreleased_keeps_the_files_mode(self, rlsbl_repo):
        sha = _make_commit(rlsbl_repo, "a.txt")
        other = _make_commit(rlsbl_repo, "b.txt")
        _add_unreleased_entry(rlsbl_repo, sha, description="Dropped")
        _add_unreleased_entry(rlsbl_repo, other, description="Kept")
        path = os.path.join(get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl")
        os.chmod(path, 0o644)

        cmd_remove(
            {"commits": sha, "auto-commit": False}, project_root=rlsbl_repo,
        )

        assert os.stat(path).st_mode & 0o777 == 0o644

    def test_editing_an_unreleased_entry_keeps_the_files_mode(self, rlsbl_repo):
        from rlsbl.commands.changelog_cmd import cmd_edit

        sha = _make_commit(rlsbl_repo, "a.txt")
        _add_unreleased_entry(rlsbl_repo, sha, description="Before")
        path = os.path.join(get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl")
        os.chmod(path, 0o644)

        cmd_edit(
            {"commits": sha, "description": "After", "auto-commit": False},
            project_root=rlsbl_repo,
        )

        assert os.stat(path).st_mode & 0o777 == 0o644
        assert [e.description for e in _unreleased_entries(rlsbl_repo)] == ["After"]

    def test_an_unlocked_released_file_keeps_its_mode(self, rlsbl_repo):
        """A released JSONL someone already unlocked must not narrow either.

        ``writable_jsonl`` relocks a file it found read-only, so the 0o600 was
        invisible there -- but a released file sitting at 0o644 goes through the
        same rewrite with no relock to hide the narrowing.
        """
        sha = _make_commit(rlsbl_repo, "a.txt")
        other = _make_commit(rlsbl_repo, "b.txt")
        jsonl_path = _create_released_jsonl(rlsbl_repo, "1.0.0", [
            {"commits": [sha], "user_facing": True,
             "description": "Gone", "type": "feature"},
            {"commits": [other], "user_facing": True,
             "description": "Stays", "type": "fix"},
        ])
        os.chmod(str(jsonl_path), 0o644)

        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"), \
                mock.patch(
                    "rlsbl.commands.changelog_cmd._sync_github_release"
                ):
            cmd_remove(
                {"commits": sha, "auto-commit": False},
                project_root=rlsbl_repo,
            )

        assert os.stat(str(jsonl_path)).st_mode & 0o777 == 0o644


class TestRemoveRefusals:

    def test_zero_matches_is_a_hard_error(self, rlsbl_repo, capsys):
        sha = _make_commit(rlsbl_repo)
        other = _make_commit(rlsbl_repo, "other.txt")
        _add_unreleased_entry(rlsbl_repo, sha, description="Only entry")

        with pytest.raises(SystemExit) as exc:
            cmd_remove(
                {"commits": other, "auto-commit": False},
                project_root=rlsbl_repo,
            )
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "No changelog entry found for commit(s)" in err
        assert other[:12] in err
        # Nothing was written.
        assert [e.description for e in _unreleased_entries(rlsbl_repo)] == [
            "Only entry",
        ]

    def test_multiple_matches_names_every_match_and_writes_nothing(
        self, rlsbl_repo, capsys,
    ):
        sha = _make_commit(rlsbl_repo)
        _add_unreleased_entry(rlsbl_repo, sha, description="A feature",
                              entry_type="feature")
        _add_unreleased_entry(rlsbl_repo, sha, description="A fix",
                              entry_type="fix")
        path = os.path.join(get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl")
        before = open(path, encoding="utf-8").read()

        with pytest.raises(SystemExit) as exc:
            cmd_remove(
                {"commits": sha, "auto-commit": False}, project_root=rlsbl_repo,
            )
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "selects 2 entries" in err
        assert "A feature" in err and "A fix" in err
        # The ids are named, so the operator can re-run naming exactly one.
        for entry in parse_jsonl(path):
            assert entry.id in err
        assert "rlsbl changelog remove --id" in err
        assert open(path, encoding="utf-8").read() == before

    def test_an_unresolvable_hash_matching_nothing_is_a_no_match(
        self, rlsbl_repo, capsys,
    ):
        with pytest.raises(SystemExit) as exc:
            cmd_remove(
                {"commits": "deadbeefdeadbeef", "auto-commit": False},
                project_root=rlsbl_repo,
            )
        assert exc.value.code == 1
        assert "No changelog entry found" in capsys.readouterr().err

    def test_an_unresolvable_hash_an_entry_stores_still_selects_it(
        self, rlsbl_repo,
    ):
        """Removal accepts a hash git can no longer resolve.

        Unlike `changelog edit`, which keeps the entry and so must be able to
        find its commits, `changelog remove` deletes it -- and the entry a
        removal is usually for is exactly the one a rebase or a scrub left
        naming commits that are gone. Refusing the hash would make the orphan
        check's own remedy unfollowable.
        """
        stale = "b2" * 20
        path = os.path.join(get_changes_dir(str(rlsbl_repo)), "unreleased.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "commits": [stale],
                "user_facing": True,
                "description": "Stale entry",
                "type": "feature",
            }, separators=(",", ":")) + "\n")

        cmd_remove(
            {"commits": stale, "auto-commit": False}, project_root=rlsbl_repo,
        )
        assert _unreleased_entries(rlsbl_repo) == []


class TestRemoveCliSurface:
    """Registration facts: the selector's election and the effect declaration.

    The dispatch wiring itself (which flags reach ``cmd_remove``) is pinned in
    ``tests/test_cli_coverage_wiring.py`` with every other command's.
    """

    def test_the_two_selectors_are_exactly_one(self):
        """Neither selector, and both at once, are refused by the framework."""
        with mock.patch("rlsbl.commands.changelog_cmd.cmd_remove") as m:
            neither = rlsbl.app.test(["changelog", "remove"])
            both = rlsbl.app.test(
                ["changelog", "remove", "--id", "01H0", "--commits", "abc123"]
            )
        assert neither.exit_code != 0
        assert both.exit_code != 0
        m.assert_not_called()

    def test_an_empty_selector_value_is_refused(self):
        with mock.patch("rlsbl.commands.changelog_cmd.cmd_remove") as m:
            result = rlsbl.app.test(["changelog", "remove", "--commits", ""])
        assert result.exit_code != 0
        assert "empty value" in result.stderr
        m.assert_not_called()

    def test_it_is_mutating_and_not_consequential(self):
        """Same classification as `changelog amend` and `changelog edit`.

        All three rewrite a JSONL file in the working tree and, for a released
        version, re-sync one GitHub Release's notes. None of them publishes,
        deletes a ref, or rewrites history, which is what the consequential set
        is reserved for.
        """
        group = rlsbl.app._groups["changelog"]
        remove = group.commands["remove"]
        assert remove.effect == "mutating"
        assert remove.consequential is False
        for sibling in ("amend", "edit"):
            assert group.commands[sibling].consequential is remove.consequential

    def test_dry_run_is_supported(self):
        remove = rlsbl.app._groups["changelog"].commands["remove"]
        assert remove.dry_run_supported is True

    def test_it_is_recorded_in_the_committed_coverage_manifest(self):
        """A new command must join `.strictcli/test-coverage.json`.

        The manifest is committed and unioned with the local shards, so a
        command covered only by this session's shard files reads as uncovered
        on a fresh clone until the manifest is regenerated.
        """
        import pathlib

        manifest = pathlib.Path(rlsbl.__file__).resolve().parents[1] \
            / ".strictcli" / "test-coverage.json"
        assert "changelog.remove" in set(
            json.loads(manifest.read_text(encoding="utf-8"))
        )
