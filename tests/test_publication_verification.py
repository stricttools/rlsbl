"""The post-publish registry probe: a release must verify its OUTCOME.

Before this check a release verified only its PROCESS -- CI green, tag pushed,
publish workflow dispatched -- and then announced success. A publish job that
concluded without producing an artifact (a skipped matrix leg, a gate that
refused, an upload that 4xx'd into a retry that never happened) therefore ended
as a green release with nothing on the registry, and nothing in the tool ever
said so.

These are the red-publish fixtures: the registry reports the version is not
being served, and the run must exit nonzero naming it -- for a standalone
release, and for every member of a batch.
"""

from unittest.mock import patch

import pytest

from githarness import git

from rlsbl.commands.release import execute
from rlsbl.commands.release.execute import (
    _announce_unverified_publication,
    _probe_publication,
    _verify_publication,
    _verify_publication_members,
)
from rlsbl.publication_probe import PublicationProbeResult, PublicationStatus
from rlsbl.resolved_target import ResolvedTarget
from rlsbl.targets import TARGETS, TargetEntry

from test_batch_main_as_candidate import (  # noqa: E402
    _batch_patches,
    _setup_batch_workspace,
    _setup_releasable_batch_workspace,
)
from rlsbl.commands.monorepo.batch_release import _cmd_batch_release


PUBLISHED = PublicationStatus.PUBLISHED
UNPUBLISHED = PublicationStatus.UNPUBLISHED
UNPROBEABLE = PublicationStatus.UNPROBEABLE


class _FakeRegistry:
    """A probeable target whose answers come from a scripted queue.

    The last scripted status repeats forever, so ``[UNPUBLISHED]`` models a
    registry that never serves the version and ``[UNPUBLISHED, PUBLISHED]``
    models one that is merely slow.
    """

    supports_publication_probe = True

    def __init__(self, name, statuses):
        self.name = name
        self._statuses = list(statuses)
        self.calls = []

    def publication_probe(self, dir_path, version, ctx=None):
        self.calls.append((dir_path, version))
        status = (
            self._statuses.pop(0) if len(self._statuses) > 1
            else self._statuses[0]
        )
        return PublicationProbeResult(
            status=status, registry=self.name, version=version,
            message=f"{self.name}: {status.value}",
        )


class _UnprobeableRegistry:
    """A target with no probe capability at all."""

    supports_publication_probe = False

    def __init__(self, name):
        self.name = name


def _resolved(name, *, path=".", publish_mode="ci"):
    return ResolvedTarget(
        target=TargetEntry(name=name, path=path),
        path=path,
        pipeline=None,
        publish_mode=publish_mode,
        artifact_kind=None,
        primary=True,
    )


def _register(monkeypatch, impl):
    monkeypatch.setitem(TARGETS, impl.name, impl)
    return impl


def _collapse_delays(monkeypatch, attempts=1):
    """Zero out the retry budget so tests never sleep.

    The wired-in call sites pass no ``delays`` of their own, so this has to
    reach them through the module attribute -- which is exactly why
    ``_probe_publication`` reads it at call time instead of binding it as a
    default argument.
    """
    monkeypatch.setattr(
        execute, "_PUBLICATION_PROBE_DELAYS", tuple([0] * attempts),
    )


# ---------------------------------------------------------------------------
# The probe itself
# ---------------------------------------------------------------------------


class TestProbePublication:

    def test_a_served_version_leaves_nothing_missing(self, monkeypatch):
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("fakereg", [PUBLISHED]))

        missing, checked = _probe_publication(
            [_resolved("fakereg")], "1.2.3", None, log=lambda m: None,
        )
        assert missing == []
        assert checked == ["fakereg"]

    def test_an_unserved_version_is_reported_missing(self, monkeypatch):
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("fakereg", [UNPUBLISHED]))

        missing, checked = _probe_publication(
            [_resolved("fakereg")], "1.2.3", None, log=lambda m: None,
        )
        assert missing == ["fakereg"]
        assert checked == ["fakereg"]

    def test_a_slow_registry_is_given_the_retry_budget(self, monkeypatch):
        """A registry that starts serving on a later attempt must pass.

        Registries take seconds to serve a version their publish API already
        accepted, so a single immediate probe would report false absences.
        """
        _collapse_delays(monkeypatch, attempts=4)
        impl = _register(
            monkeypatch,
            _FakeRegistry("fakereg", [UNPUBLISHED, UNPUBLISHED, PUBLISHED]),
        )

        missing, _checked = _probe_publication(
            [_resolved("fakereg")], "1.2.3", None, log=lambda m: None,
        )
        assert missing == []
        assert len(impl.calls) == 3, "the budget must be spent, not skipped"

    def test_the_module_delay_budget_is_read_at_call_time(self, monkeypatch):
        """Monkeypatching the module attribute must reach unparameterised calls.

        Bound as a default argument it would be frozen at import, and every
        wired-in call site (which passes no ``delays``) would sleep for the
        real budget in every test that exercises a release tail.
        """
        _register(monkeypatch, _FakeRegistry("fakereg", [UNPUBLISHED]))
        monkeypatch.setattr(execute, "_PUBLICATION_PROBE_DELAYS", (0,))
        with patch("rlsbl.commands.release.execute.time.sleep") as slept:
            _probe_publication(
                [_resolved("fakereg")], "1.2.3", None, log=lambda m: None,
            )
        slept.assert_not_called()

    def test_publish_suppressed_targets_are_not_probed(self, monkeypatch):
        """publish_mode "none" means nothing was meant to reach a registry."""
        _collapse_delays(monkeypatch)
        impl = _register(monkeypatch, _FakeRegistry("fakereg", [UNPUBLISHED]))

        missing, checked = _probe_publication(
            [_resolved("fakereg", publish_mode="none")], "1.2.3", None,
            log=lambda m: None,
        )
        assert (missing, checked) == ([], [])
        assert impl.calls == []

    def test_an_unprobeable_target_yields_no_verdict(self, monkeypatch):
        """Silence is never converted into a pass or a failure."""
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _UnprobeableRegistry("mute"))

        missing, checked = _probe_publication(
            [_resolved("mute")], "1.2.3", None, log=lambda m: None,
        )
        assert (missing, checked) == ([], [])

    def test_an_inconclusive_probe_is_reported_not_failed(self, monkeypatch):
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("fakereg", [UNPROBEABLE]))
        lines = []

        missing, checked = _probe_publication(
            [_resolved("fakereg")], "1.2.3", None, log=lines.append,
        )
        assert missing == [], "an unreachable probe is not a failed publish"
        assert checked == ["fakereg"]
        assert any("could not be probed" in line for line in lines)


