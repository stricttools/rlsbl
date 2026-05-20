"""Doctor command that diagnoses release state problems (versions, names, tags, branches, changelog, lint) and auto-repairs fixable issues."""

import os
import subprocess
import sys

from ..lock import is_stale
from ..targets import TARGETS, detect_targets
from ..targets.utils import normalize_go, normalize_npm, normalize_pypi
from ..utils import (
    check_gh_auth,
    check_gh_installed,
    extract_changelog_entry,
    get_current_branch,
    run,
)


# Check name registry: maps short names to (function_name, needs_tag, needs_version).
# Function is looked up by name on the module at call time so that unittest.mock
# patches work correctly.
CHECK_REGISTRY = {}

_this_module = sys.modules[__name__]


def _register_check(name, needs_tag=False, needs_version=False):
    """Decorator to register a named check function."""
    def decorator(fn):
        CHECK_REGISTRY[name] = {
            "fn_name": fn.__name__,
            "needs_tag": needs_tag,
            "needs_version": needs_version,
        }
        return fn
    return decorator


@_register_check("lock")
def _check_stale_lock():
    """Check for stale lock files.

    Checks both .rlsbl/lock (standalone) and .rlsbl-monorepo/lock (monorepo).
    """
    stale_paths = []
    if is_stale():
        stale_paths.append(".rlsbl/lock")
    if is_stale(lock_path=os.path.join(".rlsbl-monorepo", "lock")):
        stale_paths.append(".rlsbl-monorepo/lock")
    if stale_paths:
        return ("WARN", f"stale lock file exists at {', '.join(stale_paths)}")
    return ("PASS", "no lock file")


@_register_check("versions")
def _check_version_consistency():
    """Check that all detected targets report the same version."""
    target_entries = detect_targets(".")
    if not target_entries:
        return ("WARN", "no targets detected")

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
        return ("WARN", "no targets reported a version")
    if len(unique) > 1:
        detail = ", ".join(f"{n}={v}" for n, v in versions.items() if v is not None)
        return ("FAIL", f"version mismatch: {detail}")

    version = unique.pop()
    return ("PASS", f"{version} across {len(target_entries)} target(s)")


def _normalize_name(target_name, raw_name):
    """Normalize a package name using the appropriate target normalizer."""
    normalizers = {
        "npm": normalize_npm,
        "pypi": normalize_pypi,
        "go": normalize_go,
    }
    normalizer = normalizers.get(target_name, str.lower)
    return normalizer(raw_name)


@_register_check("names")
def _check_name_consistency():
    """Check that all detected targets report the same package name."""
    target_entries = detect_targets(".")
    if not target_entries:
        return ("WARN", "no targets detected")

    names = {}
    for name, path in target_entries:
        target = TARGETS[name]
        try:
            n = target.read_name(path)
            names[name] = n
        except Exception:
            names[name] = None

    # Targets that returned a name
    have_name = {k: v for k, v in names.items() if v is not None}
    if not have_name:
        return ("WARN", "no targets reported a name")

    # Warn about targets that couldn't provide a name
    missing = [k for k, v in names.items() if v is None]

    normalized = {k: _normalize_name(k, v) for k, v in have_name.items()}
    unique = set(normalized.values())

    if len(unique) == 1:
        raw_name = next(iter(have_name.values()))
        msg = f"{raw_name} across {len(target_entries)} target(s)"
        if missing:
            msg += f" (no name from: {', '.join(missing)})"
        return ("PASS", msg)

    detail = ", ".join(f"{k}={v}" for k, v in have_name.items())
    return ("WARN", f"name mismatch: {detail}")


@_register_check("license")
def _check_license_consistency():
    """Check that all detected targets report the same license."""
    target_entries = detect_targets(".")
    if not target_entries:
        return ("PASS", "no targets declare a license")

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
        return ("PASS", "no targets declare a license")
    if len(licenses) < 2:
        return ("PASS", f"only {len(licenses)} target(s) declare a license")

    unique = set(v.lower() for v in licenses.values())
    if len(unique) == 1:
        license_val = next(iter(licenses.values()))
        return ("PASS", f"{license_val} across {len(licenses)} target(s)")

    detail = ", ".join(f"{k}={v}" for k, v in licenses.items())
    return ("WARN", f"license mismatch: {detail}")


