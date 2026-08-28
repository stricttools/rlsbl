"""Tests for monorepo subcommand help registry and dispatch."""

import pytest

from rlsbl import app


# All expected monorepo subcommands (release is a subgroup, not listed here)
EXPECTED_SUBCOMMANDS = [
    "init", "add", "remove", "list", "sync",
    "status", "check-names", "outdated", "release",
    "cleanup",
]


class TestMonorepoNoArgs:
    def test_no_args_prints_subcommand_list(self):
        result = app.test(["monorepo"])
        assert "monorepo" in result.stdout.lower()
        for name in EXPECTED_SUBCOMMANDS:
            assert name in result.stdout

    def test_help_flag_prints_subcommand_list(self):
        result = app.test(["monorepo", "--help"])
        assert "monorepo" in result.stdout.lower()
        for name in EXPECTED_SUBCOMMANDS:
            assert name in result.stdout


class TestSubcommandHelp:
    def test_status_help(self):
        result = app.test(["monorepo", "status", "--help"])
        # The column is real JSONL coverage, not an unreleased-commit count.
        assert "Show the current version, last release tag, and changelog coverage" in result.stdout
        assert "number of unreleased commits" not in result.stdout
        assert "rlsbl monorepo status" in result.stdout

    def test_add_help(self):
        result = app.test(["monorepo", "add", "--help"])
        assert "Register a project directory in the monorepo workspace.toml configuration" in result.stdout
        assert "--name" in result.stdout
        assert "--depends-on" in result.stdout
        # There is no --watch: what CI reacts to is derived from the
        # workspace, never declared per project.
        assert "--watch" not in result.stdout
        # There is no --subtree-remote either: the mirror destination is a
        # releasable-level key, declared in workspace.toml.
        assert "--subtree-remote" not in result.stdout

    def test_init_help(self):
        result = app.test(["monorepo", "init", "--help"])
        assert "Create a new monorepo workspace by generating the .rlsbl-monorepo directory" in result.stdout
        assert "rlsbl monorepo init" in result.stdout


class TestUnknownSubcommand:
    def test_unknown_prints_error(self):
        result = app.test(["monorepo", "bogus"])
        assert result.exit_code == 1
        assert "unknown" in result.stderr.lower()
        assert "bogus" in result.stderr


class TestSubcommandsRegistry:
    def test_all_subcommands_are_registered(self):
        """All expected monorepo subcommands should appear in help output."""
        result = app.test(["monorepo", "--help"])
        for name in EXPECTED_SUBCOMMANDS:
            assert name in result.stdout, f"subcommand '{name}' missing from help output"
        # Each subcommand should have a description (non-empty text after the name)
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            for name in EXPECTED_SUBCOMMANDS:
                if line.startswith(name):
                    desc = line[len(name):].strip()
                    assert len(desc) > 0, f"subcommand '{name}' has no description"
