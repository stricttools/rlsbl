"""Committed lineage records: an append-only log of repository-surgery facts.

A LINEAGE RECORD is a JSONL file, one event per line, recording what a
repository conversion actually did -- which tags were renamed, which commits a
history rewrite moved, which tag globs departed with an extracted sub-project,
which published identity changed and from which version. It is written by the
operation that performs the surgery and read afterwards by anything that has to
explain how the repository reached its current shape.

It records history; it never drives it. Nothing here decides anything -- a
reader consults the record to EXPLAIN a divergence it already observed.

Where the file lives
--------------------

One resolution function, :func:`get_lineage_path`, decides the path, and both
locations hold the same format:

- explicit-monorepo mode: inside the releasable's state directory,
  ``.rlsbl-monorepo/releasables/<name>/lineage.jsonl`` -- pass
  ``releasable_dir`` (build it with
  :func:`rlsbl.workspace_types.get_releasable_dir`);
- standalone repos, including a standalone successor produced by an extract:
  ``<project>/.rlsbl/lineage.jsonl``.

This mirrors :func:`rlsbl.release_file.get_releases_dir` exactly -- the same
``releasable_dir``-or-``.rlsbl`` fork, so the two state homes never drift apart.

On-disk format and the append pattern
-------------------------------------

One JSON object per line, each stamped with ``format_version`` as its leading
key -- the same shape and the same append mechanics as the JSONL changelog
(:func:`rlsbl.changelog.files._append_entry_to_file`): create the parent through
the effect seam, then one ``effects.append_text`` of one line. A pure append has
no read-modify-write window, so two writers can never lose each other's event
the way the whole-file rewrite in
:func:`rlsbl.evidence_gate.write_undo_audit` can. That audit trail is where the
append-record idea comes from; the line-per-event carrier is what lets a
malformed record be reported by FILE AND LINE, and is the only carrier the
strictspec per-line ``format_version`` gate applies to.

Validation and where errors fire
--------------------------------

The strictspec-generated validator
(``rlsbl/strictspec_gen/lineage_event_validator.py``, schema
``.strictspec/lineage-event.schema.toml``) is the document authority for one
line: the ``format_version`` gate, the ``kind`` discriminator and its arm set,
field types, enums, required fields, and unknown-key rejection. rlsbl keeps only
what strictspec cannot see -- whether a recorded SHA still resolves, whether a
recorded tag still exists, cross-event ``related_to`` correlation.

There is no legacy mode. Every line rlsbl has ever written carries
``format_version = 1``; a line without it, or with any other value, is a hard
error. The format is new, so there is no pre-gate history to accommodate.

ERROR SITING: the hard error fires in :func:`read_events`, the point where a
record is read FOR USE. Detection code that merely asks whether a repository has
a lineage record calls :func:`lineage_file_exists`, which touches only the
filesystem and can never raise on content -- so a malformed record breaks the
one command that consumes it, never every command that walks the tree.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime
from typing import ClassVar

from . import effects
from .errors import RlsblError


LINEAGE_FILENAME = "lineage.jsonl"

# The current on-disk format for one lineage line. Every line rlsbl serializes
# carries this as its per-line ``format_version`` gate. Bump only alongside a
# strictspec schema format_version bump + migration.
CURRENT_FORMAT_VERSION = 1

KIND_CONVERSION = "conversion"
KIND_TAG_MAP = "tag-map"
KIND_ANCHOR_REMAP = "anchor-remap"
KIND_DEPARTED_GLOBS = "departed-globs"
KIND_BOUNDARY_ALIAS = "boundary-alias"
KIND_IDENTITY_TRANSITION = "identity-transition"
KIND_PROMOTION_SPLIT_MAP = "promotion-split-map"


class LineageError(RlsblError):
    """Malformed lineage record, or an event that fails its schema."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def get_lineage_path(project_dir: str = ".", *, releasable_dir: str | None = None) -> str:
    """Return the path to this project's lineage record.

    ``releasable_dir`` is the releasable's state directory
    (``.rlsbl-monorepo/releasables/<name>/``); when given, the record sits
    directly in it, beside ``version`` and ``releases/``. Otherwise it is the
    standalone home, ``<project_dir>/.rlsbl/lineage.jsonl`` -- which is also
    where a standalone successor produced by an extract finds its own record.

    The file may or may not exist: an absent record means no surgery has been
    recorded, which is the normal state for a repository that has never been
    converted.
    """
    if releasable_dir:
        return os.path.join(releasable_dir, LINEAGE_FILENAME)
    return os.path.join(project_dir, ".rlsbl", LINEAGE_FILENAME)


