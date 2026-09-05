"""The ``capabilities`` frozenset is gone; each axis is derived per target.

Targets used to carry ``capabilities: frozenset[str]`` -- a declaration
sitting beside the code it described, free to disagree with it. It did:

- eight targets implemented ``read_metadata`` without listing it (each one a
  no-op override that only restated the empty default, now deleted);
- two of the strings the set could hold, ``publish`` and ``build_assets``,
  were declared by no target and read by nothing, while the pipelines doc
  claimed pipeline steps were decided by them.

Each axis now has its own derivation, chosen for that axis: whether the class
overrides a method, whether a method actually yields anything, or whether the
shipped templates contain the file. This file pins the derivations, pins that
the answers reproduce what the frozensets declared, and pins that the four
sites deciding whether to run a publication probe consult the derivation
rather than a defaulted attribute read.
"""

import ast
import inspect
import os
import textwrap

import pytest

from rlsbl.targets import TARGETS
from rlsbl.targets.base import (
    CI_TEMPLATE_FILENAME,
    NOT_A_PROJECT_DIR,
    BaseTarget,
)

# The axis -> property mapping, and what each frozenset declared before the
# deletion. The derived answers must reproduce these exactly, with the single
# documented exception of read_metadata (see below).
DECLARED_BEFORE_DELETION = {
    "dart": {"ci_templates", "read_metadata", "read_name"},
    "deno": {"ci_templates", "dev_install", "read_name"},
    "docker": {"ci_templates", "read_name"},
    "flutter": {"ci_templates", "read_metadata", "read_name"},
    "go": {"ci_templates", "dev_install", "publication_probe", "read_name"},
    "hex": {"ci_templates", "dev_install", "read_name"},
    "maven": {"ci_templates", "read_metadata", "read_name"},
    "native-android": {"ci_templates", "read_name"},
    "native-ios": {"ci_templates", "read_name"},
    "npm": {
        "ci_templates", "dev_install", "publication_probe",
        "read_metadata", "read_name",
    },
    "pgdesign": {"ci_templates", "read_name"},
    "plain": set(),
    "pypi": {
        "ci_templates", "dev_install", "publication_probe",
        "read_metadata", "read_name",
    },
    "spec": {"ci_templates", "read_name"},
    "swift": {"ci_templates", "dev_install", "read_name"},
    "swift-apple": {"ci_templates", "read_name"},
    "zig": {"ci_templates", "dev_install", "read_name"},
}

# The one definition of the axis -> property map. tests/test_targets.py used to
# carry a second copy under a different name; it imports this one now.
AXIS_PROPERTIES = {
    "read_name": "supports_read_name",
    "read_metadata": "supports_read_metadata",
    "ci_templates": "provides_ci_templates",
    "dev_install": "supports_dev_install",
    "publication_probe": "supports_publication_probe",
}


def _derived(target):
    return {axis for axis, prop in AXIS_PROPERTIES.items() if getattr(target, prop)}


class TestTheAttributeIsGone:

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_no_target_carries_a_capabilities_attribute(self, name):
        assert not hasattr(TARGETS[name], "capabilities")

    def test_the_base_class_does_not_reintroduce_it(self):
        assert not hasattr(BaseTarget, "capabilities")

    def test_the_registry_covers_every_target_in_the_reference_set(self):
        """The reference set above must not go stale as targets are added."""
        assert set(DECLARED_BEFORE_DELETION) == set(TARGETS)


class TestDerivationReproducesTheDeclarations:

    @pytest.mark.parametrize("name", sorted(DECLARED_BEFORE_DELETION))
    def test_answers_match_what_was_declared(self, name):
        assert _derived(TARGETS[name]) == DECLARED_BEFORE_DELETION[name]

    def test_publish_and_build_assets_were_never_real(self):
        """Neither string was declared by any target or read by any code."""
        for declared in DECLARED_BEFORE_DELETION.values():
            assert "publish" not in declared
            assert "build_assets" not in declared
        assert "publish" not in AXIS_PROPERTIES
        assert "build_assets" not in AXIS_PROPERTIES


class TestEachDerivationIsHonestForItsAxis:

    @pytest.mark.parametrize(
        "prop,method",
        [
            ("supports_publication_probe", "publication_probe"),
            ("supports_read_name", "read_name"),
            ("supports_read_metadata", "read_metadata"),
        ],
    )
    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_override_derived_axes_track_the_override(self, name, prop, method):
        target = TARGETS[name]
        overrides = getattr(type(target), method) is not getattr(BaseTarget, method)
        assert getattr(target, prop) is overrides

    def test_no_target_restates_the_empty_read_metadata_default(self):
        """A no-op override would make supports_read_metadata lie.

        Eight targets used to carry ``def read_metadata(...): return {}`` with
        an ecosystem-specific docstring. Under the derivation those overrides
        would each claim metadata support for a target that reads none, so
        they were deleted; not overriding is the honest statement.
        """
        for name, target in TARGETS.items():
            impl = type(target).read_metadata
            if impl is BaseTarget.read_metadata:
                continue
            source = textwrap.dedent(inspect.getsource(impl))
            tree = ast.parse(source)
            body = [
                node for node in tree.body[0].body
                if not (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                )
            ]
            is_noop = (
                len(body) == 1
                and isinstance(body[0], ast.Return)
                and isinstance(body[0].value, ast.Dict)
                and not body[0].value.keys
            )
            assert not is_noop, (
                f"{name}.read_metadata only restates the empty default; delete "
                f"the override so supports_read_metadata answers honestly"
            )

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_dev_install_is_derived_from_the_specs_not_the_override(self, name):
        """swift-apple inherits the method but resolves to no specs.

        Asked of ``NOT_A_PROJECT_DIR``, which is the directory the property
        itself asks about: the answer is the target's, never the cwd's.
        """
        target = TARGETS[name]
        specs = target.dev_install_command(NOT_A_PROJECT_DIR)
        expected = any(specs.get(mode) is not None for mode in ("global", "venv"))
        assert target.supports_dev_install is expected

    def test_swift_apple_inherits_the_method_and_still_answers_no(self):
        assert (
            type(TARGETS["swift-apple"]).dev_install_command
            is not BaseTarget.dev_install_command
        )
        assert not TARGETS["swift-apple"].supports_dev_install

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_ci_templates_is_derived_from_the_shipped_template(self, name):
        target = TARGETS[name]
        directory = target.template_dir()
        expected = directory is not None and os.path.isfile(
            os.path.join(directory, CI_TEMPLATE_FILENAME)
        )
        assert target.provides_ci_templates is expected

    def test_plain_ships_templates_but_no_ci_template(self):
        """The one target with a template directory and no CI workflow."""
        plain = TARGETS["plain"]
        assert plain.template_dir() is not None
        assert not plain.provides_ci_templates


