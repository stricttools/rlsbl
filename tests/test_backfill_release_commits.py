"""Tests for the backfill_release_anchors script.

The script repairs a repository's release archives: it release commits versions to the
commit they shipped from, stamps the strictspec gate onto archives written
before the gate existed, materializes archives for versions that never got one,
and records a permanent marker on a version whose commit cannot be recovered at
all. Each of the four buckets it sorts into has a case here, plus the
idempotency property the whole pass rests on.
"""

import importlib.util
import io
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import make_workspace
from githarness import commit_file, git, init_repo

# Load the script by path (it is a script, not a package module) -- the same
# convention tests/test_backfill.py uses for the changelog backfill script.
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_release_anchors.py"
_spec = importlib.util.spec_from_file_location("backfill_release_anchors", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
# Registered before execution: the module defines dataclasses, and @dataclass
# resolves annotations through sys.modules[cls.__module__].
sys.modules["backfill_release_anchors"] = backfill
_spec.loader.exec_module(backfill)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def make_standalone(tmp_path, name="proj"):
    """A git repo with an empty standalone .rlsbl release state."""
    repo = tmp_path / name
    repo.mkdir()
    init_repo(repo)
    (repo / ".rlsbl" / "releases").mkdir(parents=True, exist_ok=True)
    (repo / ".rlsbl" / "changes").mkdir(parents=True)
    commit_file(repo, "README.md", "hello\n", "initial")
    return repo


def write_changelog_jsonl(changes_dir, version, sha="0" * 40):
    """A finalized (read-only) changelog file for a released version."""
    path = Path(changes_dir) / f"{version}.jsonl"
    path.write_text(
        '{"format_version":1,"commits":["%s"],"user_facing":false}\n' % sha,
        encoding="utf-8",
    )
    os.chmod(path, 0o444)
    return path


UNSTAMPED_ARCHIVE = """\
# Version bump type: patch, minor, major, infra, or prerelease
bump = "minor"
# Short description of this release (required)
description = "The original description, preserved verbatim."
context = \"\"\"
Multi-line context that a tomlkit round-trip must not mangle.
Second line.
\"\"\"
include = ["pypi"]
exclude = []
"""


def write_archive(releases_dir, version, content=UNSTAMPED_ARCHIVE):
    """An archived release file, locked read-only like every real one."""
    path = Path(releases_dir) / f"v{version}.toml"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o444)
    return path


def run_backfill(repo, *, dry_run=False):
    """Run the pass against *repo*, returning ``(exit_code, output)``."""
    out = io.StringIO()
    code = backfill.run(
        str(repo), dry_run=dry_run, use_gh=False, auto_commit=False, out=out
    )
    return code, out.getvalue()


def read_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def is_locked(path):
    mode = os.stat(path).st_mode
    return not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


# ---------------------------------------------------------------------------
# (a) release_commitable from a tag
# ---------------------------------------------------------------------------


