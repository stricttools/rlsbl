"""Project checks registered on the strictcli check system.

Each check is registered via ``@app.check("name")`` and receives a
:class:`~rlsbl.context.ProjectContext` (or its
:class:`~rlsbl.check_context.WorkspaceCheckContext` subclass).

The check functions return :class:`strictcli.CheckResult` with lowercase
status strings: ``"pass"``, ``"fail"``, ``"warn"``, ``"skip"``.
"""

import json
import os
import subprocess
import sys

from strictcli import CheckResult

from .check_context import WorkspaceCheckContext

# Manifest filenames used by workspace-unregistered and workspace-stale-entries
# to detect project directories. Derived from each target's detection_files
# so it stays in sync automatically when new targets are added.
def _all_detection_files():
    from .targets import TARGETS
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
    "dev-node-boundary": "workspace",
    "layers-violations": "workspace",
    "deps-stale": "workspace",
    "dead-workspace-packages": "workspace",
    # --- workspace + language-specific import scanners ---
    "deps-unused": frozenset({"pypi", "dart", "npm"}),
    "deps-undeclared": frozenset({"pypi", "dart", "npm"}),
    "deps-runtime-test-only": frozenset({"pypi", "dart", "npm"}),
    "deps-dev-in-lib": frozenset({"pypi", "dart", "npm"}),
    # --- target-specific quality checks ---
    "dead-modules": frozenset({"pypi", "go", "npm"}),
    "library-lint": frozenset({"pypi", "go", "npm"}),
    # --- quality tag (universal) ---
    "scaffold-unreplaced-vars": None,
}

# Canonical column order for the feature matrix. Only targets that
# appear as values in CHECK_TARGETS are included (not all 18 TARGETS).
MATRIX_COLUMNS: tuple[str, ...] = ("pypi", "go", "npm", "dart", "cargo",
                                    "deno", "hex", "zig", "swift",
                                    "swift-apple", "maven", "docker",
                                    "flutter-ios", "flutter-android",
                                    "pgdesign", "plain", "docs", "spec")


def get_feature_matrix() -> dict[str, dict[str, str]]:
    """Build a feature matrix mapping check names to per-target support.

    Returns ``{check_name: {target: "yes"|"no"|"all"|"workspace"}}``
    where:
    - ``"all"`` means the check is universal (works for any target)
    - ``"workspace"`` means the check requires a monorepo workspace
    - ``"yes"`` means the target is explicitly supported
    - ``"no"`` means the target is not supported
    """
    matrix: dict[str, dict[str, str]] = {}
    for check_name, targets in CHECK_TARGETS.items():
        row: dict[str, str] = {}
        if targets is None:
            for col in MATRIX_COLUMNS:
                row[col] = "all"
        elif targets == "workspace":
            for col in MATRIX_COLUMNS:
                row[col] = "workspace"
        else:
            for col in MATRIX_COLUMNS:
                row[col] = "yes" if col in targets else "no"
        matrix[check_name] = row
    return matrix


