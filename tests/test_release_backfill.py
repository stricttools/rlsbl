"""The release-archive backfill engine, behind `rlsbl release backfill`.

The pass repairs a repository's release archives: it records each version's
release commit from the commit it shipped from, completes an archive missing
required fields (the strictspec gate included), materializes archives for
released versions that never got one, adopts a version tag no store records,
and marks a version whose commit cannot be recovered at all. Every verdict has
a case here, plus the idempotency property the whole pass rests on and the
refusals that stop an apply.
"""

import io
import os
import stat
import tomllib
from pathlib import Path

import pytest

from conftest import make_workspace
from githarness import commit_file, git, init_repo

from rlsbl import release_backfill as backfill
from rlsbl.release_backfill import BackfillError
from rlsbl.release_file import write_archived_release_file


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


def run_backfill(repo, *, dry_run=False, gh=None, overrides=None):
    """Run the pass against *repo*, returning ``(exit_code, output)``.

    ``use_gh`` follows ``gh``: the GitHub Release source is only exercised when
    a test supplies a reader for it, so no test can reach a real network.
    """
    out = io.StringIO()
    code = backfill.run(
        str(repo), dry_run=dry_run, use_gh=gh is not None, gh=gh,
        auto_commit=False, out=out, overrides=overrides,
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


# A pre-gate archive that is ALSO missing a required field. The gate is not the
# only thing such a file lacks, and stamping only the gate leaves a document the
# strict reader still refuses -- so the pass completes every required field.
INCOMPLETE_ARCHIVE = """\
# Version bump type: patch, minor, major, infra, or prerelease
bump = "minor"
include = ["pypi"]
exclude = []
"""


class TestIncompletePreGateArchive:
    """An archive missing required fields is COMPLETED, not merely stamped."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path, name="incomplete")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.0\n\nThe recovered summary.\n\n"
            "### Features\n\n- a thing\n",
            encoding="utf-8",
        )
        write_archive(repo / ".rlsbl" / "releases", "0.1.0", INCOMPLETE_ARCHIVE)
        return repo

    def test_every_required_field_is_present_afterwards(self, repo):
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        for field_name in ("format_version", "bump", "include", "exclude",
                           "description"):
            assert field_name in data, field_name

    def test_the_result_is_readable_by_the_strict_reader(self, repo):
        from rlsbl.release_file import read_release_file

        run_backfill(repo)
        cfg = read_release_file(str(repo / ".rlsbl" / "releases" / "v0.1.0.toml"))
        assert cfg.description == "The recovered summary."
        assert cfg.candidate_sha == git(repo, "rev-parse", "v0.1.0^{commit}")

    def test_the_recovered_field_names_its_source(self, repo):
        run_backfill(repo)
        text = (repo / ".rlsbl" / "releases" / "v0.1.0.toml").read_text()
        assert "description reconstructed by" in text
        assert "changelog-md" in text

    def test_a_second_run_plans_nothing(self, repo):
        run_backfill(repo)
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.changed_versions == []


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

    def test_commit_subjects_are_the_source_below_the_changelog(self, repo):
        """0.1.0 has no CHANGELOG.md paragraph, so its commits describe it."""
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["description"].startswith(
            "Reconstructed from this version's commit subjects:"
        )
        assert "v0.1.0" in data["description"]
        text = (repo / ".rlsbl" / "releases" / "v0.1.0.toml").read_text()
        assert "commit-subjects:" in text

    def test_a_version_with_no_source_at_all_names_the_obligation(self, tmp_path):
        """The last link of the chain: an unrecoverable version has no commit."""
        repo = make_standalone(tmp_path, name="sourceless")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "9.9.9")

        run_backfill(repo)

        data = read_toml(repo / ".rlsbl" / "releases" / "v9.9.9.toml")
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

    def test_header_says_it_was_materialized_and_names_the_sources(self, repo):
        run_backfill(repo)
        text = (repo / ".rlsbl" / "releases" / "v0.2.0.toml").read_text()
        assert "Materialized by `rlsbl release backfill`" in text
        # The header enumerates the whole recovery chain, in order.
        for source in ("--overrides", "GitHub Release", "CHANGELOG.md",
                       "commit subjects", "placeholder"):
            assert source in text, source


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

        assert "standalone 0.1.0" in output
        assert "no tag (probed v0.1.0)" in output


# ---------------------------------------------------------------------------
# A version whose fate is already settled as NEVER RELEASED
# ---------------------------------------------------------------------------


class TestNeverReleasedArchive:
    """A phantom version's archive is settled, and the pass leaves it alone.

    ``never_released = true`` says the version NUMBER exists but no release
    does. Such a version has no tag and no version-bump commit -- by
    construction, that is what "never released" means -- so a pass that reads
    only the recorded/unrecoverable pair sees an unsettled archive, plans the
    unrecoverable marker for it, and turns a correct one-fate archive into a
    two-fate document.
    """

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        # The phantom: finalized changelog files exist (entries were written
        # and locked before the release was abandoned), the archive records the
        # never-released fate, and no tag was ever created.
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.9.0")
        write_archived_release_file(
            str(repo / ".rlsbl" / "releases"), "0.9.0",
            bump="minor", include=["pypi"], description="claimed, never shipped",
            candidate_sha=None, tree_hashes=None, never_released=True,
        )
        return repo

    def _phantom(self, plan):
        return next(v for v in plan.versions if v.version == "0.9.0")

    def test_the_pass_proposes_no_change_for_it(self, repo):
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert self._phantom(plan).changed is False
        assert "0.9.0" not in [v.version for v in plan.changed_versions]

    def test_the_plan_says_the_fate_is_already_settled(self, repo):
        plan = backfill.build_plan(str(repo), use_gh=False)
        notes = " ".join(self._phantom(plan).notes)
        assert "never_released" in notes
        assert "settled" in notes
        assert self._phantom(plan).actions == []

    def test_a_run_leaves_the_archive_byte_identical(self, repo):
        archive = repo / ".rlsbl" / "releases" / "v0.9.0.toml"
        before = archive.read_bytes()

        code, _output = run_backfill(repo)

        assert code == 0
        assert archive.read_bytes() == before
        # And the second run, with the released version's archive now written
        # too, has nothing left at all.
        _code, output = run_backfill(repo)
        assert "Nothing to do" in output

    def test_the_archive_stays_a_readable_one_fate_document(self, repo):
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.9.0.toml")
        assert data["never_released"] is True
        assert "unrecoverable" not in data
        assert "candidate_sha" not in data

    def test_a_second_run_plans_nothing(self, repo):
        run_backfill(repo)
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.changed_versions == []

    def test_a_tagless_version_is_told_how_to_declare_the_fate(self, tmp_path):
        """The note on the version-bump-commit path names the one procedure.

        The pass cannot tell a never-released version from a released one whose
        tag is gone: both have no tag, and both may have a version-bump commit.
        So when it is about to record a commit from that commit, it says what
        an operator must do FIRST if the version never actually shipped --
        writing the archive with ``never_released = true``, which is the whole
        declaration.
        """
        repo = make_standalone(tmp_path, name="tagless")
        commit_file(repo, "a.txt", "a\n", "v0.3.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.3.0")

        plan = backfill.build_plan(str(repo), use_gh=False)
        notes = " ".join(
            next(v for v in plan.versions if v.version == "0.3.0").notes
        )
        assert "never_released = true" in notes
        assert "declare the fate first" in notes
        assert "no flag and no input file" in notes


# ---------------------------------------------------------------------------
# Unexplained tags: listed first, and a hard refusal
# ---------------------------------------------------------------------------


class TestUnexplainedTags:
    """A tag nothing accounts for refuses the whole apply -- all or nothing.

    A version-shaped tag under a spelling no scope produces, and a tag that
    parses as no version at all, are the same finding: this repository cannot
    say what the tag is. Neither is written around.
    """

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        git(repo, "tag", "core@v9.9.9")
        git(repo, "tag", "milestone-3")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        return repo

    def test_both_kinds_are_listed(self, repo):
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert sorted(e.tag for e in plan.unexplained) == [
            "core@v9.9.9", "milestone-3",
        ]

    def test_the_preview_lists_them_before_any_version(self, repo):
        preview = backfill.build_preview(
            backfill.build_plan(str(repo), use_gh=False)
        )
        states = list(preview.states)
        first_version = next(
            i for i, s in enumerate(states) if s != backfill.STATE_UNEXPLAINED
        )
        assert set(states[:first_version]) == {backfill.STATE_UNEXPLAINED}

    def test_a_dry_run_reports_them_and_writes_nothing(self, repo):
        code, output = run_backfill(repo, dry_run=True)
        assert code == 1
        assert "core@v9.9.9" in output
        assert "milestone-3" in output
        assert "parses under no recognized version-tag scheme" in output
        assert not (repo / ".rlsbl" / "releases" / "v0.1.0.toml").exists()

    def test_an_apply_refuses_and_writes_nothing_at_all(self, repo):
        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)
        assert "NOTHING has been written" in str(exc.value)
        # Not even the archive the plan could have repaired.
        assert list((repo / ".rlsbl" / "releases").iterdir()) == []

    def test_the_error_names_the_three_resolutions(self, repo):
        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)
        message = str(exc.value)
        assert "ADOPT IT AS RELEASED" in message
        assert "RECORD IT AS A NON-VERSION TAG" in message
        assert "DELETE IT" in message
        assert "shipped_as" in message
        # The typed door, not the Python snippet the message used to spell out.
        assert (
            "rlsbl transition record --non-version-tag milestone-3" in message
        )
        assert "git tag -d milestone-3" in message


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


class TestTheClosingTotal:
    """The closing line counts the two things an operator acts on.

    The archives to write, and the tags that would refuse the apply. The
    per-version verdicts above it say WHICH archive gets what, so a breakdown
    by attribute here would only invite arithmetic that does not hold -- one
    archive can be materialized AND unrecoverable, and one that merely gains
    its release commit is neither.
    """

    STAMPED_ARCHIVE = 'format_version = 1\n' + UNSTAMPED_ARCHIVE

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path)
        changes = repo / ".rlsbl" / "changes"
        releases = repo / ".rlsbl" / "releases"
        # 0.1.0: an archive that already carries the gate and only needs its
        # release commit.
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(changes, "0.1.0")
        write_archive(releases, "0.1.0", self.STAMPED_ARCHIVE)
        # 0.2.0: an archive predating the gate.
        commit_file(repo, "b.txt", "b\n", "v0.2.0")
        git(repo, "tag", "v0.2.0")
        write_changelog_jsonl(changes, "0.2.0")
        write_archive(releases, "0.2.0")
        # 0.3.0: no archive at all.
        commit_file(repo, "c.txt", "c\n", "v0.3.0")
        git(repo, "tag", "v0.3.0")
        write_changelog_jsonl(changes, "0.3.0")
        return repo

    def test_the_three_states_are_each_reached(self, repo):
        plan = backfill.build_plan(str(repo), use_gh=False)
        states = {v.version: v.state for v in plan.versions}
        assert states["0.1.0"] == backfill.STATE_REPAIR
        assert states["0.2.0"] == backfill.STATE_REPAIR
        assert states["0.3.0"] == backfill.STATE_MATERIALIZE
        assert len(plan.changed_versions) == 3

    def test_the_total_counts_archives_and_unexplained_tags(self, repo):
        _code, output = run_backfill(repo, dry_run=True)
        assert "TOTAL: 3 archive(s) to write, 0 unexplained tag(s)." in output

    def test_no_attribute_breakdown_invites_arithmetic(self, repo):
        _code, output = run_backfill(repo, dry_run=True)
        assert "materialized:" not in output
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

    def test_a_run_reports_the_refusal_and_writes_nothing(self, tmp_path):
        repo = tmp_path / "ws"
        repo.mkdir()
        init_repo(repo)
        (repo / ".rlsbl-monorepo").mkdir()
        (repo / ".rlsbl-monorepo" / "workspace.toml").write_text(
            'releasables = "not-a-list"\n\n'
            '[[projects]]\npath = "."\nname = "root"\nreleasable = false\n',
            encoding="utf-8",
        )

        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)

        assert "workspace.toml" in str(exc.value)


# ---------------------------------------------------------------------------
# Adopting a tag no store records
# ---------------------------------------------------------------------------


class TestAdoptAsReleased:
    """A version tag under this repository's own scheme IS evidence of a release.

    No archive, no changelog file -- and yet the tag exists, under exactly the
    spelling this scope's scheme produces. The pass records the release the tag
    is evidence of rather than reporting the tag as unexplained.
    """

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path, name="adopt")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        # 0.4.0 is known to nothing but the tag, and it skips two minors.
        commit_file(repo, "b.txt", "b\n", "v0.4.0")
        git(repo, "tag", "v0.4.0")
        return repo

    def test_the_verdict_is_adopt(self, repo):
        plan = backfill.build_plan(str(repo), use_gh=False)
        adopted = next(v for v in plan.versions if v.version == "0.4.0")
        assert adopted.state == backfill.STATE_ADOPT
        assert adopted.tag == "v0.4.0"
        assert "adopting it as released" in " ".join(adopted.notes)

    def test_the_archive_records_the_tags_commit_and_tree(self, repo):
        run_backfill(repo)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.4.0.toml")
        assert data["candidate_sha"] == git(repo, "rev-parse", "v0.4.0^{commit}")
        assert data["tree_hashes"] == {".": git(repo, "rev-parse", "v0.4.0^{tree}")}

    def test_the_bump_reads_the_highest_component_that_moved(self, repo):
        """A gap says nothing about how many releases crossed it."""
        run_backfill(repo)
        assert read_toml(repo / ".rlsbl" / "releases" / "v0.4.0.toml")["bump"] == "minor"

    def test_every_reconstructed_field_names_its_source(self, repo):
        run_backfill(repo)
        text = (repo / ".rlsbl" / "releases" / "v0.4.0.toml").read_text()
        assert "Materialized by `rlsbl release backfill`" in text
        assert "This archive's description came from:" in text

    def test_the_tag_is_not_reported_unexplained(self, repo):
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.unexplained == []

    def test_a_second_run_plans_nothing(self, repo):
        run_backfill(repo)
        assert backfill.build_plan(str(repo), use_gh=False).changed_versions == []


def test_bump_arithmetic_over_gaps():
    """The highest-order component that differs names the bump, at any distance."""
    assert backfill.derive_bump("1.0.0", "0.9.3") == "major"
    assert backfill.derive_bump("3.0.0", "0.9.3") == "major"
    assert backfill.derive_bump("0.4.0", "0.1.0") == "minor"
    assert backfill.derive_bump("0.4.0", "0.3.9") == "minor"
    assert backfill.derive_bump("0.3.7", "0.3.0") == "patch"
    assert backfill.derive_bump("0.1.0", None) == "minor"


# ---------------------------------------------------------------------------
# shipped_as: the historical spelling a version really shipped under
# ---------------------------------------------------------------------------


class TestShippedAsConsultation:
    """An archive naming its historical tag is recorded from THAT tag."""

    def _repo(self, tmp_path, tag, *, name="renamed"):
        repo = make_standalone(tmp_path, name=name)
        commit_file(repo, "a.txt", "a\n", "release")
        git(repo, "tag", tag)
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.12.0")
        path = Path(repo / ".rlsbl" / "releases") / "v0.12.0.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "format_version = 1\n"
            'bump = "minor"\ninclude = ["pypi"]\nexclude = []\n'
            'description = "Shipped under the old name."\n'
            f'shipped_as = "{tag}"\n',
            encoding="utf-8",
        )
        os.chmod(path, 0o444)
        return repo, path

    def test_the_commit_comes_from_the_historical_spelling(self, tmp_path):
        repo, path = self._repo(tmp_path, "strictcli@v0.12.0")
        run_backfill(repo)
        data = read_toml(path)
        assert data["candidate_sha"] == git(
            repo, "rev-parse", "strictcli@v0.12.0^{commit}"
        )

    def test_the_plan_says_where_the_spelling_came_from(self, tmp_path):
        repo, _path = self._repo(tmp_path, "strictcli@v0.12.0")
        plan = backfill.build_plan(str(repo), use_gh=False)
        vp = next(v for v in plan.versions if v.version == "0.12.0")
        assert vp.recorded_from == "shipped-as"
        assert "shipped_as" in " ".join(vp.notes)

    def test_a_member_path_spelling_works_the_same_way(self, tmp_path):
        repo, path = self._repo(tmp_path, "auth-gateway/v0.12.0", name="member")
        run_backfill(repo)
        assert read_toml(path)["candidate_sha"] == git(
            repo, "rev-parse", "auth-gateway/v0.12.0^{commit}"
        )

    def test_the_historical_tag_is_not_unexplained(self, tmp_path):
        repo, _path = self._repo(tmp_path, "strictcli@v0.12.0")
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.unexplained == []

    def test_without_the_field_the_same_tag_is_unexplained(self, tmp_path):
        repo, path = self._repo(tmp_path, "strictcli@v0.12.0", name="bare")
        os.chmod(path, 0o644)
        path.write_text(
            path.read_text().replace('shipped_as = "strictcli@v0.12.0"\n', ""),
            encoding="utf-8",
        )
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert [e.tag for e in plan.unexplained] == ["strictcli@v0.12.0"]


# ---------------------------------------------------------------------------
# Tags an operator put outside the version model
# ---------------------------------------------------------------------------


class TestRecordedNonVersionTags:

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path, name="nonversion")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        git(repo, "tag", "nightly-2024-01-01")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        return repo

    def test_it_is_unexplained_until_it_is_recorded(self, repo):
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert [e.tag for e in plan.unexplained] == ["nightly-2024-01-01"]

    def test_the_event_takes_it_off_the_list(self, repo):
        from rlsbl.transition_record import (
            NonVersionTagEvent, append_event, get_transition_record_path,
        )

        append_event(
            get_transition_record_path(str(repo)),
            NonVersionTagEvent(
                tag="nightly-2024-01-01", reason="a nightly build marker",
            ),
        )
        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.unexplained == []
        assert plan.outside_the_model == ["nightly-2024-01-01"]

    def test_the_plan_says_it_is_accounted_for(self, repo):
        from rlsbl.transition_record import (
            NonVersionTagEvent, append_event, get_transition_record_path,
        )

        append_event(
            get_transition_record_path(str(repo)),
            NonVersionTagEvent(tag="nightly-2024-01-01", reason="a marker"),
        )
        _code, output = run_backfill(repo, dry_run=True)
        assert "recorded outside the version model" in output
        assert "nightly-2024-01-01" in output


# ---------------------------------------------------------------------------
# The operator's reviewed descriptions
# ---------------------------------------------------------------------------


def write_overrides(tmp_path, body, *, name="overrides.toml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


class TestOverrides:
    """``--overrides`` is applied BEFORE any derivation, and ignores nothing."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path, name="overridden")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.0\n\nThe derived paragraph.\n", encoding="utf-8",
        )
        return repo

    def test_a_reviewed_description_beats_the_derivation(self, repo, tmp_path):
        overrides = backfill.read_overrides(write_overrides(
            tmp_path,
            '[versions."0.1.0"]\ndescription = "The reviewed summary."\n',
        ))
        run_backfill(repo, overrides=overrides)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["description"] == "The reviewed summary."

    def test_context_rides_along(self, repo, tmp_path):
        overrides = backfill.read_overrides(write_overrides(
            tmp_path,
            '[versions."0.1.0"]\ndescription = "d"\ncontext = "why it happened"\n',
        ))
        run_backfill(repo, overrides=overrides)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["context"] == "why it happened"

    def test_it_rewrites_an_existing_description_too(self, tmp_path):
        repo = make_standalone(tmp_path, name="rewritten")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        path = write_archive(repo / ".rlsbl" / "releases", "0.1.0")
        overrides = backfill.read_overrides(write_overrides(
            tmp_path, '[versions."0.1.0"]\ndescription = "Reviewed and rewritten."\n',
        ))

        run_backfill(repo, overrides=overrides)

        assert read_toml(path)["description"] == "Reviewed and rewritten."
        assert is_locked(path)

    def test_a_second_run_with_the_same_file_plans_nothing(self, repo, tmp_path):
        overrides = backfill.read_overrides(write_overrides(
            tmp_path, '[versions."0.1.0"]\ndescription = "The reviewed summary."\n',
        ))
        run_backfill(repo, overrides=overrides)
        plan = backfill.build_plan(str(repo), use_gh=False, overrides=overrides)
        assert plan.changed_versions == []

    def test_a_version_the_pass_does_not_have_is_a_hard_error(self, repo, tmp_path):
        overrides = backfill.read_overrides(write_overrides(
            tmp_path, '[versions."9.9.9"]\ndescription = "nothing has this"\n',
        ))
        with pytest.raises(BackfillError) as exc:
            backfill.build_plan(str(repo), use_gh=False, overrides=overrides)
        assert "9.9.9" in str(exc.value)
        assert "nothing is silently ignored" in str(exc.value)

    def test_a_never_released_version_is_refused(self, tmp_path):
        repo = make_standalone(tmp_path, name="phantom-override")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.9.0")
        write_archived_release_file(
            str(repo / ".rlsbl" / "releases"), "0.9.0",
            bump="minor", include=["pypi"], description="claimed, never shipped",
            candidate_sha=None, tree_hashes=None, never_released=True,
        )
        overrides = backfill.read_overrides(write_overrides(
            tmp_path, '[versions."0.9.0"]\ndescription = "a description"\n',
        ))
        with pytest.raises(BackfillError) as exc:
            backfill.build_plan(str(repo), use_gh=False, overrides=overrides)
        assert "never_released" in str(exc.value)


