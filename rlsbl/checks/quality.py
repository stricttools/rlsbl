"""Quality checks (tag: quality).

Checks: library-lint, dead-modules, circular-deps,
scaffold-unreplaced-vars, scaffold-conflict-markers, test-suite.
"""

import os

from strictcli import CheckResult

from ..check_context import WorkspaceCheckContext
from ._common import _sibling_exclude_dirs


def register_quality_checks(app):
    """Register quality-tag checks on *app*."""

    @app.check("library-lint")
    def check_library_lint(ctx):
        """Library projects must pass boundary lint."""
        from ..lint import lint_library

        # Try monorepo path first
        ws_root = None
        try:
            from ..workspace import find_workspace_root, load_workspace
            ws_root = find_workspace_root(str(ctx.project_root))
        except Exception:
            pass

        if ws_root:
            try:
                projects = load_workspace(ws_root)
            except Exception:
                return CheckResult("pass", "not in a monorepo workspace")

            library_projects = [p for p in projects if p.get("library")]
            if not library_projects:
                return CheckResult("pass", "no library projects configured")

            total_errors = 0
            total_warnings = 0
            for proj in library_projects:
                proj_path = os.path.join(ws_root, proj["path"])
                results = lint_library(proj_path)
                for r in results:
                    if r.severity == "error":
                        total_errors += 1
                    elif r.severity == "warning":
                        total_warnings += 1

            if total_errors > 0:
                return CheckResult("fail", f"{total_errors} error(s), {total_warnings} warning(s)")
            if total_warnings > 0:
                return CheckResult("warn", f"{total_warnings} warning(s)")
            return CheckResult("pass", "all library projects clean")

        # Standalone projects are never libraries (only monorepo projects
        # with library = true are).  Match the release flow which skips lint
        # for non-library projects.
        return CheckResult("skip", "not a library project")

    @app.check("dead-modules")
    def check_dead_modules(ctx):
        """Unreferenced Python modules, Go internal packages, npm or Dart source files."""
        from ..targets import detect_targets

        root_str = str(ctx.project_root)
        target_entries = detect_targets(root_str)
        target_names = {e.name for e in target_entries}

        supported = {"pypi", "go", "npm", "dart"} & target_names
        if not supported:
            return CheckResult("skip", "not a Python, Go, npm, or Dart project")

        # In workspace context, exclude sibling project directories
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
            details.extend(
                f"{path}: not imported by any other module"
                for path in py_dead
            )

        if "go" in target_names:
            from ..dep_validation import find_dead_go_packages

            go_dead = find_dead_go_packages(root_str, exclude_dirs=exclude)
            all_dead.extend(go_dead)
            details.extend(
                f"{path}: internal package not imported outside itself"
                for path in go_dead
            )

        if "npm" in target_names:
            from ..dep_validation import find_dead_npm_modules

            npm_dead = find_dead_npm_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(npm_dead)
            details.extend(
                f"{path}: not reachable from any entry point"
                for path in npm_dead
            )

        if "dart" in target_names:
            from ..dep_validation import find_dead_dart_modules

            dart_dead = find_dead_dart_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(dart_dead)
            details.extend(
                f"{path}: not reachable from any entry point"
                for path in dart_dead
            )

        if all_dead:
            return CheckResult(
                "warn",
                f"{len(all_dead)} dead module(s)",
                details=details,
            )
        return CheckResult("pass", "no dead modules")

    @app.check("circular-deps")
    def check_circular_deps(ctx):
        """Detect intra-package circular import dependencies."""
        from ..targets import detect_targets

        root_str = str(ctx.project_root)
        target_entries = detect_targets(root_str)
        target_names = {e.name for e in target_entries}

        supported = {"pypi", "npm", "dart"} & target_names
        if not supported:
            return CheckResult("skip", "not a Python, npm, or Dart project")

        # In workspace context, exclude sibling project directories
        exclude = None
        if isinstance(ctx, WorkspaceCheckContext) and ctx.project is not None:
            ws_root = str(ctx.workspace_root)
            exclude = _sibling_exclude_dirs(
                ws_root, ctx.project["path"], ctx.projects,
            ) or None

        all_cycles: list[list[str]] = []
        # Track which language found each cycle for severity determination
        npm_cycles: list[list[str]] = []

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

        if not all_cycles:
            return CheckResult("pass", "no circular dependencies")

        details = []
        for cycle in all_cycles:
            details.append(
                f"cycle: {' -> '.join(cycle)} -> {cycle[0]}"
            )

        # npm cycles are errors; Python and Dart cycles are warnings
        if npm_cycles:
            return CheckResult(
                "fail",
                f"{len(all_cycles)} circular dependency cycle(s)",
                details=details,
            )
        return CheckResult(
            "warn",
            f"{len(all_cycles)} circular dependency cycle(s)",
            details=details,
        )

    @app.check("scaffold-unreplaced-vars")
    def check_scaffold_unreplaced_vars(ctx):
        """Committed scaffold files must not contain unreplaced rlsbl template variables."""
        import glob
        import re as _re

        root_str = str(ctx.project_root)

        # File patterns to scan for unreplaced template variables
        scan_patterns = [
            os.path.join(root_str, ".github", "workflows", "*.yml"),
            os.path.join(root_str, ".goreleaser.yml"),
            os.path.join(root_str, ".rlsbl", "hooks", "*.sh"),
        ]

        # rlsbl template syntax: {{word}} or {{word.word}}
        # Exclude GitHub Actions ${{ ... }} syntax (preceded by $)
        template_re = _re.compile(r"(?<!\$)\{\{\w+(?:\.\w+)*\}\}")

        # Docker metadata-action uses {{version}}, {{major}}, etc. as its
        # own template syntax on lines like "type=semver,pattern={{version}}".
        # These are not rlsbl template variables and must be excluded.
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
            return CheckResult(
                "fail",
                f"{len(errors)} file(s) with unreplaced template variables",
                details=errors,
            )
        return CheckResult("pass", "no unreplaced template variables")

    @app.check("scaffold-conflict-markers")
    def check_scaffold_conflict_markers(ctx):
        """Scaffold and workflow files must not contain git merge conflict markers."""
        import glob

        root_str = str(ctx.project_root)

        scan_patterns = [
            os.path.join(root_str, ".rlsbl", "**", "*"),
            os.path.join(root_str, ".github", "workflows", "*.yml"),
        ]

        conflict_markers = ("<<<<<<< ", "=======", ">>>>>>> ")
        errors = []

        for pattern in scan_patterns:
            for filepath in glob.glob(pattern, recursive=True):
                if not os.path.isfile(filepath):
                    continue
                rel_path = os.path.relpath(filepath, root_str)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        for lineno, line in enumerate(f, 1):
                            for marker in conflict_markers:
                                if line.startswith(marker):
                                    errors.append(
                                        f"{rel_path}:{lineno}: conflict marker '{marker.strip()}'"
                                    )
                except (OSError, UnicodeDecodeError):
                    continue

        if errors:
            return CheckResult(
                "fail",
                f"{len(errors)} conflict marker(s) found",
                details=errors,
            )
        return CheckResult("pass", "no conflict markers")

    @app.check("test-suite")
    def check_test_suite(ctx):
        """Run the project's test suite."""
        if ctx.workspace_root is not None and str(ctx.project_root) == str(ctx.workspace_root):
            return CheckResult("skip", "workspace root — use test-suite-workspace")

        from ..targets import detect_targets
        from ..testing import run_project_tests

        target_entries = detect_targets(str(ctx.project_root))
        recognized = {"pypi", "go", "npm"}
        target_name = None
        for name, _path in target_entries:
            if name in recognized:
                target_name = name
                break

        if target_name is None:
            return CheckResult("skip", "no recognized test target (pypi, go, npm)")

        passed = run_project_tests(
            target_name, project_dir=str(ctx.project_root), config=ctx.config
        )
        if passed:
            return CheckResult("pass", f"{target_name} tests passed")
        return CheckResult("fail", f"{target_name} tests failed")