class TestMarkerLessArchive:
    """An archive predating both the gate and the release commit gets both."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        write_archive(repo / ".rlsbl" / "releases", "0.1.0")
        return repo

    def test_format_version_is_stamped(self, repo):
        path = repo / ".rlsbl" / "releases" / "v0.1.0.toml"
        assert "format_version" not in read_toml(path)
        run_backfill(repo)
        assert read_toml(path)["format_version"] == 1

    def test_release_commit_is_the_tags_commit_and_tree(self, repo):
        path = repo / ".rlsbl" / "releases" / "v0.1.0.toml"
        run_backfill(repo)
        data = read_toml(path)
        expected_sha = git(repo, "rev-parse", "v0.1.0^{commit}")
        expected_tree = git(repo, "rev-parse", "v0.1.0^{tree}")
        assert data["candidate_sha"] == expected_sha
        assert data["tree_hashes"] == {".": expected_tree}

    def test_existing_content_survives(self, repo):
        path = repo / ".rlsbl" / "releases" / "v0.1.0.toml"
        run_backfill(repo)
        data = read_toml(path)
        assert data["description"] == "The original description, preserved verbatim."
        assert "Second line." in data["context"]
        assert data["bump"] == "minor"
        text = path.read_text()
        assert "# Version bump type:" in text

    def test_archive_stays_locked(self, repo):
        path = repo / ".rlsbl" / "releases" / "v0.1.0.toml"
        run_backfill(repo)
        assert is_locked(path)

    def test_result_is_a_valid_release_document(self, repo):
        from rlsbl.release_file import read_release_file

        run_backfill(repo)
        cfg = read_release_file(str(repo / ".rlsbl" / "releases" / "v0.1.0.toml"))
        assert cfg.candidate_sha
        assert cfg.tree_hashes == {".": git(repo, "rev-parse", "v0.1.0^{tree}")}


# ---------------------------------------------------------------------------
# Materializing a missing archive
# ---------------------------------------------------------------------------


class TestMissingArchive:
    """A released version with no archive gets one, locked and recorded."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        commit_file(repo, "b.txt", "b\n", "v0.2.0")
        git(repo, "tag", "v0.2.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.2.0")
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## 0.2.0\n\n"
            "A recovered description paragraph.\n\n"
            "### Features\n\n- something\n\n"
            "## 0.1.0\n\n"
            "### Features\n\n- initial\n",
            encoding="utf-8",
        )
        return repo

    def test_archive_is_created_and_locked(self, repo):
        run_backfill(repo)
        path = repo / ".rlsbl" / "releases" / "v0.2.0.toml"
        assert path.is_file()
        assert is_locked(path)

    def test_description_is_recovered_from_changelog(self, repo):
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.2.0.toml")
        assert data["description"] == "A recovered description paragraph."

    def test_description_without_a_source_names_the_obligation(self, repo):
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["description"] == backfill.PLACEHOLDER_DESCRIPTION
        assert "RECOVERY OBLIGATION" in data["description"]

    def test_bump_is_derived_from_the_predecessor(self, repo):
        run_backfill(repo)
        assert read_toml(repo / ".rlsbl" / "releases" / "v0.2.0.toml")["bump"] == "minor"
        # No predecessor: measured against 0.0.0, so 0.1.0 is a minor.
        assert read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")["bump"] == "minor"

    def test_materialized_archive_is_recorded(self, repo):
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.2.0.toml")
        assert data["candidate_sha"] == git(repo, "rev-parse", "v0.2.0^{commit}")
        assert data["tree_hashes"] == {".": git(repo, "rev-parse", "v0.2.0^{tree}")}
        assert "unrecoverable" not in data

    def test_header_says_it_was_materialized(self, repo):
        run_backfill(repo)
        text = (repo / ".rlsbl" / "releases" / "v0.2.0.toml").read_text()
        assert "Materialized by scripts/backfill_release_anchors.py" in text


def test_derive_bump_arithmetic():
    assert backfill.derive_bump("1.0.0", "0.9.3") == "major"
    assert backfill.derive_bump("0.4.0", "0.3.9") == "minor"
    assert backfill.derive_bump("0.3.1", "0.3.0") == "patch"
    assert backfill.derive_bump("0.1.0", None) == "minor"
    assert backfill.derive_bump("0.0.1", None) == "patch"


# ---------------------------------------------------------------------------
# (b) tagless versions
# ---------------------------------------------------------------------------


