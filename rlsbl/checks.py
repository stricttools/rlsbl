"""Doctor checks migrated to the strictcli check system.

Each check is registered via ``@app.check("name")`` and receives a
:class:`~rlsbl.check_context.ProjectCheckContext` (or its
:class:`~rlsbl.check_context.WorkspaceCheckContext` subclass).

The check functions return :class:`strictcli.CheckResult` with lowercase
status strings: ``"pass"``, ``"fail"``, ``"warn"``, ``"skip"``.
"""

import os
import subprocess
import sys

from strictcli import CheckResult

from .check_context import WorkspaceCheckContext


def _resolve_version_and_tag(ctx):
    """Detect version and tag from project targets rooted at *ctx*.

    Returns ``(version, tag)``; either may be ``None``.
    """
    from .targets import TARGETS, detect_targets

    target_entries = detect_targets(str(ctx.project_root))
    if not target_entries:
        return None, None

    first_name, first_path = target_entries[0]
    target = TARGETS[first_name]
    try:
        version = target.read_version(first_path)
    except Exception:
        version = None
    tag = target.tag_format(version) if version else None
    return version, tag


def register_checks(app):
    """Register all 11 doctor checks on *app*.

    Silently returns if the check system is not enabled (i.e. no
    ``.strictcli/checks.toml`` was found at import time -- this happens
    when rlsbl is imported from a working directory that is not the
    rlsbl source tree, e.g. a user's project directory).
    """
    if not getattr(app, "_checks_enabled", False):
        return

    # ------------------------------------------------------------------
    # Tag: project
    # ------------------------------------------------------------------

    @app.check("lock")
    def check_lock(ctx):
        """Detect stale lock files."""
        from .lock import is_stale

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            stale_paths = []
            if is_stale():
                stale_paths.append(".rlsbl/lock")
            if is_stale(lock_path=os.path.join(".rlsbl-monorepo", "lock")):
                stale_paths.append(".rlsbl-monorepo/lock")
        finally:
            os.chdir(original_cwd)

        if stale_paths:
            return CheckResult("warn", f"stale lock file exists at {', '.join(stale_paths)}")
        return CheckResult("pass", "no lock file")

    @app.check("version-consistency")
    def check_version_consistency(ctx):
        """All detected targets must report the same version."""
        from .targets import TARGETS, detect_targets

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("warn", "no targets detected")

        versions = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                v = target.read_version(path)
                versions[name] = v
            except Exception:
                versions[name] = None

        unique = set(v for v in versions.values() if v is not None)
        if len(unique) == 0:
            return CheckResult("warn", "no targets reported a version")
        if len(unique) > 1:
            detail = ", ".join(f"{n}={v}" for n, v in versions.items() if v is not None)
            return CheckResult("fail", f"version mismatch: {detail}")

        version = unique.pop()
        return CheckResult("pass", f"{version} across {len(target_entries)} target(s)")

    @app.check("name-consistency")
    def check_name_consistency(ctx):
        """All detected targets must report the same package name."""
        from .targets import TARGETS, detect_targets
        from .targets.utils import normalize_go, normalize_npm, normalize_pypi

        def _normalize_name(target_name, raw_name):
            normalizers = {
                "npm": normalize_npm,
                "pypi": normalize_pypi,
                "go": normalize_go,
            }
            normalizer = normalizers.get(target_name, str.lower)
            return normalizer(raw_name)

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("warn", "no targets detected")

        names = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                n = target.read_name(path)
                names[name] = n
            except Exception:
                names[name] = None

        have_name = {k: v for k, v in names.items() if v is not None}
        if not have_name:
            return CheckResult("warn", "no targets reported a name")

        missing = [k for k, v in names.items() if v is None]
        normalized = {k: _normalize_name(k, v) for k, v in have_name.items()}
        unique = set(normalized.values())

        if len(unique) == 1:
            raw_name = next(iter(have_name.values()))
            msg = f"{raw_name} across {len(target_entries)} target(s)"
            if missing:
                msg += f" (no name from: {', '.join(missing)})"
            return CheckResult("pass", msg)

        detail = ", ".join(f"{k}={v}" for k, v in have_name.items())
        return CheckResult("warn", f"name mismatch: {detail}")

    @app.check("license-consistency")
    def check_license_consistency(ctx):
        """All detected targets must report the same license."""
        from .targets import TARGETS, detect_targets

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("pass", "no targets declare a license")

        licenses = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                meta = target.read_metadata(path)
                if "license" in meta:
                    licenses[name] = meta["license"]
            except Exception:
                pass

        if len(licenses) == 0:
            return CheckResult("pass", "no targets declare a license")
        if len(licenses) < 2:
            return CheckResult("pass", f"only {len(licenses)} target(s) declare a license")

        unique = set(v.lower() for v in licenses.values())
        if len(unique) == 1:
            license_val = next(iter(licenses.values()))
            return CheckResult("pass", f"{license_val} across {len(licenses)} target(s)")

        detail = ", ".join(f"{k}={v}" for k, v in licenses.items())
        return CheckResult("warn", f"license mismatch: {detail}")

    @app.check("description-consistency")
    def check_description_consistency(ctx):
        """All detected targets must report the same description."""
        from .targets import TARGETS, detect_targets

        target_entries = detect_targets(str(ctx.project_root))
        if not target_entries:
            return CheckResult("pass", "no targets declare a description")

        descriptions = {}
        for name, path in target_entries:
            target = TARGETS[name]
            try:
                meta = target.read_metadata(path)
                if "description" in meta:
                    descriptions[name] = meta["description"]
            except Exception:
                pass

        if len(descriptions) == 0:
            return CheckResult("pass", "no targets declare a description")
        if len(descriptions) < 2:
            return CheckResult("pass", f"only {len(descriptions)} target(s) declare a description")

        unique = set(descriptions.values())
        if len(unique) == 1:
            desc_val = next(iter(descriptions.values()))
            return CheckResult("pass", f"{desc_val} across {len(descriptions)} target(s)")

        detail = ", ".join(f"{k}={v}" for k, v in descriptions.items())
        return CheckResult("warn", f"description mismatch: {detail}")

    # ------------------------------------------------------------------
    # Tag: release
    # ------------------------------------------------------------------

    @app.check("local-tag")
    def check_local_tag(ctx):
        """Git tag for the current version must exist locally."""
        from .utils import run

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return CheckResult("skip", "no version detected")

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            output = run("git", ["tag", "-l", tag])
        finally:
            os.chdir(original_cwd)

        if output:
            return CheckResult("pass", f"{tag} exists")
        return CheckResult("warn", f"{tag} not found locally")

    @app.check("remote-tag")
    def check_remote_tag(ctx):
        """Git tag for the current version must exist on origin."""
        from .utils import run

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return CheckResult("skip", "no version detected")

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            output = run("git", ["ls-remote", "--tags", "origin", tag])
        finally:
            os.chdir(original_cwd)

        if output:
            return CheckResult("pass", f"{tag} on origin")
        return CheckResult("warn", f"{tag} not found on origin")

    @app.check("github-release")
    def check_github_release(ctx):
        """GitHub Release must exist for the current version tag."""
        from .utils import check_gh_auth, check_gh_installed, run

        _version, tag = _resolve_version_and_tag(ctx)
        if not tag:
            return CheckResult("skip", "no version detected")

        if not check_gh_installed():
            return CheckResult("fail", "gh CLI is not installed")
        if not check_gh_auth():
            return CheckResult("fail", "gh CLI is not authenticated")

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            run("gh", ["release", "view", tag])
            return CheckResult("pass", f"{tag} exists")
        except subprocess.CalledProcessError:
            return CheckResult("warn", f"{tag} not found on GitHub")
        finally:
            os.chdir(original_cwd)

    @app.check("branch-sync")
    def check_branch_sync(ctx):
        """Local branch must be in sync with origin."""
        from .utils import get_current_branch, run

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            branch = get_current_branch()
            try:
                output = run("git", ["rev-list", "--left-right", "--count",
                                      f"origin/{branch}...HEAD"])
            except subprocess.CalledProcessError:
                return CheckResult("warn", f"no remote tracking for {branch}")
        finally:
            os.chdir(original_cwd)

        parts = output.split("\t")
        if len(parts) != 2:
            return CheckResult("warn", f"unexpected rev-list output: {output}")

        behind, ahead = int(parts[0]), int(parts[1])
        if behind == 0 and ahead == 0:
            return CheckResult("pass", f"up to date with origin/{branch}")
        if behind == 0 and ahead > 0:
            return CheckResult("warn", f"{ahead} commit(s) ahead of origin/{branch}")
        if behind > 0 and ahead == 0:
            return CheckResult("fail", f"{behind} commit(s) behind origin/{branch}")
        return CheckResult("fail", f"{behind} behind, {ahead} ahead of origin/{branch}")

    # ------------------------------------------------------------------
    # Tag: changelog
    # ------------------------------------------------------------------

    @app.check("changelog-entry")
    def check_changelog_entry(ctx):
        """CHANGELOG.md must have an entry for the current version."""
        from .utils import extract_changelog_entry

        version, _tag = _resolve_version_and_tag(ctx)
        if not version:
            return CheckResult("skip", "no version detected")

        changelog_path = os.path.join(str(ctx.project_root), "CHANGELOG.md")
        if not os.path.exists(changelog_path):
            return CheckResult("warn", "CHANGELOG.md not found")

        entry = extract_changelog_entry(changelog_path, version)
        if entry:
            return CheckResult("pass", f"entry for {version}")
        return CheckResult("warn", f"no entry for {version}")

    # ------------------------------------------------------------------
    # No tag
    # ------------------------------------------------------------------

    @app.check("library-lint")
    def check_library_lint(ctx):
        """Library projects must pass boundary lint."""
        from .lint import lint_library

        # Try monorepo path first
        ws_root = None
        try:
            from .workspace import find_workspace_root, load_workspace
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

        # Standalone project: lint current directory
        results = lint_library(str(ctx.project_root))

        total_errors = 0
        total_warnings = 0
        for r in results:
            if r.severity == "error":
                total_errors += 1
            elif r.severity == "warning":
                total_warnings += 1

        if total_errors > 0:
            return CheckResult("fail", f"{total_errors} error(s), {total_warnings} warning(s)")
        if total_warnings > 0:
            return CheckResult("warn", f"{total_warnings} warning(s)")
        return CheckResult("pass", "project clean")

    # ------------------------------------------------------------------
    # Tag: changelog (validation checks)
    # ------------------------------------------------------------------

    def _get_changelog_context(ctx):
        """Resolve changes_dir, tag_glob, and entries for changelog checks.

        Returns ``(changes_dir, tag_glob, entries)`` or ``None`` when the
        changes directory does not exist (caller should return skip).
        """
        from .changelog.files import get_changes_dir, read_unreleased

        changes_dir = get_changes_dir(str(ctx.project_root))
        if not os.path.isdir(changes_dir):
            return None

        tag_glob = None
        if isinstance(ctx, WorkspaceCheckContext):
            # Derive tag_glob from project name for monorepo scoping
            from .workspace import resolve_project
            proj = resolve_project(str(ctx.workspace_root), str(ctx.project_root))
            if proj is not None:
                tag_glob = f"{proj['name']}@v*"

        entries = read_unreleased(changes_dir)
        return changes_dir, tag_glob, entries

    @app.check("changelog-hashes")
    def check_changelog_hashes(ctx):
        """Every hash in unreleased.jsonl must resolve via git rev-parse."""
        from .changelog.validate import check_hashes_resolve

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, _tag_glob, entries = info

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            passed, details = check_hashes_resolve(entries)
        finally:
            os.chdir(original_cwd)

        if passed:
            return CheckResult("pass", "all hashes resolve")
        return CheckResult("fail", f"{len(details)} hash(es) failed to resolve", details=details)

    @app.check("changelog-range")
    def check_changelog_range(ctx):
        """Every resolved hash must be in the unreleased commit range."""
        from .changelog.validate import check_in_range

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, tag_glob, entries = info

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            passed, details = check_in_range(entries, tag_glob)
        finally:
            os.chdir(original_cwd)

        if passed:
            return CheckResult("pass", "all hashes in unreleased range")
        return CheckResult("fail", f"{len(details)} hash(es) out of range", details=details)

    @app.check("changelog-coverage")
    def check_changelog_coverage(ctx):
        """Every unreleased commit must appear in at least one entry."""
        from .changelog.validate import check_coverage

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, tag_glob, entries = info

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            passed, details = check_coverage(entries, tag_glob)
        finally:
            os.chdir(original_cwd)

        if passed:
            return CheckResult("pass", "all unreleased commits covered")
        # Filter out informational "skipped N ..." lines from the fail count
        fail_details = [d for d in details if not d.startswith("skipped ")]
        return CheckResult("fail", f"{len(fail_details)} uncovered commit(s)", details=details)

    @app.check("changelog-orphans")
    def check_changelog_orphans(ctx):
        """No entry should have ALL hashes unresolvable (stale/rebased)."""
        from .changelog.validate import check_no_orphans

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, _tag_glob, entries = info

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            passed, details = check_no_orphans(entries)
        finally:
            os.chdir(original_cwd)

        if passed:
            return CheckResult("pass", "no orphaned entries")
        return CheckResult("fail", f"{len(details)} orphaned entry(ies)", details=details)

    @app.check("changelog-schema")
    def check_changelog_schema(ctx):
        """Every entry must pass schema validation."""
        from .changelog.validate import check_schema

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, _tag_glob, entries = info

        passed, details = check_schema(entries)
        if passed:
            return CheckResult("pass", "all entries valid")
        return CheckResult("fail", f"{len(details)} schema error(s)", details=details)

    @app.check("changelog-batch-commits")
    def check_changelog_batch_commits(ctx):
        """No entry should have more commits than max_commits_per_entry."""
        from .changelog.validate import check_batch_size_commits, _get_batch_limits_config

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, _tag_glob, entries = info

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            batch_config = _get_batch_limits_config()
        finally:
            os.chdir(original_cwd)

        passed, details = check_batch_size_commits(entries, batch_config, version="unreleased")
        if passed:
            return CheckResult("pass", "all entries within commit batch limit")
        return CheckResult("fail", f"{len(details)} entry(ies) exceed commit limit", details=details)

    @app.check("changelog-batch-entries")
    def check_changelog_batch_entries(ctx):
        """No commit should appear in more entries than max_entries_per_commit."""
        from .changelog.validate import (
            check_batch_size_entries,
            _get_batch_limits_config,
            _read_all_versioned_entries,
        )

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        changes_dir, _tag_glob, _entries = info

        original_cwd = os.getcwd()
        try:
            os.chdir(ctx.project_root)
            batch_config = _get_batch_limits_config()
            entries_by_version = _read_all_versioned_entries(changes_dir)
        finally:
            os.chdir(original_cwd)

        passed, details = check_batch_size_entries(entries_by_version, batch_config)
        if passed:
            return CheckResult("pass", "all commits within entry batch limit")
        return CheckResult("fail", f"{len(details)} commit(s) exceed entry limit", details=details)

    # ------------------------------------------------------------------
    # Tag: workspace
    # ------------------------------------------------------------------

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

        from .targets import detect_targets

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

        project_manifests = (
            "go.mod", "pyproject.toml", "package.json", "Cargo.toml",
            "mix.exs", "deno.json", "build.zig.zon",
        )

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
            for manifest in project_manifests:
                if os.path.isfile(os.path.join(dir_path, manifest)):
                    found_project_dirs.add(entry)
                    break

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

        project_manifests = (
            "go.mod", "pyproject.toml", "package.json", "Cargo.toml",
            "mix.exs", "deno.json", "build.zig.zon",
        )

        stale = []
        for proj in ctx.projects:
            dir_path = os.path.join(root, proj["path"])
            if not os.path.isdir(dir_path):
                stale.append(proj["path"])
                continue
            has_manifest = any(
                os.path.isfile(os.path.join(dir_path, m)) for m in project_manifests
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
