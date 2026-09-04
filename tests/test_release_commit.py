"""The release commit: the archived release file's record of what shipped.

An archived ``v{X.Y.Z}.toml`` carries two release commit fields written by the release
flow itself:

* ``candidate_sha`` -- the commit CI verified, which is also the tag's commit;
* ``tree_hashes`` -- one git tree object per released path, keyed by the
  repo-relative path (``"."`` for a whole standalone repository).

They are authoritative: the ``rlsbl-ci-sha`` marker in the GitHub Release body
is their projection for CI to parse, never a second source. The editable
``unreleased.toml`` never carries them -- a hand-authored release commit is refused at
release validation -- and ``release undo`` strips them when it restores the
archive as the editable file.
"""

import dataclasses
import json
import os
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from githarness import record_release

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    validate_no_authored_release_commit,
)
from rlsbl.context import ProjectContext
from rlsbl.errors import ReleaseFileError
from rlsbl.release_file import (
    RELEASE_COMMIT_FIELDS,
    UNRECOVERABLE_FIELD,
    ReleaseConfig,
    read_release_file,
    strip_release_commit,
    unfinalize_release_file,
    write_release_commit,
)

_SHA = "a" * 40
_TREE = "b" * 40

_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / ".strictspec" / "release-file.schema.toml"
)


def _schema_fields():
    with open(_SCHEMA_PATH, "rb") as f:
        schema = tomllib.load(f)
    return set(schema["types"]["ReleaseConfig"]["fields"].keys())


def _write(tmp_path, body, name="release.toml"):
    f = tmp_path / name
    f.write_text(body)
    return str(f)


_BASE = (
    'format_version = 1\nbump = "patch"\ninclude = []\nexclude = []\n'
    'description = "x"\n'
)


# --------------------------------------------------------------------------- #
# The schema-to-code edge
# --------------------------------------------------------------------------- #

class TestSchemaToCodeEdge:
    """Every schema field is bound, and every bound field is in the schema."""

    def test_schema_fields_and_dataclass_fields_agree(self):
        # Every field is spelled the same in code and on disk, marker included:
        # ``UNRECOVERABLE_FIELD`` is the one place the key is stated, and the
        # attribute now carries the same word.
        bound = {f.name for f in dataclasses.fields(ReleaseConfig)}
        assert UNRECOVERABLE_FIELD in bound
        assert bound == _schema_fields()

    def test_release_commit_fields_are_schema_fields(self):
        assert set(RELEASE_COMMIT_FIELDS) <= _schema_fields()

    def test_every_schema_field_binds_a_value(self, tmp_path):
        # One document carrying every declared field; each must arrive bound.
        p = _write(
            tmp_path,
            'format_version = 1\nbump = "prerelease"\ninclude = ["flutter"]\n'
            'exclude = ["npm"]\ndescription = "d"\ncontext = "c"\n'
            'preid = "alpha"\nblog = true\n'
            f'candidate_sha = "{_SHA}"\n'
            '[targets.flutter]\nmode = "ota"\n'
            f'[tree_hashes]\n"." = "{_TREE}"\n',
        )
        cfg = read_release_file(p)
        assert cfg.bump == "prerelease"
        assert cfg.include == ["flutter"]
        assert cfg.exclude == ["npm"]
        assert cfg.description == "d"
        assert cfg.context == "c"
        assert cfg.preid == "alpha"
        assert cfg.blog is True
        assert cfg.targets == {"flutter": {"mode": "ota"}}
        assert cfg.candidate_sha == _SHA
        assert cfg.tree_hashes == {".": _TREE}


# --------------------------------------------------------------------------- #
# Reading release commits
# --------------------------------------------------------------------------- #

