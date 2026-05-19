"""Changelog subcommands for adding new entries, validating existing ones, and generating Markdown changelogs from JSONL sources."""

import os
import subprocess
import sys

from ..changelog.files import append_entry, changes_dir_exists, get_changes_dir
from ..changelog.generate import generate_changelog
from ..changelog.resolve import resolve_hash
from ..changelog.schema import ChangelogEntry, validate_schema
from ..changelog.validate import validate_unreleased
from ..targets import TARGETS, detect_targets
from ..utils import commit_files
from ..workspace import find_workspace_root, resolve_project


def cmd_add(flags):
    """Add a changelog entry to unreleased.jsonl.

    Required flags:
    - --commits: comma-separated commit hashes
    - --description and --type: required unless --no-user-facing is set
    """
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

    changes_dir = get_changes_dir(".")
    append_entry(changes_dir, entry)
    print(f"Added entry with {len(resolved_commits)} commit(s)")

    if not flags.get("no-commit"):
        unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
        if user_facing:
            commit_msg = f"changelog: {description}"[:72]
        else:
            commit_msg = "changelog: non-user-facing entry"
        commit_files(commit_msg, [unreleased_path], allow_failure=True)


def cmd_validate(flags):
    """Run the changelog validation engine on unreleased entries."""
    if not changes_dir_exists("."):
        print("Error: .rlsbl/changes/ does not exist.", file=sys.stderr)
        sys.exit(1)

    # Detect monorepo context for tag-scoped validation
    tag_glob = None
    monorepo_root = find_workspace_root(".")
    if monorepo_root:
        project = resolve_project(monorepo_root, ".")
        if project is None:
            print(
                "Error: current directory is inside a monorepo but not inside any project.",
                file=sys.stderr,
            )
            print(
                "Run 'rlsbl monorepo status' to see registered projects.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Compute the tag glob via the target for correct format (path-based for Go)
        target_entries = detect_targets(os.path.join(monorepo_root, project["path"]))
        if target_entries:
            target = TARGETS[target_entries[0].name]
            tag_glob = target.monorepo_tag_glob(project["name"], path=project["path"])
        else:
            tag_glob = f"{project['name']}@v*"

    changes_dir = get_changes_dir(".")
    results = validate_unreleased(changes_dir, tag_glob=tag_glob)

    overall = results["passed"]
    for name, (passed, details) in results["checks"].items():
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {name}")
        for detail in details:
            print(f"         {detail}")

    if overall:
        print("\nAll checks passed.")
    else:
        print("\nValidation failed.", file=sys.stderr)
        sys.exit(1)


def cmd_generate(flags):
    """Generate CHANGELOG.md from JSONL changelog files."""
    if not changes_dir_exists("."):
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

        changes_dir = get_changes_dir(".")
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
        content = generate_changelog(".")
        print("Generated CHANGELOG.md")

        if not flags.get("no-commit"):
            # Collect changed files: CHANGELOG.md and per-version .md files
            changed_files = _get_generated_files(".")
            if changed_files:
                commit_files(
                    "changelog: regenerate from JSONL",
                    changed_files,
                    allow_failure=True,
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
