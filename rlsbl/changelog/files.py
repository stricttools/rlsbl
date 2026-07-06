"""File management layer for JSONL changelog files including reading, writing, appending entries, and path resolution for .rlsbl directories."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import os
import re
import stat
import sys
import tempfile

import json

from .schema import ChangelogEntry, parse_entry, parse_jsonl, serialize_entry
from ..errors import ChangelogError, ConfigError

_VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)\.(\d+))?\.jsonl$"
)

# Maps pre-release identifiers to sort rank.  Stable versions use
# is_stable=1 which sorts after all pre-releases (is_stable=0).
_PREID_RANK = {"alpha": 0, "beta": 1, "rc": 2}

# Sort key type: (major, minor, patch, is_stable, preid_rank, counter)
_SemverKey = tuple[int, int, int, int, int, int]


@dataclass
class RemapResult:
    """Result of remapping hashes in one JSONL file."""
    path: str
    entries_modified: int
    hashes_remapped: int


@dataclass
class RemapReport:
    """Full report of a remap across all JSONL files in one changes dir.

    ``results`` lists the files that were modified.  ``unmapped`` and
    ``ambiguous`` record, per file path, hashes that could NOT be mapped:
    hashes matching no rewrite key, and abbreviated hashes matching more
    than one rewrite key, respectively.  Callers must decide whether
    unmapped hashes are a problem (e.g. by checking that they still
    resolve after the rewrite).
    """
    results: list[RemapResult]
    unmapped: dict[str, list[str]]
    ambiguous: dict[str, list[str]]


# Minimum abbreviated-hash length considered for prefix matching (git's
# own minimum abbreviation length).
_MIN_ABBREV = 4


def _map_hash(h: str, sha_map: dict) -> "tuple[str | None, bool]":
    """Map one (possibly abbreviated) hash through the rewrites map.

    Returns ``(new_sha, ambiguous)``.  ``new_sha`` is None when the hash
    matched no key; ``ambiguous`` is True when an abbreviated hash matched
    more than one key.
    """
    if h in sha_map:
        return sha_map[h], False
    if len(h) >= _MIN_ABBREV:
        candidates = [k for k in sha_map if k.startswith(h)]
        if len(candidates) == 1:
            return sha_map[candidates[0]], False
        if len(candidates) > 1:
            return None, True
    return None, False


def can_remap_hash(h: str, sha_map: dict) -> bool:
    """Whether ``remap_jsonl_hashes`` could fix hash ``h`` with ``sha_map``.

    True when the (possibly abbreviated) hash matches exactly one key of the
    map. Used by the scrub recovery path to decide whether a rewrite journal
    can repair a dangling changelog hash before mutating any files.
    """
    new_sha, _ambiguous = _map_hash(h, sha_map)
    return new_sha is not None


def _parse_semver(filename: str) -> _SemverKey | None:
    """Extract a sort key from a versioned filename, or None.

    Returns ``(major, minor, patch, is_stable, preid_rank, counter)``
    where ``is_stable`` is 1 for stable versions (so they sort after
    pre-releases) and ``preid_rank`` maps alpha=0, beta=1, rc=2.
    """
    m = _VERSION_RE.match(filename)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    preid = m.group(4)
    if preid is None:
        # Stable version — sorts after all pre-releases of the same base.
        return (major, minor, patch, 1, 0, 0)
    counter = int(m.group(5))
    return (major, minor, patch, 0, _PREID_RANK[preid], counter)


def get_changes_dir(project_path: str) -> str:
    """Return the path to .rlsbl/changes/ inside the project."""
    return os.path.join(project_path, ".rlsbl", "changes")


def enumerate_changelog_dirs(project_root, workspace_root=None, workspace_projects=None):
    """Enumerate every changelog changes-dir whose JSONL files may reference
    commit hashes.

    Standalone: the project's own ``.rlsbl/changes/``.  Monorepo: every
    workspace project's ``.rlsbl/changes/`` PLUS every releasable's
    ``.rlsbl-monorepo/releasables/<name>/changes/`` (enumerated from disk, so
    coverage matches what is actually in the working tree).

    ``workspace_projects`` may be passed by callers that already loaded the
    workspace; when omitted it is loaded from ``workspace_root``.

    Only directories that exist are returned.
    """
    dirs = []
    if workspace_root:
        from ..workspace import (
            RELEASABLES_DIR,
            WORKSPACE_DIR,
            get_releasable_changes_dir,
            load_workspace,
        )

        if workspace_projects is None:
            workspace_projects = load_workspace(str(workspace_root))
        for proj in workspace_projects:
            d = get_changes_dir(os.path.join(str(workspace_root), proj.path))
            if os.path.isdir(d):
                dirs.append(d)
        releasables_root = os.path.join(str(workspace_root), WORKSPACE_DIR, RELEASABLES_DIR)
        if os.path.isdir(releasables_root):
            for name in sorted(os.listdir(releasables_root)):
                d = get_releasable_changes_dir(str(workspace_root), name)
                if os.path.isdir(d):
                    dirs.append(d)
    else:
        d = get_changes_dir(str(project_root))
        if os.path.isdir(d):
            dirs.append(d)
    return dirs


def changelog_remap_globs(project_root, workspace_root=None, workspace_projects=None):
    """Build the safegit ``--remap-shas-in`` glob list for a scrub.

    The globs are repo-relative and use Go ``path.Match`` semantics (safegit's
    matchScope): ``*`` never crosses ``/``, so every glob is an exact
    per-directory pattern. Coverage is derived from the SAME enumeration as
    hash validation (``enumerate_changelog_dirs``) so remap coverage and
    validation coverage can never diverge:

    - Standalone: ``.rlsbl/changes/*.jsonl`` -- emitted unconditionally, since
      historical commits may contain changelog files even when the working
      tree currently has none.
    - Monorepo: one exact glob per enumerated per-project changes dir, plus a
      single wildcard glob ``.rlsbl-monorepo/releasables/*/changes/*.jsonl``
      that also covers releasables deleted from the working tree but still
      present in history.

    DELIBERATELY EXCLUDED: committed scrub archives (``.rlsbl/scrubs/*.json``
    and the releasable-level equivalent). They are records of what WAS -- the
    old-side SHAs they record dangle by design as soon as the original scrub
    prunes the old objects, so remapping them on a later scrub would falsify
    the record without ever making the old side resolvable. Validation
    (``validate_all_hashes_resolve``) likewise never reads them, so remap and
    validation agree on the exclusion. ``.validated`` caches carry no
    extension match and are deleted by the scrub flow anyway.
    """
    if not workspace_root:
        # Mirrors get_changes_dir(project_root) relative to the repo root.
        return [".rlsbl/changes/*.jsonl"]

    from ..workspace import RELEASABLES_DIR, WORKSPACE_DIR

    releasable_prefix = f"{WORKSPACE_DIR}/{RELEASABLES_DIR}/"
    globs = [f"{WORKSPACE_DIR}/{RELEASABLES_DIR}/*/changes/*.jsonl"]
    for d in enumerate_changelog_dirs(
        project_root, workspace_root, workspace_projects=workspace_projects,
    ):
        rel = os.path.relpath(d, str(workspace_root)).replace(os.sep, "/")
        if rel.startswith(releasable_prefix):
            # Already covered by the wildcard glob above.
            continue
        globs.append(rel + "/*.jsonl")
    return globs


def _list_jsonl_files(changes_dir):
    """All JSONL files in a changes dir: unreleased.jsonl plus versioned."""
    files = []
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    if os.path.isfile(unreleased):
        files.append(unreleased)
    for _version, path in list_versioned_files(changes_dir):
        files.append(path)
    return files


def validate_all_hashes_resolve(dirs, *, repo_root):
    """Verify that every commit hash in every JSONL file resolves via git.

    Runs ``git rev-parse`` for each distinct hash in ``repo_root`` — the
    repository the hashes belong to. The parameter is mandatory so callers
    (including the planned validation-only mode) can never accidentally
    resolve against whatever repo the process CWD happens to be in.

    Returns ``{filepath: [unresolvable hashes]}`` — empty when everything
    resolves.
    """
    from .resolve import resolve_hashes

    failures: dict[str, list[str]] = {}
    for changes_dir in dirs:
        for filepath in _list_jsonl_files(changes_dir):
            hashes = [h for entry in parse_jsonl(filepath) for h in entry.commits]
            resolved = resolve_hashes(hashes, cwd=str(repo_root))
            bad = [h for h in dict.fromkeys(hashes) if resolved.get(h) is None]
            if bad:
                failures[filepath] = bad
    return failures


def changes_dir_exists(project_path: str) -> bool:
    """Check if .rlsbl/changes/ exists in the project."""
    return os.path.isdir(get_changes_dir(project_path))


def list_versioned_files(changes_dir: str) -> list[tuple[str, str]]:
    """List all versioned JSONL files, sorted by semver (newest first).

    Matches both stable (``x.y.z.jsonl``) and pre-release
    (``x.y.z-preid.N.jsonl``) filenames.

    Returns (version_string, filepath) pairs.
    """
    results: list[tuple[_SemverKey, str, str]] = []
    if not os.path.isdir(changes_dir):
        return []
    for name in os.listdir(changes_dir):
        sort_key = _parse_semver(name)
        if sort_key is not None:
            # Strip the .jsonl suffix to recover the version string.
            version_str = name[: -len(".jsonl")]
            results.append((sort_key, version_str, os.path.join(changes_dir, name)))
    # Sort by semver tuple descending (newest first)
    results.sort(key=lambda x: x[0], reverse=True)
    return [(ver, path) for _, ver, path in results]


def read_unreleased(changes_dir: str) -> list[ChangelogEntry]:
    """Read unreleased.jsonl and return entries. Empty list if file missing."""
    path = os.path.join(changes_dir, "unreleased.jsonl")
    if not os.path.isfile(path):
        return []
    return parse_jsonl(path)


def append_entry(changes_dir: str, entry: ChangelogEntry) -> None:
    """Append one entry to unreleased.jsonl atomically.

    Writes the serialized line to a temp file, then appends it to the target.
    Creates the changes directory and unreleased.jsonl if they don't exist.
    """
    os.makedirs(changes_dir, exist_ok=True)
    target = os.path.join(changes_dir, "unreleased.jsonl")
    _append_entry_to_file(target, entry)


def append_entry_to_version(changes_dir: str, version: str, entry: ChangelogEntry) -> None:
    """Append one entry to a versioned JSONL file (e.g., 0.39.0.jsonl).

    The caller is responsible for unlocking/re-locking the file if it is read-only.
    """
    target = os.path.join(changes_dir, f"{version}.jsonl")
    _append_entry_to_file(target, entry)


def _append_entry_to_file(target: str, entry: ChangelogEntry) -> None:
    """Append one entry to any JSONL file atomically.

    Writes the serialized line to a temp file, then appends it to the target.
    Creates parent directories if they don't exist.
    """
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    line = serialize_entry(entry) + "\n"

    # Write to a temp file in the same directory (same filesystem for rename)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        os.write(fd, line.encode("utf-8"))
        os.close(fd)
        # Append the temp file content to the target
        with open(target, "a", encoding="utf-8") as f:
            with open(tmp_path, "r", encoding="utf-8") as tmp_f:
                f.write(tmp_f.read())
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _warn_stale_entries(src: str, tag_glob: str) -> None:
    """Warn on stderr for entries in unreleased.jsonl referencing out-of-range commits.

    In monorepo mode, an entry whose commits all sit before the project's last
    tag is stale — typically left over from a sibling project's release. We
    emit a warning per stale entry but do not strip them (warn-only).
    """
    # Local imports to avoid circular dependency at module load time.
    from .resolve import resolve_hashes, _git_log_hashes, _unreleased_range

    range_spec = _unreleased_range(tag_glob)
    in_range = set(_git_log_hashes(range_spec))

    # Re-parse the file with line numbers, mirroring parse_jsonl's logic.
    entries_with_lines: list[tuple[int, ChangelogEntry]] = []
    with open(src, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries_with_lines.append((line_num, parse_entry(stripped)))
            except ChangelogError:
                # Schema/JSON errors are surfaced by validate; skip here.
                continue

    all_hashes: list[str] = []
    for _, entry in entries_with_lines:
        all_hashes.extend(entry.commits)
    resolved = resolve_hashes(all_hashes)

    for line_num, entry in entries_with_lines:
        out_of_range: list[str] = []
        for h in entry.commits:
            full = resolved.get(h)
            if full is None:
                # Unresolved hashes are reported by other validation checks.
                continue
            if full not in in_range:
                out_of_range.append(h)
        if out_of_range:
            joined = ", ".join(out_of_range)
            print(
                f"warning: unreleased.jsonl line {line_num} references "
                f"out-of-range commit(s): {joined}",
                file=sys.stderr,
            )


def finalize_version(
    changes_dir: str,
    version: str,
    tag_glob: str | None = None,
) -> None:
    """Rename unreleased.jsonl to x.y.z.jsonl and create a fresh unreleased.jsonl.

    Sets the versioned file read-only (chmod 0o444).
    Raises FileNotFoundError if unreleased.jsonl doesn't exist.

    When ``tag_glob`` is provided (monorepo mode), inspects each entry in
    ``unreleased.jsonl`` before the rename and warns on stderr for any whose
    commits fall outside the current project's unreleased range. Warn-only:
    the stale entries are not stripped.
    """
    src = os.path.join(changes_dir, "unreleased.jsonl")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"unreleased.jsonl not found in {changes_dir}")

    if tag_glob is not None:
        _warn_stale_entries(src, tag_glob)

    dst = os.path.join(changes_dir, f"{version}.jsonl")
    if os.path.exists(dst):
        # os.rename would silently overwrite dst on Linux, clobbering an
        # already-finalized changelog despite its read-only permissions.
        raise ChangelogError(
            f"refusing to finalize changelog for {version}: {dst} already "
            f"exists. This usually means a previous release attempt "
            f"partially completed. Finalized changelog files are read-only "
            f"by design; inspect the existing file and remove it manually "
            f"before re-releasing."
        )
    os.rename(src, dst)
    os.chmod(dst, 0o444)

    # Create a new empty unreleased.jsonl
    open(src, "w", encoding="utf-8").close()


def unfinalize_version(changes_dir: str, version: str) -> list[str]:
    """Reverse a finalize_version: restore x.y.z.jsonl back to unreleased.jsonl.

    1. Makes the versioned file writable.
    2. Renames it to unreleased.jsonl.
    3. Deletes the per-version .md file if present.
    4. Returns the list of changed file paths (for committing).

    Returns an empty list if the versioned file doesn't exist.
    """
    versioned = os.path.join(changes_dir, f"{version}.jsonl")
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    versioned_md = os.path.join(changes_dir, f"{version}.md")

    if not os.path.isfile(versioned):
        return []

    os.chmod(versioned, 0o644)
    os.rename(versioned, unreleased)

    changed: list[str] = [unreleased]

    if os.path.isfile(versioned_md):
        os.unlink(versioned_md)
        changed.append(versioned_md)

    return changed


def is_read_only(path: str) -> bool:
    """Check if a file has no write permissions (for any user class)."""
    if not os.path.exists(path):
        return False
    mode = os.stat(path).st_mode
    return not (mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


@contextmanager
def writable_jsonl(path):
    """Context manager that temporarily makes a read-only JSONL file writable.

    If the file is already writable, yields without changing permissions.
    On exit (even after exceptions), restores original read-only state.
    """
    was_ro = is_read_only(path)
    if was_ro:
        os.chmod(path, 0o644)
    try:
        yield path
    finally:
        if was_ro:
            os.chmod(path, 0o444)


def remap_jsonl_hashes(changes_dir, sha_map) -> RemapReport:
    """Replace commit hashes in all JSONL files using a rewrites mapping.

    Scans unreleased.jsonl and all versioned *.jsonl files in changes_dir.
    Hashes are matched exactly against the (full-SHA) keys of ``sha_map``;
    abbreviated hashes are matched by unique prefix.  Only files containing
    matching hashes are modified.  Uses writable_jsonl to handle read-only
    versioned files.

    Returns a RemapReport: modified files plus, per file, the hashes that
    could not be mapped (no key match, or ambiguous abbreviated prefix).
    Returns an empty report if changes_dir does not exist.
    """
    results = []
    unmapped: dict[str, list[str]] = {}
    ambiguous: dict[str, list[str]] = {}

    if not os.path.isdir(changes_dir):
        return RemapReport(results=results, unmapped=unmapped, ambiguous=ambiguous)

    for filepath in _list_jsonl_files(changes_dir):
        entries = parse_jsonl(filepath)
        entries_modified = 0
        hashes_remapped = 0
        file_unmapped: list[str] = []
        file_ambiguous: list[str] = []

        new_entries = []
        for entry in entries:
            new_commits = []
            entry_changed = False
            for h in entry.commits:
                new_sha, is_ambiguous = _map_hash(h, sha_map)
                if new_sha is not None:
                    new_commits.append(new_sha)
                    hashes_remapped += 1
                    entry_changed = True
                else:
                    new_commits.append(h)
                    if is_ambiguous:
                        if h not in file_ambiguous:
                            file_ambiguous.append(h)
                    elif h not in file_unmapped:
                        file_unmapped.append(h)
            if entry_changed:
                entries_modified += 1
            new_entries.append(ChangelogEntry(
                commits=new_commits,
                user_facing=entry.user_facing,
                description=entry.description,
                type=entry.type,
                release_type=entry.release_type,
                id=entry.id,
                packages=entry.packages,
            ))

        if file_unmapped:
            unmapped[filepath] = file_unmapped
        if file_ambiguous:
            ambiguous[filepath] = file_ambiguous

        if entries_modified == 0:
            continue

        # Write atomically: temp file + os.replace
        lines = [serialize_entry(e) + "\n" for e in new_entries]
        content = "".join(lines)

        with writable_jsonl(filepath):
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(filepath), suffix=".tmp"
            )
            try:
                os.write(fd, content.encode("utf-8"))
                os.close(fd)
                os.replace(tmp_path, filepath)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

        results.append(RemapResult(
            path=filepath,
            entries_modified=entries_modified,
            hashes_remapped=hashes_remapped,
        ))

    return RemapReport(results=results, unmapped=unmapped, ambiguous=ambiguous)


# ---------------------------------------------------------------------------
# coverage_unit config reader
# ---------------------------------------------------------------------------

_VALID_COVERAGE_UNITS = ("commit", "changeset-file")


def read_coverage_unit(config: dict) -> str:
    """Read and validate the coverage_unit from a project config dict.

    Returns ``"commit"`` or ``"changeset-file"``.
    Raises ``ConfigError`` if the key is missing or has an invalid value.
    """
    value = config.get("coverage_unit")
    if value is None:
        raise ConfigError(
            "coverage_unit is missing from .rlsbl/config.json. "
            "Set it to \"commit\" or \"changeset-file\"."
        )
    if value not in _VALID_COVERAGE_UNITS:
        raise ConfigError(
            f"coverage_unit must be one of {_VALID_COVERAGE_UNITS!r}, "
            f"got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Pending-file operations (changeset-file mode)
# ---------------------------------------------------------------------------

def get_pending_dir(changes_dir: str) -> str:
    """Return the path to the pending/ subdirectory inside changes_dir."""
    return os.path.join(changes_dir, "pending")


def write_pending_file(pending_dir: str, entry: ChangelogEntry) -> str:
    """Write a single pending file as ``<id>.json`` in pending_dir.

    The file contains the entry fields minus ``commits`` (changeset-file
    mode entries have no commits).  Creates the directory if needed.
    Returns the full path to the written file.
    """
    os.makedirs(pending_dir, exist_ok=True)
    if not entry.id:
        raise ChangelogError("entry must have an id for pending file creation")

    data: dict = {"id": entry.id, "user_facing": entry.user_facing}
    if entry.description is not None:
        data["description"] = entry.description
    if entry.type is not None:
        data["type"] = entry.type
    if entry.release_type is not None:
        data["release_type"] = entry.release_type
    if entry.packages is not None:
        data["packages"] = entry.packages

    filepath = os.path.join(pending_dir, f"{entry.id}.json")
    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=pending_dir, suffix=".tmp")
    try:
        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.replace(tmp_path, filepath)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return filepath


def read_pending_files(pending_dir: str) -> list[ChangelogEntry]:
    """Read all ``*.json`` files in pending_dir and return ChangelogEntry objects.

    Returns an empty list if the directory does not exist or is empty.
    Files are sorted by name (lexicographic, which gives approximate
    chronological order since IDs are timestamp-prefixed).
    """
    if not os.path.isdir(pending_dir):
        return []
    entries = []
    for name in sorted(os.listdir(pending_dir)):
        if not name.endswith(".json"):
            continue
        filepath = os.path.join(pending_dir, name)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise ChangelogError(
                f"malformed pending file {filepath}: {exc}"
            ) from exc
        entries.append(ChangelogEntry(
            id=data.get("id"),
            user_facing=data.get("user_facing", False),
            description=data.get("description"),
            type=data.get("type"),
            release_type=data.get("release_type"),
            packages=data.get("packages"),
            commits=[],  # pending files have no commits
        ))
    return entries


def finalize_changeset_version(changes_dir: str, version: str) -> None:
    """Concatenate pending files into ``x.y.z.jsonl`` and clear pending/.

    1. Read all ``pending/*.json`` files.
    2. Serialize each into a JSONL line (preserving ``id``, ``packages``).
    3. Write ``x.y.z.jsonl`` (read-only 0o444).
    4. Remove all files from ``pending/``.

    Raises ``FileNotFoundError`` if the pending directory does not exist.
    Raises ``ChangelogError`` if the versioned JSONL already exists.
    """
    pending_dir = get_pending_dir(changes_dir)
    if not os.path.isdir(pending_dir):
        raise FileNotFoundError(f"pending/ not found in {changes_dir}")

    dst = os.path.join(changes_dir, f"{version}.jsonl")
    if os.path.exists(dst):
        raise ChangelogError(
            f"refusing to finalize changelog for {version}: {dst} already "
            f"exists. Inspect the existing file and remove it manually "
            f"before re-releasing."
        )

    entries = read_pending_files(pending_dir)

    # Write JSONL
    lines = [serialize_entry(e) + "\n" for e in entries]
    content = "".join(lines)

    fd, tmp_path = tempfile.mkstemp(dir=changes_dir, suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.replace(tmp_path, dst)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    os.chmod(dst, 0o444)

    # Clear pending files
    for name in os.listdir(pending_dir):
        filepath = os.path.join(pending_dir, name)
        if os.path.isfile(filepath):
            os.unlink(filepath)


def unfinalize_changeset_version(changes_dir: str, version: str) -> list[str]:
    """Reverse a changeset-mode finalization: restore pending files from JSONL.

    1. Read ``x.y.z.jsonl``.
    2. For each entry with an ``id``, write ``pending/<id>.json``.
    3. Delete ``x.y.z.jsonl`` and ``x.y.z.md`` if present.
    4. Return the list of changed file paths.

    Returns an empty list if the versioned file doesn't exist.
    """
    versioned = os.path.join(changes_dir, f"{version}.jsonl")
    versioned_md = os.path.join(changes_dir, f"{version}.md")
    pending_dir = get_pending_dir(changes_dir)

    if not os.path.isfile(versioned):
        return []

    # Make writable so we can delete it
    os.chmod(versioned, 0o644)
    entries = parse_jsonl(versioned)

    os.makedirs(pending_dir, exist_ok=True)
    changed: list[str] = []

    for entry in entries:
        if entry.id:
            path = write_pending_file(pending_dir, entry)
            changed.append(path)

    os.unlink(versioned)
    changed.append(versioned)

    if os.path.isfile(versioned_md):
        os.unlink(versioned_md)
        changed.append(versioned_md)

    return changed