class TestTaglessVersion:
    """No tag: recovery from the version-bump commit, or a permanent marker."""

    def test_release_commits_from_the_version_bump_commit(self, tmp_path):
        repo = make_standalone(tmp_path)
        sha = commit_file(repo, "a.txt", "a\n", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")

        code, output = run_backfill(repo)

        assert code == 0
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["candidate_sha"] == sha
        assert "unrecoverable" not in data
        assert "recorded from the version-bump commit" in output

    def test_marks_unrecoverable_when_nothing_is_recoverable(self, tmp_path):
        repo = make_standalone(tmp_path)
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")

        code, output = run_backfill(repo)

        assert code == 0
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["unrecoverable"] is True
        assert "candidate_sha" not in data
        assert "tree_hashes" not in data
        assert "unrecoverable" in output

    def test_marks_an_existing_archive_unrecoverable_in_place(self, tmp_path):
        repo = make_standalone(tmp_path)
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        path = write_archive(repo / ".rlsbl" / "releases", "0.1.0")

        run_backfill(repo)

        data = read_toml(path)
        assert data["unrecoverable"] is True
        assert data["description"] == "The original description, preserved verbatim."
        assert data["format_version"] == 1
        assert is_locked(path)

    def test_a_tagless_version_is_never_silently_skipped(self, tmp_path):
        repo = make_standalone(tmp_path)
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")

        _code, output = run_backfill(repo)

        assert "(b) TAGLESS versions: 1 version(s), 1 needing work" in output


# ---------------------------------------------------------------------------
# (c) foreign tags
# ---------------------------------------------------------------------------


class TestForeignTag:
    """A tag that parses but matches nothing is operator input, not a guess."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        git(repo, "tag", "core@v9.9.9")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        return repo

    def test_is_reported_with_the_probed_spellings(self, repo):
        code, output = run_backfill(repo)
        assert code == 1
        assert "core@v9.9.9" in output
        assert "monorepo scheme" in output
        assert "standalone: v9.9.9" in output

    def test_nothing_is_written_for_it(self, repo):
        run_backfill(repo)
        releases = repo / ".rlsbl" / "releases"
        assert not (releases / "v9.9.9.toml").exists()
        assert sorted(p.name for p in releases.iterdir()) == ["v0.1.0.toml"]

    def test_the_rest_of_the_pass_still_runs(self, repo):
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["candidate_sha"] == git(repo, "rev-parse", "v0.1.0^{commit}")


# ---------------------------------------------------------------------------
# (d) unrecognizable tags
# ---------------------------------------------------------------------------


def test_unrecognizable_tags_are_listed_and_left_alone(tmp_path):
    repo = make_standalone(tmp_path)
    commit_file(repo, "a.txt", "a\n", "v0.1.0")
    git(repo, "tag", "v0.1.0")
    git(repo, "tag", "milestone-3")
    write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")

    code, output = run_backfill(repo)

    assert code == 0  # non-fatal
    assert "(d) unrecognizable tags: 1" in output
    assert "milestone-3  (left untouched)" in output
    assert git(repo, "rev-parse", "milestone-3")


# ---------------------------------------------------------------------------
# A recognized non-standalone tag scheme
# ---------------------------------------------------------------------------


def test_releasable_tag_scheme_records_normally(tmp_path, monkeypatch):
    """A ``{name}@v{version}`` tag release commits its releasable's version."""
    repo = tmp_path / "ws"
    repo.mkdir()
    init_repo(repo)
    (repo / "pkgs" / "core").mkdir(parents=True)
    (repo / ".rlsbl-monorepo" / "releasables" / "core" / "changes").mkdir(parents=True)
    (repo / ".rlsbl-monorepo" / "releasables" / "core" / "releases").mkdir(parents=True, exist_ok=True)
    make_workspace(
        repo,
        [{"path": "pkgs/core", "name": "core", "releasable": "core"}],
        releasables=[{"name": "core", "tag_format": "{name}@v{version}"}],
    )
    commit_file(repo, "pkgs/core/thing.py", "x = 1\n", "core@v0.1.0")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "workspace")
    git(repo, "tag", "core@v0.1.0")
    write_changelog_jsonl(
        repo / ".rlsbl-monorepo" / "releasables" / "core" / "changes", "0.1.0"
    )

    code, output = run_backfill(repo)

    assert code == 0, output
    path = repo / ".rlsbl-monorepo" / "releasables" / "core" / "releases" / "v0.1.0.toml"
    data = read_toml(path)
    assert data["candidate_sha"] == git(repo, "rev-parse", "core@v0.1.0^{commit}")
    # The releasable's release commit names its member directory, not the whole repo.
    assert list(data["tree_hashes"]) == ["pkgs/core"]
    assert data["tree_hashes"]["pkgs/core"] == git(
        repo, "rev-parse", "core@v0.1.0:pkgs/core"
    )
    assert "core@v9" not in output  # the tag was claimed, not reported foreign


