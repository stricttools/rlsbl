"""Every GitHub Release rlsbl creates refuses a missing tag and states its Latest badge.

Two GitHub behaviors, both measured against a real repository:

* ``gh release create`` for a tag the remote does not have CREATES that tag at
  the default branch's head and publishes the Release there, firing
  ``release: published`` for a commit nobody released. With ``--verify-tag``
  the same call is refused ("tag ... doesn't exist in the repo ..., aborting
  due to --verify-tag flag"). So every creation path passes ``--verify-tag``.
* A newly created Release takes the repository's "Latest" badge by default.
  A real release keeps that default, so the newest release shows as Latest. A
  repair (``release reconcile``, the scrub's Release rewrite, ``monorepo
  mirror`` materializing a version) re-creates OLD Releases, and must never
  move the badge onto them: those pass ``--latest=false``.
"""

import subprocess

import pytest

from rlsbl.release_file import write_archived_release_file
from rlsbl.release_publication import create_args, publication

SHA = "a" * 40
TREE = "f" * 40


def _pub(version="1.2.3"):
    return publication(tag=f"v{version}", version=version, candidate_sha=SHA,
                       notes="Notes", notices=())


class TestTheArgvBuilder:

    def test_a_real_release_verifies_the_tag_and_keeps_githubs_latest_default(self):
        args = create_args(_pub(), "notes.md", moves_latest=True)
        assert "--verify-tag" in args
        assert not [a for a in args if a.startswith("--latest")], (
            "a real release keeps GitHub's default, so the newest release "
            "shows as Latest"
        )

    def test_a_repair_verifies_the_tag_and_never_moves_the_badge(self):
        args = create_args(_pub(), "notes.md", moves_latest=False)
        assert "--verify-tag" in args
        assert "--latest=false" in args

    def test_the_latest_choice_is_required(self):
        with pytest.raises(TypeError):
            create_args(_pub(), "notes.md")


class _Gh:
    """Records gh argv; `release view` answers from the set of existing tags."""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.calls = []

    def __call__(self, args, **kwargs):
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["release", "view"]:
            if args[2] in self.existing:
                return "old notes"
            raise subprocess.CalledProcessError(1, "gh release view")
        if args[:2] == ["release", "create"]:
            self.existing.add(args[2])
        return ""

    def creates(self):
        return [c for c in self.calls if c[:2] == ["release", "create"]]


class _Ctx:
    def __init__(self, root):
        self.config = {}
        self.project_root = root
        self.workspace_root = None