class TestReleaseCommitReading:

    def test_absence_reads_as_absence(self, tmp_path):
        cfg = read_release_file(_write(tmp_path, _BASE))
        assert cfg.candidate_sha is None
        assert cfg.tree_hashes is None

    def test_candidate_sha_binds(self, tmp_path):
        cfg = read_release_file(
            _write(tmp_path, _BASE + f'candidate_sha = "{_SHA}"\n')
        )
        assert cfg.candidate_sha == _SHA

    def test_tree_hashes_bind_per_path(self, tmp_path):
        cfg = read_release_file(
            _write(
                tmp_path,
                _BASE + f'[tree_hashes]\n"packages/core" = "{_TREE}"\n'
                f'"packages/cli" = "{_SHA}"\n',
            )
        )
        assert cfg.tree_hashes == {"packages/core": _TREE, "packages/cli": _SHA}

    def test_non_hash_sha_rejected(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(tmp_path, _BASE + 'candidate_sha = "HEAD"\n'))

    def test_too_short_sha_rejected(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(_write(tmp_path, _BASE + 'candidate_sha = "abc12"\n'))

    def test_non_hex_tree_hash_rejected(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(
                _write(tmp_path, _BASE + f'[tree_hashes]\n"." = "{"z" * 40}"\n')
            )

    def test_tree_hash_must_be_a_string(self, tmp_path):
        with pytest.raises(ReleaseFileError):
            read_release_file(
                _write(tmp_path, _BASE + '[tree_hashes]\n"." = 12\n')
            )


# --------------------------------------------------------------------------- #
# The hand-authored-release commit refusal
# --------------------------------------------------------------------------- #

class TestAuthoredReleaseCommitRefused:

    def test_candidate_sha_in_editable_file_refused(self):
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            candidate_sha=_SHA,
        )
        with pytest.raises(ReleaseValidationError, match="candidate_sha"):
            validate_no_authored_release_commit(cfg)

    def test_tree_hashes_in_editable_file_refused(self):
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            tree_hashes={".": _TREE},
        )
        with pytest.raises(ReleaseValidationError, match="tree_hashes"):
            validate_no_authored_release_commit(cfg)

    def test_both_named_in_one_error(self):
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            candidate_sha=_SHA, tree_hashes={".": _TREE},
        )
        with pytest.raises(ReleaseValidationError) as exc:
            validate_no_authored_release_commit(cfg)
        assert "candidate_sha" in str(exc.value)
        assert "tree_hashes" in str(exc.value)

    def test_release_commitless_config_passes(self):
        cfg = ReleaseConfig(bump="patch", include=[], exclude=[], description="x")
        validate_no_authored_release_commit(cfg)

    def test_empty_tree_hashes_table_is_still_a_release_commit(self):
        # An empty [tree_hashes] table is a present field, not an absent one.
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x", tree_hashes={},
        )
        with pytest.raises(ReleaseValidationError, match="tree_hashes"):
            validate_no_authored_release_commit(cfg)

    def test_unrecoverable_marker_in_editable_file_refused(self):
        # `unrecoverable` is written by the backfill pass onto an ARCHIVE whose
        # commit could not be recovered. On an editable release file it is a
        # hand-authored claim about a version that has not shipped -- refused
        # for the same reason the release commit fields are.
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            unrecoverable=True,
        )
        with pytest.raises(ReleaseValidationError, match=UNRECOVERABLE_FIELD):
            validate_no_authored_release_commit(cfg)

    def test_unrecoverable_false_is_still_an_authored_marker(self):
        # Absence is None; an explicitly written `unrecoverable = false` is a
        # present field and just as much the flow's to author.
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            unrecoverable=False,
        )
        with pytest.raises(ReleaseValidationError, match=UNRECOVERABLE_FIELD):
            validate_no_authored_release_commit(cfg)

    def test_unrecoverable_named_alongside_the_release_commit_fields(self):
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            candidate_sha=_SHA, tree_hashes={".": _TREE}, unrecoverable=True,
        )
        with pytest.raises(ReleaseValidationError) as exc:
            validate_no_authored_release_commit(cfg)
        assert "candidate_sha" in str(exc.value)
        assert "tree_hashes" in str(exc.value)
        assert UNRECOVERABLE_FIELD in str(exc.value)