@_register_check("description")
def _check_description_consistency():
    """Check that all detected targets report the same description."""
    target_entries = detect_targets(".")
    if not target_entries:
        return ("PASS", "no targets declare a description")

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
        return ("PASS", "no targets declare a description")
    if len(descriptions) < 2:
        return ("PASS", f"only {len(descriptions)} target(s) declare a description")

    unique = set(descriptions.values())
    if len(unique) == 1:
        desc_val = next(iter(descriptions.values()))
        return ("PASS", f"{desc_val} across {len(descriptions)} target(s)")

    detail = ", ".join(f"{k}={v}" for k, v in descriptions.items())
    return ("WARN", f"description mismatch: {detail}")


@_register_check("local-tag", needs_tag=True)
def _check_local_tag(tag):
    """Check if a git tag exists locally."""
    output = run("git", ["tag", "-l", tag])
    if output:
        return ("PASS", f"{tag} exists")
    return ("WARN", f"{tag} not found locally")


@_register_check("remote-tag", needs_tag=True)
def _check_remote_tag(tag):
    """Check if a git tag exists on the remote."""
    output = run("git", ["ls-remote", "--tags", "origin", tag])
    if output:
        return ("PASS", f"{tag} on origin")
    return ("WARN", f"{tag} not found on origin")


@_register_check("github-release", needs_tag=True)
def _check_github_release(tag):
    """Check if a GitHub Release exists for the tag."""
    if not check_gh_installed():
        print("Error: gh CLI is not installed.", file=sys.stderr)
        sys.exit(1)
    if not check_gh_auth():
        print("Error: gh CLI is not authenticated. Run 'gh auth login'.", file=sys.stderr)
        sys.exit(1)

    try:
        run("gh", ["release", "view", tag])
        return ("PASS", f"{tag} exists")
    except subprocess.CalledProcessError:
        return ("WARN", f"{tag} not found on GitHub")


@_register_check("branch-sync")
def _check_branch_sync():
    """Check if the local branch is in sync with origin."""
    branch = get_current_branch()
    try:
        output = run("git", ["rev-list", "--left-right", "--count",
                              f"origin/{branch}...HEAD"])
    except subprocess.CalledProcessError:
        return ("WARN", f"no remote tracking for {branch}")

    parts = output.split("\t")
    if len(parts) != 2:
        return ("WARN", f"unexpected rev-list output: {output}")

    behind, ahead = int(parts[0]), int(parts[1])
    if behind == 0 and ahead == 0:
        return ("PASS", f"up to date with origin/{branch}")
    if behind == 0 and ahead > 0:
        return ("WARN", f"{ahead} commit(s) ahead of origin/{branch}")
    if behind > 0 and ahead == 0:
        return ("FAIL", f"{behind} commit(s) behind origin/{branch}")
    return ("FAIL", f"{behind} behind, {ahead} ahead of origin/{branch}")


@_register_check("changelog", needs_version=True)
def _check_changelog(version):
    """Check if CHANGELOG.md has an entry for the version."""
    changelog_path = "CHANGELOG.md"
    if not os.path.exists(changelog_path):
        return ("WARN", "CHANGELOG.md not found")

    entry = extract_changelog_entry(changelog_path, version)
    if entry:
        return ("PASS", f"entry for {version}")
    return ("WARN", f"no entry for {version}")


