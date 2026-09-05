"""The `consequential` declaration: which rlsbl commands interrupt you, and which do not.

strictcli prompts ONLY for commands that declare `consequential=True`. The
inferred "mutating => confirm" rule is gone: 63% of the fleet classified
`mutating`, so two thirds of every CLI prompted while the commands that could
really hurt you were a tenth of that. This file pins BOTH halves of the
judgement -- the small set that must ask, and the everyday set that must never
ask -- so a future registration cannot quietly widen or narrow it. Every
mutating command must appear in exactly one of the two sets: a registration
pinned by neither, or claimed by both, is a test failure.

THE CRITERION IS HUMAN AUTHORITY, NOT RECOVERABILITY.

`consequential` marks the decisions only a HUMAN gets to make: an agent must
never self-approve one, whatever the cost of undoing it. That is a different
question from how expensive or irreversible the writes are, and the two come
apart in both directions -- a cheap, fully revertible act can still be one
nobody but the operator may authorize, and an expensive but purely local
rewrite of the working tree need not be. Each entry below therefore names the
DECISION being reserved, not the machinery it runs.
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
    "release reconcile":          "force-pushes tags and rewrites their Releases",
    "release backfill":           "reconstructs the record of what this project released",
    "monorepo mirror":            "force-pushes the mirror remote",
    "monorepo absorb":            "rewrites another repo's history and merges it in",
    "monorepo extract":           "deletes the extracted members and the releasable's release state from this repository, and commits that",
    "monorepo release run":       "release run, once per package, in one sweep",
    "monorepo rename-releasable": "declares a repository-history fact, pushes an alias tag to origin, and changes the tag scheme every future release of the releasable uses",
    "transition record":          "declares what this repository's history IS, silencing a reader that would otherwise keep reporting the divergence",
}

# Everyday commands that must NEVER prompt. These are the ones the old
# inferred rule swept up, and sweeping them up is what hollowed the guardrail
# out.
#
# `monorepo extract` used to be here, on the reasoning that the filter-repo
# rewrite happens on a throwaway clone and the source only loses a
# workspace.toml entry. The rebuilt command completes the move: it deletes the
# extracted members' directories and the releasable's whole release state from
# the repository you are standing in and commits that -- moving a releasable
# across a repository boundary is the operator's decision to make, so it earns
# the prompt. The bar is whose decision it is, not how heavy the machinery
# sounds.
MUST_NOT_PROMPT = [
    "commit", "scaffold", "check", "status", "targets",
    # `watch` is mutating because watching a FAILED run re-dispatches it, but
    # that rerun re-attempts a publication the operator already authorized at
    # `release run`. It decides nothing new.
    "watch",
    "changelog add", "changelog generate", "changelog amend", "changelog edit",
    # `remove` joins amend and edit: rewriting the record of an
    # already-authorized release declares nothing new. `remap` repairs stale
    # hashes to match a rewrite that has already happened.
    "changelog remove", "changelog remap",
    "release init", "release edit",
    "monorepo init", "monorepo add", "monorepo sync", "monorepo snapshot",
    "monorepo cleanup",
    # `remove` unregisters a member in workspace.toml and deletes nothing on
    # disk -- the departure of a releasable from the repository is `extract`,
    # which does prompt. `graph` is mutating only because `--output` writes the
    # rendering to the file the caller just named.
    "monorepo remove", "monorepo graph",
    "monorepo release init",
    "dev install", "dev sync", "dev status",
    # The rewrite group sweeps the working tree and nothing else. Renaming a
    # module path or flooring a dependency is the operator's own edit, made in
    # their own checkout, which they are already performing by typing the
    # command -- there is no second decision here that only a human may make.
    "rewrite go-module-path", "rewrite uv-path-sources",
]


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


def test_the_two_sets_are_disjoint():
    """A name in both sets asserts nothing about it and misleads the reader.

    ``deploy`` sat in both for a while, and a filter that dropped the duplicate
    from ``MUST_NOT_PROMPT`` made the contradiction invisible.
    """
    both = sorted(set(CONSEQUENTIAL) & set(MUST_NOT_PROMPT))
    assert not both, (
        f"{both} appear in both CONSEQUENTIAL and MUST_NOT_PROMPT; a command "
        "either interrupts the operator or it does not"
    )


def test_every_mutating_command_is_pinned():
    """Neither half of the judgement may be left unstated.

    A registration that lands in neither set is an unclassified command: it
    inherits whatever the flag happened to be, and no test would notice if that
    flipped. Pinning is therefore mandatory -- add the command to
    ``CONSEQUENTIAL`` with the act that earns the interruption, or to
    ``MUST_NOT_PROMPT`` as ordinary work.
    """
    pinned = set(CONSEQUENTIAL) | set(MUST_NOT_PROMPT)
    unpinned = sorted(
        path for path, cmd in _all_commands().items()
        if cmd.effect == "mutating" and path not in pinned
    )
    assert not unpinned, (
        f"{unpinned} are mutating but pinned by neither set; every mutating "
        "command must be declared in exactly one of them"
    )


def test_every_pinned_name_is_a_registered_command():
    """A stale name pins nothing; it just reads as though it did."""
    registered = set(_all_commands())
    missing = sorted((set(CONSEQUENTIAL) | set(MUST_NOT_PROMPT)) - registered)
    assert not missing, f"{missing} are pinned but not registered commands"


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
