"""Changelog subcommands for adding new entries, amending released versions, and generating Markdown changelogs from JSONL sources."""

import json
import os
import subprocess
import sys

from ..changelog.files import (
    NULL_SHA,
    append_entry,
    append_entry_to_version,
    enumerate_changelog_dirs,
    get_changes_dir,
    list_versioned_files,
    read_unreleased,
    remap_jsonl_hashes,
    writable_jsonl,
)
from ..changelog.generate import generate_changelog
from ..changelog.resolve import resolve_hash
from ..changelog.schema import ChangelogEntry, generate_entry_id, parse_jsonl, serialize_entry, validate_schema
from ..changelog.validate import _get_batch_limits_config
from ..config import read_project_config
from ..git_util import filter_commits_for_scope
from ..ownership import OwnershipError, OwnershipScope, releasable_state_dir
from ..utils import commit_files, run, working_tree_paths
from ..workspace import (
    find_workspace_root,
    get_releasable_changes_dir,
    get_releasable_dir,
    load_releasables,
    load_workspace,
    members_of,
    resolve_project,
    resolve_releasable_for_project,
)
from .. import effects


class _ResolvedContext:
    """Carries project, releasable, and workspace info for changelog commands.

    ``all_projects`` is mandatory and is every member of the workspace, not
    just this releasable's: file attribution is decided against the whole list
    (see :mod:`rlsbl.ownership`), so a context that carries only the members it
    cares about would answer "who owns this file?" with the wrong member.
    """

    def __init__(self, project, all_projects, releasable=None, ws_root=None,
                 member_projects=None):
        self.project = project
        self.releasable = releasable
        self.ws_root = ws_root
        self.member_projects = member_projects or []
        self.all_projects = list(all_projects)

    def scope(self):
        """The ownership scope this changelog covers.

        A releasable also claims its own state directory
        (``.rlsbl-monorepo/releasables/<name>/``): a commit that archives its
        release file or finalizes its changelog is about that releasable, and
        belongs to no member at all.
        """
        if not self.all_projects:
            raise OwnershipError(
                "changelog scope was asked for without a workspace member "
                "list. Attribution needs every member to answer at all -- a "
                "file under a nested member belongs to that member, whichever "
                "members the caller cares about -- so there is no answer to "
                "give here, silently narrowed or otherwise."
            )
        in_scope = self.member_projects or ([self.project] if self.project else [])
        if self.releasable is not None:
            return OwnershipScope.for_releasable(
                self.all_projects, in_scope, self.releasable.name,
            )
        return OwnershipScope.for_members(self.all_projects, in_scope)

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

    # Resolve the releasable this member belongs to.
    projects = load_workspace(ws_root)
    member_projects = []
    releasables = load_releasables(ws_root, projects=projects)
    releasable = resolve_releasable_for_project(project, releasables)
    if releasable is not None:
        member_projects = members_of(releasable.name, projects)

    return _ResolvedContext(
        project=project,
        releasable=releasable,
        ws_root=ws_root,
        member_projects=member_projects,
        all_projects=projects,
    )


def _check_project_scope(resolved_commits, ws_context):
    """Verify all commits touch something this changelog's scope claims.

    Hard error if any commit touches nothing in scope.  Member ownership is
    single-owner: a commit touching only another member's directory belongs in
    that member's changelog, and a commit touching only root files belongs in
    the root member's.  A releasable's scope additionally claims its own state
    directory, which belongs to no member, so a commit that only archives its
    release file or finalizes its changelog is in scope for it.

    *ws_context* is what :func:`_resolve_workspace_project` returns: a
    :class:`_ResolvedContext` carrying the whole member list, or ``None`` in
    standalone mode, where there is no scope to check.
    """
    if ws_context is None:
        return

    scope = ws_context.scope()
    if ws_context.releasable is not None:
        subject = f"releasable '{ws_context.releasable.name}'"
        # Named only for a releasable subject: a scope with no releasable
        # claims no state directory, and saying otherwise would describe a
        # scope wider than the one that just refused the commit.
        state_claim = (
            f" A releasable's changelog covers its members' files AND its own "
            f"state directory "
            f"({releasable_state_dir(ws_context.releasable.name)}/), which "
            f"belongs to no member."
        )
    else:
        project = ws_context.project
        subject = (
            f"project '{project.get('name', 'unknown')}' "
            f"(path: {project.get('path', 'unknown')})"
        )
        state_claim = ""

    in_scope = filter_commits_for_scope(
        set(resolved_commits), scope, operation="changelog add scope check",
    )
    for sha in resolved_commits:
        if sha not in in_scope:
            print(
                f"Error: commit {sha[:12]} touches nothing {subject} covers. "
                f"Every file belongs to exactly one workspace member: the "
                f"most specific declared path in workspace.toml wins, and the "
                f"root member owns whatever no other member claims."
                f"{state_claim} Add the entry from the owning member's "
                f"directory instead.",
                file=sys.stderr,
            )
            sys.exit(1)