# --------------------------------------------------------------------------- #
# Writing and stripping
# --------------------------------------------------------------------------- #

class TestWriteAndStrip:

    def test_write_release_commit_appends_both_fields(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        write_release_commit(str(p), candidate_sha=_SHA, tree_hashes={".": _TREE})
        cfg = read_release_file(str(p))
        assert cfg.candidate_sha == _SHA
        assert cfg.tree_hashes == {".": _TREE}
        # The rest of the document survives untouched.
        assert cfg.bump == "patch"
        assert cfg.description == "x"

    def test_write_release_commit_refuses_a_non_hash_sha(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        with pytest.raises(ReleaseFileError):
            write_release_commit(str(p), candidate_sha="HEAD",
                                 tree_hashes={".": _TREE})

    def test_write_release_commit_refuses_an_empty_tree_map(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        with pytest.raises(ReleaseFileError):
            write_release_commit(str(p), candidate_sha=_SHA, tree_hashes={})

    def test_strip_removes_both_fields(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        write_release_commit(str(p), candidate_sha=_SHA, tree_hashes={".": _TREE})
        assert strip_release_commit(str(p)) is True
        with open(p, "rb") as f:
            data = tomllib.load(f)
        for field in RELEASE_COMMIT_FIELDS:
            assert field not in data
        assert data["bump"] == "patch"

    def test_strip_is_a_noop_without_release_commits(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        before = p.read_text()
        assert strip_release_commit(str(p)) is False
        assert p.read_text() == before


# --------------------------------------------------------------------------- #
# The release flow authors the release commit
# --------------------------------------------------------------------------- #

def _git(repo, *args):
    return subprocess.run(
        ["git"] + list(args), cwd=str(repo), check=True,
        capture_output=True, text=True,
    ).stdout.strip()


_RELEASE_FILE = (
    "format_version = 1\n"
    'bump = "patch"\n'
    'include = ["npm"]\n'
    "exclude = []\n"
    'description = "A patch release."\n'
)


def _setup_npm_project(repo, *, release_file=None):
    """A git repo at 1.0.0 with one covered unreleased commit, ready to release."""
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.local")
    _git(repo, "config", "user.name", "Test")

    (repo / "package.json").write_text(
        json.dumps({"name": "test-pkg", "version": "1.0.0"}, indent=2) + "\n"
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.0.0\n\n- Initial release.\n"
    )
    changes_dir = repo / ".rlsbl" / "changes"
    changes_dir.mkdir(parents=True)
    (changes_dir / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n"
    )
    tracked = ["package.json", "CHANGELOG.md",
               ".rlsbl/changes/unreleased.jsonl", ".rlsbl/config.json"]
    if release_file is not None:
        releases = repo / ".rlsbl" / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        (releases / "unreleased.toml").write_text(release_file)
        tracked.append(".rlsbl/releases/unreleased.toml")
    _git(repo, "add", *tracked)
    _git(repo, "commit", "-q", "-m", "initial")
    record_release(repo, "v1.0.0")

    (repo / "feature.txt").write_text("new feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-q", "-m", "add feature")
    feature_sha = _git(repo, "rev-parse", "HEAD")

    (changes_dir / "unreleased.jsonl").write_text(
        json.dumps({
            "commits": [feature_sha],
            "user_facing": True,
            "description": "**Add feature.** New feature is now available.",
            "type": "feature",
        }) + "\n"
    )
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: add feature entry",
         "--trailer", "Autogenerated: true")


def _cover_unreleased_commits(repo, since="v1.0.0"):
    """Give every commit since *since* a non-user-facing changelog entry.

    The changelog commit itself carries the ``Autogenerated: true`` trailer, so
    it is exempt from coverage the way ``rlsbl changelog add`` leaves it.
    """
    commits = _git(repo, "log", "--format=%H", f"{since}..HEAD").split()
    jsonl = repo / ".rlsbl" / "changes" / "unreleased.jsonl"
    with open(jsonl, "a") as f:
        # One entry per commit: a single batched entry would trip the
        # max_commits_per_entry limit once a release's own commits are in it.
        for commit in commits:
            f.write(json.dumps({"commits": [commit], "user_facing": False}) + "\n")
    _git(repo, "add", ".rlsbl/changes/unreleased.jsonl")
    _git(repo, "commit", "-q", "-m", "changelog: cover the undo commits",
         "--trailer", "Autogenerated: true")


def _release(release_config):
    """Run the release flow with only the gh/push boundaries stood in for."""
    from rlsbl.commands.release import run_cmd
    from rlsbl.utils import run as real_run

    def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
        if cmd == "gh":
            return ""
        if cmd == "git" and args and args[0] == "push":
            return ""
        return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

    with (
        patch("rlsbl.commands.release.check_gh_installed", return_value=True),
        patch("rlsbl.commands.release.check_gh_auth", return_value=True),
        patch("rlsbl.commands.release.push_if_needed"),
        patch("rlsbl.commands.release.run_gh", return_value=""),
        patch("rlsbl.commands.release.run", side_effect=fake_run),
    ):
        run_cmd(
            release_config,
            {"quiet": True},
            ctx=ProjectContext(
                project_root=Path("."), workspace_root=None,
                config={"publish_mode": "ci", "pipelines": {}},
            ),
        )


class TestFlowAuthorsTheReleaseCommit:
    """A completed release leaves an archive recorded to the verified commit."""

    def _assert_recorded(self, repo, version="1.0.1"):
        archive = repo / ".rlsbl" / "releases" / f"v{version}.toml"
        assert archive.exists(), "the release file was not archived"
        cfg = read_release_file(str(archive))

        # The release commit names the commit the tag was created on -- the commit CI
        # verified -- and NOT the finalization commits stacked on top of it.
        tagged = _git(repo, "rev-parse", f"v{version}^{{}}")
        assert cfg.candidate_sha == tagged
        assert cfg.candidate_sha != _git(repo, "rev-parse", "HEAD")

        # The tree hash is git's own answer for that commit, not a synthesis.
        assert cfg.tree_hashes == {
            ".": _git(repo, "rev-parse", f"{tagged}^{{tree}}")
        }
        return cfg

    def test_renamed_archive_carries_the_release_commit(self, tmp_project):
        _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A patch release.",
        ))
        cfg = self._assert_recorded(tmp_project)
        # The operator's own fields survived the release-commit write.
        assert cfg.description == "A patch release."
        assert cfg.bump == "patch"

    def test_archive_is_read_only_after_release_commit_recording(self, tmp_project):
        _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A patch release.",
        ))
        archive = tmp_project / ".rlsbl" / "releases" / "v1.0.1.toml"
        assert (archive.stat().st_mode & 0o222) == 0, "the archive stayed writable"

    def test_release_commit_is_committed_not_left_dirty(self, tmp_project):
        _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A patch release.",
        ))
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(tmp_project),
            capture_output=True, text=True, check=True,
        ).stdout
        assert ".rlsbl/releases" not in porcelain, porcelain

    def test_synthesized_archive_carries_the_release_commit(self, tmp_project):
        # No unreleased.toml on disk: the flow synthesizes the archive from the
        # release config (the batch-member path). It release commits it just the same.
        _setup_npm_project(tmp_project)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A synthesized release.",
        ))
        cfg = self._assert_recorded(tmp_project)
        assert cfg.description == "A synthesized release."


