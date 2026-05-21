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
