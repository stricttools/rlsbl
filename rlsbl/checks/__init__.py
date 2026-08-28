"""Project checks registered on the strictcli check system.

Each check is registered via ``@app.error_check("name")`` or
``@app.warn_check("name")`` and receives ``(ctx, reporter)`` where *ctx*
is a :class:`~rlsbl.context.ProjectContext` (or its
:class:`~rlsbl.check_context.WorkspaceCheckContext` subclass) and
*reporter* is an ``ErrorReporter`` or ``WarnReporter``.

Check functions accumulate problems via ``reporter.error(text)`` /
``reporter.warn(text)`` and finalize with ``reporter.passed(msg)``,
``reporter.skipped(reason)``, or ``reporter.found(summary)``.
"""

import os

from ..targets import (
    TARGETS,
    targets_with_builtin_tests,
    targets_with_circular_dep_analysis,
    targets_with_dep_floors,
    targets_with_import_analysis,
    targets_with_library_lint,
)

# Manifest filenames used by workspace-unregistered and workspace-stale-entries
# to detect project directories. Derived from each target's detection_files
# so it stays in sync automatically when new targets are added.
def _all_detection_files():
    files = set()
    for target_cls in TARGETS.values():
        files.update(target_cls.detection_files)
    return tuple(sorted(files))


PROJECT_MANIFESTS = _all_detection_files()

# Universal project indicator: every scaffolded rlsbl project has this file.
RLSBL_CONFIG = os.path.join(".rlsbl", "config.json")


# ---------------------------------------------------------------------------
# Feature support matrix metadata
# ---------------------------------------------------------------------------
#
# Maps each check name to its supported targets. Three kinds of values:
#   None         -- universal (works for any/all targets)
#   frozenset()  -- only works for these specific targets
#   "workspace"  -- workspace-only, target-agnostic (graph/structure checks)
#
# For checks that use import scanners in workspace mode, the value is not a
# hand-listed set at all: it is derived from the targets that implement the
# matching protocol method (targets_with_import_analysis and friends), so the
# scope follows the scanners rather than restating them here.

