"""JSONL changelog entry schema providing the dataclass definition, JSON parsing, serialization, field validation, and type coercion."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..errors import ChangelogError


VALID_RELEASE_TYPES = ("ota", "build")


@dataclass
class ChangelogEntry:
    """One line in a .jsonl changelog file."""

    commits: list[str] = field(default_factory=list)
    user_facing: bool = False
    description: str | None = None
    type: str | None = None
    release_type: str | None = None  # "ota" or "build" for Flutter targets


def validate_schema(entry: ChangelogEntry) -> list[str]:
    """Return a list of schema errors for the entry. Empty list means valid."""
    errors: list[str] = []
    if not entry.commits:
        errors.append("commits is empty")
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
    return errors


def parse_entry(line: str) -> ChangelogEntry:
    """Parse one JSON line into a ChangelogEntry.

    Raises ChangelogError on malformed JSON or missing required fields.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ChangelogError(f"malformed JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ChangelogError("entry must be a JSON object")

    if "commits" not in data:
        raise ChangelogError("missing required field: commits")
    if "user_facing" not in data:
        raise ChangelogError("missing required field: user_facing")

    commits = data["commits"]
    if not isinstance(commits, list):
        raise ChangelogError("commits must be a list")

    return ChangelogEntry(
        commits=commits,
        user_facing=data["user_facing"],
        description=data.get("description"),
        type=data.get("type"),
        release_type=data.get("release_type"),
    )


def serialize_entry(entry: ChangelogEntry) -> str:
    """Serialize a ChangelogEntry to one JSON line (no trailing newline).

    Only includes non-None optional fields to keep lines compact.
    """
    data: dict = {
        "commits": entry.commits,
        "user_facing": entry.user_facing,
    }
    if entry.description is not None:
        data["description"] = entry.description
    if entry.type is not None:
        data["type"] = entry.type
    if entry.release_type is not None:
        data["release_type"] = entry.release_type
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
