"""Per-axis conformance: check skip-sets and per-target check dispatch.

Two duplications of the target registry lived in the checks:

- ``CHECK_TARGETS`` hand-listed which targets each check applies to, and
  several checks then restated the same names in their own bodies as
  ``supported = {...} & target_names`` or ``if name == "pypi"``;
- the dead-module and circular-dependency checks each dispatched to one
  detector per target name, with the user-facing explanation for each detector
  spelled out at the call site rather than beside the detector.

The scope sets are now derived from protocol properties, the per-target
detectors are protocol methods, and a single-target check reads its scope from
the matrix rather than repeating it.
"""

import pytest

from rlsbl.checks import (
    CHECK_TARGETS,
    check_scope_skip_reason,
    targets_for_check,
)
from rlsbl.targets import (
    TARGETS,
    targets_sharing_workspace_environment,
    targets_with_builtin_tests,
    targets_with_circular_dep_analysis,
    targets_with_dep_floors,
    targets_with_import_analysis,
    targets_with_library_lint,
)
from rlsbl.targets.base import BaseTarget

# The scopes as they were hand-listed before the migration. Deriving them must
# reproduce these exactly: that axis was a refactor, not a scope change.
SCOPES_BEFORE_MIGRATION = {
    "dep-floors": {"pypi", "npm", "go"},
    "deps-unused": {"pypi", "dart", "npm", "go", "maven"},
    "deps-undeclared": {"pypi", "dart", "npm", "go", "maven"},
    "deps-runtime-test-only": {"pypi", "dart", "npm", "go", "maven"},
    "deps-dev-in-lib": {"pypi", "dart", "npm", "go", "maven"},
    "dead-modules": {"pypi", "go", "npm", "dart", "maven"},
    "dead-modules-stale": {"pypi", "go", "npm", "dart", "maven"},
    "circular-deps": {"pypi", "npm", "dart", "maven"},
    "library-lint": {"pypi", "go", "npm", "maven"},
    "test-suite": {"pypi", "go", "npm", "maven"},
}

# Scope WIDENINGS decided since the migration, kept apart from the sets above so
# the migration's own claim -- deriving reproduces the hand-listed scopes
# exactly -- stays checkable, and so every later change to a check's scope has
# to be written down here as the deliberate decision it is.
#
# Flutter: a Flutter app IS Dart sources, so the Dart analysers FlutterTarget
# inherits answer for it. They were pinned back to the base during the
# migration to keep behavior identical; that pin was caution, not design, and
# was removed.
SCOPE_ADDITIONS_SINCE_MIGRATION = {
    "deps-unused": {"flutter"},
    "deps-undeclared": {"flutter"},
    "deps-runtime-test-only": {"flutter"},
    "deps-dev-in-lib": {"flutter"},
    "dead-modules": {"flutter"},
    "dead-modules-stale": {"flutter"},
    "circular-deps": {"flutter"},
}


def expected_scope(check_name):
    """The scope a check should have today: the pre-migration set plus widenings."""
    return SCOPES_BEFORE_MIGRATION[check_name] | SCOPE_ADDITIONS_SINCE_MIGRATION.get(
        check_name, set()
    )


class TestDerivedScopesReproduceTheHandListedOnes:

    @pytest.mark.parametrize("check_name", sorted(SCOPES_BEFORE_MIGRATION))
    def test_scope_is_the_hand_listed_one_plus_the_recorded_widenings(self, check_name):
        assert set(CHECK_TARGETS[check_name]) == expected_scope(check_name)

    def test_each_scope_comes_from_a_protocol_property(self):
        assert CHECK_TARGETS["dep-floors"] == targets_with_dep_floors()
        assert CHECK_TARGETS["deps-unused"] == targets_with_import_analysis()
        assert CHECK_TARGETS["dead-modules"] == targets_with_import_analysis()
        assert CHECK_TARGETS["circular-deps"] == targets_with_circular_dep_analysis()
        assert CHECK_TARGETS["library-lint"] == targets_with_library_lint()
        assert CHECK_TARGETS["test-suite"] == targets_with_builtin_tests()

    def test_go_is_out_of_cycle_detection_because_its_compiler_rejects_cycles(self):
        """The exclusion is now the absence of an override, with the reason
        recorded both there and in CHECK_EXCLUDED_TARGETS."""
        from rlsbl.checks import CHECK_EXCLUDED_TARGETS

        assert not TARGETS["go"].supports_circular_dep_analysis
        assert TARGETS["go"].supports_import_analysis
        assert "go" in CHECK_EXCLUDED_TARGETS["circular-deps"]

    def test_flutter_answers_the_source_analysis_axes_like_dart(self):
        """A Flutter app IS Dart sources, so it answers like Dart.

        FlutterTarget extends DartTarget and inherits its analysers. The
        migration rebound both detectors back to the base so the derived scopes
        would reproduce the hand-listed ones unchanged; that pin was caution,
        not design, and is gone -- Flutter is in scope for import analysis and
        cycle detection exactly as Dart is.
        """
        assert TARGETS["dart"].supports_import_analysis
        assert TARGETS["flutter"].supports_import_analysis
        assert TARGETS["flutter"].supports_circular_dep_analysis
        assert "flutter" in targets_with_import_analysis()
        assert "flutter" in targets_with_circular_dep_analysis()


