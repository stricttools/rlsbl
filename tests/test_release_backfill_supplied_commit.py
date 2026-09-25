"""``rlsbl release backfill --version <v> --commit <sha>``: a caller-supplied release commit.

The backfill pass marks an archive ``unrecoverable = true`` when neither a tag
nor a version-bump commit names the commit the version shipped from. When the
operator knows that commit, ``--version`` with ``--commit`` records it: the
command verifies that every target in the release's scope declares the version
at that commit (read by each target's own ``read_version`` from the commit's
files), that the commit is in the release branch's history, and then clears the
mark and records the commit exactly as the pass records one it found itself.

Everything here drives the real CLI dispatch on a real git repository, because
the command commits. Every refusal that names a fix performs the fix and
asserts the refusal clears.
"""

import json
import os
import stat
import tomllib

import pytest

import rlsbl
from conftest import archive_release, make_workspace
from githarness import git, init_repo
from rlsbl.release_record import read_entry


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _commit_all(root, message):
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


def _write_versions(root, *, go, npm):
    _write(root, "VERSION", f"{go}\n")
    _write(root, "npm/package.json", json.dumps({"name": "widget", "version": npm}) + "\n")


def _changelog(changes_dir, version):
    path = changes_dir / f"{version}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"format_version":1,"commits":["%s"],"user_facing":false}\n' % ("0" * 40),
    )
    os.chmod(path, 0o444)


def build_standalone(root, *, archive="unrecoverable"):
    """0.1.0 shipped from ``release_sha``; 0.2.0 is declared at ``later_sha``.

    *archive* is the 0.1.0 archive's state: ``"unrecoverable"``, ``"recorded"``
    (from ``release_sha``), ``"never-released"``, ``"unsettled"`` (an archive
    with no fate), or ``None`` (no archive at all).
    """
    init_repo(root)
    _write(root, ".rlsbl/config.json", json.dumps({
        "targets": ["go", {"name": "npm", "path": "npm/"}],
        "publish_mode": "ci",
    }) + "\n")
    _write(root, ".rlsbl/changes/unreleased.jsonl", "")
    _changelog(root / ".rlsbl" / "changes", "0.1.0")
    _write_versions(root, go="0.1.0", npm="0.1.0")
    release_sha = _commit_all(root, "the work 0.1.0 shipped")
    _write_versions(root, go="0.2.0", npm="0.2.0")
    later_sha = _commit_all(root, "the work 0.2.0 will ship")
    releases = root / ".rlsbl" / "releases"
    if archive == "unrecoverable":
        archive_release(releases, "0.1.0", None, unrecoverable=True)
    elif archive == "recorded":
        archive_release(releases, "0.1.0", release_sha,
                        tree=git(root, "rev-parse", f"{release_sha}^{{tree}}"))
    elif archive == "never-released":
        archive_release(releases, "0.1.0", None, never_released=True)
    elif archive == "unsettled":
        releases.mkdir(parents=True, exist_ok=True)
        path = releases / "v0.1.0.toml"
        path.write_text(
            'format_version = 1\nbump = "minor"\ndescription = "release 0.1.0"\n'
            "include = []\nexclude = []\n"
        )
        os.chmod(path, 0o444)
    if archive is not None:
        _commit_all(root, "archive 0.1.0")
    return release_sha, later_sha


def backfill(root, monkeypatch, *extra, dry_run=False):
    monkeypatch.chdir(root)
    argv = ["release", "backfill", "--approve-consequential", *extra]
    if dry_run:
        argv.insert(0, "--dry-run")
    return rlsbl.app.test(argv)


def supply(root, monkeypatch, sha, *extra, version="0.1.0", dry_run=False):
    return backfill(root, monkeypatch, "--version", version, "--commit", sha,
                    *extra, dry_run=dry_run)


def read_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def is_locked(path):
    mode = os.stat(path).st_mode
    return not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _refused(result, *needles):
    assert result.exit_code == 1, result.stdout + result.stderr
    for needle in needles:
        assert needle in result.stderr, result.stderr


