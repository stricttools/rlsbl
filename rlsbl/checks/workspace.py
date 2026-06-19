"""Workspace checks (tag: workspace) validating monorepo CI routing, project registration, dev-only boundaries, and inter-project dependency declarations.

Checks: workspace-ci-router, workspace-ci-synced, workspace-targets,
workspace-unregistered, workspace-stale-entries, dev-only-boundary,
dead-workspace-packages, subtree-remote-reachable, workspace-unbuildable,
layers-violations, deps-unused, deps-undeclared, deps-runtime-test-only,
deps-dev-in-lib, deps-stale, test-suite-workspace.
"""

import json
import os
import subprocess

from strictcli import CheckResult

from ..check_context import WorkspaceCheckContext
from ..workspace import project_is_dev_only, project_is_releasable
from ._common import (
    RLSBL_CONFIG,
    _build_dep_import_cache,
    _sibling_exclude_dirs,
)
from . import PROJECT_MANIFESTS


def register_workspace_checks(app):
    """Register workspace-tag checks on *app*."""

    @app.check("workspace-ci-router")
    def check_workspace_ci_router(ctx):
        """ci-router.yml must exist at the repo root."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        router = os.path.join(str(ctx.workspace_root), ".github", "workflows", "ci-router.yml")
        if os.path.isfile(router):
            return CheckResult("pass", "ci-router.yml exists")
        return CheckResult("fail", "ci-router.yml not found")

    @app.check("workspace-ci-synced")
    def check_workspace_ci_synced(ctx):
        """Each project must have a synced CI workflow at the repo root."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        missing = []
        for proj in ctx.projects:
            name = proj["name"]
            workflow = os.path.join(
                str(ctx.workspace_root), ".github", "workflows", f"{name}-ci.yml"
            )
            if not os.path.isfile(workflow):
                missing.append(name)

        if missing:
            return CheckResult(
                "fail",
                f"missing workflows: {', '.join(missing)}",
                details=[f"{n}: {n}-ci.yml not found" for n in missing],
            )
        return CheckResult("pass", f"all {len(ctx.projects)} project(s) have synced workflows")

    @app.check("workspace-targets")
    def check_workspace_targets(ctx):
        """Every project must have at least one detectable target."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..targets import detect_targets

        missing = []
        for proj in ctx.projects:
            targets = detect_targets(os.path.join(str(ctx.workspace_root), proj["path"]))
            if not targets:
                missing.append(proj["name"])

        if missing:
            return CheckResult(
                "fail",
                f"no targets detected: {', '.join(missing)}",
                details=[f"{n}: no release target found" for n in missing],
            )
        return CheckResult("pass", f"all {len(ctx.projects)} project(s) have targets")

    @app.check("workspace-unregistered")
    def check_workspace_unregistered(ctx):
        """No project directories on disk should be missing from workspace.toml."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        root = str(ctx.workspace_root)
        registered_paths = {proj["path"].rstrip("/") for proj in ctx.projects}

        # Determine gitignored directories
        gitignored = set()
        try:
            result = subprocess.run(
                ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            for line in result.stdout.splitlines():
                gitignored.add(line.rstrip("/"))
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        found_project_dirs = set()
        try:
            entries = os.listdir(root)
        except OSError:
            entries = []

        for entry in sorted(entries):
            if entry.startswith("."):
                continue
            if entry in gitignored:
                continue
            dir_path = os.path.join(root, entry)
            if not os.path.isdir(dir_path):
                continue
            # Check for rlsbl scaffolding (universal indicator)
            if os.path.isfile(os.path.join(dir_path, RLSBL_CONFIG)):
                found_project_dirs.add(entry)
                continue
            for manifest in PROJECT_MANIFESTS:
                if os.path.isfile(os.path.join(dir_path, manifest)):
                    # Skip private npm workspace roots (not real projects)
                    if manifest == "package.json":
                        try:
                            with open(os.path.join(dir_path, manifest)) as f:
                                pkg = json.load(f)
                            if pkg.get("private") is True:
                                continue
                        except (json.JSONDecodeError, OSError):
                            pass
                    found_project_dirs.add(entry)
                    break

        # Filter out directories that are parents of registered paths
        # (e.g., "web" is a parent if "web/frontend" is registered)
        found_project_dirs -= {
            d for d in found_project_dirs
            if any(rp.startswith(d + "/") for rp in registered_paths)
        }

        unregistered = sorted(found_project_dirs - registered_paths)
        if unregistered:
            return CheckResult(
                "fail",
                f"{len(unregistered)} unregistered project(s)",
                details=[f"{d}: has manifest but not in workspace.toml" for d in unregistered],
            )
        return CheckResult("pass", "no unregistered projects")

    @app.check("workspace-stale-entries")
    def check_workspace_stale_entries(ctx):
        """No workspace.toml entries should point to missing or manifest-less dirs."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        root = str(ctx.workspace_root)

        stale = []
        for proj in ctx.projects:
            dir_path = os.path.join(root, proj["path"])
            if not os.path.isdir(dir_path):
                stale.append(proj["path"])
                continue
            # Check for rlsbl scaffolding (universal indicator)
            if os.path.isfile(os.path.join(dir_path, RLSBL_CONFIG)):
                continue
            has_manifest = any(
                os.path.isfile(os.path.join(dir_path, m)) for m in PROJECT_MANIFESTS
            )
            if not has_manifest:
                stale.append(proj["path"])

        if stale:
            return CheckResult(
                "fail",
                f"{len(stale)} stale workspace entry(ies)",
                details=[f"{s}: directory missing or no manifest" for s in stale],
            )
        return CheckResult("pass", "no stale entries")

    @app.check("dev-only-boundary")
    def check_dev_only_boundary(ctx):
        """Non-dev-only projects must not have runtime deps on dev-only projects."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        # Build lookup: project name -> project dict
        projects_by_name = {p["name"]: p for p in ctx.projects}

        # Find all dev-only projects
        dev_only_names = [
            name for name, proj in projects_by_name.items()
            if project_is_dev_only(proj)
        ]

        if not dev_only_names:
            return CheckResult("pass", "no dev-only projects")

        violations = []
        for dev_name in dev_only_names:
            # Collect non-dev dependents: runtime and explicit scopes
            dependents = set()
            for scope in ("runtime", "explicit"):
                try:
                    rdeps = ctx.graph.transitive_rdeps(dev_name, scope_filter=scope)
                except KeyError:
                    continue
                dependents.update(rdeps)

            for dep_name in sorted(dependents):
                dep_proj = projects_by_name.get(dep_name)
                if dep_proj is None:
                    continue
                if not project_is_dev_only(dep_proj):
                    violations.append(
                        f"non-dev-only project '{dep_name}' has a runtime dependency "
                        f"on dev-only project '{dev_name}'. "
                        f"Bug fixes in '{dev_name}' won't appear in any changelog."
                    )

        if violations:
            return CheckResult(
                "fail",
                f"{len(violations)} boundary violation(s)",
                details=violations,
            )
        return CheckResult("pass", "dev-only boundary clean")

    @app.check("dead-workspace-packages")
    def check_dead_workspace_packages(ctx):
        """Library packages must be imported by at least one workspace sibling."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..dep_validation import find_dead_workspace_packages

        import_cache = _build_dep_import_cache(ctx)
        dead = find_dead_workspace_packages(ctx.projects, import_cache)

        if not dead:
            return CheckResult("pass", "all library packages have workspace importers")

        details = [d.message for d in dead]
        return CheckResult(
            "warn",
            f"{len(dead)} dead workspace package(s)",
            details=details,
        )

    @app.check("subtree-remote-reachable")
    def check_subtree_remote_reachable(ctx):
        """Every project with subtree_remote must have a reachable remote."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..utils import run as _run

        errors = []
        checked = 0
        for proj in ctx.projects:
            remote = proj.get("subtree_remote", "")
            if not remote:
                continue
            checked += 1
            try:
                _run("git", ["ls-remote", remote], cwd=str(ctx.workspace_root))
            except subprocess.CalledProcessError:
                errors.append(f"{proj['name']}: subtree remote unreachable: {remote}")

        if checked == 0:
            return CheckResult("skip", "no projects have subtree_remote")

        if errors:
            return CheckResult(
                "fail",
                f"{len(errors)} unreachable subtree remote(s)",
                details=errors,
            )
        return CheckResult("pass", f"all {checked} subtree remote(s) reachable")

    @app.check("workspace-unbuildable")
    def check_workspace_unbuildable(ctx):
        """Detect workspace members that fail ``uv sync --all-packages``."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a workspace")

        # Only relevant when there are pypi-target projects in the workspace
        from ..targets import detect_targets

        root = str(ctx.workspace_root)
        has_pypi = False
        for proj in ctx.projects:
            proj_dir = os.path.join(root, proj["path"])
            target_entries = detect_targets(proj_dir)
            if any(e.name == "pypi" for e in target_entries):
                has_pypi = True
                break

        if not has_pypi:
            return CheckResult("skip", "no pypi-target projects in workspace")

        try:
            result = subprocess.run(
                ["uv", "sync", "--all-packages", "--dry-run"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            return CheckResult("skip", "uv not installed")
        except subprocess.TimeoutExpired:
            return CheckResult("fail", "uv sync --all-packages --dry-run timed out after 120s")

        if result.returncode == 0:
            return CheckResult("pass", "all workspace members buildable")

        # Parse stderr for details about the failure
        stderr = result.stderr.strip()
        details = [line for line in stderr.splitlines() if line.strip()]
        summary = details[0] if details else "uv sync --all-packages --dry-run failed"
        return CheckResult("fail", summary, details=details)

    @app.check("deps-unused")
    def check_deps_unused(ctx):
        """Declared workspace deps must be imported by at least one source file."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..dep_validation import check_unused_deps, load_dep_overrides

        root = str(ctx.workspace_root)
        whitelist = load_dep_overrides(root)
        workspace_names = {p["name"] for p in ctx.projects}
        import_cache = _build_dep_import_cache(ctx)

        all_errors = []
        for proj in ctx.projects:
            name = proj["name"]
            project_dir = os.path.join(root, proj["path"])
            # When the same dep is declared with multiple scopes, hard
            # scopes ("runtime"/"explicit") take precedence over optional
            # ones ("dev"/"peer"): declared hard anywhere means hard.
            manifest_deps_with_scope: dict[str, str] = {}
            for d in ctx.graph.dependencies(name):
                if manifest_deps_with_scope.get(d.name) in ("runtime", "explicit"):
                    continue
                manifest_deps_with_scope[d.name] = d.scope
            errors = check_unused_deps(
                name, project_dir, manifest_deps_with_scope, workspace_names,
                whitelist, _cached_imports=import_cache[name],
            )
            all_errors.extend(errors)

        if all_errors:
            return CheckResult(
                "fail",
                f"{len(all_errors)} unused dependency(ies)",
                details=all_errors,
            )
        return CheckResult("pass", "no unused workspace dependencies")

    @app.check("deps-undeclared")
    def check_deps_undeclared(ctx):
        """Source files must not import workspace packages not declared as deps."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..dep_validation import check_undeclared_deps

        root = str(ctx.workspace_root)
        workspace_names = {p["name"] for p in ctx.projects}
        import_cache = _build_dep_import_cache(ctx)

        all_errors = []
        for proj in ctx.projects:
            name = proj["name"]
            project_dir = os.path.join(root, proj["path"])
            manifest_deps = {d.name for d in ctx.graph.dependencies(name)}
            errors = check_undeclared_deps(
                name, project_dir, manifest_deps, workspace_names,
                _cached_imports=import_cache[name],
            )
            all_errors.extend(errors)

        if all_errors:
            return CheckResult(
                "fail",
                f"{len(all_errors)} undeclared dependency(ies)",
                details=all_errors,
            )
        return CheckResult("pass", "no undeclared workspace dependencies")

    @app.check("deps-runtime-test-only")
    def check_deps_runtime_test_only(ctx):
        """Runtime deps used only in test code should be dev deps instead."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..dep_validation import check_runtime_test_only

        import_cache = _build_dep_import_cache(ctx)

        all_flagged = []
        for proj in ctx.projects:
            name = proj["name"]
            manifest_deps_with_scope = {
                d.name: d.scope for d in ctx.graph.dependencies(name)
            }
            lib_imports, test_imports, _guarded = import_cache[name]
            flagged = check_runtime_test_only(
                manifest_deps_with_scope, lib_imports, test_imports
            )
            for dep in flagged:
                all_flagged.append(
                    f"'{name}' declares '{dep}' as runtime dependency "
                    f"but only imports it in test code"
                )

        if all_flagged:
            return CheckResult(
                "warn",
                f"{len(all_flagged)} runtime dep(s) used only in tests",
                details=all_flagged,
            )
        return CheckResult("pass", "no runtime deps used only in tests")

    @app.check("deps-dev-in-lib")
    def check_deps_dev_in_lib(ctx):
        """Dev deps must not be imported in production code."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..dep_validation import check_dev_in_lib

        import_cache = _build_dep_import_cache(ctx)

        all_flagged = []
        for proj in ctx.projects:
            name = proj["name"]
            manifest_deps_with_scope = {
                d.name: d.scope for d in ctx.graph.dependencies(name)
            }
            lib_imports, _test_imports, _guarded = import_cache[name]
            flagged = check_dev_in_lib(manifest_deps_with_scope, lib_imports)
            for dep in flagged:
                all_flagged.append(
                    f"'{name}' declares '{dep}' as dev dependency "
                    f"but imports it in production code"
                )

        if all_flagged:
            return CheckResult(
                "fail",
                f"{len(all_flagged)} dev dep(s) imported in production code",
                details=all_flagged,
            )
        return CheckResult("pass", "no dev deps imported in production code")

    @app.check("deps-stale")
    def check_deps_stale(ctx):
        """Intra-workspace dependency constraints must satisfy current versions."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..commands.monorepo import _evaluate_constraint
        from ..targets import TARGETS, detect_targets

        root = str(ctx.workspace_root)

        # Build version lookup: project name -> current version
        project_versions = {}
        for proj in ctx.projects:
            proj_dir = os.path.join(root, proj["path"])
            target_entries = detect_targets(proj_dir)
            for entry in target_entries:
                target = TARGETS.get(entry.name)
                if target is None:
                    continue
                try:
                    version = target.read_version(entry.path)
                except Exception as e:
                    import sys
                    print(f"Warning: could not read version for {proj['name']} ({entry.name}): {e}", file=sys.stderr)
                    continue
                if version:
                    project_versions[proj["name"]] = version
                    break

        errors = []
        for proj in ctx.projects:
            name = proj["name"]
            deps = ctx.graph.dependencies(name)
            for dep in deps:
                # Only evaluate versioned constraints (not workspace/path/explicit)
                if dep.dep_type != "versioned":
                    continue
                current_version = project_versions.get(dep.name)
                if current_version is None:
                    continue
                status = _evaluate_constraint(dep.constraint, current_version)
                if status == "outdated":
                    errors.append(
                        f"{name} depends on {dep.name} {dep.constraint} "
                        f"but {dep.name} is now {current_version}"
                    )

        if errors:
            return CheckResult(
                "fail",
                f"{len(errors)} stale dependency constraint(s)",
                details=errors,
            )
        return CheckResult("pass", "all intra-workspace constraints are current")

    @app.check("layers-violations")
    def check_layers_violations(ctx):
        """Dependency edges must not violate layer ordering."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from ..layers import check_layer_violations, load_layer_config

        config = load_layer_config(str(ctx.workspace_root))
        if config is None:
            return CheckResult("skip", "layers not configured")

        # Dev-only projects sit outside the layer system entirely
        projects = [p for p in ctx.projects if not project_is_dev_only(p)]

        violations = check_layer_violations(projects, config, ctx.graph)
        if violations:
            return CheckResult(
                "fail",
                f"{len(violations)} layer violation(s)",
                details=violations,
            )
        return CheckResult("pass", "no layer violations")

    @app.check("test-suite-workspace")
    def check_test_suite_workspace(ctx):
        """Run tests for affected workspace projects."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a workspace")

        if ctx.push_stdin is None:
            return CheckResult("skip", "not in push context")

        from ..commands.pre_push_check import _parse_stdin_refs
        from ..git_util import affected_projects as _affected, get_push_changed_files
        from ..targets import detect_targets
        from ..testing import run_project_tests, sync_workspace

        stdin_lines = ctx.push_stdin.strip().splitlines()
        refs = _parse_stdin_refs(stdin_lines)
        if refs is None:
            return CheckResult("skip", "no refs parsed from push stdin")

        changed_files = get_push_changed_files(refs)
        if changed_files is None:
            return CheckResult("skip", "could not determine changed files")

        affected = _affected(changed_files, ctx.projects)

        # Filter out dev-only projects
        affected = [p for p in affected if not project_is_dev_only(p)]

        if not affected:
            return CheckResult("pass", "no affected projects need testing")

        recognized = {"pypi", "go", "npm"}
        failed_projects = []
        passed_count = 0

        # Pre-detect targets so we know if any pypi projects need syncing
        project_targets = []
        has_pypi = False
        for proj in affected:
            project_dir = os.path.join(str(ctx.workspace_root), proj["path"])
            target_entries = detect_targets(project_dir)

            target_name = None
            for name, _path in target_entries:
                if name in recognized:
                    target_name = name
                    break

            project_targets.append((proj, project_dir, target_name))
            if target_name == "pypi":
                has_pypi = True

        # Run uv sync once at workspace root for all pypi sub-projects
        if has_pypi:
            if not sync_workspace(str(ctx.workspace_root)):
                return CheckResult("fail", "uv sync --all-packages failed at workspace root")

        for proj, project_dir, target_name in project_targets:
            if target_name is None:
                # No testable target -- skip this project
                continue

            passed = run_project_tests(
                target_name,
                project_dir=project_dir,
                skip_sync=True,
            )
            if passed:
                passed_count += 1
            else:
                failed_projects.append(proj["name"])

        if failed_projects:
            return CheckResult(
                "fail",
                f"tests failed for: {', '.join(failed_projects)}",
            )
        return CheckResult("pass", f"{passed_count} project(s) tests passed")