def test_released_path_absent_at_the_commit_falls_back_to_the_root_tree(tmp_path):
    """A member directory that did not exist yet records the root tree honestly."""
    repo = tmp_path / "ws"
    repo.mkdir()
    init_repo(repo)
    (repo / "pkgs" / "core").mkdir(parents=True)
    (repo / ".rlsbl-monorepo" / "releasables" / "core" / "changes").mkdir(parents=True)
    make_workspace(
        repo,
        [{"path": "pkgs/core", "name": "core", "releasable": "core"}],
        releasables=[{"name": "core", "tag_format": "{name}@v{version}"}],
    )
    # Tag a commit from before pkgs/core existed.
    commit_file(repo, "old.txt", "old\n", "core@v0.1.0")
    git(repo, "tag", "core@v0.1.0")
    commit_file(repo, "pkgs/core/thing.py", "x = 1\n", "later")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "workspace")
    write_changelog_jsonl(
        repo / ".rlsbl-monorepo" / "releasables" / "core" / "changes", "0.1.0"
    )

    _code, output = run_backfill(repo)

    path = repo / ".rlsbl-monorepo" / "releasables" / "core" / "releases" / "v0.1.0.toml"
    data = read_toml(path)
    assert data["tree_hashes"] == {".": git(repo, "rev-parse", "core@v0.1.0^{tree}")}
    assert "did not exist at" in output


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        commit_file(repo, "b.txt", "b\n", "v0.2.0")
        git(repo, "tag", "v0.2.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.2.0")
        write_archive(repo / ".rlsbl" / "releases", "0.1.0")
        # A version with no tag at all, so the unrecoverable path is exercised too.
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.3.0")
        return repo

    def test_second_run_proposes_nothing(self, repo):
        run_backfill(repo)
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.changed_versions == []

    def test_second_run_writes_nothing(self, repo):
        run_backfill(repo)
        releases = repo / ".rlsbl" / "releases"
        before = {p.name: p.read_bytes() for p in releases.iterdir()}
        code, output = run_backfill(repo)
        after = {p.name: p.read_bytes() for p in releases.iterdir()}
        assert after == before
        assert code == 0
        assert "Nothing to do" in output

    def test_dry_run_writes_nothing_at_all(self, tmp_path):
        repo = make_standalone(tmp_path)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")

        code, output = run_backfill(repo, dry_run=True)

        assert code == 0
        assert "--dry-run: nothing written." in output
        assert not (repo / ".rlsbl" / "releases" / "v0.1.0.toml").exists()


