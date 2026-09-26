"""The release flow decides what it releases from the record's FATES, not from existence.

The state that motivated this module is the one an abandoned release attempt
leaves behind: 0.29.3 released (archived and tagged), the version files bumped
to 0.29.4, and no archive and no tag for 0.29.4. The release flow used to ask
only whether the CURRENT version had an archive or a tag, found neither, called
that a first release, and would have tagged v0.29.4 as-is with the declared
bump ignored.

The rules pinned here:

* a first release is one whose record holds no released version at all, and a
  monorepo releasable asks its own record;
* once any release exists, version files naming a number with neither an
  archive nor a tag are refused, naming ``rlsbl release abandon``;
* version files naming a number recorded never released are bumped from;
* a computed new version recorded never released is refused, naming the
  version files' value that bumps past it;
* the destroyed-tag guard no longer fires for a never-released current version;
* version files naming an unrecoverable version whose tag is absent are refused,
  and restoring the tag clears it, while ``rlsbl rewrite project-name`` records
  the version the release ships once it is cleared;
* version files naming an unrecorded version below the latest release are
  refused as behind it, never pointed at ``rlsbl release abandon``.
"""

import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import rlsbl
from conftest import archive_release, release_record_dir
from githarness import add_remote, git, init_repo
from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    compute_release_version,
)
from rlsbl.targets import TARGETS
from test_remedy_followability import assert_invocation_is_real, invocations_in


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _write_version(repo, version):
    (repo / "package.json").write_text(
        json.dumps({"name": "pkg", "version": version}, indent=2) + "\n"
    )


def _commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def _init_project(tmp_path, version, *, tag):
    """A standalone npm project at *version*, committed, with a bare origin.

    *tag* tags that commit ``v<version>`` before the origin is created, so the
    origin holds the tag too.
    """
    repo = tmp_path / "repo"
    init_repo(repo)
    _write_version(repo, version)
    (repo / ".rlsbl" / "changes").mkdir(parents=True)
    (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(json.dumps(
        {"publish_mode": "ci", "targets": ["npm"]}, indent=2,
    ) + "\n")
    (repo / ".gitignore").write_text(".rlsbl/releases/in-progress.json\n")
    sha = _commit_all(repo, f"v{version}")
    if tag:
        git(repo, "tag", f"v{version}")
    add_remote(repo, tmp_path / "origin.git")
    return repo, sha


def build(tmp_path, *, released="0.29.3", files=None, never_released=()):
    """A standalone npm project that released *released* (archived and tagged).

    *never_released* versions are archived with the never-released fate, and
    the version files are then set to *files* (default: the released version).
    The origin is a local bare repository, so the remote tag probe answers.
    """
    repo, sha = _init_project(tmp_path, released, tag=True)
    archive_release(release_record_dir(repo), released, sha,
                    tree=git(repo, "rev-parse", "HEAD^{tree}"))
    for version in never_released:
        archive_release(release_record_dir(repo), version, None,
                        never_released=True)
    _commit_all(repo, "chore: archive the release record")
    if files is not None and files != released:
        _write_version(repo, files)
        _commit_all(repo, f"v{files}")
    return repo


def build_unrecoverable(tmp_path, *, version="0.27.1"):
    """A project whose one release, *version*, is archived unrecoverable.

    The version files name it, and no tag exists: the state the backfill
    records when neither a tag nor a version-bump commit names the commit the
    version shipped from. Returns the repository and the commit it shipped from,
    which only the operator knows.
    """
    repo, sha = _init_project(tmp_path, version, tag=False)
    archive_release(release_record_dir(repo), version, None, unrecoverable=True)
    _commit_all(repo, "chore: archive the release record")
    return repo, sha


def compute(repo, bump, logs=None):
    sink = logs if logs is not None else []
    return compute_release_version(
        TARGETS["npm"], str(repo), bump, None, None, sink.append,
        project_dir=str(repo),
    )


def refusal(repo, bump):
    with pytest.raises(ReleaseValidationError) as exc:
        compute(repo, bump)
    return str(exc.value)


def run_abandon(repo, monkeypatch, *extra, release_exists=False):
    """Run `rlsbl release abandon` through the real CLI dispatch."""
    monkeypatch.chdir(repo)
    with patch(
        "rlsbl.commands.release_abandon._github_release_exists",
        return_value=release_exists,
    ):
        return rlsbl.app.test(
            ["release", "abandon", "--approve-consequential", *extra]
        )


# --------------------------------------------------------------------------- #
# The misread state: version files bumped past the latest release, unrecorded
# --------------------------------------------------------------------------- #

class TestTheMisreadStateIsRefused:

    def test_it_is_refused_before_anything_changes(self, tmp_path, monkeypatch):
        repo = build(tmp_path, files="0.29.4")
        monkeypatch.chdir(repo)
        head = git(repo, "rev-parse", "HEAD")
        logs = []

        with pytest.raises(ReleaseValidationError) as exc:
            compute(repo, "minor", logs)

        message = str(exc.value)
        assert "0.29.4" in message and "0.29.3" in message, message
        assert "rlsbl release abandon" in message, message
        assert not any("First release" in line for line in logs), logs
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "status", "--porcelain") == ""

    def test_the_remedy_names_a_real_command(self, tmp_path, monkeypatch):
        repo = build(tmp_path, files="0.29.4")
        monkeypatch.chdir(repo)

        invocations = invocations_in(refusal(repo, "minor"))

        assert invocations, "the refusal names no command"
        for invocation in invocations:
            assert_invocation_is_real(invocation)

    def test_following_the_remedy_clears_the_refusal(self, tmp_path, monkeypatch):
        """`rlsbl release abandon` records 0.29.4; the release then bumps from it."""
        repo = build(tmp_path, files="0.29.4")
        monkeypatch.chdir(repo)
        refusal(repo, "minor")

        result = run_abandon(repo, monkeypatch)
        assert result.exit_code == 0, result.stderr + result.stdout

        assert compute(repo, "minor") == ("0.29.4", "0.30.0", "minor", "v0.30.0")