# ---------------------------------------------------------------------------
# Red publish, standalone release
# ---------------------------------------------------------------------------


class TestVerifyPublication:

    def test_a_missing_artifact_exits_nonzero(self, monkeypatch, capsys):
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("fakereg", [UNPUBLISHED]))

        with pytest.raises(SystemExit) as exc:
            _verify_publication(
                [_resolved("fakereg")], "1.2.3", "v1.2.3", None,
                log=lambda m: None,
            )
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "fakereg" in err, "the registry that failed must be named"
        assert "v1.2.3" in err
        assert "not a CI failure" in err, (
            "the operator must not be sent back to re-run CI"
        )
        assert "rlsbl release retry" in err, "a remedy must be named"

    def test_a_served_version_passes_quietly(self, monkeypatch):
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("fakereg", [PUBLISHED]))
        lines = []

        _verify_publication(
            [_resolved("fakereg")], "1.2.3", "v1.2.3", None, log=lines.append,
        )
        assert any("Publication verified" in line for line in lines)

    def test_nothing_probeable_is_not_a_failure(self, monkeypatch):
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _UnprobeableRegistry("mute"))
        lines = []

        _verify_publication(
            [_resolved("mute")], "1.2.3", "v1.2.3", None, log=lines.append,
        )
        assert any("nothing to verify" in line for line in lines)


class TestVerifyPublicationMembers:

    def test_every_member_is_probed_before_anything_is_decided(
        self, monkeypatch, capsys,
    ):
        """One missing artifact must not hide another.

        A batch that half-published has to be reported whole, or the operator
        fixes one member, re-runs, and discovers the next one.
        """
        _collapse_delays(monkeypatch)
        _register(monkeypatch, _FakeRegistry("regA", [UNPUBLISHED]))
        _register(monkeypatch, _FakeRegistry("regB", [UNPUBLISHED]))

        specs = [
            ("alpha", [_resolved("regA")], "1.0.1", "alpha@v1.0.1", None),
            ("beta", [_resolved("regB")], "2.0.1", "beta@v2.0.1", None),
        ]
        with pytest.raises(SystemExit) as exc:
            _verify_publication_members(specs, log=lambda m: None)
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "alpha" in err and "beta" in err
        assert "regA" in err and "regB" in err
        assert "1.0.1" in err and "2.0.1" in err

    def test_each_member_is_probed_for_its_own_version(self, monkeypatch):
        _collapse_delays(monkeypatch)
        a = _register(monkeypatch, _FakeRegistry("regA", [PUBLISHED]))
        b = _register(monkeypatch, _FakeRegistry("regB", [PUBLISHED]))

        specs = [
            ("alpha", [_resolved("regA")], "1.0.1", "alpha@v1.0.1", None),
            ("beta", [_resolved("regB")], "2.0.1", "beta@v2.0.1", None),
        ]
        _verify_publication_members(specs, log=lambda m: None)

        assert [v for _p, v in a.calls] == ["1.0.1"]
        assert [v for _p, v in b.calls] == ["2.0.1"]

    def test_no_specs_is_a_no_op(self):
        _verify_publication_members([], log=lambda m: None)


class TestUnverifiedNotice:

    def test_the_notice_names_the_verification_command(self, capsys):
        _announce_unverified_publication("abc123", log=lambda m: None)
        err = capsys.readouterr().err
        assert "NOT verified" in err
        assert "--no-watch" in err
        assert "rlsbl watch abc123" in err

    def test_the_notice_goes_to_stderr_so_quiet_cannot_swallow_it(self, capsys):
        _announce_unverified_publication("abc123", log=lambda m: None)
        captured = capsys.readouterr()
        assert "NOT verified" in captured.err
        assert "NOT verified" not in captured.out