class TestTotalLine:
    """The closing TOTAL line counts ATTRIBUTES, not parts of a partition.

    ``materialized``, ``format-version stamped`` and ``unrecoverable`` are
    independent properties of the archives the pass will write: one archive can
    carry two of them (a materialized archive for a version with no recoverable
    commit is materialized AND unrecoverable), and an existing, already-stamped
    archive that only gains its release commit carries none. Their sum is therefore
    neither the total nor bounded by it, and a rendering that reads as a
    breakdown of the total is a lie about arithmetic the reader will do.
    """

    STAMPED_ARCHIVE = 'format_version = 1\n' + UNSTAMPED_ARCHIVE

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        changes = repo / ".rlsbl" / "changes"
        releases = repo / ".rlsbl" / "releases"
        # 0.1.0: an archive that already carries the gate and only needs its
        # release commit -- none of the three attributes.
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(changes, "0.1.0")
        write_archive(releases, "0.1.0", self.STAMPED_ARCHIVE)
        # 0.2.0: an archive predating the gate -- stamped, not materialized.
        commit_file(repo, "b.txt", "b\n", "v0.2.0")
        git(repo, "tag", "v0.2.0")
        write_changelog_jsonl(changes, "0.2.0")
        write_archive(releases, "0.2.0")
        # 0.3.0: no archive at all -- materialized, not stamped.
        commit_file(repo, "c.txt", "c\n", "v0.3.0")
        git(repo, "tag", "v0.3.0")
        write_changelog_jsonl(changes, "0.3.0")
        return repo

    def test_the_counts_do_not_sum_to_the_total(self, repo):
        """The fixture's own arithmetic, asserted so the rendering is tested
        against a case where a partition reading would be wrong."""
        plan = backfill.build_plan(str(repo), use_gh=False)
        changed = plan.changed_versions
        assert len(changed) == 3
        assert sum(1 for v in changed if v.materialize) == 1
        assert sum(1 for v in changed if v.stamp_format_version) == 1
        assert sum(1 for v in changed if v.unrecoverable) == 0

    def test_each_count_is_named_as_an_attribute(self, repo):
        _code, output = run_backfill(repo, dry_run=True)
        assert "TOTAL: 3 archive(s) to write" in output
        assert "materialized: 1" in output
        assert "format-version stamped: 1" in output
        assert "unrecoverable: 0" in output

    def test_the_reader_is_told_they_are_independent(self, repo):
        _code, output = run_backfill(repo, dry_run=True)
        total_line = next(
            line for line in output.splitlines() if line.startswith("TOTAL:")
        )
        rest = output.split(total_line, 1)[1]
        assert "independent" in rest

    def test_the_old_partition_rendering_is_gone(self, repo):
        """``(a materialized, b stamped, c unrecoverable)`` in parentheses right
        after the count read as a breakdown of it."""
        _code, output = run_backfill(repo, dry_run=True)
        assert "(1 materialized," not in output


def test_commit_message_names_the_repo_relative_scope(tmp_path):
    repo = make_standalone(tmp_path)
    plan = backfill.Plan(repo=str(repo), scopes=[])
    message = backfill.commit_message(
        plan, [".rlsbl/releases/v0.1.0.toml", ".rlsbl/releases/v0.2.0.toml"]
    )
    assert ".rlsbl/releases" in message
    assert "2 archive(s)" in message


class TestArchiveNameGrammar:
    """The script and the release record read the same directory, so they must agree
    on which files in it ARE archives, and in what order they stand.

    They did not: the script matched an arbitrary semver prerelease suffix and
    sorted prerelease identifiers as strings, while the release record recognized only
    rlsbl's own preid vocabulary and ordered counters numerically. Both are
    now the one recognizer and the one ordering that live in
    ``rlsbl.release_file``.
    """

    def _scope(self, tmp_path):
        releases = tmp_path / ".rlsbl" / "releases"
        releases.mkdir(parents=True)
        return backfill.Scope(
            label="standalone",
            releases_dir=str(releases),
            changes_dir=str(tmp_path / ".rlsbl" / "changes"),
            changelog_md=str(tmp_path / "CHANGELOG.md"),
            released_paths=["."],
            tag_formats=["v{version}"],
        ), releases

    def test_the_same_files_are_archives_for_both(self, tmp_path):
        from rlsbl.release_file import list_archived_versions

        scope, releases = self._scope(tmp_path)
        for name in ("v1.2.3.toml", "v1.2.4-rc.1.toml", "v1.2.5-dev.1.toml",
                     "notes.toml", "unreleased.toml"):
            (releases / name).write_text("bump = \"patch\"\n", encoding="utf-8")

        assert set(backfill.archived_versions(scope)) == set(
            list_archived_versions(str(releases))
        )
        assert "1.2.5-dev.1" not in backfill.archived_versions(scope)

    def test_prerelease_counters_order_numerically(self):
        from rlsbl.release_file import archive_sort_key

        versions = ["1.0.0-alpha.10", "1.0.0-alpha.2", "1.0.0", "1.0.0-rc.1"]
        assert sorted(versions, key=backfill.archive_sort_key) == [
            "1.0.0-alpha.2", "1.0.0-alpha.10", "1.0.0-rc.1", "1.0.0",
        ]
        assert backfill.archive_sort_key is archive_sort_key


