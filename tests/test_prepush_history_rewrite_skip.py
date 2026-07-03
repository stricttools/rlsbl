"""After a history rewrite (scrub), the old remote head no longer resolves
locally, so the pre-push coverage range is empty. That skip must be LOUD and
explicit -- not a silent, accidental pass."""

import subprocess

from rlsbl.commands.pre_push_check import _get_pushed_commits


def _init_repo(repo):
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)


def _commit(repo, name, content):
    (repo / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo, check=True)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    )
    return out.stdout.strip()


class TestHistoryRewriteSkipIsExplicit:
    def test_unresolvable_old_remote_head_prints_loud_skip(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        _commit(repo, "a.txt", "one")
        head = _commit(repo, "b.txt", "two")
        monkeypatch.chdir(repo)

        # The remote head recorded by git is a pre-rewrite SHA that no longer
        # exists in the local object store.
        gone = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        commits = _get_pushed_commits([(head, gone)])

        # Pass/fail semantics unchanged: no commits attributed to this ref
        assert commits == set()

        err = capsys.readouterr().err
        assert "history rewrite detected" in err
        assert "old remote head" in err
        assert "unresolvable" in err
        assert "coverage check skipped" in err

    def test_resolvable_remote_head_stays_quiet(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        base = _commit(repo, "a.txt", "one")
        head = _commit(repo, "b.txt", "two")
        monkeypatch.chdir(repo)

        commits = _get_pushed_commits([(head, base)])
        assert commits == {head}

        err = capsys.readouterr().err
        assert "history rewrite detected" not in err
