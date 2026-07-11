"""Changelog subcommands for adding new entries, amending released versions, and generating Markdown changelogs from JSONL sources."""

import json
import os
import subprocess
import sys
import tempfile

from ..changelog.files import (
    append_entry,
    append_entry_to_version,
    enumerate_changelog_dirs,
    get_changes_dir,
    get_pending_dir,
    list_versioned_files,
    read_coverage_unit,
    read_pending_files,
    read_unreleased,
    remap_jsonl_hashes,
    writable_jsonl,
    write_pending_file,
)
from ..changelog.generate import generate_changelog
from ..changelog.resolve import resolve_hash
from ..changelog.schema import ChangelogEntry, generate_entry_id, parse_jsonl, serialize_entry, validate_schema
from ..changelog.validate import _get_batch_limits_config
from ..config import read_project_config
from ..git_util import filter_commits_for_project, filter_commits_for_releasable
from ..utils import commit_files
from ..workspace import (
    find_workspace_root,
    get_releasable_changes_dir,
    get_releasable_dir,
    is_explicit_mode,
    load_releasables,
    load_workspace,
    members_of,
    resolve_project,
    resolve_releasable_for_project,
)


class _ResolvedContext:
    """Carries project, releasable, and workspace info for changelog commands."""

    def __init__(self, project, releasable=None, ws_root=None, member_projects=None):
        self.project = project
        self.releasable = releasable
        self.ws_root = ws_root
        self.member_projects = member_projects or []

    @property
    def is_releasable(self):
        return self.project.is_releasable if self.project else True

    @property
    def name(self):
        return self.project.name if self.project else None

    # Forward dict-like access to the underlying project for backward compat
    def get(self, key, default=None):
        if self.project is not None:
            return self.project.get(key, default)
        return default

    def __getitem__(self, key):
        return self.project[key]


def _resolve_workspace_project(project_root):
    """Resolve the WorkspaceProject for project_root, or None in standalone mode.

    Also checks and exits if the project is non-releasable.
    Returns a _ResolvedContext with releasable info when in explicit mode.
    """
    if project_root is None:
        return None
    ws_root = find_workspace_root(str(project_root))
    if ws_root is None:
        return None
    project = resolve_project(ws_root, str(project_root))
    if project is None:
        return None
    if not project.is_releasable:
        print("Error: non-releasable projects don't use changelogs.", file=sys.stderr)
        sys.exit(1)

    # Check for explicit releasable mode
    releasable = None
    member_projects = []
    if is_explicit_mode(ws_root):
        projects = load_workspace(ws_root)
        releasables = load_releasables(ws_root, projects=projects)
        releasable = resolve_releasable_for_project(project, releasables)
        if releasable is not None:
            member_projects = members_of(releasable.name, projects)

    return _ResolvedContext(
        project=project,
        releasable=releasable,
        ws_root=ws_root,
        member_projects=member_projects,
    )


def _check_project_scope(resolved_commits, ws_context):
    """Verify all commits touch files belonging to the project or releasable.

    Hard error if any commit does not touch the project's files.
    In explicit releasable mode, checks against all member projects.
    Skipped when ws_context is None (standalone mode).
    """
    if ws_context is None:
        return

    # In explicit releasable mode, scope to the releasable's members
    if isinstance(ws_context, _ResolvedContext) and ws_context.releasable is not None:
        members = ws_context.member_projects
        in_scope = filter_commits_for_releasable(set(resolved_commits), members)
        for sha in resolved_commits:
            if sha not in in_scope:
                print(
                    f"Error: commit {sha[:12]} does not touch files in "
                    f"releasable '{ws_context.releasable.name}'. Use the "
                    f"correct project directory or update watch patterns "
                    f"in workspace.toml.",
                    file=sys.stderr,
                )
                sys.exit(1)
        return

    # Implicit mode or raw project: scope to single project
    project = ws_context.project if isinstance(ws_context, _ResolvedContext) else ws_context
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