@_register_check("library-lint")
def _check_library_lint():
    """Check library projects for boundary violations.

    In a monorepo: lint all projects with ``library = true``.
    In a standalone project: lint the current directory directly.
    """
    from ..lint import lint_library

    # Try monorepo path first
    ws_root = None
    try:
        from ..workspace import find_workspace_root, load_workspace

        ws_root = find_workspace_root(".")
    except Exception:
        pass

    if ws_root:
        try:
            projects = load_workspace(ws_root)
        except Exception:
            return ("PASS", "not in a monorepo workspace")

        library_projects = [p for p in projects if p.get("library")]
        if not library_projects:
            return ("PASS", "no library projects configured")

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
            return ("FAIL", f"{total_errors} error(s), {total_warnings} warning(s)")
        if total_warnings > 0:
            return ("WARN", f"{total_warnings} warning(s)")
        return ("PASS", "all library projects clean")

    # Standalone project: lint current directory
    results = lint_library(".")

    total_errors = 0
    total_warnings = 0
    for r in results:
        if r.severity == "error":
            total_errors += 1
        elif r.severity == "warning":
            total_warnings += 1

    if total_errors > 0:
        return ("FAIL", f"{total_errors} error(s), {total_warnings} warning(s)")
    if total_warnings > 0:
        return ("WARN", f"{total_warnings} warning(s)")
    return ("PASS", "project clean")


def _apply_fixes(results, tag, version):
    """Apply safe auto-fixes for WARN/FAIL results."""
    fixed = 0

    # Fix stale lock (check both standalone and monorepo locations)
    status, _ = results["Lock file"]
    if status == "WARN":
        for lock_path in (os.path.join(".rlsbl", "lock"),
                          os.path.join(".rlsbl-monorepo", "lock")):
            if os.path.exists(lock_path) and is_stale(lock_path=lock_path):
                os.unlink(lock_path)
                print(f"Fixed: removed stale lock file at {lock_path}")
                fixed += 1

    # Fix missing remote tag (only if local tag exists)
    local_status, _ = results["Local tag"]
    remote_status, _ = results["Remote tag"]
    if remote_status == "WARN" and local_status == "PASS":
        try:
            run("git", ["push", "origin", tag])
            print(f"Fixed: pushed tag {tag} to origin")
            fixed += 1
        except subprocess.CalledProcessError as e:
            print(f"Could not push tag: {e}", file=sys.stderr)

    # Fix missing GitHub Release (only if remote tag exists after potential fix)
    gh_status, _ = results["GitHub Release"]
    if gh_status == "WARN":
        # Re-check remote tag status (may have been fixed above)
        remote_check = _check_remote_tag(tag)
        if remote_check[0] == "PASS":
            try:
                run("gh", ["release", "create", tag, "--title", tag,
                           "--notes", f"Release {version}"])
                print(f"Fixed: created GitHub Release {tag}")
                fixed += 1
            except subprocess.CalledProcessError as e:
                print(f"Could not create GitHub Release: {e}", file=sys.stderr)

    # Report guidance for unfixable issues
    version_status, _ = results["Version files"]
    if version_status == "FAIL":
        print("Manual fix needed: version mismatch -- update version files to agree")

    branch_status, _ = results["Branch sync"]
    if branch_status == "FAIL":
        print("Manual fix needed: run 'git pull' to sync with origin")

    changelog_status, _ = results["Changelog"]
    if changelog_status == "WARN" and version:
        print(f"Manual fix needed: add a '## {version}' entry to CHANGELOG.md")

    local_tag_status, _ = results["Local tag"]
    if local_tag_status == "WARN" and tag:
        print(f"Manual fix needed: create tag with 'git tag {tag}'")

    return fixed


def _check_workflows_synced(root, projects):
    """Check that each project has a synced CI workflow at the repo root."""
    missing = []
    for proj in projects:
        name = proj["name"]
        workflow = os.path.join(root, ".github", "workflows", f"{name}-ci.yml")
        if not os.path.isfile(workflow):
            missing.append(name)
    if missing:
        return ("WARN", f"missing workflows: {', '.join(missing)}")
    return ("PASS", f"all {len(projects)} project(s) have synced workflows")


def _check_router_exists(root):
    """Check that ci-router.yml exists at the repo root."""
    router = os.path.join(root, ".github", "workflows", "ci-router.yml")
    if os.path.isfile(router):
        return ("PASS", "ci-router.yml exists")
    return ("WARN", "ci-router.yml not found")


