"""JSONL-based structured changelog for rlsbl, storing one append-only entry per commit so coverage, validation, and CHANGELOG.md generation all derive from the same machine-readable source.

Public API re-exported from submodules.
"""

from .schema import (
    ChangelogEntry,
    generate_entry_id,
    parse_entry,
    parse_jsonl,
    serialize_entry,
    validate_schema,
)

from .files import (
    RemapReport,
    RemapResult,
    append_entry,
    changes_dir_exists,
    finalize_changeset_version,
    finalize_version,
    get_changes_dir,
    get_pending_dir,
    is_read_only,
    list_versioned_files,
    read_coverage_unit,
    read_pending_files,
    read_unreleased,
    remap_jsonl_hashes,
    unfinalize_changeset_version,
    unfinalize_version,
    write_pending_file,
    writable_jsonl,
)

from .generate import generate_changelog, generate_version_file, generate_version_section

from .resolve import resolve_hash, resolve_hashes

from .validate import validate_unreleased

__all__ = [
    "ChangelogEntry",
    "RemapReport",
    "finalize_changeset_version",
    "generate_entry_id",
    "get_pending_dir",
    "read_coverage_unit",
    "read_pending_files",
    "unfinalize_changeset_version",
    "write_pending_file",
    "RemapResult",
    "append_entry",
    "changes_dir_exists",
    "finalize_version",
    "generate_changelog",
    "generate_version_file",
    "generate_version_section",
    "get_changes_dir",
    "is_read_only",
    "list_versioned_files",
    "parse_entry",
    "parse_jsonl",
    "read_unreleased",
    "remap_jsonl_hashes",
    "resolve_hash",
    "resolve_hashes",
    "serialize_entry",
    "unfinalize_version",
    "validate_schema",
    "validate_unreleased",
    "writable_jsonl",
]
