"""Init command that scaffolds release infrastructure from templates, creating CI workflows, hooks, changelog, and config files."""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

from ..action_versions import format_action, UnknownActionError
from ..config import read_deploy_config, should_tag, read_project_config, write_project_config
from ..lock import acquire_lock, release_lock
from ..targets import TARGETS, detect_targets
from ..tagging import ensure_tags
from ..utils import commit_files, is_private_repo

HASHES_FILE = os.path.join(".rlsbl", "hashes.json")
BASES_DIR = os.path.join(".rlsbl", "bases")

_NPM_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")


def _check_npm_lockfile_missing():
    """Check if any npm lockfile exists from cwd up to the git root.

    Returns True if no lockfile is found (i.e., lockfile is missing).
    Prints a warning to stderr when missing.
    """
    current = os.path.abspath(".")
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

# Files owned by the user after initial scaffold -- never overwrite or merge
USER_OWNED = {
    "CHANGELOG.md",
    "LICENSE",
    ".rlsbl/hooks/pre-checks.sh",
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


def load_hashes():
    """Load stored file hashes from .rlsbl/hashes.json."""
    if os.path.exists(HASHES_FILE):
        with open(HASHES_FILE) as f:
            return json.load(f)
    return {}


def save_hashes(hashes):
    """Write file hashes to .rlsbl/hashes.json."""
    os.makedirs(os.path.dirname(HASHES_FILE), exist_ok=True)
    with open(HASHES_FILE, "w") as f:
        json.dump(hashes, f, indent=2)
        f.write("\n")


def _ensure_target_in_config(registry_name):
    """Add registry_name to the targets array in .rlsbl/config.json if not already present."""
    config = read_project_config()
    targets = config.get("targets", [])
    if not isinstance(targets, list):
        targets = []
    # Check if name is already in targets (as string or dict)
    existing_names = []
    for t in targets:
        if isinstance(t, str):
            existing_names.append(t)
        elif isinstance(t, dict):
            existing_names.append(t.get("name", ""))
    if registry_name not in existing_names:
        targets.append(registry_name)
    write_project_config("targets", targets)


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


def process_template(template_content, vars_dict, template_path=None):
    """Process a template string with a two-pass substitution.

    Pass 1 resolves ``{{action "owner/name"}}`` placeholders against the
    central action-version table (rlsbl/data/action_versions.toml). An
    unknown action raises :class:`UnknownActionError` immediately -- no
    implicit defaults.

    Pass 2 resolves the existing ``{{varName}}`` (and dotted ``{{a.b}}``)
    placeholders against ``vars_dict``.

    Returns ``(content, unreplaced)`` where ``unreplaced`` is the list of
    variable names in pass 2 that had no entry in ``vars_dict``. Pass 1
    misses raise instead of being collected.
    """

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
        r'\{\{action\s+"([^"]+)"\}\}', action_replacer, template_content
    )

    # Pass 2: variable placeholders (existing behavior).
    unreplaced = []

    def replacer(match):
        var_name = match.group(1)
        if var_name in vars_dict:
            return vars_dict[var_name]
        unreplaced.append(var_name)
        return match.group(0)

    content = re.sub(r"\{\{(\w+(?:\.\w+)*)\}\}", replacer, content)
    return content, unreplaced


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