CHECK_TARGETS: dict[str, frozenset[str] | None | str] = {
    # --- project tag (universal) ---
    "lock": None,
    "version-consistency": None,
    "name-consistency": None,
    "license-consistency": None,
    "description-consistency": None,
    "private-hook-stale": None,
    "config-schema": None,
    "strictspec-certificate-gate": None,
    "stricttest-floor": None,
    # dep-floors reads pyproject/uv.lock, package.json/package-lock.json, and
    # go.mod. Which ecosystems those are is the targets' own answer
    # (supports_dep_floors), not a list restated here.
    "dep-floors": targets_with_dep_floors(),
    # dep-locks names the lockfile FORMATS its readers understand, which is a
    # fact about this check rather than about the ecosystems: gradle also has a
    # lockfile, and there is no reader for it here.
    "dep-locks": frozenset({"pypi", "npm", "go"}),
    # go-module-identity compares a go.mod module path against the repository's
    # own origin identity, which only Go has.
    "go-module-identity": frozenset({"go"}),
    # strictspec-generated-floor compares a python dependency floor against the
    # python validators strictspec generated.
    "strictspec-generated-floor": frozenset({"pypi"}),
    "license-file": None,
    # --- release tag (universal) ---
    "unpublished-refs": None,
    "branch-sync": None,
    # npm-token-presence probes for the credential an npm CI publish needs, so
    # it applies wherever an npm pipeline can be configured.
    "npm-token-presence": frozenset({"npm"}),
    # Both lineage-derived follow-ups read a record no target decides the shape
    # of; go-deprecation-published then probes the Go module proxy, but which
    # transitions exist is the record's answer, not a target's.
    "old-repo-archived": None,
    "go-deprecation-published": None,
    # --- changelog tag (universal) ---
    "changelog-entry": None,
    "changelog-hashes": None,
    "changelog-range": None,
    "changelog-coverage": None,
    "changelog-orphans": None,
    "changelog-schema": None,
    "changelog-user-facing": None,
    "changelog-batch-commits": None,
    "changelog-batch-entries": None,
    "changelog-format-version": None,
    "changelog-format-version-gate": None,
    # --- workspace tag (workspace-only, target-agnostic) ---
    "router-filters-fresh": "workspace",
    "workspace-ci-router": "workspace",
    "workspace-ci-synced": "workspace",
    "workspace-targets": "workspace",
    "workspace-unregistered": "workspace",
    "workspace-stale-entries": "workspace",
    "dev-only-boundary": "workspace",
    "unversioned-boundary": "workspace",
    "layers-violations": "workspace",
    "deps-stale": "workspace",
    "dead-workspace-packages": "workspace",
    "subtree-remote-reachable": "workspace",
    "workspace-unbuildable": "workspace",
    # --- workspace + language-specific import scanners ---
    # All five sets below are the targets' own answers, derived from the
    # registry. They used to be five hand-listed copies that could drift from
    # the dispatch they described and from each other.
    "deps-unused": targets_with_import_analysis(),
    "deps-undeclared": targets_with_import_analysis(),
    "deps-runtime-test-only": targets_with_import_analysis(),
    "deps-dev-in-lib": targets_with_import_analysis(),
    # --- target-specific quality checks ---
    "dead-modules": targets_with_import_analysis(),
    "dead-modules-stale": targets_with_import_analysis(),
    "circular-deps": targets_with_circular_dep_analysis(),
    "library-lint": targets_with_library_lint(),
    # --- quality tag (universal) ---
    "scaffold-unreplaced-vars": None,
    # ruff-lint is Python-only: gated on a pypi target, mirroring dead-modules.
    "ruff-lint": frozenset({"pypi"}),
    # Path-capable tool checks and their competing-scope guards: the tools
    # (ruff, mypy) and the `uv run` invocation are Python-only.
    "lint": frozenset({"pypi"}),
    "lint-scope-guard": frozenset({"pypi"}),
    "format": frozenset({"pypi"}),
    "format-scope-guard": frozenset({"pypi"}),
    "type-check": frozenset({"pypi"}),
    "type-check-scope-guard": frozenset({"pypi"}),
    # --- phase 12 project checks ---
    "publish-mode-workflow": None,
    "npm-private-mismatch": frozenset({"npm"}),
    "target-version-readable": None,
    "dunder-version-missing": frozenset({"pypi"}),
    "selfdoc-version-drift": None,
    "scaffold-conflicts": None,
    "cross-repo-path-sources": frozenset({"pypi"}),
    # target-matrix-fresh compares a committed artifact against a regeneration
    # of the whole registry; no target decides whether it applies.
    "target-matrix-fresh": None,
    "requires-services": None,
    # dev-sync overlays are uv/Python-only (venv dist-info inspection).
    "dev-overlay-drift": frozenset({"pypi"}),
    # --- prepush tag ---
    "prepush-changelog-coverage": None,
    "prepush-gitignore-guard": None,
    "prepush-manual-warning": None,
    "test-suite": targets_with_builtin_tests(),
    "test-suite-workspace": "workspace",
    # --- maven-specific checks ---
    "maven-central-metadata": frozenset({"maven"}),
    # --- scaffold checks ---
    "scaffold-gitignore-stale": "workspace",
    # --- root config conflict ---
    "root-rlsbl-conflict": "workspace",
    # --- go companion tags ---
    "go-companion-tags": "workspace",
    # --- releasable member residue ---
    "releasable-residue": "workspace",
    # --- member pytest rootdir-escape guard ---
    "member-pytest-config": "workspace",
    # --- mixed monorepo tag-scheme guard ---
    "mixed-tag-schemes": "workspace",
    # --- launcher pipeline checks ---
    "wrapper-producer": None,
}

# Excluded targets: checks where a target is deliberately excluded because
# the compiler/toolchain already handles it. Maps check_name -> {target: reason}.
CHECK_EXCLUDED_TARGETS: dict[str, dict[str, str]] = {
    "circular-deps": {"go": "compiler rejects circular imports"},
}

# Canonical column order for the feature matrix.  The order is the
# original display order the feature matrix has always used.  The
# assertion guarantees completeness: if a new target is added but not
# listed here, startup fails loudly.
MATRIX_COLUMNS: tuple[str, ...] = (
    "pypi", "go", "npm", "dart", "deno", "hex", "zig",
    "swift", "swift-apple", "maven", "native-android", "native-ios", "docker", "flutter",
    "pgdesign", "plain", "spec",
)
assert set(MATRIX_COLUMNS) == set(TARGETS.keys()), (
    f"MATRIX_COLUMNS is out of sync with TARGETS: "
    f"missing={set(TARGETS.keys()) - set(MATRIX_COLUMNS)}, "
    f"extra={set(MATRIX_COLUMNS) - set(TARGETS.keys())}"
)


