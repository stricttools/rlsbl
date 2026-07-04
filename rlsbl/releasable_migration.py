"""Migration tooling for transitioning monorepos to the releasable model, moving packages into releasable groups with changelog and config consolidation.

Provides functions for Phase 10 of the releasable model redesign:

- ``detect_migration_state()`` -- analyze workspace readiness for migration
- ``consolidate_changelogs()`` -- merge per-package changelogs into per-releasable
- ``consolidate_versions()`` -- write releasable version from member versions
- ``create_migration_tag()`` -- create releasable-format tag from last per-package tags
"""

import json
import logging
import os
import subprocess

from .changelog.files import get_changes_dir, list_versioned_files, read_unreleased
from .changelog.generate import generate_version_file, _read_release_metadata
from .changelog.schema import ChangelogEntry, parse_jsonl, serialize_entry
from .config import read_json_config
from .errors import WorkspaceError
from .git_util import get_commit_files, file_matches_project
from .targets import detect_targets, TARGETS
from .workspace import (
    get_releasable_changes_dir,
    get_releasable_dir,
    is_explicit_mode,
    load_releasables,
    load_workspace,
    members_of,
    write_releasable_version,
)


# ---------------------------------------------------------------------------
# 10.1: detect_migration_state
# ---------------------------------------------------------------------------


def detect_migration_state(workspace_root):
    """Analyze current workspace and report migration readiness.

    Loads workspace.toml, checks if already in explicit mode, and for each
    non-dev_node project reports version, changelog presence, and entry count.
    Also suggests groupings for projects sharing the same version.

    Args:
        workspace_root: path to the monorepo root.

    Returns:
        A dict with keys:
            - ``explicit_mode`` (bool): whether [[releasables]] already exists
            - ``projects`` (list[dict]): per-project migration info, each with:
                - ``name`` (str)
                - ``path`` (str)
                - ``version`` (str or None)
                - ``has_changelog`` (bool)
                - ``unreleased_entry_count`` (int)
                - ``versioned_file_count`` (int)
                - ``dev_node`` (bool)
                - ``releasable`` (str, False, or None)
            - ``suggested_groupings`` (dict[str, list[str]]): version string
              to list of project names that share it
    """
    projects = load_workspace(workspace_root)
    explicit = is_explicit_mode(workspace_root)

    project_reports = []
    version_groups = {}

    for proj in projects:
        proj_path = os.path.join(workspace_root, proj.path)
        info = {
            "name": proj.name,
            "path": proj.path,
            "version": None,
            "has_changelog": False,
            "unreleased_entry_count": 0,
            "versioned_file_count": 0,
            "dev_node": proj.dev_node,
            "releasable": proj.releasable,
        }

        # Detect version from target manifests
        version = _read_project_version(proj_path)
        info["version"] = version

        # Check per-package changelog
        changes_dir = get_changes_dir(proj_path)
        if os.path.isdir(changes_dir):
            info["has_changelog"] = True
            try:
                entries = read_unreleased(changes_dir)
                info["unreleased_entry_count"] = len(entries)
            except Exception:
                pass
            info["versioned_file_count"] = len(list_versioned_files(changes_dir))

        project_reports.append(info)

        # Track version groupings for non-dev_node projects
        if not proj.dev_node and version is not None:
            version_groups.setdefault(version, []).append(proj.name)

    # Only suggest groupings where multiple projects share a version
    suggested = {v: names for v, names in version_groups.items() if len(names) > 1}

    return {
        "explicit_mode": explicit,
        "projects": project_reports,
        "suggested_groupings": suggested,
    }


def _read_project_version(project_path):
    """Read version from a project's target manifest.

    Returns the version string, or None if no target is detected or
    version cannot be read.
    """
    targets = detect_targets(project_path)
    if not targets:
        return None
    # Use the first detected target
    target_name = targets[0].name
    target_path = targets[0].path
    target = TARGETS.get(target_name)
    if target is None:
        return None
    try:
        return target.read_version(target_path)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 10.2: consolidate_changelogs
# ---------------------------------------------------------------------------


