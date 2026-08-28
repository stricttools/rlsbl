"""File management layer for JSONL changelog files including reading, writing, appending entries, and path resolution for .rlsbl directories."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

import os
import stat
import sys

from .schema import ChangelogEntry, parse_entry, parse_jsonl, serialize_entry
from ..errors import ChangelogError, ConfigError
from ..release_file import archive_sort_key, is_release_version
from .. import effects

# Sort key type: (major, minor, patch, is_stable, preid_rank, counter)
_SemverKey = tuple[int, int, int, int, int, int]

# The all-zeros SHA git-filter-repo uses as the target for pruned commits.
# Never a valid remap destination -- rewriting a real hash to this would
# silently corrupt a changelog entry.
NULL_SHA = "0" * 40


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
    """Extract a sort key from a versioned ``{version}.jsonl`` filename, or None.

    The version vocabulary and the ordering are :mod:`rlsbl.release_file`'s --
    the same grammar the release archives are named and ordered by, because the
    same release flow names both files for the same version. This carries only
    what is specific to a changelog file: the ``.jsonl`` extension. It used to
    restate the pattern, the pre-release channel ranks and the ordering, so a
    new channel had to be added in two places to be recognized in both.

    Returns ``(major, minor, patch, is_stable, preid_rank, counter)`` where
    ``is_stable`` is 1 for stable versions, so they sort after every
    pre-release of the same base.
    """
    suffix = ".jsonl"
    if not filename.endswith(suffix):
        return None
    version = filename[: -len(suffix)]
    if not is_release_version(version):
        return None
    return archive_sort_key(version)


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


def read_unreleased(
    changes_dir: str, *, enforce_format_version: bool = False
) -> list[ChangelogEntry]:
    """Read unreleased.jsonl and return entries. Empty list if file missing.

    ``enforce_format_version`` is threaded to :func:`parse_jsonl`: when True, a
    line lacking ``format_version`` is a hard error.
    """
    path = os.path.join(changes_dir, "unreleased.jsonl")
    if not os.path.isfile(path):
        return []
    return parse_jsonl(path, enforce_format_version=enforce_format_version)


def append_entry(changes_dir: str, entry: ChangelogEntry) -> None:
    """Append one entry to unreleased.jsonl atomically.

    Writes the serialized line to a temp file, then appends it to the target.
    Creates the changes directory and unreleased.jsonl if they don't exist.
    """
    effects.makedirs(changes_dir, exist_ok=True)
    target = os.path.join(changes_dir, "unreleased.jsonl")
    _append_entry_to_file(target, entry)


def append_entry_to_version(changes_dir: str, version: str, entry: ChangelogEntry) -> None:
    """Append one entry to a versioned JSONL file (e.g., 0.39.0.jsonl).

    The caller is responsible for unlocking/re-locking the file if it is read-only.
    """
    target = os.path.join(changes_dir, f"{version}.jsonl")
    _append_entry_to_file(target, entry)


def _append_entry_to_file(target: str, entry: ChangelogEntry) -> None:
    """Append one entry to any JSONL file, creating parents when missing.

    One append of one line, through :func:`rlsbl.effects.append_lines` -- the
    shared append the lineage record uses too.  Prior content is never read back
    and rewritten, so a `changelog add` racing another one cannot clobber the
    entry it just wrote, and a torn last line cannot swallow the new entry: the
    helper leads the append with a separating newline when the file does not end
    in one.

    It used to stage the line in a ``tempfile.mkstemp`` file and then copy that
    into the target, which bought nothing -- the copy was itself a plain append,
    so a crash mid-write could truncate the target either way -- and cost purity:
    ``mkstemp`` creates its file unconditionally, so under --dry-run the
    recorded cleanup never ran and the preview left a stray ``.tmp`` in
    ``.rlsbl/changes/``.
    """
    parent = os.path.dirname(target)
    effects.makedirs(parent, exist_ok=True)
    effects.append_lines(target, [serialize_entry(entry)])


def _warn_stale_entries(src: str, tag_glob: str) -> None:
    """Warn on stderr for entries in unreleased.jsonl referencing out-of-range commits.

    In monorepo mode, an entry whose commits all sit before the release the
    LEDGER anchors this checkout to is stale — typically left over from a
    sibling project's release. We emit a warning per stale entry but do not
    strip them (warn-only). The ledger is the release archives beside *src*'s
    own changes directory.
    """
    # Local imports to avoid circular dependency at module load time.
    from .resolve import resolve_hashes, _git_log_hashes
    from ..ledger import releases_dir_for_changes_dir, unreleased_range

    releases_dir = releases_dir_for_changes_dir(os.path.dirname(src))
    range_spec = unreleased_range(releases_dir, tag_glob=tag_glob)
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
    effects.rename(src, dst)
    effects.chmod(dst, 0o444)

    # Create a new empty unreleased.jsonl
    effects.open_write(src, "w", encoding="utf-8").close()


def _jsonl_lines(path: str) -> list[str]:
    """The non-blank lines of a JSONL file, verbatim and without newlines.

    Read as TEXT rather than parsed: an un-finalize puts lines back exactly as
    they were written, so a field this version of the parser does not model --
    or would re-serialize differently -- survives untouched.
    """
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def unfinalize_version(changes_dir: str, version: str) -> list[str]:
    """Reverse a finalize_version: restore x.y.z.jsonl back to unreleased.jsonl.

    1. Makes the versioned file writable.
    2. MERGES its entries back into unreleased.jsonl -- released entries first,
       then whatever accumulated after the release -- and removes it.
    3. Deletes the per-version .md file if present.
    4. Returns the list of changed file paths (for committing).

    Returns an empty list if the versioned file doesn't exist.

    The merge is the whole point of step 2. Between a release and the undo of
    that release, entries land in the fresh ``unreleased.jsonl``; renaming the
    versioned file over it destroyed every one of them, and the undo that did
    it reported success. Order is released-then-new, which is the order the two
    sets of commits were made in.
    """
    versioned = os.path.join(changes_dir, f"{version}.jsonl")
    unreleased = os.path.join(changes_dir, "unreleased.jsonl")
    versioned_md = os.path.join(changes_dir, f"{version}.md")

    if not os.path.isfile(versioned):
        return []

    effects.chmod(versioned, 0o644)

    post_release = _jsonl_lines(unreleased)
    if post_release:
        merged = "".join(
            line + "\n" for line in _jsonl_lines(versioned) + post_release
        )
        # 0o644, never the versioned file's lock: the result is the LIVE
        # unreleased file that the next `changelog add` appends to.
        effects.atomic_write_text(unreleased, merged, file_mode=0o644)
        effects.remove(versioned)
    else:
        effects.rename(versioned, unreleased)

    changed: list[str] = [unreleased]

    if os.path.isfile(versioned_md):
        effects.remove(versioned_md)
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
        effects.chmod(path, 0o644)
    try:
        yield path
    finally:
        if was_ro:
            effects.chmod(path, 0o444)


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
            # preserve_mode: a remap rewrites a file that already exists, and
            # rewriting it must not change what it is. Pinning 0o600 here (the
            # mode the hand-rolled mkstemp write happened to leave) turned an
            # ordinary 644 changelog into an owner-only one every time a
            # rewrite touched it. writable_jsonl relocks released files on exit
            # regardless.
            effects.atomic_write_text(filepath, content, preserve_mode=True)

        results.append(RemapResult(
            path=filepath,
            entries_modified=entries_modified,
            hashes_remapped=hashes_remapped,
        ))

    return RemapReport(results=results, unmapped=unmapped, ambiguous=ambiguous)


def load_filter_repo_commit_map(path: str) -> "tuple[dict[str, str], list[str]]":
    """Load a git-filter-repo ``commit-map`` into a clean ``{old: new}`` dict.

    git-filter-repo writes ``.git/filter-repo/commit-map`` with two quirks
    that make it unsafe to feed straight into :func:`remap_jsonl_hashes`:

    - A header row of the literal tokens ``old`` and ``new`` (whitespace
      padded). Ingested naively it becomes a junk ``{"old": "new"}`` entry.
    - Pruned commits map to the all-zeros :data:`NULL_SHA`. Ingested naively
      they would rewrite surviving real hashes to nothing, corrupting the
      changelog. This is the actual corruption vector.

    Returns ``(sha_map, pruned)`` where ``sha_map`` maps surviving old SHAs to
    their new SHAs (header and null-target rows excluded) and ``pruned`` is the
    list of old SHAs whose commits were dropped (null target), so callers can
    log how many entries reference now-deleted commits.
    """
    sha_map: dict[str, str] = {}
    pruned: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            old, new = parts[0], parts[1]
            if old == "old" and new == "new":
                continue  # git-filter-repo header row
            if new == NULL_SHA:
                pruned.append(old)  # pruned commit -- has no surviving target
                continue
            sha_map[old] = new
    return sha_map, pruned


# Config key that flips a repo from legacy (mixed-format tolerant) to enforced
# (every changelog line must carry format_version). There is NO enforced default
# value: absence is legacy AND is itself reported by a warn-level check, so the
# transition is visible, never silent. A repo sets this to true once every line
# in its changes dir(s) carries format_version.
CHANGELOG_FORMAT_VERSION_ENFORCED_KEY = "changelog_format_version_enforced"


def read_changelog_format_version_enforced(config: dict) -> "tuple[bool, bool]":
    """Read the ``changelog_format_version_enforced`` flag from a config dict.

    Returns ``(enforced, key_present)``:

    - key ABSENT -> ``(False, False)``: legacy mode. There is no enforced
      default; the absence is surfaced by the ``changelog-format-version`` warn
      check ("enforcement not yet enabled").
    - key present and boolean -> ``(value, True)``.

    A present-but-non-boolean value is a hard error (:class:`ConfigError`) --
    invalid config is never silently coerced.
    """
    if CHANGELOG_FORMAT_VERSION_ENFORCED_KEY not in config:
        return (False, False)
    value = config[CHANGELOG_FORMAT_VERSION_ENFORCED_KEY]
    if not isinstance(value, bool):
        raise ConfigError(
            f"{CHANGELOG_FORMAT_VERSION_ENFORCED_KEY} in .rlsbl/config.json must "
            f"be a boolean, got {value!r} (type {type(value).__name__})."
        )
    return (value, True)