# ---------------------------------------------------------------------------
# A workspace that exists but does not load
# ---------------------------------------------------------------------------


class TestBrokenWorkspaceIsRefused:
    """A refused workspace must never be mistaken for a plain repository.

    ``discover_scopes`` used to wrap the releasable load in a bare
    ``except Exception`` that fell back to "implicit mode: no [[releasables]]
    section". Implicit mode no longer exists -- ``load_workspace`` refuses such
    a workspace itself -- so every failure that swallow caught was a broken
    workspace being silently downgraded to a repository with no releasables,
    whose archives the pass would then leave unrepaired while reporting
    success.
    """

    def test_a_malformed_releasables_section_is_a_hard_error(self, tmp_path):
        repo = tmp_path / "ws"
        repo.mkdir()
        init_repo(repo)
        (repo / ".rlsbl-monorepo").mkdir()
        (repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            'releasables = "not-a-list"\n\n'
            '[[projects]]\npath = "."\nname = "root"\nreleasable = false\n',
            encoding="utf-8",
        )

        with pytest.raises(backfill.BackfillError) as exc:
            backfill.discover_scopes(str(repo))

        assert "workspace.toml" in str(exc.value)
        # The loader's own message, not a message this script invented.
        assert "list of tables" in str(exc.value)

    def test_unparseable_toml_is_a_hard_error(self, tmp_path):
        repo = tmp_path / "ws"
        repo.mkdir()
        init_repo(repo)
        (repo / ".rlsbl-monorepo").mkdir()
        (repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            "[[projects]\npath =\n", encoding="utf-8"
        )

        with pytest.raises(backfill.BackfillError) as exc:
            backfill.discover_scopes(str(repo))

        assert "workspace.toml" in str(exc.value)

    def test_a_member_standing_outside_every_releasable_still_gets_a_scope(
        self, tmp_path
    ):
        """The non-releasable path is preserved: only the swallow is gone."""
        repo = tmp_path / "ws"
        repo.mkdir()
        init_repo(repo)
        (repo / "pkgs" / "tool").mkdir(parents=True)
        (repo / "pkgs" / "tool" / ".rlsbl" / "releases").mkdir(parents=True)
        make_workspace(
            repo,
            [
                {"path": ".", "name": "root", "dev_only": True, "releasable": False},
                {"path": "pkgs/tool", "name": "tool", "releasable": False},
            ],
            releasables=[{"name": "core"}],
        )

        labels = [s.label for s in backfill.discover_scopes(str(repo))]

        assert "tool" in labels
        assert "core" in labels

    def test_no_workspace_file_is_still_a_standalone_repository(self, tmp_path):
        repo = make_standalone(tmp_path)
        scopes = backfill.discover_scopes(str(repo))
        assert [s.label for s in scopes] == ["standalone"]

    def test_main_reports_the_refusal_and_exits_nonzero(self, tmp_path, capsys):
        repo = tmp_path / "ws"
        repo.mkdir()
        init_repo(repo)
        (repo / ".rlsbl-monorepo").mkdir()
        (repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            'releasables = "not-a-list"\n\n'
            '[[projects]]\npath = "."\nname = "root"\nreleasable = false\n',
            encoding="utf-8",
        )

        code = backfill.main(["--repo", str(repo), "--dry-run", "--no-gh"])

        assert code == 2
        assert "workspace.toml" in capsys.readouterr().err
