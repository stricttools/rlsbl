"""Every remedy rlsbl prints must name a command that exists and work when run.

Two lies motivated this module. The changelog orphan check told operators to
run ``rlsbl changelog edit --commits <hash> --remove`` -- a flag no command has
ever declared -- and the name-availability command's own usage line named
``rlsbl check``, which is the project-check command and takes no package names
at all. Both read as instructions and neither could be followed.

The tests come in two layers:

- **structural**: every backticked ``rlsbl ...`` invocation in a message names a
  registered command and only flags that command declares;
- **followability**: the remedy is executed as printed and the finding it was
  printed for is gone afterwards.
"""

import json
import os
import re
import shlex
import sys
from unittest import mock

import pytest

import rlsbl
from conftest import make_commit as _make_commit, run_git as _run_git
from githarness import record_release
from rlsbl.changelog.files import get_changes_dir
from rlsbl.changelog.schema import generate_entry_id, parse_jsonl
from rlsbl.changelog.validate import check_no_orphans
from rlsbl.commands.changelog_cmd import cmd_add


# ---------------------------------------------------------------------------
# The structural half: does the invocation name anything real?
# ---------------------------------------------------------------------------

def invocations_in(text):
    """Every backticked ``rlsbl ...`` invocation appearing in *text*."""
    return re.findall(r"`(rlsbl\s[^`]+)`", text)


def _resolve_command(tokens):
    """Walk the app registry to the command *tokens* names, or return None."""
    node = rlsbl.app
    for i, name in enumerate(tokens):
        commands = node._commands if node is rlsbl.app else node.commands
        if name in commands:
            return commands[name], list(tokens[i + 1:])
        if name in node._groups:
            node = node._groups[name]
            continue
        return None, list(tokens[i:])
    return None, []


def _declared_flag_names(command):
    """Every long flag name the command declares, selector payloads included."""
    names = set()
    for member in command.members:
        # A selector carries an election mode; a plain flag has a `choices`
        # attribute too (its enum values), so `elect_by` is what tells them
        # apart.
        if hasattr(member, "elect_by"):
            for choice in member.choices:
                if choice.payload is not None:
                    names.add(choice.payload.name)
                else:
                    names.add(choice.name)
                for sub in choice.members:
                    names.add(sub.name)
        else:
            names.add(member.name)
    return names


# Flags the framework owns on every command; no command declares them.
_RESERVED = {"dry-run", "approve-consequential", "quiet", "verbose", "help", "json"}


