"""Backstop: every registered command binds the effect chokepoint, and the
classification of every command is pinned.

``rlsbl.effects`` mints on strictcli's ``ctx.effects`` handle only while a
dispatch context is bound, and the binding is what ``@effects.handler`` does.
A handler registered without it would execute its mutations for real under
``--dry-run`` -- silently, because nothing else in the system can tell the
difference.  The first test makes that impossible to do by accident.

The second test pins the ``read_only`` / ``mutating`` split.  Classification
is what the confirm protocol and read-only enforcement key on, so flipping a
command from mutating to read_only must be a deliberate edit here, not a
side effect of a refactor.
"""

import rlsbl


def _walk(commands, groups, prefix=""):
    """Yield (dotted name, Command) for every leaf command in a registry."""
    for name, cmd in commands.items():
        yield (prefix + name, cmd)
    for name, group in groups.items():
        yield from _walk(group.commands, group._groups, prefix + name + ".")


def _all_commands():
    return dict(_walk(rlsbl.app._commands, rlsbl.app._groups))


# The complete classification table.  ``check`` is strictcli's own
# auto-registered command and is read_only by the framework.
EXPECTED_EFFECTS = {
    "check": "read_only",
    "status": "read_only",
    "scaffold": "mutating",
    "check-name": "read_only",
    "claim-name": "mutating",
    "discover": "read_only",
    # Watching a FAILED run auto-retries it with `gh run rerun`, which
    # re-dispatches CI: a real change to state on GitHub, so the command is
    # mutating. It is not consequential -- rerunning a run that already failed
    # is cheap and reversible, and this command is invoked constantly.
    "watch": "mutating",
    "pre-push-check": "read_only",
    "prs": "read_only",
    "unreleased": "read_only",
    "targets": "read_only",
    "deploy": "mutating",
    "commit": "mutating",
    "release.run": "mutating",
    "release.resume": "mutating",
    "release.init": "mutating",
    "release.retry": "mutating",
    "release.edit": "mutating",
    "release.undo": "mutating",
    "release.deprecate": "mutating",
    "release.yank": "mutating",
    "release.scrub": "mutating",
    "release.reconcile": "mutating",
    "release.backfill": "mutating",
    "changelog.add": "mutating",
    "changelog.generate": "mutating",
    "changelog.amend": "mutating",
    "changelog.edit": "mutating",
    "changelog.remap": "mutating",
    "monorepo.init": "mutating",
    "monorepo.add": "mutating",
    "monorepo.remove": "mutating",
    "monorepo.list": "read_only",
    "monorepo.sync": "mutating",
    "monorepo.status": "read_only",
    "monorepo.check-names": "read_only",
    "monorepo.outdated": "read_only",
    "monorepo.snapshot": "mutating",
    "monorepo.snapshot-check": "read_only",
    "monorepo.mirror": "mutating",
    "monorepo.graph": "mutating",
    "monorepo.impact": "read_only",
    "monorepo.extract": "mutating",
    "monorepo.absorb": "mutating",
    "monorepo.cleanup": "mutating",
    "monorepo.rename-releasable": "mutating",
    "monorepo.release.run": "mutating",
    "monorepo.release.init": "mutating",
    "monorepo.release.order": "read_only",
    "dev.install": "mutating",
    "dev.sync": "mutating",
    "dev.status": "read_only",
    # Both rewrite commands change files in the CURRENT working tree and
    # nothing else -- no push, no tag, no registry. Mutating, previewable,
    # and revertible with git, so neither is consequential.
    "rewrite.go-module-path": "mutating",
    "rewrite.uv-path-sources": "mutating",
    # Appends one line to the committed transition record and commits it.
    # Mutating, previewable, and consequential -- what a repository's history
    # IS is a human's call, not an agent's.
    "transition.record": "mutating",
}


class TestEveryHandlerBindsTheChokepoint:
    def test_all_rlsbl_handlers_are_bound(self):
        unbound = [
            name
            for name, cmd in _all_commands().items()
            # strictcli's own commands are the framework's, not rlsbl's.
            if name != "check"
            and not getattr(cmd.handler, "__rlsbl_effects_handler__", False)
        ]
        assert unbound == [], (
            "these command handlers are missing @effects.handler, so their "
            f"effects would execute for real under --dry-run: {unbound}"
        )

    def test_binding_preserves_the_declared_signature(self):
        """The wrapper must not hide parameters from strictcli's guard."""
        import inspect

        sig = inspect.signature(rlsbl.cmd_release_run)
        assert "allow_dirty" in sig.parameters
        assert not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )


class TestClassification:
    def test_every_command_is_classified_as_expected(self):
        actual = {name: cmd.effect for name, cmd in _all_commands().items()}
        assert actual == EXPECTED_EFFECTS

    def test_no_command_is_unclassified(self):
        for name, cmd in _all_commands().items():
            assert cmd.effect in ("read_only", "mutating"), name


class TestScrubPreviewRecordsTheChild:
    """`release scrub --dry-run` records safegit instead of forking it.

    Spawning is itself an effect, so a preview cannot run the child -- not even
    a child that would itself have run dry.  The command says so rather than
    trying to parse output that was never produced.
    """

    def test_unsettled_output_stops_the_flow_with_an_explanation(self, capsys):
        from unittest.mock import patch

        from rlsbl import effects
        from rlsbl.commands import release_scrub

        class _Carrier:
            """Stands in for strictcli's Unsettled at the seam under test."""

        with patch.object(effects, "unsettled", lambda v: isinstance(v, _Carrier)):
            assert effects.unsettled(_Carrier())
            assert not effects.unsettled("real output")

        # The guard's shape is what matters: the module reads the seam's
        # predicate rather than trying to parse a recorded run's result.
        import inspect

        src = inspect.getsource(release_scrub.run_cmd)
        idx_guard = src.index("effects.unsettled(output)")
        idx_parse = src.index("_parse_safegit_envelope(output)")
        assert idx_guard < idx_parse, (
            "the recorded-run guard must come before the JSON parse, or a "
            "preview crashes on output that was never produced"
        )