class TestPerTargetDetectorsAreProtocolMethods:

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_a_target_out_of_scope_finds_nothing_rather_than_erroring(self, name, tmp_path):
        target = TARGETS[name]
        if target.supports_import_analysis:
            return
        assert target.find_dead_modules(str(tmp_path)) == []
        assert target.find_circular_dependencies(str(tmp_path)) == []

    def test_each_detector_supplies_its_own_explanation(self, tmp_path):
        """The reason string moved next to the detector that produces it."""
        from unittest.mock import patch

        with patch(
            "rlsbl.dep_validation.find_dead_go_packages",
            return_value=["internal/foo"],
        ):
            assert TARGETS["go"].find_dead_modules(str(tmp_path)) == [
                ("internal/foo", "internal package not imported outside itself")
            ]

        with patch(
            "rlsbl.dep_validation.find_dead_modules", return_value=["lib/x.py"],
        ):
            assert TARGETS["pypi"].find_dead_modules(str(tmp_path)) == [
                ("lib/x.py", "not imported by any other module")
            ]

    def test_bfs_detectors_still_subtract_declared_exclusions(self, tmp_path):
        from unittest.mock import patch

        with patch(
            "rlsbl.dep_validation.find_dead_npm_modules",
            return_value=["src/a.js", "src/b.js"],
        ):
            found = TARGETS["npm"].find_dead_modules(
                str(tmp_path), suppress={"src/b.js"},
            )
        assert [p for p, _r in found] == ["src/a.js"]

    def test_union_detectors_still_receive_the_suppress_set(self, tmp_path):
        """Threading suppress into the detector is what prevents laundering."""
        from unittest.mock import patch

        with patch(
            "rlsbl.dep_validation.find_dead_modules", return_value=[],
        ) as detector:
            TARGETS["pypi"].find_dead_modules(str(tmp_path), suppress={"lib/x.py"})
        assert detector.call_args.kwargs["suppress"] == {"lib/x.py"}


class TestSingleTargetChecksReadTheMatrix:

    @pytest.mark.parametrize(
        "check_name", ["ruff-lint", "maven-central-metadata", "dunder-version-missing"],
    )
    def test_scope_is_answerable_from_the_matrix(self, check_name):
        assert targets_for_check(check_name)

    def test_skip_reason_names_the_supported_targets(self):
        reason = check_scope_skip_reason("ruff-lint", {"zig"})
        assert reason is not None
        assert "pypi" in reason

    def test_no_skip_when_the_target_is_in_scope(self):
        assert check_scope_skip_reason("ruff-lint", {"zig", "pypi"}) is None

    def test_a_universal_check_has_no_target_set(self):
        with pytest.raises(ValueError, match="not target-specific"):
            targets_for_check("lock")

    def test_a_workspace_check_has_no_target_set(self):
        with pytest.raises(ValueError, match="not target-specific"):
            targets_for_check("workspace-ci-router")

    def test_an_unregistered_check_is_a_hard_error(self):
        with pytest.raises(KeyError, match="not in CHECK_TARGETS"):
            targets_for_check("no-such-check")


class TestWorkspaceEnvironmentSharing:
    """The uv-workspace questions ask a property, not the target name."""

    def test_only_pypi_shares_one_resolved_environment(self):
        assert targets_sharing_workspace_environment() == {"pypi"}

    @pytest.mark.parametrize("name", sorted(TARGETS))
    def test_the_property_is_declared_on_every_target(self, name):
        assert isinstance(TARGETS[name].shares_workspace_environment, bool)


class TestCompanionTagsComeFromTheTarget:
    """The go-companion-tags check asks companion_tags, not the target name."""

    def test_go_supplies_the_module_proxy_tag(self):
        assert TARGETS["go"].companion_tags("pkg", "1.2.3", path="packages/pkg") == [
            "packages/pkg/v1.2.3"
        ]

    def test_a_trailing_slash_does_not_double_up(self):
        assert TARGETS["go"].companion_tags("pkg", "1.2.3", path="packages/pkg/") == [
            "packages/pkg/v1.2.3"
        ]

    def test_go_has_none_outside_a_monorepo(self):
        assert TARGETS["go"].companion_tags("pkg", "1.2.3") == []

    @pytest.mark.parametrize("name", sorted(n for n in TARGETS if n != "go"))
    def test_no_other_target_claims_companion_tags(self, name):
        assert TARGETS[name].companion_tags("pkg", "1.2.3", path="packages/pkg") == []
        assert type(TARGETS[name]).companion_tags is BaseTarget.companion_tags