# --------------------------------------------------------------------------- #
# A never-released current version is bumped from
# --------------------------------------------------------------------------- #

class TestANeverReleasedCurrentVersionIsBumpedFrom:

    @pytest.mark.parametrize("bump, expected", [
        ("minor", "0.30.0"),
        ("patch", "0.29.5"),
    ])
    def test_the_declared_bump_applies(self, tmp_path, monkeypatch, bump, expected):
        repo = build(tmp_path, files="0.29.4", never_released=("0.29.4",))
        monkeypatch.chdir(repo)
        logs = []

        current, new, bump_type, tag = compute(repo, bump, logs)

        assert (current, new, bump_type, tag) == ("0.29.4", expected, bump, f"v{expected}")
        assert not any("First release" in line for line in logs), logs

    def test_the_highest_archive_never_decides_the_base(self, tmp_path, monkeypatch):
        """pgdesign's shape: never-released 1.0.0 and 1.0.1 above the real 0.27.1.

        The files name 0.27.1, so the release bumps from 0.27.1. rlsbl never
        picks a never-released number as the base on its own, however high.
        """
        repo = build(tmp_path, released="0.27.1",
                     never_released=("1.0.0", "1.0.1"))
        monkeypatch.chdir(repo)

        assert compute(repo, "minor") == ("0.27.1", "0.28.0", "minor", "v0.28.0")


# --------------------------------------------------------------------------- #
# Reusing a never-released number is refused
# --------------------------------------------------------------------------- #

class TestReusingANeverReleasedNumberIsRefused:

    def test_it_is_refused_naming_the_way_past_it(self, tmp_path, monkeypatch):
        repo = build(tmp_path, never_released=("0.29.4",))
        monkeypatch.chdir(repo)
        head = git(repo, "rev-parse", "HEAD")

        message = refusal(repo, "patch")

        assert "0.29.4" in message and "never released" in message, message
        assert "0.29.5" in message, message
        assert git(repo, "rev-parse", "HEAD") == head

    def test_every_number_in_a_run_of_them_is_skipped(self, tmp_path, monkeypatch):
        repo = build(tmp_path, never_released=("0.29.4", "0.29.5"))
        monkeypatch.chdir(repo)

        message = refusal(repo, "patch")

        assert "0.29.5" in message and "0.29.6" in message, message

    def test_following_the_remedy_clears_the_refusal(self, tmp_path, monkeypatch):
        repo = build(tmp_path, never_released=("0.29.4",))
        monkeypatch.chdir(repo)
        refusal(repo, "patch")

        # The named fix: set the version files to the never-released number.
        _write_version(repo, "0.29.4")
        _commit_all(repo, "0.29.4")

        assert compute(repo, "patch") == ("0.29.4", "0.29.5", "patch", "v0.29.5")

    def test_a_bump_that_does_not_land_on_it_is_unaffected(self, tmp_path, monkeypatch):
        repo = build(tmp_path, never_released=("0.29.4",))
        monkeypatch.chdir(repo)

        assert compute(repo, "minor") == ("0.29.3", "0.30.0", "minor", "v0.30.0")


