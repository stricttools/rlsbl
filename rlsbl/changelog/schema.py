"""JSONL changelog entry schema providing the dataclass definition, JSON parsing, serialization, field validation, and type coercion.

The strictspec-generated validators
(``rlsbl/strictspec_gen/changelog_entry_commit_validator.py`` and
``changelog_entry_changeset_file_validator.py``) are the DOCUMENT authority for
one JSONL line: the per-line ``format_version`` gate and the field/enum/
conditional-required shape. This module routes shape validation through them and
keeps only what strictspec cannot see (hash resolution, tag ranges, coverage vs
git, batch limits, cross-file rules) native -- those live in ``validate.py`` and
``files.py``.

Transition contract (see docs/changelog.md): every line rlsbl WRITES carries
``format_version = 1``. Reading is EXPLICIT two-mode -- a line carrying
``format_version`` is validated via strictspec; a line lacking it is legacy and
accepted ONLY when the caller opts into legacy mode
(``enforce_format_version=False``, the transition default). With
``enforce_format_version=True`` a missing gate is a hard error. The absence is
never silent: a warn-level check surfaces "enforcement not yet enabled" until a
repo runs the one-time stamping script and sets
``changelog_format_version_enforced`` in its config.
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


def _mode_validator(coverage_unit: str):
    """Return the strictspec-generated validator module for a coverage mode."""
    if coverage_unit == "commit":
        from ..strictspec_gen import changelog_entry_commit_validator as _v
        return _v
    from ..strictspec_gen import changelog_entry_changeset_file_validator as _v
    return _v


# Diagnostic-message field extractors. strictspec message text is pinned
# (spec/appendix-surface-syntax.md), so these patterns are stable.
_RE_MISSING = re.compile(r"Missing required field (\w+) ")
_RE_CONDITIONAL = re.compile(r"Field (\w+) at .* is required when")
_RE_UNKNOWN = re.compile(r"Unknown key (\w+) ")
_RE_PATH_FIELD = re.compile(r"\$\.(\w+)")


def _native_message(diag, entry: ChangelogEntry, coverage_unit: str) -> str:
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
        if field_name == "id":
            return "id is required in changeset-file mode"
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
        if field_name == "commits":
            return "commits must be empty in changeset-file mode"
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


def validate_schema(entry: ChangelogEntry, *, coverage_unit: str = "commit") -> list[str]:
    """Return a list of schema errors for the entry. Empty list means valid.

    ``coverage_unit`` selects which strictspec schema is the authority:

    - ``"commit"`` (default): ``commits`` is required, ``id`` is optional.
    - ``"changeset-file"``: ``commits`` is forbidden, ``id`` is required.

    The entry is serialized (stamping ``format_version = CURRENT_FORMAT_VERSION``)
    and validated through the mode-appropriate strictspec-generated validator --
    the single shape engine. Diagnostics are rendered back into rlsbl's native
    wording by :func:`_native_message`.
    """
    if coverage_unit not in ("commit", "changeset-file"):
        return [f"unknown coverage_unit: {coverage_unit!r}"]
    validator = _mode_validator(coverage_unit)
    line = serialize_entry(entry).encode("utf-8")
    _root, diags = validator.validate_bytes(line, "jsonl")
    return [_native_message(d, entry, coverage_unit) for d in diags]


def _gate_line(line: str) -> None:
    """Run the strictspec per-line ``format_version`` gate on a raw JSONL line.

    Uses the commit-mode validator's compiled program, but the gate is
    mode-neutral (both schemas declare the identical ``format_version`` gate),
    so this checks only the gate, never entry shape. Raises ChangelogError when
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
      ``enforce_format_version=True`` a missing gate is a hard error directing
      the operator to run the one-time stamping script.

    Raises ChangelogError on malformed JSON or missing required fields.
    Historical entries without ``id`` load fine (``id`` is optional on read).
    Entries without ``commits`` are allowed (changeset-file mode entries
    stored in finalized JSONL may have an empty commits list).
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
            "format_version gate. Run scripts/stamp_changelog_format_version.py "
            "to stamp the changes dir, then set "
            '"changelog_format_version_enforced": true in .rlsbl/config.json.'
        )

    if "user_facing" not in data:
        raise ChangelogError("missing required field: user_facing")

    # commits defaults to empty list when absent (changeset-file mode entries
    # stored in finalized JSONL have no commits field).
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
    keep lines compact. Omits ``commits`` when the list is empty (changeset-file
    mode).
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
