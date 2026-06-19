"""File management layer for JSONL changelog files including reading, writing, appending entries, and path resolution for .rlsbl directories."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import os
import re
import stat
import sys
import tempfile

from .schema import ChangelogEntry, parse_entry, parse_jsonl, serialize_entry
from ..errors import ChangelogError

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.jsonl$")


@dataclass
class RemapResult:
    """Result of remapping hashes in one JSONL file."""
    path: str
    entries_modified: int
    hashes_remapped: int


def _parse_semver(filename: str) -> tuple[int, int, int] | None:
    """Extract (major, minor, patch) from a versioned filename, or None."""
    m = _VERSION_RE.match(filename)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def get_changes_dir(project_path: str) -> str:
    """Return the path to .rlsbl/changes/ inside the project."""
    return os.path.join(project_path, ".rlsbl", "changes")


def changes_dir_exists(project_path: str) -> bool:
    """Check if .rlsbl/changes/ exists in the project."""
    return os.path.isdir(get_changes_dir(project_path))


def list_versioned_files(changes_dir: str) -> list[tuple[str, str]]:
    """List all x.y.z.jsonl files, sorted by semver (newest first).

    Returns (version_string, filepath) pairs.
    """
    results: list[tuple[tuple[int, int, int], str, str]] = []
    if not os.path.isdir(changes_dir):
        return []
    for name in os.listdir(changes_dir):
        semver = _parse_semver(name)
        if semver is not None:
            version_str = f"{semver[0]}.{semver[1]}.{semver[2]}"
            results.append((semver, version_str, os.path.join(changes_dir, name)))
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
    from .resolve import resolve_hashes
    from .validate import _git_log_hashes, _unreleased_range

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
    with open(src, "w", encoding="utf-8") as f:
        pass  # empty file


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


def remap_jsonl_hashes(changes_dir, sha_map):
    """Replace commit hashes in all JSONL files using a mapping.

    Scans unreleased.jsonl and all versioned *.jsonl files in changes_dir.
    Only modifies files that contain hashes present in sha_map.
    Uses writable_jsonl to handle read-only versioned files.

    Returns a list of RemapResult for each file that was modified.
    Returns an empty list if changes_dir does not exist or no files match.
    """
    if not os.path.isdir(changes_dir):
        return []

    results = []

    # Collect all JSONL files: unreleased + versioned
    jsonl_files = []
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    if os.path.isfile(unreleased):
        jsonl_files.append(unreleased)
    for _version, path in list_versioned_files(changes_dir):
        jsonl_files.append(path)

    for filepath in jsonl_files:
        entries = parse_jsonl(filepath)
        entries_modified = 0
        hashes_remapped = 0

        new_entries = []
        for entry in entries:
            new_commits = []
            entry_changed = False
            for h in entry.commits:
                if h in sha_map:
                    new_commits.append(sha_map[h])
                    hashes_remapped += 1
                    entry_changed = True
                else:
                    new_commits.append(h)
            if entry_changed:
                entries_modified += 1
            new_entries.append(ChangelogEntry(
                commits=new_commits,
                user_facing=entry.user_facing,
                description=entry.description,
                type=entry.type,
                release_type=entry.release_type,
                packages=entry.packages,
            ))

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

    return results
