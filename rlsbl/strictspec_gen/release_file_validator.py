# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.2.3
# schema:              rlsbl-release-file (format_version 1)
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
    "release-file.schema.toml": "# strictspec schema for the rlsbl release file (.rlsbl/releases/unreleased.toml\n# while it is editable, .rlsbl/releases/v{X.Y.Z}.toml once archived).\n# Source of truth: rlsbl/release_file.py (ReleaseConfig, _validate_release_config).\n# This validates the raw DOCUMENT SHAPE at read time; rlsbl keeps a small set of\n# consumer-native refinements (whitespace-only description, the flutter\n# required-mode gate) as native checks -- see rlsbl/release_file.py.\n#\n# AUTHORITY. The archived release file is the authoritative record of WHICH\n# COMMIT AND TREE a version shipped from: the `candidate_sha` and `tree_hashes`\n# release commit fields are written by the release flow at the archive step, from the\n# commit its own CI verified, and rlsbl rewrites that archive only through its\n# own documented unlock paths. (Its local file mode is hygiene, not the\n# guarantee: git records no read-only bit, so a fresh clone's archives are\n# writable.)\n# The `<!-- rlsbl-ci-sha: ... -->` marker in the GitHub Release body is a\n# PROJECTION of `candidate_sha` for CI's publish check to parse -- it restates\n# the release commit for a consumer that cannot read the repository, and it never\n# outranks it. When the two disagree, the archive is right and the Release body\n# is stale.\n#\n# The release commit fields are optional in the SHAPE because they only exist once the\n# flow has authored them: the editable pre-release file never carries them (a\n# hand-authored release commit is refused natively at release validation, see\n# rlsbl/commands/release/validate.py), and archives written before release-commit recording\n# existed carry neither.\n#\n# THE VERSION-FATE MODEL. An ARCHIVE is in exactly one of three states, and\n# every reader dispatches on which:\n#\n#   recorded         candidate_sha + tree_hashes -- it shipped, from a known commit\n#   unrecoverable    unrecoverable = true        -- it shipped, from a commit nothing can name\n#   never_released   never_released = true       -- the version NUMBER exists, no release does\n#\n# The schema enforces the EXCLUSION half of that rule (no document carries two\n# states) and leaves the \"exactly one\" half to the archive readers, because the\n# same schema also validates the EDITABLE unreleased.toml, whose correct state\n# is none of the three. `rlsbl.release_record.read_entry` is where an archive in\n# none of them is a hard error.\n#\n# Exclusion is judged on PRESENCE, not on the value: rlsbl writes a marker only\n# when it is true (see write_archived_release_file), so `unrecoverable = false`\n# beside a candidate_sha is a hand-authored document rather than a state rlsbl\n# produced, and the same refusal covers it.\n\nname = \"rlsbl-release-file\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"toml\"\nrole = \"schema\"\nroot = \"ReleaseConfig\"\ntargets = [\"python\"]\ndescription = \"A single-project rlsbl release descriptor: bump type, target selection, and release prose.\"\n\n[types.ReleaseConfig]\ntype = \"record\"\n\n[types.ReleaseConfig.fields.bump]\ntype = \"enum\"\nrequired = true\nvalues = [\"patch\", \"minor\", \"major\", \"infra\", \"prerelease\"]\ndescription = \"Version bump type (VALID_BUMP_TYPES).\"\n\n[types.ReleaseConfig.fields.include]\ntype = \"array\"\nrequired = true\ndescription = \"Target names to release. May be empty; disjoint from exclude.\"\n[types.ReleaseConfig.fields.include.item]\ntype = \"string\"\n\n[types.ReleaseConfig.fields.exclude]\ntype = \"array\"\nrequired = true\ndescription = \"Target names to skip. Disjoint from include.\"\n[types.ReleaseConfig.fields.exclude.item]\ntype = \"string\"\n\n[types.ReleaseConfig.fields.targets]\ntype = \"map\"\nrequired = false\nkey_pattern = \"^[a-z][a-z0-9-]*$\"\ndescription = \"Per-target configuration, keyed by target name. Each key must appear in include.\"\n[types.ReleaseConfig.fields.targets.value]\ntype = \"TargetConfig\"\n\n[types.ReleaseConfig.fields.description]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Short summary of the release. Whitespace-only is additionally rejected natively.\"\n\n[types.ReleaseConfig.fields.context]\ntype = \"string\"\nrequired = false\ndescription = \"Optional multiline prose explaining why the changes were made.\"\n\n[types.ReleaseConfig.fields.preid]\ntype = \"string\"\nrequired = false\ndescription = \"Pre-release identifier. strictspec owns only its string type; membership in VALID_PREIDS, the empty-string-means-unset semantics, and the infra/stable couplings are consumer-native (rlsbl treats preid = \\\"\\\" and whitespace as unset, which an enum cannot express).\"\n\n[types.ReleaseConfig.fields.blog]\ntype = \"boolean\"\nrequired = false\ndescription = \"Whether to generate a blog post for this release.\"\n\n[types.ReleaseConfig.fields.candidate_sha]\ntype = \"GitObjectHash\"\nrequired = false\ndescription = \"AUTHORITATIVE: the commit this version shipped from -- the release candidate whose CI concluded green, which is also the commit the version tag points at. Written by the release flow into the archived v{X.Y.Z}.toml at the archive step; never authored by hand, and absent from the editable unreleased.toml (release validation refuses one that carries it). The rlsbl-ci-sha marker in the GitHub Release body is this field's projection for CI to parse, not a second source.\"\n\n[types.ReleaseConfig.fields.tree_hashes]\ntype = \"map\"\nrequired = false\nkey_pattern = \"^(\\\\.|[A-Za-z0-9._][A-Za-z0-9._/-]*)$\"\ndescription = \"AUTHORITATIVE: the released content, one git tree object per released path, keyed by the repo-relative path (\\\".\\\" for a whole standalone repository). A standalone release has the single \\\".\\\" entry carrying the root tree of candidate_sha; a workspace releasable has one entry per member directory, because no single git object covers a set of member subtrees -- a per-member table is therefore the honest representation rather than a synthesized hash. Written by the release flow alongside candidate_sha, at the same archive step and under the same rule: rlsbl rewrites the archive only through its own documented unlock paths.\"\n[types.ReleaseConfig.fields.tree_hashes.value]\ntype = \"GitObjectHash\"\n\n[types.ReleaseConfig.fields.unrecoverable]\ntype = \"boolean\"\nrequired = false\ndescription = \"A PERMANENT RECORD OF FAILED RECOVERY, written only by the backfill pass and only when set to true: this version SHIPPED, but the commit it shipped from could not be recovered from any source -- no tag under any recognized scheme (v{X.Y.Z}, name@v{X.Y.Z}, path/v{X.Y.Z}) and no version-bump commit in history. It is the alternative to silently leaving an archive with no release commit. The release flow never writes it (a flow that is releasing knows its own candidate), and it is stripped along with the release commit when `release undo` restores an archive as the editable unreleased.toml. Distinct from never_released: this version HAS consumers, its refs are real, and only rlsbl's knowledge of where it came from is lost.\"\n\n[types.ReleaseConfig.fields.never_released]\ntype = \"boolean\"\nrequired = false\ndescription = \"THE VERSION NUMBER EXISTS BUT NO RELEASE DOES, written only when set to true: a phantom tag's version, or a version claimed and abandoned. It is not a failed recovery -- there is nothing to recover, because nothing was ever published under this number. Every read that asks what this project RELEASED skips such a version: it is not the latest release, it does not bound the unreleased range, `release undo` does not select it, its refs are never demanded as missing, and `release reconcile` never plans a deletion of a tag that happens to carry its name. Its changelog section is still rendered -- a phantom version can have finalized changelog files, and hiding them would lose the record -- annotated as never released.\"\n\n[types.ReleaseConfig.fields.shipped_as]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"The historical tag spelling this version ACTUALLY shipped under, when it differs from the tag scheme in effect today (e.g. \\\"strictcli@v0.12.0\\\" on a version now tagged \\\"v0.12.0\\\", or \\\"auth-gateway/v0.1.0\\\" on one absorbed under a workspace scheme). One string per archive: a version shipped once, under one name. Orthogonal to the three fate states -- legal on a recorded and on an unrecoverable archive, refused on a never-released one, which shipped under nothing.\"\n\n# NOTE: the preid couplings (preid == \"stable\" requires bump == \"prerelease\";\n# bump == \"infra\" forbids preid) are consumer-native, not schema constraints:\n# rlsbl treats preid = \"\" / whitespace as UNSET, so a present-but-empty preid\n# must not trip forbidden-when. Enforced natively in _bind_release_config.\n\n# Every key of targets must appear in include.\n[[types.ReleaseConfig.constraints]]\nform = \"intra-document-references\"\nreference = \"targets\"\nresolves_into = \"include\"\nresolves_by = \"map-key\"\ndescription = \"A [targets.<name>] section is only valid when <name> is in include.\"\n\n# include and exclude must be element-disjoint.\n[[types.ReleaseConfig.constraints]]\nform = \"collections-disjoint\"\nleft = \"include\"\nright = \"exclude\"\nnormalization = \"none\"\ndescription = \"No element appears in both include and exclude.\"\n\n# The version-fate states exclude one another. Zero present is the editable\n# unreleased.toml and is legal here; an ARCHIVE with zero is refused by\n# rlsbl.release_record.read_entry, which is the only reader that knows it is\n# looking at an archive.\n[[types.ReleaseConfig.constraints]]\nform = \"mutual-exclusion\"\nfields = [\"candidate_sha\", \"unrecoverable\", \"never_released\"]\ndescription = \"A version has one fate: recorded, unrecoverable, or never released.\"\n\n# tree_hashes is the other half of the release commit, so it is forbidden beside\n# either marker even in a document that omits candidate_sha.\n[[types.ReleaseConfig.constraints]]\nform = \"forbidden-when\"\nfield = \"tree_hashes\"\nwhen = { field = \"unrecoverable\", predicate = \"present\" }\ndescription = \"An unrecoverable version has no released trees to record.\"\n\n[[types.ReleaseConfig.constraints]]\nform = \"forbidden-when\"\nfield = \"tree_hashes\"\nwhen = { field = \"never_released\", predicate = \"present\" }\ndescription = \"A version that was never released released no trees.\"\n\n# A version that was never released shipped under no tag, so there is no\n# historical spelling for it to have shipped under.\n[[types.ReleaseConfig.constraints]]\nform = \"forbidden-when\"\nfield = \"shipped_as\"\nwhen = { field = \"never_released\", predicate = \"present\" }\ndescription = \"A never-released version shipped under nothing.\"\n\n# GitObjectHash -- a git object name, commit or tree. Same shape as the transition record\n# schema's GitSha, and for the same reason.\n[types.GitObjectHash]\ntype = \"string\"\nregex = \"^[0-9a-f]{7,40}$\"\ndescription = \"A git object hash, commit or tree, abbreviated (>= 7) or full (40). The release flow records whatever `git rev-parse` answered, which is the full form; the shorter bound exists so a record made from an abbreviation is readable rather than rejected. Resolution against the object database is rlsbl-native, not a shape check.\"\n\n# TargetConfig -- per-target overrides (only mode is recognized today).\n[types.TargetConfig]\ntype = \"record\"\n[types.TargetConfig.fields.mode]\ntype = \"enum\"\nrequired = false\nvalues = [\"ota\", \"build\"]\ndescription = \"Flutter delivery mode (VALID_TARGET_MODES). The flutter required-mode gate is native.\"\n",
}
_EMBEDDED_MAIN_FILE = "release-file.schema.toml"