class TestMultiMemberReleaseCommitHelper:
    """A multi-member releasable release commits one tree per member path.

    No single git object covers a SET of subtrees, so the archive carries a
    per-member table. Each value must be git's own answer for that path at the
    verified commit -- anything synthesized over the members would be an rlsbl
    invention no git command can reproduce or check.
    """

    def _two_member_repo(self, repo):
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@test.local")
        _git(repo, "config", "user.name", "Test")
        for name, body in (("core", "core code\n"), ("cli", "cli code\n")):
            d = repo / "packages" / name
            d.mkdir(parents=True)
            (d / "main.py").write_text(body)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "initial")
        return _git(repo, "rev-parse", "HEAD")

    def _trees(self, repo, sha, paths):
        from rlsbl.commands.release.execute import release_commit_tree_hashes
        from rlsbl.utils import run

        return release_commit_tree_hashes(
            sha, run=run, git_root=str(repo), member_package_paths=paths,
        )

    def test_one_entry_per_member_equal_to_git_rev_parse(self, tmp_path):
        repo = tmp_path / "repo"
        sha = self._two_member_repo(repo)
        paths = ["packages/core", "packages/cli"]

        trees = self._trees(repo, sha, paths)

        assert trees == {
            p: _git(repo, "rev-parse", f"{sha}:{p}") for p in paths
        }
        assert len(trees) == 2
        # Distinct member content means distinct trees: a single shared value
        # (e.g. the root tree used for every key) would pass a laxer assertion.
        assert trees["packages/core"] != trees["packages/cli"]
        # And neither is the root tree.
        root_tree = _git(repo, "rev-parse", f"{sha}^{{tree}}")
        assert root_tree not in trees.values()

    def test_a_member_path_that_does_not_exist_is_a_hard_error(self, tmp_path):
        from rlsbl.errors import RlsblError

        repo = tmp_path / "repo"
        sha = self._two_member_repo(repo)
        with pytest.raises(RlsblError, match="packages/missing"):
            self._trees(repo, sha, ["packages/core", "packages/missing"])


