"""Quality checks (tag: quality) covering library lint, dead module detection, circular dependencies, unreplaced scaffold variables, and the test suite.

Checks: library-lint, dead-modules, circular-deps,
scaffold-unreplaced-vars, test-suite.
"""

import os

from ..check_context import WorkspaceCheckContext
from ..utils import get_check_timeout
from ._common import _sibling_exclude_dirs


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
        """Project must pass ruff lint checks."""
        import subprocess as _sp

        from ..utils import require_tool

        if not require_tool("ruff", fatal=False):
            return reporter.skipped("ruff not installed")

        timeout = get_check_timeout(ctx.config)
        try:
            result = _sp.run(
                ["ruff", "check", str(ctx.project_root), "--quiet"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, _sp.TimeoutExpired) as exc:
            reporter.error(f"ruff failed to run: {exc}")
            return reporter.found(f"ruff failed to run: {exc}")

        if result.returncode == 0:
            return reporter.passed("ruff clean")

        output = (result.stdout or result.stderr or "").strip()
        lines = [ln for ln in output.splitlines() if ln.strip()]
        for line in lines[:50]:
            reporter.error(line)
        return reporter.found(f"ruff reported {len(lines)} issue(s)")

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

        all_dead: list[str] = []
        details: list[str] = []

        if "pypi" in target_names:
            from ..dep_validation import find_dead_modules
            py_dead = find_dead_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(py_dead)
            details.extend(f"{path}: not imported by any other module" for path in py_dead)

        if "go" in target_names:
            from ..dep_validation import find_dead_go_packages
            go_dead = find_dead_go_packages(root_str, exclude_dirs=exclude)
            all_dead.extend(go_dead)
            details.extend(f"{path}: internal package not imported outside itself" for path in go_dead)

        if "npm" in target_names:
            from ..dep_validation import find_dead_npm_modules
            npm_dead = find_dead_npm_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(npm_dead)
            details.extend(f"{path}: not reachable from any entry point" for path in npm_dead)

        if "dart" in target_names:
            from ..dep_validation import find_dead_dart_modules
            dart_dead = find_dead_dart_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(dart_dead)
            details.extend(f"{path}: not reachable from any entry point" for path in dart_dead)

        if "maven" in target_names:
            from ..dep_validation import find_dead_jvm_modules
            jvm_dead = find_dead_jvm_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(jvm_dead)
            details.extend(f"{path}: not reachable from any entry point" for path in jvm_dead)

        if all_dead:
            for d in details:
                reporter.warn(d)
            return reporter.found(f"{len(all_dead)} dead module(s)")
        return reporter.passed("no dead modules")

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

        from ..targets import detect_targets, resolve_releasable_config_dir_for_ctx
        from ..testing import run_project_tests

        rel_dir = resolve_releasable_config_dir_for_ctx(ctx)
        target_entries = detect_targets(str(ctx.project_root), releasable_config_dir=rel_dir)
        recognized = {"pypi", "go", "npm", "maven"}
        target_name = None
        for name, _path in target_entries:
            if name in recognized:
                target_name = name
                break

        if target_name is None:
            return reporter.skipped("no recognized test target (pypi, go, npm, maven)")

        passed = run_project_tests(
            target_name,
            project_dir=str(ctx.project_root),
            workspace_root=str(ctx.workspace_root) if ctx.workspace_root else None,
            config=ctx.config,
        )
        if passed:
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