class TestProbeDecidingSitesConsultTheDerivation:
    """The four sites that decide whether to run a publication probe.

    Two of them (the pipeline pre-publish check and the release's publication
    verification) previously used ``getattr(target, "capabilities",
    frozenset())``. A default there answers "cannot probe" for a target that
    can, which either publishes over an already-published version or drops a
    target out of the post-publish verification set without a word.
    """

    PROBE_SITES = {
        "rlsbl/pipelines/base.py": "probe_before_publish",
        "rlsbl/commands/release/execute.py": "supports_publication_probe",
        "rlsbl/evidence_gate.py": "gather",
        "rlsbl/commands/yank.py": "run_cmd",
    }

    def _source(self, relpath):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, relpath), "r", encoding="utf-8") as f:
            return f.read()

    @pytest.mark.parametrize("relpath", sorted(PROBE_SITES))
    def test_the_site_reads_the_property(self, relpath):
        assert "supports_publication_probe" in self._source(relpath)

    @pytest.mark.parametrize("relpath", sorted(PROBE_SITES))
    def test_the_site_has_no_defaulted_attribute_read(self, relpath):
        """No getattr-with-default and no .get-with-default for this axis."""
        tree = ast.parse(self._source(relpath))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) == 3:
                arg = node.args[1]
                assert not (
                    isinstance(arg, ast.Constant)
                    and arg.value
                    in ("capabilities", "supports_publication_probe")
                ), f"{relpath}: defaulted read of the probe-support axis"
            if isinstance(fn, ast.Attribute) and fn.attr == "get" and len(node.args) == 2:
                arg = node.args[0]
                assert not (
                    isinstance(arg, ast.Constant)
                    and arg.value
                    in ("capabilities", "publication_probe", "supports_publication_probe")
                ), f"{relpath}: defaulted lookup of the probe-support axis"

    def _pipeline(self, target_name):
        from rlsbl.pipelines.base import BasePipeline

        pipeline = BasePipeline(
            name="p", pipeline_type="t", local=False, config={},
        )
        pipeline.target = target_name
        return pipeline

    def test_the_pipeline_site_actually_skips_a_probe_for_a_non_prober(self):
        """A non-prober's registry is never contacted.

        The spy goes on ``BaseTarget.publication_probe`` rather than on zig's
        class, on purpose: the support answer is derived by comparing the two,
        so patching only the subclass would make zig look like a prober and
        the test would be asserting nothing.
        """
        from unittest.mock import patch

        assert not TARGETS["zig"].supports_publication_probe
        with patch.object(BaseTarget, "publication_probe") as probe:
            assert not TARGETS["zig"].supports_publication_probe
            proceed = self._pipeline("zig").probe_before_publish(".", "1.0.0", None)
        assert proceed is True
        probe.assert_not_called()

    def test_the_pipeline_site_probes_for_a_prober(self):
        from unittest.mock import patch

        from rlsbl.publication_probe import PublicationProbeResult, PublicationStatus

        result = PublicationProbeResult(
            status=PublicationStatus.PUBLISHED,
            registry="npm",
            version="1.0.0",
            message="already there",
        )
        with patch.object(
            type(TARGETS["npm"]), "publication_probe", return_value=result,
        ) as probe:
            proceed = self._pipeline("npm").probe_before_publish(".", "1.0.0", None)
        assert proceed is False
        probe.assert_called_once()

    def test_the_release_verification_site_selects_only_probers(self):
        """A target that cannot probe drops out of the verification set.

        Not counted as verified and not reported as missing -- either would be
        an answer the registry never gave.
        """
        from rlsbl.commands.release.execute import _probe_publication
        from rlsbl.resolved_target import ResolvedTarget
        from rlsbl.targets import TargetEntry

        def _resolved(name):
            return ResolvedTarget(
                target=TargetEntry(name=name, path="."),
                path=".",
                pipeline=None,
                publish_mode="ci",
                artifact_kind=None,
                primary=True,
            )

        missing, checked = _probe_publication(
            [_resolved("zig")], "1.0.0", None, log=lambda *_a, **_k: None,
            delays=[0],
        )
        assert checked == []
        assert missing == []
