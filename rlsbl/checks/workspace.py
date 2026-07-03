"""Workspace checks (tag: workspace) validating monorepo CI routing, project registration, dev-only boundaries, and inter-project dependency declarations.

Checks: workspace-ci-router, workspace-ci-synced, workspace-targets,
workspace-unregistered, workspace-stale-entries, dev-only-boundary,
unversioned-boundary, dead-workspace-packages, subtree-remote-reachable,
workspace-unbuildable, layers-violations, deps-unused, deps-undeclared,
deps-runtime-test-only, deps-dev-in-lib, deps-stale, root-rlsbl-conflict,
test-suite-workspace.
"""

import json
import os
import subprocess

from strictcli import CheckResult

from ..utils import get_check_timeout
from ..workspace import WorkspaceProject, members_of, project_is_dev_only
from ._common import (
    RLSBL_CONFIG,
    _build_dep_import_cache,
)
from . import PROJECT_MANIFESTS


def register_workspace_checks(app):
    """Register workspace-tag checks on *app*."""

    @app.check("workspace-ci-router")
    def check_workspace_ci_router(ctx):
        """ci-router.yml must exist at the repo root."""
        router = os.path.join(str(ctx.workspace_root), ".github", "workflows", "ci-router.yml")
        if os.path.isfile(router):
            return CheckResult("pass", "ci-router.yml exists")
        return CheckResult("fail", "ci-router.yml not found")

    @app.check("workspace-ci-synced")
    def check_workspace_ci_synced(ctx):
        """Each project must have a synced CI workflow at the repo root."""
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
        from ..targets import collect_releasable_targets, detect_targets, resolve_releasable_config_dir

        def _is_releasable_false(proj):
            if isinstance(proj, WorkspaceProject):
                return proj.releasable is False
            return proj.get("releasable") is False

        # Filter out projects that don't need targets:
        # - dev_only projects (can't release)
        # - releasable=false projects (explicitly non-releasable)
        checkable = [
            proj for proj in ctx.projects
            if not project_is_dev_only(proj) and not _is_releasable_false(proj)
        ]

        missing = []
        for proj in checkable:
            rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
            targets = detect_targets(os.path.join(str(ctx.workspace_root), proj["path"]), releasable_config_dir=rel_dir)
            if not targets:
                missing.append(proj["name"])

        # Verify union of targets per releasable is non-empty
        missing_releasables = []
        for rel in ctx.releasables:
            member_projs = members_of(rel.name, ctx.projects)
            target_names = collect_releasable_targets(rel.name, member_projs, str(ctx.workspace_root))
            if not target_names:
                missing_releasables.append(rel.name)

        details = [f"{n}: no release target found" for n in missing]
        details += [f"releasable '{r}': no targets across any member" for r in missing_releasables]

        if missing or missing_releasables:
            parts = []
            if missing:
                parts.append(f"no targets detected: {', '.join(missing)}")
            if missing_releasables:
                parts.append(f"releasable(s) with no targets: {', '.join(missing_releasables)}")
            return CheckResult("fail", "; ".join(parts), details=details)

        skipped = len(ctx.projects) - len(checkable)
        rel_count = len(ctx.releasables)
        msg = f"all {len(checkable)} project(s) have targets"
        if skipped:
            msg += f" ({skipped} skipped)"
        if rel_count:
            msg += f", {rel_count} releasable(s) verified"
        return CheckResult("pass", msg)

    @app.check("workspace-unregistered")
    def check_workspace_unregistered(ctx):
        """No project directories on disk should be missing from workspace.toml."""
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

    @app.check("unversioned-boundary")
    def check_unversioned_boundary(ctx):
        """Releasable projects must not have runtime deps on unversioned projects.

        An unversioned project (``releasable = false``, not dev-only) is
        skipped by changelog coverage entirely, so its changes would ship
        inside consumer releases with zero changelog trail. Dev-only
        projects are excluded here -- they are dev-only-boundary's job.
        """
        from ..workspace import project_is_releasable

        projects_by_name = {p["name"]: p for p in ctx.projects}

        # Unversioned: explicitly releasable = false, but not dev-only
        unversioned_names = [
            name for name, proj in projects_by_name.items()
            if proj.get("releasable") is False and not project_is_dev_only(proj)
        ]

        if not unversioned_names:
            return CheckResult("pass", "no unversioned projects")

        violations = []
        for unv_name in unversioned_names:
            # Collect releasable dependents: runtime and explicit scopes
            dependents = set()
            for scope in ("runtime", "explicit"):
                try:
                    rdeps = ctx.graph.transitive_rdeps(unv_name, scope_filter=scope)
                except KeyError:
                    continue
                dependents.update(rdeps)

            for dep_name in sorted(dependents):
                dep_proj = projects_by_name.get(dep_name)
                if dep_proj is None:
                    continue
                if project_is_releasable(dep_proj):
                    violations.append(
                        f"releasable project '{dep_name}' has a runtime dependency "
                        f"on unversioned project '{unv_name}' (releasable = false). "
                        f"Changes in '{unv_name}' ship inside '{dep_name}' releases "
                        f"with no changelog coverage."
                    )

        if violations:
            return CheckResult(
                "fail",
                f"{len(violations)} boundary violation(s)",
                details=violations,
            )
        return CheckResult("pass", "unversioned boundary clean")

    @app.check("dead-workspace-packages")
    def check_dead_workspace_packages(ctx):
        """Library packages must be imported by at least one workspace sibling.

        Published releasable members (non-private, with pipelines) are
        exempt -- they are consumed externally via a package registry.
        """
        from ..dep_validation import find_dead_workspace_packages
        from ..member_context import resolve_member_context
        from ..pipelines import load_pipelines
        from ..targets import resolve_releasable_config_dir

        import_cache = _build_dep_import_cache(ctx)

        # Build set of published member names for exemption
        published_members = set()
        for rel in ctx.releasables:
            rel_members = members_of(rel.name, ctx.projects)
            for proj in rel_members:
                abs_pkg = os.path.join(str(ctx.workspace_root), proj["path"])
                rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
                try:
                    member = resolve_member_context(
                        abs_pkg, releasable_config_dir=rel_dir,
                    )
                    if member.is_private:
                        continue
                    pipelines = load_pipelines(member.config)
                    if pipelines:
                        published_members.add(proj["name"])
                except Exception:
                    continue

        dead = find_dead_workspace_packages(
            ctx.projects, import_cache,
            published_members=published_members,
        )

        if not dead:
            msg = "all library packages have workspace importers"
            if published_members:
                msg += f" ({len(published_members)} published member(s) exempt)"
            return CheckResult("pass", msg)

        details = [d.message for d in dead]
        return CheckResult(
            "warn",
            f"{len(dead)} dead workspace package(s)",
            details=details,
        )

    @app.check("subtree-remote-reachable")
    def check_subtree_remote_reachable(ctx):
        """Every project with subtree_remote must have a reachable remote."""
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
        # Only relevant when there are pypi-target projects in the workspace
        from ..targets import detect_targets, resolve_releasable_config_dir

        root = str(ctx.workspace_root)
        has_pypi = False
        for proj in ctx.projects:
            proj_dir = os.path.join(root, proj["path"])
            rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
            target_entries = detect_targets(proj_dir, releasable_config_dir=rel_dir)
            if any(e.name == "pypi" for e in target_entries):
                has_pypi = True
                break

        if not has_pypi:
            return CheckResult("skip", "no pypi-target projects in workspace")

        timeout = get_check_timeout(ctx.config)
        try:
            result = subprocess.run(
                ["uv", "sync", "--all-packages", "--dry-run"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return CheckResult("skip", "uv not installed")
        except subprocess.TimeoutExpired:
            return CheckResult("fail", f"uv sync --all-packages --dry-run timed out after {timeout}s")

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
        from ..commands.monorepo import _evaluate_constraint
        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir

        root = str(ctx.workspace_root)

        # Build version lookup: project name -> current version
        project_versions = {}
        for proj in ctx.projects:
            proj_dir = os.path.join(root, proj["path"])
            rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
            target_entries = detect_targets(proj_dir, releasable_config_dir=rel_dir)
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
        from ..layers import check_layer_violations, load_layer_config

        config = load_layer_config(str(ctx.workspace_root))
        if config is None:
            return CheckResult("skip", "layers not configured")

        violations = check_layer_violations(ctx.projects, config, ctx.graph)
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
        if ctx.push_stdin is None:
            return CheckResult("skip", "not in push context")

        from ..commands.pre_push_check import _parse_stdin_refs
        from ..git_util import affected_projects as _affected, get_push_changed_files
        from ..targets import detect_targets, resolve_releasable_config_dir
        from ..testing import run_project_tests, sync_workspace

        stdin_lines = ctx.push_stdin.strip().splitlines()
        refs = _parse_stdin_refs(stdin_lines)
        if refs is None:
            return CheckResult("skip", "no refs parsed from push stdin")

        changed_files = get_push_changed_files(refs)
        if changed_files is None:
            return CheckResult("skip", "could not determine changed files")

        affected = _affected(changed_files, ctx.projects)

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
            rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
            target_entries = detect_targets(project_dir, releasable_config_dir=rel_dir)

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
            timeout = get_check_timeout(ctx.config)
            if not sync_workspace(str(ctx.workspace_root), check_timeout=timeout):
                return CheckResult("fail", "uv sync --all-packages failed at workspace root")

        for proj, project_dir, target_name in project_targets:
            if target_name is None:
                # No testable target -- skip this project
                continue

            passed = run_project_tests(
                target_name,
                project_dir=project_dir,
                workspace_root=str(ctx.workspace_root),
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

    @app.check("scaffold-gitignore-stale")
    def check_scaffold_gitignore_stale(ctx):
        """Workspace project .gitignore files must contain rlsbl-managed entries."""
        from importlib.resources import files as pkg_files

        # Load expected entries from the scaffold gitignore template
        template_text = (
            pkg_files("rlsbl") / "templates" / "shared" / "gitignore.tpl"
        ).read_text()
        # Only check rlsbl-specific entries (lines containing ".rlsbl")
        rlsbl_entries = [
            line.strip()
            for line in template_text.splitlines()
            if ".rlsbl" in line and line.strip() and not line.strip().startswith("#")
        ]

        if not rlsbl_entries:
            return CheckResult("pass", "no rlsbl-specific gitignore entries in template")

        ws_root = str(ctx.workspace_root)
        missing_projects = []

        for proj in ctx.projects:
            proj_dir = os.path.join(ws_root, proj["path"])
            gitignore_path = os.path.join(proj_dir, ".gitignore")
            if not os.path.isfile(gitignore_path):
                missing_projects.append(
                    f"{proj['name']}: .gitignore not found"
                )
                continue

            try:
                with open(gitignore_path, encoding="utf-8") as f:
                    gitignore_lines = {line.strip() for line in f}
            except OSError:
                missing_projects.append(
                    f"{proj['name']}: could not read .gitignore"
                )
                continue

            missing_entries = [
                entry for entry in rlsbl_entries
                if entry not in gitignore_lines
            ]
            if missing_entries:
                missing_projects.append(
                    f"{proj['name']}: missing {', '.join(missing_entries)}"
                )

        if missing_projects:
            return CheckResult(
                "warn",
                f"{len(missing_projects)} project(s) with stale .gitignore",
                details=missing_projects,
            )
        return CheckResult("pass", "all project .gitignore files are up to date")

    @app.check("root-rlsbl-conflict")
    def check_root_rlsbl_conflict(ctx):
        """Root .rlsbl/ must not coexist with .rlsbl-monorepo/."""
        root = str(ctx.workspace_root)
        has_rlsbl = os.path.isdir(os.path.join(root, ".rlsbl"))
        has_monorepo = os.path.isdir(os.path.join(root, ".rlsbl-monorepo"))
        if has_rlsbl and has_monorepo:
            return CheckResult(
                "fail",
                "root .rlsbl/ conflicts with .rlsbl-monorepo/ "
                "— remove root .rlsbl/ after migrating its contents to the releasable",
            )
        return CheckResult("pass", "no root config conflict")

    @app.check("go-companion-tags")
    def check_go_companion_tags(ctx):
        """Releasables with non-private Go members should have companion tags."""
        from ..errors import ConfigError
        from ..member_context import resolve_member_context
        from ..targets import resolve_releasable_config_dir
        from ..workspace import read_releasable_version

        if not ctx.releasables:
            return CheckResult("skip", "no releasables defined")

        root = str(ctx.workspace_root)
        missing = []
        config_errors = []
        checked_any = False

        for rel in ctx.releasables:
            member_projs = members_of(rel.name, ctx.projects)
            if not member_projs:
                continue

            # Read the releasable's current version. An unreadable version
            # is a check FAILURE naming the releasable, not a silent skip --
            # same no-silent-skip rule as broken member configs below.
            try:
                version = read_releasable_version(root, rel.name)
            except Exception as e:
                config_errors.append(
                    f"{rel.name}: cannot read releasable version: {e}"
                )
                continue

            for proj in member_projs:
                pkg_path = proj["path"]
                abs_pkg = os.path.join(root, pkg_path)

                # Resolve effective config and targets with releasable
                # inheritance (single source of truth: member_context).
                # A broken member config is a check FAILURE, not a silent
                # skip -- the release flow hard-errors on the same config,
                # so the check must not disagree about the member set.
                rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
                try:
                    member = resolve_member_context(
                        abs_pkg, releasable_config_dir=rel_dir,
                    )
                    if member.is_private:
                        continue
                    has_go = any(e.name == "go" for e in member.targets)
                except ConfigError as e:
                    config_errors.append(
                        f"{rel.name}/{proj['name']}: member config error: {e}"
                    )
                    continue
                if not has_go:
                    continue

                checked_any = True

                # Check if the companion tag exists
                sep = "" if pkg_path.endswith("/") else "/"
                expected_tag = f"{pkg_path}{sep}v{version}"
                result = subprocess.run(
                    ["git", "tag", "-l", expected_tag],
                    capture_output=True, text=True, cwd=root,
                )
                if not result.stdout.strip():
                    missing.append(
                        f"{rel.name}/{proj['name']}: missing companion tag {expected_tag}"
                    )

        if config_errors:
            return CheckResult(
                "fail",
                f"{len(config_errors)} releasable/member config error(s)",
                details=config_errors,
            )

        if not checked_any:
            return CheckResult("skip", "no non-private Go members in releasables")

        if missing:
            return CheckResult(
                "warn",
                f"{len(missing)} Go companion tag(s) missing",
                details=missing,
            )
        return CheckResult("pass", "all Go companion tags exist")

    @app.check("releasable-residue")
    def check_releasable_residue(ctx):
        """Releasable member packages must not carry per-package release state."""
        from ..releasable_cleanup import verify_minimal_rlsbl

        if not ctx.releasables:
            return CheckResult("skip", "no releasables defined")

        root = str(ctx.workspace_root)
        findings = []
        for rel in ctx.releasables:
            for proj in members_of(rel.name, ctx.projects):
                abs_pkg = os.path.join(root, proj["path"])
                # Root-path members are exempt: their .rlsbl/ and root
                # CHANGELOG.md are workspace-level files.
                if os.path.realpath(abs_pkg) == os.path.realpath(root):
                    continue
                for entry in verify_minimal_rlsbl(abs_pkg):
                    findings.append(
                        f"{rel.name}/{proj['name']}: .rlsbl/{entry}"
                    )

        if findings:
            return CheckResult(
                "fail",
                f"{len(findings)} per-package release-state residue item(s); "
                f"run `rlsbl monorepo cleanup` to remove them",
                details=findings,
            )
        return CheckResult("pass", "no per-package release-state residue")
