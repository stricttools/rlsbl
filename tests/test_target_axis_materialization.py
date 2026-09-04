"""``release_materialization_policy``: may a reconcile recreate a missing release ref?

``rlsbl release reconcile`` materializes a ref the release record records but
the remote does not carry. For most ecosystems that is a pure repair -- the tag
names a version that was already released, and pushing it publishes nothing new.

For Go it is not. A Go tag IS the published artifact: ``proxy.golang.org``
resolves ``<module path>@<tag>`` and caches the answer permanently. Pushing an
old version's tag under a module path the repository has since CHANGED publishes
that version under the new identity for the first time, and it can never be
withdrawn. Go therefore declares the refusing policy, and the axis is what makes
that a per-target fact the support matrix carries rather than a special case
buried in the reconciler.
"""

from rlsbl.targets import TARGETS
from rlsbl.targets.base import (
    MATERIALIZATION_POLICIES,
    MATERIALIZE_ALWAYS,
    MATERIALIZE_UNLESS_IDENTITY_CHANGED,
    BaseTarget,
)
from rlsbl.targets.introspect import AXIS_NAMES, target_axis_answers


class TestThePolicyIsAPerTargetFact:

    def test_every_target_declares_one_from_the_closed_vocabulary(self):
        for name, target in TARGETS.items():
            assert target.release_materialization_policy in MATERIALIZATION_POLICIES, (
                f"target {name} declares an unknown materialization policy: "
                f"{target.release_materialization_policy!r}"
            )

    def test_the_default_is_to_materialize(self):
        assert BaseTarget().release_materialization_policy == MATERIALIZE_ALWAYS

    def test_go_refuses_an_identity_transition(self):
        assert (
            TARGETS["go"].release_materialization_policy
            == MATERIALIZE_UNLESS_IDENTITY_CHANGED
        )

    def test_go_is_the_only_refusing_target(self):
        refusing = sorted(
            name for name, t in TARGETS.items()
            if t.release_materialization_policy == MATERIALIZE_UNLESS_IDENTITY_CHANGED
        )
        assert refusing == ["go"], (
            "a target whose tags are its published artifact must say so "
            "deliberately; this list is the record of which ones do"
        )


class TestItReachesTheMatrix:

    def test_the_axis_is_registered(self):
        assert "release_materialization_policy" in AXIS_NAMES

    def test_every_target_answers_it(self):
        answers = target_axis_answers()
        for name, row in answers.items():
            assert row["release_materialization_policy"] in MATERIALIZATION_POLICIES, (
                f"{name} answered the axis with {row!r}"
            )

    def test_the_committed_artifact_carries_it(self):
        import json
        import os

        from rlsbl.targets.introspect import MATRIX_RELPATH

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, MATRIX_RELPATH), encoding="utf-8") as f:
            matrix = json.load(f)
        assert any(
            axis["name"] == "release_materialization_policy"
            for axis in matrix["axes"]
        )
        assert (
            matrix["targets"]["go"]["release_materialization_policy"]
            == MATERIALIZE_UNLESS_IDENTITY_CHANGED
        )
