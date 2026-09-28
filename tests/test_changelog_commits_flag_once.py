"""`--commits` on the changelog commands is given once, never repeated.

`--commits` is one comma-separated value. strictcli keeps only the last
occurrence of a flag that is not declared repeatable, so
`rlsbl changelog add --commits a --commits b` used to record an entry covering
`b` alone and report success. A repeated `--commits` is refused, naming the
one-flag spelling; that spelling records every hash.
"""

import json
import sys
from unittest.mock import patch

import pytest

import rlsbl
from githarness import commit_file, git, init_repo


def _refuse(argv):
    with patch.object(sys, "argv", ["rlsbl", *argv]):
        rlsbl._refuse_repeated_commits_flag()


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    init_repo(repo)
    (repo / ".rlsbl" / "changes").mkdir(parents=True)
    (repo / ".rlsbl" / "changes" / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["pypi"]}) + "\n"
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "proj"\nversion = "0.1.0"\n'
    )
    commit_file(repo, "a.py", "a = 1\n", "a")
    first = git(repo, "rev-parse", "HEAD")
    commit_file(repo, "b.py", "b = 1\n", "b")
    second = git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    return repo, first, second


class TestARepeatedCommitsFlagIsRefused:
    @pytest.mark.parametrize("command", ["add", "amend", "edit"])
    def test_each_changelog_command_refuses_it(self, command, capsys):
        with pytest.raises(SystemExit) as exc:
            _refuse(["changelog", command, "--commits", "a", "--commits", "b"])

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--commits was given 2 times" in err
        assert "--commits a,b" in err

    def test_the_equals_spelling_counts_too(self, capsys):
        with pytest.raises(SystemExit):
            _refuse(["changelog", "add", "--commits=a", "--commits", "b"])

        assert "--commits a,b" in capsys.readouterr().err

    def test_a_reserved_flag_may_precede_the_command(self, capsys):
        with pytest.raises(SystemExit):
            _refuse([
                "--quiet", "changelog", "add", "--commits", "a",
                "--commits", "b",
            ])

    def test_one_commits_flag_is_left_alone(self):
        _refuse(["changelog", "add", "--commits", "a,b", "--no-user-facing"])

    def test_other_commands_are_left_alone(self):
        _refuse(["status", "--commits", "a", "--commits", "b"])

    def test_the_named_spelling_records_every_hash(self, project, capsys):
        repo, first, second = project
        with pytest.raises(SystemExit):
            _refuse([
                "changelog", "add", "--commits", first, "--commits", second,
                "--no-user-facing", "--no-auto-commit",
            ])
        named = f"--commits {first},{second}"
        assert named in capsys.readouterr().err

        result = rlsbl.app.test([
            "changelog", "add", *named.split(" "),
            "--no-user-facing", "--no-auto-commit",
        ])

        assert result.exit_code == 0, result.stdout + result.stderr
        entry = json.loads(
            (repo / ".rlsbl" / "changes" / "unreleased.jsonl").read_text()
        )
        assert entry["commits"] == [first, second]
