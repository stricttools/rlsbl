"""Changelog subcommands for adding new entries, amending released versions, and generating Markdown changelogs from JSONL sources."""

import json
import os
import subprocess
import sys

from ..changelog.files import (
    append_entry,
    append_entry_to_version,
    changes_dir_exists,
    get_changes_dir,
    is_read_only,
    read_unreleased,
    writable_jsonl,
)
from ..changelog.generate import generate_changelog
from ..changelog.resolve import resolve_hash
from ..changelog.schema import ChangelogEntry, parse_jsonl, validate_schema
from ..changelog.validate import _get_batch_limits_config
from ..config import read_project_config
from ..git_util import filter_commits_for_project
from ..utils import commit_files
from ..workspace import find_workspace_root, resolve_project


def _resolve_workspace_project(project_root):
    """Resolve the WorkspaceProject for project_root, or None in standalone mode.

    Also checks and exits if the project is a dev_node.
    """
    if project_root is None:
        return None
    ws_root = find_workspace_root(str(project_root))
    if ws_root is None:
        return None
    project = resolve_project(ws_root, str(project_root))
    if project is None:
        return None
    if project.get("dev_node"):
        print("Error: dev node projects don't use changelogs.", file=sys.stderr)
        sys.exit(1)
    return project


def _check_project_scope(resolved_commits, project):
    """Verify all commits touch files belonging to the project.

    Hard error if any commit does not touch the project's files.
    Skipped when project is None (standalone mode).
    """
    if project is None:
        return
    in_scope = filter_commits_for_project(set(resolved_commits), project)
    for sha in resolved_commits:
        if sha not in in_scope:
            name = project.get("name", project.get("path", "unknown"))
            path = project.get("path", "unknown")
            print(
                f"Error: commit {sha[:12]} does not touch files in "
                f"project '{name}' (path: {path}). Use the correct "
                f"project directory or update watch patterns in "
                f"workspace.toml.",
                file=sys.stderr,
            )
            sys.exit(1)