def plan_mappings(template_dir, mappings, vars_dict, force, update=False):
    """Compute what process_mappings would do, without writing anything.

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
        theirs, unreplaced = process_template(raw, vars_dict, template_path=template_path)

        # --- User-owned files: never overwrite (even with --force),
        # except LICENSE gets its copyright year updated on --update.
        if os.path.exists(target) and target in USER_OWNED:
            if update and target == "LICENSE":
                from datetime import datetime
                current_year = str(datetime.now().year)
                with open(target, "r", encoding="utf-8") as f:
                    content = f.read()
                # Match "Copyright (c) YYYY" or "Copyright (c) YYYY-YYYY"
                old_year = None
                def _capture_range(m):
                    nonlocal old_year
                    if m.group(2) == current_year:
                        return m.group(0)
                    old_year = f"{m.group(1).split()[-1]}-{m.group(2)}"
                    return f"{m.group(1)}-{current_year}"
                updated_content = re.sub(
                    r"(Copyright\s+\(c\)\s+\d{4})-(\d{4})",
                    _capture_range,
                    content,
                )
                if updated_content == content:
                    def _capture_single(m):
                        nonlocal old_year
                        if m.group(2) == current_year:
                            return m.group(0)
                        old_year = m.group(2)
                        return f"{m.group(1)}{m.group(2)}-{current_year}"
                    updated_content = re.sub(
                        r"(Copyright\s+\(c\)\s+)(\d{4})(?![-\d])",
                        _capture_single,
                        content,
                    )
                if updated_content != content:
                    year_detail = (
                        f"year updated ({old_year} -> {old_year.split('-')[0]}-{current_year})"
                        if old_year and "-" in old_year
                        else f"year updated ({old_year} -> {old_year}-{current_year})"
                    ) if old_year else "year updated"
                    plans.append({
                        "target": "LICENSE",
                        "status": year_detail,
                        "bucket": "created",
                        "action": "write_no_base",
                        "content": updated_content,
                    })
                else:
                    plans.append({
                        "target": target,
                        "status": "user-owned",
                        "bucket": "skipped",
                        "action": "none",
                    })
            else:
                plans.append({
                    "target": target,
                    "status": "user-owned",
                    "bucket": "skipped",
                    "action": "none",
                })
            continue

        # --- .gitignore: additive set-union merge (append new lines, never remove) ---
        if target == ".gitignore" and os.path.exists(target) and not force:
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
            continue

        if action == "save_base_only":
            _save_base(target, plan["base_content"])
            if plan["bucket"] == "skipped":
                skipped.append((target, plan["status"]))
            else:
                created.append((target, plan["status"]))
            if plan.get("warning"):
                warnings.append(plan["warning"])
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
            if plan.get("unreplaced"):
                warnings.append(
                    f"{target}: unreplaced vars: {', '.join(plan['unreplaced'])}"
                )
            continue

    return created, skipped, warnings, new_hashes


def process_mappings(template_dir, mappings, vars_dict, force, update=False,
                     existing_hashes=None):
    """Process a list of template mappings: read each template, apply vars, write target files.

    Uses a universal three-way merge (via git merge-file) for existing files:
    base (last scaffolded version) + ours (user's current file) + theirs (new template).
    USER_OWNED files are never overwritten or merged (except LICENSE year update).

    Returns (created, skipped, warnings, new_hashes).
    created/skipped are lists of (target, status) tuples for unified display.

    Implemented as plan_mappings() (pure analysis) + apply_plans() (side effects).
    """
    plans = plan_mappings(template_dir, mappings, vars_dict, force, update=update)
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
            if plan.get("unreplaced"):
                warnings.append(
                    f"{plan['target']}: unreplaced vars: {', '.join(plan['unreplaced'])}"
                )

    _print_file_status_table(created, skipped)

    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  {w}")

    print()
    print("DRY RUN -- no files were written, no commits made.")


def _install_or_update_pre_push_hook():
    """Install the rlsbl pre-push hook, upgrading older versions in place.

    See rlsbl/hook_hashes.py for the historical hash set.

    Behavior:
      - .git missing                -> no-op
      - hook missing                -> write current template, chmod 755
      - hook matches current hash   -> no-op (already up to date)
      - hook matches old known hash -> overwrite, print upgrade notice
      - hook hash unknown           -> skip, print warning + unified diff
    """
    import difflib
    from ..hook_hashes import (
        CURRENT_PRE_PUSH_HOOK,
        CURRENT_PRE_PUSH_HOOK_HASH,
        PRE_PUSH_HOOK_HASHES,
        compute_hook_hash,
    )

    if not os.path.isdir(".git"):
        return

    hook_target = os.path.join(".git", "hooks", "pre-push")

    if not os.path.exists(hook_target):
        os.makedirs(os.path.join(".git", "hooks"), exist_ok=True)
        with open(hook_target, "w", encoding="utf-8") as f:
            f.write(CURRENT_PRE_PUSH_HOOK)
        os.chmod(hook_target, 0o755)
        print("Installed pre-push hook (.git/hooks/pre-push)")
        return

    with open(hook_target, "r", encoding="utf-8") as f:
        installed = f.read()
    installed_hash = compute_hook_hash(installed)

    if installed_hash == CURRENT_PRE_PUSH_HOOK_HASH:
        return

    if installed_hash in PRE_PUSH_HOOK_HASHES:
        with open(hook_target, "w", encoding="utf-8") as f:
            f.write(CURRENT_PRE_PUSH_HOOK)
        os.chmod(hook_target, 0o755)
        print("Updated pre-push hook (was an older rlsbl version).")
        return

    # Unknown content -- assume user-customized. Show a diff so the user can
    # decide whether to delete the hook and re-scaffold to accept ours.
    diff_lines = list(difflib.unified_diff(
        installed.splitlines(keepends=True),
        CURRENT_PRE_PUSH_HOOK.splitlines(keepends=True),
        fromfile=hook_target,
        tofile="rlsbl template",
    ))
    diff_text = "".join(diff_lines)
    print(
        "pre-push hook appears customized -- not overwriting. Diff:\n"
        f"{diff_text}"
        "  To accept the rlsbl template, delete the hook and re-run scaffold.",
        file=sys.stderr,
    )


def _finalize_scaffold(existing_hashes, all_hash_dicts, created, skipped, warnings,
                       registry=None, flags=None, registries=None,
                       npm_lockfile_missing=False):
    """Shared post-processing for scaffold: chmod, hooks, version marker, hashes, tagging, summary.

    all_hash_dicts is a list of dicts to merge into existing_hashes.
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

    # Write scaffolding version marker so the pre-push hook can detect drift
    from rlsbl import __version__
    marker_dir = os.path.join(".", ".rlsbl")
    os.makedirs(marker_dir, exist_ok=True)
    marker_path = os.path.join(marker_dir, "version")
    with open(marker_path, "w") as f:
        f.write(__version__ + "\n")
    print("Wrote scaffolding version marker (.rlsbl/version)")

    # Persist file hashes for future --update customization detection
    all_new_hashes = {}
    for h in all_hash_dicts:
        all_new_hashes.update(h)
    existing_hashes.update(all_new_hashes)
    save_hashes(existing_hashes)

    # Ecosystem tagging
    if should_tag(flags):
        ensure_tags(registries)

    # Print unified file list with dot-padded status column
    _print_file_status_table(created, skipped)

    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  {w}")

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

    # Auto-commit scaffold changes unless --no-commit is set
    if flags.get("no-commit"):
        print("Skipping commit (--no-commit).")
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
    for rlsbl_file in [HASHES_FILE, os.path.join(".rlsbl", "version"), config_file]:
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
    if not os.path.isdir(".git"):
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
        print(f"Warning: could not untrack gitignored files: {e}")

    if commit_files("rlsbl scaffold", files_to_commit, allow_failure=True):
        print("Committed scaffold changes.")


def _resolve_private(flags):
    """Determine if this is a private repository.

    Checks --private flag first, then saved config, then auto-detects via GitHub API.

    On --update: if ``private`` is missing from config and no --private flag was
    passed, prints an error and exits.  The user must add the key explicitly.

    On new scaffold: auto-detects if needed and returns the result.  The caller
    is responsible for persisting the value to config.json.

    Returns True/False, or False if detection fails (new scaffold only).
    """
    if flags.get("private"):
        return True

    # Check saved config
    config = read_project_config()
    if "private" in config:
        return bool(config["private"])

    # On --update, auto-detect and persist to config
    if flags.get("update"):
        detected = is_private_repo()
        value = detected if detected is not None else False
        write_project_config("private", value)
        label = "private repo" if value else "public repo"
        print(f"Auto-detected private: {str(value).lower()} ({label}). Written to config.json.")
        return value

    # Auto-detect via GitHub API (new scaffold only)
    detected = is_private_repo()
    if detected is not None:
        return detected

    return False


def _filter_mappings_for_private(mappings):
    """Remove publish template mappings (private repos don't publish to registries)."""
    return [m for m in mappings if "publish" not in m["template"]]


def _append_deploy_workflow_if_configured(mappings):
    """Add deploy workflow template to mappings if deploy config exists."""
    deploy_targets, _ = read_deploy_config()
    if deploy_targets:
        mappings = list(mappings)
        mappings.append({
            "template": ".github/workflows/deploy.yml.tpl",
            "target": ".github/workflows/deploy.yml",
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


def _trigger_monorepo_sync(no_commit=False):
    """If the current directory is inside a monorepo workspace, run sync.

    Uses a subprocess so that sys.exit() calls inside sync don't kill scaffold.
    Failures are silently ignored -- sync is best-effort after scaffold.

    When ``no_commit`` is True, propagates ``--no-commit`` to the sync call so
    a single user invocation with ``--no-commit`` produces zero commits.
    """
    from ..workspace import find_workspace_root

    ws_root = find_workspace_root(".")
    if ws_root:
        try:
            cmd = [sys.executable, "-m", "rlsbl", "monorepo", "sync"]
            if no_commit:
                cmd.append("--no-commit")
            subprocess.run(
                cmd,
                cwd=ws_root,
                check=False,
            )
        except Exception:
            pass


def run_cmd(registry, args, flags):
    """Init command handler.

    Scaffolds release infrastructure (CI, publish workflows, changelog, etc.)
    from templates.
    """
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
    acquire_lock()

    try:
        # Register this target in .rlsbl/config.json targets array
        # (skipped under --dry-run -- we don't write config)
        if not dry_run:
            _ensure_target_in_config(registry)

        # Determine if this is a private repository
        private = _resolve_private(flags)
        if not dry_run:
            write_project_config("private", private)

        # Gather template variables
        vars_dict = reg.template_vars(".")
        from datetime import datetime
        vars_dict["year"] = str(datetime.now().year)

        force = flags.get("force", False)
        update = flags.get("update", False)

        existing_hashes = load_hashes()

        # Process registry-specific templates
        reg_mappings = reg.template_mappings()
        if private:
            reg_mappings = _filter_mappings_for_private(reg_mappings)

        reg_plans = plan_mappings(
            reg.template_dir(), reg_mappings, vars_dict, force, update=update,
        )

        shared_plans = []
        if not flags.get("skip-shared"):
            shared_mappings = reg.shared_template_mappings()
            shared_mappings = _append_deploy_workflow_if_configured(shared_mappings)
            shared_plans = plan_mappings(
                reg.shared_template_dir(), shared_mappings, vars_dict, force, update=update,
            )

        if dry_run:
            _print_dry_run_report([reg_plans, shared_plans], registry=registry,
                                   registries=[registry])
            return

        reg_created, reg_skipped, reg_warnings, reg_hashes = apply_plans(reg_plans)
        shared_created, shared_skipped, shared_warnings, shared_hashes = apply_plans(shared_plans)

        created = reg_created + shared_created
        skipped = reg_skipped + shared_skipped
        warnings = reg_warnings + shared_warnings

        # Warn if Go project has main in cmd/ but not at root
        if registry == "go":
            if reg._has_cmd_main(".") and not reg._has_root_main("."):
                print(
                    "Warning: Go project has main package in cmd/ but not at root.\n"
                    "'go install module@latest' won't work. Consider moving main.go "
                    "to the project root.",
                    file=sys.stderr,
                )

        _finalize_scaffold(
            existing_hashes, [reg_hashes, shared_hashes],
            created, skipped, warnings, registry=registry,
            flags=flags, registries=[registry],
            npm_lockfile_missing=npm_lockfile_missing,
        )

        if private:
            _print_private_summary()

        # If inside a monorepo, sync root CI workflows
        _trigger_monorepo_sync(no_commit=bool(flags.get("no-commit")))
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


def _generate_merged_publish(targets, template_vars):
    """Generate a merged publish.yml from individual target publish templates.

    Reads each target's publish.yml.tpl, extracts jobs/permissions/env,
    and composes a single workflow with all jobs merged.
    """
    templates_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")

    all_on_triggers = []
    all_permissions = []
    all_env_entries = []
    all_jobs = []
    seen_env_keys = set()

    for target_name in targets:
        tpl_path = os.path.join(templates_root, target_name, "publish.yml.tpl")
        if not os.path.exists(tpl_path):
            continue

        with open(tpl_path, "r", encoding="utf-8") as f:
            raw = f.read()

        # Build per-target vars: start with the full merged dict, then overlay
        # this target's own vars un-namespaced so {{registryUrl}} etc. resolve
        # even when this target is not the primary.
        prefix = f"{target_name}."
        per_target_vars = dict(template_vars)
        for key, value in template_vars.items():
            if key.startswith(prefix):
                per_target_vars[key[len(prefix):]] = value

        # Process template variables
        content, _ = process_template(raw, per_target_vars, template_path=tpl_path)
        lines = content.splitlines(keepends=True)

        # Extract top-level on block
        on_block, lines = _extract_top_level_block(lines, "on")
        if on_block:
            all_on_triggers.append(_parse_on_triggers(on_block))

        # Extract top-level permissions block
        perm_block, lines = _extract_top_level_block(lines, "permissions")
        if perm_block:
            all_permissions.append(_parse_permissions(perm_block))

        # Extract top-level env block
        env_block, lines = _extract_top_level_block(lines, "env")
        if env_block:
            for k, full_line in _parse_env(env_block):
                if k not in seen_env_keys:
                    seen_env_keys.add(k)
                    all_env_entries.append(full_line)

        # Extract jobs section
        job_lines = _extract_jobs_section(lines)
        if not job_lines:
            continue

        # Find the job key name (first non-blank, non-comment line at 2-space indent)
        original_job_key = None
        for jl in job_lines:
            stripped = jl.rstrip()
            if stripped and not stripped.startswith("#"):
                # Should be like "  jobname:" at 2-space indent
                match = re.match(r"^  (\S+):\s*$", stripped)
                if match:
                    original_job_key = match.group(1)
                break

        if original_job_key is None:
            continue

        # Rename the job key to the target name for uniqueness
        renamed_lines = []
        key_replaced = False
        for jl in job_lines:
            if not key_replaced:
                stripped = jl.rstrip()
                if stripped and not stripped.startswith("#"):
                    # Replace original job key with target name
                    jl = jl.replace(f"  {original_job_key}:", f"  {target_name}:", 1)
                    key_replaced = True
            renamed_lines.append(jl)

        all_jobs.append(renamed_lines)

    # Compose the merged workflow
    output_lines = []
    output_lines.append("name: Publish\n")
    output_lines.append("\n")

    # Merged on: triggers
    merged_triggers = _merge_on_triggers(all_on_triggers)
    output_lines.append("on:\n")
    for trigger_key, sub_lines in merged_triggers.items():
        if sub_lines:
            output_lines.append(f"  {trigger_key}:\n")
            for sl in sub_lines:
                line = sl if sl.endswith("\n") else sl + "\n"
                output_lines.append(line)
        else:
            output_lines.append(f"  {trigger_key}:\n")

    # Merged permissions
    merged_perms = _merge_permissions(all_permissions)
    if merged_perms:
        output_lines.append("\n")
        output_lines.append("permissions:\n")
        for k in sorted(merged_perms):
            output_lines.append(f"  {k}: {merged_perms[k]}\n")

    # Merged env
    if all_env_entries:
        output_lines.append("\n")
        output_lines.append("env:\n")
        for entry in all_env_entries:
            # Ensure the line is properly indented (should already be 2-space)
            line = entry if entry.endswith("\n") else entry + "\n"
            output_lines.append(line)

    # Jobs
    output_lines.append("\n")
    output_lines.append("jobs:\n")
    for i, job_lines in enumerate(all_jobs):
        # Strip trailing blank lines from previous job
        if i > 0:
            # Add a blank line between jobs
            output_lines.append("\n")
        for jl in job_lines:
            line = jl if jl.endswith("\n") else jl + "\n"
            output_lines.append(line)

    # Remove trailing blank lines
    result = "".join(output_lines)
    result = result.rstrip("\n") + "\n"
    return result


def _merge_template_vars(registries_list, primary, target_paths):
    """Build a merged template vars dict with namespaced keys from all targets.

    The primary target's vars are included un-namespaced (as the base).
    Every target's vars are also included with a namespace prefix:
    ``{target_name}.{key}`` so templates can reference target-specific values
    like ``{{pypi.minRequiredPython}}``.

    target_paths is a dict mapping target name to its directory path.
    """
    merged = {}
    # Primary target's vars as base (un-namespaced)
    primary_target = TARGETS[primary]
    primary_vars = primary_target.template_vars(target_paths.get(primary, "."))
    merged.update(primary_vars)
    # All targets' vars namespaced
    for target_name in registries_list:
        target = TARGETS[target_name]
        target_vars = target.template_vars(target_paths.get(target_name, "."))
        for key, value in target_vars.items():
            merged[f"{target_name}.{key}"] = value
    return merged


def _plan_merged_publish(publish_target, merged_content, force, update):
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
    if update:
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
    return {
        "target": publish_target,
        "status": "exists",
        "bucket": "skipped",
        "action": "none",
    }


def run_cmd_multi(registries_list, args, flags):
    """Scaffold for multiple registries with a merged publish workflow.

    Uses the primary registry for template vars and CI, then writes a merged
    publish.yml that contains jobs for all detected registries.
    """
    primary = registries_list[0]
    reg = TARGETS[primary]

    if not reg.check_project_exists("."):
        print(f"Error: no {primary} project found in current directory.", file=sys.stderr)
        sys.exit(1)

    # Check for npm lockfile
    npm_lockfile_missing = False
    if "npm" in registries_list:
        npm_lockfile_missing = _check_npm_lockfile_missing()

    dry_run = flags.get("dry-run", False)

    # Acquire advisory lock to prevent concurrent rlsbl operations
    acquire_lock()

    try:
        # Register all targets in .rlsbl/config.json targets array
        # (skipped under --dry-run -- we don't write config)
        if not dry_run:
            for r in registries_list:
                _ensure_target_in_config(r)

        # Determine if this is a private repository
        private = _resolve_private(flags)
        if not dry_run:
            write_project_config("private", private)

        print(f"Multiple registries detected: {', '.join(registries_list)}")
        if private:
            print("Scaffolding for private repository (no publish workflow).")
        else:
            print("Scaffolding with merged publish workflow.")

        # Build per-target path mapping from detect_targets or default to "."
        target_entries = detect_targets(".")
        target_paths = {entry.name: entry.path for entry in target_entries}
        vars_dict = _merge_template_vars(registries_list, primary, target_paths)
        from datetime import datetime
        vars_dict["year"] = str(datetime.now().year)

        force = flags.get("force", False)
        update = flags.get("update", False)
        existing_hashes = load_hashes()

        # Process primary registry CI template only (publish will come from merged)
        ci_mappings = [m for m in reg.template_mappings() if "publish" not in m["template"]]
        ci_plans = plan_mappings(
            reg.template_dir(), ci_mappings, vars_dict, force, update=update,
        )

        # Plan the merged publish workflow (skip for private repos)
        merged_plans = []
        if not private:
            publish_target = os.path.join(".github", "workflows", "publish.yml")
            merged_content = _generate_merged_publish(registries_list, vars_dict)
            merged_plans = [_plan_merged_publish(
                publish_target, merged_content, force, update,
            )]

        # Plan shared templates (once)
        shared_mappings = reg.shared_template_mappings()
        shared_mappings = _append_deploy_workflow_if_configured(shared_mappings)
        shared_plans = plan_mappings(
            reg.shared_template_dir(), shared_mappings, vars_dict, force, update=update,
        )

        if dry_run:
            _print_dry_run_report(
                [ci_plans, merged_plans, shared_plans],
                registries=registries_list,
            )
            return

        ci_created, ci_skipped, ci_warnings, ci_hashes = apply_plans(ci_plans)
        merged_created, merged_skipped, merged_warnings, merged_hashes = apply_plans(merged_plans)
        shared_created, shared_skipped, shared_warnings, shared_hashes = apply_plans(shared_plans)

        created = ci_created + merged_created + shared_created
        skipped = ci_skipped + merged_skipped + shared_skipped
        warnings = ci_warnings + merged_warnings + shared_warnings

        _finalize_scaffold(
            existing_hashes, [ci_hashes, merged_hashes, shared_hashes],
            created, skipped, warnings,
            flags=flags, registries=registries_list,
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
        _trigger_monorepo_sync(no_commit=bool(flags.get("no-commit")))
    finally:
        release_lock()
