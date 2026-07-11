"""Init command that scaffolds release infrastructure from templates, creating CI workflows, hooks, changelog, and config files."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

from ..action_versions import format_action, UnknownActionError
from ..ci_yaml import (
    parse_ci_workflow,
    emit_ci_workflow,
    inject_working_directory,
    rewrite_version_file_inputs,
)
from ..errors import ConfigError
from ..config import (
    _project_config,
    read_deploy_config,
    read_json_config,
    read_project_config,
    should_tag,
    write_project_config,
)
from ..lock import acquire_lock, release_lock
from ..pipelines import PIPELINE_TYPES, load_pipelines
from ..targets import TARGETS, detect_targets
from ..tagging import ensure_tags
from ..utils import commit_files, is_private_repo

MANAGED_FILES = os.path.join(".rlsbl", "managed-files.json")
BASES_DIR = os.path.join(".rlsbl", "bases")

_NPM_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")


def _find_git_dir():
    """Find the .git directory using git rev-parse, which works from any subdirectory.

    Returns the absolute path to the .git directory, or None if not inside a git repo.
    Unlike os.path.isdir(".git"), this works when CWD is a monorepo sub-project
    where .git/ lives at the repo root.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, check=True,
        )
        return os.path.abspath(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _is_workspace_root(project_root):
    """Check if project_root is a monorepo workspace root.

    Returns True when ``.rlsbl-monorepo/workspace.toml`` exists at the
    project root. Workspace roots are not importable Python packages, so
    CI templates (which run import checks) should be skipped -- the
    ci-router already handles per-package CI.
    """
    if project_root is None:
        return False
    return os.path.isfile(
        os.path.join(str(project_root), ".rlsbl-monorepo", "workspace.toml")
    )


def _is_non_releasable_project(project_root):
    """Check if the current project is non-releasable in its monorepo workspace.

    Returns False if not in a monorepo or if the project is releasable.
    """
    if project_root is None:
        return False
    from ..workspace import find_workspace_root, resolve_project
    ws_root = find_workspace_root(str(project_root))
    if ws_root is None:
        return False
    project = resolve_project(ws_root, str(project_root))
    if project is None:
        return False
    return not project.is_releasable


def _is_releasable_member_project(project_root):
    """Check if the project belongs to a named releasable in explicit mode.

    When a project has ``releasable = "name"`` (explicit mode), its changelog
    infrastructure lives at the releasable level, not per-package. Per-package
    ``CHANGELOG.md`` and ``unreleased.jsonl`` should be skipped.

    Returns False if not in a monorepo, not in explicit mode, or project has
    no named releasable assignment.
    """
    if project_root is None:
        return False
    from ..workspace import find_workspace_root, is_explicit_mode, resolve_project
    ws_root = find_workspace_root(str(project_root))
    if ws_root is None:
        return False
    if not is_explicit_mode(ws_root):
        return False
    project = resolve_project(ws_root, str(project_root))
    if project is None:
        return False
    # In explicit mode, projects with a named releasable have their changelog
    # at the releasable level. Only string values indicate membership.
    return isinstance(project.releasable, str)


def _get_releasable_config_dir(project_root):
    """Return the releasable config directory for a releasable member project.

    Returns the path to ``.rlsbl-monorepo/releasables/{name}/`` if the
    project belongs to a releasable in explicit mode, or None otherwise.
    """
    if project_root is None:
        return None
    from ..workspace import (
        find_workspace_root,
        get_releasable_dir,
        is_explicit_mode,
        load_releasables,
        load_workspace,
        resolve_releasable_for_project,
        resolve_project,
    )

    ws_root = find_workspace_root(str(project_root))
    if ws_root is None:
        return None
    if not is_explicit_mode(ws_root):
        return None
    project = resolve_project(ws_root, str(project_root))
    if project is None:
        return None
    if not isinstance(project.releasable, str):
        return None
    projects = load_workspace(ws_root)
    releasables = load_releasables(ws_root, projects=projects)
    rel = resolve_releasable_for_project(project, releasables)
    if rel is None:
        return None
    return get_releasable_dir(ws_root, rel.name)


def _skip_redundant_releasable_configs(project_root, warnings):
    """Remove per-package config.json that duplicates releasable-level config.

    When a releasable member's per-package config is identical to the
    releasable-level config, the per-package file is redundant -- the
    config inheritance system will produce the same result without it.

    For files that existed before scaffold and are identical to the
    releasable config, a warning is emitted suggesting cleanup.

    Modifies ``warnings`` list in place (appends cleanup warnings).

    Returns a list of paths that were removed (for commit tracking).
    """
    rel_config_dir = _get_releasable_config_dir(project_root)
    if rel_config_dir is None:
        return []

    removed = []

    rel_path = os.path.join(rel_config_dir, "config.json")
    pkg_path = os.path.join(str(project_root), ".rlsbl", "config.json")

    if not os.path.isfile(pkg_path):
        return []
    if not os.path.isfile(rel_path):
        return []

    pkg_config = read_json_config(pkg_path)
    rel_config = read_json_config(rel_path)

    if pkg_config == rel_config:
        subprocess.run(
            [
                "saferm", "delete",
                "--description",
                "Removing redundant per-package .rlsbl/config.json "
                "(identical to releasable config)",
                pkg_path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        removed.append(pkg_path)
        print(
            "Skipped per-package .rlsbl/config.json "
            "(identical to releasable config)"
        )

    return removed


def _check_npm_lockfile_missing(start_dir="."):
    """Check if any npm lockfile exists from start_dir up to the git root.

    Returns True if no lockfile is found (i.e., lockfile is missing).
    Prints a warning to stderr when missing.
    """
    current = os.path.abspath(start_dir)
    while True:
        for lockfile in _NPM_LOCKFILES:
            if os.path.exists(os.path.join(current, lockfile)):
                return False
        # Stop if we reached the git root
        if os.path.isdir(os.path.join(current, ".git")):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    print(
        'Warning: no lockfile found. CI uses "npm ci" which requires package-lock.json.',
        file=sys.stderr,
    )
    return True


def _is_npm_wrapper(npm_dir_path):
    """Check if the npm package at npm_dir_path is a wrapper.

    A package is a wrapper if it has no test script OR has zero dependencies
    (no ``dependencies`` and no ``devDependencies``, or both are empty objects).
    """
    pkg_path = os.path.join(npm_dir_path, "package.json")
    if not os.path.exists(pkg_path):
        return True
    with open(pkg_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    test_script = pkg.get("scripts", {}).get("test", "")
    if not test_script.strip():
        return True
    deps = pkg.get("dependencies", {})
    dev_deps = pkg.get("devDependencies", {})
    if not deps and not dev_deps:
        return True
    return False


# Files owned by the user after initial scaffold -- never overwrite or merge
USER_OWNED = {
    "CHANGELOG.md",
    ".npmignore",
    ".rlsbl/changes/unreleased.jsonl",
    # Custom workflow files: never created by scaffold, never touched on update.
    # Users put extra jobs here to avoid three-way merge conflicts on ci.yml/publish.yml.
    # The same paths work for both standalone projects and monorepo roots, since
    # monorepo workflows live under .github/workflows/ regardless.
    ".github/workflows/ci-custom.yml",
    ".github/workflows/publish-custom.yml",
}


def file_hash(path):
    """SHA-256 hash of a file's contents."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_managed_files():
    """Load the managed-files registry from .rlsbl/managed-files.json.

    The managed-files registry tracks template-derived files from apply_plans
    for orphan detection.

    Returns the files dict ({path: hash}), or {} if the file is missing.
    """
    if os.path.exists(MANAGED_FILES):
        with open(MANAGED_FILES) as f:
            data = json.load(f)
        return data.get("files", {})
    return {}


def save_managed_files(files):
    """Write the managed-files registry to .rlsbl/managed-files.json."""
    os.makedirs(os.path.dirname(MANAGED_FILES), exist_ok=True)
    with open(MANAGED_FILES, "w") as f:
        json.dump({"version": 1, "files": files}, f, indent=2)
        f.write("\n")


def _ensure_target_in_config(registry_name, ctx):
    """Add ``registry_name`` to the ``targets`` array in the on-disk per-project
    ``.rlsbl/config.json`` if not already present.

    Reads the CURRENT targets straight from disk -- the exact same file
    ``write_project_config`` writes -- rather than from ``ctx.config``.
    ``ctx.config`` may be empty (a bare ``ProjectContext``) or releasable-merged;
    using it would clobber structured on-disk entries like
    ``{"name": "go", "path": "go/"}`` down to plain strings, losing the
    subdirectory path. Structured dict entries are preserved untouched; the new
    registry is appended as a plain string only when it is not already present
    under either representation. The file is written only when something was
    actually added. ``ctx.config`` is refreshed to a fresh read of the merged
    project config afterwards so callers observe disk state.
    """
    disk_targets = read_json_config(_project_config(ctx.project_root)).get("targets", [])
    if not isinstance(disk_targets, list):
        disk_targets = []
    # Names already present, under either the plain-string or {"name": ...} form.
    existing_names = []
    for t in disk_targets:
        if isinstance(t, str):
            existing_names.append(t)
        elif isinstance(t, dict):
            existing_names.append(t.get("name", ""))
    if registry_name not in existing_names:
        new_targets = list(disk_targets) + [registry_name]
        write_project_config("targets", new_targets, ctx.project_root)
    # Refresh ctx.config from disk in both branches so it never diverges.
    ctx.config = read_project_config(ctx.project_root)


NEXT_STEPS = {
    "npm": [
        "Add an NPM_TOKEN secret to your GitHub repo (Settings > Secrets > Actions)",
        "Push to GitHub to activate the CI workflow",
        "Run rlsbl release [patch|minor|major]",
    ],
    "pypi": [
        "Push to GitHub",
        "Configure Trusted Publishing on pypi.org",
        "Run rlsbl release [patch|minor|major]",
    ],
    "go": [
        "GoReleaser runs in CI via GitHub Actions (no local install needed)",
        "Push to GitHub to activate the CI workflow",
        "Run rlsbl release [patch|minor|major]",
    ],
}


_ESCAPE_SENTINEL = "__RLSBL_ESCAPE_OPEN__"


def process_template(template_content, vars_dict, template_path=None, *, required_vars=None):
    r"""Process a template string with substitution and escape handling.

    Pass 1 resolves ``{{action "owner/name"}}`` placeholders against the
    central action-version table (rlsbl/data/action_versions.toml). An
    unknown action raises :class:`UnknownActionError` immediately -- no
    implicit defaults.

    Pass 1.5 resolves conditional blocks ``{{#if varName}}...{{/if}}``.
    If ``vars_dict[varName]`` is truthy (present and non-empty string),
    the body is kept; otherwise the entire block is removed. Blank lines
    left by removed blocks are collapsed. Non-nested only. Actions inside
    conditional blocks are resolved (Pass 1 runs first); variables inside
    surviving blocks are resolved (Pass 2 runs after).

    Pass 2 resolves the existing ``{{varName}}`` (and dotted ``{{a.b}}``)
    placeholders against ``vars_dict``.

    Escaped placeholders: ``\{{word}}`` in a template emits ``{{word}}``
    literally in the output (the backslash is consumed, the braces are
    preserved). This lets templates contain third-party ``{{...}}`` syntax
    (e.g. Docker metadata-action's ``{{version}}``) without colliding with
    rlsbl's template engine.

    If *required_vars* is provided (a set of variable names), any variable
    in that set that remains unreplaced after substitution raises
    :class:`ValueError`. This turns silent placeholder leaks into hard
    errors for critical template variables.

    Returns ``(content, unreplaced)`` where ``unreplaced`` is the list of
    variable names in pass 2 that had no entry in ``vars_dict``. Pass 1
    misses raise instead of being collected.
    """

    # Pre-pass: shelter escaped placeholders from substitution.
    content = template_content.replace(r"\{{", _ESCAPE_SENTINEL)

    # Pass 1: action placeholders.
    def action_replacer(match):
        action_name = match.group(1)
        try:
            return format_action(action_name)
        except UnknownActionError as exc:
            ctx = f" in {template_path}" if template_path else ""
            raise UnknownActionError(
                f"Unknown action {action_name!r}{ctx}: {exc}"
            ) from exc

    content = re.sub(
        r'\{\{action\s+"([^"]+)"\}\}', action_replacer, content
    )

    # Pass 1.5: conditional blocks — {{#if varName}}...{{/if}}.
    # Non-nested only. Truthy = present in vars_dict and non-empty string.
    # Removed blocks have surrounding blank lines collapsed to avoid gaps.
    def conditional_replacer(match):
        var_name = match.group(1)
        body = match.group(2)
        if vars_dict.get(var_name):
            return body
        # Remove the block and strip leading/trailing blank lines from the gap.
        return ""

    content = re.sub(
        r"\{\{#if\s+(\w+(?:\.\w+)*)\}\}(.*?)\{\{/if\}\}",
        conditional_replacer,
        content,
        flags=re.DOTALL,
    )

    # Collapse runs of 3+ newlines (left by removed conditional blocks) to 2.
    content = re.sub(r"\n{3,}", "\n\n", content)

    # Pass 2: variable placeholders (existing behavior).
    unreplaced = []

    def replacer(match):
        var_name = match.group(1)
        if var_name in vars_dict:
            return vars_dict[var_name]
        unreplaced.append(var_name)
        return match.group(0)

    content = re.sub(r"\{\{(\w+(?:\.\w+)*)\}\}", replacer, content)

    # Validate required variables before restoring escapes.
    if required_vars:
        missing = set(required_vars) & set(unreplaced)
        if missing:
            ctx = f" in {template_path}" if template_path else ""
            names = ", ".join(sorted(missing))
            raise ConfigError(
                f"Required template variable(s) not provided{ctx}: {names}"
            )

    # Post-pass: restore escaped placeholders to literal ``{{``.
    content = content.replace(_ESCAPE_SENTINEL, "{{")

    return content, unreplaced


def check_unreplaced_vars(source_path, unreplaced):
    """Raise ConfigError if *unreplaced* is non-empty.

    Shared by scaffold (apply_plans) and monorepo sync so the check
    logic lives in one place and tests can exercise it directly.
    """
    if unreplaced:
        raise ConfigError(
            f"{source_path}: unresolved template variables: "
            f"{', '.join(unreplaced)}"
        )


def _save_base(target, content):
    """Save rendered template content as the merge base for future three-way merges."""
    base_path = os.path.join(BASES_DIR, target)
    os.makedirs(os.path.dirname(base_path), exist_ok=True)
    with open(base_path, "w", encoding="utf-8") as f:
        f.write(content)


def _load_base(target):
    """Load the stored merge base for a target file. Returns None if not stored."""
    base_path = os.path.join(BASES_DIR, target)
    if not os.path.exists(base_path):
        return None
    with open(base_path, "r", encoding="utf-8") as f:
        return f.read()


def _three_way_merge(ours_text, base_text, theirs_text):
    """Three-way merge using git merge-file.

    Writes three temp files in the project dir (not /tmp), runs
    `git merge-file -p ours base theirs`, and returns (merged_text, has_conflicts).
    Exit code: 0 = clean merge, positive = number of conflicts, negative = error.
    """
    ours_tmp = theirs_tmp = base_tmp = None
    try:
        ours_tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ours", dir=".", delete=False, encoding="utf-8",
        )
        ours_tmp.write(ours_text)
        ours_tmp.close()

        base_tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".base", dir=".", delete=False, encoding="utf-8",
        )
        base_tmp.write(base_text)
        base_tmp.close()

        theirs_tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".theirs", dir=".", delete=False, encoding="utf-8",
        )
        theirs_tmp.write(theirs_text)
        theirs_tmp.close()

        result = subprocess.run(
            ["git", "merge-file", "-p", ours_tmp.name, base_tmp.name, theirs_tmp.name],
            capture_output=True, text=True,
        )
        merged_text = result.stdout
        # Exit code 0 = clean, positive = number of conflicts, negative = error
        has_conflicts = result.returncode > 0
        if result.returncode < 0:
            # Treat errors as conflicts so the caller knows something went wrong
            has_conflicts = True
        return merged_text, has_conflicts
    finally:
        for tmp in (ours_tmp, base_tmp, theirs_tmp):
            if tmp is not None:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass


def plan_mappings(template_dir, mappings, vars_dict, force, *, required_vars=None):
    """Compute what process_mappings would do, without writing anything.

    When *required_vars* is provided (a set of variable names), it is
    forwarded to :func:`process_template` for every mapping. Any required
    variable that remains unresolved raises :class:`ValueError`, turning
    silent placeholder leaks into hard scaffold-time errors.

    Returns a list of plan dicts. Each plan represents one mapping and contains:
      - "target": the target file path
      - "status": one of "new", "updated", "unchanged", "skipped", "user-owned",
        or a string starting with "CONFLICTS"; or status values like
        "overwritten", "created", "merged", "updated (additive merge)",
        "year updated (...)" -- the same vocabulary the original function
        produced for the (created, skipped) lists.
      - "bucket": "created" or "skipped" -- which result list this entry belongs in
      - "action": one of "write", "save_base_only", "license_year_update",
        "gitignore_merge", "merge_write", "none". Tells apply_plans what to do.
      - "content": the bytes to write (when action requires it). None otherwise.
      - "base_content": template content to save as the new merge base. None
        when no base should be saved this run.
      - "warning": optional extra warning string emitted alongside this plan
      - "unreplaced": list of unreplaced template var names (for warnings)
      - "year_update": for license_year_update, a dict with "current_year",
        "old_year" so apply can recompute the new content
      - "additive_lines": for gitignore_merge, the lines to append
      - "existing_content": for gitignore_merge, the original content
      - "template_not_found": True for warning-only entries
    """
    plans = []

    for mapping in mappings:
        template = mapping["template"]
        target = mapping["target"]

        template_path = os.path.join(template_dir, template)
        if not os.path.exists(template_path):
            plans.append({
                "target": target,
                "status": None,
                "bucket": None,
                "action": "warn_only",
                "warning": f"Template not found: {template_path}",
            })
            continue

        with open(template_path, "r", encoding="utf-8") as f:
            raw = f.read()
        theirs, unreplaced = process_template(
            raw, vars_dict, template_path=template_path, required_vars=required_vars,
        )

        # --- User-owned files: never overwrite (even with --force).
        if os.path.exists(target) and target in USER_OWNED:
            plans.append({
                "target": target,
                "status": "user-owned",
                "bucket": "skipped",
                "action": "none",
            })
            continue

        # --- .gitignore: additive set-union merge (append new lines, never remove) ---
        if target == ".gitignore" and os.path.exists(target):
            with open(target, "r", encoding="utf-8") as f:
                existing_content = f.read()
            existing_lines = set()
            for line in existing_content.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    existing_lines.add(stripped)
            new_lines = []
            for line in theirs.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and stripped not in existing_lines:
                    new_lines.append(line)
            if new_lines:
                parts = [existing_content.rstrip("\n"), ""] + new_lines + [""]
                merged_content = "\n".join(parts)
                plans.append({
                    "target": target,
                    "status": "updated (additive merge)",
                    "bucket": "created",
                    "action": "write",
                    "content": merged_content,
                    "base_content": theirs,
                })
            else:
                plans.append({
                    "target": target,
                    "status": "unchanged",
                    "bucket": "skipped",
                    "action": "save_base_only",
                    "base_content": theirs,
                })
            continue

        # --- New file or force overwrite (non-user-owned): write and save base ---
        if not os.path.exists(target) or force:
            is_overwrite = os.path.exists(target) and force
            status = "overwritten" if is_overwrite else "created"
            plan = {
                "target": target,
                "status": status,
                "bucket": "created",
                "action": "write",
                "content": theirs,
                "base_content": theirs,
            }
            if unreplaced:
                plan["unreplaced"] = unreplaced
            plans.append(plan)
            continue

        # --- Three-way merge for all other existing files ---
        with open(target, "r", encoding="utf-8") as f:
            ours = f.read()
        base = _load_base(target)

        if base is None:
            # No base stored (legacy project or first update after migration).
            if ours == theirs:
                plans.append({
                    "target": target,
                    "status": "unchanged, base seeded",
                    "bucket": "skipped",
                    "action": "save_base_only",
                    "base_content": theirs,
                })
            else:
                plans.append({
                    "target": target,
                    "status": "no base -- run scaffold --force to enable merging",
                    "bucket": "skipped",
                    "action": "save_base_only",
                    "base_content": theirs,
                    "warning": (
                        f"{target}: no base stored, cannot merge; "
                        "run scaffold --force to reset"
                    ),
                })
            continue

        if ours == base:
            plan = {
                "target": target,
                "status": "updated",
                "bucket": "created",
                "action": "write",
                "content": theirs,
                "base_content": theirs,
            }
            if unreplaced:
                plan["unreplaced"] = unreplaced
            plans.append(plan)
        elif base == theirs:
            plans.append({
                "target": target,
                "status": "unchanged",
                "bucket": "skipped",
                "action": "none",
            })
        elif ours == theirs:
            plans.append({
                "target": target,
                "status": "unchanged",
                "bucket": "skipped",
                "action": "none",
            })
        else:
            merged, has_conflicts = _three_way_merge(ours, base, theirs)
            if has_conflicts:
                plan = {
                    "target": target,
                    "status": "CONFLICTS -- resolve manually",
                    "bucket": "created",
                    "action": "write",
                    "content": merged,
                    "base_content": theirs,
                    "warning": f"{target}: merge conflicts detected, resolve manually",
                }
            else:
                plan = {
                    "target": target,
                    "status": "merged",
                    "bucket": "created",
                    "action": "write",
                    "content": merged,
                    "base_content": theirs,
                }
            if unreplaced:
                plan["unreplaced"] = unreplaced
            plans.append(plan)

    return plans


def apply_plans(plans):
    """Apply a list of plans from plan_mappings, performing all side effects.

    Returns (created, skipped, warnings, new_hashes) matching the original
    process_mappings return shape.
    """
    created = []
    skipped = []
    warnings = []
    new_hashes = {}

    for plan in plans:
        action = plan.get("action")
        target = plan["target"]

        if action == "warn_only":
            warnings.append(plan["warning"])
            continue

        if action == "none":
            if plan["bucket"] == "skipped":
                skipped.append((target, plan["status"]))
            else:
                created.append((target, plan["status"]))
            if os.path.exists(target):
                new_hashes[target] = file_hash(target)
            continue

        if action == "save_base_only":
            _save_base(target, plan["base_content"])
            if plan["bucket"] == "skipped":
                skipped.append((target, plan["status"]))
            else:
                created.append((target, plan["status"]))
            if plan.get("warning"):
                warnings.append(plan["warning"])
            if os.path.exists(target):
                new_hashes[target] = file_hash(target)
            continue

        if action in ("write", "write_no_base"):
            target_dir = os.path.dirname(target)
            if target_dir and target_dir != ".":
                os.makedirs(target_dir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(plan["content"])
            if action == "write" and plan.get("base_content") is not None:
                _save_base(target, plan["base_content"])
            new_hashes[target] = file_hash(target)
            if plan["bucket"] == "created":
                created.append((target, plan["status"]))
            else:
                skipped.append((target, plan["status"]))
            if plan.get("warning"):
                warnings.append(plan["warning"])
            check_unreplaced_vars(target, plan.get("unreplaced"))
            continue

    return created, skipped, warnings, new_hashes


def process_mappings(template_dir, mappings, vars_dict, force):
    """Process a list of template mappings: read each template, apply vars, write target files.

    Uses a universal three-way merge (via git merge-file) for existing files:
    base (last scaffolded version) + ours (user's current file) + theirs (new template).
    USER_OWNED files are never overwritten or merged.

    Returns (created, skipped, warnings, new_hashes).
    created/skipped are lists of (target, status) tuples for unified display.

    Implemented as plan_mappings() (pure analysis) + apply_plans() (side effects).
    """
    plans = plan_mappings(template_dir, mappings, vars_dict, force)
    return apply_plans(plans)


def _print_file_status_table(created, skipped):
    """Print the unified file list table with dot-padded status column."""
    all_files = [(t, s) for t, s in created] + [(t, s) for t, s in skipped]
    if not all_files:
        return
    all_files.sort(key=lambda item: item[0])
    max_target_len = max(len(t) for t, _ in all_files)
    pad_width = max_target_len + 4
    print("Files:")
    for target, status in all_files:
        dots = " " + "." * (pad_width - len(target)) + " "
        print(f"  {target}{dots}{status}")


def _print_dry_run_report(plans_groups, registry=None, registries=None):
    """Print the file status table from plans without applying them.

    plans_groups is a list of plan lists (registry plans, shared plans, etc.).
    """
    print("=" * 60)
    print("DRY RUN -- no changes made")
    print("=" * 60)

    created = []
    skipped = []
    warnings = []
    for plans in plans_groups:
        for plan in plans:
            if plan.get("action") == "warn_only":
                warnings.append(plan["warning"])
                continue
            if plan.get("bucket") == "created":
                created.append((plan["target"], plan["status"]))
            elif plan.get("bucket") == "skipped":
                skipped.append((plan["target"], plan["status"]))
            if plan.get("warning"):
                warnings.append(plan["warning"])
            check_unreplaced_vars(plan["target"], plan.get("unreplaced"))

    _print_file_status_table(created, skipped)

    if warnings:
        print("Warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  {w}", file=sys.stderr)

    # Show orphans that would be removed
    if os.path.exists(MANAGED_FILES):
        planned_targets = set()
        for plans in plans_groups:
            for plan in plans:
                if plan.get("action") != "warn_only":
                    planned_targets.add(plan["target"])
        old_managed = load_managed_files()
        orphan_keys = set(old_managed.keys()) - planned_targets
        orphans_to_show = sorted(orphan_keys)
        if orphans_to_show:
            print()
            for orphan_path in orphans_to_show:
                print(f"Would remove: {orphan_path}")

    print()
    print("DRY RUN -- no files were written, no commits made.")


def _install_or_update_hook(hook_name, current_content, current_hash, known_hashes):
    """Install or update a git hook, upgrading older rlsbl versions in place.

    Generic hook installer used for any git hook type (pre-push,
    post-rewrite, etc.).  Each hook type provides its own content,
    current hash, and set of known historical hashes.

    Args:
        hook_name: git hook name (e.g. "pre-push", "post-rewrite").
        current_content: the current template content to install.
        current_hash: SHA-256 hash of current_content (via compute_hook_hash).
        known_hashes: frozenset of all historical content hashes for this hook.

    Behavior:
      - .git missing                -> no-op
      - hook missing                -> write current template, chmod 755
      - hook matches current hash   -> no-op (already up to date)
      - hook matches old known hash -> overwrite, print upgrade notice
      - hook hash unknown           -> skip, print warning + unified diff
    """
    import difflib
    from ..hook_hashes import compute_hook_hash

    git_dir = _find_git_dir()
    if git_dir is None:
        return

    hooks_dir = os.path.join(git_dir, "hooks")
    hook_target = os.path.join(hooks_dir, hook_name)

    if not os.path.exists(hook_target):
        os.makedirs(hooks_dir, exist_ok=True)
        with open(hook_target, "w", encoding="utf-8") as f:
            f.write(current_content)
        os.chmod(hook_target, 0o755)
        print(f"Installed {hook_name} hook (.git/hooks/{hook_name})")
        return

    with open(hook_target, "r", encoding="utf-8") as f:
        installed = f.read()
    installed_hash = compute_hook_hash(installed)

    if installed_hash == current_hash:
        return

    if installed_hash in known_hashes:
        with open(hook_target, "w", encoding="utf-8") as f:
            f.write(current_content)
        os.chmod(hook_target, 0o755)
        print(f"Updated {hook_name} hook (was an older rlsbl version).")
        return

    # Unknown content -- assume user-customized. Show a diff so the user can
    # decide whether to delete the hook and re-scaffold to accept ours.
    diff_lines = list(difflib.unified_diff(
        installed.splitlines(keepends=True),
        current_content.splitlines(keepends=True),
        fromfile=hook_target,
        tofile="rlsbl template",
    ))
    diff_text = "".join(diff_lines)
    print(
        f"{hook_name} hook appears customized -- not overwriting. Diff:\n"
        f"{diff_text}"
        "  To accept the rlsbl template, delete the hook and re-run scaffold.",
        file=sys.stderr,
    )


def _install_or_update_pre_push_hook():
    """Install the rlsbl pre-push hook, upgrading older versions in place.

    Delegates to the generic ``_install_or_update_hook`` with pre-push-specific
    content and hashes from ``hook_hashes.py``.
    """
    from ..hook_hashes import (
        CURRENT_PRE_PUSH_HOOK,
        CURRENT_PRE_PUSH_HOOK_HASH,
        PRE_PUSH_HOOK_HASHES,
    )

    _install_or_update_hook(
        "pre-push",
        CURRENT_PRE_PUSH_HOOK,
        CURRENT_PRE_PUSH_HOOK_HASH,
        PRE_PUSH_HOOK_HASHES,
    )


def _install_or_update_post_rewrite_hook():
    """Install the rlsbl post-rewrite hook, upgrading older versions in place.

    Delegates to the generic ``_install_or_update_hook`` with
    post-rewrite-specific content and hashes from ``hook_hashes.py``.
    """
    from ..hook_hashes import (
        CURRENT_POST_REWRITE_HOOK,
        CURRENT_POST_REWRITE_HOOK_HASH,
        POST_REWRITE_HOOK_HASHES,
    )

    _install_or_update_hook(
        "post-rewrite",
        CURRENT_POST_REWRITE_HOOK,
        CURRENT_POST_REWRITE_HOOK_HASH,
        POST_REWRITE_HOOK_HASHES,
    )


def _finalize_scaffold(all_hash_dicts, created, skipped, warnings, *,
                       registry=None, flags=None, registries=None,
                       npm_lockfile_missing=False, target_paths=None,
                       project_root, config):
    """Shared post-processing for scaffold: chmod, hooks, version marker, tagging, summary.

    all_hash_dicts is a list of dicts to merge for managed-files tracking.
    flags is the CLI flags dict (used for tagging check).
    registries is a list of registry names (used for tagging).
    npm_lockfile_missing: if True, prepend a lockfile step to npm next steps.
    """
    if flags is None:
        flags = {}
    if registries is None:
        registries = [registry] if registry else []
    # Make all shell scripts in .rlsbl/hooks/ executable
    hooks_dir = os.path.join(".", ".rlsbl", "hooks")
    if os.path.isdir(hooks_dir):
        for entry in os.listdir(hooks_dir):
            if entry.endswith(".sh"):
                os.chmod(os.path.join(hooks_dir, entry), 0o755)

    # Install or update the pre-push hook.
    #
    # Strategy: content-hash detection. Every prior rlsbl-shipped hook content
    # has its SHA-256 in PRE_PUSH_HOOK_HASHES. If the installed hook matches
    # one of those, it's safe to overwrite with the current template. If the
    # hash is unknown the user has likely customized it -- leave it alone and
    # show a diff so they can decide.
    _install_or_update_pre_push_hook()

    # Install or update the post-rewrite hook (automatic changelog hash
    # remapping after amend/rebase). Same content-hash strategy as pre-push.
    _install_or_update_post_rewrite_hook()

    # Write scaffolding version marker so the pre-push hook can detect drift
    from rlsbl import __version__
    marker_dir = os.path.join(".", ".rlsbl")
    os.makedirs(marker_dir, exist_ok=True)
    marker_path = os.path.join(marker_dir, "version")
    with open(marker_path, "w") as f:
        f.write(__version__ + "\n")
    print("Wrote scaffolding version marker (.rlsbl/version)")

    # Persist file hashes for future customization detection
    all_new_hashes = {}
    for h in all_hash_dicts:
        all_new_hashes.update(h)

    # Detect and clean up orphaned managed files
    # Orphan detection uses managed-files.json (template-derived files only).
    old_managed = load_managed_files()
    orphan_keys = set(old_managed.keys()) - set(all_new_hashes.keys())
    dry_run = flags.get("dry-run", False)
    force = flags.get("force", False)
    orphan_removed: set[str] = set()
    for orphan_path in sorted(orphan_keys):
        if dry_run:
            print(f"Would remove: {orphan_path}")
            continue
        if not os.path.exists(orphan_path):
            continue
        stored_hash = old_managed[orphan_path]
        if force or file_hash(orphan_path) == stored_hash:
            subprocess.run(
                [
                    "saferm", "delete",
                    "--description",
                    f"Removing orphaned scaffold file: {orphan_path}",
                    orphan_path,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            orphan_removed.add(orphan_path)
            # Also clean up the merge base if it exists
            base_path = os.path.join(BASES_DIR, orphan_path)
            if os.path.exists(base_path):
                subprocess.run(
                    [
                        "saferm", "delete",
                        "--description",
                        f"Removing orphaned scaffold merge base: {base_path}",
                        base_path,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                # Prune empty parent directories up to BASES_DIR
                try:
                    os.removedirs(os.path.dirname(base_path))
                except OSError:
                    pass
            created.append((orphan_path, "removed (orphan)"))
        else:
            print(
                f"Warning: {orphan_path} has been modified — skipping orphan deletion "
                f"(use --force to override)",
                file=sys.stderr,
            )

    # Sweep orphaned merge bases: base files whose corresponding managed file
    # is no longer in the current scaffold run (e.g., target changed, file
    # template removed).  The first pass above handles bases for orphaned managed
    # files; this second pass catches bases that linger after the managed file
    # was removed outside the orphan loop (e.g., manually deleted or renamed).
    if os.path.isdir(BASES_DIR):
        for dirpath, _dirnames, filenames in os.walk(BASES_DIR):
            for fname in filenames:
                base_abs = os.path.join(dirpath, fname)
                managed_rel = os.path.relpath(base_abs, BASES_DIR)
                if managed_rel not in all_new_hashes:
                    if dry_run:
                        print(f"Would remove orphaned base: {base_abs}")
                    else:
                        subprocess.run(
                            [
                                "saferm", "delete",
                                "--description",
                                f"Removing orphaned scaffold merge base: {base_abs}",
                                base_abs,
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        print(f"Removing orphaned scaffold merge base: {base_abs}")
                        # Prune empty parent directories up to BASES_DIR
                        try:
                            os.removedirs(os.path.dirname(base_abs))
                        except OSError:
                            pass

    # Save managed-files registry (template-derived files for orphan tracking)
    save_managed_files(all_new_hashes)

    # Clean up legacy hashes.json if present
    legacy_hashes = os.path.join(".rlsbl", "hashes.json")
    if os.path.exists(legacy_hashes):
        subprocess.run(
            [
                "saferm", "delete",
                "--description",
                "legacy hashes.json no longer used by scaffold",
                legacy_hashes,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    # Ecosystem tagging
    if should_tag(flags, config):
        ensure_tags(registries, target_paths=target_paths, project_root=project_root)

    # Print unified file list with dot-padded status column
    _print_file_status_table(created, skipped)

    if warnings:
        print("Warnings:", file=sys.stderr)
        for w in warnings:
            print(f"  {w}", file=sys.stderr)

    # Tip when ci.yml or publish.yml had a merge conflict: recommend the
    # user-owned custom workflow file so they don't fight three-way merge.
    _CUSTOM_WORKFLOW_TIP_PATHS = (
        ".github/workflows/ci.yml",
        ".github/workflows/publish.yml",
    )
    conflicted_workflow_paths = [
        t for t, s in created
        if t in _CUSTOM_WORKFLOW_TIP_PATHS and s.startswith("CONFLICTS")
    ]
    if conflicted_workflow_paths:
        for cw in conflicted_workflow_paths:
            custom = cw.replace(".yml", "-custom.yml")
            print(
                f"   Tip: to customize CI without conflicts, move your custom jobs to\n"
                f"   {custom} (scaffold never touches this file)."
            )

    # Helpful note when existing CI workflow is preserved
    ci_path = ".github/workflows/ci.yml"
    if any(t == ci_path for t, _ in skipped):
        print("\nNote: Existing CI workflow preserved. Review and merge manually if needed.")

    # Next steps
    if registry:
        steps = NEXT_STEPS.get(registry)
        if steps:
            steps = list(steps)
            if npm_lockfile_missing:
                steps.insert(0, 'Run "npm install" and commit package-lock.json before pushing')
            print("\nNext steps:")
            for i, step in enumerate(steps, 1):
                print(f"  {i}. {step}")

    # Auto-commit scaffold changes unless --no-auto-commit is set
    if not flags.get("auto-commit", True):
        print("Skipping commit (--no-auto-commit).")
        return

    # Collect all files that were created/modified (not "unchanged" or "skipped").
    # Conflicted files contain merge markers and must NOT be committed.
    conflicted_files = [t for t, s in created if s.startswith("CONFLICTS")]
    files_to_commit = [t for t, s in created
                       if s not in ("unchanged", "skipped", "user-owned")
                       and not s.startswith("CONFLICTS")]
    if conflicted_files:
        print(
            f"Skipped commit for {len(conflicted_files)} conflicted file(s). "
            "Resolve markers and commit manually:",
            file=sys.stderr,
        )
        for cf in conflicted_files:
            print(f"  {cf}", file=sys.stderr)
    # Include .rlsbl/ internal files written during scaffold
    config_file = os.path.join(".rlsbl", "config.json")
    for rlsbl_file in [MANAGED_FILES, os.path.join(".rlsbl", "version"), config_file]:
        if os.path.exists(rlsbl_file) and rlsbl_file not in files_to_commit:
            files_to_commit.append(rlsbl_file)
    # Include any base files that were saved for the created targets
    if os.path.isdir(BASES_DIR):
        for target, _ in created:
            base_path = os.path.join(BASES_DIR, target)
            if os.path.exists(base_path) and base_path not in files_to_commit:
                files_to_commit.append(base_path)

    if not files_to_commit:
        return

    # Only attempt commit if we're in a git repo
    if _find_git_dir() is None:
        return

    # Untrack files that are now gitignored but still tracked by git.
    # Adding a path to .gitignore only prevents new untracked files from being
    # staged -- it has no effect on files already in the index.
    try:
        result = subprocess.run(
            ["git", "ls-files", "-ic", "--exclude-standard", "-z"],
            capture_output=True, text=True, check=True,
        )
        if result.stdout:
            ignored_tracked = [f for f in result.stdout.split("\0") if f]
            for path in ignored_tracked:
                subprocess.run(
                    ["git", "rm", "--cached", path],
                    capture_output=True, text=True, check=True,
                )
            # Commit the removal separately so it doesn't interfere with
            # safegit's file staging (which would re-add the files).
            subprocess.run(
                ["git", "commit", "--trailer", "Autogenerated: true", "-m", "untrack gitignored files"],
                capture_output=True, text=True, check=True,
            )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"Warning: could not untrack gitignored files: {e}", file=sys.stderr)

    if commit_files("rlsbl scaffold", files_to_commit, allow_failure=True):
        print("Committed scaffold changes.")


def _resolve_private(flags, ctx):
    """Determine if this is a private repository.

    Checks --private flag first, then saved config, then auto-detects via GitHub API.
    The caller is responsible for persisting the value to config.json.

    Returns True/False, or False if detection fails.
    """
    if flags.get("private"):
        return True

    # Check saved config
    if "private" in ctx.config:
        return bool(ctx.config["private"])

    # Auto-detect via GitHub API
    detected = is_private_repo()
    if detected is not None:
        return detected

    return False


def _append_deploy_workflow_if_configured(mappings, config):
    """Add deploy workflow template to mappings if deploy config exists."""
    deploy_targets, _ = read_deploy_config(config)
    if deploy_targets:
        mappings = list(mappings)
        mappings.append({
            "template": ".github/workflows/deploy.yml.tpl",
            "target": ".github/workflows/deploy.yml",
        })
    return mappings


def _append_release_dispatch_if_configured(mappings, config):
    """Add release-dispatch workflow template to mappings if remote_release is enabled."""
    if config.get("remote_release"):
        mappings = list(mappings)
        mappings.append({
            "template": ".github/workflows/release-dispatch.yml.tpl",
            "target": ".github/workflows/release-dispatch.yml",
        })
    return mappings


def _print_private_summary():
    """Print helpful output for private repository scaffold."""
    print("\nPrivate repository detected. Scaffold configured for private distribution.")
    print("- publish.yml skipped (no public registry)")
    print("- Asset upload is a built-in release step (configure via publish.<target>.assets)")
    print("\nConsumers can install via:")
    print('  Python: uv pip install "pkg @ git+ssh://git@github.com/owner/repo@vX.Y.Z"')
    print("  npm:    npm install git+ssh://git@github.com/owner/repo#vX.Y.Z")
    print("  Go:     go get github.com/owner/repo@vX.Y.Z")


def _ensure_pipeline_config(registries, ctx):
    """Generate default pipeline config for detected targets if not already present.

    For each detected target whose name matches a PIPELINE_TYPES key,
    creates a pipeline entry with name=target_name, type=target_name, local=false.
    If multiple targets share the same pipeline type, errors with a message
    telling the user to name pipelines manually.

    Writes the generated pipeline entries to config.json under the "pipelines" key.
    Skips if "pipelines" already exists in config.
    """
    if "pipelines" in ctx.config:
        return

    pipelines = {}
    seen_types = {}
    for target_name in registries:
        if target_name in PIPELINE_TYPES:
            if target_name in seen_types:
                print(
                    f"Error: multiple targets use the same pipeline type '{target_name}'. "
                    f"Configure pipelines manually in .rlsbl/config.json.",
                    file=sys.stderr,
                )
                sys.exit(1)
            seen_types[target_name] = True
            pipelines[target_name] = {
                "type": target_name,
                "local": False,
            }

    if pipelines:
        ctx.config = write_project_config("pipelines", pipelines, ctx.project_root)


def _trigger_monorepo_sync(auto_commit=True):
    """If the current directory is inside a monorepo workspace, run sync.

    Uses a subprocess so that sys.exit() calls inside sync don't kill scaffold.
    Failures are silently ignored -- sync is best-effort after scaffold.

    When ``auto_commit`` is False, propagates ``--no-auto-commit`` to the sync
    call so a single user invocation with ``--no-auto-commit`` produces zero
    commits.
    """
    from ..workspace import find_workspace_root

    ws_root = find_workspace_root(".")
    if ws_root:
        try:
            cmd = [sys.executable, "-m", "rlsbl", "monorepo", "sync"]
            if not auto_commit:
                cmd.append("--no-auto-commit")
            subprocess.run(
                cmd,
                cwd=ws_root,
                check=False,
            )
        except Exception as e:
            from ..utils import warn_exception
            warn_exception("monorepo sync after scaffold failed", e)


def run_cmd(registry, args, flags, ctx):
    """Init command handler.

    Scaffolds release infrastructure (CI, publish workflows, changelog, etc.)
    from templates.
    """
    project_root = ctx.project_root if ctx else None

    # Workspace roots are not packages -- skip all per-package scaffold
    if _is_workspace_root(project_root):
        print("Skipping scaffold at workspace root (use rlsbl monorepo sync instead)")
        return

    reg = TARGETS[registry]

    # Check that a project file exists
    if not reg.check_project_exists("."):
        print(f"Error: no {registry} project found in current directory.", file=sys.stderr)
        print(reg.get_project_init_hint(), file=sys.stderr)
        sys.exit(1)

    # Check for npm lockfile
    npm_lockfile_missing = False
    if registry == "npm":
        npm_lockfile_missing = _check_npm_lockfile_missing()

    dry_run = flags.get("dry-run", False)

    # Acquire advisory lock to prevent concurrent rlsbl operations
    acquire_lock(project_root=project_root)

    try:
        # Register this target in .rlsbl/config.json targets array
        # (skipped under --dry-run -- we don't write config)
        if not dry_run:
            _ensure_target_in_config(registry, ctx=ctx)

        # Determine if this is a private repository
        private = _resolve_private(flags, ctx=ctx)
        if not dry_run:
            ctx.config = write_project_config("private", private, project_root)

        # Generate default pipeline config if not present
        if not dry_run and not private:
            _ensure_pipeline_config([registry], ctx)

        # Gather template variables
        vars_dict = reg.template_vars(".", ctx)
        from datetime import datetime
        vars_dict["year"] = str(datetime.now().year)

        # Publish gate: publish workflows wait for this repo's CI check
        # runs on the release commit. The filter covers every scaffolded
        # target's CI job names.
        from ..publish_gate import ci_check_regex_for_targets, gate_job_template_snippet
        gate_targets = list((ctx.config or {}).get("targets") or [])
        if registry not in gate_targets:
            gate_targets.append(registry)
        vars_dict["publishGate"] = gate_job_template_snippet(
            ci_check_regex_for_targets(gate_targets)
        )

        force = flags.get("force", False)

        # Process registry-specific templates (CI only, no publish).
        # Workspace roots skip CI templates -- the ci-router handles
        # per-package CI, and root-level import checks would fail.
        is_ws_root = _is_workspace_root(project_root)
        reg_plans = []
        if not is_ws_root:
            reg_mappings = reg.template_mappings(ctx)

            reg_plans = plan_mappings(
                reg.template_dir(), reg_mappings, vars_dict, force,
                required_vars={"name", "registryUrl"},
            )

        # Process pipeline publish templates (skip for private repos and workspace roots)
        pipeline_plans = []
        if not private and not is_ws_root:
            pipelines = load_pipelines(ctx.config)
            for pipeline in pipelines.values():
                p_mappings = pipeline.template_mappings(ctx)
                p_dir = pipeline.template_dir()
                if p_mappings and p_dir:
                    pipeline_plans.extend(plan_mappings(
                        p_dir, p_mappings, vars_dict, force,
                    ))

        shared_plans = []
        if not flags.get("skip-shared"):
            shared_mappings = reg.shared_template_mappings(ctx)
            shared_mappings = _append_deploy_workflow_if_configured(shared_mappings, ctx.config)
            shared_mappings = _append_release_dispatch_if_configured(shared_mappings, ctx.config)

            # Non-releasable projects and releasable members skip per-package
            # changelog infrastructure. Non-releasable projects have no
            # changelog at all; releasable members have changelog at the
            # releasable level (`.rlsbl-monorepo/releasables/{name}/changes/`).
            if _is_non_releasable_project(project_root) or _is_releasable_member_project(project_root):
                shared_mappings = [
                    m for m in shared_mappings
                    if m["target"] not in ("CHANGELOG.md", ".rlsbl/changes/unreleased.jsonl")
                ]

            shared_plans = plan_mappings(
                reg.shared_template_dir(), shared_mappings, vars_dict, force,
            )

        if dry_run:
            _print_dry_run_report([reg_plans, pipeline_plans, shared_plans],
                                   registry=registry, registries=[registry])
            return

        reg_created, reg_skipped, reg_warnings, reg_hashes = apply_plans(reg_plans)
        pipe_created, pipe_skipped, pipe_warnings, pipe_hashes = apply_plans(pipeline_plans)
        shared_created, shared_skipped, shared_warnings, shared_hashes = apply_plans(shared_plans)

        created = reg_created + pipe_created + shared_created
        skipped = reg_skipped + pipe_skipped + shared_skipped
        warnings = reg_warnings + pipe_warnings + shared_warnings

        # Note when a Go project's main packages live under cmd/ only:
        # `go install module@latest` targets the module root, so users
        # install with the package-qualified path instead.
        if registry == "go":
            from ..go_introspect import list_main_packages
            go_mains = list_main_packages(".")
            if go_mains and not any(p.rel_dir == "." for p in go_mains):
                pkg_paths = ", ".join(
                    f"'go install {p.import_path}@latest'" for p in go_mains
                )
                print(
                    "Note: Go project's main package(s) live under cmd/, not "
                    "at the module root. 'go install <module>@latest' won't "
                    f"work; users install with {pkg_paths}.",
                    file=sys.stderr,
                )

        # Remove per-package config.json that duplicates releasable config
        _skip_redundant_releasable_configs(project_root, warnings)

        _finalize_scaffold(
            [reg_hashes, pipe_hashes, shared_hashes],
            created, skipped, warnings, registry=registry,
            flags=flags, registries=[registry],
            npm_lockfile_missing=npm_lockfile_missing,
            target_paths={registry: "."},
            project_root=project_root,
            config=ctx.config,
        )

        if private:
            _print_private_summary()

        # If inside a monorepo, sync root CI workflows
        _trigger_monorepo_sync(auto_commit=flags.get("auto-commit", True))
    finally:
        release_lock()


def _extract_top_level_block(lines, key):
    """Extract a top-level YAML block (e.g., 'permissions:', 'env:') from template lines.

    Returns (block_lines, remaining_lines) where block_lines are the key + its
    indented children, and remaining_lines are everything else.
    """
    block = []
    remaining = []
    in_block = False
    for line in lines:
        if not in_block:
            stripped = line.rstrip()
            if stripped == f"{key}:" or stripped.startswith(f"{key}: "):
                in_block = True
                block.append(line)
                continue
            remaining.append(line)
        else:
            # Still in block if line is blank or starts with whitespace
            stripped = line.rstrip()
            if stripped == "" or line[0] in (" ", "\t"):
                block.append(line)
            else:
                in_block = False
                remaining.append(line)
    return block, remaining


def _parse_permissions(block_lines):
    """Parse permission key-value pairs from a permissions block.

    Returns a dict like {"contents": "write", "id-token": "write"}.
    """
    perms = {}
    for line in block_lines:
        stripped = line.strip()
        if stripped.startswith("permissions") or not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            perms[k.strip()] = v.strip()
    return perms


def _parse_env(block_lines):
    """Parse env key-value pairs from an env block.

    Returns a list of (key, full_line) tuples to preserve formatting.
    Keys are used for deduplication; full lines are used for output.
    """
    entries = []
    for line in block_lines:
        stripped = line.strip()
        if stripped.startswith("env") or not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped:
            k = stripped.split(":", 1)[0].strip()
            entries.append((k, line))
    return entries


def _merge_permissions(perm_dicts):
    """Merge multiple permission dicts, choosing the most permissive value for each key.

    Permission escalation order: read < write.
    """
    merged = {}
    order = {"read": 0, "write": 1}
    for d in perm_dicts:
        for k, v in d.items():
            if k not in merged:
                merged[k] = v
            else:
                # Pick the more permissive value
                if order.get(v, 0) > order.get(merged[k], 0):
                    merged[k] = v
    return merged


def _parse_on_triggers(block_lines):
    """Parse an ``on:`` block into a dict of trigger names to sub-block lines.

    Each trigger key maps to a list of its indented continuation lines (if any).
    Triggers without sub-keys (e.g. ``workflow_dispatch:``) map to an empty list.

    Returns a dict like::

        {"release": ["    types: [published]\\n"], "workflow_dispatch": []}
    """
    triggers = {}
    current_trigger = None
    for line in block_lines:
        stripped = line.rstrip()
        # Skip the ``on:`` header line, blank lines before first trigger, and comments
        if stripped == "on:" or stripped.startswith("on: "):
            continue
        if not stripped or stripped.startswith("#"):
            # Blank or comment lines inside a trigger's sub-block
            if current_trigger is not None:
                triggers[current_trigger].append(line)
            continue
        # A line at exactly 2-space indent is a trigger key
        if line.startswith("  ") and not line.startswith("    "):
            key = stripped.rstrip(":").strip()
            current_trigger = key
            triggers[key] = []
        elif current_trigger is not None:
            # Deeper-indented continuation line belongs to the current trigger
            triggers[current_trigger].append(line)
    return triggers


def _merge_on_triggers(trigger_dicts):
    """Merge multiple parsed ``on:`` trigger dicts into a single dict.

    Unions all trigger keys. For triggers with sub-blocks, the first non-empty
    sub-block wins (all templates currently have identical sub-blocks).
    Ensures ``workflow_dispatch`` is always present.
    """
    merged = {}
    for d in trigger_dicts:
        for key, sub_lines in d.items():
            if key not in merged:
                merged[key] = sub_lines
            else:
                # Keep the first non-empty sub-block
                if not merged[key] and sub_lines:
                    merged[key] = sub_lines
    # Guarantee workflow_dispatch is present
    if "workflow_dispatch" not in merged:
        merged["workflow_dispatch"] = []
    return merged


def _extract_jobs_section(lines):
    """Extract the content under the 'jobs:' key from template lines.

    Returns lines starting from the first job definition (the indented content
    after 'jobs:'), not including the 'jobs:' line itself.
    """
    in_jobs = False
    job_lines = []
    for line in lines:
        stripped = line.rstrip()
        if not in_jobs:
            if stripped == "jobs:":
                in_jobs = True
                continue
        else:
            job_lines.append(line)
    return job_lines


def _generate_merged_publish(targets, template_vars, target_paths=None):
    """Generate a merged publish.yml from individual target publish templates.

    Reads each target's publish.yml.tpl, renders template variables, parses
    as structured YAML, and merges on-triggers, permissions, env, and jobs
    into a single workflow dict.

    When *target_paths* is provided (a dict mapping target name to its
    directory path), subdirectory targets get:
    - ``defaults.run.working-directory`` injected into their jobs
    - ``packages-dir`` rewritten for PyPI publish actions
    - version-file inputs prefixed for setup actions
    """
    from io import StringIO

    from ruamel.yaml import YAML

    from ..publish_gate import (
        GATE_JOB_KEY,
        build_gate_job,
        ci_check_regex_for_targets,
        publish_concurrency_block,
    )

    if target_paths is None:
        target_paths = {}

    templates_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
    # Use round-trip loader to preserve flow-style sequences like
    # ``types: [published]`` (safe loader loses flow vs block info).
    yaml_loader = YAML(typ="rt")

    all_on_triggers = {}
    all_permissions = []
    merged_env = {}
    merged_jobs = {}

    for target_name in targets:
        tpl_path = os.path.join(templates_root, target_name, "publish.yml.tpl")
        if not os.path.exists(tpl_path):
            continue

        with open(tpl_path, "r", encoding="utf-8") as f:
            raw = f.read()

        # Build per-target vars: start with the full merged dict, then overlay
        # this target's own vars un-namespaced so {{registryUrl}} etc. resolve
        # even when this target is not the primary.
        per_target_vars = dict(template_vars)
        _overlay_target_vars(per_target_vars, target_name)

        # Process template variables, then parse as structured YAML.
        # Unresolved {{var}} placeholders are not valid YAML (parsed as flow
        # mappings).  Whole-line placeholders (block insertions like
        # {{homebrewEnv}}) are dropped entirely.  Inline placeholders are
        # sheltered as __UNRESOLVED__var__ strings before loading and
        # restored after serialization.
        content, _ = process_template(raw, per_target_vars, template_path=tpl_path)
        # Drop whole-line unresolved placeholders (block insertions)
        content = re.sub(r"^[ \t]*\{\{\w+(?:\.\w+)*\}\}\s*$", "", content, flags=re.MULTILINE)
        # Shelter remaining inline unresolved placeholders.
        # Dots in names (e.g. zig.projectName) are encoded as _DOT_ so the
        # sentinel is a single \w+ token that survives YAML round-tripping.
        def _shelter(m):
            return "__UNRESOLVED__" + m.group(1).replace(".", "_DOT_") + "__"
        content = re.sub(r"\{\{(\w+(?:\.\w+)*)\}\}", _shelter, content)
        data = yaml_loader.load(content)
        if not isinstance(data, dict):
            continue

        # Collect on-triggers (union, first non-empty sub-block wins)
        on_block = data.get("on")
        if isinstance(on_block, dict):
            for trigger_key, trigger_val in on_block.items():
                if trigger_key not in all_on_triggers:
                    all_on_triggers[trigger_key] = trigger_val
                elif all_on_triggers[trigger_key] is None and trigger_val is not None:
                    all_on_triggers[trigger_key] = trigger_val

        # Collect workflow-level permissions
        perms = data.get("permissions")
        if isinstance(perms, dict):
            all_permissions.append(perms)

        # Collect workflow-level env (first occurrence of each key wins)
        env = data.get("env")
        if isinstance(env, dict):
            for k, v in env.items():
                if k not in merged_env:
                    merged_env[k] = v

        # Extract and transform jobs
        jobs = data.get("jobs")
        if not isinstance(jobs, dict):
            continue

        target_path = target_paths.get(target_name, ".")

        # Inject working-directory for subdirectory targets
        if target_path != ".":
            for job in jobs.values():
                defaults = job.get("defaults") or {}
                run_block = defaults.get("run") or {}
                run_block["working-directory"] = target_path
                defaults["run"] = run_block
                job["defaults"] = defaults

        # Rewrite action paths for subdirectory targets
        if target_path != ".":
            _rewrite_action_paths_for_jobs(jobs, target_path)

        # Drop per-target gate jobs: the merged workflow gets exactly ONE
        # gate (inserted below) covering all targets' CI checks.
        jobs.pop(GATE_JOB_KEY, None)

        # Rename job keys to target name for uniqueness.
        # Single-job templates get the target name as key (e.g. "npm").
        # Multi-job templates (e.g. Go with npmPublishJobs) get the first
        # job as target name, additional jobs as "{target}-{original_key}".
        # needs: references follow the rename; "gate" always points at the
        # single merged gate and is never renamed.
        job_keys = list(jobs)
        key_map = {}
        for i, original_key in enumerate(job_keys):
            if i == 0:
                key_map[original_key] = target_name
            else:
                key_map[original_key] = f"{target_name}-{original_key}"

        def _map_need(need):
            if need == GATE_JOB_KEY:
                return GATE_JOB_KEY
            return key_map.get(need, need)

        for original_key in job_keys:
            job = jobs.pop(original_key)
            needs = job.get("needs")
            if isinstance(needs, str):
                needs = [_map_need(needs)]
            elif isinstance(needs, list):
                needs = [_map_need(n) for n in needs]
            else:
                needs = []
            # Every publish job waits for the gate.
            if GATE_JOB_KEY not in needs:
                needs.insert(0, GATE_JOB_KEY)
            job["needs"] = needs[0] if len(needs) == 1 else needs
            merged_jobs[key_map[original_key]] = job

    # Guarantee workflow_dispatch is present
    if "workflow_dispatch" not in all_on_triggers:
        all_on_triggers["workflow_dispatch"] = None

    # Merge permissions (most permissive value per key)
    merged_perms = _merge_permissions(all_permissions)

    # Compose the final workflow dict
    workflow = {"name": "Publish"}
    workflow["on"] = all_on_triggers
    # Per-ref publish concurrency: a dispatch retry at the same tag queues
    # behind the in-flight run; a publish is never cancelled mid-flight.
    workflow["concurrency"] = publish_concurrency_block()
    if merged_perms:
        workflow["permissions"] = dict(sorted(merged_perms.items()))
    if merged_env:
        workflow["env"] = merged_env
    # Single gate for all targets: waits for every target's CI check runs
    # on the release commit before any publish job starts.
    workflow["jobs"] = {
        GATE_JOB_KEY: build_gate_job(
            check_regex=ci_check_regex_for_targets(list(targets))
        ),
        **merged_jobs,
    }

    # Serialize with ruamel.yaml
    yml = YAML()
    yml.default_flow_style = False
    yml.indent(mapping=2, sequence=4, offset=2)

    # Use flow style for short lists (e.g., types: [published])
    def _str_representer(representer, data):
        if "\n" in data:
            return representer.represent_scalar(
                "tag:yaml.org,2002:str", data, style="|"
            )
        return representer.represent_scalar("tag:yaml.org,2002:str", data)

    yml.representer.add_representer(str, _str_representer)

    stream = StringIO()
    yml.dump(workflow, stream)
    result = stream.getvalue()
    # Restore sheltered template placeholders (decode _DOT_ back to .)
    def _unshelter(m):
        return "{{" + m.group(1).replace("_DOT_", ".") + "}}"
    result = re.sub(r"__UNRESOLVED__(.+?)__", _unshelter, result)
    result = result.rstrip("\n") + "\n"
    return result


# Action inputs that contain file paths and need subdirectory prefixing
_SETUP_VERSION_FILE_KEYS = {
    "actions/setup-go": "go-version-file",
    "actions/setup-python": "python-version-file",
    "actions/setup-node": "node-version-file",
}


def _rewrite_action_paths_for_jobs(jobs, project_path):
    """Rewrite action inputs with file paths so they are relative to *project_path*.

    Modifies *jobs* in place. Handles:
    - ``pypa/gh-action-pypi-publish``: sets ``with.packages-dir``
    - ``actions/setup-{go,python,node}``: prefixes version-file paths
    """
    for job in jobs.values():
        for step in job.get("steps", []):
            uses = step.get("uses", "")

            # PyPI publish action
            if "pypa/gh-action-pypi-publish" in uses:
                with_block = step.setdefault("with", {})
                with_block["packages-dir"] = f"{project_path}/dist/"

            # Setup actions with version-file inputs
            for action_substring, version_key in _SETUP_VERSION_FILE_KEYS.items():
                if action_substring in uses:
                    with_block = step.get("with", {})
                    if version_key in with_block:
                        val = with_block[version_key]
                        if isinstance(val, str) and not val.startswith(
                            f"{project_path}/"
                        ):
                            with_block[version_key] = f"{project_path}/{val}"


def _overlay_target_vars(merged_vars, target_name):
    """Promote namespaced ``{target_name}.{key}`` entries to bare ``{key}``.

    Scans *merged_vars* for keys starting with ``{target_name}.`` and adds
    bare versions so templates like ``{{registryUrl}}`` resolve even when
    the target is not the primary.  Does not overwrite existing bare keys.
    """
    prefix = f"{target_name}."
    for key, value in list(merged_vars.items()):
        if key.startswith(prefix):
            bare = key[len(prefix):]
            merged_vars[bare] = value


def _merge_template_vars(registries_list, primary, target_paths, ctx):
    """Build a merged template vars dict with namespaced keys from all targets.

    The primary target's vars are included un-namespaced (as the base).
    Non-primary targets contribute only their namespaced keys (keys
    containing a dot), so they do not overwrite the primary's bare keys.

    TemplateVars auto-generates ``{target_name}.{key}`` entries, so no
    manual namespacing loop is needed.

    target_paths is a dict mapping target name to its directory path.
    """
    merged = {}
    # Primary target's vars as base (un-namespaced + namespaced)
    primary_target = TARGETS[primary]
    primary_vars = primary_target.template_vars(target_paths.get(primary, "."), ctx)
    merged.update(primary_vars)
    # Non-primary targets: add only namespaced keys (preserve primary's bare keys)
    for target_name in registries_list:
        if target_name == primary:
            continue
        target = TARGETS[target_name]
        target_vars = target.template_vars(target_paths.get(target_name, "."), ctx)
        for key, value in target_vars.items():
            if "." in key:
                merged[key] = value
    return merged


def _plan_merged_publish(publish_target, merged_content, force):
    """Compute a plan for the merged publish workflow (analysis only)."""
    is_overwrite = os.path.exists(publish_target)
    if not is_overwrite or force:
        status = "overwritten" if is_overwrite else "created"
        return {
            "target": publish_target,
            "status": status,
            "bucket": "created",
            "action": "write",
            "content": merged_content,
            "base_content": merged_content,
        }
    with open(publish_target, "r", encoding="utf-8") as f:
        ours = f.read()
    base = _load_base(publish_target)
    if base is None:
        if ours == merged_content:
            return {
                "target": publish_target,
                "status": "unchanged, base seeded",
                "bucket": "skipped",
                "action": "save_base_only",
                "base_content": merged_content,
            }
        return {
            "target": publish_target,
            "status": "no base -- run scaffold --force to enable merging",
            "bucket": "skipped",
            "action": "save_base_only",
            "base_content": merged_content,
            "warning": (
                f"{publish_target}: no base stored, cannot merge; "
                "run scaffold --force to reset"
            ),
        }
    if ours == base:
        return {
            "target": publish_target,
            "status": "updated",
            "bucket": "created",
            "action": "write",
            "content": merged_content,
            "base_content": merged_content,
        }
    if base == merged_content or ours == merged_content:
        return {
            "target": publish_target,
            "status": "unchanged",
            "bucket": "skipped",
            "action": "none",
        }
    merged_text, has_conflicts = _three_way_merge(ours, base, merged_content)
    if has_conflicts:
        return {
            "target": publish_target,
            "status": "CONFLICTS -- resolve manually",
            "bucket": "created",
            "action": "write",
            "content": merged_text,
            "base_content": merged_content,
            "warning": f"{publish_target}: merge conflicts detected, resolve manually",
        }
    return {
        "target": publish_target,
        "status": "merged",
        "bucket": "created",
        "action": "write",
        "content": merged_text,
        "base_content": merged_content,
    }


def run_cmd_multi(registries_list, args, flags, ctx):
    """Scaffold for multiple registries with per-target CI and merged publish.

    Generates per-target CI workflows (ci-{target}.yml) and a merged
    publish.yml that contains jobs for all detected registries.
    """
    project_root = ctx.project_root if ctx else None

    # Workspace roots are not packages -- skip all per-package scaffold
    if _is_workspace_root(project_root):
        print("Skipping scaffold at workspace root (use rlsbl monorepo sync instead)")
        return

    primary = registries_list[0]
    reg = TARGETS[primary]

    if not reg.check_project_exists("."):
        print(f"Error: no {primary} project found in current directory.", file=sys.stderr)
        sys.exit(1)

    # Build per-target path mapping early (read-only, no lock needed)
    target_entries = detect_targets(".")
    target_paths = {entry.name: entry.path for entry in target_entries}

    # Check for npm lockfile using the detected npm target path
    npm_lockfile_missing = False
    if "npm" in registries_list:
        npm_lockfile_missing = _check_npm_lockfile_missing(target_paths.get("npm", "."))

    dry_run = flags.get("dry-run", False)

    # Acquire advisory lock to prevent concurrent rlsbl operations
    acquire_lock(project_root=project_root)

    try:
        # Register all targets in .rlsbl/config.json targets array
        # (skipped under --dry-run -- we don't write config)
        if not dry_run:
            for r in registries_list:
                _ensure_target_in_config(r, ctx=ctx)

        # Determine if this is a private repository
        private = _resolve_private(flags, ctx=ctx)
        if not dry_run:
            ctx.config = write_project_config("private", private, project_root)

        # Generate default pipeline config if not present
        if not dry_run and not private:
            _ensure_pipeline_config(registries_list, ctx)

        print(f"Multiple registries detected: {', '.join(registries_list)}")
        if private:
            print("Scaffolding for private repository (no publish workflow).")
        else:
            print("Scaffolding with merged publish workflow.")
        vars_dict = _merge_template_vars(registries_list, primary, target_paths, ctx)
        from datetime import datetime
        vars_dict["year"] = str(datetime.now().year)

        force = flags.get("force", False)

        # Process per-target CI templates: each target gets its own ci-{name}.yml.
        # Workspace roots skip CI templates -- the ci-router handles
        # per-package CI, and root-level import checks would fail.
        is_ws_root = _is_workspace_root(project_root)
        _wf_prefix = os.path.join(".github", "workflows", "")
        _ci_prefix = os.path.join(".github", "workflows", "ci")
        ci_plans = []
        seen_targets = set()
        extra_plans = []
        if not is_ws_root:
            for r in registries_list:
                target_obj = TARGETS[r]
                all_mappings = target_obj.template_mappings(ctx)

                # Split into CI mappings and non-workflow mappings
                ci_mappings = []
                non_wf_mappings = []
                for m in all_mappings:
                    if m["target"].startswith(_ci_prefix):
                        ci_mappings.append(m)
                    elif not m["target"].startswith(_wf_prefix):
                        non_wf_mappings.append(m)

                # Skip npm CI for wrapper packages (no test script)
                if r == "npm" and ci_mappings:
                    npm_dir = target_paths.get("npm", ".")
                    if _is_npm_wrapper(npm_dir):
                        print("Skipping npm CI (no test script in package.json)")
                        ci_mappings = []

                # Rewrite CI target filenames: ci.yml -> ci-{target}.yml
                for m in ci_mappings:
                    original = m["target"]
                    dirname = os.path.dirname(original)
                    basename = os.path.basename(original)
                    new_basename = basename.replace("ci.yml", f"ci-{r}.yml")
                    m["target"] = os.path.join(dirname, new_basename)

                if ci_mappings:
                    # Build per-target vars: overlay this target's namespaced vars
                    # un-namespaced so {{importName}} etc. resolve even when this
                    # target is not the primary.
                    ci_vars = dict(vars_dict)
                    _overlay_target_vars(ci_vars, r)
                    new_plans = plan_mappings(
                        target_obj.template_dir(), ci_mappings, ci_vars, force,
                    )

                    # Inject working-directory for subdirectory targets
                    target_path = target_paths.get(r, ".")
                    if target_path != ".":
                        for plan in new_plans:
                            if plan.get("content"):
                                doc = parse_ci_workflow(plan["content"])
                                if doc is not None:
                                    inject_working_directory(doc, target_path)
                                    rewrite_version_file_inputs(doc, target_path.rstrip("/"))
                                    plan["content"] = emit_ci_workflow(doc)
                                    if plan.get("base_content") is not None:
                                        base_doc = parse_ci_workflow(plan["base_content"])
                                        if base_doc is not None:
                                            inject_working_directory(base_doc, target_path)
                                            rewrite_version_file_inputs(base_doc, target_path.rstrip("/"))
                                            plan["base_content"] = emit_ci_workflow(base_doc)

                    ci_plans.extend(new_plans)

                # Collect non-workflow files (deduplicated)
                for m in non_wf_mappings:
                    if m["target"] not in seen_targets:
                        seen_targets.add(m["target"])
                        extra_plans.extend(plan_mappings(
                            target_obj.template_dir(), [m], vars_dict, force,
                        ))

        # Plan the merged publish workflow (skip for private repos and workspace roots)
        # Read publish templates from pipeline types instead of targets
        merged_plans = []
        if not private and not is_ws_root:
            publish_target = os.path.join(".github", "workflows", "publish.yml")
            merged_content = _generate_merged_publish(registries_list, vars_dict, target_paths)
            merged_plans = [_plan_merged_publish(
                publish_target, merged_content, force,
            )]

        # Plan shared templates (once)
        shared_mappings = reg.shared_template_mappings(ctx)
        shared_mappings = _append_deploy_workflow_if_configured(shared_mappings, ctx.config)
        shared_mappings = _append_release_dispatch_if_configured(shared_mappings, ctx.config)

        # Non-releasable projects and releasable members skip per-package
        # changelog infrastructure (see comment in run_cmd for rationale).
        if _is_non_releasable_project(project_root) or _is_releasable_member_project(project_root):
            shared_mappings = [
                m for m in shared_mappings
                if m["target"] not in ("CHANGELOG.md", ".rlsbl/changes/unreleased.jsonl")
            ]

        shared_plans = plan_mappings(
            reg.shared_template_dir(), shared_mappings, vars_dict, force,
        )

        if dry_run:
            _print_dry_run_report(
                [ci_plans, extra_plans, merged_plans, shared_plans],
                registries=registries_list,
            )
            return

        ci_created, ci_skipped, ci_warnings, ci_hashes = apply_plans(ci_plans)
        extra_created, extra_skipped, extra_warnings, extra_hashes = apply_plans(extra_plans)
        merged_created, merged_skipped, merged_warnings, merged_hashes = apply_plans(merged_plans)
        shared_created, shared_skipped, shared_warnings, shared_hashes = apply_plans(shared_plans)

        created = ci_created + extra_created + merged_created + shared_created
        skipped = ci_skipped + extra_skipped + merged_skipped + shared_skipped
        warnings = ci_warnings + extra_warnings + merged_warnings + shared_warnings

        # Remove per-package config.json that duplicates releasable config
        _skip_redundant_releasable_configs(project_root, warnings)

        _finalize_scaffold(
            [ci_hashes, extra_hashes, merged_hashes, shared_hashes],
            created, skipped, warnings,
            flags=flags, registries=registries_list,
            target_paths=target_paths,
            project_root=project_root,
            config=ctx.config,
        )

        if private:
            _print_private_summary()
        else:
            # Show combined next steps for dual-registry
            steps = [
                "Add an NPM_TOKEN secret to your GitHub repo (Settings > Secrets > Actions)",
                "Configure Trusted Publishing on pypi.org",
                "Push to GitHub to activate the CI workflow",
                "Run rlsbl release [patch|minor|major]",
            ]
            if npm_lockfile_missing:
                steps.insert(0, 'Run "npm install" and commit package-lock.json before pushing')
            print("\nNext steps:")
            for i, step in enumerate(steps, 1):
                print(f"  {i}. {step}")

        # If inside a monorepo, sync root CI workflows
        _trigger_monorepo_sync(auto_commit=flags.get("auto-commit", True))
    finally:
        release_lock()