class TestOverridesFileShape:

    def test_a_missing_file_is_named(self, tmp_path):
        with pytest.raises(BackfillError, match="no overrides file"):
            backfill.read_overrides(str(tmp_path / "nope.toml"))

    def test_an_unknown_top_level_key_is_refused(self, tmp_path):
        path = write_overrides(tmp_path, 'descriptions = {}\n')
        with pytest.raises(BackfillError, match="unknown top-level key"):
            backfill.read_overrides(path)

    def test_a_file_with_no_versions_table_is_refused(self, tmp_path):
        with pytest.raises(BackfillError, match=r"no \[versions\] table"):
            backfill.read_overrides(write_overrides(tmp_path, "\n"))

    def test_an_unknown_entry_key_is_refused(self, tmp_path):
        path = write_overrides(
            tmp_path, '[versions."0.1.0"]\ndescription = "d"\nbump = "minor"\n',
        )
        with pytest.raises(BackfillError, match="unknown key"):
            backfill.read_overrides(path)

    def test_an_empty_description_is_refused(self, tmp_path):
        path = write_overrides(tmp_path, '[versions."0.1.0"]\ndescription = "  "\n')
        with pytest.raises(BackfillError, match="non-empty string"):
            backfill.read_overrides(path)

    def test_the_shipped_shape_round_trips(self, tmp_path):
        path = write_overrides(
            tmp_path,
            '[versions."0.1.0"]\n'
            'description = "The first release."\n'
            'context = """\nWhy it happened.\n"""\n'
            '\n[versions."0.2.0"]\ndescription = "The second."\n',
        )
        overrides = backfill.read_overrides(path)
        assert sorted(overrides) == ["0.1.0", "0.2.0"]
        assert overrides["0.1.0"].description == "The first release."
        assert "Why it happened." in overrides["0.1.0"].context
        assert overrides["0.2.0"].context == ""