def consolidate_changelogs(workspace_root, releasable_name, member_projects,
                           *, tag_format=None, version=None):
    """Merge member packages' unreleased.jsonl into per-releasable changelog.

    For each member project, reads their per-package unreleased.jsonl and
    merges all entries into the releasable's changes directory. Each entry
    gains a ``packages`` field listing which member packages are affected
    (derived from commit file paths via project path/watch matching).

    Cross-package dedup: entries from different packages that reference the
    exact same set of commits are merged into a single entry, combining
    their ``packages`` lists. This prevents the same commit from appearing
    in too many entries and violating ``max_entries_per_commit``.

    Batch limit exclusions: per-package ``batch_limits.exclusions`` from
    each member's ``.rlsbl/config.json`` are collected. After merging, any
    consolidated entry that exceeds ``max_commits_per_entry`` (default 5)
    gets a new exclusion auto-created in the releasable's config.

    Consolidation-point tag: when ``tag_format`` and ``version`` are
    provided, a tag is created at HEAD after writing the merged file. This
    resets the unreleased range so that only post-consolidation commits
    need coverage -- the merged entries cover pre-consolidation work.

    Historical versioned JSONL files are left in place (they are read-only
    records of past releases).

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the target releasable.
        member_projects: list of WorkspaceProject instances in this releasable.
        tag_format: optional releasable tag format (e.g., "{name}@v{version}").
            When provided with version, a consolidation-point tag is created
            at HEAD.
        version: optional version string for the consolidation-point tag.

    Returns:
        A dict with:
            - ``entries_merged`` (int): total entries written
            - ``source_projects`` (list[str]): projects that had entries
            - ``dest_path`` (str): path to the releasable's unreleased.jsonl
            - ``duplicates_merged`` (int): entries merged due to identical commits
            - ``exclusions_created`` (int): batch limit exclusions auto-created
            - ``consolidation_tag`` (str or None): tag created at HEAD, if any
    """
    dest_changes_dir = get_releasable_changes_dir(workspace_root, releasable_name)
    os.makedirs(dest_changes_dir, exist_ok=True)
    dest_path = os.path.join(dest_changes_dir, "unreleased.jsonl")

    all_entries = []
    source_projects = []

    for proj in member_projects:
        proj_path = os.path.join(workspace_root, proj.path)
        changes_dir = get_changes_dir(proj_path)
        entries = read_unreleased(changes_dir)
        if entries:
            source_projects.append(proj.name)
            for entry in entries:
                # Derive packages field from commit file paths
                packages = _derive_packages_for_entry(
                    entry, member_projects, workspace_root
                )
                merged = ChangelogEntry(
                    commits=entry.commits,
                    user_facing=entry.user_facing,
                    description=entry.description,
                    type=entry.type,
                    release_type=entry.release_type,
                    packages=packages if packages else None,
                )
                all_entries.append(merged)

    # Bug 3 fix: deduplicate entries with identical commit sets across packages.
    # Group by frozenset of commits; merge entries with identical commit lists.
    all_entries, duplicates_merged = _dedup_entries(all_entries)

    # Write all merged entries to the releasable's unreleased.jsonl
    lines = [serialize_entry(e) + "\n" for e in all_entries]
    with open(dest_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # Merge versioned JSONL files from member projects into the releasable.
    # Collect entries per version across all members, tag with packages,
    # dedup, and write to the releasable's changes dir.
    versioned_merged = _merge_versioned_files(
        workspace_root, releasable_name, member_projects, dest_changes_dir,
    )

    # Copy release .toml metadata for versioned files from member projects.
    rel_dir = get_releasable_dir(workspace_root, releasable_name)
    _copy_release_metadata(workspace_root, member_projects, rel_dir)

    # Regenerate per-version .md files from the merged JSONL files.
    rel_releases_dir = os.path.join(rel_dir, "releases")
    for version, _jsonl_path in list_versioned_files(dest_changes_dir):
        desc, ctx = _read_release_metadata(
            workspace_root, version, releases_dir=rel_releases_dir,
        )
        generate_version_file(
            dest_changes_dir, version,
            description=desc, context=ctx,
        )

    # Bug 2 fix: collect batch_limits exclusions from per-package configs
    # and auto-create exclusions for entries exceeding max_commits_per_entry.
    exclusions_created = _migrate_batch_exclusions(
        workspace_root, releasable_name, member_projects, all_entries,
    )

    # Bug 1 fix: create a consolidation-point tag at HEAD so the unreleased
    # range starts from this point. Pre-consolidation commits are covered by
    # the merged entries; only post-consolidation commits need new coverage.
    consolidation_tag = None
    if tag_format and version:
        consolidation_tag = _create_consolidation_tag(
            workspace_root, releasable_name, tag_format, version,
        )

    return {
        "entries_merged": len(all_entries),
        "source_projects": source_projects,
        "dest_path": dest_path,
        "duplicates_merged": duplicates_merged,
        "exclusions_created": exclusions_created,
        "consolidation_tag": consolidation_tag,
        "versioned_files_merged": versioned_merged,
    }


def _derive_packages_for_entry(entry, member_projects, workspace_root):
    """Derive the packages field for a changelog entry.

    For each commit in the entry, checks which member projects are
    affected by the commit's changed files. Returns a sorted, deduplicated
    list of project names.

    If commit files cannot be determined (e.g., not in a git repo during
    testing), returns an empty list.
    """
    affected = set()
    for sha in entry.commits:
        files = get_commit_files(sha)
        if files is None:
            continue
        for filepath in files:
            for proj in member_projects:
                if file_matches_project(filepath, proj):
                    affected.add(proj.name)
    return sorted(affected)


def _dedup_entries(entries):
    """Deduplicate entries with identical commit sets.

    Entries from different packages that reference the exact same set of
    commits are merged into one entry, combining their ``packages`` lists.
    When merging, the first user-facing entry's description and type win
    (non-user-facing entries contribute only their packages).

    Returns ``(deduped_entries, count_of_merges)`` where count_of_merges is
    the number of entries that were folded into another (i.e.,
    ``len(original) - len(deduped)``).
    """
    # Group entries by their commit set (as a frozen sorted tuple for order-
    # independent matching).
    groups = {}
    for entry in entries:
        key = tuple(sorted(entry.commits))
        groups.setdefault(key, []).append(entry)

    deduped = []
    total_merged = 0
    for _key, group in groups.items():
        if len(group) == 1:
            deduped.append(group[0])
            continue

        # Merge all entries in the group into one
        total_merged += len(group) - 1
        combined_packages = set()
        # Pick the first user-facing entry for description/type; fall back
        # to the first entry if none are user-facing.
        base = None
        for entry in group:
            if entry.user_facing and base is None:
                base = entry
            if entry.packages:
                combined_packages.update(entry.packages)
        if base is None:
            base = group[0]

        merged = ChangelogEntry(
            commits=base.commits,
            user_facing=base.user_facing,
            description=base.description,
            type=base.type,
            release_type=base.release_type,
            packages=sorted(combined_packages) if combined_packages else base.packages,
        )
        deduped.append(merged)

    return deduped, total_merged


def _merge_versioned_files(workspace_root, releasable_name, member_projects,
                           dest_changes_dir):
    """Merge versioned JSONL files from all member projects into the releasable.

    For each version found across any member, reads entries from all members
    that have that version, tags each entry with ``packages: [member_name]``
    (if not already set), deduplicates, and writes to the releasable's
    ``changes/{version}.jsonl``. The output file is set read-only (chmod 444).

    Members are processed alphabetically for deterministic output order.

    Returns the number of versioned files written.
    """
    version_entries: dict[str, list[tuple[str, ChangelogEntry]]] = {}

    for proj in sorted(member_projects, key=lambda p: p.name):
        proj_path = os.path.join(workspace_root, proj.path)
        changes_dir = get_changes_dir(proj_path)
        for version, jsonl_path in list_versioned_files(changes_dir):
            entries = parse_jsonl(jsonl_path)
            for entry in entries:
                version_entries.setdefault(version, []).append(
                    (proj.name, entry)
                )

    if not version_entries:
        return 0

    files_written = 0
    for version, member_entries in version_entries.items():
        tagged = []
        for member_name, entry in member_entries:
            packages = entry.packages if entry.packages else [member_name]
            tagged.append(ChangelogEntry(
                commits=entry.commits,
                user_facing=entry.user_facing,
                description=entry.description,
                type=entry.type,
                release_type=entry.release_type,
                packages=packages,
            ))

        deduped, _ = _dedup_entries(tagged)

        dest_path = os.path.join(dest_changes_dir, f"{version}.jsonl")
        lines = [serialize_entry(e) + "\n" for e in deduped]
        with open(dest_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.chmod(dest_path, 0o444)
        files_written += 1

    return files_written


def _copy_release_metadata(workspace_root, member_projects, releasable_dir):
    """Copy release .toml metadata from member projects to the releasable.

    For each versioned JSONL file that was migrated, checks if the member
    has a corresponding ``v{version}.toml`` in their ``.rlsbl/releases/``
    directory. If found, copies it to the releasable's ``releases/``
    directory. When two members have the same version's ``.toml``, their
    descriptions are merged into a combined historical note.
    """
    import tomlkit

    version_tomls: dict[str, list[tuple[str, dict]]] = {}

    for proj in sorted(member_projects, key=lambda p: p.name):
        proj_path = os.path.join(workspace_root, proj.path)
        releases_dir = os.path.join(proj_path, ".rlsbl", "releases")
        if not os.path.isdir(releases_dir):
            continue
        for name in os.listdir(releases_dir):
            if not name.startswith("v") or not name.endswith(".toml"):
                continue
            version = name[1:-5]
            toml_path = os.path.join(releases_dir, name)
            try:
                with open(toml_path, "r", encoding="utf-8") as f:
                    data = tomlkit.load(f)
            except (OSError, tomlkit.exceptions.ParseError):
                continue
            version_tomls.setdefault(version, []).append(
                (proj.name, dict(data))
            )

    if not version_tomls:
        return

    dest_releases_dir = os.path.join(releasable_dir, "releases")
    os.makedirs(dest_releases_dir, exist_ok=True)

    for version, tomls in version_tomls.items():
        if len(tomls) == 1:
            _member_name, merged = tomls[0]
        else:
            member_labels = [
                f"{member_name} {version}" for member_name, _data in tomls
            ]
            merged_desc = "Historical: " + ", ".join(member_labels)
            merged = dict(tomls[0][1])
            merged["description"] = merged_desc
            contexts = [
                data.get("context", "")
                for _name, data in tomls
                if data.get("context", "")
            ]
            if contexts:
                merged["context"] = "\n\n".join(contexts)

        dest_path = os.path.join(dest_releases_dir, f"v{version}.toml")
        with open(dest_path, "w", encoding="utf-8") as f:
            tomlkit.dump(merged, f)


def _migrate_batch_exclusions(workspace_root, releasable_name,
                              member_projects, entries):
    """Collect per-package batch exclusions and create releasable-level ones.

    Per-package exclusions reference ``(version, line_number)`` tuples that
    become invalid after consolidation (line numbers change in the merged
    file). Instead of carrying over stale exclusions, this function scans
    the consolidated entries and auto-creates new exclusions for any entry
    that exceeds ``max_commits_per_entry``.

    Writes the exclusions to a ``config.json`` in the releasable's directory
    (``<workspace>/.rlsbl-monorepo/releasables/<name>/config.json``).

    Returns the number of exclusions created.
    """
    # Determine max_commits_per_entry from per-package configs (use the
    # smallest value across members to be conservative).
    # Uses inheritance-aware loading so releasable-level batch_limits are
    # picked up by packages that don't override them.
    from .config import read_project_config
    rel_config_dir = get_releasable_dir(workspace_root, releasable_name)
    max_commits = 5  # default
    for proj in member_projects:
        abs_pkg = os.path.join(workspace_root, proj.path)
        proj_config = read_project_config(abs_pkg, releasable_config_dir=rel_config_dir)
        bl = proj_config.get("batch_limits", {})
        if isinstance(bl, dict):
            val = bl.get("max_commits_per_entry")
            if isinstance(val, int) and not isinstance(val, bool) and val > 0:
                max_commits = min(max_commits, val)

    # Find entries that exceed the limit and need exclusions
    new_exclusions = []
    for i, entry in enumerate(entries):
        if len(entry.commits) > max_commits:
            new_exclusions.append({
                "reason": "auto-created during changelog consolidation",
                "entries": [{"version": "unreleased", "line": i + 1}],
            })

    if not new_exclusions:
        return 0

    # Write to the releasable's config.json
    rel_dir = get_releasable_dir(workspace_root, releasable_name)
    os.makedirs(rel_dir, exist_ok=True)
    config_path = os.path.join(rel_dir, "config.json")
    config = read_json_config(config_path)
    bl = config.setdefault("batch_limits", {})
    existing = bl.setdefault("exclusions", [])
    existing.extend(new_exclusions)

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_path, config_path)

    return len(new_exclusions)


def _create_consolidation_tag(workspace_root, releasable_name, tag_format,
                              version):
    """Create a consolidation-point tag at HEAD.

    The tag uses the releasable's tag format and version, marking the
    point where per-package changelogs were consolidated. This resets
    the unreleased range (``<tag>..HEAD``) so only post-consolidation
    commits need coverage.

    Returns the tag name on success, None on failure.
    """
    tag = tag_format.format(name=releasable_name, version=version)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        head_sha = result.stdout.strip()
        if len(head_sha) != 40:
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    try:
        subprocess.run(
            ["git", "tag", tag, head_sha],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return None

    return tag


# ---------------------------------------------------------------------------
# 10.3: consolidate_versions
# ---------------------------------------------------------------------------


def consolidate_versions(workspace_root, releasable_name, member_projects):
    """Write releasable version file from member versions.

    Reads each member's version from their target manifest (via detect_targets
    + read_version). If all members share the same version, writes it to the
    releasable version file. If they differ, returns the conflicting versions
    without writing.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.
        member_projects: list of WorkspaceProject instances.

    Returns:
        A dict with:
            - ``status`` (str): "ok" if all versions match and were written,
              "conflict" if versions differ, "empty" if no versions found
            - ``version`` (str or None): the common version (if ok)
            - ``versions`` (dict[str, str]): project name -> version mapping
              (always present)
    """
    versions = {}
    for proj in member_projects:
        proj_path = os.path.join(workspace_root, proj.path)
        version = _read_project_version(proj_path)
        if version is not None:
            versions[proj.name] = version

    if not versions:
        return {
            "status": "empty",
            "version": None,
            "versions": versions,
        }

    unique_versions = set(versions.values())

    if len(unique_versions) == 1:
        common_version = next(iter(unique_versions))
        write_releasable_version(workspace_root, releasable_name, common_version)
        return {
            "status": "ok",
            "version": common_version,
            "versions": versions,
        }

    return {
        "status": "conflict",
        "version": None,
        "versions": versions,
    }


# ---------------------------------------------------------------------------
# 10.5: create_migration_tag
# ---------------------------------------------------------------------------


def create_migration_tag(workspace_root, releasable_name, tag_format,
                         member_projects):
    """Create a releasable-format tag pointing to the last per-package release.

    For each member project, finds the latest per-package tag (via git
    describe). Picks the most recent across all members and creates a
    releasable-format tag pointing to the same commit.

    This gives ``_unreleased_range()`` a starting point with the new tag glob.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable.
        tag_format: the releasable's tag format string (e.g., "{name}@v{version}").
        member_projects: list of WorkspaceProject instances.

    Returns:
        A dict with:
            - ``status`` (str): "created", "no_tags", or "error"
            - ``tag`` (str or None): the created tag name
            - ``commit`` (str or None): the commit the tag points to
            - ``source_tag`` (str or None): the per-package tag used as source
            - ``member_tags`` (dict[str, str]): project name -> latest tag
            - ``skipped_members`` (list[str]): members skipped (no scoped tag)
    """
    logger = logging.getLogger(__name__)

    member_tags = {}
    tag_commits = {}
    skipped_members = []

    for proj in member_projects:
        tag_glob = f"{proj.name}@v*"
        tag = _git_describe_tag(tag_glob, workspace_root)
        if tag is None:
            # No scoped tag found for this member -- skip it rather than
            # falling back to the repo-wide v* glob, which in a monorepo
            # may belong to a completely different package.
            logger.warning(
                "No scoped tag matching '%s' found for member '%s'; skipping",
                tag_glob, proj.name,
            )
            skipped_members.append(proj.name)
            continue
        member_tags[proj.name] = tag
        commit = _git_rev_parse(tag, workspace_root)
        if commit:
            tag_commits[tag] = commit

    if not member_tags:
        return {
            "status": "no_tags",
            "tag": None,
            "commit": None,
            "source_tag": None,
            "member_tags": member_tags,
            "skipped_members": skipped_members,
        }

    # Find the most recent tag by commit date
    most_recent_tag = _find_most_recent_tag(
        list(member_tags.values()), workspace_root
    )
    if most_recent_tag is None:
        return {
            "status": "error",
            "tag": None,
            "commit": None,
            "source_tag": None,
            "member_tags": member_tags,
            "skipped_members": skipped_members,
        }

    commit = tag_commits.get(most_recent_tag)
    if commit is None:
        commit = _git_rev_parse(most_recent_tag, workspace_root)

    if commit is None:
        return {
            "status": "error",
            "tag": None,
            "commit": None,
            "source_tag": most_recent_tag,
            "member_tags": member_tags,
            "skipped_members": skipped_members,
        }

    # Extract version from the source tag (e.g., "mylib@v0.1.0" -> "0.1.0")
    version = _extract_version_from_tag(most_recent_tag)
    if version is None:
        return {
            "status": "error",
            "tag": None,
            "commit": commit,
            "source_tag": most_recent_tag,
            "member_tags": member_tags,
            "skipped_members": skipped_members,
        }

    # Format the new releasable tag
    new_tag = tag_format.format(name=releasable_name, version=version)

    # Create the tag. If it already exists (e.g., from a consolidation-point
    # tag created during changelog merge), treat it as success -- the tag
    # serves the same range-boundary purpose regardless of which step created it.
    tag_exists = _git_rev_parse(new_tag, workspace_root) is not None
    if not tag_exists:
        try:
            subprocess.run(
                ["git", "tag", new_tag, commit],
                cwd=workspace_root,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            return {
                "status": "error",
                "tag": new_tag,
                "commit": commit,
                "source_tag": most_recent_tag,
                "member_tags": member_tags,
                "skipped_members": skipped_members,
            }

    return {
        "status": "created",
        "tag": new_tag,
        "commit": commit,
        "source_tag": most_recent_tag,
        "member_tags": member_tags,
        "skipped_members": skipped_members,
    }


def _git_describe_tag(tag_glob, cwd):
    """Run git describe to find the latest tag matching a glob.

    Returns the tag string or None.
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", tag_glob],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _git_rev_parse(ref, cwd):
    """Resolve a git ref to a full SHA.

    Returns the SHA string or None.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if len(sha) == 40:
                return sha
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def _find_most_recent_tag(tags, cwd):
    """Find the most recent tag by topological commit order.

    Uses ``git log`` with all tags as start points and ``--topo-order`` to
    determine which tag's commit appears first (most recent). This is
    reliable even when commits share the same timestamp.

    Returns the tag name or None if comparison fails.
    """
    if not tags:
        return None
    if len(tags) == 1:
        return tags[0]

    # Resolve each tag to its commit SHA
    tag_to_sha = {}
    for tag in tags:
        sha = _git_rev_parse(tag, cwd)
        if sha:
            tag_to_sha[tag] = sha

    if not tag_to_sha:
        return None
    if len(tag_to_sha) == 1:
        return next(iter(tag_to_sha))

    sha_to_tag = {sha: tag for tag, sha in tag_to_sha.items()}

    # Use git log to walk from HEAD in topo order, and return the first
    # commit that matches one of our tag SHAs. That is the most recent.
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", "--topo-order"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                sha = line.strip()
                if sha in sha_to_tag:
                    return sha_to_tag[sha]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fallback: return the first tag if git log fails
    return tags[0]


def _extract_version_from_tag(tag):
    """Extract the version string from a tag.

    Handles formats like:
    - "v1.2.3" -> "1.2.3"
    - "mylib@v1.2.3" -> "1.2.3"
    - "path/v1.2.3" -> "1.2.3"

    Returns None if no version pattern is found.
    """
    import re
    match = re.search(r"v(\d+\.\d+\.\d+(?:-[a-zA-Z0-9.]+)?)", tag)
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Pre-migration validation
# ---------------------------------------------------------------------------


def _validate_no_changelog_collisions(workspace_root, releasable_name,
                                       member_projects):
    """Validate that member changelogs don't collide with existing releasable state.

    Checks two conditions:

    1. **Versioned file collision**: if a member's per-package ``changes/``
       has a versioned JSONL file (e.g., ``0.1.0.jsonl``) and the releasable's
       ``changes/`` already has that same version, the merge would overwrite
       or duplicate historical entries.

    2. **Unreleased entry conflict**: if the releasable's ``unreleased.jsonl``
       already has entries AND any member also has unreleased entries, the
       merge would duplicate or lose entries.

    Both conditions raise ``WorkspaceError`` with details about the conflict.

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the target releasable.
        member_projects: list of WorkspaceProject instances in this releasable.

    Raises:
        WorkspaceError: if any collision is detected.
    """
    rel_changes_dir = get_releasable_changes_dir(workspace_root, releasable_name)

    # Collect releasable's existing versioned files
    rel_versions = set()
    if os.path.isdir(rel_changes_dir):
        for ver_str, _path in list_versioned_files(rel_changes_dir):
            rel_versions.add(ver_str)

    # Check 1: versioned file collisions
    collisions = []
    for proj in member_projects:
        proj_path = os.path.join(workspace_root, proj.path)
        proj_changes_dir = get_changes_dir(proj_path)
        if not os.path.isdir(proj_changes_dir):
            continue
        for ver_str, _path in list_versioned_files(proj_changes_dir):
            if ver_str in rel_versions:
                collisions.append((proj.name, ver_str))

    if collisions:
        details = ", ".join(
            f"{name}: {ver}" for name, ver in collisions
        )
        raise WorkspaceError(
            f"Versioned changelog collision: releasable '{releasable_name}' "
            f"already has JSONL files for versions that members also have. "
            f"Conflicts: {details}. "
            f"Resolve by removing or renaming the conflicting files before "
            f"migrating."
        )

    # Check 2: unreleased entry conflict
    rel_unreleased = read_unreleased(rel_changes_dir)
    if rel_unreleased:
        members_with_unreleased = []
        for proj in member_projects:
            proj_path = os.path.join(workspace_root, proj.path)
            proj_changes_dir = get_changes_dir(proj_path)
            proj_unreleased = read_unreleased(proj_changes_dir)
            if proj_unreleased:
                members_with_unreleased.append(proj.name)

        if members_with_unreleased:
            raise WorkspaceError(
                f"Unreleased changelog conflict: releasable "
                f"'{releasable_name}' already has "
                f"{len(rel_unreleased)} unreleased entries, and "
                f"members {', '.join(members_with_unreleased)} also have "
                f"unreleased entries. Consolidation would duplicate or "
                f"overwrite entries. Clear the releasable's unreleased.jsonl "
                f"or member entries before migrating."
            )


# ---------------------------------------------------------------------------
# 10.6: cmd_migrate_releasable -- orchestrate full migration
# ---------------------------------------------------------------------------


def cmd_migrate_releasable(workspace_root, releasable_name, *, dry_run=False,
                           yes=False):
    """Orchestrate the full migration of a releasable from per-package to releasable model.

    Steps:
    1. ``detect_migration_state()`` -- show current state
    2. ``consolidate_versions()`` -- write releasable version from member versions
       (before changelogs so the version is available for the consolidation tag)
    3. ``consolidate_changelogs()`` -- merge per-package changelogs into per-releasable,
       creating a consolidation-point tag that resets the unreleased range
    4. ``create_migration_tag()`` -- create releasable-format tag
    5. ``cleanup_per_package_release_state()`` -- remove orphaned per-package state

    Args:
        workspace_root: path to the monorepo root.
        releasable_name: name of the releasable to migrate.
        dry_run: if True, report what would happen without making changes.
        yes: if True, skip interactive confirmation prompts.

    Returns:
        A dict with:
            - ``releasable_name`` (str)
            - ``dry_run`` (bool)
            - ``state`` (dict): result of detect_migration_state
            - ``changelogs`` (dict or None): result of consolidate_changelogs
            - ``versions`` (dict or None): result of consolidate_versions
            - ``tag`` (dict or None): result of create_migration_tag
            - ``cleanup`` (list or None): paths removed by cleanup

    Raises:
        WorkspaceError: if workspace is not in explicit mode or releasable
            not found.
    """
    from .releasable_cleanup import cleanup_per_package_release_state

    # Load workspace and validate releasable exists
    projects = load_workspace(workspace_root)
    if not is_explicit_mode(workspace_root):
        raise WorkspaceError(
            "Workspace is not in explicit mode. "
            "Add [[releasables]] to workspace.toml first."
        )

    releasables = load_releasables(workspace_root, projects=projects)
    target_releasable = None
    for rel in releasables:
        if rel.name == releasable_name:
            target_releasable = rel
            break
    if target_releasable is None:
        available = ", ".join(r.name for r in releasables)
        raise WorkspaceError(
            f"Releasable '{releasable_name}' not found. Available: {available}"
        )

    member_projects = members_of(releasable_name, projects)
    if not member_projects:
        raise WorkspaceError(
            f"Releasable '{releasable_name}' has no member packages"
        )

    # Pre-migration validation: check for changelog collisions
    _validate_no_changelog_collisions(
        workspace_root, releasable_name, member_projects,
    )

    # Step 1: detect migration state
    state = detect_migration_state(workspace_root)

    result = {
        "releasable_name": releasable_name,
        "dry_run": dry_run,
        "state": state,
        "changelogs": None,
        "versions": None,
        "tag": None,
        "cleanup": None,
    }

    if dry_run:
        # In dry-run mode, report what would happen without side effects
        member_names = [p.name for p in member_projects]
        result["members"] = member_names
        result["tag_format"] = target_releasable.tag_format
        return result

    # Interactive confirmation before destructive steps
    if not yes:
        member_names = [p.name for p in member_projects]
        print(f"Migrating releasable '{releasable_name}' with members: "
              f"{', '.join(member_names)}")
        print("This will:")
        print("  1. Merge per-package changelogs into the releasable directory")
        print("  2. Consolidate member versions into the releasable version file")
        print("  3. Create a releasable-format migration tag")
        print("  4. Remove per-package .rlsbl/changes/ and .rlsbl/releases/ directories")
        response = input("Proceed? [y/N] ").strip().lower()
        if response not in ("y", "yes"):
            raise WorkspaceError("Migration cancelled by user")

    # Step 2: consolidate versions (before changelogs, so we have the
    # version string for the consolidation-point tag)
    version_result = consolidate_versions(
        workspace_root, releasable_name, member_projects,
    )
    result["versions"] = version_result

    if version_result["status"] == "conflict":
        raise WorkspaceError(
            f"Version conflict among members: {version_result['versions']}. "
            "Resolve version differences before migrating."
        )

    # Step 3: consolidate changelogs (with tag_format + version for the
    # consolidation-point tag that resets the unreleased range)
    consolidated_version = version_result["version"]
    changelog_result = consolidate_changelogs(
        workspace_root, releasable_name, member_projects,
        tag_format=target_releasable.tag_format,
        version=consolidated_version,
    )
    result["changelogs"] = changelog_result

    # Step 4: create migration tag
    tag_result = create_migration_tag(
        workspace_root, releasable_name,
        target_releasable.tag_format, member_projects,
    )
    result["tag"] = tag_result

    # Step 5: cleanup per-package release state
    removed = cleanup_per_package_release_state(
        workspace_root, projects=projects, releasables=releasables,
    )
    result["cleanup"] = removed

    return result
