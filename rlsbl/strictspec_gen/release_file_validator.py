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
    "release-file.schema.toml": "# strictspec schema for the rlsbl release file (.rlsbl/releases/unreleased.toml\n# while it is editable, .rlsbl/releases/v{X.Y.Z}.toml once archived).\n# Source of truth: rlsbl/release_file.py (ReleaseConfig, _validate_release_config).\n# This validates the raw DOCUMENT SHAPE at read time; rlsbl keeps a small set of\n# consumer-native refinements (whitespace-only description, the flutter\n# required-mode gate) as native checks -- see rlsbl/release_file.py.\n#\n# AUTHORITY. The archived release file is the authoritative record of WHICH\n# COMMIT AND TREE a version shipped from: the `candidate_sha` and `tree_hashes`\n# anchor fields are written by the release flow at the archive step, from the\n# commit its own CI verified, and the archive is read-only from that instant.\n# The `<!-- rlsbl-ci-sha: ... -->` marker in the GitHub Release body is a\n# PROJECTION of `candidate_sha` for CI's publish check to parse -- it restates\n# the anchor for a consumer that cannot read the repository, and it never\n# outranks it. When the two disagree, the archive is right and the Release body\n# is stale.\n#\n# The anchor fields are optional in the SHAPE because they only exist once the\n# flow has authored them: the editable pre-release file never carries them (a\n# hand-authored anchor is refused natively at release validation, see\n# rlsbl/commands/release/validate.py), and archives written before anchoring\n# existed carry neither.\n\nname = \"rlsbl-release-file\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"toml\"\nrole = \"schema\"\nroot = \"ReleaseConfig\"\ntargets = [\"python\"]\ndescription = \"A single-project rlsbl release descriptor: bump type, target selection, and release prose.\"\n\n[types.ReleaseConfig]\ntype = \"record\"\n\n[types.ReleaseConfig.fields.bump]\ntype = \"enum\"\nrequired = true\nvalues = [\"patch\", \"minor\", \"major\", \"infra\", \"prerelease\"]\ndescription = \"Version bump type (VALID_BUMP_TYPES).\"\n\n[types.ReleaseConfig.fields.include]\ntype = \"array\"\nrequired = true\ndescription = \"Target names to release. May be empty; disjoint from exclude.\"\n[types.ReleaseConfig.fields.include.item]\ntype = \"string\"\n\n[types.ReleaseConfig.fields.exclude]\ntype = \"array\"\nrequired = true\ndescription = \"Target names to skip. Disjoint from include.\"\n[types.ReleaseConfig.fields.exclude.item]\ntype = \"string\"\n\n[types.ReleaseConfig.fields.targets]\ntype = \"map\"\nrequired = false\nkey_pattern = \"^[a-z][a-z0-9-]*$\"\ndescription = \"Per-target configuration, keyed by target name. Each key must appear in include.\"\n[types.ReleaseConfig.fields.targets.value]\ntype = \"TargetConfig\"\n\n[types.ReleaseConfig.fields.description]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Short summary of the release. Whitespace-only is additionally rejected natively.\"\n\n[types.ReleaseConfig.fields.context]\ntype = \"string\"\nrequired = false\ndescription = \"Optional multiline prose explaining why the changes were made.\"\n\n[types.ReleaseConfig.fields.preid]\ntype = \"string\"\nrequired = false\ndescription = \"Pre-release identifier. strictspec owns only its string type; membership in VALID_PREIDS, the empty-string-means-unset semantics, and the infra/stable couplings are consumer-native (rlsbl treats preid = \\\"\\\" and whitespace as unset, which an enum cannot express).\"\n\n[types.ReleaseConfig.fields.blog]\ntype = \"boolean\"\nrequired = false\ndescription = \"Whether to generate a blog post for this release.\"\n\n[types.ReleaseConfig.fields.candidate_sha]\ntype = \"GitObjectHash\"\nrequired = false\ndescription = \"AUTHORITATIVE: the commit this version shipped from -- the release candidate whose CI concluded green, which is also the commit the version tag points at. Written by the release flow into the archived v{X.Y.Z}.toml before the archive is locked read-only; never authored by hand, and absent from the editable unreleased.toml (release validation refuses one that carries it). The rlsbl-ci-sha marker in the GitHub Release body is this field's projection for CI to parse, not a second source.\"\n\n[types.ReleaseConfig.fields.tree_hashes]\ntype = \"map\"\nrequired = false\nkey_pattern = \"^(\\\\.|[A-Za-z0-9._][A-Za-z0-9._/-]*)$\"\ndescription = \"AUTHORITATIVE: the released content, one git tree object per released path, keyed by the repo-relative path (\\\".\\\" for a whole standalone repository). A standalone release has the single \\\".\\\" entry carrying the root tree of candidate_sha; a workspace releasable has one entry per member directory, because no single git object covers a set of member subtrees -- a per-member table is therefore the honest representation rather than a synthesized hash. Written by the release flow alongside candidate_sha and read-only from the same instant.\"\n[types.ReleaseConfig.fields.tree_hashes.value]\ntype = \"GitObjectHash\"\n\n# NOTE: the preid couplings (preid == \"stable\" requires bump == \"prerelease\";\n# bump == \"infra\" forbids preid) are consumer-native, not schema constraints:\n# rlsbl treats preid = \"\" / whitespace as UNSET, so a present-but-empty preid\n# must not trip forbidden-when. Enforced natively in _bind_release_config.\n\n# Every key of targets must appear in include.\n[[types.ReleaseConfig.constraints]]\nform = \"intra-document-references\"\nreference = \"targets\"\nresolves_into = \"include\"\nresolves_by = \"map-key\"\ndescription = \"A [targets.<name>] section is only valid when <name> is in include.\"\n\n# include and exclude must be element-disjoint.\n[[types.ReleaseConfig.constraints]]\nform = \"collections-disjoint\"\nleft = \"include\"\nright = \"exclude\"\nnormalization = \"none\"\ndescription = \"No element appears in both include and exclude.\"\n\n# GitObjectHash -- a git object name, commit or tree. Same shape as the lineage\n# schema's GitSha, and for the same reason.\n[types.GitObjectHash]\ntype = \"string\"\nregex = \"^[0-9a-f]{7,40}$\"\ndescription = \"A git object hash, commit or tree, abbreviated (>= 7) or full (40). The release flow records whatever `git rev-parse` answered, which is the full form; the shorter bound exists so a record made from an abbreviation is readable rather than rejected. Resolution against the object database is rlsbl-native, not a shape check.\"\n\n# TargetConfig -- per-target overrides (only mode is recognized today).\n[types.TargetConfig]\ntype = \"record\"\n[types.TargetConfig.fields.mode]\ntype = \"enum\"\nrequired = false\nvalues = [\"ota\", \"build\"]\ndescription = \"Flutter delivery mode (VALID_TARGET_MODES). The flutter required-mode gate is native.\"\n",
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