# --------------------------------------------------------------------------- #
# First releases
# --------------------------------------------------------------------------- #

def _fresh_project(tmp_path, version="0.1.0"):
    repo = tmp_path / "fresh"
    init_repo(repo)
    _write_version(repo, version)
    (repo / ".rlsbl" / "changes").mkdir(parents=True)
    (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    _commit_all(repo, "initial")
    add_remote(repo, tmp_path / "fresh-origin.git")
    return repo


class TestFirstReleases:

    def test_an_empty_record_is_a_first_release(self, tmp_path, monkeypatch):
        repo = _fresh_project(tmp_path)
        monkeypatch.chdir(repo)
        logs = []

        assert compute(repo, "minor", logs) == ("0.1.0", "0.1.0", None, "v0.1.0")
        assert any("First release" in line for line in logs), logs

    def test_a_record_holding_only_never_released_numbers_is_a_first_release(
        self, tmp_path, monkeypatch,
    ):
        repo = _fresh_project(tmp_path)
        archive_release(release_record_dir(repo), "0.0.9", None, never_released=True)
        _commit_all(repo, "record 0.0.9")
        monkeypatch.chdir(repo)

        assert compute(repo, "minor") == ("0.1.0", "0.1.0", None, "v0.1.0")

    def test_a_new_releasable_is_a_first_release_beside_released_siblings(
        self, tmp_path, monkeypatch,
    ):
        """A releasable asks its OWN record, not the workspace's."""
        ws = tmp_path / "ws"
        init_repo(ws)
        rel_root = ws / ".rlsbl-monorepo" / "releasables"
        for name, version in (("old", "1.0.0"), ("fresh", "0.1.0")):
            (rel_root / name / "changes").mkdir(parents=True)
            (rel_root / name / "changes" / "unreleased.jsonl").write_text("")
            (rel_root / name / "version").write_text(version + "\n")
        sha = _commit_all(ws, "initial")
        git(ws, "tag", "old@v1.0.0")
        archive_release(str(rel_root / "old" / "releases"), "1.0.0", sha)
        _commit_all(ws, "archive old 1.0.0")
        add_remote(ws, tmp_path / "ws-origin.git")
        monkeypatch.chdir(ws)

        result = compute_release_version(
            TARGETS["npm"], str(ws), "minor", None, None, lambda _m: None,
            workspace_root=str(ws), releasable_name="fresh",
            releasable_tag_fmt="{name}@v{version}", project_dir=str(ws),
        )

        assert result == ("0.1.0", "0.1.0", None, "fresh@v0.1.0")


# --------------------------------------------------------------------------- #
# Version files behind the latest release
# --------------------------------------------------------------------------- #

class TestVersionFilesBehindTheLatestRelease:

    def test_it_is_refused_naming_both_numbers(self, tmp_path, monkeypatch):
        repo = build(tmp_path, files="0.29.1")
        monkeypatch.chdir(repo)
        head = git(repo, "rev-parse", "HEAD")

        message = refusal(repo, "patch")

        assert "behind the latest release" in message, message
        assert "0.29.1" in message and "0.29.3" in message, message
        assert "at least 0.29.3" in message, message
        assert "abandon" not in message, message
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "status", "--porcelain") == ""

    def test_following_the_fix_clears_the_refusal(self, tmp_path, monkeypatch):
        repo = build(tmp_path, files="0.29.1")
        monkeypatch.chdir(repo)
        refusal(repo, "patch")

        # The named fix: set the version files to the latest release.
        _write_version(repo, "0.29.3")
        _commit_all(repo, "0.29.3")

        assert compute(repo, "patch") == ("0.29.3", "0.29.4", "patch", "v0.29.4")

    def test_an_unorderable_version_is_refused_and_fixing_it_clears(
        self, tmp_path, monkeypatch,
    ):
        repo = build(tmp_path, files="0.29")
        monkeypatch.chdir(repo)

        message = refusal(repo, "patch")

        assert "0.29 is not a version the release record can hold" in message
        assert "Fix the version files" in message, message

        _write_version(repo, "0.29.3")
        _commit_all(repo, "0.29.3")
        assert compute(repo, "patch")[1] == "0.29.4"

    def test_a_version_above_the_latest_release_still_names_abandon(
        self, tmp_path, monkeypatch,
    ):
        repo = build(tmp_path, files="0.29.4")
        monkeypatch.chdir(repo)

        message = refusal(repo, "patch")

        assert "behind the latest release" not in message, message
        assert "rlsbl release abandon" in message, message