# ---------------------------------------------------------------------------
# Red publish, batch tail
# ---------------------------------------------------------------------------


def _run_watched_batch(root, *, npm_status, ci_return=("green", [])):
    """Run a batch with --watch, CI green, and a scripted npm registry."""
    probe = _FakeRegistry("npm", [npm_status])

    def fake_probe(_self, dir_path, version, ctx=None):
        return probe.publication_probe(dir_path, version, ctx)

    patches = _batch_patches(ci_return=ci_return) + [
        patch("rlsbl.targets.npm.NpmTarget.publication_probe", fake_probe),
        patch("rlsbl.commands.watch.run_cmd"),
    ]
    for p in patches:
        p.start()
    try:
        _cmd_batch_release(
            {"quiet": True, "watch": True}, project_root=str(root),
        )
    finally:
        for p in patches:
            p.stop()
    return probe


class TestBatchTailVerifiesEveryMember:
    """The batch orchestrator's tail must ask the registry too.

    The batch tail used to watch CI and stop. A batch is precisely where a
    half-published release hides: many packages ship from one candidate commit,
    each through its own publish job, and one job producing nothing looked
    identical to a clean run.
    """

    def test_a_missing_artifact_fails_the_batch(self, tmp_project, monkeypatch,
                                                capsys):
        _collapse_delays(monkeypatch)
        _setup_batch_workspace(tmp_project)

        with pytest.raises(SystemExit) as exc:
            _run_watched_batch(tmp_project, npm_status=UNPUBLISHED)
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "alpha" in err and "beta" in err, (
            "every member whose artifact is missing must be named"
        )
        assert "npm" in err
        # The releases themselves stand: the tags exist and are not rolled back.
        tags = git(tmp_project, "tag", "-l").split()
        assert "alpha@v1.0.1" in tags and "beta@v1.0.1" in tags

    def test_a_served_batch_completes(self, tmp_project, monkeypatch):
        _collapse_delays(monkeypatch)
        _setup_batch_workspace(tmp_project)

        probe = _run_watched_batch(tmp_project, npm_status=PUBLISHED)

        assert sorted(v for _p, v in probe.calls) == ["1.0.1", "1.0.1"], (
            "both members must be probed for their own new version"
        )

    def test_the_releasable_batch_tail_verifies_too(self, tmp_project,
                                                    monkeypatch, capsys):
        """Explicit (releasable) mode shares the tail, so it shares the check."""
        _collapse_delays(monkeypatch)
        _setup_releasable_batch_workspace(tmp_project)

        with pytest.raises(SystemExit) as exc:
            _run_watched_batch(tmp_project, npm_status=UNPUBLISHED)
        assert exc.value.code == 1

        err = capsys.readouterr().err
        assert "alpha" in err and "beta" in err

    def test_a_red_ci_is_not_answered_with_a_registry_report(
        self, tmp_project, monkeypatch,
    ):
        """A failed publish needs no missing-artifact report on top of it."""
        _collapse_delays(monkeypatch)
        _setup_batch_workspace(tmp_project)
        probe = _FakeRegistry("npm", [UNPUBLISHED])

        def fake_probe(_self, dir_path, version, ctx=None):
            return probe.publication_probe(dir_path, version, ctx)

        patches = _batch_patches(ci_return=("green", [])) + [
            patch("rlsbl.targets.npm.NpmTarget.publication_probe", fake_probe),
            patch("rlsbl.commands.watch.run_cmd", side_effect=SystemExit(1)),
        ]
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc:
                _cmd_batch_release(
                    {"quiet": True, "watch": True}, project_root=str(tmp_project),
                )
        finally:
            for p in patches:
                p.stop()

        assert exc.value.code == 1
        assert probe.calls == [], (
            "the registry must not be probed after a red CI verdict"
        )

    def test_no_watch_probes_nothing_and_says_so(self, tmp_project, monkeypatch,
                                                 capsys):
        """--no-watch is an explicit mode, announced -- not a silent skip."""
        _collapse_delays(monkeypatch)
        _setup_batch_workspace(tmp_project)
        probe = _FakeRegistry("npm", [UNPUBLISHED])

        def fake_probe(_self, dir_path, version, ctx=None):
            return probe.publication_probe(dir_path, version, ctx)

        patches = _batch_patches(ci_return=("green", [])) + [
            patch("rlsbl.targets.npm.NpmTarget.publication_probe", fake_probe),
        ]
        for p in patches:
            p.start()
        try:
            _cmd_batch_release({"quiet": True}, project_root=str(tmp_project))
        finally:
            for p in patches:
                p.stop()

        assert probe.calls == [], (
            "without the CI wait the publish job has not run; probing there "
            "would report every release as missing"
        )
        err = capsys.readouterr().err
        assert "NOT verified" in err
        assert "rlsbl watch" in err