def _entry_ref(entry, ordinal):
    """Human-readable reference to an existing entry.

    Prefers the entry's stable ULID ``id`` (survives unrelated edits to the
    file). Legacy entries without an id fall back to a 1-based ordinal, noted
    explicitly so the reader knows it is positional and unstable.
    """
    if entry.id:
        return f"entry {entry.id}"
    return f"legacy entry #{ordinal}"


def _check_duplicate_commits(existing_entries, new_entry):
    """Check if any commits in new_entry already appear in existing entries.

    Hard error (nothing is written, the process exits) when a commit appears
    in an existing entry with the SAME user_facing value and type. Allowed
    (the new entry IS written) when the type/user_facing differ: one commit
    may legitimately carry, say, both a feature and a fix -- validation bounds
    this via max_entries_per_commit.

    Existing entries are named by their stable ULID id (see ``_entry_ref``) so
    the message stays valid across unrelated edits to the file.
    """
    for new_hash in new_entry.commits:
        for i, existing in enumerate(existing_entries, start=1):
            if new_hash in existing.commits:
                short = new_hash[:12]
                ref = _entry_ref(existing, i)
                desc = existing.description or "(no description)"
                if (existing.user_facing == new_entry.user_facing
                        and existing.type == new_entry.type):
                    print(
                        f"Error: commit {short} is already covered by {ref}: "
                        f"{desc}. Nothing was written. Edit the existing entry "
                        f"with `rlsbl changelog edit`, or pass a different "
                        f"--type if this is genuinely a different kind of "
                        f"change.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                else:
                    print(
                        f"Warning: commit {short} also appears in {ref}: "
                        f"{desc}. This entry WAS still written -- the same "
                        f"commit may carry distinct changelog types (e.g. a "
                        f"feature and a fix), so this duplicate is legal. "
                        f"max_entries_per_commit bounds this at validation.",
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


def _entry_is_committed(entry, unreleased_path):
    """True when HEAD's copy of ``unreleased_path`` already carries ``entry``.

    The authority on whether an entry was recorded is git, not the commit tool's
    exit status.  Two ``changelog add`` runs at once append safely -- the append
    never reads the file back -- but their commits collide: whichever stages
    first carries BOTH lines, and the loser's commit then finds nothing left to
    stage and exits non-zero even though its entry is safely in the tree.

    A repository git cannot answer for (no HEAD yet, the file untracked at HEAD)
    reads as not committed, which is the conservative direction: the caller
    retries and then says so.
    """
    directory = os.path.dirname(unreleased_path) or "."
    try:
        top = run("git", ["rev-parse", "--show-toplevel"], cwd=directory)
        rel = os.path.relpath(os.path.abspath(unreleased_path), top)
        blob = run("git", ["show", f"HEAD:{rel}"], cwd=directory)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    return f'"{entry.id}"' in blob


def _commit_appended_entry(entry, unreleased_path, commit_msg):
    """Commit the just-appended entry, judging the outcome by git.

    Losing the commit race is benign and is reported as what it is; an entry
    that no commit carries is a hard error, because "added" was printed and the
    file is one tree-cleaning step away from losing it silently.

    A commit the tool reports as made is taken at its word, as is the no-op it
    reports when the named file already matches HEAD -- that state means HEAD's
    copy holds this entry.  Only a REPORTED failure is put to git, and only then
    is a retry spent.
    """
    for attempt in (1, 2):
        if commit_files(commit_msg, [unreleased_path], allow_failure=True):
            return
        if _entry_is_committed(entry, unreleased_path):
            print(
                "The entry is recorded even so: a concurrent `changelog add` "
                "committed the file first, carrying this entry with it."
            )
            return
        if attempt == 1:
            print(
                "Warning: this run's commit did not record the entry; retrying "
                "once (a concurrent `changelog add` may hold the commit lock).",
                file=sys.stderr,
            )
    print(
        f"Error: the entry was appended to {unreleased_path} but no commit "
        f"carries it.\n"
        f"The file itself is correct -- commit it with:\n"
        f'  safegit commit -m "{commit_msg}" -- {unreleased_path}',
        file=sys.stderr,
    )
    sys.exit(1)


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


def _derive_packages_from_commits(resolved_commits, scope):
    """Derive the list of affected package names from commit file paths.

    Single-owner attribution: each changed file names exactly one member, so a
    commit that touches ``pkg/inner`` no longer claims ``pkg`` as well.  Only
    members inside *scope* are reported -- another releasable's packages never
    leak into this changelog -- and the result is sorted and deduplicated, or
    ``None`` when nothing in scope was touched.

    The narrowing is deliberate and only affects what is *derived*: a manual
    broadening via ``rlsbl changelog edit`` stays exactly as written.
    """
    if scope is None or not scope.owned:
        return None
    from ..git_util import commit_owner_names

    affected = set()
    for sha in resolved_commits:
        owners = commit_owner_names(
            sha, scope.members, operation="changelog add packages derivation",
        )
        affected.update(owners & scope.owned)
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
            resolved_commits, ws_context.scope(),
        )
        if packages:
            entry.packages = packages


def cmd_add(flags, project_root):
    """Add a changelog entry.

    Appends to unreleased.jsonl. Required flags: --commits, --description,
    --type (the latter two unless --no-user-facing).

    Under --dry-run, all validation runs but nothing is written.
    """
    ws_context = _resolve_workspace_project(project_root)
    dry_run = flags.get("dry-run", False)

    releasable_config_dir = None
    if (isinstance(ws_context, _ResolvedContext)
            and ws_context.releasable is not None
            and ws_context.ws_root is not None):
        releasable_config_dir = get_releasable_dir(
            ws_context.ws_root, ws_context.releasable.name,
        )
    config = read_project_config(project_root, releasable_config_dir=releasable_config_dir)

    return _cmd_add_commit(flags, project_root, ws_context, config, dry_run)


def _cmd_add_commit(flags, project_root, ws_context, config, dry_run):
    """Add a changelog entry to unreleased.jsonl."""
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
            with effects.open_write(config_path, "w", encoding="utf-8") as f:
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
        _commit_appended_entry(entry, unreleased_path, commit_msg)



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
            generate_version_section,
            read_archive_metadata,
        )

        sections = []

        unreleased = read_unreleased(changes_dir)
        if unreleased:
            sections.append(generate_version_section("Unreleased", unreleased))

        for version, jsonl_path in list_versioned_files(changes_dir):
            from ..changelog.schema import parse_jsonl

            entries = parse_jsonl(jsonl_path)
            meta = read_archive_metadata(
                project_root, version, releases_dir=releases_dir_override,
            )
            sections.append(generate_version_section(
                version, entries, description=meta.description,
                context=meta.context, bump_type=meta.bump or None,
                never_released=meta.never_released,
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


def _resolve_selector_commits(commits_raw):
    """Resolve a ``--commits`` selector string to the full SHAs it names.

    Shared by ``changelog edit`` and ``changelog remove``: both address an
    existing entry by the commits it covers, and both must resolve an
    abbreviated hash the same way before comparing it against what the JSONL
    stores (always full SHAs).
    """
    commits = [h.strip() for h in commits_raw.split(",") if h.strip()]
    if not commits:
        print("Error: --commits must contain at least one hash.", file=sys.stderr)
        sys.exit(1)

    resolved = []
    for h in commits:
        full = resolve_hash(h)
        if full is None:
            print(f"Error: commit hash does not resolve: {h}", file=sys.stderr)
            sys.exit(1)
        resolved.append(full)
    return resolved


def _find_entry_matches(changes_dir, *, id_filter=None, search_set=frozenset()):
    """Every entry an ``--id`` or ``--commits`` selector addresses.

    Returns a list of ``(file_path, line_index, entry, version_or_none)``,
    unreleased.jsonl first and then the versioned files newest-first, so a
    caller reporting several matches lists them in a stable order. ``version``
    is ``None`` for the unreleased file and the bare semver otherwise.
    """
    matches = []

    def _entry_matches(entry):
        if id_filter and entry.id == id_filter:
            return True
        if search_set and search_set.intersection(entry.commits):
            return True
        return False

    unreleased_path = os.path.join(changes_dir, "unreleased.jsonl")
    if os.path.isfile(unreleased_path):
        for idx, entry in enumerate(parse_jsonl(unreleased_path)):
            if _entry_matches(entry):
                matches.append((unreleased_path, idx, entry, None))

    for version, jsonl_path in list_versioned_files(changes_dir):
        for idx, entry in enumerate(parse_jsonl(jsonl_path)):
            if _entry_matches(entry):
                matches.append((jsonl_path, idx, entry, version))

    return matches


def _selector_description(id_filter, resolved_search):
    """How the selector that produced a match set is named in a message."""
    if resolved_search:
        return "--commits " + ", ".join(h[:12] for h in resolved_search)
    return f"--id {id_filter}"


def _refuse_no_match(id_filter, resolved_search):
    """Exit naming the selector that addressed no entry at all."""
    if resolved_search:
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


def _rewrite_entries(target_path, entries):
    """Atomically rewrite a JSONL file to hold exactly *entries*.

    ``file_mode`` pins the 0o600 the mkstemp-based hand-rolled write produced;
    a released file is relocked by :func:`writable_jsonl` around the call.
    """
    content = "".join(serialize_entry(e) + "\n" for e in entries)
    effects.atomic_write_text(target_path, content, file_mode=0o600)


def _finish_released_write(ws_context, project_root, changes_dir, file_path,
                           version, *, message, auto_commit, commit_msg):
    """The tail every write to a RELEASED version's JSONL shares.

    Regenerates CHANGELOG.md at its canonical home, re-syncs that version's
    GitHub Release notes, and commits the JSONL plus everything the
    regeneration touched. ``message`` is the one line naming what just happened
    to the JSONL, printed ahead of the regeneration line.
    """
    changelog_outputs = _regenerate_changelog_outputs(
        ws_context, project_root, changes_dir,
    )
    print(message)
    print("Regenerated CHANGELOG.md")

    # Sync GitHub Release notes (best-effort)
    _sync_github_release(version)

    if auto_commit:
        changed_files = [file_path]
        md_path = os.path.join(changes_dir, f"{version}.md")
        if os.path.isfile(md_path):
            changed_files.append(md_path)
        for changelog_path in changelog_outputs:
            if os.path.isfile(changelog_path):
                changed_files.append(changelog_path)
        commit_files(commit_msg, changed_files, allow_failure=True)


def cmd_edit(flags, project_root):
    """Edit an existing changelog entry in unreleased or released JSONL files.

    Finds the entry by commit hash, applies field changes, and rewrites
    the file atomically. For released files, temporarily unlocks the
    read-only file, regenerates CHANGELOG.md, and syncs GitHub Release notes.

    This is a sparse update of one changelog entry, and the CLI declares it as
    one (``update_of("changelog-entry", write_mode="sparse")``). Two rules that
    used to be hand-rolled here are the framework's now and are NOT re-checked:

    - **at least one property** (``--description`` / ``--type`` /
      ``--user-facing``) is refused at parse time by the update declaration;
    - **at least one identity member** (``--commits`` / ``--id``) is refused by
      the ``entry-selection`` at-least-one constraint.

    ``unset-type`` / ``unset-description`` carry ``ctx.unset(...)``: a cleared
    property is a WRITE of absence, which is why it cannot be read off the
    value (an untouched property delivers the same None).

    Under --dry-run, all validation and entry matching runs but nothing is
    written: no file rewrite, no CHANGELOG.md regeneration, no GitHub
    Release sync, no commit.
    """
    ws_context = _resolve_workspace_project(project_root)

    unset_type = bool(flags.get("unset-type"))
    unset_description = bool(flags.get("unset-description"))
    user_facing_value = flags.get("user-facing")  # None means not written

    # Selection criteria: --id or --commits (the constraint guarantees one).
    id_filter = flags.get("id") or None
    commits_raw = flags.get("commits") or ""

    resolved_search = _resolve_selector_commits(commits_raw) if commits_raw else []

    # Search for matching entries across all JSONL files
    changes_dir = _resolve_changes_dir(ws_context, project_root)
    matches = _find_entry_matches(
        changes_dir, id_filter=id_filter, search_set=set(resolved_search),
    )

    if not matches:
        _refuse_no_match(id_filter, resolved_search)

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
    writes_type = bool(flags.get("type"))
    writes_description = bool(flags.get("description"))
    if user_facing_value is False:
        # Flip the flag. description/type stay on the line unless the
        # invocation CLEARS them: they are unused while the entry is
        # non-user-facing (generation filters on user_facing) and survive a
        # later flip back, so removing them has to be asked for.
        entry.user_facing = False
    elif user_facing_value is True:
        entry.user_facing = True
        # If the entry doesn't already have description/type, require them
        if not entry.description and not writes_description:
            print(
                "Error: --description is required when setting --user-facing "
                "on an entry without an existing description.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not entry.type and not writes_type:
            print(
                "Error: --type is required when setting --user-facing "
                "on an entry without an existing type.",
                file=sys.stderr,
            )
            sys.exit(1)

    # A clear is a write of absence and is honored whatever --user-facing did;
    # a value write is suppressed when the same invocation flips the entry to
    # non-user-facing, where the value would have no reader.
    if unset_type:
        entry.type = None
    elif writes_type and user_facing_value is not False:
        entry.type = flags["type"]
    if unset_description:
        entry.description = None
    elif writes_description and user_facing_value is not False:
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
        _rewrite_entries(target_path, all_entries)

    if is_released:
        with writable_jsonl(file_path):
            _rewrite_file(file_path)

        desc = entry.description or "entry"
        _finish_released_write(
            ws_context,
            project_root,
            changes_dir,
            file_path,
            version,
            message=f"Edited entry in {version}.jsonl",
            auto_commit=flags.get("auto-commit", True),
            commit_msg=f"changelog: edit {version}: {desc}",
        )
    else:
        _rewrite_file(file_path)
        print("Edited entry in unreleased.jsonl")

        # Auto-commit
        if flags.get("auto-commit", True):
            desc = entry.description or "entry"
            commit_files(f"changelog: edit unreleased: {desc}", [file_path], allow_failure=True)


def cmd_remove(flags, project_root):
    """Remove one changelog entry from unreleased.jsonl or a released JSONL file.

    Selection is the same as ``changelog edit``'s -- ``--id`` or ``--commits``,
    resolved by :func:`_find_entry_matches` -- with one deliberate difference:
    ``edit`` disambiguates several matches with ``--type``, while a removal
    refuses them. Deleting the wrong line is not correctable by re-running with
    a better flag, so the ambiguity is reported with every match named and
    nothing is written.

    The file is rewritten atomically without the removed line. A released file
    goes through the unlock/relock flow, regenerates CHANGELOG.md and re-syncs
    that version's GitHub Release notes, exactly as ``amend`` and ``edit`` do.

    Under --dry-run the entry is located and printed but nothing is written: no
    file rewrite, no CHANGELOG.md regeneration, no GitHub Release sync, no
    commit.
    """
    ws_context = _resolve_workspace_project(project_root)

    id_filter = flags.get("id") or None
    commits_raw = flags.get("commits") or ""
    resolved_search = _resolve_selector_commits(commits_raw) if commits_raw else []

    changes_dir = _resolve_changes_dir(ws_context, project_root)
    matches = _find_entry_matches(
        changes_dir, id_filter=id_filter, search_set=set(resolved_search),
    )

    if not matches:
        _refuse_no_match(id_filter, resolved_search)

    if len(matches) > 1:
        print(
            f"Error: {_selector_description(id_filter, resolved_search)} "
            f"selects {len(matches)} entries, and a removal deletes exactly "
            f"one. Nothing was written. The matches are:",
            file=sys.stderr,
        )
        for _fp, idx, ent, ver in matches:
            loc = f"v{ver}" if ver else "unreleased"
            desc = ent.description or "(no description)"
            print(
                f"  [{loc}] {_entry_ref(ent, idx + 1)} type={ent.type}: {desc}",
                file=sys.stderr,
            )
        print(
            "Name one of them: `rlsbl changelog remove --id <id>`.",
            file=sys.stderr,
        )
        sys.exit(1)

    file_path, line_index, entry, version = matches[0]
    is_released = version is not None
    location = f"{version}.jsonl" if is_released else "unreleased.jsonl"

    if flags.get("dry-run", False):
        print(f"Would remove from {location}:")
        print(serialize_entry(entry))
        print("(dry-run: no files written)")
        return

    def _remove_line(target_path):
        all_entries = parse_jsonl(target_path)
        del all_entries[line_index]
        _rewrite_entries(target_path, all_entries)

    desc = entry.description or "entry"
    auto_commit = flags.get("auto-commit", True)

    if is_released:
        with writable_jsonl(file_path):
            _remove_line(file_path)
        _finish_released_write(
            ws_context,
            project_root,
            changes_dir,
            file_path,
            version,
            message=f"Removed entry from {location}",
            auto_commit=auto_commit,
            commit_msg=f"changelog: remove from {version}: {desc}",
        )
    else:
        _remove_line(file_path)
        print(f"Removed entry from {location}")
        if auto_commit:
            commit_files(
                f"changelog: remove from unreleased: {desc}",
                [file_path],
                allow_failure=True,
            )


def _parse_sha_map_lines(lines):
    """Parse ``old_sha new_sha`` lines into a dict.

    Accepts the format git's post-rewrite hook emits: each line is
    ``<old-sha> <new-sha>`` (optionally followed by extra fields which
    are ignored). Blank lines and lines starting with ``#`` are skipped.
    Returns ``{old_sha: new_sha}``.

    Hardened against being fed a raw git-filter-repo ``commit-map`` directly:

    - The literal ``old new`` header row git-filter-repo writes is skipped
      rather than ingested as a junk ``{"old": "new"}`` mapping.
    - Rows whose target is the all-zeros null SHA (git-filter-repo's marker
      for a pruned commit) are dropped with a warning. Keeping them would let
      a real hash be rewritten to nothing, corrupting the changelog entry.
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
        old, new = parts[0], parts[1]
        if old == "old" and new == "new":
            continue  # git-filter-repo commit-map header row
        if new == NULL_SHA:
            print(
                f"Warning: ignoring line {lineno}: {old} maps to the null SHA "
                f"(pruned commit); refusing to rewrite a real hash to nothing.",
                file=sys.stderr,
            )
            continue
        sha_map[old] = new
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
        result = effects.run(
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

    ``untracked="all"`` so a brand-new generated file inside a wholly untracked
    directory is reported by name: git's default collapses such a directory
    into one record, which names no file to commit.
    """
    try:
        dirty = working_tree_paths(cwd=repo_root, paths=paths, untracked="all")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    return [os.path.join(repo_root, filepath) for filepath in dirty]


def _get_generated_files(project_path: str) -> list[str]:
    """Return paths of files modified or created by generate_changelog.

    Checks git status for CHANGELOG.md and .rlsbl/changes/*.md files.
    ``untracked="all"`` for the same reason as :func:`_filter_dirty_files`:
    on a project whose ``.rlsbl/changes/`` is not tracked yet, the default
    output names only the directory, and every generated ``.md`` inside it
    would go uncommitted.
    """
    try:
        dirty = working_tree_paths(untracked="all")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    files = []
    for filepath in dirty:
        # Match CHANGELOG.md or .rlsbl/changes/*.md
        if filepath == "CHANGELOG.md":
            files.append(os.path.join(project_path, filepath))
        elif filepath.startswith(".rlsbl/changes/") and filepath.endswith(".md"):
            files.append(os.path.join(project_path, filepath))
    return files