# --------------------------------------------------------------------------- #
# An unrecoverable current version
# --------------------------------------------------------------------------- #

class TestAnUnrecoverableCurrentVersionIsRefused:

    def test_it_is_refused_before_anything_changes(self, tmp_path, monkeypatch):
        repo, _sha = build_unrecoverable(tmp_path)
        monkeypatch.chdir(repo)
        head = git(repo, "rev-parse", "HEAD")
        logs = []

        with pytest.raises(ReleaseValidationError) as exc:
            compute(repo, "minor", logs)

        message = str(exc.value)
        assert "0.27.1" in message and "unrecoverable" in message, message
        assert "git tag v0.27.1 <" in message, message
        assert "abandon" not in message, message
        assert not any("New version" in line for line in logs), logs
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "status", "--porcelain") == ""

    def test_restoring_the_tag_clears_the_refusal(self, tmp_path, monkeypatch):
        repo, shipped_from = build_unrecoverable(tmp_path)
        monkeypatch.chdir(repo)
        refusal(repo, "minor")

        # The named fix: tag the commit the operator knows it shipped from.
        git(repo, "tag", "v0.27.1", shipped_from)

        assert compute(repo, "minor") == ("0.27.1", "0.28.0", "minor", "v0.28.0")


# --------------------------------------------------------------------------- #
# The destroyed-tag guard
# --------------------------------------------------------------------------- #

class TestTheDestroyedTagGuard:

    def test_a_never_released_current_version_does_not_trip_it(
        self, tmp_path, monkeypatch,
    ):
        """No tag for a never-released number is the expected state, not a loss."""
        repo = build(tmp_path, files="0.29.4", never_released=("0.29.4",))
        monkeypatch.chdir(repo)

        assert compute(repo, "patch")[1] == "0.29.5"

    def test_a_destroyed_tag_names_its_recorded_commit_and_restoring_it_clears(
        self, tmp_path, monkeypatch,
    ):
        repo = build(tmp_path)
        release_sha = git(repo, "rev-parse", "v0.29.3")
        git(repo, "tag", "-d", "v0.29.3")
        monkeypatch.chdir(repo)

        message = refusal(repo, "patch")

        assert f"git tag v0.29.3 {release_sha}" in message, message
        assert "move the version forward" not in message, message

        git(repo, "tag", "v0.29.3", release_sha)
        assert compute(repo, "patch")[1] == "0.29.4"


# --------------------------------------------------------------------------- #
# The project-rename command asks the same decision
# --------------------------------------------------------------------------- #