# ---------------------------------------------------------------------------
# The GitHub Release source, and what counts as content in a body
# ---------------------------------------------------------------------------


class TestReleaseBodySubstance:
    """Auto-generated compare-link boilerplate is not content."""

    BOILERPLATE = (
        "## What's Changed\n\n"
        "**Full Changelog**: https://github.com/o/r/compare/v0.1.0...v0.2.0\n"
    )

    def test_boilerplate_alone_is_absent(self):
        assert backfill.body_is_substantive(self.BOILERPLATE) is False
        assert backfill.description_from_body(self.BOILERPLATE) is None

    def test_an_empty_body_is_absent(self):
        assert backfill.body_is_substantive("") is False
        assert backfill.body_is_substantive(None) is False

    def test_prose_is_content(self):
        body = "A real summary paragraph.\n\n### Features\n\n- a thing\n"
        assert backfill.body_is_substantive(body) is True
        assert backfill.description_from_body(body) == "A real summary paragraph."

    def test_bullets_are_content(self):
        body = self.BOILERPLATE.replace(
            "**Full Changelog**", "- fixed the thing\n**Full Changelog**",
        )
        assert backfill.body_is_substantive(body) is True
        assert backfill.description_from_body(body) == "fixed the thing"

    def test_a_blockquote_opening_is_content(self):
        body = "> **Deprecated:** use 0.3.0.\n\n**Full Changelog**: http://x\n"
        assert backfill.body_is_substantive(body) is True
        assert backfill.description_from_body(body) == "**Deprecated:** use 0.3.0."


