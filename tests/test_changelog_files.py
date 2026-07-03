"""Tests for rlsbl.changelog.files."""

import json
import os
import stat

import pytest

from conftest import run_git as _run_git, git_head as _git_head, make_commit as _make_commit
from rlsbl.changelog.files import (
    _parse_semver,
    append_entry,
    changes_dir_exists,
    finalize_version,
    get_changes_dir,
    is_read_only,
    list_versioned_files,
    read_unreleased,
)
from rlsbl.changelog.schema import ChangelogEntry, parse_jsonl
from rlsbl.errors import ChangelogError


class TestGetChangesDir:
    """Tests for get_changes_dir."""

    def test_returns_expected_path(self, tmp_path):
        result = get_changes_dir(str(tmp_path))
        assert result == os.path.join(str(tmp_path), ".rlsbl", "changes")


class TestChangesDirExists:
    """Tests for changes_dir_exists."""

    def test_returns_false_when_missing(self, tmp_path):
        assert changes_dir_exists(str(tmp_path)) is False

    def test_returns_true_when_present(self, tmp_path):
        (tmp_path / ".rlsbl" / "changes").mkdir(parents=True)
        assert changes_dir_exists(str(tmp_path)) is True


class TestListVersionedFiles:
    """Tests for list_versioned_files."""

    def test_empty_directory(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        assert list_versioned_files(str(changes)) == []

    def test_nonexistent_directory(self, tmp_path):
        assert list_versioned_files(str(tmp_path / "nope")) == []

    def test_sorts_by_semver_descending(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        # Create files in non-sorted order
        for name in ["0.1.0.jsonl", "1.0.0.jsonl", "0.2.0.jsonl", "0.1.1.jsonl", "2.0.0.jsonl"]:
            (changes / name).write_text("")

        result = list_versioned_files(str(changes))
        versions = [ver for ver, _ in result]
        assert versions == ["2.0.0", "1.0.0", "0.2.0", "0.1.1", "0.1.0"]

    def test_ignores_non_versioned_files(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "1.0.0.jsonl").write_text("")
        (changes / "unreleased.jsonl").write_text("")
        (changes / "notes.txt").write_text("")
        (changes / ".validated").write_text("")

        result = list_versioned_files(str(changes))
        assert len(result) == 1
        assert result[0][0] == "1.0.0"

    def test_returns_full_paths(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "1.2.3.jsonl").write_text("")

        result = list_versioned_files(str(changes))
        assert result[0][1] == str(changes / "1.2.3.jsonl")


class TestReadUnreleased:
    """Tests for read_unreleased."""

    def test_returns_empty_when_file_missing(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        assert read_unreleased(str(changes)) == []

    def test_reads_entries(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        path = changes / "unreleased.jsonl"
        lines = [
            json.dumps({"commits": ["a"], "user_facing": False}),
            json.dumps({"commits": ["b"], "user_facing": True, "description": "X", "type": "fix"}),
        ]
        path.write_text("\n".join(lines) + "\n")
        entries = read_unreleased(str(changes))
        assert len(entries) == 2
        assert entries[0].commits == ["a"]
        assert entries[1].type == "fix"


class TestAppendEntry:
    """Tests for append_entry."""

    def test_creates_dir_and_file(self, tmp_path):
        changes = str(tmp_path / ".rlsbl" / "changes")
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        append_entry(changes, entry)

        assert os.path.isdir(changes)
        entries = read_unreleased(changes)
        assert len(entries) == 1
        assert entries[0].commits == ["abc"]

    def test_appends_to_existing(self, tmp_path):
        changes = str(tmp_path / ".rlsbl" / "changes")
        entry1 = ChangelogEntry(commits=["abc"], user_facing=False)
        entry2 = ChangelogEntry(
            commits=["def"],
            user_facing=True,
            description="Feature",
            type="feature",
        )
        append_entry(changes, entry1)
        append_entry(changes, entry2)

        entries = read_unreleased(changes)
        assert len(entries) == 2
        assert entries[0].commits == ["abc"]
        assert entries[1].description == "Feature"

    def test_no_temp_files_left(self, tmp_path):
        changes = str(tmp_path / ".rlsbl" / "changes")
        entry = ChangelogEntry(commits=["abc"], user_facing=False)
        append_entry(changes, entry)

        files = os.listdir(changes)
        assert all(not f.endswith(".tmp") for f in files)


class TestFinalizeVersion:
    """Tests for finalize_version."""

    def test_renames_and_creates_new_unreleased(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "1.0.0")

        versioned = changes / "1.0.0.jsonl"
        assert versioned.exists()
        assert not unreleased.read_text().strip()  # new empty file
        # Original content moved to versioned file
        entries = parse_jsonl(str(versioned))
        assert len(entries) == 1
        assert entries[0].commits == ["abc"]

    def test_versioned_file_is_read_only(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "unreleased.jsonl").write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "2.0.0")

        versioned = changes / "2.0.0.jsonl"
        mode = os.stat(str(versioned)).st_mode
        assert not (mode & stat.S_IWUSR)
        assert not (mode & stat.S_IWGRP)
        assert not (mode & stat.S_IWOTH)

    def test_raises_when_no_unreleased(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()

        with pytest.raises(FileNotFoundError, match="unreleased.jsonl not found"):
            finalize_version(str(changes), "1.0.0")

    def test_refuses_to_overwrite_existing_versioned_file(self, tmp_path):
        """A re-run after a mid-release failure must not clobber an
        already-finalized (read-only) changelog file."""
        changes = tmp_path / "changes"
        changes.mkdir()

        existing_content = (
            json.dumps({"commits": ["old1"], "user_facing": False}) + "\n"
        )
        existing = changes / "1.2.3.jsonl"
        existing.write_text(existing_content)
        os.chmod(str(existing), 0o444)

        unreleased_content = (
            json.dumps({"commits": ["new1"], "user_facing": False}) + "\n"
        )
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(unreleased_content)

        with pytest.raises(ChangelogError, match=r"1\.2\.3\.jsonl"):
            finalize_version(str(changes), "1.2.3")

        # The already-finalized file is untouched (content and read-only mode)
        assert existing.read_text() == existing_content
        assert not (os.stat(str(existing)).st_mode & stat.S_IWUSR)
        # unreleased.jsonl still exists with its original content
        assert unreleased.read_text() == unreleased_content


class TestFinalizeVersionStaleWarning:
    """Tests for finalize_version's stale-entry warning (monorepo mode)."""

    @pytest.fixture
    def monorepo_repo(self, tmp_path, monkeypatch):
        """Git repo with one pre-tag commit, a monorepo tag, and a post-tag commit."""
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)

        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")

        (repo / "README.md").write_text("# test\n")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial")

        pre_tag_sha = _git_head(repo)

        _run_git(repo, "tag", "mylib@v0.1.0")

        post_tag_sha = _make_commit(repo, "post.txt", "post-tag commit")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)

        return repo, pre_tag_sha, post_tag_sha

    def test_finalize_no_warnings_when_all_in_range(self, monorepo_repo, capsys):
        """All entries reference in-range commits => no warnings."""
        repo, _pre_tag_sha, post_tag_sha = monorepo_repo
        changes = repo / ".rlsbl" / "changes"
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            json.dumps({"commits": [post_tag_sha], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "0.2.0", tag_glob="mylib@v*")

        captured = capsys.readouterr()
        assert "warning" not in captured.err
        # Rename still happened.
        assert (changes / "0.2.0.jsonl").exists()
        assert unreleased.exists()
        assert unreleased.read_text() == ""

    def test_finalize_warns_on_stale_entries(self, monorepo_repo, capsys):
        """Entry referencing a pre-tag commit => warning printed, rename still happens."""
        repo, pre_tag_sha, post_tag_sha = monorepo_repo
        changes = repo / ".rlsbl" / "changes"
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            "\n".join(
                [
                    json.dumps({"commits": [post_tag_sha], "user_facing": False}),
                    json.dumps({"commits": [pre_tag_sha], "user_facing": False}),
                ]
            )
            + "\n"
        )

        finalize_version(str(changes), "0.2.0", tag_glob="mylib@v*")

        captured = capsys.readouterr()
        assert "warning" in captured.err
        assert "line 2" in captured.err
        assert pre_tag_sha in captured.err
        # Line 1 references an in-range commit => not mentioned.
        assert "line 1" not in captured.err
        # Rename still happened.
        assert (changes / "0.2.0.jsonl").exists()
        assert unreleased.read_text() == ""

    def test_finalize_no_warnings_without_tag_glob(self, monorepo_repo, capsys):
        """Without tag_glob (non-monorepo case), no stale check runs."""
        repo, pre_tag_sha, _post_tag_sha = monorepo_repo
        changes = repo / ".rlsbl" / "changes"
        unreleased = changes / "unreleased.jsonl"
        # Even an entry with a pre-tag commit should not trigger a warning
        # because we didn't pass tag_glob.
        unreleased.write_text(
            json.dumps({"commits": [pre_tag_sha], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "0.2.0")

        captured = capsys.readouterr()
        assert "warning" not in captured.err
        assert (changes / "0.2.0.jsonl").exists()


class TestIsReadOnly:
    """Tests for is_read_only."""

    def test_nonexistent_file(self, tmp_path):
        assert is_read_only(str(tmp_path / "nope")) is False

    def test_writable_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        assert is_read_only(str(f)) is False

    def test_read_only_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        os.chmod(str(f), 0o444)
        assert is_read_only(str(f)) is True


class TestParseSemverPrerelease:
    """Tests for _parse_semver with pre-release filenames."""

    def test_stable_version(self):
        result = _parse_semver("1.2.3.jsonl")
        assert result == (1, 2, 3, 1, 0, 0)

    def test_alpha_version(self):
        result = _parse_semver("0.43.0-alpha.0.jsonl")
        assert result == (0, 43, 0, 0, 0, 0)

    def test_beta_version(self):
        result = _parse_semver("1.0.0-beta.3.jsonl")
        assert result == (1, 0, 0, 0, 1, 3)

    def test_rc_version(self):
        result = _parse_semver("2.1.0-rc.7.jsonl")
        assert result == (2, 1, 0, 0, 2, 7)

    def test_non_versioned_returns_none(self):
        assert _parse_semver("unreleased.jsonl") is None

    def test_unknown_preid_returns_none(self):
        assert _parse_semver("1.0.0-gamma.0.jsonl") is None

    def test_no_extension_returns_none(self):
        assert _parse_semver("1.0.0") is None

    def test_sort_order_prerelease_before_stable(self):
        """Pre-releases of the same base version sort before the stable release."""
        alpha = _parse_semver("0.43.0-alpha.0.jsonl")
        stable = _parse_semver("0.43.0.jsonl")
        assert alpha < stable

    def test_sort_order_alpha_before_beta(self):
        alpha = _parse_semver("0.43.0-alpha.0.jsonl")
        beta = _parse_semver("0.43.0-beta.0.jsonl")
        assert alpha < beta

    def test_sort_order_beta_before_rc(self):
        beta = _parse_semver("0.43.0-beta.0.jsonl")
        rc = _parse_semver("0.43.0-rc.0.jsonl")
        assert beta < rc

    def test_sort_order_rc_before_stable(self):
        rc = _parse_semver("0.43.0-rc.0.jsonl")
        stable = _parse_semver("0.43.0.jsonl")
        assert rc < stable

    def test_sort_order_alpha_counter_increment(self):
        a0 = _parse_semver("0.43.0-alpha.0.jsonl")
        a1 = _parse_semver("0.43.0-alpha.1.jsonl")
        a5 = _parse_semver("0.43.0-alpha.5.jsonl")
        assert a0 < a1 < a5

    def test_sort_order_full_prerelease_cycle(self):
        """Full sort order: alpha.0 < alpha.1 < beta.0 < rc.0 < stable."""
        keys = [
            _parse_semver("0.43.0-alpha.0.jsonl"),
            _parse_semver("0.43.0-alpha.1.jsonl"),
            _parse_semver("0.43.0-beta.0.jsonl"),
            _parse_semver("0.43.0-rc.0.jsonl"),
            _parse_semver("0.43.0.jsonl"),
        ]
        assert keys == sorted(keys)

    def test_different_base_versions_still_sort_correctly(self):
        """Pre-release of a higher base version sorts after stable of a lower."""
        stable_low = _parse_semver("0.42.0.jsonl")
        alpha_high = _parse_semver("0.43.0-alpha.0.jsonl")
        assert stable_low < alpha_high


class TestListVersionedFilesPrerelease:
    """Tests for list_versioned_files with pre-release filenames."""

    def test_includes_prerelease_files(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        for name in ["0.43.0-alpha.0.jsonl", "0.43.0.jsonl", "0.42.0.jsonl"]:
            (changes / name).write_text("")

        result = list_versioned_files(str(changes))
        versions = [ver for ver, _ in result]
        assert "0.43.0-alpha.0" in versions
        assert "0.43.0" in versions
        assert "0.42.0" in versions

    def test_sorts_prerelease_before_stable(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        for name in [
            "0.43.0.jsonl",
            "0.43.0-alpha.0.jsonl",
            "0.43.0-beta.0.jsonl",
            "0.43.0-rc.0.jsonl",
        ]:
            (changes / name).write_text("")

        result = list_versioned_files(str(changes))
        versions = [ver for ver, _ in result]
        # Newest first (descending), so stable first, then rc, beta, alpha
        assert versions == [
            "0.43.0",
            "0.43.0-rc.0",
            "0.43.0-beta.0",
            "0.43.0-alpha.0",
        ]

    def test_mixed_stable_and_prerelease_sort(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        for name in [
            "0.42.0.jsonl",
            "0.43.0-alpha.0.jsonl",
            "0.43.0-alpha.1.jsonl",
            "0.43.0-beta.0.jsonl",
            "0.43.0.jsonl",
            "1.0.0-rc.0.jsonl",
        ]:
            (changes / name).write_text("")

        result = list_versioned_files(str(changes))
        versions = [ver for ver, _ in result]
        assert versions == [
            "1.0.0-rc.0",
            "0.43.0",
            "0.43.0-beta.0",
            "0.43.0-alpha.1",
            "0.43.0-alpha.0",
            "0.42.0",
        ]

    def test_ignores_unknown_preid_files(self, tmp_path):
        """Files with unknown preids (e.g. gamma) are silently ignored."""
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "1.0.0.jsonl").write_text("")
        (changes / "1.0.0-gamma.0.jsonl").write_text("")

        result = list_versioned_files(str(changes))
        versions = [ver for ver, _ in result]
        assert versions == ["1.0.0"]

    def test_returns_full_paths_for_prerelease(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "0.43.0-alpha.0.jsonl").write_text("")

        result = list_versioned_files(str(changes))
        assert result[0][1] == str(changes / "0.43.0-alpha.0.jsonl")


class TestFinalizeVersionPrerelease:
    """Tests for finalize_version with pre-release versions."""

    def test_finalize_prerelease_version(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        unreleased = changes / "unreleased.jsonl"
        unreleased.write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "0.43.0-alpha.0")

        versioned = changes / "0.43.0-alpha.0.jsonl"
        assert versioned.exists()
        assert not unreleased.read_text().strip()
        entries = parse_jsonl(str(versioned))
        assert len(entries) == 1
        assert entries[0].commits == ["abc"]

    def test_finalize_prerelease_is_read_only(self, tmp_path):
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "unreleased.jsonl").write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "1.0.0-beta.3")

        versioned = changes / "1.0.0-beta.3.jsonl"
        mode = os.stat(str(versioned)).st_mode
        assert not (mode & stat.S_IWUSR)
        assert not (mode & stat.S_IWGRP)
        assert not (mode & stat.S_IWOTH)

    def test_finalize_prerelease_shows_up_in_list(self, tmp_path):
        """A finalized pre-release file is discoverable by list_versioned_files."""
        changes = tmp_path / "changes"
        changes.mkdir()
        (changes / "unreleased.jsonl").write_text(
            json.dumps({"commits": ["abc"], "user_facing": False}) + "\n"
        )

        finalize_version(str(changes), "0.43.0-alpha.0")

        result = list_versioned_files(str(changes))
        versions = [ver for ver, _ in result]
        assert "0.43.0-alpha.0" in versions


# ---------------------------------------------------------------------------
# enumerate_changelog_dirs / validate_all_hashes_resolve
# ---------------------------------------------------------------------------


class TestEnumerateChangelogDirs:
    """enumerate_changelog_dirs must cover per-project .rlsbl/changes AND
    releasable-level .rlsbl-monorepo/releasables/*/changes directories."""

    def test_standalone(self, tmp_path):
        from rlsbl.changelog.files import enumerate_changelog_dirs

        changes = tmp_path / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        dirs = enumerate_changelog_dirs(str(tmp_path), None)
        assert dirs == [str(changes)]

    def test_standalone_missing_dir(self, tmp_path):
        from rlsbl.changelog.files import enumerate_changelog_dirs

        assert enumerate_changelog_dirs(str(tmp_path), None) == []

    def test_monorepo_includes_releasable_dirs(self, tmp_path):
        from rlsbl.changelog.files import enumerate_changelog_dirs

        ws = tmp_path / "ws"
        proj_changes = ws / "packages" / "alpha" / ".rlsbl" / "changes"
        proj_changes.mkdir(parents=True)
        rel_changes = ws / ".rlsbl-monorepo" / "releasables" / "core" / "changes"
        rel_changes.mkdir(parents=True)
        # A releasable dir without a changes/ subdir must be skipped
        (ws / ".rlsbl-monorepo" / "releasables" / "empty").mkdir(parents=True)

        (ws / ".rlsbl-monorepo").mkdir(exist_ok=True)
        (ws / ".rlsbl-monorepo" / "workspace.toml").write_text(
            'projects = [{ path = "packages/alpha", name = "alpha" }]\n'
        )

        dirs = enumerate_changelog_dirs(str(ws / "packages" / "alpha"), str(ws))
        assert str(proj_changes) in dirs
        assert str(rel_changes) in dirs
        assert len(dirs) == 2


class TestValidateAllHashesResolve:
    """validate_all_hashes_resolve reports hashes that git cannot resolve."""

    def test_reports_unresolvable_hashes(self, tmp_path, monkeypatch):
        from rlsbl.changelog.files import validate_all_hashes_resolve

        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q", "-b", "main")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")
        _make_commit(repo, "a.txt", "c1")
        good = _git_head(repo)

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        bad = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        (changes / "unreleased.jsonl").write_text(
            json.dumps({"commits": [good], "user_facing": False}) + "\n"
            + json.dumps({"commits": [bad], "user_facing": False}) + "\n"
        )

        monkeypatch.chdir(repo)
        failures = validate_all_hashes_resolve([str(changes)], repo_root=str(repo))
        assert failures == {str(changes / "unreleased.jsonl"): [bad]}

    def test_all_resolve(self, tmp_path, monkeypatch):
        from rlsbl.changelog.files import validate_all_hashes_resolve

        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q", "-b", "main")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")
        _make_commit(repo, "a.txt", "c1")
        good = _git_head(repo)

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "1.0.0.jsonl").write_text(
            json.dumps({"commits": [good], "user_facing": True,
                        "description": "d", "type": "fix"}) + "\n"
        )
        os.chmod(str(changes / "1.0.0.jsonl"), 0o444)

        monkeypatch.chdir(repo)
        assert validate_all_hashes_resolve([str(changes)], repo_root=str(repo)) == {}

    def test_resolves_against_explicit_repo_root_not_cwd(self, tmp_path, monkeypatch):
        """Hash resolution must run in the EXPLICIT repo_root, not whatever
        the process CWD happens to be -- the planned validation-only mode
        may run from outside the target repo."""
        from rlsbl.changelog.files import validate_all_hashes_resolve

        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(repo, "init", "-q", "-b", "main")
        _run_git(repo, "config", "user.email", "test@test.local")
        _run_git(repo, "config", "user.name", "Test")
        _make_commit(repo, "a.txt", "c1")
        good = _git_head(repo)

        other = tmp_path / "other"
        other.mkdir()
        _run_git(other, "init", "-q", "-b", "main")
        _run_git(other, "config", "user.email", "test@test.local")
        _run_git(other, "config", "user.name", "Test")
        _make_commit(other, "b.txt", "c1")

        changes = repo / ".rlsbl" / "changes"
        changes.mkdir(parents=True)
        (changes / "unreleased.jsonl").write_text(
            json.dumps({"commits": [good], "user_facing": False}) + "\n"
        )

        # CWD is a DIFFERENT repo where the hash does not exist: passing
        # the correct repo_root must still resolve everything...
        monkeypatch.chdir(other)
        assert validate_all_hashes_resolve([str(changes)], repo_root=str(repo)) == {}
        # ...and pointing repo_root at the wrong repo must report it.
        failures = validate_all_hashes_resolve([str(changes)], repo_root=str(other))
        assert failures == {str(changes / "unreleased.jsonl"): [good]}


# ---------------------------------------------------------------------------
# changelog_remap_globs / can_remap_hash (safegit --remap-shas-in support)
# ---------------------------------------------------------------------------


def _go_path_match(pattern, path):
    """Faithful model of Go path.Match for the glob shapes rlsbl emits.

    Go's path.Match matches per-segment: ``*`` never crosses ``/`` and the
    pattern must consume the whole path (same number of segments). safegit's
    matchScope tries the full path first, then the basename.
    """
    import fnmatch

    def segments_match(pat, p):
        pat_segs = pat.split("/")
        p_segs = p.split("/")
        if len(pat_segs) != len(p_segs):
            return False
        return all(fnmatch.fnmatchcase(s, ps) for ps, s in zip(pat_segs, p_segs))

    if segments_match(pattern, path):
        return True
    return segments_match(pattern, path.rsplit("/", 1)[-1])


class TestChangelogRemapGlobs:
    """changelog_remap_globs builds the safegit --remap-shas-in glob list.

    It must be derived from the same enumeration as validation
    (enumerate_changelog_dirs) so remap coverage and validation coverage
    can never diverge, and every glob must be an exact per-directory
    pattern (Go path.Match: ``*`` never crosses ``/``).
    """

    def test_standalone(self, tmp_path):
        from rlsbl.changelog.files import changelog_remap_globs

        # Emitted even when the dir does not exist yet: HISTORICAL commits
        # may still contain .rlsbl/changes/ files that need remapping.
        assert changelog_remap_globs(str(tmp_path), None) == [
            ".rlsbl/changes/*.jsonl"
        ]

    def test_monorepo_per_project_and_releasable_wildcard(self, tmp_path):
        from rlsbl.changelog.files import changelog_remap_globs

        ws = tmp_path / "ws"
        proj_changes = ws / "packages" / "alpha" / ".rlsbl" / "changes"
        proj_changes.mkdir(parents=True)
        rel_changes = ws / ".rlsbl-monorepo" / "releasables" / "core" / "changes"
        rel_changes.mkdir(parents=True)
        (ws / ".rlsbl-monorepo" / "workspace.toml").write_text(
            'projects = [{ path = "packages/alpha", name = "alpha" }]\n'
        )

        globs = changelog_remap_globs(str(ws / "packages" / "alpha"), str(ws))

        # Releasable dirs are covered by ONE wildcard segment glob so that
        # releasables deleted from the working tree but present in history
        # are still remapped. Per-project dirs get exact per-directory globs.
        assert ".rlsbl-monorepo/releasables/*/changes/*.jsonl" in globs
        assert "packages/alpha/.rlsbl/changes/*.jsonl" in globs
        # No literal releasable glob: the wildcard already covers it, and a
        # duplicate would just repeat work in safegit's walk.
        assert ".rlsbl-monorepo/releasables/core/changes/*.jsonl" not in globs

    def test_globs_cover_every_enumerated_dir(self, tmp_path):
        """Invariant: every dir validation checks is matched by some glob
        under Go path.Match semantics (as used by safegit's matchScope)."""
        from rlsbl.changelog.files import (
            changelog_remap_globs,
            enumerate_changelog_dirs,
        )

        ws = tmp_path / "ws"
        for d in [
            ws / "packages" / "alpha" / ".rlsbl" / "changes",
            ws / "libs" / "deep" / "beta" / ".rlsbl" / "changes",
            ws / ".rlsbl-monorepo" / "releasables" / "core" / "changes",
            ws / ".rlsbl-monorepo" / "releasables" / "extra" / "changes",
        ]:
            d.mkdir(parents=True)
        (ws / ".rlsbl-monorepo" / "workspace.toml").write_text(
            "projects = ["
            '{ path = "packages/alpha", name = "alpha" },'
            '{ path = "libs/deep/beta", name = "beta" },'
            "]\n"
        )

        dirs = enumerate_changelog_dirs(str(ws / "packages" / "alpha"), str(ws))
        globs = changelog_remap_globs(str(ws / "packages" / "alpha"), str(ws))

        assert len(dirs) == 4
        for d in dirs:
            rel = os.path.relpath(d, str(ws)).replace(os.sep, "/")
            sample = rel + "/unreleased.jsonl"
            assert any(_go_path_match(g, sample) for g in globs), (
                f"no glob covers {sample}: {globs}"
            )

    def test_globs_do_not_match_scrub_archives_or_caches(self, tmp_path):
        """DECISION: committed scrub archives (.rlsbl/scrubs/*.json and the
        releasable equivalent) are records of what WAS -- their recorded SHAs
        intentionally reference pre-rewrite objects and already dangle the
        moment the original scrub prunes them. Remapping them on a later
        scrub would falsify the record, so they are EXCLUDED from the remap
        globs; validation likewise never reads them. The .validated caches
        are deleted by the scrub flow and must not be remapped either."""
        from rlsbl.changelog.files import changelog_remap_globs

        globs = changelog_remap_globs(str(tmp_path), None)
        for path in [
            ".rlsbl/scrubs/scrub-deadbeef1234.json",
            ".rlsbl/changes/.validated",
        ]:
            assert not any(_go_path_match(g, path) for g in globs), (
                f"{path} must not be covered by remap globs: {globs}"
            )


class TestCanRemapHash:
    """can_remap_hash reports whether the recovery remap could fix a hash."""

    def test_exact_match(self):
        from rlsbl.changelog.files import can_remap_hash

        assert can_remap_hash("a" * 40, {"a" * 40: "b" * 40})

    def test_unique_prefix(self):
        from rlsbl.changelog.files import can_remap_hash

        assert can_remap_hash("abcd12", {"abcd12" + "a" * 34: "b" * 40})

    def test_ambiguous_prefix_not_fixable(self):
        from rlsbl.changelog.files import can_remap_hash

        sha_map = {"abcd12" + "a" * 34: "1" * 40, "abcd12" + "b" * 34: "2" * 40}
        assert not can_remap_hash("abcd12", sha_map)

    def test_unknown_hash_not_fixable(self):
        from rlsbl.changelog.files import can_remap_hash

        assert not can_remap_hash("f" * 40, {"a" * 40: "b" * 40})