def lineage_file_exists(path: str) -> bool:
    """True when a lineage record file is present at ``path``.

    DETECTION ONLY. It reads no content and validates nothing, so scanning code
    that runs on every command can ask this without a malformed record turning
    into a repository-wide hard error. The error belongs at the read-for-use
    site, :func:`read_events`.
    """
    return os.path.isfile(path)


# ---------------------------------------------------------------------------
# Event identity and time
# ---------------------------------------------------------------------------


def new_event_id() -> str:
    """Generate a unique lineage event id.

    Timestamp-prefixed UUID4 hex, so ids sort approximately by creation order
    without an external dependency: ``<16 hex ns><32 hex uuid4>``.

    This deliberately mirrors ``rlsbl.changelog.schema.generate_entry_id``
    rather than importing it. A later phase has the changelog reading lineage
    anchor remaps, and importing the changelog package from here would close
    that loop into an import cycle. Two independent record systems each owning
    their own id generator is the cost of keeping them independent.
    """
    return format(time.time_ns(), "016x") + uuid.uuid4().hex


def now_timestamp() -> str:
    """Current local time as RFC 3339 with a UTC offset, to the second.

    The schema declares ``recorded_at`` as an offset datetime, so the offset is
    mandatory and ``+02:00``-style -- not the ``+0200`` that ``time.strftime``
    produces.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Nested value records
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class LineageEndpoint:
    """One side of a conversion: which repository, and which slice of it."""

    repo: str
    path: str | None = None
    project: str | None = None
    releasable: str | None = None
    tag_format: str | None = None


@dataclass(kw_only=True)
class TagMapping:
    """One old-tag -> new-tag correspondence."""

    old_tag: str
    new_tag: str
    new_commit: str
    old_commit: str | None = None


@dataclass(kw_only=True)
class AnchorMapping:
    """One old-SHA -> new-SHA correspondence produced by a history rewrite."""

    old_sha: str
    new_sha: str


@dataclass(kw_only=True)
class BoundaryAlias:
    """One alias tag created at a conversion point."""

    alias_tag: str
    aliased_tag: str
    commit: str


@dataclass(kw_only=True)
class SplitMapping:
    """One monorepo-commit -> subtree-split-commit correspondence."""

    source_sha: str
    split_sha: str


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class _LineageEventBase:
    """Fields every lineage event carries.

    ``id`` and ``recorded_at`` are optional at construction and stamped by
    :func:`append_events`, so a writer states only the fact it is recording.
    """

    KIND: ClassVar[str]
    # field name -> (nested dataclass, is_list)
    NESTED: ClassVar[dict[str, tuple[type, bool]]] = {}

    id: str | None = None
    recorded_at: str | None = None
    related_to: str | None = None


@dataclass(kw_only=True)
class ConversionEvent(_LineageEventBase):
    """A sub-project extracted out of a workspace, or a repository absorbed in."""

    KIND: ClassVar[str] = KIND_CONVERSION
    NESTED: ClassVar[dict[str, tuple[type, bool]]] = {
        "source": (LineageEndpoint, False),
        "destination": (LineageEndpoint, False),
    }

    direction: str  # "extract" | "absorb"
    source: LineageEndpoint
    destination: LineageEndpoint
    commit: str


@dataclass(kw_only=True)
class TagMapEvent(_LineageEventBase):
    """The tag renames a conversion performed."""

    KIND: ClassVar[str] = KIND_TAG_MAP
    NESTED: ClassVar[dict[str, tuple[type, bool]]] = {"mappings": (TagMapping, True)}

    mappings: list[TagMapping]


@dataclass(kw_only=True)
class AnchorRemapEvent(_LineageEventBase):
    """The old-SHA -> new-SHA correspondence a history rewrite produced."""

    KIND: ClassVar[str] = KIND_ANCHOR_REMAP
    NESTED: ClassVar[dict[str, tuple[type, bool]]] = {"mappings": (AnchorMapping, True)}

    rewrite: str
    mappings: list[AnchorMapping]


@dataclass(kw_only=True)
class DepartedGlobsEvent(_LineageEventBase):
    """Tag globs that stopped belonging here because their sub-project left."""

    KIND: ClassVar[str] = KIND_DEPARTED_GLOBS
    NESTED: ClassVar[dict[str, tuple[type, bool]]] = {
        "destination": (LineageEndpoint, False)
    }

    globs: list[str]
    destination: LineageEndpoint


@dataclass(kw_only=True)
class BoundaryAliasEvent(_LineageEventBase):
    """Alias tags created at a conversion point."""

    KIND: ClassVar[str] = KIND_BOUNDARY_ALIAS
    NESTED: ClassVar[dict[str, tuple[type, bool]]] = {"aliases": (BoundaryAlias, True)}

    aliases: list[BoundaryAlias]


@dataclass(kw_only=True)
class IdentityTransitionEvent(_LineageEventBase):
    """A published identity changed, effective from a stated version."""

    KIND: ClassVar[str] = KIND_IDENTITY_TRANSITION

    facet: str  # see the schema enum: go-module-path, package-name, ...
    old: str
    new: str
    effective_version: str


@dataclass(kw_only=True)
class PromotionSplitMapEvent(_LineageEventBase):
    """The subtree-split correspondence persisted when a mirror is promoted."""

    KIND: ClassVar[str] = KIND_PROMOTION_SPLIT_MAP
    NESTED: ClassVar[dict[str, tuple[type, bool]]] = {"mappings": (SplitMapping, True)}

    subtree_path: str
    mirror_remote: str
    mappings: list[SplitMapping]
    promoted_version: str | None = None


LineageEvent = (
    ConversionEvent
    | TagMapEvent
    | AnchorRemapEvent
    | DepartedGlobsEvent
    | BoundaryAliasEvent
    | IdentityTransitionEvent
    | PromotionSplitMapEvent
)

EVENT_CLASSES: dict[str, type] = {
    cls.KIND: cls
    for cls in (
        ConversionEvent,
        TagMapEvent,
        AnchorRemapEvent,
        DepartedGlobsEvent,
        BoundaryAliasEvent,
        IdentityTransitionEvent,
        PromotionSplitMapEvent,
    )
}

EVENT_KINDS: tuple[str, ...] = tuple(EVENT_CLASSES)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _plain(value):
    """Recursively convert nested value dataclasses to dicts, dropping None."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _plain(getattr(value, f.name))
            for f in fields(value)
            if getattr(value, f.name) is not None
        }
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def serialize_event(event) -> str:
    """Serialize one event to a single JSON line (no trailing newline).

    ``format_version`` leads (the per-line gate), then ``kind``, then the
    event's own fields in declaration order. ``None``-valued optional fields are
    omitted, so a line carries exactly the facts that were stated.
    """
    data: dict = {"format_version": CURRENT_FORMAT_VERSION, "kind": event.KIND}
    for f in fields(event):
        value = getattr(event, f.name)
        if value is None:
            continue
        data[f.name] = _plain(value)
    return json.dumps(data, separators=(",", ":"))


