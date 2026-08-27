"""The release anchor: the archived release file's record of what shipped.

An archived ``v{X.Y.Z}.toml`` carries two anchor fields written by the release
flow itself:

* ``candidate_sha`` -- the commit CI verified, which is also the tag's commit;
* ``tree_hashes`` -- one git tree object per released path, keyed by the
  repo-relative path (``"."`` for a whole standalone repository).

They are authoritative: the ``rlsbl-ci-sha`` marker in the GitHub Release body
is their projection for CI to parse, never a second source. The editable
``unreleased.toml`` never carries them -- a hand-authored anchor is refused at
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

from rlsbl.commands.release.validate import (
    ReleaseValidationError,
    validate_no_authored_anchors,
)
from rlsbl.context import ProjectContext
from rlsbl.errors import ReleaseFileError
from rlsbl.release_file import (
    ANCHOR_FIELDS,
    ReleaseConfig,
    read_release_file,
    strip_release_anchor,
    unfinalize_release_file,
    write_release_anchor,
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
        bound = {f.name for f in dataclasses.fields(ReleaseConfig)}
        assert bound == _schema_fields()

    def test_anchor_fields_are_schema_fields(self):
        assert set(ANCHOR_FIELDS) <= _schema_fields()

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
# Reading anchors
# --------------------------------------------------------------------------- #

class TestAnchorReading:

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
# The hand-authored-anchor refusal
# --------------------------------------------------------------------------- #

class TestAuthoredAnchorRefused:

    def test_candidate_sha_in_editable_file_refused(self):
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            candidate_sha=_SHA,
        )
        with pytest.raises(ReleaseValidationError, match="candidate_sha"):
            validate_no_authored_anchors(cfg)

    def test_tree_hashes_in_editable_file_refused(self):
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            tree_hashes={".": _TREE},
        )
        with pytest.raises(ReleaseValidationError, match="tree_hashes"):
            validate_no_authored_anchors(cfg)

    def test_both_named_in_one_error(self):
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x",
            candidate_sha=_SHA, tree_hashes={".": _TREE},
        )
        with pytest.raises(ReleaseValidationError) as exc:
            validate_no_authored_anchors(cfg)
        assert "candidate_sha" in str(exc.value)
        assert "tree_hashes" in str(exc.value)

    def test_anchorless_config_passes(self):
        cfg = ReleaseConfig(bump="patch", include=[], exclude=[], description="x")
        validate_no_authored_anchors(cfg)

    def test_empty_tree_hashes_table_is_still_an_anchor(self):
        # An empty [tree_hashes] table is a present field, not an absent one.
        cfg = ReleaseConfig(
            bump="patch", include=[], exclude=[], description="x", tree_hashes={},
        )
        with pytest.raises(ReleaseValidationError, match="tree_hashes"):
            validate_no_authored_anchors(cfg)


# --------------------------------------------------------------------------- #
# Writing and stripping
# --------------------------------------------------------------------------- #

class TestWriteAndStrip:

    def test_write_release_anchor_appends_both_fields(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        write_release_anchor(str(p), candidate_sha=_SHA, tree_hashes={".": _TREE})
        cfg = read_release_file(str(p))
        assert cfg.candidate_sha == _SHA
        assert cfg.tree_hashes == {".": _TREE}
        # The rest of the document survives untouched.
        assert cfg.bump == "patch"
        assert cfg.description == "x"

    def test_write_release_anchor_refuses_a_non_hash_sha(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        with pytest.raises(ReleaseFileError):
            write_release_anchor(str(p), candidate_sha="HEAD",
                                 tree_hashes={".": _TREE})

    def test_write_release_anchor_refuses_an_empty_tree_map(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        with pytest.raises(ReleaseFileError):
            write_release_anchor(str(p), candidate_sha=_SHA, tree_hashes={})

    def test_strip_removes_both_fields(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        write_release_anchor(str(p), candidate_sha=_SHA, tree_hashes={".": _TREE})
        assert strip_release_anchor(str(p)) is True
        with open(p, "rb") as f:
            data = tomllib.load(f)
        for field in ANCHOR_FIELDS:
            assert field not in data
        assert data["bump"] == "patch"

    def test_strip_is_a_noop_without_anchors(self, tmp_path):
        p = Path(_write(tmp_path, _BASE))
        before = p.read_text()
        assert strip_release_anchor(str(p)) is False
        assert p.read_text() == before


# --------------------------------------------------------------------------- #
# The release flow authors the anchor
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
        releases.mkdir(parents=True)
        (releases / "unreleased.toml").write_text(release_file)
        tracked.append(".rlsbl/releases/unreleased.toml")
    _git(repo, "add", *tracked)
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "tag", "v1.0.0")

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


class TestFlowAuthorsTheAnchor:
    """A completed release leaves an archive anchored to the verified commit."""

    def _assert_anchored(self, repo, version="1.0.1"):
        archive = repo / ".rlsbl" / "releases" / f"v{version}.toml"
        assert archive.exists(), "the release file was not archived"
        cfg = read_release_file(str(archive))

        # The anchor names the commit the tag was created on -- the commit CI
        # verified -- and NOT the finalization commits stacked on top of it.
        tagged = _git(repo, "rev-parse", f"v{version}^{{}}")
        assert cfg.candidate_sha == tagged
        assert cfg.candidate_sha != _git(repo, "rev-parse", "HEAD")

        # The tree hash is git's own answer for that commit, not a synthesis.
        assert cfg.tree_hashes == {
            ".": _git(repo, "rev-parse", f"{tagged}^{{tree}}")
        }
        return cfg

    def test_renamed_archive_carries_the_anchor(self, tmp_project):
        _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A patch release.",
        ))
        cfg = self._assert_anchored(tmp_project)
        # The operator's own fields survived the anchoring.
        assert cfg.description == "A patch release."
        assert cfg.bump == "patch"

    def test_archive_is_read_only_after_anchoring(self, tmp_project):
        _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A patch release.",
        ))
        archive = tmp_project / ".rlsbl" / "releases" / "v1.0.1.toml"
        assert (archive.stat().st_mode & 0o222) == 0, "the archive stayed writable"

    def test_anchor_is_committed_not_left_dirty(self, tmp_project):
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

    def test_synthesized_archive_carries_the_anchor(self, tmp_project):
        # No unreleased.toml on disk: the flow synthesizes the archive from the
        # release config (the batch-member path). It anchors it just the same.
        _setup_npm_project(tmp_project)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A synthesized release.",
        ))
        cfg = self._assert_anchored(tmp_project)
        assert cfg.description == "A synthesized release."


class TestFlowRefusesAnAuthoredAnchor:

    def test_anchored_editable_file_aborts_the_release(self, tmp_project, capsys):
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


class TestUndoAndReRelease:
    """The whole cycle: release, undo, release again -- each anchored afresh."""

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

    def test_undo_strips_the_anchor_and_the_re_release_re_authors_it(
        self, tmp_project,
    ):
        _setup_npm_project(tmp_project, release_file=_RELEASE_FILE)
        _release(ReleaseConfig(
            bump="patch", include=["npm"], exclude=[],
            description="A patch release.",
        ))
        archive = tmp_project / ".rlsbl" / "releases" / "v1.0.1.toml"
        first_anchor = read_release_file(str(archive)).candidate_sha
        assert first_anchor

        self._undo(tmp_project)

        # The archive is gone and the editable file is back -- anchor-free, so
        # the next release is not refused by the authored-anchor check.
        assert not archive.exists()
        restored = tmp_project / ".rlsbl" / "releases" / "unreleased.toml"
        with open(restored, "rb") as f:
            data = tomllib.load(f)
        for field in ANCHOR_FIELDS:
            assert field not in data, f"{field} survived the undo"
        validate_no_authored_anchors(read_release_file(str(restored)))

        # Cover the commits the undo itself made (the revert and the audit
        # record), exactly as an operator would before re-releasing.
        _cover_unreleased_commits(tmp_project)

        # Releasing the freed version again re-authors the anchor, at the new
        # candidate commit (the reverts moved history along).
        _release(read_release_file(str(restored)))
        cfg = read_release_file(str(archive))
        tagged = _git(tmp_project, "rev-parse", "v1.0.1^{}")
        assert cfg.candidate_sha == tagged
        assert cfg.candidate_sha != first_anchor
        assert cfg.tree_hashes == {
            ".": _git(tmp_project, "rev-parse", f"{tagged}^{{tree}}")
        }


class TestUnfinalizeStripsAnchors:
    """`release undo` restores the archive as the editable file, anchor-free.

    Without the strip, the restored ``unreleased.toml`` carries the anchor of
    the release that was just undone, and the next release refuses it.
    """

    def _archive(self, tmp_path, version="1.0.1"):
        releases = tmp_path / "releases"
        releases.mkdir()
        archived = releases / f"v{version}.toml"
        archived.write_text(_BASE)
        write_release_anchor(str(archived), candidate_sha=_SHA,
                             tree_hashes={".": _TREE})
        os.chmod(archived, 0o444)
        return str(releases), archived

    def test_restored_editable_file_carries_no_anchor(self, tmp_path):
        releases, _archived = self._archive(tmp_path)
        changed = unfinalize_release_file(releases, "1.0.1")
        assert changed
        restored = Path(releases) / "unreleased.toml"
        with open(restored, "rb") as f:
            data = tomllib.load(f)
        for field in ANCHOR_FIELDS:
            assert field not in data, f"{field} survived the undo"

    def test_restored_file_passes_the_release_validation(self, tmp_path):
        releases, _archived = self._archive(tmp_path)
        unfinalize_release_file(releases, "1.0.1")
        cfg = read_release_file(os.path.join(releases, "unreleased.toml"))
        validate_no_authored_anchors(cfg)

    def test_restored_file_can_be_re_anchored(self, tmp_path):
        releases, _archived = self._archive(tmp_path)
        unfinalize_release_file(releases, "1.0.1")
        restored = os.path.join(releases, "unreleased.toml")
        write_release_anchor(restored, candidate_sha=_TREE,
                             tree_hashes={".": _SHA})
        cfg = read_release_file(restored)
        assert cfg.candidate_sha == _TREE
        assert cfg.tree_hashes == {".": _SHA}
