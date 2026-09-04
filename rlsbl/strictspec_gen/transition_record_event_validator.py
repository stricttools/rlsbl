# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.2.3
# schema:              rlsbl-transition-record-event (format_version 1)
# regenerate:          strictspec gen --manifest strictspec.toml
#
# Released under the MIT license (unencumbered). This file is machine-generated;
# edit the schema and regenerate, never this file.
# ruff: noqa
from __future__ import annotations

from dataclasses import dataclass, replace

import strictspec
from strictspec import Diagnostic, Value

# GENERATED_BY is the strictspec release that produced this file. The runtime
# pairing guard hard-errors unless it matches the linked runtime exactly.
GENERATED_BY = "0.2.3"
SCHEMA_FORMAT_VERSION = 1

# _EMBEDDED_SCHEMA carries the compiled schema (and its imported type-definition
# files and scalar manifest) so the validator is self-contained and does no IO.
_EMBEDDED_SCHEMA = {
    "transition-record-event.schema.toml": "# strictspec schema for one rlsbl TRANSITION RECORD line.\n# Source of truth: rlsbl/transition_record.py (the event dataclasses and read_events).\n# One document = one JSONL line = one transition record event.\n#\n# WHAT A TRANSITION RECORD IS: a committed, append-only log of repository-surgery\n# FACTS -- what a conversion did, which tags were renamed, which commits a\n# history rewrite moved. It is written once by the operation that performed the\n# surgery and read afterwards by anything that needs to explain how the\n# repository got to its current shape. It records history; it never drives it.\n#\n# SCOPE: strictspec owns the raw DOCUMENT SHAPE -- the per-line format_version\n# gate, the `kind` discriminator and its arm set, field types, enums, required\n# fields, and unknown-key rejection. rlsbl keeps everything strictspec cannot\n# see native: whether a recorded SHA still resolves in this repository, whether\n# a recorded tag still exists, and any cross-event correlation (`related_to`\n# pointing at an event id in the same or another file).\n\nname = \"rlsbl-transition-record-event\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"jsonl\"\nrole = \"schema\"\nroot = \"TransitionRecordEvent\"\ntargets = [\"python\"]\ndescription = \"One JSONL transition record line: a single repository-surgery fact, discriminated by `kind`.\"\n\n# ---------------------------------------------------------------------------\n# Shared scalar refinements\n# ---------------------------------------------------------------------------\n\n[types.GitSha]\ntype = \"string\"\nregex = \"^[0-9a-f]{7,40}$\"\ndescription = \"A git commit hash, abbreviated (>= 7) or full (40). Transition record writers always record the FULL 40-character form; the shorter bound exists so a hand-written record is readable rather than rejected. Resolution against the object database is rlsbl-native, not a shape check.\"\n\n# ---------------------------------------------------------------------------\n# Shared records\n# ---------------------------------------------------------------------------\n\n[types.TransitionRecordEndpoint]\ntype = \"record\"\ndescription = \"One side of a conversion: which repository, and which slice of it.\"\n\n[types.TransitionRecordEndpoint.fields.repo]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Repository identity. A remote URL for a repository other than the one holding this record, or \\\".\\\" for this repository itself.\"\n\n[types.TransitionRecordEndpoint.fields.path]\ntype = \"string\"\nrequired = false\ndescription = \"Repo-relative subtree path of this endpoint. Absent means the repository root (a standalone repo). Present means a monorepo sub-project directory.\"\n\n[types.TransitionRecordEndpoint.fields.project]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"Workspace project name at this endpoint, when the endpoint is inside an rlsbl workspace.\"\n\n[types.TransitionRecordEndpoint.fields.releasable]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"Releasable group name at this endpoint, when the endpoint's versioning is owned by a releasable.\"\n\n[types.TransitionRecordEndpoint.fields.tag_format]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The tag format in effect at this endpoint (e.g. \\\"v{version}\\\" standalone, \\\"{name}@v{version}\\\" in a workspace). Recorded because a conversion is exactly where it changes.\"\n\n[types.TagMapping]\ntype = \"record\"\ndescription = \"One old-tag -> new-tag correspondence.\"\n\n[types.TagMapping.fields.old_tag]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The tag name as it existed before the conversion.\"\n\n[types.TagMapping.fields.new_tag]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The tag name after the conversion.\"\n\n[types.TagMapping.fields.old_commit]\ntype = \"GitSha\"\nrequired = false\ndescription = \"The commit the old tag pointed at, in the PRE-rewrite object graph. Absent when the pre-rewrite graph is not available to the writer (e.g. tags imported from a remote that was never fetched in full).\"\n\n[types.TagMapping.fields.new_commit]\ntype = \"GitSha\"\nrequired = true\ndescription = \"The commit the new tag points at, in THIS repository's object graph.\"\n\n[types.ReleaseCommitMapping]\ntype = \"record\"\ndescription = \"One old-SHA -> new-SHA correspondence produced by a history rewrite.\"\n\n[types.ReleaseCommitMapping.fields.old_sha]\ntype = \"GitSha\"\nrequired = true\ndescription = \"The commit hash before the rewrite. Typically unresolvable in this repository afterwards -- that is the point of recording it.\"\n\n[types.ReleaseCommitMapping.fields.new_sha]\ntype = \"GitSha\"\nrequired = true\ndescription = \"The commit hash after the rewrite.\"\n\n[types.BoundaryAlias]\ntype = \"record\"\ndescription = \"One alias tag created at a conversion point so a pre-conversion version stays addressable under the post-conversion naming.\"\n\n[types.BoundaryAlias.fields.alias_tag]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The alias tag name created (in the post-conversion tag format).\"\n\n[types.BoundaryAlias.fields.aliased_tag]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The pre-existing tag the alias duplicates (in the pre-conversion tag format).\"\n\n[types.BoundaryAlias.fields.commit]\ntype = \"GitSha\"\nrequired = true\ndescription = \"The commit both tags point at.\"\n\n[types.SplitMapping]\ntype = \"record\"\ndescription = \"One monorepo-commit -> subtree-split-commit correspondence.\"\n\n[types.SplitMapping.fields.source_sha]\ntype = \"GitSha\"\nrequired = true\ndescription = \"The commit in the monorepo's own history.\"\n\n[types.SplitMapping.fields.split_sha]\ntype = \"GitSha\"\nrequired = true\ndescription = \"The corresponding commit in the deterministic subtree split pushed to the mirror.\"\n\n# ---------------------------------------------------------------------------\n# Event arms\n#\n# Every arm repeats the four common fields (format_version, id, recorded_at,\n# kind) plus the optional `related_to`. They are repeated rather than factored\n# into a wrapper because `kind` is the discriminator and must sit at the\n# document root for the union to select an arm without an extra nesting level.\n# ---------------------------------------------------------------------------\n\n[types.ConversionEvent]\ntype = \"record\"\ndescription = \"A repository conversion: a sub-project extracted out of a workspace, or an external repository absorbed into one.\"\n\n[types.ConversionEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version gate. Every line rlsbl writes carries 1.\"\n\n[types.ConversionEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file. Other events reference it via `related_to`.\"\n\n[types.ConversionEvent.fields.recorded_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event was appended (RFC 3339, with offset).\"\n\n[types.ConversionEvent.fields.kind]\ntype = \"literal\"\nvalue = \"conversion\"\nrequired = true\n\n[types.ConversionEvent.fields.related_to]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The id of an earlier event this one elaborates. Absent on a standalone event.\"\n\n[types.ConversionEvent.fields.direction]\ntype = \"enum\"\nrequired = true\nvalues = [\"extract\", \"absorb\"]\ndescription = \"\\\"extract\\\" moved a sub-project out of a workspace into its own repository; \\\"absorb\\\" moved an external repository in as a sub-project.\"\n\n[types.ConversionEvent.fields.source]\ntype = \"TransitionRecordEndpoint\"\nrequired = true\ndescription = \"Where the code came from.\"\n\n[types.ConversionEvent.fields.destination]\ntype = \"TransitionRecordEndpoint\"\nrequired = true\ndescription = \"Where the code went.\"\n\n[types.ConversionEvent.fields.commit]\ntype = \"GitSha\"\nrequired = true\ndescription = \"The commit in THIS repository at which the conversion took effect -- the merge/rewrite commit for an absorb, the last pre-removal commit for an extract.\"\n\n[types.TagMapEvent]\ntype = \"record\"\ndescription = \"The tag renames a conversion performed. Split from the conversion event because a conversion may rename hundreds of tags and the mapping is read on its own.\"\n\n[types.TagMapEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version gate. Every line rlsbl writes carries 1.\"\n\n[types.TagMapEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.TagMapEvent.fields.recorded_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event was appended (RFC 3339, with offset).\"\n\n[types.TagMapEvent.fields.kind]\ntype = \"literal\"\nvalue = \"tag-map\"\nrequired = true\n\n[types.TagMapEvent.fields.related_to]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The id of the conversion event whose tags these are.\"\n\n[types.TagMapEvent.fields.mappings]\ntype = \"array\"\nrequired = true\nmin_len = 1\ndescription = \"The tag renames. A tag-map event with no mappings is not a fact worth recording, so the array is non-empty.\"\n[types.TagMapEvent.fields.mappings.item]\ntype = \"TagMapping\"\n\n[types.ReleaseCommitRemapEvent]\ntype = \"record\"\ndescription = \"The commit correspondence a history rewrite produced. \\\"Release commit\\\" is any SHA another rlsbl record points at -- a changelog entry's commits, a tag's target, an earlier transition record event's commit -- which the rewrite invalidated.\"\n\n[types.ReleaseCommitRemapEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version gate. Every line rlsbl writes carries 1.\"\n\n[types.ReleaseCommitRemapEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.ReleaseCommitRemapEvent.fields.recorded_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event was appended (RFC 3339, with offset).\"\n\n[types.ReleaseCommitRemapEvent.fields.kind]\ntype = \"literal\"\nvalue = \"anchor-remap\"\nrequired = true\n\n[types.ReleaseCommitRemapEvent.fields.related_to]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The id of the conversion event whose rewrite produced this mapping.\"\n\n[types.ReleaseCommitRemapEvent.fields.rewrite]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"What performed the rewrite, in the writer's own words (e.g. \\\"git-filter-repo --to-subdirectory-filter\\\"). Free text: it explains the mapping to a human, it is never dispatched on.\"\n\n[types.ReleaseCommitRemapEvent.fields.mappings]\ntype = \"array\"\nrequired = true\nmin_len = 1\ndescription = \"The old-SHA -> new-SHA pairs. Non-empty: a rewrite that moved nothing is not recorded.\"\n[types.ReleaseCommitRemapEvent.fields.mappings.item]\ntype = \"ReleaseCommitMapping\"\n\n[types.DepartedGlobsEvent]\ntype = \"record\"\ndescription = \"Tag globs that stopped belonging to this repository because the sub-project they named was extracted. A reader that still sees tags matching a departed glob knows they are residue, not live releases.\"\n\n[types.DepartedGlobsEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version gate. Every line rlsbl writes carries 1.\"\n\n[types.DepartedGlobsEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.DepartedGlobsEvent.fields.recorded_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event was appended (RFC 3339, with offset).\"\n\n[types.DepartedGlobsEvent.fields.kind]\ntype = \"literal\"\nvalue = \"departed-globs\"\nrequired = true\n\n[types.DepartedGlobsEvent.fields.related_to]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The id of the conversion event that made these globs depart.\"\n\n[types.DepartedGlobsEvent.fields.globs]\ntype = \"array\"\nrequired = true\nmin_len = 1\ndescription = \"The tag globs (in the same syntax rlsbl's tag_glob matching uses) that left.\"\n[types.DepartedGlobsEvent.fields.globs.item]\ntype = \"string\"\nnon_empty = true\n\n[types.DepartedGlobsEvent.fields.destination]\ntype = \"TransitionRecordEndpoint\"\nrequired = true\ndescription = \"The repository the globs went to, so a reader can follow them.\"\n\n[types.BoundaryAliasEvent]\ntype = \"record\"\ndescription = \"Alias tags created at a conversion point. Historical releases keep their original tag names; the aliases make the version immediately before the conversion addressable under the new naming too.\"\n\n[types.BoundaryAliasEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version gate. Every line rlsbl writes carries 1.\"\n\n[types.BoundaryAliasEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.BoundaryAliasEvent.fields.recorded_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event was appended (RFC 3339, with offset).\"\n\n[types.BoundaryAliasEvent.fields.kind]\ntype = \"literal\"\nvalue = \"boundary-alias\"\nrequired = true\n\n[types.BoundaryAliasEvent.fields.related_to]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The id of the conversion event these aliases mark the boundary of.\"\n\n[types.BoundaryAliasEvent.fields.aliases]\ntype = \"array\"\nrequired = true\nmin_len = 1\ndescription = \"The alias tags created. Non-empty: an event recording no alias records nothing.\"\n[types.BoundaryAliasEvent.fields.aliases.item]\ntype = \"BoundaryAlias\"\n\n[types.IdentityTransitionEvent]\ntype = \"record\"\ndescription = \"A change to a published identity of the project -- the string consumers use to depend on it -- effective from a stated version. A Go module path change is the archetype.\"\n\n[types.IdentityTransitionEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version gate. Every line rlsbl writes carries 1.\"\n\n[types.IdentityTransitionEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.IdentityTransitionEvent.fields.recorded_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event was appended (RFC 3339, with offset).\"\n\n[types.IdentityTransitionEvent.fields.kind]\ntype = \"literal\"\nvalue = \"identity-transition\"\nrequired = true\n\n[types.IdentityTransitionEvent.fields.related_to]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The id of the conversion event that forced the transition, when one did.\"\n\n[types.IdentityTransitionEvent.fields.facet]\ntype = \"enum\"\nrequired = true\nvalues = [\"go-module-path\", \"package-name\", \"releasable-name\", \"project-name\", \"tag-format\", \"repository-url\"]\ndescription = \"WHICH identity changed. A closed set on purpose: every arm names something a consumer or a tool resolves by, and adding a facet is a deliberate schema change, not an accident of free text.\"\n\n[types.IdentityTransitionEvent.fields.old]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The identity before the transition.\"\n\n[types.IdentityTransitionEvent.fields.new]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The identity after the transition.\"\n\n[types.IdentityTransitionEvent.fields.effective_version]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The project version from which the new identity is in effect. Releases at and after this version use `new`; releases before it use `old`.\"\n\n[types.PromotionSplitMapEvent]\ntype = \"record\"\ndescription = \"The subtree-split correspondence persisted when a mirror is promoted. Recorded because the split is deterministic but expensive to recompute, and a later reader needs to map a monorepo commit to the mirror commit that carries it.\"\n\n[types.PromotionSplitMapEvent.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version gate. Every line rlsbl writes carries 1.\"\n\n[types.PromotionSplitMapEvent.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier for this event, unique within the file.\"\n\n[types.PromotionSplitMapEvent.fields.recorded_at]\ntype = \"datetime\"\nrequired = true\ndatetime_kind = \"offset\"\ndescription = \"When the event was appended (RFC 3339, with offset).\"\n\n[types.PromotionSplitMapEvent.fields.kind]\ntype = \"literal\"\nvalue = \"promotion-split-map\"\nrequired = true\n\n[types.PromotionSplitMapEvent.fields.related_to]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The id of a related event, when the promotion accompanies a conversion.\"\n\n[types.PromotionSplitMapEvent.fields.subtree_path]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The repo-relative path that was split out.\"\n\n[types.PromotionSplitMapEvent.fields.mirror_remote]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The remote URL the split was pushed to.\"\n\n[types.PromotionSplitMapEvent.fields.promoted_version]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The project version at which the promotion happened, when the promotion was tied to a release.\"\n\n[types.PromotionSplitMapEvent.fields.mappings]\ntype = \"array\"\nrequired = true\nmin_len = 1\ndescription = \"The source-commit -> split-commit pairs. Non-empty: an empty split map records nothing.\"\n[types.PromotionSplitMapEvent.fields.mappings.item]\ntype = \"SplitMapping\"\n\n# ---------------------------------------------------------------------------\n# The document root: one line is exactly one event, selected by `kind`.\n# ---------------------------------------------------------------------------\n\n[types.TransitionRecordEvent]\ntype = \"discriminated-union\"\ndiscriminator = \"kind\"\ndescription = \"One transition record line. The `kind` field selects the arm; an unrecognized kind is a hard error, never a skipped line.\"\n\n[types.TransitionRecordEvent.arms.conversion]\ntype = \"ConversionEvent\"\n\n[types.TransitionRecordEvent.arms.tag-map]\ntype = \"TagMapEvent\"\n\n[types.TransitionRecordEvent.arms.anchor-remap]\ntype = \"ReleaseCommitRemapEvent\"\n\n[types.TransitionRecordEvent.arms.departed-globs]\ntype = \"DepartedGlobsEvent\"\n\n[types.TransitionRecordEvent.arms.boundary-alias]\ntype = \"BoundaryAliasEvent\"\n\n[types.TransitionRecordEvent.arms.identity-transition]\ntype = \"IdentityTransitionEvent\"\n\n[types.TransitionRecordEvent.arms.promotion-split-map]\ntype = \"PromotionSplitMapEvent\"\n",
}
_EMBEDDED_MAIN_FILE = "transition-record-event.schema.toml"