def targets_for_check(check_name: str) -> frozenset[str]:
    """Return the targets a target-specific check applies to.

    ``CHECK_TARGETS`` is the one place a check's target scope is written down,
    and a check that needs to skip for an inapplicable project reads it from
    here rather than repeating the target names in its own body. Raises for a
    check that is universal or workspace-only -- asking those for a target set
    is a bug, not a question with an empty answer.
    """
    try:
        targets = CHECK_TARGETS[check_name]
    except KeyError:
        raise KeyError(
            f"check '{check_name}' is not in CHECK_TARGETS; register it there"
        ) from None
    if targets is None or isinstance(targets, str):
        raise ValueError(
            f"check '{check_name}' is not target-specific "
            f"({'universal' if targets is None else targets}); it has no target set"
        )
    return targets


def check_scope_skip_reason(check_name: str, target_names) -> str | None:
    """Return a skip reason when *target_names* misses this check's scope.

    None means the check applies. The message names the targets the check does
    support, which is the matrix's answer rather than a sentence restating it.
    """
    scope = targets_for_check(check_name)
    if scope & set(target_names):
        return None
    return f"no {' / '.join(sorted(scope))} target"


def get_feature_matrix() -> dict[str, dict[str, str]]:
    """Build a feature matrix mapping check names to per-target support.

    Returns ``{check_name: {target: "yes"|"no"|"n/a"|"all"|"workspace"}}``
    where:
    - ``"all"`` means the check is universal (works for any target)
    - ``"workspace"`` means the check requires a monorepo workspace
    - ``"yes"`` means the target is explicitly supported
    - ``"n/a"`` means the check is deliberately excluded (toolchain handles it)
    - ``"no"`` means the target is not supported
    """
    matrix: dict[str, dict[str, str]] = {}
    for check_name, targets in CHECK_TARGETS.items():
        row: dict[str, str] = {}
        excluded = CHECK_EXCLUDED_TARGETS.get(check_name, {})
        if targets is None:
            for col in MATRIX_COLUMNS:
                row[col] = "all"
        elif targets == "workspace":
            for col in MATRIX_COLUMNS:
                row[col] = "workspace"
        else:
            for col in MATRIX_COLUMNS:
                if col in excluded:
                    row[col] = "n/a"
                elif col in targets:
                    row[col] = "yes"
                else:
                    row[col] = "no"
        matrix[check_name] = row
    return matrix


def generate_feature_matrix_data() -> tuple[list[str], list[list[str]]]:
    """Generate raw data for the check-vs-target feature support matrix.

    Only includes checks that have target-specific behavior (not
    universal or workspace-only), since those are the interesting rows.

    Returns ``(headers, rows)`` where *headers* is ``["Check", col1, ...]``
    and each row is ``[check_name, cell1, ...]``.
    """
    matrix = get_feature_matrix()

    # Filter to checks with at least one "yes", "no", or "n/a" (target-specific)
    interesting = {
        name: row for name, row in matrix.items()
        if any(v in ("yes", "no", "n/a") for v in row.values())
    }

    if not interesting:
        return ["Check"], []

    # Determine which columns have at least one "yes" among interesting rows
    active_cols = [
        col for col in MATRIX_COLUMNS
        if any(row.get(col) == "yes" for row in interesting.values())
    ]

    headers = ["Check"] + active_cols

    rows: list[list[str]] = []
    for check_name in sorted(interesting):
        row = interesting[check_name]
        cells = [check_name]
        for col in active_cols:
            val = row[col]
            if val == "yes":
                cells.append("yes")
            elif val == "n/a":
                cells.append("n/a")
            else:
                cells.append("no")
        rows.append(cells)

    return headers, rows


def register_checks(app):
    """Register all project checks on *app*.

    Silently returns if the check system is not enabled (i.e. no
    ``.strictcli/checks.toml`` was found at import time -- this happens
    when rlsbl is imported from a working directory that is not the
    rlsbl source tree, e.g. a user's project directory).
    """
    if not getattr(app, "_checks_enabled", False):
        return

    from .scope import scope_adapter
    app.set_scope_adapter(scope_adapter)

    from .project import register_project_checks
    from .release import register_release_checks
    from .changelog import register_changelog_checks
    from .workspace import register_workspace_checks
    from .quality import register_quality_checks
    from .prepush import register_prepush_checks
    from .strictspec_gate import register_strictspec_gate_checks

    register_project_checks(app)
    register_release_checks(app)
    register_changelog_checks(app)
    register_workspace_checks(app)
    register_quality_checks(app)
    register_prepush_checks(app)
    register_strictspec_gate_checks(app)