class TestFlowRefusesAnAuthoredReleaseCommit:

    def test_recorded_editable_file_aborts_the_release(self, tmp_project, capsys):
        _setup_npm_project(
            tmp_project,
            release_file=_RELEASE_FILE + f'candidate_sha = "{_SHA}"\n',
        )
        head_before = _git(tmp_project, "rev-parse", "HEAD")
        with pytest.raises(SystemExit) as exc:
            _release(read_release_file(
                str(tmp_project / ".rlsbl" / "releases" / "unreleased.toml")
            ))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "candidate_sha" in err
        # Refused before any mutation: no version bump commit was made.
        assert _git(tmp_project, "rev-parse", "HEAD") == head_before


def _setup_two_member_releasable_workspace(root):
    """One releasable ('alpha') with TWO member packages, npm targets.

    The shape a per-member release commit table exists for: the release ships
    ``packages/core`` and ``packages/cli`` together under one version.
    """
    from rlsbl.workspace import (
        Releasable,
        get_releasable_changes_dir,
        get_releasable_dir,
        save_workspace,
        write_releasable_version,
    )
    from conftest import with_root_member
    from test_batch_main_as_candidate import _write_batch_file

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@test.local")
    _git(root, "config", "user.name", "Test")

    for name in ("core", "cli"):
        d = root / "packages" / name
        d.mkdir(parents=True)
        (d / "package.json").write_text(
            json.dumps({"name": name, "version": "1.0.0"}, indent=2) + "\n"
        )
        (d / "main.js").write_text(f"// {name}\n")

    save_workspace(
        str(root),
        with_root_member([
            {"path": "packages/core", "name": "core", "releasable": "alpha"},
            {"path": "packages/cli", "name": "cli", "releasable": "alpha"},
        ]),
        releasables=[Releasable(name="alpha")],
    )
    write_releasable_version(str(root), "alpha", "1.0.0")
    changes_dir = get_releasable_changes_dir(str(root), "alpha")
    os.makedirs(changes_dir, exist_ok=True)
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write("")
    with open(
        os.path.join(get_releasable_dir(str(root), "alpha"), "config.json"), "w",
    ) as f:
        json.dump({"publish_mode": "ci", "targets": ["npm"], "pipelines": {}}, f)

    _write_batch_file(root, (
        "[releasables.alpha]\n"
        'bump = "patch"\ndescription = "Alpha patch"\n'
        'include = ["npm"]\nexclude = []\n'
    ))

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    record_release(root, "alpha@v1.0.0")

    # One feature commit touching BOTH members, so each member's subtree really
    # moves and the two release commits cannot coincidentally match the old ones.
    for name in ("core", "cli"):
        (root / "packages" / name / "feature.txt").write_text(f"{name} feature\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "alpha: add feature to both members")
    sha = _git(root, "rev-parse", "HEAD")
    with open(os.path.join(changes_dir, "unreleased.jsonl"), "w") as f:
        f.write(json.dumps({
            "commits": [sha],
            "user_facing": True,
            "description": "**Alpha feature.** It works.",
            "type": "feature",
        }) + "\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "changelog: alpha feature")


class TestBatchReleaseCommitsAMultiMemberReleasable:
    """The batch release path release commits a releasable's synthesized archive.

    A releasable batch has no per-member editable release file: the archive is
    synthesized from the workspace-level batch TOML, and it must be recorded
    from the instant it exists -- with one tree entry per member path, since
    the release ships all of them under one version.
    """

    def _archive(self, root, version="1.0.1"):
        from rlsbl.release_file import get_releases_dir
        from rlsbl.workspace import get_releasable_dir

        return os.path.join(
            get_releases_dir(
                releasable_dir=get_releasable_dir(str(root), "alpha"),
            ),
            f"v{version}.toml",
        )

    def test_the_synthesized_archive_carries_one_release_commit_per_member(
        self, tmp_project,
    ):
        from test_batch_main_as_candidate import _run_batch

        _setup_two_member_releasable_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        archive = self._archive(tmp_project)
        assert os.path.isfile(archive), "the releasable's archive is missing"
        cfg = read_release_file(archive)

        verified = _git(tmp_project, "rev-list", "-n", "1", "alpha@v1.0.1")
        assert cfg.candidate_sha == verified, (
            "the release commit must name the commit the tag was created on"
        )

        # One entry per member path, each git's own answer -- not the root
        # tree, and not one value repeated across the members.
        expected = {
            path: _git(tmp_project, "rev-parse", f"{verified}:{path}")
            for path in ("packages/core", "packages/cli")
        }
        assert cfg.tree_hashes == expected
        assert len(set(cfg.tree_hashes.values())) == 2
        root_tree = _git(tmp_project, "rev-parse", f"{verified}^{{tree}}")
        assert root_tree not in cfg.tree_hashes.values()

        # The operator's batch metadata survived the release-commit write.
        assert cfg.description == "Alpha patch"
        assert cfg.bump == "patch"

    def test_the_recorded_archive_is_locked_and_committed(self, tmp_project):
        from test_batch_main_as_candidate import _run_batch

        _setup_two_member_releasable_workspace(tmp_project)
        _run_batch(tmp_project, ci_return=("green", []))

        archive = self._archive(tmp_project)
        assert (os.stat(archive).st_mode & 0o222) == 0, (
            "the recorded archive must be read-only"
        )
        status = _git(tmp_project, "status", "--porcelain")
        assert "v1.0.1.toml" not in status, status


class TestUndoAndReRelease:
    """The whole cycle: release, undo, release again -- each recorded afresh."""

    def _undo(self, repo):
        from rlsbl.commands.undo import run_cmd as undo_run_cmd
        from rlsbl.evidence_gate import (
            Evidence, EvidenceKind, GateResult, Verdict,
        )
        from rlsbl.utils import run as real_run

        def fake_run(cmd, args=None, timeout=120, env=None, cwd=None):
            # No remote in this fixture: the tag pushes are the only calls
            # that need standing in for. Everything else is real git.
            if cmd == "git" and args and args[0] == "push":
                return ""
            return real_run(cmd, args=args, timeout=timeout, env=env, cwd=cwd)

        cleared = GateResult(
            Verdict.CLEARED,
            [Evidence("registry_probe", "npm", EvidenceKind.UNPUBLISHED,
                      "not on npm")],
            "unpublished",
        )
        with (
            patch("rlsbl.commands.undo.check_gh_installed", return_value=True),
            patch("rlsbl.commands.undo.check_gh_auth", return_value=True),
            patch("rlsbl.commands.undo.run_gh", return_value=""),
            patch("rlsbl.commands.undo.run_evidence_gate", return_value=cleared),
            patch("rlsbl.commands.undo.push_if_needed"),
            patch("rlsbl.commands.undo.run", side_effect=fake_run),
        ):
            undo_run_cmd("npm", [], {}, ctx=ProjectContext(
                project_root=Path(str(repo)), workspace_root=None,
                config={"publish_mode": "ci"},
            ))

    def test_undo_strips_the_release_commit_and_the_re_release_re_authors_it(
        self, tmp_project,
    ):
        _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A patch release.",
        ))
        archive = tmp_project / ".rlsbl" / "releases" / "v1.0.1.toml"
        first_release_commit = read_release_file(str(archive)).candidate_sha
        assert first_release_commit

        self._undo(tmp_project)

        # The archive is gone and the editable file is back -- release commit-free, so
        # the next release is not refused by the authored-release commit check.
        assert not archive.exists()
        restored = tmp_project / ".rlsbl" / "releases" / "unreleased.toml"
        with open(restored, "rb") as f:
            data = tomllib.load(f)
        for field in RELEASE_COMMIT_FIELDS:
            assert field not in data, f"{field} survived the undo"
        validate_no_authored_release_commit(read_release_file(str(restored)))

        # Cover the commits the undo itself made (the revert and the audit
        # record), exactly as an operator would before re-releasing.
        _cover_unreleased_commits(tmp_project)

        # Releasing the freed version again re-authors the release commit, at the new
        # candidate commit (the reverts moved history along).
        _release(read_release_file(str(restored)))
        cfg = read_release_file(str(archive))
        tagged = _git(tmp_project, "rev-parse", "v1.0.1^{}")
        assert cfg.candidate_sha == tagged
        assert cfg.candidate_sha != first_release_commit
        assert cfg.tree_hashes == {
            ".": _git(tmp_project, "rev-parse", f"{tagged}^{{tree}}")
        }