class TestTheProjectRenameDecision:

    def _rename(self, repo, monkeypatch):
        (repo / ".rlsbl" / "releases" / "unreleased.toml").write_text(
            'format_version = 1\nbump = "minor"\ninclude = ["npm"]\n'
            'exclude = []\ndescription = "the next release"\n'
        )
        _commit_all(repo, "release file")
        monkeypatch.chdir(repo)
        return rlsbl.app.test([
            "--dry-run", "rewrite", "project-name", "--from", "pkg",
            "--to", "gadget", "--approve-consequential",
        ])

    def test_the_misread_state_is_refused(self, tmp_path, monkeypatch):
        repo = build(tmp_path, files="0.29.4")

        result = self._rename(repo, monkeypatch)

        assert result.exit_code != 0, result.stdout
        text = result.stderr + result.stdout
        assert "rlsbl release abandon" in text, text
        assert "effective 0.29.4" not in text, text

    def test_a_never_released_current_version_is_bumped_from(
        self, tmp_path, monkeypatch,
    ):
        repo = build(tmp_path, files="0.29.4", never_released=("0.29.4",))

        result = self._rename(repo, monkeypatch)

        assert result.exit_code == 0, result.stderr + result.stdout
        assert "effective 0.30.0" in result.stdout, result.stdout

    def test_version_files_behind_the_latest_release_are_refused(
        self, tmp_path, monkeypatch,
    ):
        repo = build(tmp_path, files="0.29.1")

        result = self._rename(repo, monkeypatch)

        assert result.exit_code != 0, result.stdout
        text = result.stderr + result.stdout
        assert "behind the latest release" in text, text

    def test_an_unrecoverable_current_version_is_bumped_from(
        self, tmp_path, monkeypatch,
    ):
        """The release refuses until the tag is restored; the rename does not.

        What the rename records is the version the next release ships, and once
        the tag is restored that release bumps from the unrecoverable version.
        """
        repo, _sha = build_unrecoverable(tmp_path)

        result = self._rename(repo, monkeypatch)

        assert result.exit_code == 0, result.stderr + result.stdout
        assert "effective 0.28.0" in result.stdout, result.stdout


# --------------------------------------------------------------------------- #
# The archive step never writes over a never-released archive
# --------------------------------------------------------------------------- #

class TestTheArchiveStepBackstop:

    def test_a_never_released_archive_is_refused(self, tmp_path):
        from rlsbl.commands.release.execute import refuse_never_released_archive
        from rlsbl.errors import ReleaseFileError

        releases = release_record_dir(tmp_path)
        archive_release(releases, "0.29.4", None, never_released=True)
        before = (tmp_path / ".rlsbl/releases/v0.29.4.toml").read_bytes()

        with pytest.raises(ReleaseFileError) as exc:
            refuse_never_released_archive(releases, "0.29.4")

        assert "never released" in str(exc.value)
        assert (tmp_path / ".rlsbl/releases/v0.29.4.toml").read_bytes() == before

    def test_the_executor_stops_at_the_archive_step(self, tmp_project, capsys):
        """End to end: the record changes under a running release.

        The release decides 1.0.1 before anything changes, and a never-released
        archive for 1.0.1 appears before the archive step (written here from the
        step just before it). The executor stops there: the archive is neither
        renamed over nor written into, the release file is not consumed, and no
        tag is created.
        """
        from pathlib import Path

        import rlsbl.config as config_module
        from rlsbl.commands.release import run_cmd
        from rlsbl.release_file import read_release_file
        from test_release_file_relocation import (
            _make_ctx,
            _rc,
            _release_patches,
            _setup_releasable_workspace,
            _write_releasable_release_file,
        )

        core = _setup_releasable_workspace(tmp_project)
        release_file = Path(_write_releasable_release_file(tmp_project))
        releases = str(release_file.parent)
        archive = release_file.parent / "v1.0.1.toml"
        written = {}
        real_clean = config_module.clean_stale_exclusions

        def the_record_changes(path):
            archive_release(releases, "1.0.1", None, never_released=True)
            written["bytes"] = archive.read_bytes()
            return real_clean(path)

        with ExitStack() as stack:
            for active in _release_patches():
                stack.enter_context(active)
            stack.enter_context(patch(
                "rlsbl.config.clean_stale_exclusions",
                side_effect=the_record_changes,
            ))
            with pytest.raises(SystemExit):
                run_cmd(_rc(), {"quiet": True, "skip-lock": True},
                        ctx=_make_ctx(core, tmp_project))

        stderr = capsys.readouterr().err
        assert "refusing to archive the release of 1.0.1" in stderr, stderr
        assert written, "the release never reached the step before the archive"
        assert archive.read_bytes() == written["bytes"]
        assert read_release_file(str(archive)).never_released is True
        assert release_file.exists()
        assert "alpha@v1.0.1" not in git(tmp_project, "tag", "-l").split()
        log = git(tmp_project, "log", "--format=%s")
        assert "finalize release file" not in log, log

    def test_no_archive_or_a_released_one_passes(self, tmp_path):
        from rlsbl.commands.release.execute import refuse_never_released_archive

        releases = release_record_dir(tmp_path)
        archive_release(releases, "0.29.3", "a" * 40)

        refuse_never_released_archive(releases, "0.29.3")
        refuse_never_released_archive(releases, "0.29.4")
