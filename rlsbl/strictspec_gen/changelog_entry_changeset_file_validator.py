# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.1.0
# schema:              rlsbl-changelog-entry-changeset-file (format_version 1)
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
GENERATED_BY = "0.1.0"
SCHEMA_FORMAT_VERSION = 1

# _EMBEDDED_SCHEMA carries the compiled schema (and its imported type-definition
# files and scalar manifest) so the validator is self-contained and does no IO.
_EMBEDDED_SCHEMA = {
    "changelog-entry-changeset-file-mode.schema.toml": "# strictspec schema for one rlsbl JSONL changelog line -- CHANGESET-FILE mode.\n# Source of truth: rlsbl/changelog/schema.py (ChangelogEntry, validate_schema\n# with coverage_unit=\"changeset-file\"). Sibling of the commit-mode schema; the\n# consumer selects which schema to validate against from .rlsbl/config.json's\n# coverage_unit (explicit mode selection).\n#\n# The ONLY structural differences from commit mode: commits is FORBIDDEN, id is\n# REQUIRED. Everything else (per-line format_version, user_facing,\n# description/type conditional-required, release_type, packages) is identical.\n#\n# commits is FORBIDDEN in changeset-file mode. strictspec has no positive\n# \"this field must always be absent\" construct: a forbidden field is simply one\n# the closed record does NOT declare, so a present commits is an unknown-key\n# error. rlsbl renders that verdict as \"commits must be empty in changeset-file\n# mode\" (validate_schema).\n\nname = \"rlsbl-changelog-entry-changeset-file\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"jsonl\"\nrole = \"schema\"\nroot = \"ChangelogEntry\"\ntargets = [\"python\"]\ndescription = \"One JSONL changelog line in changeset-file mode: commits forbidden, id required.\"\n\n[types.ChangelogEntry]\ntype = \"record\"\n\n[types.ChangelogEntry.fields.format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"Per-line format_version (JSONL per-line gate).\"\n\n[types.ChangelogEntry.fields.user_facing]\ntype = \"boolean\"\nrequired = true\ndescription = \"Whether this entry appears in the published changelog.\"\n\n[types.ChangelogEntry.fields.description]\ntype = \"string\"\nrequired = false\nnon_empty = true\ndescription = \"Markdown one-liner. REQUIRED WHEN user_facing == true (see constraint).\"\n\n[types.ChangelogEntry.fields.type]\ntype = \"enum\"\nrequired = false\nvalues = [\"feature\", \"fix\", \"breaking\"]\ndescription = \"Entry type. REQUIRED WHEN user_facing == true (see constraint).\"\n\n[types.ChangelogEntry.fields.release_type]\ntype = \"enum\"\nrequired = false\nvalues = [\"ota\", \"build\"]\ndescription = \"Flutter target release channel. Optional, unconstrained by user_facing.\"\n\n[types.ChangelogEntry.fields.id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Stable identifier. REQUIRED in changeset-file mode (validate_schema: \\\"id is required in changeset-file mode\\\").\"\n\n[types.ChangelogEntry.fields.packages]\ntype = \"array\"\nrequired = false\n[types.ChangelogEntry.fields.packages.item]\ntype = \"string\"\n\n# description required when user_facing == true\n[[types.ChangelogEntry.constraints]]\nform = \"conditional-required\"\nfield = \"description\"\nwhen = { field = \"user_facing\", predicate = \"equals\", value = true }\n\n# type required when user_facing == true\n[[types.ChangelogEntry.constraints]]\nform = \"conditional-required\"\nfield = \"type\"\nwhen = { field = \"user_facing\", predicate = \"equals\", value = true }\n",
}
_EMBEDDED_MAIN_FILE = "changelog-entry-changeset-file-mode.schema.toml"

# Version pairing: generated code and runtime must be the same release. This runs
# at import, so a skewed runtime hard-errors before any validation is attempted.
strictspec.require_runtime_version(GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[ChangelogEntry | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[ChangelogEntry | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the phase-2
    constraint vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_ChangelogEntry(v), result.diagnostics


def validate_value(v: Value) -> tuple[ChangelogEntry | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_ChangelogEntry(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class ChangelogEntry:
    """Frozen typed binding of the "ChangelogEntry" record. Immutable; use with_* for
    copy-on-write.
    """

    format_version: int
    user_facing: bool
    description: str
    type: str
    release_type: str
    id: str
    packages: list[str]

    def with_format_version(self, v: int) -> ChangelogEntry:
        return replace(self, format_version=v)

    def with_user_facing(self, v: bool) -> ChangelogEntry:
        return replace(self, user_facing=v)

    def with_description(self, v: str) -> ChangelogEntry:
        return replace(self, description=v)

    def with_type(self, v: str) -> ChangelogEntry:
        return replace(self, type=v)

    def with_release_type(self, v: str) -> ChangelogEntry:
        return replace(self, release_type=v)

    def with_id(self, v: str) -> ChangelogEntry:
        return replace(self, id=v)

    def with_packages(self, v: list[str]) -> ChangelogEntry:
        return replace(self, packages=v)


def _bind_ChangelogEntry(v: Value) -> ChangelogEntry | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_format_version = v.field("format_version")
    f_user_facing = v.field("user_facing")
    f_description = v.field("description")
    f_type = v.field("type")
    f_release_type = v.field("release_type")
    f_id = v.field("id")
    f_packages = v.field("packages")
    return ChangelogEntry(
        format_version=(f_format_version[0].int()[0] if f_format_version[1] else 0),
        user_facing=(f_user_facing[0].bool()[0] if f_user_facing[1] else False),
        description=(f_description[0].string()[0] if f_description[1] else ""),
        type=(f_type[0].string()[0] if f_type[1] else ""),
        release_type=(f_release_type[0].string()[0] if f_release_type[1] else ""),
        id=(f_id[0].string()[0] if f_id[1] else ""),
        packages=([e.string()[0] for e in f_packages[0].items()] if f_packages[1] else []),
    )