def _check_project_targets(projects, root):
    """Check that each project has at least one detectable target."""
    missing = []
    for proj in projects:
        targets = detect_targets(os.path.join(root, proj["path"]))
        if not targets:
            missing.append(proj["name"])
    if missing:
        return ("WARN", f"no targets detected: {', '.join(missing)}")
    return ("PASS", f"all {len(projects)} project(s) have targets")


def _resolve_version_and_tag():
    """Detect version and tag from project targets.

    Returns (version, tag) tuple; either may be None.
    """
    target_entries = detect_targets(".")
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


def _run_single_check(check_name, tag, version):
    """Run a single registered check by name.

    Looks up the function by name on the module at call time so that
    unittest.mock patches are respected.

    Returns (status, message) tuple.
    """
    entry = CHECK_REGISTRY[check_name]
    fn = getattr(_this_module, entry["fn_name"])

    if entry["needs_tag"]:
        if not tag:
            return ("WARN", "no version detected")
        return fn(tag)
    if entry["needs_version"]:
        if not version:
            return ("WARN", "no version detected")
        return fn(version)
    return fn()


def run_cmd(registry, args, flags):
    """Run diagnostic checks on the release state."""
    check_name = flags.get("check")

    # --check <name>: run a single check and exit
    if check_name:
        if check_name not in CHECK_REGISTRY:
            valid = ", ".join(sorted(CHECK_REGISTRY.keys()))
            print(f"Error: unknown check '{check_name}'. Valid checks: {valid}",
                  file=sys.stderr)
            sys.exit(1)

        version, tag = _resolve_version_and_tag()
        status, message = _run_single_check(check_name, tag, version)
        print(f"{check_name}: {status} -- {message}")
        sys.exit(1 if status == "FAIL" else 0)

    # No --check: run all checks (original behavior)
    version, tag = _resolve_version_and_tag()
    if not detect_targets("."):
        print("Warning: no targets detected.", file=sys.stderr)

    # Run checks
    results = {}
    results["Lock file"] = _run_single_check("lock", tag, version)
    results["Version files"] = _run_single_check("versions", tag, version)
    results["Package names"] = _run_single_check("names", tag, version)
    results["License"] = _run_single_check("license", tag, version)
    results["Description"] = _run_single_check("description", tag, version)
    results["Local tag"] = _run_single_check("local-tag", tag, version)
    results["Remote tag"] = _run_single_check("remote-tag", tag, version)
    results["GitHub Release"] = _run_single_check("github-release", tag, version)
    results["Branch sync"] = _run_single_check("branch-sync", tag, version)
    results["Changelog"] = _run_single_check("changelog", tag, version)
    results["Library lint"] = _run_single_check("library-lint", tag, version)

    # Print aligned table
    label_width = max(len(label) for label in results)
    issues = 0
    for label, (status, message) in results.items():
        padded_label = f"{label}:".ljust(label_width + 1)
        print(f"{padded_label}  {status} -- {message}")
        if status in ("WARN", "FAIL"):
            issues += 1

    # Summary
    print()
    if issues == 0:
        print("All checks passed.")
    else:
        print(f"{issues} issue(s) found.")

    # Monorepo workspace checks
    try:
        from ..workspace import find_workspace_root, load_workspace

        ws_root = find_workspace_root(".")
        if ws_root:
            projects = load_workspace(ws_root)
            print()
            print("Monorepo:")
            mono_results = {}
            mono_results["CI router"] = _check_router_exists(ws_root)
            mono_results["Synced workflows"] = _check_workflows_synced(
                ws_root, projects
            )
            mono_results["Project targets"] = _check_project_targets(projects, ws_root)

            mono_label_width = max(len(label) for label in mono_results)
            for label, (status, message) in mono_results.items():
                padded_label = f"{label}:".ljust(mono_label_width + 1)
                print(f"{padded_label}  {status} -- {message}")
                if status in ("WARN", "FAIL"):
                    issues += 1
    except Exception:
        pass  # not in a monorepo, skip

    # Auto-fix if requested
    if flags.get("fix") and issues > 0 and tag and version:
        print()
        _apply_fixes(results, tag, version)