def assert_invocation_is_real(invocation):
    """The invocation names a registered command and only declared flags."""
    tokens = shlex.split(invocation)
    assert tokens and tokens[0] == "rlsbl"
    command, rest = _resolve_command(tokens[1:])
    assert command is not None, (
        f"{invocation!r} names no registered rlsbl command"
    )
    declared = _declared_flag_names(command)
    for token in rest:
        if not token.startswith("--"):
            continue
        name = token[2:].split("=", 1)[0]
        if name.startswith("no-"):
            name = name[3:]
        assert name in declared or name in _RESERVED, (
            f"{invocation!r} names --{name}, which `{command.name}` does not "
            f"declare. Declared: {sorted(declared)}"
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def rlsbl_repo(tmp_path, monkeypatch):
    """A git repo with .rlsbl/ scaffolding and a baseline version tag."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@test.local")
    _run_git(repo, "config", "user.name", "Test")

    (repo / "README.md").write_text("# test\n")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-q", "-m", "initial")

    changes = repo / ".rlsbl" / "changes"
    changes.mkdir(parents=True)
    (changes / "unreleased.jsonl").write_text("")
    (repo / ".rlsbl" / "config.json").write_text(
        json.dumps({"publish_mode": "ci", "targets": ["npm"]}) + "\n"
    )
    (repo / "package.json").write_text(
        json.dumps({"name": "demo", "version": "0.1.0"}) + "\n"
    )
    _run_git(repo, "add", ".rlsbl", "package.json")
    _run_git(repo, "commit", "-q", "-m", "scaffold")

    # A tag with no release archive is refused by every release-record read, so
    # the baseline release is RECORDED, not merely tagged.
    record_release(repo, "v0.1.0")
    return repo


def _changes_dir(repo):
    return get_changes_dir(str(repo))


def _releases_dir(repo):
    from rlsbl.release_record import releases_dir_for_changes_dir

    return releases_dir_for_changes_dir(_changes_dir(repo))


def _unreleased_entries(repo):
    return parse_jsonl(os.path.join(_changes_dir(repo), "unreleased.jsonl"))


def _write_entries(repo, entries):
    """Write unreleased.jsonl directly, for lines cmd_add would refuse."""
    path = os.path.join(_changes_dir(repo), "unreleased.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def _add_entry(repo, sha, description="Feature", entry_type="feature"):
    with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
        cmd_add(
            {
                "commits": sha,
                "description": description,
                "type": entry_type,
                "user-facing": True,
                "auto-commit": False,
            },
            project_root=repo,
        )


def _orphan_details(repo):
    entries = _unreleased_entries(repo)
    passed, details = check_no_orphans(entries, _releases_dir(repo))
    return passed, details


# ---------------------------------------------------------------------------
# The changelog orphan check's remedy
# ---------------------------------------------------------------------------

class TestOrphanRemedy:
    """The orphan finding's remedy names real commands and clears the finding.

    A fully orphaned entry (every hash unresolvable) and an effectively
    orphaned one (resolvable but outside the unreleased range) print separate
    messages, and both used to end in the same unreal flag.
    """

    def _fully_orphaned(self, repo):
        """An entry whose only commit is not in this history at all.

        What a rebase or a scrub leaves behind: the line still names a
        40-character SHA and `git rev-parse` no longer answers for it.
        """
        stale = "a1" * 20
        _write_entries(repo, [{
            "id": generate_entry_id(),
            "commits": [stale],
            "user_facing": True,
            "description": "Orphaned feature",
            "type": "feature",
        }])
        return stale

    def _effectively_orphaned(self, repo):
        """An entry whose commit resolves but sits before the release."""
        sha = _make_commit(repo, "old.txt")
        # Record a release AT that commit: the unreleased range starts after
        # it, so the hash resolves but is out of range.
        record_release(repo, "v0.2.0")
        _add_entry(repo, sha, description="Already released feature")
        _make_commit(repo, "new.txt")
        return sha

    def test_fully_orphaned_remedy_is_real(self, rlsbl_repo):
        self._fully_orphaned(rlsbl_repo)
        passed, details = _orphan_details(rlsbl_repo)
        assert not passed
        text = " ".join(details)
        assert invocations_in(text), f"no remedy invocation in {text!r}"
        for invocation in invocations_in(text):
            assert_invocation_is_real(invocation)

    def test_effectively_orphaned_remedy_is_real(self, rlsbl_repo):
        self._effectively_orphaned(rlsbl_repo)
        passed, details = _orphan_details(rlsbl_repo)
        assert not passed
        text = " ".join(details)
        assert invocations_in(text), f"no remedy invocation in {text!r}"
        for invocation in invocations_in(text):
            assert_invocation_is_real(invocation)

    def _drive_removal(self, repo, details):
        """Run the removal invocation the finding printed, exactly as printed."""
        removals = [
            inv for inv in invocations_in(" ".join(details))
            if inv.startswith("rlsbl changelog remove")
        ]
        assert len(removals) == 1, (
            f"expected one removal remedy, got {removals!r}"
        )
        with mock.patch("rlsbl.commands.changelog_cmd.commit_files"):
            result = rlsbl.app.test(shlex.split(removals[0])[1:])
        assert result.exit_code == 0, result.stderr
        return removals[0]

    def test_following_the_fully_orphaned_remedy_clears_it(self, rlsbl_repo):
        self._fully_orphaned(rlsbl_repo)
        _passed, details = _orphan_details(rlsbl_repo)
        self._drive_removal(rlsbl_repo, details)

        passed, details = _orphan_details(rlsbl_repo)
        assert passed, details
        assert _unreleased_entries(rlsbl_repo) == []

    def test_following_the_effectively_orphaned_remedy_clears_it(self, rlsbl_repo):
        self._effectively_orphaned(rlsbl_repo)
        _passed, details = _orphan_details(rlsbl_repo)
        self._drive_removal(rlsbl_repo, details)

        passed, details = _orphan_details(rlsbl_repo)
        assert passed, details

    def test_a_legacy_entry_without_an_id_gets_a_followable_remedy(
        self, rlsbl_repo,
    ):
        """An entry written before ids existed is addressed by its commits.

        The remedy cannot name an id the line does not carry, so it names the
        stale hash instead -- and removal must accept a hash git can no longer
        resolve, which is the whole condition the remedy is printed under.
        """
        stale = "0" * 40
        _write_entries(rlsbl_repo, [{
            "commits": [stale],
            "user_facing": True,
            "description": "Legacy entry",
            "type": "feature",
        }])

        _passed, details = _orphan_details(rlsbl_repo)
        self._drive_removal(rlsbl_repo, details)
        assert _unreleased_entries(rlsbl_repo) == []


# ---------------------------------------------------------------------------
# The name-availability command's usage line
# ---------------------------------------------------------------------------

class TestCheckNameUsageLine:

    def test_the_usage_line_names_check_name(self, capsys):
        from rlsbl.commands.check import run_cmd

        exit_code, payload = run_cmd("npm", [], {})
        assert exit_code == 1
        assert payload == []
        message = capsys.readouterr().err
        usage = re.search(r"Usage: (rlsbl [^\n]+)", message)
        assert usage, message
        # `rlsbl check` runs the project's checks and takes no package names;
        # the command that does is `rlsbl check-name`.
        assert usage.group(1).startswith("rlsbl check-name ")

    def test_the_usage_line_runs_when_its_placeholders_are_filled_in(
        self, capsys,
    ):
        """The printed usage line, with its placeholders filled, is executable.

        Driven through the same argv path the real entry point uses: package
        names are variadic positionals, which strictcli does not parse, so
        ``_extract_variadic_args`` lifts them out of ``sys.argv`` before the
        app ever sees it. Running the usage line through ``app.test`` alone
        would test a path no user takes.
        """
        from rlsbl.commands.check import run_cmd

        run_cmd("npm", [], {})
        usage = re.search(r"Usage: (rlsbl [^\n]+)", capsys.readouterr().err)
        assert usage, "the refusal printed no usage line"

        # Fill the placeholders the usage line declares.
        typed = []
        for token in shlex.split(usage.group(1)):
            if token == "<name>":
                typed.append("somename")
            elif token.startswith("[<") or token == "...]":
                continue  # the optional repeat
            elif token.startswith("<") and "|" in token:
                typed.append(token.strip("<>").split("|")[0])
            else:
                typed.append(token)

        with mock.patch.object(sys, "argv", typed):
            variadic = rlsbl._extract_variadic_args()
            argv = list(sys.argv[1:])

        with mock.patch("rlsbl._variadic_args", variadic), \
                mock.patch("rlsbl.commands.check.run_cmd") as m:
            m.return_value = (0, [])
            result = rlsbl.app.test(argv)

        assert result.exit_code == 0, result.stderr
        assert m.call_args[0][0] == "npm"
        assert m.call_args[0][1] == ["somename"]


# ---------------------------------------------------------------------------
# `rlsbl status`'s release hint
# ---------------------------------------------------------------------------

class TestStatusReleaseHint:

    def test_the_hint_is_a_complete_release_invocation(self, rlsbl_repo, capsys):
        """The hint used to name the bare group, which is not runnable.

        `rlsbl release` is a command GROUP: typed alone it prints the group's
        help. And `release run` requires two negatable booleans with no
        defaults, so even the right subcommand is incomplete without them.
        """
        from rlsbl.commands.status import run_cmd
        from rlsbl.context import create_context

        _make_commit(rlsbl_repo, "later.txt")

        ctx = create_context(rlsbl_repo)
        run_cmd("npm", [], {}, ctx=ctx)
        out = capsys.readouterr().out

        hint = [line for line in out.splitlines() if line.startswith("!")]
        assert hint, out
        invocations = invocations_in(hint[0])
        assert invocations, hint[0]
        for invocation in invocations:
            assert_invocation_is_real(invocation)
        assert "release run" in invocations[0]

        # Every required boolean `release run` declares is answered in one of
        # its two forms. Derived from the declaration, so a newly required
        # flag makes this fail rather than going unmentioned in the hint.
        run = rlsbl.app._groups["release"].commands["run"]
        required_bools = [
            flag.name for flag in run.flags
            if flag.presence == "required" and flag.type is bool
        ]
        assert required_bools
        for name in required_bools:
            assert (
                f"--{name}" in invocations[0]
                or f"--no-{name}" in invocations[0]
            ), f"the hint leaves --{name} unanswered: {invocations[0]!r}"