def _build_nested(cls, raw, field_name: str):
    """Construct one nested value dataclass from a raw JSON object."""
    if not isinstance(raw, dict):
        raise LineageError(f"field {field_name} must be a JSON object")
    return cls(**raw)


def parse_event(line: str):
    """Parse one JSON line into the event dataclass its ``kind`` selects.

    Raises :class:`LineageError` on malformed JSON, a missing or unsupported
    ``format_version``, an unknown ``kind``, a missing required field, an
    unknown key, or any other schema violation. There is no tolerant mode: a
    record that cannot be read is never half-read.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LineageError(f"malformed JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LineageError("event must be a JSON object")

    _gate_line(line)
    _validate_line(line)

    # strictspec has accepted the line, so `kind` is present and is one of the
    # declared arms; the lookup below cannot miss.
    cls = EVENT_CLASSES[data["kind"]]

    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        nested = cls.NESTED.get(f.name)
        if nested is None:
            kwargs[f.name] = raw
            continue
        nested_cls, is_list = nested
        if is_list:
            kwargs[f.name] = [
                _build_nested(nested_cls, item, f.name) for item in raw
            ]
        else:
            kwargs[f.name] = _build_nested(nested_cls, raw, f.name)
    return cls(**kwargs)


def _gate_line(line: str) -> None:
    """Run the strictspec per-line ``format_version`` gate.

    Unlike the changelog, absence is an error too: the lineage format was born
    with the gate, so there is no legacy line to accommodate and no config key
    that could turn enforcement off.
    """
    import strictspec

    from .strictspec_gen import lineage_event_validator as validator

    result = strictspec.version_gate(validator._program, line.encode("utf-8"), "jsonl")
    if result.ok:
        return
    raise LineageError("; ".join(d.message for d in result.diagnostics))


def _validate_line(line: str) -> None:
    """Validate one line's full shape through the strictspec validator."""
    from .strictspec_gen import lineage_event_validator as validator

    _root, diags = validator.validate_bytes(line.encode("utf-8"), "jsonl")
    if diags:
        raise LineageError("; ".join(d.message for d in diags))


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def append_events(path: str, events) -> list:
    """Append events to the lineage record at ``path``, in the order given.

    Each event is stamped with an ``id`` and a ``recorded_at`` when it does not
    carry them, validated, and written as one line. The stamped copies are
    returned -- the caller's objects are left alone, and a caller that needs the
    ids it just wrote (to reference them from a later event's ``related_to``)
    reads them off the return value.

    The write is one append per line through the effect seam, creating the
    parent directory when missing. Appending never reads the existing file, so a
    concurrent writer cannot be clobbered and an already-written event is never
    rewritten.

    Every event is validated BEFORE anything is written, so an invalid event in
    the batch aborts the whole append rather than leaving a partial record.
    """
    stamped = []
    lines = []
    for event in events:
        e = replace(
            event,
            id=event.id or new_event_id(),
            recorded_at=event.recorded_at or now_timestamp(),
        )
        line = serialize_event(e)
        _gate_line(line)
        _validate_line(line)
        stamped.append(e)
        lines.append(line)

    if not lines:
        return []

    parent = os.path.dirname(path)
    if parent:
        effects.makedirs(parent, exist_ok=True)
    effects.append_text(path, "".join(line + "\n" for line in lines))
    return stamped


def append_event(path: str, event):
    """Append one event. Returns the stamped copy as written."""
    return append_events(path, [event])[0]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_events(path: str, *, kinds=None) -> list:
    """Read the lineage record at ``path`` and return its events in order.

    An absent file yields an empty list: no record means no surgery was ever
    recorded, which is the normal state.

    THIS IS THE READ-FOR-USE SITE, so this is where malformed content is a hard
    error. Any unreadable line -- bad JSON, unknown ``kind``, missing required
    field, wrong ``format_version`` -- raises :class:`LineageError` naming the
    file and the line number. ``kinds`` filters the RESULT, never the
    validation: a malformed line of a kind the caller did not ask for still
    stops the read, because a record that cannot be read in full cannot be
    trusted in part.
    """
    if not os.path.isfile(path):
        return []

    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, raw in enumerate(f, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                events.append(parse_event(stripped))
            except LineageError as exc:
                raise LineageError(f"{path}:{line_num}: {exc}") from exc

    if kinds is None:
        return events
    wanted = set(kinds)
    return [e for e in events if e.KIND in wanted]