# Version pairing: generated code and runtime must be the same release. This runs
# at import, so a skewed runtime hard-errors before any validation is attempted.
strictspec.require_runtime_version(GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[ReleaseConfig | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[ReleaseConfig | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the phase-2
    constraint vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_ReleaseConfig(v), result.diagnostics


def validate_value(v: Value) -> tuple[ReleaseConfig | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_ReleaseConfig(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class ReleaseConfig:
    """Frozen typed binding of the "ReleaseConfig" record. Immutable; use with_* for
    copy-on-write.
    """

    bump: str
    include: list[str]
    exclude: list[str]
    targets: Value
    description: str
    context: str
    preid: str
    blog: bool
    candidate_sha: str
    tree_hashes: Value
    unrecoverable: bool
    never_released: bool
    shipped_as: str

    def with_bump(self, v: str) -> ReleaseConfig:
        return replace(self, bump=v)

    def with_include(self, v: list[str]) -> ReleaseConfig:
        return replace(self, include=v)

    def with_exclude(self, v: list[str]) -> ReleaseConfig:
        return replace(self, exclude=v)

    def with_targets(self, v: Value) -> ReleaseConfig:
        return replace(self, targets=v)

    def with_description(self, v: str) -> ReleaseConfig:
        return replace(self, description=v)

    def with_context(self, v: str) -> ReleaseConfig:
        return replace(self, context=v)

    def with_preid(self, v: str) -> ReleaseConfig:
        return replace(self, preid=v)

    def with_blog(self, v: bool) -> ReleaseConfig:
        return replace(self, blog=v)

    def with_candidate_sha(self, v: str) -> ReleaseConfig:
        return replace(self, candidate_sha=v)

    def with_tree_hashes(self, v: Value) -> ReleaseConfig:
        return replace(self, tree_hashes=v)

    def with_unrecoverable(self, v: bool) -> ReleaseConfig:
        return replace(self, unrecoverable=v)

    def with_never_released(self, v: bool) -> ReleaseConfig:
        return replace(self, never_released=v)

    def with_shipped_as(self, v: str) -> ReleaseConfig:
        return replace(self, shipped_as=v)


def _bind_ReleaseConfig(v: Value) -> ReleaseConfig | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_bump = v.field("bump")
    f_include = v.field("include")
    f_exclude = v.field("exclude")
    f_targets = v.field("targets")
    f_description = v.field("description")
    f_context = v.field("context")
    f_preid = v.field("preid")
    f_blog = v.field("blog")
    f_candidate_sha = v.field("candidate_sha")
    f_tree_hashes = v.field("tree_hashes")
    f_unrecoverable = v.field("unrecoverable")
    f_never_released = v.field("never_released")
    f_shipped_as = v.field("shipped_as")
    return ReleaseConfig(
        bump=(f_bump[0].string()[0] if f_bump[1] else ""),
        include=([e.string()[0] for e in f_include[0].items()] if f_include[1] else []),
        exclude=([e.string()[0] for e in f_exclude[0].items()] if f_exclude[1] else []),
        targets=(f_targets[0] if f_targets[1] else Value(None, "json")),
        description=(f_description[0].string()[0] if f_description[1] else ""),
        context=(f_context[0].string()[0] if f_context[1] else ""),
        preid=(f_preid[0].string()[0] if f_preid[1] else ""),
        blog=(f_blog[0].bool()[0] if f_blog[1] else False),
        candidate_sha=(f_candidate_sha[0].string()[0] if f_candidate_sha[1] else ""),
        tree_hashes=(f_tree_hashes[0] if f_tree_hashes[1] else Value(None, "json")),
        unrecoverable=(f_unrecoverable[0].bool()[0] if f_unrecoverable[1] else False),
        never_released=(f_never_released[0].bool()[0] if f_never_released[1] else False),
        shipped_as=(f_shipped_as[0].string()[0] if f_shipped_as[1] else ""),
    )


@dataclass(frozen=True, kw_only=True)
class TargetConfig:
    """Frozen typed binding of the "TargetConfig" record. Immutable; use with_* for
    copy-on-write.
    """

    mode: str

    def with_mode(self, v: str) -> TargetConfig:
        return replace(self, mode=v)


def _bind_TargetConfig(v: Value) -> TargetConfig | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_mode = v.field("mode")
    return TargetConfig(
        mode=(f_mode[0].string()[0] if f_mode[1] else ""),
    )