def generate_feature_matrix_markdown() -> str:
    """Generate a Markdown table of check-vs-target support.

    Only includes checks that have target-specific behavior (not
    universal or workspace-only), since those are the interesting rows.
    """
    matrix = get_feature_matrix()

    # Filter to checks with at least one "yes" or "no" (target-specific)
    interesting = {
        name: row for name, row in matrix.items()
        if any(v in ("yes", "no") for v in row.values())
    }

    if not interesting:
        return "No target-specific checks registered.\n"

    # Determine which columns have at least one "yes" among interesting rows
    active_cols = [
        col for col in MATRIX_COLUMNS
        if any(row.get(col) == "yes" for row in interesting.values())
    ]

    # Build table
    lines: list[str] = []
    header = "| Check | " + " | ".join(active_cols) + " |"
    separator = "|-------|" + "|".join("-" * (len(c) + 2) for c in active_cols) + "|"
    lines.append(header)
    lines.append(separator)

    for check_name in sorted(interesting):
        row = interesting[check_name]
        cells = []
        for col in active_cols:
            val = row[col]
            cells.append("yes" if val == "yes" else "no")
        lines.append(f"| {check_name} | " + " | ".join(cells) + " |")

    lines.append("")
    return "\n".join(lines)


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
    """Register all project checks on *app*.

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

        root_str = str(ctx.project_root)
        stale_paths = []
        if is_stale(lock_path=os.path.join(root_str, ".rlsbl", "lock"), project_root=ctx.project_root):
            stale_paths.append(".rlsbl/lock")
        if is_stale(lock_path=os.path.join(root_str, ".rlsbl-monorepo", "lock"), project_root=ctx.project_root):
            stale_paths.append(".rlsbl-monorepo/lock")

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

        # Include selfdoc.json even when "docs" is not in the explicit
        # targets list, so version drift is always caught.
        detected_names = {name for name, _path in target_entries}
        if "docs" not in detected_names:
            import os as _os
            selfdoc_path = _os.path.join(str(ctx.project_root), "selfdoc.json")
            if _os.path.exists(selfdoc_path):
                from .targets.docs import DocsTarget
                try:
                    v = DocsTarget().read_version(str(ctx.project_root))
                    versions["docs"] = v
                except Exception:
                    versions["docs"] = None

        unique = set(v for v in versions.values() if v is not None)
        if len(unique) == 0:
            return CheckResult("warn", "no targets reported a version")
        if len(unique) > 1:
            detail = ", ".join(f"{n}={v}" for n, v in versions.items() if v is not None)
            return CheckResult("fail", f"version mismatch: {detail}")

        version = unique.pop()
        return CheckResult("pass", f"{version} across {len(versions)} target(s)")

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
                n = target.read_name(path, ctx=ctx)
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

    @app.check("private-hook-stale")
    def check_private_hook_stale(ctx):
        """Detect legacy private asset upload code in post-release hook."""
        hook_path = os.path.join(str(ctx.project_root), ".rlsbl", "hooks", "post-release.sh")
        if not os.path.exists(hook_path):
            return CheckResult("pass", "no post-release hook")

        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The old private hook template had this distinctive comment line
        if "Post-release hook for private repositories" in content:
            return CheckResult(
                "fail",
                "Post-release hook contains legacy private asset upload code. "
                "Asset upload is now a built-in release step. "
                "Run `rlsbl scaffold` to get the standard hook template.",
            )
        return CheckResult("pass", "no legacy private hook code")

    @app.check("config-schema")
    def check_config_schema(ctx):
        """Validate .rlsbl/config.json schema: private key and publish config."""
        from .config import validate_publish_config

        config = ctx.config
        errors = []

        if "private" not in config:
            errors.append('"private" key missing from .rlsbl/config.json')

        publish = config.get("publish", {})
        if isinstance(publish, dict):
            for target_name in publish:
                try:
                    validate_publish_config(config, target_name)
                except ValueError as e:
                    errors.append(str(e))

        if errors:
            return CheckResult("fail", f"{len(errors)} config error(s)", details=errors)
        return CheckResult("pass", "config schema valid")

    @app.check("license-file")
    def check_license_file(ctx):
        """LICENSE file must exist, be non-empty, and have no template variables."""
        import re as _re

        license_path = os.path.join(str(ctx.project_root), "LICENSE")
        if not os.path.exists(license_path):
            return CheckResult("fail", "LICENSE file not found in project root")

        try:
            size = os.path.getsize(license_path)
        except OSError:
            return CheckResult("fail", "cannot read LICENSE file")

        if size == 0:
            return CheckResult("fail", "LICENSE file is empty")

        with open(license_path, "r", encoding="utf-8") as f:
            content = f.read()

        template_vars = _re.findall(r"\{\{\w+(?:\.\w+)*\}\}", content)
        if template_vars:
            return CheckResult(
                "fail",
                f"LICENSE contains unreplaced template variable(s): {', '.join(template_vars)}",
            )

        return CheckResult("pass", "LICENSE file valid")

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

        output = run("git", ["tag", "-l", tag], cwd=str(ctx.project_root))

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

        output = run("git", ["ls-remote", "--tags", "origin", tag], cwd=str(ctx.project_root))

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

        try:
            run("gh", ["release", "view", tag], cwd=str(ctx.project_root))
            return CheckResult("pass", f"{tag} exists")
        except subprocess.CalledProcessError:
            return CheckResult("warn", f"{tag} not found on GitHub")

    @app.check("branch-sync")
    def check_branch_sync(ctx):
        """Local branch must be in sync with origin."""
        from .utils import get_current_branch, run

        root_str = str(ctx.project_root)
        branch = get_current_branch()
        try:
            output = run("git", ["rev-list", "--left-right", "--count",
                                  f"origin/{branch}...HEAD"], cwd=root_str)
        except subprocess.CalledProcessError:
            return CheckResult("warn", f"no remote tracking for {branch}")

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

        # Standalone projects are never libraries (only monorepo projects
        # with library = true are).  Match the release flow which skips lint
        # for non-library projects.
        return CheckResult("skip", "not a library project")

    # ------------------------------------------------------------------
    # Tag: changelog (validation checks)
    # ------------------------------------------------------------------

    def _get_changelog_context(ctx):
        """Resolve changes_dir, tag_glob, project, and entries for changelog checks.

        Returns ``(changes_dir, tag_glob, project, entries)`` or ``None`` when the
        changes directory does not exist (caller should return skip).
        The ``project`` value is a dict with ``path`` and ``watch`` keys when
        running in monorepo mode, or ``None`` for standalone projects.
        """
        from .changelog.files import get_changes_dir, read_unreleased

        changes_dir = get_changes_dir(str(ctx.project_root))
        if not os.path.isdir(changes_dir):
            return None

        tag_glob = None
        project = None
        if isinstance(ctx, WorkspaceCheckContext):
            # Derive tag_glob and project dict from workspace for monorepo scoping
            from .workspace import resolve_project
            proj = resolve_project(str(ctx.workspace_root), str(ctx.project_root))
            if proj is not None:
                # Use the target's monorepo_tag_glob() to get the correct
                # tag pattern (e.g. Go uses "path/v*" not "name@v*").
                from .targets import TARGETS, detect_targets
                target_entries = detect_targets(str(ctx.project_root))
                if target_entries:
                    target = TARGETS[target_entries[0].name]
                    tag_glob = target.monorepo_tag_glob(proj['name'], path=proj['path'])
                else:
                    tag_glob = f"{proj['name']}@v*"
                project = proj

        entries = read_unreleased(changes_dir)
        return changes_dir, tag_glob, project, entries

    @app.check("changelog-hashes")
    def check_changelog_hashes(ctx):
        """Every hash in unreleased.jsonl must resolve via git rev-parse."""
        from .changelog.validate import check_hashes_resolve

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, _tag_glob, _project, entries = info

        passed, details = check_hashes_resolve(entries)

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
        _changes_dir, tag_glob, project, entries = info

        passed, details = check_in_range(entries, tag_glob, project=project)

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
        _changes_dir, tag_glob, project, entries = info

        if project is not None and project.get("dev_node"):
            return CheckResult("skip", "dev node project")

        passed, details = check_coverage(entries, tag_glob, project=project)

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
        _changes_dir, _tag_glob, _project, entries = info

        passed, details = check_no_orphans(entries)

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
        _changes_dir, _tag_glob, _project, entries = info

        passed, details = check_schema(entries)
        if passed:
            return CheckResult("pass", "all entries valid")
        return CheckResult("fail", f"{len(details)} schema error(s)", details=details)

    @app.check("changelog-user-facing")
    def check_changelog_user_facing(ctx):
        """At least one entry must be user-facing."""
        from .changelog.validate import check_has_user_facing

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, _tag_glob, project, entries = info

        if project is not None and project.get("dev_node"):
            return CheckResult("skip", "dev node project")

        passed, details = check_has_user_facing(entries)
        if passed:
            return CheckResult("pass", "has user-facing entries")
        return CheckResult("warn", "no user-facing entries", details=details)

    @app.check("changelog-batch-commits")
    def check_changelog_batch_commits(ctx):
        """No entry should have more commits than max_commits_per_entry."""
        from .changelog.validate import check_batch_size_commits, _get_batch_limits_config

        info = _get_changelog_context(ctx)
        if info is None:
            return CheckResult("skip", "no .rlsbl/changes/ directory")
        _changes_dir, _tag_glob, _project, entries = info

        batch_config = _get_batch_limits_config(ctx.config)

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
        changes_dir, _tag_glob, _project, _entries = info

        batch_config = _get_batch_limits_config(ctx.config)
        entries_by_version = _read_all_versioned_entries(changes_dir)

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

    @app.check("dev-node-boundary")
    def check_dev_node_boundary(ctx):
        """Non-dev-node projects must not have runtime deps on dev node projects."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        # Build lookup: project name -> project dict
        projects_by_name = {p["name"]: p for p in ctx.projects}

        # Find all dev node projects
        dev_node_names = [
            name for name, proj in projects_by_name.items()
            if proj.get("dev_node")
        ]

        if not dev_node_names:
            return CheckResult("pass", "no dev node projects")

        violations = []
        for dev_name in dev_node_names:
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
                if not dep_proj.get("dev_node"):
                    violations.append(
                        f"non-dev-node project '{dep_name}' has a runtime dependency "
                        f"on dev node project '{dev_name}'. "
                        f"Bug fixes in '{dev_name}' won't appear in any changelog."
                    )

        if violations:
            return CheckResult(
                "fail",
                f"{len(violations)} boundary violation(s)",
                details=violations,
            )
        return CheckResult("pass", "dev node boundary clean")

    # ------------------------------------------------------------------
    # Dead workspace packages
    # ------------------------------------------------------------------

    @app.check("dead-workspace-packages")
    def check_dead_workspace_packages(ctx):
        """Library packages must be imported by at least one workspace sibling."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from .dep_validation import find_dead_workspace_packages

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

    # ------------------------------------------------------------------
    # Dependency validation
    # ------------------------------------------------------------------

    def _sibling_exclude_dirs(root, project_path, all_projects):
        """Compute sibling project directories to exclude from a scan.

        For a project at ``project_path``, returns a list of other
        workspace project directories that are subdirectories of this
        project's path.  This prevents walk_source_files from descending
        into sibling projects when the current project is at a parent
        path (e.g. ``path = "."``).
        """
        project_abs = os.path.normpath(os.path.join(root, project_path))
        exclude = []
        for other in all_projects:
            other_path = other["path"]
            if other_path == project_path:
                continue
            other_abs = os.path.normpath(os.path.join(root, other_path))
            # Only exclude if the other project is strictly inside this
            # project's directory tree.
            if other_abs.startswith(project_abs + os.sep):
                exclude.append(other_abs)
        return exclude

    def _build_dep_import_cache(ctx):
        """Build per-project import scan cache for dependency checks.

        Returns a dict mapping project name to (lib_imports, test_imports).
        All dep checks (unused, undeclared, runtime-test-only, dev-in-lib)
        share one scan pass via this cache.  The result is memoized on the
        context object so that multiple checks in the same run reuse a
        single scan instead of re-walking every project's source tree.
        """
        cached = getattr(ctx, "_dep_import_cache", None)
        if cached is not None:
            return cached

        from .dep_validation import _get_imported_workspace_packages

        root = str(ctx.workspace_root)
        workspace_names = {p["name"] for p in ctx.projects}
        cache = {}
        for proj in ctx.projects:
            project_dir = os.path.join(root, proj["path"])
            exclude = _sibling_exclude_dirs(root, proj["path"], ctx.projects)
            cache[proj["name"]] = _get_imported_workspace_packages(
                project_dir, workspace_names,
                exclude_dirs=exclude or None,
            )
        ctx._dep_import_cache = cache
        return cache

    @app.check("deps-unused")
    def check_deps_unused(ctx):
        """Declared workspace deps must be imported by at least one source file."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from .dep_validation import check_unused_deps, load_dep_overrides

        root = str(ctx.workspace_root)
        whitelist = load_dep_overrides(root)
        workspace_names = {p["name"] for p in ctx.projects}
        import_cache = _build_dep_import_cache(ctx)

        all_errors = []
        for proj in ctx.projects:
            name = proj["name"]
            project_dir = os.path.join(root, proj["path"])
            manifest_deps = {d.name for d in ctx.graph.dependencies(name)}
            errors = check_unused_deps(
                name, project_dir, manifest_deps, workspace_names, whitelist,
                _cached_imports=import_cache[name],
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

        from .dep_validation import check_undeclared_deps

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

        from .dep_validation import check_runtime_test_only

        import_cache = _build_dep_import_cache(ctx)

        all_flagged = []
        for proj in ctx.projects:
            name = proj["name"]
            manifest_deps_with_scope = {
                d.name: d.scope for d in ctx.graph.dependencies(name)
            }
            lib_imports, test_imports = import_cache[name]
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

        from .dep_validation import check_dev_in_lib

        import_cache = _build_dep_import_cache(ctx)

        all_flagged = []
        for proj in ctx.projects:
            name = proj["name"]
            manifest_deps_with_scope = {
                d.name: d.scope for d in ctx.graph.dependencies(name)
            }
            lib_imports, _test_imports = import_cache[name]
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

        from .commands.monorepo import _evaluate_constraint
        from .targets import TARGETS, detect_targets

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
                except Exception:
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

    # ------------------------------------------------------------------
    # Layer violations
    # ------------------------------------------------------------------

    @app.check("layers-violations")
    def check_layers_violations(ctx):
        """Dependency edges must not violate layer ordering."""
        if not isinstance(ctx, WorkspaceCheckContext):
            return CheckResult("skip", "not a monorepo workspace")

        from .layers import check_layer_violations, load_layer_config

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

    # ------------------------------------------------------------------
    # Dead module detection
    # ------------------------------------------------------------------

    @app.check("dead-modules")
    def check_dead_modules(ctx):
        """Unreferenced Python modules, Go internal packages, or npm source files."""
        from .targets import detect_targets

        root_str = str(ctx.project_root)
        target_entries = detect_targets(root_str)
        target_names = {e.name for e in target_entries}

        supported = {"pypi", "go", "npm"} & target_names
        if not supported:
            return CheckResult("skip", "not a Python, Go, or npm project")

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
            from .dep_validation import find_dead_modules

            py_dead = find_dead_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(py_dead)
            details.extend(
                f"{path}: not imported by any other module"
                for path in py_dead
            )

        if "go" in target_names:
            from .dep_validation import find_dead_go_packages

            go_dead = find_dead_go_packages(root_str, exclude_dirs=exclude)
            all_dead.extend(go_dead)
            details.extend(
                f"{path}: internal package not imported outside itself"
                for path in go_dead
            )

        if "npm" in target_names:
            from .dep_validation import find_dead_npm_modules

            npm_dead = find_dead_npm_modules(root_str, exclude_dirs=exclude)
            all_dead.extend(npm_dead)
            details.extend(
                f"{path}: not reachable from any entry point"
                for path in npm_dead
            )

        if all_dead:
            return CheckResult(
                "warn",
                f"{len(all_dead)} dead module(s)",
                details=details,
            )
        return CheckResult("pass", "no dead modules")

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

        errors = []
        for pattern in scan_patterns:
            for filepath in glob.glob(pattern):
                rel_path = os.path.relpath(filepath, root_str)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()
                except (OSError, UnicodeDecodeError):
                    continue

                matches = template_re.findall(content)
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