# Version pairing: generated code and runtime must be the same release. This runs
# at import, so a skewed runtime hard-errors before any validation is attempted.
strictspec.require_runtime_version(GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the phase-2
    constraint vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return v, result.diagnostics


def validate_value(v: Value) -> tuple[Value | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return v, result.diagnostics


@dataclass(frozen=True, kw_only=True)
class TransitionRecordEndpoint:
    """Frozen typed binding of the "TransitionRecordEndpoint" record. Immutable; use with_* for
    copy-on-write.
    """

    repo: str
    path: str
    project: str
    releasable: str
    tag_format: str

    def with_repo(self, v: str) -> TransitionRecordEndpoint:
        return replace(self, repo=v)

    def with_path(self, v: str) -> TransitionRecordEndpoint:
        return replace(self, path=v)

    def with_project(self, v: str) -> TransitionRecordEndpoint:
        return replace(self, project=v)

    def with_releasable(self, v: str) -> TransitionRecordEndpoint:
        return replace(self, releasable=v)

    def with_tag_format(self, v: str) -> TransitionRecordEndpoint:
        return replace(self, tag_format=v)


def _bind_TransitionRecordEndpoint(v: Value) -> TransitionRecordEndpoint | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_repo = v.field("repo")
    f_path = v.field("path")
    f_project = v.field("project")
    f_releasable = v.field("releasable")
    f_tag_format = v.field("tag_format")
    return TransitionRecordEndpoint(
        repo=(f_repo[0].string()[0] if f_repo[1] else ""),
        path=(f_path[0].string()[0] if f_path[1] else ""),
        project=(f_project[0].string()[0] if f_project[1] else ""),
        releasable=(f_releasable[0].string()[0] if f_releasable[1] else ""),
        tag_format=(f_tag_format[0].string()[0] if f_tag_format[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class TagMapping:
    """Frozen typed binding of the "TagMapping" record. Immutable; use with_* for
    copy-on-write.
    """

    old_tag: str
    new_tag: str
    old_commit: str
    new_commit: str

    def with_old_tag(self, v: str) -> TagMapping:
        return replace(self, old_tag=v)

    def with_new_tag(self, v: str) -> TagMapping:
        return replace(self, new_tag=v)

    def with_old_commit(self, v: str) -> TagMapping:
        return replace(self, old_commit=v)

    def with_new_commit(self, v: str) -> TagMapping:
        return replace(self, new_commit=v)


def _bind_TagMapping(v: Value) -> TagMapping | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_old_tag = v.field("old_tag")
    f_new_tag = v.field("new_tag")
    f_old_commit = v.field("old_commit")
    f_new_commit = v.field("new_commit")
    return TagMapping(
        old_tag=(f_old_tag[0].string()[0] if f_old_tag[1] else ""),
        new_tag=(f_new_tag[0].string()[0] if f_new_tag[1] else ""),
        old_commit=(f_old_commit[0].string()[0] if f_old_commit[1] else ""),
        new_commit=(f_new_commit[0].string()[0] if f_new_commit[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class ReleaseCommitMapping:
    """Frozen typed binding of the "ReleaseCommitMapping" record. Immutable; use with_* for
    copy-on-write.
    """

    old_sha: str
    new_sha: str

    def with_old_sha(self, v: str) -> ReleaseCommitMapping:
        return replace(self, old_sha=v)

    def with_new_sha(self, v: str) -> ReleaseCommitMapping:
        return replace(self, new_sha=v)


def _bind_ReleaseCommitMapping(v: Value) -> ReleaseCommitMapping | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_old_sha = v.field("old_sha")
    f_new_sha = v.field("new_sha")
    return ReleaseCommitMapping(
        old_sha=(f_old_sha[0].string()[0] if f_old_sha[1] else ""),
        new_sha=(f_new_sha[0].string()[0] if f_new_sha[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class BoundaryAlias:
    """Frozen typed binding of the "BoundaryAlias" record. Immutable; use with_* for
    copy-on-write.
    """

    alias_tag: str
    aliased_tag: str
    commit: str

    def with_alias_tag(self, v: str) -> BoundaryAlias:
        return replace(self, alias_tag=v)

    def with_aliased_tag(self, v: str) -> BoundaryAlias:
        return replace(self, aliased_tag=v)

    def with_commit(self, v: str) -> BoundaryAlias:
        return replace(self, commit=v)


def _bind_BoundaryAlias(v: Value) -> BoundaryAlias | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_alias_tag = v.field("alias_tag")
    f_aliased_tag = v.field("aliased_tag")
    f_commit = v.field("commit")
    return BoundaryAlias(
        alias_tag=(f_alias_tag[0].string()[0] if f_alias_tag[1] else ""),
        aliased_tag=(f_aliased_tag[0].string()[0] if f_aliased_tag[1] else ""),
        commit=(f_commit[0].string()[0] if f_commit[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class SplitMapping:
    """Frozen typed binding of the "SplitMapping" record. Immutable; use with_* for
    copy-on-write.
    """

    source_sha: str
    split_sha: str

    def with_source_sha(self, v: str) -> SplitMapping:
        return replace(self, source_sha=v)

    def with_split_sha(self, v: str) -> SplitMapping:
        return replace(self, split_sha=v)


def _bind_SplitMapping(v: Value) -> SplitMapping | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_source_sha = v.field("source_sha")
    f_split_sha = v.field("split_sha")
    return SplitMapping(
        source_sha=(f_source_sha[0].string()[0] if f_source_sha[1] else ""),
        split_sha=(f_split_sha[0].string()[0] if f_split_sha[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class ConversionEvent:
    """Frozen typed binding of the "ConversionEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    recorded_at: str
    kind: str
    related_to: str
    direction: str
    source: TransitionRecordEndpoint
    destination: TransitionRecordEndpoint
    commit: str

    def with_format_version(self, v: int) -> ConversionEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> ConversionEvent:
        return replace(self, id=v)

    def with_recorded_at(self, v: str) -> ConversionEvent:
        return replace(self, recorded_at=v)

    def with_kind(self, v: str) -> ConversionEvent:
        return replace(self, kind=v)

    def with_related_to(self, v: str) -> ConversionEvent:
        return replace(self, related_to=v)

    def with_direction(self, v: str) -> ConversionEvent:
        return replace(self, direction=v)

    def with_source(self, v: TransitionRecordEndpoint) -> ConversionEvent:
        return replace(self, source=v)

    def with_destination(self, v: TransitionRecordEndpoint) -> ConversionEvent:
        return replace(self, destination=v)

    def with_commit(self, v: str) -> ConversionEvent:
        return replace(self, commit=v)


def _bind_ConversionEvent(v: Value) -> ConversionEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_recorded_at = v.field("recorded_at")
    f_kind = v.field("kind")
    f_related_to = v.field("related_to")
    f_direction = v.field("direction")
    f_source = v.field("source")
    f_destination = v.field("destination")
    f_commit = v.field("commit")
    return ConversionEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        recorded_at=(f_recorded_at[0].datetime()[0] if f_recorded_at[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        related_to=(f_related_to[0].string()[0] if f_related_to[1] else ""),
        direction=(f_direction[0].string()[0] if f_direction[1] else ""),
        source=(_bind_TransitionRecordEndpoint(f_source[0]) if f_source[1] else None),
        destination=(_bind_TransitionRecordEndpoint(f_destination[0]) if f_destination[1] else None),
        commit=(f_commit[0].string()[0] if f_commit[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class TagMapEvent:
    """Frozen typed binding of the "TagMapEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    recorded_at: str
    kind: str
    related_to: str
    mappings: list[TagMapping]

    def with_format_version(self, v: int) -> TagMapEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> TagMapEvent:
        return replace(self, id=v)

    def with_recorded_at(self, v: str) -> TagMapEvent:
        return replace(self, recorded_at=v)

    def with_kind(self, v: str) -> TagMapEvent:
        return replace(self, kind=v)

    def with_related_to(self, v: str) -> TagMapEvent:
        return replace(self, related_to=v)

    def with_mappings(self, v: list[TagMapping]) -> TagMapEvent:
        return replace(self, mappings=v)


def _bind_TagMapEvent(v: Value) -> TagMapEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_recorded_at = v.field("recorded_at")
    f_kind = v.field("kind")
    f_related_to = v.field("related_to")
    f_mappings = v.field("mappings")
    return TagMapEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        recorded_at=(f_recorded_at[0].datetime()[0] if f_recorded_at[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        related_to=(f_related_to[0].string()[0] if f_related_to[1] else ""),
        mappings=([_bind_TagMapping(e) for e in f_mappings[0].items()] if f_mappings[1] else []),
    )


@dataclass(frozen=True, kw_only=True)
class ReleaseCommitRemapEvent:
    """Frozen typed binding of the "ReleaseCommitRemapEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    recorded_at: str
    kind: str
    related_to: str
    rewrite: str
    mappings: list[ReleaseCommitMapping]

    def with_format_version(self, v: int) -> ReleaseCommitRemapEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> ReleaseCommitRemapEvent:
        return replace(self, id=v)

    def with_recorded_at(self, v: str) -> ReleaseCommitRemapEvent:
        return replace(self, recorded_at=v)

    def with_kind(self, v: str) -> ReleaseCommitRemapEvent:
        return replace(self, kind=v)

    def with_related_to(self, v: str) -> ReleaseCommitRemapEvent:
        return replace(self, related_to=v)

    def with_rewrite(self, v: str) -> ReleaseCommitRemapEvent:
        return replace(self, rewrite=v)

    def with_mappings(self, v: list[ReleaseCommitMapping]) -> ReleaseCommitRemapEvent:
        return replace(self, mappings=v)


def _bind_ReleaseCommitRemapEvent(v: Value) -> ReleaseCommitRemapEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_recorded_at = v.field("recorded_at")
    f_kind = v.field("kind")
    f_related_to = v.field("related_to")
    f_rewrite = v.field("rewrite")
    f_mappings = v.field("mappings")
    return ReleaseCommitRemapEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        recorded_at=(f_recorded_at[0].datetime()[0] if f_recorded_at[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        related_to=(f_related_to[0].string()[0] if f_related_to[1] else ""),
        rewrite=(f_rewrite[0].string()[0] if f_rewrite[1] else ""),
        mappings=([_bind_ReleaseCommitMapping(e) for e in f_mappings[0].items()] if f_mappings[1] else []),
    )


@dataclass(frozen=True, kw_only=True)
class DepartedGlobsEvent:
    """Frozen typed binding of the "DepartedGlobsEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    recorded_at: str
    kind: str
    related_to: str
    globs: list[str]
    destination: TransitionRecordEndpoint

    def with_format_version(self, v: int) -> DepartedGlobsEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> DepartedGlobsEvent:
        return replace(self, id=v)

    def with_recorded_at(self, v: str) -> DepartedGlobsEvent:
        return replace(self, recorded_at=v)

    def with_kind(self, v: str) -> DepartedGlobsEvent:
        return replace(self, kind=v)

    def with_related_to(self, v: str) -> DepartedGlobsEvent:
        return replace(self, related_to=v)

    def with_globs(self, v: list[str]) -> DepartedGlobsEvent:
        return replace(self, globs=v)

    def with_destination(self, v: TransitionRecordEndpoint) -> DepartedGlobsEvent:
        return replace(self, destination=v)


def _bind_DepartedGlobsEvent(v: Value) -> DepartedGlobsEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_recorded_at = v.field("recorded_at")
    f_kind = v.field("kind")
    f_related_to = v.field("related_to")
    f_globs = v.field("globs")
    f_destination = v.field("destination")
    return DepartedGlobsEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        recorded_at=(f_recorded_at[0].datetime()[0] if f_recorded_at[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        related_to=(f_related_to[0].string()[0] if f_related_to[1] else ""),
        globs=([e.string()[0] for e in f_globs[0].items()] if f_globs[1] else []),
        destination=(_bind_TransitionRecordEndpoint(f_destination[0]) if f_destination[1] else None),
    )


@dataclass(frozen=True, kw_only=True)
class BoundaryAliasEvent:
    """Frozen typed binding of the "BoundaryAliasEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    recorded_at: str
    kind: str
    related_to: str
    aliases: list[BoundaryAlias]

    def with_format_version(self, v: int) -> BoundaryAliasEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> BoundaryAliasEvent:
        return replace(self, id=v)

    def with_recorded_at(self, v: str) -> BoundaryAliasEvent:
        return replace(self, recorded_at=v)

    def with_kind(self, v: str) -> BoundaryAliasEvent:
        return replace(self, kind=v)

    def with_related_to(self, v: str) -> BoundaryAliasEvent:
        return replace(self, related_to=v)

    def with_aliases(self, v: list[BoundaryAlias]) -> BoundaryAliasEvent:
        return replace(self, aliases=v)


def _bind_BoundaryAliasEvent(v: Value) -> BoundaryAliasEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_recorded_at = v.field("recorded_at")
    f_kind = v.field("kind")
    f_related_to = v.field("related_to")
    f_aliases = v.field("aliases")
    return BoundaryAliasEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        recorded_at=(f_recorded_at[0].datetime()[0] if f_recorded_at[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        related_to=(f_related_to[0].string()[0] if f_related_to[1] else ""),
        aliases=([_bind_BoundaryAlias(e) for e in f_aliases[0].items()] if f_aliases[1] else []),
    )


@dataclass(frozen=True, kw_only=True)
class IdentityTransitionEvent:
    """Frozen typed binding of the "IdentityTransitionEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    recorded_at: str
    kind: str
    related_to: str
    facet: str
    old: str
    new: str
    effective_version: str

    def with_format_version(self, v: int) -> IdentityTransitionEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> IdentityTransitionEvent:
        return replace(self, id=v)

    def with_recorded_at(self, v: str) -> IdentityTransitionEvent:
        return replace(self, recorded_at=v)

    def with_kind(self, v: str) -> IdentityTransitionEvent:
        return replace(self, kind=v)

    def with_related_to(self, v: str) -> IdentityTransitionEvent:
        return replace(self, related_to=v)

    def with_facet(self, v: str) -> IdentityTransitionEvent:
        return replace(self, facet=v)

    def with_old(self, v: str) -> IdentityTransitionEvent:
        return replace(self, old=v)

    def with_new(self, v: str) -> IdentityTransitionEvent:
        return replace(self, new=v)

    def with_effective_version(self, v: str) -> IdentityTransitionEvent:
        return replace(self, effective_version=v)


def _bind_IdentityTransitionEvent(v: Value) -> IdentityTransitionEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_recorded_at = v.field("recorded_at")
    f_kind = v.field("kind")
    f_related_to = v.field("related_to")
    f_facet = v.field("facet")
    f_old = v.field("old")
    f_new = v.field("new")
    f_effective_version = v.field("effective_version")
    return IdentityTransitionEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        recorded_at=(f_recorded_at[0].datetime()[0] if f_recorded_at[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        related_to=(f_related_to[0].string()[0] if f_related_to[1] else ""),
        facet=(f_facet[0].string()[0] if f_facet[1] else ""),
        old=(f_old[0].string()[0] if f_old[1] else ""),
        new=(f_new[0].string()[0] if f_new[1] else ""),
        effective_version=(f_effective_version[0].string()[0] if f_effective_version[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class PromotionSplitMapEvent:
    """Frozen typed binding of the "PromotionSplitMapEvent" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    id: str
    recorded_at: str
    kind: str
    related_to: str
    subtree_path: str
    mirror_remote: str
    promoted_version: str
    mappings: list[SplitMapping]

    def with_format_version(self, v: int) -> PromotionSplitMapEvent:
        return replace(self, format_version=v)

    def with_id(self, v: str) -> PromotionSplitMapEvent:
        return replace(self, id=v)

    def with_recorded_at(self, v: str) -> PromotionSplitMapEvent:
        return replace(self, recorded_at=v)

    def with_kind(self, v: str) -> PromotionSplitMapEvent:
        return replace(self, kind=v)

    def with_related_to(self, v: str) -> PromotionSplitMapEvent:
        return replace(self, related_to=v)

    def with_subtree_path(self, v: str) -> PromotionSplitMapEvent:
        return replace(self, subtree_path=v)

    def with_mirror_remote(self, v: str) -> PromotionSplitMapEvent:
        return replace(self, mirror_remote=v)

    def with_promoted_version(self, v: str) -> PromotionSplitMapEvent:
        return replace(self, promoted_version=v)

    def with_mappings(self, v: list[SplitMapping]) -> PromotionSplitMapEvent:
        return replace(self, mappings=v)


def _bind_PromotionSplitMapEvent(v: Value) -> PromotionSplitMapEvent | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_id = v.field("id")
    f_recorded_at = v.field("recorded_at")
    f_kind = v.field("kind")
    f_related_to = v.field("related_to")
    f_subtree_path = v.field("subtree_path")
    f_mirror_remote = v.field("mirror_remote")
    f_promoted_version = v.field("promoted_version")
    f_mappings = v.field("mappings")
    return PromotionSplitMapEvent(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        recorded_at=(f_recorded_at[0].datetime()[0] if f_recorded_at[1] else ""),
        kind=(f_kind[0].string()[0] if f_kind[1] else ""),
        related_to=(f_related_to[0].string()[0] if f_related_to[1] else ""),
        subtree_path=(f_subtree_path[0].string()[0] if f_subtree_path[1] else ""),
        mirror_remote=(f_mirror_remote[0].string()[0] if f_mirror_remote[1] else ""),
        promoted_version=(f_promoted_version[0].string()[0] if f_promoted_version[1] else ""),
        mappings=([_bind_SplitMapping(e) for e in f_mappings[0].items()] if f_mappings[1] else []),
    )