def _build_entry(flags, resolved_commits):
    """Build and validate a ChangelogEntry from CLI flags and resolved commits.

    Reads user_facing, description, type, and release_type from flags.
    Validates that user-facing entries have description and type.
    Returns a validated ChangelogEntry.
    """
    user_facing = flags.get("user-facing", True)
    description = flags.get("description") or None
    entry_type = flags.get("type") or None

    if user_facing:
        if not description:
            print(
                "Error: --description is required for user-facing entries. "
                "Use --no-user-facing to mark as internal.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not entry_type:
            print(
                "Error: --type is required for user-facing entries. "
                "Use --no-user-facing to mark as internal.",
                file=sys.stderr,
            )
            sys.exit(1)

    entry = ChangelogEntry(
        commits=resolved_commits,
        user_facing=user_facing,
        description=description,
        type=entry_type,
        release_type=flags.get("release-type") or None,
        id=generate_entry_id(),
    )

    errors = validate_schema(entry)
    if errors:
        for err in errors:
            print(f"Error: schema validation: {err}", file=sys.stderr)
        sys.exit(1)

    return entry


def _regenerate_changelog_outputs(ws_context, project_root, changes_dir):
    """Regenerate CHANGELOG.md via the single home resolver.

    In explicit releasable mode, writes the canonical CHANGELOG.md into the
    releasable dir and regenerates the combined root CHANGELOG.md; otherwise
    writes the project-root CHANGELOG.md. Returns the list of output paths
    (for auto-commit).
    """
    if (isinstance(ws_context, _ResolvedContext)
            and ws_context.releasable is not None
            and ws_context.ws_root is not None):
        from ..changelog.home import (
            generate_workspace_changelog,
            get_changelog_home,
            get_workspace_changelog_path,
        )

        from ..release_file import get_releases_dir

        releasable_dir = get_releasable_dir(
            ws_context.ws_root, ws_context.releasable.name,
        )
        canonical = get_changelog_home(project_root, releasable_dir=releasable_dir)
        generate_changelog(
            project_root,
            changes_dir_override=changes_dir,
            changelog_output_path=canonical,
            releases_dir_override=get_releases_dir(
                project_root, releasable_dir=releasable_dir,
            ),
        )
        generate_workspace_changelog(ws_context.ws_root)
        return [canonical, get_workspace_changelog_path(ws_context.ws_root)]

    generate_changelog(project_root)
    return [os.path.join(project_root, "CHANGELOG.md")]


def _resolve_changes_dir(ws_context, project_root):
    """Return the appropriate changes directory based on context.

    In explicit releasable mode, returns the releasable's changes dir.
    Otherwise, returns the per-project changes dir.
    """
    if (isinstance(ws_context, _ResolvedContext)
            and ws_context.releasable is not None
            and ws_context.ws_root is not None):
        return get_releasable_changes_dir(ws_context.ws_root, ws_context.releasable.name)
    return get_changes_dir(project_root)


def _derive_packages_from_commits(resolved_commits, member_projects):
    """Derive the list of affected package names from commit file paths.

    For each commit, checks which member projects have files touched.
    Returns a sorted, deduplicated list of project names, or None if
    there are no member projects to check against.
    """
    if not member_projects:
        return None
    from ..git_util import get_commit_files, file_matches_project

    affected = set()
    for sha in resolved_commits:
        files = get_commit_files(sha)
        if files is None:
            continue
        for filepath in files:
            for proj in member_projects:
                if file_matches_project(filepath, proj):
                    name = proj.name if hasattr(proj, "name") else proj["name"]
                    affected.add(name)
    return sorted(affected) if affected else None


def _populate_packages_field(entry, resolved_commits, ws_context):
    """Auto-populate ``entry.packages`` in explicit releasable mode.

    Releasable-scoped: only the current releasable's members are considered,
    so a commit touching sub-projects of another releasable does not leak
    those packages into this releasable's changelog entry. No-op outside
    explicit releasable mode (non-releasable context or no member projects).
    """
    if (isinstance(ws_context, _ResolvedContext)
            and ws_context.releasable is not None
            and ws_context.member_projects):
        packages = _derive_packages_from_commits(
            resolved_commits, ws_context.member_projects,
        )
        if packages:
            entry.packages = packages


def cmd_add(flags, project_root):
    """Add a changelog entry.

    In commit mode (``coverage_unit = "commit"``):
    - Appends to unreleased.jsonl.
    - Required flags: --commits, --description, --type (unless --no-user-facing).

    In changeset-file mode (``coverage_unit = "changeset-file"``):
    - Writes a pending file in ``.rlsbl/changes/pending/<id>.json``.
    - --commits is ignored (no commit association).
    - Required flags: --description, --type (unless --no-user-facing).

    Under --dry-run, all validation runs but nothing is written.
    """
    ws_context = _resolve_workspace_project(project_root)
    dry_run = flags.get("dry-run", False)

    # Determine coverage mode
    releasable_config_dir = None
    if (isinstance(ws_context, _ResolvedContext)
            and ws_context.releasable is not None
            and ws_context.ws_root is not None):
        releasable_config_dir = get_releasable_dir(
            ws_context.ws_root, ws_context.releasable.name,
        )
    config = read_project_config(project_root, releasable_config_dir=releasable_config_dir)
    coverage_unit = read_coverage_unit(config)

    if coverage_unit == "changeset-file":
        return _cmd_add_changeset(flags, project_root, ws_context, config, dry_run)
    return _cmd_add_commit(flags, project_root, ws_context, config, dry_run)


def _cmd_add_changeset(flags, project_root, ws_context, config, dry_run):
    """Add a changelog entry in changeset-file mode (pending file)."""
    user_facing = flags.get("user-facing", True)
    description = flags.get("description") or None
    entry_type = flags.get("type") or None

    if user_facing:
        if not description:
            print(
                "Error: --description is required for user-facing entries. "
                "Use --no-user-facing to mark as internal.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not entry_type:
            print(
                "Error: --type is required for user-facing entries. "
                "Use --no-user-facing to mark as internal.",
                file=sys.stderr,
            )
            sys.exit(1)

    entry = ChangelogEntry(
        id=generate_entry_id(),
        user_facing=user_facing,
        description=description,
        type=entry_type,
        release_type=flags.get("release-type") or None,
    )

    # Auto-populate packages field in explicit releasable mode
    if (isinstance(ws_context, _ResolvedContext)
            and ws_context.releasable is not None):
        entry.packages = [ws_context.releasable.name]

    errors = validate_schema(entry, coverage_unit="changeset-file")
    if errors:
        for err in errors:
            print(f"Error: schema validation: {err}", file=sys.stderr)
        sys.exit(1)

    changes_dir = _resolve_changes_dir(ws_context, project_root)
    pending_dir = get_pending_dir(changes_dir)

    if dry_run:
        print(serialize_entry(entry))
        print("(dry-run: no files written)")
        return

    path = write_pending_file(pending_dir, entry)
    print(f"Added pending file: {os.path.basename(path)}")

    if flags.get("auto-commit", True):
        if user_facing:
            commit_msg = f"changelog: {description}"
        else:
            commit_msg = "changelog: non-user-facing entry"
        commit_files(commit_msg, [path], allow_failure=True, autogenerated=True)


def _cmd_add_commit(flags, project_root, ws_context, config, dry_run):
    """Add a changelog entry in commit mode (unreleased.jsonl)."""
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

    _check_project_scope(resolved_commits, ws_context)

    entry = _build_entry(flags, resolved_commits)
    user_facing = entry.user_facing
    description = entry.description

    # Auto-populate packages field in explicit releasable mode
    _populate_packages_field(entry, resolved_commits, ws_context)

    # Check batch size limit before writing
    releasable_config_dir = None
    if (isinstance(ws_context, _ResolvedContext)
            and ws_context.releasable is not None
            and ws_context.ws_root is not None):
        releasable_config_dir = get_releasable_dir(
            ws_context.ws_root, ws_context.releasable.name,
        )
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
        # In releasable mode, write to releasable-level config.json
        changes_dir_for_line = _resolve_changes_dir(ws_context, project_root)
        existing_for_line = read_unreleased(changes_dir_for_line)
        line_number = len(existing_for_line) + 1
        reason = description if description else "non-user-facing batch"
        exclusion = {
            "reason": reason,
            "entries": [{"version": "unreleased", "line": line_number}],
        }
        if dry_run:
            print(f"Would auto-create batch exclusion for line {line_number} in .rlsbl/config.json")
        else:
            if releasable_config_dir is not None:
                config_path = os.path.join(releasable_config_dir, "config.json")
            else:
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

    changes_dir = _resolve_changes_dir(ws_context, project_root)
    existing = read_unreleased(changes_dir)
    _check_duplicate_commits(existing, entry)

    if dry_run:
        print(serialize_entry(entry))
        print("(dry-run: no files written)")
        return

    append_entry(changes_dir, entry)
    print(f"Added entry with {len(resolved_commits)} commit(s)")

    if flags.get("auto-commit", True):
        unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
        if user_facing:
            commit_msg = f"changelog: {description}"
        else:
            commit_msg = "changelog: non-user-facing entry"
        commit_files(commit_msg, [unreleased_path], allow_failure=True)



def cmd_generate(flags, project_root):
    """Generate CHANGELOG.md from JSONL changelog files."""
    ws_context = _resolve_workspace_project(project_root)

    # Determine the changes dir (releasable or per-project)
    changes_dir = _resolve_changes_dir(ws_context, project_root)
    if not os.path.isdir(changes_dir):
        print("Error: changes directory does not exist.", file=sys.stderr)
        sys.exit(1)

    # Determine where to write CHANGELOG.md
    is_releasable_mode = (
        isinstance(ws_context, _ResolvedContext)
        and ws_context.releasable is not None
        and ws_context.ws_root is not None
    )

    # Archived release files (v{x}.toml metadata backfill) live at the
    # releasable level in explicit releasable mode.
    releases_dir_override = None
    if is_releasable_mode:
        from ..release_file import get_releases_dir
        from ..workspace import get_releasable_dir as _get_rel_dir

        releases_dir_override = get_releases_dir(
            project_root,
            releasable_dir=_get_rel_dir(
                ws_context.ws_root, ws_context.releasable.name,
            ),
        )

    dry_run = flags.get("dry-run", False)

    if dry_run:
        # NOTE: list_versioned_files and read_unreleased come from the
        # module-level import -- a local re-import here would shadow the
        # name for the WHOLE function and break the auto-commit path below.
        from ..changelog.generate import (
            _HEADER_COMMENT,
            _read_release_metadata_full,
            generate_version_section,
        )

        sections = []

        unreleased = read_unreleased(changes_dir)
        if unreleased:
            sections.append(generate_version_section("Unreleased", unreleased))

        for version, jsonl_path in list_versioned_files(changes_dir):
            from ..changelog.schema import parse_jsonl

            entries = parse_jsonl(jsonl_path)
            ver_desc, ver_ctx, ver_bump = _read_release_metadata_full(
                project_root, version, releases_dir=releases_dir_override,
            )
            sections.append(generate_version_section(
                version, entries, description=ver_desc, context=ver_ctx,
                bump_type=ver_bump or None,
            ))

        body = "\n".join(sections)
        content = f"{_HEADER_COMMENT}\n\n# Changelog\n\n{body}"
        print(content)
        print("\n(dry-run: no files written)")
    else:
        if is_releasable_mode:
            from ..changelog.home import (
                generate_workspace_changelog,
                get_changelog_home,
                get_workspace_changelog_path,
            )
            from ..workspace import get_releasable_dir
            releasable_dir = get_releasable_dir(
                ws_context.ws_root, ws_context.releasable.name,
            )
            canonical_path = get_changelog_home(
                project_root, releasable_dir=releasable_dir,
            )
            content = generate_changelog(
                project_root,
                changes_dir_override=changes_dir,
                changelog_output_path=canonical_path,
                releases_dir_override=releases_dir_override,
            )
            # Regenerate the combined root CHANGELOG.md covering all
            # releasables of the workspace.
            generate_workspace_changelog(ws_context.ws_root)
        else:
            content = generate_changelog(project_root)
        print("Generated CHANGELOG.md")

        if flags.get("auto-commit", True):
            if is_releasable_mode:
                # Explicit paths: canonical releasable CHANGELOG.md, combined
                # root CHANGELOG.md, and per-version .md files in the
                # releasable changes dir -- filtered to those git reports as
                # changed.
                candidates = [
                    canonical_path,
                    get_workspace_changelog_path(ws_context.ws_root),
                ]
                for _version, jsonl_path in list_versioned_files(changes_dir):
                    md_path = jsonl_path[: -len(".jsonl")] + ".md"
                    candidates.append(md_path)
                changed_files = _filter_dirty_files(candidates, ws_context.ws_root)
                commit_cwd = ws_context.ws_root
            else:
                # Collect changed files: CHANGELOG.md and per-version .md files
                changed_files = _get_generated_files(project_root)
                commit_cwd = None
            if changed_files:
                commit_files(
                    "changelog: regenerate from JSONL",
                    changed_files,
                    allow_failure=True,
                    cwd=commit_cwd,
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
    - --no-validate-hashes: skip hash validation (for old/amended commits)

    Under --dry-run, all validation runs but nothing is written: no JSONL
    append, no CHANGELOG.md regeneration, no GitHub Release sync, no commit.
    """
    ws_context = _resolve_workspace_project(project_root)

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

    validate_hashes = flags.get("validate-hashes", True)

    if not validate_hashes:
        resolved_commits = commits
    else:
        resolved_commits = []
        for h in commits:
            full = resolve_hash(h)
            if full is None:
                print(f"Error: commit hash does not resolve: {h}", file=sys.stderr)
                sys.exit(1)
            resolved_commits.append(full)

    if validate_hashes:
        _check_project_scope(resolved_commits, ws_context)

    entry = _build_entry(flags, resolved_commits)
    user_facing = entry.user_facing
    description = entry.description

    # Auto-populate packages field in explicit releasable mode (releasable-scoped).
    _populate_packages_field(entry, resolved_commits, ws_context)

    changes_dir = _resolve_changes_dir(ws_context, project_root)
    jsonl_path = os.path.join(changes_dir, f"{version}.jsonl")

    if not os.path.isfile(jsonl_path):
        print(f"Error: {jsonl_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    # Validate against existing entries (read-only, no unlock needed)
    existing = parse_jsonl(jsonl_path)
    _check_duplicate_commits(existing, entry)

    if flags.get("dry-run", False):
        print(serialize_entry(entry))
        print("(dry-run: no files written)")
        return

    with writable_jsonl(jsonl_path):
        append_entry_to_version(changes_dir, version, entry)
        print(f"Amended {version}.jsonl with {len(resolved_commits)} commit(s)")

    # Regenerate CHANGELOG.md at the canonical home (releasable-aware)
    changelog_outputs = _regenerate_changelog_outputs(
        ws_context, project_root, changes_dir,
    )
    print("Regenerated CHANGELOG.md")

    # Sync GitHub Release notes (best-effort)
    _sync_github_release(version)

    # Auto-commit all changed files
    changed_files = [jsonl_path]
    md_path = os.path.join(changes_dir, f"{version}.md")
    if os.path.isfile(md_path):
        changed_files.append(md_path)
    for changelog_path in changelog_outputs:
        if os.path.isfile(changelog_path):
            changed_files.append(changelog_path)

    commit_msg = f"changelog: amend {version}"
    if user_facing and description:
        commit_msg = f"changelog: amend {version}: {description}"
    commit_files(commit_msg, changed_files, allow_failure=True)


def cmd_edit(flags, project_root):
    """Edit an existing changelog entry in unreleased or released JSONL files.

    Finds the entry by commit hash, applies field changes, and rewrites
    the file atomically. For released files, temporarily unlocks the
    read-only file, regenerates CHANGELOG.md, and syncs GitHub Release notes.

    Required flags:
    - --commits: comma-separated commit hashes identifying the target entry

    At least one edit flag required:
    - --type: new type value (feature, fix, breaking)
    - --description: new description text
    - --user-facing / --no-user-facing: set user_facing status

    Under --dry-run, all validation and entry matching runs but nothing is
    written: no file rewrite, no CHANGELOG.md regeneration, no GitHub
    Release sync, no commit.
    """
    ws_context = _resolve_workspace_project(project_root)

    # Validate at least one edit flag is provided
    has_type = bool(flags.get("type"))
    has_description = bool(flags.get("description"))
    user_facing_value = flags.get("user-facing")  # None means not set
    has_user_facing_change = user_facing_value is not None
    if not (has_type or has_description or has_user_facing_change):
        print(
            "Error: at least one edit flag is required "
            "(--type, --description, --user-facing, --no-user-facing).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Parse selection criteria: --id or --commits
    id_filter = flags.get("id") or None
    commits_raw = flags.get("commits", "")

    if not id_filter and not commits_raw:
        print("Error: --commits or --id is required.", file=sys.stderr)
        sys.exit(1)

    search_set = set()
    if commits_raw:
        commits = [h.strip() for h in commits_raw.split(",") if h.strip()]
        if not commits:
            print("Error: --commits must contain at least one hash.", file=sys.stderr)
            sys.exit(1)

        resolved_search = []
        for h in commits:
            full = resolve_hash(h)
            if full is None:
                print(f"Error: commit hash does not resolve: {h}", file=sys.stderr)
                sys.exit(1)
            resolved_search.append(full)
        search_set = set(resolved_search)

    # Search for matching entries across all JSONL files
    changes_dir = _resolve_changes_dir(ws_context, project_root)
    matches = []  # list of (file_path, line_index, entry, version_or_none)

    def _entry_matches(entry):
        if id_filter and entry.id == id_filter:
            return True
        if search_set and search_set.intersection(entry.commits):
            return True
        return False

    # Search unreleased.jsonl first
    unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
    if os.path.isfile(unreleased_path):
        entries = parse_jsonl(unreleased_path)
        for idx, entry in enumerate(entries):
            if _entry_matches(entry):
                matches.append((unreleased_path, idx, entry, None))

    # Then search versioned files (newest first)
    for version, jsonl_path in list_versioned_files(changes_dir):
        entries = parse_jsonl(jsonl_path)
        for idx, entry in enumerate(entries):
            if _entry_matches(entry):
                matches.append((jsonl_path, idx, entry, version))

    if not matches:
        if commits_raw:
            short_hashes = ", ".join(h[:12] for h in resolved_search)
            print(
                f"Error: No changelog entry found for commit(s): {short_hashes}",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: No changelog entry found for id: {id_filter}",
                file=sys.stderr,
            )
        sys.exit(1)

    # Disambiguate if multiple matches
    if len(matches) > 1:
        type_filter = flags.get("type") or None
        if type_filter:
            filtered = [m for m in matches if m[2].type == type_filter]
            if len(filtered) == 0:
                print(
                    f"Error: No entry with type '{type_filter}' found for these commits.",
                    file=sys.stderr,
                )
                sys.exit(1)
            elif len(filtered) > 1:
                print("Error: Multiple entries match even after type filter:", file=sys.stderr)
                for fp, _idx, ent, ver in filtered:
                    loc = f"v{ver}" if ver else "unreleased"
                    desc = ent.description or "(no description)"
                    print(f"  [{loc}] type={ent.type}: {desc}", file=sys.stderr)
                sys.exit(1)
            matches = filtered
        else:
            print("Error: Multiple entries match -- use --type to disambiguate:", file=sys.stderr)
            for fp, _idx, ent, ver in matches:
                loc = f"v{ver}" if ver else "unreleased"
                desc = ent.description or "(no description)"
                print(f"  [{loc}] type={ent.type}: {desc}", file=sys.stderr)
            sys.exit(1)

    file_path, line_index, entry, version = matches[0]
    is_released = version is not None

    # Apply edits
    if user_facing_value is False:
        entry.user_facing = False
        entry.description = None
        entry.type = None
    elif user_facing_value is True:
        entry.user_facing = True
        # If the entry doesn't already have description/type, require them
        if not entry.description and not has_description:
            print(
                "Error: --description is required when setting --user-facing "
                "on an entry without an existing description.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not entry.type and not has_type:
            print(
                "Error: --type is required when setting --user-facing "
                "on an entry without an existing type.",
                file=sys.stderr,
            )
            sys.exit(1)

    if has_type and user_facing_value is not False:
        entry.type = flags["type"]
    if has_description and user_facing_value is not False:
        entry.description = flags["description"]

    # Validate the edited entry
    errors = validate_schema(entry)
    if errors:
        for err in errors:
            print(f"Error: schema validation: {err}", file=sys.stderr)
        sys.exit(1)

    if flags.get("dry-run", False):
        print(serialize_entry(entry))
        print("(dry-run: no files written)")
        return

    # Rewrite the file atomically
    def _rewrite_file(target_path):
        all_entries = parse_jsonl(target_path)
        all_entries[line_index] = entry
        lines = [serialize_entry(e) + "\n" for e in all_entries]
        content = "".join(lines)
        parent = os.path.dirname(target_path)
        fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp_path, target_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    if is_released:
        with writable_jsonl(file_path):
            _rewrite_file(file_path)

        # Regenerate CHANGELOG.md at the canonical home (releasable-aware)
        changelog_outputs = _regenerate_changelog_outputs(
            ws_context, project_root, changes_dir,
        )
        print(f"Edited entry in {version}.jsonl")
        print("Regenerated CHANGELOG.md")

        # Sync GitHub Release notes (best-effort)
        _sync_github_release(version)

        # Auto-commit
        if flags.get("auto-commit", True):
            changed_files = [file_path]
            md_path = os.path.join(changes_dir, f"{version}.md")
            if os.path.isfile(md_path):
                changed_files.append(md_path)
            for changelog_path in changelog_outputs:
                if os.path.isfile(changelog_path):
                    changed_files.append(changelog_path)
            desc = entry.description or "entry"
            commit_files(f"changelog: edit {version}: {desc}", changed_files, allow_failure=True)
    else:
        _rewrite_file(file_path)
        print("Edited entry in unreleased.jsonl")

        # Auto-commit
        if flags.get("auto-commit", True):
            desc = entry.description or "entry"
            commit_files(f"changelog: edit unreleased: {desc}", [file_path], allow_failure=True)


def _parse_sha_map_lines(lines):
    """Parse ``old_sha new_sha`` lines into a dict.

    Accepts the format git's post-rewrite hook emits: each line is
    ``<old-sha> <new-sha>`` (optionally followed by extra fields which
    are ignored). Blank lines and lines starting with ``#`` are skipped.
    Returns ``{old_sha: new_sha}``.
    """
    sha_map = {}
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            print(
                f"Warning: ignoring malformed line {lineno}: {stripped!r}",
                file=sys.stderr,
            )
            continue
        sha_map[parts[0]] = parts[1]
    return sha_map


def cmd_remap(flags, project_root):
    """Remap stale commit hashes in all JSONL changelog files.

    Reads a mapping of old-SHA to new-SHA from one of three sources
    (``--map-file``, ``--from-journal``, ``--stdin``) and applies it to
    every JSONL file in the project's (or monorepo's) changelog dirs.

    At least one source is required; no source is a hard error.
    Auto-commits with ``Autogenerated: true`` trailer.
    """
    map_file = flags.get("map-file") or None
    from_journal = flags.get("from-journal", False)
    from_stdin = flags.get("stdin", False)
    dry_run = flags.get("dry-run", False)

    sources = sum([bool(map_file), from_journal, from_stdin])
    if sources == 0:
        print(
            "Error: at least one map source is required: "
            "--map-file, --from-journal, or --stdin.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build the sha_map from the selected source(s). Multiple sources
    # are merged (later source wins on conflict), but typically only one
    # is used.
    sha_map = {}

    if map_file:
        if not os.path.isfile(map_file):
            print(f"Error: map file not found: {map_file}", file=sys.stderr)
            sys.exit(1)
        with open(map_file, "r", encoding="utf-8") as f:
            sha_map.update(_parse_sha_map_lines(f.readlines()))
        if not sha_map:
            print("Error: map file contains no valid mappings.", file=sys.stderr)
            sys.exit(1)

    if from_journal:
        from ..commands.release_scrub import _load_rewrite_journal

        journal = _load_rewrite_journal()
        if journal is None:
            print(
                "Error: no safegit rewrite journal found "
                "(.git/safegit/rewrite-maps.jsonl).",
                file=sys.stderr,
            )
            sys.exit(1)
        commit_map = journal.get("commit_map", {})
        if not commit_map:
            print("Error: rewrite journal contains no commit mappings.", file=sys.stderr)
            sys.exit(1)
        sha_map.update(commit_map)

    if from_stdin:
        stdin_lines = sys.stdin.readlines()
        stdin_map = _parse_sha_map_lines(stdin_lines)
        if not stdin_map:
            print("Error: stdin contains no valid mappings.", file=sys.stderr)
            sys.exit(1)
        sha_map.update(stdin_map)

    if not sha_map:
        print("Error: no mappings found from any source.", file=sys.stderr)
        sys.exit(1)

    # Enumerate all changelog dirs (same enumeration as validation).
    ws_root = find_workspace_root(str(project_root))
    all_dirs = enumerate_changelog_dirs(
        str(project_root),
        workspace_root=ws_root,
    )

    if not all_dirs:
        print("No changelog directories found.", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"Would remap {len(sha_map)} hash(es) across {len(all_dirs)} changelog dir(s)")
        print("(dry-run: no files written)")
        return

    # Apply the remap to every changelog dir.
    all_modified = []
    total_entries = 0
    total_hashes = 0
    all_unmapped = {}
    all_ambiguous = {}

    for changes_dir in all_dirs:
        report = remap_jsonl_hashes(changes_dir, sha_map)
        for result in report.results:
            all_modified.append(result.path)
            total_entries += result.entries_modified
            total_hashes += result.hashes_remapped
        all_unmapped.update(report.unmapped)
        all_ambiguous.update(report.ambiguous)

    # Report warnings for unmapped/ambiguous hashes.
    for filepath, hashes in all_unmapped.items():
        for h in hashes:
            print(f"Warning: unmapped hash in {filepath}: {h}", file=sys.stderr)
    for filepath, hashes in all_ambiguous.items():
        for h in hashes:
            print(f"Warning: ambiguous hash in {filepath}: {h}", file=sys.stderr)

    if not all_modified:
        print("No hashes matched -- nothing to remap.")
        return

    print(
        f"Remapped {total_hashes} hash(es) in {total_entries} "
        f"entr{'y' if total_entries == 1 else 'ies'} across "
        f"{len(all_modified)} file(s)."
    )

    # Auto-commit with Autogenerated trailer.
    commit_files(
        "changelog: remap stale commit hashes",
        all_modified,
        allow_failure=True,
        autogenerated=True,
    )


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


def _filter_dirty_files(paths: list[str], repo_root: str) -> list[str]:
    """Return the subset of *paths* that git reports as modified/untracked.

    Paths are returned as absolute paths. Used by releasable-mode changelog
    generation where the generated files live outside the member project.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", *paths],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_root,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    dirty = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        filepath = line[3:].strip()
        dirty.append(os.path.join(repo_root, filepath))
    return dirty


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
