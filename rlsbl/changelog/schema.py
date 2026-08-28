"""JSONL changelog entry schema with dataclass definition, JSON parsing, serialization, field validation, type coercion, and entry ID generation.

The strictspec-generated validator
(``rlsbl/strictspec_gen/changelog_entry_commit_validator.py``) is the DOCUMENT
authority for one JSONL line: the per-line ``format_version`` gate and the
field/enum/conditional-required shape. This module routes shape validation
through it and keeps only what strictspec cannot see (hash resolution, tag
ranges, coverage vs git, batch limits, cross-file rules) native -- those live in
``validate.py`` and ``files.py``.

Transition contract (see docs/changelog.md): every line rlsbl WRITES carries
``format_version = 1``. Reading is EXPLICIT two-mode -- a line carrying
``format_version`` is validated via strictspec; a line lacking it is legacy and
accepted ONLY when the caller opts into legacy mode
(``enforce_format_version=False``, the transition default). With
``enforce_format_version=True`` a missing gate is a hard error. The absence is
never silent: a warn-level check surfaces "enforcement not yet enabled" until a
repo records its ``changelog_format_version_enforced`` decision in its config.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field

from ..errors import ChangelogError


VALID_RELEASE_TYPES = ("ota", "build")
VALID_TYPES = ("feature", "fix", "breaking")

# The current on-disk format for one JSONL changelog line. Every line rlsbl
# serializes carries this as its per-line ``format_version`` gate. Bump only
# alongside a strictspec schema format_version bump + migration.
CURRENT_FORMAT_VERSION = 1


def generate_entry_id() -> str:
    """Generate a unique entry ID.

    Uses a timestamp-prefixed UUID4 hex for approximate lexicographic
    sortability without adding external dependencies.  Format:
    ``<timestamp_hex><uuid4_hex>`` (48 chars total: 16 timestamp + 32 uuid).
    """
    # Nanosecond timestamp gives enough precision; hex-encode for compactness.
    ts_hex = format(time.time_ns(), "016x")
    return ts_hex + uuid.uuid4().hex


@dataclass
class ChangelogEntry:
    """One line in a .jsonl changelog file."""

    commits: list[str] = field(default_factory=list)
    user_facing: bool = False
    description: str | None = None
    type: str | None = None
    release_type: str | None = None  # "ota" or "build" for Flutter targets
    id: str | None = None  # stable ULID-style identifier; optional on read for historical compat
    packages: list[str] | None = None  # optional: affected member packages in a releasable


# Diagnostic-message field extractors. strictspec message text is pinned
# (spec/appendix-surface-syntax.md), so these patterns are stable.
_RE_MISSING = re.compile(r"Missing required field (\w+) ")
_RE_CONDITIONAL = re.compile(r"Field (\w+) at .* is required when")
_RE_UNKNOWN = re.compile(r"Unknown key (\w+) ")
_RE_PATH_FIELD = re.compile(r"\$\.(\w+)")


def _native_message(diag, entry: ChangelogEntry) -> str:
    """Render one strictspec diagnostic as an rlsbl-native schema error string.

    strictspec is the shape engine; this is a thin presentation adapter that
    preserves the historical rlsbl wording (which the check layer and tests
    read) without a second validation implementation.
    """
    code = diag.code
    if code == "STRICTSPEC_TYPE_MISSING_REQUIRED":
        m = _RE_MISSING.search(diag.message)
        field_name = m.group(1) if m else "?"
        if field_name == "commits":
            return "commits is empty"
        return f"missing required field: {field_name}"
    if code == "STRICTSPEC_INTRA_CONDITIONAL_REQUIRED":
        m = _RE_CONDITIONAL.search(diag.message)
        field_name = m.group(1) if m else "?"
        if field_name == "description":
            return "user_facing entry missing description"
        if field_name == "type":
            return "user_facing entry missing type"
        return f"user_facing entry missing {field_name}"
    if code == "STRICTSPEC_TYPE_NOT_ENUM_MEMBER":
        m = _RE_PATH_FIELD.search(diag.path)
        field_name = m.group(1) if m else "?"
        if field_name == "type":
            return f"invalid type: {entry.type!r} (must be one of {VALID_TYPES})"
        if field_name == "release_type":
            return (
                f"invalid release_type: {entry.release_type!r} "
                f"(must be one of {VALID_RELEASE_TYPES})"
            )
        return diag.message
    if code == "STRICTSPEC_KEY_UNKNOWN":
        m = _RE_UNKNOWN.search(diag.message)
        field_name = m.group(1) if m else "?"
        return f"unknown key: {field_name}"
    if code in ("STRICTSPEC_TYPE_NOT_ARRAY", "STRICTSPEC_TYPE_NOT_STRING"):
        # A wrong-typed `packages` value (not a list, or a list with a non-string
        # item) keeps rlsbl's historical wording. The path is `$.packages` for
        # the container and `$.packages[N]` for an item; both extract "packages".
        m = _RE_PATH_FIELD.search(diag.path)
        field_name = m.group(1) if m else "?"
        if field_name == "packages":
            return "packages must be a list of strings"
        return diag.message
    return diag.message


def validate_schema(entry: ChangelogEntry) -> list[str]:
    """Return a list of schema errors for the entry. Empty list means valid.

    ``commits`` is required and ``id`` is optional. The entry is serialized
    (stamping ``format_version = CURRENT_FORMAT_VERSION``) and validated through
    the strictspec-generated validator -- the single shape engine. Diagnostics
    are rendered back into rlsbl's native wording by :func:`_native_message`.
    """
    from ..strictspec_gen import changelog_entry_commit_validator as validator

    line = serialize_entry(entry).encode("utf-8")
    _root, diags = validator.validate_bytes(line, "jsonl")
    return [_native_message(d, entry) for d in diags]


def _gate_line(line: str) -> None:
    """Run the strictspec per-line ``format_version`` gate on a raw JSONL line.

    Uses the validator's compiled program, but checks only the gate, never entry
    shape. Raises ChangelogError when
    ``format_version`` is present but not accepted (e.g. a future/wrong value).
    A line with NO ``format_version`` passes here silently -- the legacy/enforced
    decision is the caller's (see :func:`parse_entry`).
    """
    import strictspec

    from ..strictspec_gen import changelog_entry_commit_validator as _v

    result = strictspec.version_gate(_v._program, line.encode("utf-8"), "jsonl")
    if result.ok:
        return
    for d in result.diagnostics:
        # GATE_ABSENT (no format_version) is the caller's decision, not an error
        # here; anything else (unsupported/wrong version) is a hard error.
        if d.code != "STRICTSPEC_GATE_ABSENT":
            raise ChangelogError(d.message)


def parse_entry(line: str, *, enforce_format_version: bool = False) -> ChangelogEntry:
    """Parse one JSON line into a ChangelogEntry.

    The per-line ``format_version`` gate is routed through strictspec:

    - a line carrying ``format_version`` is validated via strictspec (a wrong or
      unsupported version is a hard error);
    - a line lacking ``format_version`` is LEGACY. It is accepted only when
      ``enforce_format_version`` is False (the transition default). With
      ``enforce_format_version=True`` a missing gate is a hard error telling the
      operator to stamp the line, re-add the entry, or record a deliberate
      legacy-mode decision in ``.rlsbl/config.json``.

    Raises ChangelogError on malformed JSON or missing required fields.
    Historical entries without ``id`` load fine (``id`` is optional on read).
    Entries without ``commits`` load with an empty commits list; the
    ``changelog-schema`` check is what rejects them.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ChangelogError(f"malformed JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ChangelogError("entry must be a JSON object")

    # Per-line format_version gate (strictspec is the authority for stamped
    # lines; the legacy allowance is explicit).
    _gate_line(line)
    if "format_version" not in data and enforce_format_version:
        raise ChangelogError(
            "missing format_version: this changelog line predates the "
            'format_version gate. Add \'"format_version":1\' to the line (or '
            "re-add the entry with `rlsbl changelog add`, which always stamps "
            "it), or set \"changelog_format_version_enforced\": false in "
            ".rlsbl/config.json to read this repo in legacy mode."
        )

    if "user_facing" not in data:
        raise ChangelogError("missing required field: user_facing")

    # commits defaults to empty list when absent; validate_schema is the
    # authority that rejects an entry with no commits.
    commits = data.get("commits", [])
    if not isinstance(commits, list):
        raise ChangelogError("commits must be a list")

    return ChangelogEntry(
        commits=commits,
        user_facing=data["user_facing"],
        description=data.get("description"),
        type=data.get("type"),
        release_type=data.get("release_type"),
        id=data.get("id"),
        packages=data.get("packages"),
    )


def serialize_entry(entry: ChangelogEntry) -> str:
    """Serialize a ChangelogEntry to one JSON line (no trailing newline).

    Every line is stamped with ``format_version = CURRENT_FORMAT_VERSION`` as the
    leading key (the per-line gate). Only includes non-None optional fields to
    keep lines compact. Omits ``commits`` when the list is empty.
    """
    data: dict = {"format_version": CURRENT_FORMAT_VERSION}
    if entry.id is not None:
        data["id"] = entry.id
    if entry.commits:
        data["commits"] = entry.commits
    data["user_facing"] = entry.user_facing
    if entry.description is not None:
        data["description"] = entry.description
    if entry.type is not None:
        data["type"] = entry.type
    if entry.release_type is not None:
        data["release_type"] = entry.release_type
    if entry.packages is not None:
        data["packages"] = entry.packages
    return json.dumps(data, separators=(",", ":"))


def entry_content_key(entry: ChangelogEntry) -> tuple:
    """The identity of an entry that carries no ``id``: what the entry SAYS.

    ``id`` is optional on read, so a historical line has none and cannot be
    recognized by it. The fallback is the same identity the changelog's own
    consolidation dedup uses -- the commit SET plus the fields that carry
    meaning -- which is what makes "have I already copied this entry?"
    answerable for a line that predates entry ids.

    The commits are a frozenset because order is not part of an entry's
    identity: the same commits listed in another order are the same entry.
    """
    return (
        frozenset(entry.commits),
        bool(entry.user_facing),
        entry.description,
        entry.type,
        entry.release_type,
    )


def parse_jsonl(path: str, *, enforce_format_version: bool = False) -> list[ChangelogEntry]:
    """Read a .jsonl file and return a list of ChangelogEntry objects.

    Raises ChangelogError with line number on malformed JSON. When
    ``enforce_format_version`` is True, a line lacking ``format_version`` is a
    hard error (the caller threads this from the project's
    ``changelog_format_version_enforced`` config -- see
    :func:`rlsbl.changelog.files.read_changelog_format_version_enforced`).
    """
    entries: list[ChangelogEntry] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(
                    parse_entry(stripped, enforce_format_version=enforce_format_version)
                )
            except ChangelogError as exc:
                raise ChangelogError(f"line {line_num}: {exc}") from exc
    return entries