def _check_duplicate_commits(existing_entries, new_entry):
    """Check if any commits in new_entry already appear in existing entries.

    Hard error when a commit appears in an existing entry with the same
    user_facing value and type. Warning when the types differ (legitimate
    case: one commit spanning multiple changelog types).
    """
    for new_hash in new_entry.commits:
        for i, existing in enumerate(existing_entries, start=1):
            if new_hash in existing.commits:
                short = new_hash[:12]
                if (existing.user_facing == new_entry.user_facing
                        and existing.type == new_entry.type):
                    desc = existing.description or "(no description)"
                    print(
                        f"Error: commit {short} already covered by entry {i}: {desc}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                else:
                    desc = existing.description or "(no description)"
                    print(
                        f"Warning: commit {short} already in entry {i}: {desc}",
                        file=sys.stderr,
                    )


def cmd_add(flags, project_root):
    """Add a changelog entry to unreleased.jsonl.

    Required flags:
    - --commits: comma-separated commit hashes
    - --description and --type: required unless --no-user-facing is set
    """
    ws_project = _resolve_workspace_project(project_root)

    commits_raw = flags.get("commits", "")
    if not commits_raw:
        print("Error: --commits is required.", file=sys.stderr)
        sys.exit(1)

    commits = [h.strip() for h in commits_raw.split(",") if h.strip()]
    if not commits:
        print("Error: --commits must contain at least one hash.", file=sys.stderr)
        sys.exit(1)

    # Resolve each hash
    resolved_commits = []
    for h in commits:
        full = resolve_hash(h)
        if full is None:
            print(f"Error: commit hash does not resolve: {h}", file=sys.stderr)
            sys.exit(1)
        resolved_commits.append(full)

    _check_project_scope(resolved_commits, ws_project)

    no_user_facing = flags.get("no-user-facing", False)
    user_facing = not no_user_facing
    description = flags.get("description") or None
    entry_type = flags.get("type") or None

    if user_facing:
        if not description:
            print(
                "Error: --description is required for user-facing entries. "
                "Use --no-user-facing to skip.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not entry_type:
            print(
                "Error: --type is required for user-facing entries. "
                "Use --no-user-facing to skip.",
                file=sys.stderr,
            )
            sys.exit(1)

    entry = ChangelogEntry(
        commits=resolved_commits,
        user_facing=user_facing,
        description=description,
        type=entry_type,
    )

    errors = validate_schema(entry)
    if errors:
        for err in errors:
            print(f"Error: schema validation: {err}", file=sys.stderr)
        sys.exit(1)

    # Check batch size limit before writing
    config = read_project_config(project_root)
    batch_config = _get_batch_limits_config(config)
    max_commits = batch_config.get("max_commits_per_entry", 5)
    if len(resolved_commits) > max_commits:
        allow_batch = flags.get("allow-batch", False)
        if not allow_batch:
            print(
                f"Error: entry has {len(resolved_commits)} commits but the limit is "
                f"{max_commits} (batch_limits.max_commits_per_entry in .rlsbl/config.json).\n"
                f"Either split into smaller entries, add an exclusion to "
                f"batch_limits.exclusions in .rlsbl/config.json, or re-run with "
                f"--allow-batch to auto-create an exclusion.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Auto-create an exclusion in config.json
        changes_dir_for_line = get_changes_dir(project_root)
        existing_for_line = read_unreleased(changes_dir_for_line)
        line_number = len(existing_for_line) + 1
        reason = description[:60] if description else "non-user-facing batch"
        exclusion = {
            "reason": reason,
            "entries": [{"version": "unreleased", "line": line_number}],
        }
        config_path = os.path.join(project_root, ".rlsbl", "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        batch_limits = config_data.setdefault("batch_limits", {})
        exclusions = batch_limits.setdefault("exclusions", [])
        exclusions.append(exclusion)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=2)
            f.write("\n")
        print(f"Auto-created batch exclusion for line {line_number} in .rlsbl/config.json")

    changes_dir = get_changes_dir(project_root)
    existing = read_unreleased(changes_dir)
    _check_duplicate_commits(existing, entry)
    append_entry(changes_dir, entry)
    print(f"Added entry with {len(resolved_commits)} commit(s)")

    if not flags.get("no-commit"):
        unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
        if user_facing:
            commit_msg = f"changelog: {description}"[:72]
        else:
            commit_msg = "changelog: non-user-facing entry"
        commit_files(commit_msg, [unreleased_path], allow_failure=True)



def cmd_generate(flags, project_root):
    """Generate CHANGELOG.md from JSONL changelog files."""
    _resolve_workspace_project(project_root)

    if not changes_dir_exists(project_root):
        print("Error: .rlsbl/changes/ does not exist.", file=sys.stderr)
        sys.exit(1)

    dry_run = flags.get("dry-run", False)

    if dry_run:
        # Generate content without writing by temporarily redirecting
        # We need to generate the content but not write CHANGELOG.md.
        # generate_changelog() both generates and writes, so we replicate
        # the generation logic without the write step.
        from ..changelog.files import list_versioned_files, read_unreleased
        from ..changelog.generate import (
            _HEADER_COMMENT,
            generate_version_section,
        )

        changes_dir = get_changes_dir(project_root)
        sections = []

        unreleased = read_unreleased(changes_dir)
        if unreleased:
            sections.append(generate_version_section("Unreleased", unreleased))

        for version, jsonl_path in list_versioned_files(changes_dir):
            from ..changelog.schema import parse_jsonl

            entries = parse_jsonl(jsonl_path)
            sections.append(generate_version_section(version, entries))

        body = "\n".join(sections)
        content = f"{_HEADER_COMMENT}\n\n# Changelog\n\n{body}"
        print(content)
        print("\n(dry-run: no files written)")
    else:
        content = generate_changelog(project_root)
        print("Generated CHANGELOG.md")

        if not flags.get("no-commit"):
            # Collect changed files: CHANGELOG.md and per-version .md files
            changed_files = _get_generated_files(project_root)
            if changed_files:
                commit_files(
                    "changelog: regenerate from JSONL",
                    changed_files,
                    allow_failure=True,
                )


def cmd_amend(flags, project_root):
    """Amend a released version's JSONL changelog by appending a new entry.

    Unlocks the read-only versioned JSONL file, appends the entry, re-locks it,
    regenerates CHANGELOG.md, and optionally syncs GitHub Release notes.

    Required flags:
    - --version: which released version to amend (e.g., "0.39.0")
    - --commits: comma-separated commit hashes

    Optional flags:
    - --description and --type: required unless --no-user-facing is set
    - --no-user-facing: mark entry as non-user-facing
    - --no-resolve: skip hash validation (for old/amended commits)
    """
    ws_project = _resolve_workspace_project(project_root)

    version = flags.get("version", "")
    if not version:
        print("Error: --version is required.", file=sys.stderr)
        sys.exit(1)

    commits_raw = flags.get("commits", "")
    if not commits_raw:
        print("Error: --commits is required.", file=sys.stderr)
        sys.exit(1)

    commits = [h.strip() for h in commits_raw.split(",") if h.strip()]
    if not commits:
        print("Error: --commits must contain at least one hash.", file=sys.stderr)
        sys.exit(1)

    no_resolve = flags.get("no-resolve", False)

    if no_resolve:
        resolved_commits = commits
    else:
        resolved_commits = []
        for h in commits:
            full = resolve_hash(h)
            if full is None:
                print(f"Error: commit hash does not resolve: {h}", file=sys.stderr)
                sys.exit(1)
            resolved_commits.append(full)

    if not no_resolve:
        _check_project_scope(resolved_commits, ws_project)

    no_user_facing = flags.get("no-user-facing", False)
    user_facing = not no_user_facing
    description = flags.get("description") or None
    entry_type = flags.get("type") or None

    if user_facing:
        if not description:
            print(
                "Error: --description is required for user-facing entries. "
                "Use --no-user-facing to skip.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not entry_type:
            print(
                "Error: --type is required for user-facing entries. "
                "Use --no-user-facing to skip.",
                file=sys.stderr,
            )
            sys.exit(1)

    entry = ChangelogEntry(
        commits=resolved_commits,
        user_facing=user_facing,
        description=description,
        type=entry_type,
    )

    errors = validate_schema(entry)
    if errors:
        for err in errors:
            print(f"Error: schema validation: {err}", file=sys.stderr)
        sys.exit(1)

    changes_dir = get_changes_dir(project_root)
    jsonl_path = os.path.join(changes_dir, f"{version}.jsonl")

    if not os.path.isfile(jsonl_path):
        print(f"Error: {jsonl_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    with writable_jsonl(jsonl_path):
        existing = parse_jsonl(jsonl_path)
        _check_duplicate_commits(existing, entry)
        append_entry_to_version(changes_dir, version, entry)
        print(f"Amended {version}.jsonl with {len(resolved_commits)} commit(s)")

    # Regenerate CHANGELOG.md
    generate_changelog(project_root)
    print("Regenerated CHANGELOG.md")

    # Sync GitHub Release notes (best-effort)
    _sync_github_release(version)

    # Auto-commit all changed files
    changed_files = [jsonl_path]
    md_path = os.path.join(changes_dir, f"{version}.md")
    if os.path.isfile(md_path):
        changed_files.append(md_path)
    changelog_path = os.path.join(project_root, "CHANGELOG.md")
    if os.path.isfile(changelog_path):
        changed_files.append(changelog_path)

    commit_msg = f"changelog: amend {version}"
    if user_facing and description:
        commit_msg = f"changelog: amend {version}: {description}"[:72]
    commit_files(commit_msg, changed_files, allow_failure=True)


def _sync_github_release(version: str) -> None:
    """Sync GitHub Release notes for a version (best-effort, warns on failure)."""
    try:
        result = subprocess.run(
            ["rlsbl", "release", "edit", version],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            print(f"Synced GitHub Release notes for v{version}")
        else:
            print(
                f"Warning: could not sync GitHub Release notes: {result.stderr.strip()}",
                file=sys.stderr,
            )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(
            f"Warning: could not sync GitHub Release notes: {e}",
            file=sys.stderr,
        )


def _get_generated_files(project_path: str) -> list[str]:
    """Return paths of files modified or created by generate_changelog.

    Checks git status for CHANGELOG.md and .rlsbl/changes/*.md files.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    changes_dir = get_changes_dir(project_path)
    files = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        filepath = line[3:].strip()
        # Match CHANGELOG.md or .rlsbl/changes/*.md
        if filepath == "CHANGELOG.md":
            files.append(os.path.join(project_path, filepath))
        elif filepath.startswith(".rlsbl/changes/") and filepath.endswith(".md"):
            files.append(os.path.join(project_path, filepath))
    return files
