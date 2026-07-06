"""JSONL changelog entry schema providing the dataclass definition, JSON parsing, serialization, field validation, and type coercion."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from ..errors import ChangelogError


VALID_RELEASE_TYPES = ("ota", "build")


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


def validate_schema(entry: ChangelogEntry, *, coverage_unit: str = "commit") -> list[str]:
    """Return a list of schema errors for the entry. Empty list means valid.

    ``coverage_unit`` controls mode-dependent validation:

    - ``"commit"`` (default): ``commits`` is required, ``id`` is optional.
    - ``"changeset-file"``: ``commits`` is forbidden, ``id`` is required.
    """
    errors: list[str] = []
    if coverage_unit == "commit":
        if not entry.commits:
            errors.append("commits is empty")
    elif coverage_unit == "changeset-file":
        if entry.commits:
            errors.append("commits must be empty in changeset-file mode")
        if not entry.id:
            errors.append("id is required in changeset-file mode")
    else:
        errors.append(f"unknown coverage_unit: {coverage_unit!r}")
    if entry.user_facing:
        if not entry.description:
            errors.append("user_facing entry missing description")
        if not entry.type:
            errors.append("user_facing entry missing type")
    if entry.release_type is not None and entry.release_type not in VALID_RELEASE_TYPES:
        errors.append(
            f"invalid release_type: {entry.release_type!r} "
            f"(must be one of {VALID_RELEASE_TYPES})"
        )
    if entry.id is not None and not isinstance(entry.id, str):
        errors.append("id must be a string")
    if entry.packages is not None:
        if not isinstance(entry.packages, list):
            errors.append("packages must be a list of strings")
        elif not all(isinstance(p, str) for p in entry.packages):
            errors.append("packages must be a list of strings")
    return errors


def parse_entry(line: str) -> ChangelogEntry:
    """Parse one JSON line into a ChangelogEntry.

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

    Only includes non-None optional fields to keep lines compact.
    Omits ``commits`` when the list is empty (changeset-file mode).
    """
    data: dict = {}
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


def parse_jsonl(path: str) -> list[ChangelogEntry]:
    """Read a .jsonl file and return a list of ChangelogEntry objects.

    Raises ChangelogError with line number on malformed JSON.
    """
    entries: list[ChangelogEntry] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(parse_entry(stripped))
            except ChangelogError as exc:
                raise ChangelogError(f"line {line_num}: {exc}") from exc
    return entries
