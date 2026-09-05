"""``rlsbl release backfill`` end to end, through its own command module.

The engine's verdicts are pinned in ``tests/test_release_backfill.py``; this
file pins the command around it -- which repository it works on, that the
preview really writes nothing, that an apply commits what it wrote, and that
both refusals reach the process as a non-zero exit with the remedy printed.
"""

import os
from pathlib import Path

import pytest

from githarness import commit_file, git, init_repo

from conftest import make_workspace
from rlsbl.commands.release_backfill import run_cmd
from rlsbl.context import create_context


def make_repo(tmp_path, name="proj"):
    repo = tmp_path / name
    repo.mkdir()
    init_repo(repo)
    (repo / ".rlsbl" / "releases").mkdir(parents=True)
    (repo / ".rlsbl" / "changes").mkdir(parents=True)
    commit_file(repo, "README.md", "hello\n", "initial")
    commit_file(repo, "a.txt", "a\n", "v0.1.0")
    git(repo, "tag", "v0.1.0")
    changes = repo / ".rlsbl" / "changes"
    (changes / "0.1.0.jsonl").write_text(
        '{"format_version":1,"commits":["%s"],"user_facing":false}\n' % ("0" * 40),
        encoding="utf-8",
    )
    os.chmod(changes / "0.1.0.jsonl", 0o444)
    return repo


def ctx_for(repo):
    return create_context(Path(repo))


def archive(repo):
    return repo / ".rlsbl" / "releases" / "v0.1.0.toml"


class TestTheDryRun:

    def test_it_prints_the_plan_and_writes_nothing(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        run_cmd({"dry-run": True, "auto-commit": True}, ctx=ctx_for(repo))
        out = capsys.readouterr().out
        assert "standalone 0.1.0" in out
        assert "Nothing was written" in out
        assert not archive(repo).exists()

    def test_it_names_what_would_be_written(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        run_cmd({"dry-run": True, "auto-commit": True}, ctx=ctx_for(repo))
        assert "1 archive(s) would be written" in capsys.readouterr().out


class TestTheApply:

    def test_it_writes_the_archive_and_commits_it(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        run_cmd({"dry-run": False, "auto-commit": True}, ctx=ctx_for(repo))
        assert archive(repo).is_file()
        # The archive itself is committed; the fixture's untracked changelog
        # directory is not the command's to commit.
        assert ".rlsbl/releases" not in git(repo, "status", "--porcelain")
        assert "Backfill release archives" in git(repo, "log", "-1", "--pretty=%B")
        assert "wrote .rlsbl/releases/v0.1.0.toml" in capsys.readouterr().out

    def test_no_auto_commit_leaves_the_archive_uncommitted(self, tmp_path):
        repo = make_repo(tmp_path)
        run_cmd({"dry-run": False, "auto-commit": False}, ctx=ctx_for(repo))
        assert archive(repo).is_file()
        assert git(repo, "status", "--porcelain") != ""

    def test_a_second_apply_has_nothing_to_do(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        run_cmd({"dry-run": False, "auto-commit": True}, ctx=ctx_for(repo))
        head = git(repo, "rev-parse", "HEAD")
        capsys.readouterr()

        run_cmd({"dry-run": False, "auto-commit": True}, ctx=ctx_for(repo))

        assert "Nothing to do" in capsys.readouterr().out
        assert git(repo, "rev-parse", "HEAD") == head


class TestTheRefusals:

    def test_an_unexplained_tag_exits_one_with_the_remedies(
        self, tmp_path, capsys,
    ):
        repo = make_repo(tmp_path)
        git(repo, "tag", "milestone-3")
        with pytest.raises(SystemExit) as exc:
            run_cmd({"dry-run": False, "auto-commit": True}, ctx=ctx_for(repo))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "milestone-3" in err
        assert "ADOPT IT AS RELEASED" in err
        assert not archive(repo).exists()

    def test_the_preview_renders_the_whole_plan_and_still_exits_one(
        self, tmp_path, capsys,
    ):
        """Like `release reconcile --plan`: a preview that found a blocker fails.

        Exit 0 here would tell a caller the repository is accounted for when it
        is not -- but the plan is rendered in full first, so the operator sees
        what the apply would have done as well as what stands in its way.
        """
        repo = make_repo(tmp_path)
        git(repo, "tag", "milestone-3")
        with pytest.raises(SystemExit) as exc:
            run_cmd({"dry-run": True, "auto-commit": True}, ctx=ctx_for(repo))
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "tag milestone-3" in captured.out
        assert "1 unexplained tag(s) would refuse the apply" in captured.out
        assert "ADOPT IT AS RELEASED" in captured.err
        assert not archive(repo).exists()

    def test_a_stash_is_reported_by_the_preview_without_refusing_it(
        self, tmp_path, capsys,
    ):
        repo = make_repo(tmp_path)
        (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")
        git(repo, "stash")
        run_cmd({"dry-run": True, "auto-commit": True}, ctx=ctx_for(repo))
        out = capsys.readouterr().out
        assert "stash entry/entries are present" in out
        assert "will refuse the apply" in out

    def test_a_bad_overrides_file_exits_one(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        overrides = tmp_path / "o.toml"
        overrides.write_text('[versions."9.9.9"]\ndescription = "x"\n',
                             encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            run_cmd(
                {"dry-run": True, "auto-commit": True,
                 "overrides": str(overrides)},
                ctx=ctx_for(repo),
            )
        assert exc.value.code == 1
        assert "9.9.9" in capsys.readouterr().err


class TestTheRepositoryItWorksOn:

    def test_a_workspace_is_backfilled_whole(self, tmp_path, capsys):
        """One tag namespace, so one pass over every releasable in it."""
        repo = tmp_path / "ws"
        repo.mkdir()
        init_repo(repo)
        (repo / "pkgs" / "core" / ".rlsbl").mkdir(parents=True)
        (repo / "pkgs" / "core" / ".rlsbl" / "config.json").write_text(
            '{"publish_mode": "ci", "targets": ["pypi"]}\n', encoding="utf-8",
        )
        state = repo / ".rlsbl-monorepo" / "releasables" / "core"
        (state / "changes").mkdir(parents=True)
        (state / "releases").mkdir(parents=True)
        make_workspace(
            repo,
            [{"path": "pkgs/core", "name": "core", "releasable": "core"}],
            releasables=[{"name": "core", "tag_format": "{name}@v{version}"}],
        )
        commit_file(repo, "pkgs/core/thing.py", "x = 1\n", "core@v0.1.0")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "workspace")
        git(repo, "tag", "core@v0.1.0")
        (state / "changes" / "0.1.0.jsonl").write_text(
            '{"format_version":1,"commits":["%s"],"user_facing":false}\n' % ("0" * 40),
            encoding="utf-8",
        )
        os.chmod(state / "changes" / "0.1.0.jsonl", 0o444)

        run_cmd(
            {"dry-run": False, "auto-commit": False},
            ctx=create_context(repo, workspace_root=repo),
        )

        assert (state / "releases" / "v0.1.0.toml").is_file()
        assert "core 0.1.0" in capsys.readouterr().out
