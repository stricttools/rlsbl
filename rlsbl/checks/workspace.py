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

from ..utils import get_check_timeout, tag_exists_locally
from ..workspace import WorkspaceProject, members_of, project_is_dev_only
from ._common import (
    RLSBL_CONFIG,
    _build_dep_import_cache,
)
from . import PROJECT_MANIFESTS


def register_workspace_checks(app):
    """Register workspace-tag checks on *app*."""

    @app.error_check("workspace-ci-router")
    def check_workspace_ci_router(ctx, reporter):
        """ci-router.yml must exist at the repo root."""
        router = os.path.join(str(ctx.workspace_root), ".github", "workflows", "ci-router.yml")
        if os.path.isfile(router):
            return reporter.passed("ci-router.yml exists")
        reporter.error("ci-router.yml not found")
        return reporter.found("ci-router.yml not found")

    @app.error_check("workspace-ci-synced")
    def check_workspace_ci_synced(ctx, reporter):
        """Each project's CI jobs must be inlined into the shared ci-router.yml."""
        from ..targets import detect_targets, TARGETS, resolve_releasable_config_dir
        from ..ci_router import _router_ci_job_keys
        from ruamel.yaml import YAML

        required = []
        skipped = 0
        for proj in ctx.projects:
            proj_dir = os.path.join(str(ctx.workspace_root), proj["path"])
            rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
            try:
                entries = detect_targets(proj_dir, releasable_config_dir=rel_dir)
            except Exception:
                entries = []
            if entries:
                has_ci = any(
                    "ci_templates" in TARGETS[e.name].capabilities
                    for e in entries
                    if e.name in TARGETS
                )
                if not has_ci:
                    skipped += 1
                    continue
            required.append((proj["name"], _router_ci_job_keys(proj)))

        if not required:
            msg = "no in-scope projects require CI inlining"
            if skipped:
                msg += f" ({skipped} skipped, no ci_templates capability)"
            return reporter.passed(msg)

        router_path = os.path.join(
            str(ctx.workspace_root), ".github", "workflows", "ci-router.yml"
        )
        if not os.path.isfile(router_path):
            reporter.error("ci-router.yml not found -- run `rlsbl monorepo sync`")
            return reporter.found("ci-router.yml not found -- run `rlsbl monorepo sync`")
        try:
            with open(router_path, "r", encoding="utf-8") as f:
                router = YAML(typ="safe").load(f)
        except Exception as e:
            reporter.error(f"cannot parse ci-router.yml: {e}")
            return reporter.found(f"cannot parse ci-router.yml: {e}")

        router_jobs = set((router or {}).get("jobs", {}).keys())

        missing = []
        for name, prefixes in required:
            for prefix in prefixes:
                if not any(
                    k == prefix or k.startswith(prefix + "-") for k in router_jobs
                ):
                    missing.append(f"{name} ({prefix})")

        if missing:
            for m in missing:
                reporter.error(f"{m}: no jobs keyed for this project in ci-router.yml")
            return reporter.found(
                f"projects not inlined in ci-router.yml: {', '.join(missing)}"
            )
        msg = f"all {len(required)} project(s) inlined in ci-router.yml"
        if skipped:
            msg += f" ({skipped} skipped, no ci_templates capability)"
        return reporter.passed(msg)

    @app.error_check("workspace-targets")
    def check_workspace_targets(ctx, reporter):
        """Every project must have at least one detectable target."""
        from ..targets import collect_releasable_targets, detect_targets, resolve_releasable_config_dir

        def _is_releasable_false(proj):
            if isinstance(proj, WorkspaceProject):
                return proj.releasable is False
            return proj.get("releasable") is False

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

        from ..errors import ConfigError

        missing_releasables = []
        banned_releasables = []
        for rel in ctx.releasables:
            member_projs = members_of(rel.name, ctx.projects)
            try:
                target_names = collect_releasable_targets(rel.name, member_projs, str(ctx.workspace_root))
            except ConfigError as e:
                banned_releasables.append((rel.name, str(e)))
                continue
            if not target_names:
                missing_releasables.append(rel.name)

        if missing or missing_releasables or banned_releasables:
            for n in missing:
                reporter.error(f"{n}: no release target found")
            for r in missing_releasables:
                reporter.error(f"releasable '{r}': no targets across any member")
            for r, msg in banned_releasables:
                reporter.error(f"releasable '{r}': {msg}")
            parts = []
            if missing:
                parts.append(f"no targets detected: {', '.join(missing)}")
            if missing_releasables:
                parts.append(f"releasable(s) with no targets: {', '.join(missing_releasables)}")
            if banned_releasables:
                parts.append(
                    f"releasable(s) with banned empty targets: "
                    f"{', '.join(n for n, _ in banned_releasables)}"
                )
            return reporter.found("; ".join(parts))

        skipped_count = len(ctx.projects) - len(checkable)
        rel_count = len(ctx.releasables)
        msg = f"all {len(checkable)} project(s) have targets"
        if skipped_count:
            msg += f" ({skipped_count} skipped)"
        if rel_count:
            msg += f", {rel_count} releasable(s) verified"
        return reporter.passed(msg)

    @app.error_check("workspace-unregistered")
    def check_workspace_unregistered(ctx, reporter):
        """No project directories on disk should be missing from workspace.toml."""
        root = str(ctx.workspace_root)
        registered_paths = {proj["path"].rstrip("/") for proj in ctx.projects}

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
            if os.path.isfile(os.path.join(dir_path, RLSBL_CONFIG)):
                found_project_dirs.add(entry)
                continue
            for manifest in PROJECT_MANIFESTS:
                if os.path.isfile(os.path.join(dir_path, manifest)):
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

        found_project_dirs -= {
            d for d in found_project_dirs
            if any(rp.startswith(d + "/") for rp in registered_paths)
        }

        unregistered = sorted(found_project_dirs - registered_paths)
        if unregistered:
            for d in unregistered:
                reporter.error(f"{d}: has manifest but not in workspace.toml")
            return reporter.found(f"{len(unregistered)} unregistered project(s)")
        return reporter.passed("no unregistered projects")

    @app.error_check("workspace-stale-entries")
    def check_workspace_stale_entries(ctx, reporter):
        """No workspace.toml entries should point to missing or manifest-less dirs."""
        root = str(ctx.workspace_root)

        stale = []
        for proj in ctx.projects:
            dir_path = os.path.join(root, proj["path"])
            if not os.path.isdir(dir_path):
                stale.append(proj["path"])
                continue
            if os.path.isfile(os.path.join(dir_path, RLSBL_CONFIG)):
                continue
            has_manifest = any(
                os.path.isfile(os.path.join(dir_path, m)) for m in PROJECT_MANIFESTS
            )
            if not has_manifest:
                stale.append(proj["path"])

        if stale:
            for s in stale:
                reporter.error(f"{s}: directory missing or no manifest")
            return reporter.found(f"{len(stale)} stale workspace entry(ies)")
        return reporter.passed("no stale entries")

    @app.error_check("dev-only-boundary")
    def check_dev_only_boundary(ctx, reporter):
        """Non-dev-only projects must not have runtime deps on dev-only projects."""
        projects_by_name = {p["name"]: p for p in ctx.projects}

        dev_only_names = [
            name for name, proj in projects_by_name.items()
            if project_is_dev_only(proj)
        ]

        if not dev_only_names:
            return reporter.passed("no dev-only projects")

        violations = []
        for dev_name in dev_only_names:
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
            for v in violations:
                reporter.error(v)
            return reporter.found(f"{len(violations)} boundary violation(s)")
        return reporter.passed("dev-only boundary clean")

    @app.error_check("unversioned-boundary")
    def check_unversioned_boundary(ctx, reporter):
        """Releasable projects must not have runtime deps on unversioned projects."""
        from ..workspace import project_is_releasable

        projects_by_name = {p["name"]: p for p in ctx.projects}

        unversioned_names = [
            name for name, proj in projects_by_name.items()
            if proj.get("releasable") is False and not project_is_dev_only(proj)
        ]

        if not unversioned_names:
            return reporter.passed("no unversioned projects")

        violations = []
        for unv_name in unversioned_names:
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
            for v in violations:
                reporter.error(v)
            return reporter.found(f"{len(violations)} boundary violation(s)")
        return reporter.passed("unversioned boundary clean")

    @app.warn_check("dead-workspace-packages")
    def check_dead_workspace_packages(ctx, reporter):
        """Library packages must be imported by at least one workspace sibling."""
        from ..dep_validation import find_dead_workspace_packages
        from ..member_context import resolve_member_context
        from ..pipelines import load_pipelines
        from ..targets import resolve_releasable_config_dir

        import_cache = _build_dep_import_cache(ctx)

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
                    if member.publish_mode == "none":
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
            return reporter.passed(msg)

        for d in dead:
            reporter.warn(d.message)
        return reporter.found(f"{len(dead)} dead workspace package(s)")

    @app.error_check("subtree-remote-reachable")
    def check_subtree_remote_reachable(ctx, reporter):
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
            return reporter.skipped("no projects have subtree_remote")

        if errors:
            for err in errors:
                reporter.error(err)
            return reporter.found(f"{len(errors)} unreachable subtree remote(s)")
        return reporter.passed(f"all {checked} subtree remote(s) reachable")

    @app.error_check("workspace-unbuildable")
    def check_workspace_unbuildable(ctx, reporter):
        """Detect workspace members that fail ``uv sync --all-packages``."""
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
            return reporter.skipped("no pypi-target projects in workspace")

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
            return reporter.skipped("uv not installed")
        except subprocess.TimeoutExpired:
            reporter.error(f"uv sync --all-packages --dry-run timed out after {timeout}s")
            return reporter.found(f"uv sync --all-packages --dry-run timed out after {timeout}s")

        if result.returncode == 0:
            return reporter.passed("all workspace members buildable")

        stderr = result.stderr.strip()
        details = [line for line in stderr.splitlines() if line.strip()]
        summary = details[0] if details else "uv sync --all-packages --dry-run failed"
        for detail in details:
            reporter.error(detail)
        return reporter.found(summary)

    @app.error_check("deps-unused")
    def check_deps_unused(ctx, reporter):
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
            for err in all_errors:
                reporter.error(err)
            return reporter.found(f"{len(all_errors)} unused dependency(ies)")
        return reporter.passed("no unused workspace dependencies")

    @app.error_check("deps-undeclared")
    def check_deps_undeclared(ctx, reporter):
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
            for err in all_errors:
                reporter.error(err)
            return reporter.found(f"{len(all_errors)} undeclared dependency(ies)")
        return reporter.passed("no undeclared workspace dependencies")

    @app.warn_check("deps-runtime-test-only")
    def check_deps_runtime_test_only(ctx, reporter):
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
            for f in all_flagged:
                reporter.warn(f)
            return reporter.found(f"{len(all_flagged)} runtime dep(s) used only in tests")
        return reporter.passed("no runtime deps used only in tests")

    @app.error_check("deps-dev-in-lib")
    def check_deps_dev_in_lib(ctx, reporter):
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
            for f in all_flagged:
                reporter.error(f)
            return reporter.found(f"{len(all_flagged)} dev dep(s) imported in production code")
        return reporter.passed("no dev deps imported in production code")

    @app.error_check("deps-stale")
    def check_deps_stale(ctx, reporter):
        """Intra-workspace dependency constraints must satisfy current versions."""
        from ..constraints import _evaluate_constraint
        from ..targets import TARGETS, detect_targets, resolve_releasable_config_dir

        root = str(ctx.workspace_root)

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
            for err in errors:
                reporter.error(err)
            return reporter.found(f"{len(errors)} stale dependency constraint(s)")
        return reporter.passed("all intra-workspace constraints are current")

    @app.error_check("layers-violations")
    def check_layers_violations(ctx, reporter):
        """Dependency edges must not violate layer ordering."""
        from ..layers import check_layer_violations, load_layer_config

        config = load_layer_config(str(ctx.workspace_root))
        if config is None:
            return reporter.skipped("layers not configured")

        violations = check_layer_violations(ctx.projects, config, ctx.graph)
        if violations:
            for v in violations:
                reporter.error(v)
            return reporter.found(f"{len(violations)} layer violation(s)")
        return reporter.passed("no layer violations")

    @app.error_check("test-suite-workspace")
    def check_test_suite_workspace(ctx, reporter):
        """Run tests for affected workspace projects."""
        if ctx.push_stdin is None:
            return reporter.skipped("not in push context")

        from ..prepush_utils import _parse_stdin_refs
        from ..git_util import affected_projects as _affected, get_push_changed_files
        from ..targets import detect_targets, resolve_releasable_config_dir
        from ..testing import run_project_tests, sync_workspace

        stdin_lines = ctx.push_stdin.strip().splitlines()
        refs = _parse_stdin_refs(stdin_lines)
        if refs is None:
            return reporter.skipped("no refs parsed from push stdin")

        changed_files = get_push_changed_files(refs)
        if changed_files is None:
            return reporter.skipped("could not determine changed files")

        affected = _affected(changed_files, ctx.projects)

        if not affected:
            return reporter.passed("no affected projects need testing")

        recognized = {"pypi", "go", "npm", "maven"}
        failed_projects = []
        passed_count = 0

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

        if has_pypi:
            timeout = get_check_timeout(ctx.config)
            if not sync_workspace(str(ctx.workspace_root), check_timeout=timeout):
                reporter.error("uv sync --all-packages failed at workspace root")
                return reporter.found("uv sync --all-packages failed at workspace root")

        for proj, project_dir, target_name in project_targets:
            if target_name is None:
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
            reporter.error(f"tests failed for: {', '.join(failed_projects)}")
            return reporter.found(f"tests failed for: {', '.join(failed_projects)}")
        return reporter.passed(f"{passed_count} project(s) tests passed")

    @app.warn_check("scaffold-gitignore-stale")
    def check_scaffold_gitignore_stale(ctx, reporter):
        """Workspace project .gitignore files must contain rlsbl-managed entries."""
        from importlib.resources import files as pkg_files

        template_text = (
            pkg_files("rlsbl") / "templates" / "shared" / "gitignore.tpl"
        ).read_text()
        rlsbl_entries = [
            line.strip()
            for line in template_text.splitlines()
            if ".rlsbl" in line and line.strip() and not line.strip().startswith("#")
        ]

        if not rlsbl_entries:
            return reporter.passed("no rlsbl-specific gitignore entries in template")

        ws_root = str(ctx.workspace_root)
        missing_projects = []

        for proj in ctx.projects:
            proj_dir = os.path.join(ws_root, proj["path"])
            gitignore_path = os.path.join(proj_dir, ".gitignore")
            if not os.path.isfile(gitignore_path):
                missing_projects.append(f"{proj['name']}: .gitignore not found")
                continue

            try:
                with open(gitignore_path, encoding="utf-8") as f:
                    gitignore_lines = {line.strip() for line in f}
            except OSError:
                missing_projects.append(f"{proj['name']}: could not read .gitignore")
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
            for mp in missing_projects:
                reporter.warn(mp)
            return reporter.found(f"{len(missing_projects)} project(s) with stale .gitignore")
        return reporter.passed("all project .gitignore files are up to date")

    @app.error_check("root-rlsbl-conflict")
    def check_root_rlsbl_conflict(ctx, reporter):
        """Root .rlsbl/ must not coexist with .rlsbl-monorepo/."""
        root = str(ctx.workspace_root)
        has_rlsbl = os.path.isdir(os.path.join(root, ".rlsbl"))
        has_monorepo = os.path.isdir(os.path.join(root, ".rlsbl-monorepo"))
        if has_rlsbl and has_monorepo:
            msg = (
                "root .rlsbl/ conflicts with .rlsbl-monorepo/ "
                "-- remove root .rlsbl/ after migrating its contents to the releasable"
            )
            reporter.error(msg)
            return reporter.found(msg)
        return reporter.passed("no root config conflict")

    @app.warn_check("go-companion-tags")
    def check_go_companion_tags(ctx, reporter):
        """Releasables with publishing Go members should have companion tags."""
        from ..errors import ConfigError
        from ..member_context import resolve_member_context
        from ..targets import resolve_releasable_config_dir
        from ..workspace import read_releasable_version

        if not ctx.releasables:
            return reporter.skipped("no releasables defined")

        root = str(ctx.workspace_root)
        missing = []
        config_errors = []
        checked_any = False

        for rel in ctx.releasables:
            member_projs = members_of(rel.name, ctx.projects)
            if not member_projs:
                continue

            try:
                version = read_releasable_version(root, rel.name)
            except Exception as e:
                config_errors.append(f"{rel.name}: cannot read releasable version: {e}")
                continue

            for proj in member_projs:
                pkg_path = proj["path"]
                abs_pkg = os.path.join(root, pkg_path)

                rel_dir = resolve_releasable_config_dir(proj, ctx.workspace_root)
                try:
                    member = resolve_member_context(
                        abs_pkg, releasable_config_dir=rel_dir,
                    )
                    if member.publish_mode == "none":
                        continue
                    has_go = any(e.name == "go" for e in member.targets)
                except ConfigError as e:
                    config_errors.append(f"{rel.name}/{proj['name']}: member config error: {e}")
                    continue
                if not has_go:
                    continue

                checked_any = True

                sep = "" if pkg_path.endswith("/") else "/"
                expected_tag = f"{pkg_path}{sep}v{version}"
                if not tag_exists_locally(expected_tag, cwd=root):
                    missing.append(f"{rel.name}/{proj['name']}: missing companion tag {expected_tag}")

        if config_errors:
            for ce in config_errors:
                reporter.warn(ce)
            return reporter.found(f"{len(config_errors)} releasable/member config error(s)")

        if not checked_any:
            return reporter.skipped("no publishing Go members in releasables")

        if missing:
            for m in missing:
                reporter.warn(m)
            return reporter.found(f"{len(missing)} Go companion tag(s) missing")
        return reporter.passed("all Go companion tags exist")

    @app.error_check("releasable-residue")
    def check_releasable_residue(ctx, reporter):
        """Releasable member packages must not carry per-package release state."""
        from ..releasable_cleanup import verify_minimal_rlsbl

        if not ctx.releasables:
            return reporter.skipped("no releasables defined")

        root = str(ctx.workspace_root)
        findings = []
        for rel in ctx.releasables:
            for proj in members_of(rel.name, ctx.projects):
                abs_pkg = os.path.join(root, proj["path"])
                if os.path.realpath(abs_pkg) == os.path.realpath(root):
                    continue
                for entry in verify_minimal_rlsbl(abs_pkg):
                    findings.append(f"{rel.name}/{proj['name']}: .rlsbl/{entry}")

        if findings:
            for f in findings:
                reporter.error(f)
            return reporter.found(
                f"{len(findings)} per-package release-state residue item(s); "
                f"run `rlsbl monorepo cleanup` to remove them"
            )
        return reporter.passed("no per-package release-state residue")

    @app.error_check("member-pytest-config")
    def check_member_pytest_config(ctx, reporter):
        """Members with tests must pin their own pytest rootdir when the root
        has a conftest.py."""
        import tomllib

        root = str(ctx.workspace_root)
        if not os.path.isfile(os.path.join(root, "conftest.py")):
            return reporter.skipped(
                "workspace root has no conftest.py; no pytest rootdir-escape hazard"
            )

        findings = []
        for proj in ctx.projects:
            abs_pkg = os.path.join(root, proj["path"])
            if os.path.realpath(abs_pkg) == os.path.realpath(root):
                continue

            pyproject = os.path.join(abs_pkg, "pyproject.toml")
            if not os.path.isfile(pyproject):
                continue

            if not os.path.isdir(os.path.join(abs_pkg, "tests")):
                continue

            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError):
                findings.append(proj["name"])
                continue

            has_config = "ini_options" in data.get("tool", {}).get("pytest", {})
            if not has_config:
                findings.append(proj["name"])

        if findings:
            for name in findings:
                reporter.error(
                    f"{name}: add a [tool.pytest.ini_options] table to "
                    f'{name}/pyproject.toml (e.g. testpaths = ["tests"]) to '
                    "pin pytest's rootdir to the member"
                )
            return reporter.found(
                f"{len(findings)} member(s) with tests but no own "
                "[tool.pytest.ini_options] while the workspace root has a "
                "conftest.py -- pytest's rootdir escapes to the workspace "
                "root, silently loading the root conftest and its config"
            )
        return reporter.passed("all members with tests pin their own pytest config")