class TestUnfinalizeStripsReleaseCommits:
    """`release undo` restores the archive as the editable file, release commit-free.

    Without the strip, the restored ``unreleased.toml`` carries the release commit of
    the release that was just undone, and the next release refuses it.
    """

    def _archive(self, tmp_path, version="1.0.1"):
        releases = tmp_path / "releases"
        releases.mkdir()
        archived = releases / f"v{version}.toml"
        archived.write_text(_BASE)
        write_release_commit(str(archived), candidate_sha=_SHA,
                             tree_hashes={".": _TREE})
        os.chmod(archived, 0o444)
        return str(releases), archived

    def test_restored_editable_file_carries_no_release_commit(self, tmp_path):
        releases, _archived = self._archive(tmp_path)
        changed = unfinalize_release_file(releases, "1.0.1")
        assert changed
        restored = Path(releases) / "unreleased.toml"
        with open(restored, "rb") as f:
            data = tomllib.load(f)
        for field in RELEASE_COMMIT_FIELDS:
            assert field not in data, f"{field} survived the undo"

    def test_restored_file_passes_the_release_validation(self, tmp_path):
        releases, _archived = self._archive(tmp_path)
        unfinalize_release_file(releases, "1.0.1")
        cfg = read_release_file(os.path.join(releases, "unreleased.toml"))
        validate_no_authored_release_commit(cfg)

    def test_restored_file_can_be_re_recorded(self, tmp_path):
        releases, _archived = self._archive(tmp_path)
        unfinalize_release_file(releases, "1.0.1")
        restored = os.path.join(releases, "unreleased.toml")
        write_release_commit(restored, candidate_sha=_TREE,
                             tree_hashes={".": _SHA})
        cfg = read_release_file(restored)
        assert cfg.candidate_sha == _TREE
        assert cfg.tree_hashes == {".": _SHA}
