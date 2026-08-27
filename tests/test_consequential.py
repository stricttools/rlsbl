"""The `consequential` declaration: which rlsbl commands interrupt you, and which do not.

strictcli prompts ONLY for commands that declare `consequential=True`. The
inferred "mutating => confirm" rule is gone: 63% of the fleet classified
`mutating`, so two thirds of every CLI prompted while the genuinely dangerous
commands were a tenth of that. This file pins BOTH halves of the judgement --
the small set that must ask, and the everyday set that must never ask -- so a
future registration cannot quietly widen or narrow it.
"""

import subprocess
import sys

from rlsbl import app


def _walk(commands, groups, prefix=""):
    for name, cmd in commands.items():
        yield prefix + name, cmd
    for gname, group in groups.items():
        yield from _walk(group.commands, group._groups, f"{prefix}{gname} ")


def _all_commands():
    return dict(_walk(app._commands, app._groups))


# Every command that interrupts the operator, with the act that earns it.
CONSEQUENTIAL = {
    "claim-name":                 "publishes a permanent, unrecoverable name to a public registry",
    "deploy":                     "ships to a live environment",
    "release run":                "tags, pushes and triggers the registry publish",
    "release resume":             "finishes that same push/tag/publish",
    "release retry":              "dispatches the publish workflows",
    "release undo":               "deletes the GitHub Release and the remote tag",
    "release deprecate":          "makes a public statement about a shipped version",
    "release yank":               "removes a published version from public registries",
    "release scrub":              "rewrites history and force-pushes it",
    "release reconcile":          "force-pushes tags and recreates their Releases",
    "monorepo mirror":            "force-pushes the mirror remote",
    "monorepo absorb":            "rewrites another repo's history and merges it in",
    "monorepo release run":       "release run, once per package, in one sweep",
}

# Everyday commands that must NEVER prompt. These are the ones the old
# inferred rule caught, and catching them is what hollowed the guardrail out.
#
# `monorepo extract` and `monorepo extract-releasable` are here deliberately.
# They read as dangerous -- a git filter-repo history rewrite -- but the
# rewrite happens on a throwaway clone the command itself just made at
# target_path. Nothing that anyone has ever pulled is rewritten, nothing is
# pushed, no registry or public artifact is touched, and the source monorepo
# loses only the extracted entries from workspace.toml. The bar is
# unrecoverability, not how heavy the machinery sounds.
MUST_NOT_PROMPT = [
    "commit", "scaffold", "check", "status", "targets", "deploy",
    "changelog add", "changelog generate", "changelog amend", "changelog edit",
    "release init", "release edit",
    "monorepo init", "monorepo add", "monorepo sync", "monorepo snapshot",
    "monorepo cleanup", "monorepo migrate-releasable",
    "monorepo rename-releasable", "monorepo release init",
    "monorepo extract", "monorepo extract-releasable",
    "dev install", "dev sync", "dev status",
    # The rewrite group sweeps the working tree and nothing else. Both
    # commands preview the whole plan under --dry-run, refuse to apply when a
    # count moved, and leave every write revertible with git -- none of which
    # is a reason to interrupt an operator.
    "rewrite go-module-path", "rewrite uv-path-sources",
]
MUST_NOT_PROMPT = [c for c in MUST_NOT_PROMPT if c not in CONSEQUENTIAL]


def test_exactly_these_commands_are_consequential():
    declared = {
        path for path, cmd in _all_commands().items()
        if getattr(cmd, "consequential", False)
    }
    assert declared == set(CONSEQUENTIAL), (
        "the consequential set changed; every entry must name an act worth "
        "interrupting someone for"
    )


def test_no_read_only_command_is_consequential():
    """strictcli rejects it at registration; this pins the intent too."""
    for path, cmd in _all_commands().items():
        if getattr(cmd, "consequential", False):
            assert cmd.effect == "mutating", path


def test_the_everyday_commands_never_prompt():
    for path in MUST_NOT_PROMPT:
        cmd = _all_commands().get(path)
        assert cmd is not None, f"{path} is not a registered command"
        assert not getattr(cmd, "consequential", False), (
            f"{path} must not interrupt the operator: it is ordinary work"
        )


class TestNonInteractiveStdin:
    """A consequential command refuses on a closed stdin, and proceeds with the flag."""

    def _run(self, argv, cwd):
        return subprocess.run(
            [sys.executable, "-P", "-m", "rlsbl", *argv],
            capture_output=True, text=True, cwd=str(cwd), stdin=subprocess.DEVNULL,
        )

    def test_refuses_with_the_pinned_message(self, tmp_path):
        result = self._run(["release", "yank", "1.0.0"], tmp_path)
        assert result.returncode == 1
        assert (
            "error: stdin is not interactive; a consequential command must be "
            "confirmed at a terminal" in result.stderr
        ), result.stderr
        # The refusal names what is required, never the token that lifts it
        # (strictcli contract §12.6, amended at the protocol round).
        assert "--approve-consequential" not in result.stderr, result.stderr

    def test_approve_consequential_gets_past_the_prompt(self, tmp_path):
        result = self._run(
            ["release", "yank", "1.0.0", "--approve-consequential"], tmp_path,
        )
        # Past the gate: it now fails on the project itself, not on stdin.
        assert "stdin is not interactive" not in result.stderr, result.stderr

    def test_a_non_consequential_command_is_not_gated(self, tmp_path):
        result = self._run(["changelog", "generate"], tmp_path)
        assert "stdin is not interactive" not in result.stderr, result.stderr