@pytest.fixture
def standalone(tmp_path):
    root = tmp_path / "widget"
    release_sha, later_sha = build_standalone(root)
    return root, release_sha, later_sha


# ---------------------------------------------------------------------------
# The success path
# ---------------------------------------------------------------------------


class TestStandalone:
    def test_the_commit_is_recorded_and_the_mark_cleared(self, standalone, monkeypatch):
        root, release_sha, _later = standalone
        result = supply(root, monkeypatch, release_sha)
        assert result.exit_code == 0, result.stdout + result.stderr

        path = root / ".rlsbl" / "releases" / "v0.1.0.toml"
        data = read_toml(path)
        assert "unrecoverable" not in data
        assert data["candidate_sha"] == release_sha
        assert data["tree_hashes"] == {
            ".": git(root, "rev-parse", f"{release_sha}^{{tree}}"),
        }
        assert data["description"] == "release 0.1.0"
        assert is_locked(path)
        entry = read_entry(str(root / ".rlsbl" / "releases"), "0.1.0", cwd=str(root))
        assert entry.candidate_sha == release_sha

    def test_the_archive_is_committed_as_autogenerated(self, standalone, monkeypatch):
        root, release_sha, _later = standalone
        assert supply(root, monkeypatch, release_sha).exit_code == 0
        assert git(root, "status", "--porcelain") == ""
        body = git(root, "log", "-1", "--format=%B")
        assert "Autogenerated: true" in body
        assert git(root, "log", "-1", "--format=", "--name-only") == (
            ".rlsbl/releases/v0.1.0.toml")

    def test_an_abbreviated_commit_is_recorded_in_full(self, standalone, monkeypatch):
        root, release_sha, _later = standalone
        assert supply(root, monkeypatch, release_sha[:10]).exit_code == 0
        data = read_toml(root / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["candidate_sha"] == release_sha

    def test_the_dry_run_verifies_and_writes_nothing(self, standalone, monkeypatch):
        root, release_sha, later_sha = standalone
        path = root / ".rlsbl" / "releases" / "v0.1.0.toml"
        before = path.read_bytes()
        result = supply(root, monkeypatch, release_sha, dry_run=True)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert release_sha[:12] in result.stdout
        assert path.read_bytes() == before
        # The verification is real under --dry-run: a wrong commit is refused.
        refused = supply(root, monkeypatch, later_sha, dry_run=True)
        _refused(refused, "0.2.0")
        assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# The flags go together
# ---------------------------------------------------------------------------


class TestTheFlagsGoTogether:
    def test_version_without_commit_is_refused(self, standalone, monkeypatch):
        root, _sha, _later = standalone
        result = backfill(root, monkeypatch, "--version", "0.1.0")
        assert result.exit_code != 0
        assert "--commit" in result.stdout + result.stderr

    def test_commit_without_version_is_refused(self, standalone, monkeypatch):
        root, sha, _later = standalone
        result = backfill(root, monkeypatch, "--commit", sha)
        assert result.exit_code != 0
        assert "--version" in result.stdout + result.stderr

    def test_releasable_without_commit_is_refused(self, standalone, monkeypatch):
        root, _sha, _later = standalone
        result = backfill(root, monkeypatch, "--releasable", "core")
        assert result.exit_code != 0
        assert "--commit" in result.stdout + result.stderr

    def test_releasable_in_a_standalone_repository_is_refused(
        self, standalone, monkeypatch,
    ):
        root, sha, _later = standalone
        result = supply(root, monkeypatch, sha, "--releasable", "core")
        _refused(result, "standalone")
        assert supply(root, monkeypatch, sha).exit_code == 0


# ---------------------------------------------------------------------------
# Verification refusals
# ---------------------------------------------------------------------------


class TestTheVersionAtTheCommit:
    def test_a_commit_declaring_another_version_is_refused(
        self, standalone, monkeypatch,
    ):
        root, release_sha, later_sha = standalone
        path = root / ".rlsbl" / "releases" / "v0.1.0.toml"
        before = path.read_bytes()
        result = supply(root, monkeypatch, later_sha)
        _refused(result, "go", "npm", "0.2.0", later_sha[:12])
        assert path.read_bytes() == before
        # The fix: the commit 0.1.0 actually shipped from.
        assert supply(root, monkeypatch, release_sha).exit_code == 0

    def test_one_disagreeing_target_is_named(self, tmp_path, monkeypatch):
        root = tmp_path / "widget"
        build_standalone(root)
        _write_versions(root, go="0.1.0", npm="0.0.9")
        mixed = _commit_all(root, "a commit where only go declares 0.1.0")
        result = supply(root, monkeypatch, mixed)
        _refused(result, "npm", "0.0.9")
        assert "go declares" not in result.stderr

    def test_a_commit_where_a_target_declares_nothing_is_refused(
        self, tmp_path, monkeypatch,
    ):
        root = tmp_path / "widget"
        release_sha, _later = build_standalone(root)
        git(root, "rm", "-q", "npm/package.json")
        missing = _commit_all(root, "drop the npm manifest")
        result = supply(root, monkeypatch, missing)
        _refused(result, "npm", missing[:12])
        assert supply(root, monkeypatch, release_sha).exit_code == 0


class TestTheCommit:
    def test_an_unknown_commit_is_refused(self, standalone, monkeypatch):
        root, release_sha, _later = standalone
        result = supply(root, monkeypatch, "0123456789abcdef0123")
        _refused(result, "0123456789abcdef0123", "release branch's history")
        assert supply(root, monkeypatch, release_sha).exit_code == 0

    def test_a_commit_outside_heads_history_is_refused(self, standalone, monkeypatch):
        root, release_sha, _later = standalone
        dangling = git(
            root, "commit-tree", f"{release_sha}^{{tree}}", "-m", "off to the side",
        )
        result = supply(root, monkeypatch, dangling)
        _refused(result, dangling[:12], "not an ancestor of HEAD",
                 "release branch's history")
        assert supply(root, monkeypatch, release_sha).exit_code == 0


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


class TestTheArchive:
    def test_a_recorded_commit_is_never_overwritten(self, tmp_path, monkeypatch):
        root = tmp_path / "widget"
        release_sha, later_sha = build_standalone(root, archive="recorded")
        path = root / ".rlsbl" / "releases" / "v0.1.0.toml"
        before = path.read_bytes()
        result = supply(root, monkeypatch, release_sha)
        _refused(result, "already records", "never overwritten")
        assert path.read_bytes() == before

    def test_a_never_released_version_is_refused(self, tmp_path, monkeypatch):
        root = tmp_path / "widget"
        release_sha, _later = build_standalone(root, archive="never-released")
        result = supply(root, monkeypatch, release_sha)
        _refused(result, "never_released")

    def test_a_version_with_no_archive_is_refused_until_backfilled(
        self, tmp_path, monkeypatch,
    ):
        root = tmp_path / "widget"
        release_sha, _later = build_standalone(root, archive=None)
        result = supply(root, monkeypatch, release_sha)
        _refused(result, "no archive", "rlsbl release backfill")
        # The fix it names: the plain pass materializes the archive and, with
        # no tag and no version-bump commit, marks it unrecoverable.
        plain = backfill(root, monkeypatch)
        assert plain.exit_code == 0, plain.stdout + plain.stderr
        result = supply(root, monkeypatch, release_sha)
        assert result.exit_code == 0, result.stdout + result.stderr
        data = read_toml(root / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["candidate_sha"] == release_sha

    def test_an_archive_with_no_fate_is_refused_until_backfilled(
        self, tmp_path, monkeypatch,
    ):
        root = tmp_path / "widget"
        release_sha, _later = build_standalone(root, archive="unsettled")
        result = supply(root, monkeypatch, release_sha)
        _refused(result, "not marked unrecoverable", "rlsbl release backfill")
        plain = backfill(root, monkeypatch)
        assert plain.exit_code == 0, plain.stdout + plain.stderr
        result = supply(root, monkeypatch, release_sha)
        assert result.exit_code == 0, result.stdout + result.stderr
        data = read_toml(root / ".rlsbl" / "releases" / "v0.1.0.toml")
        assert data["candidate_sha"] == release_sha


# ---------------------------------------------------------------------------
# A workspace releasable
# ---------------------------------------------------------------------------


def build_workspace(root):
    """Releasable ``core`` (member ``pkgs/core``) shipped 0.1.0 from ``release_sha``."""
    init_repo(root)
    _write(root, "pkgs/core/.rlsbl/config.json",
           '{"publish_mode": "ci", "targets": ["pypi"]}\n')
    _write(root, "pkgs/core/pyproject.toml",
           '[project]\nname = "core"\nversion = "0.1.0"\n')
    state = root / ".rlsbl-monorepo" / "releasables" / "core"
    (state / "releases").mkdir(parents=True)
    _write(root, ".rlsbl-monorepo/releasables/core/changes/unreleased.jsonl", "")
    _changelog(state / "changes", "0.1.0")
    _write(root, ".rlsbl-monorepo/releasables/core/version", "0.1.0\n")
    make_workspace(
        root,
        [{"path": "pkgs/core", "name": "core", "releasable": "core"}],
        releasables=[{"name": "core", "tag_format": "{name}@v{version}"}],
    )
    release_sha = _commit_all(root, "the work core 0.1.0 shipped")
    _write(root, ".rlsbl-monorepo/releasables/core/version", "0.2.0\n")
    _write(root, "pkgs/core/pyproject.toml",
           '[project]\nname = "core"\nversion = "0.2.0"\n')
    later_sha = _commit_all(root, "the work core 0.2.0 will ship")
    archive_release(state / "releases", "0.1.0", None, unrecoverable=True)
    _commit_all(root, "archive core 0.1.0")
    return release_sha, later_sha


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    release_sha, later_sha = build_workspace(root)
    return root, release_sha, later_sha


class TestWorkspaceReleasable:
    def _archive(self, root):
        return root / ".rlsbl-monorepo" / "releasables" / "core" / "releases" / "v0.1.0.toml"

    def test_the_workspace_root_needs_releasable_until_named(
        self, workspace, monkeypatch,
    ):
        root, release_sha, _later = workspace
        result = supply(root, monkeypatch, release_sha)
        _refused(result, "rlsbl release backfill --releasable core")
        result = supply(root, monkeypatch, release_sha, "--releasable", "core")
        assert result.exit_code == 0, result.stdout + result.stderr
        data = read_toml(self._archive(root))
        assert "unrecoverable" not in data
        assert data["candidate_sha"] == release_sha
        assert data["tree_hashes"] == {
            "pkgs/core": git(root, "rev-parse", f"{release_sha}:pkgs/core"),
        }
        assert is_locked(self._archive(root))

    def test_a_member_directory_names_its_releasable(self, workspace, monkeypatch):
        root, release_sha, _later = workspace
        result = supply(root / "pkgs" / "core", monkeypatch, release_sha)
        assert result.exit_code == 0, result.stdout + result.stderr
        assert read_toml(self._archive(root))["candidate_sha"] == release_sha

    def test_a_member_directory_refuses_the_selector(self, workspace, monkeypatch):
        root, release_sha, _later = workspace
        result = supply(root / "pkgs" / "core", monkeypatch, release_sha,
                        "--releasable", "core")
        _refused(result, "--releasable is only accepted at the workspace root")
        assert supply(root / "pkgs" / "core", monkeypatch, release_sha).exit_code == 0

    def test_the_version_file_at_the_commit_must_declare_the_version(
        self, workspace, monkeypatch,
    ):
        root, release_sha, later_sha = workspace
        result = supply(root, monkeypatch, later_sha, "--releasable", "core")
        _refused(result, ".rlsbl-monorepo/releasables/core/version", "0.2.0")
        result = supply(root, monkeypatch, release_sha, "--releasable", "core")
        assert result.exit_code == 0, result.stdout + result.stderr