class TestTheReleaseBodySource:
    """The first link of the chain, driven through an injected reader."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path, name="ghsource")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        (repo / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.0\n\nThe changelog paragraph.\n",
            encoding="utf-8",
        )
        return repo

    def _gh(self, body):
        def run(args, config=None):
            assert args[:3] == ["release", "view", "v0.1.0"], args
            return body
        return run

    def test_a_substantive_body_wins(self, repo):
        run_backfill(repo, gh=self._gh("The Release notes paragraph.\n"))
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["description"] == "The Release notes paragraph."

    def test_boilerplate_falls_through_to_the_changelog(self, repo):
        run_backfill(repo, gh=self._gh(
            "## What's Changed\n\n**Full Changelog**: https://x/compare/a...b\n"
        ))
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["description"] == "The changelog paragraph."

    def test_an_unreadable_release_falls_through(self, repo):
        def boom(args, config=None):
            raise RuntimeError("no gh here")

        run_backfill(repo, gh=boom)
        data = read_toml(repo / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["description"] == "The changelog paragraph."


# ---------------------------------------------------------------------------
# Managed-repo hygiene: a stash refuses the apply
# ---------------------------------------------------------------------------


class TestStashRefusal:
    """A stash is uncommitted work with no branch, and nothing here owns it."""

    @pytest.fixture
    def repo(self, tmp_path):
        repo = make_standalone(tmp_path, name="stashed")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")
        # Bare `git stash`, not `git stash push`: the suite's push guard
        # reads the second token, and a stash is not a push.
        git(repo, "stash")
        return repo

    def test_the_apply_refuses(self, repo):
        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)
        assert "stash" in str(exc.value)
        assert "git stash drop" in str(exc.value)

    def test_nothing_is_written(self, repo):
        with pytest.raises(BackfillError):
            run_backfill(repo)
        assert list((repo / ".rlsbl" / "releases").iterdir()) == []

    def test_a_dry_run_still_previews(self, repo):
        code, output = run_backfill(repo, dry_run=True)
        assert code == 0
        assert "standalone 0.1.0" in output


# ---------------------------------------------------------------------------
# Remedy-followability: every printed remedy is performed verbatim here
# ---------------------------------------------------------------------------


class TestEveryRemedyClearsItsError:
    """A remedy a command prints that does not work is worse than no remedy."""

    def _repo_with(self, tmp_path, tag, name):
        repo = make_standalone(tmp_path, name=name)
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        git(repo, "tag", tag)
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        return repo

    def test_resolution_one_adopt_as_released(self, tmp_path):
        """The automatic form: a tag this scope's own scheme produces."""
        repo = make_standalone(tmp_path, name="remedy-adopt")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        # No archive, no changelog file: only the tag records this release.
        code, _output = run_backfill(repo)
        assert code == 0
        assert (repo / ".rlsbl" / "releases" / "v0.1.0.toml").is_file()
        assert backfill.build_plan(str(repo), use_gh=False).unexplained == []

    def test_resolution_one_through_shipped_as(self, tmp_path):
        """The spelling an older scheme used: the archive names it, verbatim."""
        repo = self._repo_with(tmp_path, "oldname@v0.2.0", "remedy-shipped-as")
        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)
        assert 'shipped_as = "oldname@v0.2.0"' in str(exc.value)

        # Perform it: record that version's archive with the historical spelling.
        write_archived_release_file(
            str(repo / ".rlsbl" / "releases"), "0.2.0",
            bump="minor", include=["pypi"], description="Shipped under the old name.",
            candidate_sha=None, tree_hashes=None, unrecoverable=True,
            shipped_as="oldname@v0.2.0",
        )

        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.unexplained == []

    def test_resolution_two_record_it_as_a_non_version_tag(self, tmp_path):
        repo = self._repo_with(tmp_path, "vendor-import", "remedy-record")
        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)
        message = str(exc.value)
        assert (
            "rlsbl transition record --non-version-tag vendor-import" in message
        )

        # Perform it as the command the message now names, not as the Python
        # snippet it used to spell out.
        from rlsbl.commands.transition_record_cmd import run_cmd as record_cmd
        from rlsbl.context import create_context
        from rlsbl.transition_record import (
            KIND_NON_VERSION_TAG, get_transition_record_path,
        )

        assert get_transition_record_path(str(repo)) in message
        record_cmd(
            {
                "kind": KIND_NON_VERSION_TAG, "subject": "vendor-import",
                "reason": "imported with the history",
                "dry-run": False, "auto-commit": True,
            },
            ctx=create_context(Path(repo)),
        )

        code, _output = run_backfill(repo)
        assert code == 0
        assert backfill.build_plan(str(repo), use_gh=False).unexplained == []

    def test_resolution_three_delete_it(self, tmp_path):
        repo = self._repo_with(tmp_path, "milestone-3", "remedy-delete")
        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)
        assert "git tag -d milestone-3" in str(exc.value)

        git(repo, "tag", "-d", "milestone-3")

        code, _output = run_backfill(repo)
        assert code == 0

    def test_the_stash_remedy_clears_the_stash_error(self, tmp_path):
        repo = make_standalone(tmp_path, name="remedy-stash")
        commit_file(repo, "a.txt", "a\n", "v0.1.0")
        git(repo, "tag", "v0.1.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.1.0")
        (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")
        git(repo, "stash")

        with pytest.raises(BackfillError) as exc:
            run_backfill(repo)
        assert "git stash drop" in str(exc.value)

        git(repo, "stash", "drop")

        code, _output = run_backfill(repo)
        assert code == 0

    def test_the_declare_first_remedy_settles_a_tagless_version(self, tmp_path):
        """The note on a tagless version names a procedure that really works."""
        repo = make_standalone(tmp_path, name="remedy-declare")
        commit_file(repo, "a.txt", "a\n", "v0.3.0")
        write_changelog_jsonl(repo / ".rlsbl" / "changes", "0.3.0")

        plan = backfill.build_plan(str(repo), use_gh=False)
        notes = " ".join(next(v for v in plan.versions).notes)
        assert "never_released = true" in notes

        # Perform it: declare the fate by writing the archive.
        write_archived_release_file(
            str(repo / ".rlsbl" / "releases"), "0.3.0",
            bump="minor", include=["pypi"], description="claimed, never shipped",
            candidate_sha=None, tree_hashes=None, never_released=True,
        )

        plan = backfill.build_plan(str(repo), use_gh=False)
        assert plan.changed_versions == []
        assert "settled" in " ".join(next(v for v in plan.versions).notes)
