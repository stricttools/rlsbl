"""Quality checks (tag: quality) covering library lint, dead module detection, circular dependencies, unreplaced scaffold variables, and the test suite.

Checks: library-lint, dead-modules, circular-deps,
scaffold-unreplaced-vars, test-suite.
"""

import os

from ..check_context import WorkspaceCheckContext
from ..utils import get_check_timeout
from ._common import _sibling_exclude_dirs
from .. import effects

# Minimum ruff release the ruff-lint check is built against: the JSON output
# schema (code/message/fix/location/filename fields) and the default rule set
# are pinned to this floor. Mirrors SAFEGIT_MIN_VERSION in release_scrub.py.
RUFF_MIN_VERSION = (0, 15, 20)


def register_quality_checks(app):
    """Register quality-tag checks on *app*."""

    @app.error_check("library-lint")
    def check_library_lint(ctx, reporter):
        """Library projects must pass boundary lint."""
        from ..lint import lint_library
        from ..targets import resolve_releasable_config_dir

        if not ctx.projects:
            return reporter.passed("no library projects configured")

        ws_root = str(ctx.workspace_root)
        timeout = get_check_timeout(ctx.config)
        total_errors = 0
        total_warnings = 0
        for proj in ctx.projects:
            proj_path = os.path.join(ws_root, proj["path"])
            rel_config_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
            releasable_lint_dir = (
                os.path.join(rel_config_dir, "lint") if rel_config_dir else None
            )
            results = lint_library(
                proj_path,
                allowed_imports=proj.get("lint_allow"),
                check_timeout=timeout,
                releasable_lint_dir=releasable_lint_dir,
            )
            for r in results:
                if r.severity == "error":
                    total_errors += 1
                elif r.severity == "warning":
                    total_warnings += 1

        if total_errors > 0:
            reporter.error(f"{total_errors} error(s), {total_warnings} warning(s)")
            return reporter.found(f"{total_errors} error(s), {total_warnings} warning(s)")
        if total_warnings > 0:
            reporter.warn(f"{total_warnings} warning(s)")
            return reporter.found(f"{total_warnings} warning(s)")
        return reporter.passed("all library projects clean")

    @app.error_check("ruff-lint")
    def check_ruff_lint(ctx, reporter):
        """Python (pypi) projects must pass ruff lint checks."""
        import json as _json
        import re as _re
        import subprocess as _sp
        from collections import Counter

        from ..targets import detect_targets, resolve_releasable_config_dir_for_ctx
        from ..utils import require_tool

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(
            str(ctx.project_root), releasable_config_dir=rel_dir
        )
        target_names = {e.name for e in target_entries}
        if "pypi" not in target_names:
            return reporter.skipped("not a Python (pypi) project")

        if not require_tool("ruff", fatal=False):
            reporter.error(
                "ruff is not installed -- add ruff to the project's dev "
                "dependencies (e.g. `uv add --dev ruff`)"
            )
            return reporter.found("ruff is not installed")

        timeout = get_check_timeout(ctx.config)

        # Version floor: the JSON output shape and rule set are pinned to a
        # known-good floor, mirroring the SAFEGIT_MIN_VERSION pattern.
        try:
            version_proc = effects.run(
                ["ruff", "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, _sp.TimeoutExpired) as exc:
            reporter.error(f"ruff --version failed to run: {exc}")
            return reporter.found(f"ruff --version failed to run: {exc}")

        version_str = (version_proc.stdout or version_proc.stderr or "").strip()
        m = _re.search(r"(\d+)\.(\d+)\.(\d+)", version_str)
        if not m:
            reporter.error(f"cannot parse ruff version from {version_str!r}")
            return reporter.found("cannot parse ruff version")
        version_tuple = tuple(int(g) for g in m.groups())
        if version_tuple < RUFF_MIN_VERSION:
            found_ver = ".".join(str(p) for p in version_tuple)
            min_ver = ".".join(str(p) for p in RUFF_MIN_VERSION)
            reporter.error(
                f"ruff >= {min_ver} required, found {found_ver} -- upgrade ruff"
            )
            return reporter.found(f"ruff {found_ver} is below the {min_ver} floor")

        try:
            result = effects.run(
                [
                    "ruff", "check", str(ctx.project_root),
                    "--output-format=json", "--quiet",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, _sp.TimeoutExpired) as exc:
            reporter.error(f"ruff failed to run: {exc}")
            return reporter.found(f"ruff failed to run: {exc}")

        if result.returncode == 0:
            return reporter.passed("ruff clean")

        raw = (result.stdout or "").strip()
        try:
            violations = _json.loads(raw) if raw else []
        except _json.JSONDecodeError:
            stderr = (result.stderr or "").strip()
            reporter.error(f"ruff produced unparseable output: {stderr or raw}")
            return reporter.found("ruff produced unparseable output")

        if not violations:
            # Non-zero exit with no parsed violations: surface the raw failure.
            stderr = (result.stderr or "").strip()
            reporter.error(f"ruff failed (exit {result.returncode}): {stderr}")
            return reporter.found(f"ruff failed (exit {result.returncode})")

        count = len(violations)
        rule_counts = Counter(v.get("code") or "?" for v in violations)
        fixable = sum(1 for v in violations if v.get("fix"))

        for v in violations[:50]:
            code = v.get("code") or "?"
            msg = v.get("message", "")
            filename = v.get("filename", "")
            loc = v.get("location") or {}
            row = loc.get("row")
            col = loc.get("column")
            if row is not None and col is not None:
                where = f"{filename}:{row}:{col}"
            elif row is not None:
                where = f"{filename}:{row}"
            else:
                where = filename
            reporter.error(f"{where}: {code} {msg}")

        top = ", ".join(f"{code} x{n}" for code, n in rule_counts.most_common(3))
        return reporter.found(
            f"ruff reported {count} violation(s) [{top}]; {fixable} fixable"
        )

    # -- path-capable tool checks -------------------------------------------
    #
    # Registered from a table rather than six near-identical functions: the
    # only thing that varies is the check name, and a copy of the body per
    # tool is how the three of them drift apart.
    def _register_tool_check(check_name):
        from ..tool_checks import guard_name, run_scope_guard, run_tool_check

        @app.error_check(check_name)
        def _tool_check(ctx, reporter, _name=check_name):
            """Run the project's declared paths through this tool."""
            return run_tool_check(ctx, reporter, _name)

        @app.error_check(guard_name(check_name))
        def _tool_scope_guard(ctx, reporter, _name=check_name):
            """The tool's own config must not carry competing scope."""
            return run_scope_guard(ctx, reporter, _name)

    from ..tool_checks import TOOL_CHECKS as _TOOL_CHECKS

    for _tool_check_name in sorted(_TOOL_CHECKS):
        _register_tool_check(_tool_check_name)

    @app.warn_check("dead-modules")
    def check_dead_modules(ctx, reporter):
        """Unreferenced Python modules, Go internal packages, npm or Dart source files."""
        from ..targets import detect_targets, resolve_releasable_config_dir_for_ctx

        root_str = str(ctx.project_root)
        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(root_str, releasable_config_dir=rel_dir)
        target_names = {e.name for e in target_entries}

        supported = {"pypi", "go", "npm", "dart", "maven"} & target_names
        if not supported:
            return reporter.skipped("not a Python, Go, npm, Dart, or Maven project")

        exclude = None
        if isinstance(ctx, WorkspaceCheckContext) and ctx.project is not None:
            ws_root = str(ctx.workspace_root)
            exclude = _sibling_exclude_dirs(
                ws_root, ctx.project["path"], ctx.projects,
            ) or None

        # Declared dead-module exclusions (legitimate non-entry points).
        from ..dep_validation import load_dead_module_exclusions
        config_dir = rel_dir if rel_dir is not None else os.path.join(root_str, ".rlsbl")
        suppress = set(load_dead_module_exclusions(config_dir))

        all_dead: list[str] = []
        details: list[str] = []

        if "pypi" in target_names:
            # Union-of-imports detector: thread suppress so a listed file's
            # own imports cannot keep any other module alive (no laundering).
            from ..dep_validation import find_dead_modules
            py_dead = find_dead_modules(root_str, exclude_dirs=exclude, suppress=suppress)
            all_dead.extend(py_dead)
            details.extend(f"{path}: not imported by any other module" for path in py_dead)

        if "go" in target_names:
            # Union-of-imports detector: thread suppress (Go paths are
            # package directories) to prevent entry-point laundering.
            from ..dep_validation import find_dead_go_packages
            go_dead = find_dead_go_packages(root_str, exclude_dirs=exclude, suppress=suppress)
            all_dead.extend(go_dead)
            details.extend(f"{path}: internal package not imported outside itself" for path in go_dead)

        # npm/Dart/JVM detectors are BFS-from-entry-points: a non-entry
        # suppressed file's edges are never traversed, so subtracting the
        # listed paths from the reported dead set is provably sufficient.
        if "npm" in target_names:
            from ..dep_validation import find_dead_npm_modules
            npm_dead = [
                p for p in find_dead_npm_modules(root_str, exclude_dirs=exclude)
                if p not in suppress
            ]
            all_dead.extend(npm_dead)
            details.extend(f"{path}: not reachable from any entry point" for path in npm_dead)

        if "dart" in target_names:
            from ..dep_validation import find_dead_dart_modules
            dart_dead = [
                p for p in find_dead_dart_modules(root_str, exclude_dirs=exclude)
                if p not in suppress
            ]
            all_dead.extend(dart_dead)
            details.extend(f"{path}: not reachable from any entry point" for path in dart_dead)

        if "maven" in target_names:
            from ..dep_validation import find_dead_jvm_modules
            jvm_dead = [
                p for p in find_dead_jvm_modules(root_str, exclude_dirs=exclude)
                if p not in suppress
            ]
            all_dead.extend(jvm_dead)
            details.extend(f"{path}: not reachable from any entry point" for path in jvm_dead)

        if all_dead:
            for d in details:
                reporter.warn(d)
            return reporter.found(f"{len(all_dead)} dead module(s)")
        return reporter.passed("no dead modules")

    @app.error_check("dead-modules-stale")
    def check_dead_modules_stale(ctx, reporter):
        """Declared dead-module exclusions must point to existing files."""
        from ..dep_validation import load_dead_module_exclusions
        from ..targets import resolve_releasable_config_dir_for_ctx

        root_str = str(ctx.project_root)
        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        config_dir = rel_dir if rel_dir is not None else os.path.join(root_str, ".rlsbl")
        exclusions = load_dead_module_exclusions(config_dir)
        if not exclusions:
            return reporter.passed("no dead-module exclusions")

        toml_rel = os.path.relpath(
            os.path.join(config_dir, "dead-modules.toml"), root_str
        )
        stale = [
            path for path in sorted(exclusions)
            if not os.path.exists(os.path.join(root_str, path))
        ]

        if stale:
            for s in stale:
                reporter.error(
                    f"{s}: declared in {toml_rel} but does not exist on disk"
                )
            return reporter.found(f"{len(stale)} stale dead-module exclusion(s)")
        return reporter.passed("all dead-module exclusions exist")

    @app.warn_check("circular-deps")
    def check_circular_deps(ctx, reporter):
        """Detect intra-package circular import dependencies."""
        from ..targets import detect_targets, resolve_releasable_config_dir_for_ctx

        root_str = str(ctx.project_root)
        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(root_str, releasable_config_dir=rel_dir)
        target_names = {e.name for e in target_entries}

        supported = {"pypi", "npm", "dart", "maven"} & target_names
        if not supported:
            return reporter.skipped("not a Python, npm, Dart, or Maven project")

        exclude = None
        if isinstance(ctx, WorkspaceCheckContext) and ctx.project is not None:
            ws_root = str(ctx.workspace_root)
            exclude = _sibling_exclude_dirs(
                ws_root, ctx.project["path"], ctx.projects,
            ) or None

        all_cycles: list[list[str]] = []

        if "pypi" in target_names:
            from ..dep_validation import find_circular_python_deps
            py_cycles = find_circular_python_deps(root_str, exclude_dirs=exclude)
            all_cycles.extend(py_cycles)

        if "npm" in target_names:
            from ..dep_validation import find_circular_npm_deps
            npm_cycles = find_circular_npm_deps(root_str, exclude_dirs=exclude)
            all_cycles.extend(npm_cycles)

        if "dart" in target_names:
            from ..dep_validation import find_circular_dart_deps
            dart_cycles = find_circular_dart_deps(root_str, exclude_dirs=exclude)
            all_cycles.extend(dart_cycles)

        if "maven" in target_names:
            from ..dep_validation import find_circular_jvm_deps
            jvm_cycles = find_circular_jvm_deps(root_str, exclude_dirs=exclude)
            all_cycles.extend(jvm_cycles)

        if not all_cycles:
            return reporter.passed("no circular dependencies")

        for cycle in all_cycles:
            reporter.warn(f"cycle: {' -> '.join(cycle)} -> {cycle[0]}")
        return reporter.found(f"{len(all_cycles)} circular dependency cycle(s)")

    @app.error_check("scaffold-unreplaced-vars")
    def check_scaffold_unreplaced_vars(ctx, reporter):
        """Committed scaffold files must not contain unreplaced rlsbl template variables."""
        import glob
        import re as _re

        root_str = str(ctx.project_root)

        scan_patterns = [
            os.path.join(root_str, ".github", "workflows", "*.yml"),
            os.path.join(root_str, ".goreleaser.yml"),
            os.path.join(root_str, ".rlsbl", "hooks", "*.sh"),
        ]

        template_re = _re.compile(r"(?<!\$)\{\{\w+(?:\.\w+)*\}\}")
        docker_meta_re = _re.compile(r"type=semver,pattern=")

        errors = []
        for pattern in scan_patterns:
            for filepath in glob.glob(pattern):
                rel_path = os.path.relpath(filepath, root_str)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                except (OSError, UnicodeDecodeError):
                    continue

                matches = []
                for line in lines:
                    if docker_meta_re.search(line):
                        continue
                    matches.extend(template_re.findall(line))
                if matches:
                    unique = sorted(set(matches))
                    errors.append(
                        f"{rel_path}: unreplaced variable(s) {', '.join(unique)}"
                    )

        if errors:
            for err in errors:
                reporter.error(err)
            return reporter.found(f"{len(errors)} file(s) with unreplaced template variables")
        return reporter.passed("no unreplaced template variables")

    @app.error_check("test-suite")
    def check_test_suite(ctx, reporter):
        """Run the project's test suite."""
        if ctx.workspace_root is not None and str(ctx.project_root) == str(ctx.workspace_root):
            return reporter.skipped("workspace root -- use test-suite-workspace")

        from ..targets import (
            detect_targets,
            resolve_releasable_config_dir_for_ctx,
            targets_with_builtin_tests,
        )
        from ..targets.outcomes import SuiteRunStatus
        from ..testing import run_project_tests

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        # Derived from the registry: a target ships a runner iff it overrides
        # run_tests. The check no longer keeps its own copy of the set.
        runnable = targets_with_builtin_tests()
        target_name = next(
            (name for name, _path in target_entries if name in runnable), None,
        )

        if target_name is None:
            detected = ", ".join(sorted({name for name, _path in target_entries}))
            supported = ", ".join(sorted(runnable))
            if detected:
                return reporter.skipped(
                    f"no target with a built-in test runner -- detected "
                    f"{detected}; runners exist for {supported}"
                )
            return reporter.skipped(
                f"no targets detected; test runners exist for {supported}"
            )

        outcome = run_project_tests(
            target_name,
            project_dir=str(ctx.project_root),
            workspace_root=str(ctx.workspace_root) if ctx.workspace_root else None,
            config=ctx.config,
        )
        if outcome.status is SuiteRunStatus.SKIPPED:
            # Unreachable while `runnable` is derived from the same property
            # the runner uses, but a skip must never render as a pass.
            return reporter.skipped(outcome.message)
        if outcome.passed:
            return reporter.passed(f"{target_name} tests passed")
        reporter.error(f"{target_name} tests failed")
        return reporter.found(f"{target_name} tests failed")

    @app.error_check("maven-central-metadata")
    def check_maven_central_metadata(ctx, reporter):
        """Validate Maven Central publishing requirements."""
        from ..targets import detect_targets, resolve_releasable_config_dir_for_ctx
        from ..maven_central import validate_maven_central_metadata

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        if not any(name == "maven" for name, _path in target_entries):
            return reporter.skipped("not a maven project")

        pipelines = ctx.config.get("pipelines", {})
        if not any(p.get("type") == "maven-central" for p in pipelines.values()):
            return reporter.skipped("no maven-central pipeline configured")

        errors = validate_maven_central_metadata(str(ctx.project_root))
        if errors:
            for err in errors:
                reporter.error(err)
            return reporter.found(f"{len(errors)} Maven Central requirement(s) not met")
        return reporter.passed("Maven Central metadata requirements satisfied")
