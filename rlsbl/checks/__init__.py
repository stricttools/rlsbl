"""Project checks registered on the strictcli check system.

Each check is registered via ``@app.check("name")`` and receives a
:class:`~rlsbl.context.ProjectContext` (or its
:class:`~rlsbl.check_context.WorkspaceCheckContext` subclass).

The check functions return :class:`strictcli.CheckResult` with lowercase
status strings: ``"pass"``, ``"fail"``, ``"warn"``, ``"skip"``.
"""

import os

from ..targets import TARGETS

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
# For checks that use import scanners in workspace mode, the targets
# listed are the languages the scanners support (pypi=Python, dart, npm).

CHECK_TARGETS: dict[str, frozenset[str] | None | str] = {
    # --- project tag (universal) ---
    "lock": None,
    "version-consistency": None,
    "name-consistency": None,
    "license-consistency": None,
    "description-consistency": None,
    "private-hook-stale": None,
    "config-schema": None,
    "license-file": None,
    # --- release tag (universal) ---
    "local-tag": None,
    "remote-tag": None,
    "github-release": None,
    "branch-sync": None,
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
    # --- workspace tag (workspace-only, target-agnostic) ---
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
    "deps-unused": frozenset({"pypi", "dart", "npm", "go", "maven"}),
    "deps-undeclared": frozenset({"pypi", "dart", "npm", "go", "maven"}),
    "deps-runtime-test-only": frozenset({"pypi", "dart", "npm", "go", "maven"}),
    "deps-dev-in-lib": frozenset({"pypi", "dart", "npm", "go", "maven"}),
    # --- target-specific quality checks ---
    "dead-modules": frozenset({"pypi", "go", "npm", "dart", "maven"}),
    "circular-deps": frozenset({"pypi", "npm", "dart", "maven"}),
    "library-lint": frozenset({"pypi", "go", "npm", "maven"}),
    # --- quality tag (universal) ---
    "scaffold-unreplaced-vars": None,
    "ruff-lint": None,
    # --- phase 12 project checks ---
    "private-publish-workflow": None,
    "npm-private-mismatch": frozenset({"npm"}),
    "target-version-readable": None,
    "dunder-version-missing": frozenset({"pypi"}),
    "selfdoc-version-drift": None,
    "scaffold-conflicts": None,
    "cross-repo-path-sources": frozenset({"pypi"}),
    # dev-sync overlays are uv/Python-only (venv dist-info inspection).
    "dev-overlay-drift": frozenset({"pypi"}),
    # --- prepush tag ---
    "prepush-changelog-coverage": None,
    "prepush-gitignore-guard": None,
    "prepush-manual-warning": None,
    "test-suite": frozenset({"pypi", "go", "npm", "maven"}),
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
}

# Excluded targets: checks where a target is deliberately excluded because
# the compiler/toolchain already handles it. Maps check_name -> {target: reason}.
CHECK_EXCLUDED_TARGETS: dict[str, dict[str, str]] = {
    "circular-deps": {"go": "compiler rejects circular imports"},
}

# Canonical column order for the feature matrix.  The order is the
# original display order the feature matrix has always used.  The
# assertion guarantees completeness: if a 19th target is added but not
# listed here, startup fails loudly.
MATRIX_COLUMNS: tuple[str, ...] = (
    "pypi", "go", "npm", "dart", "cargo", "deno", "hex", "zig",
    "swift", "swift-apple", "maven", "native-android", "native-ios", "docker", "flutter",
    "pgdesign", "plain", "spec",
)
assert set(MATRIX_COLUMNS) == set(TARGETS.keys()), (
    f"MATRIX_COLUMNS is out of sync with TARGETS: "
    f"missing={set(TARGETS.keys()) - set(MATRIX_COLUMNS)}, "
    f"extra={set(MATRIX_COLUMNS) - set(TARGETS.keys())}"
)


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

    register_project_checks(app)
    register_release_checks(app)
    register_changelog_checks(app)
    register_workspace_checks(app)
    register_quality_checks(app)
    register_prepush_checks(app)
