# strictspec generated validator. DO NOT EDIT.
#
# strictspec generator: 0.1.0
# schema:              rlsbl-strictspec-adjudication (format_version 1)
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
    "adjudication.schema.toml": "# strictspec schema for a diff-certificate ADJUDICATION file consumed by\n# rlsbl's certificate deploy gate. Source of truth: strictspec\n# spec/appendix-certificates.md Part B. A no-corpus consumer commits this file\n# to discharge otherwise-unsupported claims. It is itself a gated strictspec\n# TOML document (it carries format_version).\n\nname = \"rlsbl-strictspec-adjudication\"\nmeta_version = 1\nformat_version = 1\ndocument_syntax = \"toml\"\nrole = \"schema\"\nroot = \"Adjudication\"\ntargets = [\"python\"]\ndescription = \"A committed adjudication file that discharges unsupported diff-certificate claims for a no-corpus consumer.\"\n\n[types.Adjudication]\ntype = \"record\"\n\n[types.Adjudication.fields.schema_id]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The schema whose migration is being adjudicated.\"\n\n[types.Adjudication.fields.old_format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"N.\"\n\n[types.Adjudication.fields.new_format_version]\ntype = \"integer\"\nrequired = true\ndescription = \"N+1.\"\n\n[types.Adjudication.fields.adjudications]\ntype = \"array\"\nrequired = true\nmin_len = 1\ndescription = \"One entry per unsupported claim being discharged.\"\n[types.Adjudication.fields.adjudications.item]\ntype = \"AdjudicationEntry\"\n\n[types.AdjudicationEntry]\ntype = \"record\"\n\n[types.AdjudicationEntry.fields.claim_kind]\ntype = \"enum\"\nrequired = true\nvalues = [\"flip-scan\", \"migrate-round-trip-soundness\", \"migrate-round-trip-completeness\", \"down-taxonomy\"]\ndescription = \"Which claim is discharged (matches certificate claim kind).\"\n\n[types.AdjudicationEntry.fields.scope]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The precise scope of the claim being discharged.\"\n\n[types.AdjudicationEntry.fields.justification]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"The signed manual justification -- why this claim is safe absent corpus evidence.\"\n\n[types.AdjudicationEntry.fields.author]\ntype = \"string\"\nrequired = true\nnon_empty = true\ndescription = \"Who adjudicated.\"\n\n[types.AdjudicationEntry.fields.date]\ntype = \"date\"\nrequired = true\ndescription = \"When (RFC 3339 full-date).\"\n",
}
_EMBEDDED_MAIN_FILE = "adjudication.schema.toml"

# Version pairing: generated code and runtime must be the same release. This runs
# at import, so a skewed runtime hard-errors before any validation is attempted.
strictspec.require_runtime_version(GENERATED_BY)
_program = strictspec.compile_embedded(_EMBEDDED_SCHEMA, _EMBEDDED_MAIN_FILE)


def validate_bytes(input: bytes, syntax: str) -> tuple[Adjudication | None, tuple[Diagnostic, ...]]:
    """RAW-BYTES entry point: lossless parse of input in the given syntax
    ("json" | "toml" | "jsonl"), then validate. Returns the typed root value
    (None when any diagnostic fired) and the ordered diagnostics.
    """
    return validate_bytes_with_evidence(input, syntax, None)


def validate_bytes_with_evidence(input: bytes, syntax: str, evidence: dict | None) -> tuple[Adjudication | None, tuple[Diagnostic, ...]]:
    """validate_bytes plus cross-document resolver evidence for the phase-2
    constraint vocabulary.
    """
    result = _program.validate_with_evidence(input, syntax, evidence)
    if not result.valid:
        return None, result.diagnostics
    v = strictspec.load_value(input, syntax)
    return _bind_Adjudication(v), result.diagnostics


def validate_value(v: Value) -> tuple[Adjudication | None, tuple[Diagnostic, ...]]:
    """TAGGED-VALUE entry point: validate an already-parsed tagged document value
    (from strictspec.load_value or a typed constructor). Raw untagged dicts are
    never accepted.
    """
    result = _program.validate_value(v)
    if not result.valid:
        return None, result.diagnostics
    return _bind_Adjudication(v), result.diagnostics


@dataclass(frozen=True, kw_only=True)
class Adjudication:
    """Frozen typed binding of the "Adjudication" record. Immutable; use with_* for
    copy-on-write.
    """

    schema_id: str
    old_format_version: int
    new_format_version: int
    adjudications: list[AdjudicationEntry]

    def with_schema_id(self, v: str) -> Adjudication:
        return replace(self, schema_id=v)

    def with_old_format_version(self, v: int) -> Adjudication:
        return replace(self, old_format_version=v)

    def with_new_format_version(self, v: int) -> Adjudication:
        return replace(self, new_format_version=v)

    def with_adjudications(self, v: list[AdjudicationEntry]) -> Adjudication:
        return replace(self, adjudications=v)


def _bind_Adjudication(v: Value) -> Adjudication | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_schema_id = v.field("schema_id")
    f_old_format_version = v.field("old_format_version")
    f_new_format_version = v.field("new_format_version")
    f_adjudications = v.field("adjudications")
    return Adjudication(
        schema_id=(f_schema_id[0].string()[0] if f_schema_id[1] else ""),
        old_format_version=(f_old_format_version[0].int()[0] if f_old_format_version[1] else 0),
        new_format_version=(f_new_format_version[0].int()[0] if f_new_format_version[1] else 0),
        adjudications=([_bind_AdjudicationEntry(e) for e in f_adjudications[0].items()] if f_adjudications[1] else []),
    )


@dataclass(frozen=True, kw_only=True)
class AdjudicationEntry:
    """Frozen typed binding of the "AdjudicationEntry" record. Immutable; use with_* for
    copy-on-write.
    """

    claim_kind: str
    scope: str
    justification: str
    author: str
    date: str

    def with_claim_kind(self, v: str) -> AdjudicationEntry:
        return replace(self, claim_kind=v)

    def with_scope(self, v: str) -> AdjudicationEntry:
        return replace(self, scope=v)

    def with_justification(self, v: str) -> AdjudicationEntry:
        return replace(self, justification=v)

    def with_author(self, v: str) -> AdjudicationEntry:
        return replace(self, author=v)

    def with_date(self, v: str) -> AdjudicationEntry:
        return replace(self, date=v)


def _bind_AdjudicationEntry(v: Value) -> AdjudicationEntry | None:
    if v.kind() != strictspec.Kind.RECORD:
        return None
    f_claim_kind = v.field("claim_kind")
    f_scope = v.field("scope")
    f_justification = v.field("justification")
    f_author = v.field("author")
    f_date = v.field("date")
    return AdjudicationEntry(
        claim_kind=(f_claim_kind[0].string()[0] if f_claim_kind[1] else ""),
        scope=(f_scope[0].string()[0] if f_scope[1] else ""),
        justification=(f_justification[0].string()[0] if f_justification[1] else ""),
        author=(f_author[0].string()[0] if f_author[1] else ""),
        date=(f_date[0].datetime()[0] if f_date[1] else ""),
    )