class TestTheScrubsReleaseRewrite:
    """The scrub re-creates a moved tag's missing Release: a repair."""

    def _update(self, tmp_path, gh):
        from rlsbl.commands.release_reconcile import update_github_releases

        (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
        return update_github_releases(
            [{"refname": "refs/tags/v1.0.0"}], ctx=_Ctx(str(tmp_path)),
            project_root=str(tmp_path), workspace_projects=None,
            tag_schemes=None, gh=gh, gh_installed=lambda: True,
            gh_auth=lambda: True, extract_entry=lambda _p, _v: "- A change.\n",
        )

    def test_a_release_with_a_recorded_commit(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        write_archived_release_file(
            str(tmp_path / ".rlsbl" / "releases"), "1.0.0", bump="patch",
            include=["spec"], description="d", candidate_sha=SHA,
            tree_hashes={".": TREE},
        )
        gh = _Gh()
        self._update(tmp_path, gh)
        (create,) = gh.creates()
        assert "--verify-tag" in create
        assert "--latest=false" in create

    def test_a_release_with_no_recorded_commit(self, tmp_path, monkeypatch):
        """The markerless path builds its own argv; it gets the same flags."""
        monkeypatch.chdir(tmp_path)
        gh = _Gh()
        self._update(tmp_path, gh)
        (create,) = gh.creates()
        assert "--verify-tag" in create
        assert "--latest=false" in create


class TestTheReconcile:

    def test_a_materialized_release(self, tmp_path, monkeypatch):
        from rlsbl.commands.release_reconcile import (
            Explanations,
            Observation,
            apply_item,
            build_preview,
        )
        from rlsbl.targets.base import BaseTarget
        from rlsbl.targets.refs import ref_context

        monkeypatch.chdir(tmp_path)
        releases = tmp_path / ".rlsbl" / "releases"
        write_archived_release_file(
            str(releases), "1.0.0", bump="patch", include=["spec"],
            description="d", candidate_sha=SHA, tree_hashes={".": TREE},
        )
        refs = {"refs/tags/v1.0.0": SHA, "refs/tags/v1.0.0^{}": SHA}
        preview = build_preview(
            observation=Observation(
                remote_refs=refs, local_refs=refs, releases=frozenset(),
                releases_known=True,
            ),
            explanations=Explanations(), target=BaseTarget(),
            ref_ctx=ref_context(repo_root=str(tmp_path)),
            releases_dir=str(releases),
        )
        gh = _Gh()
        apply_item(
            preview.by_key("release:v1.0.0"), ctx=_Ctx(str(tmp_path)),
            releases_dir=str(releases), changelog_path=None, push_timeout=30,
            gh=gh, log=lambda *_: None,
        )
        (create,) = gh.creates()
        assert "--verify-tag" in create
        assert "--latest=false" in create


class TestTheMirror:

    def _monorepo(self, root):
        root.mkdir(parents=True)
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@t.local"],
            ["config", "user.name", "Test"],
            ["config", "commit.gpgsign", "false"],
        ):
            subprocess.run(["git", *args], cwd=root, check=True)
        member = root / "packages" / "lib"
        member.mkdir(parents=True)
        (member / "index.js").write_text("module.exports = 1;\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=root, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def _publish(self, tmp_path, *, moves_latest):
        from rlsbl import mirror_publication as mp

        root = tmp_path / "mono"
        sha = self._monorepo(root)
        remote = tmp_path / "mirror.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        gh = _Gh()
        mp.publish_version(
            remote=str(remote), root=str(root), subtree_path="packages/lib",
            version="1.0.0", tag="v1.0.0", release_commit_sha=sha, notes="n",
            gh=gh, directory=str(tmp_path), moves_latest=moves_latest,
        )
        (create,) = gh.creates()
        return create

    def test_the_release_flows_mirror_release_is_a_real_release(self, tmp_path):
        create = self._publish(tmp_path, moves_latest=True)
        assert "--verify-tag" in create
        assert not [a for a in create if a.startswith("--latest")]

    def test_monorepo_mirror_materializing_a_version_is_a_repair(self, tmp_path):
        create = self._publish(tmp_path, moves_latest=False)
        assert "--verify-tag" in create
        assert "--latest=false" in create

    def test_the_monorepo_mirror_command_passes_the_repair_flag(self, monkeypatch):
        """``monorepo mirror``'s own call site states it is a repair."""
        from rlsbl.commands.monorepo import mirror_cmd

        seen = {}

        def fake_publish_version(**kwargs):
            seen.update(kwargs)
            return "s", "pushed", "created"

        monkeypatch.setattr(
            "rlsbl.mirror_publication.publish_version", fake_publish_version,
        )

        class Plan:
            state = "missing"
            version = "1.0.0"
            tag = "v1.0.0"
            release_commit_sha = SHA
            notes = ""
            reason = None

        mirror_cmd._apply_tag(Plan(), "remote", ".", "packages/lib",
                              notes_dir=".")
        assert seen["moves_latest"] is False


class TestTheReleaseFlow:
    """``rlsbl release run`` creates a real release: verified, default badge."""

    def _release(self, repo, fake_run_gh):
        from unittest.mock import patch

        from rlsbl.commands.release import run_cmd
        from rlsbl.context import ProjectContext
        from rlsbl.release_file import ReleaseConfig
        from test_post_push_failure_state import _setup_npm_project

        _setup_npm_project(repo)
        with (
            patch("rlsbl.commands.release.check_gh_installed", return_value=True),
            patch("rlsbl.commands.release.check_gh_auth", return_value=True),
            patch("rlsbl.commands.release.validate_gh_push_access"),
            patch("rlsbl.commands.release.validate_branch_and_remote",
                  return_value="main"),
            patch("rlsbl.commands.release.push_if_needed"),
            patch("rlsbl.commands.release.resolve_tag_push_plan",
                  return_value=False),
            patch("rlsbl.commands.release.execute.time.sleep"),
            patch("rlsbl.commands.release.run_gh", side_effect=fake_run_gh),
        ):
            run_cmd(
                ReleaseConfig(bump="patch", include=["npm"], exclude=[],
                              description="test release"),
                {"quiet": True},
                ctx=ProjectContext(
                    project_root=repo, workspace_root=None,
                    config={"publish_mode": "ci", "pipelines": {}},
                ),
            )

    def test_the_release_is_created_verified_with_githubs_latest_default(
        self, mock_git_repo,
    ):
        gh = _Gh()
        self._release(mock_git_repo, gh)
        (create,) = gh.creates()
        assert "--verify-tag" in create
        assert not [a for a in create if a.startswith("--latest")]

    def test_the_manual_creation_hint_verifies_the_tag(self, mock_git_repo, capsys):
        """A failed creation prints the command that creates it by hand. That
        command must refuse a tag the remote lacks, like the release's own, and
        the notes file it names must exist."""
        import os
        import shlex

        def gh(args, **kwargs):
            if args[:2] in (["release", "create"], ["release", "view"]):
                raise subprocess.CalledProcessError(1, "gh release")
            return ""

        with pytest.raises(SystemExit):
            self._release(mock_git_repo, gh)
        err = capsys.readouterr().err
        (line,) = [ln for ln in err.splitlines() if "To create the release:" in ln]
        argv = shlex.split(line.split("To create the release:", 1)[1])
        assert argv[:3] == ["gh", "release", "create"]
        assert "--verify-tag" in argv
        notes = argv[argv.index("--notes-file") + 1]
        assert os.path.isfile(os.path.join(str(mock_git_repo), notes)) or \
            os.path.isfile(notes)
