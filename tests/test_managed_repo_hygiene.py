"""Managed-repo hygiene: a stash, and a changelog a non-releasable member owns.

Two states a managed repository must not be operated in:

- **A stash.** Uncommitted work with no branch of its own. Nothing in the
  repository records what it belongs to, so an operation that commits,
  rewrites or force-pushes this working tree can neither carry it along nor
  tell that it left it behind. Every such operation asks one shared probe.
- **A non-releasable member carrying its own ``.rlsbl/changes/``.** A member
  that stands outside every releasable has no version, no release and no
  changelog; a changes directory under it is either residue from before the
  conversion or content that belongs to a releasable. The paths that would
  otherwise read it refuse instead.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import capture_all_checks, make_workspace
from githarness import commit_file, git, init_repo

from rlsbl.check_context import WorkspaceCheckContext
from rlsbl.context import ProjectContext
from rlsbl.errors import ChangelogError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _repo_with_stash(tmp_path, name="stashed"):
    repo = tmp_path / name
    repo.mkdir()
    init_repo(repo)
    (repo / ".rlsbl" / "releases").mkdir(parents=True, exist_ok=True)
    (repo / ".rlsbl" / "changes").mkdir(parents=True, exist_ok=True)
    commit_file(repo, "README.md", "hello\n", "initial")
    (repo / "README.md").write_text("uncommitted\n", encoding="utf-8")
    # Bare `git stash`, not `git stash push`: the suite's push guard reads the
    # second token, and a stash is not a push.
    git(repo, "stash")
    return repo


# ---------------------------------------------------------------------------
# The shared probe
# ---------------------------------------------------------------------------


class TestOneSharedStashProbe:
    def test_the_probe_sees_a_stash(self, tmp_path):
        from rlsbl.git_util import stash_entries

        repo = _repo_with_stash(tmp_path)
        assert stash_entries(str(repo))

    def test_the_probe_is_empty_without_one(self, tmp_path):
        from rlsbl.git_util import stash_entries

        repo = tmp_path / "clean"
        repo.mkdir()
        init_repo(repo)
        commit_file(repo, "README.md", "hello\n", "initial")
        assert stash_entries(str(repo)) == []

    def test_every_refusing_operation_reads_the_same_probe(self):
        """The probe has one implementation, and the callers import it."""
        import rlsbl.commands.release.validate as release_validate
        import rlsbl.commands.release_reconcile as reconcile
        import rlsbl.release_backfill as backfill
        from rlsbl.git_util import stash_entries

        assert backfill.stash_entries is stash_entries
        for module in (release_validate, reconcile):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "stash" in source, module.__name__


# ---------------------------------------------------------------------------
# A stash refuses each guarded operation
# ---------------------------------------------------------------------------


class TestStashRefusesTheGuardedOperations:
    def test_release_run_refuses(self, tmp_path, monkeypatch, capsys):
        from rlsbl.commands.release import run_cmd
        from rlsbl.release_file import ReleaseConfig

        repo = _repo_with_stash(tmp_path, name="release-run")
        (repo / "package.json").write_text('{"name": "x", "version": "0.1.0"}\n')
        monkeypatch.chdir(repo)

        config = ReleaseConfig(
            bump="patch", include=["npm"], exclude=[], description="d",
        )
        with pytest.raises(SystemExit):
            run_cmd(
                config, {"quiet": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"publish_mode": "ci", "pipelines": {}},
                ),
            )
        combined = capsys.readouterr()
        assert "stash" in (combined.err + combined.out)

    def test_release_resume_refuses(self, tmp_path, monkeypatch, capsys):
        from rlsbl.commands.release import resume_cmd

        repo = _repo_with_stash(tmp_path, name="release-resume")
        monkeypatch.chdir(repo)

        state = {
            "new_version": "0.1.1", "tag": "v0.1.1", "branch": "main",
            "completed_steps": [],
        }
        with pytest.raises(SystemExit):
            resume_cmd(
                state, {"quiet": True, "skip-lock": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"publish_mode": "ci", "pipelines": {}},
                ),
            )
        combined = capsys.readouterr()
        assert "stash" in (combined.err + combined.out)

    def test_reconcile_apply_refuses(self, tmp_path, monkeypatch, capsys):
        from rlsbl.commands.release_reconcile import run_cmd

        repo = _repo_with_stash(tmp_path, name="reconcile")
        monkeypatch.chdir(repo)

        with pytest.raises(SystemExit):
            run_cmd(
                {"mode": "apply", "quiet": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"publish_mode": "ci", "pipelines": {}},
                ),
            )
        combined = capsys.readouterr()
        assert "stash" in (combined.err + combined.out)

    def test_the_refusal_names_the_drop(self, tmp_path, monkeypatch, capsys):
        from rlsbl.commands.release_reconcile import run_cmd

        repo = _repo_with_stash(tmp_path, name="reconcile-remedy")
        monkeypatch.chdir(repo)

        with pytest.raises(SystemExit):
            run_cmd(
                {"mode": "apply", "quiet": True},
                ctx=ProjectContext(
                    project_root=Path("."), workspace_root=None,
                    config={"publish_mode": "ci", "pipelines": {}},
                ),
            )
        combined = capsys.readouterr()
        assert "git stash drop" in (combined.err + combined.out)

    def test_performing_the_named_remedy_clears_the_refusal(
        self, tmp_path, monkeypatch, capsys
    ):
        """The remedy the refusal names is followed here, not just quoted.

        A refusal is only as good as the way out it offers, so this runs the
        exact command the message prints and asserts the same operation stops
        refusing -- once for the check that reports the stash first, and once
        for the operation that would otherwise have run over it.
        """
        from rlsbl.commands.release_reconcile import run_cmd
        from rlsbl.git_util import stash_entries

        repo = _repo_with_stash(tmp_path, name="stash-remedy")
        monkeypatch.chdir(repo)
        checks = capture_all_checks()

        def _stash_free():
            return checks["stash-free"](ProjectContext(
                project_root=repo, workspace_root=None, config={},
            ))

        def _reconcile_output():
            # Whatever stops the apply, the question here is only whether the
            # stash is still what stops it -- so every failure shape is caught
            # and answered from the output.
            try:
                run_cmd(
                    {"mode": "apply", "quiet": True},
                    ctx=ProjectContext(
                        project_root=Path("."), workspace_root=None,
                        config={"publish_mode": "ci", "pipelines": {}},
                    ),
                )
            except BaseException:  # noqa: BLE001 -- SystemExit included
                pass
            captured = capsys.readouterr()
            return captured.err + captured.out

        refused = _reconcile_output()
        assert "stash" in refused
        assert _stash_free().status == "fail"

        # The remedy, verbatim from the refusal.
        assert "git stash drop" in refused
        git(repo, "stash", "drop")

        assert stash_entries(str(repo)) == []
        assert _stash_free().status == "pass"
        # The reconcile still cannot run in this fixture -- there is no plan to
        # apply -- but the stash is no longer what stops it.
        assert "stash" not in _reconcile_output()


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


class TestStashFreeCheck:
    def test_it_fails_on_a_present_stash(self, tmp_path, monkeypatch):
        repo = _repo_with_stash(tmp_path, name="check-red")
        monkeypatch.chdir(repo)
        checks = capture_all_checks()
        result = checks["stash-free"](ProjectContext(
            project_root=repo, workspace_root=None, config={},
        ))
        assert result.status == "fail"
        assert "stash" in result.message

    def test_it_passes_without_one(self, tmp_path, monkeypatch):
        repo = tmp_path / "check-green"
        repo.mkdir()
        init_repo(repo)
        commit_file(repo, "README.md", "hello\n", "initial")
        monkeypatch.chdir(repo)
        checks = capture_all_checks()
        result = checks["stash-free"](ProjectContext(
            project_root=repo, workspace_root=None, config={},
        ))
        assert result.status == "pass"

    def test_it_is_registered_with_the_project_tag(self):
        import tomllib

        checks_toml = (
            Path(__file__).resolve().parent.parent
            / "rlsbl" / "data" / "checks.toml"
        )
        with open(checks_toml, "rb") as handle:
            meta = tomllib.load(handle)["checks"]["stash-free"]
        assert "project" in meta["tags"]
        assert meta["severity"] == "error"

    def test_it_has_a_documented_row(self):
        docs = (
            Path(__file__).resolve().parent.parent / "docs" / "checks.md"
        ).read_text(encoding="utf-8")
        assert "`stash-free`" in docs


# ---------------------------------------------------------------------------
# A non-releasable member's own changes dir
# ---------------------------------------------------------------------------


def _workspace_with_member_changes(tmp_path, *, releasable):
    """A workspace whose 'tools' member carries its own .rlsbl/changes/."""
    init_repo(tmp_path)
    for sub in ("core", "tools"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / sub / "package.json").write_text(
            '{"name": "%s", "version": "0.1.0"}\n' % sub
        )
    members = [
        {"path": "core", "name": "core", "releasable": "core"},
        {"path": "tools", "name": "tools", **(
            {"releasable": "tools"} if releasable
            else {"dev_only": True, "releasable": False}
        )},
    ]
    make_workspace(
        tmp_path, members,
        releasables=["core", "tools"] if releasable else ["core"],
    )
    changes = tmp_path / "tools" / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")
    return tmp_path


class TestNonReleasableMemberChangesDir:
    def test_hash_enumeration_refuses(self, tmp_path):
        from rlsbl.changelog.files import enumerate_changelog_dirs

        root = _workspace_with_member_changes(tmp_path, releasable=False)
        with pytest.raises(ChangelogError) as exc:
            enumerate_changelog_dirs(str(root), str(root))
        message = str(exc.value)
        assert "tools" in message
        assert ".rlsbl/changes" in message.replace(os.sep, "/")

    def test_hash_enumeration_accepts_a_releasable_member(self, tmp_path):
        from rlsbl.changelog.files import enumerate_changelog_dirs

        root = _workspace_with_member_changes(tmp_path, releasable=True)
        dirs = enumerate_changelog_dirs(str(root), str(root))
        assert any("tools" in d for d in dirs)

    def test_the_named_remedy_clears_the_refusal_on_the_same_repository(
        self, tmp_path
    ):
        """One of the three remedies is performed on the OFFENDING fixture.

        The refusal offers three ways out: delete the directory, move its
        entries into a releasable's changes directory, or give the member a
        `releasable = "<name>"`. This follows the third one on the repository
        that was just refused -- same directory, same entries -- and asserts
        the reading path that refused now answers.
        """
        from rlsbl.changelog.files import enumerate_changelog_dirs

        root = _workspace_with_member_changes(tmp_path, releasable=False)
        with pytest.raises(ChangelogError) as exc:
            enumerate_changelog_dirs(str(root), str(root))
        assert 'releasable = "<name>"' in str(exc.value)

        # The remedy: 'tools' joins a releasable of its own.
        make_workspace(
            root,
            [
                {"path": "core", "name": "core", "releasable": "core"},
                {"path": "tools", "name": "tools", "releasable": "tools"},
            ],
            releasables=["core", "tools"],
        )

        dirs = enumerate_changelog_dirs(str(root), str(root))
        assert any("tools" in d for d in dirs)

    def test_monorepo_status_refuses(self, tmp_path, monkeypatch):
        from rlsbl.commands.monorepo import _cmd_status

        root = _workspace_with_member_changes(tmp_path, releasable=False)
        monkeypatch.chdir(root)
        with pytest.raises((ChangelogError, SystemExit)):
            _cmd_status({}, project_root=".")

    def test_prepush_coverage_refuses(self, tmp_path, monkeypatch):
        from rlsbl.workspace import load_releasables, load_workspace

        root = _workspace_with_member_changes(tmp_path, releasable=False)
        monkeypatch.chdir(root)
        projects = load_workspace(str(root))
        ctx = WorkspaceCheckContext(
            project_root=root, workspace_root=root, config={},
            projects=projects,
            releasables=load_releasables(str(root), projects),
        )
        ctx.push_stdin = "refs/heads/main abc123 refs/heads/main def456\n"
        checks = capture_all_checks()
        with patch(
            "rlsbl.git_util.get_push_changed_files", return_value=["tools/x.py"]
        ):
            result = checks["prepush-changelog-coverage"](ctx)
        assert result.status == "fail"
        assert "tools" in result.message
